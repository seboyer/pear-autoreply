"""Tests for utils/humanization.py — compute_send_at."""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from autoreplies.utils.humanization import compute_send_at


def _now(hour: int, minute: int = 0, *, tz_name: str = "America/New_York") -> datetime:
    """Build a fixed aware datetime in the given tz, converted to UTC."""
    import zoneinfo

    tz = zoneinfo.ZoneInfo(tz_name)
    local = datetime(2026, 5, 29, hour, minute, 0, tzinfo=tz)
    return local.astimezone(UTC)


class _FixedRng:
    """Deterministic RNG that always returns the same value."""

    def __init__(self, val: int) -> None:
        self._val = val

    def randint(self, a: int, b: int) -> int:
        return self._val


# ── Within-hours tests ────────────────────────────────────────────────────────


def test_within_hours_returns_utc() -> None:
    now = _now(10)  # 10am ET - within 8am-11pm window
    result = compute_send_at(now, rng=_FixedRng(120))
    assert result.tzinfo is not None
    # Not verifying UTC specifically, just aware
    assert result >= now


def test_within_hours_adds_jitter() -> None:
    now = _now(10)
    result = compute_send_at(now, rng=_FixedRng(180))
    delta = (result - now).total_seconds()
    assert abs(delta - 180) < 1  # 180s jitter


def test_within_hours_min_jitter() -> None:
    now = _now(10)
    result = compute_send_at(now, within_jitter_seconds=(60, 300), rng=_FixedRng(60))
    delta = (result - now).total_seconds()
    assert abs(delta - 60) < 1


def test_within_hours_max_jitter_is_300() -> None:
    now = _now(10)
    result = compute_send_at(now, within_jitter_seconds=(60, 300), rng=_FixedRng(300))
    delta = (result - now).total_seconds()
    assert abs(delta - 300) < 1


def test_within_hours_boundary_start() -> None:
    """8:00am ET is the start of the working window."""
    now = _now(8, 0)
    result = compute_send_at(now, rng=_FixedRng(60))
    assert result > now


def test_within_hours_boundary_before_end() -> None:
    """10:59pm ET is still within the 8am-11pm window."""
    now = _now(22, 59)
    result = compute_send_at(now, rng=_FixedRng(60))
    assert result > now


# ── Out-of-hours tests ────────────────────────────────────────────────────────


def test_out_of_hours_midnight_schedules_next_morning() -> None:
    """Midnight ET → schedule at 8am ET + jitter."""
    import zoneinfo

    now = _now(0, 0)  # midnight ET
    result = compute_send_at(now, rng=_FixedRng(0), out_of_hours_jitter_seconds=(0, 0))
    tz = zoneinfo.ZoneInfo("America/New_York")
    local_result = result.astimezone(tz)
    assert local_result.hour == 8
    assert local_result.minute == 0


def test_out_of_hours_23_schedules_next_morning() -> None:
    """11pm ET (boundary, excluded) → schedule next morning."""
    import zoneinfo

    now = _now(23)  # 11pm ET = end of window, excluded
    result = compute_send_at(now, rng=_FixedRng(0), out_of_hours_jitter_seconds=(0, 0))
    tz = zoneinfo.ZoneInfo("America/New_York")
    local_result = result.astimezone(tz)
    assert local_result.hour == 8
    # Must be tomorrow
    local_now = now.astimezone(tz)
    assert local_result.date() > local_now.date()


def test_out_of_hours_adds_jitter() -> None:

    now = _now(2)  # 2am ET — outside hours
    result_no_jitter = compute_send_at(
        now, rng=_FixedRng(0), out_of_hours_jitter_seconds=(0, 0)
    )
    result_with_jitter = compute_send_at(
        now, rng=_FixedRng(1800), out_of_hours_jitter_seconds=(0, 3600)
    )
    delta = (result_with_jitter - result_no_jitter).total_seconds()
    assert abs(delta - 1800) < 1


def test_out_of_hours_early_morning_before_8() -> None:
    """7:59am ET — still out-of-hours, should schedule for today at 8am."""
    import zoneinfo

    now = _now(7, 59)
    result = compute_send_at(now, rng=_FixedRng(0), out_of_hours_jitter_seconds=(0, 0))
    tz = zoneinfo.ZoneInfo("America/New_York")
    local_result = result.astimezone(tz)
    # 8am today (same date, since 7:59am hasn't passed 8am yet)
    local_now = now.astimezone(tz)
    assert local_result.hour == 8
    assert local_result.date() == local_now.date()


def test_requires_aware_datetime() -> None:
    naive = datetime(2026, 5, 29, 10, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_send_at(naive)


# ── DST boundary ──────────────────────────────────────────────────────────────


def test_dst_spring_forward_2am_skipped() -> None:
    """Spring-forward 2026-03-08 2am ET doesn't exist; compute_send_at must not crash."""
    import zoneinfo

    tz = zoneinfo.ZoneInfo("America/New_York")
    # Use 1:30am ET (before spring forward) as "now"
    local = datetime(2026, 3, 8, 1, 30, tzinfo=tz)
    now = local.astimezone(UTC)
    # Should schedule for 8am ET that same morning (candidate is today at 8am,
    # which is after 1:30am, so no +1 day needed)
    result = compute_send_at(now, rng=_FixedRng(0), out_of_hours_jitter_seconds=(0, 0))
    local_result = result.astimezone(tz)
    assert local_result.hour == 8
    assert local_result.date() == local.date()


# ── Randomness distribution ───────────────────────────────────────────────────


def test_within_hours_distribution() -> None:
    """Within-hours: send_at is always between now+min_jitter and now+max_jitter."""
    rng = random.Random(42)
    now = _now(10)
    for _ in range(50):
        result = compute_send_at(now, within_jitter_seconds=(60, 300), rng=rng)
        delta = (result - now).total_seconds()
        assert 60 <= delta <= 300
