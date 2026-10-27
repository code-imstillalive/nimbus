"""Occupancy (home / away) for the load forecast (nimbus #1847).

The shape that measured best on #1847 (away, it more than halved the error
from the first day; on return it was level with no occupancy), built here:
- **Away**, with fewer than `MIN_AWAY_DAYS` away days in history: each time
  slot's `BASE_QUANTILE` over the last `BASE_DAYS` days, roughly the house's
  own base load at that time of day. It needs no away history.
- **Away**, with enough away days: the mean of the house's own away days for
  that slot.
- **Home**: the model, trained with the away periods left out
  (`mask_away`), so its home pattern isn't flattened by them.

Prior art: EMHASS's ML forecaster takes extra covariates (`weather_features`
for training, `weather_future` at prediction time) in one model with no
home/away split; HAEO has no load forecaster. Nimbus departs from a plain
covariate because the gain measured on #1847 came from separate home and
away profiles and a base-load fall-back, which a single covariate over one
trip's history cannot learn.

Pure functions, no Home Assistant imports (`ml/` stays HA-free). The caller
reads the entities and passes `occupancy_events` as (time, 1.0 home /
0.0 away), sorted by time.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import numpy as np

# Away days needed before the learned away profile replaces the base load.
# #1847: switching after 3 or after 5 away days measured the same to 0.01.
MIN_AWAY_DAYS = 3
# The base load: this quantile of each slot over the last BASE_DAYS days.
BASE_QUANTILE = 0.2
BASE_DAYS = 14
# A day counts as away when at least this share of it was away.
AWAY_DAY_SHARE = 0.75


@dataclass(frozen=True)
class AwayProfile:
    """kW per slot of the day, and which rule built it."""

    kind: Literal["base_load", "learned_away"]
    slot_minutes: int
    kw: tuple[float, ...]  # one value per slot, from local midnight
    away_days: int

    def at(self, t: datetime) -> float:
        slot = (t.hour * 60 + t.minute) // self.slot_minutes
        return self.kw[slot % len(self.kw)]


def away_at(occupancy_events: Sequence[tuple[datetime, float]], t: datetime) -> bool:
    """True when the last occupancy event at or before `t` says away. Before
    the first event, or after a NaN (unknown), the house counts as home: the
    forecast then stays exactly as it is without occupancy."""
    times = [e[0] for e in occupancy_events]
    i = bisect_right(times, t) - 1
    if i < 0:
        return False
    v = occupancy_events[i][1]
    return bool(np.isfinite(v)) and v < 0.5


def away_periods(
    occupancy_events: Sequence[tuple[datetime, float]], end: datetime
) -> list[tuple[datetime, datetime]]:
    """The [start, stop) spans the events say were away, up to `end`."""
    spans: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    for t, v in occupancy_events:
        if t >= end:
            break
        away = bool(np.isfinite(v)) and v < 0.5
        if away and start is None:
            start = t
        elif not away and start is not None:
            spans.append((start, t))
            start = None
    if start is not None:
        spans.append((start, end))
    return spans


def mask_away(
    load_events: Sequence[tuple[datetime, float]],
    spans: Sequence[tuple[datetime, datetime]],
) -> list[tuple[datetime, float]]:
    """`load_events` with every away span turned into a gap: a NaN marker at
    its start (#1556's gap convention, which training leaves out) and no
    readings inside it. The first reading after the span ends the gap."""
    if not spans:
        return list(load_events)
    out: list[tuple[datetime, float]] = []
    events = list(load_events)
    j = 0
    for start, stop in spans:
        while j < len(events) and events[j][0] < start:
            out.append(events[j])
            j += 1
        out.append((start, float("nan")))
        while j < len(events) and events[j][0] < stop:
            j += 1
    out.extend(events[j:])
    return out


def forward_away(
    times: Sequence[datetime],
    *,
    away_now: bool,
    departure: datetime | None = None,
    return_at: datetime | None = None,
) -> np.ndarray:
    """Away (True) per forecast time.

    - Away now: away until `return_at` if it is set and ahead, else for the
      whole horizon.
    - Home now: home, unless a `departure` ahead is set, then away from it
      until `return_at` (if set and after the departure) or the horizon's end.
    """
    t = list(times)
    out = np.zeros(len(t), dtype=bool)
    if not t:
        return out
    now = t[0]
    if away_now:
        until = return_at if return_at is not None and return_at > now else None
        for i, ti in enumerate(t):
            out[i] = until is None or ti < until
        return out
    if departure is not None and departure > now:
        until = return_at if return_at is not None and return_at > departure else None
        for i, ti in enumerate(t):
            out[i] = ti >= departure and (until is None or ti < until)
    return out


def _slot_values(
    times: Sequence[datetime], values: Sequence[float], slot_minutes: int
) -> list[list[float]]:
    n = (24 * 60) // slot_minutes
    by_slot: list[list[float]] = [[] for _ in range(n)]
    for t, v in zip(times, values, strict=True):
        if v is None or not np.isfinite(v):
            continue
        by_slot[(t.hour * 60 + t.minute) // slot_minutes].append(float(v))
    return by_slot


def build_profile(
    times: Sequence[datetime],
    values: Sequence[float | None],
    occupancy_events: Sequence[tuple[datetime, float]],
    *,
    now: datetime,
    slot_minutes: int,
) -> AwayProfile | None:
    """The away profile from a resampled load history (`times`, `values`,
    one per slot, local time). None when there is not a single usable day."""
    if not times:
        return None
    days: dict = {}
    for t, v in zip(times, values, strict=True):
        days.setdefault(t.date(), []).append((t, v))
    away_days = []
    for day, rows in days.items():
        shares = [away_at(occupancy_events, t) for t, _ in rows]
        if rows and sum(shares) >= AWAY_DAY_SHARE * (24 * 60 // slot_minutes):
            away_days.append(day)
    kind: Literal["base_load", "learned_away"]
    if len(away_days) >= MIN_AWAY_DAYS:
        rows = [(t, v) for d in away_days for t, v in days[d]]
        kind, quantile = "learned_away", None
    else:
        since = now - timedelta(days=BASE_DAYS)
        rows = [(t, v) for t, v in zip(times, values, strict=True) if t >= since]
        kind, quantile = "base_load", BASE_QUANTILE
    by_slot = _slot_values([r[0] for r in rows], [r[1] for r in rows], slot_minutes)
    if not any(by_slot):
        return None
    filled = [
        (float(np.mean(s)) if quantile is None else float(np.quantile(s, quantile)))
        if s
        else None
        for s in by_slot
    ]
    known = [v for v in filled if v is not None]
    fallback = float(np.median(known))
    return AwayProfile(
        kind=kind,
        slot_minutes=slot_minutes,
        kw=tuple(fallback if v is None else max(0.0, v) for v in filled),
        away_days=len(away_days),
    )


def apply(
    times: Sequence[datetime],
    forecast: Sequence[float],
    away: np.ndarray,
    profile: AwayProfile,
) -> np.ndarray:
    """`forecast` with every away time replaced by the profile's value."""
    out = np.asarray(forecast, dtype=float).copy()
    for i, t in enumerate(times):
        if away[i]:
            out[i] = profile.at(t)
    return out
