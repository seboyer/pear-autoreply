"""Backfill public.inquiries gaps from the Oct 29 - Nov 5 2026 Supabase timeouts.

Input: docs/incidents/2026-11-supabase-timeouts_backfill.csv (one row per
affected inquiry, built from Render logs; see the incident write-up). Every
reply in it WAS sent - this only repairs Supabase bookkeeping:

  * missing row      -> upsert rebuilt from the production Airtable inquiry
                        (same mapping as process_lead phase B), with created_at
                        set to the Airtable record's creation time
  * missing reply_*  -> PATCH reply_gmail_message_id (from the CSV, cross-checked
                        against Airtable) + reply_message (Airtable Reply (Autoreply))
  * RFC-822 Message-IDs (rfc822_message_id, reply_rfc822_message_id) need Gmail
    via the service account. Without --gmail they are left null; re-run with
    --gmail where the key exists to fill just those.

State-driven and idempotent: each run reads the current Supabase row and writes
only what is still missing. Dry run by default; --apply writes.

    python scripts/backfill_supabase_2026_11.py                 # dry run
    python scripts/backfill_supabase_2026_11.py --check-mapping # validate mapping
    python scripts/backfill_supabase_2026_11.py --apply [--gmail]
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pyairtable import Api

from autoreplies.config import get_settings
from autoreplies.logging_config import configure_logging
from autoreplies.services.airtable_schema import PROD
from autoreplies.services.supabase import SupabaseClient

log = logging.getLogger("backfill")

DEFAULT_CSV = (
    Path(__file__).resolve().parent.parent / "docs/incidents/2026-11-supabase-timeouts_backfill.csv"
)
SUPABASE_COLS = (
    "id,gmail_message_id,user_id,apartment_id,apartment_failsafe,name_form,email_form,"
    "name,email,phone,message,type_platform,method,rfc822_message_id,created_at,"
    "reply_gmail_message_id,reply_message,reply_rfc822_message_id"
)


@dataclass
class Plan:
    inquiry_id: str
    mailbox: str
    lead_gmail_id: str
    reply_gmail_id: str
    upsert: dict[str, Any] | None = None
    reply: dict[str, Any] | None = None
    lead_rfc822_needed: bool = False
    reply_rfc822_needed: bool = False
    problems: list[str] = field(default_factory=list)


def _blank_to_none(v: Any) -> Any:
    return None if v in ("", [], None) else v


def phase_b_payload(rec: dict[str, Any], lead_gmail_id: str) -> dict[str, Any]:
    """Rebuild process_lead._phase_b_write_supabase's kwargs from the Airtable row.

    Phase A writes Airtable and Supabase from the same parsed lead:
    name_form == name == "first last", email_form == email == parsed.email,
    type_platform == Type (Non Website) == parsed.source.
    """
    inq = PROD.inquiries
    f = rec["fields"]
    name = _blank_to_none(f.get(inq.name_form))
    email = _blank_to_none(f.get(inq.email_form))
    apartment = f.get(inq.apartment) or []
    user = f.get(inq.user) or []
    return {
        "id": rec["id"],
        "gmail_message_id": lead_gmail_id,
        "user_id": user[0] if user else None,
        "apartment_id": apartment[0] if apartment else None,
        "apartment_failsafe": _blank_to_none(f.get(inq.apartment_failsafe)),
        "name_form": name,
        "email_form": email,
        "name": name,
        "email": email,
        "phone": _blank_to_none(f.get(inq.phone)),
        "message": _blank_to_none(f.get(inq.message)),
        "type_platform": f.get(inq.type_non_website) or "",
        "created_at": rec["createdTime"],
    }


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


def fetch_supabase_rows(settings: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    headers = {
        "apikey": settings.supabase_service_role_key,
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
    }
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 80):
        resp = httpx.get(
            f"{settings.supabase_url.rstrip('/')}/rest/v1/inquiries",
            headers=headers,
            params={"select": SUPABASE_COLS, "id": f"in.({','.join(ids[i : i + 80])})"},
            timeout=30.0,
        )
        resp.raise_for_status()
        out.update({r["id"]: r for r in resp.json()})
    return out


def build_plans(
    rows: list[dict[str, str]],
    airtable: dict[str, dict[str, Any]],
    supabase: dict[str, dict[str, Any]],
) -> list[Plan]:
    inq = PROD.inquiries
    plans = []
    for row in rows:
        iid = row["inquiry_id"]
        p = Plan(
            inquiry_id=iid,
            mailbox=row["mailbox"],
            lead_gmail_id=row["lead_gmail_message_id"],
            reply_gmail_id=row["reply_gmail_message_id"],
        )
        plans.append(p)
        rec = airtable.get(iid)
        if rec is None:
            p.problems.append("Airtable inquiry not found")
            continue
        f = rec["fields"]
        at_reply_id = f.get(inq.gmail_message_id_autoreply)
        if at_reply_id != p.reply_gmail_id:
            p.problems.append(
                f"Airtable Gmail Message ID (Autoreply)={at_reply_id!r} != logged reply "
                f"{p.reply_gmail_id!r}"
            )
            continue
        reply_body = _blank_to_none(f.get(inq.reply_autoreply))
        if reply_body is None:
            p.problems.append("Airtable Reply (Autoreply) is empty")
            continue

        current = supabase.get(iid)
        if current is None:
            p.upsert = phase_b_payload(rec, p.lead_gmail_id)
        if current is None or not current.get("reply_gmail_message_id"):
            p.reply = {
                "id": iid,
                "reply_gmail_message_id": p.reply_gmail_id,
                "reply_message": reply_body,
            }
        p.lead_rfc822_needed = current is None or not current.get("rfc822_message_id")
        p.reply_rfc822_needed = current is None or not current.get("reply_rfc822_message_id")
    return plans


def check_mapping(
    rows: list[dict[str, str]],
    airtable: dict[str, dict[str, Any]],
    supabase: dict[str, dict[str, Any]],
) -> int:
    """Rebuild the payload for rows phase B DID write and diff against Supabase."""
    compared = mismatches = 0
    for row in rows:
        iid = row["inquiry_id"]
        rec, cur = airtable.get(iid), supabase.get(iid)
        if rec is None or cur is None:
            continue
        compared += 1
        built = phase_b_payload(rec, row["lead_gmail_message_id"])
        for key, val in built.items():
            if key == "created_at":
                continue  # DB default on live rows; backfill sets it explicitly
            if cur.get(key) != val:
                mismatches += 1
                print(f"  {iid} {key}: supabase={cur.get(key)!r} airtable-built={val!r}")
    # The reply writeback path: Airtable Reply (Autoreply) vs Supabase reply_message,
    # for rows where send_reply_job wrote both.
    reply_compared = reply_mismatches = 0
    for row in rows:
        iid = row["inquiry_id"]
        rec, cur = airtable.get(iid), supabase.get(iid)
        if rec is None or cur is None or not cur.get("reply_gmail_message_id"):
            continue
        reply_compared += 1
        at_body = rec["fields"].get(PROD.inquiries.reply_autoreply)
        at_id = rec["fields"].get(PROD.inquiries.gmail_message_id_autoreply)
        if at_body != cur.get("reply_message") or at_id != cur.get("reply_gmail_message_id"):
            reply_mismatches += 1
            print(f"  {iid} reply: Airtable and Supabase disagree")
    print(f"mapping check: compared {compared} existing rows, {mismatches} field mismatches")
    print(f"reply check:   compared {reply_compared} rows, {reply_mismatches} mismatches")
    return mismatches + reply_mismatches


def fill_rfc822(plans: list[Plan], settings: Any) -> None:
    from autoreplies.services.gmail import GmailClient

    clients: dict[str, GmailClient] = {}
    for p in plans:
        if not (p.lead_rfc822_needed or p.reply_rfc822_needed) or p.problems:
            continue
        gmail = clients.get(p.mailbox)
        if gmail is None:
            gmail = GmailClient(
                mailbox_email=p.mailbox,
                credentials_path=settings.google_application_credentials,
            )
            clients[p.mailbox] = gmail
        for needed, gid, attr in (
            (p.lead_rfc822_needed, p.lead_gmail_id, "lead"),
            (p.reply_rfc822_needed, p.reply_gmail_id, "reply"),
        ):
            if not needed:
                continue
            try:
                msg, _ = gmail.get_message(gid)
                mid = (msg.get("Message-ID") or "").strip() or None
            except Exception as exc:
                p.problems.append(f"Gmail fetch {attr} {gid}: {exc!r}"[:200])
                continue
            if attr == "lead":
                p.lead_rfc822_value = mid  # type: ignore[attr-defined]
            else:
                p.reply_rfc822_value = mid  # type: ignore[attr-defined]


def _has_work(p: Plan) -> bool:
    return not p.problems and (
        p.upsert is not None
        or p.reply is not None
        or bool(getattr(p, "lead_rfc822_value", None))
        or bool(getattr(p, "reply_rfc822_value", None))
    )


def apply(plans: list[Plan], settings: Any, *, limit: int | None) -> int:
    """Write each plan; a failing row records a problem and the batch continues.
    Returns the number of inquiries processed."""
    sb = SupabaseClient(
        url=settings.supabase_url,
        service_role_key=settings.supabase_service_role_key,
        write_rfc822=True,  # the RFC-822 columns exist in production
    )
    headers = dict(sb._headers)
    done = 0
    # Missing rows first: each also needs its reply columns, so a --limit 1
    # canary exercises both the upsert (incl. created_at) and the reply PATCH.
    for p in sorted(plans, key=lambda p: p.upsert is None):
        if not _has_work(p):
            continue
        if limit is not None and done >= limit:
            break
        done += 1
        try:
            _apply_one(p, sb, headers)
        except Exception as exc:
            p.problems.append(f"apply failed: {exc!r}"[:300])
        time.sleep(0.05)
    return done


def _apply_one(p: Plan, sb: SupabaseClient, headers: dict[str, str]) -> None:
    lead_rfc = getattr(p, "lead_rfc822_value", None)
    reply_rfc = getattr(p, "reply_rfc822_value", None)
    if p.upsert is not None:
        sb.upsert_inquiry(**p.upsert, rfc822_message_id=lead_rfc)
        log.info("upserted row inquiry=%s", p.inquiry_id)
    elif lead_rfc:
        resp = httpx.patch(
            f"{sb.url}/rest/v1/inquiries",
            headers=headers,
            params={"id": f"eq.{p.inquiry_id}"},
            json={"rfc822_message_id": lead_rfc},
            timeout=30.0,
        )
        resp.raise_for_status()
    if p.reply is not None or reply_rfc:
        reply = p.reply or {}
        if not reply:
            # rfc822-only pass: re-send the existing values (idempotent).
            reply = {
                "id": p.inquiry_id,
                "reply_gmail_message_id": p.reply_gmail_id,
                "reply_message": p._existing_reply_message,  # type: ignore[attr-defined]
            }
        row = sb.update_inquiry_reply(**reply, reply_rfc822_message_id=reply_rfc)
        if not row:
            p.problems.append("reply PATCH matched 0 rows")
        else:
            log.info("reply columns written inquiry=%s", p.inquiry_id)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--apply", action="store_true", help="write to production Supabase")
    ap.add_argument("--gmail", action="store_true", help="fetch RFC-822 Message-IDs from Gmail")
    ap.add_argument("--check-mapping", action="store_true")
    ap.add_argument("--limit", type=int, help="with --apply: process at most N inquiries (canary)")
    args = ap.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if settings.airtable_base_id != PROD.base_id:
        print(f"refusing: AIRTABLE_BASE_ID={settings.airtable_base_id} is not production")
        return 2

    rows = load_csv(args.csv)
    ids = [r["inquiry_id"] for r in rows]
    table = Api(settings.airtable_token).table(PROD.base_id, PROD.inquiries.id)
    airtable: dict[str, dict[str, Any]] = {}
    for iid in ids:
        airtable[iid] = dict(table.get(iid, use_field_ids=True))
    supabase = fetch_supabase_rows(settings, ids)

    if args.check_mapping:
        return 1 if check_mapping(rows, airtable, supabase) else 0

    plans = build_plans(rows, airtable, supabase)
    for p in plans:
        cur = supabase.get(p.inquiry_id) or {}
        p._existing_reply_message = cur.get("reply_message")  # type: ignore[attr-defined]
    if args.gmail:
        fill_rfc822(plans, settings)

    upserts = [p for p in plans if p.upsert is not None and not p.problems]
    replies = [p for p in plans if p.reply is not None and not p.problems]
    rfc = [p for p in plans if (p.lead_rfc822_needed or p.reply_rfc822_needed) and not p.problems]
    problems = [p for p in plans if p.problems]
    print(f"inquiries in CSV:          {len(plans)}")
    print(f"rows to create:            {len(upserts)}")
    print(f"reply columns to write:    {len(replies)}")
    print(
        f"RFC-822 IDs still missing: {len(rfc)} ({'fetching' if args.gmail else 'needs --gmail'})"
    )
    print(f"skipped with problems:     {len(problems)}")
    for p in problems:
        print(f"  {p.inquiry_id}: {'; '.join(p.problems)}")
    if upserts:  # keys only: values are prospect PII
        print("upsert keys:", sorted(k for k, v in (upserts[0].upsert or {}).items() if v))

    if not args.apply:
        print("dry run - nothing written. Re-run with --apply to write.")
        return 0
    processed = apply(plans, settings, limit=args.limit)
    print(f"processed {processed} inquiries")
    late = [p for p in plans if p.problems and p not in problems]
    print(f"applied. new problems during apply: {len(late)}")
    for p in late:
        print(f"  {p.inquiry_id}: {'; '.join(p.problems)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
