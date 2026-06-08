"""One-shot diagnostic: query production mailboxes for lead emails.

Connects to Airtable (production base) to discover monitored inboxes,
then queries each inbox via Gmail for any messages from StreetEasy or Zillow
in the past LOOKBACK_DAYS days. Reports subjects and dates — no side effects.

Usage:
    uv run python scripts/check_prod_inbox_leads.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoreplies.config import get_settings
from autoreplies.parsers.base import ParserError, parse
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import get_schema
from autoreplies.services.gmail import GmailClient
from autoreplies.workers.poller import LEAD_SENDER_QUERY

LOOKBACK_DAYS = 30

CREDS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(__file__), "..", "secrets", "sa.json"),
)


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main() -> None:
    settings = get_settings()

    print(f"[config] Airtable base: {settings.active_airtable_base_id}")
    print(f"[config] Credentials:   {CREDS_PATH}")
    print(f"[config] Lookback:      {LOOKBACK_DAYS} days\n")

    schema = get_schema(settings.active_airtable_base_id)
    airtable = AirtableClient(token=settings.airtable_token, schema=schema)

    mailboxes = airtable.list_monitored_leads_emails()
    if not mailboxes:
        print("No monitored mailboxes found (Autoreply Enabled = TRUE with a Leads Email).")
        return

    print(f"Found {len(mailboxes)} monitored mailbox(es): {', '.join(mailboxes)}\n")

    after_epoch = int(time.time()) - LOOKBACK_DAYS * 86400
    query = f"{LEAD_SENDER_QUERY} after:{after_epoch}"
    print(f"Gmail query: {query!r}\n")

    total_hits = 0  # emails that would actually trigger a reply (pass the parser)

    for mailbox in mailboxes:
        try:
            client = GmailClient(mailbox_email=mailbox, credentials_path=CREDS_PATH)
            refs, _ = client.messages_list(q=query, max_results=50)
            count = len(refs)
            if count == 0:
                print(f"  {mailbox}: CLEAN")
                continue

            print(f"  {mailbox}: {count} sender-match email(s) — checking parser")
            parser_hits = 0
            for ref in refs:
                msg, _ = client.get_message(ref.id)
                subject = msg.get("Subject", "<no subject>")
                date_str = msg.get("Date", "")
                try:
                    lead = parse(msg)
                    parser_hits += 1
                    total_hits += 1
                    print(f"    *** WOULD TRIGGER *** [{date_str}] {subject!r}")
                    print(f"      -> {lead.source} | {lead.address} | {lead.prospect_email}")
                except ParserError:
                    print(f"    [no-op] [{date_str}] {subject!r}")

            if parser_hits == 0:
                print(f"    (all {count} emails filtered by parser — no triggers)")
        except Exception as exc:
            print(f"  {mailbox}: ERROR — {exc}")

    print()
    if total_hits == 0:
        print("Result: 0 lead emails found across all inboxes. Safe to proceed.")
    else:
        print(f"Result: {total_hits} lead email(s) found across production inboxes.")
        sys.exit(1)


if __name__ == "__main__":
    main()
