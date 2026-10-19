"""Layer 1 of the rebuilt suite (#1837), round 3: `solver/lp.py` on its own.

The same-mutant comparison on `lp.py` (old 11/20, new 4/20, posted on #1837)
found the new set's misses on paths `build_plan` with default options never
takes: `LPProblem.absorb` (the stochastic stage's joint problem) and the
lexicographic and calibrated solve modes production runs. These properties
drive `LPProblem` directly, on small random problems that are always feasible
(x = 0 meets every row) and bounded (every variable has a finite range).
"""

from __future__ import annotations

import numpy as np
from hypothesis import given
from hypothesis import strategies as st
from solver.lp import CalibratedOptions, LexOptions, LPProblem
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
