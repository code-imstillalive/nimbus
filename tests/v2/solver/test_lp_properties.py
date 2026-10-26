"""Layer 1 of the rebuilt suite (#1837), round 3: `solver/lp.py` on its own.

The same-mutant comparison on `lp.py` (old 11/20, new 4/20, posted on #1837)
found the new set's misses on paths `build_plan` with default options never
takes: `LPProblem.absorb` (the stochastic stage's joint problem) and the
lexicographic and calibrated solve modes production runs. These properties
drive `LPProblem` directly, on small random problems that are always feasible
(x = 0 meets every row) and bounded (every variable has a finite range).
"""

from __future__ import annotations

import logging
import math
from unittest.mock import patch

import highspy
import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import lp
from solver.lp import BlendedOptions, CalibratedOptions, LexOptions, LPProblem
from test_plan_invariants import SETTINGS, _num


@st.composite
def problems(draw, prefix: str = ""):
    """A feasible, bounded LP: n variables in [0, ub], m rows
    sum(a * x) <= b with a, b >= 0 (so x = 0 is feasible), a primary cost
    of either sign, and a secondary cost on a random subset. Returns the
    problem and a spec to rebuild it."""
    n = draw(st.integers(min_value=2, max_value=6))
    m = draw(st.integers(min_value=1, max_value=4))
    ub = draw(st.lists(_num(0.5, 10.0), min_size=n, max_size=n))
    cost = draw(st.lists(_num(-5.0, 5.0), min_size=n, max_size=n))
    second = draw(
        st.lists(st.one_of(st.none(), _num(-3.0, 3.0)), min_size=n, max_size=n)
    )
    rows = [
        (
            draw(st.lists(_num(0.0, 3.0), min_size=n, max_size=n)),
            draw(_num(0.5, 20.0)),
            draw(st.booleans()),
        )
        for _ in range(m)
    ]
    return {"n": n, "ub": ub, "cost": cost, "second": second, "rows": rows}


def _build(spec) -> LPProblem:
    p = LPProblem()
    for j in range(spec["n"]):
        p.add_variable(f"x{j}", lb=0.0, ub=spec["ub"][j], cost=spec["cost"][j])
        if spec["second"][j] is not None:
            p.set_secondary_cost(f"x{j}", spec["second"][j])
    for i, (a, b, named) in enumerate(spec["rows"]):
        terms = {f"x{j}": a[j] for j in range(spec["n"]) if a[j] != 0.0}
        if not terms:
            terms = {"x0": 1.0}
        p.add_ub_constraint(terms, b, name=f"row{i}" if named else None)
    return p


def _secondary(p: LPProblem, result) -> float:
    return float(
        sum(
            c * float(result.x[p._var_index[name]])
            for name, c in p._secondary_cost.items()
        )
    )


@SETTINGS
@given(problems(), problems(), _num(0.1, 5.0), _num(0.1, 5.0))
def test_an_absorbed_problem_solves_as_the_sum_of_its_parts(a, b, sa, sb):
    """Two independent problems absorbed under their own prefixes and cost
    scales solve to the scaled sum of their own optima, each part's cost read
    back through `primary_cost_of(within=, prefix=)` is its own optimum, and
    every row, named or not, is reported under its prefixed name."""
    pa, pb = _build(a), _build(b)
    ra, rb = pa.solve(), pb.solve()
    assert ra.status == rb.status == "optimal"
    joint = LPProblem()
    joint.absorb(pa, prefix="a.", cost_scale=sa)
    joint.absorb(pb, prefix="b.", cost_scale=sb)
    rj = joint.solve()
    assert rj.status == "optimal"
    expected = sa * ra.objective + sb * rb.objective
    assert abs(rj.objective - expected) <= 1e-6 * max(1.0, abs(expected))
    got_a = pa.primary_cost_of(rj, within=joint, prefix="a.")
    assert abs(got_a - ra.objective) <= 1e-6 * max(1.0, abs(ra.objective))
    for tag, spec in (("a.", a), ("b.", b)):
        for i, (_a, _b, named) in enumerate(spec["rows"]):
            assert f"{tag}{f'row{i}' if named else f'ub_{i}'}" in rj.duals


@SETTINGS
@given(problems())
def test_absorbing_into_an_empty_problem_reproduces_it(spec):
    """`absorb(prefix="", cost_scale=1.0)` into an empty problem is the
    problem itself: the same optimum and the same point."""
    p = _build(spec)
    joint = LPProblem()
    joint.absorb(p, prefix="", cost_scale=1.0)
    r, rj = p.solve(), joint.solve()
    assert r.status == rj.status == "optimal"
    assert abs(r.objective - rj.objective) <= 1e-9 * max(1.0, abs(r.objective))
    np.testing.assert_allclose(r.x, rj.x, atol=1e-9)


@SETTINGS
@given(problems())
def test_the_lexicographic_solve_keeps_the_primary_and_improves_the_secondary(
    spec,
):
    """Lexicographic mode, which production's tie-breaks rely on: the primary
    objective stays at its plain optimum (within its tie slack), and among
    primary-optimal points the secondary is no worse than at the plain
    solve's point."""
    p = _build(spec)
    plain = p.solve()
    lex = p.solve(options=LexOptions())
    assert plain.status == lex.status == "optimal"
    primary = p.primary_cost_of(lex)
    assert primary <= plain.objective + 1e-7 * max(1.0, abs(plain.objective))
    assert _secondary(p, lex) <= _secondary(p, plain) + 1e-6


@SETTINGS
@given(problems())
def test_the_calibrated_solve_keeps_the_primary_within_its_tolerance(spec):
    """Calibrated mode, production's default: the primary objective is
    within `calibration_tolerance` (relative) of its plain optimum."""
    p = _build(spec)
    plain = p.solve()
    options = CalibratedOptions()
    cal = p.solve(options=options)
    assert plain.status == cal.status == "optimal"
    primary = p.primary_cost_of(cal)
    slack = options.calibration_tolerance * max(1.0, abs(plain.objective))
    assert primary <= plain.objective + slack + 1e-7


@SETTINGS
@given(problems())
def test_ranging_brackets_every_value_and_cost_it_reports(spec):
    """#490 (Signals 1/7 of #489): `solve(ranging=True)` ranges every
    variable and every named row exactly once (highspy's cost arrays come
    back one element too long, which must never be attributed to a
    variable; an unnamed row is ranged under its generated name), each bound interval contains the variable's value, each cost
    interval contains its cost, and the headroom helpers are the distances
    to those intervals."""
    problem = _build(spec)
    result = problem.solve(ranging=True)
    if result.status != "optimal":
        return
    names = {f"x{j}" for j in range(spec["n"])}
    rows = {f"row{i}" for i, (_a, _b, named) in enumerate(spec["rows"]) if named}
    assert result.ranging_valid is True
    for field in ("col_bound_up", "col_bound_dn", "col_cost_up", "col_cost_dn"):
        assert set(getattr(result, field)) == names, field
    for field in ("row_bound_up", "row_bound_dn"):
        ranged = set(getattr(result, field))
        assert rows <= ranged and len(ranged) == len(spec["rows"]), field
    for j in range(spec["n"]):
        name = f"x{j}"
        x = problem.value_of(result, name)
        lo, hi = result.col_bound_dn[name].value, result.col_bound_up[name].value
        assert lo - 1e-6 <= x <= hi + 1e-6, name
        c = spec["cost"][j]
        assert (
            result.col_cost_dn[name].value - 1e-6
            <= c
            <= result.col_cost_up[name].value + 1e-6
        ), name
        room = result.bound_headroom(name)
        assert room is not None
        assert room.down == pytest.approx(x - lo, abs=1e-6) or math.isinf(lo)
        assert room.up == pytest.approx(hi - x, abs=1e-6) or math.isinf(hi)
    assert result.bound_headroom("no_such_variable") is None
    assert result.rhs_headroom("no_such_row") is None


@SETTINGS
@given(problems())
def test_ranging_not_asked_for_is_never_computed(spec):
    """#490: ranging is opt-in. Unasked, HiGHS's getRanging is never
    called (solve time unchanged for every other caller), validity is None
    and every ranging field is empty."""
    problem = _build(spec)
    calls: list[None] = []
    real = highspy.Highs.getRanging

    def tracking(self):
        calls.append(None)
        return real(self)

    with patch.object(highspy.Highs, "getRanging", tracking):
        result = problem.solve()
    assert calls == []
    assert result.ranging_valid is None
    for field in (
        "col_bound_up",
        "col_bound_dn",
        "col_cost_up",
        "col_cost_dn",
        "row_bound_up",
        "row_bound_dn",
    ):
        assert getattr(result, field) == {}, field
    assert result.bound_headroom("x0") is None


def test_ranging_on_the_probe_lp_matches_the_hand_worked_values():
    """#489's research probe, every number checkable by hand:
    min 0.30 imp + 0.05 exp, imp - exp + solar = 2, solar <= 6."""
    p = LPProblem()
    p.add_variable("imp", cost=0.30)
    p.add_variable("exp", cost=0.05)
    p.add_variable("solar", ub=6.0)
    p.add_eq_constraint({"imp": 1.0, "exp": -1.0, "solar": 1.0}, 2.0, name="balance")
    with patch.object(
        highspy.Highs, "getRanging", _counting(calls := [], highspy.Highs.getRanging)
    ):
        r = p.solve(ranging=True)
    assert len(calls) == 1
    assert (p.value_of(r, "imp"), p.value_of(r, "solar")) == (0.0, 2.0)
    assert r.reduced_costs == pytest.approx({"imp": 0.30, "exp": 0.05, "solar": 0.0})
    up = {k: v.value for k, v in r.col_bound_up.items()}
    dn = {k: v.value for k, v in r.col_bound_dn.items()}
    assert up == pytest.approx({"imp": 2.0, "exp": 4.0, "solar": 6.0})
    assert dn == pytest.approx({"imp": -4.0, "exp": -2.0, "solar": 0.0})
    assert (r.col_cost_dn["solar"].value, r.col_cost_up["solar"].value) == (
        pytest.approx(-0.05),
        pytest.approx(0.30),
    )
    assert r.row_bound_up["balance"].value == pytest.approx(6.0)
    assert r.row_bound_dn["balance"].value == pytest.approx(0.0)
    room = r.rhs_headroom("balance")
    assert (room.down, room.up) == (pytest.approx(2.0), pytest.approx(4.0))


def _counting(calls, real):
    def tracking(self):
        calls.append(None)
        return real(self)

    return tracking


def test_ranging_on_a_pinned_or_failed_solve():
    """#490 with #238: a MIP's ranging is the pinned LP's (valid, every
    column); a zero-width column is degenerate headroom; an infeasible
    solve reports ranging invalid with nothing in it, or None if unasked."""
    p = LPProblem()
    y = p.add_variable("y", binary=True)
    x = p.add_variable("x", ub=10.0, cost=-1.0)
    p.add_ub_constraint({x: 1.0, y: -10.0}, 0.0, name="gate")
    r = p.solve(ranging=True)
    assert r.status == "optimal" and r.ranging_valid is True
    assert set(r.col_bound_up) == {"x", "y"}

    p = LPProblem()
    p.add_variable("imp", cost=0.30)
    p.add_variable("solar", lb=6.0, ub=6.0)
    p.add_eq_constraint({"imp": 1.0, "solar": 1.0}, 6.0, name="balance")
    room = p.solve(ranging=True).bound_headroom("solar")
    assert room.degenerate and (room.down, room.up) == (0.0, 0.0)

    def infeasible():
        q = LPProblem()
        q.add_variable("x", ub=1.0)
        q.add_eq_constraint({"x": 1.0}, 5.0)
        return q

    r = infeasible().solve(ranging=True)
    assert r.status == "infeasible" and r.ranging_valid is False
    assert r.col_bound_up == {} and r.row_bound_up == {}
    assert infeasible().solve().ranging_valid is None


@SETTINGS
@given(problems(), st.lists(_num(-6.0, 6.0), min_size=1, max_size=4), st.data())
def test_every_ranging_sweep_step_holds_inside_its_own_interval(spec, costs, data):
    """`sweep_cost_with_ranging` (the offer curve's per-step sensitivity):
    every re-solve completes, its value stays within the variable's bounds,
    and where ranging is valid the swept cost lies inside the interval it
    reports for that step."""
    p = _build(spec)
    result = p.solve(keep_basis=True)
    assert result.status == "optimal"
    j = data.draw(st.integers(min_value=0, max_value=spec["n"] - 1))
    steps = result.sweep_cost_with_ranging(f"x{j}", costs)
    assert len(steps) == len(costs)
    for cost, step in zip(costs, steps, strict=True):
        assert -1e-9 <= step.value <= spec["ub"][j] + 1e-9
        if step.cost_dn is not None and step.cost_up is not None:
            assert step.cost_dn.value - 1e-7 <= cost <= step.cost_up.value + 1e-7


_KNOWN = (
    "simplex_iteration_count",
    "mip_node_count",
    "mip_gap",
    "mip_dual_bound",
    "primal_solution_status",
)


@SETTINGS
@given(
    st.dictionaries(
        st.one_of(st.sampled_from(_KNOWN), st.sampled_from(("zz_extra", "aa_extra"))),
        st.one_of(st.integers(-5, 10**6).map(float), _num(-1e3, 1e3)),
    )
)
def test_the_highs_info_line_lists_every_field_known_first(info):
    """The #773 diagnostic line: every field appears exactly once, the known
    fields first in their fixed order and any others after them sorted, and a
    whole number prints without `.0` (`simplex_iterations=0` must be
    unmissable)."""
    from solver.lp import _HIGHS_INFO_FIELDS, format_highs_info

    line = format_highs_info(info)
    if not info:
        assert line == "unavailable"
        return
    tokens = line.split(" ")
    names = [t.split("=", 1)[0] for t in tokens]
    known = [n for n in _HIGHS_INFO_FIELDS if n in info]
    extra = sorted(set(info) - set(_HIGHS_INFO_FIELDS))
    expected = [
        n.replace("simplex_iteration_count", "simplex_iterations")
        for n in known + extra
    ]
    assert names == expected
    for token, name in zip(tokens, known + extra, strict=True):
        value = info[name]
        if value == int(value):
            assert token.split("=", 1)[1] == str(int(value))


@SETTINGS
@given(problems())
def test_the_captured_point_is_the_solved_point(spec):
    """The point captured for a MIP start or a pin (#773's phase-2 fix) is
    the solve's own full column vector."""
    from solver.lp import _capture_integral_point

    p = _build(spec)
    result = p.solve(keep_basis=True)
    assert result.status == "optimal"
    captured = _capture_integral_point(result._highs)
    assert captured is not None
    np.testing.assert_allclose(captured, result.x, atol=1e-9)


@SETTINGS
@given(problems(), st.integers(min_value=0, max_value=6))
def test_the_calibrated_solve_holds_a_tiny_primary_against_a_large_secondary(spec, k):
    """The calibrated weight search, where it matters most: primary costs a
    millionth of the secondary's. The weight it settles on must still hold
    the primary within the tolerance, however small the primary's
    differences are. The lexicographic bound it starts from allows
    `_LEX_PRIMARY_TIE_ABS_SLACK` (1e-7, the solver's own feasibility
    tolerance made explicit), so that is allowed on top."""
    spec = {**spec, "cost": [c * 10.0**-k for c in spec["cost"]]}
    p = _build(spec)
    plain = p.solve()
    options = CalibratedOptions()
    cal = p.solve(options=options)
    assert plain.status == cal.status == "optimal"
    tol = max(1e-8, abs(plain.objective) * options.calibration_tolerance)
    assert p.primary_cost_of(cal) <= plain.objective + tol + 1e-7 + 1e-12


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _slow_call_levels(problem: LPProblem) -> tuple[str, list[int]]:
    """Solve with every HiGHS call counted as slow (threshold 0 s, alarm
    threshold out of reach), returning the status and the levels of the
    #773 slow-call lines logged."""
    from solver import lp

    handler = _Capture()
    saved = (lp._SLOW_LP_CALL_SECONDS, lp._ALARMING_LP_CALL_SECONDS)
    saved_level = lp._LOGGER.level
    lp._SLOW_LP_CALL_SECONDS, lp._ALARMING_LP_CALL_SECONDS = 0.0, 1e9
    lp._LOGGER.addHandler(handler)
    lp._LOGGER.setLevel(logging.DEBUG)
    try:
        result = problem.solve()
    finally:
        lp._SLOW_LP_CALL_SECONDS, lp._ALARMING_LP_CALL_SECONDS = saved
        lp._LOGGER.removeHandler(handler)
        lp._LOGGER.setLevel(saved_level)
    levels = [
        r.levelno for r in handler.records if "#773 diag: HiGHS call" in r.getMessage()
    ]
    return result.status, levels


@SETTINGS
@given(problems())
def test_a_slow_call_that_solved_is_logged_quietly(spec):
    """A slow HiGHS call that reached optimal is a DEBUG line, never a
    WARNING: only a call that failed, or ran past the alarm threshold, is
    alarming (#773)."""
    status, levels = _slow_call_levels(_build(spec))
    assert status == "optimal"
    assert levels
    assert all(level == logging.DEBUG for level in levels)


def test_a_slow_call_that_failed_is_a_warning():
    """The same slow call on an infeasible problem (x >= 1 and x <= 0) is a
    WARNING, whatever its time."""
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=1.0, cost=1.0)
    p.add_ub_constraint({"x": -1.0}, -1.5)
    status, levels = _slow_call_levels(p)
    assert status != "optimal"
    assert logging.WARNING in levels


# --- #696 / #702 / #773: what each solve option promises ---------------------------


@SETTINGS
@given(problems())
def test_without_options_the_secondary_cost_is_never_read(spec):
    """#696: options=None is the plain solve. A problem with secondary costs
    and the same problem without them solve to the same objective."""
    with_second = _build(spec)
    without = _build({**spec, "second": [None] * spec["n"]})
    a, b = with_second.solve(), without.solve()
    assert a.status == b.status
    if a.status == "optimal":
        assert a.objective == pytest.approx(b.objective, abs=1e-6)


@SETTINGS
@given(problems(), st.sampled_from([1e-6, 1e-3, 1.0, 10.0]))
def test_a_blended_solve_is_the_optimum_of_the_blended_cost(spec, weight):
    """#696: BlendedOptions minimises primary + weight x secondary, nothing
    more: its point costs what the plain optimum of that one blended cost
    does (so a large enough weight can override a real price, by design)."""
    p = _build(spec)
    blended = p.solve(options=BlendedOptions(blend_weight=weight))
    second = [c if c is not None else 0.0 for c in spec["second"]]
    plain = _build(
        {
            **spec,
            "cost": [
                c + weight * s2 for c, s2 in zip(spec["cost"], second, strict=True)
            ],
            "second": [None] * spec["n"],
        }
    ).solve()
    assert blended.status == plain.status
    if plain.status != "optimal":
        return
    got = p.primary_cost_of(blended) + weight * _secondary(p, blended)
    assert got == pytest.approx(
        plain.objective, abs=1e-6 * max(1.0, abs(plain.objective))
    )


@SETTINGS
@given(problems())
def test_a_calibrated_solve_reports_the_primary_cost(spec):
    """#696: the calibrated solve's objective is the real primary cost of its
    point, never the mixed-unit blended figure it searched with."""
    p = _build(spec)
    r = p.solve(options=CalibratedOptions())
    if r.status == "optimal":
        assert r.objective == pytest.approx(p.primary_cost_of(r), abs=1e-6)


@SETTINGS
@given(
    st.lists(st.sampled_from([1.0, 2.0, 5.0]), min_size=2, max_size=4),
    st.data(),
    st.sampled_from(["lex", "calibrated", "blended"]),
)
def test_a_binary_choice_is_primary_first_then_secondary(primary, data, kind):
    """#702 (#696's follow-up): options on a MIP. Exactly one of several
    switches is on. The switch chosen is always among the cheapest on the
    primary cost, whatever the secondary (even 1000x, so a binary is never
    flipped against a real price); among primary ties, the cheapest on the
    secondary. #702's first design pinned the binaries from a primary-only
    solve and so picked a tie arbitrarily. A continuous tie on the same
    problem is still broken by the secondary once the binary is set, and
    the equality row's dual is the real LP dual (#238's recovery)."""
    k = len(primary)
    second = data.draw(
        st.lists(st.sampled_from([-3.0, -1.0, 0.0, 1000.0]), min_size=k, max_size=k)
    )
    p = LPProblem()
    for i in range(k):
        p.add_variable(f"g{i}", binary=True, cost=primary[i])
        p.set_secondary_cost(f"g{i}", second[i])
    p.add_eq_constraint({f"g{i}": 1.0 for i in range(k)}, 1.0, name="exactly_one")
    p.add_variable("x1", ub=10.0, cost=1.0)
    p.add_variable("x2", ub=10.0, cost=1.0)
    p.set_secondary_cost("x2", -1.0)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0, name="total")
    options = {
        "lex": LexOptions(),
        "calibrated": CalibratedOptions(),
        "blended": BlendedOptions(blend_weight=1e-6),
    }[kind]
    r = p.solve(options=options)
    assert r.status == "optimal"
    on = [i for i in range(k) if p.value_of(r, f"g{i}") > 0.5]
    assert len(on) == 1
    (chosen,) = on
    best = min(primary)
    assert primary[chosen] == best
    tied = [i for i in range(k) if primary[i] == best]
    if kind != "blended":
        assert second[chosen] == min(second[i] for i in tied)
        assert p.value_of(r, "x2") == pytest.approx(10.0, abs=1e-4)
        assert r.duals["total"] == pytest.approx(1.0, abs=1e-4)


def test_a_failed_phase_falls_back_to_the_plain_solve_and_says_so(monkeypatch):
    """#773 (a phase failing to reach optimal on two real installs, root
    cause unknown): the failure used to escape `build_plan` uncaught. Now
    the plan is the plain options=None solve, exactly, and a #773 WARNING
    says so."""
    for options in [LexOptions(), CalibratedOptions()]:
        with monkeypatch.context() as mp:
            mp.setattr(lp, "_lex_calibration_failed_until", 0.0)

            def fails(*_a, **_k):
                raise ValueError("an internal lex/calibration phase failed")

            mp.setattr(lp, "_solve_with_options", fails)
            p = LPProblem()
            p.add_variable("x1", ub=10.0, cost=1.0)
            p.add_variable("x2", ub=10.0, cost=1.0)
            p.set_secondary_cost("x2", -1.0)
            p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0, name="total")
            records: list[logging.LogRecord] = []
            handler = logging.Handler(logging.WARNING)
            handler.emit = records.append
            lp._LOGGER.addHandler(handler)
            try:
                r = p.solve(options=options)
            finally:
                lp._LOGGER.removeHandler(handler)
                lp._lex_calibration_failed_until = 0.0
            assert r.status == "optimal"
            assert r.objective == pytest.approx(p.solve().objective)
            assert any("#773" in rec.getMessage() for rec in records)


def test_with_no_primary_cost_the_secondary_alone_picks_the_point():
    """Nimbus's choice, unlike HAEO's (which raises): an all-zero primary is
    a legitimate tie-break-only problem, and the secondary picks the point."""
    p = LPProblem()
    p.add_variable("x1", ub=10.0)
    p.add_variable("x2", ub=10.0)
    p.add_eq_constraint({"x1": 1.0, "x2": 1.0}, 10.0)
    p.set_secondary_cost("x2", -1.0)
    r = p.solve(options=LexOptions())
    assert r.status == "optimal"
    assert p.value_of(r, "x2") == pytest.approx(10.0, abs=1e-4)
