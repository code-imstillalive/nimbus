"""Real regression test for nimbus issue #346 (Mark Purcell): acquire_lock()
had no self-PID check.

In native mode LOCK_PATH holds HA's OWN pid -- solver_runtime.py's driver
calls acquire_lock()/release_lock() in-process, on a worker thread of the
same `hass` process, every solve cycle. A worker thread mid-LP-solve when
HA is stopped/killed is not guaranteed to reach release_lock() (called from
solver_runtime.py's own `finally:`), so the lock file can be left behind
holding this same process's own PID. In a Docker/HAOS container that PID is
frequently identical across restarts -- without a self-PID check,
`os.kill(old_pid, 0)` genuinely succeeds (it's us), acquire_lock() returns
False forever, and every subsequent tick silently skips
("previous cycle still in progress") with no way to ever self-heal.

Imports and exercises the REAL acquire_lock()/release_lock() (not a
reimplementation), against a real temp file for LOCK_PATH -- same "import
solver_writer directly, real file I/O for its own lock/state files" pattern
already established by test_solver_writer_min_soc_floor.py and siblings.
"""

import os
import tempfile
import unittest

import _solver_path  # noqa: F401
import solver_writer


class TestSelfPidLockReclaim(unittest.TestCase):
    def setUp(self):
        fd, self.lock_path = tempfile.mkstemp(prefix="nimbus_test_lock_")
        os.close(fd)
        os.remove(self.lock_path)  # acquire_lock() itself creates it
        self._orig_lock_path = solver_writer.LOCK_PATH
        solver_writer.LOCK_PATH = self.lock_path

    def tearDown(self):
        solver_writer.LOCK_PATH = self._orig_lock_path
        if os.path.exists(self.lock_path):
            os.remove(self.lock_path)

    def test_stale_lock_holding_our_own_pid_is_reclaimed_not_treated_as_held(self):
        # Simulate the real failure: a previous native-mode cycle wrote
        # OUR OWN current pid (a container restart landed on the same PID)
        # and never got to release_lock() before HA was killed.
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

        acquired = solver_writer.acquire_lock()

        self.assertTrue(
            acquired,
            "a lock file holding OUR OWN pid must never be treated as a "
            "genuinely overlapping run -- before this fix, every tick "
            "would return False forever with no way to self-heal",
        )
        # And it genuinely re-wrote the file with the current pid, not just
        # returned True while leaving the old (also-correct, coincidentally
        # identical) content untouched -- exercise release/re-acquire too.
        solver_writer.release_lock()
        self.assertFalse(os.path.exists(self.lock_path))

    def test_lock_held_by_a_real_different_running_process_is_still_respected(self):
        # Our own parent process -- genuinely real, alive, and guaranteed
        # different from os.getpid() on every platform this test runs on
        # (unlike a fixed literal such as PID 1, which is POSIX init and
        # not meaningfully "alive" in the same way on Windows).
        other_pid = os.getppid()
        assert other_pid != os.getpid()
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write(str(other_pid))

        acquired = solver_writer.acquire_lock()

        self.assertFalse(
            acquired,
            "a lock genuinely held by a different, real, alive process "
            "must still block -- the self-PID fix must not weaken this",
        )

    def test_lock_holding_a_dead_pid_is_reclaimed(self):
        # A PID essentially guaranteed not to exist.
        dead_pid = 2**30
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write(str(dead_pid))

        acquired = solver_writer.acquire_lock()

        self.assertTrue(acquired)

    def test_corrupt_lock_file_is_reclaimed(self):
        with open(self.lock_path, "w", encoding="utf-8") as f:
            f.write("not-a-pid")

        acquired = solver_writer.acquire_lock()

        self.assertTrue(acquired)

    def test_no_existing_lock_file_acquires_cleanly(self):
        acquired = solver_writer.acquire_lock()

        self.assertTrue(acquired)
        with open(self.lock_path, "r", encoding="utf-8") as f:
            self.assertEqual(int(f.read().strip()), os.getpid())


if __name__ == "__main__":
    unittest.main()
