"""CLI dispatch for the testing harness.

Usage: python -m autoreplies.harness <subcommand> [options]

Subcommands:
    watch       Long-running poll loop.
    backfill    Process all leads since a given date.
    replay      Re-run a single message, bypassing dedup.
    diff        Cross-base comparison report (H5).
    stats       Aggregate metrics report (H5).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from autoreplies.logging_config import configure_logging

logger = logging.getLogger(__name__)


def _cmd_watch(args: argparse.Namespace) -> int:
    from autoreplies.config import get_settings
    from autoreplies.harness.pipeline import build_harness_airtable_client, build_harness_pipeline
    from autoreplies.harness.poller import PollerConfig, run_forever
    from autoreplies.harness.state import HarnessState
    from autoreplies.services.gmail import GmailClient

    settings = get_settings()
    state = HarnessState(settings.harness_state_path)
    airtable = build_harness_airtable_client()
    pipeline_run = build_harness_pipeline()
    interval = args.interval or settings.harness_poll_interval_seconds

    def gmail_factory(mailbox: str) -> GmailClient:
        return GmailClient(
            mailbox_email=mailbox,
            credentials_path=settings.google_application_credentials,
        )

    config = PollerConfig(
        interval_seconds=interval,
        bootstrap_lookback_seconds=settings.harness_bootstrap_lookback_seconds,
    )
    run_forever(
        airtable=airtable,
        gmail_factory=gmail_factory,
        state=state,
        dispatch=pipeline_run,
        config=config,
    )
    return 0


def _cmd_backfill(args: argparse.Namespace) -> int:
    from autoreplies.config import get_settings
    from autoreplies.harness.pipeline import build_harness_airtable_client, build_harness_pipeline
    from autoreplies.harness.poller import LEAD_SENDER_QUERY
    from autoreplies.harness.state import HarnessState
    from autoreplies.services.gmail import GmailClient

    settings = get_settings()
    state = HarnessState(settings.harness_state_path)
    airtable = build_harness_airtable_client()
    pipeline_run = build_harness_pipeline()

    since_dt = datetime.fromisoformat(args.since)
    since_unix = int(since_dt.timestamp())
    query = f"{LEAD_SENDER_QUERY} after:{since_unix}"

    mailboxes = [args.mailbox] if args.mailbox else airtable.list_agent_emails()

    total = 0
    for mailbox in mailboxes:
        gmail = GmailClient(
            mailbox_email=mailbox,
            credentials_path=settings.google_application_credentials,
        )
        messages = gmail.list_messages(query=query)
        processed = 0
        for message_id, _ in messages:
            if args.limit is not None and total >= args.limit:
                break
            if state.was_processed(message_id):
                logger.debug("backfill: skip (already processed) message_id=%s", message_id)
                continue
            try:
                pipeline_run(message_id, mailbox)
                processed += 1
                total += 1
            except Exception:
                logger.exception("backfill: failed message_id=%s mailbox=%s", message_id, mailbox)
                total += 1
        logger.info("backfill: mailbox=%s processed=%d", mailbox, processed)

    logger.info("backfill: complete total=%d", total)
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from autoreplies.harness.pipeline import build_harness_pipeline

    pipeline_run = build_harness_pipeline()
    try:
        pipeline_run(args.message_id, args.mailbox)
    except Exception:
        logger.exception("replay: failed message_id=%s mailbox=%s", args.message_id, args.mailbox)
        return 1
    logger.info("replay: done message_id=%s", args.message_id)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    import sys

    from autoreplies.harness.diff import HarnessDiff, to_csv
    from autoreplies.harness.pipeline import (
        build_harness_airtable_client,
        build_production_airtable_client_readonly,
    )

    prod_airtable = build_production_airtable_client_readonly()
    test_airtable = build_harness_airtable_client()
    rows = HarnessDiff(prod_airtable, test_airtable).compute(args.since)
    csv_output = to_csv(rows)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(csv_output)
        logger.info("diff: wrote %d rows to %s", len(rows), args.out)
    else:
        sys.stdout.write(csv_output)
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    from autoreplies.harness.pipeline import build_harness_airtable_client
    from autoreplies.harness.stats import HarnessStats

    airtable = build_harness_airtable_client()
    report = HarnessStats(airtable).compute(args.since)
    print(report.format_table())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m autoreplies.harness",
        description="Pear autoreplies testing harness",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # watch
    watch_p = sub.add_parser("watch", help="Long-running poll loop")
    watch_p.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Override HARNESS_POLL_INTERVAL_SECONDS from .env",
    )
    watch_p.set_defaults(func=_cmd_watch)

    # backfill
    backfill_p = sub.add_parser("backfill", help="Process all leads since a given date")
    backfill_p.add_argument("--since", required=True, metavar="YYYY-MM-DD")
    backfill_p.add_argument("--mailbox", default=None, metavar="EMAIL")
    backfill_p.add_argument("--limit", type=int, default=None, metavar="N")
    backfill_p.set_defaults(func=_cmd_backfill)

    # replay
    replay_p = sub.add_parser("replay", help="Re-run a single message (bypasses dedup)")
    replay_p.add_argument("message_id", help="Gmail message ID to replay")
    replay_p.add_argument("--mailbox", required=True, metavar="EMAIL")
    replay_p.set_defaults(func=_cmd_replay)

    # diff
    diff_p = sub.add_parser("diff", help="Cross-base comparison report (H5)")
    diff_p.add_argument("--since", required=True, metavar="YYYY-MM-DD")
    diff_p.add_argument("--out", default=None, metavar="FILE")
    diff_p.set_defaults(func=_cmd_diff)

    # stats
    stats_p = sub.add_parser("stats", help="Aggregate metrics report (H5)")
    stats_p.add_argument("--since", required=True, metavar="YYYY-MM-DD")
    stats_p.set_defaults(func=_cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
