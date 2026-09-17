"""nimbus issue #773: dump the REAL failing instance instead of building
another synthetic one.

Mark Purcell's own proposed next step, 2026-09-17, and the reasoning is
what makes it worth doing rather than another hypothesis:

    "the synthetic sweep has now failed twice to reproduce the shape
     that matters (3.1x max vs production's 21-60x, and inverting at the
     largest synthetic size) -- two failed synthetic reproductions is a
     real signal that guessing at *why* production's root relaxation
     goes fractional isn't going to get there analytically."

HiGHS can hand over the model it choked on. These tests pin the three
properties that decide whether that capability is safe to leave on:

  1. a healthy install writes nothing
  2. a failure writes exactly one file per phase, not one per cycle
  3. nothing the dump can do takes down a solve

(3) is the load-bearing one. This runs on a path that is already going
badly, on real households' hardware, and an exception here would convert
an intermittent slow solve into a hard failure -- strictly worse than
the problem being diagnosed.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import _solver_path  # noqa: F401
from solver import lp


class _FakeHighs:
    """Stands in for the HiGHS handle. Records what it was asked to
    write, and can be told to fail the way a real one might."""

    def __init__(self, *, raises=None):
        self.written: list[str] = []
        self._raises = raises

    def writeModel(self, path):  # mirrors highspy's own camelCase name
        if self._raises is not None:
            raise self._raises
        self.written.append(path)


class _DumpTestCase(unittest.TestCase):
    def setUp(self):
        lp._LP_DUMPED_PHASES.clear()
        self.addCleanup(lp._LP_DUMPED_PHASES.clear)


class TestItWritesTheModelOnce(_DumpTestCase):
    def test_a_failure_writes_a_file_and_returns_its_path(self):
        h = _FakeHighs()
        path = lp._dump_failing_model(h, "fail_phase1_primary")
        self.assertIsNotNone(path)
        self.assertEqual(h.written, [path])
        self.assertIn("nimbus_773_fail_phase1_primary", path)
        self.assertTrue(path.endswith(".mps"))

    def test_the_same_phase_is_not_written_twice(self):
        """An intermittent fault that fires for hours must not quietly
        fill a household's disk. The first capture of a given failure is
        the one worth having; later ones are the same shape."""
        h = _FakeHighs()
        first = lp._dump_failing_model(h, "fail_phase1_primary")
        second = lp._dump_failing_model(h, "fail_phase1_primary")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(h.written), 1)

    def test_a_different_phase_still_gets_its_own_capture(self):
        """Bounded by the number of distinct phases (~4) rather than by
        a counter someone has to trust -- and a phase 2 failure is a
        genuinely different instance from a phase 1 failure."""
        h = _FakeHighs()
        self.assertIsNotNone(lp._dump_failing_model(h, "fail_phase1_primary"))
        self.assertIsNotNone(lp._dump_failing_model(h, "slow_phase2_secondary"))
        self.assertEqual(len(h.written), 2)

    def test_the_dump_directory_is_configurable(self):
        import os
        import tempfile

        target = tempfile.mkdtemp()
        old = os.environ.get(lp._LP_DUMP_DIR_ENV)
        os.environ[lp._LP_DUMP_DIR_ENV] = target
        try:
            path = lp._dump_failing_model(_FakeHighs(), "fail_x")
        finally:
            if old is None:
                os.environ.pop(lp._LP_DUMP_DIR_ENV, None)
            else:
                os.environ[lp._LP_DUMP_DIR_ENV] = old
        self.assertTrue(path.startswith(target))


class TestItNeverTakesDownASolve(_DumpTestCase):
    """The property that decides whether this is safe to leave enabled.
    It runs on a path that is already going badly, on real households'
    hardware -- an exception here would turn an intermittent slow solve
    into a hard failure, which is strictly worse than the problem being
    diagnosed."""

    def test_a_write_failure_returns_none_instead_of_raising(self):
        h = _FakeHighs(raises=OSError("read-only file system"))
        self.assertIsNone(lp._dump_failing_model(h, "fail_phase1_primary"))

    def test_an_older_highspy_without_writemodel_is_survivable(self):
        """`writeModel` is not guaranteed to exist on every highspy the
        manifest allows. An AttributeError must read as 'no dump' rather
        than as a crash."""
        h = MagicMock(spec=[])  # no attributes at all
        self.assertIsNone(lp._dump_failing_model(h, "fail_phase1_primary"))

    def test_an_unexpected_exception_type_is_also_survivable(self):
        h = _FakeHighs(raises=RuntimeError("something internal"))
        self.assertIsNone(lp._dump_failing_model(h, "fail_phase1_primary"))


class TestTheLabelIsNotTrustedAsAFilename(_DumpTestCase):
    """The phase label reaches this as a caller-supplied string. Keeping
    it to a filename-safe subset is cheaper than reasoning about every
    caller, now and later."""

    def test_path_separators_cannot_escape_the_dump_directory(self):
        import os
        import tempfile

        target = tempfile.mkdtemp()
        old = os.environ.get(lp._LP_DUMP_DIR_ENV)
        os.environ[lp._LP_DUMP_DIR_ENV] = target
        try:
            path = lp._dump_failing_model(_FakeHighs(), "../../etc/passwd")
        finally:
            if old is None:
                os.environ.pop(lp._LP_DUMP_DIR_ENV, None)
            else:
                os.environ[lp._LP_DUMP_DIR_ENV] = old
        # The property that matters is that the file lands INSIDE the
        # dump directory. `..` surviving as literal dots in the basename
        # is cosmetic -- `nimbus_773_.._.._etc_passwd.mps` is an
        # ordinary file in `target`, because the separators are what
        # would have let it traverse and those are gone.
        self.assertEqual(os.path.dirname(path), target)
        self.assertNotIn(os.sep, os.path.basename(path))
        self.assertNotIn("/", os.path.basename(path))
        self.assertEqual(os.path.normpath(path), path)

    def test_a_very_long_label_is_truncated(self):
        import os

        path = lp._dump_failing_model(_FakeHighs(), "x" * 400)
        self.assertLess(len(os.path.basename(path)), 120)


class TestTheGateIsTheExistingTwoTierCondition(unittest.TestCase):
    """Gated on the same `alarming` condition #945 already uses to decide
    WARNING vs DEBUG, so a healthy install never writes a byte -- which
    is what makes leaving this on by default defensible rather than
    something an operator has to pre-arm before the intermittent fault
    they cannot predict."""

    def test_the_alarming_threshold_is_below_the_per_call_limit(self):
        """A call that hits the per-call time limit must be captured.
        If the alarming threshold ever rose above it, the exact case
        #773 is about would stop being dumped."""
        self.assertLess(lp._ALARMING_LP_CALL_SECONDS, lp.DEFAULT_TIME_LIMIT_SECONDS)

    def test_alarming_is_stricter_than_merely_slow(self):
        """A merely-slow-but-healthy call is information, not an alarm,
        and must not write a multi-megabyte file."""
        self.assertGreater(lp._ALARMING_LP_CALL_SECONDS, lp._SLOW_LP_CALL_SECONDS)


if __name__ == "__main__":
    unittest.main()
