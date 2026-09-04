"""Regression test for nimbus issue #365 (Mark Purcell, codebase review),
item 4: `solver_runtime.py`'s own module-level globals (`_solver_writer`'s
`set_native_hass()` binding, `_last_solve_completed_monotonic`,
`_import_error_notified`, `_price_latency_sensor`,
`_consecutive_lock_skips`) used to live for the lifetime of the PROCESS,
not the config entry -- since this integration is `single_config_entry:
true` (nimbus issue #359), the only way a "different" entry ever exists
is a genuine remove-then-re-add, but even that rare case deserves a clean
slate rather than silently inheriting a removed entry's own state (a
stale "solved N seconds ago" reading feeding issue #295's own cron-
suppression window, or a missing-dependency notification permanently
suppressed for a new entry even after the underlying problem recurred).

`async_unload_entry()` now calls `solver_runtime.reset_module_state()`
after a successful platform unload. This test exercises the REAL
`async_unload_entry()` (not a reimplementation), same convention as its
sibling `test_init_unload_cancels_triggers_before_teardown.py`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import custom_components.nimbus_load as nimbus_init
from custom_components.nimbus_load import solver_runtime


def _make_entry(entry_id: str) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = {}
    return entry


def _seed_solver_runtime_state() -> None:
    solver_runtime._solver_writer = object()
    solver_runtime._last_solve_completed_monotonic = 12345.0
    solver_runtime._import_error_notified = True
    solver_runtime._price_latency_sensor = object()
    solver_runtime._consecutive_lock_skips = 7


def _clear_solver_runtime_state() -> None:
    solver_runtime._solver_writer = None
    solver_runtime._last_solve_completed_monotonic = None
    solver_runtime._import_error_notified = False
    solver_runtime._price_latency_sensor = None
    solver_runtime._consecutive_lock_skips = 0


class TestUnloadResetsSolverRuntimeGlobals:
    def teardown_method(self):
        _clear_solver_runtime_state()

    def test_successful_unload_resets_every_solver_runtime_global(self):
        _seed_solver_runtime_state()
        entry = _make_entry("test_entry_reset_globals")
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        hass.services.has_service.return_value = True

        result = asyncio.run(nimbus_init.async_unload_entry(hass, entry))

        assert result is True
        assert solver_runtime._solver_writer is None
        assert solver_runtime._last_solve_completed_monotonic is None
        assert solver_runtime._import_error_notified is False
        assert solver_runtime._price_latency_sensor is None
        assert solver_runtime._consecutive_lock_skips == 0
        # nimbus issue #365 item 1: services must also be torn down on a
        # successful unload -- see test_services.py's own dedicated
        # coverage of async_unregister_services() itself for the detail;
        # this just confirms async_unload_entry() actually calls it.
        assert hass.services.async_remove.call_count == 3

    def test_failed_platform_unload_does_not_reset_globals(self):
        # A failed unload_platforms() means entities/timers may still be
        # live -- resetting solver_runtime's own bindings underneath a
        # still-running setup would be the real bug, not the fix.
        _seed_solver_runtime_state()
        entry = _make_entry("test_entry_failed_unload")
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

        result = asyncio.run(nimbus_init.async_unload_entry(hass, entry))

        assert result is False
        assert solver_runtime._solver_writer is not None
        assert solver_runtime._last_solve_completed_monotonic == 12345.0
        assert solver_runtime._import_error_notified is True
        hass.services.async_remove.assert_not_called()


class TestResetModuleStateDirectly:
    def teardown_method(self):
        _clear_solver_runtime_state()

    def test_reset_module_state_clears_everything(self):
        _seed_solver_runtime_state()
        solver_runtime.reset_module_state()
        assert solver_runtime._solver_writer is None
        assert solver_runtime._last_solve_completed_monotonic is None
        assert solver_runtime._import_error_notified is False
        assert solver_runtime._price_latency_sensor is None
        assert solver_runtime._consecutive_lock_skips == 0
