"""A failed solve never overwrites a good published plan (#757) (#1837)."""

from __future__ import annotations

import ast
import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401 -- sys.path setup side effect
import numpy as np
from _writer_source import find_function
from solver.elements import PeriodGrid
from solver.network import _infeasible_plan


def _plan(status: str, raw_status: str | None = None):
    periods = PeriodGrid(
        hours=np.array([0.5] * 4), start=datetime(2026, 1, 1, tzinfo=UTC)
    )
    return _infeasible_plan(periods, status, iterations=0, raw_status=raw_status)


def _publish_plan_node() -> ast.FunctionDef:
    # nimbus #1304 (spec 005): this content moved to solver_publish.py.
    # The union of solver_writer.py and every module #1298 has extracted is
    # what this test was always asserting against -- not which file it is in.
    return find_function("publish_plan")


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
        # test_the_failed_branch_returns
        self.assertTrue(
            any(isinstance(stmt, ast.Return) for stmt in self.guard.body),
            "the plan.solver_failed branch must return before publishing "
            "anything -- nimbus issue #757",
        )

        # test_it_returns_nothing_rather_than_a_value: publish_plan() has no return value anywhere else; a `return
        for stmt in self.guard.body:
            if isinstance(stmt, ast.Return):
                self.assertIsNone(stmt.value)

        # test_the_guard_is_reached_before_any_publish_call: Ordering is the whole point. A guard placed after the first
        posts = [
            node.lineno
            for node in ast.walk(self.func)
            if isinstance(node, ast.Call)
            # nimbus #1304 (spec 005): publish_plan moved to solver_publish.py
            # and reaches this through the deferred seam as `sw.ha_post_state`,
            # so a bare-Name matcher finds nothing. Both shapes count -- which
            # is the property this test means, not the syntax of the call.
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "ha_post_state")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "ha_post_state"
                )
            )
        ]
        self.assertTrue(posts, "no ha_post_state() calls found in publish_plan()")
        self.assertLess(self.guard.lineno, min(posts))

        # test_the_operator_is_told_why_nothing_was_published: Skipping silently would trade one invisible failure for
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

        # test_infeasible_is_not_also_guarded_out: A deliberate scope boundary, asserted so a later 'tidy-up'
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
