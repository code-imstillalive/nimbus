"""A failing solve names its own blend-calibration fallback (#1179) (#1837)."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import sys
import textwrap
import unittest
from pathlib import Path

import numpy as np
from solver import lp as _solver_lp

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver import lp as lp_mod
from custom_components.nimbus_load.solver import network as net_mod
from custom_components.nimbus_load.solver.elements import PeriodGrid

FIELD = "calibration_min_weight_fallback"
_HOURS = np.full(4, 0.25)


class TestTheFlagExistsAndDefaultsHonestly(unittest.TestCase):
    """Property 1."""

    def test_lpresult_defaults_to_false_not_none(self):
        # test_lpresult_defaults_to_false_not_none
        _solver_lp._lex_calibration_failed_until = 0.0
        r = lp_mod.LPResult(status="error")
        self.assertIs(getattr(r, FIELD, None), False)

        # test_plan_defaults_to_false_not_none
        _solver_lp._lex_calibration_failed_until = 0.0
        f = {x.name: x for x in dataclasses.fields(net_mod.Plan)}
        self.assertIn(FIELD, f, "Plan must mirror LPResult's own field")
        self.assertIs(f[FIELD].default, False)


class TestEveryReturnPropagatesIt(unittest.TestCase):
    """Property 2 -- parsed, not eyeballed."""

    def test_calibrate_reports_three_values(self):
        # test_calibrate_reports_three_values
        _solver_lp._lex_calibration_failed_until = 0.0
        ann = str(inspect.signature(lp_mod._calibrate_blend_weight).return_annotation)
        self.assertIn("bool", ann)

        # test_every_solve_with_options_return_carries_all_three_diagnostics: 5, not 4, since #1179's third half: the flag, the reason, AND the
        _solver_lp._lex_calibration_failed_until = 0.0
        arities = self._return_arities(lp_mod._solve_with_options)
        self.assertTrue(arities, "no returns found -- test is not testing anything")
        self.assertEqual(
            set(arities),
            {5},
            f"every return must carry the flag, the reason AND the weight; "
            f"got {arities}",
        )

        # test_every_calibrate_return_carries_both_diagnostics: The one CI caught and these tests originally did not.
        _solver_lp._lex_calibration_failed_until = 0.0
        arities = self._return_arities(lp_mod._calibrate_blend_weight)
        self.assertTrue(arities, "no returns found -- test is not testing anything")
        self.assertEqual(
            set(arities),
            {4},
            f"every outer return must carry the flag AND the reason; got {arities}",
        )

    def _return_arities(self, fn_obj) -> list[int]:
        """Arity of every `return <tuple>` in a function's OWN body.

        Nested helpers are excluded -- `_calibrate_blend_weight` contains
        a probe closure with its own 2-tuple return, and counting that
        would make this assert something false.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn_obj))).body[0]
        nested = {
            n
            for child in ast.iter_child_nodes(tree)
            for n in ast.walk(child)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        return [
            len(n.value.elts) if isinstance(n.value, ast.Tuple) else 1
            for n in ast.walk(tree)
            if isinstance(n, ast.Return) and n.value is not None and n not in nested
        ]


class TestAFailingPlanCarriesIt(unittest.TestCase):
    """Property 3 -- the path a failed cycle actually takes."""

    def _grid(self) -> PeriodGrid:
        return PeriodGrid(hours=_HOURS)


class TestTheFailureLogSaysWhich(unittest.TestCase):
    """Property 5 -- a field nothing reports is not observability."""

    def _window(self) -> str:
        # nimbus #1304 (spec 005): the #757 failure warning moved to
        # solver_publish.py with publish_plan. writer_source() is the union
        # across solver_writer.py and every module #1298 has extracted -- the
        # claim is "the writer warns", not "one file contains the string".
        from _writer_source import writer_source

        src = writer_source()
        i = src.find("solve did not complete after")
        assert i > -1, "the #757 failure warning moved or was renamed"
        # Deliberately looks BACKWARDS as well. Since #1179's second half the
        # message is assembled into `calibration_note` above the _LOGGER call
        # -- a forward-only window silently stopped seeing the very text these
        # tests exist to pin, and reported it as the feature being absent.
        return src[max(0, i - 3000) : i + 1400]

    def test_the_warning_reports_the_fallback(self):
        # test_the_warning_reports_the_fallback
        _solver_lp._lex_calibration_failed_until = 0.0
        window = self._window()
        self.assertIn("#1179", window, "the warning must cite the issue")
        self.assertIn(
            f"plan.{FIELD}",
            window,
            "the warning must read the flag, not just mention it",
        )
        self.assertIn("minimum weight", window)

        # test_it_distinguishes_both_outcomes: A line that only speaks up on fallback cannot be told apart
        _solver_lp._lex_calibration_failed_until = 0.0
        window = self._window()
        self.assertIn("FELL BACK", window)
        self.assertIn("no minimum-weight fallback", window)


if __name__ == "__main__":
    unittest.main()
