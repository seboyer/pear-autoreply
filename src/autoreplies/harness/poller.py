"""Gmail query-based polling loop for the harness.

Per HARNESS_BUILD_BRIEF.md § H4: list messages matching the lead-sender
allowlist `from:(noreply@email.streeteasy.com OR
rentalclientservices@zillowrentals.com)` since the per-mailbox cursor, dedup
against the SQLite state store, and dispatch each new message through the
harness pipeline. Production uses Pub/Sub + history.list; this side-car uses
timestamp-based polling so there's no Gmail filter setup to coordinate.

The Gmail surface this module needs is intentionally narrow — see
`MessageLister`. Production's `services/gmail.py` is not extended here; H4e (or
Phase 1) wires a concrete adapter.

Cursor semantics: the cursor advances to the max `internalDate` of every
message seen in a batch, including dedup-skipped and failed dispatches. Failed
messages stay in `processed_messages` with an `error` payload; replay is the
recovery path. See `HarnessState.mark_processed` and `tests/harness/test_state.py`.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from autoreplies.harness.state import HarnessState
from autoreplies.services.airtable import AirtableClient

logger = logging.getLogger(__name__)

LEAD_SENDER_QUERY = (
    "from:(noreply@email.streeteasy.com OR rentalclientservices@zillowrentals.com)"
)


# ── Collaborator protocols ────────────────────────────────────────────────────


class MessageLister(Protocol):
    """Minimal Gmail surface the poller needs.

    Implementations must page internally — `max_results` is the per-page hint
    (matching Gmail's `maxResults`), and the return is the full result set for
    the query. Each tuple is `(gmail_message_id, internal_date_unix_ms)`.
    """

    def list_messages(
        self, *, query: str, max_results: int = 100
    ) -> list[tuple[str, int]]: ...


DispatchFn = Callable[[str, str], None]
"""Process a single message. Args: (gmail_message_id, mailbox_email)."""


# ── Cooperative shutdown ──────────────────────────────────────────────────────


class ShutdownFlag:
    """Cooperative shutdown signal backed by a `threading.Event`.

    Using an Event (not a bare bool + `time.sleep`) is what lets the poll-loop
    interval wait abort the instant a signal arrives. `time.sleep` does not
    raise on signal delivery on POSIX, so SIGTERM during a 60s wait would mean
    up to 60s latency to exit.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Block up to `timeout` seconds; return True if shutdown was requested."""
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
    """TTL cache for the agent mailbox list.

    Refreshes every `ttl_seconds` or when `invalidate()` is called (e.g. via
    SIGHUP). The cache is bound to the worker process lifetime and is not
    persisted — a process restart re-fetches from Airtable.
    """

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

    def get(self, airtable: AirtableClient) -> list[str]:
        if self._cached is None or (self._now() - self._fetched_at) >= self._ttl:
            self._cached = airtable.list_monitored_autoreply_inboxes()
            self._fetched_at = self._now()
        return list(self._cached)

    def invalidate(self) -> None:
        self._cached = None
        self._fetched_at = 0.0


def discover_monitored_mailboxes(
    airtable: AirtableClient,
    *,
    cache: MailboxCache | None = None,
) -> list[str]:
    """Resolve the current monitored mailbox list.

    Returns legacy autoreply inboxes for users with Autoreply Enabled (Agent) = TRUE.
    Honors `cache` if provided.
    """
    if cache is None:
        return airtable.list_monitored_autoreply_inboxes()
    return cache.get(airtable)


# ── poll_once ─────────────────────────────────────────────────────────────────


@dataclass
class PollStats:
    """Per-call counters returned by `poll_once`; useful for logs and tests."""

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
    state: HarnessState,
    dispatch: DispatchFn,
    bootstrap_lookback_seconds: int,
    max_results: int = 100,
    now_ms: Callable[[], int] = _default_now_ms,
) -> PollStats:
    """List + dispatch unprocessed lead messages for one mailbox.

    On first call for `mailbox` (no cursor stored), seeds `last_seen` to
    `now - bootstrap_lookback_seconds * 1000`. Subsequent calls resume from the
    persisted cursor. The cursor advances to the max `internalDate` across the
    batch — including dedup-skipped and failed messages, so we don't refetch
    them indefinitely.
    """
    stats = PollStats()

    last_seen_ms = state.get_last_seen(mailbox)
    if last_seen_ms is None:
        last_seen_ms = now_ms() - bootstrap_lookback_seconds * 1000

    # Gmail's `after:` operator takes whole seconds.
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
    """Tunables for `run_forever`. All values come from `Settings` in normal use."""

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
    airtable: AirtableClient,
    gmail_factory: Callable[[str], MessageLister],
    state: HarnessState,
    dispatch: DispatchFn,
    config: PollerConfig,
    shutdown: ShutdownFlag | None = None,
) -> None:
    """Loop forever: discover mailboxes, poll each, sleep, repeat.

    Mailboxes are polled sequentially with a small inter-mailbox sleep to
    spread Gmail API calls. SIGTERM/SIGINT request a graceful shutdown — the
    in-flight `dispatch` finishes, then the loop exits before the next mailbox.
    SIGHUP invalidates the mailbox cache so a freshly-added agent picks up on
    the next iteration without a restart.
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
            logger.exception("run_forever: monitored mailbox discovery failed; retrying after interval")
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
                # A failure on one mailbox must not take down the others.
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
