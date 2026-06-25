"""Tests for workers/poller_state.py — PollerState."""

from pathlib import Path

import pytest

from autoreplies.workers.poller_state import PollerState


@pytest.fixture
def state(tmp_path: Path) -> PollerState:
    return PollerState(tmp_path / "poller.sqlite")


def test_get_last_seen_returns_none_initially(state: PollerState) -> None:
    assert state.get_last_seen("agent@pearnyc.com") is None


def test_set_and_get_last_seen(state: PollerState) -> None:
    state.set_last_seen("agent@pearnyc.com", 1_000_000_000_000)
    assert state.get_last_seen("agent@pearnyc.com") == 1_000_000_000_000


def test_set_last_seen_upserts(state: PollerState) -> None:
    state.set_last_seen("agent@pearnyc.com", 1_000)
    state.set_last_seen("agent@pearnyc.com", 2_000)
    assert state.get_last_seen("agent@pearnyc.com") == 2_000


def test_was_processed_returns_false_initially(state: PollerState) -> None:
    assert state.was_processed("msg-abc") is False


def test_mark_and_check_processed(state: PollerState) -> None:
    state.mark_processed("msg-abc", "agent@pearnyc.com")
    assert state.was_processed("msg-abc") is True


def test_mark_processed_with_error(state: PollerState) -> None:
    state.mark_processed("msg-xyz", "agent@pearnyc.com", error="RuntimeError: boom")
    assert state.was_processed("msg-xyz") is True


def test_mark_processed_with_inquiry_id(state: PollerState) -> None:
    state.mark_processed("msg-qrs", "agent@pearnyc.com", inquiry_id="recINQ1")
    assert state.was_processed("msg-qrs") is True


def test_independent_mailboxes(state: PollerState) -> None:
    state.set_last_seen("a@pearnyc.com", 100)
    state.set_last_seen("b@pearnyc.com", 200)
    assert state.get_last_seen("a@pearnyc.com") == 100
    assert state.get_last_seen("b@pearnyc.com") == 200


def test_creates_parent_directories(tmp_path: Path) -> None:
    deep_path = tmp_path / "deep" / "nested" / "poller.sqlite"
    s = PollerState(deep_path)
    assert deep_path.exists()
    s.close()


# ── replied_fingerprints dedup ────────────────────────────────────────────────


def test_record_and_lookup_fingerprint(state: PollerState) -> None:
    state.record_reply(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-abc",
        gmail_message_id="msg-A",
        inquiry_id="recINQ1",
    )
    result = state.recent_duplicate_message_id(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-abc",
        exclude_message_id="msg-B",
        within_seconds=3600,
    )
    assert result == "msg-A"


def test_exclude_self_returns_none(state: PollerState) -> None:
    """Querying with exclude_message_id=A when only A is recorded returns None."""
    state.record_reply(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-abc",
        gmail_message_id="msg-A",
        inquiry_id="recINQ1",
    )
    result = state.recent_duplicate_message_id(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-abc",
        exclude_message_id="msg-A",
        within_seconds=3600,
    )
    assert result is None


def test_exclude_self_returns_prior_when_different_exclude(state: PollerState) -> None:
    """Recording A, then querying with exclude_message_id=B returns A."""
    state.record_reply(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-abc",
        gmail_message_id="msg-A",
        inquiry_id="recINQ1",
    )
    result = state.recent_duplicate_message_id(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-abc",
        exclude_message_id="msg-B",
        within_seconds=3600,
    )
    assert result == "msg-A"


def test_window_expiry_returns_none(state: PollerState) -> None:
    """A fingerprint recorded at time T is not returned when queried far in the future."""
    from datetime import UTC, datetime, timedelta

    past = datetime(2020, 1, 1, tzinfo=UTC)
    state.record_reply(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-xyz",
        gmail_message_id="msg-old",
        inquiry_id=None,
        now=past,
    )
    far_future = past + timedelta(days=365)
    result = state.recent_duplicate_message_id(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-xyz",
        exclude_message_id="msg-new",
        within_seconds=3600,
        now=far_future,
    )
    assert result is None


def test_mailbox_scoping(state: PollerState) -> None:
    """A fingerprint recorded for mailbox A does not match mailbox B."""
    state.record_reply(
        mailbox="agent-a@pearnyc.com",
        fingerprint="fp-shared",
        gmail_message_id="msg-A",
        inquiry_id=None,
    )
    result = state.recent_duplicate_message_id(
        mailbox="agent-b@pearnyc.com",
        fingerprint="fp-shared",
        exclude_message_id="msg-B",
        within_seconds=3600,
    )
    assert result is None


def test_unknown_fingerprint_returns_none(state: PollerState) -> None:
    result = state.recent_duplicate_message_id(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-never-recorded",
        exclude_message_id="msg-X",
        within_seconds=3600,
    )
    assert result is None


# ── replied_persons dedup (Phase 2) ───────────────────────────────────────────


def test_record_and_lookup_person_reply(state: PollerState) -> None:
    state.record_person_reply(
        person_id="person-abc",
        mailbox="agent@pearnyc.com",
        gmail_message_id="msg-A",
    )
    result = state.recent_person_reply(
        person_id="person-abc",
        mailbox="agent@pearnyc.com",
        exclude_message_id="msg-B",
        within_seconds=1209600,
    )
    assert result is True


def test_person_exclude_self_returns_false(state: PollerState) -> None:
    """Querying with exclude_message_id=A when only A is recorded returns False."""
    state.record_person_reply(
        person_id="person-abc",
        mailbox="agent@pearnyc.com",
        gmail_message_id="msg-A",
    )
    result = state.recent_person_reply(
        person_id="person-abc",
        mailbox="agent@pearnyc.com",
        exclude_message_id="msg-A",
        within_seconds=1209600,
    )
    assert result is False


def test_person_exclude_self_returns_prior_when_different_exclude(state: PollerState) -> None:
    """Recording A, then querying with exclude_message_id=B returns True."""
    state.record_person_reply(
        person_id="person-abc",
        mailbox="agent@pearnyc.com",
        gmail_message_id="msg-A",
    )
    result = state.recent_person_reply(
        person_id="person-abc",
        mailbox="agent@pearnyc.com",
        exclude_message_id="msg-B",
        within_seconds=1209600,
    )
    assert result is True


def test_person_window_expiry_returns_false(state: PollerState) -> None:
    """A reply recorded at time T is not returned when queried far in the future."""
    from datetime import UTC, datetime, timedelta

    past = datetime(2020, 1, 1, tzinfo=UTC)
    state.record_person_reply(
        person_id="person-abc",
        mailbox="agent@pearnyc.com",
        gmail_message_id="msg-old",
        now=past,
    )
    far_future = past + timedelta(days=365)
    result = state.recent_person_reply(
        person_id="person-abc",
        mailbox="agent@pearnyc.com",
        exclude_message_id="msg-new",
        within_seconds=1209600,
        now=far_future,
    )
    assert result is False


def test_person_mailbox_scoping(state: PollerState) -> None:
    """A reply recorded for mailbox A does not match mailbox B."""
    state.record_person_reply(
        person_id="person-shared",
        mailbox="agent-a@pearnyc.com",
        gmail_message_id="msg-A",
    )
    result = state.recent_person_reply(
        person_id="person-shared",
        mailbox="agent-b@pearnyc.com",
        exclude_message_id="msg-B",
        within_seconds=1209600,
    )
    assert result is False


def test_person_unknown_returns_false(state: PollerState) -> None:
    result = state.recent_person_reply(
        person_id="person-never-recorded",
        mailbox="agent@pearnyc.com",
        exclude_message_id="msg-X",
        within_seconds=1209600,
    )
    assert result is False
