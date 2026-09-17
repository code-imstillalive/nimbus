"""nimbus #773: a captured instance that is silently perishable is a
capture that gets lost.

`_dump_failing_model()` writes to `tempfile.gettempdir()` unless
`NIMBUS_LP_DUMP_DIR` says otherwise. On a container deployment that
directory is cleared on restart.

**Observed 2026-09-18.** The first real capture this apparatus ever
produced — the instance two synthetic reproduction attempts could not
match, and the reason the whole diagnostic exists — landed at
`/tmp/nimbus_773_slow_lex_phase_phase2_secondary.mps` on an install that
restarts several times an evening during ordinary release verification.

The log line announcing it ended:

    Set NIMBUS_LP_DUMP_DIR to choose where these land.

Which frames a persistent location as a **preference**. A reader who does
not already know that the default is volatile has no reason to hurry, and
the default outcome is: capture the artefact, announce it, lose it, and
wait for the next occurrence to do it again.

That is the same failure #944 documents — the code did the right thing and
said so only in a form nobody would act on in time. The fix there was to
say the consequence out loud; same here.

These tests pin both branches of the message, because the useful half is
the *warning*, and a warning that silently stops appearing is worse than
one that was never there.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
from solver import lp


class TestTheVolatileCase(unittest.TestCase):
    """`NIMBUS_LP_DUMP_DIR` unset — the default, and the one that loses
    files."""

    def test_it_says_the_file_will_not_survive(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lp._LP_DUMP_DIR_ENV, None)
            note = lp._dump_location_note()
        self.assertIn("TEMPORARY", note)
        self.assertIn("will not survive", note)

    def test_it_tells_the_reader_to_act_now(self):
        """The consequence is time-bound, so the instruction has to be
        too. "Attach it to the issue" is not enough if the file is gone
        by the time anyone reads the log."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lp._LP_DUMP_DIR_ENV, None)
            note = lp._dump_location_note()
        self.assertIn("NOW", note)

    def test_it_names_the_env_var_as_the_durable_fix(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(lp._LP_DUMP_DIR_ENV, None)
            note = lp._dump_location_note()
        self.assertIn(lp._LP_DUMP_DIR_ENV, note)
        self.assertIn("persistent", note)


class TestThePersistentCase(unittest.TestCase):
    """`NIMBUS_LP_DUMP_DIR` set — the operator has already chosen, and
    must not be warned about a problem they do not have."""

    def test_it_does_not_cry_wolf(self):
        with patch.dict(os.environ, {lp._LP_DUMP_DIR_ENV: "/data/nimbus_dumps"}):
            note = lp._dump_location_note()
        self.assertNotIn("TEMPORARY", note)
        self.assertNotIn("will not survive", note)

    def test_it_confirms_the_path_persists(self):
        with patch.dict(os.environ, {lp._LP_DUMP_DIR_ENV: "/data/nimbus_dumps"}):
            note = lp._dump_location_note()
        self.assertIn("persists", note)


class TestBothCallSitesUseIt(unittest.TestCase):
    """The note is worthless if only one of the two dump paths carries
    it. Checked as source text rather than by driving a real 12k-variable
    HiGHS failure, for the same reason
    `test_solver_writer_family_a_freshness_repush.py` does: the condition
    cannot be produced on demand, and the wiring is what regresses.
    """

    def test_neither_site_passes_the_bare_env_var_as_a_format_arg(self):
        """The old shape was `_LOGGER.warning(..., dump_path,
        _LP_DUMP_DIR_ENV)` — the variable's NAME interpolated as advice.
        The new shape passes the note instead.

        A first version of this test asserted the old *wording* was
        absent from the file. It failed, correctly: `_dump_location_note`'s
        own docstring quotes that wording to explain what changed. A guard
        that cannot tell prose from code cries wolf, and this repo has
        already switched off tests that did.
        """
        src = lp.__file__.replace(".pyc", ".py")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        normalized = " ".join(text.split())
        self.assertNotIn(
            "dump_path, _LP_DUMP_DIR_ENV,",
            normalized,
            "a dump log line still interpolates the env var name as its "
            "closing advice, instead of saying whether the file it just "
            "wrote will still be there",
        )

    def test_both_sites_call_the_note(self):
        src = lp.__file__.replace(".pyc", ".py")
        with open(src, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(
            text.count("_dump_location_note()"),
            3,
            "expected one definition and exactly two call sites (the slow "
            "path and the failing path) -- a dump that announces itself "
            "without saying whether it will still be there is the whole "
            "defect this fixes",
        )


if __name__ == "__main__":
    unittest.main()
