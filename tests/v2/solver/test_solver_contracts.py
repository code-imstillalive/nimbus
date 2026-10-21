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
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from hypothesis import given
from hypothesis import strategies as st
from solver.elements import PeriodGrid
from solver.lp import CalibratedOptions, LexOptions, LPProblem
from solver.network import _infeasible_plan
from test_lp_properties import _build, problems
from test_plan_invariants import SETTINGS

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
