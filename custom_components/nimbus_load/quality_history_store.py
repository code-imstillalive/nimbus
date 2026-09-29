"""A durable, dated record of each day's quality score -- nimbus issue #1355, step 2.

## The gap this closes

`sensor.nimbus_solver_quality_report` carries the day's score and its per-hour
detail (`hourly_regret`, `soc_discrepancy_hourly`, `j_*_hourly`) for **one**
day: the next day's publish replaces them. Its rolling `history` attribute keeps
only a handful of scalars per day, and #1248 showed it can be truncated. The
recorder keeps the state but not these attributes (they are excluded to stay
under the 16 KB cap). Once a day ages out of every one of those, it cannot be
re-derived: on the install that reported #1355, `rescore_history` for a
disputed day answered *"no usable real history for this day"*. #1318 hit the
same wall for 27 Sep's per-hour figures.

So a disputed day became permanently unsettleable. This file keeps a copy of
each day's score and its per-hour detail, independent of the attribute, the
recorder and the carry-forward ratchet.

## Shape, borrowed from #1289's forecast-snapshot layer on purpose

`forecast_snapshot_store.py` already solved the parts that are easy to get
wrong, so this copies its decisions rather than re-deriving them:

* **One HA `Store`, overwritten, never unlinked.** #1324 found that deleting a
  file inside `/config` races the backup walk and aborts the whole archive.
  Pruning happens inside the stored dict, so no file is ever removed.
* **Captured after a successful solve, on the event loop**, from the published
  entity -- the same numbers a household sees, not a second source of truth.
* **In-memory copy, written through** (#1295): the common cycle is a dict
  lookup, not a disk read.
* **Never raises.** A capture is a record-keeper; a solve that succeeded must
  never be reported as failed because a record could not be written.

## One deliberate difference: a day is REWRITTEN when its score changes

The forecast snapshot is write-once, because a forecast made at 09:00 is
different evidence from one made at 00:05. A score is the opposite: the
midnight figure is provisional and the ~06:00 rescore replaces it once P2P
settlement lands (on the reference household, 28 Sep read -44.23% at midnight
and 73.0% after settlement). Keeping the first write would archive exactly the
wrong number. So each day is re-captured whenever its fingerprint (score,
settlement status, provisional flag) changes, and `captured_at` plus
`provisional` say which version is on file.

## Retention

~20 KB per day with the three `j_*_hourly` series, which are the ones #1318
could not recover. **120 days** keeps the file around 2.4 MB -- well past the
reference household's 45-day recorder purge, which is the point, without
turning one JSON rewrite into a large one.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

KEEP_DAYS = 120

_STORE_VERSION = 1
_SOURCE_ENTITY = "sensor.nimbus_solver_quality_report"

#: Scalars copied as-is when present. Kept explicit so a new attribute does not
#: silently start being archived at 120x its size.
_SCALAR_KEYS: tuple[str, ...] = (
    "epr",
    "epr_pct",
    "epr_reliable",
    "epr_reason",
    "epr_denominator_reason",
    "j_ref",
    "j_ach",
    "j_star",
    "j_star_evaluator",
    "j_star_path_delta",
    "regret_dollars",
    "regret_path_delta_share",
    "theoretical_maximum_yield",
    "value_captured",
    "soc_discrepancy_max_pct",
    "soc_discrepancy_mean_pct",
    "soc_discrepancy_reliable",
    "soc_discrepancy_reason",
    "real_p2p_settlement_status",
    "real_p2p_dollars",
    "scored_participants",
    "nimbus_version",
)

#: The per-hour detail that ages out and cannot be rebuilt -- the reason this
#: file exists.
_HOURLY_KEYS: tuple[str, ...] = (
    "hourly_regret",
    "soc_discrepancy_hourly",
    "j_ref_hourly",
    "j_ach_hourly",
    "j_star_hourly",
)

_STORES: dict[str, Store[dict[str, Any]]] = {}
_CACHE: dict[str, dict[str, Any]] = {}


def build_quality_capture(
    attributes: dict[str, Any], *, captured_at: str
) -> tuple[str, dict[str, Any]] | None:
    """The (day, record) to archive from a published quality report, or None.

    None when there is no dated score to archive -- the entity is `unknown`
    after a restart, or carries only metadata -- because an empty record would
    later read as a scored day that had no data, which is worse than no record.
    """
    day = attributes.get("latest_date")
    if not isinstance(day, str) or not day or attributes.get("epr") is None:
        return None
    record: dict[str, Any] = {k: attributes[k] for k in _SCALAR_KEYS if k in attributes}
    for k in _HOURLY_KEYS:
        if k in attributes:
            record[k] = attributes[k]
    history = attributes.get("history")
    row = history.get(day) if isinstance(history, dict) else None
    # `"p": 1` is the in-row provisional marker (#1200); absent means settled.
    record["provisional"] = bool(isinstance(row, dict) and row.get("p"))
    record["captured_at"] = captured_at
    return day, record


def fingerprint(record: dict[str, Any]) -> tuple[Any, ...]:
    """What has to change for a day to be re-captured. Deliberately excludes
    `captured_at`, or every cycle would rewrite the file."""
    return (
        record.get("epr"),
        record.get("j_ach"),
        record.get("regret_dollars"),
        record.get("real_p2p_settlement_status"),
        record.get("provisional"),
    )


def prune(stored: dict[str, Any], *, keep_days: int) -> dict[str, Any]:
    """Keep the newest `keep_days` days. ISO dates sort chronologically."""
    if len(stored) <= keep_days:
        return stored
    return {k: stored[k] for k in sorted(stored)[-keep_days:]}


def _store_for(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    store = _STORES.get(entry_id)
    if store is None:
        store = Store(hass, _STORE_VERSION, f"{DOMAIN}_{entry_id}_quality_history")
        _STORES[entry_id] = store
    return store


async def _async_stored(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    cached = _CACHE.get(entry_id)
    if cached is not None:
        return cached
    stored = await _store_for(hass, entry_id).async_load() or {}
    _CACHE[entry_id] = stored
    return stored


async def async_capture_quality_day(hass: HomeAssistant, entry_id: str) -> bool:
    """Archive the published day's score if it is new or has changed.

    Returns True only when something was written. Never raises.
    """
    try:
        state = hass.states.get(_SOURCE_ENTITY)
        if state is None:
            return False
        built = build_quality_capture(
            dict(state.attributes), captured_at=dt_util.now().isoformat()
        )
        if built is None:
            return False
        day, record = built

        stored = await _async_stored(hass, entry_id)
        existing = stored.get(day)
        if isinstance(existing, dict) and fingerprint(existing) == fingerprint(record):
            return False

        # Copy before mutating, and update the cache only after the write
        # lands: a failed save must not leave memory claiming a record the
        # file does not hold (#1295's ordering).
        updated = prune({**stored, day: record}, keep_days=KEEP_DAYS)
        await _store_for(hass, entry_id).async_save(updated)
        _CACHE[entry_id] = updated
        _LOGGER.info(
            "Nimbus: archived the %s quality score (%s, %s) -- a durable copy "
            "that outlives the sensor's rolling history (nimbus issue #1355)",
            day,
            record.get("epr_pct", record.get("epr")),
            "provisional" if record["provisional"] else "settled",
        )
    except Exception:
        _LOGGER.debug("Nimbus: quality history capture skipped", exc_info=True)
        return False
    return True


async def async_load_quality_history(
    hass: HomeAssistant, entry_id: str
) -> dict[str, Any]:
    """Every archived day for this entry, `{}` if none. Never raises."""
    try:
        return dict(await _async_stored(hass, entry_id))
    except Exception:
        _LOGGER.debug("Nimbus: quality history load failed", exc_info=True)
        return {}


def reset_quality_history_cache() -> None:
    """Unload/test hook: module state must not leak between entries, and a
    `Store` holds a `hass` reference that must not outlive its instance."""
    _CACHE.clear()
    _STORES.clear()
