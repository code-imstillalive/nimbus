"""Regression test for nimbus issue #365 (Mark Purcell, codebase review),
item 4's harder half: a dispatched executor solve is not interruptible --
cancelling whichever Task happens to be awaiting it (as async_unload_entry
already did for the startup-retry task) does not stop the underlying
worker thread once it has actually started running, so it can keep
calling ha_post_state() after platforms have been torn down.

solver_runtime.async_run_solve() now records the dispatched executor
future in a module-level _in_flight_future; wait_for_in_flight_solve()
(called from async_unload_entry(), before platform teardown) awaits it,
bounded by a timeout so unload/reload never blocks indefinitely.

Uses real asyncio.Future objects (matching hass.async_add_executor_job's
own real return type) rather than mocking away the underlying mechanism,
so this exercises the genuine ordering/timing behaviour, not a
reimplementation of it.
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


def _clear_solver_runtime_state() -> None:
    solver_runtime._solver_writer = None
    solver_runtime._last_solve_completed_monotonic = None
    solver_runtime._import_error_notified = False
    solver_runtime._price_latency_sensor = None
    solver_runtime._consecutive_lock_skips = 0
    solver_runtime._in_flight_future = None


def _make_entry(entry_id: str) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = {}
    return entry


class TestWaitForInFlightSolveDirectly:
    def teardown_method(self):
        _clear_solver_runtime_state()

    def test_no_op_when_nothing_is_in_flight(self):
        async def _run():
            solver_runtime._in_flight_future = None
            # Must return promptly -- if this ever blocked, the test
            # itself would hang/timeout rather than silently pass.
            await asyncio.wait_for(
                solver_runtime.wait_for_in_flight_solve(), timeout=1.0
            )

        asyncio.run(_run())

    def test_waits_for_a_genuinely_in_flight_future_to_resolve(self):
        events: list[str] = []

        async def _run():
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            solver_runtime._in_flight_future = future

            async def _resolve_after_a_tick():
                await asyncio.sleep(0.05)
                events.append("resolved")
                future.set_result(True)

            asyncio.ensure_future(_resolve_after_a_tick())
            await solver_runtime.wait_for_in_flight_solve(timeout=5.0)
            events.append("wait_returned")

        asyncio.run(_run())
        # The wait must not return BEFORE the future actually resolves.
        assert events == ["resolved", "wait_returned"]

    def test_times_out_without_raising_if_the_future_never_resolves(self):
        async def _run():
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            solver_runtime._in_flight_future = future
            # Must return (not hang, not raise) once the bounded timeout
            # elapses, even though `future` is never resolved.
            await asyncio.wait_for(
                solver_runtime.wait_for_in_flight_solve(timeout=0.05), timeout=2.0
            )

        asyncio.run(_run())

    def test_does_not_disturb_the_original_awaiter(self):
        # asyncio.shield() must protect the shared future from being
        # affected by wait_for_in_flight_solve()'s own bounded timeout --
        # the ORIGINAL caller (e.g. the periodic-solve callback) must
        # still get the real result even if our wait times out first.
        async def _run():
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            solver_runtime._in_flight_future = future

            async def _resolve_after_a_tick():
                await asyncio.sleep(0.1)
                future.set_result(True)

            asyncio.ensure_future(_resolve_after_a_tick())
            # Times out well before the future actually resolves.
            await solver_runtime.wait_for_in_flight_solve(timeout=0.01)
            # The original future itself must still be alive and able to
            # resolve normally afterwards.
            result = await future
            assert result is True

        asyncio.run(_run())


class TestAsyncRunSolveTracksInFlightFuture:
    def teardown_method(self):
        _clear_solver_runtime_state()

    def test_in_flight_future_is_set_during_the_call_and_cleared_after(self):
        async def _run():
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            hass = MagicMock()
            hass.async_add_executor_job = MagicMock(return_value=future)

            assert solver_runtime._in_flight_future is None
            run_task = asyncio.ensure_future(solver_runtime.async_run_solve(hass))
            # Poll rather than a single asyncio.sleep(0) tick -- how many
            # scheduling hops it takes for run_task to reach `await
            # future` isn't guaranteed to be exactly one under every
            # asyncio/pytest-plugin combination, so wait for the actual
            # condition instead of assuming a fixed number of yields.
            for _ in range(100):
                if solver_runtime._in_flight_future is future:
                    break
                await asyncio.sleep(0)
            assert solver_runtime._in_flight_future is future

            future.set_result(True)
            result = await run_task
            assert result is True
            assert solver_runtime._in_flight_future is None

        asyncio.run(_run())


class TestUnloadEntryWaitsForInFlightSolveBeforeTeardown:
    def teardown_method(self):
        _clear_solver_runtime_state()

    def test_platform_teardown_happens_only_after_the_in_flight_solve_resolves(self):
        events: list[str] = []

        async def _run():
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()
            solver_runtime._in_flight_future = future

            async def _resolve_after_a_tick():
                await asyncio.sleep(0.05)
                events.append("solve_resolved")
                future.set_result(True)

            asyncio.ensure_future(_resolve_after_a_tick())

            async def _unload_platforms(*args, **kwargs):
                events.append("platforms_torn_down")
                return True

            entry = _make_entry("test_entry_wait_for_in_flight")
            hass = MagicMock()
            hass.config_entries.async_unload_platforms = AsyncMock(
                side_effect=_unload_platforms
            )
            hass.services.has_service.return_value = True

            await nimbus_init.async_unload_entry(hass, entry)

        asyncio.run(_run())
        assert events == ["solve_resolved", "platforms_torn_down"]

    def test_unload_proceeds_even_if_the_in_flight_solve_times_out(self):
        # A solve that's still running well past the bounded timeout must
        # not block unload forever -- teardown proceeds anyway. Patches
        # wait_for_in_flight_solve itself (rather than the default
        # timeout constant, which is already bound into the function's
        # own signature at import time) with a short-timeout version.
        async def _run():
            loop = asyncio.get_event_loop()
            future: asyncio.Future = loop.create_future()  # never resolved
            solver_runtime._in_flight_future = future
            original = solver_runtime.wait_for_in_flight_solve

            async def _short_timeout_wait(timeout: float = 0.05):
                await original(timeout=0.05)

            entry = _make_entry("test_entry_wait_timeout")
            hass = MagicMock()
            hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
            hass.services.has_service.return_value = True

            solver_runtime.wait_for_in_flight_solve = _short_timeout_wait
            try:
                result = await asyncio.wait_for(
                    nimbus_init.async_unload_entry(hass, entry), timeout=2.0
                )
            finally:
                solver_runtime.wait_for_in_flight_solve = original

            assert result is True

        asyncio.run(_run())
