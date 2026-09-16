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

**Both halves now exist, and they shipped separately on purpose.**

*Visibility* (v0.94.350): the guard must report its own failure, at
WARNING, naming what was lost. That came first because it is what made
the second half diagnosable at all -- #873's own hoist then failed with
an `AttributeError` swallowed into silent absence, and the WARNING is
the only reason anyone could see which call raised.

*Isolation* (this): the loop body is wrapped per load, so one bad load
costs itself and nothing else. Deliberately a pure re-indent of 839
lines, verified mechanically (dedent the result and it is byte-identical
to the original) rather than by eye -- #873 established that a
mechanical move of a block containing `if` can silently re-parent a
following `elif` while passing ruff, mypy and its own tests.

`TestOneBadLoadDoesNotCostTheRest` is the behavioural half, and it is
what the gap-pin that used to stand here asked for by name.
"""

from __future__ import annotations

import inspect
import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
import test_solver_writer_controllable_loads as _controllable


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

    def test_the_loop_now_isolates_each_load(self):
        """The other half, landed. This replaces the honest gap-pin that
        stood here while only the visibility half existed -- that test's
        own docstring said to swap it for a behavioural one, and
        `TestOneBadLoadDoesNotCostTheRest` below is it.

        Kept as a structural check too, because the behavioural test can
        only prove isolation works for the failure it injects, while
        this proves the loop body is wrapped at all.
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
        self.assertRegex(
            first_stmt,
            r"^\s*try:\s*$",
            "the per-load loop body is no longer wrapped, so one bad "
            "load costs every load after it again (nimbus #1019)",
        )
        self.assertIn(
            "continue",
            body,
            "a per-load handler that does not `continue` is not isolation",
        )


class TestOneBadLoadDoesNotCostTheRest(unittest.TestCase):
    """nimbus issue #1019, behaviourally.

    Two controllable loads, both wanting to turn on. The FIRST is made
    to raise at a point outside the dispatch call itself -- dispatch
    already had its own per-load handler, so injecting there would prove
    nothing about this fix.

    `_resolve_controllable_load_tuning()` is the injection point: it is
    module-level, called early in every iteration, and its failure is
    exactly the shape #1019 describes (a config/entity read going wrong
    for one load). Before this fix the raise escaped to the outermost
    handler and the second load was never commanded at all.
    """

    def setUp(self):
        self._orig_native_hass = solver_writer._NATIVE_HASS
        self._orig_tuning = solver_writer._resolve_controllable_load_tuning
        _controllable.__dict__["_FakeRunStateStore"]._shared_data.clear()
        self._loop, self._loop_thread = _controllable._make_running_loop()

    def tearDown(self):
        solver_writer._NATIVE_HASS = self._orig_native_hass
        solver_writer._resolve_controllable_load_tuning = self._orig_tuning
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def _run(self, *, poison: str | None):
        import numpy as np

        bad = _controllable._fake_subentry(
            "s_bad",
            "controllable_load",
            {"controllable_load_device_entity": "switch.bad_load"},
        )
        good = _controllable._fake_subentry(
            "s_good",
            "controllable_load",
            {"controllable_load_device_entity": "switch.good_load"},
        )
        services = _controllable._FakeServiceCalls()
        entry = SimpleNamespace(
            entry_id="entry_1019",
            subentries={s.subentry_id: s for s in (bad, good)},
        )
        solver_writer._NATIVE_HASS = SimpleNamespace(
            config_entries=SimpleNamespace(async_entries=lambda domain: [entry]),
            services=services,
            loop=self._loop,
            states=SimpleNamespace(get=lambda eid: None),
        )

        real = self._orig_tuning

        def _maybe_raise(data, subentry):
            if poison is not None and subentry.subentry_id == poison:
                raise RuntimeError("simulated per-load failure (nimbus #1019)")
            return real(data, subentry)

        solver_writer._resolve_controllable_load_tuning = _maybe_raise

        now = datetime(2026, 9, 17, 8, 0, tzinfo=_controllable._TZ)
        plan = _controllable._fake_plan(
            sheddable=[
                _controllable._fake_load_plan("s_bad", np.array([1.5, 1.5])),
                _controllable._fake_load_plan("s_good", np.array([1.5, 1.5])),
            ]
        )
        solver_writer.apply_commanded_state_guard(
            plan, now, _controllable._grid(now, 4, minutes=5)
        )
        return services

    def test_the_fixture_dispatches_both_loads_when_nothing_fails(self):
        """Guards the fixture. If this stopped dispatching two loads, the
        real test below would pass for the wrong reason."""
        services = self._run(poison=None)
        dispatched = {data["entity_id"] for _d, _s, data in services.calls}
        self.assertEqual(dispatched, {"switch.bad_load", "switch.good_load"})

    def test_a_load_that_raises_does_not_take_the_rest_of_the_cycle(self):
        """The defect, and the fix. Before #1019's second half, the
        second load was never commanded -- with no error visible at
        default log levels until v0.94.350 raised the outer handler to
        WARNING."""
        services = self._run(poison="s_bad")
        dispatched = {data["entity_id"] for _d, _s, data in services.calls}
        self.assertIn(
            "switch.good_load",
            dispatched,
            "a healthy load was left uncommanded because a DIFFERENT "
            "load raised -- that is exactly nimbus #1019",
        )
        self.assertNotIn(
            "switch.bad_load",
            dispatched,
            "the failing load should be skipped, not dispatched anyway",
        )

    def test_the_failure_names_the_load_it_lost(self):
        """An isolated failure that does not say WHICH load failed is
        only half useful -- the outer handler could never say, because
        by the time it runs the frame is gone."""
        with patch.object(solver_writer, "_LOGGER") as log:
            self._run(poison="s_bad")
        messages = [c.args[0] for c in log.warning.call_args_list if c.args]
        self.assertTrue(
            any("#1019" in m and "NOT commanded" in m for m in messages),
            f"no per-load warning naming the loss: {messages}",
        )
        named = [c for c in log.warning.call_args_list if c.args and "s_bad" in c.args]
        self.assertTrue(named, "the warning does not carry the failing subentry id")


if __name__ == "__main__":
    unittest.main()
