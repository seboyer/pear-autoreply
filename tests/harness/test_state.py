"""Tests for harness/state.py — SQLite state store."""

from pathlib import Path

import pytest

from autoreplies.harness.state import HarnessState


@pytest.fixture()
def state(tmp_path: Path) -> HarnessState:
    return HarnessState(tmp_path / "test_harness.sqlite")


# ── WAL mode ──────────────────────────────────────────────────────────────────


def test_wal_mode_enabled(state: HarnessState) -> None:
    row = state._conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


# ── mailbox cursor ────────────────────────────────────────────────────────────


def test_get_last_seen_missing(state: HarnessState) -> None:
    assert state.get_last_seen("agent@pearnyc.com") is None


def test_set_and_get_last_seen(state: HarnessState) -> None:
    state.set_last_seen("agent@pearnyc.com", 1_700_000_000_000)
    assert state.get_last_seen("agent@pearnyc.com") == 1_700_000_000_000


def test_set_last_seen_upserts(state: HarnessState) -> None:
    state.set_last_seen("agent@pearnyc.com", 1_000)
    state.set_last_seen("agent@pearnyc.com", 2_000)
    assert state.get_last_seen("agent@pearnyc.com") == 2_000


def test_multiple_mailboxes_independent(state: HarnessState) -> None:
    state.set_last_seen("a@pearnyc.com", 1_000)
    state.set_last_seen("b@pearnyc.com", 9_000)
    assert state.get_last_seen("a@pearnyc.com") == 1_000
    assert state.get_last_seen("b@pearnyc.com") == 9_000


# ── message dedup ─────────────────────────────────────────────────────────────


def test_was_processed_false_initially(state: HarnessState) -> None:
    assert state.was_processed("msg-abc") is False


def test_mark_and_check_processed(state: HarnessState) -> None:
    state.mark_processed("msg-abc", "agent@pearnyc.com", "recINQ1", "recDRAFT1")
    assert state.was_processed("msg-abc") is True


def test_mark_processed_error_only(state: HarnessState) -> None:
    state.mark_processed("msg-fail", "agent@pearnyc.com", error="timeout")
    assert state.was_processed("msg-fail") is True


def test_mark_processed_idempotent(state: HarnessState) -> None:
    """Re-marking the same message-id replaces, doesn't duplicate."""
    state.mark_processed("msg-dup", "agent@pearnyc.com", "recINQ1", "recDRAFT1")
    state.mark_processed("msg-dup", "agent@pearnyc.com", "recINQ1", "recDRAFT1")
    count = state._conn.execute(
        "SELECT COUNT(*) FROM processed_messages WHERE gmail_message_id = ?",
        ("msg-dup",),
    ).fetchone()[0]
    assert count == 1


def test_parent_dir_created(tmp_path: Path) -> None:
    """HarnessState creates missing parent directories."""
    nested = tmp_path / "deep" / "nested" / "harness.sqlite"
    s = HarnessState(nested)
    assert nested.exists()
    s.close()


# ── replied_fingerprints dedup ────────────────────────────────────────────────


def test_record_and_lookup_fingerprint(state: HarnessState) -> None:
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


def test_exclude_self_returns_none(state: HarnessState) -> None:
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


def test_exclude_self_returns_prior_when_different_exclude(state: HarnessState) -> None:
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


def test_window_expiry_returns_none(state: HarnessState) -> None:
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


def test_mailbox_scoping(state: HarnessState) -> None:
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


def test_unknown_fingerprint_returns_none(state: HarnessState) -> None:
    result = state.recent_duplicate_message_id(
        mailbox="agent@pearnyc.com",
        fingerprint="fp-never-recorded",
        exclude_message_id="msg-X",
        within_seconds=3600,
    )
    assert result is None


# ── replied_persons dedup (Phase 2) ───────────────────────────────────────────


def test_record_and_lookup_person_reply(state: HarnessState) -> None:
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


def test_person_exclude_self_returns_false(state: HarnessState) -> None:
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


def test_person_exclude_self_returns_prior_when_different_exclude(state: HarnessState) -> None:
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


def test_person_window_expiry_returns_false(state: HarnessState) -> None:
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


def test_person_mailbox_scoping(state: HarnessState) -> None:
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


def test_person_unknown_returns_false(state: HarnessState) -> None:
    result = state.recent_person_reply(
        person_id="person-never-recorded",
        mailbox="agent@pearnyc.com",
        exclude_message_id="msg-X",
        within_seconds=1209600,
    )
    assert result is False
