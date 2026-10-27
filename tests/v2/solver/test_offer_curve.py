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

import unittest

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import lp
from solver import lp as _solver_lp
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


def _cheap_banded_plan(imp0=None, curve=True):
    """Raw prices of 1c with a band that hedges import up towards 60c: the
    risk-adjusted prices the LP pays (54c) are fifty times the raw ones the
    soft-SoC penalty is derived from, so the penalty (10 x 1c) is small
    next to what recovering a battery below its Min SoC costs."""
    n = 4
    imp = np.full(n, 0.01)
    if imp0 is not None:
        imp[0] = imp0
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=imp,
            export_price=np.full(n, 0.005),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
            import_price_upper=np.full(n, 0.60),
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=40.0,
                initial_soc_kwh=2.0,
                min_soc_kwh=8.0,
                max_soc_kwh=32.0,
                max_charge_kw=5.0,
                max_discharge_kw=5.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.005,
                salvage_value=0.0,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
        import_price_risk_aversion=0.9,
        compute_offer_curve=curve,
    )


def _cheap_banded_import(price):
    """Period-0 ordinary import of a fresh plan at the raw price behind the
    adjusted `price`."""
    from solver.network import _raw_price_for_adjusted

    raw = _raw_price_for_adjusted(price, 0.01, np.full(4, 0.60), 0.9, direction="up")
    d = _cheap_banded_plan(imp0=raw, curve=False)
    return float(d.grid_import_kw[0] - d.grid_import_excess_kw[0])


def _banded_plan(n, solar, start, aversion, imp0=None, exp0=None, curve=True):
    """`_curve_plan`'s site with a price-risk band on both sides (#1993):
    import could run to 0.60, export fall to 0.02, hedged by `aversion`."""
    imp = np.full(n, 0.30)
    exp = np.full(n, 0.10)
    if imp0 is not None:
        imp[0] = imp0
    if exp0 is not None:
        exp[0] = exp0
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=imp,
            export_price=exp,
            import_limit_kw=50.0,
            export_limit_kw=50.0,
            import_price_upper=np.full(n, 0.60),
            export_price_lower=np.full(n, 0.02),
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=40.0,
                initial_soc_kwh=start,
                min_soc_kwh=8.0,
                max_soc_kwh=32.0,
                max_charge_kw=5.0,
                max_discharge_kw=5.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.0,
            )
        ],
        solar=SolarConfig(forecast_kw=np.asarray(solar, dtype=float)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
        import_price_risk_aversion=aversion,
        export_price_risk_aversion=aversion,
        compute_offer_curve=curve,
    )


def test_the_raw_price_behind_an_adjusted_one_is_the_hedge_inverted():
    # test_the_raw_price_behind_an_adjusted_one_is_the_hedge_inverted: #1993: `_raw_price_for_adjusted` undoes `_risk_adjusted_one_sided` at
    _solver_lp._lex_calibration_failed_until = 0.0
    for direction, raw, bound in [("up", 0.30, 0.60), ("down", 0.10, 0.02)]:
        for aversion in [0.0, 0.25, 0.9, 1.0]:
            _solver_lp._lex_calibration_failed_until = 0.0
            from solver.network import _raw_price_for_adjusted, _risk_adjusted_one_sided

            band = np.array([bound])
            for r in np.linspace(-0.5, 1.5, 41):
                adjusted = float(
                    _risk_adjusted_one_sided(
                        np.array([r]), band, aversion, direction=direction
                    )[0]
                )
                back = _raw_price_for_adjusted(
                    adjusted, raw, band, aversion, direction=direction
                )
                # At the bound itself (to float noise) every raw price ties.
                inside = r <= bound + 1e-12 if direction == "up" else r >= bound - 1e-12
                if aversion >= 1.0 and inside:
                    assert back == raw
                else:
                    assert back == pytest.approx(r, abs=1e-12)

    # test_no_interval_claims_more_than_the_price_coupled_solve_holds: #1990: a walked plateau whose ranging reaches the domain's end, and
    _solver_lp._lex_calibration_failed_until = 0.0
    from solver.network import (
        _OFFER_CURVE_DOMAIN_MAX,
        _offer_curve_ranging_walk,
    )

    curve, ranging = _offer_curve_ranging_walk(
        _PricedFake(),
        "imp",
        retail=0.30,
        hours0=1.0,
        negated=False,
        cost_map=lambda price: {"imp": price, "tie": min(price, 0.05)},
    )
    kws = [kw for _p, kw in curve]
    assert kws[0] == 7.0 and 6.0 in kws
    for (price, kw), interval in zip(curve, ranging, strict=True):
        lower = max(interval[0], _OFFER_CURVE_DOMAIN_MIN)
        upper = min(interval[1], _OFFER_CURVE_DOMAIN_MAX)
        for end in (lower, upper):
            inside = price + 0.999 * (end - price)
            assert _PricedFake.truth(inside) == kw, (price, kw, interval)

    # test_a_non_finite_cost_is_refused_before_highs_sees_it: #2054: HiGHS's dual simplex never returns on a NaN cost, whatever its
    _solver_lp._lex_calibration_failed_until = 0.0
    p = lp.LPProblem()
    x = p.add_variable("x", lb=0.0, ub=1.0, cost=1.0)
    result = p.solve(keep_basis=True)
    with pytest.raises(ValueError, match="non-finite cost"):
        result.sweep_costs_with_ranging(x, [{x: float("nan")}])


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


class _UnboundedFake:
    """An LP whose ranging is unbounded both ways, as HiGHS reports it
    (`-inf`, `inf`), recording every price the walk asks it to solve at."""

    def __init__(self):
        self.prices: list[float] = []

    def sweep_costs_with_ranging(self, var, cost_maps, *, with_solution=False):
        from solver.lp import SweepRangingStep

        (costs,) = cost_maps
        self.prices.append(costs[var])
        return [
            SweepRangingStep(
                value=1.0, cost_dn=_record(-np.inf), cost_up=_record(np.inf)
            )
        ]


class TestOfferCurveRangingWalkControlFlow(unittest.TestCase):
    """nimbus issue #678: the walk's own termination rules, isolated from
    a real solve via `_FakeRangingResult` -- directly answers #678's own
    explicitly-flagged open questions (the epsilon nudge, the iteration
    safety cap, what happens on a degenerate/non-monotonic ranging
    result).
    """

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


class TestOfferCurveRangingWalkBackstop(unittest.TestCase):
    """Nimbus issue #705: the one gap-targeted extra solve run when the
    walk and the mandatory retail solve leave a real, unresolved gap
    between them -- confirmed live on this household's real export curve
    (a genuine 5.48kW->11.37kW jump hidden in an unsampled 6.41c-9.21c
    band). Isolated from a real LP the same way every other control-flow
    test in this file is.
    """

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


_PAID_ALL_DAY = (-1.0, -1.0, -1.0)
# Paid to import in one period only: the plan's own direction round already
# holds a battery's binary that the curve's exact solve then needs too.
_PAID_ONCE = (0.10, -1.0, 0.30, 0.30, 0.30, 0.30)
# Paid to import late only: the plan needs no direction round, so only the
# curve's own rounds keep a point swept at period 0 from charging one member
# from the other.
_PAID_LATE = (0.20, 0.20, 0.20, -1.0, 0.20, 0.20)


def _shared_charger_plan(imp0=None, curve=True, prices=_PAID_ALL_DAY):
    """#2031's site: paid to import, two batteries on one 7 kW charger."""
    imp = np.array(prices, dtype=float)
    n = len(imp)
    if imp0 is not None:
        imp[0] = imp0
    group = {"shared_charger_group": "g", "shared_charger_max_kw": 7.0}
    return build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=imp,
            export_price=np.full(n, -2.0),
            import_limit_kw=28.0,
            export_limit_kw=10.0,
        ),
        batteries=[
            BatteryConfig(
                name=name,
                capacity_kwh=5.0,
                initial_soc_kwh=0.0 if prices == _PAID_ALL_DAY else 2.0,
                min_soc_kwh=0.0,
                max_soc_kwh=4.375,
                max_charge_kw=7.0,
                max_discharge_kw=7.0,
                charge_efficiency=0.875,
                discharge_efficiency=0.875,
                charge_cost=0.005,
                discharge_cost=0.005,
                salvage_value=0.0,
                **group,
            )
            for name in ("a", "c")
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[
            LoadConfig(
                name="house",
                forecast_kw=np.zeros(n) if prices == _PAID_ALL_DAY else np.ones(n),
            )
        ],
        compute_offer_curve=curve,
    )


# The site's 12 direction pairs: 3 grid, 6 battery, 3 shared charger. A
# budget of 12 buys one exact fallback: its targeted and its every-pair round.
def test_the_curves_exact_work_is_bounded(monkeypatch):
    """#2031 (Mark Purcell): past its budget of direction pairs, a point keeps
    the best one-way plan its search found instead of an exact MIP, so a
    large site's curve cannot run on (#2010's bounded secondary work)."""
    for budget, most in [(0, 0), (12, 2), (None, None)]:
        _solver_lp._lex_calibration_failed_until = 0.0
        with monkeypatch.context() as mp:
            import copy as copy_mod

            from solver import lp, network

            mp.setattr(network, "_OFFER_CURVE_ONE_WAY_NODES", 0)
            if budget is not None:
                mp.setattr(network, "_OFFER_CURVE_EXACT_PAIR_BUDGET", budget)
            copies = []
            real = copy_mod.deepcopy

            def deepcopy(obj, *a, **k):
                if isinstance(obj, lp.LPProblem):
                    copies.append(obj)  # noqa: B023
                return real(obj, *a, **k)  # noqa: B023

            mp.setattr(network.copy, "deepcopy", deepcopy)
            plan = _shared_charger_plan()
            assert plan.status == "optimal"
            assert plan.offer_curve_import
            if most is None:
                assert len(copies) > 2  # unbounded by the tests above
            else:
                assert len(copies) <= most
                assert (len(copies) > 0) == (most > 0)

    # test_the_exact_check_starts_from_the_sweeps_own_point
    # #2121: where the plan already holds direction binaries, the exact check
    # is seeded with the sweep's own one-way point, and the curve it draws is
    # the curve it draws unseeded.
    def curves(seed):
        _solver_lp._lex_calibration_failed_until = 0.0
        seeded = []
        real = lp._offer_mip_start

        def offer(h, col_values, *, phase):
            if phase == "offer_curve_exact":
                seeded.append(seed)
                if not seed:
                    return False
            return real(h, col_values, phase=phase)

        with monkeypatch.context() as mp:
            mp.setattr(lp, "_offer_mip_start", offer)
            plan = _shared_charger_plan(prices=_PAID_ONCE)
        assert plan.status == "optimal"
        return seeded, plan.offer_curve_import, plan.offer_curve_export

    seeded, *with_seed = curves(True)
    _, *without = curves(False)
    assert seeded
    for a, b in zip(with_seed, without, strict=True):
        assert [list(p) for p in a] == pytest.approx([list(p) for p in b], abs=1e-6)
