"""nimbus #1217: the daily retrain no longer fires every subentry on the
same second.

**The measured burst.** Every coordinator scheduled its retrain at
`minute=0, second=0`, so an install's whole fleet trained back to back on
one executor pool. On the reference household, 2026-09-25: five models
retrained between 06:00:45 and 06:01:23 on 3,360-4,205 points each, and
**21 of that day's 21 "previous cycle still in progress" skips fell in
hour 06 -- zero across the other 23 hours.** The day before had the same
shape: 24 skips, all in hour 06.

**Why it mattered beyond its own noise.** `fetch_entity_history_range()`
waits `future.result(timeout=30)` on a recorder read and degrades to `[]`
on any failure, logging only at DEBUG. Inside that window the quality
rescore's SoC read timed out at **exactly 30.121 s** -- it started at
06:00:03.517 and the first contention warning landed at 06:00:33.638 --
so the day was scored against a hardcoded 50% opening state of charge.
That is #1214, which published **EPR 21.11%** for a day that scores
**68.86%** with the real 16.6%.

#1215 stopped the wrong number being published. It did not stop the read
failing. This spreads the contention that made it fail.

The properties pinned here:

1. **Deterministic** -- a subentry lands on the same minute across
   restarts and across processes. `hash()` would not do: Python
   randomises string hashing per process, so the schedule would move on
   every restart and a household could never predict when its own signal
   retrains.
2. **Inside the configured hour** -- the retrain hour stays the retrain
   hour; this only decides where within it a subentry lands.
3. **Actually spread** -- distinct subentries do not all collapse onto
   the same minute, which is the entire point.
4. **The schedule uses it** -- a property nothing passes to
   `async_track_time_change` is not a fix.
"""

from __future__ import annotations

import inspect
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.coordinator import NimbusCoordinator


def _coord(subentry_id: str) -> NimbusCoordinator:
    c = NimbusCoordinator.__new__(NimbusCoordinator)
    c.subentry = MagicMock()
    c.subentry.subentry_id = subentry_id
    return c


class TestTheOffsetIsDeterministic(unittest.TestCase):
    """Property 1 -- stable across restarts and processes."""

    def test_the_same_id_always_gives_the_same_minute(self):
        a = _coord("01K2F0S9Y9AJJTF2115TMNR2BM")._retrain_minute
        b = _coord("01K2F0S9Y9AJJTF2115TMNR2BM")._retrain_minute
        self.assertEqual(a, b)

    def test_it_does_not_use_pythons_randomised_string_hash(self):
        """The trap: `hash()` on a str is salted per process, so the
        schedule would move on every restart.

        Parsed rather than grepped. A plain text search matches the
        docstring above, which discusses `hash()` by name -- so the check
        would fail for the wrong reason, and could equally pass for one.
        """
        import ast
        import textwrap

        tree = ast.parse(
            textwrap.dedent(inspect.getsource(NimbusCoordinator._retrain_minute.fget))
        )
        called = {
            n.func.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        self.assertNotIn(
            "hash",
            called,
            "a per-process-salted hash would give a different retrain "
            "minute on every restart",
        )
        attrs = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        self.assertTrue(
            {"sha256", "sha1", "md5", "blake2b"} & attrs,
            f"expected a stable digest; calls found: {sorted(attrs)}",
        )


class TestTheOffsetStaysInsideTheHour(unittest.TestCase):
    """Property 2 -- the configured retrain hour is still the hour."""

    def test_every_id_lands_in_0_to_59(self):
        for i in range(200):
            m = _coord(f"subentry-{i}")._retrain_minute
            self.assertGreaterEqual(m, 0)
            self.assertLessEqual(m, 59)


class TestItActuallySpreads(unittest.TestCase):
    """Property 3 -- the entire point."""

    def test_a_realistic_fleet_does_not_collapse_onto_one_minute(self):
        # The reference household runs ~25 subentries -- the install that
        # produced 25 simultaneous warnings in one second (#1195).
        minutes = {_coord(f"subentry-{i}")._retrain_minute for i in range(25)}
        self.assertGreater(
            len(minutes),
            1,
            "if every subentry still shares a minute, nothing was spread",
        )
        # Not asserting a specific spread -- a digest modulo 60 will
        # collide sometimes and that is fine. What must not happen is
        # everything landing together.
        self.assertGreaterEqual(len(minutes), 10, sorted(minutes))


class TestTheScheduleUsesIt(unittest.TestCase):
    """Property 4 -- a property nothing reads is not a fix."""

    def test_async_track_time_change_is_given_the_offset(self):
        src = inspect.getsource(NimbusCoordinator.async_setup)
        self.assertIn("minute=self._retrain_minute", src)
        self.assertNotIn(
            "minute=0",
            src,
            "the fixed minute=0 is what stacked the whole fleet on one second",
        )

    def test_the_hour_is_still_the_configured_one(self):
        """Control: spreading must not have moved the retrain off the
        household's chosen hour."""
        src = inspect.getsource(NimbusCoordinator.async_setup)
        self.assertIn("hour=self._retrain_hour", src)
        self.assertIsNone(
            re.search(r"hour=\d", src),
            "the retrain hour must stay configurable, not become a literal",
        )


if __name__ == "__main__":
    unittest.main()
