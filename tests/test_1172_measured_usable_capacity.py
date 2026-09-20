"""nimbus #1172: measure this pack's usable capacity from the day's own
monotonic SoC rise, using figures the report already computes.

## Why a monotonic rise rather than the energy balance

`battery_energy_balance()` deliberately does not solve for capacity, and
its own source comment says why:

    within one scored window `measured` is a NET swing, and a window
    with both directions cannot attribute it to either side. Computing
    k and e from a net would produce a confident number out of a
    quantity that does not contain the answer.

That is correct, and it is exactly why this splits the day first. A
monotonic charge phase has no discharge to confound it. The same comment
notes *"pairing two windows is the caller's job"* -- this is the caller
doing it, on the one shape that is unambiguous.

## The real measurement this is calibrated against

Four consecutive days on the reference household, each a near-full sweep,
integrating the battery power sensor over the day's monotonic rise:

    16 Sep   2.0 -> 100.0%   110.33 kWh   ->  112.6 kWh
    17 Sep   2.0 -> 100.0%   107.70 kWh   ->  109.9 kWh
    18 Sep   2.0 -> 100.0%   107.52 kWh   ->  109.7 kWh
    19 Sep   2.0 ->  99.3%   104.80 kWh   ->  107.7 kWh

Mean **110.0 kWh against 122.2 configured** (119.8 after the 98% SoH
derate). `TestTheRealDay` below drives the same 19 Sep numbers through
the helper and pins that it lands on the raw-sample answer within the
resolution cost.

## Why the threshold is 50 points and not a round 10

The rise sits in the denominator, so a small one amplifies every error
in it. At 50 points a 0.5-point reading error moves the answer by 1%; at
5 points it moves it by 10%, which is larger than the effect being
measured. Set where the real measurements are: four days rose 97.3-98.0
points, so 50 accepts every genuine sweep with margin while refusing
shallow days that carry no signal.
"""

from __future__ import annotations

import unittest
from typing import ClassVar

import _solver_path  # noqa: F401
import solver_writer

_cap = solver_writer._measured_usable_capacity_kwh


def _soc(series):
    """soc_discrepancy_hourly's real shape, hour -> real_pct."""
    return [
        {"hour": f"2026-09-19T{h:02d}:00:00+10:00", "real_pct": v}
        for h, v in enumerate(series)
    ]


def _power(series):
    """j_ach_hourly's real shape; positive battery_kw is charge."""
    return {
        f"2026-09-19T{h:02d}:00:00+10:00": {"battery_kw": v}
        for h, v in enumerate(series)
    }


class TestTheRealDay(unittest.TestCase):
    """The reference household's 19 Sep report, hour for hour."""

    REAL_SOC: ClassVar[list[float]] = [
        17.7, 16.8, 16.0, 15.1, 13.7, 3.5, 2.0, 2.4, 4.7, 7.9, 13.2, 19.9,
        28.6, 36.0, 59.0, 84.3, 99.3, 99.3, 88.8, 77.4, 65.6, 53.6, 41.5, 29.8,
    ]  # fmt: skip
    BATTERY_KW: ClassVar[list[float]] = [
        -0.98, -0.82, -0.84, -0.84, -6.40, -0.99, 0.41, 2.71, 3.78, 6.10,
        7.85, 10.10, 8.23, 25.08, 28.49, 13.74, 1.62, -12.88, -13.49, -13.42,
        -13.53, -13.12, -12.90, -12.89,
    ]  # fmt: skip

    def test_it_lands_on_the_raw_sample_answer(self):
        """Raw-sample integration of the same day gives 107.7 kWh. Hourly
        resolution costs ~1.6%, which is the price of needing no extra
        recorder read."""
        got = _cap(_soc(self.REAL_SOC), _power(self.BATTERY_KW))
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, 107.7, delta=3.0)

    def test_it_is_well_below_the_configured_figure(self):
        """The finding, not just the arithmetic: 122.2 configured
        (119.8 after SoH) against a pack that measures ~110."""
        got = _cap(_soc(self.REAL_SOC), _power(self.BATTERY_KW))
        self.assertLess(got, 115.0)

    def test_the_fixture_is_the_real_shape(self):
        """Guards the premise. These are the published attributes, so if
        the shape changes the test must be re-derived rather than
        quietly keep passing on a shape production no longer emits."""
        self.assertEqual(len(self.REAL_SOC), 24)
        self.assertEqual(len(self.BATTERY_KW), 24)
        self.assertEqual(min(self.REAL_SOC), 2.0)
        self.assertEqual(max(self.REAL_SOC), 99.3)


class TestItRefusesWhenTheRiseCannotCarryAnAnswer(unittest.TestCase):
    def test_a_shallow_day_reports_none(self):
        """Most days on a shallow-cycling install. An honest absence, not
        a number divided out of a denominator too small to hold one."""
        soc = _soc([40.0 + h * 0.5 for h in range(24)])  # +11.5 points
        self.assertIsNone(_cap(soc, _power([5.0] * 24)))

    def test_a_rise_just_under_the_threshold_is_refused(self):
        soc = _soc([10.0 + h * (49.0 / 23.0) for h in range(24)])
        self.assertIsNone(_cap(soc, _power([5.0] * 24)))

    def test_a_rise_just_over_the_threshold_is_accepted(self):
        soc = _soc([10.0 + h * (51.0 / 23.0) for h in range(24)])
        self.assertIsNotNone(_cap(soc, _power([5.0] * 24)))

    def test_a_flat_day_reports_none(self):
        self.assertIsNone(_cap(_soc([50.0] * 24), _power([0.0] * 24)))


class TestItSurvivesRealInputs(unittest.TestCase):
    def test_missing_inputs_are_survivable(self):
        self.assertIsNone(_cap(None, _power([5.0] * 24)))
        self.assertIsNone(_cap(_soc([2.0, 99.0]), None))
        self.assertIsNone(_cap([], {}))

    def test_a_row_without_a_reading_is_skipped_not_crashed(self):
        soc = _soc([2.0 + h * 4.0 for h in range(24)])
        soc[3]["real_pct"] = None
        self.assertIsNotNone(_cap(soc, _power([5.0] * 24)))

    def test_a_rise_with_no_recorded_charge_reports_none(self):
        """SoC climbed and the power sensor saw nothing. That is an
        instrumentation gap, not a capacity measurement -- returning a
        number here would divide by a charge that was never observed."""
        soc = _soc([2.0 + h * 4.0 for h in range(24)])
        self.assertIsNone(_cap(soc, _power([0.0] * 24)))

    def test_a_small_dip_does_not_end_the_rise(self):
        """The real sensor quantises and wobbles. A 0.3-point dip
        mid-charge must not split a genuine sweep into two halves that
        each fail the threshold."""
        series = [2.0 + h * 4.2 for h in range(24)]
        series[10] -= 0.3
        self.assertIsNotNone(_cap(_soc(series), _power([5.0] * 24)))

    def test_a_real_turnaround_does_end_the_rise(self):
        """Charge to 60%, discharge to 10%, charge to 55%. Neither leg
        clears 50 points, so there is no clean phase to measure."""
        up = [10.0 + h * 8.0 for h in range(7)]  # 10 -> 58
        down = [58.0 - h * 8.0 for h in range(1, 7)]  # 50 -> 10
        up2 = [10.0 + h * 4.0 for h in range(1, 12)]  # 14 -> 54
        series = (up + down + up2)[:24]
        series += [series[-1]] * (24 - len(series))
        self.assertIsNone(_cap(_soc(series), _power([5.0] * 24)))


class TestOnlyChargeCounts(unittest.TestCase):
    def test_discharge_hours_inside_the_window_do_not_subtract(self):
        """A brief discharge inside a rising phase must not be netted
        off -- that would reintroduce the net-swing confound this whole
        approach exists to avoid."""
        soc = _soc([2.0 + h * 4.2 for h in range(24)])
        clean = _cap(soc, _power([5.0] * 24))
        with_dip = _cap(soc, _power([5.0] * 10 + [-5.0] + [5.0] * 13))
        self.assertIsNotNone(with_dip)
        self.assertLess(with_dip, clean)
        # 5 kWh removed from the numerator, not 10 (which a net would give)
        self.assertAlmostEqual(
            clean - with_dip, 5.0 / (soc[-1]["real_pct"] - 2.0) * 100, delta=0.3
        )


class TestItReachesThePublishedReport(unittest.TestCase):
    def test_the_report_publishes_the_field(self):
        import ast
        from pathlib import Path

        src = Path(solver_writer.__file__.replace(".pyc", ".py")).read_text(
            encoding="utf-8"
        )
        self.assertIn('"measured_usable_capacity_kwh"', src)
        tree = ast.parse(src)
        fn = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_compute_report_for_window"
        )
        called = {
            (
                n.func.attr
                if isinstance(n.func, ast.Attribute)
                else getattr(n.func, "id", "")
            )
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
        }
        self.assertIn("_measured_usable_capacity_kwh", called)

    def test_it_is_not_in_the_per_row_history_tuple(self):
        self.assertNotIn(
            "measured_usable_capacity_kwh", solver_writer._QUALITY_HISTORY_FIELDS
        )


if __name__ == "__main__":
    unittest.main()
