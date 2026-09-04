"""Regression tests for nimbus issue #356 (Mark Purcell, codebase review),
item 1: `_solve_highs()` collapsed every non-optimal, non-infeasible,
non-unbounded HiGHS status (kTimeLimit, kIterationLimit, kSolutionLimit,
kUnknown, kUnboundedOrInfeasible, kModelError, kSolveError) into
`status="infeasible"` -- indistinguishable from a model HiGHS actually
proved has no feasible dispatch at all. These are genuine SOLVER-level
failures on a model whose real feasibility was never determined either
way, and are now reported as `status="error"` with HiGHS's own status
name preserved in `LPResult.raw_status`.

Also covers the accompanying fix: `_solve_highs()` never set a time
limit on the underlying `highspy.Highs()` instance at all, so a
genuinely pathological problem (e.g. a hard MIP) could block the calling
thread (HA's own shared executor pool, per solver_writer.py) indefinitely.

Directly forcing a REAL kTimeLimit/kIterationLimit/etc. from HiGHS is
slow and inherently flaky (it depends on HiGHS actually failing to
converge in time on whatever problem shape happens to be hard enough
today, on whatever machine runs this test) -- so the status-MAPPING
logic itself is tested by patching `highspy.Highs.getModelStatus` to
return each of the collapsed statuses directly on an otherwise-trivial,
genuinely solvable LP. This exercises the exact same code path
`_solve_highs()` runs for a real occurrence of each status, without
depending on HiGHS actually taking that path in real time.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import highspy
from solver import lp
from solver.lp import LPProblem


def _trivial_problem() -> LPProblem:
    p = LPProblem()
    p.add_variable("x", lb=0.0, ub=10.0)
    p.set_cost("x", -1.0)
    return p


class TestCollapsedHighsStatusesBecomeError(unittest.TestCase):
    def _assert_maps_to_error(self, highs_status) -> None:
        p = _trivial_problem()
        with patch.object(highspy.Highs, "getModelStatus", return_value=highs_status):
            result = p.solve()
        self.assertEqual(result.status, "error")
        self.assertIsNotNone(result.raw_status)
        # Never silently equal to "infeasible" -- that's precisely the bug
        # this fix removes.
        self.assertNotEqual(result.status, "infeasible")

    def test_time_limit_maps_to_error_not_infeasible(self):
        self._assert_maps_to_error(highspy.HighsModelStatus.kTimeLimit)

    def test_iteration_limit_maps_to_error_not_infeasible(self):
        self._assert_maps_to_error(highspy.HighsModelStatus.kIterationLimit)

    def test_solution_limit_maps_to_error_not_infeasible(self):
        self._assert_maps_to_error(highspy.HighsModelStatus.kSolutionLimit)

    def test_unknown_maps_to_error_not_infeasible(self):
        self._assert_maps_to_error(highspy.HighsModelStatus.kUnknown)

    def test_model_error_maps_to_error_not_infeasible(self):
        self._assert_maps_to_error(highspy.HighsModelStatus.kModelError)

    def test_solve_error_maps_to_error_not_infeasible(self):
        self._assert_maps_to_error(highspy.HighsModelStatus.kSolveError)

    def test_raw_status_names_the_real_highs_status(self):
        p = _trivial_problem()
        with patch.object(
            highspy.Highs,
            "getModelStatus",
            return_value=highspy.HighsModelStatus.kTimeLimit,
        ):
            result = p.solve()
        self.assertEqual(result.raw_status, "Time limit reached")


class TestGenuineInfeasibleAndUnboundedUnaffected(unittest.TestCase):
    """The fix must not touch the two REAL, already-correct outcomes --
    a genuinely infeasible or unbounded model still reports exactly that,
    not "error"."""

    def test_genuinely_infeasible_model_still_reports_infeasible(self):
        p = LPProblem()
        p.add_variable("x", lb=5.0, ub=10.0)
        p.add_ub_constraint({"x": 1.0}, 1.0)  # x <= 1, but lb=5 -- infeasible
        result = p.solve()
        self.assertEqual(result.status, "infeasible")
        self.assertIsNone(result.raw_status)

    def test_genuinely_unbounded_model_still_reports_unbounded(self):
        p = LPProblem()
        p.add_variable("x", lb=0.0, ub=float("inf"))
        p.set_cost("x", -1.0)  # minimize -x with no upper bound -- unbounded
        result = p.solve()
        self.assertEqual(result.status, "unbounded")
        self.assertIsNone(result.raw_status)

    def test_optimal_solve_has_no_raw_status(self):
        result = _trivial_problem().solve()
        self.assertEqual(result.status, "optimal")
        self.assertIsNone(result.raw_status)


class TestTimeLimitIsActuallySetOnTheHighsInstance(unittest.TestCase):
    """Mutation-style direct check: confirms `_solve_highs()` genuinely
    calls `setOptionValue("time_limit", ...)` on the real Highs instance,
    not just that `DEFAULT_TIME_LIMIT_SECONDS` exists as an unused
    constant somewhere in the module."""

    def test_time_limit_option_is_set_to_the_module_default(self):
        seen_calls: list[tuple[str, object]] = []
        real_set_option_value = highspy.Highs.setOptionValue

        def _spy(self, key, value):
            seen_calls.append((key, value))
            return real_set_option_value(self, key, value)

        with patch.object(highspy.Highs, "setOptionValue", _spy):
            _trivial_problem().solve()

        self.assertIn(("time_limit", lp.DEFAULT_TIME_LIMIT_SECONDS), seen_calls)
