"""nimbus #937: the day's forecast snapshot is captured from
async_run_solve, only on a successful solve, on the event loop. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the capture itself is in tests/v2/forecasting/test_snapshot_capture.py."""

from __future__ import annotations

import inspect
import sys
import unittest
from datetime import timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BNE = timezone(timedelta(hours=10))


class TestOnlyASuccessfulSolveCaptures(unittest.TestCase):
    def test_the_capture_is_called_from_async_run_solve(self):
        from custom_components.nimbus_load import solver_runtime

        src = inspect.getsource(solver_runtime.async_run_solve)
        self.assertIn("async_capture_todays_snapshot", src)

    def test_it_is_guarded_on_the_success_flag(self):
        """Asserted on the parsed CONDITION, not on the literal text `if ok:`.

        Stage 3 tightened this guard to `if ok and entry_id:` -- same
        semantics, strictly stronger -- and a string match failed on a change
        that made the code better. A guard that breaks on a correct
        refactor teaches people to delete guards.
        """
        import ast

        from custom_components.nimbus_load import solver_runtime

        tree = ast.parse(inspect.getsource(solver_runtime.async_run_solve).strip())
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            if "ok" not in names:
                continue
            if "async_capture_todays_snapshot" in ast.dump(node):
                return  # the capture sits inside a branch gated on `ok`
        raise AssertionError(
            "the capture is not inside a branch whose condition tests `ok`"
        )

    def test_it_runs_on_the_event_loop_not_the_worker_thread(self):
        """A Store write must be awaited. _run_one_cycle() runs in an
        executor, so the capture deliberately sits in the async wrapper."""
        from custom_components.nimbus_load import solver_runtime

        self.assertNotIn(
            "async_capture_todays_snapshot",
            inspect.getsource(solver_runtime._run_one_cycle),
        )
