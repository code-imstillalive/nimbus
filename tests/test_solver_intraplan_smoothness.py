"""Real, live-reported bug (2026-08-20, direct household question: "why
does nimbus decide to make such decisions and charge in bursts not
continuously... is is price sensitivity or what?"). Pulled directly from
the real production forecast (sensor.nimbus_solver_battery_forecast,
generated_at 2026-08-20T13:15:00): a SINGLE solve's own battery_kw swung
-1.25 -> -33.15 -> -0.30 kW across three consecutive 5-minute periods
while the real import_price was byte-identical (0.0202) across all of
them -- confirmed NOT price sensitivity, confirmed NOT within-solve
instability (this is one solve's own internal choice, not two different
cron runs disagreeing).

Root cause: genuine LP degeneracy. When price (and load/solar) barely
change across several adjacent periods, the LP has zero cost preference
for WHICH exact minute-by-minute shape delivers the same total energy --
a smooth ramp and a jagged burst are equally "optimal," and which one
HiGHS happens to land on is essentially arbitrary.

Fix: network.py's new mechanism 4 (smoothness_weight) adds a tiny
L1-linearized penalty (same technique as the already-existing mechanism 1,
proximal_weight -- just comparing each period against its own immediately
preceding period within THIS solve, not against a previous solve's own
plan). This test proves three things together, not just "does it not
crash":
  1. The bug is genuinely reproducible from a flat/near-flat price signal
     alone (real household numbers, not invented).
  2. The fix eliminates the jaggedness (measured as sum of |consecutive
     deltas|) while leaving total_cost EXACTLY unchanged -- the real
     signature of genuine degeneracy, not a heuristic that happens to
     look better.
  3. The fix does NOT smear a genuine, large, real transition (a sharp
     price step, mimicking the real 5pm P2P boundary) -- the exact risk
     this codebase already explicitly declined to accept for the
     alternative fix (max_rate_kw, a hard cap) -- proven safe here for
     this different, soft-cost mechanism instead.
"""

import unittest
from datetime import datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan


def _jaggedness(discharge_kw: np.ndarray, charge_kw: np.ndarray) -> float:
    net = discharge_kw - charge_kw
    return float(np.sum(np.abs(np.diff(net))))


def _flat_price_scenario():
    """12 five-minute periods, real household numbers (2026-08-20 13:15
    onward): import_price/export_price genuinely flat throughout, solar
    and load both real and near-constant -- nothing in the true economic
    signal should ever justify a burst.
    """
    n = 12
    periods = PeriodGrid(
        hours=np.full(n, 5.0 / 60.0), start=datetime(2026, 8, 20, 13, 15)
    )
    grid = GridConfig(
        import_price=np.full(n, 0.0202),
        export_price=np.full(n, -0.0053),
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    battery = BatteryConfig(
        capacity_kwh=122.2,
        initial_soc_kwh=112.2,
        min_soc_kwh=2.44,
        max_soc_kwh=122.2,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.926,
        discharge_efficiency=0.926,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )
    solar = SolarConfig(
        forecast_kw=np.full(n, 7.0), lower_kw=np.full(n, 6.0), upper_kw=np.full(n, 8.0)
    )
    loads = [
        LoadConfig(
            name="load",
            forecast_kw=np.full(n, 5.8),
            lower_kw=np.full(n, 5.0),
            upper_kw=np.full(n, 6.5),
        )
    ]
    return periods, grid, battery, solar, loads


def _price_step_scenario():
    """12 five-minute periods, a real, sharp price step at the midpoint
    (mimicking the actual 5pm P2P window boundary this project already
    depends on staying sharp) -- $0.02 for the first half, $0.50 for the
    second. A genuine, large, correctly-timed transition, not a tie.
    """
    n = 12
    periods = PeriodGrid(
        hours=np.full(n, 5.0 / 60.0), start=datetime(2026, 8, 20, 13, 15)
    )
    import_price = np.concatenate([np.full(6, 0.02), np.full(6, 0.50)])
    export_price = np.concatenate([np.full(6, -0.005), np.full(6, 0.45)])
    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=44.0,
        export_limit_kw=44.0,
    )
    battery = BatteryConfig(
        capacity_kwh=122.2,
        initial_soc_kwh=90.0,
        min_soc_kwh=2.44,
        max_soc_kwh=122.2,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.926,
        discharge_efficiency=0.926,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )
    solar = SolarConfig(
        forecast_kw=np.full(n, 3.0), lower_kw=np.full(n, 2.5), upper_kw=np.full(n, 3.5)
    )
    loads = [
        LoadConfig(
            name="load",
            forecast_kw=np.full(n, 2.0),
            lower_kw=np.full(n, 1.5),
            upper_kw=np.full(n, 2.5),
        )
    ]
    return periods, grid, battery, solar, loads


class TestIntraplanSmoothness(unittest.TestCase):
    def test_flat_price_burst_is_reproducible_without_the_fix(self):
        """Confirms the bug is real BEFORE claiming the fix works -- same
        discipline as the export-bonus tiebreak test: prove the failure
        mode exists, not just that the fix produces a nice-looking number.
        """
        periods, grid, battery, solar, loads = _flat_price_scenario()
        plan = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            smoothness_weight=0.0,
        )
        self.assertEqual(plan.status, "optimal")
        jag = _jaggedness(plan.battery_discharge_kw, plan.battery_charge_kw)
        self.assertGreater(
            jag,
            20.0,
            "expected the real degenerate burst pattern to reproduce with the fix off",
        )

    def test_smoothness_weight_eliminates_burst_at_zero_extra_cost(self):
        """The real signature of genuine degeneracy: eliminating the burst
        must not cost a single extra cent, since both shapes were already
        equally optimal.
        """
        periods, grid, battery, solar, loads = _flat_price_scenario()
        plan_off = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            smoothness_weight=0.0,
        )
        plan_on = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            smoothness_weight=0.005,
        )
        self.assertEqual(plan_on.status, "optimal")
        jag_on = _jaggedness(plan_on.battery_discharge_kw, plan_on.battery_charge_kw)
        self.assertLess(
            jag_on, 1.0, "smoothness_weight should reduce the burst to essentially flat"
        )
        self.assertAlmostEqual(
            plan_on.total_cost,
            plan_off.total_cost,
            places=2,
            msg="a genuine degenerate tie must cost the same either way",
        )

    def test_genuine_price_step_is_not_smeared(self):
        """The exact risk this codebase already flagged for max_rate_kw
        (a hard cap) -- proven here NOT to apply to this soft-cost
        mechanism at the same weight that fully fixes the flat-price case.
        """
        periods, grid, battery, solar, loads = _price_step_scenario()
        plan_off = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            smoothness_weight=0.0,
        )
        plan_on = build_plan(
            periods=periods,
            grid=grid,
            battery=battery,
            solar=solar,
            loads=loads,
            smoothness_weight=0.005,
        )
        self.assertEqual(plan_on.status, "optimal")
        net_off = plan_off.battery_discharge_kw - plan_off.battery_charge_kw
        net_on = plan_on.battery_discharge_kw - plan_on.battery_charge_kw
        step_off = abs(float(net_off[6] - net_off[5]))
        step_on = abs(float(net_on[6] - net_on[5]))
        self.assertGreater(
            step_off,
            50.0,
            "sanity check: the unfixed scenario really does have a big real step",
        )
        self.assertGreater(
            step_on, 50.0, "the fix must not smear a genuine, large, real transition"
        )
        self.assertAlmostEqual(step_on, step_off, delta=1.0)


if __name__ == "__main__":
    unittest.main()
