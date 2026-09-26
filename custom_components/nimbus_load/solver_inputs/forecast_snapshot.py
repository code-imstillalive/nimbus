"""Capture what the forecast SAID, so tomorrow can score it.

nimbus issue #937. That issue measured naive persistence beating the ML load
forecaster on 11 of 14 scored days (mean -$0.71/day) and named its own
top-priority action:

    More days. The single highest-value thing is simply letting it
    accumulate, now that #919 puts it on every install.

There are none, and there could not be: the day-ahead `forecast_regret`
decomposition is assembled **only** in the standalone cron quality writer, and
has never existed in the integration. So the 14-day window stopped when the
reference household moved to the native path, and no HACS install has ever
produced one.

## Why this cannot be backfilled, which is why it is worth building now

`research/forecast_capture.py` established the constraint by measurement, not
assumption:

    NONE of the day-ahead forecasts this project has -- solar (Solcast) or
    price (nem_pd7day) -- have ANY recorder history at all (0 history points
    over a 2-day check)... Nimbus's own load forecast DOES have real
    historical STATE points, but its `forecast` array attribute (where the
    actual prediction lives) is not preserved -- only the bare summary state.

    Conclusion: there is no way to reconstruct what any of these forecasts
    said for a PAST day. This isn't a data-quality gap, it's a
    data-existence gap -- the only honest fix is to start capturing forward
    from today.

**Every day this does not exist is a day of evidence permanently lost.** That
is the whole argument for building the capture before the analysis that
consumes it, and it is the opposite of the usual "wire it before you ship it"
rule — there is nothing to wire until something has been captured.

## What this module is, and is not

Pure functions over plain data: build a serialisable snapshot from the arrays a
solve already has, put one back onto an arbitrary grid, and decide what to
keep. **Zero Home Assistant imports**, so its tests load it by path and prove
that rather than asserting it — the same shape as `calendar_trips.py` and
`sign_convention.py`.

It does **not** decide when to capture, where to persist, or how to score. Those
belong to the integration, which has `hass`, a `Store`, and the scorer.

## The one real design decision here

A snapshot is keyed by **local** day, not UTC. The thing being scored is "what
did the forecast say about Tuesday", and Tuesday is a local-calendar fact for
the household reading the number. Every other daily artefact in this project
(the quality report's `history`, the counterfactual writer) is keyed the same
way, and a UTC key would put a Brisbane evening's forecast under the following
day for no one's benefit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "SNAPSHOT_VERSION",
    "SnapshotPoint",
    "build_snapshot",
    "day_key_for",
    "prune_snapshots",
    "resample_snapshot_to_grid",
]

#: Bumped only when the stored shape changes incompatibly. A reader that finds
#: a version it does not know must skip that day rather than guess at it --
#: a mis-parsed forecast would score as a real forecast error, which is the
#: single most misleading thing this whole mechanism could do.
SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class SnapshotPoint:
    """One forecast point: what solar and load were predicted to be at `time`."""

    time: datetime
    solar_kw: float
    load_kw: float


def day_key_for(moment: datetime) -> str:
    """The local-day key a snapshot is filed under (`YYYY-MM-DD`).

    Takes the datetime as given. A caller holding an aware datetime should
    convert it to the household's own timezone first -- this deliberately does
    not guess at a timezone, because silently filing a forecast under the wrong
    day is indistinguishable downstream from a forecast that was wrong.
    """
    return moment.strftime("%Y-%m-%d")


def build_snapshot(
    period_starts: list[datetime],
    solar_kw: list[float],
    load_kw: list[float],
    *,
    captured_at: datetime,
    horizon_hours: float | None = None,
) -> dict:
    """A serialisable record of what the forecast said, ready for a Store.

    Deliberately stores an explicit `time` per point rather than a start plus a
    step. This project's own grid is **tiered** -- the first periods are
    minutes wide and later ones an hour -- so a start-and-step encoding would
    silently mis-place every point past the tier boundary. `solar.py`'s own
    live anchor sits at index 0 for the same reason.

    Truncates to the shortest of the three inputs rather than raising: a solve
    that produced fewer load points than solar points is a real shape this
    codebase has seen, and refusing to capture anything at all that day would
    trade a partial record for none.
    """
    n = min(len(period_starts), len(solar_kw), len(load_kw))
    return {
        "version": SNAPSHOT_VERSION,
        "captured_at": captured_at.isoformat(),
        "horizon_hours": horizon_hours,
        "points": [
            {
                "time": period_starts[i].isoformat(),
                "solar_kw": float(solar_kw[i]),
                "load_kw": float(load_kw[i]),
            }
            for i in range(n)
        ],
    }


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def resample_snapshot_to_grid(
    snapshot: dict, grid_times: list[datetime]
) -> tuple[list[float], list[float]] | None:
    """Put a captured snapshot onto an arbitrary grid, nearest-at-or-before.

    Returns `None` -- never zeros -- when the snapshot is unusable: an
    unknown version, no points, or nothing at or before the first grid time.
    That distinction is the whole point. A missing forecast says **nothing**
    about forecast quality, whereas a zero-filled one scores as a forecast that
    confidently predicted darkness and no load, which would look like the worst
    possible forecast rather than like an absent one.

    `load_forecast_snapshot()`'s own docstring in the standalone writer makes
    the same demand of its callers: *"treat None as 'skip the forecast-regret
    decomposition for this day entirely', never as 'assume zero forecast
    error'."*

    Sample-and-hold, matching `resample_history_mean`'s documented principle
    for state-like series: a forecast point states a level expected to persist
    until the next point, so averaging across points would invent a value the
    forecast never made.
    """
    if not isinstance(snapshot, dict):
        return None
    if snapshot.get("version") != SNAPSHOT_VERSION:
        return None
    raw = snapshot.get("points") or []
    points: list[SnapshotPoint] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        when = _parse(str(p.get("time")))
        if when is None:
            continue
        try:
            points.append(
                SnapshotPoint(when, float(p["solar_kw"]), float(p["load_kw"]))
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not points or not grid_times:
        return None
    points.sort(key=lambda p: p.time)

    solar: list[float] = []
    load: list[float] = []
    idx = 0
    current: SnapshotPoint | None = None
    for t in grid_times:
        while idx < len(points) and points[idx].time <= t:
            current = points[idx]
            idx += 1
        if current is None:
            # The grid starts before the snapshot does. Refusing the whole day
            # is right: a partial grid silently filled with the first point would
            # attribute the forecast a prediction it never made for those
            # periods.
            return None
        solar.append(current.solar_kw)
        load.append(current.load_kw)
    return solar, load


def prune_snapshots(snapshots: dict, *, keep_days: int) -> dict:
    """Keep the newest `keep_days` snapshots by day key, drop the rest.

    Bounded because this lives in a `Store` that is rewritten whole: an
    unbounded dict would grow forever and be re-serialised on every write, the
    same slow leak `load_run_state.py` already guards against.

    `keep_days` defaults are the caller's business, but note the re-score
    service accepts 1-30 days back, so a retention shorter than that would
    create days that can be re-scored for EPR and not for forecast regret --
    an asymmetry a household would reasonably read as a bug.
    """
    if keep_days <= 0:
        return {}
    keys = sorted(k for k in snapshots if isinstance(k, str))
    keep = set(keys[-keep_days:])
    return {k: v for k, v in snapshots.items() if k in keep}
