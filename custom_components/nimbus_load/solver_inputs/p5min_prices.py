"""AEMO's 5-minute pre-dispatch in the prices the solve plans against
(nimbus #1653).

On 9 Oct 2026 the price spiked to $703-860/MWh from 04:55 to 05:30 AEST.
`sensor.nimbus_p5min_forecast` (#1668) showed it coming about half an hour
ahead; the retail forecast the solve reads did not. So the battery sold at
04:30, ran empty and imported through the spike.

Nothing here decides to charge. For each future period inside the P5MIN
horizon (about an hour), the plan's import and export prices become this
install's retail price for P5MIN's wholesale price in that interval. The LP
sees the higher price and does whatever the arithmetic says. When a later
run drops the spike, the next solve sees the lower price and stops.

**Wholesale to retail** is learned per install, per side, from its own
recorded history (`fit_markup()`): retail = slope x wholesale + an amount
per half-hour of day. Measured on the reference install, 10 Oct 2026, paired
by interval: export is 1.0605 x RRP to within 0.05 c/kWh, import 1.1666 x
RRP + network. A flat markup (slope 1) understated the 9 Oct import price by
about 14 c/kWh at the spike. And pairing each retail sample with the
wholesale sample "at or before" it matched LocalVolts' price (posted ~15 s
into an interval) with the PREVIOUS interval's AEMO price (posted ~78 s in),
and kept LocalVolts' provisional first post. So both feeds are reduced to
the LAST value recorded in each 5-minute interval and paired on the
interval. A retailer with a pure margin fits slope ~1 and loses nothing.

What is deliberately left alone:

- the settlement block in progress. Its price is the retail source's own
  settled state (#220), a fact rather than a forecast;
- network and flat fees, which are added afterwards as for every source;
- every period outside the P5MIN horizon.

No-op -- the plan's prices exactly as before -- when: there is no P5MIN
entity (any non-NEM install), its run is stale, or a side has under a day of
paired history to learn from. Never raises into the solve.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]

_UNIQUE_ID_SUFFIX = "_p5min_forecast"


def _half_hour(local: datetime) -> int:
    return local.hour * 2 + local.minute // 30


@dataclass(frozen=True)
class Markup:
    """retail = slope x wholesale + intercept[half-hour of day]."""

    slope: float
    intercept: dict[int, float]
    n_pairs: int

    def retail(self, wholesale: float, when: datetime) -> float:
        default = statistics.fmean(self.intercept.values())
        bucket = _half_hour(solver_shared._local(when))
        return self.slope * wholesale + self.intercept.get(bucket, default)


_markup_cache: dict[tuple[str, str], tuple[float, Markup | None]] = {}
# AEMO publishes a run every 5 minutes. Three missed runs means the feed is
# not current, and a stale spike must not move the battery.
MAX_RUN_AGE = timedelta(minutes=15)
SOURCE_LABEL = "p5min"
# The markup moves over days, not seconds; the solve can run every 15 s.
_MARKUP_TTL_S = 1800.0
# Fit needs at least a day of paired 5-minute intervals.
MIN_PAIRS = 288
# Outside this the data is not telling us a slope, and slope 1 (a pure
# margin) is the honest fallback.
SLOPE_RANGE = (0.5, 2.0)
# Six 5-minute intervals: the last half-hour of that time of day recorded.
RECENT_PER_BUCKET = 6
_INTERVAL_S = 300
_last_logged_run: str | None = None
_last_reason: str | None = None
# Its own logger, so `logger.set_level` can turn this module up alone.
_LOGGER = logging.getLogger(__name__)


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


def settled_by_interval(
    history: list[tuple[datetime, float]],
) -> dict[int, tuple[datetime, float]]:
    """The LAST value recorded in each 5-minute interval, keyed by interval.

    LocalVolts posts a provisional price at the start of an interval and the
    real one seconds later (9 Oct 05:05: 0.182 at :03, then 0.912 at :50);
    AEMO's own current-price sensor updates about 78 s in. The last value is
    the one each feed settled on."""
    out: dict[int, tuple[datetime, float]] = {}
    for t, v in sorted(history, key=lambda p: p[0]):
        key = int(t.timestamp()) // _INTERVAL_S
        out[key] = (datetime.fromtimestamp(key * _INTERVAL_S, t.tzinfo), float(v))
    return out


def _slope(pairs: list[tuple[datetime, float, float]]) -> float:
    """Least squares with a separate intercept per half-hour OF EACH DAY, so
    the slope comes only from retail moving WITH wholesale inside the same
    half-hour on the same day. Never from a time-of-day network step, and
    never from a tariff that changed between days -- LocalVolts moved the
    reference install from TOU network (22.3 c/kWh 16:00-21:00) to a flat
    7.5 c/kWh on 9 Oct 2026, inside the learning window."""
    groups: dict[tuple[object, int], list[tuple[float, float]]] = {}
    for when, x, y in pairs:
        local = solver_shared._local(when)
        groups.setdefault((local.date(), _half_hour(local)), []).append((x, y))
    sxx = sxy = 0.0
    for rows in groups.values():
        mx = statistics.fmean(x for x, _ in rows)
        my = statistics.fmean(y for _, y in rows)
        for x, y in rows:
            sxx += (x - mx) ** 2
            sxy += (x - mx) * (y - my)
    slope = sxy / sxx if sxx > 1e-6 else 1.0
    return slope if SLOPE_RANGE[0] <= slope <= SLOPE_RANGE[1] else 1.0


def _intercepts(
    pairs: list[tuple[datetime, float, float]], slope: float
) -> dict[int, float]:
    """Per half-hour of day, the median of (retail - slope x wholesale) over
    that half-hour's most recent RECENT_PER_BUCKET intervals: the CURRENT
    tariff, not a week's average of an old and a new one. Median, so one
    glitched interval does not move it."""
    by: dict[int, list[tuple[datetime, float]]] = {}
    for when, x, y in pairs:
        bucket = _half_hour(solver_shared._local(when))
        by.setdefault(bucket, []).append((when, y - slope * x))
    return {
        b: statistics.median(v for _, v in sorted(rows)[-RECENT_PER_BUCKET:])
        for b, rows in by.items()
    }


def fit_markup(
    retail: list[tuple[datetime, float]], wholesale: list[tuple[datetime, float]]
) -> Markup | None:
    """retail = slope x wholesale + intercept[half-hour], from settled values
    paired on the same 5-minute interval, or None under a day of pairs.

    One refit of the slope after dropping pairs whose residual exceeds six
    median absolute deviations (floor 2 c/kWh), so a feed glitch cannot tilt
    it; on clean data it changes nothing."""
    r, w = settled_by_interval(retail), settled_by_interval(wholesale)
    pairs = [(r[k][0], w[k][1], r[k][1]) for k in sorted(r.keys() & w.keys())]
    if len(pairs) < MIN_PAIRS:
        return None
    slope = _slope(pairs)
    intercept = _intercepts(pairs, slope)
    res = [
        y - slope * x - intercept[_half_hour(solver_shared._local(t))]
        for t, x, y in pairs
    ]
    med = statistics.median(res)
    mad = statistics.median(abs(e - med) for e in res)
    limit = max(0.02, 6 * mad)
    keep = [p for p, e in zip(pairs, res, strict=True) if abs(e - med) <= limit]
    if len(keep) >= MIN_PAIRS:
        slope = _slope(keep)
        intercept = _intercepts(pairs, slope)
    return Markup(slope=slope, intercept=intercept, n_pairs=len(pairs))


def overlay(
    values: list[float],
    sources: list[str],
    rows: list[dict],
    grid_times: list[datetime],
    not_before: datetime,
    markup: Markup | None,
) -> list[int]:
    """Replace `values[i]` in place for every period starting at or after
    `not_before` inside a P5MIN interval with this install's retail price
    for that interval's wholesale price. Returns the indices changed. Pure
    apart from the two lists it is given."""
    if markup is None:
        return []
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
                values[i] = float(markup.retail(wholesale, t))
                sources[i] = SOURCE_LABEL
                changed.append(i)
                break
    return changed


def _markup(
    retail_entity: str, wholesale_entity: str, history_for: Callable
) -> Markup | None:
    key = (retail_entity, wholesale_entity)
    hit = _markup_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _MARKUP_TTL_S:
        return hit[1]
    retail = history_for(retail_entity)
    wholesale = history_for(wholesale_entity)
    markup = fit_markup(retail, wholesale)
    _markup_cache[key] = (time.monotonic(), markup)
    if markup is None:
        _LOGGER.info(
            "Nimbus #1653: not enough history to learn %s from %s yet "
            "(%d and %d samples; needs %d paired 5-minute intervals)",
            retail_entity,
            wholesale_entity,
            len(retail),
            len(wholesale),
            MIN_PAIRS,
        )
    else:
        _LOGGER.info(
            "Nimbus #1653: %s = %.4f x %s + %.4f..%.4f $/kWh (%d intervals)",
            retail_entity,
            markup.slope,
            wholesale_entity,
            min(markup.intercept.values()),
            max(markup.intercept.values()),
            markup.n_pairs,
        )
    return markup


def apply(
    cfg: dict,
    grid_times: list[datetime],
    not_before: datetime,
    now: datetime,
    import_side: tuple[list[float], list[str]],
    export_side: tuple[list[float], list[str]],
    history_for: Callable,
) -> dict:
    """Overlay both sides in place. Returns what was done, for the log and
    the published plan; `{"applied": False, ...}` on every no-op path.

    `history_for(entity_id)` returns that entity's recorded numeric history
    as [(datetime, $/kWh)]. It is passed in by solver_writer rather than
    imported from it, so this module adds no seam back up the layer map."""
    try:
        entity = resolve_entity_id()
        if entity is None:
            return _inactive("no_p5min_entity")
        try:
            state = solver_shared.ha_get(entity)
        except Exception as err:  # noqa: BLE001 -- registered but no state yet
            _LOGGER.debug("Nimbus #1653: %s unreadable: %s", entity, err)
            return _inactive("no_p5min_state", entity)
        rows = fresh_rows(state, now)
        if not rows:
            return _inactive("no_fresh_run", entity)
        # Learned against the configured regional current price where there
        # is one; otherwise against the P5MIN sensor's own recorded state
        # (the interval in progress, i.e. AEMO's own dispatch price).
        wholesale = cfg.get("solver_regional_spot_current_price_sensor") or entity
        done = {}
        peak: dict[str, float | None] = {}
        for name, key, (values, sources) in (
            ("import", "solver_import_price_sensor", import_side),
            ("export", "solver_export_price_sensor", export_side),
        ):
            retail = cfg.get(key)
            markup = _markup(retail, wholesale, history_for) if retail else None
            changed = overlay(values, sources, rows, grid_times, not_before, markup)
            done[name] = len(changed)
            peak[name] = max((values[i] for i in changed), default=None)
        run = (state.get("attributes") or {}).get("run_datetime")
        if not any(done.values()):
            return _inactive("no_markup", entity)
        _report("applied")
        global _last_logged_run
        if any(done.values()) and run != _last_logged_run:
            _last_logged_run = run
            _LOGGER.info(
                "Nimbus #1653: P5MIN run %s priced %d import / %d export periods "
                "(highest import %s, export %s $/kWh)",
                run,
                done["import"],
                done["export"],
                _fmt(peak.get("import")),
                _fmt(peak.get("export")),
            )
        return {"applied": any(done.values()), "periods": done, "run": run}
    except Exception as err:  # noqa: BLE001 -- must never break a real solve
        _LOGGER.warning("Nimbus #1653: P5MIN price overlay failed: %s", err)
        return {"applied": False, "reason": "error"}


_WHY = {
    "no_p5min_entity": "no P5MIN sensor on this install (non-NEM, or no region found)",
    "no_p5min_state": "%s has no state yet",
    "no_fresh_run": "%s has no run from the last 15 minutes; spikes are not visible",
    "no_markup": "no wholesale-to-retail conversion learned yet for %s",
}


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _report(reason: str, entity: str | None = None) -> None:
    """Log the overlay's state once each time it CHANGES, so an install can
    tell from its own log why the next hour is or is not on P5MIN prices.
    A stale feed on a NEM install is a warning: spikes are invisible then."""
    global _last_reason
    if reason == _last_reason:
        return
    _last_reason = reason
    if reason == "applied":
        _LOGGER.info("Nimbus #1653: pricing the next hour on AEMO P5MIN")
        return
    why = _WHY[reason] % entity if "%s" in _WHY[reason] else _WHY[reason]
    level = logging.WARNING if reason == "no_fresh_run" else logging.INFO
    _LOGGER.log(level, "Nimbus #1653: P5MIN overlay inactive: %s", why)


def _inactive(reason: str, entity: str | None = None) -> dict:
    _report(reason, entity)
    return {"applied": False, "reason": reason}


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
