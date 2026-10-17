"""What the Solver decided on, kept for replay (nimbus #1657, gate 2).

Recorder keeps the state of `sensor.nimbus_solver_battery_forecast`, not its
`forecast` rows, so once a decision has passed there is no way to see what the
LP was looking at when it made it. Twice in the week of 2 Oct 2026 (#1652,
#1658) the question "did Nimbus see the spike coming?" could not be answered
for exactly that reason. AEMO's own forecasts are recoverable from NEMWEB
afterwards; the provider-transformed, Nimbus-resolved inputs are not.

This module is the pure half: which rows to keep, in what shape, and when.
`decision_inputs_store.py` does the I/O. Nothing here touches Home Assistant.

## What is kept

* **A head on every successful solve**: the plan's first `HEAD_HOURS` of
  rows, the fields in `ROW_FIELDS`, plus the solve's own metadata. That is the
  part of the plan that becomes a command.
* **The whole horizon** every `HORIZON_EVERY`, and on any solve whose period-0
  dispatch class (charge / self-consume / discharge, the dispatch
  automation's own +/-0.05 kW deadband) differs from the previous solve's. So
  a decision like 9 Oct 04:25 always has its full horizon, not only the
  scheduled one either side of it.
* **The resolved config**, once per segment per distinct fingerprint, and
  **the price and forecast sources' own state** (entity, value, last update,
  first forecast row) at capture time.

Rows are stored column-wise: one list per field. The same few keys repeated
on every row are most of the bytes of the published attribute.

## Each price source's own forecast series (nimbus #1786, step 1)

On **full-horizon records** only, every price source the config names (import
and export primaries and secondaries, the forecast array, the regional spot
feeds, the P2P matched rate) plus Nimbus's own `sensor.nimbus_p5min_forecast`
also carries its forecast series as the provider published it, so a
consolidation rule other than the one the Solver used can be scored later
against the same inputs. `source_series()` reads each shape with
`price_intervals.py`'s own readers (AEMO NEM Data, NEM PD7DAY, Amber Electric
`forecasts`, Amber Express `forecast` on `detailedForecast` boundaries),
P5MIN's `{start, end, value}` rows, and the generic `{time, value}` /
`calibrated` rows. The value is stored as published, never unit-converted;
the body carries the sensor's `unit_of_measurement` and `to_dollars`, the
factor the Solver itself applies to reach $/kWh (1 for a provider shape,
whose adapter already reads $/kWh). OpenADR's forecast exists only behind an
action and is not fetched here.

A body is stored once per segment file under its fingerprint, the way configs
are, and the source line names it (`series`), so an unchanged source costs
one 16-character reference per record.

## What is NOT kept

Provider-native fields a reader does not use (Amber's `spot_per_kwh`,
PD7DAY's `raw_value`, LocalVolts' per-row quality). LocalVolts keeps its own
rows for about three days and AEMO's are on NEMWEB, as #1658 records.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

from . import price_intervals as pi
from .solver_shared import parse_iso

HEAD_HOURS = 2.0
HORIZON_EVERY = timedelta(minutes=30)
HEAD_KEEP = timedelta(hours=48)
HORIZON_KEEP = timedelta(days=7)
#: The dispatch automation's own deadband: > +0.05 discharge, < -0.05 charge.
DEADBAND_KW = 0.05

#: Rounding slack when summing the plan's rounded period lengths (36 s).
_HOURS_TOLERANCE = 0.01

KIND_HEAD = "head"
KIND_HORIZON = "horizon"
KEEP = {KIND_HEAD: HEAD_KEEP, KIND_HORIZON: HORIZON_KEEP}

#: Per-period fields kept from the published plan's `forecast` rows: what the
#: LP was given (prices and their provenance, load, solar, envelope) and what
#: it decided (battery, grid, SoC, the energy shadow price). The flow
#: decomposition and savings split are derivable from these and are left out.
ROW_FIELDS = (
    "time",
    "hours",
    "import_price",
    "import_price_raw",
    "import_price_source",
    "export_price",
    "export_price_raw",
    "export_price_source",
    "bonus_price",
    "load_kw",
    "solar_kw",
    "battery_kw",
    "soc_pct",
    "grid_import_kw",
    "grid_export_kw",
    "export_bonus_kw",
    "envelope_import_limit_kw",
    "envelope_export_limit_kw",
    "shadow_price",
    "dispatch_direction",
    # nimbus #1797: the calibrated solar band (#1807) the stochastic solve's
    # scenarios are built from, so a stochastic cycle can be replayed.
    "solar_p10_kw",
    "solar_p90_kw",
    # nimbus #1797: what the LP counted on after the risk-aversion dials, so
    # the replay (scripts/stochastic_replay.py) rebuilds the plan that ran.
    "solar_effective_kw",
    "import_price_effective",
    "export_price_effective",
)

#: Top-level attributes of the published plan kept with each record.
META_FIELDS = (
    "generated_at",
    "nimbus_version",
    "status",
    "solve_seconds",
    "n_periods",
    "horizon_hours",
    "total_cost",
    "binding_constraint_now",
    "binding_constraint_shadow_price",
    "energy_shadow_price_now",
    "load_whole_house_live_now_kw",
    "load_forecast_source_used",
    "price_spike_active",
    # nimbus #1797: shadow or drive, what it decided and why it fell back.
    "stochastic",
)

#: Config keys whose entity is a decision input worth a provenance line.
_SOURCE_KEY_HINTS = ("price_sensor", "forecast_sensor", "array_sensor")

#: Config attributes that are presentation, not configuration.
_CONFIG_IGNORED = ("friendly_name", "unresolved_required_keys")


def dispatch_class(battery_kw: float | None) -> str | None:
    """The class the dispatch automation would act on, or None if unknown."""
    if battery_kw is None:
        return None
    if battery_kw > DEADBAND_KW:
        return "discharge"
    if battery_kw < -DEADBAND_KW:
        return "charge"
    return "self_consume"


def horizon_reason(
    last_horizon_at: datetime | None,
    now: datetime,
    previous_class: str | None,
    current_class: str | None,
) -> str | None:
    """Why this solve's full horizon should be kept, or None if it need not.

    A class change is reported even within the interval, because the
    decisions worth replaying are the ones where the plan changed its mind.
    """
    if last_horizon_at is None:
        return "first"
    if (
        previous_class is not None
        and current_class is not None
        and previous_class != current_class
    ):
        return "dispatch_class_change"
    if now - last_horizon_at >= HORIZON_EVERY:
        return "interval"
    return None


def columns(rows: list[dict[str, Any]], head_hours: float | None) -> dict[str, list]:
    """`rows` as one list per `ROW_FIELDS` field.

    With `head_hours`, only the rows that start within that many hours of the
    first row, by the rows' own `hours` (the plan is tiered: 5-minute rows
    first, 30-minute later, so a row count would mean different spans on
    different plans). A row without a usable `hours` ends the head there,
    rather than guessing its length.
    """
    kept: list[dict[str, Any]] = []
    elapsed = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if head_hours is not None:
            # The published `hours` is rounded to 4 dp (5 min = 0.0833), so 24
            # of them sum to 1.9992; without the tolerance a 25th row slips in.
            if elapsed >= head_hours - _HOURS_TOLERANCE:
                break
            try:
                elapsed += float(row.get("hours"))
            except (TypeError, ValueError):
                kept.append(row)
                break
        kept.append(row)
    return {field: [row.get(field) for row in kept] for field in ROW_FIELDS}


def as_head(record: dict[str, Any]) -> dict[str, Any]:
    """A record trimmed to its first `HEAD_HOURS`, as a head would have been
    filed. A head is returned unchanged; a horizon keeps its `kind` and
    `reason` so a reader can tell it was trimmed."""
    if record.get("kind") != KIND_HORIZON:
        return record
    cols = record.get("rows") or {}
    n = len(cols.get("time") or [])
    rows = [{f: (cols.get(f) or [None] * n)[i] for f in ROW_FIELDS} for i in range(n)]
    return {**record, "rows": columns(rows, HEAD_HOURS), "trimmed_to_head": True}


def config_fingerprint(config: dict[str, Any]) -> str:
    """A short, stable fingerprint of the resolved config's content."""
    body = {k: v for k, v in config.items() if k not in _CONFIG_IGNORED}
    raw = json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def config_body(config: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in config.items() if k not in _CONFIG_IGNORED}


def source_entities(config: dict[str, Any]) -> list[str]:
    """Every entity the config names as a price or forecast input, in order."""
    out: list[str] = []
    for key in sorted(config):
        value = config[key]
        if (
            isinstance(value, str)
            and value.startswith("sensor.")
            and any(hint in key for hint in _SOURCE_KEY_HINTS)
            and value not in out
        ):
            out.append(value)
    return out


def source_line(
    entity_id: str,
    state: str | None,
    last_updated: str | None,
    attributes: dict[str, Any] | None,
) -> dict[str, Any]:
    """One source's provenance at capture time: its value, when it last
    changed, and the first and last time its forecast covers."""
    line: dict[str, Any] = {
        "entity_id": entity_id,
        "state": state,
        "last_updated": last_updated,
    }
    fc = (attributes or {}).get("forecast")
    if isinstance(fc, list) and fc:
        line["forecast_rows"] = len(fc)
        line["forecast_first"] = _row_time(fc[0])
        line["forecast_last"] = _row_time(fc[-1])
    return line


#: Of the source keys, the ones naming a PRICE input (#1786): solar, load and
#: weather forecasts keep only their provenance line.
_PRICE_KEY_HINTS = ("price", "spot", "matched_rate")
#: Nimbus's own AEMO 5-minute pre-dispatch (#1653): a price source the config
#: does not name, kept when the install publishes it.
P5MIN_ENTITY = "sensor.nimbus_p5min_forecast"

SHAPE_GENERIC = "time_value"
SHAPE_START_END = "start_end_value"


def price_source_entities(config: dict[str, Any]) -> list[str]:
    """The price inputs among `source_entities()`, in the same order, then
    `P5MIN_ENTITY`: the sources whose forecast series a horizon keeps."""
    out = [
        config[key]
        for key in sorted(config)
        if isinstance(config[key], str)
        and config[key].startswith("sensor.")
        and any(hint in key for hint in _SOURCE_KEY_HINTS)
        and any(hint in key for hint in _PRICE_KEY_HINTS)
    ]
    out.append(P5MIN_ENTITY)
    return list(dict.fromkeys(out))


def _start_end_value_intervals(rows: Any) -> list[pi.PriceInterval]:
    """`{start, end, value}` rows (P5MIN's own shape) -> intervals."""
    parsed = []
    for row in rows if isinstance(rows, list) else []:
        try:
            start, end = parse_iso(row["start"]), parse_iso(row["end"])
            value = float(row["value"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(value) and end > start:
            parsed.append(pi.PriceInterval(start, end, value))
    parsed.sort(key=lambda i: i.start)
    return parsed


def _generic_points(rows: Any) -> list[tuple[datetime, float]]:
    """The Solver's generic `{time, value}` rows (`calibrated` when `value` is
    absent), read as `_resample_generic_price_forecast_raw()` reads them."""
    points = []
    for row in rows if isinstance(rows, list) else []:
        try:
            raw = row["value"] if "value" in row else row["calibrated"]
            when, value = parse_iso(row["time"]), float(raw)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(value):
            points.append((when, value))
    points.sort(key=lambda p: p[0])
    return points


def _provider_series(attrs: dict[str, Any]) -> tuple[str, list[pi.PriceInterval]]:
    """(shape, intervals) for a provider shape, in `price_intervals.
    _provider_intervals()`'s own order; ("", []) when it is none of them."""
    fc: Any = attrs.get("forecast")
    if pi.is_aemo_nem_rows(fc):
        return pi.AEMO_NEM_DOMAIN, pi.aemo_nem_intervals(fc)
    if pi.is_pd7day_rows(fc):
        return pi.PD7DAY_DOMAIN, pi.pd7day_intervals(fc)
    if pi.is_amber_core_rows(fc):
        return pi.AMBER_CORE_DOMAIN, pi.amber_core_intervals(fc)
    rows: Any = attrs.get(pi.AMBER_CORE_ROWS_KEY)
    if not fc and pi.is_amber_core_rows(rows):
        return pi.AMBER_CORE_DOMAIN, pi.amber_core_intervals(rows)
    if pi.AMBER_EXPRESS_DETAILED_KEY in attrs:
        detailed = attrs[pi.AMBER_EXPRESS_DETAILED_KEY]
        found = pi.amber_express_intervals(fc, detailed)
        if found is not None:
            return pi.AMBER_EXPRESS_DOMAIN, found
    if isinstance(fc, list) and any(
        isinstance(r, dict) and {"start", "end", "value"} <= r.keys() for r in fc
    ):
        return SHAPE_START_END, _start_end_value_intervals(fc)
    return "", []


def source_series(attributes: dict[str, Any] | None) -> dict[str, Any] | None:
    """A price source's forecast as published, or None when it has none.

    `{shape, unit, to_dollars, t0, step_min, value, end}`: each row's start
    as minutes after the previous row's (the first row's after `t0`, so 0;
    a repeated 5 compresses to almost nothing, cumulative offsets do not),
    its value exactly as the reader takes it from the provider (never
    unit-converted), and `end` the final interval's end where the shape
    states one. `to_dollars` is the factor the Solver applies to reach
    $/kWh. `series_points()` decodes it."""
    attrs = attributes or {}
    shape, intervals = _provider_series(attrs)
    points = [(i.start, i.value) for i in intervals]
    end = intervals[-1].end if intervals else None
    to_dollars = 1.0
    if not shape:
        shape, points = SHAPE_GENERIC, _generic_points(attrs.get("forecast"))
        to_dollars = pi.price_unit_scale(attrs.get("unit_of_measurement"))
    if not points:
        return None
    t0 = points[0][0]
    starts = [t for t, _v in points]
    steps = [(b - a).total_seconds() / 60.0 for a, b in pairwise([t0, *starts])]
    return {
        "shape": shape,
        "unit": attrs.get("unit_of_measurement"),
        "to_dollars": to_dollars,
        "t0": t0.isoformat(),
        "step_min": [int(m) if m == int(m) else round(m, 4) for m in steps],
        "value": [v for _t, v in points],
        "end": end.isoformat() if end is not None else None,
    }


def series_fingerprint(body: dict[str, Any]) -> str:
    """A short, stable fingerprint of a series body's content."""
    raw = json.dumps(body, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def series_points(body: dict[str, Any]) -> list[tuple[datetime, float]]:
    """A stored series body back to (start, value) points."""
    at = datetime.fromisoformat(str(body["t0"]))
    points = []
    for step, value in zip(
        body.get("step_min") or [], body.get("value") or [], strict=True
    ):
        at += timedelta(minutes=float(step))
        points.append((at, float(value)))
    return points


def _row_time(row: Any) -> str | None:
    if not isinstance(row, dict):
        return None
    for key in ("time", "start_time", "start", "intervalEnd", "nem_time"):
        if row.get(key) is not None:
            return str(row[key])
    return None


def build_record(
    attributes: dict[str, Any],
    *,
    captured_at: datetime,
    kind: str,
    published_at: str | None = None,
    config_hash: str | None,
    sources: list[dict[str, Any]],
    reason: str | None = None,
) -> dict[str, Any]:
    """One stored record from the published plan's attributes."""
    rows = attributes.get("forecast")
    rows = rows if isinstance(rows, list) else []
    record: dict[str, Any] = {
        "kind": kind,
        "captured_at": captured_at.isoformat(),
        # When the plan entity was written: the solve's own time. Not
        # `generated_at`, which is the plan's first period boundary.
        "published_at": published_at,
        "config": config_hash,
        "sources": sources,
        "rows": columns(rows, HEAD_HOURS if kind == KIND_HEAD else None),
    }
    if reason is not None:
        record["reason"] = reason
    for field in META_FIELDS:
        if field in attributes:
            record[field] = attributes[field]
    return record


def segment_key(when: datetime) -> str:
    """The hourly segment a record belongs to, in UTC so a DST change can
    never map two hours to one key."""
    if when.tzinfo is None:
        raise ValueError("segment_key needs an aware datetime")
    return when.astimezone(UTC).strftime("%Y%m%d%H")


def segment_start(key: str) -> datetime:
    return datetime.strptime(key, "%Y%m%d%H").replace(tzinfo=UTC)


def expired(keys: list[str], now: datetime, keep: timedelta) -> list[str]:
    """The segment keys wholly older than `keep`. A segment is dropped only
    once its END has aged out, so a window is never cut short by an hour."""
    out = []
    for key in keys:
        try:
            end = segment_start(key) + timedelta(hours=1)
        except ValueError:
            continue
        if now - end > keep:
            out.append(key)
    return out


def segments_overlapping(keys: list[str], start: datetime, end: datetime) -> list[str]:
    out = []
    for key in sorted(keys):
        try:
            s = segment_start(key)
        except ValueError:
            continue
        if s < end and s + timedelta(hours=1) > start:
            out.append(key)
    return out


def in_window(record: dict[str, Any], start: datetime, end: datetime) -> bool:
    try:
        at = datetime.fromisoformat(str(record.get("captured_at")))
    except ValueError:
        return False
    return start <= at < end
