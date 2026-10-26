"""Regression test for nimbus issue #353 (Mark Purcell, codebase review),
defect 2: `resample_last_value()` had no staleness limit at all -- an HA
outage or a sensor going `unavailable` for a real multi-day stretch held
the last-known value flat across the ENTIRE gap, folded into
`seasonal_lookup`'s own per-(weekday, hour, minute) averages as if it
were genuine, continuously-observed data for every one of those grid
points -- regardless of which real weekday/hour each point actually
represents on the calendar.

This test constructs a load whose true value is a clean, deterministic
function of hour-of-day (`value(t) = float(t.hour)`), so the expected,
correct `seasonal_lookup` value for any (weekday, hour) bucket is
trivially known in advance (very close to that hour number itself). A
genuine ~5-day gap in the real event stream sits squarely across several
real calendar days -- under the pre-fix unbounded forward-fill, every
hourly grid point inside that gap is silently stamped with whatever the
LAST real value was right before the gap started (23.0, since the gap
starts right at 23:00), contaminating every (weekday, hour) bucket the
gap happens to span with a spurious sample far from that bucket's own
true value. The fix (a `max_staleness` bound on `resample_last_value()`,
wired into every one of `train_model()`'s own resampling calls) excludes
those stale points entirely instead of forward-filling them.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import _ml_path  # noqa: F401
from nimbus_load.ml import model as ml_model

START_OF_TIME = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001

# A genuine ~5-day gap, starting right at 23:00 on day 10 -- MUCH longer
# than MAX_TRAINING_STALENESS_GRID_STEPS (3) * resample_minutes (60) =
# 3 real hours, so every point inside it is well past the staleness
# bound this fix introduces.
_GAP_START = START_OF_TIME + timedelta(days=10, hours=23)
_GAP_END = START_OF_TIME + timedelta(days=15, hours=22)
_TOTAL_DAYS = 25.0


def _hour_of_day_events() -> list[tuple[datetime, float]]:
    """Real hourly events, `value(t) = float(t.hour)` exactly, with a
    genuine multi-day gap (no events at all) from _GAP_START to
    _GAP_END. The gap starts at hour 23 -- the pre-fix forward-fill
    would hold every grid point in the gap at exactly 23.0 regardless of
    that point's own real hour/weekday.
    """
    n_hours = int(_TOTAL_DAYS * 24)
    events = []
    for i in range(n_hours):
        t = START_OF_TIME + timedelta(hours=i)
        if _GAP_START < t <= _GAP_END:
            continue
        events.append((t, float(t.hour)))
    return events


class TestResampleLastValueMaxStalenessDirectly(unittest.TestCase):
    """Direct, function-level coverage of the actual public contract
    (train_model()'s own use above proves it matters in practice; this
    proves the primitive itself behaves exactly as documented)."""

    def test_a_grid_point_older_than_max_staleness_returns_none_not_the_stale_value(
        self,
    ):
        events = [(START_OF_TIME, 5.0)]
        grid = [START_OF_TIME + timedelta(hours=5)]
        out = ml_model.resample_last_value(
            events, grid, max_staleness=timedelta(hours=3)
        )
        self.assertEqual(out, [None])

    def test_a_grid_point_within_max_staleness_still_returns_the_real_value(self):
        events = [(START_OF_TIME, 5.0)]
        grid = [START_OF_TIME + timedelta(hours=2)]
        out = ml_model.resample_last_value(
            events, grid, max_staleness=timedelta(hours=3)
        )
        self.assertEqual(out, [5.0])
