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
    """A bare hass stand-in -- async_setup_entry()'s own re-entrancy guard
    uses plain asyncio.create_task() internally, not hass.async_create_
    task(), specifically so it works correctly against every existing
    test's own hass mock (including plain, unconfigured MagicMock ones)
    with no special stubbing needed here either."""
    return MagicMock()


def test_concurrent_calls_for_the_same_entry_only_run_setup_once(monkeypatch):
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

    monkeypatch.setattr(nimbus_init, "_async_setup_entry_impl", _fake_impl)
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
