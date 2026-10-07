"""Provider price rows -> explicit price intervals (nimbus #1550).

Every pricing provider publishes its forecast in its own shape. This module
turns one provider's native rows into `PriceInterval`s with explicit start
and end, so coverage is measured from the intervals themselves rather than
from a field name or a first-to-last span (#1550's live capture found both
misleading: an OpenADR `forecast_end` that was the last row's START, and a
seven-day span hiding a 24-hour hole).

One function per provider, added as each adapter lands. Pure: no Home
Assistant import, so the solver path and the tests call the same code.

AEMO NEM Data (nimbus #1578)
----------------------------
`cabberley/HA_AemoNemData`, domain `aemo_nem`. The regional
`current_30min_forecast` sensor's `forecast` attribute holds rows
`{start_time, end_time, price}`: 30-minute intervals, `price` already in
$/kWh. It is AEMO's WHOLESALE regional price. Nimbus reads it in two places:
the regional spot forecast (which extends a retail feed past its own
horizon), and any import or export price field the household points at it --
it is often used directly as a feed-in price, and, with the configured
network/flat fees Nimbus adds to the import price, as a buy price (Mark
Purcell, #1578). Nimbus only ever PROPOSES it for the regional spot forecast:
whether a household's own tariff passes spot through is theirs to say.

* `price` is read as published. It is never divided by 1,000 again.
* Negative and zero prices are real and kept. A missing, non-numeric or
  non-finite price drops that ROW, never the whole forecast.
* A row with no usable start/end, or an end not after its start, is dropped.
* A duplicate start keeps the first row; a row starting inside the previous
  interval is dropped, so intervals never overlap.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

try:
    from .solver_shared import parse_iso
except ImportError:  # standalone/cron deployment (see solver_writer.py)
    from solver_shared import parse_iso  # type: ignore[no-redef]


@dataclass(frozen=True)
class PriceInterval:
    """One priced interval, [start, end), value in $/kWh."""

    start: datetime
    end: datetime
    value: float


AEMO_NEM_DOMAIN = "aemo_nem"
AEMO_NEM_FORECAST_KEY = "current_30min_forecast"
_AEMO_ROW_KEYS = ("start_time", "end_time", "price")


def is_aemo_nem_rows(rows: Any) -> bool:
    """True when `rows` is AEMO NEM Data's forecast shape: a non-empty list
    whose dict rows carry `start_time`, `end_time` and `price`."""
    if not isinstance(rows, list) or not rows:
        return False
    return any(
        isinstance(r, dict) and all(k in r for k in _AEMO_ROW_KEYS) for r in rows
    )


def aemo_nem_intervals(rows: Iterable[Any]) -> list[PriceInterval]:
    """AEMO NEM Data `forecast` rows -> ordered, non-overlapping intervals."""
    parsed: list[PriceInterval] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            start = parse_iso(row["start_time"])
            end = parse_iso(row["end_time"])
            value = float(row["price"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not math.isfinite(value) or end <= start:
            continue
        parsed.append(PriceInterval(start, end, value))
    return _ordered(parsed)


# --- NEM PD7DAY (nimbus #1581) ---------------------------------------------
#
# purcell-lab/nem_pd7day, domain `nem_pd7day`. Every forecast row carries
# `time` (interval START) and `nemtime` (interval END, AEMO's convention),
# 30 minutes apart. Three different PRICE BASES share that shape:
#
# * wholesale, `sensor.<..>_nem_spot_price_forecast` (unique_id
#   `nem_pd7day_<region>_forecast`): `raw_value` (AEMO's own PD7DAY price),
#   `calibrated` (spike-calibrated) and `value` (the selected, calibrated
#   price);
# * an import TARIFF (unique_id `<entry>_<region>_<distributor>_<code>_tariff`):
#   `value` = calibrated spot PLUS that tariff's network component, with
#   `spot`, `spot_raw`, `period` and `network_rate` alongside;
# * an export tariff (`..._export_tariff`): `value` = the feed-in price.
#
# Nimbus reads the published `value` (falling back to `calibrated` only when a
# row has no `value` key at all, as the generic reader always has). It never
# re-applies calibration or network charges, and a row whose `value` is null
# (the integration's own "could not calibrate this interval") is missing --
# never filled from `raw_value` or `spot`.
#
# The day 2-7 "continuation" sensors (`..._days27`) start after Amber
# Express's own horizon. Nimbus never stitches one onto another source by
# itself: #1550's capture found the naive join leaves a 30-minute hole.

PD7DAY_DOMAIN = "nem_pd7day"
BASIS_WHOLESALE = "wholesale"
BASIS_TARIFF = "tariff"


def is_pd7day_rows(rows: Any) -> bool:
    """True for NEM PD7DAY's forecast shape: rows carrying `time` and
    `nemtime`."""
    if not isinstance(rows, list) or not rows:
        return False
    return any(isinstance(r, dict) and "time" in r and "nemtime" in r for r in rows)


def pd7day_basis(rows: Iterable[Any]) -> str:
    """`tariff` when the rows carry a network component (`network_rate` or a
    separate `spot`), else `wholesale`."""
    for r in rows or []:
        if isinstance(r, dict) and ("network_rate" in r or "spot" in r):
            return BASIS_TARIFF
    return BASIS_WHOLESALE


def pd7day_intervals(rows: Iterable[Any]) -> list[PriceInterval]:
    """NEM PD7DAY `forecast` rows -> ordered, non-overlapping intervals of
    the published price."""
    parsed: list[PriceInterval] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        raw = row["value"] if "value" in row else row.get("calibrated")
        try:
            start = parse_iso(row["time"])
            end = parse_iso(row["nemtime"])
            value = float(raw)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not math.isfinite(value) or end <= start:
            continue
        parsed.append(PriceInterval(start, end, value))
    return _ordered(parsed)


def _ordered(parsed: list[PriceInterval]) -> list[PriceInterval]:
    parsed.sort(key=lambda i: i.start)
    out: list[PriceInterval] = []
    for interval in parsed:
        if out and interval.start < out[-1].end:
            continue  # duplicate start or overlap: the first row stands
        out.append(interval)
    return out


# --- Amber Express (nimbus #1580) -------------------------------------------
#
# hass-energy/amber-express, domain `amber_express`. Each price sensor
# (unique_id `<site>_general_price` / `<site>_feed_in_price`) publishes:
#
# * `forecast: [{time, value}]` -- `value` is ALREADY the household's selected
#   pricing basis (`advanced_price_predicted`, falling back to `per_kwh` when
#   absent; or `per_kwh`), ALREADY sign-flipped for feed-in (positive =
#   earnings) and ALREADY carries the demand-window charge on the general
#   channel. `time` is the interval start, floored to the minute.
# * `detailedForecast` (case-sensitive) -- Amber's own rows with
#   `start_time` one second past the boundary (`03:20:01Z`), `end_time` on it
#   (`03:25:00Z`) and a nominal `duration` (5 or 30).
#
# Nimbus reads the price from `forecast[].value` exactly as published (never
# negating feed-in again, never re-adding the demand window, never choosing
# a different basis) and takes only the interval BOUNDARIES from
# `detailedForecast`: canonical start = end - duration, accepted only when
# Amber's own start sits inside the first minute of it. A simple row with no
# detailed counterpart ends where the next row starts (Amber's own
# `interpolation_mode: previous`) when that is at most 30 minutes away;
# otherwise, like a final row with none, it is not counted as coverage.
# `select.<..>_pricing_mode` is read-only to Nimbus: it is never operated.

AMBER_EXPRESS_DOMAIN = "amber_express"
AMBER_EXPRESS_DETAILED_KEY = "detailedForecast"
_AMBER_MAX_INTERVAL = timedelta(minutes=30)


def amber_express_intervals(forecast: Any, detailed: Any) -> list[PriceInterval] | None:
    """Amber Express `forecast` prices on `detailedForecast` boundaries, or
    None when either attribute is not a list (the caller keeps its own
    `{time, value}` handling)."""
    if not isinstance(forecast, list) or not isinstance(detailed, list):
        return None
    ends: dict[datetime, datetime] = {}
    for d in detailed:
        if not isinstance(d, dict):
            continue
        try:
            end = parse_iso(d["end_time"])
            minutes = int(d["duration"])
            amber_start = parse_iso(d["start_time"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        start = end - timedelta(minutes=minutes)
        if minutes > 0 and start <= amber_start < start + timedelta(minutes=1):
            ends[start] = end
    points: list[tuple[datetime, float]] = []
    for row in forecast:
        if not isinstance(row, dict):
            continue
        try:
            when = parse_iso(row["time"])
            value = float(row["value"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(value):
            points.append((when, value))
    points.sort(key=lambda p: p[0])
    parsed: list[PriceInterval] = []
    for k, (start, value) in enumerate(points):
        end = ends.get(start)
        if end is None and k + 1 < len(points):
            # Only across a gap no longer than Amber's longest interval: a
            # row hours before the next is not hours of coverage.
            nxt = points[k + 1][0]
            end = nxt if nxt - start <= _AMBER_MAX_INTERVAL else None
        if end is not None and end > start:
            parsed.append(PriceInterval(start, end, value))
    return _ordered(parsed)


# --- Amber Electric, Home Assistant core (nimbus #1579) ---------------------
#
# homeassistant/components/amberelectric. The forecast sensors (unique_id
# `<site>-forecasts-<channel>`, channel `general`, `feed_in` or
# `controlled_load`) publish the PLURAL attribute `forecasts`: rows of
# `start_time` (one second past the boundary, as Amber's API gives it),
# `end_time`, `duration`, `per_kwh`, `spot_per_kwh`, ... in $/kWh.
#
# `per_kwh` is the price, already sign-normalised for feed-in by HA
# (`AmberForecastSensor`: `per_kwh * -1` on the feed-in channel), so positive
# feed-in is earnings. `spot_per_kwh` is the wholesale component and is
# never substituted for it. The advanced price prediction exists only in the
# `amberelectric.get_forecasts` action response, not on the sensor, so it is
# not read here.

AMBER_CORE_DOMAIN = "amberelectric"
AMBER_CORE_ROWS_KEY = "forecasts"


def is_amber_core_rows(rows: Any) -> bool:
    """True for HA core Amber's `forecasts` shape: rows carrying
    `start_time`, `end_time` and `per_kwh`."""
    if not isinstance(rows, list) or not rows:
        return False
    return any(
        isinstance(r, dict) and "per_kwh" in r and "end_time" in r and "start_time" in r
        for r in rows
    )


def amber_core_intervals(rows: Iterable[Any]) -> list[PriceInterval]:
    """HA core Amber `forecasts` rows -> intervals of `per_kwh`, on canonical
    boundaries (start = end - duration when Amber's own start is inside its
    first minute, else Amber's start as given)."""
    parsed: list[PriceInterval] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            end = parse_iso(row["end_time"])
            amber_start = parse_iso(row["start_time"])
            value = float(row["per_kwh"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not math.isfinite(value):
            continue
        start = amber_start
        try:
            nominal = end - timedelta(minutes=int(row["duration"]))
        except (KeyError, TypeError, ValueError):
            nominal = None
        if nominal is not None and nominal <= amber_start < nominal + timedelta(
            minutes=1
        ):
            start = nominal
        if end > start:
            parsed.append(PriceInterval(start, end, value))
    return _ordered(parsed)


# --- OpenADR 3 VEN (nimbus #1583) -------------------------------------------
#
# grid-coordination/openadr3-ven-hass, domain `openadr3_ven`. One sensor per
# (program, payload type), unique_id `<entry>_<program id>_<payload type>`.
# The sensor's attributes carry only a summary (`payload_type`,
# `forecast_rows`, `forecast_start`, `forecast_end`, ...): the full forecast
# exists only in the entity action `openadr3_ven.get_forecast`, which returns
# `{entity_id: {payload_type, unit, forecast: [{datetime, value,
# interval_minutes}]}}` from the integration's own cached data.
#
# * Only `PRICE` and `EXPORT_PRICE` are prices; `GHG` and any other payload
#   type are never read as one.
# * Each row is [datetime, datetime + interval_minutes). A row with no usable
#   length is 60 minutes, the integration's own default (`sensor.py`). #1550's
#   capture had 5-, 15- and 30-minute rows in one response, and an hourly
#   program with no rows for a whole day inside a seven-day span.
# * `forecast_end` is the LAST ROW'S START, not the end of coverage; it is not
#   used. Coverage is measured from the rows.
# * OpenADR states neither currency nor whether a value is a full tariff or an
#   incentive added to one; the `$` in the unit establishes neither. Nimbus
#   therefore never proposes an OpenADR price for a field: it reads one
#   correctly when the household chooses it, and says what it found.

OPENADR_DOMAIN = "openadr3_ven"
OPENADR_PRICE_PAYLOADS = frozenset({"PRICE", "EXPORT_PRICE"})
_OPENADR_DEFAULT_MINUTES = 60


def is_openadr_price_attrs(attrs: Any) -> bool:
    """True for an OpenADR 3 VEN price sensor's summary attributes."""
    return (
        isinstance(attrs, dict)
        and "forecast_rows" in attrs
        and attrs.get("payload_type") in OPENADR_PRICE_PAYLOADS
    )


def openadr_rows_from_response(response: Any, entity_id: str) -> list[Any] | None:
    """The forecast rows for `entity_id` from an `openadr3_ven.get_forecast`
    response, or None when the response is missing, for another entity, or
    not a price payload."""
    if not isinstance(response, dict):
        return None
    block = response.get(entity_id)
    if not isinstance(block, dict):
        return None
    if block.get("payload_type") not in OPENADR_PRICE_PAYLOADS:
        return None
    rows = block.get("forecast")
    return rows if isinstance(rows, list) else None


def openadr_intervals(rows: Iterable[Any]) -> list[PriceInterval]:
    """OpenADR forecast rows -> ordered, non-overlapping intervals."""
    parsed: list[PriceInterval] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            start = parse_iso(row["datetime"])
            value = float(row["value"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        try:
            minutes = int(row.get("interval_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            minutes = _OPENADR_DEFAULT_MINUTES
        if math.isfinite(value):
            parsed.append(
                PriceInterval(start, start + timedelta(minutes=minutes), value)
            )
    return _ordered(parsed)


def intervals_from_rows(rows: Any) -> list[PriceInterval] | None:
    """Intervals for a provider shape this module knows, else None (the
    caller keeps its own `{time, value}` handling)."""
    if is_aemo_nem_rows(rows):
        return aemo_nem_intervals(rows)
    if is_pd7day_rows(rows):
        return pd7day_intervals(rows)
    if is_amber_core_rows(rows):
        return amber_core_intervals(rows)
    return None


def _provider_intervals(attrs: Any) -> list[PriceInterval] | None:
    if not isinstance(attrs, dict):
        return None
    found = intervals_from_rows(attrs.get("forecast"))
    if found is None and not attrs.get("forecast"):
        # nimbus #1579: HA core Amber Electric's forecast sensors publish the
        # PLURAL `forecasts`. Before, they read as no forecast at all and the
        # current price was held flat across the whole horizon.
        rows = attrs.get(AMBER_CORE_ROWS_KEY)
        if is_amber_core_rows(rows):
            found = amber_core_intervals(rows)
    if found is None and AMBER_EXPRESS_DETAILED_KEY in attrs:
        # nimbus #1580: Amber Express. `forecast[].value` as published (basis,
        # feed-in sign and demand window already applied upstream), on the
        # interval boundaries of its `detailedForecast`.
        found = amber_express_intervals(
            attrs.get("forecast"), attrs[AMBER_EXPRESS_DETAILED_KEY]
        )
    return found


def is_provider_shape(attrs: Any) -> bool:
    """True when a price sensor's attributes are a provider shape this module
    reads (so the solver's generic `{time, value}` reader hands it here)."""
    return is_openadr_price_attrs(attrs) or _provider_intervals(attrs) is not None


def provider_forecast_on_grid(
    attrs: dict[str, Any],
    grid_times: list[datetime],
    fetch_rows: Callable[[], Any] | None = None,
) -> tuple[list[float], list[bool]] | None:
    """(value, real) per grid time for a provider shape, or None when no row
    is usable. The import/export price reader's path for these providers
    (nimbus #1578, Mark Purcell: AEMO spot is often used as a feed-in price,
    and with network tariff as a buy price -- the configured network/flat
    fees are added to the import price downstream; nimbus #1581: PD7DAY's
    published value, on its own `nemtime` interval ends). `fetch_rows`
    supplies an action-only provider's rows (nimbus #1583, OpenADR); it is
    called only for such a sensor."""
    if is_openadr_price_attrs(attrs):
        rows = fetch_rows() if fetch_rows is not None else None
        intervals = openadr_intervals(rows) if rows else []
    else:
        intervals = _provider_intervals(attrs) or []
    if not intervals:
        return None
    return on_grid(intervals, grid_times)


def on_grid(
    intervals: list[PriceInterval], grid_times: list[datetime]
) -> tuple[list[float], list[bool]]:
    """(value, real) per grid time. The value holds the latest interval that
    started at or before it (the first interval's before any), as every
    generic price source does; `real` is True only inside an interval, so a
    hole or anything past the final END is not coverage."""
    values: list[float] = []
    covered: list[bool] = []
    for gt in grid_times:
        held = [i for i in intervals if i.start <= gt]
        values.append(held[-1].value if held else intervals[0].value)
        covered.append(any(i.start <= gt < i.end for i in intervals))
    return values, covered


@dataclass(frozen=True)
class Coverage:
    """What a set of intervals actually covers."""

    first_start: datetime | None
    final_end: datetime | None
    covered_hours: float
    gaps: tuple[tuple[datetime, datetime], ...]


def coverage(intervals: list[PriceInterval]) -> Coverage:
    """Coverage measured from the intervals: the final END (not the last
    start), the hours actually priced, and every hole between intervals."""
    if not intervals:
        return Coverage(None, None, 0.0, ())
    gaps = tuple((a.end, b.start) for a, b in pairwise(intervals) if b.start > a.end)
    covered = sum((i.end - i.start for i in intervals), timedelta())
    return Coverage(
        intervals[0].start,
        intervals[-1].end,
        covered.total_seconds() / 3600.0,
        gaps,
    )


def as_step_points(intervals: list[PriceInterval]) -> list[tuple[datetime, float]]:
    """(start, value) points for the solver's existing hold-the-latest-point
    lookups, which key on interval start."""
    return [(i.start, i.value) for i in intervals]
