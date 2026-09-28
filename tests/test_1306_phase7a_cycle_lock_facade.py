"""The overlap guard still reads `solver_writer.LOCK_PATH` after Phase 7a.

## What this file exists to prevent

nimbus #1306 (spec 007, Phase 7a) moved `acquire_lock`/`release_lock` into
`solver/cycle_lock.py`. Spec step 2 says to move `LOCK_PATH` with them. It
deliberately did **not** move, and this file is the mechanical reason that
choice is safe rather than merely argued.

Four test sites rebind `LOCK_PATH` on the `solver_writer` module object, and
**none of them is visible to a literal-name scan**:

| site | how |
|---|---|
| `tests/_isolated_state.py:74` | `patch.object(solver_writer, const, ...)`, `const` from a tuple of strings |
| `tests/hass_integration/conftest.py:85` | same shape, its own tuple |
| `tests/test_solve_overlap_guard.py:63` | `solver_writer.LOCK_PATH = ...`, plain assignment |
| `tests/test_solver_writer_lock_self_pid.py:37` | plain assignment |

Spec 007's own inventory records `LOCK_PATH` as having **0 patch sites** for
exactly that reason -- the same blindness #1400 and #1401 record for
`_NATIVE_HASS`, and the reason Phase 7b is blocked on fixing it.

Had the constant moved, a site missed in the sweep would keep rebinding
`solver_writer.LOCK_PATH` while the guard read `cycle_lock`'s own copy: i.e.
`/opt/nimbus_solver_forecast_writer.lock`, silently, on any machine where
`/opt` exists. That is #1330's defect class precisely -- tests writing
production state, invisible on CI and on a Windows dev box because the path
simply fails there.

So the property below is not a style preference. It is the one that makes the
extraction safe, and it is asserted rather than trusted.
"""

from __future__ import annotations

import os
import pathlib
import unittest

import _solver_path  # noqa: F401
import solver_writer
from _isolated_state import isolated_state_paths
from solver import cycle_lock


class TestTheGuardStillHonoursSolverWriterLockPath(unittest.TestCase):
    def tearDown(self):
        solver_writer.release_lock()

    def test_a_rebind_of_solver_writer_LOCK_PATH_changes_where_the_lock_lands(self):
        """The whole design, in one assertion.

        Read at CALL time through the wrapper, so every existing rebind -- by
        `patch.object`, by plain assignment -- keeps biting. If `acquire_lock`
        ever closes over a path captured at import, or reads a copy living in
        `cycle_lock`, this fails.
        """
        with isolated_state_paths(solver_writer) as tmpdir:
            self.assertTrue(solver_writer.acquire_lock())
            self.assertTrue(
                os.path.exists(solver_writer.LOCK_PATH),
                "the PID file was not written where solver_writer.LOCK_PATH points",
            )
            self.assertEqual(
                os.path.dirname(os.path.abspath(solver_writer.LOCK_PATH)),
                os.path.abspath(tmpdir),
                "isolated_state_paths() redirected LOCK_PATH but the guard wrote "
                "somewhere else -- on a machine with /opt this is a real "
                "production-state write (#1330's defect class)",
            )

    def test_the_isolation_helper_is_not_silently_bypassed(self):
        """Non-vacuity for the assertion above: outside the helper the path is
        the production default, so the test above is genuinely observing a
        redirect and not a coincidence."""
        self.assertTrue(
            solver_writer.LOCK_PATH.endswith("nimbus_solver_forecast_writer.lock"),
            "the unpatched default changed shape; this file's premise needs review",
        )
        with isolated_state_paths(solver_writer):
            self.assertNotEqual(
                solver_writer.LOCK_PATH,
                "/opt/nimbus_solver_forecast_writer.lock",
            )

    def test_the_process_local_lock_is_one_object_not_two(self):
        """`_IN_PROCESS_LOCK` IS aliased rather than wrapped, because nothing
        rebinds it -- it is only acquired and released. Two distinct Lock
        objects would mean the PID-file half and the threading half disagreed
        about whether a solve is running, which is #757 reopened."""
        self.assertIs(solver_writer._IN_PROCESS_LOCK, cycle_lock._IN_PROCESS_LOCK)

    def test_cycle_lock_defines_no_LOCK_PATH_of_its_own(self):
        """A second storage location is the failure this design exists to avoid.
        If one is ever added, the wrapper's argument would be shadowed for any
        caller that omits it, and the four rebind sites would go quiet."""
        self.assertFalse(
            hasattr(cycle_lock, "LOCK_PATH"),
            "solver/cycle_lock.py grew its own LOCK_PATH -- see this file's "
            "docstring for why that is a silent-write hazard",
        )

    def test_cycle_lock_does_not_import_solver_writer(self):
        """`solver/` is the lowest layer in the import-linter contract. The
        path arrives as a parameter precisely so no upward reach is needed --
        not even the deferred `_solver_writer()` seam the other phases use."""
        src = pathlib.Path(cycle_lock.__file__).read_text(encoding="utf-8")
        for forbidden in ("import solver_writer", "_solver_writer("):
            self.assertNotIn(forbidden, src)

    def test_the_wrapper_passes_the_path_positionally_to_the_real_function(self):
        """Pins the delegation itself, so a future edit cannot quietly make
        `solver_writer.acquire_lock` a reimplementation that drifts."""
        seen: list[str] = []

        def spy(lock_path: str) -> bool:
            seen.append(lock_path)
            return True

        original = cycle_lock.acquire_lock
        cycle_lock.acquire_lock = spy  # type: ignore[assignment]
        try:
            with isolated_state_paths(solver_writer):
                self.assertTrue(solver_writer.acquire_lock())
                self.assertEqual(seen, [solver_writer.LOCK_PATH])
        finally:
            cycle_lock.acquire_lock = original  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
