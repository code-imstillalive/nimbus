"""Real regression guard for a household-reported gap (2026-08-30): on a
full HA restart, the `number.nimbus_solver_*` required entities (battery
capacity, max charge/discharge, grid max import/export) restore their own
real values from the entity registry on their OWN schedule, independent of
Nimbus's own setup. The single "one immediate cycle at setup" call in
async_setup_entry() could race that restore and see `sensor.nimbus_solver_
config` still reporting `unconfigured` -- async_run_solve() has no retry of
its own, so losing that one race meant waiting out the full periodic cron
interval (up to 5 minutes) for a second chance. Confirmed live: a restart
at 00:50 UTC produced its first real solve at 00:55:00 UTC, a genuine
5-minute cron boundary, not an early retry succeeding.

Fix: `_async_run_solve_with_startup_retries()` calls async_run_solve() once
immediately, then retries a bounded number of times (short delay between
attempts) before giving up and leaving it to the periodic cron. This file
locks in:

1. A successful first attempt returns immediately -- no wasted retries,
   no wasted delay, on the common case (config already resolved).
2. A failing first attempt (then succeeding) retries with the configured
   delay between attempts, not immediately and not the full cron interval.
3. All-failing attempts give up after exactly _STARTUP_RETRY_ATTEMPTS
   calls -- a genuinely-unconfigured fresh install doesn't retry forever.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import (
    _STARTUP_RETRY_ATTEMPTS,
    _STARTUP_RETRY_DELAY_SECONDS,
    _async_run_solve_with_startup_retries,
)


async def _run(coro):
    return await coro


def test_successful_first_attempt_returns_immediately_with_no_retries():
    import asyncio

    run_solve = AsyncMock(return_value=True)
    with (
        patch(
            "custom_components.nimbus_load.solver_runtime.async_run_solve", run_solve
        ),
        patch(
            "custom_components.nimbus_load.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock,
    ):
        asyncio.run(_run(_async_run_solve_with_startup_retries(hass=None)))
    assert run_solve.call_count == 1
    sleep_mock.assert_not_called()


def test_retries_with_the_configured_delay_until_it_succeeds():
    import asyncio

    run_solve = AsyncMock(side_effect=[False, False, True])
    with (
        patch(
            "custom_components.nimbus_load.solver_runtime.async_run_solve", run_solve
        ),
        patch(
            "custom_components.nimbus_load.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock,
    ):
        asyncio.run(_run(_async_run_solve_with_startup_retries(hass=None)))
    assert run_solve.call_count == 3
    assert sleep_mock.call_count == 2
    for call in sleep_mock.call_args_list:
        assert call.args[0] == _STARTUP_RETRY_DELAY_SECONDS


def test_gives_up_after_the_configured_number_of_attempts():
    import asyncio

    run_solve = AsyncMock(return_value=False)
    with (
        patch(
            "custom_components.nimbus_load.solver_runtime.async_run_solve", run_solve
        ),
        patch(
            "custom_components.nimbus_load.asyncio.sleep", new=AsyncMock()
        ) as sleep_mock,
    ):
        asyncio.run(_run(_async_run_solve_with_startup_retries(hass=None)))
    assert run_solve.call_count == _STARTUP_RETRY_ATTEMPTS
    # One fewer sleep than attempts -- no delay after the final, giving-up attempt.
    assert sleep_mock.call_count == _STARTUP_RETRY_ATTEMPTS - 1
