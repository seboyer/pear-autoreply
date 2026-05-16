"""Generate src/autoreplies/services/airtable_schema.py from the Pear Tracker base.

Unlike `dump_airtable_schema.py` (which dumps the entire base — useful as a
lookup/audit tool), this script emits a *curated* module containing only the
tables and fields this project actually uses. The CURATED dict below is the
contract: a field is in the schema module iff it is named here.

If you need to add a field to the schema, add it here and re-run the script.
Don't edit `airtable_schema.py` by hand.

Usage (from repo root, with venv activated):
    python scripts/generate_airtable_schema.py
    python scripts/generate_airtable_schema.py --base TEST
    python scripts/generate_airtable_schema.py --out path/to/schema.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import keyword
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from autoreplies.config import Settings  # noqa: E402

META_URL = "https://api.airtable.com/v0/meta/bases/{base_id}/tables"

DEFAULT_OUT = PROJECT_ROOT / "src" / "autoreplies" / "services" / "airtable_schema.py"

# Bare strings derive their py_ident via to_py_ident(); tuple form
# ("Display Name", "py_ident_override") uses the explicit identifier.
FieldEntry = str | tuple[str, str]


@dataclass
class TableSpec:
    """Declares which fields a table contributes, and which bases it lives in."""

    fields: list[FieldEntry]
    # These fields are added only when generating for a TEST base.
    test_only_fields: list[FieldEntry] = field(default_factory=list)
    # Which base const-name keys include this table ("PROD", "TEST", "STAGING").
    bases: tuple[str, ...] = ("PROD", "TEST")


# ---------------------------------------------------------------------------
# Curated field list — the project's contract with Airtable.
# Keys are exact Airtable table display names; values are TableSpec instances.
# Add entries here when the autoreplies pipeline needs a new field, then re-run.
# ---------------------------------------------------------------------------

CURATED: dict[str, TableSpec] = {
    "Users": TableSpec(
        fields=[
            "Email",  # primary inbox — prod lookup; prospect→user match; Slack display
            "Type",  # "Agent" / "Admin" / other
            "Name",  # Slack display: "Agent: Jane Doe"
            "Phone",  # prospect→user match
            "Autoreply (Agent)",  # per-agent reply template (PROD, synced — used post-cutover)
            "Autoreply Email (Agent)",  # legacy per-user inbox — the harness polls this
            "Autoreply Enabled (Agent)",  # checkbox: source of truth for "in scope" rows (prod + harness)
        ],
        test_only_fields=[
            (
                "Record ID",
                "source_record_id",
            ),  # synced prod RECORD_ID() — used by H5 diff translator
            # New editable per-agent template field on TEST. Zapier still owns
            # `Autoreply (Agent)` on PROD; at cutover, sales will copy the
            # contents of this field into the production one and we'll wire
            # production to read from `autoreply_agent` again.
            ("Autoreply Test Template (Agent)", "autoreply_test_template"),
        ],
    ),
    "Apartments": TableSpec(
        fields=[
            "Streeteasy",  # URL-based listing-ID match
            "Full Address",  # rapidfuzz address match
            "Apartment",  # Slack display label
        ],
        test_only_fields=[
            ("Record", "source_record_id"),  # synced prod RECORD_ID() — used by H5 diff translator
        ],
    ),
    "Inquiries": TableSpec(
        fields=[
            # NB: `Agent` on Inquiries is a *lookup* through the linked Apartment,
            # not a directly-writable link field. Don't add it here.
            "Name (Form)",  # write — prospect's name
            "Email (Form)",  # write — prospect's email
            "Phone",  # write — prospect's phone (null for Zillow)
            "Message",  # write — prospect's free-text
            "Apartment",  # write — link if matched
            "Apartment (FailSafe)",  # write — raw parsed address (always, for audit)
            "User",  # write — link if existing user matched
            "Method",  # write — constant "Web"
            "Type (Non Website)",  # write — "StreetEasy" or "Zillow"
            "Gmail Message ID (Autoreply)",  # write + idempotency lookup
        ],
    ),
    "Drafts": TableSpec(
        fields=[
            # Per TESTING_HARNESS_PLAN.md § 3 — display names confirmed against test base.
            "Inquiry",
            "Recipient",
            "Subject",
            "Body Plaintext",
            "Body HTML",
            "Source",
            "Parser Used",
            "Template Source",
            "Reply Route",
            "Skipped Reason",
            "Apartment Match Strategy",
            "Apartment Match Confidence",
            "LLM Model",
            "LLM Latency Ms",
            "Would Send At",
            "Notes / Warnings",
            "Gmail Message ID",
            "Sender",
        ],
        bases=("TEST",),
    ),
}


# ---------------------------------------------------------------------------
# Identifier sanitization
# ---------------------------------------------------------------------------

_IDENT_NON_ALNUM = re.compile(r"[^0-9a-zA-Z]+")
_IDENT_LEADING_DIGIT = re.compile(r"^(\d)")


def to_py_ident(name: str) -> str:
    cleaned = _IDENT_NON_ALNUM.sub("_", name).strip("_").lower()
    cleaned = _IDENT_LEADING_DIGIT.sub(r"_\1", cleaned) or "field"
    if keyword.iskeyword(cleaned):
        cleaned += "_"
    return cleaned


def table_class_name(table_name: str) -> str:
    parts = re.split(r"[^0-9a-zA-Z]+", table_name)
    camel = "".join(p[:1].upper() + p[1:] for p in parts if p)
    return f"{camel}Table"


def resolve_field_entry(entry: FieldEntry) -> tuple[str, str]:
    """Return (display_name, py_ident) for a bare string or an override tuple."""
    if isinstance(entry, tuple):
        display_name, py_ident = entry
    else:
        display_name = entry
        py_ident = to_py_ident(entry)
    return display_name, py_ident


# ---------------------------------------------------------------------------
# Metadata fetch
# ---------------------------------------------------------------------------


@dataclass
class TableMeta:
    id: str
    name: str
    # ordered list of (py_ident, airtable_field_id, original_name)
    fields: list[tuple[str, str, str]] = field(default_factory=list)


def fetch_curated_base(
    token: str,
    base_id: str,
    const_name: str,
) -> tuple[list[TableMeta], list[str]]:
    """Fetch the base's metadata and project it onto CURATED.

    Only tables whose TableSpec.bases includes `const_name` are fetched.
    For TEST, fields + test_only_fields are included; for other keys, fields only.

    Returns (curated_tables, warnings). Warnings list any curated tables/fields
    that weren't found in the live base.
    """
    resp = httpx.get(
        META_URL.format(base_id=base_id),
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if resp.status_code == 403:
        raise SystemExit(
            f"403 Forbidden for base {base_id}. The PAT needs schema.bases:read "
            "and the base must be in the token's Access list."
        )
    if resp.status_code == 404:
        raise SystemExit(f"404 Not Found for base {base_id}. Check the base ID in .env.")
    resp.raise_for_status()
    payload: dict[str, Any] = resp.json()

    by_name = {t["name"]: t for t in payload.get("tables", [])}
    warnings: list[str] = []

    curated_tables: list[TableMeta] = []
    for table_name, spec in CURATED.items():
        if const_name not in spec.bases:
            continue

        wanted_entries: list[FieldEntry] = list(spec.fields)
        if const_name == "TEST":
            wanted_entries += list(spec.test_only_fields)

        live_table = by_name.get(table_name)
        if live_table is None:
            warnings.append(f"table {table_name!r} not found in base {base_id}")
            continue

        live_fields = {f["name"]: f for f in live_table.get("fields", [])}
        meta = TableMeta(id=live_table["id"], name=table_name)

        for entry in wanted_entries:
            display_name, py_ident = resolve_field_entry(entry)
            live = live_fields.get(display_name)
            if live is None:
                needle = display_name.lower()
                near = [n for n in live_fields if needle in n.lower() or n.lower() in needle]
                hint = f" Closest: {near}" if near else f" Available: {sorted(live_fields)}"
                warnings.append(
                    f"field {display_name!r} not found on table {table_name!r} in base {base_id}.{hint}"
                )
                continue
            meta.fields.append((py_ident, live["id"], display_name))

        curated_tables.append(meta)

    return curated_tables, warnings


# ---------------------------------------------------------------------------
# Code emission
# ---------------------------------------------------------------------------


def emit_module(bases: dict[str, tuple[str, list[TableMeta]]]) -> str:
    """Render the schema module text.

    `bases` maps a Python constant name (e.g. "PROD") to (base_id, curated_tables).
    The shape is taken from the union across bases; if a curated table/field is
    missing in some base, its instance gets a `"MISSING"` placeholder.
    """
    timestamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    out: list[str] = [
        '"""Airtable schema — immutable IDs for the Pear Tracker base.',
        "",
        f"Generated by scripts/generate_airtable_schema.py at {timestamp}.",
        "Do not edit by hand. Add fields to the CURATED dict in that script and re-run.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from dataclasses import dataclass",
        "",
        "",
    ]

    # Canonical shape: union of tables/fields across all bases (keyed by py_ident).
    shape: dict[str, list[tuple[str, str]]] = {}
    # table_name -> [(py_ident, original_name), ...]
    for _, (_, tables) in bases.items():
        for t in tables:
            existing = shape.setdefault(t.name, [])
            known = {row[0] for row in existing}
            for py, _atid, orig in t.fields:
                if py not in known:
                    existing.append((py, orig))
                    known.add(py)

    # One dataclass per table.
    for table_name, fields_ in shape.items():
        cls = table_class_name(table_name)
        out.append("@dataclass(frozen=True)")
        out.append(f"class {cls}:")
        out.append(f'    """Immutable IDs for the {table_name} table."""')
        out.append("")
        out.append("    id: str  # tbl…")
        for py, orig in fields_:
            out.append(f"    {py}: str  # fld…  — {orig}")
        out.append("")
        out.append("")

    # Container.
    out.append("@dataclass(frozen=True)")
    out.append("class PearTrackerSchema:")
    out.append('    """All curated tables in one Pear Tracker base."""')
    out.append("")
    out.append("    base_id: str  # app…")
    for table_name in shape:
        out.append(f"    {to_py_ident(table_name)}: {table_class_name(table_name)}")
    out.append("")
    out.append("")

    # Per-base instances.
    for const_name, (base_id, tables) in bases.items():
        by_name = {t.name: t for t in tables}
        out.append(f"{const_name} = PearTrackerSchema(")
        out.append(f'    base_id="{base_id}",')
        for table_name, fields_ in shape.items():
            attr = to_py_ident(table_name)
            cls = table_class_name(table_name)
            t = by_name.get(table_name)
            if t is None:
                out.append(f"    # WARNING: table {table_name!r} not present in this base")
                out.append(
                    f'    {attr}={cls}(id="MISSING", '
                    + ", ".join(f'{py}="MISSING"' for py, _ in fields_)
                    + "),"
                )
                continue
            lookup = {orig: atid for _, atid, orig in t.fields}
            out.append(f"    {attr}={cls}(")
            out.append(f'        id="{t.id}",')
            for py, orig in fields_:
                atid = lookup.get(orig, "MISSING")
                marker = "  # !! not in this base" if atid == "MISSING" else ""
                out.append(f'        {py}="{atid}",{marker}')
            out.append("    ),")
        out.append(")")
        out.append("")

    # Lookup.
    out.append("SCHEMAS: dict[str, PearTrackerSchema] = {")
    for const_name in bases:
        out.append(f"    {const_name}.base_id: {const_name},")
    out.append("}")
    out.append("")
    out.append("")
    out.append("def get_schema(base_id: str) -> PearTrackerSchema:")
    out.append('    """Resolve the schema for a given Airtable base ID."""')
    out.append("    try:")
    out.append("        return SCHEMAS[base_id]")
    out.append("    except KeyError as exc:")
    out.append('        raise KeyError(f"No schema registered for base {base_id!r}") from exc')
    out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Maps --base flag values to the env setting that must be present.
BASE_ENV_KEYS: dict[str, str] = {
    "PROD": "airtable_base_id",
    "TEST": "airtable_test_base_id",
    "STAGING": "airtable_staging_base_id",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        choices=["PROD", "TEST", "STAGING"],
        default="PROD",
        help=(
            "Validation gate: assert this base is configured and present in the run. "
            "Always fetches every base whose ID is set in .env regardless of this flag."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Where to write the module (default: {DEFAULT_OUT.relative_to(PROJECT_ROOT)}).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Print to stdout instead of writing to --out.",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Write the module even if some curated fields weren't found.",
    )
    args = parser.parse_args()

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        print(f"ERROR: no .env at {env_file}", file=sys.stderr)
        return 2
    settings = Settings(_env_file=str(env_file))  # type: ignore[call-arg]
    if not settings.airtable_token or "X" in settings.airtable_token[:20]:
        print("ERROR: AIRTABLE_TOKEN not set or still the placeholder", file=sys.stderr)
        return 2

    # Build the set of bases to fetch from env.
    targets: dict[str, str] = {}
    if settings.airtable_base_id:
        targets["PROD"] = settings.airtable_base_id
    if settings.airtable_staging_base_id:
        targets["STAGING"] = settings.airtable_staging_base_id
    if settings.airtable_test_base_id:
        targets["TEST"] = settings.airtable_test_base_id
    if not targets:
        print("ERROR: no AIRTABLE_BASE_ID or AIRTABLE_TEST_BASE_ID set", file=sys.stderr)
        return 2

    # Validate that the requested --base is actually configured.
    required_attr = BASE_ENV_KEYS[args.base]
    if args.base not in targets:
        print(
            f"ERROR: --base {args.base} requires {required_attr.upper()} to be set in .env.",
            file=sys.stderr,
        )
        return 2

    # Refuse to silently drop a base that's already present in the on-disk module.
    # The generator rebuilds the file from scratch; if a base's env var is unset,
    # its entire schema would vanish without this guard — the failure mode that
    # bit us when adding Drafts.Sender (the schema temporarily lost PROD because
    # AIRTABLE_BASE_ID was empty locally).
    if args.out.exists():
        existing = args.out.read_text()
        for base in BASE_ENV_KEYS:
            if f"\n{base} = PearTrackerSchema(" in existing and base not in targets:
                print(
                    f"ERROR: existing schema has {base} but its env var "
                    f"({BASE_ENV_KEYS[base].upper()}) is empty. Either set it in .env "
                    f"to regenerate {base}, or delete the {base} block from "
                    f"{args.out.relative_to(PROJECT_ROOT)} first if {base} is intentionally retired.",
                    file=sys.stderr,
                )
                return 2

    bases: dict[str, tuple[str, list[TableMeta]]] = {}
    all_warnings: list[str] = []
    for const_name, base_id in targets.items():
        print(f"Fetching {const_name} base {base_id}…", file=sys.stderr)
        tables, warnings = fetch_curated_base(settings.airtable_token, base_id, const_name)
        bases[const_name] = (base_id, tables)
        for w in warnings:
            print(f"WARN [{const_name}]: {w}", file=sys.stderr)
            all_warnings.append(w)

    if all_warnings and not args.allow_missing:
        print(
            "\nERROR: curated fields/tables were not found. Fix the names in CURATED "
            "or pass --allow-missing to write anyway (with MISSING placeholders).",
            file=sys.stderr,
        )
        return 1

    module_text = emit_module(bases)

    if args.stdout:
        sys.stdout.write(module_text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(module_text)
        # Run ruff format so the emitted module matches repo style — otherwise the
        # `ruff format --check` CI step trips on long lines (e.g. PROD's Drafts
        # placeholder, which we splat one-arg-per-line at write time).
        try:
            subprocess.run(
                ["ruff", "format", str(args.out)],
                check=True,
                cwd=PROJECT_ROOT,
                stdout=subprocess.DEVNULL,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            print(
                f"WARN: ruff format on {args.out.relative_to(PROJECT_ROOT)} failed: {exc}. "
                "Run `ruff format` manually before committing.",
                file=sys.stderr,
            )
        print(f"Wrote {args.out.relative_to(PROJECT_ROOT)}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
