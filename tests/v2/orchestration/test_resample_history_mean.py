"""History is averaged over time, not over samples (#1008) (#1837)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import solver_writer

TZ = timezone(timedelta(hours=10))
T0 = datetime(2026, 9, 15, 0, 0, tzinfo=TZ)


class TestResampleHistoryMeanIsTimeWeighted(unittest.TestCase):
    def test_a_brief_spike_cannot_outweigh_a_long_plateau(self):
        """The shape that caused this: one 40 kW spike lasting a few
        seconds, then a steady -2 kW for the rest of the hour.

        A plain sample average calls this hour +19 kW. The battery spent
        99.8% of it charging at 2 kW."""
        pts = [
            (T0, 40.0),
            (T0 + timedelta(seconds=6), -2.0),
        ]
        mean = solver_writer.resample_history_mean(pts, [T0], period_hours=1.0)[0]
        # 40 held 6 s, -2 held 3594 s.
        expected = (40.0 * 6 + -2.0 * 3594) / 3600
        self.assertAlmostEqual(mean, expected, places=9)
        self.assertLess(mean, 0.0, "an hour spent charging must not read as discharge")
        self.assertAlmostEqual(mean, -1.93, places=2)

    def test_the_plain_sample_average_would_have_said_the_opposite(self):
        """Pins the size of the defect rather than merely its direction:
        the old behaviour is off by more than 20 kW on this hour."""
        pts = [
            (T0, 40.0),
            (T0 + timedelta(seconds=6), -2.0),
        ]
        mean = solver_writer.resample_history_mean(pts, [T0], period_hours=1.0)[0]
        plain = (40.0 + -2.0) / 2
        self.assertGreater(abs(plain - mean), 20.0)

    def test_evenly_spaced_samples_are_unchanged(self):
        """The load-bearing backward-compatibility claim: when samples
        ARE evenly spaced, time-weighting and sample-averaging agree, so
        every well-behaved signal scores exactly as before."""
        pts = [
            (T0 + timedelta(minutes=15 * i), v)
            for i, v in enumerate([1.0, 2.0, 3.0, 4.0])
        ]
        mean = solver_writer.resample_history_mean(pts, [T0], period_hours=1.0)[0]
        self.assertAlmostEqual(mean, (1.0 + 2.0 + 3.0 + 4.0) / 4, places=9)

    def test_energy_over_a_day_matches_a_known_duty_cycle(self):
        """End to end on the property that actually matters: integrating
        the resampled series must return the energy that really moved.

        One hour at 10 kW then twenty-three at 0 is 10 kWh. Sample
        averaging gets this right only by accident; duration weighting
        gets it right by construction."""
        pts = [(T0, 10.0), (T0 + timedelta(hours=1), 0.0)]
        grid = [T0 + timedelta(hours=h) for h in range(24)]
        series = solver_writer.resample_history_mean(pts, grid, period_hours=1.0)
        self.assertAlmostEqual(sum(series) * 1.0, 10.0, places=9)

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
