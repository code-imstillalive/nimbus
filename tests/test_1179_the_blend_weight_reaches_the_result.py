"""nimbus issue #1179: the blend weight actually used reaches `LPResult`.

## What the issue asked for, verbatim

> The elapsed time and `raw_status` are already logged. What is not logged, and
> would decide this, is whether the *failing* solves are the ones that took the
> minimum-weight fallback — i.e. carrying the blend weight actually used into
> the failure warning [...] **Today they are correlated only by second.**

`_calibrate_blend_weight()` has always returned that weight as its first
element, and its caller discarded it with a bare `_`. So the one number needed
to answer the question has existed the whole time and never reached the result.

## What it makes answerable

The 20 Sep episode: **153 solve failures, 44 blend warnings, 37 of the 44
sharing a timestamp-second with a failure**. That is a correlation between two
log lines and a clock. With the weight on the result, a failed cycle states the
weight it was handed — so "were the failing solves the ones given `1e-12`"
becomes a field you read rather than an alignment you infer.

## What this deliberately does NOT do

The issue is explicit:

> **Not proposed:** Changing the blend tolerance or the minimum weight. Both
> re-price live dispatch on an install that is already 20 releases behind, and
> the mechanism is not established.

Nothing here changes a tolerance, a weight, or a solve. `TestTheSolveIsUnchanged`
pins that on the source.

## Why it cannot be validated by absence

The episode ran 2.7 hours on 20 Sep and has not recurred. An intermittent
defect's instrumentation cannot be confirmed by a quiet night — it has to
already be in place when the episode returns. Same argument
`calibration_fallback_reason` shipped on.

## The bug this nearly shipped with

Widening the caller to unpack five while two early-exit returns still yielded
four would have crashed on exactly the paths that do not calibrate. The existing
arity guard in `test_1179_failure_names_its_calibration_fallback.py` caught it,
which is why that test exists; this file does not duplicate it.
"""

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


class TestTheFieldExists(unittest.TestCase):
    def test_lpresult_carries_the_weight(self):
        self.assertIn("calibration_weight_used", lp_mod.LPResult.__dataclass_fields__)

    def test_it_defaults_to_None(self):
        """None means "no calibration ran", the same posture as the two
        sibling #1179 fields. A default of 0.0 or 1.0 would be a claim."""
        field = lp_mod.LPResult.__dataclass_fields__["calibration_weight_used"]
        self.assertIsNone(field.default)

    def test_it_sits_beside_its_two_siblings(self):
        """Grouped deliberately -- all three are #1179 diagnostics and are
        read together when an episode recurs."""
        names = list(lp_mod.LPResult.__dataclass_fields__)
        i = names.index("calibration_weight_used")
        neighbourhood = names[max(0, i - 3) : i + 1]
        self.assertIn("calibration_min_weight_fallback", neighbourhood)
        self.assertIn("calibration_fallback_reason", neighbourhood)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
