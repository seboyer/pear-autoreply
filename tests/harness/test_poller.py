"""Tests for harness/poller.py — Gmail query polling loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from autoreplies.harness.poller import (
    LEAD_SENDER_QUERY,
    MailboxCache,
    PollerConfig,
    ShutdownFlag,
    discover_monitored_mailboxes,
    poll_once,
    run_forever,
)
from autoreplies.harness.state import HarnessState
from autoreplies.services.airtable import AirtableClient

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def state(tmp_path: Path) -> HarnessState:
    return HarnessState(tmp_path / "harness.sqlite")


@pytest.fixture()
def airtable() -> AirtableClient:
    return MagicMock(spec=AirtableClient)


def _gmail_returning(messages: list[tuple[str, int]]) -> MagicMock:
    """Build a MessageLister mock that returns `messages` on list_messages."""
    g = MagicMock()
    g.list_messages.return_value = messages
    return g


# ── LEAD_SENDER_QUERY ─────────────────────────────────────────────────────────


def test_lead_sender_query_matches_plan() -> None:
    """The sender allowlist must mirror PLAN.md § 1 verbatim."""
    assert LEAD_SENDER_QUERY == (
        "from:(noreply@email.streeteasy.com OR rentalclientservices@zillowrentals.com)"
    )


# ── discover_monitored_mailboxes ──────────────────────────────────────────────


def test_discover_monitored_mailboxes_no_cache_calls_airtable(airtable: AirtableClient) -> None:
    airtable.list_monitored_autoreply_inboxes.return_value = ["a@pearnyc.com", "b@pearnyc.com"]
    assert discover_monitored_mailboxes(airtable) == ["a@pearnyc.com", "b@pearnyc.com"]
    airtable.list_monitored_autoreply_inboxes.assert_called_once()


def test_mailbox_cache_caches_within_ttl(airtable: AirtableClient) -> None:
    airtable.list_monitored_autoreply_inboxes.side_effect = [
        ["a@pearnyc.com"],
        ["a@pearnyc.com", "b@pearnyc.com"],
    ]
    clock = [0.0]
    cache = MailboxCache(ttl_seconds=3600, now=lambda: clock[0])

    assert discover_monitored_mailboxes(airtable, cache=cache) == ["a@pearnyc.com"]
    clock[0] = 100.0  # still well inside TTL
    assert discover_monitored_mailboxes(airtable, cache=cache) == ["a@pearnyc.com"]
    assert airtable.list_monitored_autoreply_inboxes.call_count == 1


def test_mailbox_cache_refreshes_after_ttl(airtable: AirtableClient) -> None:
    airtable.list_monitored_autoreply_inboxes.side_effect = [
        ["a@pearnyc.com"],
        ["a@pearnyc.com", "b@pearnyc.com"],
    ]
    clock = [0.0]
    cache = MailboxCache(ttl_seconds=10, now=lambda: clock[0])

    assert discover_monitored_mailboxes(airtable, cache=cache) == ["a@pearnyc.com"]
    clock[0] = 10.0  # at TTL boundary — refresh
    assert discover_monitored_mailboxes(airtable, cache=cache) == [
        "a@pearnyc.com",
        "b@pearnyc.com",
    ]
    assert airtable.list_monitored_autoreply_inboxes.call_count == 2


def test_mailbox_cache_invalidate_forces_refresh(airtable: AirtableClient) -> None:
    airtable.list_monitored_autoreply_inboxes.side_effect = [
        ["a@pearnyc.com"],
        ["c@pearnyc.com"],
    ]
    cache = MailboxCache(ttl_seconds=3600)

    discover_monitored_mailboxes(airtable, cache=cache)
    cache.invalidate()
    assert discover_monitored_mailboxes(airtable, cache=cache) == ["c@pearnyc.com"]
    assert airtable.list_monitored_autoreply_inboxes.call_count == 2


def test_mailbox_cache_get_returns_a_copy(airtable: AirtableClient) -> None:
    """Callers mutating the returned list must not poison the cache."""
    airtable.list_monitored_autoreply_inboxes.return_value = ["a@pearnyc.com"]
    cache = MailboxCache(ttl_seconds=3600)
    first = cache.get(airtable)
    first.append("rogue@pearnyc.com")
    assert cache.get(airtable) == ["a@pearnyc.com"]


# ── poll_once: bootstrap & query construction ─────────────────────────────────


def test_poll_once_bootstrap_seeds_lookback_window(state: HarnessState) -> None:
    """First call uses `now - lookback` as the after: filter."""
    fake_now_ms = 1_700_000_000_000  # arbitrary
    gmail = _gmail_returning([])

    stats = poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=lambda *_: None,
        bootstrap_lookback_seconds=60,
        now_ms=lambda: fake_now_ms,
    )

    gmail.list_messages.assert_called_once()
    query = gmail.list_messages.call_args.kwargs["query"]
    expected_after_sec = (fake_now_ms - 60_000) // 1000
    assert query == f"{LEAD_SENDER_QUERY} after:{expected_after_sec}"
    assert stats.fetched == 0


def test_poll_once_resumes_from_cursor(state: HarnessState) -> None:
    state.set_last_seen("a@pearnyc.com", 1_700_000_000_000)
    gmail = _gmail_returning([])

    poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=lambda *_: None,
        bootstrap_lookback_seconds=60,
    )

    query = gmail.list_messages.call_args.kwargs["query"]
    assert query == f"{LEAD_SENDER_QUERY} after:1700000000"


def test_poll_once_empty_batch_does_not_advance_cursor(state: HarnessState) -> None:
    state.set_last_seen("a@pearnyc.com", 1_000)
    gmail = _gmail_returning([])

    poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=lambda *_: None,
        bootstrap_lookback_seconds=60,
    )

    assert state.get_last_seen("a@pearnyc.com") == 1_000


# ── poll_once: dispatch & dedup ───────────────────────────────────────────────


def test_poll_once_dispatches_new_messages_and_marks_processed(state: HarnessState) -> None:
    gmail = _gmail_returning([("msg-1", 1_700_000_001_000), ("msg-2", 1_700_000_002_000)])
    dispatched: list[tuple[str, str]] = []

    stats = poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=lambda mid, mb: dispatched.append((mid, mb)),
        bootstrap_lookback_seconds=60,
    )

    assert dispatched == [("msg-1", "a@pearnyc.com"), ("msg-2", "a@pearnyc.com")]
    assert state.was_processed("msg-1") is True
    assert state.was_processed("msg-2") is True
    assert stats.fetched == 2
    assert stats.new == 2
    assert stats.succeeded == 2
    assert stats.failed == 0
    assert stats.skipped_dedup == 0


def test_poll_once_skips_already_processed(state: HarnessState) -> None:
    state.mark_processed("msg-1", "a@pearnyc.com", "recINQ1", "recDRAFT1")
    gmail = _gmail_returning([("msg-1", 1_700_000_001_000), ("msg-2", 1_700_000_002_000)])
    dispatched: list[str] = []

    stats = poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=lambda mid, _mb: dispatched.append(mid),
        bootstrap_lookback_seconds=60,
    )

    assert dispatched == ["msg-2"]
    assert stats.skipped_dedup == 1
    assert stats.new == 1
    assert stats.succeeded == 1


def test_poll_once_failed_dispatch_marks_with_error(state: HarnessState) -> None:
    gmail = _gmail_returning([("msg-good", 1_000), ("msg-bad", 2_000), ("msg-after", 3_000)])

    def dispatch(message_id: str, _mailbox: str) -> None:
        if message_id == "msg-bad":
            raise RuntimeError("boom")

    stats = poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=dispatch,
        bootstrap_lookback_seconds=60,
    )

    assert stats.succeeded == 2
    assert stats.failed == 1
    # Bad message is recorded so we don't retry it next poll.
    assert state.was_processed("msg-bad") is True

    row = state._conn.execute(
        "SELECT error FROM processed_messages WHERE gmail_message_id = ?",
        ("msg-bad",),
    ).fetchone()
    assert "RuntimeError" in row[0]
    assert "boom" in row[0]


# ── poll_once: cursor advancement ─────────────────────────────────────────────


def test_poll_once_advances_cursor_to_max_internal_date(state: HarnessState) -> None:
    state.set_last_seen("a@pearnyc.com", 1_700_000_000_000)
    gmail = _gmail_returning(
        [
            ("msg-1", 1_700_000_001_000),
            ("msg-2", 1_700_000_005_000),
            ("msg-3", 1_700_000_003_000),
        ]
    )

    poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=lambda *_: None,
        bootstrap_lookback_seconds=60,
    )

    assert state.get_last_seen("a@pearnyc.com") == 1_700_000_005_000


def test_poll_once_advances_cursor_past_dedup_skipped(state: HarnessState) -> None:
    """Dedup-skipped messages still contribute to max internalDate so we don't
    re-fetch them indefinitely (per advisor + brief)."""
    state.set_last_seen("a@pearnyc.com", 1_700_000_000_000)
    state.mark_processed("msg-old", "a@pearnyc.com")
    gmail = _gmail_returning([("msg-old", 1_700_000_009_000)])

    poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=lambda *_: None,
        bootstrap_lookback_seconds=60,
    )

    assert state.get_last_seen("a@pearnyc.com") == 1_700_000_009_000


def test_poll_once_advances_cursor_past_failed(state: HarnessState) -> None:
    """Failed messages count toward the cursor max — recovery is via `replay`."""
    state.set_last_seen("a@pearnyc.com", 1_700_000_000_000)
    gmail = _gmail_returning([("msg-bad", 1_700_000_007_000)])

    poll_once(
        mailbox="a@pearnyc.com",
        gmail_client=gmail,
        state=state,
        dispatch=_raise_runtime,
        bootstrap_lookback_seconds=60,
    )

    assert state.get_last_seen("a@pearnyc.com") == 1_700_000_007_000


def _raise_runtime(*_args: Any) -> None:
    raise RuntimeError("synthetic failure")


# ── run_forever ───────────────────────────────────────────────────────────────


def test_run_forever_iterates_each_mailbox_and_exits_on_shutdown(
    state: HarnessState, airtable: AirtableClient
) -> None:
    airtable.list_monitored_autoreply_inboxes.return_value = ["a@pearnyc.com", "b@pearnyc.com"]
    gmail = _gmail_returning([("msg-1", 1_700_000_001_000)])

    dispatched: list[tuple[str, str]] = []

    shutdown = ShutdownFlag()
    config = PollerConfig(
        interval_seconds=0,
        bootstrap_lookback_seconds=60,
        mailbox_inter_sleep_seconds=0,
        install_signal_handlers=False,
        _testing_max_iterations=1,
    )

    run_forever(
        airtable=airtable,
        gmail_factory=lambda _mb: gmail,
        state=state,
        dispatch=lambda mid, mb: dispatched.append((mid, mb)),
        config=config,
        shutdown=shutdown,
    )

    # Each mailbox got polled once.
    assert gmail.list_messages.call_count == 2
    # Dispatch ran for the first mailbox only; the second was already deduped
    # because list_messages returns the same list for both.
    assert dispatched == [("msg-1", "a@pearnyc.com")]


def test_run_forever_stops_when_shutdown_set_mid_iteration(
    state: HarnessState, airtable: AirtableClient
) -> None:
    airtable.list_monitored_autoreply_inboxes.return_value = ["a@pearnyc.com", "b@pearnyc.com"]
    gmail = _gmail_returning([])

    shutdown = ShutdownFlag()

    def gmail_factory(mailbox: str) -> Any:
        # Trip shutdown after the first mailbox is polled.
        if mailbox == "a@pearnyc.com":
            shutdown.request()
        return gmail

    config = PollerConfig(
        interval_seconds=0,
        bootstrap_lookback_seconds=60,
        mailbox_inter_sleep_seconds=0,
        install_signal_handlers=False,
    )

    run_forever(
        airtable=airtable,
        gmail_factory=gmail_factory,
        state=state,
        dispatch=lambda *_: None,
        config=config,
        shutdown=shutdown,
    )

    # Only the first mailbox was actually polled — second one's call was skipped.
    assert gmail.list_messages.call_count == 1


def test_run_forever_continues_after_per_mailbox_exception(
    state: HarnessState, airtable: AirtableClient
) -> None:
    airtable.list_monitored_autoreply_inboxes.return_value = ["a@pearnyc.com", "b@pearnyc.com"]
    gmail_good = _gmail_returning([])

    def gmail_factory(mailbox: str) -> Any:
        if mailbox == "a@pearnyc.com":
            raise RuntimeError("a is sick")
        return gmail_good

    config = PollerConfig(
        interval_seconds=0,
        bootstrap_lookback_seconds=60,
        mailbox_inter_sleep_seconds=0,
        install_signal_handlers=False,
        _testing_max_iterations=1,
    )

    run_forever(
        airtable=airtable,
        gmail_factory=gmail_factory,
        state=state,
        dispatch=lambda *_: None,
        config=config,
        shutdown=ShutdownFlag(),
    )

    # b@ still got polled despite a@ raising.
    assert gmail_good.list_messages.call_count == 1


def test_run_forever_handles_airtable_discovery_failure(
    state: HarnessState, airtable: AirtableClient
) -> None:
    airtable.list_monitored_autoreply_inboxes.side_effect = RuntimeError("airtable down")

    config = PollerConfig(
        interval_seconds=0,
        bootstrap_lookback_seconds=60,
        mailbox_inter_sleep_seconds=0,
        install_signal_handlers=False,
        _testing_max_iterations=1,
    )

    # Should not raise: a flaky discovery means an empty mailbox list for this
    # iteration; the loop tries again on the next tick.
    run_forever(
        airtable=airtable,
        gmail_factory=lambda _mb: _gmail_returning([]),
        state=state,
        dispatch=lambda *_: None,
        config=config,
        shutdown=ShutdownFlag(),
    )


def test_shutdown_flag_wait_returns_true_when_set() -> None:
    flag = ShutdownFlag()
    flag.request()
    assert flag.wait(0.01) is True


def test_shutdown_flag_wait_times_out_when_unset() -> None:
    flag = ShutdownFlag()
    assert flag.wait(0.01) is False
