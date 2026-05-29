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
