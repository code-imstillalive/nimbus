"""Is the price source's forecast fresh? (nimbus #1793, Mark Purcell)

On 16 Oct 2026 LocalVolts stopped rebuilding the reference household's
interval data at 10:01 AEST. Every poll still succeeded and every entity's
`last_updated` kept moving, but each one carried the same rows, whose
`lastUpdate` stayed at 2026-10-16T00:01:55Z for more than two and a half
hours. Those rows held AEMO's 10:01 pre-dispatch, a false spike of $9-27/kWh
for 13:00-16:00 that AEMO and Amber withdrew within the hour. The plan kept
solving against it every minute and sold 36-39 kW at 12:30 into a forecast
of $1.01/kWh while dispatch was ~5c/kWh.

Measured on the same install over the 30 hours before it, the refresh stamp
is normally 0-28.4 minutes old (median 0.9; LocalVolts rebuilds at about
:02 and :32). `STALE_AFTER` is 60 minutes, about twice the worst normal age.

Where the stamp is read, in order, from each configured price entity:

1. `source_updated` -- purcell-lab/localvolts_v2#53, on every LV v2 sensor:
   the newest `lastUpdate` among the rows.
2. `lastUpdate` -- the household template sensors, and LV v2's
   `current_buy_rate` / `current_sell_rate`.
3. For an LV v2 entity on a release before #53, which carries neither, its
   siblings `sensor.localvolts_v2_current_buy_rate` / `_current_sell_rate`.

`last_update` (LV v2's FETCH time) is deliberately not read: it moved every
poll on 16 Oct while the data underneath was hours old.

With no stamp anywhere the source counts as fresh -- an install whose
provider publishes no refresh time is unchanged.

Pure apart from the `read_state` it is given: no `homeassistant` import.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=60)
STAMP_ATTRIBUTES = ("source_updated", "lastUpdate")
LV_V2_PREFIX = "sensor.localvolts_v2_"
LV_V2_STAMP_SIBLINGS = (
    "sensor.localvolts_v2_current_buy_rate",
    "sensor.localvolts_v2_current_sell_rate",
)
FALLBACK_LABEL = "aemo_fallback"


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _stamp(
    read_state: Callable[[str], dict[str, Any]], entity_id: str
) -> datetime | None:
    try:
        attrs = (read_state(entity_id) or {}).get("attributes") or {}
    except Exception as err:  # noqa: BLE001 -- an unreadable entity has no stamp
        _LOGGER.debug("Nimbus #1793: %s unreadable for freshness: %s", entity_id, err)
        return None
    for key in STAMP_ATTRIBUTES:
        stamp = _parse(attrs.get(key))
        if stamp is not None:
            return stamp
    return None


def source_updated(
    read_state: Callable[[str], dict[str, Any]], entity_ids: Iterable[str | None]
) -> tuple[datetime | None, str | None]:
    """(newest refresh stamp, the entity it came from) across the price
    entities, or (None, None) when none of them publishes one."""
    ids = [e for e in entity_ids if e]
    found: list[tuple[datetime, str]] = []
    for entity_id in ids:
        stamp = _stamp(read_state, entity_id)
        if stamp is not None:
            found.append((stamp, entity_id))
    if not found and any(e.startswith(LV_V2_PREFIX) for e in ids):
        for entity_id in LV_V2_STAMP_SIBLINGS:
            stamp = _stamp(read_state, entity_id)
            if stamp is not None:
                found.append((stamp, entity_id))
    if not found:
        return None, None
    stamp, entity_id = max(found, key=lambda pair: pair[0])
    return stamp, entity_id


def check(
    read_state: Callable[[str], dict[str, Any]],
    entity_ids: Iterable[str | None],
    now: datetime,
    stale_after: timedelta = STALE_AFTER,
) -> dict[str, Any] | None:
    """The source's freshness, or None when it publishes no refresh stamp.
    Never raises."""
    try:
        stamp, entity_id = source_updated(read_state, entity_ids)
    except Exception as err:  # noqa: BLE001 -- a freshness probe must never break a solve
        _LOGGER.warning("Nimbus #1793: price freshness check failed: %s", err)
        return None
    if stamp is None:
        return None
    age = now - stamp
    return {
        "stale": age > stale_after,
        "source_updated": stamp.isoformat(),
        "age_minutes": round(age.total_seconds() / 60.0, 1),
        "stale_after_minutes": round(stale_after.total_seconds() / 60.0, 1),
        "entity_id": entity_id,
    }


def aemo_series(
    grid_times: list[datetime],
    aemo_pts: list[tuple[datetime, float]],
    offset_by_5min: dict[int, float],
    local: Callable[[datetime], datetime],
    live_price: float | None = None,
) -> list[float] | None:
    """Every period priced from AEMO's forecast plus the learned per-5-minute
    retail markup -- the same rule `resample_price_with_extrapolation` uses
    beyond a source's horizon. The current 5-minute interval takes the live
    AEMO dispatch price plus the markup when one is given. None when AEMO has
    no point at or before a period, so the caller keeps what it had."""
    if not grid_times or not aemo_pts:
        return None
    pts = sorted(aemo_pts, key=lambda p: p[0])
    block_end = grid_times[0].replace(second=0, microsecond=0) + timedelta(
        minutes=5 - local(grid_times[0]).minute % 5
    )
    out: list[float] = []
    for gt in grid_times:
        bucket = local(gt).hour * 12 + local(gt).minute // 5
        markup = offset_by_5min.get(bucket, 0.0)
        if gt < block_end and live_price is not None:
            out.append(float(live_price + markup))
            continue
        value = None
        for t, v in pts:
            if t <= gt:
                value = v
            else:
                break
        if value is None:
            value = pts[0][1] if pts[0][0] - gt <= timedelta(minutes=30) else None
        if value is None:
            return None
        out.append(float(value + markup))
    return out
