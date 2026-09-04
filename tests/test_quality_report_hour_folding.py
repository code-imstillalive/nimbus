"""Regression test for nimbus issue #356 (Mark Purcell, codebase review),
item 4: `quality_report._hourly_means_by_key()` used to fold EVERY period
onto exactly 24 hour-of-day buckets via `hour_index = floor(cum) % 24`.

That was silently correct only because `compute_daily_quality_report()`
was, at the time, the only caller, and always passed exactly one real
calendar day (24h). Issue #316's own `nimbus_load.compute_quality_report`
service (added in v0.94.42) lets a caller request an ARBITRARY window via
`_compute_report_for_window(..., allow_partial=True)`, explicitly for
diagnostics/backfill/A-B comparison against windows longer than 24h. For
any such window, the old `% 24` genuinely averaged DIFFERENT REAL
CALENDAR DAYS' data into the same hour-of-day bucket -- e.g. a 48h
window's hour 24 (the start of day 2) landed in the exact same bucket as
hour 0 (the start of day 1), blending two distinct days' prices/dispatch
into one silently-wrong number.

The fix keys by REAL ELAPSED HOUR from day_start (no modulo) -- a <=24h
window (every existing caller before #316's service) is untouched
(confirmed separately by tests/test_dst_wall_clock_arithmetic.py, whose
existing 24h-window assertions still pass unmodified against this fix),
and a longer window now returns one row per real hour actually in it.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from itertools import pairwise
from zoneinfo import ZoneInfo

import _solver_path  # noqa: F401
import numpy as np
from solver.quality_report import _hourly_means_by_key

BRISBANE = ZoneInfo("Australia/Brisbane")


class TestLongWindowNoLongerFoldsDistinctDaysTogether(unittest.TestCase):
    def test_48h_window_returns_48_rows_not_24(self):
        n = 192  # 48h at 15-min resolution
        hours = np.full(n, 0.25)
        day_start = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
        out = _hourly_means_by_key(
            hours=hours,
            per_period={"load_kw": np.arange(n, dtype=float)},
            day_start=day_start,
        )
        self.assertEqual(len(out), 48)

    def test_hour_24_and_hour_0_are_no_longer_the_same_bucket(self):
        # Two real, genuinely different days' worth of data: day 1 is a
        # flat 10.0 kW load, day 2 is a flat 50.0 kW load. Under the old
        # `% 24` fold, hour 24 (day 2, hour 0) would land in the SAME
        # bucket as hour 0 (day 1, hour 0), averaging to 30.0 -- neither
        # day's own real value. The fix must keep them in separate rows.
        n = 192
        hours = np.full(n, 0.25)
        load = np.concatenate([np.full(96, 10.0), np.full(96, 50.0)])
        day_start = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
        out = _hourly_means_by_key(
            hours=hours, per_period={"load_kw": load}, day_start=day_start
        )
        keys = list(out.keys())
        self.assertEqual(len(keys), 48)
        # First real day's hour-0 row.
        self.assertEqual(out[keys[0]]["load_kw"], 10.0)
        # Second real day's hour-0 row (real elapsed hour 24) -- under the
        # pre-fix `% 24` this would be the SAME key as keys[0], silently
        # overwritten/averaged. Must now be its own distinct row, at its
        # own true value.
        self.assertEqual(out[keys[24]]["load_kw"], 50.0)
        self.assertNotEqual(keys[0], keys[24])

    def test_24h_window_still_produces_exactly_24_rows(self):
        # The normal, pre-#316 case -- must be completely unaffected.
        n = 96
        hours = np.full(n, 0.25)
        day_start = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
        out = _hourly_means_by_key(
            hours=hours,
            per_period={"load_kw": np.arange(n, dtype=float)},
            day_start=day_start,
        )
        self.assertEqual(len(out), 24)

    def test_partial_window_under_24h_produces_fewer_rows_not_24(self):
        # allow_partial=True also permits windows SHORTER than 24h --
        # this should produce only as many rows as real hours exist, not
        # a full 24 with the rest silently zero-filled from folding.
        n = 24  # 6h at 15-min resolution
        hours = np.full(n, 0.25)
        day_start = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
        out = _hourly_means_by_key(
            hours=hours,
            per_period={"load_kw": np.arange(n, dtype=float)},
            day_start=day_start,
        )
        self.assertEqual(len(out), 6)

    def test_keys_are_real_distinct_increasing_utc_instants(self):
        n = 192
        hours = np.full(n, 0.25)
        day_start = datetime(2026, 8, 24, 0, 0, tzinfo=BRISBANE)
        out = _hourly_means_by_key(
            hours=hours,
            per_period={"load_kw": np.arange(n, dtype=float)},
            day_start=day_start,
        )
        utc_keys = [datetime.fromisoformat(k).astimezone(UTC) for k in out]
        self.assertEqual(len(utc_keys), len(set(utc_keys)))
        for a, b in pairwise(utc_keys):
            self.assertLess(a, b)


if __name__ == "__main__":
    unittest.main()
