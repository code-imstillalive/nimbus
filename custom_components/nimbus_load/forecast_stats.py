"""What a load's power forecast adds up to (household, 22 Oct 2026).

A load's forecast sensor published its curve and nothing about it: to know
how much energy the pool or the lounge circuit is expected to use tomorrow, a
household had to integrate the `forecast` attribute themselves. This reads
the same points the Solver reads -- each value held until the next point, as
`resample_forecast` does -- and sums them:

* `energy_next_24h_kwh`: from now to 24 hours ahead;
* `energy_forecast_kwh`: over the whole published forecast from now;
* `peak_next_24h_kw` and `peak_next_24h_at`: the highest point in the next
  24 hours, and when.

The last point is held for as long as the spacing before it. A point with no
value is skipped, never read as zero. Pure: no Home Assistant import.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

ATTR_ENERGY_NEXT_24H_KWH = "energy_next_24h_kwh"
ATTR_ENERGY_FORECAST_KWH = "energy_forecast_kwh"
ATTR_PEAK_NEXT_24H_KW = "peak_next_24h_kw"
ATTR_PEAK_NEXT_24H_AT = "peak_next_24h_at"

_DAY = timedelta(hours=24)


def _points(forecast: Iterable[Mapping[str, Any]]) -> list[tuple[datetime, float]]:
    out = []
    for point in forecast or ():
        try:
            raw = point.get("time")
            when = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
            value = point.get("value")
            if value is None:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            out.append((when, float(value)))
        except (AttributeError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda p: p[0])


def _overlap_hours(start: datetime, end: datetime, lo: datetime, hi: datetime) -> float:
    s, e = max(start, lo), min(end, hi)
    return max(0.0, (e - s).total_seconds() / 3600.0)


def _spans(
    pts: list[tuple[datetime, float]],
) -> list[tuple[datetime, datetime, float]]:
    """(start, end, value): each point held until the next, the last for as
    long as the spacing before it."""
    spans = []
    for i, (when, value) in enumerate(pts):
        if i + 1 < len(pts):
            end = pts[i + 1][0]
        elif i > 0:
            end = when + (when - pts[i - 1][0])
        else:
            end = when
        spans.append((when, end, value))
    return spans


def energy_summary(
    forecast: Iterable[Mapping[str, Any]], now: datetime
) -> dict[str, float | str | None]:
    """The four attributes above for a kW forecast; empty with no points."""
    spans = _spans(_points(forecast))
    if not spans:
        return {}
    horizon_end = max(end for _, end, _ in spans)
    day_end = now + _DAY
    next_day = sum(v * _overlap_hours(s, e, now, day_end) for s, e, v in spans)
    whole = sum(v * _overlap_hours(s, e, now, horizon_end) for s, e, v in spans)
    in_day = [(v, s) for s, e, v in spans if e > now and s < day_end]
    peak = max(in_day, key=lambda p: p[0]) if in_day else None
    return {
        ATTR_ENERGY_NEXT_24H_KWH: round(next_day, 3),
        ATTR_ENERGY_FORECAST_KWH: round(whole, 3),
        ATTR_PEAK_NEXT_24H_KW: round(peak[0], 3) if peak else None,
        ATTR_PEAK_NEXT_24H_AT: max(peak[1], now).isoformat() if peak else None,
    }


#: A controllable load counts as running above this, kW.
RUNNING_KW = 0.01


def plan_summary(
    power_forecast: Iterable[Mapping[str, Any]],
    cost_forecast: Iterable[Mapping[str, Any]],
    now: datetime,
) -> dict[str, dict[str, float | None]]:
    """nimbus #2128: a controllable load's plan summed over the next 24 hours
    and the whole plan from now -- energy (kWh), cost (each period's planned
    cost, the part of it inside the window), runtime (hours above
    RUNNING_KW) and the average price (cost / energy, None with no energy).
    Empty without a power plan."""
    spans = _spans(_points(power_forecast))
    if not spans:
        return {}
    cost_at = dict(_points(cost_forecast))
    horizon_end = max(end for _, end, _ in spans)
    out: dict[str, dict[str, float | None]] = {}
    for key, until in (("next_24h", now + _DAY), ("plan", horizon_end)):
        energy = cost = runtime = 0.0
        priced = False
        for start, end, kw in spans:
            hours = _overlap_hours(start, end, now, until)
            if hours <= 0:
                continue
            energy += kw * hours
            if kw > RUNNING_KW:
                runtime += hours
            if start in cost_at and end > start:
                priced = True
                cost += cost_at[start] * hours / ((end - start).total_seconds() / 3600)
        out[key] = {
            "energy_kwh": round(energy, 3),
            "cost": round(cost, 4) if priced else None,
            "runtime_hours": round(runtime, 2),
            "average_price": (
                round(cost / energy, 4) if priced and energy > 1e-9 else None
            ),
        }
    return out
