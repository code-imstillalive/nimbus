"""Shared state every v2 solver test starts from (#1837).

#773's cooldown is process state: a phase that fails in one test arms it for
300 s, and every later test on the same worker then gets the plain solve
instead of the phased one, so a tie-break property fails for a reason that
is not its own. Seen on CI for #1962: the export-bonus block and smoothness
properties failed once at the end of a worker's run and passed on the rerun.
Each test starts and ends with the cooldown off, so none inherits or leaks
one; a test about the cooldown arms it itself.
"""

from __future__ import annotations

import pytest
from solver import lp


@pytest.fixture(autouse=True)
def _no_inherited_cooldown():
    lp._lex_calibration_failed_until = 0.0
    yield
    lp._lex_calibration_failed_until = 0.0
