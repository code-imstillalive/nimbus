"""Regression test for a live-reproduced bug (devhub, 2026-09-01, the
household's own long-deferred "number.nimbus_solver_* entities reset to
their schema placeholder minimum on some restarts" investigation):
HA's own "abandon a slow async_setup_entry() and silently retry it while
the original coroutine keeps running" behaviour let TWO genuinely
concurrent calls to async_setup_entry() run for the SAME entry_id --
confirmed live via a real `ha_get_logs` pull showing a hub-wide "Platform
nimbus_load does not generate unique IDs" collision storm across every
number/sensor/switch entity, ~4.5s after a first, successful setup
completed for that same entry.

Fix (see __init__.py's own _setup_tasks module-level comment): a second
call for the SAME entry_id, while a first is still genuinely in flight,
waits for the first attempt's own result instead of running the whole
setup body a second time.

This test proves the fix at the unit level: two "concurrent" calls to
async_setup_entry() for the same entry.entry_id must only run the real
setup body ONCE, and both calls must get back the SAME result -- while a
THIRD, genuinely later call (after the first has fully finished, e.g. a
real subsequent reload) must run the real setup body again fresh, proving
the guard doesn't wedge a config entry permanently.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import custom_components.nimbus_load as nimbus_init


def _make_hass() -> MagicMock:
    """A hass stand-in whose async_create_task() genuinely schedules the
    given coroutine as a real asyncio Task -- required for this test to
    exercise real concurrency (a plain MagicMock would never actually run
    the coroutine at all, defeating the point of this specific test)."""
    hass = MagicMock()
    hass.async_create_task = lambda coro, name=None: asyncio.ensure_future(coro)
    return hass


def test_concurrent_calls_for_the_same_entry_only_run_setup_once():
    nimbus_init._setup_tasks.clear()
    entry = MagicMock()
    entry.entry_id = "test-entry-reentrancy"

    call_count = 0
    release = asyncio.Event()

    async def _fake_impl(hass, entry) -> bool:
        nonlocal call_count
        call_count += 1
        # Genuinely yields control back to the event loop and stays
        # "in flight" until explicitly released -- this is what lets a
        # second call arrive while the first is still running, the exact
        # real-world shape of HA's own abandon-and-retry race.
        await release.wait()
        return True

    nimbus_init._async_setup_entry_impl = _fake_impl
    hass = _make_hass()

    async def _run() -> tuple[bool, bool]:
        first = asyncio.ensure_future(nimbus_init.async_setup_entry(hass, entry))
        # Let `first` actually start and reach its own await point before
        # the second call arrives -- real HA's own abandon-and-retry
        # timing is exactly this shape (the original attempt is already
        # mid-flight, not merely queued, when the retry begins).
        await asyncio.sleep(0)
        second = asyncio.ensure_future(nimbus_init.async_setup_entry(hass, entry))
        await asyncio.sleep(0)
        # Both calls are now genuinely waiting -- release the real body
        # and confirm both resolve to its one real result.
        release.set()
        return await first, await second

    result_first, result_second = asyncio.run(_run())

    assert call_count == 1, (
        f"expected the real setup body to run exactly once for two "
        f"concurrent calls on the same entry_id, ran {call_count} times -- "
        f"this is the exact live-confirmed duplicate-entity-creation bug"
    )
    assert result_first is True
    assert result_second is True


def test_a_later_call_after_the_first_finished_runs_setup_fresh():
    """The guard must not permanently wedge a config entry -- once a
    genuinely-completed setup's task is .done(), a real subsequent call
    (a normal reload, or a legitimate later re-entry) must run the setup
    body again, not silently reuse a stale finished result forever."""
    nimbus_init._setup_tasks.clear()
    entry = MagicMock()
    entry.entry_id = "test-entry-reentrancy-later-call"

    call_count = 0

    async def _fake_impl(hass, entry) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    nimbus_init._async_setup_entry_impl = _fake_impl
    hass = _make_hass()

    async def _run() -> None:
        result_a = await nimbus_init.async_setup_entry(hass, entry)
        result_b = await nimbus_init.async_setup_entry(hass, entry)
        assert result_a is True
        assert result_b is True

    asyncio.run(_run())

    assert call_count == 2, (
        f"expected two SEQUENTIAL (non-overlapping) calls to each run the "
        f"real setup body fresh, got {call_count} -- the guard must only "
        f"suppress genuinely CONCURRENT re-entry, never a normal later call"
    )


def test_setup_tasks_dict_is_cleaned_up_after_completion():
    """A real, if lower-stakes, hygiene check: the module-level dict this
    guard relies on must not accumulate a stale entry for an entry_id
    whose setup has already finished -- an unbounded leak here would be a
    real (if slow) memory/correctness concern on a long-running HA
    instance that reconfigures Nimbus many times over its lifetime."""
    nimbus_init._setup_tasks.clear()
    entry = MagicMock()
    entry.entry_id = "test-entry-reentrancy-cleanup"

    async def _fake_impl(hass, entry) -> bool:
        return True

    nimbus_init._async_setup_entry_impl = _fake_impl
    hass = _make_hass()

    asyncio.run(nimbus_init.async_setup_entry(hass, entry))

    assert entry.entry_id not in nimbus_init._setup_tasks, (
        "a completed setup's own task must be removed from _setup_tasks, "
        "not left behind indefinitely"
    )


if __name__ == "__main__":
    tests = [
        test_concurrent_calls_for_the_same_entry_only_run_setup_once,
        test_a_later_call_after_the_first_finished_runs_setup_fresh,
        test_setup_tasks_dict_is_cleaned_up_after_completion,
    ]
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
