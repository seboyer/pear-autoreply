"""Per-agent reply-template lookup with a Pear-wide fallback.

Each Users record carries a free-text template in a slot-filled Airtable field
(`{{first_name|there}}`, `{{apartment_address|the listing}}`, etc.). When the
agent's field is empty, the system falls back to `PEAR_FALLBACK_TEMPLATE`
loaded from `FALLBACK_TEMPLATE.md` at the repo root — the single source of
truth for the default. See PLAN.md § 4 + Appendix A.

The Airtable field this reads from is base-dependent:
  - Harness (TEST base): `Users.autoreply_test_template`
    (a new editable field that mirrors the eventual production shape)
  - Production (PROD base, post-cutover): `Users.autoreply_agent`
    (the existing field, currently owned by the legacy Zapier flow)
Callers pass the field ID explicitly so this module stays base-agnostic.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

TemplateSource = Literal["agent", "pear_default"]

# Module-level pointer to FALLBACK_TEMPLATE.md. The default resolves the path
# relative to this file so it works in editable installs and packaged wheels.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_FALLBACK_PATH = _PROJECT_ROOT / "FALLBACK_TEMPLATE.md"


def _extract_template_body(markdown: str) -> str:
    """Pull the `## Template body` blockquote out of FALLBACK_TEMPLATE.md.

    The .md file is human-readable documentation. The actual template is the
    blockquoted lines under the `## Template body` heading, with `> ` prefixes
    stripped. Stops at the next `## ` heading.
    """
    section_re = re.compile(
        r"^##\s+Template body\s*\n(.*?)(?:^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = section_re.search(markdown)
    if not match:
        raise RuntimeError("FALLBACK_TEMPLATE.md missing a '## Template body' section")

    body_lines: list[str] = []
    for raw in match.group(1).splitlines():
        stripped = raw.strip()
        if not stripped.startswith(">"):
            continue
        # Strip leading "> " (or just ">") and preserve interior spacing.
        body_lines.append(stripped[1:].lstrip(" "))

    if not body_lines:
        raise RuntimeError("FALLBACK_TEMPLATE.md '## Template body' contained no blockquote")

    # Collapse trailing blank lines but preserve interior blank-line paragraph breaks.
    while body_lines and not body_lines[-1]:
        body_lines.pop()
    return "\n".join(body_lines)


@lru_cache(maxsize=1)
def _load_fallback_template() -> str:
    return _extract_template_body(_FALLBACK_PATH.read_text(encoding="utf-8"))


def get_pear_fallback_template() -> str:
    """Return the Pear-wide fallback template body (cached)."""
    return _load_fallback_template()


def reload_pear_fallback_template() -> str:
    """Drop the cache and re-read FALLBACK_TEMPLATE.md.

    Wired into the (planned) `/admin/reload-template` endpoint so a sales-team
    edit takes effect without a redeploy. See PLAN.md § "Implementation notes".
    """
    _load_fallback_template.cache_clear()
    return _load_fallback_template()


def get_template_for_agent(
    agent_record: dict[str, Any],
    *,
    template_field_id: str,
) -> tuple[str, TemplateSource]:
    """Return `(template_text, source)` for one agent.

    `template_field_id` is the Airtable field ID of the per-agent template
    column for the active base — typically `schema.users.autoreply_agent` in
    production and `schema.users.autoreply_test_template` in the harness.
    Passing it in keeps this module base-agnostic.

    When the agent's template field is missing or blank, returns the
    Pear-wide fallback with source `"pear_default"`.
    """
    fields = agent_record.get("fields", {}) if agent_record else {}
    raw = fields.get(template_field_id)
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), "agent"
    return get_pear_fallback_template(), "pear_default"
