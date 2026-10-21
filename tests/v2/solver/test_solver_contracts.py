"""Layer 1 of the rebuilt suite (#1837): contracts the solver area's cutover
check found only an old file holding (tests/v2/solver/CUTOVER.md).

- A matrix entry at or below HiGHS's `small_matrix_value` (1e-9) is dropped
  before the row goes in, so it can neither raise (highspy 1.15 raises on
  HiGHS's drop warning, HAEO #501/#511) nor change the answer; a row whose
  every entry is dropped keeps its bounds, so 0 == 1 stays infeasible
  (#1837, 20 Oct).
- A plan that did not solve hands each consumer its own zero arrays: none
  is the same object as another, so writing into one cannot change the
  rest (#356).
- A committed P2P block (#1761) is an exact ceiling: export inside it never
  exceeds the committed rate, and a block the battery can fund is delivered
  exactly. (Its shortfall price: `test_plan_features`.)
- The failing-model dump (#773) marks a phase dumped only once the write
  succeeds, so a transient failure never blocks the next capture (#1074,
  found in the IV&V pass since #1058).
  Each phase gets one capture, named for its issue, in the configured
  folder; a label cannot escape that folder or grow the filename past
  reason; nothing the write can throw reaches the solve; and only an
  alarming call (#945's tier), not a merely slow one, is captured.
- The "Solar forecast risk" dial (`risk_aversion`) leans solar toward its
  lower band and leaves house load on its point forecast (household, 12 Oct
  2026): a wide load band once had the plan budget load that never came.
- The same physical scenario costs and dispatches the same whatever the
  period length (#338): the soft-SoC penalty is per kWh, not per period.
- The offer curve's cost sweep (#494, Signals 5/7 of #489) re-solves at
  each cost with only that one coefficient changed, puts the coefficient
  back afterwards, and refuses without a kept basis or a known variable;
  its ranging form (#676) reports the same values and, at each step, the
  exact cost interval that step holds over.
- The lexicographic tie bound's slack (#773) is the solver's own
  feasibility tolerance made explicit: never below it (the bound would cut
  off the very optimum it came from, as `phase2_secondary` once did live),
  never a price (under a thousandth of a cent at household objectives).
- A solve HiGHS could not finish (time or iteration limit, unknown, model
  or solve error) is `error`, with HiGHS's own status name, never
  `infeasible`: only a model proved to have no plan is that (#356). Every
  solve carries the module's time limit, so a hard model cannot hold the
  caller's thread for ever.
- With a previous plan, period 0 is anchored to what was dispatched (the
  old plan's period 0), not to the old period its start falls in; every
  later period keeps the old plan's timeline (#1576; #635, #633; measured on
  #1417). A previous plan older than one period is past, so containment
  holds for period 0 too.
- Pinning a MIP's binaries (#616's on/off) for the clean re-solve (#773) relaxes exactly
  the binary columns to continuous and fixes each at its rounded value,
  leaves every other column alone, and warns, with the count and the worst
  gap, only when a binary sits beyond the integrality tolerance. The
  warning is measurement only: it never changes the pin.
- A pinned re-solve runs under the tolerance its point was accepted under
  (#773): the LP's primal tolerance is widened to the MIP's for that block
  only (never narrowed), put back afterwards even on an exception, and a
  solver that refuses option calls never breaks the solve.
- With a flat export bonus price, the capped bonus volume is claimed in
  one block that runs to the end of the window (household, 20 Aug 2026:
  "our window closes 0.00"), never scattered ON/OFF across it, and the
  export itself does not move.
- An adequacy load reports what it cost to run at the plan's own marginal
  price (#483, sub-issue of #476; #613 dual): with import the marginal kWh, that is its energy at the
  import price of each period; under a free solar surplus, nothing. It is
  reported whether or not the load has a value (#482's profit needs one).
  Its profit is its value less that cost, over every window (#612); a load
  priced out everywhere runs nothing and earns exactly 0.
- Narrowing an adequacy load's window never makes the plan cheaper: the
  plan it picks in the wide window is optimal for it, so an unused period
  with a lower marginal price (#662: per kWh) is not a missed saving (#769's "smaller
  observation": a load needing hours cannot use one cheap period stranded
  among dear ones).
- A binary variable (#238) is exactly [0, 1] and refuses another bound;
  `is_mip` says whether any exists. A MIP's duals are recovered from the
  pinned re-solve, so a binding row carries its real price and a slack row
  zero, not the zeros a raw branch-and-bound returns.
- Smoothness (`smoothness_weight`, mechanism 4; household 20 Aug 2026,
  "charge in bursts not continuously"; #725 for adequacy loads): on flat
  prices it flattens a degenerate shape at no extra cost, leaves a
  semi-continuous load hard 0-or-max, and never smears a real price step.
- With export paying more than import by more than the round trip loses
  (the case the linear caps alone cannot close, #245, #266; found via
  #236 and #264), the plan
  still never charges and discharges, or imports and exports, in one
  period: the exclusivity binaries are enforced (#238, #1535), and the
  combined caps hold.
- The proximal anchor is a switching cost, not an energy cost (#1406): it
  costs the same per kW of deviation in every period, however short, so it
  is not 30 times weaker on the 1-minute period that becomes the inverter
  command. Smoothness keeps its hours factor on purpose: its gain scales
  with hours too, so the two cancel.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import highspy
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import lp
from solver.elements import (
    AdequacyLoadConfig,
    AdequacyWindow,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.lp import CalibratedOptions, LexOptions, LPProblem
from solver.network import (
    BatteryPlan,
    Plan,
    _add_intraplan_smoothness_penalty,
    _add_proximal_penalty,
    _anchor_source,
    _infeasible_plan,
    build_plan,
)
from test_lp_properties import _build, problems
from test_plan_invariants import SETTINGS, _num, sites
from test_plan_invariants import _solve as _solve_site

TINY = st.sampled_from([1e-9, -1e-9, 5e-10, -1e-10, 1e-11, 1e-12])


@SETTINGS
@given(problems(), st.data())
def test_a_tiny_row_entry_is_dropped_and_changes_nothing(spec, data):
    """The same problem with a tiny entry added to one row, for a variable
    the row does not hold, solves to the same objective in every mode."""
    plain = _build(spec)
    i = data.draw(st.integers(0, len(spec["rows"]) - 1))
    tiny = data.draw(TINY)
    p = LPProblem()
    for j in range(spec["n"]):
        p.add_variable(f"x{j}", lb=0.0, ub=spec["ub"][j], cost=spec["cost"][j])
        if spec["second"][j] is not None:
            p.set_secondary_cost(f"x{j}", spec["second"][j])
    p.add_variable("extra", lb=0.0, ub=1.0, cost=0.0)
    for k, (a, b, _named) in enumerate(spec["rows"]):
        terms = {f"x{j}": a[j] for j in range(spec["n"]) if a[j] != 0.0} or {"x0": 1.0}
        if k == i:
            terms["extra"] = tiny
        p.add_ub_constraint(terms, b)
    for options in (None, LexOptions(), CalibratedOptions()):
        want = plain.solve(options=options)
        got = p.solve(options=options)
        assert want.status == got.status == "optimal", options
        assert abs(plain.primary_cost_of(want) - p.primary_cost_of(got)) <= 1e-6 * max(
            1.0, abs(plain.primary_cost_of(want))
        ), options


@given(TINY)
def test_a_row_whose_every_entry_is_dropped_keeps_its_bounds(tiny):
    """tiny * x == 1 becomes 0 == 1 once the entry is dropped: infeasible, as
    HiGHS would build it, never silently satisfied."""
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=1.0, cost=1.0)
    p.add_eq_constraint({"x": tiny}, 1.0)
    assert p.solve().status != "optimal"


@given(st.integers(min_value=1, max_value=48))
def test_a_plan_that_did_not_solve_shares_no_array(n):
    periods = PeriodGrid(hours=np.ones(n), start=datetime(2026, 1, 1, tzinfo=UTC))
    plan = _infeasible_plan(periods, "infeasible", iterations=0)
    arrays = {
        name: value
        for name, value in vars(plan).items()
        if isinstance(value, np.ndarray)
    }
    assert len(arrays) >= 8  # the check is not vacuous
    ids = [id(a) for a in arrays.values()]
    assert len(ids) == len(set(ids)), "two plan fields are the same array"
    first = next(iter(arrays))
    arrays[first][...] = 5.0
    for name, a in arrays.items():
        if name != first:
            assert not np.any(a == 5.0), name


@SETTINGS
@given(problems(), st.data())
def test_a_tiny_cost_beside_real_ones_still_takes_the_phased_solve(spec, data):
    """The lexicographic bound rows are built from the cost vectors, so a
    float-noise cost lands in a row too: the phased solve still runs (no
    fallback to the plain solve) and holds the primary."""
    j = data.draw(st.integers(0, spec["n"] - 1))
    spec = {**spec, "cost": [*spec["cost"]]}
    spec["cost"][j] = data.draw(TINY)
    p = _build(spec)
    plain = p.solve()
    for options in (LexOptions(), CalibratedOptions()):
        result = p.solve(options=options)
        assert result.status == "optimal", options
        if isinstance(options, CalibratedOptions):
            assert result.calibration_weight_used is not None
        assert p.primary_cost_of(result) <= plain.objective + 1e-6 * max(
            1.0, abs(plain.objective)
        )


@SETTINGS
@given(
    st.integers(min_value=1, max_value=6),
    _num(1.0, 15.0),
    _num(0.05, 1.0),
    _num(0.0, 120.0),
)
def test_a_committed_block_is_an_exact_ceiling(length, rate, export_price, soc):
    """Inside the block export never exceeds the rate, however well export
    pays; with energy enough for the whole block, it is exactly the rate."""
    n = length + 2
    fixed = np.full(n, np.nan)
    fixed[1 : 1 + length] = rate
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, 0.2),
            export_price=np.full(n, export_price),
            import_limit_kw=42.0,
            export_limit_kw=40.0,
            fixed_export_kw=fixed,
        ),
        batteries=[
            BatteryConfig(
                name="b",
                capacity_kwh=200.0,
                initial_soc_kwh=max(soc, 2.4),
                min_soc_kwh=2.4,
                max_soc_kwh=200.0,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=0.93,
                discharge_efficiency=0.93,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.0,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(n))],
    )
    assert plan.status == "optimal"
    block = plan.grid_export_kw[1 : 1 + length]
    assert np.all(block <= rate + 1e-6)
    # Fundable from the battery alone, before any charging: deliver it all.
    if (max(soc, 2.4) - 2.4) * 0.93 >= rate * length + 1e-6:
        np.testing.assert_allclose(block, rate, atol=1e-6)


class _Highs:
    def __init__(self, raises=None):
        self.written: list[str] = []
        self._raises = raises

    def writeModel(self, path):
        if self._raises is not None:
            raise self._raises
        self.written.append(path)


@given(st.sampled_from(["phase2_pin_resolve", "phase1_primary", "x y/z"]))
def test_a_failed_dump_never_blocks_the_next_one(label):
    lp._LP_DUMPED_PHASES.clear()
    try:
        assert lp._dump_failing_model(_Highs(OSError("read-only")), label) is None
        working = _Highs()
        path = lp._dump_failing_model(working, label)
        assert path is not None and working.written == [path]
        # Once written, the same phase is not written again.
        again = _Highs()
        assert lp._dump_failing_model(again, label) is None
        assert again.written == []
    finally:
        lp._LP_DUMPED_PHASES.clear()


def _battery(**over):
    return BatteryConfig(
        **{
            "name": "home",
            "capacity_kwh": 120.0,
            "initial_soc_kwh": 60.0,
            "min_soc_kwh": 2.4,
            "max_soc_kwh": 120.0,
            "max_charge_kw": 40.0,
            "max_discharge_kw": 40.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "charge_cost": 0.005,
            "discharge_cost": 0.01,
            "salvage_value": 0.1,
            **over,
        }
    )


@SETTINGS
@given(
    _num(0.05, 1.0),
    _num(0.0, 10.0),
    _num(0.0, 8.0),
    _num(0.0, 1.0),
    _num(0.0, 15.0),
)
def test_the_risk_dial_leans_solar_and_never_load(risk, band, solar, low, block):
    """With the dial on, a house-load band changes nothing, and solar counts
    as forecast - risk x (forecast - lower)."""
    n = 6
    fixed = np.full(n, block) if block > 0.5 else None

    def plan_with(load_band):
        return build_plan(
            periods=PeriodGrid(np.full(n, 0.5)),
            grid=GridConfig(
                import_price=np.full(n, 0.30),
                export_price=np.full(n, 0.20),
                import_limit_kw=42.0,
                export_limit_kw=40.0,
                fixed_export_kw=fixed,
            ),
            batteries=[_battery(initial_soc_kwh=110.0)],
            solar=SolarConfig(
                forecast_kw=np.full(n, solar),
                lower_kw=np.full(n, solar * low),
                upper_kw=np.full(n, solar + 1.0),
            ),
            loads=[
                LoadConfig(
                    name="house",
                    forecast_kw=np.full(n, 1.2),
                    lower_kw=np.zeros(n) if load_band else None,
                    upper_kw=np.full(n, 1.2 + band) if load_band else None,
                )
            ],
            risk_aversion=risk,
        )

    banded, bare = plan_with(True), plan_with(False)
    assert banded.status == bare.status == "optimal"
    for field in ("battery_charge_kw", "battery_discharge_kw", "grid_import_kw"):
        np.testing.assert_allclose(
            getattr(banded, field), getattr(bare, field), atol=1e-6, err_msg=field
        )
    np.testing.assert_allclose(
        banded.effective_solar_kw, solar - risk * (solar - solar * low), atol=1e-9
    )


@SETTINGS
@given(_num(25.0, 90.0), _num(0.1, 0.3), _num(-0.05, 0.0))
def test_the_same_scenario_costs_the_same_on_any_period_length(soc, rate, export_gap):
    """12 hours on a 1 h, 15 min or 5 min grid, flat prices, a terminal
    value at the last period: the same cost and the same end SoC (#338).
    Export at or below the terminal rate, and that at or below import, so
    nothing is bought to sell and the only question is what to hold."""
    results = []
    for width in (1.0, 0.25, 1.0 / 12.0):
        n = round(12.0 / width)
        plan = build_plan(
            periods=PeriodGrid(np.full(n, width)),
            grid=GridConfig(
                import_price=np.full(n, 0.30),
                export_price=np.full(n, rate + export_gap),
                import_limit_kw=44.0,
                export_limit_kw=44.0,
            ),
            batteries=[
                _battery(
                    capacity_kwh=100.0,
                    initial_soc_kwh=soc,
                    min_soc_kwh=20.0,
                    max_soc_kwh=90.0,
                    charge_efficiency=0.99,
                    discharge_efficiency=0.99,
                    discharge_cost=0.005,
                    salvage_value=0.0,
                    terminal_value_breakpoints=[(70.0, rate)],
                    terminal_value_period_indices=[n - 1],
                )
            ],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
        )
        assert plan.status == "optimal", width
        results.append((plan.total_cost, plan.battery_soc_kwh[-1]))
    costs = [c for c, _ in results]
    ends = [e for _, e in results]
    assert max(costs) - min(costs) < 1e-3, results
    assert max(ends) - min(ends) < 1e-3, results


def _arbitrage(price: float, earn: float, demand: float, solar: float):
    p = LPProblem()
    p.add_variable("imp", ub=100.0, cost=price)
    p.add_variable("exp", ub=100.0, cost=-earn)
    p.add_variable("solar", ub=solar)
    p.add_eq_constraint({"imp": 1.0, "exp": -1.0, "solar": 1.0}, demand)
    return p


@SETTINGS
@given(
    _num(0.0, 0.3),
    _num(1.0, 5.0),
    _num(0.0, 1.0),
    _num(0.0, 2.0),
    st.lists(_num(-1.0, 2.0), min_size=1, max_size=8),
)
def test_the_cost_sweep_re_solves_each_cost_and_puts_it_back(
    earn, demand, solar_share, price, costs
):
    """Free solar is always used; import is 100 kW (to export) while it
    costs less than export earns, else exactly the shortfall."""
    costs = [c for c in costs if abs(c - earn) > 1e-3]
    if abs(price - earn) <= 1e-3 or not costs:
        return
    solar = demand * solar_share

    def imports_at(c):
        return 100.0 if c < earn else demand - solar

    p = _arbitrage(price, earn, demand, solar)
    result = p.solve(keep_basis=True)
    assert result.status == "optimal"
    base_exp = p.value_of(result, "exp")
    # End the sweep on the other side of `earn` from the solve-time price, so
    # a coefficient left behind would change the next solve.
    costs = [*costs, -1.0 if price >= earn else 2.0]
    swept = result.sweep_cost("imp", costs)
    np.testing.assert_allclose(swept, [imports_at(c) for c in costs], atol=1e-6)
    # Put back: an export sweep straight after sees the solve-time import
    # cost, and so does a sweep at that cost.
    assert result.sweep_cost("exp", [-earn]) == pytest.approx([base_exp])
    assert result.sweep_cost("imp", [price]) == pytest.approx([imports_at(price)])


def test_the_cost_sweep_refuses_without_a_basis_or_a_known_variable():
    p = _arbitrage(0.3, 0.05, 2.0, 6.0)
    with pytest.raises(ValueError, match="keep_basis"):
        p.solve().sweep_cost("imp", [0.3])
    with pytest.raises(KeyError):
        p.solve(keep_basis=True).sweep_cost("nope", [0.3])


@given(st.floats(min_value=0.0, max_value=1000.0))
def test_the_lex_tie_slack_is_the_solver_tolerance_never_a_price(objective):
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    _status, primal_tol = h.getOptionValue("primal_feasibility_tolerance")
    slack = max(
        lp._LEX_PRIMARY_TIE_ABS_SLACK, objective * lp._LEX_PRIMARY_TIE_REL_SLACK
    )
    assert float(primal_tol) <= slack < 1e-5
    # At least ten times tighter than the secondary side's epsilon.
    assert slack <= max(1e-6, objective * 1e-6) / 10


_UNFINISHED = [
    "kTimeLimit",
    "kIterationLimit",
    "kSolutionLimit",
    "kUnknown",
    "kModelError",
    "kSolveError",
]


def _bounded_lp() -> LPProblem:
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=10.0, cost=-1.0)
    return p


@given(st.sampled_from(_UNFINISHED))
def test_an_unfinished_solve_is_an_error_never_infeasible(name):
    status = getattr(highspy.HighsModelStatus, name)
    with patch.object(highspy.Highs, "getModelStatus", return_value=status):
        result = _bounded_lp().solve()
    assert result.status == "error"
    assert result.raw_status == highspy.Highs().modelStatusToString(status)


def test_a_proved_outcome_keeps_its_own_name():
    infeasible = LPProblem()
    infeasible.add_variable("x", lb=5.0, ub=10.0)
    infeasible.add_ub_constraint({"x": 1.0}, 1.0)
    unbounded = LPProblem()
    unbounded.add_variable("x", lb=0.0, ub=float("inf"), cost=-1.0)
    for problem, want in (
        (infeasible, "infeasible"),
        (unbounded, "unbounded"),
        (_bounded_lp(), "optimal"),
    ):
        result = problem.solve()
        assert (result.status, result.raw_status) == (want, None)


def test_every_solve_carries_the_time_limit():
    seen: list[tuple[str, object]] = []
    real = highspy.Highs.setOptionValue

    def spy(self, key, value):
        seen.append((key, value))
        return real(self, key, value)

    with patch.object(highspy.Highs, "setOptionValue", spy):
        _bounded_lp().solve()
    assert ("time_limit", lp.DEFAULT_TIME_LIMIT_SECONDS) in seen


@SETTINGS
@given(
    _num(0.0, 0.3),
    _num(1.0, 5.0),
    _num(0.0, 1.0),
    _num(0.0, 2.0),
    st.lists(_num(-1.0, 2.0), min_size=1, max_size=8),
)
def test_the_ranging_sweep_reports_each_steps_exact_interval(
    earn, demand, solar_share, price, costs
):
    """Below `earn` import sits at 100 kW for every cost down to minus
    infinity and up to `earn`; above it, at the shortfall from `earn` up to
    plus infinity. The values are the plain sweep's, and the coefficient is
    put back afterwards."""
    costs = [c for c in costs if abs(c - earn) > 1e-3]
    if abs(price - earn) <= 1e-3 or not costs:
        return
    solar = demand * solar_share
    p = _arbitrage(price, earn, demand, solar)
    result = p.solve(keep_basis=True)
    base_exp = p.value_of(result, "exp")
    costs = [*costs, -1.0 if price >= earn else 2.0]
    steps = result.sweep_cost_with_ranging("imp", costs)
    assert result.sweep_cost("exp", [-earn]) == pytest.approx([base_exp])
    plain = p.solve(keep_basis=True).sweep_cost("imp", costs)
    for cost, step, value in zip(costs, steps, plain, strict=True):
        assert step.value == pytest.approx(value, abs=1e-6)
        if cost < earn:
            assert step.value == pytest.approx(100.0)
            assert step.cost_dn.value == -np.inf
            assert step.cost_up.value == pytest.approx(earn, abs=1e-6)
        else:
            assert step.value == pytest.approx(demand - solar, abs=1e-6)
            assert step.cost_dn.value == pytest.approx(earn, abs=1e-6)
            assert step.cost_up.value == np.inf
            # Nothing bounds the interval above. Below `earn`, with import
            # basic at a real shortfall, export enters the basis and import
            # leaves it for its 100 kW bound.
            assert (step.cost_up.in_var, step.cost_up.out_var) == (None, None)
            if demand - solar > 1e-6:
                assert (step.cost_dn.in_var, step.cost_dn.out_var) == ("exp", "imp")


def test_the_ranging_sweep_refuses_without_a_basis_or_a_known_variable():
    p = _arbitrage(0.3, 0.05, 2.0, 6.0)
    with pytest.raises(ValueError, match="keep_basis"):
        p.solve().sweep_cost_with_ranging("imp", [0.3])
    with pytest.raises(KeyError):
        p.solve(keep_basis=True).sweep_cost_with_ranging("nope", [0.3])


def _previous(dispatched: float, planned: float, start: datetime) -> Plan:
    """A tiered plan solved at `start`: a 1-minute period 0, then 5 minutes."""
    discharge = np.array([dispatched, planned])
    return Plan(
        status="optimal",
        periods=PeriodGrid(hours=np.array([1 / 60, 5 / 60]), start=start),
        battery_charge_kw=np.zeros(2),
        battery_discharge_kw=discharge,
        battery_soc_kwh=np.array([29.8, 28.3]),
        grid_import_kw=np.zeros(2),
        grid_export_kw=discharge - 2.0,
        export_bonus_kw=np.zeros(2),
        solar_used_kw=np.zeros(2),
        solar_curtailed_kw=np.zeros(2),
        sheddable_loads=[],
        adequacy_loads=[],
        total_cost=None,
        soc_penalty_cost=0.0,
        iterations=0,
        batteries=[
            BatteryPlan(
                name="home",
                charge_kw=np.zeros(2),
                discharge_kw=discharge,
                soc_kwh=np.array([29.8, 28.3]),
            )
        ],
    )


@SETTINGS
@given(_num(3.0, 20.0), _num(3.0, 20.0), st.sampled_from([0, 30, 60]))
def test_period_0_holds_what_was_dispatched(dispatched, planned, seconds):
    """Economics near-indifferent (export margin 0.1c over wear), so the
    anchor decides. A solve inside old period 0 or 1 holds the dispatched
    rate in period 0; periods inside old period 1 follow its plan."""
    if abs(dispatched - planned) < 2.0:
        return
    t0 = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
    n = 5
    plan = build_plan(
        periods=PeriodGrid(
            hours=np.array([1 / 60] * 4 + [5 / 60]),
            start=datetime(2026, 10, 2, 10, 0, seconds, tzinfo=UTC)
            if seconds < 60
            else datetime(2026, 10, 2, 10, 1, tzinfo=UTC),
        ),
        grid=GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.11),
            import_limit_kw=30.0,
            export_limit_kw=25.0,
        ),
        batteries=[
            _battery(
                capacity_kwh=40.0,
                initial_soc_kwh=30.0,
                min_soc_kwh=4.0,
                max_soc_kwh=40.0,
                max_charge_kw=25.0,
                max_discharge_kw=25.0,
                charge_efficiency=0.98,
                discharge_efficiency=0.98,
                charge_cost=0.01,
                discharge_cost=0.099,
                salvage_value=0.0,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
        previous_plan=_previous(dispatched, planned, t0),
        proximal_weight=0.005,
    )
    assert plan.status == "optimal"
    d = plan.batteries[0].discharge_kw
    assert abs(d[0] - dispatched) < 1e-3
    if seconds == 60:
        # 10:02-10:04 lie inside old period 1 (10:01-10:06).
        np.testing.assert_allclose(d[1:4], planned, atol=1e-3)


@given(st.integers(0, 40), st.integers(0, 40))
def test_only_a_recent_period_0_anchors_to_the_dispatched_value(new, old):
    """Period 0 anchors to old period 0 while it starts inside old period 0
    or 1; otherwise every period anchors to the old period containing it."""
    want = 0 if new == 0 and old <= 1 else old
    assert _anchor_source(new, old) == want


class _PinHighs:
    def __init__(self, values):
        self.values = values
        self.bounds: dict[int, tuple[float, float]] = {}
        self.relaxed: list[int] = []

    def val(self, i):
        return self.values[i]

    def changeColIntegrality(self, i, kind):
        assert kind == highspy.HighsVarType.kContinuous
        self.relaxed.append(i)

    def changeColBounds(self, i, lo, hi):
        self.bounds[i] = (lo, hi)


class _Warnings(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@given(
    st.lists(
        st.one_of(
            st.sampled_from([0.0, 1.0, 1e-9, 1.0 - 1e-9, 0.5]),
            st.floats(0.0, 1.0),
            st.floats(-5.0, 10.0),
        ),
        min_size=1,
        max_size=8,
    ),
    st.data(),
)
def test_pinning_fixes_each_binary_at_its_rounded_value(values, data):
    n = len(values)
    binary = sorted(data.draw(st.sets(st.integers(0, n - 1))))
    h = _PinHighs(values)
    handler = _Warnings()
    lp._LOGGER.addHandler(handler)
    try:
        lp._pin_binaries_to_current_solution(h, list(range(n)), binary)
    finally:
        lp._LOGGER.removeHandler(handler)
    assert h.relaxed == binary
    assert h.bounds == {
        i: (float(round(values[i])), float(round(values[i]))) for i in binary
    }
    worst = max((abs(values[i] - round(values[i])) for i in binary), default=0.0)
    warned = [m for m in handler.messages if "away from integral" in m]
    if worst > lp._BINARY_INTEGRALITY_TOLERANCE:
        assert len(warned) == 1
        assert f"pin {len(binary)} binaries" in warned[0]
        assert f"{worst:.3e}" in warned[0]
    else:
        assert warned == []


class _Options:
    def __init__(self, lp_tol, mip_tol, fail_set_after=None, fail_get=False):
        self.opts = {
            "primal_feasibility_tolerance": lp_tol,
            "mip_feasibility_tolerance": mip_tol,
        }
        self.sets = 0
        self.fail_set_after = fail_set_after
        self.fail_get = fail_get

    def getOptionValue(self, name):
        if self.fail_get:
            raise RuntimeError("no options")
        return (0, self.opts[name])

    def setOptionValue(self, name, value):
        self.sets += 1
        if self.fail_set_after is not None and self.sets > self.fail_set_after:
            raise RuntimeError("no options")
        self.opts[name] = value


def test_highs_accepts_a_mip_point_looser_than_an_lp_one():
    """The premise, read from the installed solver: if these ever agree,
    the widening is no longer needed."""
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    _s, lp_tol = h.getOptionValue("primal_feasibility_tolerance")
    _s, mip_tol = h.getOptionValue("mip_feasibility_tolerance")
    assert mip_tol > lp_tol


@given(st.floats(1e-10, 1e-3), st.floats(1e-10, 1e-3), st.booleans())
def test_the_pin_resolve_widens_the_lp_tolerance_for_its_block_only(
    lp_tol, mip_tol, raise_inside
):
    h = _Options(lp_tol, mip_tol)
    try:
        with lp._lp_tolerance_matching_mip(h):
            assert h.opts["primal_feasibility_tolerance"] == max(lp_tol, mip_tol)
            if raise_inside:
                raise RuntimeError("phase failed")
    except RuntimeError:
        assert raise_inside
    assert h.opts["primal_feasibility_tolerance"] == lp_tol


@given(st.sampled_from(["get", "restore"]))
def test_a_solver_refusing_option_calls_never_breaks_the_solve(fails):
    h = (
        _Options(1e-7, 1e-6, fail_get=True)
        if fails == "get"
        else _Options(1e-7, 1e-6, fail_set_after=1)
    )
    ran = []
    with lp._lp_tolerance_matching_mip(h):
        ran.append(True)
    assert ran == [True]


@SETTINGS
@given(st.integers(3, 8), _num(5.0, 13.0), _num(0.1, 0.9), _num(0.05, 0.5))
def test_a_flat_export_bonus_is_claimed_in_one_block_at_the_end(
    n, rate, share, premium
):
    cap = round(rate * n * share, 4)
    plan = build_plan(
        periods=PeriodGrid(
            hours=np.ones(n), start=datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.full(n, 0.15),
            export_price=np.full(n, 0.09),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
            export_bonus_price=np.full(n, premium),
            export_bonus_volume_kwh=cap,
        ),
        batteries=[
            _battery(
                capacity_kwh=200.0,
                initial_soc_kwh=200.0,
                min_soc_kwh=10.0,
                max_soc_kwh=200.0,
                max_charge_kw=rate,
                max_discharge_kw=rate,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.02,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
    )
    assert plan.status == "optimal"
    np.testing.assert_allclose(plan.grid_export_kw, rate, atol=1e-4)
    bonus = plan.export_bonus_kw
    assert abs(float(np.sum(bonus)) - cap) < 1e-4
    on = bonus > 1e-4
    first = int(np.argmax(on))
    assert on[first:].all(), bonus  # one block, running to the end
    # Every claimed period but the block's first claims the full rate.
    np.testing.assert_allclose(bonus[first + 1 :], rate, atol=1e-4)


@SETTINGS
@given(
    st.integers(2, 8),
    st.sampled_from([0.25, 0.5, 1.0]),
    st.lists(_num(0.02, 0.6), min_size=8, max_size=8),
    _num(0.5, 3.0),
    st.booleans(),
)
def test_an_adequacy_load_costs_its_energy_at_the_marginal_price(
    n, width, prices, power, surplus
):
    price = np.array(prices[:n])
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=price,
            export_price=np.zeros(n),
            import_limit_kw=power + 5.0,
            export_limit_kw=5.0,
        ),
        batteries=[_battery(available=False)],
        solar=SolarConfig(forecast_kw=np.full(n, power + 10.0 if surplus else 0.0)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="pool",
                max_power_kw=power,
                target_kwh=power * width,
                deadline_period=n - 1,
                shortfall_price=20.0,
            )
        ],
    )
    assert plan.status == "optimal"
    (al,) = plan.adequacy_loads
    assert al.profit_horizon is None
    if surplus:
        assert abs(al.marginal_cost) < 1e-6
    else:
        expected = float(np.sum(price * al.power_kw * width))
        assert abs(al.marginal_cost - expected) < 1e-6


@SETTINGS
@given(sites(), st.data())
def test_narrowing_an_adequacy_window_never_makes_the_plan_cheaper(site, data):
    adequacy = site["adequacy"]
    if adequacy is None:
        return
    wide = _solve_site(site)
    pulled = data.draw(
        st.integers(adequacy["earliest_period"], adequacy["deadline_period"])
    )
    narrow = _solve_site({**site, "adequacy": {**adequacy, "deadline_period": pulled}})
    assert wide.status == narrow.status == "optimal"
    # HiGHS stops a MIP within its relative gap (1e-4), so allow that much.
    tol = 1e-4 * max(1.0, abs(wide.total_cost)) + 1e-6
    assert narrow.total_cost >= wide.total_cost - tol


@given(st.sampled_from([None, 0.5, 2.0]), st.sampled_from(["lb", "ub"]))
def test_a_binary_is_zero_to_one_and_refuses_another_bound(value, which):
    p = LPProblem()
    assert not p.is_mip
    p.add_variable("x", lb=0.0, ub=10.0)
    assert not p.is_mip
    if value is None:
        p.add_variable("y", binary=True)
        i = p._var_index["y"]
        assert (p._lb[i], p._ub[i], p._binary[i]) == (0.0, 1.0, True)
        assert not p._binary[p._var_index["x"]]
        assert p.is_mip
    else:
        with pytest.raises(ValueError):
            p.add_variable("y", binary=True, **{which: value})


@SETTINGS
@given(_num(1.0, 50.0), _num(0.1, 3.0), _num(0.0, 40.0), _num(1.0, 100.0))
def test_a_mips_duals_are_recovered_from_the_pinned_resolve(cap, value, fixed, room):
    """A switch with a fixed cost gates output up to `cap`; a second row
    caps output at `cap + room`, never binding. On when the output is
    worth more than the switch costs."""
    if abs(value * cap - fixed) < 1e-3:
        return
    p = LPProblem()
    p.add_variable("on", binary=True, cost=fixed)
    p.add_variable("out", lb=0.0, ub=1000.0, cost=-value)
    p.add_ub_constraint({"out": 1.0, "on": -cap}, 0.0, name="gate")
    p.add_ub_constraint({"out": 1.0}, cap + room, name="slack")
    result = p.solve()
    assert result.status == "optimal"
    on = value * cap > fixed
    assert p.value_of(result, "on") == pytest.approx(1.0 if on else 0.0)
    assert p.value_of(result, "out") == pytest.approx(cap if on else 0.0)
    assert result.objective == pytest.approx(min(fixed - value * cap, 0.0))
    assert result.duals["slack"] == pytest.approx(0.0, abs=1e-9)
    assert {"on", "out"} <= set(result.reduced_costs)
    if on:
        # One more unit through the gate is worth `value`.
        assert abs(result.duals["gate"]) == pytest.approx(value, rel=1e-6)


def _jag(*series):
    return float(sum(np.sum(np.abs(np.diff(x))) for x in series))


@SETTINGS
@given(st.integers(6, 14), _num(0.3, 2.0), st.integers(1, 4), st.booleans())
def test_smoothness_flattens_a_degenerate_adequacy_load_for_free(
    n, power, periods_needed, semi
):
    width = 5.0 / 60.0

    def plan_with(weight):
        return build_plan(
            periods=PeriodGrid(np.full(n, width)),
            grid=GridConfig(
                import_price=np.full(n, 0.0202),
                export_price=np.full(n, -0.0053),
                import_limit_kw=44.0,
                export_limit_kw=44.0,
            ),
            batteries=[_battery(available=False)],
            solar=SolarConfig(forecast_kw=np.zeros(n)),
            loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
            adequacy_loads=[
                AdequacyLoadConfig(
                    name="hws",
                    max_power_kw=power,
                    target_kwh=power * width * periods_needed,
                    deadline_period=n - 1,
                    shortfall_price=5.0,
                )
            ],
            adequacy_semi_continuous=semi,
            adequacy_earliness_budget_kw=0.0,
            smoothness_weight=weight,
        )

    off, on = plan_with(0.0), plan_with(0.005)
    assert off.status == on.status == "optimal"
    p = on.adequacy_loads[0].power_kw
    if semi:
        assert np.all((p < 1e-6) | (np.abs(p - power) < 1e-6))
    else:
        assert _jag(p) < 0.05 * power
        assert abs(on.total_cost - off.total_cost) < 1e-2


@SETTINGS
@given(_num(0.3, 0.8), _num(10.0, 60.0))
def test_smoothness_never_smears_a_real_price_step(high, soc):
    """A sharp import/export step at the midpoint: the battery's swing at
    the step is the same with smoothing on as off (within 1 kW)."""
    n = 12
    imp = np.concatenate([np.full(6, 0.02), np.full(6, high)])
    exp_ = np.concatenate([np.full(6, -0.005), np.full(6, high - 0.05)])

    def net(weight):
        plan = build_plan(
            periods=PeriodGrid(np.full(n, 5.0 / 60.0)),
            grid=GridConfig(
                import_price=imp,
                export_price=exp_,
                import_limit_kw=44.0,
                export_limit_kw=44.0,
            ),
            batteries=[
                _battery(
                    capacity_kwh=122.2,
                    initial_soc_kwh=soc + 30.0,
                    min_soc_kwh=2.44,
                    max_soc_kwh=122.2,
                    charge_efficiency=0.926,
                    discharge_efficiency=0.926,
                    salvage_value=0.15,
                )
            ],
            solar=SolarConfig(forecast_kw=np.full(n, 3.0)),
            loads=[LoadConfig(name="load", forecast_kw=np.full(n, 2.0))],
            smoothness_weight=weight,
            battery_charge_earliness_budget_kw=0.0,
        )
        assert plan.status == "optimal"
        return plan.battery_discharge_kw - plan.battery_charge_kw

    off, on = net(0.0), net(0.005)
    step_off, step_on = abs(off[6] - off[5]), abs(on[6] - on[5])
    assert abs(step_on - step_off) <= 1.0, (step_off, step_on)


@SETTINGS
@given(_num(2.0, 20.0), _num(0.0, 3.0))
def test_smoothness_flattens_a_degenerate_battery_charge_for_free(headroom, net_load):
    """Flat prices, import far below salvage, a battery `headroom` kWh short
    of full: charging it is worth it, but any shape over the hour is equally
    good. Smoothing picks a flat one, at the same cost."""
    n = 12

    def plan_with(weight):
        return build_plan(
            periods=PeriodGrid(np.full(n, 5.0 / 60.0)),
            grid=GridConfig(
                import_price=np.full(n, 0.0202),
                export_price=np.full(n, -0.0053),
                import_limit_kw=44.0,
                export_limit_kw=44.0,
            ),
            batteries=[
                _battery(
                    capacity_kwh=122.2,
                    initial_soc_kwh=122.2 - headroom,
                    min_soc_kwh=2.44,
                    max_soc_kwh=122.2,
                    charge_efficiency=0.926,
                    discharge_efficiency=0.926,
                    salvage_value=0.15,
                )
            ],
            solar=SolarConfig(forecast_kw=np.full(n, 7.0)),
            loads=[LoadConfig(name="load", forecast_kw=np.full(n, 7.0 + net_load))],
            smoothness_weight=weight,
            battery_charge_earliness_budget_kw=0.0,
        )

    off, on = plan_with(0.0), plan_with(0.005)
    assert off.status == on.status == "optimal"
    assert _jag(on.battery_discharge_kw - on.battery_charge_kw) < 1.0
    assert abs(on.total_cost - off.total_cost) < 1e-2


@given(
    st.lists(st.text(min_size=1, max_size=300), min_size=1, max_size=4, unique=True),
    st.sampled_from(["773", "1179"]),
)
def test_each_phase_is_captured_once_safely_in_the_configured_folder(labels, issue):
    import os
    import tempfile

    target = tempfile.mkdtemp()
    old = os.environ.get(lp._LP_DUMP_DIR_ENV)
    os.environ[lp._LP_DUMP_DIR_ENV] = target
    lp._LP_DUMPED_PHASES.clear()
    try:
        written: list[str] = []
        for label in labels:
            h = _Highs()
            path = lp._dump_failing_model(h, label, issue=issue)
            assert path is not None
            written.append(path)
            assert h.written == [path]
            base = os.path.basename(path)
            assert os.path.dirname(path) == target
            assert os.sep not in base and "/" not in base
            assert base.startswith(f"nimbus_{issue}_") and base.endswith(".mps")
            assert len(base) < 120
            # Once per phase.
            assert lp._dump_failing_model(_Highs(), label, issue=issue) is None
        assert len(written) == len(labels)
    finally:
        lp._LP_DUMPED_PHASES.clear()
        if old is None:
            os.environ.pop(lp._LP_DUMP_DIR_ENV, None)
        else:
            os.environ[lp._LP_DUMP_DIR_ENV] = old


@given(st.sampled_from([OSError("ro"), RuntimeError("x"), ValueError("y"), None]))
def test_nothing_the_dump_throws_reaches_the_solve(error):
    lp._LP_DUMPED_PHASES.clear()
    try:
        h = MagicMock(spec=[]) if error is None else _Highs(error)
        assert lp._dump_failing_model(h, "phase") is None
    finally:
        lp._LP_DUMPED_PHASES.clear()


def test_only_an_alarming_call_is_captured():
    """Captured at the alarming threshold, below the per-call limit (so a
    call that hits the limit is captured) and above merely slow."""
    assert (
        lp._SLOW_LP_CALL_SECONDS
        < lp._ALARMING_LP_CALL_SECONDS
        < lp.DEFAULT_TIME_LIMIT_SECONDS
    )


@SETTINGS
@given(
    st.integers(2, 12),
    st.sampled_from([5 / 60, 0.25, 1.0]),
    st.lists(_num(0.05, 0.6), min_size=12, max_size=12),
    _num(0.05, 0.5),
    _num(1.0, 20.0),
    _num(0.0, 6.0),
)
def test_a_profitable_spread_still_moves_one_way_per_period(
    n, width, prices, premium, power, load
):
    imp = np.array(prices[:n])
    limit = 30.0
    plan = build_plan(
        periods=PeriodGrid(np.full(n, width)),
        grid=GridConfig(
            import_price=imp,
            export_price=imp + premium,
            import_limit_kw=limit,
            export_limit_kw=limit,
        ),
        batteries=[
            _battery(
                capacity_kwh=40.0,
                initial_soc_kwh=20.0,
                min_soc_kwh=2.0,
                max_soc_kwh=38.0,
                max_charge_kw=power,
                max_discharge_kw=power,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, load))],
    )
    assert plan.status == "optimal"
    # One-way either already, or after the direction-exclusive re-solve.
    assert plan.direction_exclusivity in ("not_needed", "enforced")
    assert plan.direction_two_way_pairs == 0
    ch, dc = plan.battery_charge_kw, plan.battery_discharge_kw
    gi, ge = plan.grid_import_kw, plan.grid_export_kw
    assert (np.minimum(ch, dc) <= 1e-6).all()
    assert (np.minimum(gi, ge) <= 1e-6).all()
    assert (ch + dc <= power + 1e-6).all()
    assert (gi + ge <= limit + 1e-6).all()


@SETTINGS
@given(_num(0.02, 0.6), _num(0.02, 0.6), st.booleans(), _num(0.3, 2.0))
def test_a_value_priced_load_earns_its_value_less_its_cost_in_every_window(
    value, price, two_windows, power
):
    if abs(value - price) < 1e-3:
        return
    n = 24
    target = power * 2  # two hours of the load
    # Windowed only: an unwindowed load's credit is #1963's fix and test.
    windows = (
        AdequacyWindow(earliest_period=2, deadline_period=8, target_kwh=target),
    ) + (
        (AdequacyWindow(earliest_period=14, deadline_period=20, target_kwh=target),)
        if two_windows
        else ()
    )
    plan = build_plan(
        periods=PeriodGrid(np.ones(n)),
        grid=GridConfig(
            import_price=np.full(n, price),
            export_price=np.zeros(n),
            import_limit_kw=power + 5.0,
            export_limit_kw=5.0,
        ),
        batteries=[_battery(available=False)],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=power,
                target_kwh=target,
                earliest_period=2,
                deadline_period=8,
                shortfall_price=0.01,  # shedding is cheaper than running dear
                value_per_kwh=value,
                windows=windows,
            )
        ],
    )
    assert plan.status == "optimal"
    (al,) = plan.adequacy_loads
    energy = float(np.sum(al.power_kw))
    if value < price:
        assert energy <= 1e-6
        assert abs(al.profit_horizon) <= 1e-6
    else:
        # The credit is capped at the target (#606): it runs each window's
        # target, no more.
        assert abs(energy - target * (2 if two_windows else 1)) <= 1e-4
        assert abs(al.profit_horizon - (value - price) * energy) <= 1e-6


_DURATIONS = st.lists(
    st.sampled_from([1 / 60, 5 / 60, 0.25, 0.5, 1.0]), min_size=2, max_size=8
)


@given(_DURATIONS, _num(0.001, 0.2), st.data())
def test_the_proximal_anchor_costs_the_same_in_every_period(hours, weight, data):
    n = len(hours)
    previous = np.array(data.draw(st.lists(_num(-40.0, 40.0), min_size=n, max_size=n)))
    p = LPProblem()
    names = [f"batt_{t}" for t in range(n)]
    for name in names:
        p.add_variable(name, lb=-100.0, ub=100.0)
    _add_proximal_penalty(p, names, "batt", {t: t for t in range(n)}, previous, weight)
    prox = {k: v for k, v in p._cost.items() if k.startswith("prox_")}
    assert len(prox) == 2 * n
    assert set(prox.values()) == {weight}


@given(_DURATIONS, _num(0.001, 0.2))
def test_smoothness_still_costs_per_hour(hours, weight):
    n = len(hours)
    p = LPProblem()
    names = [f"s_{t}" for t in range(n)]
    for name in names:
        p.add_variable(name, lb=-100.0, ub=100.0)
    _add_intraplan_smoothness_penalty(p, names, "s", n, np.array(hours), weight)
    for t in range(1, n):
        assert p._cost[f"smooth_pos_s_{t}"] == pytest.approx(weight * hours[t])


@given(st.sampled_from(["zero_weight", "no_alignment", "no_previous"]))
def test_an_idle_anchor_adds_nothing(case):
    p = LPProblem()
    p.add_variable("batt_0", lb=-100.0, ub=100.0)
    alignment = {} if case == "no_alignment" else {0: 0}
    previous = None if case == "no_previous" else np.array([-40.0])
    weight = 0.0 if case == "zero_weight" else 0.045
    _add_proximal_penalty(p, ["batt_0"], "batt", alignment, previous, weight)
    assert [k for k in p._cost if k.startswith("prox_")] == []
