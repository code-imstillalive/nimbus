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

import pytest
from hypothesis import given
from hypothesis import strategies as st
from solver import lp
from solver.lp import LPProblem
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


def _counting(calls, real):
    def tracking(self):
        calls.append(None)
        return real(self)

    return tracking


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


# --- #696 / #702 / #773: what each solve option promises ---------------------------
