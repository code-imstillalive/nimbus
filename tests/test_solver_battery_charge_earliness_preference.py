"""nimbus issue #692 (household, live-observed 2026-09-10): the battery's
own charge decision has never had an earliness preference, unlike
adequacy loads (#613, item 2 of 3). Real, direct evidence this is a
genuine gap: a live devhub solve held battery_charge at EXACTLY 0 kW for
27 minutes at a roughly flat 6.1-8.4c/kWh price band, then jumped to
near-max charge five minutes later at the IDENTICAL price the earlier
periods had already seen and declined -- nothing in the objective
distinguished those moments, so the LP was free to land on either.
Separately, the real household's own battery sat at a critically-low 2-4%
SoC for a full hour while import price was a moderate, perfectly
reasonable 6-9c/kWh the whole time, with the plan choosing not to charge
at all until later.

Same technique as test_solver_adequacy_earliness_preference.py: direct
LP-level proof of network.build_plan()'s new
`battery_charge_earliness_budget_kw` mechanism -- never large enough to
override a REAL price difference (materiality preserved), but large
enough to deterministically break a genuine tie toward charging as soon
as possible.

No `allowed` mask exists on BatteryConfig (unlike AdequacyLoadConfig), so
periods are forced open/closed via GridConfig.import_limit_kw's own
real per-period array support (#493 item 0) instead -- with no solar and
no other charge source, a period with import_limit_kw=0 makes charging
in that period mathematically impossible, giving the same "force exactly
one period" precision the load test's own `allowed` mask provides.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid, SolarConfig
from solver.network import DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW, build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _run_forced_single_period_charge(
    *,
    n: int,
    period: int,
    charge_kwh: float,
    max_charge_kw: float,
    import_price: float = 0.20,
    battery_charge_earliness_budget_kw: float = DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
):
    """Forces the whole charge_kwh to be delivered in exactly one period,
    by zeroing GridConfig.import_limit_kw everywhere except that period --
    two otherwise-identical solves differing only in WHICH single period
    was forced can be compared on plan.total_cost alone, same reasoning
    as the load test's own _run_forced_single_period()."""
    periods = _flat_grid(n)
    import_limit = np.zeros(n)
    import_limit[period] = max_charge_kw
    grid = GridConfig(
        import_price=np.full(n, import_price),
        export_price=np.full(n, 0.0),
        import_limit_kw=import_limit,
        export_limit_kw=0.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    # Real household ask: charge_kwh worth of energy MUST be delivered
    # somewhere -- a real, large terminal salvage_value makes reaching
    # capacity_kwh by the horizon end strictly worth the real charge_cost
    # paid to get there, same "a real economic reason exists to charge
    # AT ALL, but WHEN is genuinely tied" shape the load test's own
    # target_kwh commitment gives it.
    battery = BatteryConfig(
        name="battery",
        capacity_kwh=charge_kwh,
        initial_soc_kwh=0.0,
        min_soc_kwh=0.0,
        max_soc_kwh=charge_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=0.0,
        charge_efficiency=0.99,
        discharge_efficiency=0.99,
        charge_cost=0.0,
        discharge_cost=0.01,
        salvage_value=1.0,
    )
    return build_plan(
        periods=periods,
        grid=grid,
        batteries=[battery],
        solar=solar,
        loads=[],
        battery_charge_earliness_budget_kw=battery_charge_earliness_budget_kw,
    )


class TestBatteryEarlinessBreaksGenuineTies(unittest.TestCase):
    def test_earlier_forced_charge_period_is_strictly_cheaper_by_default(self):
        plan_early = _run_forced_single_period_charge(
            n=24, period=2, charge_kwh=2.0, max_charge_kw=2.0
        )
        plan_late = _run_forced_single_period_charge(
            n=24, period=20, charge_kwh=2.0, max_charge_kw=2.0
        )
        self.assertEqual(plan_early.status, "optimal")
        self.assertEqual(plan_late.status, "optimal")
        self.assertLess(plan_early.total_cost, plan_late.total_cost)

    def test_no_earliness_bias_when_budget_is_zero(self):
        # Same two forced-period solves, but with the mechanism fully
        # disabled -- with a flat price and no other differentiator,
        # WHICH single period delivers the charge must cost identically
        # either way once the earliness term is gone.
        plan_early = _run_forced_single_period_charge(
            n=24,
            period=2,
            charge_kwh=2.0,
            max_charge_kw=2.0,
            battery_charge_earliness_budget_kw=0.0,
        )
        plan_late = _run_forced_single_period_charge(
            n=24,
            period=20,
            charge_kwh=2.0,
            max_charge_kw=2.0,
            battery_charge_earliness_budget_kw=0.0,
        )
        self.assertAlmostEqual(plan_early.total_cost, plan_late.total_cost, places=6)

    def test_unforced_placement_prefers_the_earliest_feasible_periods(self):
        # The real, user-facing behaviour change: with every period
        # equally free (a flat import_limit_kw, no forcing) and a
        # genuinely flat price, the battery now charges in the EARLIEST
        # periods available, not an arbitrary later block -- this is the
        # exact shape of the real household's own live complaint (a
        # critically-low battery sitting idle through a perfectly good
        # early charging window, only charging much later).
        n = 24
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=np.full(n, 0.20),
            export_price=np.full(n, 0.0),
            import_limit_kw=10.0,
            export_limit_kw=0.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = BatteryConfig(
            name="battery",
            capacity_kwh=2.0,
            initial_soc_kwh=0.0,
            min_soc_kwh=0.0,
            max_soc_kwh=2.0,
            max_charge_kw=1.0,
            max_discharge_kw=0.0,
            charge_efficiency=0.99,
            discharge_efficiency=0.99,
            charge_cost=0.0,
            discharge_cost=0.01,
            salvage_value=1.0,
        )
        plan = build_plan(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=[]
        )
        self.assertEqual(plan.status, "optimal")
        charge = plan.batteries[0].charge_kw
        # 2.0 kWh capacity at 0.99 charge_efficiency needs 2.0/0.99=2.0202
        # kWh of raw input -- 2 full 1.0 kW periods (1.98 kWh stored) plus
        # a small top-off in period 2 (~0.0204 kW), not a clean 2-period
        # cutoff. The real behaviour under test is that this lands in the
        # EARLIEST 3 periods, nothing later -- not the exact split.
        self.assertAlmostEqual(float(charge[0]), 1.0, places=4)
        self.assertAlmostEqual(float(charge[1]), 1.0, places=4)
        self.assertGreater(float(charge[2]), 0.0)
        self.assertLess(float(charge[2]), 0.05)
        self.assertTrue(np.allclose(charge[3:], 0.0, atol=1e-4))


class TestBatteryEarlinessNeverOverridesARealPriceSignal(unittest.TestCase):
    def test_a_real_cheaper_later_period_still_wins(self):
        # The real, non-negotiable guarantee: a genuine price difference
        # (well above MIN_CHARGE_DISCHARGE_COST_SPREAD) always beats the
        # earliness nudge -- charging later at a real discount must still
        # win over charging earlier at full price.
        n = 24
        periods = _flat_grid(n)
        import_price = np.full(n, 0.20)
        import_price[20] = 0.05  # a real, large discount late in the horizon
        import_limit = np.full(n, 2.0)
        grid = GridConfig(
            import_price=import_price,
            export_price=np.full(n, 0.0),
            import_limit_kw=import_limit,
            export_limit_kw=0.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = BatteryConfig(
            name="battery",
            capacity_kwh=2.0,
            initial_soc_kwh=0.0,
            min_soc_kwh=0.0,
            max_soc_kwh=2.0,
            max_charge_kw=2.0,
            max_discharge_kw=0.0,
            charge_efficiency=0.99,
            discharge_efficiency=0.99,
            charge_cost=0.0,
            discharge_cost=0.01,
            salvage_value=1.0,
        )
        plan = build_plan(
            periods=periods, grid=grid, batteries=[battery], solar=solar, loads=[]
        )
        self.assertEqual(plan.status, "optimal")
        charge = plan.batteries[0].charge_kw
        # max_charge_kw=2.0 at 0.99 efficiency delivers 1.98 kWh in one
        # period -- almost, but not quite, the full 2.0 kWh capacity, so
        # the LP charges at max power in period 20 (the real, genuine
        # discount -- the actual economic decision under test) AND picks
        # up the tiny 0.0204 kW leftover top-off at period 0 (the
        # earliest available period, since that fragment is genuinely
        # cost-tied between any two periods at either price -- exactly
        # the earliness mechanism's own job). The real decision this test
        # guards is period 20 getting the BULK charge despite being late
        # in the horizon, not that literally zero charging happens
        # anywhere else.
        self.assertAlmostEqual(float(charge[20]), 2.0, places=4)
        self.assertAlmostEqual(float(charge[0]), 0.0204, places=3)
        self.assertTrue(np.allclose(charge[1:20], 0.0, atol=1e-4))
        self.assertTrue(np.allclose(charge[21:], 0.0, atol=1e-4))


if __name__ == "__main__":
    unittest.main()
