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

import ast
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

    def _lp_tree(self) -> ast.Module:
        src = lp.__file__.replace(".pyc", ".py")
        with open(src, encoding="utf-8") as f:
            return ast.parse(f.read())

    def test_both_sites_call_the_note(self):
        """Every dump-announcing log line in lp.py passes the note.

        Counted as CALLS, in the AST, not as occurrences of the string
        `_dump_location_note()` in the file.

        nimbus issue #1179 is why. This assertion used to require exactly 3
        occurrences of that substring -- one definition and two call sites --
        and it broke the moment another function's *docstring* mentioned the
        name, which is +1 with no behaviour change at all. That is precisely
        the failure the sibling test above already records in its own
        docstring: "A guard that cannot tell prose from code cries wolf, and
        this repo has already switched off tests that did." The same guard
        was two assertions away from doing it.

        Counting calls instead pins the property the issue is about -- a dump
        is never announced without saying whether it will survive -- and
        cannot be moved by a comment, a docstring or a reference in prose.
        """
        tree = self._lp_tree()
        logging_calls_passing_the_note = 0
        total_note_calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "_dump_location_note"
            ):
                total_note_calls += 1
            is_log = (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_LOGGER"
            )
            if is_log and any(
                isinstance(a, ast.Call)
                and isinstance(a.func, ast.Name)
                and a.func.id == "_dump_location_note"
                for a in node.args
            ):
                logging_calls_passing_the_note += 1

        self.assertEqual(
            logging_calls_passing_the_note,
            2,
            "expected exactly two log lines to pass the note (the slow path "
            "and the failing path) -- a dump that announces itself without "
            "saying whether it will still be there is the whole defect this "
            "fixes",
        )
        # Non-vacuity: the two above are calls, so the total can never be
        # lower, and this catches a third caller appearing unnoticed.
        self.assertEqual(
            total_note_calls,
            3,
            "expected three calls to _dump_location_note(): the two log "
            "lines above, plus the one delegation inside the public "
            "dump_location_note() alias that solver_writer.py uses for the "
            "same purpose (nimbus issue #1179)",
        )

    def test_the_public_alias_exists_and_only_delegates(self):
        """nimbus issue #1179: `solver_writer.py` announces its own #1179
        capture and needs this sentence there. It goes through a public
        alias rather than reaching across modules for the private name, so a
        rename in `lp.py` cannot silently break the other module -- and the
        alias must stay a pure delegation, not a second copy of the wording.
        """
        self.assertTrue(
            callable(getattr(lp, "dump_location_note", None)),
            "lp.dump_location_note() is the public entry point "
            "solver_writer.py calls; removing it breaks that log line",
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(lp.dump_location_note(), lp._dump_location_note())
        with patch.dict(os.environ, {lp._LP_DUMP_DIR_ENV: "/somewhere"}, clear=True):
            self.assertEqual(lp.dump_location_note(), lp._dump_location_note())


if __name__ == "__main__":
    unittest.main()
