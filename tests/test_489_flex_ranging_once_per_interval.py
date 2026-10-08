"""nimbus issue #489: HiGHS ranging runs once per 5-minute interval, not on
every solve.

Measured on a real install, ranging moved the median solve from 1.16 s to
10.3 s (~9x), and it ran on every solve -- one every ~17 s on the native
runtime. That is why `switch.nimbus_solver_flex_signals_enabled` cannot be
left on, and so why every flex sensor reads `unknown` in practice. The only
consumer with a cadence of its own, the nem-flex-telemetry record, is one
record per 5-minute interval, so ranging the first solve of each interval
feeds it exactly as before at roughly 1/18th of the cost.

Pinned here, against real solves (the plan-assembly harness from #1303):

1. **Ranging never changes the plan.** A ranged and an unranged solve of the
   same inputs produce the identical dispatch -- the precondition for
   alternating them. If this ever fails, the cadence gate would itself
   introduce plan changes every 5 minutes, i.e. chatter.
2. **The cadence.** Switch on: the first solve in an interval ranges; later
   solves in the same interval do not and say so (`flex_ranging_deferred`);
   the next interval ranges again. Switch off: never ranges, never deferred.
3. **A failed ranging solve is retried**, not skipped for the interval.
4. **The hold.** On a deferred cycle both flex publishers re-post their last
   real payload unchanged, so the push sensors do not go `unavailable` between
   ranging solves. With the switch off they post nothing, as before.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_plan
import solver_publish
import solver_writer

from tests.test_1303_plan_assembly_extraction import CFG, NOW, _fingerprint, _kwargs

ON = {**CFG, "solver_flex_signals_enabled": True}
OFF = {**CFG, "solver_flex_signals_enabled": False}


def _assemble(cfg, now=NOW):
    kwargs = _kwargs()
    kwargs["now"] = now
    return solver_plan.assemble_and_solve_plan(dict(cfg), **kwargs)


class _Reset(unittest.TestCase):
    def setUp(self):
        solver_writer.reset_flex_ranging_cadence()
        self.addCleanup(solver_writer.reset_flex_ranging_cadence)


class TestRangingDoesNotChangeThePlan(_Reset):
    def test_a_ranged_and_an_unranged_solve_dispatch_identically(self):
        ranged = _assemble(ON)
        self.assertIsNotNone(
            ranged.plan.grid_signals, "the ranged solve did not produce signals"
        )
        solver_writer.reset_flex_ranging_cadence()
        unranged = _assemble(OFF)
        self.assertIsNone(unranged.plan.grid_signals)
        self.assertEqual(_fingerprint(ranged), _fingerprint(unranged))


class TestTheCadence(_Reset):
    def test_first_solve_of_an_interval_ranges_the_rest_defer(self):
        first = _assemble(ON)
        self.assertIsNotNone(first.plan.grid_signals)
        self.assertFalse(first.flex_ranging_deferred)

        later = _assemble(ON, NOW + timedelta(seconds=17))
        self.assertIsNone(later.plan.grid_signals)
        self.assertTrue(later.flex_ranging_deferred)

    def test_the_next_interval_ranges_again(self):
        _assemble(ON)
        nxt = _assemble(ON, NOW + timedelta(minutes=5))
        self.assertIsNotNone(nxt.plan.grid_signals)
        self.assertFalse(nxt.flex_ranging_deferred)

    def test_intervals_are_wall_clock_5_minute_slots(self):
        """Keyed on the UTC 5-minute slot, like the telemetry record's own
        `interval_start_utc` -- not "5 minutes since the last ranging"."""
        start = NOW.replace(minute=4, second=50)
        _assemble(ON, start)
        across = _assemble(ON, start + timedelta(seconds=20))  # 12:05:10
        self.assertFalse(across.flex_ranging_deferred)

    def test_switch_off_never_ranges_and_never_defers(self):
        for _ in range(2):
            a = _assemble(OFF)
            self.assertIsNone(a.plan.grid_signals)
            self.assertFalse(a.flex_ranging_deferred)

    def test_a_ranging_solve_that_produced_no_signals_is_retried(self):
        real = solver_plan.network.build_plan

        def no_signals(**kw):
            return dataclasses.replace(real(**kw), grid_signals=None)

        with patch.object(solver_plan.network, "build_plan", side_effect=no_signals):
            failed = _assemble(ON)
        self.assertIsNone(failed.plan.grid_signals)
        retry = _assemble(ON, NOW + timedelta(seconds=17))
        self.assertIsNotNone(retry.plan.grid_signals)
        self.assertFalse(retry.flex_ranging_deferred)


class TestTheHold(_Reset):
    def _publish_signals(self, plan, hold_last):
        with patch.object(solver_writer, "ha_post_state") as post:
            solver_writer.publish_flex_signals(plan, hold_last=hold_last)
        return post

    def test_a_deferred_cycle_re_posts_the_last_real_payload(self):
        ranged = _assemble(ON).plan
        real_post = self._publish_signals(ranged, hold_last=False)
        real_post.assert_called_once()
        entity, state, attrs = real_post.call_args[0]
        self.assertEqual(entity, "sensor.nimbus_flex_signals")

        deferred = SimpleNamespace(grid_signals=None)
        held = self._publish_signals(deferred, hold_last=True)
        held.assert_called_once()
        self.assertEqual(held.call_args[0], (entity, state, attrs))
        self.assertEqual(
            held.call_args[0][2]["generated_at"],
            attrs["generated_at"],
            "a held payload must keep its own generated_at",
        )

    def test_switch_off_posts_nothing_even_with_a_payload_cached(self):
        self._publish_signals(_assemble(ON).plan, hold_last=False)
        off = self._publish_signals(SimpleNamespace(grid_signals=None), False)
        off.assert_not_called()

    def test_nothing_to_hold_before_the_first_real_post(self):
        held = self._publish_signals(SimpleNamespace(grid_signals=None), True)
        held.assert_not_called()

    def test_the_telemetry_record_is_held_the_same_way(self):
        solver_publish.publish_flex_telemetry_record.__globals__[
            "_LAST_FLEX_TELEMETRY_NO_RECORD"
        ].clear()
        record = {"interval_start_utc": "2026-09-28T02:00:00Z"}
        built = SimpleNamespace(record=record, reason="", clamped_fields=())
        none = SimpleNamespace(record=None, reason="no ranging", clamped_fields=())
        kw = {
            "batteries": [],
            "import_price": 0.1,
            "export_price": 0.05,
            "import_limit_kw": 15.0,
            "export_limit_kw": 30.0,
            "period_hours": 0.5,
        }
        with (
            patch.object(
                solver_writer, "build_flex_telemetry_record", return_value=built
            ),
            patch.object(solver_writer, "ha_post_state") as first,
        ):
            solver_publish.publish_flex_telemetry_record({}, None, NOW, **kw)
        first.assert_called_once()

        for hold_last, expect_post in ((True, True), (False, False)):
            with (
                patch.object(
                    solver_writer, "build_flex_telemetry_record", return_value=none
                ),
                patch.object(solver_writer, "ha_post_state") as post,
            ):
                solver_publish.publish_flex_telemetry_record(
                    {}, None, NOW, hold_last=hold_last, **kw
                )
            with self.subTest(hold_last=hold_last):
                if expect_post:
                    self.assertEqual(post.call_args[0], first.call_args[0])
                else:
                    # nimbus #1634: not held -- the reason is published,
                    # never the stale record.
                    self.assertEqual(post.call_args[0][1], "no_record")
                    self.assertEqual(
                        post.call_args[0][2]["last_record_interval"],
                        record["interval_start_utc"],
                    )


class TestMainWiresTheFlag(unittest.TestCase):
    def test_main_passes_the_deferred_flag_to_both_publishers(self):
        """Source-level: `main()` must hand `flex_ranging_deferred` to both
        publishers, or the hold never engages in production."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(solver_writer.main))
        wired = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", None))
                for kw in node.keywords:
                    if (
                        kw.arg == "hold_last"
                        and isinstance(kw.value, ast.Attribute)
                        and kw.value.attr == "flex_ranging_deferred"
                    ):
                        wired.add(name)
        self.assertEqual(
            wired, {"publish_flex_signals", "publish_flex_telemetry_record"}
        )


if __name__ == "__main__":
    unittest.main()
