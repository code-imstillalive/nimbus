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
        r = lp_mod.LPResult(status="error")
        self.assertIs(getattr(r, FIELD, None), False)

    def test_plan_defaults_to_false_not_none(self):
        f = {x.name: x for x in dataclasses.fields(net_mod.Plan)}
        self.assertIn(FIELD, f, "Plan must mirror LPResult's own field")
        self.assertIs(f[FIELD].default, False)


class TestEveryReturnPropagatesIt(unittest.TestCase):
    """Property 2 -- parsed, not eyeballed."""

    def test_calibrate_reports_three_values(self):
        ann = str(inspect.signature(lp_mod._calibrate_blend_weight).return_annotation)
        self.assertIn("bool", ann)

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

    def test_every_solve_with_options_return_carries_all_three_diagnostics(self):
        """5, not 4, since #1179's third half: the flag, the reason, AND the
        blend weight actually used.

        This assertion is the reason the weight change was safe to make. It
        failed the moment the caller was widened to unpack five while two
        early-exit returns still yielded four -- the non-calibrated path and
        the lex-restore path, neither of which runs a calibration. Both now
        return None for the weight, matching the two sibling fields' own
        "no calibration ran" posture.

        A signature change nobody propagated is not a fix -- exactly how the
        early-exit return in `_calibrate_blend_weight` was missed the first
        time (see the test below).
        """
        arities = self._return_arities(lp_mod._solve_with_options)
        self.assertTrue(arities, "no returns found -- test is not testing anything")
        self.assertEqual(
            set(arities),
            {5},
            f"every return must carry the flag, the reason AND the weight; "
            f"got {arities}",
        )

    def test_every_calibrate_return_carries_both_diagnostics(self):
        """The one CI caught and these tests originally did not.

        `_calibrate_blend_weight` has TWO outer returns: the main path,
        and an early exit for "no primary cost to distort" which picks a
        safe default weight instead of calibrating. Only the first was
        updated, and mypy found the second -- so the arity check now
        covers this function too, not just its caller.
        """
        arities = self._return_arities(lp_mod._calibrate_blend_weight)
        self.assertTrue(arities, "no returns found -- test is not testing anything")
        self.assertEqual(
            set(arities),
            {4},
            f"every outer return must carry the flag AND the reason; got {arities}",
        )


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
        window = self._window()
        self.assertIn("#1179", window, "the warning must cite the issue")
        self.assertIn(
            f"plan.{FIELD}",
            window,
            "the warning must read the flag, not just mention it",
        )
        self.assertIn("minimum weight", window)

    def test_it_distinguishes_both_outcomes(self):
        """A line that only speaks up on fallback cannot be told apart
        from a line that failed to fire at all."""
        window = self._window()
        self.assertIn("FELL BACK", window)
        self.assertIn("no minimum-weight fallback", window)


if __name__ == "__main__":
    unittest.main()
