"""nimbus issue #757: the solve overlap guard must actually guard in
native (in-process) mode.

`solver_runtime._run_one_cycle()` gates every solve on
`solver_writer.acquire_lock()`. That guard was a PID file alone -- correct
for the standalone/cron script, where two runs genuinely are two
processes. In native mode every solve runs in the *same* `hass` process,
on different executor worker threads, so the PID in the file is always
our own.

`acquire_lock()` has an explicit branch for that, added for #346:

    if old_pid == os.getpid():
        pass  # stale file from an unclean stop -- safe to reclaim

#346 was a real bug and that fix was right for it -- a worker thread
killed mid-solve leaves the file behind holding a PID that, in a
container, is frequently identical after restart, so `os.kill(old_pid, 0)`
succeeds (it's us), every tick returns False forever, and nothing cleans
up. But "this PID is our own" is *also* exactly what a genuine concurrent
in-process solve looks like, and a PID file has no information left to
tell the two apart.

Measured before the fix, against the real function:

    thread A acquires: True
    thread B acquires WHILE A HOLDS IT: True

So in native mode the guard had never blocked anything, and #315's
"previous cycle still in progress" WARNING was unreachable code. That is
the concurrency half of #757 -- four solves within five seconds against a
one-minute timer, each publishing over the last, which is why ten
investigations of that issue disagreed with each other about whether a
battery participant was being "excluded".

Both mechanisms are kept, because they answer genuinely different
questions and neither substitutes for the other: a process-local
`threading.Lock` for the in-process case, the PID file for the
cross-process one. The tests below pin both, and pin that a failure of
either one leaves no lock behind.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


class _LockTestCase(unittest.TestCase):
    """Every test runs against its own real lock file, and leaves the
    process-local lock released no matter how it exits -- these are
    module-global resources shared with the rest of the suite."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._original_path = solver_writer.LOCK_PATH
        solver_writer.LOCK_PATH = os.path.join(self._tmpdir.name, "solver.lock")

    def tearDown(self):
        # Belt and braces: release until it is genuinely free, so one
        # failing assertion can't wedge every later test in the file.
        for _ in range(4):
            try:
                solver_writer._IN_PROCESS_LOCK.release()
            except RuntimeError:
                break
        solver_writer.LOCK_PATH = self._original_path
        self._tmpdir.cleanup()

    def _acquire_on_another_thread(self) -> bool:
        result: list[bool] = []
        thread = threading.Thread(
            target=lambda: result.append(solver_writer.acquire_lock())
        )
        thread.start()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "acquire_lock() must never block")
        return result[0]


class TestInProcessOverlap(_LockTestCase):
    """The #757 defect itself."""

    def test_a_second_thread_cannot_acquire_while_the_first_holds_it(self):
        self.assertTrue(solver_writer.acquire_lock())
        self.assertFalse(
            self._acquire_on_another_thread(),
            "a concurrent in-process solve must be refused -- this is the "
            "case a PID file structurally cannot see (nimbus issue #757)",
        )

    def test_the_second_thread_can_acquire_once_the_first_releases(self):
        """The guard must refuse an overlap, not permanently wedge."""
        self.assertTrue(solver_writer.acquire_lock())
        self.assertFalse(self._acquire_on_another_thread())
        solver_writer.release_lock()
        self.assertTrue(self._acquire_on_another_thread())
        solver_writer.release_lock()

    def test_acquire_never_blocks(self):
        """A blocking acquire would be worse than the bug: it would park
        an HA executor worker thread for the whole of another solve,
        which is exactly the executor starvation #773 documents."""
        self.assertTrue(solver_writer.acquire_lock())
        self._acquire_on_another_thread()  # asserts non-blocking internally

    def test_sequential_cycles_are_unaffected(self):
        """The overwhelmingly common case -- one solve at a time, over
        and over -- must be byte-identical to before."""
        for _ in range(5):
            self.assertTrue(solver_writer.acquire_lock())
            solver_writer.release_lock()


class TestPidFileStillWorks(_LockTestCase):
    """The cross-process half, unchanged. The standalone/cron script is a
    genuinely separate process and the PID file is the only thing that
    can see it."""

    def test_a_live_foreign_pid_blocks(self):
        with open(solver_writer.LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid() + 1))
        # A patched os.kill that doesn't raise is what "that PID is a real
        # running process" looks like to this function -- asserted this
        # way rather than with a real foreign PID, whose os.kill(pid, 0)
        # behaviour genuinely differs between POSIX and Windows (see
        # acquire_lock()'s own docstring).
        with patch.object(solver_writer.os, "kill", return_value=None):
            self.assertFalse(solver_writer.acquire_lock())

    def test_a_dead_foreign_pid_is_reclaimed(self):
        with open(solver_writer.LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid() + 1))
        with patch.object(solver_writer.os, "kill", side_effect=ProcessLookupError):
            self.assertTrue(solver_writer.acquire_lock())
        solver_writer.release_lock()

    def test_a_stale_file_holding_our_own_pid_is_reclaimed(self):
        """nimbus issue #346's own regression, asserted here so the #757
        fix can't be mistaken for a reason to revert it: a worker thread
        killed mid-solve leaves this file behind holding our PID, and in
        a container that PID is frequently identical after a restart."""
        with open(solver_writer.LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        self.assertTrue(solver_writer.acquire_lock())
        solver_writer.release_lock()

    def test_a_corrupt_lock_file_is_reclaimed(self):
        with open(solver_writer.LOCK_PATH, "w", encoding="utf-8") as f:
            f.write("not a pid")
        self.assertTrue(solver_writer.acquire_lock())
        solver_writer.release_lock()


class TestNoLockIsLeftBehind(_LockTestCase):
    """Two locks means two ways to leak one. A refusal by either must
    leave the other free, or the guard turns a transient overlap into a
    permanently dead solver."""

    def test_a_pid_file_refusal_releases_the_in_process_lock(self):
        with open(solver_writer.LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid() + 1))
        with patch.object(solver_writer.os, "kill", return_value=None):
            self.assertFalse(solver_writer.acquire_lock())
        # The refusal above must not have kept the process-local lock:
        # once the foreign run is gone, the very next tick must succeed.
        os.remove(solver_writer.LOCK_PATH)
        self.assertTrue(
            solver_writer.acquire_lock(),
            "a PID-file refusal leaked the in-process lock -- every "
            "subsequent solve on this install would be refused forever",
        )
        solver_writer.release_lock()

    def test_release_without_acquire_is_harmless(self):
        """release_lock() is called from a `finally:` and must never be
        the thing that raises."""
        solver_writer.release_lock()
        solver_writer.release_lock()
        self.assertTrue(solver_writer.acquire_lock())
        solver_writer.release_lock()

    def test_release_removes_the_pid_file(self):
        self.assertTrue(solver_writer.acquire_lock())
        self.assertTrue(os.path.exists(solver_writer.LOCK_PATH))
        solver_writer.release_lock()
        self.assertFalse(os.path.exists(solver_writer.LOCK_PATH))


class TestUnderRealConcurrency(_LockTestCase):
    """The shape the live trace actually showed: several worker threads
    arriving at once. Exactly one may proceed."""

    def test_exactly_one_of_many_simultaneous_threads_wins(self):
        n = 8
        barrier = threading.Barrier(n)
        winners: list[bool] = []
        guard = threading.Lock()
        all_recorded = threading.Event()

        def contend():
            barrier.wait(timeout=10)
            got = solver_writer.acquire_lock()
            with guard:
                winners.append(got)
                if len(winners) == n:
                    all_recorded.set()
            if got:
                # The winner holds until every other thread has recorded
                # its own result, rather than for a fixed sleep -- with a
                # sleep, a thread descheduled past it would tidily win an
                # already-free lock and the test would pass for the wrong
                # reason.
                all_recorded.wait(timeout=10)
                solver_writer.release_lock()

        threads = [threading.Thread(target=contend) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
            self.assertFalse(t.is_alive())

        self.assertEqual(
            sum(winners),
            1,
            f"{sum(winners)} of {n} simultaneous solves were allowed to "
            "proceed; exactly one may (nimbus issue #757)",
        )


if __name__ == "__main__":
    unittest.main()
