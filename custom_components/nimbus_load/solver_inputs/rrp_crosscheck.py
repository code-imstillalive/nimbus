"""Every price source brought back to the wholesale RRP and compared (#2190).

Mark Purcell's design (31 Oct 2026): no source is primary. Each source's
own spot figure is reduced to the bare AEMO RRP and compared, interval by
interval, current and future. They should match. Where they don't, the cause
is investigated case by case, so this module reports and never adjusts a
price. A source that stays put while two others move, on any interval, is
the stale one.

How each source's RRP is read, by the shape it publishes, never by name:

- LocalVolts v2 spot sensor (`source_field: spotCost`): RRP x the site's
  loss factor (MLF x DLF), with a further measured 1.10 on the buy side
  (`LV_BUY_SPOT_MULTIPLIER`, 1.1667 = 1.0605 x 1.10 over 30 Oct 2026). Its
  `forecast` rows are read for every interval.
- Amber Express (`detailedForecast` rows carrying `spot_per_kwh`): RRP x a
  measured 1.10 (`price_intervals.AMBER_SPOT_MULTIPLIER`), on the interval in
  progress only. Its forecast rows can repeat one spot value instead of
  forecasting it (#2190), so they are not read.
- NEM PD7DAY rows: `raw_value` (the wholesale forecast) or, on a tariff row,
  `spot_raw`, as published.
- AEMO NEM Data forecast rows (`start_time`, `end_time`, `price`): AEMO's
  own predispatch RRP, as published, for every row.
- Any other sensor: its state is the RRP for the interval in progress (the
  AEMO 5-minute price).

Pure: no Home Assistant import, no I/O. `solver_writer` owns the reads.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

try:
    from .. import price_intervals
except ImportError:  # standalone/cron deployment (see solver_writer.py)
    import price_intervals  # type: ignore[no-redef]

INTERVAL_SECONDS = 300
#: Ahead of now, the intervals compared.
HORIZON = timedelta(hours=24)
#: Two RRPs further apart than this, in $/kWh, disagree.
TOLERANCE = 0.005
#: A value that moved by more than this between two reads, moved.
MOVED = 1e-6
#: How many of the widest disagreements the report lists.
WORST_SHOWN = 10
#: LocalVolts' buy spot over its sell spot, measured 30 Oct 2026 (cause not
#: established, so not labelled).
LV_BUY_SPOT_MULTIPLIER = 1.10

KIND_LOCALVOLTS = "localvolts_spot"
KIND_AMBER = "amber_spot"
KIND_PD7DAY = "pd7day"
KIND_AEMO_FORECAST = "aemo_forecast"
KIND_CURRENT = "current_price"


@dataclass(frozen=True)
class SourceRead:
    """One source's RRP per 5-minute interval (key = start epoch // 300)."""

    label: str
    kind: str
    divisor: float | None
    points: dict[int, float] = field(default_factory=dict)
    reason: str | None = None


def _key(t: datetime) -> int:
    return int(t.timestamp()) // INTERVAL_SECONDS


def _spread(
    start: datetime, end: datetime, value: float, now: datetime
) -> dict[int, float]:
    """`value` on every 5-minute key from `start` up to `end`, inside the
    horizon."""
    first = max(_key(start), _key(now))
    last = min(_key(end - timedelta(seconds=1)), _key(now + HORIZON))
    return {k: value for k in range(first, last + 1)}


def _is_localvolts_spot(attrs: Mapping[str, Any]) -> bool:
    return attrs.get("source_field") == "spotCost" and attrs.get("direction") in (
        "Buy",
        "Sell",
    )


def _amber_detailed(attrs: Mapping[str, Any]) -> list | None:
    rows = attrs.get(price_intervals.AMBER_EXPRESS_DETAILED_KEY)
    if isinstance(rows, list) and any(
        isinstance(r, dict) and "spot_per_kwh" in r for r in rows
    ):
        return rows
    return None


def _pd7day_points(rows: list, now: datetime) -> dict[int, float]:
    points: dict[int, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = row["raw_value"] if "raw_value" in row else row.get("spot_raw")
        try:
            start = price_intervals.parse_iso(row["time"])
            end = price_intervals.parse_iso(row["nemtime"])
            value = float(raw)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(value) and end > start:
            points.update(_spread(start, end, value, now))
    return points


def read_source(
    label: str,
    state: Any,
    attrs: Mapping[str, Any] | None,
    site_factor: float | None,
    now: datetime,
) -> SourceRead:
    """`label`'s RRP per interval, from its state and attributes."""
    attrs = attrs or {}
    if _is_localvolts_spot(attrs):
        if site_factor is None:
            return SourceRead(
                label, KIND_LOCALVOLTS, None, reason="no_site_loss_factor"
            )
        divisor = site_factor * (
            LV_BUY_SPOT_MULTIPLIER if attrs.get("direction") == "Buy" else 1.0
        )
        points: dict[int, float] = {}
        for iv in price_intervals.generic_forecast_intervals(attrs.get("forecast")):
            points.update(_spread(iv.start, iv.end, iv.value / divisor, now))
        return SourceRead(label, KIND_LOCALVOLTS, divisor, points)
    detailed = _amber_detailed(attrs)
    if detailed is not None:
        found = price_intervals.amber_spot_in_progress(detailed, now)
        if found is None:
            return SourceRead(
                label,
                KIND_AMBER,
                price_intervals.AMBER_SPOT_MULTIPLIER,
                reason="no_interval_in_progress",
            )
        start, rrp = found
        return SourceRead(
            label, KIND_AMBER, price_intervals.AMBER_SPOT_MULTIPLIER, {_key(start): rrp}
        )
    rows = attrs.get("forecast")
    if price_intervals.is_aemo_nem_rows(rows):
        points = {}
        for iv in price_intervals.aemo_nem_intervals(rows):
            points.update(_spread(iv.start, iv.end, iv.value, now))
        return SourceRead(label, KIND_AEMO_FORECAST, 1.0, points)
    if price_intervals.is_pd7day_rows(rows):
        return SourceRead(label, KIND_PD7DAY, 1.0, _pd7day_points(rows, now))
    try:
        value = float(state)
    except (TypeError, ValueError):
        return SourceRead(label, KIND_CURRENT, 1.0, reason="no_value")
    if not math.isfinite(value):
        return SourceRead(label, KIND_CURRENT, 1.0, reason="no_value")
    return SourceRead(label, KIND_CURRENT, 1.0, {_key(now): value})


#: Where each source shape says when its figures were made, newest first.
AGE_KEYS = ("forecast_generated_at", "source_updated", "last_update")


def as_of(state: Mapping[str, Any] | None) -> str | None:
    """When a source's figures were made: its own forecast or update stamp,
    else the entity's `last_updated`. Shown beside a disagreement, so an
    old forecast is told apart from a wrong one (#2205)."""
    state = state or {}
    attrs = state.get("attributes") or {}
    for key in AGE_KEYS:
        if attrs.get(key):
            return str(attrs[key])
    value = state.get("last_updated")
    return None if value is None else str(value)


def _iso(key: int) -> str:
    return datetime.fromtimestamp(key * INTERVAL_SECONDS, UTC).isoformat()


def compare(reads: list[SourceRead]) -> dict[str, Any]:
    """Every interval two or more sources cover, and where they disagree by
    more than `TOLERANCE`, the widest `WORST_SHOWN` first."""
    by_key: dict[int, dict[str, float]] = {}
    for r in reads:
        for k, v in r.points.items():
            by_key.setdefault(k, {})[r.label] = v
    shared = {k: vals for k, vals in by_key.items() if len(vals) >= 2}
    disagreeing = [
        (max(vals.values()) - min(vals.values()), k, vals)
        for k, vals in shared.items()
        if max(vals.values()) - min(vals.values()) > TOLERANCE
    ]
    disagreeing.sort(key=lambda d: (-d[0], d[1]))
    return {
        "compared_intervals": len(shared),
        "disagreeing_intervals": len(disagreeing),
        "worst": [
            {
                "start": _iso(k),
                "spread": round(gap, 6),
                "rrp": {label: round(v, 6) for label, v in sorted(vals.items())},
            }
            for gap, k, vals in disagreeing[:WORST_SHOWN]
        ],
    }


class StaleWatch:
    """Between one read and the next, on any interval: a source that stayed
    put while two or more others moved is counted as stale there."""

    def __init__(self) -> None:
        self._last: dict[str, dict[int, float]] = {}

    def update(self, reads: list[SourceRead]) -> list[dict[str, Any]]:
        now_points = {r.label: dict(r.points) for r in reads}
        flagged: dict[str, list[int]] = {}
        keys = (
            set().union(*(p.keys() for p in now_points.values()))
            if now_points
            else set()
        )
        for k in keys:
            moved: dict[str, bool] = {}
            for label, pts in now_points.items():
                before = self._last.get(label, {}).get(k)
                if k in pts and before is not None:
                    moved[label] = abs(pts[k] - before) > MOVED
            if sum(moved.values()) >= 2:
                for label, did in moved.items():
                    if not did:
                        flagged.setdefault(label, []).append(k)
        self._last = now_points
        return [
            {"source": label, "intervals": len(ks), "first": _iso(min(ks))}
            for label, ks in sorted(flagged.items())
        ]


def report(
    reads: list[SourceRead],
    stale: list[dict[str, Any]],
    site_factor: float | None,
    ages: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """The published `rrp_crosscheck` attribute. `ages` is each source's
    `as_of()`, shown per source and on each listed disagreement."""
    ages = ages or {}
    compared = compare(reads)
    for row in compared["worst"]:
        row["as_of"] = {label: ages.get(label) for label in row["rrp"]}
    return {
        "tolerance": TOLERANCE,
        "site_loss_factor": None if site_factor is None else round(site_factor, 6),
        "sources": [
            {
                "source": r.label,
                "kind": r.kind,
                "divisor": None if r.divisor is None else round(r.divisor, 6),
                "intervals": len(r.points),
                "reason": r.reason,
                "as_of": ages.get(r.label),
            }
            for r in reads
        ],
        **compared,
        "stale": stale,
    }
