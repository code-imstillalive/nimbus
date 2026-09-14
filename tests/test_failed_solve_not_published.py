"""nimbus issue #757: a failed solve must not overwrite a good published
plan.

#757 was filed as "a battery participant is silently excluded from the
solve" and sat open through ten separate investigations that disagreed
with each other. The premise was wrong. A live trace showed
`build_plan()` returning `batteries=['home','Test EV']` with
`status='optimal'` on all 24 of 24 cycles, while 7 of 10 actual publishes
in the same window carried `batteries=[]` with `status='error'`, each on
a different worker thread. Nothing was ever excluded -- failed solves
were overwriting good ones, and whoever looked next saw whichever write
happened to land last.

`_infeasible_plan()` builds a well-formed but entirely zero-filled Plan
for any non-optimal solve. Published, that is indistinguishable on a
dashboard from a real solve that genuinely decided to do nothing, which
is what made ten readings of the same system disagree.

Two layers, matching the split `test_solve_diagnostics_load_counts.py`
already uses for the same publish path:

1. **The status predicate**, tested directly against real `Plan` objects
   built by the real `_infeasible_plan()`. `solver_failed` must be true
   for "error" and false for every other status -- notably NOT
   `not is_optimal`, since "infeasible" is a real modelling answer the
   household needs to see and #773's own fallback depends on being
   published.
2. **The call site**, walked in `solver_writer.py`'s own AST, so the
   predicate can't stay correct while `publish_plan()` quietly stops
   returning early on it.
"""

from __future__ import annotations

import ast
import pathlib
import unittest
from datetime import UTC, datetime

import _solver_path
import numpy as np
from solver.elements import PeriodGrid
from solver.network import _infeasible_plan


def _plan(status: str, raw_status: str | None = None):
    periods = PeriodGrid(
        hours=np.array([0.5] * 4), start=datetime(2026, 1, 1, tzinfo=UTC)
    )
    return _infeasible_plan(periods, status, iterations=0, raw_status=raw_status)


class TestSolverFailedPredicate(unittest.TestCase):
    def test_error_is_the_only_failed_status(self):
        self.assertTrue(_plan("error", "Time limit reached").solver_failed)

    def test_infeasible_is_an_answer_not_a_failure(self):
        """The distinction the whole guard rests on. HiGHS PROVED no
        feasible dispatch exists -- that is a real result about the
        model, and #773's own fallback path depends on it reaching the
        published plan."""
        plan = _plan("infeasible")
        self.assertFalse(plan.solver_failed)
        self.assertFalse(plan.is_optimal)

    def test_unbounded_is_also_an_answer(self):
        self.assertFalse(_plan("unbounded").solver_failed)

    def test_optimal_is_not_a_failure(self):
        self.assertFalse(_plan("optimal").solver_failed)

    def test_it_is_not_merely_the_negation_of_is_optimal(self):
        """Written out explicitly because the tempting one-line
        implementation (`not self.is_optimal`) passes three of the four
        tests above and is wrong."""
        non_optimal = [_plan(s) for s in ("infeasible", "unbounded", "error")]
        self.assertEqual([p.is_optimal for p in non_optimal], [False, False, False])
        self.assertEqual([p.solver_failed for p in non_optimal], [False, False, True])

    def test_a_failed_plan_really_is_empty(self):
        """Why publishing one is harmful in the first place: it is not a
        partial plan, it is a confident-looking plan of zeros."""
        plan = _plan("error", "Time limit reached")
        self.assertEqual(plan.batteries, [])
        self.assertEqual(plan.sheddable_loads, [])
        self.assertEqual(plan.adequacy_loads, [])
        self.assertEqual(plan.thermal_loads, [])
        for arr in (
            plan.battery_charge_kw,
            plan.battery_discharge_kw,
            plan.grid_import_kw,
            plan.grid_export_kw,
        ):
            self.assertTrue(bool(np.all(arr == 0.0)))


def _publish_plan_node() -> ast.FunctionDef:
    src = pathlib.Path(
        _solver_path._SOLVER_PARENT  # type: ignore[attr-defined]
    ).joinpath("solver_writer.py")
    tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "publish_plan":
            return node
    raise AssertionError("publish_plan() not found in solver_writer.py")


class TestPublishPlanReturnsEarly(unittest.TestCase):
    """The predicate above can stay perfectly correct while the call site
    drifts back to publishing anyway -- this pins the call site."""

    def setUp(self):
        self.func = _publish_plan_node()
        self.guard = self._find_guard(self.func)

    @staticmethod
    def _find_guard(func: ast.FunctionDef) -> ast.If:
        """The `elif plan.solver_failed:` handler, found structurally
        rather than by line number."""
        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if (
                isinstance(test, ast.Attribute)
                and test.attr == "solver_failed"
                and isinstance(test.value, ast.Name)
                and test.value.id == "plan"
            ):
                return node
        raise AssertionError(
            "publish_plan() no longer guards on plan.solver_failed -- a "
            "failed solve's all-zero plan would overwrite the last good "
            "published one again (nimbus issue #757)"
        )

    def test_the_failed_branch_returns(self):
        self.assertTrue(
            any(isinstance(stmt, ast.Return) for stmt in self.guard.body),
            "the plan.solver_failed branch must return before publishing "
            "anything -- nimbus issue #757",
        )

    def test_it_returns_nothing_rather_than_a_value(self):
        """publish_plan() has no return value anywhere else; a `return
        something` here would be a new, unhandled contract."""
        for stmt in self.guard.body:
            if isinstance(stmt, ast.Return):
                self.assertIsNone(stmt.value)

    def test_the_guard_is_reached_before_any_publish_call(self):
        """Ordering is the whole point. A guard placed after the first
        ha_post_state() would be decoration."""
        posts = [
            node.lineno
            for node in ast.walk(self.func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ha_post_state"
        ]
        self.assertTrue(posts, "no ha_post_state() calls found in publish_plan()")
        self.assertLess(self.guard.lineno, min(posts))

    def test_the_operator_is_told_why_nothing_was_published(self):
        """Skipping silently would trade one invisible failure for
        another -- the branch must still log before it returns."""
        logged = [
            node
            for node in ast.walk(self.guard)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("warning", "error")
        ]
        self.assertTrue(
            logged,
            "the plan.solver_failed branch must log before returning -- "
            "an unexplained gap in the published plan is the failure mode "
            "nimbus issue #757 spent ten investigations on",
        )

    def test_infeasible_is_not_also_guarded_out(self):
        """A deliberate scope boundary, asserted so a later 'tidy-up'
        that folds the two statuses together fails here rather than
        silently hiding real infeasibility from the household."""
        for node in ast.walk(self.func):
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if not isinstance(test, ast.Compare):
                continue
            if not any(
                isinstance(c, ast.Constant) and c.value == "infeasible"
                for c in test.comparators
            ):
                continue
            self.assertFalse(
                any(isinstance(stmt, ast.Return) for stmt in node.body),
                "publish_plan() must still publish an infeasible plan -- "
                "that is a real modelling answer, not a solver failure",
            )


if __name__ == "__main__":
    unittest.main()
