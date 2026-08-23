"""BatteryConfig.charge_power_curve / discharge_power_curve -- direct
household ask (2026-08-21): two real, distinct physical phenomena named
precisely, both needing a POWER-LIMIT mechanism (what's physically
achievable), not a cost mechanism:

1. Real lithium charge current genuinely tapers as SoC approaches full
   (the CC->CV curve) -- a battery physically cannot accept the same
   current near the top as it can mid-range.
2. Most BMS units lose accurate SoC resolution below roughly 15% -- a
   real measurement-caution reason to taper the ACHIEVABLE discharge
   rate near the floor too, not just a hard cutoff at min_soc_kwh.

Both are implemented as a concave piecewise-linear UPPER BOUND on the
existing charge[t]/discharge[t] variables, expressed as extra <=
constraint ROWS (no new LP variables at all) -- the standard "achievable
power = min over every segment's own extended line" LP technique, valid
only when the curve is genuinely concave (non-increasing slopes).

These tests prove: (1) None is fully backward compatible; (2) bad
config is rejected with a clear error, not a silent wrong answer; (3)
the curve genuinely BINDS real dispatch at the SoC levels it's meant to
-- not just "doesn't crash"; (4) both directions (charge tapering near
full, discharge tapering near empty) work correctly under the same
non-increasing-slope test; (5) real solve time stays cheap at a
realistic horizon length, matching the household's own explicit
compute-cost concern.

Every expected numeric value below is hand-derived from the curve's own
(soc_kwh, max_power_kw) points, not guessed -- see the inline comment at
each assertion for the exact arithmetic. Test scenarios are deliberately
built with (a) a large enough capacity_kwh relative to a single period's
possible energy delta that the pre-existing SoC-ceiling bound (soc[t] <=
max_soc_kwh) never confounds what's being tested, and (b) a real,
unambiguous economic incentive (salvage_value for charging, a strong
export_price for discharging) so the LP has a genuine reason to push
against whatever limit is actually binding, rather than sitting idle at
zero because nothing rewards it either way.
"""

import time
import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    DegenerateConfigError,
    GridConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = dict(
        capacity_kwh=100.0,
        initial_soc_kwh=40.0,
        min_soc_kwh=5.0,
        max_soc_kwh=100.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )
    defaults.update(overrides)
    return BatteryConfig(**defaults)


# Valid, realistic curves reused across tests. Slopes verified by hand
# before being written down here (each must be non-increasing):
# CHARGE_CURVE segment slopes: 0, then (6-10)/(90-80)=-0.4, then
# (0-6)/(100-90)=-0.6 -- 0 >= -0.4 >= -0.6, valid (taper accelerates
# toward full, matching real CC->CV physics).
CHARGE_CURVE = [(5.0, 10.0), (80.0, 10.0), (90.0, 6.0), (100.0, 0.0)]
# DISCHARGE_CURVE segment slopes: (10-0.5)/(50-5)=0.2111, then
# (10-10)/(100-50)=0 -- 0.2111 >= 0, valid (steady ramp-up from the
# floor, then flat once real capacity is genuinely available).
DISCHARGE_CURVE = [(5.0, 0.5), (50.0, 10.0), (100.0, 10.0)]


class TestBackwardCompatibility(unittest.TestCase):
    def test_none_curve_leaves_charge_bound_at_flat_max(self):
        """No curve configured -> charge[t] reaches the full flat
        max_charge_kw given a real incentive to do so -- proves the new
        constraint code path is a genuine no-op, not silently always-on.
        initial_soc=40, charging 10kW*0.99eff*1h=9.9kWh -> final soc=49.9,
        comfortably under max_soc=100 (no ceiling collision)."""
        n = 1
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.001),
            export_price=np.full(n, 0.02),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(
            salvage_value=0.05
        )  # real, unambiguous reason to end with more charge
        plan = build_plan(periods=periods, grid=grid, battery=battery, solar=solar)
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(plan.battery_charge_kw[0], 10.0, places=3)


class TestValidation(unittest.TestCase):
    def test_too_few_points_rejected(self):
        with self.assertRaises(ValueError):
            _base_battery(charge_power_curve=[(5.0, 10.0)])

    def test_unsorted_points_rejected(self):
        with self.assertRaises(ValueError):
            _base_battery(charge_power_curve=[(80.0, 10.0), (5.0, 10.0), (100.0, 0.0)])

    def test_wrong_first_point_rejected(self):
        with self.assertRaises(ValueError):
            _base_battery(
                charge_power_curve=[(10.0, 10.0), (100.0, 0.0)]
            )  # doesn't start at min_soc_kwh=5.0

    def test_wrong_last_point_rejected(self):
        with self.assertRaises(ValueError):
            _base_battery(
                charge_power_curve=[(5.0, 10.0), (99.0, 0.0)]
            )  # doesn't end at max_soc_kwh=100.0

    def test_negative_power_rejected(self):
        with self.assertRaises(ValueError):
            _base_battery(charge_power_curve=[(5.0, 10.0), (100.0, -1.0)])

    def test_increasing_slope_convex_curve_rejected(self):
        """The real correctness-critical check: a convex (increasing-
        slope) curve would silently produce a WRONG, non-binding bound
        under the min-of-lines LP technique -- must be rejected outright,
        not merely discouraged. Slopes here: (2-1)/(50-5)=0.0222, then
        (10-2)/(100-50)=0.16 -- increasing, invalid."""
        with self.assertRaises(DegenerateConfigError):
            _base_battery(charge_power_curve=[(5.0, 1.0), (50.0, 2.0), (100.0, 10.0)])

    def test_valid_curves_accepted(self):
        battery = _base_battery(
            charge_power_curve=CHARGE_CURVE, discharge_power_curve=DISCHARGE_CURVE
        )
        self.assertEqual(battery.charge_power_curve, CHARGE_CURVE)
        self.assertEqual(battery.discharge_power_curve, DISCHARGE_CURVE)


class TestChargeCurveGenuinelyBinds(unittest.TestCase):
    """The important test: prove the taper actually constrains real
    dispatch, at exactly the SoC levels it's meant to -- not just that
    the LP still solves. salvage_value=0.05 gives the LP a real,
    unambiguous reason to charge as much as physically/curve-allowed,
    isolating exactly the mechanism under test."""

    def _solve(self, initial_soc_kwh: float, curve):
        n = 1
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.001),
            export_price=np.full(n, 0.02),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(
            initial_soc_kwh=initial_soc_kwh,
            charge_power_curve=curve,
            salvage_value=0.05,
        )
        return build_plan(periods=periods, grid=grid, battery=battery, solar=solar)

    def test_low_soc_charges_at_full_flat_rate(self):
        """soc=40 is inside CHARGE_CURVE's own flat first segment
        [5,80] -> curve value is exactly 10.0, identical to the flat
        max_charge_kw. Charging 10kW*0.99*1h=9.9kWh -> final soc=49.9,
        well under max_soc=100, no ceiling collision."""
        plan = self._solve(initial_soc_kwh=40.0, curve=CHARGE_CURVE)
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(plan.battery_charge_kw[0], 10.0, places=3)

    def test_high_soc_charge_is_tapered_below_flat_max(self):
        """soc=90 is exactly CHARGE_CURVE's own second breakpoint ->
        curve value is exactly 6.0, well under the flat
        max_charge_kw=10.0. Charging 6kW*0.99*1h=5.94kWh -> final
        soc=95.94, under max_soc=100, no ceiling collision -- the curve,
        not the SoC ceiling, is the real binding constraint here."""
        plan = self._solve(initial_soc_kwh=90.0, curve=CHARGE_CURVE)
        self.assertEqual(plan.status, "optimal")
        self.assertLess(plan.battery_charge_kw[0], 10.0 - 1e-6)
        self.assertAlmostEqual(plan.battery_charge_kw[0], 6.0, places=2)

    def test_curve_never_allows_more_than_flat_max_would(self):
        """Sanity check the curve is a genuine restriction, never an
        accidental loosening -- every value on CHARGE_CURVE is <=
        max_charge_kw=10.0 by construction, confirmed directly."""
        self.assertTrue(all(pw <= 10.0 + 1e-9 for _s, pw in CHARGE_CURVE))


class TestDischargeCurveGenuinelyBinds(unittest.TestCase):
    def _solve(self, initial_soc_kwh: float, curve):
        n = 1
        periods = _flat_grid(n)
        # Strong export price -- a real, unambiguous incentive to
        # discharge as much as physically/curve-allowed, isolating
        # exactly the mechanism under test (same reasoning as the
        # charge tests' own salvage_value, applied to the other side).
        grid = GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 5.0),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(
            initial_soc_kwh=initial_soc_kwh, discharge_power_curve=curve
        )
        return build_plan(periods=periods, grid=grid, battery=battery, solar=solar)

    def test_near_floor_discharge_is_tapered(self):
        """soc=30 is inside DISCHARGE_CURVE's own ramp segment [5,50] ->
        curve value = 0.5 + 0.21111*(30-5) = 5.7778, well under the flat
        10.0kW ceiling. Real headroom above the floor (30-5=25kWh) would
        separately allow up to ~24.75kW under the existing "can't
        discharge more than stored" constraint, so the curve -- not that
        constraint -- is confirmed to be what's actually binding here."""
        plan = self._solve(initial_soc_kwh=30.0, curve=DISCHARGE_CURVE)
        self.assertEqual(plan.status, "optimal")
        self.assertLess(plan.battery_discharge_kw[0], 10.0 - 1e-6)
        self.assertAlmostEqual(plan.battery_discharge_kw[0], 5.7778, places=3)

    def test_higher_soc_discharges_at_full_flat_rate(self):
        """soc=70 is inside DISCHARGE_CURVE's own flat second segment
        [50,100] -> curve value is exactly 10.0, identical to the flat
        max_discharge_kw. Real headroom (70-5=65kWh) separately allows
        ~64.35kW under the stored-energy constraint, so 10.0 here is
        genuinely the curve's own ceiling, not that other constraint."""
        plan = self._solve(initial_soc_kwh=70.0, curve=DISCHARGE_CURVE)
        self.assertEqual(plan.status, "optimal")
        self.assertAlmostEqual(plan.battery_discharge_kw[0], 10.0, places=3)


class TestSolveTimeStaysCheap(unittest.TestCase):
    """Direct response to the household's own explicit compute-time
    concern -- confirms adding a real curve doesn't meaningfully
    regress solve time at a realistic production-scale horizon."""

    def test_real_horizon_scale_solve_time(self):
        n = 288  # matches this project's own real Tier1 period count (5-min, 24h)
        periods = _flat_grid(n, hours=5.0 / 60.0)
        rng = np.random.default_rng(42)
        grid = GridConfig(
            import_price=0.15 + 0.10 * rng.random(n),
            export_price=0.05 + 0.05 * rng.random(n),
            import_limit_kw=40.0,
            export_limit_kw=40.0,
        )
        solar = SolarConfig(
            forecast_kw=np.clip(10.0 * np.sin(np.linspace(0, np.pi, n)), 0, None)
        )
        battery = _base_battery(
            capacity_kwh=122.2,
            initial_soc_kwh=61.0,
            min_soc_kwh=2.4,
            max_soc_kwh=122.2,
            max_charge_kw=40.0,
            max_discharge_kw=40.0,
            salvage_value=0.10,
            # Slopes: 0, then (2-40)/(122.2-97.8)=-1.5574 -- non-increasing, valid.
            charge_power_curve=[(2.4, 40.0), (97.8, 40.0), (122.2, 2.0)],
            # Slopes: (40-3)/(18.3-2.4)=2.3270, then 0 -- non-increasing, valid.
            discharge_power_curve=[(2.4, 3.0), (18.3, 40.0), (122.2, 40.0)],
        )
        start = time.perf_counter()
        plan = build_plan(periods=periods, grid=grid, battery=battery, solar=solar)
        elapsed = time.perf_counter() - start
        self.assertEqual(plan.status, "optimal")
        # Generous ceiling -- this project's own real production solves
        # (full ~360-period horizon, considerably more constraints than
        # this 288-period test) measured ~0.4s under the highspy
        # backend. 5s leaves very wide margin while still catching any
        # real regression toward the old multi-minute crisis.
        self.assertLess(
            elapsed,
            5.0,
            f"solve took {elapsed:.2f}s, expected well under 5s at this scale",
        )


if __name__ == "__main__":
    unittest.main()
