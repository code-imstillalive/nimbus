"""Regression test for nimbus repo issue #211: a second, independent
NimbusCoordinator object for the SAME subentry_id (the exact shape a
re-entrant async_setup_entry() call produces -- see __init__.py's own
_solver_timer_unsub comment for the full "HA abandons and retries a slow
async_setup_entry()" story) must not leave two live, untracked cold-start
retrain tasks running for that subentry.

PR #210 backgrounded the cold-start retrain via a bare
hass.async_create_task(self._async_retrain()) specifically so it wouldn't
block hub setup -- but never tracked or cancelled it, unlike the periodic
solve timer #213 later fixed the same way for __init__.py's own
_solver_timer_unsub. self._retraining (an instance attribute) only guards
a SINGLE coordinator object against retraining twice concurrently; it does
nothing for a second, independent coordinator object.

This test proves the fix: coordinator.py's own module-level _retrain_tasks
dict (keyed by subentry_id, mirroring _solver_timer_unsub's own pattern)
cancels an old coordinator's still-running retrain task the moment a new
coordinator for the same subentry_id schedules its own -- using real
asyncio.Task objects (not MagicMocks, since real cancellation semantics
are exactly what's under test), same established stub-and-drive-async-
setup technique as test_init_periodic_solve_timer_idempotent.py.
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
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load.coordinator import NimbusCoordinator

_SUBENTRY_ID = "test-subentry-idempotent-retrain"


async def _slow_retrain() -> None:
    # Stands in for the real _async_retrain() (sequential recorder fetches
    # + real ML training) -- long enough that a "second setup races the
    # first" test can reliably observe the first task still in flight
    # before cancelling it, without needing to mock out that whole body.
    await asyncio.sleep(10)


def _make_bare_coordinator(loop: asyncio.AbstractEventLoop) -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()
    coord.hass.async_add_executor_job = AsyncMock(return_value=None)
    # Real asyncio.Task objects via the real running loop -- a MagicMock
    # stand-in for async_create_task would auto-satisfy `.done()`/`.cancel()`
    # without exercising any real cancellation semantics, defeating the
    # point of this test.
    coord.hass.async_create_task = lambda coro: loop.create_task(coro)
    coord.entry = MagicMock()
    coord.entry.options = {}
    coord.subentry = MagicMock()
    coord.subentry.subentry_id = _SUBENTRY_ID
    coord._retraining = False
    coord._async_retrain = _slow_retrain
    return coord


def test_second_coordinator_for_same_subentry_cancels_first_retrain_task():
    async def _run() -> None:
        loop = asyncio.get_running_loop()
        coordinator_module._retrain_tasks.pop(_SUBENTRY_ID, None)

        first = _make_bare_coordinator(loop)
        await first.async_setup()
        first_task = coordinator_module._retrain_tasks[_SUBENTRY_ID]
        # Let the scheduled task actually start running (it awaits
        # async_add_executor_job, an AsyncMock -- gives the event loop a
        # real chance to begin it) before the "second setup" race hits.
        await asyncio.sleep(0)
        assert not first_task.done(), (
            "test setup issue: first retrain task finished before the "
            "second coordinator's own setup could race it"
        )

        second = _make_bare_coordinator(loop)
        await second.async_setup()
        # cancel() only requests cancellation -- give the event loop a tick
        # to actually deliver CancelledError into the task before checking.
        await asyncio.sleep(0)

        # The real assertion: the first coordinator's own task is now
        # cancelled, and only the second's is tracked -- exactly the same
        # "cancel old before registering new" guarantee
        # _solver_timer_unsub's own periodic-solve fix already provides.
        assert first_task.cancelled() or first_task.done()
        second_task = coordinator_module._retrain_tasks[_SUBENTRY_ID]
        assert second_task is not first_task

        # Cleanup: don't leak a live task past this test.
        second_task.cancel()
        try:
            await second_task
        except asyncio.CancelledError:
            pass
        coordinator_module._retrain_tasks.pop(_SUBENTRY_ID, None)

    asyncio.run(_run())


def test_async_unload_cancels_the_tracked_retrain_task():
    async def _run() -> None:
        loop = asyncio.get_running_loop()
        coordinator_module._retrain_tasks.pop(_SUBENTRY_ID, None)

        coord = _make_bare_coordinator(loop)
        coord._unsub_retrain = None
        await coord.async_setup()
        task = coordinator_module._retrain_tasks[_SUBENTRY_ID]
        await asyncio.sleep(0)
        assert not task.done()

        coord.async_unload()

        assert _SUBENTRY_ID not in coordinator_module._retrain_tasks
        await asyncio.sleep(0)
        assert task.cancelled()

    asyncio.run(_run())
