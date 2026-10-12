"""nimbus #1360: an Unknown HiGHS status gets one retry before the previous
plan is kept (a tester's install, 11 Oct 2026: 169 Unknowns in three hours
with a $23/kWh spike in the horizon). A plain solve is re-run with the
interior-point solver; a calibrated/lex solve falls back to the plain solve.
"""

from __future__ import annotations

import _solver_path  # noqa: F401
import highspy
from solver import lp


class _FakeHighs:
    def __init__(self, after_retry):
        self.options = {}
        self.calls = []
        self._after = after_retry

    def setOptionValue(self, k, v):
        self.options[k] = v
        self.calls.append(("set", k, v))

    def clearSolver(self):
        self.calls.append(("clear",))

    def run(self):
        self.calls.append(("run", self.options.get("solver")))

    def getModelStatus(self):
        return self._after

    def getInfo(self):
        return type("I", (), {"simplex_iteration_count": 0})()


def test_the_retry_runs_ipm_once_and_restores_the_solver_option():
    h = _FakeHighs(highspy.HighsModelStatus.kOptimal)
    status = lp._retry_unknown_with_ipm(h)
    assert status == highspy.HighsModelStatus.kOptimal
    assert ("run", "ipm") in h.calls
    assert h.options["solver"] == "choose"  # restored


def test_a_failed_retry_still_restores_the_option():
    h = _FakeHighs(highspy.HighsModelStatus.kUnknown)
    assert lp._retry_unknown_with_ipm(h) == highspy.HighsModelStatus.kUnknown
    assert h.options["solver"] == "choose"


def test_the_retry_is_wired_before_the_error_result():
    from pathlib import Path

    text = Path(lp.__file__).read_text(encoding="utf-8")
    at = text.index("status = h.getModelStatus()\n    # nimbus #1360")
    block = text[at : at + 2000]
    assert "highspy.HighsModelStatus.kUnknown and not binary_cols" in block
    assert "options is not None" in block
    assert "_solve_highs(" in block and "options=None" in block
    assert "_retry_unknown_with_ipm(h)" in block


def test_a_real_lp_still_solves_unchanged():
    """The retry path is not taken on an ordinary optimal solve."""
    p = lp.LPProblem()
    p.add_variable("x", lb=1.0, ub=10.0, cost=1.0)
    res = p.solve()
    assert res.status == "optimal"
