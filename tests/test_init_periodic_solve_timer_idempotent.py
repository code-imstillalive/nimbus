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

Fix: track the unsub callable for _periodic_solve's own
async_track_time_interval registration in a module-level dict keyed by
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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
    # async_setup_entry()'s own final line schedules one immediate solve
    # via hass.async_create_task(solver_runtime.async_run_solve(hass)) --
    # a real coroutine that a plain MagicMock stand-in for
    # hass.async_create_task never awaits or closes, which Python warns
    # about ("coroutine was never awaited"). Not a bug in the code under
    # test, just test-harness hygiene -- close it explicitly, same
    # technique test_coordinator_setup_does_not_block_on_retrain.py's own
    # scheduled_coro.close() already uses for the identical situation.
    coro.close()
    return MagicMock()


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=None)
    hass.async_create_task = MagicMock(side_effect=_close_coro_task)
    return hass


def _run_setup_entry_twice_for_same_entry() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Runs the real async_setup_entry() twice against the same entry_id,
    with every side effect this test isn't about mocked out. Returns
    (first_unsub, second_unsub, async_track_time_interval_mock) so the
    caller can assert on cancellation/storage behaviour."""
    nimbus_init.health.install_log_buffer_handler = MagicMock()
    nimbus_init.services.async_register_services = MagicMock()
    nimbus_init.frontend.async_register_frontend = AsyncMock(return_value=None)
    nimbus_init.async_get_integration = AsyncMock(return_value=MagicMock())
    nimbus_init.solver_runtime.async_run_solve = AsyncMock(return_value=True)

    unsub_mocks = [MagicMock(name="unsub_1"), MagicMock(name="unsub_2")]
    nimbus_init.async_track_time_interval = MagicMock(side_effect=unsub_mocks)

    entry = _make_entry("test_entry_id_211")
    hass = _make_hass()

    asyncio.run(nimbus_init.async_setup_entry(hass, entry))
    asyncio.run(nimbus_init.async_setup_entry(hass, entry))

    return unsub_mocks[0], unsub_mocks[1], nimbus_init.async_track_time_interval


def test_second_setup_for_same_entry_id_cancels_the_first_timer():
    first_unsub, second_unsub, tracker = _run_setup_entry_twice_for_same_entry()

    assert tracker.call_count == 2, (
        "expected async_track_time_interval to be called once per "
        "async_setup_entry() invocation"
    )
    first_unsub.assert_called_once(), (
        "the FIRST call's own periodic-solve timer was never cancelled "
        "when async_setup_entry() ran a second time for the same "
        "entry_id -- this is the exact #211 bug: two live timers both "
        "calling solver_runtime.async_run_solve() once a minute"
    )
    second_unsub.assert_not_called(), (
        "the second (still-live, should-survive) timer's own unsub was "
        "called -- it should still be the one live timer after two "
        "setup calls for the same entry_id"
    )


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
