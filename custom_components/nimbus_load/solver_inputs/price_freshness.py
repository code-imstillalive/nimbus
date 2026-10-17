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
3. For any LV v2 entity, its siblings `sensor.localvolts_v2_current_buy_rate`
   / `_current_sell_rate` -- always, not only when the others are missing.

The OLDEST stamp found decides. LV v2 2.11.0's `source_updated` could read
a stall as fresh: during 16 Oct's stall LocalVolts restamped the elapsed
rows at 00:30:34Z, and 2.11.0 took the newest stamp of all rows (fixed in
2.11.1, purcell-lab/localvolts_v2#55; Mark Purcell on #1793). The current
row's `lastUpdate` on `current_buy_rate` read 00:01:56Z at 12:42 that day,
so checking it as well, and trusting the oldest, catches the stall on
every LV v2 release.

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
    """(oldest refresh stamp, the entity it came from) across the price
    entities and, for LV v2, its current-rate siblings; (None, None) when
    none of them publishes one."""
    ids = [e for e in entity_ids if e]
    found: list[tuple[datetime, str]] = []
    for entity_id in ids:
        stamp = _stamp(read_state, entity_id)
        if stamp is not None:
            found.append((stamp, entity_id))
    if any(e.startswith(LV_V2_PREFIX) for e in ids):
        for entity_id in LV_V2_STAMP_SIBLINGS:
            if entity_id in ids:
                continue
            stamp = _stamp(read_state, entity_id)
            if stamp is not None:
                found.append((stamp, entity_id))
    if not found:
        return None, None
    stamp, entity_id = min(found, key=lambda pair: pair[0])
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


# ---- the same rule over a past day, for scoring (nimbus #1825) -------------
#
# On 16 Oct 2026 the daily quality report scored the reference household's
# day against the stalled rows above as if they were real: about 40 kWh
# "sold" into a $1-10/kWh spike that never existed, published as EPR 43.4%
# and $28.93 regret, $33 of it from 12:00-13:00.
#
# The recorder keeps a row only when a state CHANGES, so a stamp's age is
# read at the moment its row was recorded, never at the period being
# scored: a buy rate that holds 0.0 all evening records nothing, and an age
# measured from its last row would grow for hours while LocalVolts was
# perfectly fresh (measured: 17:35-20:15 and 21:05-23:55 on 16 Oct read
# "stale" that way). Read at its record time, 16 Oct is stale 11:10-13:20,
# and 13-15 Oct never pass 57 minutes.


def stale_period_mask(
    stamp_rows: Iterable[Iterable[tuple[datetime, datetime]]],
    grid_times: list[datetime],
    stale_after: timedelta = STALE_AFTER,
) -> list[bool]:
    """Per period, whether the price source was stale.

    `stamp_rows` holds, for each price entity, its recorded states as
    (recorded at, refresh stamp). A period is stale when, for any entity,
    the latest row at or before it carried a stamp more than `stale_after`
    old when it was recorded: the oldest stamp decides, as in `check()`.
    An entity with no row yet says nothing about that period."""
    ages: list[list[tuple[datetime, timedelta]]] = [
        sorted((recorded, recorded - stamp) for recorded, stamp in rows)
        for rows in stamp_rows
    ]
    ages = [rows for rows in ages if rows]
    out: list[bool] = []
    cursor = [0] * len(ages)
    latest: list[timedelta | None] = [None] * len(ages)
    for gt in grid_times:
        stale = False
        for i, rows in enumerate(ages):
            while cursor[i] < len(rows) and rows[cursor[i]][0] <= gt:
                latest[i] = rows[cursor[i]][1]
                cursor[i] += 1
            age = latest[i]
            if age is not None and age > stale_after:
                stale = True
        out.append(stale)
    return out


def stale_windows(
    grid_times: list[datetime],
    mask: list[bool],
    step: timedelta,
    local: Callable[[datetime], datetime],
) -> list[str]:
    """The stale runs as local "HH:MM-HH:MM" (end exclusive)."""
    out: list[str] = []
    start: datetime | None = None
    for i, gt in enumerate(grid_times):
        if mask[i] and start is None:
            start = gt
        if start is not None and (not mask[i] or i == len(grid_times) - 1):
            end = gt if not mask[i] else gt + step
            out.append(f"{local(start):%H:%M}-{local(end):%H:%M}")
            start = None
    return out


def parse_stamp(value: Any) -> datetime | None:
    """A refresh stamp as the recorder stored it, or None."""
    return _parse(value)
