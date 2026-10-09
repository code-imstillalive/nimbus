"""nimbus #1656 (part of #1652): HAEO's reader treated a hole in NEM PD7DAY's
day 2-7 series (up to 9 h around 12:30, purcell-lab/nem_pd7day#235) as a real
$0 price. Nimbus's reader must not: a hole is not covered, never priced at 0,
and a row whose published `value` is null is missing, not $0."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import _solver_path  # noqa: F401
from price_intervals import coverage, on_grid, pd7day_intervals

AEST = timezone(timedelta(hours=10))
T0 = datetime(2026, 10, 10, 0, 0, tzinfo=AEST)


def _row(start: datetime, value):
    return {
        "time": start.isoformat(),
        "nemtime": (start + timedelta(minutes=30)).isoformat(),
        "value": value,
    }


def _series():
    rows = [
        _row(T0 + timedelta(minutes=30 * k), 0.12) for k in range(24)
    ]  # 00:00-12:00
    rows.append(_row(T0 + timedelta(hours=12), None))  # integration could not calibrate
    # the 9 h hole: nothing from 12:00 to 21:30
    rows += [
        _row(T0 + timedelta(hours=21, minutes=30 + 30 * k), 0.31) for k in range(5)
    ]
    return rows


def test_the_hole_is_reported_as_a_gap():
    cov = coverage(pd7day_intervals(_series()))
    assert cov.gaps == (
        (T0 + timedelta(hours=12), T0 + timedelta(hours=21, minutes=30)),
    )
    assert cov.covered_hours == 12.0 + 2.5


def test_a_null_value_row_is_missing_not_zero():
    starts = [i.start for i in pd7day_intervals(_series())]
    assert T0 + timedelta(hours=12) not in starts


def test_no_period_in_the_hole_is_priced_at_zero_or_counted_as_real():
    grid = [T0 + timedelta(minutes=30 * k) for k in range(48)]
    values, real = on_grid(pd7day_intervals(_series()), grid)
    for t, v, r in zip(grid, values, real, strict=True):
        in_hole = T0 + timedelta(hours=12) <= t < T0 + timedelta(hours=21, minutes=30)
        if in_hole:
            assert r is False, t
            assert v != 0.0, t  # held, flagged not real: never free energy
        else:
            assert r is True, t
