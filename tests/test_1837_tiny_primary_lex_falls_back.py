"""#1837: a lexicographic or calibrated solve whose primary costs all sit at
or below HiGHS's smallest matrix value used to raise a bare Exception from
the phase-1 bound row. It now takes the same plain single-objective fallback
as every other phase failure, and returns the primary optimum."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from solver import lp
from solver.lp import CalibratedOptions, LexOptions, LPProblem


def _problem(primary: float) -> LPProblem:
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=1.0, cost=primary)
    p.set_secondary_cost("x", 1.0)
    return p


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    # A fallback starts the module's lex cooldown; keep each case fresh.
    monkeypatch.setattr(lp, "_lex_calibration_failed_until", 0.0)


@pytest.mark.parametrize("options", [LexOptions(), CalibratedOptions()])
@pytest.mark.parametrize("primary", [-1e-9, -1e-10, -1e-12])
def test_a_tiny_primary_falls_back_instead_of_raising(options, primary):
    p = _problem(primary)
    result = p.solve(options=options)
    assert result.status == "optimal"
    # It took the plain fallback: no calibrated weight was used.
    assert result.calibration_weight_used is None
    # A cost this small is below HiGHS's own cost tolerance, so any x in
    # [0, 1] is optimal to its precision: the primary is within the lex
    # tie slack (1e-7) of its optimum, `primary`.
    assert p.primary_cost_of(result) <= primary + lp._LEX_PRIMARY_TIE_ABS_SLACK


def test_an_ordinary_primary_still_takes_the_phased_solve():
    p = _problem(-1.0)
    result = p.solve(options=CalibratedOptions())
    assert result.status == "optimal"
    assert result.calibration_weight_used is not None
