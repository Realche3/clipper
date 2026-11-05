"""Pure slot math for the post queue. No DB, no clock: callers pass `now` and taken slots.

All datetimes are naive local time (the posting machine's clock), matching POSTING_WINDOW.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta

MAX_STEPS = 10_000  # safety bound; a valid config always finds a slot long before this


def parse_window(spec: str) -> tuple[time, time]:
    """'10:00-23:30' -> (10:00, 23:30). Windows crossing midnight are not supported."""
    try:
        start_s, end_s = spec.split("-")
        start, end = time.fromisoformat(start_s.strip()), time.fromisoformat(end_s.strip())
    except ValueError as e:
        raise ValueError(f"POSTING_WINDOW must look like '10:00-23:30', got {spec!r}") from e
    if start >= end:
        raise ValueError(f"POSTING_WINDOW start must be before end, got {spec!r}")
    return start, end


def next_slot(
    now: datetime,
    taken: Iterable[datetime],
    *,
    interval: timedelta,
    window: tuple[time, time],
    daily_cap: int,
) -> datetime:
    """Earliest time >= now that is inside the window, at least `interval` away from every
    taken slot, on a day with fewer than `daily_cap` slots. Fills gaps left by cancellations.
    """
    if daily_cap < 1:
        raise ValueError("daily cap must be at least 1")
    taken = sorted(taken)
    start, end = window
    candidate = now.replace(second=0, microsecond=0)
    if candidate < now:
        candidate += timedelta(minutes=1)

    for _ in range(MAX_STEPS):
        day_start = datetime.combine(candidate.date(), start)
        day_end = datetime.combine(candidate.date(), end)
        next_day_start = day_start + timedelta(days=1)

        if candidate < day_start:
            candidate = day_start
            continue
        if candidate > day_end:
            candidate = next_day_start
            continue
        if sum(1 for t in taken if t.date() == candidate.date()) >= daily_cap:
            candidate = next_day_start
            continue
        clashes = [t for t in taken if abs(t - candidate) < interval]
        if clashes:
            candidate = max(clashes) + interval
            continue
        return candidate

    raise RuntimeError("could not find a posting slot; check POSTING_WINDOW and caps")
