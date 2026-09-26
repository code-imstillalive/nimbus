"""nimbus #1179, second half: the minimum-weight fallback must say WHY it fired,
because the two possible reasons point in opposite causal directions.

## What #1229 settled, and what it did not

#1229 made a failing cycle name *whether* it took the blend fallback, joining the
two log lines by cycle instead of by timestamp-second. That was the right first
move and it is not enough. Reading the calibrator shows why:

```python
def _primary_acceptable(log_w: float) -> bool:
    ...
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return False                                        # (a)
    ...
    return bl_primary_cost <= lex_primary_cost + abs_tol    # (b)
```

The fallback is entered when this returns False at **both** ends of the bracket,
and `False` means one of two completely different things:

**(a) the probe solve itself was non-optimal.** The model was already failing
before any weight was chosen. The blend collapse is then a **symptom** of the
failure, not its cause, and the blend warning and the solve failure are two
views of one event.

**(b) both probes solved fine**, but no weight in the bracket preserved the
primary cost within tolerance. A genuine calibration verdict — and the *only*
branch in which #1179's original hypothesis, that handing HiGHS `1e-12` is
itself what breaks the solve, is even available.

## Why this is the number that decides the issue

#1179's hypothesis assumes (b). Its own strongest counter-evidence, which it
honestly flags as unexplained, is that blend warnings accompanied only **44 of
153** failures. **(a) explains that directly**: the other 109 failed at a phase
that never reaches calibration at all.

So a single field separates "the collapsed weight broke the solve" from "the
solve was already broken and the collapse is how that looks from inside the
calibrator". Until it is recorded, the episode cannot be diagnosed either way —
and the regime reverted on its own and stayed clean for four nights, so a fix
cannot be validated by absence and an episode cannot be diagnosed
retrospectively if the instrumentation was not already there.

## Scope

Observability only. Nothing branches on the reason inside the solver, no solve
changes shape, and the calibration tolerance and minimum weight are untouched —
#1179 lists both under "Not proposed" because they re-price live dispatch, and
#999 and #773's own thread both record why that must not ship on a plausible
story.

## What was tried and did NOT work, so it is not repeated

An offline reproduction against the two real captured instances
(`nimbus_773_fail_phase2_secondary.mps`, 12,138 cols / 1,412 integer, and
`nimbus_773_slow_lex_phase_phase2_secondary.mps`), solving each at weights
`1e-12 … 1e0`. It is **not a valid test** and its result should not be cited:
the captures carry a single objective, not the primary/secondary split the blend
is built from, so standing in a synthetic primary made every weight report
`kUnboundedOrInfeasible` at 0 simplex iterations — including `w=1.0`. That
measures the synthetic objective, not the blend. Reproducing this offline needs a
capture that preserves both vectors, which is a separate piece of work.
"""

from __future__ import annotations

import ast
import dataclasses
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


class TestTheFieldExistsOnBothDataclasses(unittest.TestCase):
    """It has to survive the whole trip: LPResult -> Plan -> the log line."""

    def test_lpresult_defaults_to_None(self):
        """None, not "" and not a status string.

        Unlike the sibling bool -- where "did not take the fallback" is equally
        true whether calibration ran or not -- there is no honest reason to
        report when no fallback happened. None says that.
        """
        self.assertIsNone(getattr(lp_mod.LPResult(status="error"), FIELD, "missing"))

    def test_plan_mirrors_it_and_also_defaults_to_None(self):
        fields = {f.name: f for f in dataclasses.fields(net_mod.Plan)}
        self.assertIn(FIELD, fields, "Plan must mirror LPResult's own field")
        self.assertIsNone(fields[FIELD].default)

    def test_the_infeasible_plan_path_accepts_it(self):
        """`_infeasible_plan()` is the path a FAILED cycle actually takes --
        the only path where this field has any use at all. A field that
        reaches every plan except the failing ones would be useless."""
        params = inspect.signature(net_mod._infeasible_plan).parameters
        self.assertIn(FIELD, params)
        self.assertIsNone(params[FIELD].default)


class TestTheCalibratorDistinguishesTheTwoReasons(unittest.TestCase):
    """The substance: the source must actually branch on which probe outcome
    happened, not report one label for both."""

    def _calibrator_src(self) -> str:
        return textwrap.dedent(inspect.getsource(lp_mod._calibrate_blend_weight))

    def test_the_probe_records_a_non_optimal_status_separately(self):
        src = self._calibrator_src()
        self.assertIn(PROBE_NOT_OPTIMAL, src)
        self.assertIn(COST_NOT_PRESERVED, src)

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

    def test_the_calibrator_warning_reports_both_ends(self):
        """One label for the pair is not enough to audit: hi and lo can be
        rejected for different reasons, and that combination is itself a
        finding."""
        src = self._calibrator_src()
        i = src.find("no blend weight ")
        self.assertGreater(i, -1, "the calibrator warning moved or was renamed")
        window = src[i : i + 1600]
        self.assertIn("hi=%s", window)
        self.assertIn("lo=%s", window)
        self.assertIn("#1179", window)


class TestTheFailureLineExplainsWhatTheReasonMeans(unittest.TestCase):
    """A label nobody can interpret is not observability. Whoever reads this
    line at 03:00 during the next episode should not have to find this issue
    to know which way the causality runs."""

    def _window(self) -> str:
        from custom_components.nimbus_load import solver_writer

        src = inspect.getsource(solver_writer)
        i = src.find("solve did not complete after")
        assert i > -1, "the #757 failure warning moved or was renamed"
        return src[max(0, i - 3000) : i + 1400]

    def test_it_names_the_reason_field(self):
        self.assertIn(f"plan.{FIELD}", self._window())

    def test_it_spells_out_the_symptom_reading(self):
        window = self._window()
        self.assertIn(PROBE_NOT_OPTIMAL, window)
        self.assertIn("ALREADY failing", window)
        self.assertIn("symptom", window)

    def test_it_spells_out_the_cause_reading(self):
        window = self._window()
        self.assertIn(COST_NOT_PRESERVED, window)
        self.assertIn("CAUSE", window)

    def test_an_unrecorded_reason_does_not_read_as_a_real_one(self):
        """An older release's plan, or any path that did not set it, must not
        silently render as a blank or as one of the two real verdicts."""
        self.assertIn("unrecorded", self._window())


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
