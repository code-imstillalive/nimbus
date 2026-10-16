"""Consolidated price forecast and per-source running error (nimbus #1786).

Mark Purcell on #1786: the consolidation rule should be chosen by measured
least error on live data, and confidence should come from each source's
running mean absolute error. This module is the measurement half: it
combines every price source Nimbus already reads into one series per side
(import, export) and keeps each source's running error by forecast lead, so
the rule that wins can be adopted on evidence.

Pure: no Home Assistant import, no I/O. `solver_writer` owns the reads, the
state file and the switch; this module only computes.

Two pieces:

- `consolidate()`: per period, a weighted mean of the sources available in
  that period when weights cover all of them, else their median. `spread`
  is max - min across the available sources.
- `SourceErrorTracker`: records each source's forecast for each future
  5-minute interval at four lead buckets (keeping, per bucket, the forecast
  issued closest to that bucket's representative lead), and when an
  interval settles updates an EWMA of the absolute error per
  (source, bucket). `weights(bucket)` is inverse MAE, normalised, only once
  every included source has at least `MIN_SAMPLES` settled samples.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from statistics import median
from typing import Any

INTERVAL_SECONDS = 300

#: (name, lower minutes inclusive, upper minutes exclusive, representative lead).
#: The representative lead is the bucket's midpoint: of the forecasts issued
#: for one interval inside a bucket, the one closest to it is kept.
LEAD_BUCKETS: tuple[tuple[str, float, float, float], ...] = (
    ("0-5 min", 0.0, 5.0, 2.5),
    ("5-60 min", 5.0, 60.0, 32.5),
    ("1-4 h", 60.0, 240.0, 150.0),
    ("4-24 h", 240.0, 1440.0, 840.0),
)

#: EWMA smoothing for the absolute error (about a 20-sample memory).
EWMA_ALPHA = 0.05
#: Settled samples every included source needs before weights are offered.
MIN_SAMPLES = 50
#: Pending forecasts are never kept further ahead than this.
MAX_PENDING_AHEAD_HOURS = 48.0
#: Floor on MAE when inverting it, so a source with zero error so far does
#: not take an infinite weight.
_MAE_FLOOR = 1e-4

Weights = dict[str, float]


def lead_bucket(lead_minutes: float) -> str | None:
    """The lead bucket a lead in minutes falls in, or None outside 0-24 h."""
    for name, lo, hi, _rep in LEAD_BUCKETS:
        if lo <= lead_minutes < hi:
            return name
    return None


def _available(
    series_by_source: dict[str, Sequence[float | None]], i: int
) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, series in series_by_source.items():
        if i < len(series):
            v = series[i]
            if v is not None:
                out[name] = float(v)
    return out


def consolidate(
    series_by_source: dict[str, Sequence[float | None]],
    weights: Weights | Sequence[Weights | None] | None = None,
) -> tuple[list[float | None], list[float | None], str]:
    """Combine several per-period price series into one.

    `weights` is either one dict for every period, a per-period list of
    dicts (or None), or None. A period uses the weighted mean only when the
    weights give a positive weight to EVERY source available in that
    period; otherwise it uses the median of the available sources. A period
    with no source is None in both returned lists.

    Returns `(values, spread, method)`, where `method` is "weighted" when
    every valued period was weighted, "median" when none was, and "mixed"
    otherwise ("none" when no period had any source).
    """
    n = max((len(s) for s in series_by_source.values()), default=0)
    values: list[float | None] = []
    spread: list[float | None] = []
    n_weighted = 0
    n_valued = 0
    for i in range(n):
        avail = _available(series_by_source, i)
        if not avail:
            values.append(None)
            spread.append(None)
            continue
        n_valued += 1
        w: Weights | None
        if weights is None or isinstance(weights, dict):
            w = weights
        else:
            w = weights[i] if i < len(weights) else None
        if w and all(w.get(name, 0.0) > 0.0 for name in avail):
            total = sum(w[name] for name in avail)
            values.append(sum(avail[name] * w[name] for name in avail) / total)
            n_weighted += 1
        else:
            values.append(float(median(avail.values())))
        spread.append(max(avail.values()) - min(avail.values()))
    if n_valued == 0:
        method = "none"
    elif n_weighted == n_valued:
        method = "weighted"
    elif n_weighted == 0:
        method = "median"
    else:
        method = "mixed"
    return values, spread, method


def aemo_markup_series(
    grid_times: Sequence[datetime],
    aemo_pts: Sequence[tuple[datetime, float]] | None,
    offset_by_5min: dict[int, float] | None,
    bucket_of: Callable[[datetime], int],
) -> list[float | None]:
    """The regional wholesale forecast plus the learned 5-minute-of-day retail
    markup, on `grid_times` -- the same arithmetic
    `resample_price_with_extrapolation()` uses past a source's own coverage,
    but over the whole horizon, so it can be scored as a source of its own.
    None before AEMO's first point, and everywhere when AEMO is absent."""
    if not aemo_pts:
        return [None] * len(grid_times)
    pts = sorted(aemo_pts, key=lambda p: p[0])
    offsets = offset_by_5min or {}
    out: list[float | None] = []
    j = -1
    for gt in grid_times:
        while j + 1 < len(pts) and pts[j + 1][0] <= gt:
            j += 1
        if j < 0:
            out.append(None)
        else:
            out.append(float(pts[j][1]) + float(offsets.get(bucket_of(gt), 0.0)))
    return out


def interval_key(t: datetime) -> int:
    """The 5-minute interval containing `t`, as epoch seconds // 300."""
    return int(t.timestamp()) // INTERVAL_SECONDS


class SourceErrorTracker:
    """Running per-source, per-lead absolute error of a price forecast.

    `pending[interval][source][bucket] = [lead_minutes, forecast]` holds, for
    each future interval, the forecast closest to each bucket's
    representative lead. `observe_settled()` is called every solve with the
    current interval and its settled price; the previous interval is scored
    once, with the last settled value seen for it, as soon as a later
    interval is observed.
    """

    def __init__(self) -> None:
        self.pending: dict[int, dict[str, dict[str, list[float]]]] = {}
        self.mae: dict[str, dict[str, float]] = {}
        self.samples: dict[str, dict[str, int]] = {}
        self.last_interval: int | None = None
        self.last_settled: float | None = None

    # -- recording -----------------------------------------------------

    def record(
        self,
        source: str,
        now: datetime,
        interval_start: datetime,
        value: float | None,
    ) -> None:
        """Record `source`'s forecast `value` for the interval starting at
        `interval_start`, issued at `now`."""
        if value is None:
            return
        lead = (interval_start - now).total_seconds() / 60.0
        if lead < 0 or lead > MAX_PENDING_AHEAD_HOURS * 60.0:
            return
        bucket = lead_bucket(lead)
        if bucket is None:
            return
        rep = next(b[3] for b in LEAD_BUCKETS if b[0] == bucket)
        slot = (
            self.pending.setdefault(interval_key(interval_start), {})
            .setdefault(source, {})
            .get(bucket)
        )
        if slot is None or abs(lead - rep) <= abs(slot[0] - rep):
            self.pending[interval_key(interval_start)][source][bucket] = [
                lead,
                float(value),
            ]

    def record_series(
        self,
        source: str,
        now: datetime,
        grid_times: Sequence[datetime],
        period_hours: Sequence[float],
        series: Sequence[float | None],
        first_index: int = 0,
    ) -> None:
        """Record a per-period series, expanded onto every 5-minute interval
        each period covers. Periods before `first_index` (the current
        settled block) are skipped."""
        horizon = now + timedelta(hours=MAX_PENDING_AHEAD_HOURS)
        for i in range(first_index, min(len(grid_times), len(series))):
            v = series[i]
            if v is None:
                continue
            start = grid_times[i]
            hours = period_hours[i] if i < len(period_hours) else 0.5
            end = start + timedelta(hours=float(hours))
            # Every 5-minute interval the period overlaps, starting with the
            # one containing its start (the current one is skipped by
            # record(), whose lead would be negative).
            t = datetime.fromtimestamp(
                interval_key(start) * INTERVAL_SECONDS, start.tzinfo
            )
            while t < end and t <= horizon:
                self.record(source, now, t, v)
                t += timedelta(seconds=INTERVAL_SECONDS)

    # -- settling ------------------------------------------------------

    def settle(self, key: int, settled: float) -> int:
        """Score every pending forecast for interval `key` against `settled`,
        then drop it and anything older. Returns how many were scored."""
        scored = 0
        for source, buckets in self.pending.get(key, {}).items():
            for bucket, (_lead, forecast) in buckets.items():
                err = abs(forecast - settled)
                n = self.samples.setdefault(source, {}).get(bucket, 0)
                prev = self.mae.setdefault(source, {}).get(bucket)
                self.mae[source][bucket] = (
                    err if prev is None else (1 - EWMA_ALPHA) * prev + EWMA_ALPHA * err
                )
                self.samples[source][bucket] = n + 1
                scored += 1
        for k in [k for k in self.pending if k <= key]:
            del self.pending[k]
        return scored

    def observe_settled(self, now: datetime, settled: float | None) -> bool:
        """Called every solve. Returns True when any forecast was scored."""
        key = interval_key(now)
        scored = False
        if self.last_interval is not None and key > self.last_interval:
            if self.last_settled is not None:
                scored = self.settle(self.last_interval, self.last_settled) > 0
            self.last_settled = None
        if self.last_interval is None or key >= self.last_interval:
            self.last_interval = key
            if settled is not None:
                self.last_settled = float(settled)
        # Bounded memory: nothing older than the current interval survives.
        for k in [k for k in self.pending if k < key]:
            del self.pending[k]
        return scored

    # -- weights -------------------------------------------------------

    def weights(
        self, bucket: str, sources: Sequence[str] | None = None
    ) -> Weights | None:
        """Inverse-MAE weights, normalised, over `sources` (default: every
        source with any samples). None unless every included source has at
        least MIN_SAMPLES settled samples in this bucket."""
        names = list(sources) if sources is not None else sorted(self.mae)
        if not names:
            return None
        inv: dict[str, float] = {}
        for name in names:
            if self.samples.get(name, {}).get(bucket, 0) < MIN_SAMPLES:
                return None
            inv[name] = 1.0 / max(self.mae[name][bucket], _MAE_FLOOR)
        total = sum(inv.values())
        return {name: v / total for name, v in inv.items()}

    def weights_by_period(
        self,
        now: datetime,
        grid_times: Sequence[datetime],
        sources: Sequence[str],
    ) -> list[Weights | None]:
        cache: dict[str | None, Weights | None] = {}
        out: list[Weights | None] = []
        for gt in grid_times:
            b = lead_bucket((gt - now).total_seconds() / 60.0)
            if b not in cache:
                cache[b] = self.weights(b, sources) if b is not None else None
            out.append(cache[b])
        return out

    # -- persistence ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending": {
                str(k): {s: dict(b) for s, b in v.items()}
                for k, v in self.pending.items()
            },
            "mae": {s: dict(b) for s, b in self.mae.items()},
            "samples": {s: dict(b) for s, b in self.samples.items()},
            "last_interval": self.last_interval,
            "last_settled": self.last_settled,
        }

    @classmethod
    def from_dict(cls, data: Any) -> SourceErrorTracker:
        """Tolerant: anything malformed comes back as an empty tracker."""
        t = cls()
        if not isinstance(data, dict):
            return t
        try:
            for k, v in (data.get("pending") or {}).items():
                t.pending[int(k)] = {
                    str(s): {str(b): [float(x[0]), float(x[1])] for b, x in bs.items()}
                    for s, bs in v.items()
                }
            t.mae = {
                str(s): {str(b): float(x) for b, x in bs.items()}
                for s, bs in (data.get("mae") or {}).items()
            }
            t.samples = {
                str(s): {str(b): int(x) for b, x in bs.items()}
                for s, bs in (data.get("samples") or {}).items()
            }
            li = data.get("last_interval")
            t.last_interval = int(li) if li is not None else None
            ls = data.get("last_settled")
            t.last_settled = float(ls) if ls is not None else None
        except (TypeError, ValueError, AttributeError, IndexError):
            return cls()
        return t

    def summary(self) -> dict[str, Any]:
        """The published view: rounded MAE and sample counts by source and
        lead, and the weights each bucket would use."""
        sources = sorted(self.mae)
        return {
            "mae_by_source_and_lead": {
                s: {b: round(v, 5) for b, v in self.mae[s].items()} for s in sources
            },
            "samples": {s: dict(self.samples.get(s, {})) for s in sources},
            "weights": {
                b[0]: self.weights(b[0], sources) if sources else None
                for b in LEAD_BUCKETS
            },
        }


def consolidate_side(
    tracker: SourceErrorTracker,
    series: dict[str, list[float | None]],
    now: datetime,
    grid_times: Sequence[datetime],
    period_hours: Sequence[float],
    n_settled: int,
    settled: float | None,
) -> tuple[list[float | None], dict[str, Any], bool]:
    """One side's whole cycle: consolidate with the weights earned so far,
    score the interval that just ended, record this solve's forecasts for
    the future intervals (never the current settled block), and summarise.
    Returns `(values, published summary, whether anything was scored)`."""
    names = sorted(series)
    values, spread, method = consolidate(
        series, tracker.weights_by_period(now, grid_times, names)
    )
    scored = tracker.observe_settled(now, settled)
    for name, s in series.items():
        tracker.record_series(
            name, now, grid_times, period_hours, s, first_index=n_settled
        )
    head = spread[n_settled] if n_settled < len(spread) else None
    summary = {
        "sources": names,
        "method": method,
        "spread_now": round(head, 5) if head is not None else None,
        **tracker.summary(),
    }
    return values, summary, scored


def replace_future(
    values: Sequence[float | None],
    target: list[float],
    labels: list[str],
    first_index: int,
) -> None:
    """Live mode: in place, every period from `first_index` on that has a
    consolidated value takes it, labelled "consolidated". Periods before
    `first_index` (the current settled block) and periods with no value are
    left exactly as they are."""
    for i in range(first_index, min(len(values), len(target))):
        v = values[i]
        if v is not None:
            target[i] = float(v)
            labels[i] = "consolidated"
