"""IV&V finding (since-#1058 pass, 2026-09-17): #773's failing-model dump
(v0.94.370, `_dump_failing_model()`) marks a phase "dumped" before it has
actually succeeded in dumping it.

`_dump_failing_model()` in `solver/lp.py` adds `label` to the module-level
`_LP_DUMPED_PHASES` set BEFORE entering the `try` block that calls
`h.writeModel(path)`. If that write raises for any reason -- and the
function's own docstring names "a missing `writeModel` on an older
highspy" and, by extension, any transient filesystem failure as exactly
the kind of thing this must survive without dying -- the label is left
in `_LP_DUMPED_PHASES` regardless. Every later call for that same phase
then returns `None` at the very first line (`if label in
_LP_DUMPED_PHASES: return None`) without ever calling `writeModel`
again, for the rest of the process's lifetime.

#773 is explicitly framed (both in this repo's own CLAUDE.md worklog and
in this commit's own message) as an INTERMITTENT fault that can recur
"for hours" on a real household's install between HA restarts. A single
bad-luck moment -- `/tmp` momentarily full, a backup holding a lock, a
tmpfiles-cleanup race -- during the FIRST occurrence of a given phase
failure permanently and silently defeats the diagnostic for every
SUBSEQUENT occurrence of that same phase, with no signal beyond one
DEBUG line that is never repeated. That is the opposite of what this
feature exists to do: capture "the next live occurrence" of the fault.

This test constructs exactly that sequence -- a first call that fails
with a transient OSError, followed by a second call (same label) with a
handle that WOULD succeed -- and asserts the second call actually
writes the file. It fails against current code because the dedup set
already contains the label after the first, failed attempt.

Fix shape suggested (not prescribed): only add to `_LP_DUMPED_PHASES`
after `h.writeModel()` returns successfully -- e.g. move the `.add()`
call to immediately before `return path`, or wrap the whole body in a
`try/except` and only mark success in an `else`/on the success path.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401
import pytest
from solver import lp


class _FakeHighs:
    """Stands in for the HiGHS handle, same convention as
    test_lp_failing_model_dump.py's own fixture."""

    def __init__(self, *, raises=None):
        self.written: list[str] = []
        self._raises = raises

    def writeModel(self, path):
        if self._raises is not None:
            raise self._raises
        self.written.append(path)


class TestDedupDoesNotPermanentlyBlockAfterATransientFailure(unittest.TestCase):
    def setUp(self):
        lp._LP_DUMPED_PHASES.clear()
        self.addCleanup(lp._LP_DUMPED_PHASES.clear)

    @pytest.mark.xfail(
        reason=(
            "nimbus IV&V (since #1058, 2026-09-17): _LP_DUMPED_PHASES.add(label) "
            "runs before writeModel() is attempted, so a transient write failure "
            "(e.g. a momentarily read-only /tmp) permanently disables the dump "
            "for that phase for the rest of the process, even though the very "
            "next occurrence of the same fault could have been captured fine. "
            "See the module docstring above for the full #773 reasoning."
        ),
        strict=True,
    )
    def test_a_second_occurrence_can_still_be_dumped_after_a_transient_first_failure(
        self,
    ):
        failing_handle = _FakeHighs(raises=OSError("read-only file system"))
        first = lp._dump_failing_model(failing_handle, "phase2_pin_resolve")
        self.assertIsNone(first, "the first, failing attempt correctly reports no dump")

        working_handle = _FakeHighs()
        second = lp._dump_failing_model(working_handle, "phase2_pin_resolve")

        self.assertIsNotNone(
            second,
            "the SAME phase failing again, this time with a working handle, "
            "should be captured -- the diagnostic's whole point is catching "
            "the next live occurrence of an intermittent fault, not just the "
            "first attempt regardless of whether that attempt itself succeeded.",
        )
        self.assertEqual(working_handle.written, [second])


if __name__ == "__main__":
    unittest.main()
