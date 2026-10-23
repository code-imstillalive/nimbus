"""nimbus issue #843, option B (Mark Purcell's own A/B/C steer,
2026-09-13: "Option C -- both, with B ... landing first as the cheap
always-on guard").

A battery participant's own power sensor can briefly report a different
unit than it does now. Confirmed live on Mark's household:
`sensor.garage_my_t_my_pack_power` reports `W` for ~80 seconds as the car
wakes from sleep, then switches to `kW`:

    08:51:04.410  state=1514.417   unit="W"     <- really 1.514417 kW
    08:51:30.004  state=-774.818   (still W)
    08:52:27.078  state=-0.774994  <- kW from here on

`_kw_scale_factor()` reads the sensor's CURRENT live unit once and applies
that single scale to the whole day, so it cannot see a mid-day change --
that 1514.417 was scored as 1514.417 kW against a 25 kW-configured EV, and
the published quality report showed ~1,500 kW of achieved battery power
against a real ~80 kW fleet ceiling.

Option B is the cheap always-on guard: a reading that far outside a
participant's OWN configured envelope is corrupt by definition, whatever
produced it. It discards rather than clamps (see the helper's docstring),
and it filters the RAW history before resampling so the bad value never
enters a period mean at all.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import _solver_path  # noqa: F401
import solver_writer
from solver_inputs import battery_participants as battery_participants_inputs

BRISBANE = solver_writer.LOCAL_TZ
DAY = datetime(2026, 9, 13, 0, 0, tzinfo=BRISBANE)

# Mark's real configured envelope for both EVs.
EV_MAX_KW = 25.0
BOUND = EV_MAX_KW * battery_participants_inputs._PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE


def _at(hour: float) -> datetime:
    return DAY + timedelta(hours=hour)


def _drop(hist, *, scale=1.0, bound=BOUND):
    return battery_participants_inputs._drop_implausible_power_samples(
        hist,
        power_scale=scale,
        max_plausible_kw=bound,
        participant_name="ev_my",
        power_sensor="sensor.garage_my_t_my_pack_power",
    )


class TestTheRealIncident(unittest.TestCase):
    """Mark's actual 13 Sep rows, verbatim."""

    def test_the_1514_kw_wake_transient_is_discarded(self):
        hist = [
            (_at(8.851), 1514.417),  # the real corrupt row, W read as kW
            (_at(8.858), -774.818),  # still W
            (_at(8.874), -0.774994),  # genuine kW from here
            (_at(8.875), -0.42282),
        ]
        kept = _drop(hist)
        self.assertEqual(
            [v for _t, v in kept],
            [-0.774994, -0.42282],
            "both W-scale readings are beyond 10x a 25 kW envelope and must go; "
            "both genuine kW readings must survive untouched",
        )

    def test_genuine_readings_are_never_touched(self):
        # A real car charging hard at its 25 kW cap, plus discharge.
        hist = [(_at(9), 24.9), (_at(10), -25.0), (_at(11), 0.0)]
        self.assertEqual(_drop(hist), hist)


class TestTheBoundItself(unittest.TestCase):
    def test_plausible_hardware_overshoot_survives(self):
        # A 25 kW-rated device briefly reading 30 kW is real hardware
        # behaviour, not corruption -- 1.2x must never be discarded.
        hist = [(_at(9), 30.0)]
        self.assertEqual(_drop(hist), hist)

    def test_a_badly_conservative_config_still_passes_real_data(self):
        # Someone types 5.0 for a genuinely 25 kW charger. Every real
        # reading is then 5x the configured figure -- still inside a 10x
        # bound, so their real data is not silently thrown away.
        bound = (
            5.0 * battery_participants_inputs._PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE
        )
        hist = [(_at(9), 24.9), (_at(10), -25.0)]
        self.assertEqual(_drop(hist, bound=bound), hist)

    def test_a_unit_error_is_still_caught_with_room_to_spare(self):
        # Even against that same over-conservative 5 kW config, a real
        # 1000x W-vs-kW error is two orders of magnitude past the bound.
        bound = (
            5.0 * battery_participants_inputs._PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE
        )
        self.assertEqual(_drop([(_at(9), 1514.417)], bound=bound), [])

    def test_the_bound_is_applied_after_power_scale(self):
        # A genuinely-W sensor whose live unit IS "W" gets scale 0.001,
        # so 24900 W == 24.9 kW is real data and must survive.
        self.assertEqual(
            _drop([(_at(9), 24900.0)], scale=0.001),
            [(_at(9), 24900.0)],
        )
        # ...while 1514417 W (1514 kW) is still corrupt at that scale.
        self.assertEqual(_drop([(_at(9), 1514417.0)], scale=0.001), [])

    def test_sign_does_not_matter_only_magnitude(self):
        self.assertEqual(_drop([(_at(9), -1514.417)]), [])


class TestNoEnvelopeConfigured(unittest.TestCase):
    """`max_plausible_kw <= 0` means nothing to judge against."""

    def test_zero_bound_is_a_no_op_not_a_clamp_to_zero(self):
        hist = [(_at(9), 1514.417), (_at(10), -0.77)]
        self.assertEqual(
            _drop(hist, bound=0.0),
            hist,
            "an unconfigured envelope must pass everything through "
            "untouched, never discard everything as 'above zero'",
        )

    def test_negative_bound_is_also_a_no_op(self):
        hist = [(_at(9), 1514.417)]
        self.assertEqual(_drop(hist, bound=-1.0), hist)

    def test_empty_history_is_safe(self):
        self.assertEqual(_drop([], bound=0.0), [])
        self.assertEqual(_drop([]), [])


class TestDiscardRatherThanClamp(unittest.TestCase):
    """The design decision, pinned so it can't silently regress."""

    def test_a_dropped_sample_leaves_no_substitute_value_behind(self):
        # If this ever clamps instead of discards, the bad row would
        # reappear as BOUND (250.0) and this length check fails.
        kept = _drop([(_at(8.851), 1514.417), (_at(9), 5.0)])
        self.assertEqual(len(kept), 1)
        self.assertNotIn(BOUND, [v for _t, v in kept])

    def test_surviving_periods_are_rebuilt_from_real_samples_only(self):
        # End to end through the real resampler: an hour holding one
        # corrupt sample plus five good ones must average ONLY the good
        # ones, not blend the corrupt value in at 1/6 weight (which
        # would read ~252 kW -- no longer obviously wrong, which is
        # exactly why the filter runs upstream of the mean).
        hist = [(_at(9) + timedelta(minutes=m), 6.0) for m in (10, 20, 30, 40, 50)]
        hist.insert(0, (_at(9) + timedelta(minutes=5), 1514.417))
        kept = _drop(hist)
        means = solver_writer.resample_history_mean(kept, [_at(9)], 1.0)
        self.assertAlmostEqual(means[0], 6.0, places=6)

    def test_a_period_left_with_nothing_reads_zero_not_the_bad_value(self):
        # The hour containing ONLY the corrupt sample. After the drop it
        # has no real samples at all, so resample_history_mean() falls
        # through to its default -- which, since v0.94.285, is genuinely
        # 0.0 for a flow signal rather than a future reading.
        kept = _drop([(_at(9) + timedelta(minutes=5), 1514.417)])
        means = solver_writer.resample_history_mean(kept, [_at(9)], 1.0)
        self.assertEqual(means[0], 0.0)


if __name__ == "__main__":
    unittest.main()
