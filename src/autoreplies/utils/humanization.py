"""Humanization delay — compute when an autoreply should actually go out.

Pure function; inject `now` and `rng` for determinism in tests.
All defaults are configurable via Settings (humanization_* knobs).
"""

from __future__ import annotations

import random
import zoneinfo
from datetime import UTC, datetime, timedelta


def compute_send_at(
    now: datetime,
    *,
    tz_name: str = "America/New_York",
    working_hours: tuple[int, int] = (8, 23),
    within_jitter_seconds: tuple[int, int] = (60, 300),
    out_of_hours_jitter_seconds: tuple[int, int] = (0, 3600),
    rng: random.Random | None = None,
) -> datetime:
    """Compute when an autoreply should go out.

    - Within working_hours (start inclusive, end exclusive, local tz):
      now + uniform(within_jitter_seconds). Hard ceiling: max 5 min per product spec.
    - Outside working_hours: next working_hours[0] in local tz
      + uniform(out_of_hours_jitter_seconds).
    - All 7 days treated as working days (StreetEasy/Zillow leads arrive on weekends).

    `now` must be timezone-aware. Returns an aware UTC datetime.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    _rng = rng or random.Random()
    tz = zoneinfo.ZoneInfo(tz_name)
    local_now = now.astimezone(tz)
    start_h, end_h = working_hours

    if start_h <= local_now.hour < end_h:
        jitter = _rng.randint(within_jitter_seconds[0], within_jitter_seconds[1])
        send_at = now + timedelta(seconds=jitter)
    else:
        # Next working-hours start: today if not yet reached, tomorrow otherwise.
        candidate = local_now.replace(hour=start_h, minute=0, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        jitter = _rng.randint(out_of_hours_jitter_seconds[0], out_of_hours_jitter_seconds[1])
        send_at = candidate + timedelta(seconds=jitter)

    return send_at.astimezone(UTC)
