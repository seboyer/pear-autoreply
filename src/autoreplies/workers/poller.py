"""Production Gmail query-based polling loop.

Polls primary agent mailboxes (firstname@pearnyc.com) for inbound StreetEasy/Zillow
leads, dispatches each through the full production pipeline (parse → match →
template-fill → Airtable → Supabase → Slack → humanized-delay Gmail send).

CLI: python -m autoreplies.workers.poller

Architecture mirrors harness/poller.py. The harness and production pollers target
different mailboxes (autoreply vs primary) so they never collide during the
migration window. This module must not import anything from autoreplies.harness.*
— enforced by tests/test_distinctness.py.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from autoreplies.workers.poller_state import PollerState

logger = logging.getLogger(__name__)

LEAD_SENDER_QUERY = "from:(noreply@email.streeteasy.com OR rentalclientservices@zillowrentals.com)"


# ── Collaborator protocols ────────────────────────────────────────────────────


class MessageLister(Protocol):
    """Minimal Gmail surface the poller needs."""

    def list_messages(self, *, query: str, max_results: int = 100) -> list[tuple[str, int]]: ...


DispatchFn = Callable[[str, str], None]
"""Process a single message. Args: (gmail_message_id, mailbox_email)."""


# ── Cooperative shutdown ──────────────────────────────────────────────────────


class ShutdownFlag:
    """Cooperative shutdown signal backed by a threading.Event."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout=timeout)


def install_signal_handlers(
    shutdown: ShutdownFlag,
    *,
    mailbox_cache: MailboxCache | None = None,
) -> None:
    """Wire SIGTERM/SIGINT → shutdown.request(), SIGHUP → mailbox_cache.invalidate()."""

    def _shutdown_handler(signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; requesting shutdown.", signum)
        shutdown.request()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    if mailbox_cache is not None and hasattr(signal, "SIGHUP"):

        def _sighup_handler(signum: int, _frame: Any) -> None:
            logger.info("Received SIGHUP; invalidating mailbox cache.")
            mailbox_cache.invalidate()

        signal.signal(signal.SIGHUP, _sighup_handler)


# ── Agent-mailbox discovery (cached) ──────────────────────────────────────────


class MailboxCache:
    """TTL cache for the primary agent mailbox list."""

    def __init__(
        self,
        ttl_seconds: int = 6 * 3600,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._now = now
        self._cached: list[str] | None = None
        self._fetched_at: float = 0.0

    def get(self, airtable: Any) -> list[str]:
        if self._cached is None or (self._now() - self._fetched_at) >= self._ttl:
            self._cached = airtable.list_monitored_leads_emails()
            self._fetched_at = self._now()
        return list(self._cached)

    def invalidate(self) -> None:
        self._cached = None
        self._fetched_at = 0.0


def discover_monitored_mailboxes(
    airtable: Any,
    *,
    cache: MailboxCache | None = None,
) -> list[str]:
    """Resolve the current monitored primary-email list."""
    if cache is None:
        return airtable.list_monitored_leads_emails()
    return cache.get(airtable)


# ── poll_once ─────────────────────────────────────────────────────────────────


@dataclass
class PollStats:
    """Per-call counters returned by poll_once."""

    fetched: int = 0
    new: int = 0
    skipped_dedup: int = 0
    succeeded: int = 0
    failed: int = 0
    max_internal_date_ms: int | None = None


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def poll_once(
    *,
    mailbox: str,
    gmail_client: MessageLister,
    state: PollerState,
    dispatch: DispatchFn,
    bootstrap_lookback_seconds: int,
    max_results: int = 100,
    now_ms: Callable[[], int] = _default_now_ms,
) -> PollStats:
    """List + dispatch unprocessed lead messages for one mailbox."""
    stats = PollStats()

    last_seen_ms = state.get_last_seen(mailbox)
    if last_seen_ms is None:
        last_seen_ms = now_ms() - bootstrap_lookback_seconds * 1000

    query = f"{LEAD_SENDER_QUERY} after:{last_seen_ms // 1000}"
    messages = gmail_client.list_messages(query=query, max_results=max_results)
    stats.fetched = len(messages)

    if not messages:
        return stats

    max_seen = last_seen_ms
    for message_id, internal_date_ms in messages:
        if internal_date_ms > max_seen:
            max_seen = internal_date_ms

        if state.was_processed(message_id):
            stats.skipped_dedup += 1
            continue

        stats.new += 1
        try:
            dispatch(message_id, mailbox)
        except Exception as exc:
            stats.failed += 1
            logger.exception(
                "poll_once: dispatch failed mailbox=%s message_id=%s",
                mailbox,
                message_id,
            )
            state.mark_processed(message_id, mailbox, error=repr(exc))
            continue

        stats.succeeded += 1
        state.mark_processed(message_id, mailbox)

    state.set_last_seen(mailbox, max_seen)
    stats.max_internal_date_ms = max_seen
    return stats


# ── run_forever ───────────────────────────────────────────────────────────────


@dataclass
class PollerConfig:
    """Tunables for run_forever."""

    interval_seconds: int
    bootstrap_lookback_seconds: int
    mailbox_inter_sleep_seconds: float = 0.5
    mailbox_cache_ttl_seconds: int = 6 * 3600
    max_results_per_page: int = 100
    install_signal_handlers: bool = True
    _testing_max_iterations: int | None = field(default=None, repr=False)
    """Bound the loop in tests. Production leaves this None."""


def run_forever(
    *,
    airtable: Any,
    gmail_factory: Callable[[str], MessageLister],
    state: PollerState,
    dispatch: DispatchFn,
    config: PollerConfig,
    shutdown: ShutdownFlag | None = None,
) -> None:
    """Loop forever: discover primary mailboxes, poll each, sleep, repeat.

    SIGHUP invalidates the mailbox cache so a newly-enabled agent picks up on
    the next iteration without a process restart.
    """
    if shutdown is None:
        shutdown = ShutdownFlag()

    mailbox_cache = MailboxCache(ttl_seconds=config.mailbox_cache_ttl_seconds)

    if config.install_signal_handlers:
        install_signal_handlers(shutdown, mailbox_cache=mailbox_cache)

    iteration = 0
    while not shutdown.is_set():
        iteration += 1

        try:
            mailboxes = discover_monitored_mailboxes(airtable, cache=mailbox_cache)
        except Exception:
            logger.exception(
                "run_forever: monitored mailbox discovery failed; retrying after interval"
            )
            mailboxes = []

        for mailbox in mailboxes:
            if shutdown.is_set():
                break

            try:
                stats = poll_once(
                    mailbox=mailbox,
                    gmail_client=gmail_factory(mailbox),
                    state=state,
                    dispatch=dispatch,
                    bootstrap_lookback_seconds=config.bootstrap_lookback_seconds,
                    max_results=config.max_results_per_page,
                )
                logger.info(
                    "poll_once mailbox=%s fetched=%d new=%d "
                    "skipped_dedup=%d succeeded=%d failed=%d",
                    mailbox,
                    stats.fetched,
                    stats.new,
                    stats.skipped_dedup,
                    stats.succeeded,
                    stats.failed,
                )
            except Exception:
                logger.exception("poll_once: unhandled error for mailbox=%s", mailbox)

            if shutdown.is_set():
                break

            if config.mailbox_inter_sleep_seconds > 0:
                shutdown.wait(config.mailbox_inter_sleep_seconds)

        if (
            config._testing_max_iterations is not None
            and iteration >= config._testing_max_iterations
        ):
            break

        if shutdown.is_set():
            break

        shutdown.wait(config.interval_seconds)

    logger.info("run_forever: exiting after %d iteration(s).", iteration)


# ── CLI entrypoint ────────────────────────────────────────────────────────────


def main() -> int:
    from redis import Redis
    from rq import Queue

    from autoreplies.config import get_settings
    from autoreplies.logging_config import configure_logging
    from autoreplies.pipeline.process_lead import process_lead
    from autoreplies.pipeline.strategies import build_production_strategies
    from autoreplies.services.airtable import AirtableClient
    from autoreplies.services.airtable_schema import get_schema
    from autoreplies.services.gmail import GmailClient
    from autoreplies.services.slack import SlackClient
    from autoreplies.services.supabase import SupabaseClient

    settings = get_settings()
    configure_logging(settings.log_level)

    log = logging.getLogger("autoreplies.workers.poller")
    log.info("production poller starting (env=%s)", settings.app_env)

    redis_conn = Redis.from_url(settings.redis_url)
    queue = Queue("default", connection=redis_conn)
    slack = SlackClient(bot_token=settings.slack_bot_token, channel=settings.slack_channel)
    supabase = SupabaseClient(
        url=settings.supabase_url, service_role_key=settings.supabase_service_role_key
    )
    strategies = build_production_strategies(
        queue=queue, slack_client=slack, supabase_client=supabase
    )

    schema = get_schema(settings.active_airtable_base_id)
    airtable = AirtableClient(
        token=settings.airtable_token,
        schema=schema,
    )

    from autoreplies.services.llm import LLMClient

    llm = LLMClient(api_key=settings.anthropic_api_key, model=settings.anthropic_model)

    def gmail_factory(mailbox: str) -> GmailClient:
        return GmailClient(
            mailbox_email=mailbox,
            credentials_path=settings.google_application_credentials,
        )

    state = PollerState(settings.poller_state_path)

    def dispatch(message_id: str, mailbox: str) -> None:
        gmail = gmail_factory(mailbox)
        process_lead(
            message_id,
            mailbox,
            strategies=strategies,
            gmail=gmail,
            airtable=airtable,
            llm=llm,
            agent_lookup_by="leads",
            dedup=state,
            dedup_window_seconds=settings.dedup_window_seconds,
        )

    cfg = PollerConfig(
        interval_seconds=settings.poller_poll_interval_seconds,
        bootstrap_lookback_seconds=settings.poller_bootstrap_lookback_seconds,
    )

    run_forever(
        airtable=airtable,
        gmail_factory=gmail_factory,
        state=state,
        dispatch=dispatch,
        config=cfg,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
