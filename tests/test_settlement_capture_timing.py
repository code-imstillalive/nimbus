"""Real, pure-function test coverage for the standalone writer script's
settlement-capture wait logic (nimbus issue #232 follow-up, 2026-08-27).

Background: the standalone `nimbus_solver_forecast_writer.py` cron
script deploys on a bare `* * * * *` -- unchanged, untouched by #244/
#247's phase-alignment fix, which only lives in the native in-process
solver's own timer (`custom_components/nimbus_load/__init__.py`).
Confirmed live against this project's own production NUC1: on an
install running BOTH writers against the same entity, the standalone
script's every-60s, arbitrary-phase writes dominate what a viewer
actually sees, making #247's fix invisible in practice even though it
shipped correctly.

`seconds_to_settlement_capture()` is the fix -- a short, bounded wait
applied only to the one tick per 5-minute cycle that lands close to a
real NEM settlement boundary, so that specific run catches the settled
price tick instead of racing it. The other four ticks each cycle are
completely unaffected (0.0 seconds, no behavior change at all) --
deliberately preserving the every-minute cadence the household asked
for, not trading it away.

These tests exercise the pure timing function directly with
hand-constructed datetimes, not the real network-dependent parts of the
script (fetch_solver_config(), build_tiered_grid(), etc., all already
covered elsewhere and unrelated to this change).
"""

import importlib.util
import os
import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_SCRIPT_PATH = os.path.join(
    _REPO_ROOT,
    "docs",
    "real-world-integration",
    "files",
    "nimbus_solver_forecast_writer.py",
)
# The script under test does `sys.path.insert(0, os.environ.get(
# "NIMBUS_SOLVER_PATH", <hardcoded NUC path>))` then `from solver import
# elements, network` -- point it at this repo's own real solver package
# (custom_components/nimbus_load/) via the same env var it already
# supports, matching tests/_solver_path.py's own path for every other
# solver-level test in this suite.
os.environ.setdefault(
    "NIMBUS_SOLVER_PATH", os.path.join(_REPO_ROOT, "custom_components", "nimbus_load")
)

_BRISBANE = ZoneInfo("Australia/Brisbane")


def _load_module():
    """Import the standalone script as a module without running its own
    `if __name__ == "__main__":` block (guarded by that check already,
    so a plain import is safe) -- avoids needing this whole test file
    to live inside docs/real-world-integration/files/ just to get a
    normal package-relative import working.
    """
    spec = importlib.util.spec_from_file_location(
        "nimbus_solver_forecast_writer_under_test", _SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()
seconds_to_settlement_capture = _mod.seconds_to_settlement_capture
TARGET = _mod._SETTLEMENT_CAPTURE_TARGET_SECOND
WINDOW = _mod._SETTLEMENT_CAPTURE_WINDOW_SECONDS


def _at(minute: int, second: int) -> datetime:
    return datetime(2026, 8, 27, 18, minute, second, tzinfo=_BRISBANE)


class TestSettlementCaptureTiming(unittest.TestCase):
    def test_exactly_on_a_boundary_waits_the_full_target(self):
        # test_exactly_on_a_boundary_waits_the_full_target: A run landing at exactly :X0:00 (e.g. cron fired at :00
        self.assertAlmostEqual(seconds_to_settlement_capture(_at(0, 0)), 30.0)
        self.assertAlmostEqual(seconds_to_settlement_capture(_at(5, 0)), 30.0)
        self.assertAlmostEqual(seconds_to_settlement_capture(_at(35, 0)), 30.0)

        # test_a_few_seconds_past_a_boundary_waits_the_remainder: Real Python startup overhead means a cron-triggered run
        self.assertAlmostEqual(seconds_to_settlement_capture(_at(0, 3)), 27.0)
        self.assertAlmostEqual(seconds_to_settlement_capture(_at(10, 12)), 18.0)

        # test_already_at_the_target_second_needs_no_wait
        self.assertEqual(seconds_to_settlement_capture(_at(0, 30)), 0.0)

        # test_past_the_target_but_still_in_window_needs_no_wait: A run landing at, say, :35 past a boundary is already past
        self.assertEqual(seconds_to_settlement_capture(_at(0, 35)), 0.0)
        self.assertEqual(seconds_to_settlement_capture(_at(0, 39)), 0.0)

        # test_far_from_any_boundary_never_waits: The overwhelming common case -- 4 of every 5 minute-ticks --
        self.assertEqual(seconds_to_settlement_capture(_at(1, 0)), 0.0)
        self.assertEqual(seconds_to_settlement_capture(_at(2, 30)), 0.0)
        self.assertEqual(seconds_to_settlement_capture(_at(3, 15)), 0.0)
        self.assertEqual(seconds_to_settlement_capture(_at(4, 45)), 0.0)

        # test_just_outside_the_capture_window_is_a_no_op: The boundary between "close enough to wait for" and "closer
        self.assertEqual(seconds_to_settlement_capture(_at(0, WINDOW)), 0.0)
        self.assertGreater(seconds_to_settlement_capture(_at(0, WINDOW - 15)), 0.0)

        # test_every_5_minute_boundary_behaves_identically: The wait logic must be identical at every real NEM boundary
        for boundary_minute in range(0, 60, 5):
            with self.subTest(minute=boundary_minute):
                self.assertAlmostEqual(
                    seconds_to_settlement_capture(_at(boundary_minute, 5)),
                    float(TARGET - 5),
                )

        # test_return_value_is_never_negative
        for m in range(60):
            for s in (0, 1, 15, 29, 30, 31, 39, 40, 45, 59):
                self.assertGreaterEqual(seconds_to_settlement_capture(_at(m, s)), 0.0)
