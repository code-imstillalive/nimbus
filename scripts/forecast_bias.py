#!/usr/bin/env python3
"""Forecast-minus-settled price bias, by lead time and hour of day (nimbus #1749).

Read-only measurement for #1749's revisit: does the price forecast the Solver
trades against run persistently above or below what the price settles at, and
at which leads and hours? It applies no correction anywhere and changes
nothing on any install.

Two inputs, one method:

    python scripts/forecast_bias.py export EXPORT.json [EXPORT2.json ...]
    python scripts/forecast_bias.py rest --base-url URL --token-file PATH \\
        --start ISO --end ISO --import-entity sensor.x [--export-entity sensor.y]

**export** reads what `nimbus_load.export_decision_inputs` returns (#1657 gate
2, #1676): JSON as the service response, the REST wrapper
(`{"service_response": ...}`), or YAML copied from Developer Tools (needs
PyYAML). Pass several files to cover more than the service's 24 h per call;
records repeated across files are counted once. A forecast is a plan row's
price at publish time (`<side>_price_raw` by default, the configured source's
own value before blending and fees; `--price-field landed` uses
`<side>_price`). The settled value comes from the same export: each record's
row 0 carries the price sensor's current value at that solve.

**rest** reads Home Assistant's recorder over its REST API, GET only, with a
token read from the file the caller names. The price sensor's state history
gives the settled values; the forecast comes from the `forecast` attribute of
the forecast entity (`--import-forecast-entity`, default the price entity
itself) where the recorder kept it. It often does not: HA drops an oversized
attribute from history, which is why #1676 exists. Supported forecast rows are
Nimbus's generic `{time, value}` step points (`calibrated` when `value` is
absent) and explicit `{start_time, end_time, price}` intervals in $/kWh.

## Definitions

* **Bias = forecast - settled, in c/kWh.** Negative means the forecast was
  below what settled (13 Oct's afternoon, per #1749).
* **Settled** is the last value the price held in an interval of
  `--interval-minutes` (5 or 30 for the NEM), intervals aligned to the clock.
  From an export: the current-interval price of the LAST solve published
  inside the interval; an interval with no solve in it has no settled value.
  From REST: the value the sensor held at the interval's end. HA records a
  state only when it changes, so an interval with no change inside settles at
  the value carried into it (counted as `held`).
* **Gaps are never filled.** An interval the sensor was unavailable at the end
  of, that the data does not reach, or (export) that no solve ran in, has no
  settled value; every forecast that needs it is counted `unpaired`, never
  interpolated.
* **A forecast for a span longer than one interval** (a 30-minute plan row
  against 5-minute settlement) is compared with the time-weighted mean of the
  settled intervals it covers, and only when every one of them is settled.
  Plan rows shorter than an interval (the 1-minute tier) are merged per
  interval and observation first, so they count once.
* **Lead** = the forecast span's start minus when it was observed (the plan's
  `published_at`; a history row's `last_updated`). Buckets: 0-30 min, 30-60,
  1-2 h, 2-4 h, 4-8 h, 8-24 h, 24 h+.
* **in-interval** is lead 0: the first value seen inside an interval (its
  opening value) against its settled value. From an export that needs two
  solves in the interval; from REST, one recorded state change.
* Every observation counts: a plan re-solved every 15 s contributes a
  forecast per solve, so near leads carry more weight than far ones.
* From an export, plan rows past the end of the configured price source's own
  forecast (its `forecast_last` in the record's source lines) are the
  Solver's extrapolation, not a forecast, and are excluded unless
  `--include-extrapolated`. Rows in row 0's 5-minute block carry the current
  price (solver_writer's settled override) and are never counted as
  forecasts.

Output is a table per side (import = buy, export = sell) of n, mean, median,
p90 and the share of pairs that settled above the forecast, by lead and by the
hour of day of the forecast span's start (`--tz`; the REST path defaults to
HA's own `time_zone`, the export path to this machine's). `--json` prints the
same as JSON, with a lead x hour cross-tab.

Stdlib only, deliberately: importing modules from `custom_components/` outside
HA shadows the stdlib (`select.py`), and this has to run on any machine.
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import importlib
import json
import math
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

SIDES = ("import", "export")
IN_INTERVAL = "in-interval"
#: (label, lower bound inclusive, upper bound exclusive), minutes of lead.
LEAD_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("0-30 min", 0.0, 30.0),
    ("30-60 min", 30.0, 60.0),
    ("1-2 h", 60.0, 120.0),
    ("2-4 h", 120.0, 240.0),
    ("4-8 h", 240.0, 480.0),
    ("8-24 h", 480.0, 1440.0),
    ("24 h+", 1440.0, math.inf),
)
BUCKET_ORDER = (IN_INTERVAL, *(label for label, _lo, _hi in LEAD_BUCKETS))
#: solver_writer.py forces every row in row 0's 5-minute block to the price
#: sensor's current state (#220), so those rows are never a forecast.
SOLVER_BLOCK_MINUTES = 5
_UNAVAILABLE = {"unknown", "unavailable", "none", ""}


class RefusedRequestError(RuntimeError):
    """A request other than a plain GET was about to be sent."""


# ---- time ------------------------------------------------------------------


def parse_time(value: Any) -> datetime | None:
    """An aware datetime from an ISO string, else None. A naive time is
    refused rather than guessed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def interval_index(when: datetime, minutes: int) -> int:
    """The clock-aligned interval `when` falls in (whole intervals since the
    epoch, so 5 and 30 minutes land on :00/:05 and :00/:30)."""
    return math.floor(when.timestamp() / (minutes * 60))


def interval_start(index: int, minutes: int) -> datetime:
    return datetime.fromtimestamp(index * minutes * 60, tz=UTC)


def lead_bucket(lead: timedelta) -> str | None:
    """The lead-time bucket of a forecast seen `lead` before its span starts.
    None for a negative lead (that is the in-interval case, measured from
    opening values instead)."""
    minutes = lead.total_seconds() / 60.0
    for label, lo, hi in LEAD_BUCKETS:
        if lo <= minutes < hi:
            return label
    return None


# ---- the shared method -----------------------------------------------------


@dataclass(frozen=True)
class Sample:
    """The price held from `at` on, in c/kWh; None while unavailable."""

    at: datetime
    value: float | None


@dataclass(frozen=True)
class Forecast:
    """A value, in c/kWh, observed at `observed_at` for [start, end)."""

    observed_at: datetime
    start: datetime
    end: datetime
    value: float


@dataclass
class Settlement:
    settled: dict[int, float] = field(default_factory=dict)
    opening: dict[int, float] = field(default_factory=dict)
    held: int = 0


def _covered(
    start: datetime, end: datetime, windows: Sequence[tuple[datetime, datetime]]
) -> bool:
    return any(ws <= start and end <= we for ws, we in windows)


def merge_windows(
    windows: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for ws, we in sorted(windows):
        if out and ws <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], we))
        else:
            out.append((ws, we))
    return out


def settle(
    samples: Sequence[Sample],
    minutes: int,
    windows: Sequence[tuple[datetime, datetime]],
    *,
    require_sample_inside: bool,
    min_inside_for_opening: int,
) -> Settlement:
    """Settled (last value held) and opening (first value seen) per interval.

    Only intervals wholly inside `windows` are settled: one cut off by the
    data's own edge cannot be known to have ended where the data does.
    `require_sample_inside` refuses to settle an interval nothing was
    observed in (an export, where no solve means no reading); without it the
    value carried into the interval settles it (REST, where HA records only
    changes)."""
    ordered = sorted(samples, key=lambda s: s.at)
    times = [s.at for s in ordered]
    result = Settlement()
    for ws, we in merge_windows(windows):
        first = interval_index(ws, minutes)
        if interval_start(first, minutes) < ws:
            first += 1
        idx = first
        while interval_start(idx + 1, minutes) <= we:
            start = interval_start(idx, minutes)
            end = interval_start(idx + 1, minutes)
            lo = bisect.bisect_left(times, start)
            hi = bisect.bisect_left(times, end)
            inside = ordered[lo:hi]
            last = ordered[hi - 1] if hi else None
            usable = inside or not require_sample_inside
            if last is not None and last.value is not None and usable:
                result.settled[idx] = last.value
                if not inside:
                    result.held += 1
                opening = inside[0].value if inside else None
                if len(inside) >= min_inside_for_opening and opening is not None:
                    result.opening[idx] = opening
            idx += 1
    return result


def merge_short(forecasts: Sequence[Forecast], minutes: int) -> list[Forecast]:
    """One observation's rows that fall inside the same interval, merged into
    one forecast (duration-weighted mean), so a 1-minute tier counts once."""
    out: list[Forecast] = []
    groups: dict[int, list[Forecast]] = defaultdict(list)
    for f in forecasts:
        if f.end <= f.start:
            continue
        idx = interval_index(f.start, minutes)
        if interval_index(f.end - timedelta(microseconds=1), minutes) == idx:
            groups[idx].append(f)
        else:
            out.append(f)
    for group in groups.values():
        group.sort(key=lambda f: f.start)
        weights = [(f.end - f.start).total_seconds() for f in group]
        value = sum(f.value * w for f, w in zip(group, weights, strict=True))
        out.append(
            Forecast(
                group[0].observed_at,
                group[0].start,
                max(f.end for f in group),
                value / sum(weights),
            )
        )
    out.sort(key=lambda f: f.start)
    return out


@dataclass(frozen=True)
class Pair:
    bucket: str
    hour: int
    bias: float  # forecast - settled, c/kWh


@dataclass
class Counts:
    observations: int = 0
    forecasts: int = 0
    paired: int = 0
    unpaired: int = 0
    not_ahead: int = 0
    beyond_source_forecast: int = 0
    intervals_settled: int = 0
    intervals_held: int = 0
    in_interval_pairs: int = 0


def pair(
    forecasts: Iterable[Forecast],
    settlement: Settlement,
    minutes: int,
    tz: tzinfo,
    counts: Counts,
) -> list[Pair]:
    """Every forecast with a settled value for its whole span, plus the
    in-interval (opening vs settled) pairs."""
    pairs: list[Pair] = []
    settled = settlement.settled
    for f in forecasts:
        counts.forecasts += 1
        first = interval_index(f.start, minutes)
        if interval_start(first, minutes) <= f.observed_at:
            # Its interval had already begun when it was seen: not a
            # forecast ahead. Lead 0 is measured from opening values.
            counts.not_ahead += 1
            continue
        last = interval_index(f.end - timedelta(microseconds=1), minutes)
        total = 0.0
        weighted = 0.0
        missing = False
        for idx in range(first, last + 1):
            if idx not in settled:
                missing = True
                break
            lo = max(f.start, interval_start(idx, minutes))
            hi = min(f.end, interval_start(idx + 1, minutes))
            seconds = (hi - lo).total_seconds()
            total += seconds
            weighted += settled[idx] * seconds
        if missing or total <= 0:
            counts.unpaired += 1
            continue
        bucket = lead_bucket(f.start - f.observed_at)
        if bucket is None:  # pragma: no cover - excluded above
            counts.not_ahead += 1
            continue
        counts.paired += 1
        pairs.append(
            Pair(bucket, f.start.astimezone(tz).hour, f.value - weighted / total)
        )
    for idx, opening in sorted(settlement.opening.items()):
        if idx in settled:
            hour = interval_start(idx, minutes).astimezone(tz).hour
            pairs.append(Pair(IN_INTERVAL, hour, opening - settled[idx]))
            counts.in_interval_pairs += 1
    counts.intervals_settled = len(settled)
    counts.intervals_held = settlement.held
    return pairs


# ---- statistics ------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        raise ValueError("percentile of nothing")
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def summarise(biases: Sequence[float]) -> dict[str, float | int | None]:
    if not biases:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "p90": None,
            "settled_above_pct": None,
        }
    n = len(biases)
    return {
        "n": n,
        "mean": sum(biases) / n,
        "median": percentile(biases, 0.5),
        "p90": percentile(biases, 0.9),
        # bias < 0: the forecast was below what settled.
        "settled_above_pct": 100.0 * sum(1 for b in biases if b < 0) / n,
    }


def tabulate(pairs: Sequence[Pair]) -> dict[str, Any]:
    by_lead: dict[str, list[float]] = defaultdict(list)
    by_hour: dict[int, list[float]] = defaultdict(list)
    cross: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for p in pairs:
        by_lead[p.bucket].append(p.bias)
        cross[p.bucket][p.hour].append(p.bias)
        if p.bucket != IN_INTERVAL:
            by_hour[p.hour].append(p.bias)
    return {
        "by_lead": {b: summarise(by_lead.get(b, [])) for b in BUCKET_ORDER},
        "by_hour": {str(h): summarise(by_hour.get(h, [])) for h in range(24)},
        "by_lead_and_hour": {
            b: {str(h): summarise(v) for h, v in sorted(cross[b].items())}
            for b in BUCKET_ORDER
            if b in cross
        },
    }


# ---- path 1: an export_decision_inputs response -----------------------------


def _unwrap(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [d for item in data for d in _unwrap(item)]
    if isinstance(data, dict):
        if "service_response" in data:
            return _unwrap(data["service_response"])
        if isinstance(data.get("records"), list):
            return [data]
    raise ValueError("not an export_decision_inputs response (no `records` list)")


def load_exports(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Every export in the given files: JSON (optionally .gz) or YAML."""
    exports: list[dict[str, Any]] = []
    for path in paths:
        raw = (
            gzip.decompress(path.read_bytes())
            if path.suffix == ".gz"
            else path.read_bytes()
        )
        text = raw.decode("utf-8")
        name = path.name.removesuffix(".gz")
        if name.endswith((".yaml", ".yml")):
            try:
                yaml = importlib.import_module("yaml")  # optional dependency
            except ImportError as err:
                raise SystemExit(
                    f"{path}: reading YAML needs PyYAML; save the response as JSON"
                ) from err
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        exports.extend(_unwrap(data))
    return exports


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return out if math.isfinite(out) else None


def _source_cutoff(
    record: dict[str, Any], config: dict[str, Any] | None, side: str
) -> datetime | None:
    """The latest time the configured price source's own forecast reached at
    capture, from the record's source lines."""
    if not config:
        return None
    entities = {
        config.get(f"solver_{side}_price_sensor"),
        config.get("solver_price_forecast_array_sensor"),
    } - {None}
    ends = [
        parse_time(line.get("forecast_last"))
        for line in record.get("sources") or []
        if isinstance(line, dict) and line.get("entity_id") in entities
    ]
    known = [e for e in ends if e is not None]
    return max(known) if known else None


def export_observations(
    exports: Sequence[dict[str, Any]],
    side: str,
    *,
    minutes: int,
    price_field: str,
    include_extrapolated: bool,
    counts: Counts,
) -> tuple[list[Sample], list[Forecast], list[tuple[datetime, datetime]]]:
    """Samples (row 0, the current price at each solve), forecasts (later
    rows) and the windows the exports cover, for one side."""
    key = f"{side}_price_raw" if price_field == "raw" else f"{side}_price"
    configs: dict[str, Any] = {}
    windows: list[tuple[datetime, datetime]] = []
    best: dict[str, dict[str, Any]] = {}
    for export in exports:
        configs.update(export.get("configs") or {})
        ws, we = parse_time(export.get("start")), parse_time(export.get("end"))
        if ws is not None and we is not None:
            windows.append((ws, we))
        for record in export.get("records") or []:
            if not isinstance(record, dict):
                continue
            ident = str(record.get("published_at") or record.get("captured_at"))
            # The same solve can arrive twice (overlapping exports, or a
            # horizon also seen trimmed to a head): keep its longest copy.
            n_rows = len((record.get("rows") or {}).get("time") or [])
            held = best.get(ident)
            if held is None or n_rows > len((held.get("rows") or {}).get("time") or []):
                best[ident] = record

    samples: list[Sample] = []
    forecasts: list[Forecast] = []
    for record in best.values():
        at = parse_time(record.get("published_at")) or parse_time(
            record.get("captured_at")
        )
        cols = record.get("rows") or {}
        times = cols.get("time") or []
        hours = cols.get("hours") or []
        values = cols.get(key) or []
        if at is None or not times:
            continue
        counts.observations += 1
        v0 = _num(values[0]) if values else None
        samples.append(Sample(at, None if v0 is None else v0 * 100.0))
        row0 = parse_time(times[0])
        if row0 is None:
            continue
        block = SOLVER_BLOCK_MINUTES
        block_end = interval_start(interval_index(row0, block) + 1, block)
        cutoff = (
            None
            if include_extrapolated
            else _source_cutoff(record, configs.get(str(record.get("config"))), side)
        )
        rows: list[Forecast] = []
        for i, raw_time in enumerate(times):
            start = parse_time(raw_time)
            h = _num(hours[i]) if i < len(hours) else None
            v = _num(values[i]) if i < len(values) else None
            if start is None or h is None or h <= 0 or v is None or start < block_end:
                continue
            if cutoff is not None and start > cutoff:
                counts.beyond_source_forecast += 1
                continue
            # The published `hours` is rounded to 4 dp (5 min = 0.0833).
            end = start + timedelta(seconds=round(h * 3600))
            rows.append(Forecast(at, start, end, v * 100.0))
        forecasts.extend(merge_short(rows, minutes))
    if not windows and samples:
        times_seen = sorted(s.at for s in samples)
        windows.append((times_seen[0], times_seen[-1]))
    return samples, forecasts, windows


# ---- path 2: Home Assistant REST, read-only ----------------------------------


class GetOnlyClient:
    """GET requests to one Home Assistant, and nothing else.

    Every request goes through `send()`, which refuses anything that is not a
    plain GET without a body before it reaches the network."""

    def __init__(
        self,
        base_url: str,
        token: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = 120.0,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                f"base URL must be http(s)://host[:port], got {base_url!r}"
            )
        self._base = base_url.rstrip("/")
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def send(self, request: urllib.request.Request) -> bytes:
        if request.get_method() != "GET" or request.data is not None:
            raise RefusedRequestError(
                f"refused {request.get_method()} {request.full_url}: this tool only reads (GET)"
            )
        with self._opener(request, timeout=self._timeout) as response:
            body: bytes = response.read()
        return body

    def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = self._base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
        )
        return json.loads(self.send(request).decode("utf-8"))

    def history(
        self, entities: Sequence[str], start: datetime, end: datetime
    ) -> dict[str, list[dict[str, Any]]]:
        """Full-attribute state history, one day per request, attribute-only
        changes included (that is when a forecast attribute refreshes)."""
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        chunk = start
        while chunk < end:
            chunk_end = min(chunk + timedelta(days=1), end)
            path = "/api/history/period/" + urllib.parse.quote(
                chunk.isoformat(), safe=""
            )
            data = self.get_json(
                path,
                {
                    "filter_entity_id": ",".join(entities),
                    "end_time": chunk_end.isoformat(),
                    "significant_changes_only": "0",
                },
            )
            for series in data or []:
                if series and isinstance(series[0], dict):
                    out[str(series[0].get("entity_id"))].extend(series)
            chunk = chunk_end
        return out


def price_scale_to_cents(unit: Any) -> float:
    """c/kWh per unit of a price sensor's value, from its unit_of_measurement.
    Mirrors `price_intervals.price_unit_scale` (pinned by a test), x100."""
    text = str(unit or "").strip().lower().replace(" ", "")
    for per, factor in (("/kwh", 1.0), ("/mwh", 0.001)):
        if text.endswith(per):
            prefix = text[: -len(per)]
            return 100.0 * factor * (0.01 if prefix in ("c", "¢") else 1.0)
    return 100.0


def state_samples(rows: Sequence[dict[str, Any]]) -> list[Sample]:
    """The price's value over time, from state CHANGES only (`last_changed`),
    so an attribute-only update does not look like a new reading."""
    samples: dict[datetime, Sample] = {}
    for row in rows:
        at = parse_time(row.get("last_changed")) or parse_time(row.get("last_updated"))
        if at is None:
            continue
        scale = price_scale_to_cents(
            (row.get("attributes") or {}).get("unit_of_measurement")
        )
        state = str(row.get("state", "")).strip()
        value: float | None
        if state.lower() in _UNAVAILABLE:
            value = None
        else:
            try:
                value = float(state) * scale
            except ValueError:
                value = None
            if value is not None and not math.isfinite(value):
                value = None
        samples[at] = Sample(at, value)
    return [samples[k] for k in sorted(samples)]


def forecast_rows(
    attributes: dict[str, Any], observed_at: datetime, max_step: timedelta
) -> list[Forecast] | None:
    """One history row's forecast attribute as forecasts, or None if it has
    none in a shape this tool reads."""
    rows = attributes.get("forecast")
    if not isinstance(rows, list) or not rows:
        return None
    dicts = [r for r in rows if isinstance(r, dict)]
    explicit = [
        r for r in dicts if "start_time" in r and "end_time" in r and "price" in r
    ]
    out: list[Forecast] = []
    if explicit:
        # {start_time, end_time, price}: AEMO NEM Data's shape, $/kWh as published.
        for r in explicit:
            s, e, v = (
                parse_time(r["start_time"]),
                parse_time(r["end_time"]),
                _num(r["price"]),
            )
            if s is not None and e is not None and v is not None and e > s:
                out.append(Forecast(observed_at, s, e, v * 100.0))
        return out
    scale = price_scale_to_cents(attributes.get("unit_of_measurement"))
    points: list[tuple[datetime, float]] = []
    for r in dicts:
        t = parse_time(r.get("time"))
        v = _num(r["value"] if "value" in r else r.get("calibrated"))
        if t is not None and v is not None:
            points.append((t, v * scale))
    if not points:
        return None
    points.sort(key=lambda p: p[0])
    # {time, value} step points hold until the next point. The last point has
    # no end, and a step longer than `max_step` is a hole held flat: neither
    # is a forecast for a known span, so neither is used.
    for (t, v), (t_next, _v) in pairwise(points):
        if timedelta(0) < t_next - t <= max_step:
            out.append(Forecast(observed_at, t, t_next, v))
    return out


def rest_observations(
    history: dict[str, list[dict[str, Any]]],
    price_entity: str,
    forecast_entity: str,
    *,
    minutes: int,
    max_step: timedelta,
    counts: Counts,
) -> tuple[list[Sample], list[Forecast]]:
    samples = state_samples(history.get(price_entity, []))
    forecasts: list[Forecast] = []
    seen: set[datetime] = set()
    for row in history.get(forecast_entity, []):
        at = parse_time(row.get("last_updated"))
        if at is None or at in seen:
            continue
        seen.add(at)
        found = forecast_rows(row.get("attributes") or {}, at, max_step)
        if found is None:
            continue
        counts.observations += 1
        forecasts.extend(merge_short(found, minutes))
    return samples, forecasts


# ---- output ------------------------------------------------------------------


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:+.{digits}f}"


def _rows(title: str, label_header: str, table: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        title,
        f"  {label_header:<12} {'n':>7} {'mean':>8} {'median':>8} {'p90':>8} {'settled>fc':>11}",
    ]
    for label, s in table.items():
        pct = s["settled_above_pct"]
        lines.append(
            f"  {label:<12} {s['n']:>7} {_fmt(s['mean']):>8} {_fmt(s['median']):>8} "
            f"{_fmt(s['p90']):>8} {('-' if pct is None else f'{pct:.0f}%'):>11}"
        )
    return lines


def render_text(report: dict[str, Any]) -> str:
    lines = [
        f"Forecast bias (forecast - settled, c/kWh) from {report['source']}",
        (
            f"interval {report['interval_minutes']} min; hours in "
            f"{report['timezone']}; settled = {report['settled_definition']}"
        ),
        "Negative = the forecast was below what settled. No gap is filled.",
    ]
    for side, body in report["sides"].items():
        c = body["counts"]
        lines += [
            "",
            f"== {side} ({'buy' if side == 'import' else 'sell'}) ==",
            (
                f"observations {c['observations']}, forecasts {c['forecasts']}, "
                f"paired {c['paired']}, unpaired (no settled value) "
                f"{c['unpaired']}, already begun {c['not_ahead']}, beyond source "
                f"forecast {c['beyond_source_forecast']}; intervals settled "
                f"{c['intervals_settled']} ({c['intervals_held']} held), "
                f"in-interval pairs {c['in_interval_pairs']}"
            ),
        ]
        lines += _rows("By lead", "lead", body["by_lead"])
        hours = {f"{int(h):02d}:00": s for h, s in body["by_hour"].items() if s["n"]}
        lines += _rows("By hour of day (forecast span start; leads > 0)", "hour", hours)
    return "\n".join(lines)


def build_report(
    source: str,
    minutes: int,
    tz_name: str,
    settled_definition: str,
    per_side: dict[str, tuple[list[Pair], Counts]],
) -> dict[str, Any]:
    return {
        "source": source,
        "interval_minutes": minutes,
        "timezone": tz_name,
        "bias": "forecast - settled, c/kWh",
        "settled_definition": settled_definition,
        "sides": {
            side: {"counts": vars(counts), **tabulate(pairs)}
            for side, (pairs, counts) in per_side.items()
        },
    }


# ---- CLI -----------------------------------------------------------------------


def _minutes(text: str) -> int:
    value = int(text)
    if value < 1 or 1440 % value:
        raise argparse.ArgumentTypeError(
            "interval minutes must divide a day (5, 30, ...)"
        )
    return value


def _aware(text: str) -> datetime:
    parsed = parse_time(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"{text!r}: give an ISO time with an offset")
    return parsed


def _tz(name: str | None) -> tuple[tzinfo, str]:
    if name:
        return ZoneInfo(name), name
    local = datetime.now().astimezone().tzinfo or UTC
    return local, f"{local} (this machine)"


def run_export(args: argparse.Namespace) -> dict[str, Any]:
    exports = load_exports([Path(p) for p in args.files])
    tz, tz_name = _tz(args.tz)
    per_side: dict[str, tuple[list[Pair], Counts]] = {}
    for side in SIDES:
        counts = Counts()
        samples, forecasts, windows = export_observations(
            exports,
            side,
            minutes=args.interval_minutes,
            price_field=args.price_field,
            include_extrapolated=args.include_extrapolated,
            counts=counts,
        )
        settlement = settle(
            samples,
            args.interval_minutes,
            windows,
            require_sample_inside=True,
            min_inside_for_opening=2,
        )
        per_side[side] = (
            pair(forecasts, settlement, args.interval_minutes, tz, counts),
            counts,
        )
    return build_report(
        f"export_decision_inputs ({len(exports)} response(s), {args.price_field} prices)",
        args.interval_minutes,
        tz_name,
        "the current-interval price of the last solve published inside the interval",
        per_side,
    )


def run_rest(
    args: argparse.Namespace, opener: Callable[..., Any] = urllib.request.urlopen
) -> dict[str, Any]:
    if args.end <= args.start:
        raise SystemExit("--end must be after --start")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit(f"{args.token_file}: empty token file")
    client = GetOnlyClient(args.base_url, token, opener=opener)
    if args.tz:
        tz, tz_name = _tz(args.tz)
    else:
        ha_tz = str(client.get_json("/api/config").get("time_zone") or "UTC")
        tz, tz_name = ZoneInfo(ha_tz), f"{ha_tz} (Home Assistant's)"
    sides = {
        side: (entity, getattr(args, f"{side}_forecast_entity") or entity)
        for side in SIDES
        if (entity := getattr(args, f"{side}_entity"))
    }
    if not sides:
        raise SystemExit("give --import-entity and/or --export-entity")
    entities = sorted({e for pairing in sides.values() for e in pairing})
    history = client.history(entities, args.start, args.end)
    max_step = timedelta(minutes=args.max_step_minutes)
    per_side: dict[str, tuple[list[Pair], Counts]] = {}
    for side, (entity, forecast_entity) in sides.items():
        counts = Counts()
        samples, forecasts = rest_observations(
            history,
            entity,
            forecast_entity,
            minutes=args.interval_minutes,
            max_step=max_step,
            counts=counts,
        )
        settlement = settle(
            samples,
            args.interval_minutes,
            [(args.start, args.end)],
            require_sample_inside=False,
            min_inside_for_opening=1,
        )
        per_side[side] = (
            pair(forecasts, settlement, args.interval_minutes, tz, counts),
            counts,
        )
    return build_report(
        f"Home Assistant history at {args.base_url}",
        args.interval_minutes,
        tz_name,
        "the value the sensor held at the end of the interval",
        per_side,
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Forecast-minus-settled price bias by lead time and hour "
        "of day (nimbus #1749). Read-only; see the module docstring."
    )
    sub = p.add_subparsers(dest="mode", required=True)
    for name in ("export", "rest"):
        s = sub.add_parser(name)
        s.add_argument("--interval-minutes", type=_minutes, default=5)
        s.add_argument("--tz", help="IANA zone for the hour-of-day table")
        s.add_argument(
            "--json", action="store_true", help="print JSON instead of tables"
        )
        if name == "export":
            s.add_argument("files", nargs="+", help="export_decision_inputs responses")
            s.add_argument("--price-field", choices=("raw", "landed"), default="raw")
            s.add_argument("--include-extrapolated", action="store_true")
        else:
            s.add_argument("--base-url", required=True)
            s.add_argument(
                "--token-file", required=True, help="a file holding a long-lived token"
            )
            s.add_argument("--start", type=_aware, required=True)
            s.add_argument("--end", type=_aware, required=True)
            for side in SIDES:
                s.add_argument(f"--{side}-entity")
                s.add_argument(f"--{side}-forecast-entity")
            s.add_argument("--max-step-minutes", type=float, default=30.0)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = run_export(args) if args.mode == "export" else run_rest(args)
    print(json.dumps(report, indent=2) if args.json else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
