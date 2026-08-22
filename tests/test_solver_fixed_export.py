"""Real, live-reported finding (2026-08-20, direct household evidence --
a live chart showing the Solver's own proposed dispatch swinging between
near-40kW and near-zero, chasing whichever 5-min period showed the
highest real per-interval P2P rate, while the actual, live automation
held one flat ~13kW plateau the whole window). Direct household
explanation: P2P is not a plain price-taking market -- it's a matching
arrangement where a CONSISTENT, pre-committed delivery rate is itself
part of what earns the rate. Chasing the momentary best price doesn't
execute a smarter version of the deal, it breaks it.

GridConfig.fixed_export_kw (elements.py) / network.py's own construction
of the grid_export[t] LP variable is the fix: a per-period array pinning
grid_export[t]'s own lb/ub to an exact value wherever given, forcing the
LP to treat that period's export RATE as a given rather than a free
decision -- while everything else (which source funds it, SoC planning
leading in) stays genuinely free.

This test proves three things together, not just "does it not crash":
  1. WITHOUT the fix, a genuinely variable price signal really does make
     the LP swing dispatch period-to-period (reproduces the bug from
     first principles, not assumed).
  2. WITH the fix, grid_export_kw is EXACTLY constant for every period in
     the fixed window, regardless of how much the price varies.
  3. The LP still correctly plans SoC continuity AROUND the fixed window
     -- given a low starting SoC and a real pre-window charging
     opportunity, it charges enough beforehand to sustain the fixed rate
     for the whole window, exactly the "smarter about everything else"
     claim the fix is supposed to preserve.
"""

import unittest
from datetime import datetime, timedelta

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


def _scenario(fixed_kw: float | None):
    """26 hourly periods: 10h "daytime" (real charging opportunity, cheap
    import), then the real 7h P2P window (17:00-24:00), then 9h
    overnight. A genuinely variable export price during the P2P window
    (43-65c, matching the real range this project found live) -- this is
    what causes the swing WITHOUT the fix. Battery starts at a real low
    SoC (20%) specifically so sustaining a flat 11.5kW export for 7h
    requires genuine pre-window charging, not something it could already
    coast through on a full battery.
    """
    n = 26
    start = datetime(2026, 8, 20, 7, 0)  # 07:00 local
    hours = np.array([1.0] * n)
    periods = PeriodGrid(hours=hours, start=start)

    # Real window import price (~0.55, matching live data found
    # 2026-08-20 -- 0.5377-0.5525) -- deliberately NOT cheap. An earlier
    # draft of this test used an artificially cheap 0.30 window import
    # price, which let the LP fund export via direct import-then-export
    # in the same period whenever export_price > 0.30 (true for every
    # window rate below) -- a free-arbitrage escape hatch that masked
    # the real SoC-scarcity-driven swing this test exists to reproduce,
    # not a genuine reflection of real conditions (real P2P-window import
    # price is itself high, not cheap).
    import_price = np.array([0.15] * 10 + [0.55] * 7 + [0.15] * 9)
    # Real, variable P2P-window export rate (43-65c range, matching live
    # data found 2026-08-20) -- outside the window, a plain low spot rate.
    window_rates = [0.65, 0.50, 0.43, 0.60, 0.55, 0.48, 0.62]
    export_price = np.array([0.08] * 10 + window_rates + [0.08] * 9)

    fixed_export_kw = None
    if fixed_kw is not None:
        fixed_export_kw = np.full(n, np.nan)
        fixed_export_kw[10:17] = fixed_kw  # the 7 window periods, 0-indexed 10..16

    grid = GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=44.0,
        export_limit_kw=44.0,
        fixed_export_kw=fixed_export_kw,
    )
    battery = BatteryConfig(
        capacity_kwh=122.2,
        initial_soc_kwh=122.2 * 0.20,
        min_soc_kwh=122.2 * 0.05,
        max_soc_kwh=122.2 * 1.0,
        max_charge_kw=40.0,
        max_discharge_kw=40.0,
        charge_efficiency=0.975,
        discharge_efficiency=0.975,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    loads = [LoadConfig(name="house", forecast_kw=np.full(n, 1.5))]
    return periods, grid, battery, solar, loads


class TestFixedExportKw(unittest.TestCase):
    def test_without_fix_variable_price_makes_export_swing(self):
        periods, grid, battery, solar, loads = _scenario(fixed_kw=None)
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        window_export = plan.grid_export_kw[10:17]
        # Real swing, not just "some noise" -- confirms this scenario
        # genuinely reproduces the bug before asserting the fix beats it.
        self.assertGreater(
            window_export.max() - window_export.min(),
            5.0,
            f"expected a real swing without the fix, got a range of only "
            f"{window_export.max() - window_export.min():.2f}kW: {window_export}",
        )

    def test_with_fix_export_is_exactly_constant_through_the_window(self):
        target_kw = 11.5
        periods, grid, battery, solar, loads = _scenario(fixed_kw=target_kw)
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        window_export = plan.grid_export_kw[10:17]
        for i, v in enumerate(window_export):
            self.assertAlmostEqual(
                v,
                target_kw,
                places=4,
                msg=f"period {10 + i}: expected exactly {target_kw}kW, got {v}",
            )

    def test_periods_outside_the_window_are_still_free(self):
        target_kw = 11.5
        periods, grid, battery, solar, loads = _scenario(fixed_kw=target_kw)
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        # Outside the fixed window, export should NOT be pinned to 11.5 --
        # it's whatever the LP freely decides (almost certainly near 0,
        # given no solar and a low, still-recovering SoC).
        outside = list(plan.grid_export_kw[:10]) + list(plan.grid_export_kw[17:])
        self.assertFalse(
            all(abs(v - target_kw) < 0.01 for v in outside),
            "periods outside the fixed window incorrectly all pinned to the fixed rate too",
        )

    def test_lp_still_plans_real_pre_window_charging_to_sustain_it(self):
        target_kw = 11.5
        periods, grid, battery, solar, loads = _scenario(fixed_kw=target_kw)
        plan = build_plan(
            periods=periods, grid=grid, battery=battery, solar=solar, loads=loads
        )
        self.assertEqual(plan.status, "optimal")
        # Genuine, real energy requirement for 7h @ 11.5kW export + 1.5kW
        # house load, funded from the battery (no solar in this
        # scenario): needs real charging beforehand, not something a 20%-
        # SoC start could already coast through unaided.
        soc_at_window_start = plan.battery_soc_kwh[10]
        soc_at_start = battery.initial_soc_kwh
        self.assertGreater(
            soc_at_window_start,
            soc_at_start,
            f"expected real pre-window charging (SoC should rise from the low "
            f"20% start), but SoC at window start ({soc_at_window_start:.1f}kWh) "
            f"was not higher than the initial SoC ({soc_at_start:.1f}kWh)",
        )
        # And the window itself must not run the battery below its own
        # real floor while sustaining the fixed rate for the full 7h.
        window_soc = plan.battery_soc_kwh[10:18]
        self.assertGreaterEqual(window_soc.min(), battery.min_soc_kwh - 0.01)

    def test_none_is_a_complete_no_op(self):
        """Byte-identical to before this field existed -- the same
        scenario, explicitly passing fixed_export_kw=None, must match a
        GridConfig built without the field at all."""
        periods, grid_with_none, battery, solar, loads = _scenario(fixed_kw=None)
        grid_without_field = GridConfig(
            import_price=grid_with_none.import_price,
            export_price=grid_with_none.export_price,
            import_limit_kw=grid_with_none.import_limit_kw,
            export_limit_kw=grid_with_none.export_limit_kw,
        )
        plan_a = build_plan(
            periods=periods,
            grid=grid_with_none,
            battery=battery,
            solar=solar,
            loads=loads,
        )
        plan_b = build_plan(
            periods=periods,
            grid=grid_without_field,
            battery=battery,
            solar=solar,
            loads=loads,
        )
        np.testing.assert_allclose(plan_a.grid_export_kw, plan_b.grid_export_kw)
        self.assertAlmostEqual(plan_a.total_cost, plan_b.total_cost, places=6)


if __name__ == "__main__":
    unittest.main()
