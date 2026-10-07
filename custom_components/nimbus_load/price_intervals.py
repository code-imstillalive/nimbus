"""Provider price rows -> explicit price intervals (nimbus #1550).

Every pricing provider publishes its forecast in its own shape. This module
turns one provider's native rows into `PriceInterval`s with explicit start
and end, so coverage is measured from the intervals themselves rather than
from a field name or a first-to-last span (#1550's live capture found both
misleading: an OpenADR `forecast_end` that was the last row's START, and a
seven-day span hiding a 24-hour hole).

One function per provider, added as each adapter lands. Pure: no Home
Assistant import, so the solver path and the tests call the same code.

AEMO NEM Data (nimbus #1578)
----------------------------
`cabberley/HA_AemoNemData`, domain `aemo_nem`. The regional
`current_30min_forecast` sensor's `forecast` attribute holds rows
`{start_time, end_time, price}`: 30-minute intervals, `price` already in
$/kWh. It is AEMO's WHOLESALE regional price, not anyone's retail import
tariff or feed-in payment, so Nimbus uses it only where a wholesale forecast
belongs (the regional spot forecast that extends a retail feed past its own
horizon), never as an import or export price.

* `price` is read as published. It is never divided by 1,000 again.
* Negative and zero prices are real and kept. A missing, non-numeric or
  non-finite price drops that ROW, never the whole forecast.
* A row with no usable start/end, or an end not after its start, is dropped.
* A duplicate start keeps the first row; a row starting inside the previous
  interval is dropped, so intervals never overlap.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

try:
    from .solver_shared import parse_iso
except ImportError:  # standalone/cron deployment (see solver_writer.py)
    from solver_shared import parse_iso  # type: ignore[no-redef]


@dataclass(frozen=True)
class PriceInterval:
    """One priced interval, [start, end), value in $/kWh."""

    start: datetime
    end: datetime
    value: float


AEMO_NEM_DOMAIN = "aemo_nem"
AEMO_NEM_FORECAST_KEY = "current_30min_forecast"
_AEMO_ROW_KEYS = ("start_time", "end_time", "price")


def is_aemo_nem_rows(rows: Any) -> bool:
    """True when `rows` is AEMO NEM Data's forecast shape: a non-empty list
    whose dict rows carry `start_time`, `end_time` and `price`."""
    if not isinstance(rows, list) or not rows:
        return False
    return any(
        isinstance(r, dict) and all(k in r for k in _AEMO_ROW_KEYS) for r in rows
    )


def aemo_nem_intervals(rows: Iterable[Any]) -> list[PriceInterval]:
    """AEMO NEM Data `forecast` rows -> ordered, non-overlapping intervals."""
    parsed: list[PriceInterval] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            start = parse_iso(row["start_time"])
            end = parse_iso(row["end_time"])
            value = float(row["price"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not math.isfinite(value) or end <= start:
            continue
        parsed.append(PriceInterval(start, end, value))
    parsed.sort(key=lambda i: i.start)
    out: list[PriceInterval] = []
    for interval in parsed:
        if out and interval.start < out[-1].end:
            continue  # duplicate start or overlap: the first row stands
        out.append(interval)
    return out


@dataclass(frozen=True)
class Coverage:
    """What a set of intervals actually covers."""

    first_start: datetime | None
    final_end: datetime | None
    covered_hours: float
    gaps: tuple[tuple[datetime, datetime], ...]


def coverage(intervals: list[PriceInterval]) -> Coverage:
    """Coverage measured from the intervals: the final END (not the last
    start), the hours actually priced, and every hole between intervals."""
    if not intervals:
        return Coverage(None, None, 0.0, ())
    gaps = tuple((a.end, b.start) for a, b in pairwise(intervals) if b.start > a.end)
    covered = sum((i.end - i.start for i in intervals), timedelta())
    return Coverage(
        intervals[0].start,
        intervals[-1].end,
        covered.total_seconds() / 3600.0,
        gaps,
    )


def as_step_points(intervals: list[PriceInterval]) -> list[tuple[datetime, float]]:
    """(start, value) points for the solver's existing hold-the-latest-point
    lookups, which key on interval start."""
    return [(i.start, i.value) for i in intervals]
