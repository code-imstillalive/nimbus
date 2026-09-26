"""Persist one day-ahead forecast snapshot per local day.

nimbus issue #937, stage 2. Stage 1 (`solver_inputs/forecast_snapshot.py`)
built the pure shape; this gives it somewhere to live and a moment to be
written.

## Why once per day, and why the first solve of the day

The thing being scored is *"what did the forecast say about today, before today
happened"*. The first successful solve after local midnight is the closest
thing to that a 1-minute solver produces, and it needs no timer of its own:

* **self-healing** — a restart at 09:00 on a day with no snapshot yet still
  captures one, late but real, instead of losing the day entirely;
* **idempotent** — every later solve that day sees a snapshot already filed and
  does nothing, so the cost is one dict lookup per cycle;
* **no new trigger to get wrong** — #1217 is this repo's own reminder that a
  fixed-time job on every install is a thundering herd waiting to happen.

The cost is honest and worth stating: a snapshot captured at 09:00 after a
restart has already seen nine hours of the day it is forecasting, so it will
look better than a true midnight forecast. `captured_at` is stored precisely so
a consumer can tell, and refuse the day if it wants a clean comparison.

## Why it reads the published sensor rather than the solver's internals

`sensor.nimbus_solver_battery_forecast` already carries a `forecast` array with
`time`, `solar_kw` and `load_kw` per period — exactly the three fields a
snapshot needs, and the same numbers the LP actually saw. Reaching into
`solver_writer`'s internals would create a second source of truth for the same
arrays and a second thing to keep in step.

## Failure posture

Every failure is swallowed and logged at debug. A capture is a diagnostic; a
solve that succeeded must never be reported as failed because a snapshot could
not be written. The same posture `_async_rename_stale_forecast_entities()` and
the sign-convention check already take.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .solver_inputs.forecast_snapshot import (
    build_snapshot,
    day_key_for,
    prune_snapshots,
)

_LOGGER = logging.getLogger(__name__)

#: One entry per local day. 35 rather than 30 so the re-score service's own
#: 1-30 day window is fully covered with a few days of headroom -- a retention
#: exactly equal to the window would drop a day the moment a re-score reached
#: for it. See `prune_snapshots()`'s own note.
KEEP_DAYS = 35

_STORE_VERSION = 1
_SOURCE_ENTITY = "sensor.nimbus_solver_battery_forecast"


#: Per-entry `Store` instances, reused rather than reconstructed.
#: nimbus issue #1295 (Mark Purcell, IV&V #1289).
_STORES: dict[str, Store[dict[str, Any]]] = {}

#: Per-entry authoritative IN-MEMORY copy of the whole stored dict.
#:
#: This, not the Store instance, is what actually removes the per-cycle disk
#: read #1295 reports -- and the distinction is worth recording, because
#: reusing the instance alone does NOT remove it. Measured against HA's own
#: `helpers/storage.py` rather than assumed:
#:
#:   * `Store.async_load()` sets `self._load_future` and then clears it in a
#:     `finally`, so it de-duplicates CONCURRENT callers and caches nothing
#:     across sequential ones;
#:   * `_async_load_data()` short-circuits on `self._data` only while a write
#:     is PENDING (the `async_delay_save` debounce), which is a write-side
#:     cache, not a read-side one;
#:   * the store manager's own cache is consulted next -- but
#:     `_StoreManager.async_invalidate(key)` is called whenever a Store saves,
#:     "to ensure that the cache is not used after that", so the very first
#:     capture write permanently invalidates it for this key.
#:
#: Net: instance reuse fixes the object churn and restores the write
#: debounce; only holding the data ourselves fixes the read. Both are done,
#: because both were real.
_CACHE: dict[str, dict[str, Any]] = {}


def _store_for(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The one `Store` for this entry, constructed once.

    nimbus issue #1295: this used to build a fresh `Store` on every call --
    roughly 288 objects a day at a 5-minute cadence, forever -- and a fresh
    instance also has no pending-write state, so it defeated the delayed-save
    debounce that a persistent instance provides.
    """
    store = _STORES.get(entry_id)
    if store is None:
        store = Store(hass, _STORE_VERSION, f"{DOMAIN}_{entry_id}_forecast_snapshots")
        _STORES[entry_id] = store
    return store


async def _async_stored(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """The stored snapshots for this entry, read from disk at most once.

    After the first call this is a dict lookup -- which is what the stage 2
    commit message claimed the common path already was, and #1295 correctly
    established it was not. Every mutation goes through
    `_async_save_stored()`, so the in-memory copy and the file cannot
    silently diverge.
    """
    cached = _CACHE.get(entry_id)
    if cached is not None:
        return cached
    stored = await _store_for(hass, entry_id).async_load() or {}
    _CACHE[entry_id] = stored
    return stored


async def _async_save_stored(
    hass: HomeAssistant, entry_id: str, stored: dict[str, Any]
) -> None:
    """Persist, then update the in-memory copy.

    That order matters. A failed write leaves the cache matching what is
    actually on disk, rather than claiming a snapshot that was never stored
    -- which would make every later cycle skip a capture it still owed, and
    lose the day silently. Losing a day is the one cost this whole feature
    exists to avoid.
    """
    await _store_for(hass, entry_id).async_save(stored)
    _CACHE[entry_id] = stored


def _rows_from_published_forecast(hass: HomeAssistant) -> list[dict[str, Any]]:
    state = hass.states.get(_SOURCE_ENTITY)
    if state is None:
        return []
    rows = state.attributes.get("forecast")
    return rows if isinstance(rows, list) else []


async def async_capture_todays_snapshot(hass: HomeAssistant, entry_id: str) -> bool:
    """Capture today's forecast if it has not been captured yet.

    Returns True only when a snapshot was genuinely written, so a caller can
    log the first capture of the day without logging every no-op cycle after
    it. Never raises.
    """
    try:
        now_local = dt_util.now()
        key = day_key_for(now_local)

        stored = await _async_stored(hass, entry_id)
        if key in stored:
            # The common path, and now genuinely a dict lookup rather than a
            # full disk read plus JSON parse on every cycle (#1295).
            return False

        rows = _rows_from_published_forecast(hass)
        if not rows:
            # No published forecast yet (a solve that failed, or the very
            # first cycle after a restart). Not an error: the next cycle a
            # minute later will find one, and the day is only lost if every
            # cycle fails.
            return False

        times: list[Any] = []
        solar: list[float] = []
        load: list[float] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            when = dt_util.parse_datetime(str(row.get("time")))
            if when is None:
                continue
            try:
                solar.append(float(row["solar_kw"]))
                load.append(float(row["load_kw"]))
            except (KeyError, TypeError, ValueError):
                continue
            times.append(when)
        if not times:
            return False

        # Copied before mutating, so a failed save cannot leave the
        # in-memory copy holding a snapshot that never reached disk.
        stored = dict(stored)
        stored[key] = build_snapshot(
            times, solar, load, captured_at=now_local, horizon_hours=None
        )
        # Pruned on write rather than on read: this Store is rewritten whole,
        # so an unbounded dict would be re-serialised on every capture.
        await _async_save_stored(
            hass, entry_id, prune_snapshots(stored, keep_days=KEEP_DAYS)
        )
        _LOGGER.info(
            "Nimbus: captured today's day-ahead forecast snapshot for %s "
            "(%d points) -- this is what tomorrow's forecast-regret "
            "decomposition scores against (nimbus issue #937)",
            key,
            len(times),
        )
    except Exception:
        _LOGGER.debug("Nimbus: forecast snapshot capture skipped", exc_info=True)
        return False
    return True


async def async_load_snapshot(
    hass: HomeAssistant, entry_id: str, day_key: str
) -> dict[str, Any] | None:
    """The snapshot filed for `day_key`, or None.

    **None means "skip the decomposition for this day entirely"**, never
    "assume zero forecast error" -- a missing forecast says nothing about
    forecast quality, whereas a zero-filled one scores as the worst forecast
    possible. Same contract the standalone writer's own
    `load_forecast_snapshot()` states.
    """
    try:
        stored = await _async_stored(hass, entry_id)
    except Exception:
        _LOGGER.debug("Nimbus: forecast snapshot load failed", exc_info=True)
        return None
    value = stored.get(day_key)
    return value if isinstance(value, dict) else None


# nimbus issue #937 stage 3. The scorer runs on a WORKER THREAD inside
# sw.main(), where a Store read cannot be awaited -- so the async side makes
# sure the data is in memory before dispatching the executor job, and the
# scorer then reads it synchronously from there.
#
# Deliberately reads `_CACHE` above rather than keeping a second day-keyed
# cache of its own. Two caches of one artefact is two things that can
# disagree, and this disagreement would be invisible: a stale snapshot scores
# as a real forecast rather than as a missing one, which is the single most
# misleading thing this mechanism could do.


async def async_ensure_snapshots_loaded(hass: HomeAssistant, entry_id: str) -> None:
    """Make sure this entry's snapshots are in memory. Never raises.

    Called from the async side before the solve is dispatched to the
    executor. After the first call it is a dict lookup, so calling it every
    cycle is cheap -- and that is the point: the scorer must never find an
    empty cache on the one cycle that needed it.
    """
    try:
        await _async_stored(hass, entry_id)
    except Exception:
        _LOGGER.debug("Nimbus: forecast snapshot load failed", exc_info=True)


def get_cached_snapshot(day_key: str) -> dict[str, Any] | None:
    """The stored snapshot for `day_key`, read synchronously.

    **None means "skip the decomposition for this day"**, never "assume zero
    forecast error" -- the same contract every other layer of this feature
    states, and the one that matters most here, because a zero-filled
    forecast scores as the worst forecast possible rather than as an absent
    one.

    Resolves the entry the same way `solver_runtime._entry_id_for_snapshot()`
    does: only when there is exactly one. With two entries cached there is no
    honest way to tell from here whose forecast this is, and guessing would
    score one install's day against another install's forecast -- so it
    declines instead, and the report says the snapshot was missing.
    """
    if len(_CACHE) != 1:
        return None
    stored = next(iter(_CACHE.values()))
    value = stored.get(day_key)
    return value if isinstance(value, dict) else None


def reset_snapshot_cache() -> None:
    """Test/unload hook -- module state must not leak between config entries
    or between tests, the same reason solver_runtime has reset_module_state().

    Drops the `Store` instances too, not just the data: a `Store` holds a
    `hass` reference, so keeping one past unload would pin a dead instance
    for the life of the process.
    """
    _CACHE.clear()
    _STORES.clear()
