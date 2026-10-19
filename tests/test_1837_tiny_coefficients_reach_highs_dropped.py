"""#1837, and HAEO #501/#511 (Mark): HiGHS drops a matrix entry at or below
its `small_matrix_value` (1e-9) with a warning, and highspy 1.15's
`addConstr` raises on that warning after adding the row. A single
float-noise coefficient in any row -- a model row, or the lexicographic bound
rows built from the cost vectors -- made a solve raise, or fall back.

Rows now go in through `addRow` with those entries dropped first, and the
bound rows shift their right-hand side by what was dropped, so the phased
solve runs and keeps its tie-breaks."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from solver import lp
from solver.lp import CalibratedOptions, LexOptions, LPProblem


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    # A fallback would start the module's lex cooldown; keep each case fresh.
    monkeypatch.setattr(lp, "_lex_calibration_failed_until", 0.0)


@pytest.mark.parametrize("options", [LexOptions(), CalibratedOptions()])
@pytest.mark.parametrize("tiny", [-1e-9, -1e-10, 1e-11, 5e-10])
def test_a_tiny_cost_beside_real_ones_takes_the_phased_solve(options, tiny):
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=1.0, cost=-1.0)
    p.add_variable("y", lb=0.0, ub=1000.0, cost=tiny)
    p.set_secondary_cost("x", 1.0)
    p.set_secondary_cost("y", 1e-3)
    result = p.solve(options=options)
    assert result.status == "optimal"
    if isinstance(options, CalibratedOptions):
        # It ran the phased solve rather than falling back.
        assert result.calibration_weight_used is not None
    plain = p.solve()
    assert p.primary_cost_of(result) <= plain.objective + 1e-7 + 1e-12


@pytest.mark.parametrize("options", [LexOptions(), CalibratedOptions()])
def test_an_all_tiny_primary_still_solves(options):
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=1.0, cost=-1e-12)
    p.set_secondary_cost("x", 1.0)
    result = p.solve(options=options)
    assert result.status == "optimal"


def test_a_tiny_coefficient_in_a_model_row_is_dropped_not_raised():
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=2.0, cost=-1.0)
    p.add_variable("y", lb=0.0, ub=1.0, cost=0.0)
    p.add_ub_constraint({"x": 1.0, "y": 1e-11}, 1.0)
    result = p.solve()
    assert result.status == "optimal"
    assert p.value_of(result, "x") == pytest.approx(1.0)


def test_a_row_whose_every_coefficient_is_dropped_keeps_its_bounds():
    """0 == 1 once the tiny term is dropped: still infeasible, as HiGHS would
    have built it, never silently satisfied."""
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=1.0, cost=1.0)
    p.add_eq_constraint({"x": 1e-12}, 1.0)
    assert p.solve().status != "optimal"
