"""IV&V finding (4fd40c3..22205ad pass, 2026-09-26, head issue #1289, this
finding #1295): `async_capture_todays_snapshot()`
(`forecast_snapshot_store.py`) is called once per solve cycle (every ~1
minute, per `solver_runtime.async_run_solve()`'s own wiring). Its own
docstring states the intended cost of the common, already-captured path:

    "idempotent -- every later solve that day sees a snapshot already filed
    and does nothing, so the cost is one dict lookup per cycle"

That is not what the code does. `_store_for()` constructs a brand-new
`Store` object on every single call, and the function then unconditionally
`await`s `store.async_load()` before doing the "one dict lookup" the
docstring describes. A fresh `Store` instance has no in-memory `_data`
cache to hit, so HA's own `Store.async_load()` re-reads and re-parses the
file from disk every cycle, all day, forever -- not the "one dict lookup"
the docstring promises, and not a cost that scales down once today's entry
exists.

FIXED 2026-09-26 (PR for #937 stage 3). Un-xfailed here, and kept as a
permanent regression test rather than deleted -- this is the assertion that
would catch the cost silently coming back.

What the fix actually needed, because the finding's own proposed remedy
(reuse the `Store` instance) is necessary but NOT sufficient, measured
against HA's own `helpers/storage.py`:

  * `Store.async_load()` sets `self._load_future` and clears it in a
    `finally`, so it de-duplicates CONCURRENT callers and caches nothing
    across sequential ones;
  * `_async_load_data()` short-circuits on `self._data` only while a write is
    PENDING (the `async_delay_save` debounce) -- a write-side cache;
  * the store manager's cache is consulted next, but
    `_StoreManager.async_invalidate(key)` runs whenever a Store saves, "to
    ensure that the cache is not used after that" -- so the very first
    capture write permanently invalidates it for this key.

So instance reuse fixes the object churn and restores the write debounce,
and a module-level per-entry data cache (`_CACHE`, cleared by
`reset_module_state()` on unload) is what actually removes the read. Both
landed, because both were real.

This test tracks how many times the underlying store is actually asked to
load across two solve cycles on the same day, using the same
`_FakeStore`/`_run()` pattern as
`tests/test_937_forecast_snapshot_store.py`. The existing suite only
asserts `store.saves == []` on the no-op path (nothing is WRITTEN) -- it
never asserts anything about how many times the store is READ, which is
the actual cost this finding is about.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import forecast_snapshot_store as fss

BNE = timezone(timedelta(hours=10))


class _FakeStore:
    def __init__(self, initial: dict | None = None):
        self.data = dict(initial or {})
        self.saves: list[dict] = []
        self.loads = 0

    async def async_load(self):
        self.loads += 1
        return dict(self.data)

    async def async_save(self, data):
        self.saves.append(data)
        self.data = dict(data)


class _State:
    def __init__(self, attributes):
        self.attributes = attributes


class _States:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, entity_id):
        return self._m.get(entity_id)


class _Hass:
    def __init__(self, rows=None):
        attrs = {"forecast": rows} if rows is not None else {}
        self.states = _States(
            {fss._SOURCE_ENTITY: _State(attrs)} if rows is not None else {}
        )


def _rows(n=6, start=None):
    start = start or datetime(2026, 9, 26, 0, 0, tzinfo=BNE)
    return [
        {
            "time": (start + timedelta(minutes=5 * i)).isoformat(),
            "solar_kw": float(i),
            "load_kw": 1.0 + i,
        }
        for i in range(n)
    ]


def _run(hass, store, *, now=None, entry="hub1"):
    now = now or datetime(2026, 9, 26, 0, 1, tzinfo=BNE)
    real_store_for, real_now = fss._store_for, fss.dt_util.now
    fss._store_for = lambda _h, _e: store
    fss.dt_util.now = lambda: now
    try:
        return asyncio.run(fss.async_capture_todays_snapshot(hass, entry))
    finally:
        fss._store_for, fss.dt_util.now = real_store_for, real_now


class TestTheNoOpPathDoesNotReReadTheStore(unittest.TestCase):
    def test_a_second_solve_the_same_day_does_not_reload_the_store(self):
        store = _FakeStore()
        hass = _Hass(_rows())

        first = _run(hass, store)
        self.assertTrue(first, "first solve of the day must capture")
        self.assertEqual(store.loads, 1)

        second = _run(hass, store)
        self.assertFalse(second, "second solve the same day must be a no-op")
        self.assertEqual(
            store.loads,
            1,
            f"expected the already-captured day to be served from an "
            f"in-memory cache with no further Store read, but the store was "
            f"loaded {store.loads} times across two solve cycles on the "
            f"same day",
        )


if __name__ == "__main__":
    unittest.main()
