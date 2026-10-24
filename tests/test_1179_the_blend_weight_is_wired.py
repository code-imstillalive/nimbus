"""nimbus #1179: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in tests/v2/solver/test_blend_weight_result.py."""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver import lp as lp_mod


class TestTheWeightIsNoLongerDiscarded(unittest.TestCase):
    """The actual defect: the value existed and was thrown away."""

    def test_the_caller_no_longer_unpacks_it_into_underscore(self):
        src = inspect.getsource(lp_mod._solve_with_options)
        # Anchor on the ASSIGNMENT, not the first mention of the name -- the
        # function's docstring references the helper several paragraphs
        # before the call, and an earlier version of this test matched that
        # and read 400 characters of prose instead.
        i = src.index(") = _calibrate_blend_weight(")
        window = src[max(0, i - 500) : i]
        self.assertIn("calibration_weight_used", window)
        self.assertNotIn("        _,\n", window)

    def test_calibrate_still_returns_the_weight_first(self):
        """The contract this depends on. If the weight ever stopped being
        element 0, the caller would silently bind the wrong number."""
        src = inspect.getsource(lp_mod._calibrate_blend_weight)
        returns = [
            node
            for node in ast.walk(ast.parse(src.strip()))
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
        ]
        self.assertTrue(returns)
        for r in returns:
            with self.subTest(line=r.lineno):
                first = r.value.elts[0]
                name = getattr(first, "id", None)
                self.assertIn(
                    name,
                    ("weight", "_DEFAULT_BLEND_WEIGHT", None),
                    "element 0 of every return must still be the weight",
                )


class TestEveryConstructionSiteCarriesIt(unittest.TestCase):
    def test_no_LPResult_sets_the_fallback_flag_without_the_weight(self):
        """A construction that carries the flag but not the weight is the
        shape of a half-propagated change -- the same class the arity guard
        catches on the return side."""
        src = Path(lp_mod.__file__).read_text(encoding="utf-8")
        flag = "calibration_min_weight_fallback=calibration_min_weight_fallback"
        weight = "calibration_weight_used=calibration_weight_used"
        self.assertEqual(
            src.count(flag),
            src.count(weight),
            "every LPResult carrying the fallback flag must also carry the "
            "weight (#1179)",
        )


class TestTheSolveIsUnchanged(unittest.TestCase):
    """#1179 explicitly rules out re-pricing live dispatch on an unproven
    mechanism. This change must be diagnostic only."""

    def test_the_minimum_weight_constant_is_untouched(self):
        src = Path(lp_mod.__file__).read_text(encoding="utf-8")
        self.assertIn("1e-12", src, "the minimum-weight fallback still exists")

    def test_nothing_branches_on_the_new_field(self):
        """If any code READ this field to make a decision, it would stop
        being instrumentation and start being a behaviour change."""
        src = Path(lp_mod.__file__).read_text(encoding="utf-8")
        reads = [
            ln
            for ln in src.splitlines()
            if "calibration_weight_used" in ln
            and (
                "if " in ln or "while " in ln or "return calibration_weight_used" in ln
            )
        ]
        self.assertEqual(reads, [], f"nothing may branch on it; found {reads}")
