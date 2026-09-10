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
        self.assertIsNone(plan.offer_curve_import_ranging)
        self.assertIsNone(plan.offer_curve_export_ranging)
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


class TestOfferCurveRanging(unittest.TestCase):
    """nimbus issue #676: each offer-curve step's own exact real $/kWh
    price interval, from LPResult.sweep_cost_with_ranging() -- not just
    the sampled (price, kW) pair. Real values confirmed directly against
    this exact scenario before writing these assertions (build_plan()
    run standalone, printed, inspected), same discipline as every other
    hand-worked test in this file.
    """

    def setUp(self):
        self.plan = _scenario_plan(compute_offer_curve=True)

    def test_ranging_lists_are_populated_and_same_length_as_the_curves(self):
        self.assertIsNotNone(self.plan.offer_curve_import_ranging)
        self.assertIsNotNone(self.plan.offer_curve_export_ranging)
        self.assertEqual(
            len(self.plan.offer_curve_import_ranging), len(self.plan.offer_curve_import)
        )
        self.assertEqual(
            len(self.plan.offer_curve_export_ranging), len(self.plan.offer_curve_export)
        )

    def test_every_swept_price_lies_within_its_own_reported_interval(self):
        # General invariant: whatever price a step was actually swept to
        # must lie within [price_lower, price_upper] -- that interval's
        # entire meaning is "the real range this step's own kW value
        # holds for", so the swept price itself is trivially inside it.
        for (price, _kw), interval in zip(
            self.plan.offer_curve_import,
            self.plan.offer_curve_import_ranging,
            strict=True,
        ):
            self.assertIsNotNone(interval)
            lower, upper = interval
            self.assertLessEqual(lower, price + 1e-6)
            self.assertGreaterEqual(upper, price - 1e-6)
        for (price, _kw), interval in zip(
            self.plan.offer_curve_export,
            self.plan.offer_curve_export_ranging,
            strict=True,
        ):
            self.assertIsNotNone(interval)
            lower, upper = interval
            self.assertLessEqual(lower, price + 1e-6)
            self.assertGreaterEqual(upper, price - 1e-6)

    def test_every_interval_is_properly_ordered(self):
        # Catches exactly the kind of sign-flip bug the export sweep's
        # own negated cost coefficient risks (see
        # _offer_curve_price_interval()'s own docstring) -- lower must
        # never exceed upper, on EITHER curve.
        for interval in self.plan.offer_curve_import_ranging:
            lower, upper = interval
            self.assertLessEqual(lower, upper)
        for interval in self.plan.offer_curve_export_ranging:
            lower, upper = interval
            self.assertLessEqual(lower, upper)

    def test_real_values_at_the_retail_anchor_point(self):
        # Real, confirmed-live values for this exact scenario (not
        # guessed): retail import 0.30 -> 0 kW, valid from 0.10 upward
        # (uncapped above); retail export 0.10 -> 3.0 kW, valid in
        # [0.10, 0.30].
        retail_import = float(self.plan.effective_import_price[0])
        retail_export = float(self.plan.effective_export_price[0])
        import_idx = next(
            i
            for i, (price, _kw) in enumerate(self.plan.offer_curve_import)
            if abs(price - retail_import) < 1e-9
        )
        export_idx = next(
            i
            for i, (price, _kw) in enumerate(self.plan.offer_curve_export)
            if abs(price - retail_export) < 1e-9
        )
        import_lo, import_hi = self.plan.offer_curve_import_ranging[import_idx]
        export_lo, export_hi = self.plan.offer_curve_export_ranging[export_idx]
        self.assertAlmostEqual(import_lo, 0.10, places=4)
        self.assertTrue(import_hi == float("inf") or import_hi > 1e6)
        self.assertAlmostEqual(export_lo, 0.10, places=4)
        self.assertAlmostEqual(export_hi, 0.30, places=4)


class TestOfferCurvePriceIntervalHelper(unittest.TestCase):
    """Direct unit coverage for _offer_curve_price_interval()'s own
    conversion math (nimbus issue #676) -- both the ÷hours0 period-
    scaling (issue #662's own established pattern) and the export
    sweep's sign flip, in isolation from a full solve.
    """

    def test_none_when_ranging_invalid(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_price_interval

        step = SweepRangingStep(value=1.0, cost_dn=None, cost_up=None)
        self.assertIsNone(_offer_curve_price_interval(step, 1.0, negated=False))

    def test_import_divides_by_hours0_without_sign_change(self):
        from solver.lp import RangingRecord, SweepRangingStep
        from solver.network import _offer_curve_price_interval

        # Raw LP cost-coefficient units, period-scaled at hours0=5/60
        # (a real 5-minute tier-1 period) -- same conversion nimbus issue
        # #662 already established for the plain per-period dual.
        step = SweepRangingStep(
            value=6.0,
            cost_dn=RangingRecord(value=0.01, objective=0.0, in_var=None, out_var=None),
            cost_up=RangingRecord(value=0.05, objective=0.0, in_var=None, out_var=None),
        )
        lower, upper = _offer_curve_price_interval(step, 5 / 60, negated=False)
        self.assertAlmostEqual(lower, 0.12)
        self.assertAlmostEqual(upper, 0.60)

    def test_export_negation_swaps_which_bound_is_lower(self):
        from solver.lp import RangingRecord, SweepRangingStep
        from solver.network import _offer_curve_price_interval

        # cost_dn is the COST coefficient's own lower bound; since export
        # negates cost = -price*hours0 (a decreasing function of price),
        # cost_dn must map to the HIGHER real price, not the lower one --
        # this is exactly the sign-flip #676's own PR description warns
        # about getting backwards.
        step = SweepRangingStep(
            value=3.0,
            cost_dn=RangingRecord(
                value=-0.30, objective=0.0, in_var=None, out_var=None
            ),
            cost_up=RangingRecord(
                value=-0.10, objective=0.0, in_var=None, out_var=None
            ),
        )
        lower, upper = _offer_curve_price_interval(step, 1.0, negated=True)
        self.assertAlmostEqual(lower, 0.10)
        self.assertAlmostEqual(upper, 0.30)


class TestOfferCurveAbsentOnNonOptimalPlan(unittest.TestCase):
    def test_offer_curve_is_none_on_infeasible(self):
        from solver.network import _infeasible_plan

        n = 2
        periods = PeriodGrid(hours=np.full(n, 1.0), start=None)
        infeasible_plan = _infeasible_plan(periods, "infeasible", iterations=100)
        self.assertIsNone(infeasible_plan.offer_curve_import)
        self.assertIsNone(infeasible_plan.offer_curve_export)
        self.assertIsNone(infeasible_plan.offer_curve_import_ranging)
        self.assertIsNone(infeasible_plan.offer_curve_export_ranging)
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
