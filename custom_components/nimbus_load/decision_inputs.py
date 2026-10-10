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

## What is NOT kept

Provider-native rows before Nimbus resolves them. LocalVolts keeps its own for
about three days and AEMO's are on NEMWEB, as #1658 records.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

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
