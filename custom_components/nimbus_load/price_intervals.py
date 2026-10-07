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
from collections.abc import Iterable
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


def intervals_from_rows(rows: Any) -> list[PriceInterval] | None:
    """Intervals for a provider shape this module knows, else None (the
    caller keeps its own `{time, value}` handling)."""
    if is_aemo_nem_rows(rows):
        return aemo_nem_intervals(rows)
    if is_pd7day_rows(rows):
        return pd7day_intervals(rows)
    return None


def _provider_intervals(attrs: Any) -> list[PriceInterval] | None:
    if not isinstance(attrs, dict):
        return None
    return intervals_from_rows(attrs.get("forecast"))


def is_provider_shape(attrs: Any) -> bool:
    """True when a price sensor's attributes are a provider shape this module
    reads (so the solver's generic `{time, value}` reader hands it here)."""
    return _provider_intervals(attrs) is not None


def provider_forecast_on_grid(
    attrs: dict[str, Any], grid_times: list[datetime]
) -> tuple[list[float], list[bool]] | None:
    """(value, real) per grid time for a provider shape, or None when no row
    is usable. The import/export price reader's path for these providers
    (nimbus #1578, Mark Purcell: AEMO spot is often used as a feed-in price,
    and with network tariff as a buy price -- the configured network/flat
    fees are added to the import price downstream; nimbus #1581: PD7DAY's
    published value, on its own `nemtime` interval ends)."""
    intervals = _provider_intervals(attrs)
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
