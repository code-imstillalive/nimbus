"""Real regression test for a live-reproduced bug found on devhub, 2026-08-26:
`async_setup()` used to `await self._async_retrain()` INLINE whenever no
persisted model existed yet -- and `_async_retrain()` does several
sequential recorder history fetches plus a real ML training job, all
before `__init__.py`'s own per-subentry setup loop even reaches
`async_forward_entry_setups()` (i.e. before ANY entity is registered).

On an install with several subentries simultaneously lacking a persisted
model (confirmed live: devhub, right after a `.pkl` reset with the
mirror/topology/load subentries all still present), that sequential
blocking chain can take long enough to risk HA's own slow-setup timeout.
A real "Platform nimbus_load does not generate unique IDs" ERROR burst
(every hub-level number/switch/sensor entity duplicate-registering) was
captured live at exactly that moment -- consistent with HA abandoning/
retrying a setup that ran long, while the original attempt's still-
executing training work (executor jobs, which don't get interrupted by
task cancellation) finished and tried to register the same entities a
second time.

Fix: schedule `_async_retrain()` via `hass.async_create_task()` (fire and
forget) instead of awaiting it -- same pattern __init__.py already uses
for the Solver's own first cycle. `_async_update_data()` already returns
a well-defined, already-exercised "untrained" state dict whenever
`self._trained is None` (see its own docstring/code), so nothing
downstream needs training to have finished by the time hub setup
completes.

This test proves the fix at the unit level: `async_setup()` must return
promptly, without ever awaiting `_async_retrain()` itself, whenever no
model is persisted on disk.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.coordinator import NimbusCoordinator


def _make_bare_coordinator() -> NimbusCoordinator:
    """Same technique as test_coordinator_helpers.py's own
    _make_bare_coordinator(): bypass DataUpdateCoordinator's real
    __init__ chain, set only the attributes async_setup() itself reads.
    """
    return NimbusCoordinator.__new__(NimbusCoordinator)


def test_async_setup_schedules_retrain_in_the_background_when_untrained():
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    # Both disk-load steps report "nothing persisted yet" -- the exact
    # cold-start condition that used to trigger the inline, blocking
    # await self._async_retrain() call.
    coord.hass.async_add_executor_job = AsyncMock(return_value=None)
    coord.hass.async_create_task = MagicMock()
    # _retrain_hour is a @property reading self.entry.options -- can't be
    # set directly on the instance, has to go through a real-shaped entry.
    coord.entry = MagicMock()
    coord.entry.options = {}
    # subentry_id is the key async_setup() now tracks the retrain task
    # under (nimbus repo issue #211's own idempotent-retrain fix) -- a
    # unique string per test avoids cross-test pollution of the shared
    # module-level _retrain_tasks dict.
    coord.subentry = MagicMock()
    coord.subentry.subentry_id = "test-subentry-schedules-retrain"

    async def _run() -> None:
        await coord.async_setup()

    asyncio.run(_run())

    # The real assertion: async_setup() must hand the retrain coroutine
    # off to hass.async_create_task() -- NOT await it directly. If this
    # regresses back to a plain `await self._async_retrain()`, this
    # mock is never called at all (the call the old code made was a
    # bare coroutine await, invisible to this mock).
    assert coord.hass.async_create_task.call_count == 1, (
        "async_setup() must schedule retrain via hass.async_create_task(), "
        "not await it inline -- see this file's own module docstring for "
        "the real, live-reproduced bug this guards against."
    )

    # Confirm the scheduled coroutine really is _async_retrain() (not some
    # other accidental task), then close it without running it -- it was
    # never awaited by production code in this test (async_create_task is
    # mocked), so it must be closed explicitly or Python warns about an
    # unawaited coroutine.
    (scheduled_coro,), _kwargs = coord.hass.async_create_task.call_args
    assert inspect.iscoroutine(scheduled_coro)
    assert scheduled_coro.cr_code.co_name == "_async_retrain"
    scheduled_coro.close()

    # And the real behavioural guarantee this fix relies on: async_setup()
    # itself returned WITHOUT the retrain having actually run synchronously
    # -- _trained stays exactly what the (mocked) disk-load reported, None,
    # not something _async_retrain() would have set on a real completion.
    assert coord._trained is None
