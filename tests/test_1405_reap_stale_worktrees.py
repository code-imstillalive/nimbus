"""`coverage_compare.reap_stale_worktrees()` — nimbus issue #1405.

## Why a reaper rather than a better teardown

The teardown is already correct, and #1354 already tightened it: both worktrees
are created inside one `try` and removed in its `finally`, inside a
`TemporaryDirectory`. **No `finally`, `__exit__` or `atexit` handler runs on a
SIGKILL**, and these gates are unusually exposed — a comparison spawns child
pytest runs measured in tens of minutes, and `test_gates_coverage_compare.py`
invokes the tool as a subprocess of its own.

Measured on the reference machine when this was written: **28 leaked trees, 22 of
them from a single day**, splitting into two populations —

    11 dirs   no wt-* present  -> the `finally` HAD removed both worktrees
    16 dirs   wt-* present     -> the `finally` never ran at all

— and on one of the 11, **0 read-only files with `shutil.rmtree` succeeding when
retried**. So `__exit__` did not fail, it never ran. Nothing was locked.

## The property that matters most here is the one about NOT deleting

An unconditional reaper would be a race against any concurrent comparison. The
age threshold is the whole safety argument, so `test_a_fresh_tree_is_never_reaped`
is the most important test in this file: a live run's tree is minutes old, never
hours.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "gates"))

from coverage_compare import WORKTREE_PREFIX, reap_stale_worktrees


class TestReaper(unittest.TestCase):
    def setUp(self):
        # An isolated root, so these tests never touch the real temp dir. The
        # first version of them did, and reaped 27 real leaked trees on the
        # development box -- see reap_stale_worktrees()'s own docstring.
        self._root_ctx = tempfile.TemporaryDirectory(prefix="reap-test-root-")
        self.root = pathlib.Path(self._root_ctx.name)
        self.made: list[pathlib.Path] = []

    def tearDown(self):
        self._root_ctx.cleanup()

    def _tree(self, age_hours: float, *, with_worktree_dirs: bool) -> pathlib.Path:
        """A synthetic leaked tree of a given age, in either measured shape."""
        d = pathlib.Path(tempfile.mkdtemp(prefix=WORKTREE_PREFIX, dir=self.root))
        self.made.append(d)
        (d / "gate.coveragerc").write_text("[run]\n", encoding="utf-8")
        (d / "cov-base").mkdir()
        if with_worktree_dirs:
            (d / "wt-base").mkdir()
            (d / "wt-head").mkdir()
        old = time.time() - age_hours * 3600.0
        os.utime(d, (old, old))
        return d

    def test_a_fresh_tree_is_never_reaped(self):
        """THE safety property. A concurrent comparison's tree is minutes old,
        so the threshold must protect it -- otherwise this reaper becomes a way
        to break a running gate, which is strictly worse than the leak."""
        live = self._tree(age_hours=0.0, with_worktree_dirs=True)
        reap_stale_worktrees(older_than_hours=6.0, tmp_root=self.root)
        self.assertTrue(live.exists(), "a tree created just now was reaped")

    def test_a_stale_tree_with_no_worktree_dirs_is_reaped(self):
        """The 11-dir population: the `finally` ran and removed both worktrees,
        and only the temp tree was left. Nothing in git's metadata refers to it,
        so a `git worktree prune` alone would never touch it."""
        stale = self._tree(age_hours=48.0, with_worktree_dirs=False)
        reaped = reap_stale_worktrees(older_than_hours=6.0, tmp_root=self.root)
        self.assertFalse(stale.exists())
        self.assertIn(f"tree {stale.name}", reaped)

    def test_a_stale_tree_with_worktree_dirs_is_reaped(self):
        """The 16-dir population: killed before the `finally` ran at all."""
        stale = self._tree(age_hours=48.0, with_worktree_dirs=True)
        reaped = reap_stale_worktrees(older_than_hours=6.0, tmp_root=self.root)
        self.assertFalse(stale.exists())
        self.assertIn(f"tree {stale.name}", reaped)

    def test_the_threshold_is_honoured_on_both_sides_of_the_boundary(self):
        """Not just "old is reaped" -- that the number is actually read."""
        young = self._tree(age_hours=1.0, with_worktree_dirs=False)
        old = self._tree(age_hours=5.0, with_worktree_dirs=False)
        reap_stale_worktrees(older_than_hours=3.0, tmp_root=self.root)
        self.assertTrue(young.exists(), "1h tree reaped under a 3h threshold")
        self.assertFalse(old.exists(), "5h tree survived a 3h threshold")

    def test_it_reports_what_it_reaped_and_nothing_it_did_not(self):
        """Silent cleanup of hundreds of megabytes would hide how often runs are
        dying, which is information worth surfacing."""
        stale = self._tree(age_hours=48.0, with_worktree_dirs=False)
        fresh = self._tree(age_hours=0.0, with_worktree_dirs=False)
        reaped = reap_stale_worktrees(older_than_hours=6.0, tmp_root=self.root)
        self.assertIn(f"tree {stale.name}", reaped)
        self.assertNotIn(f"tree {fresh.name}", reaped)

    def test_it_touches_nothing_outside_its_own_prefix(self):
        """A temp dir belonging to any other tool must be invisible to this."""
        other = pathlib.Path(tempfile.mkdtemp(prefix="some-other-tool-", dir=self.root))
        self.made.append(other)
        old = time.time() - 72 * 3600.0
        os.utime(other, (old, old))
        reap_stale_worktrees(older_than_hours=6.0, tmp_root=self.root)
        self.assertTrue(other.exists(), "reaped a directory it does not own")

    def test_it_is_safe_to_run_when_there_is_nothing_to_reap(self):
        self.assertIsInstance(
            reap_stale_worktrees(older_than_hours=100000.0, tmp_root=self.root), list
        )


if __name__ == "__main__":
    unittest.main()
