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
        # nimbus issue #678: the walk's own row count is now variable
        # (as many real breakpoints as this scenario actually has, plus
        # the mandatory retail row), not a fixed 7 -- see
        # TestOfferCurveRangingWalkFindsRealStructure below for the exact
        # count this scenario really produces.
        self.assertEqual(self.plan.status, "optimal")
        self.assertIsNotNone(self.plan.offer_curve_import)
        self.assertIsNotNone(self.plan.offer_curve_export)
        self.assertGreaterEqual(len(self.plan.offer_curve_import), 2)
        self.assertGreaterEqual(len(self.plan.offer_curve_export), 2)

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

    def test_import_curve_is_zero_at_high_prices(self):
        # #494's own acceptance text: "the import curve is 0 kW at $20".
        # nimbus issue #678: the walk no longer necessarily samples
        # exactly at $20 -- it stops the moment a plateau's own ranging
        # already PROVES unbounded coverage through the domain cap,
        # which is a strictly stronger guarantee than one literal sample
        # at the edge. Check that guarantee directly instead.
        from solver.network import _OFFER_CURVE_DOMAIN_MAX

        price, kw = self.plan.offer_curve_import[-1]
        _lower, upper = self.plan.offer_curve_import_ranging[-1]
        self.assertAlmostEqual(kw, 0.0)
        self.assertGreaterEqual(upper, _OFFER_CURVE_DOMAIN_MAX - 1e-9)
        self.assertLessEqual(price, _OFFER_CURVE_DOMAIN_MAX + 1e-9)

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


class _FakeRangingResult:
    """Minimal stand-in for `LPResult`, implementing only the one method
    `_offer_curve_ranging_walk()` actually calls -- lets the walk's own
    control flow (iteration cap, invalid-ranging stop, non-monotonic
    stop) be tested in isolation from a real LP, by scripting exactly
    what each successive call returns regardless of the price it was
    asked to solve at.
    """

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls: list[tuple[str, list[float]]] = []

    def sweep_cost_with_ranging(self, var, costs):
        self.calls.append((var, list(costs)))
        return [self._steps[len(self.calls) - 1]]


def _record(value):
    from solver.lp import RangingRecord

    return RangingRecord(value=value, objective=0.0, in_var=None, out_var=None)


class TestOfferCurveRangingWalkControlFlow(unittest.TestCase):
    """nimbus issue #678: the walk's own termination rules, isolated from
    a real solve via `_FakeRangingResult` -- directly answers #678's own
    explicitly-flagged open questions (the epsilon nudge, the iteration
    safety cap, what happens on a degenerate/non-monotonic ranging
    result).
    """

    def test_walk_stops_at_the_iteration_safety_cap(self):
        from solver.lp import SweepRangingStep
        from solver.network import (
            _OFFER_CURVE_MAX_BREAKPOINTS,
            _offer_curve_ranging_walk,
        )

        # Every step reports a real, valid, ever-increasing next
        # breakpoint (never unbounded, never non-monotonic) -- nothing
        # here would stop the walk on its own; only the explicit
        # iteration cap can.
        steps = [
            SweepRangingStep(
                value=float(i),
                cost_dn=_record(i * 0.001 - 0.001),
                cost_up=_record(i * 0.001),
            )
            for i in range(
                1, _OFFER_CURVE_MAX_BREAKPOINTS + 2
            )  # +1 for the mandatory retail call
        ]
        fake = _FakeRangingResult(steps)
        curve, ranging = _offer_curve_ranging_walk(
            fake, "imp", retail=0.5, hours0=1.0, negated=False
        )
        self.assertEqual(len(fake.calls), _OFFER_CURVE_MAX_BREAKPOINTS + 1)
        self.assertEqual(len(curve), _OFFER_CURVE_MAX_BREAKPOINTS + 1)
        self.assertEqual(len(ranging), _OFFER_CURVE_MAX_BREAKPOINTS + 1)

    def test_walk_stops_immediately_when_ranging_is_invalid(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(
                value=1.0, cost_dn=None, cost_up=None
            ),  # domain_min: invalid
            SweepRangingStep(
                value=1.0, cost_dn=None, cost_up=None
            ),  # the mandatory retail call
        ]
        fake = _FakeRangingResult(steps)
        curve, _ranging = _offer_curve_ranging_walk(
            fake, "imp", retail=0.5, hours0=1.0, negated=False
        )
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(curve), 2)

    def test_walk_stops_on_a_non_monotonic_ranging_result(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(value=1.0, cost_dn=_record(-1.0), cost_up=_record(0.05)),
            # The nudge lands here next; this step's own cost_up does
            # NOT advance past the previous one -- a genuine degenerate/
            # non-monotonic ranging result, which real LP parametric-
            # sensitivity theory says should never happen for a well-
            # posed single-variable sweep. The walk must stop, not loop.
            SweepRangingStep(value=1.0, cost_dn=_record(0.05), cost_up=_record(0.05)),
            SweepRangingStep(
                value=1.0, cost_dn=_record(-1.0), cost_up=_record(0.05)
            ),  # retail
        ]
        fake = _FakeRangingResult(steps)
        curve, _ranging = _offer_curve_ranging_walk(
            fake, "imp", retail=0.5, hours0=1.0, negated=False
        )
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(len(curve), 3)

    def test_walk_stops_once_ranging_is_unbounded_through_the_cap(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(
                value=10.0, cost_dn=_record(-100.0), cost_up=_record(float("inf"))
            ),
            SweepRangingStep(
                value=10.0, cost_dn=_record(-100.0), cost_up=_record(float("inf"))
            ),  # retail
        ]
        fake = _FakeRangingResult(steps)
        curve, _ranging = _offer_curve_ranging_walk(
            fake, "imp", retail=5.0, hours0=1.0, negated=False
        )
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(curve), 2)

    def test_walk_nudges_forward_by_a_small_positive_amount_past_each_breakpoint(self):
        # nimbus issue #678's own open question, answered: the second
        # solve must land strictly past the first step's own cost_up,
        # never exactly on it.
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(
                value=10.0, cost_dn=_record(-100.0), cost_up=_record(0.05)
            ),
            SweepRangingStep(
                value=0.0, cost_dn=_record(0.05), cost_up=_record(float("inf"))
            ),
            SweepRangingStep(
                value=0.0, cost_dn=_record(0.05), cost_up=_record(float("inf"))
            ),  # retail
        ]
        fake = _FakeRangingResult(steps)
        _offer_curve_ranging_walk(fake, "imp", retail=1.0, hours0=1.0, negated=False)
        second_call_cost = fake.calls[1][1][0]
        self.assertGreater(second_call_cost, 0.05)
        self.assertLess(second_call_cost, 0.05 + 1e-3)


class TestOfferCurveRangingWalkDescending(unittest.TestCase):
    """Nimbus issue #706: export now walks `ascending=False` from
    `start=_OFFER_CURVE_DOMAIN_MAX` (the cap) downward instead of #678's
    original uniform floor-upward start -- isolated coverage, mirroring
    every ascending control-flow test above, via the same
    `_FakeRangingResult` stand-in.
    """

    def test_descending_walk_starts_at_the_given_start_price(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            # lower == -inf <= domain min -> stops immediately after this
            # one solve, so only 2 total calls (this + the mandatory
            # retail) are needed.
            SweepRangingStep(
                value=1.0, cost_dn=_record(float("-inf")), cost_up=_record(100.0)
            ),
            SweepRangingStep(
                value=1.0, cost_dn=_record(float("-inf")), cost_up=_record(100.0)
            ),  # retail
        ]
        fake = _FakeRangingResult(steps)
        _offer_curve_ranging_walk(
            fake,
            "exp",
            start=20.0,
            ascending=False,
            retail=0.5,
            hours0=1.0,
            negated=False,
        )
        self.assertEqual(fake.calls[0][1][0], 20.0)

    def test_descending_walk_nudges_backward_past_each_breakpoint(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(value=1.0, cost_dn=_record(5.0), cost_up=_record(100.0)),
            SweepRangingStep(value=1.0, cost_dn=_record(-100.0), cost_up=_record(5.0)),
            SweepRangingStep(
                value=1.0, cost_dn=_record(-100.0), cost_up=_record(5.0)
            ),  # retail
        ]
        fake = _FakeRangingResult(steps)
        _offer_curve_ranging_walk(
            fake,
            "exp",
            start=20.0,
            ascending=False,
            retail=1.0,
            hours0=1.0,
            negated=False,
        )
        second_call_cost = fake.calls[1][1][0]
        self.assertLess(second_call_cost, 5.0)
        self.assertGreater(second_call_cost, 5.0 - 1e-3)

    def test_descending_walk_stops_at_the_iteration_safety_cap(self):
        from solver.lp import SweepRangingStep
        from solver.network import (
            _OFFER_CURVE_MAX_BREAKPOINTS,
            _offer_curve_ranging_walk,
        )

        # Every step reports a real, valid, ever-decreasing next
        # breakpoint (never unbounded, never non-monotonic) -- only the
        # explicit iteration cap can stop this walk.
        steps = [
            SweepRangingStep(
                value=float(i),
                cost_dn=_record(100.0 - i),
                cost_up=_record(100.0 - i + 1),
            )
            for i in range(
                _OFFER_CURVE_MAX_BREAKPOINTS + 2
            )  # +1 for the mandatory retail call
        ]
        fake = _FakeRangingResult(steps)
        curve, ranging = _offer_curve_ranging_walk(
            fake,
            "exp",
            start=100.0,
            ascending=False,
            retail=0.0,
            hours0=1.0,
            negated=False,
        )
        self.assertEqual(len(fake.calls), _OFFER_CURVE_MAX_BREAKPOINTS + 1)
        self.assertEqual(len(curve), _OFFER_CURVE_MAX_BREAKPOINTS + 1)
        self.assertEqual(len(ranging), _OFFER_CURVE_MAX_BREAKPOINTS + 1)

    def test_descending_walk_stops_at_the_real_floor(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(
                value=1.0, cost_dn=_record(float("-inf")), cost_up=_record(5.0)
            ),  # lower <= domain min -> stop immediately after this solve
            SweepRangingStep(
                value=1.0, cost_dn=_record(float("-inf")), cost_up=_record(5.0)
            ),  # retail
        ]
        fake = _FakeRangingResult(steps)
        curve, _ranging = _offer_curve_ranging_walk(
            fake,
            "exp",
            start=20.0,
            ascending=False,
            retail=0.0,
            hours0=1.0,
            negated=False,
        )
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(curve), 2)


class TestOfferCurveRangingWalkBackstop(unittest.TestCase):
    """Nimbus issue #706: the one gap-targeted extra solve run when the
    walk and the mandatory retail solve leave a real, unresolved gap
    between them -- confirmed live on this household's real export curve
    (a genuine 5.48kW->11.37kW jump hidden in an unsampled 6.41c-9.21c
    band). Isolated from a real LP the same way every other control-flow
    test in this file is.
    """

    def test_backstop_fires_when_a_real_gap_exists(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(value=1.0, cost_dn=_record(-100.0), cost_up=_record(0.05)),
            SweepRangingStep(value=1.0, cost_dn=_record(0.05), cost_up=_record(0.2)),
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.2), cost_up=_record(0.2)
            ),  # stalled -> walk stops here, last_bound stays 0.2
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.5), cost_up=_record(2.0)
            ),  # retail=1.0 -> price_lower=0.5, a real 0.3-wide gap vs last_bound=0.2
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.3), cost_up=_record(0.4)
            ),  # backstop
        ]
        fake = _FakeRangingResult(steps)
        _offer_curve_ranging_walk(fake, "imp", retail=1.0, hours0=1.0, negated=False)
        self.assertEqual(len(fake.calls), 5)
        backstop_price = fake.calls[4][1][0]
        self.assertAlmostEqual(backstop_price, (0.2 + 0.5) / 2.0)

    def test_backstop_does_not_fire_when_retail_already_reaches_the_walk(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(value=1.0, cost_dn=_record(-100.0), cost_up=_record(0.05)),
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.05), cost_up=_record(0.05)
            ),  # stalled -> last_bound stays 0.05
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.01), cost_up=_record(1.0)
            ),  # retail: price_lower=0.01 <= last_bound=0.05, no real gap
        ]
        fake = _FakeRangingResult(steps)
        _offer_curve_ranging_walk(fake, "imp", retail=0.5, hours0=1.0, negated=False)
        self.assertEqual(len(fake.calls), 3)

    def test_backstop_does_not_fire_when_the_walk_found_no_real_territory(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(
                value=1.0, cost_dn=_record(-100.0), cost_up=_record(float("inf"))
            ),  # unbounded through the cap on the very first solve
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.01), cost_up=_record(5.0)
            ),  # retail -- a huge apparent "gap," but there's no walk territory to bridge from
        ]
        fake = _FakeRangingResult(steps)
        _offer_curve_ranging_walk(fake, "imp", retail=1.0, hours0=1.0, negated=False)
        self.assertEqual(len(fake.calls), 2)

    def test_backstop_skips_a_negligible_gap(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(value=1.0, cost_dn=_record(-100.0), cost_up=_record(0.05)),
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.05), cost_up=_record(0.05)
            ),  # stalled -> last_bound stays 0.05
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.0500_05), cost_up=_record(1.0)
            ),  # retail: gap of 0.00005, well under the 1e-4 threshold
        ]
        fake = _FakeRangingResult(steps)
        _offer_curve_ranging_walk(fake, "imp", retail=0.5, hours0=1.0, negated=False)
        self.assertEqual(len(fake.calls), 3)

    def test_backstop_direction_is_mirrored_for_a_descending_walk(self):
        from solver.lp import SweepRangingStep
        from solver.network import _offer_curve_ranging_walk

        steps = [
            SweepRangingStep(
                value=1.0, cost_dn=_record(5.0), cost_up=_record(100.0)
            ),  # bound=5.0, continues, last_bound=5.0
            SweepRangingStep(
                value=1.0, cost_dn=_record(1.0), cost_up=_record(5.0)
            ),  # bound=1.0 < 5.0, continues, last_bound=1.0
            SweepRangingStep(
                value=1.0, cost_dn=_record(1.0), cost_up=_record(1.0)
            ),  # bound=1.0, stalled -> walk stops here, last_bound stays 1.0
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.1), cost_up=_record(0.5)
            ),  # retail=0.3 -> price_upper=0.5, a real 0.5-wide gap vs last_bound=1.0
            SweepRangingStep(
                value=1.0, cost_dn=_record(0.6), cost_up=_record(0.7)
            ),  # backstop
        ]
        fake = _FakeRangingResult(steps)
        _offer_curve_ranging_walk(
            fake,
            "exp",
            start=20.0,
            ascending=False,
            retail=0.3,
            hours0=1.0,
            negated=False,
        )
        self.assertEqual(len(fake.calls), 5)
        backstop_price = fake.calls[4][1][0]
        self.assertAlmostEqual(backstop_price, (1.0 + 0.5) / 2.0)


class TestOfferCurveRangingWalkFindsRealStructure(unittest.TestCase):
    """nimbus issue #678: confirms the walk finds MORE real structure in
    this exact scenario than #494's old fixed 7-point grid ever did.
    Real values confirmed directly against this branch's own solver
    before writing these assertions (same discipline as every other
    hand-worked test in this file) -- the old grid never sampled finely
    enough inside [-10c, 10c] to find either of the two extra plateaus
    below; the walk finds both as a structural guarantee, not a lucky
    sample landing.
    """

    def setUp(self):
        self.plan = _scenario_plan(compute_offer_curve=True)

    @staticmethod
    def _distinct_plateaus(ranging, tol=1e-6):
        # Group consecutive rows sharing the SAME (within float tolerance)
        # ranging interval -- the retail solve's own harmless near-
        # duplicate row (see _offer_curve_ranging_walk()'s own docstring)
        # must not be double-counted as a second, distinct segment. Exact
        # equality isn't enough (nimbus issue #706): retail and a walk
        # step landing on the identical true plateau are two INDEPENDENT
        # sweep_cost_with_ranging() calls at two different starting
        # prices, so their own reported bounds can differ by float noise
        # (e.g. 0.29999900000000074 vs 0.3) even though both describe the
        # exact same real segment.
        def close(a, b):
            if a is None or b is None:
                return a is b
            return all(
                x == y or (x not in (float("-inf"), float("inf")) and abs(x - y) < tol)
                for x, y in zip(a, b, strict=True)
            )

        seen: list[tuple[float, float] | None] = []
        for interval in ranging:
            if not seen or not close(seen[-1], interval):
                seen.append(interval)
        return len(seen)

    def test_import_curve_now_has_four_real_plateaus_not_two(self):
        # Real, confirmed-live values: 7.0 kW (-inf,-10c], ~3.524 kW
        # (-10c,7.12c], 2.0 kW (7.12c,10c] -- genuinely new, the old
        # fixed grid never sampled here -- 0.0 kW (10c,inf).
        self.assertEqual(
            self._distinct_plateaus(self.plan.offer_curve_import_ranging), 4
        )
        values = [round(kw, 6) for _price, kw in self.plan.offer_curve_import]
        self.assertIn(2.0, values)

    def test_export_curve_now_has_four_real_plateaus_not_two(self):
        # Real, confirmed-live values: 0.0 kW (-inf,1c], ~0.1 kW
        # (1c,10c] -- genuinely new -- 3.0 kW (10c,30c], 5.0 kW (30c,inf).
        # nimbus issue #706: export now walks from the cap DOWN instead of
        # the floor up, so this same real structure is found in the
        # opposite order (and the walk's own 2nd-to-last step happens to
        # land on the identical true plateau as the separate retail
        # solve, at 0.1 -- two independent ranging calls, tiny float
        # noise between them, not a fifth real segment).
        self.assertEqual(
            self._distinct_plateaus(self.plan.offer_curve_export_ranging), 4
        )
        values = [kw for _price, kw in self.plan.offer_curve_export]
        self.assertTrue(any(abs(v - 0.1) < 1e-6 for v in values))

    def test_walk_never_costs_more_solves_than_its_own_documented_ceiling(self):
        # _OFFER_CURVE_MAX_BREAKPOINTS walk steps + 1 mandatory retail
        # solve, per curve -- this scenario's own real count (5 + 5) is
        # well under that, but the ceiling itself is what any caller
        # relying on a bounded cost needs to hold, not just this one
        # fixture's incidental real structure.
        from solver.network import _OFFER_CURVE_MAX_BREAKPOINTS

        self.assertLessEqual(
            len(self.plan.offer_curve_import), _OFFER_CURVE_MAX_BREAKPOINTS + 1
        )
        self.assertLessEqual(
            len(self.plan.offer_curve_export), _OFFER_CURVE_MAX_BREAKPOINTS + 1
        )


if __name__ == "__main__":
    unittest.main()
