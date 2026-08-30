"""Real regression guard for issue #211 (live devhub recurrence, confirmed
via a live `ha core logs -f` capture, 2026-08-27): sensor.nimbus_solver_
battery_forecast / sensor.nimbus_household_load_total_forecast each getting
a genuine, independent solve pushed to them roughly every 60 seconds, a few
seconds apart -- i.e. TWO live async_track_time_interval registrations for
_periodic_solve calling solver_runtime.async_run_solve() once a minute each,
not one solve double-writing (solver_writer.py has exactly one
ha_post_state() call site per entity -- ruled out by reading the source).

Same root mechanism nimbus PR #210 already fixed for retrain (this
project's own test_coordinator_setup_does_not_block_on_retrain.py: HA
abandoning/retrying a slow async_setup_entry() while the original attempt's
own coroutine keeps running in the background, eventually finishing and
re-registering everything a second time) -- just a different slow step
tripping the same abandon-and-retry path, since #210 only backgrounded the
retrain call specifically. The abandoned attempt's own
hass.config_entries.async_forward_entry_setups() call is a silent no-op the
second time round (platforms already forwarded), so it doesn't reproduce
the LOUD "does not generate unique IDs" error #210 fixed -- but nothing
stopped it from reaching the _periodic_solve registration and creating a
second, independent, permanently-live timer.

Fix: track the unsub callable for _periodic_solve's own periodic-timer
registration (async_track_time_interval at the time this was fixed;
async_track_utc_time_change since issue #244's phase-locked cron -- the
idempotent single-timer-per-entry mechanism this test covers is unchanged
by that swap) in a module-level dict keyed by
entry_id (matching solver_writer.py's own _ENTITY_UPDATE_HANDLERS pattern
for an identical class of problem -- NOT hass.data[DOMAIN], which this
project deliberately moved off of for Quality Scale Bronze, see
coordinator.py's own comment). A second async_setup_entry() call for the
SAME entry_id cancels the first timer before registering its own, so at
most one lives at a time regardless of how many times setup runs.

This test calls the REAL async_setup_entry() twice with the same mock
entry (same entry_id), against tests/_ha_stubs.py's stand-in
homeassistant.* modules (the real `homeassistant` package installed
locally predates ConfigSubentry -- same environment gap documented on
issue #85), and asserts the first timer's unsub gets called exactly once
(cancelled) before the second is stored, proving at most one lives at a
time.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import custom_components.nimbus_load as nimbus_init


def _make_entry(entry_id: str) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    # Empty subentries -- forecastable_subentries ends up [], so the
    # coordinator-setup loop this test isn't about never runs at all.
    entry.subentries = {}
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    return entry


def _close_coro_task(coro, *_args, **_kwargs) -> MagicMock:
    # async_setup_entry()'s own final block schedules the startup-solve
    # retry loop via hass.async_create_background_task(...) -- a real
    # coroutine that a plain MagicMock stand-in never awaits or closes,
    # which Python warns about ("coroutine was never awaited"). Not a bug
    # in the code under test, just test-harness hygiene -- close it
    # explicitly, same technique test_coordinator_setup_does_not_block_
    # on_retrain.py's own scheduled_coro.close() already uses for the
    # identical situation. Also wired to hass.async_create_task below,
    # since that's still used elsewhere in this module (e.g. the price-
    # watcher's own immediate-solve trigger) even though it's no longer
    # what the startup-retry task itself uses.
    coro.close()
    return MagicMock()


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=None)
    hass.async_create_task = MagicMock(side_effect=_close_coro_task)
    hass.async_create_background_task = MagicMock(side_effect=_close_coro_task)
    return hass


def _run_setup_entry_twice_for_same_entry() -> tuple[MagicMock, MagicMock, int]:
    """Runs the real async_setup_entry() twice against the same entry_id,
    with every side effect this test isn't about mocked out. Returns
    (first_unsub, second_unsub, async_track_utc_time_change_call_count) so
    the caller can assert on cancellation/storage behaviour.

    Uses patch.object() (auto-restoring), NOT direct attribute assignment
    on the shared nimbus_init/health/services/solver_runtime module
    objects -- a plain `nimbus_init.health.install_log_buffer_handler =
    MagicMock()` mutates the REAL, shared module for the rest of the
    pytest session (all test files run in one process), which silently
    broke test_sensor_health_report.py and test_services.py's own tests
    of the real functions when first written this way (caught by CI).
    """
    unsub_mocks = [MagicMock(name="unsub_1"), MagicMock(name="unsub_2")]

    entry = _make_entry("test_entry_id_211")
    hass = _make_hass()

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(nimbus_init.health, "install_log_buffer_handler")
        )
        stack.enter_context(
            patch.object(nimbus_init.services, "async_register_services")
        )
        stack.enter_context(
            patch.object(
                nimbus_init.frontend,
                "async_register_frontend",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                nimbus_init,
                "async_get_integration",
                new=AsyncMock(return_value=MagicMock()),
            )
        )
        stack.enter_context(
            patch.object(
                nimbus_init.solver_runtime,
                "async_run_solve",
                new=AsyncMock(return_value=True),
            )
        )
        tracker = stack.enter_context(
            patch.object(
                nimbus_init,
                "async_track_utc_time_change",
                new=MagicMock(side_effect=unsub_mocks),
            )
        )

        asyncio.run(nimbus_init.async_setup_entry(hass, entry))
        asyncio.run(nimbus_init.async_setup_entry(hass, entry))

        # Captured (not re-read from nimbus_init) BEFORE the ExitStack
        # restores the original async_track_utc_time_change on exit --
        # reading it after would see the unpatched real one instead.
        call_count = tracker.call_count

    return unsub_mocks[0], unsub_mocks[1], call_count


def test_second_setup_for_same_entry_id_cancels_the_first_timer():
    first_unsub, second_unsub, call_count = _run_setup_entry_twice_for_same_entry()

    assert call_count == 2, (
        "expected async_track_utc_time_change to be called once per "
        "async_setup_entry() invocation"
    )
    # .assert_called_once()/.assert_not_called() are plain mock method
    # calls, not `assert` statements -- they raise their own
    # AssertionError with a built-in message on failure, so there's no
    # `, "message"` form to attach here the way a real `assert` allows.
    first_unsub.assert_called_once()  # the FIRST timer must be cancelled
    # when async_setup_entry() runs a second time for the same entry_id
    # -- this is the exact #211 bug otherwise: two live timers both
    # calling solver_runtime.async_run_solve() once a minute.
    second_unsub.assert_not_called()  # the second (surviving) timer
    # must still be live after two setup calls for the same entry_id.


def test_module_level_dict_stores_only_the_latest_unsub():
    first_unsub, second_unsub, _tracker = _run_setup_entry_twice_for_same_entry()

    stored = nimbus_init._solver_timer_unsub.get("test_entry_id_211")
    assert stored is second_unsub, (
        "_solver_timer_unsub should hold the LATEST timer's unsub after "
        "two setup calls for the same entry_id, not the cancelled first "
        "one and not None"
    )
    assert stored is not first_unsub


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
