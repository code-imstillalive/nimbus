"""nimbus issue #613 (item 2 of 3): deferrable loads should prefer the
EARLIEST feasible period when the real system cost of several periods is
genuinely tied, instead of landing on whichever arbitrary vertex HiGHS's
own simplex pivoting happens to settle on. Direct LP-level proof of
network.build_plan()'s new `adequacy_earliness_budget_kw` mechanism (see
that function's own docstring, and DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW's
in network.py) -- never large enough to override a REAL price difference
(materiality preserved), but large enough to deterministically break a
genuine tie toward running as soon as possible.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    DEFAULT_ADEQUACY_SHORTFALL_PRICE,
    AdequacyLoadConfig,
    AdequacyWindow,
    BatteryConfig,
    GridConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW, build_plan


def _flat_grid(n: int, hours: float = 1.0) -> PeriodGrid:
    return PeriodGrid(hours=np.full(n, hours), start=None)


def _base_battery(**overrides) -> BatteryConfig:
    defaults = {
        "name": "battery",
        "capacity_kwh": 20.0,
        "initial_soc_kwh": 10.0,
        "min_soc_kwh": 2.0,
        "max_soc_kwh": 20.0,
        "max_charge_kw": 10.0,
        "max_discharge_kw": 10.0,
        "charge_efficiency": 0.99,
        "discharge_efficiency": 0.99,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
    }
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _flat_price_scenario(n: int = 24, import_price: float = 0.20):
    """A wholly flat, tied-cost world: no solar, no battery incentive to
    prefer one hour over another, a single constant import price for
    every period. Any real cost difference the LP sees between two
    feasible periods for the adequacy load comes ONLY from the earliness
    term under test, never from a genuine price signal."""
    periods = _flat_grid(n)
    grid = GridConfig(
        import_price=np.full(n, import_price),
        export_price=np.full(n, 0.0),
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )
    solar = SolarConfig(forecast_kw=np.zeros(n))
    battery = _base_battery(
        initial_soc_kwh=10.0, max_charge_kw=0.0, max_discharge_kw=0.0
    )
    return periods, grid, solar, battery


def _run_forced_single_period(
    *,
    n: int,
    period: int,
    target_kwh: float,
    max_power_kw: float,
    import_price: float = 0.20,
    adequacy_earliness_budget_kw: float = DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW,
):
    """Forces the whole target to be delivered in exactly one period via
    `allowed`, so two otherwise-identical solves differing only in WHICH
    single period was forced can be compared on `plan.total_cost` alone
    -- any difference is attributable ONLY to the earliness term, not to
    HiGHS's own solver-internal tie-breaking (which is real, but
    implementation-defined and not something a test should depend on)."""
    periods, grid, solar, battery = _flat_price_scenario(n, import_price)
    allowed = np.zeros(n, dtype=bool)
    allowed[period] = True
    adequacy = [
        AdequacyLoadConfig(
            name="hws",
            max_power_kw=max_power_kw,
            target_kwh=target_kwh,
            deadline_period=n - 1,
            earliest_period=0,
            allowed=allowed,
            shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
        )
    ]
    return build_plan(
        periods=periods,
        grid=grid,
        batteries=[battery],
        solar=solar,
        loads=[],
        adequacy_loads=adequacy,
        adequacy_earliness_budget_kw=adequacy_earliness_budget_kw,
    )


class TestEarlinessBreaksGenuineTies(unittest.TestCase):
    def test_earlier_forced_period_is_strictly_cheaper_by_default(self):
        plan_early = _run_forced_single_period(
            n=24, period=2, target_kwh=1.0, max_power_kw=2.0
        )
        plan_late = _run_forced_single_period(
            n=24, period=20, target_kwh=1.0, max_power_kw=2.0
        )
        self.assertEqual(plan_early.status, "optimal")
        self.assertEqual(plan_late.status, "optimal")
        self.assertLess(plan_early.total_cost, plan_late.total_cost)

    def test_no_earliness_bias_when_budget_is_zero(self):
        # Same two forced-period solves, but with the mechanism fully
        # disabled -- with a flat price and no battery/solar incentive,
        # WHICH single period delivers 1.0 kWh must cost identically
        # either way once the earliness term is gone.
        plan_early = _run_forced_single_period(
            n=24,
            period=2,
            target_kwh=1.0,
            max_power_kw=2.0,
            adequacy_earliness_budget_kw=0.0,
        )
        plan_late = _run_forced_single_period(
            n=24,
            period=20,
            target_kwh=1.0,
            max_power_kw=2.0,
            adequacy_earliness_budget_kw=0.0,
        )
        self.assertAlmostEqual(plan_early.total_cost, plan_late.total_cost, places=6)

    def test_unforced_placement_prefers_the_earliest_feasible_periods(self):
        # The real, user-facing behaviour change: with every period
        # equally free (no `allowed` mask at all) and a genuinely flat
        # price, the load now delivers in the EARLIEST periods of its
        # own window, not an arbitrary vertex.
        n = 24
        periods, grid, solar, battery = _flat_price_scenario(n)
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=1.0,
                target_kwh=2.0,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = plan.adequacy_loads[0].power_kw
        self.assertAlmostEqual(float(power[0]), 1.0, places=4)
        self.assertAlmostEqual(float(power[1]), 1.0, places=4)
        self.assertTrue(np.allclose(power[2:], 0.0, atol=1e-4))


class TestEarlinessNeverOverridesARealPriceSignal(unittest.TestCase):
    def test_a_real_cheaper_later_period_still_wins(self):
        # Materiality preserved: period 20 is genuinely, substantially
        # cheaper (a real price spread far larger than the earliness
        # budget could ever contribute) -- the LP must still place the
        # load there, not at period 2, despite the earliness term's own
        # preference for running sooner.
        n = 24
        import_price = np.full(n, 0.30)
        import_price[20] = 0.02  # a real, deliberate cheap period late in the horizon
        periods = _flat_grid(n)
        grid = GridConfig(
            import_price=import_price,
            export_price=np.full(n, 0.0),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        solar = SolarConfig(forecast_kw=np.zeros(n))
        battery = _base_battery(max_charge_kw=0.0, max_discharge_kw=0.0)
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=1.0,
                target_kwh=1.0,
                deadline_period=n - 1,
                earliest_period=0,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = plan.adequacy_loads[0].power_kw
        self.assertAlmostEqual(float(power[20]), 1.0, places=3)
        self.assertAlmostEqual(float(np.sum(power[:20])), 0.0, places=3)


class TestEarlinessRespectsWindowsIndependently(unittest.TestCase):
    def test_each_window_prefers_its_own_earliest_periods(self):
        # nimbus issue #612 interaction: a windowed load's earliness
        # preference must anchor to each window's OWN start, not bias
        # everything toward window 1 just because it's earlier in
        # absolute time (see network.py's own comment on why the global
        # elapsed_hours array still gets this right).
        n = 48
        periods, grid, solar, battery = _flat_price_scenario(n)
        adequacy = [
            AdequacyLoadConfig(
                name="hws",
                max_power_kw=1.0,
                target_kwh=1.0,
                deadline_period=16,
                earliest_period=6,
                shortfall_price=DEFAULT_ADEQUACY_SHORTFALL_PRICE,
                windows=(
                    AdequacyWindow(
                        earliest_period=6, deadline_period=16, target_kwh=1.0
                    ),
                    AdequacyWindow(
                        earliest_period=30, deadline_period=40, target_kwh=1.0
                    ),
                ),
            )
        ]
        plan = build_plan(
            periods=periods,
            grid=grid,
            batteries=[battery],
            solar=solar,
            loads=[],
            adequacy_loads=adequacy,
        )
        self.assertEqual(plan.status, "optimal")
        power = plan.adequacy_loads[0].power_kw
        self.assertAlmostEqual(float(power[6]), 1.0, places=4)
        self.assertAlmostEqual(float(power[30]), 1.0, places=4)
        self.assertTrue(np.allclose(power[7:16], 0.0, atol=1e-4))
        self.assertTrue(np.allclose(power[31:40], 0.0, atol=1e-4))


class TestEarlinessBudgetIsBoundedAcrossTheWholeHorizon(unittest.TestCase):
    def test_max_possible_earliness_spread_never_exceeds_the_budget_constant(self):
        # By construction (network.py's own DEFAULT_ADEQUACY_EARLINESS_
        # BUDGET_KW docstring): forcing delivery into the very LAST
        # period of a long horizon vs. the very FIRST must cost at most
        # `adequacy_earliness_budget_kw` $/kWh more, regardless of how
        # long the horizon is -- never large enough to be mistaken for a
        # real MIN_CHARGE_DISCHARGE_COST_SPREAD-sized price difference.
        n = 96  # a full 96h horizon, this project's real forecast_horizon_hours
        target_kwh = 1.0
        plan_first = _run_forced_single_period(
            n=n, period=0, target_kwh=target_kwh, max_power_kw=2.0
        )
        plan_last = _run_forced_single_period(
            n=n, period=n - 1, target_kwh=target_kwh, max_power_kw=2.0
        )
        spread_per_kwh = (plan_last.total_cost - plan_first.total_cost) / target_kwh
        self.assertLessEqual(
            spread_per_kwh, DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW + 1e-9
        )
        # And it should be a real, non-trivial fraction of the budget for
        # a genuinely long horizon (not accidentally ~0), confirming the
        # mechanism is actually active, not silently no-op'd.
        self.assertGreater(spread_per_kwh, DEFAULT_ADEQUACY_EARLINESS_BUDGET_KW * 0.5)


if __name__ == "__main__":
    unittest.main()
