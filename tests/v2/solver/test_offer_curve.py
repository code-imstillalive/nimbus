"""Layer 1 of the rebuilt suite (#1837): the period-0 offer curve (#494,
Signals 5/7 of #489) and its ranging walk (#676, #678).

The curve is a shortcut for "what would the plan do at this price", so its
oracle is the plan itself (#733, Mark Purcell's live finding): every point
it publishes must equal a fresh solve with period 0 at that price. The walk's
own control flow (iteration cap, invalid or non-monotonic ranging, the
backstop) is checked against a scripted stand-in for the LP result, carried
over from the old file with its fixtures.
"""

from __future__ import annotations

import time
import unittest

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import lp
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import _OFFER_CURVE_DOMAIN_MIN, build_plan
from test_plan_invariants import SETTINGS, _num


def _grid(
    n: int, *, import_price=0.30, export_price=0.10, price0_override=None
) -> GridConfig:
    imp = np.full(n, import_price)
    if price0_override is not None:
        imp[0] = price0_override
    return GridConfig(
        import_price=imp,
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


def _curve_plan(n, solar, start, imp0=None, exp0=None, options=None, curve=True):
    imp = np.full(n, 0.30)
    exp = np.full(n, 0.10)
    if imp0 is not None:
        imp[0] = imp0
    if exp0 is not None:
        exp[0] = exp0
    battery = BatteryConfig(
        name="home",
        capacity_kwh=40.0,
        initial_soc_kwh=start,
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
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=imp,
            export_price=exp,
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        ),
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.asarray(solar, dtype=float)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
        compute_offer_curve=curve,
        solve_options=options,
    )


_SITES = st.tuples(
    st.integers(2, 5),
    st.data(),
    _num(2.0, 38.0),
    st.booleans(),
)


def _drawn(site):
    n, data, start, calibrated = site
    solar = data.draw(st.lists(_num(0.0, 6.0), min_size=n, max_size=n))
    options = lp.CalibratedOptions() if calibrated else None
    return n, solar, start, options


@SETTINGS
@given(_SITES)
def test_the_offer_curve_is_an_ordered_ladder_anchored_on_the_plan(site):
    """#494's acceptance, and #676/#678's ranging walk: prices run upward
    from the market floor; import never rises with price and export never
    falls; each swept price lies inside its own reported, ordered ranging
    interval; the walk never publishes more points than its documented
    ceiling (its steps, the retail solve and #705's backstop); the import curve starts at the market floor; and both curves
    carry a point at the retail price,
    with and without the calibrated objective. (That point's kW can differ
    from the plan's own period 0 on a genuine tie: with equal prices either
    side, discharging now or later costs the same. Exactness is #1990's.)"""
    n, solar, start, options = _drawn(site)
    plan = _curve_plan(n, solar, start, options=options)
    assert plan.status == "optimal"
    from solver.network import _OFFER_CURVE_MAX_BREAKPOINTS

    for curve, ranging, rising in (
        (plan.offer_curve_import, plan.offer_curve_import_ranging, False),
        (plan.offer_curve_export, plan.offer_curve_export_ranging, True),
    ):
        prices = [p for p, _ in curve]
        kws = [k for _, k in curve]
        assert prices == sorted(prices)
        steps = np.diff(kws)
        assert ((steps >= -1e-6) if rising else (steps <= 1e-6)).all()
        assert len(ranging) == len(curve) <= _OFFER_CURVE_MAX_BREAKPOINTS + 2
        for (price, _kw), interval in zip(curve, ranging, strict=True):
            if interval is None:
                continue
            lower, upper = interval
            assert lower <= upper
            assert lower - 1e-9 <= price <= upper + 1e-9
    assert plan.offer_curve_import[0][0] == _OFFER_CURVE_DOMAIN_MIN
    for curve, retail in (
        (plan.offer_curve_import, plan.effective_import_price),
        (plan.offer_curve_export, plan.effective_export_price),
    ):
        assert any(abs(p - float(retail[0])) < 1e-9 for p, _k in curve)


@SETTINGS
@given(_SITES)
def test_every_offer_curve_point_is_what_a_fresh_solve_at_its_price_does(site):
    """#733 (Mark Purcell, live) and #1990: the curve is a shortcut for "what
    would the plan do at this price", not a separate opinion. Every published
    import point is the period-0 import a fresh solve at that price plans,
    and every export point the period-0 export. On a genuine tie (equal
    prices either side of period 0, so now or later costs the same) either
    answer is optimal; the point then matches a fresh solve a hair either
    side of its price. #1990 found the curve wrong mid-plateau (the #732
    loss tie-break and #390 valve move with the price; the sweep moved only
    the import cost) and on points a real plan re-solves one-way (#238)."""
    n, solar, start, options = _drawn(site)
    plan = _curve_plan(n, solar, start, options=options)
    assert plan.status == "optimal"

    def fresh(key, price, flow):
        d = _curve_plan(n, solar, start, options=options, curve=False, **{key: price})
        return float(getattr(d, flow)[0])

    for curve, key, flow in (
        (plan.offer_curve_import, "imp0", "grid_import_kw"),
        (plan.offer_curve_export, "exp0", "grid_export_kw"),
    ):
        for price, kw in curve:
            if fresh(key, price, flow) == pytest.approx(kw, abs=1e-4):
                continue
            eps = 1e-4 * max(1.0, abs(price))
            sides = {round(fresh(key, price + d, flow), 4) for d in (-eps, eps)}
            assert round(kw, 4) in sides, (key, price, kw, sides)


@SETTINGS
@given(_SITES)
def test_every_ranging_interval_holds_its_point_from_end_to_end(site):
    """#676 and #1990: a point's ranging interval claims its kW holds across
    the whole interval, so a fresh solve just inside either end plans that
    kW too (an unbounded end is checked at the market floor or cap). #1990
    found the far end of a walked plateau taken from the LP's own ranging,
    which holds every other cost still, so it claimed a plateau ran to the
    cap when a coupled cost ended it far sooner."""
    from solver.network import _OFFER_CURVE_DOMAIN_MAX, _OFFER_CURVE_DOMAIN_MIN

    n, solar, start, options = _drawn(site)
    plan = _curve_plan(n, solar, start, options=options)
    assert plan.status == "optimal"

    def fresh(key, price, flow):
        d = _curve_plan(n, solar, start, options=options, curve=False, **{key: price})
        return float(getattr(d, flow)[0])

    for curve, ranging, key, flow in (
        (
            plan.offer_curve_import,
            plan.offer_curve_import_ranging,
            "imp0",
            "grid_import_kw",
        ),
        (
            plan.offer_curve_export,
            plan.offer_curve_export_ranging,
            "exp0",
            "grid_export_kw",
        ),
    ):
        for (price, kw), interval in zip(curve, ranging, strict=True):
            if interval is None:
                continue
            lower = max(interval[0], _OFFER_CURVE_DOMAIN_MIN)
            upper = min(interval[1], _OFFER_CURVE_DOMAIN_MAX)
            for end in (lower, upper):
                if abs(end - price) < 1e-6:
                    continue
                inside = price + 0.999 * (end - price)
                if fresh(key, inside, flow) == pytest.approx(kw, abs=1e-4):
                    continue
                eps = 1e-4 * max(1.0, abs(inside))
                sides = {round(fresh(key, inside + d, flow), 4) for d in (-eps, eps)}
                assert round(kw, 4) in sides, (key, price, kw, interval, inside, sides)


def _valve_plan(imp0=None, curve=True):
    imp = np.full(3, 0.30)
    if imp0 is not None:
        imp[0] = imp0
    return build_plan(
        periods=PeriodGrid(np.ones(3)),
        grid=GridConfig(
            import_price=imp,
            export_price=np.full(3, 0.10),
            import_limit_kw=3.0,
            export_limit_kw=3.0,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=40.0,
                initial_soc_kwh=2.0,
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
        ],
        solar=SolarConfig(forecast_kw=np.zeros(3)),
        loads=[LoadConfig(name="house", forecast_kw=np.array([1.0, 1.0, 10.0]))],
        compute_offer_curve=curve,
    )


def test_an_import_point_carries_the_emergency_import_markup_at_its_own_price():
    """#390 and #1990: period 2 needs more than the 3 kW limit and the
    battery can give, so it draws #390's emergency import, marked up by 10x
    the plan's highest import price. Each kWh charged now saves 0.95 x 0.95
    kWh of it, so a real plan keeps charging at the limit whatever period
    0's price: at $10/kWh the markup is $100. Held at the plan's own $5,
    the curve stopped charging near $4.80."""
    plan = _valve_plan()
    assert plan.status == "optimal"
    assert float(plan.grid_import_excess_kw[2]) > 1.0
    for price, kw in plan.offer_curve_import:
        # The curve offers ordinary import; the emergency draw is not bought
        # at the offered price (#238 counts it apart the same way).
        d = _valve_plan(imp0=price, curve=False)
        fresh = float(d.grid_import_kw[0] - d.grid_import_excess_kw[0])
        assert kw == pytest.approx(fresh, abs=1e-4), (price, kw, fresh)
    covering = [
        kw
        for (_price, kw), interval in zip(
            plan.offer_curve_import, plan.offer_curve_import_ranging, strict=True
        )
        if interval is not None and interval[0] <= 10.0 <= interval[1]
    ]
    assert covering and all(kw == pytest.approx(3.0, abs=1e-4) for kw in covering)


def test_the_retail_point_claims_only_the_range_a_real_solve_holds():
    """#1990, found by the interval property above: the retail point's
    ranging held every other cost still, so on this site it claimed 6 kW
    all the way down from the market floor, where a fresh solve plans 7 kW.
    Each end of an off-walk point is now checked against the real solve."""
    from solver.network import _OFFER_CURVE_DOMAIN_MIN

    plan = _curve_plan(2, [1.0, 0.0], 2.0)
    retail = float(plan.effective_import_price[0])
    ((kw, interval),) = [
        (k, r)
        for (p, k), r in zip(
            plan.offer_curve_import, plan.offer_curve_import_ranging, strict=True
        )
        if abs(p - retail) < 1e-9
    ]
    lower = max(interval[0], _OFFER_CURVE_DOMAIN_MIN)
    inside = retail + 0.999 * (lower - retail)
    fresh = float(
        _curve_plan(2, [1.0, 0.0], 2.0, imp0=inside, curve=False).grid_import_kw[0]
    )
    assert fresh == pytest.approx(kw, abs=1e-4), (kw, interval, inside, fresh)


def test_the_offer_curve_is_opt_in_and_absent_from_a_failed_plan():
    """#494: off by default, so no extra solves; a plan that did not solve
    carries no curve, ranging or timing."""
    plan = _curve_plan(4, [0, 3, 0, 0], 20.0, curve=False)
    assert plan.offer_curve_import is None and plan.offer_curve_export is None
    assert plan.offer_curve_sweep_seconds is None
    from solver.network import _infeasible_plan

    failed = _infeasible_plan(PeriodGrid(np.ones(2)), "infeasible", iterations=1)
    for field in (
        "offer_curve_import",
        "offer_curve_export",
        "offer_curve_import_ranging",
        "offer_curve_export_ranging",
        "offer_curve_sweep_seconds",
    ):
        assert getattr(failed, field) is None, field


def test_the_offer_curve_sweep_stays_under_half_a_second():
    """#494's bar: the whole sweep under 0.5 s, self-reported and measured
    on a 24-period day, with and without the calibrated objective."""
    for options in (None, lp.CalibratedOptions()):
        started = time.monotonic()
        plan = _curve_plan(24, np.full(24, 1.5), 20.0, options=options)
        assert plan.status == "optimal"
        assert plan.offer_curve_sweep_seconds < 0.5
        assert time.monotonic() - started < 0.5 + 5.0


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


class _PricedFake:
    """A stand-in for `LPResult` that answers by price (#1990): the true
    kW is 7 below 5c and 6 above, while its ranging, like an LP's with the
    coupled costs held still, claims each plateau runs the whole domain."""

    def sweep_costs_with_ranging(self, var, cost_maps, *, with_solution=False):
        from solver.lp import SweepRangingStep

        (costs,) = cost_maps
        return [
            SweepRangingStep(
                value=self.truth(costs[var]),
                cost_dn=_record(-1e9),
                cost_up=_record(1e9),
            )
        ]

    @staticmethod
    def truth(price):
        return 7.0 if price < 0.05 else 6.0


def test_no_interval_claims_more_than_the_price_coupled_solve_holds():
    """#1990: a walked plateau whose ranging reaches the domain's end, and
    the retail point's two ends, are each checked against the solve at
    that price, not taken from the ranging."""
    from solver.network import (
        _OFFER_CURVE_DOMAIN_MAX,
        _OFFER_CURVE_DOMAIN_MIN,
        _offer_curve_ranging_walk,
    )

    curve, ranging = _offer_curve_ranging_walk(
        _PricedFake(),
        "imp",
        retail=0.30,
        hours0=1.0,
        negated=False,
        cost_map=lambda price: {"imp": price},
    )
    kws = [kw for _p, kw in curve]
    assert kws[0] == 7.0 and 6.0 in kws
    for (price, kw), interval in zip(curve, ranging, strict=True):
        lower = max(interval[0], _OFFER_CURVE_DOMAIN_MIN)
        upper = min(interval[1], _OFFER_CURVE_DOMAIN_MAX)
        for end in (lower, upper):
            inside = price + 0.999 * (end - price)
            assert _PricedFake.truth(inside) == kw, (price, kw, interval)


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
    """Nimbus issue #705: export now walks `ascending=False` from
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
    """Nimbus issue #705: the one gap-targeted extra solve run when the
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
