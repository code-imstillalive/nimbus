"""A source peak stays at its own target interval through start and end
conversion, filtering and indexing (#1657, #1658) (#1837).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
import solver_writer

AEST = timezone(timedelta(hours=10))
BASE = 0.286
PEAK = 1.08
PEAK_START = datetime(2026, 10, 9, 5, 20, tzinfo=AEST)
PEAK_END = PEAK_START + timedelta(minutes=5)


def _forecast(tz) -> list[dict]:
    # LocalVolts-style 5-minute rows, 04:25 to 06:00 AEST, one peak at 05:20.
    t = datetime(2026, 10, 9, 4, 25, tzinfo=AEST)
    rows = []
    while t < datetime(2026, 10, 9, 6, 0, tzinfo=AEST):
        rows.append(
            {
                "time": t.astimezone(tz).isoformat(),
                "value": PEAK if t == PEAK_START else BASE,
            }
        )
        t += timedelta(minutes=5)
    return rows


@pytest.mark.parametrize("rows_tz", [AEST, UTC], ids=["rows_aest", "rows_utc"])
@pytest.mark.parametrize("now_tz", [AEST, UTC], ids=["now_aest", "now_utc"])
def test_a_0520_peak_never_reaches_a_period_that_starts_before_0520(rows_tz, now_tz):
    now = datetime(2026, 10, 9, 4, 25, 10, tzinfo=AEST).astimezone(now_tz)
    times, hours = solver_writer.build_tiered_grid(now)
    prices, real = solver_writer.resample_price_with_extrapolation(
        _forecast(rows_tz), "value", times, [], {}
    )

    assert prices[0] == BASE
    assert times[0] < PEAK_START
    for t, h, p in zip(times, hours, prices, strict=True):
        if t < PEAK_START:
            assert p == BASE, (t.astimezone(AEST), p)
        if t >= PEAK_END and t + timedelta(hours=h) <= datetime(
            2026, 10, 9, 6, 0, tzinfo=AEST
        ):
            assert p == BASE, (t.astimezone(AEST), p)

    # and the peak is still there, at its own interval
    at_peak = [p for t, p in zip(times, prices, strict=True) if t == PEAK_START]
    assert at_peak == [PEAK]
    assert real[0] is True
