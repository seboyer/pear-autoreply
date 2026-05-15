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
