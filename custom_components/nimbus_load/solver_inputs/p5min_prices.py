"""AEMO's 5-minute pre-dispatch in the prices the solve plans against
(nimbus #1653).

On 9 Oct 2026 the price spiked to $703-860/MWh from 04:55 to 05:30 AEST.
`sensor.nimbus_p5min_forecast` (#1668) showed it coming about half an hour
ahead; the retail forecast the solve reads did not. So the battery sold at
04:30, ran empty and imported through the spike.

Nothing here decides to charge. For each future period inside the P5MIN
horizon (about an hour), the plan's import and export prices become
P5MIN's wholesale price for that interval plus this install's own learned
retail markup for that 5-minute-of-day -- the same conversion the price
tail past the retail forecast has used since 2026-08-16
(`compute_5min_offset()`). The LP sees the higher price and does whatever
the arithmetic says. When a later run drops the spike, the next solve sees
the lower price and stops.

What is deliberately left alone:

- the settlement block in progress. Its price is the retail source's own
  settled state (#220), a fact rather than a forecast;
- network and flat fees, which are added afterwards as for every source;
- every period outside the P5MIN horizon.

The markup is additive, learned at ordinary prices, so a retailer whose
price scales with wholesale (e.g. LocalVolts sells at ~1.06 x RRP) is
understated during a spike by that percentage. It errs toward doing less,
not more, and is the same convention as the existing tail.

No-op -- the plan's prices exactly as before -- when: there is no P5MIN
entity (any non-NEM install), its run is stale, or a side has no learned
markup yet. Never raises into the solve.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]

_UNIQUE_ID_SUFFIX = "_p5min_forecast"
# AEMO publishes a run every 5 minutes. Three missed runs means the feed is
# not current, and a stale spike must not move the battery.
MAX_RUN_AGE = timedelta(minutes=15)
SOURCE_LABEL = "p5min"
# The markup moves over days, not seconds; the solve can run every 15 s.
_OFFSET_TTL_S = 1800.0
_offset_cache: dict[tuple[str, str], tuple[float, dict[int, float]]] = {}
_last_logged_run: str | None = None


def resolve_entity_id() -> str | None:
    """This install's own P5MIN entity, or None.

    Inside HA only. The sensor is created by this integration, and it is
    looked up by its unique_id rather than its plain id: on an install that
    also mirrors another HA (remote_homeassistant) the plain id can belong
    to the mirror, and this battery must not be priced off another
    install's feed. The standalone/cron writer has no registry and does not
    run this integration's sensors, so it gets no overlay."""
    hass = solver_shared.NATIVE.hass
    if hass is None:
        return None
    try:
        from homeassistant.helpers import entity_registry as er
    except ImportError:  # pragma: no cover - standalone/cron path
        return None
    registry = er.async_get(hass)
    found = [
        registry.async_get_entity_id(
            "sensor", "nimbus_load", f"{entry.entry_id}{_UNIQUE_ID_SUFFIX}"
        )
        for entry in hass.config_entries.async_entries("nimbus_load")
    ]
    ours = [entity_id for entity_id in found if entity_id]
    return ours[0] if len(ours) == 1 else None


def fresh_rows(state: dict | None, now: datetime) -> list[dict]:
    """The run's rows, or [] when there is no run or it is stale."""
    attrs = (state or {}).get("attributes") or {}
    run = attrs.get("run_datetime")
    rows = attrs.get("forecast") or []
    if not run or not rows:
        return []
    try:
        age = now - datetime.fromisoformat(run)
    except (TypeError, ValueError):
        return []
    return rows if age <= MAX_RUN_AGE else []


def overlay(
    values: list[float],
    sources: list[str],
    rows: list[dict],
    grid_times: list[datetime],
    not_before: datetime,
    offset_by_5min: dict[int, float],
) -> list[int]:
    """Replace `values[i]` in place for every period starting at or after
    `not_before` inside a P5MIN interval with that interval's wholesale
    price plus the markup for the period's 5-minute-of-day. Returns the
    indices changed. Pure apart from the two lists it is given."""
    if not offset_by_5min:
        return []
    mean = sum(offset_by_5min.values()) / len(offset_by_5min)
    spans = []
    for row in rows:
        try:
            spans.append(
                (
                    datetime.fromisoformat(row["start"]),
                    datetime.fromisoformat(row["end"]),
                    float(row["value"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    changed = []
    for i, t in enumerate(grid_times):
        if t < not_before:
            continue
        for start, end, wholesale in spans:
            if start <= t < end:
                local = solver_shared._local(t)
                bucket = local.hour * 12 + local.minute // 5
                values[i] = float(wholesale + offset_by_5min.get(bucket, mean))
                sources[i] = SOURCE_LABEL
                changed.append(i)
                break
    return changed


def _offset(
    retail_entity: str, wholesale_entity: str, offset_for: Callable
) -> dict[int, float]:
    key = (retail_entity, wholesale_entity)
    hit = _offset_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _OFFSET_TTL_S:
        return hit[1]
    offset = offset_for(retail_entity, wholesale_entity)
    _offset_cache[key] = (time.monotonic(), offset)
    return offset


def apply(
    cfg: dict,
    grid_times: list[datetime],
    not_before: datetime,
    now: datetime,
    import_side: tuple[list[float], list[str]],
    export_side: tuple[list[float], list[str]],
    offset_for: Callable,
) -> dict:
    """Overlay both sides in place. Returns what was done, for the log and
    the published plan; `{"applied": False, ...}` on every no-op path.

    `offset_for(retail_entity, wholesale_entity)` returns the learned
    5-minute-of-day markup. It is passed in by solver_writer rather than
    imported from it, so this module adds no seam back up the layer map."""
    try:
        entity = resolve_entity_id()
        if entity is None:
            return {"applied": False, "reason": "no_p5min_entity"}
        try:
            state = solver_shared.ha_get(entity)
        except Exception as err:  # noqa: BLE001 -- registered but no state yet
            solver_shared._LOGGER.debug("Nimbus #1653: %s unreadable: %s", entity, err)
            return {"applied": False, "reason": "no_p5min_state"}
        rows = fresh_rows(state, now)
        if not rows:
            return {"applied": False, "reason": "no_fresh_run"}
        # The markup against the configured regional price where there is
        # one; otherwise against the P5MIN sensor's own recorded state.
        wholesale = cfg.get("solver_regional_spot_current_price_sensor") or entity
        done = {}
        for name, key, (values, sources) in (
            ("import", "solver_import_price_sensor", import_side),
            ("export", "solver_export_price_sensor", export_side),
        ):
            retail = cfg.get(key)
            offset = _offset(retail, wholesale, offset_for) if retail else {}
            done[name] = len(
                overlay(values, sources, rows, grid_times, not_before, offset)
            )
        run = (state.get("attributes") or {}).get("run_datetime")
        global _last_logged_run
        if any(done.values()) and run != _last_logged_run:
            _last_logged_run = run
            solver_shared._LOGGER.info(
                "Nimbus #1653: P5MIN run %s priced %d import / %d export periods",
                run,
                done["import"],
                done["export"],
            )
        return {"applied": any(done.values()), "periods": done, "run": run}
    except Exception as err:  # noqa: BLE001 -- must never break a real solve
        solver_shared._LOGGER.warning(
            "Nimbus #1653: P5MIN price overlay failed: %s", err
        )
        return {"applied": False, "reason": "error"}


def rebase_export_bonus(
    bonus: list[float], export_before: list[float], export_after: list[float]
) -> list[float]:
    """Keep the P2P premium a premium OVER spot after the overlay.

    `export_bonus_price` was built as max(0, p2p_rate - spot) from the
    pre-overlay spot (solver_inputs/prices.py), so where the overlay raised
    spot inside a P2P block the old premium would pay the spike AND the full
    premium on the same kWh. Inside a block p2p_rate = old spot + old bonus,
    so the premium becomes max(0, that - new spot). Periods with no bonus or
    an unchanged price are returned as they were."""
    out = list(bonus)
    for i, b in enumerate(bonus):
        if b > 0 and export_after[i] != export_before[i]:
            out[i] = max(0.0, export_before[i] + b - export_after[i])
    return out
