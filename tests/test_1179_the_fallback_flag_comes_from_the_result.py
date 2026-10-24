"""nimbus #1179: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in tests/v2/solver/test_calibration_fallback.py."""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver import lp as lp_mod
from custom_components.nimbus_load.solver import network as net_mod
from custom_components.nimbus_load.solver.elements import PeriodGrid

FIELD = "calibration_min_weight_fallback"
_HOURS = np.full(4, 0.25)


class TestAFailingPlanCarriesIt(unittest.TestCase):
    """Property 3 -- the path a failed cycle actually takes."""

    def _grid(self) -> PeriodGrid:
        return PeriodGrid(hours=_HOURS)

    def test_the_solve_result_is_the_source(self):
        """The call site must read it off LPResult, not hardcode it."""
        src = inspect.getsource(net_mod._build_plan_once)
        self.assertIn(
            f"{FIELD}=result.{FIELD}",
            src,
            "the failing-plan path must carry LPResult's own value through",
        )


class TestASuccessfulPlanCarriesItToo(unittest.TestCase):
    """Property 4 -- the base rate, without which this proves nothing."""

    def test_the_optimal_plan_reads_it_off_the_result(self):
        src = inspect.getsource(net_mod._build_plan_once)
        self.assertEqual(
            src.count(f"{FIELD}=result.{FIELD}"),
            2,
            "both the optimal and the non-optimal Plan must carry it -- "
            "failures alone cannot establish whether the fallback is "
            "unusual",
        )


class TestNothingBranchesOnIt(unittest.TestCase):
    """Control: this is diagnostic. If the solve starts behaving
    differently depending on the flag, it has stopped being
    observability and #1179's own "Not proposed" line is being
    violated."""

    def test_no_control_flow_reads_the_flag_in_the_solver(self):
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
                    f"{mod.__name__} branches on the diagnostic flag",
                )
