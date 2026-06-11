"""One-shot diagnostic: detect duplicate inquiry emails in production mailboxes.

For each monitored mailbox, queries lead emails over the lookback window, parses each
with the production parser, computes a content fingerprint per lead, and groups
messages by fingerprint. Any group with >1 message is a suspected duplicate set.

Reports: subjects, message_ids, time gaps between consecutive messages in the group.
At the end, prints the MAX observed gap — this informs the `dedup_window_seconds`
setting. Rule of thumb: set dedup_window_seconds to (max_observed_gap_seconds * 2).

No side effects. No DB writes. Read-only.

Usage:
    uv run python scripts/check_duplicate_inquiries.py
    uv run python scripts/check_duplicate_inquiries.py --mailbox garland@pearnyc.com
    uv run python scripts/check_duplicate_inquiries.py --mailbox a@pearnyc.com --mailbox b@pearnyc.com --lookback-days 90

When one or more --mailbox values are given, Airtable discovery is skipped and
exactly those mailboxes are scanned. Useful for the autoreply mailboxes (e.g.
garland@pearnyc.com), which have more history than freshly-launched production
primary inboxes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from autoreplies.config import get_settings
from autoreplies.logging_config import configure_logging
from autoreplies.parsers.base import ParserError, parse
from autoreplies.pipeline.dedup import compute_fingerprint
from autoreplies.services.airtable import AirtableClient
from autoreplies.services.airtable_schema import get_schema
from autoreplies.services.gmail import GmailClient
from autoreplies.workers.poller import LEAD_SENDER_QUERY

DEFAULT_LOOKBACK_DAYS = 30

CREDS_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(__file__), "..", "secrets", "sa.json"),
)


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mailbox",
        action="append",
        default=None,
        metavar="EMAIL",
        help="Scan this mailbox instead of Airtable-discovered inboxes (repeatable).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        metavar="N",
        help=f"How far back to scan (default {DEFAULT_LOOKBACK_DAYS}).",
    )
    args = parser.parse_args()
    lookback_days: int = args.lookback_days

    configure_logging("INFO")

    settings = get_settings()

    print(f"[config] Credentials:   {CREDS_PATH}")
    print(f"[config] Lookback:      {lookback_days} days")

    if args.mailbox:
        mailboxes = args.mailbox
        print(f"[config] Mailboxes:     {', '.join(mailboxes)} (explicit override)\n")
    else:
        print(f"[config] Airtable base: {settings.active_airtable_base_id}\n")
        schema = get_schema(settings.active_airtable_base_id)
        airtable = AirtableClient(token=settings.airtable_token, schema=schema)
        mailboxes = airtable.list_monitored_leads_emails()
        if not mailboxes:
            print("No monitored mailboxes found (Autoreply Enabled = TRUE with a Leads Email).")
            return
        print(f"Found {len(mailboxes)} monitored mailbox(es): {', '.join(mailboxes)}\n")

    after_epoch = int(time.time()) - lookback_days * 86400
    query = f"{LEAD_SENDER_QUERY} after:{after_epoch}"
    print(f"Gmail query: {query!r}\n")

    max_gap_seconds: float = 0.0
    total_duplicate_groups = 0

    for mailbox in mailboxes:
        print(f"=== {mailbox} ===")
        try:
            client = GmailClient(mailbox_email=mailbox, credentials_path=CREDS_PATH)
            messages = client.list_messages(query=query, max_results=200)
            if not messages:
                print("  (no lead emails found)\n")
                continue

            print(f"  {len(messages)} sender-match email(s) — parsing and fingerprinting...\n")

            # Build a list of (fingerprint, message_id, internal_date_ms, subject) tuples.
            records: list[tuple[str, str, int, str]] = []
            for message_id, internal_date_ms in messages:
                try:
                    msg, _ = client.get_message(message_id)
                    subject = (msg.get("Subject") or "").strip()
                    lead = parse(msg)
                    fp = compute_fingerprint(
                        mailbox=mailbox,
                        prospect_email=lead.email,
                        message_body=lead.message_body,
                        apartment_address=lead.apartment_address,
                        source=lead.source,
                    )
                    records.append((fp, message_id, internal_date_ms, subject))
                except ParserError:
                    pass  # Not a parseable lead — skip (same filter as the pipeline)
                except Exception as exc:
                    print(f"  [error fetching {message_id}]: {exc}")

            # Group by fingerprint.
            by_fp: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
            for fp, mid, ts, subj in records:
                by_fp[fp].append((mid, ts, subj))

            duplicate_fps = {fp: msgs for fp, msgs in by_fp.items() if len(msgs) > 1}

            if not duplicate_fps:
                print("  No duplicate fingerprint groups found.\n")
                continue

            print(f"  {len(duplicate_fps)} duplicate group(s) found:\n")
            total_duplicate_groups += len(duplicate_fps)

            for fp, msgs in duplicate_fps.items():
                # Sort by timestamp.
                msgs_sorted = sorted(msgs, key=lambda x: x[1])
                print(f"  Fingerprint: {fp[:16]}…")
                for i, (mid, ts, subj) in enumerate(msgs_sorted):
                    print(f"    [{i+1}] {_ts(ts)}  message_id={mid}  subject={subj!r}")
                # Compute gaps between consecutive messages.
                gaps_sec = [
                    (msgs_sorted[i + 1][1] - msgs_sorted[i][1]) / 1000
                    for i in range(len(msgs_sorted) - 1)
                ]
                for gap in gaps_sec:
                    print(f"         ^ gap to next: {gap:.0f}s ({gap/60:.1f} min)")
                    if gap > max_gap_seconds:
                        max_gap_seconds = gap
                print()

        except Exception as exc:
            print(f"  ERROR: {exc}\n")

    print("=" * 60)
    if total_duplicate_groups == 0:
        print("Result: No duplicate inquiry groups found across all inboxes.")
        print("  dedup_window_seconds can remain at the default (21600 = 6h).")
    else:
        print(
            f"Result: {total_duplicate_groups} duplicate group(s) found. "
            f"Max gap between re-sends: {max_gap_seconds:.0f}s "
            f"({max_gap_seconds / 60:.1f} min)."
        )
        recommended = int(max_gap_seconds * 2)
        print(
            f"  Recommended dedup_window_seconds: {recommended} "
            f"(= max_gap * 2; current default is 21600)."
        )


if __name__ == "__main__":
    main()
