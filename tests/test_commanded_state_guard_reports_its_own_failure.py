"""nimbus issue #1019 -- the commanded-state guard could abandon a whole
solve cycle's dispatch and say nothing about it.

Every controllable load is commanded through one coroutine:

    for subentry_id, period0_kw, load_kind, load_plan in entries_with_ids:
        ...

with **no per-load try/except**. The only broad handler is the outermost
one, and it logged at DEBUG:

    except Exception:
        _LOGGER.debug("Nimbus: commanded-state guard failed for this
                       solve cycle", exc_info=True)

So any raise -- an unavailable entity, a sensor returning an unexpected
type, a malformed subentry, a recorder hiccup mid-fetch -- abandons the
cycle for every load not yet processed. Loads already dispatched stay
dispatched; the rest are simply never commanded. At default log levels a
household sees nothing at all.

At a 5-minute cadence the next cycle usually succeeds, so the symptom is
intermittent missed dispatch rather than an obvious outage. That is the
shape of #757, which took ten investigations, and of #315, where the
ABSENCE of a warning was the evidence nobody thought to check.

**How it was found.** Attempting #873's hoist, which made the thermal
block run for every controllable load. Something in it raised for
climate-domain loads, and the symptom was not an error -- it was 13
dispatch tests reporting `len(services.calls) == 0` with no message.
Failure presenting as *absence* is precisely what this handler's log
level caused.

This pins the visibility half: the guard must report its own failure.
Per-load isolation is the other half of #1019 and needs the loop body
re-indented, so it is deliberately not bundled with this.
"""

from __future__ import annotations

import inspect
import re
import unittest

import _solver_path  # noqa: F401
import solver_writer


def _guard_source() -> str:
    return inspect.getsource(solver_writer.apply_commanded_state_guard)


class TestTheGuardReportsItsOwnFailure(unittest.TestCase):
    def setUp(self):
        self.src = _guard_source()

    def test_the_outermost_handler_is_not_debug(self):
        """The defect itself. A guard that reports its own failure at
        DEBUG is indistinguishable from one that never ran -- #757's
        lesson, restated on a different function."""
        tail = self.src[self.src.rindex("except Exception:") :]
        self.assertNotIn(
            "_LOGGER.debug(",
            tail,
            "the commanded-state guard's last-resort handler logs at "
            "DEBUG again -- a silently abandoned dispatch cycle is "
            "exactly what nimbus #1019 is about",
        )

    def test_the_outermost_handler_warns(self):
        tail = self.src[self.src.rindex("except Exception:") :]
        self.assertIn("_LOGGER.warning(", tail)

    def test_the_warning_says_what_was_actually_lost(self):
        """Naming the consequence matters more than naming the error.
        'guard failed' does not tell a household that loads went
        uncommanded; the traceback alone does not either."""
        tail = self.src[self.src.rindex("except Exception:") :]
        self.assertRegex(
            tail,
            r"NOT commanded",
            "the warning must say that loads were left uncommanded, not "
            "merely that something failed",
        )
        self.assertIn(
            "exc_info=True",
            tail,
            "the traceback is the only thing identifying WHICH failure "
            "abandoned the cycle",
        )

    def test_the_handler_still_swallows_rather_than_propagates(self):
        """The handler must not be 'fixed' into a raise. Dispatch failing
        must never take down the solve cycle it runs inside -- that is
        why the blanket catch exists and it should stay."""
        tail = self.src[self.src.rindex("except Exception:") :]
        # Strip comments first. The handler's own prose explains the
        # failure mode using the word "raise", and a naive text scan
        # matches that rather than any statement -- which is exactly
        # what happened on this test's first run. A fair reminder that
        # a source-text check tests text, not behaviour.
        code = [ln for ln in tail.split("\n") if not ln.strip().startswith("#")]
        offenders = [ln for ln in code if ln.strip().startswith("raise")]
        self.assertEqual(
            offenders,
            [],
            "the last-resort handler must keep swallowing -- a "
            "controllable-load failure must not propagate into the "
            f"solve cycle it runs inside: {offenders}",
        )

    def test_the_known_gap_is_recorded_not_forgotten(self):
        """Per-load isolation is the unfixed half. If someone lands it,
        this test should be updated deliberately rather than the comment
        quietly rotting."""
        self.assertIn(
            "#1019",
            self.src,
            "the guard should carry a pointer to the open per-load "
            "isolation gap, so the next reader knows one bad load still "
            "costs the remainder of the cycle",
        )

    def test_the_loop_still_has_no_per_load_isolation(self):
        """Pins the gap honestly rather than implying it is fixed.

        When per-load isolation lands, this fails and should be replaced
        by a real behavioural test: seed two loads, make the first raise
        OUTSIDE the dispatch call, assert the second is still commanded.
        """
        loop = re.search(
            r"for subentry_id, period0_kw, load_kind, load_plan in "
            r"entries_with_ids:\n(.*?)\n\s+import asyncio as _asyncio",
            self.src,
            re.DOTALL,
        )
        self.assertIsNotNone(loop, "the per-load loop moved -- re-read this test")
        body = loop.group(1)
        first_stmt = next(
            ln
            for ln in body.split("\n")
            if ln.strip() and not ln.strip().startswith("#")
        )
        self.assertNotRegex(
            first_stmt,
            r"^\s*try:\s*$",
            "per-load isolation appears to have landed -- good. Replace "
            "this test with the behavioural one described in its "
            "docstring and close the remaining half of #1019.",
        )


if __name__ == "__main__":
    unittest.main()
