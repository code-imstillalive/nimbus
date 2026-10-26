"""History is averaged over time, not over samples (#1008) (#1837)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import solver_writer

TZ = timezone(timedelta(hours=10))
T0 = datetime(2026, 9, 15, 0, 0, tzinfo=TZ)


class TestResampleHistoryMeanIsTimeWeighted(unittest.TestCase):
    def test_a_value_carried_in_from_before_the_window_is_weighted(self):
        """A sample recorded BEFORE the window still holds into it --
        that is what 'state change' recording means, and ignoring it
        would treat a steady signal as absent."""
        pts = [
            (T0 - timedelta(minutes=30), 8.0),
            (T0 + timedelta(minutes=45), 0.0),
        ]
        mean = solver_writer.resample_history_mean(pts, [T0], period_hours=1.0)[0]
        # 8.0 holds the first 45 min, 0.0 the last 15.
        self.assertAlmostEqual(mean, (8.0 * 2700 + 0.0 * 900) / 3600, places=9)
        self.assertAlmostEqual(mean, 6.0, places=9)

    def test_out_of_order_or_duplicate_timestamps_do_not_explode(self):
        """Recorder rows can share a timestamp. A zero-length span must
        contribute nothing rather than divide by zero."""
        pts = [
            (T0, 1.0),
            (T0, 2.0),
            (T0 + timedelta(minutes=30), 3.0),
        ]
        mean = solver_writer.resample_history_mean(pts, [T0], period_hours=1.0)[0]
        self.assertTrue(-100.0 < mean < 100.0)


if __name__ == "__main__":
    unittest.main()
