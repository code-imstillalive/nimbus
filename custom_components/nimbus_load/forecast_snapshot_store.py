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


def _store_for(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, _STORE_VERSION, f"{DOMAIN}_{entry_id}_forecast_snapshots")


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

        store = _store_for(hass, entry_id)
        stored = await store.async_load() or {}
        if key in stored:
            return False  # already have today's -- the common path

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

        stored[key] = build_snapshot(
            times, solar, load, captured_at=now_local, horizon_hours=None
        )
        # Pruned on write rather than on read: this Store is rewritten whole,
        # so an unbounded dict would be re-serialised on every capture.
        await store.async_save(prune_snapshots(stored, keep_days=KEEP_DAYS))
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
        stored = await _store_for(hass, entry_id).async_load() or {}
    except Exception:
        _LOGGER.debug("Nimbus: forecast snapshot load failed", exc_info=True)
        return None
    value = stored.get(day_key)
    return value if isinstance(value, dict) else None
