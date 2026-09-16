"""nimbus issue #843 (Mark Purcell, root-caused against real household
recorder data): resample_history_nearest() is documented as a
"nearest-at-or-before" lookup, but it always initialised its running
value to `pts[0][1]` -- the chronologically FIRST sample in the whole
list. When no sample actually preceded the requested instant, the inner
loop broke immediately and that initial value survived, so the function
returned a reading recorded AFTER the instant asked for, and never fell
through to the `default` its callers explicitly passed.

Real confirmed impact: an EV whose telemetry genuinely sleeps overnight
(zero recorder rows 00:00 -> 08:51, real Tesla behaviour) had every hour
from 00:00-07:00 inherit its first post-wake reading. That reading was
also a ~1000x-wrong W-vs-kW boot transient (the separate, still-open
half of #843), so the quality report published ~1,500 kW of achieved
battery power -- ~19x the household's real physical fleet ceiling -- for
eight straight hours. The unit bug corrupted ONE sample; this bug is
what smeared it across a third of the day.

The fix keeps backfill available as an explicit opt-in, because the
physically correct answer genuinely differs by signal type: a power FLOW
that has no data was not flowing, but an EV's SoC really did sit
unchanged while it was parked.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.LOCAL_TZ
DAY = datetime(2026, 9, 13, 0, 0, tzinfo=BRISBANE)


def _at(hour: float) -> datetime:
    return DAY + timedelta(hours=hour)


class TestNothingPrecedesTheRequestedInstant(unittest.TestCase):
    """The core #843 defect and its fix."""

    def test_returns_default_not_a_later_sample(self):
        # The real shape: sensor asleep until 08:51, first reading is a
        # corrupted W-scale boot transient.
        pts = [(_at(8.85), 1514.417), (_at(8.86), -774.818), (_at(8.87), -0.775)]
        got = solver_writer.resample_history_nearest(pts, [_at(h) for h in range(8)])
        self.assertEqual(
            got,
            [0.0] * 8,
            "every hour before the sensor's first real sample must fall "
            "through to `default`, never inherit a reading from the future",
        )

    def test_the_exact_regression_never_reappears(self):
        # Guards the specific published number from the live incident.
        pts = [(_at(8.85), 1514.417)]
        got = solver_writer.resample_history_nearest(pts, [_at(0.0)])
        self.assertNotAlmostEqual(
            got[0],
            1514.417,
            msg="00:00 must not inherit the 08:51 sample (#843's real blowup)",
        )
        self.assertEqual(got, [0.0])

    def test_honours_a_non_zero_default(self):
        pts = [(_at(12.0), 0.30)]
        got = solver_writer.resample_history_nearest(pts, [_at(6.0)], default=0.20)
        self.assertEqual(got, [0.20])

    def test_availability_mask_assume_away_intent_is_now_real(self):
        # _resolve_battery_participant_history()'s own stated posture:
        # "no real history means assume away (0.0)". Before #843 a car
        # whose first row of the day read "on" had every earlier period
        # masked as home -- the exact opposite.
        home_numeric = [(_at(9.0), 1.0), (_at(17.0), 0.0)]
        got = solver_writer.resample_history_nearest(
            home_numeric, [_at(h) for h in (0, 3, 6)], default=0.0
        )
        self.assertEqual(got, [0.0, 0.0, 0.0])


class TestBackfillFirstOptIn(unittest.TestCase):
    """STATE signals (SoC, price) genuinely want the old behaviour."""

    def test_backfills_the_earliest_real_sample(self):
        # A parked EV's SoC really did sit at its wake-up value all
        # night -- backfilling is physically right here, unlike a flow.
        soc = [(_at(8.85), 62.0), (_at(12.0), 80.0)]
        got = solver_writer.resample_history_nearest(
            soc, [_at(h) for h in (0, 4)], default=20.0, backfill_first=True
        )
        self.assertEqual(got, [62.0, 62.0])

    def test_falls_back_to_default_when_history_is_genuinely_empty(self):
        got = solver_writer.resample_history_nearest(
            [], [_at(0.0)], default=50.0, backfill_first=True
        )
        self.assertEqual(got, [50.0])

    def test_opt_in_does_not_override_a_real_preceding_sample(self):
        soc = [(_at(1.0), 40.0), (_at(9.0), 70.0)]
        got = solver_writer.resample_history_nearest(
            soc, [_at(5.0)], default=20.0, backfill_first=True
        )
        self.assertEqual(got, [40.0], "a real at-or-before sample always wins")


class TestUnchangedBehaviourWhereASamplePrecedes(unittest.TestCase):
    """The fix must not disturb the normal, well-covered path."""

    def test_picks_the_latest_sample_at_or_before(self):
        pts = [(_at(0.0), 1.0), (_at(2.0), 2.0), (_at(4.0), 3.0)]
        got = solver_writer.resample_history_nearest(
            pts, [_at(h) for h in (0, 1, 2, 3, 4, 5)]
        )
        self.assertEqual(got, [1.0, 1.0, 2.0, 2.0, 3.0, 3.0])

    def test_exact_boundary_is_inclusive(self):
        pts = [(_at(2.0), 7.0)]
        self.assertEqual(solver_writer.resample_history_nearest(pts, [_at(2.0)]), [7.0])

    def test_empty_history_returns_default_either_way(self):
        self.assertEqual(
            solver_writer.resample_history_nearest([], [_at(0.0)], default=0.05), [0.05]
        )


class TestResampleHistoryMeanInheritsTheFix(unittest.TestCase):
    """resample_history_mean() falls back to the nearest lookup for a
    period with zero real samples -- the exact path the EV hit."""

    def test_empty_period_before_any_sample_is_zero_not_backfilled(self):
        pts = [(_at(8.85), 1514.417), (_at(8.9), -0.775)]
        got = solver_writer.resample_history_mean(
            pts, [_at(h) for h in range(4)], period_hours=1.0
        )
        self.assertEqual(got, [0.0] * 4)

    def test_periods_with_real_samples_still_average_them(self):
        """nimbus issue #1008: the average is TIME-WEIGHTED, so this is
        no longer (2.0 + 4.0) / 2.

        Hand-computed over the one-hour window starting at 1.0:

            2.0 for  360 s   (1.1 -> 1.2)
            4.0 for 2880 s   (1.2 -> window end at 2.0)
                  ---------
                    3240 s

            (2*360 + 4*2880) / 3240 = 3.7778

        The 1.0 -> 1.1 gap before the first sample is NOT weighted at
        the default: nothing precedes the window, so there is no
        observation to carry in, and inventing one would bias every
        scored day's first period. A period with no samples at all
        still falls through to the nearest-lookup, which is where this
        class's "absent power reads as 0, never backfilled" convention
        lives.

        4.0 held for 48 of the 54 observed minutes. A plain sample
        average calls this 3.0, weighting a six-minute reading exactly
        as heavily as a forty-eight-minute one."""
        pts = [(_at(1.1), 2.0), (_at(1.2), 4.0)]
        got = solver_writer.resample_history_mean(pts, [_at(1.0)], period_hours=1.0)
        self.assertAlmostEqual(got[0], (2.0 * 360 + 4.0 * 2880) / 3240)
        self.assertAlmostEqual(got[0], 3.7778, places=4)


if __name__ == "__main__":
    unittest.main()
