"""Speed, D (household, 22 Oct 2026): a tick or price
change that finds a solve still running is no longer dropped. The running
cycle runs ONE follow-up when it finishes; a follow-up never chains another,
and a cycle that starts clears any request made before it."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_runtime


class _Hass:
    """Runs each executor job inline, as one thread would."""

    def async_add_executor_job(self, fn, *args):
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        fut.set_result(fn(*args))
        return fut


def _sw(hass, ticks_during):
    """A writer whose main() sees `ticks_during[i]` other ticks arrive while
    cycle i holds the lock."""
    state = {"held": False, "cycle": 0}
    sw = MagicMock(spec=["acquire_lock", "release_lock", "main"])

    def acquire():
        if state["held"]:
            return False
        state["held"] = True
        return True

    def release():
        state["held"] = False

    def main():
        i = state["cycle"]
        state["cycle"] += 1
        for _ in range(ticks_during[i] if i < len(ticks_during) else 0):
            assert solver_runtime._run_one_cycle(hass) is False  # skipped

    sw.acquire_lock.side_effect = acquire
    sw.release_lock.side_effect = release
    sw.main.side_effect = main
    return sw


def _solve(ticks_during, *, requested_before=False):
    solver_runtime.reset_module_state()
    solver_runtime._rerun_requested = requested_before
    hass = _Hass()
    sw = _sw(hass, ticks_during)
    with (
        patch.object(solver_runtime, "_ensure_ready", return_value=sw),
        patch.object(solver_runtime, "_log_dispatch_dry_run"),
        patch.object(solver_runtime, "_entry_id_for_snapshot", return_value=None),
    ):
        ok = asyncio.run(solver_runtime.async_run_solve(hass))
    return ok, sw.main.call_count


def test_a_tick_during_a_solve_gets_one_follow_up():
    assert _solve([1]) == (True, 2)


def test_several_ticks_during_a_solve_coalesce_into_one():
    assert _solve([3]) == (True, 2)


def test_no_tick_no_follow_up():
    assert _solve([0]) == (True, 1)


def test_a_follow_up_never_chains_another():
    """Ticks during the follow-up too: still exactly two cycles. The request
    is left for the next ordinary cycle, which clears it as it starts."""
    assert _solve([1, 1]) == (True, 2)
    assert solver_runtime._rerun_requested is True


def test_a_cycle_that_starts_clears_an_earlier_request():
    assert _solve([0], requested_before=True) == (True, 1)
    assert solver_runtime._rerun_requested is False


def test_a_tick_from_another_real_thread_gets_one_follow_up():
    """Mark Purcell's review of #2010: the tests above run inline. Here the
    cycle runs on a real executor thread holding a real lock, and the tick
    arrives from a second real thread while it is held."""
    import threading

    solver_runtime.reset_module_state()
    lock = threading.Lock()
    in_main = threading.Event()
    release = threading.Event()
    calls = {"main": 0}

    sw = MagicMock(spec=["acquire_lock", "release_lock", "main"])
    sw.acquire_lock.side_effect = lambda: lock.acquire(blocking=False)
    sw.release_lock.side_effect = lock.release

    def main():
        calls["main"] += 1
        if calls["main"] == 1:
            in_main.set()
            assert release.wait(5), "the competing tick never arrived"

    sw.main.side_effect = main

    class _ThreadHass:
        def async_add_executor_job(self, fn, *args):
            return asyncio.get_running_loop().run_in_executor(None, fn, *args)

    hass = _ThreadHass()

    async def scenario():
        loop = asyncio.get_running_loop()
        solve = asyncio.ensure_future(solver_runtime.async_run_solve(hass))
        assert await loop.run_in_executor(None, in_main.wait, 5)
        skipped = await loop.run_in_executor(None, solver_runtime._run_one_cycle, hass)
        release.set()
        return await solve, skipped

    with (
        patch.object(solver_runtime, "_ensure_ready", return_value=sw),
        patch.object(solver_runtime, "_log_dispatch_dry_run"),
        patch.object(solver_runtime, "_entry_id_for_snapshot", return_value=None),
    ):
        ok, skipped = asyncio.run(scenario())
    assert skipped is False  # the competing tick found the cycle running
    assert ok is True
    assert calls["main"] == 2  # ...and got exactly one follow-up
    assert not lock.locked()
