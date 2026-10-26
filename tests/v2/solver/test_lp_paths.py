"""Layer 1 of the rebuilt suite (#1837), round 4: `solver/lp.py` paths the
20 Oct re-score found the new set missing (old 20/20, new 9/20, seed 7078).

Each test states what the code is for, with its own oracle: the true
lexicographic optimum computed independently by two plain solves, the empty
problem, the dense cost vector's definition, the calibrated solve's safe
default, the #773 cooldown, the binary-pinning warning and the diagnostic
formatter's edge values.
"""

from __future__ import annotations

import logging
import math

import pytest
from hypothesis import given
from solver import lp
from solver.lp import CalibratedOptions, LexOptions, LPProblem
from test_lp_properties import _build, _secondary, problems
from test_plan_invariants import SETTINGS


@pytest.fixture(autouse=True)
def _no_cooldown():
    """No test inherits another's #773 cooldown."""
    saved = lp._lex_calibration_failed_until
    lp._lex_calibration_failed_until = 0.0
    yield
    lp._lex_calibration_failed_until = saved


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


def _captured(fn):
    handler = _Capture()
    saved = lp._LOGGER.level
    lp._LOGGER.addHandler(handler)
    lp._LOGGER.setLevel(logging.DEBUG)
    try:
        out = fn()
    finally:
        lp._LOGGER.removeHandler(handler)
        lp._LOGGER.setLevel(saved)
    return out, handler.records


def _true_lexicographic(spec) -> tuple[float, float]:
    """(primary optimum, least secondary among primary-optimal points), by
    two plain solves: the oracle the lexicographic mode must meet."""
    p = _build(spec)
    first = p.solve()
    assert first.status == "optimal"
    q = LPProblem()
    for j in range(spec["n"]):
        q.add_variable(f"x{j}", lb=0.0, ub=spec["ub"][j], cost=spec["second"][j] or 0.0)
    for i, (a, b, _named) in enumerate(spec["rows"]):
        terms = {f"x{j}": a[j] for j in range(spec["n"]) if a[j] != 0.0}
        q.add_ub_constraint(terms or {"x0": 1.0}, b, name=f"r{i}")
    primary = {f"x{j}": spec["cost"][j] for j in range(spec["n"])}
    # The same tie slack the lexicographic mode allows itself: a looser
    # oracle buys a better secondary the mode is not allowed to reach.
    slack = max(
        lp._LEX_PRIMARY_TIE_ABS_SLACK,
        abs(first.objective) * lp._LEX_PRIMARY_TIE_REL_SLACK,
    )
    q.add_ub_constraint(primary, first.objective + slack, name="primary_at_opt")
    second = q.solve()
    assert second.status == "optimal"
    return first.objective, second.objective


@SETTINGS
@given(problems())
def test_the_lexicographic_solve_reaches_the_true_lexicographic_optimum(spec):
    """Not merely no worse than the plain point: among primary-optimal
    points, the lexicographic mode's secondary is the least there is, and no
    phase fell back on a feasible problem."""
    p = _build(spec)
    lex, records = _captured(lambda: p.solve(options=LexOptions()))
    assert lex.status == "optimal"
    best_primary, best_secondary = _true_lexicographic(spec)
    assert p.primary_cost_of(lex) <= best_primary + 1e-6 * max(1.0, abs(best_primary))
    assert _secondary(p, lex) <= best_secondary + 1e-5 * max(1.0, abs(best_secondary))
    fallbacks = [r for r in records if "falling back" in r.getMessage()]
    assert fallbacks == []


def test_the_empty_problem_is_optimal_at_zero():
    result = LPProblem().solve()
    assert result.status == "optimal"
    assert result.objective == 0.0
    assert len(result.x) == 0


@SETTINGS
@given(problems())
def test_the_dense_cost_vector_is_the_dict_with_zero_elsewhere(spec):
    """A variable the cost dict does not name costs nothing."""
    p = _build(spec)
    named = {f"x{j}": float(j + 1) for j in range(0, spec["n"], 2)}
    vec = lp._dense_cost_vector(p, named)
    for i, name in enumerate(p._var_names):
        assert vec[i] == named.get(name, 0.0)


@SETTINGS
@given(problems())
def test_with_no_primary_cost_the_calibrated_weight_is_the_safe_default(spec):
    """Nothing to distort: the documented 1e-3 default, and the secondary
    is still minimised."""
    spec = {**spec, "cost": [0.0] * spec["n"]}
    p = _build(spec)
    cal = p.solve(options=CalibratedOptions())
    assert cal.status == "optimal"
    assert cal.calibration_weight_used == pytest.approx(1e-3)
    _primary, best_secondary = _true_lexicographic(spec)
    assert _secondary(p, cal) <= best_secondary + 1e-5 * max(1.0, abs(best_secondary))


class _PinRecorder:
    def __init__(self):
        self.bounds = {}

    def changeColIntegrality(self, i, _kind):
        pass

    def changeColBounds(self, i, lo, hi):
        self.bounds[i] = (lo, hi)


def _pin(x, binary_cols):
    h = _PinRecorder()
    var_array = list(range(len(x)))
    h.val = lambda i: x[i]
    _out, records = _captured(
        lambda: lp._pin_binaries_to_current_solution(h, var_array, binary_cols)
    )
    warned = [r for r in records if "away from integral" in r.getMessage()]
    return h.bounds, warned


def test_pinning_no_binaries_warns_of_nothing():
    bounds, warned = _pin([0.3, 0.7], [])
    assert bounds == {} and warned == []


@pytest.mark.parametrize(
    ("value", "warns"),
    [(1.0, False), (lp._BINARY_INTEGRALITY_TOLERANCE, False), (0.5, True)],
)
def test_pinning_warns_only_beyond_the_integrality_tolerance(value, warns):
    """At exactly the tolerance it is still integral enough; well beyond it,
    the rounding is the suspect and the WARNING says so. The pin itself is
    the rounded value either way."""
    bounds, warned = _pin([value, 0.0], [0])
    assert bool(warned) is warns
    assert bounds[0] == (float(round(value)), float(round(value)))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (math.inf, "mip_gap=inf"),
        (-math.inf, "mip_gap=-inf"),
        (math.nan, "mip_gap=nan"),
        (1e15, "mip_gap=1e+15"),
        (1e15 - 1.0, "mip_gap=999999999999999"),
        (0.25, "mip_gap=0.25"),
    ],
)
def test_the_diagnostic_line_survives_edge_values(value, expected):
    """HiGHS reports `mip_gap = inf` on a pure LP: the formatter must not
    raise on it, and a value at or past 1e15 prints as a float, not as a
    long integer."""
    assert lp.format_highs_info({"mip_gap": value}) == expected
