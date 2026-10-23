"""nimbus issue #1008 -- `resample_history_mean()` averaged SAMPLES, not
TIME, and the scorer's achieved battery series was wrong because of it.

Home Assistant's recorder stores state CHANGES. Samples are therefore
irregularly spaced: dense while a value is moving, sparse while it holds.
`sum(vals) / len(vals)` weights a two-second spike exactly as heavily as
a forty-minute plateau.

That is not a hypothetical. Measured on the reference household for
2026-09-15, three independent accounts of the same battery, same day:

    scorer (sample mean)    99.675 kWh in / 107.428 out  = -7.75 kWh net
    inverter counters      106.6   kWh in / 100.9   out  = +5.70 kWh net
    HA's own statistics    mean -0.2229 kW x 24 h        = -5.35 kWh net

(This module's sign convention is positive = discharge, so a negative
net is net CHARGING.) **HA's statistics mean is time-weighted**, which is
why it agrees with the inverters' cumulative counters to 0.35 kWh while
the scorer disagreed by 13.5 kWh -- and inverted the day's direction
entirely: the reconstructed SoC ran 16.07% -> 0.44% on a day the real
pack went 17.4% -> ~20%.

The household's battery jumps to +-40 kW and then sits flat. Every step
of a spike writes a recorder row; a steady plateau writes almost none. So
the plain mean was systematically dragged toward the spikes.

Worth recording what was NOT wrong, because three plausible causes were
investigated and eliminated first:

  * the sensor itself -- its own cumulative counters reconcile with the
    per-inverter counters to the decimal, and with measured stored
    energy to ~1%;
  * the sign convention -- `logger_battery_power` reads +13.2 kW while
    the inverter reports `discharging_power = 13201 W`, so positive =
    discharge is correct and `sign_inverted` is rightly unset;
  * per-row units (#843) -- a real latent issue on this read path, but
    not the cause here.

The averaging was.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import _solver_path  # noqa: F401
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

    def test_a_single_sample_holds_the_whole_period(self):
        pts = [(T0 + timedelta(minutes=30), 5.0)]
        mean = solver_writer.resample_history_mean(pts, [T0], period_hours=1.0)[0]
        # Nothing precedes the window, so the first half hour is
        # genuinely unobserved and is not weighted at all -- weighting
        # it at the default would invent a reading. The 5.0 that WAS
        # observed holds the rest.
        self.assertAlmostEqual(mean, 5.0, places=9)

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
