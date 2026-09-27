"""A scalar `period_hours` must not crash the participant departure deadline.

## The defect (nimbus issue #1336, found by Mark Purcell)

`_resolve_battery_participant_history()` is typed `period_hours: float` and passes
the scalar it computed (`TIER1_PERIOD_HOURS`/`TIER2_PERIOD_HOURS`) straight into
`_participant_departure_deadline()`, which was typed `period_hours: list` and did
`float(period_hours[t])`. Reproduced exactly:

    TypeError: object of type 'float' has no len()

## What it cost, which is not what the issue assumed

The issue left one question open — *"need to confirm on read whether it is inside
or outside that block"* — because the answer decides the impact. **It is inside.**
Determined from the AST: the call at line 682 sits in the `try` spanning 450-787
whose handler is `except (ValueError, TypeError)`. A `TypeError` is caught.

So it never took down the day's report. What it did instead is worse in a
specific way: the handler logs

    "battery participant '%s' could not be scored for window [...] --
     excluded from this day's multi-battery score"

and `continue`s. The participant is **silently dropped from the scored fleet**,
and the warning attributes it to the household's own config. Anyone reading that
line would go and check their config, which was fine. The EPR, regret and
`scored_participants` then describe a smaller fleet than the one configured, with
no indication that anything is wrong. Nothing looks broken.

## Why it survived

Two blind spots, and the second is the interesting one.

1. **Every direct test passes a real list.** `test_1111`'s fixture is
   `PERIOD_HOURS = [1.0] * 24`, so the scalar shape the only real caller uses is
   never exercised.
2. **No test calls `_resolve_battery_participant_history()` at all.** Checked
   across the suite: every existing test of it asserts on
   `inspect.getsource()` — `test_1109`, `test_1111` and `test_1247` all read its
   source text. Source-text coverage cannot see an argument shape, so a function
   can be heavily "tested" and have zero execution coverage of the path that
   breaks.

## The `idx > 0` trap, which this file must not fall into

The loop that indexes `period_hours` is
`for t in range(min(idx, len(actual_charge_kw)))`, so **at `idx == 0` it never
runs and the bug is invisible**. The issue notes #1310's fixture deliberately
keeps its departure-deadline scenario at `idx == 0` for exactly that reason.

A regression test for this that happened to resolve to index 0 would pass on the
broken code. `test_the_scenario_actually_reaches_the_indexing_loop` below asserts
the index is greater than zero, so this file cannot pass for the wrong reason.
"""

from __future__ import annotations

import ast
import inspect
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _solver_path  # noqa: F401
from solver_inputs import battery_participants as bp

#: A UTC-stamped window, matching `test_1111`'s own reasoning: `_local()` is what
#: the live path uses, so a UTC window proves the timezone conversion is not
#: dropped. Brisbane is UTC+10, so local hour 15 is index 5 here -- deliberately
#: NOT index 0, see the module docstring.
START = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
GRID_TIMES = [START + timedelta(hours=i) for i in range(24)]
DEPARTURE_HOUR = 15
CAPACITY_KWH = 60.0
INITIAL_SOC_KWH = 20.0
REQUIRED_PCT = 80.0
EFFICIENCY = 0.95


def _deadline(period_hours):
    return bp._participant_departure_deadline(
        grid_times=GRID_TIMES,
        period_hours=period_hours,
        departure_hour=DEPARTURE_HOUR,
        must_have_soc_pct=REQUIRED_PCT,
        capacity_kwh=CAPACITY_KWH,
        initial_soc_kwh=INITIAL_SOC_KWH,
        actual_charge_kw=np.array([1.0] * 24),
        actual_discharge_kw=np.zeros(24),
        efficiency=EFFICIENCY,
    )


class TestEveryShapeTheCallersActuallyUse(unittest.TestCase):
    """The scalar is what the real call site passes; the list is what every
    existing test passes. Both must work, and they must agree."""

    def test_a_scalar_does_not_crash(self):
        idx, kwh = _deadline(1.0)
        self.assertIsNotNone(idx)
        self.assertIsNotNone(kwh)

    def test_the_three_shapes_agree_exactly(self):
        """Not "all three work" -- all three give the SAME answer. A
        normalisation that quietly changed the number would pass a
        does-not-crash test and corrupt the oracle's constraint."""
        scalar = _deadline(1.0)
        listed = _deadline([1.0] * 24)
        array = _deadline(np.full(24, 1.0))
        self.assertEqual(scalar, listed)
        self.assertEqual(scalar, tuple(array))

    def test_a_non_uniform_sequence_is_still_honoured(self):
        """The normalisation must apply ONLY to a scalar. Broadcasting a real
        per-period sequence would silently flatten a tiered horizon, which is
        the opposite of the bug and harder to see."""
        uniform = _deadline([1.0] * 24)
        tiered = _deadline([0.5] * 12 + [1.0] * 12)
        self.assertNotEqual(
            uniform,
            tiered,
            "a genuinely non-uniform cadence must produce a different achieved "
            "SoC than a uniform one -- if these match, the sequence is being "
            "overwritten",
        )


class TestTheScenarioIsNotVacuous(unittest.TestCase):
    """The guard this file needs most. See the module docstring's `idx > 0` note."""

    def test_the_scenario_actually_reaches_the_indexing_loop(self):
        idx, _kwh = _deadline([1.0] * 24)
        self.assertIsNotNone(idx)
        self.assertGreater(
            idx,
            0,
            "at idx == 0 the loop that indexes period_hours never runs, so a "
            "scalar would not crash and this whole file would pass against the "
            "unfixed code (nimbus #1336)",
        )

    def test_the_departure_hour_resolves_through_local_time(self):
        """Pins the premise of the index above: 15:00 Brisbane against a
        UTC-stamped window is index 5, not 15."""
        idx, _kwh = _deadline([1.0] * 24)
        self.assertEqual(idx, 5)


class TestTheNormalisationLivesInTheCallee(unittest.TestCase):
    """Where the fix sits decides whether it can be reintroduced.

    Normalising inside `_participant_departure_deadline()` makes the mismatch
    unreproducible from any caller. Fixing only the one call site would leave the
    next caller free to repeat it -- and the call site is not obviously wrong to
    read, since passing a scalar it just computed is the natural thing to do.
    """

    def test_the_callee_normalises_rather_than_the_call_site(self):
        src = inspect.getsource(bp._participant_departure_deadline)
        self.assertIn("isinstance(period_hours", src)

    def test_the_caller_may_still_pass_its_scalar(self):
        """The real call site forwards the bare `period_hours` name. That is
        fine now, and this asserts it rather than leaving it to chance -- if a
        future change makes the caller build an array instead, the callee's
        normalisation becomes dead code and this test says so."""
        tree = ast.parse(inspect.getsource(bp._resolve_battery_participant_history))
        forwarded = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_participant_departure_deadline"
            ):
                for kw in node.keywords:
                    if kw.arg == "period_hours":
                        forwarded = kw.value
        self.assertIsNotNone(
            forwarded, "the history resolver must still call the deadline helper"
        )
        self.assertIsInstance(
            forwarded,
            ast.Name,
            "the call site passes a bare name; if it now builds a sequence "
            "inline, the callee's scalar normalisation is no longer exercised "
            "by the real path and needs its own direct coverage",
        )


class TestTheExceptionWasCaughtNotPropagated(unittest.TestCase):
    """Records the answer to #1336's own open question, so it is not re-derived.

    Asserted structurally rather than trusted: the call must sit inside a `try`
    that catches `TypeError`, because that is what made the failure silent, and
    the whole account of the impact rests on it.
    """

    def test_the_call_is_inside_a_typeerror_handler(self):
        src = Path(bp.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        call_line = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "_participant_departure_deadline"
            ):
                call_line = node.lineno
        self.assertIsNotNone(call_line)

        catching = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            if not (node.lineno <= call_line <= (node.end_lineno or node.lineno)):
                continue
            for handler in node.handlers:
                names = []
                if isinstance(handler.type, ast.Tuple):
                    names = [getattr(e, "id", "") for e in handler.type.elts]
                elif handler.type is not None:
                    names = [getattr(handler.type, "id", "")]
                if "TypeError" in names:
                    catching.append((node.lineno, node.end_lineno))
        self.assertTrue(
            catching,
            "the call is NOT inside a TypeError handler, which means this bug "
            "propagated rather than silently excluding one participant -- the "
            "impact described in #1336 and in this file's docstring is then "
            "wrong and should be re-derived",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
