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
  Each phase gets one capture, named for its issue (#1179), in the configured
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
- With a flat export bonus price (the cap in `p2p_export.py`, #355), the capped bonus volume is claimed in
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
- An adequacy load's `max_kwh_per_day` (#482) caps each real calendar day
  separately, the whole horizon when the plan has no timestamps, and
  nothing when unset; a cap of zero or less is refused.
"""

from __future__ import annotations

import ast
import logging
import pathlib
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import highspy
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import lp, network
from solver import lp as _solver_lp
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.lp import CalibratedOptions, LexOptions, LPProblem
from solver.network import (
    BatteryPlan,
    Plan,
    _infeasible_plan,
    build_plan,
)
from test_lp_properties import _build, problems
from test_plan_invariants import SETTINGS, _num

TINY = st.sampled_from([1e-9, -1e-9, 5e-10, -1e-10, 1e-11, 1e-12])


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


class _Highs:
    def __init__(self, raises=None):
        self.written: list[str] = []
        self._raises = raises

    def writeModel(self, path):
        if self._raises is not None:
            raise self._raises
        self.written.append(path)


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


def _arbitrage(price: float, earn: float, demand: float, solar: float):
    p = LPProblem()
    p.add_variable("imp", ub=100.0, cost=price)
    p.add_variable("exp", ub=100.0, cost=-earn)
    p.add_variable("solar", ub=solar)
    p.add_eq_constraint({"imp": 1.0, "exp": -1.0, "solar": 1.0}, demand)
    return p


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


# --- #1360: an Unknown status gets one interior-point retry --------------------


@pytest.fixture
def _retry_counts(monkeypatch):
    monkeypatch.setattr(lp, "UNKNOWN_RETRIES", {"attempted": 0, "recovered": 0})
    monkeypatch.setattr(lp, "_UNKNOWN_RETRY_NOTED", set())
    monkeypatch.setattr(lp, "_lex_calibration_failed_until", 0.0)


_REAL_STATUS = highspy.Highs.getModelStatus


def _unknown(rescued: bool):
    """HiGHS stops at Unknown; with `rescued`, the interior-point re-run
    reaches the real (optimal) answer."""

    def status(self):
        if rescued and self.getOptionValue("solver")[1] == "ipm":
            return _REAL_STATUS(self)
        return highspy.HighsModelStatus.kUnknown

    return status


def _two_objective_lp() -> LPProblem:
    p = _bounded_lp()
    p.add_variable("y", lb=0.0, ub=5.0, cost=1.0)
    p.set_secondary_cost("y", 1.0)
    return p


@pytest.mark.parametrize("rescued", [True, False])
@pytest.mark.parametrize(
    "options", [None, CalibratedOptions(), LexOptions()], ids=["plain", "cal", "lex"]
)
def test_an_unknown_gets_one_interior_point_retry(options, rescued, _retry_counts):
    """#1360 (a tester's install, 11 Oct 2026: 169 Unknowns in three hours
    with a $23/kWh spike in the horizon): before the previous plan is kept,
    the solve is re-run once with the interior-point solver, on every path a
    calibrated or lex solve can fall back to. Every retry is counted, and a
    rescue as recovered (#1360, 15 Oct: the episode was only found from
    recorder history, so the counts are published)."""
    with patch.object(highspy.Highs, "getModelStatus", _unknown(rescued)):
        problem = _two_objective_lp()
        result = problem.solve(options=options)
    if rescued:
        assert result.status == "optimal"
        assert problem.value_of(result, "x") == pytest.approx(10.0)
        assert lp.UNKNOWN_RETRIES == {"attempted": 1, "recovered": 1}
    else:
        assert (result.status, result.raw_status) == ("error", "Unknown")
        assert lp.UNKNOWN_RETRIES == {"attempted": 1, "recovered": 0}


def test_a_quiet_solve_is_retried_but_never_counted(_retry_counts):
    """#1360 / #1797: the counters are the live solve's; the stochastic
    joint solve (`quiet_diagnostics`) is still rescued but not counted."""
    p = _bounded_lp()
    p.quiet_diagnostics = True
    with patch.object(highspy.Highs, "getModelStatus", _unknown(True)):
        result = p.solve()
    assert result.status == "optimal"
    assert lp.UNKNOWN_RETRIES == {"attempted": 0, "recovered": 0}


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
    # test_the_ranging_sweep_refuses_without_a_basis_or_a_known_variable
    _solver_lp._lex_calibration_failed_until = 0.0
    p = _arbitrage(0.3, 0.05, 2.0, 6.0)
    with pytest.raises(ValueError, match="keep_basis"):
        p.solve().sweep_cost_with_ranging("imp", [0.3])
    with pytest.raises(KeyError):
        p.solve(keep_basis=True).sweep_cost_with_ranging("nope", [0.3])

    # test_highs_accepts_a_mip_point_looser_than_an_lp_one: The premise, read from the installed solver: if these ever agree,
    _solver_lp._lex_calibration_failed_until = 0.0
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    _s, lp_tol = h.getOptionValue("primal_feasibility_tolerance")
    _s, mip_tol = h.getOptionValue("mip_feasibility_tolerance")
    assert mip_tol > lp_tol


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


def _jag(*series):
    return float(sum(np.sum(np.abs(np.diff(x))) for x in series))


@SETTINGS
@given(_num(2.0, 20.0), _num(0.0, 3.0), st.booleans())
def test_smoothness_flattens_a_degenerate_battery_charge_for_free(
    headroom, net_load, calibrated
):
    """Flat prices, import far below salvage, a battery `headroom` kWh short
    of full: charging it is worth it, but any shape over the hour is equally
    good. Smoothing picks a flat one, at the same cost; #696: also through
    the calibrated secondary channel, as in production."""
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
            solve_options=CalibratedOptions() if calibrated and weight else None,
        )

    off, on = plan_with(0.0), plan_with(0.005)
    assert off.status == on.status == "optimal"
    assert _jag(on.battery_discharge_kw - on.battery_charge_kw) < 1.0
    assert abs(on.total_cost - off.total_cost) < 1e-2


# --- #773: one phase-breakdown line per slow solve --------------------------------


class _PhaseHighs:
    """Only the reads `_timed_lp_call` makes."""

    def __init__(self, status: str = "Optimal") -> None:
        self._info = MagicMock(
            simplex_iteration_count=1234, mip_node_count=7, mip_gap=0.0
        )
        self._status = status

    def getInfo(self):
        return self._info

    def getModelStatus(self):
        return object()

    def modelStatusToString(self, _status):
        return self._status


def _clock(durations):
    """Elapsed times for `_timed_lp_call`: one entry and one exit tick per
    call, `durations` apart. Each call starts from 0 so its elapsed time is
    `d` exactly: a running total minus its predecessor is not (1.5566 +
    1.5566 + 5.0 - 3.1132 is just under 5.0, the slow line)."""
    ticks = []
    for d in durations:
        ticks += [0.0, d]
    it = iter(ticks)
    return lambda: next(it, 0.0)


def _breakdowns(monkeypatch, durations, status="Optimal", raise_last=False):
    """The phase-breakdown records one solve of `durations` emits."""
    h = _PhaseHighs(status)
    monkeypatch.setattr(lp.time, "monotonic", _clock(durations))
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append
    level = lp._LOGGER.level
    lp._LOGGER.addHandler(handler)
    lp._LOGGER.setLevel(logging.DEBUG)
    try:
        with lp._phase_breakdown():
            for i, _d in enumerate(durations):
                with lp._timed_lp_call(h, f"lex_phase:p{i}"):
                    if raise_last and i == len(durations) - 1:
                        raise RuntimeError("phase blew up")
    except RuntimeError:
        assert raise_last
    finally:
        lp._LOGGER.removeHandler(handler)
        lp._LOGGER.setLevel(level)
    return [r for r in records if "phase breakdown" in r.getMessage()]


@SETTINGS
@given(
    st.lists(_num(0.01, 59.0), min_size=1, max_size=4),
    st.sampled_from(["Optimal", "Infeasible", "Time limit reached"]),
)
def test_a_breakdown_names_every_phase_once_any_is_slow(durations, status):
    """#773 (16 cycles on a real install: phase 2 took 30-57 s while
    phase 1 never crossed the 5 s line, so only the expensive phase was
    ever logged). One line per solve, only when a phase crossed the slow
    line, naming EVERY phase (the fast one is the control); WARNING when a
    phase is alarming or did not end optimal, else DEBUG."""
    with pytest.MonkeyPatch.context() as mp:
        lines = _breakdowns(mp, durations, status)
    slow = max(durations) >= lp._SLOW_LP_CALL_SECONDS
    if not slow:
        assert lines == []
        return
    assert len(lines) == 1
    msg = lines[0].getMessage()
    for i, d in enumerate(durations):
        assert f"lex_phase:p{i}" in msg
        assert f"{d:.1f}s" in msg
    assert "iters=1234" in msg and "nodes=7" in msg
    alarming = max(durations) >= lp._ALARMING_LP_CALL_SECONDS or status != "Optimal"
    assert lines[0].levelno == (logging.WARNING if alarming else logging.DEBUG)


# --- #773: the per-call timing line ----------------------------------------------


class _InfoHighs(_PhaseHighs):
    def __init__(self, status="Optimal", iterations=1234, nodes=7, gap=0.0):
        super().__init__(status)
        self._info = MagicMock(
            simplex_iteration_count=iterations, mip_node_count=nodes, mip_gap=gap
        )


class _BrokenInfoHighs(_PhaseHighs):
    def getInfo(self):
        raise RuntimeError("highs info unavailable")


def _timing_lines(monkeypatch, h, elapsed, raises=None, label="primary_minimize"):
    """The `#773 diag` records one timed call of `elapsed` seconds emits.
    With `raises`, the call raises it; the records are returned on the
    exception as `.lines`."""
    monkeypatch.setattr(lp.time, "monotonic", _clock([elapsed]))
    monkeypatch.setattr(lp, "_dump_failing_model", lambda *a, **k: None)
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append
    level = lp._LOGGER.level
    lp._LOGGER.addHandler(handler)
    lp._LOGGER.setLevel(logging.DEBUG)
    try:
        with lp._timed_lp_call(h, label):
            if raises is not None:
                raise raises
    except BaseException as exc:
        exc.lines = [r for r in records if "#773 diag" in r.getMessage()]
        raise
    finally:
        lp._LOGGER.removeHandler(handler)
        lp._LOGGER.setLevel(level)
    return [r for r in records if "#773 diag" in r.getMessage()]


def test_an_unreadable_info_struct_never_fails_the_call(monkeypatch):
    # test_an_unreadable_info_struct_never_fails_the_call: #773: the reads are best-effort, so a diagnostic is never why a solve
    _solver_lp._lex_calibration_failed_until = 0.0
    with monkeypatch.context() as mp:
        for elapsed in [0.1, 70.0]:
            _solver_lp._lex_calibration_failed_until = 0.0
            with mp.context() as mp:
                lines = _timing_lines(mp, _BrokenInfoHighs(), elapsed)
                if elapsed < lp._SLOW_LP_CALL_SECONDS:
                    assert lines == []
                    continue
                (line,) = lines
                assert "70.0s" in line.getMessage()
                assert "simplex_iterations=None" in line.getMessage()

    # test_every_highs_solve_in_lp_is_timed: #773: the helper can be right while a new call site skips it. Every
    _solver_lp._lex_calibration_failed_until = 0.0
    tree = _lp_tree()
    assert _untimed_solves(tree) == []
    attrs = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "h"
        and n.func.attr in ("run", "minimize")
    }
    assert attrs == {"run", "minimize"}
    untimed = ast.parse("def f(h):\n    h.run()\n")
    assert _untimed_solves(untimed) == ["h.run() at line 2"]
    # #2047: `_run(h)` is a solve too, and the bare `h.run()` inside `_run`
    # itself is the one exemption.
    assert _untimed_solves(ast.parse("def f(h):\n    _run(h)\n")) == [
        "_run(h) at line 2"
    ]
    assert _untimed_solves(ast.parse("def _run(h):\n    h.run()\n")) == []
    # Every HiGHS run in lp goes through `_run`, so through the wall deadline.
    bare = [
        n.lineno
        for f in ast.walk(tree)
        if isinstance(f, ast.FunctionDef) and f.name != "_run"
        for n in ast.walk(f)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "h"
    ]
    assert bare == []
    # Mark Purcell, #2096 review: `h.minimize` is the one solve not made
    # through `_run`, so its deadline bound is checked here: the statement
    # right before it must be `_bound_by_wall_deadline(h)`.
    assert _unbounded_minimizes(tree) == []
    unbounded = ast.parse("def f(h, e):\n    h.minimize(e)\n")
    assert _unbounded_minimizes(unbounded) == ["h.minimize() at line 2"]
    bounded = ast.parse(
        "def f(h, e):\n    _bound_by_wall_deadline(h)\n    h.minimize(e)\n"
    )
    assert _unbounded_minimizes(bounded) == []
    timed = ast.parse(
        "def f(h):\n    with _timed_lp_call(h, 'x'):\n        h.minimize()\n"
    )
    assert _untimed_solves(timed) == []

    # test_no_call_site_reports_a_hardcoded_problem_size: #773 review: `primary_minimize` was first wired with `n_binary=0`, a
    _solver_lp._lex_calibration_failed_until = 0.0
    offenders = [
        f"{kw.arg}={kw.value.value!r} at line {node.lineno}"
        for node in ast.walk(_lp_tree())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_timed_lp_call"
        for kw in node.keywords
        if kw.arg in ("n_vars", "n_binary")
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is not None
    ]
    assert offenders == []

    # test_only_an_alarming_call_is_captured: Captured at the alarming threshold, below the per-call limit (so a
    _solver_lp._lex_calibration_failed_until = 0.0
    assert (
        lp._SLOW_LP_CALL_SECONDS
        < lp._ALARMING_LP_CALL_SECONDS
        < lp.DEFAULT_TIME_LIMIT_SECONDS
    )


def _lp_tree():
    return ast.parse(pathlib.Path(lp.__file__).read_text(encoding="utf-8"))


def _is_call(stmt, *, name=None, attr=None) -> bool:
    """`stmt` is a bare call to `name(...)`, or to `h.<attr>(...)`."""
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return False
    func = stmt.value.func
    if name is not None:
        return isinstance(func, ast.Name) and func.id == name
    return (
        isinstance(func, ast.Attribute)
        and func.attr == attr
        and isinstance(func.value, ast.Name)
        and func.value.id == "h"
    )


def _unbounded_minimizes(tree) -> list[str]:
    """Each `h.minimize(...)` whose previous statement is not
    `_bound_by_wall_deadline(h)` (#2096)."""
    found = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for i, stmt in enumerate(body):
            if _is_call(stmt, attr="minimize") and not (
                i > 0 and _is_call(body[i - 1], name="_bound_by_wall_deadline")
            ):
                found.append(f"h.minimize() at line {stmt.lineno}")
    return found


def _untimed_solves(tree) -> list[str]:
    """Each `h.run()` / `h.minimize()` / `_run(h)` outside a
    `with _timed_lp_call(...)`, except the `h.run()` inside `_run` itself."""
    timed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run":
            timed.update(id(inner) for inner in ast.walk(node))
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "_timed_lp_call"
            for item in node.items
        ):
            timed.update(id(inner) for inner in ast.walk(node))
    return [
        f"h.{node.func.attr}() at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("run", "minimize")
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "h"
        and id(node) not in timed
    ] + [
        f"_run(h) at line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run"
        and id(node) not in timed
    ]


# --- #940: the echoed thermal rates are reporting only ---------------------------

_TH_N = 12


def _thermal_plan(**over) -> Plan:
    price = np.array([0.05] * 6 + [0.60] * 6)  # a real cheap/dear swing
    thermal = {
        "name": "hws",
        "max_power_kw": 3.0,
        "initial_temperature_c": 42.0,
        "target_temperature_c": 60.0,
        "earliest_period": 0,
        "deadline_period": _TH_N - 1,
        "heating_rate_c_per_kwh": 8.0,
        "idle_decay_c_per_hour": 0.5,
        **over,
    }
    return build_plan(
        periods=PeriodGrid(np.ones(_TH_N), start=datetime(2026, 9, 16, tzinfo=UTC)),
        grid=GridConfig(
            import_price=price,
            export_price=price - 0.02,
            import_limit_kw=100.0,
            export_limit_kw=100.0,
        ),
        batteries=[
            _battery(
                capacity_kwh=40.0,
                initial_soc_kwh=20.0,
                min_soc_kwh=4.0,
                max_soc_kwh=40.0,
                max_charge_kw=10.0,
                max_discharge_kw=10.0,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(_TH_N)),
        loads=[LoadConfig(name="house", forecast_kw=np.ones(_TH_N))],
        thermal_loads=[ThermalLoadConfig(**thermal)],
    )


def _dispatch(plan: Plan) -> tuple:
    tl = plan.thermal_loads[0]
    return (
        plan.status,
        plan.total_cost,
        *(
            a.tobytes()
            for a in (
                plan.battery_charge_kw,
                plan.battery_discharge_kw,
                plan.battery_soc_kwh,
                plan.grid_import_kw,
                plan.grid_export_kw,
                tl.power_kw,
                tl.temperature_c,
            )
        ),
    )


_ORIGINS = st.sampled_from([None, "override", "learned", "fallback"])


# --- #1751: the direction check names its outcome on every path ---------------


def _spread_site() -> dict:
    """#1535's counterexample: a constant 21.16c import against 61.66c
    export, which the first solve answers two-way."""
    n = 6
    return {
        "periods": PeriodGrid(np.full(n, 0.5)),
        "grid": GridConfig(
            import_price=np.full(n, 0.2116),
            export_price=np.full(n, 0.6166),
            import_limit_kw=32.0,
            export_limit_kw=30.0,
        ),
        "batteries": [_battery()],
        "solar": SolarConfig(forecast_kw=np.zeros(n)),
        "loads": [LoadConfig(name="house", forecast_kw=np.full(n, 0.357))],
    }


def _re_solve(make):
    """Run the real first solve; replace each direction-exclusive re-solve
    with `make(real_re_solve)`."""
    real = network._build_plan_thermal_fallback

    def fake(**kw):
        if "direction_binaries" not in kw:
            return real(**kw)
        return make(real(**kw))

    return patch.object(network, "_build_plan_thermal_fallback", fake)


def _first_two_way_pairs() -> tuple[Plan, int]:
    first = network._build_plan_thermal_fallback(**_spread_site())
    pairs = len(network._opposing_flow_binaries(first))
    assert first.status == "optimal" and pairs > 0  # the premise
    return first, pairs


_DURATIONS = st.lists(
    st.sampled_from([1 / 60, 5 / 60, 0.25, 0.5, 1.0]), min_size=2, max_size=8
)


@SETTINGS
@given(_num(2.0, 15.0), _num(1.0, 3.0), st.sampled_from(["days", "global", "none"]))
def test_max_kwh_per_day_caps_each_day(cap, target_share, mode):
    n = 48  # two days, hourly; day 1 cheap, day 2 dear
    target = round(cap * target_share, 4)
    start = None if mode == "global" else datetime(2026, 9, 1, tzinfo=UTC)
    plan = build_plan(
        periods=PeriodGrid(hours=np.ones(n), start=start),
        grid=GridConfig(
            import_price=np.concatenate([np.full(24, 0.05), np.full(24, 0.50)]),
            export_price=np.zeros(n),
            import_limit_kw=50.0,
            export_limit_kw=0.0,
        ),
        batteries=[_battery(available=False)],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
        adequacy_loads=[
            AdequacyLoadConfig(
                name="miner",
                max_power_kw=5.0,
                target_kwh=target,
                deadline_period=n - 1,
                shortfall_price=5.0,
                max_kwh_per_day=None if mode == "none" else cap,
            )
        ],
        # Continuous, so energy is not rounded up to whole hours at full power.
        adequacy_semi_continuous=False,
    )
    assert plan.status == "optimal"
    power = plan.adequacy_loads[0].power_kw
    day1, day2 = float(np.sum(power[:24])), float(np.sum(power[24:]))
    if mode == "days":
        assert day1 <= cap + 1e-6 and day2 <= cap + 1e-6
        assert abs(day1 + day2 - min(target, 2 * cap)) < 1e-4
    elif mode == "global":
        assert day1 + day2 <= cap + 1e-6
    else:
        assert abs(day1 - target) < 1e-4 and day2 < 1e-6


# --- #478: an adequacy block is anchored to the previous solve, by load name ----

_ADQ_N = 24


def _adequacy_plan(price, *, name="hws", previous=None) -> Plan:
    return build_plan(
        periods=PeriodGrid(np.ones(_ADQ_N), start=datetime(2026, 9, 10, tzinfo=UTC)),
        grid=GridConfig(
            import_price=price,
            export_price=np.zeros(_ADQ_N),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[_battery(max_charge_kw=0.0, max_discharge_kw=0.0)],
        solar=SolarConfig(forecast_kw=np.zeros(_ADQ_N)),
        loads=[],
        adequacy_loads=[
            AdequacyLoadConfig(
                name=name,
                max_power_kw=0.65,
                target_kwh=1.3,
                deadline_period=_ADQ_N - 1,
                earliest_period=0,
                shortfall_price=50.0,
            )
        ],
        previous_plan=previous,
    )


def _block(plan: Plan) -> tuple[float, ...]:
    p = np.asarray(plan.adequacy_loads[0].power_kw)
    return tuple(round(float(p[t]), 4) for t in (5, 6, 15, 16))


_AT_5_6, _AT_15_16 = (0.65, 0.65, 0.0, 0.0), (0.0, 0.0, 0.65, 0.65)


# --- #773 / #1179: every dump says whether it will survive ---------------------


def test_the_dump_note_says_whether_the_file_survives(monkeypatch, tmp_path, caplog):
    # test_the_dump_note_says_whether_the_file_survives: #773 (18 Sep 2026: the first real capture landed in a container's
    _solver_lp._lex_calibration_failed_until = 0.0
    with monkeypatch.context() as mp:
        for configured in [None, "/data/nimbus_dumps"]:
            _solver_lp._lex_calibration_failed_until = 0.0
            with mp.context() as mp:
                if configured is None:
                    mp.delenv(lp._LP_DUMP_DIR_ENV, raising=False)
                else:
                    mp.setenv(lp._LP_DUMP_DIR_ENV, configured)
                note = lp._dump_location_note()
                assert lp.dump_location_note() == note
                if configured is None:
                    for word in ("TEMPORARY", "will not survive", "NOW", "persistent"):
                        assert word in note, word
                    assert lp._LP_DUMP_DIR_ENV in note
                else:
                    assert "persists" in note
                    assert "TEMPORARY" not in note and "will not survive" not in note

    # test_every_dump_announcement_says_whether_it_survives: #773: both dump paths, the alarming slow call and the failed phase,
    _solver_lp._lex_calibration_failed_until = 0.0
    caplog.clear()
    tmp_1 = tmp_path / "test_every_dump_announcement_says_whether_it_survives"
    tmp_1.mkdir()
    with monkeypatch.context() as mp:
        mp.setenv(lp._LP_DUMP_DIR_ENV, str(tmp_1))
        mp.setattr(lp, "_lex_calibration_failed_until", 0.0)
        mp.setattr(lp, "UNKNOWN_RETRIES", {"attempted": 0, "recovered": 0})
        lp._LP_DUMPED_PHASES.clear()
        note = lp._dump_location_note()
        try:
            with caplog.at_level(logging.WARNING, logger=lp._LOGGER.name):
                mp.setattr(lp.time, "monotonic", _clock([46.0]))
                with lp._timed_lp_call(_WritingHighs(), "slow_phase"):
                    pass
                mp.undo()
                mp.setenv(lp._LP_DUMP_DIR_ENV, str(tmp_1))
                mp.setattr(lp, "_lex_calibration_failed_until", 0.0)
                with patch.object(highspy.Highs, "getModelStatus", _unknown(False)):
                    _two_objective_lp().solve(options=LexOptions())
        finally:
            lp._LP_DUMPED_PHASES.clear()
        announced = [
            r.getMessage() for r in caplog.records if "wrote the" in r.getMessage()
        ]
        assert any("slow model" in m for m in announced)
        assert any("failing model" in m for m in announced)
        for message in announced:
            assert message.endswith(note), message

    # test_the_tier0_minutes_tether_to_the_period_they_fall_inside: #635's captured plans: 1-minute periods 17:26-17:29 tether to the
    _solver_lp._lex_calibration_failed_until = 0.0
    old = PeriodGrid(np.full(4, 5 / 60), start=datetime(2026, 9, 9, 17, 25, tzinfo=UTC))
    new = PeriodGrid(
        np.array([1 / 60] * 4 + [5 / 60]),
        start=datetime(2026, 9, 9, 17, 26, tzinfo=UTC),
    )
    previous = _infeasible_plan(old, "optimal", iterations=1)
    assert network._align_previous_periods(new, previous) == {
        0: 0,
        1: 0,
        2: 0,
        3: 0,
        4: 1,
    }
    failed = _infeasible_plan(old, "infeasible", iterations=0)
    assert network._align_previous_periods(new, failed) == {}
    floating = PeriodGrid(np.full(4, 0.25), start=None)
    assert (
        network._align_previous_periods(
            floating, _infeasible_plan(floating, "optimal", iterations=1)
        )
        == {}
    )


class _WritingHighs(_PhaseHighs):
    def writeModel(self, path):
        with open(path, "w", encoding="utf-8"):
            pass


# --- #356 / #635: aligning a new grid to the previous plan's --------------------


def _containment_oracle(new: PeriodGrid, old: PeriodGrid) -> dict[int, int]:
    """#635's rule as a plain restart-from-zero scan (an independent oracle
    for the two-pointer merge, #356): each new period tethers to the old
    period its start falls inside, within the alignment tolerance."""
    from datetime import timedelta

    tol = network._ALIGNMENT_TOLERANCE
    out: dict[int, int] = {}
    for i, t in enumerate(new.period_starts):
        j = 0
        while (
            j < len(old.period_starts) - 1
            and old.period_starts[j] + timedelta(hours=float(old.hours[j])) <= t + tol
        ):
            j += 1
        end = old.period_starts[j] + timedelta(hours=float(old.hours[j]))
        if old.period_starts[j] - tol <= t < end + tol:
            out[i] = j
    return out


_WIDTHS = st.sampled_from([1 / 60, 5 / 60, 0.25, 0.5, 1.0])


# --- household 20/22 Aug 2026: a committed P2P block is held flat --------------


def _p2p_day(window_rates, *, fixed=None, window_import=0.55, terminal=None):
    """10 h of cheap daytime import, a 7 h P2P window at `window_rates`,
    9 h overnight; a 122 kWh battery starting at 20%."""
    n = 26
    imp = np.array([0.15] * 10 + [window_import] * 7 + [0.15] * 9)
    exp = np.array([0.08] * 10 + list(window_rates) + [0.08] * 9)
    pin = None
    if fixed is not None:
        pin = np.full(n, np.nan)
        pin[10:17] = fixed
    return build_plan(
        periods=PeriodGrid(np.ones(n), start=datetime(2026, 8, 20, 7, tzinfo=UTC)),
        grid=GridConfig(
            import_price=imp,
            export_price=exp,
            import_limit_kw=44.0,
            export_limit_kw=44.0,
            fixed_export_kw=pin,
        ),
        batteries=[
            _battery(
                capacity_kwh=122.2,
                initial_soc_kwh=122.2 * 0.2,
                min_soc_kwh=122.2 * 0.05,
                max_soc_kwh=122.2,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=0.975,
                discharge_efficiency=0.975,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.15,
                terminal_value_breakpoints=terminal,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 1.5))],
    )
