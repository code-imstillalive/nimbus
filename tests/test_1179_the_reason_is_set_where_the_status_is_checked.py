"""nimbus #1179: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in tests/v2/solver/test_fallback_reason.py."""

from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver import lp as lp_mod
from custom_components.nimbus_load.solver import network as net_mod

FIELD = "calibration_fallback_reason"
PROBE_NOT_OPTIMAL = "probe_not_optimal"
COST_NOT_PRESERVED = "cost_not_preserved"


class TestTheCalibratorDistinguishesTheTwoReasons(unittest.TestCase):
    """The substance: the source must actually branch on which probe outcome
    happened, not report one label for both."""

    def _calibrator_src(self) -> str:
        return textwrap.dedent(inspect.getsource(lp_mod._calibrate_blend_weight))

    def test_the_non_optimal_label_is_set_where_the_status_is_checked(self):
        """Pinned structurally rather than by eyeballing the strings: the
        `probe_not_optimal` label must be assigned inside the branch that tests
        the model status, or the two reasons could be swapped without any test
        noticing."""
        tree = ast.parse(self._calibrator_src())
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.unparse(node.test)
            if "getModelStatus" not in test_src:
                continue
            body_src = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            if PROBE_NOT_OPTIMAL in body_src:
                found = True
        self.assertTrue(
            found,
            "`probe_not_optimal` must be recorded in the branch that finds the "
            "probe solve non-optimal -- that is the whole distinction",
        )

    def test_probe_not_optimal_wins_when_either_end_reports_it(self):
        """Asymmetric on purpose. If EITHER end of the bracket failed to solve,
        the model's own health is in question and that is the more important
        fact -- reporting `cost_not_preserved` because the other end happened to
        reach the cost comparison would bury it.

        Pinned through the AST rather than as a source string: the first version
        of this test matched the ternary's exact text and broke the moment the
        formatter reflowed it across lines, reporting a formatting change as a
        missing feature.
        """
        tree = ast.parse(self._calibrator_src())
        chosen = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.IfExp)
            and isinstance(n.body, ast.Constant)
            and n.body.value == PROBE_NOT_OPTIMAL
            and isinstance(n.orelse, ast.Constant)
            and n.orelse.value == COST_NOT_PRESERVED
        ]
        self.assertEqual(
            len(chosen),
            1,
            "expected exactly one conditional choosing probe_not_optimal over "
            "cost_not_preserved",
        )
        test_src = ast.unparse(chosen[0].test)
        self.assertIn(PROBE_NOT_OPTIMAL, test_src)
        self.assertIn("in reasons", test_src, "decided by membership across BOTH ends")


class TestTheSolverStillDoesNotBranchOnAnyOfThis(unittest.TestCase):
    """Control, and the reason #1179 says "observability only": if the solve
    starts behaving differently depending on these diagnostics, the issue's own
    "Not proposed" line is being violated.

    Scoped to the solver modules. The publisher legitimately branches on the
    reason -- to choose the words of a log message, which changes no dispatch.
    """

    def test_no_control_flow_in_the_solver_reads_the_reason(self):
        for mod in (lp_mod, net_mod):
            tree = ast.parse(inspect.getsource(mod))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.If, ast.While)):
                    continue
                names = {
                    n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)
                } | {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
                self.assertNotIn(
                    FIELD,
                    names,
                    f"{mod.__name__}: the solve must not depend on a diagnostic",
                )
