"""`acquire_lock()`'s stale-PID recovery, which no golden scenario reaches.

Spec 007 (Phase 7 of #1298) step 1 requires this **before** anything moves:
`acquire_lock`/`release_lock`/`LOCK_PATH`/`_IN_PROCESS_LOCK` are about to relocate
verbatim into `solver/cycle_lock.py`, and the branch that matters most under a move
is the one the suite never executed.

## Why this branch in particular

The overlap guard has two mechanisms and five outcomes. A golden scenario exercises
exactly one of them — "no lock file, take it" — because it runs a single solve in a
clean temp dir. Everything below that first case is reached only by a real crashed
run, and a relocation is precisely when an untested branch stops working quietly.

The stale-PID path also carries a claim in its own docstring that nothing checked:

> *a real, live discrepancy found testing this same check on Windows (where a
> nonexistent PID instead raises a plain `OSError`, not that specific subclass) is
> exactly why this catches `OSError` broadly, not just the one POSIX-specific
> subclass*

That reasoning is load-bearing and platform-dependent, so it is asserted here for
**both** exception shapes rather than only the one this machine happens to raise.
`os.kill` is patched rather than probed with a real PID, so every case is
deterministic on every platform — a test that depends on PID 1 existing, or on some
number *not* existing, is a test that fails on someone else's machine.

## The case that would wedge production if it broke

`test_a_live_foreign_pid_blocks_AND_releases_the_in_process_lock`. On the "another
run is genuinely alive" path `acquire_lock` returns False, and it must hand back the
`threading.Lock` it took on the way in. If it ever stops doing that, the first
concurrent solve wedges **every subsequent solve in the process** — no error, no log
line, just a writer that never runs again until restarted. That is #757's own defect
class, and returning False is the only path where the release is easy to lose.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer
from _isolated_state import isolated_state_paths


class TestAcquireLockStalePidRecovery(unittest.TestCase):
    def tearDown(self):
        # release_lock() removes the PID file and releases _IN_PROCESS_LOCK in a
        # `finally`, swallowing RuntimeError when it is already unlocked -- so this
        # is a safe reset whatever the test above did, and without it one leaked
        # acquire would hang every later case in this file.
        solver_writer.release_lock()

    def _write_lock(self, pid: str) -> None:
        with open(solver_writer.LOCK_PATH, "w", encoding="utf-8") as handle:
            handle.write(pid)

    # ---------------------------------------------------------------- baseline
    def test_no_lock_file_acquires_and_records_our_own_pid(self):
        """The only case a golden scenario reaches. Here for contrast."""
        with isolated_state_paths(solver_writer):
            self.assertFalse(os.path.exists(solver_writer.LOCK_PATH))
            self.assertTrue(solver_writer.acquire_lock())
            with open(solver_writer.LOCK_PATH, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), str(os.getpid()))

    # ------------------------------------------------------- recovery branches
    def test_a_lock_file_holding_our_own_pid_is_reclaimed(self):
        """An unclean stop by THIS process. `os.kill(os.getpid(), 0)` would
        succeed, so without the explicit self-PID check the run would block on
        its own leftover file forever."""
        with isolated_state_paths(solver_writer):
            self._write_lock(str(os.getpid()))
            self.assertTrue(solver_writer.acquire_lock())

    def test_a_stale_pid_raising_ProcessLookupError_is_reclaimed(self):
        """The POSIX shape: a crashed previous run whose PID no longer exists."""
        with isolated_state_paths(solver_writer):
            self._write_lock(str(os.getpid() + 1))
            with patch.object(solver_writer.os, "kill", side_effect=ProcessLookupError):
                self.assertTrue(solver_writer.acquire_lock())
            with open(solver_writer.LOCK_PATH, encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), str(os.getpid()))

    def test_a_stale_pid_raising_a_PLAIN_OSError_is_also_reclaimed(self):
        """The Windows shape, and the reason the `except` clause names `OSError`
        rather than `ProcessLookupError`. Asserted separately so narrowing that
        clause to the POSIX subclass fails here instead of on someone's laptop."""
        with isolated_state_paths(solver_writer):
            self._write_lock(str(os.getpid() + 1))
            with patch.object(
                solver_writer.os, "kill", side_effect=OSError("no such process")
            ):
                self.assertTrue(solver_writer.acquire_lock())

    def test_a_pid_we_are_not_permitted_to_signal_is_reclaimed(self):
        """`PermissionError` means the PID exists but belongs to another user, so
        it is *not* our stale lock -- but the docstring's own rule is that ANY
        failure to positively confirm the old PID is treated as unlocked, rather
        than ever permanently wedging every future run. Pinned because the
        alternative reading (treat it as alive and block) is defensible and would
        be a silent behaviour change."""
        with isolated_state_paths(solver_writer):
            self._write_lock(str(os.getpid() + 1))
            with patch.object(solver_writer.os, "kill", side_effect=PermissionError):
                self.assertTrue(solver_writer.acquire_lock())

    def test_an_empty_or_corrupt_lock_file_is_reclaimed(self):
        """`int("")` raises ValueError, which the same `except` covers."""
        for content in ("", "   ", "not-a-pid", "12.5"):
            with self.subTest(content=repr(content)):
                with isolated_state_paths(solver_writer):
                    self._write_lock(content)
                    self.assertTrue(solver_writer.acquire_lock())
                solver_writer.release_lock()

    # -------------------------------------------------- the genuinely-locked path
    def test_a_live_foreign_pid_blocks_AND_releases_the_in_process_lock(self):
        """The one that would wedge production if it broke.

        Returning False is correct. Failing to hand back the threading.Lock taken
        on the way in is not: the next solve would find `_IN_PROCESS_LOCK` already
        held and return False forever, silently, until the process restarted.
        """
        with isolated_state_paths(solver_writer):
            self._write_lock(str(os.getpid() + 1))
            with patch.object(solver_writer.os, "kill", return_value=None):
                self.assertFalse(solver_writer.acquire_lock())
            # the real assertion: the process-local lock is free again
            self.assertTrue(
                solver_writer._IN_PROCESS_LOCK.acquire(blocking=False),
                "acquire_lock() returned False without releasing _IN_PROCESS_LOCK "
                "-- every subsequent solve in this process would be blocked "
                "silently (#757's own defect class)",
            )
            solver_writer._IN_PROCESS_LOCK.release()

    def test_the_in_process_lock_alone_blocks_a_second_acquire(self):
        """The threading half, which a PID file structurally cannot see: a second
        solve on another worker thread of THIS process."""
        with isolated_state_paths(solver_writer):
            self.assertTrue(solver_writer.acquire_lock())
            self.assertFalse(
                solver_writer.acquire_lock(),
                "a second acquire in the same process must fail on "
                "_IN_PROCESS_LOCK before the PID file is ever consulted",
            )

    def test_release_lock_is_safe_to_call_twice(self):
        """It is itself called from a `finally:` and must never be the thing that
        raises -- hence the swallowed RuntimeError on an already-free lock."""
        with isolated_state_paths(solver_writer):
            self.assertTrue(solver_writer.acquire_lock())
            solver_writer.release_lock()
            solver_writer.release_lock()  # must not raise
            self.assertFalse(os.path.exists(solver_writer.LOCK_PATH))


if __name__ == "__main__":
    unittest.main()
