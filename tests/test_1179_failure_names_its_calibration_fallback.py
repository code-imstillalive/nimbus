"""nimbus #1179: a failing solve cycle names its own blend-calibration
fallback, instead of leaving that joinable only by timestamp-second.

**What went unmeasurable.** On 2026-09-20 the reference household
produced **153 failed solve cycles in 2.7 hours** inside the P2P
window, alongside **44** "no blend weight preserves primary cost ...
using minimum weight 1.00e-12" warnings -- and **37 of those 44 shared
a timestamp-SECOND with a failure**. That is a correlation by clock,
not by cycle. Nothing tied the two log lines to the same solve, so the
blend-collapse hypothesis could be neither confirmed nor dropped:
blend warnings accompanied only 44 of the 153 failures, which is
equally consistent with "one of several routes to the same failure"
and with "the warning is logged on a subset of the cycles that take
it".

The issue named the remedy itself: "carrying the blend weight actually
used into the failure warning, or logging the two from the same cycle
with a shared identifier."

**Why this has to land before the next episode.** The regime reverted
on its own and stayed clean for four nights (#1179's own follow-up),
so it is intermittent on a multi-night scale. A fix cannot be
validated by absence, and an episode cannot be diagnosed
retrospectively if the instrumentation was not already there.

**Observability only.** Nothing branches on the new flag and no solve
changes shape. Deliberately NOT touching the calibration tolerance or
the minimum weight -- #1179 lists both under "Not proposed", because
they re-price live dispatch.

The properties pinned here:

1. **The flag exists on both dataclasses and defaults to False** --
   not None. "Did not take the fallback" is the honest answer both
   when calibration ran and found a weight, and when no calibration
   ran at all.
2. **Every `_solve_with_options` return carries it.** A signature
   change nobody propagated is not a fix.
3. **A failing plan carries it** through `_infeasible_plan`, which is
   the path a failed cycle actually takes.
4. **A successful plan carries it too** -- without a base rate among
   healthy cycles, "the failures took the fallback" is unfalsifiable.
   If every cycle takes it, it explains nothing.
5. **The failure log line says which.** A field nothing reports is not
   observability.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import sys
import textwrap
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

    def test_default_is_false(self):
        p = net_mod._infeasible_plan(self._grid(), "error", 0, raw_status="kUnknown")
        self.assertIs(getattr(p, FIELD), False)

    def test_it_is_carried_when_set(self):
        p = net_mod._infeasible_plan(
            self._grid(),
            "error",
            0,
            raw_status="kUnknown",
            **{FIELD: True},
        )
        self.assertIs(getattr(p, FIELD), True)

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


class TestTheFailureLogSaysWhich(unittest.TestCase):
    """Property 5 -- a field nothing reports is not observability."""

    def _window(self) -> str:
        from custom_components.nimbus_load import solver_writer

        src = inspect.getsource(solver_writer)
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


if __name__ == "__main__":
    unittest.main()
