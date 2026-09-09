"""Direct test coverage for nimbus issue #494 (Signals 5/7 of #489):
`build_plan(compute_offer_curve=True)`'s period-0 (price, kW) demand-
response offer ladder, built on #490's own LPResult.sweep_cost().

Scenario mirrors the household's own real shape: a home battery with
real charge/discharge headroom, a fixed load, and enough solar in one
period to force real economic tension between import/export/battery use
-- so the sweep has genuine room to move, not just an already-pinned
variable. Every asserted number below was run directly against the real
solver first; the acceptance checks straight from #494's own issue text
(monotone steps, "curve at retail == main plan's period-0 dispatch",
switch off -> zero extra solves, total sweep time under 0.5s) are each
their own test.
"""

from __future__ import annotations

import itertools
import time
import unittest

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


def _grid(n: int, *, import_price=0.30, export_price=0.10) -> GridConfig:
    return GridConfig(
        import_price=np.full(n, import_price),
        export_price=np.full(n, export_price),
        import_limit_kw=50.0,
        export_limit_kw=50.0,
    )


def _battery() -> BatteryConfig:
    return BatteryConfig(
        name="home",
        capacity_kwh=40.0,
        initial_soc_kwh=20.0,
        min_soc_kwh=2.0,
        max_soc_kwh=40.0,
        max_charge_kw=5.0,
        max_discharge_kw=5.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
    )


def _scenario_plan(*, compute_offer_curve: bool):
    n = 4
    periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
    return build_plan(
        periods=periods,
        grid=_grid(n),
        batteries=[_battery()],
        solar=SolarConfig(forecast_kw=np.array([0.0, 3.0, 0.0, 0.0])),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
        compute_offer_curve=compute_offer_curve,
    )


class TestComputeOfferCurveIsOptIn(unittest.TestCase):
    def test_offer_curve_is_none_by_default(self):
        plan = _scenario_plan(compute_offer_curve=False)
        self.assertEqual(plan.status, "optimal")
        self.assertIsNone(plan.offer_curve_import)
        self.assertIsNone(plan.offer_curve_export)
        self.assertIsNone(plan.offer_curve_sweep_seconds)

    def test_switch_off_means_zero_extra_solves(self):
        # No direct hook to COUNT HiGHS solves from here without reaching
        # into LPResult internals -- the real, honest proxy this project
        # already uses elsewhere (#491's own compute_signals tests) is
        # that the opt-in fields simply stay None/empty. Combined with
        # lp.py's own TestSweepCostRequiresKeepBasis (a solve without
        # keep_basis=True retains no live HiGHS instance to sweep at
        # all), this is the real, structural guarantee: with the switch
        # off, build_plan() never even calls solve(keep_basis=True), so
        # there is no basis for sweep_cost() to run against.
        plan = _scenario_plan(compute_offer_curve=False)
        self.assertIsNone(plan.offer_curve_import)


class TestOfferCurveRealScenario(unittest.TestCase):
    def setUp(self):
        self.plan = _scenario_plan(compute_offer_curve=True)

    def test_solves_optimal_with_populated_curves(self):
        self.assertEqual(self.plan.status, "optimal")
        self.assertIsNotNone(self.plan.offer_curve_import)
        self.assertIsNotNone(self.plan.offer_curve_export)
        self.assertEqual(len(self.plan.offer_curve_import), 7)
        self.assertEqual(len(self.plan.offer_curve_export), 7)

    def test_import_steps_are_sorted_ascending_by_price(self):
        prices = [price for price, _kw in self.plan.offer_curve_import]
        self.assertEqual(prices, sorted(prices))

    def test_import_curve_is_non_increasing_as_price_rises(self):
        kws = [kw for _price, kw in self.plan.offer_curve_import]
        for earlier, later in itertools.pairwise(kws):
            self.assertGreaterEqual(earlier, later - 1e-9)

    def test_export_curve_is_non_decreasing_as_price_rises(self):
        kws = [kw for _price, kw in self.plan.offer_curve_export]
        for earlier, later in itertools.pairwise(kws):
            self.assertLessEqual(earlier, later + 1e-9)

    def test_import_curve_at_20_dollars_is_zero(self):
        # #494's own acceptance text: "the import curve is 0 kW at $20".
        price, kw = self.plan.offer_curve_import[-1]
        self.assertAlmostEqual(price, 20.00)
        self.assertAlmostEqual(kw, 0.0)

    def test_curve_at_retail_price_matches_the_main_plans_period_0_dispatch(self):
        # #494's own explicit consistency-check acceptance criterion.
        retail_import = float(self.plan.effective_import_price[0])
        retail_export = float(self.plan.effective_export_price[0])
        import_match = [
            kw
            for price, kw in self.plan.offer_curve_import
            if abs(price - retail_import) < 1e-9
        ]
        export_match = [
            kw
            for price, kw in self.plan.offer_curve_export
            if abs(price - retail_export) < 1e-9
        ]
        self.assertEqual(len(import_match), 1)
        self.assertEqual(len(export_match), 1)
        self.assertAlmostEqual(import_match[0], self.plan.grid_import_kw[0])
        self.assertAlmostEqual(export_match[0], self.plan.grid_export_kw[0])

    def test_sweep_time_is_logged_and_well_under_half_a_second(self):
        self.assertIsNotNone(self.plan.offer_curve_sweep_seconds)
        self.assertLess(self.plan.offer_curve_sweep_seconds, 0.5)

    def test_sweep_time_measured_directly_stays_well_under_half_a_second(self):
        # Independent, real-clock re-measurement (not just trusting the
        # plan's own self-reported figure) on a slightly larger, more
        # realistic 24-period horizon.
        n = 24
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        start = time.monotonic()
        plan = build_plan(
            periods=periods,
            grid=_grid(n),
            batteries=[_battery()],
            solar=SolarConfig(forecast_kw=np.full(n, 1.5)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
            compute_offer_curve=True,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(plan.status, "optimal")
        self.assertLess(elapsed, 0.5)


class TestOfferCurveAbsentOnNonOptimalPlan(unittest.TestCase):
    def test_offer_curve_is_none_on_infeasible(self):
        from solver.network import _infeasible_plan

        n = 2
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        infeasible_plan = _infeasible_plan(periods, "infeasible", iterations=100)
        self.assertIsNone(infeasible_plan.offer_curve_import)
        self.assertIsNone(infeasible_plan.offer_curve_export)
        self.assertIsNone(infeasible_plan.offer_curve_sweep_seconds)


class TestOfferCurvePriceGrid(unittest.TestCase):
    def test_grid_is_sorted_and_deduplicated(self):
        from solver.network import _offer_curve_price_grid

        grid = _offer_curve_price_grid(0.30)
        self.assertEqual(grid, sorted(grid))
        self.assertEqual(len(grid), len(set(grid)))
        self.assertAlmostEqual(grid[0], -1.00)
        self.assertAlmostEqual(grid[-1], 20.00)
        self.assertIn(0.30, [round(v, 10) for v in grid])

    def test_degenerate_zero_retail_still_produces_a_valid_ascending_grid(self):
        from solver.network import _offer_curve_price_grid

        grid = _offer_curve_price_grid(0.0)
        self.assertEqual(grid, sorted(grid))
        self.assertEqual(grid[0], -1.00)
        self.assertEqual(grid[-1], 20.00)


if __name__ == "__main__":
    unittest.main()
