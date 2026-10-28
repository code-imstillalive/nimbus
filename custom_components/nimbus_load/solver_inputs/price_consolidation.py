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

- `consolidate()`: per period, a weighted mean of the available sources
  that have earned a weight, else (none has) their median. `spread` is
  max - min across the available sources.
- `SourceErrorTracker`: records each source's forecast for each future
  5-minute interval at four lead buckets (keeping, per bucket, the forecast
  issued closest to that bucket's representative lead), and when an
  interval settles updates an EWMA of the absolute error per
  (source, bucket). `weights(bucket)` is inverse MAE, normalised, over the
  sources with at least `MIN_SAMPLES` settled samples: a source added later
  is scored first and joins the blend once it has earned a weight.

Standing offsets (household, 21 Oct 2026: "amber is always more expensive
than Lv - 2 c on average cos they have their hedging costs on top of their
fees"): a source priced on another contract is scored against the
provider's settled price, so its contract premium is not forecast error.
The tracker also keeps an EWMA of each source's signed error (its bias) per
lead bucket; the MAE scores the error left after removing the bias learned
so far, and a source that has earned a weight joins the blend with its bias
removed. The table still shows each source's own price.
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
#: The blend itself, scored beside the sources so the switch to `live` can
#: rest on its measured error (#1786: "chosen by measured least error"). It
#: never earns a weight, and it is scored on its raw error: it enters the
#: plan as it is, while each source enters with its offset removed.
BLEND_SOURCE = "consolidated"
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
    spike_threshold: float | None = None,
) -> tuple[list[float | None], list[float | None], str]:
    """Combine several per-period price series into one.

    `weights` is either one dict for every period, a per-period list of
    dicts (or None), or None. A period takes the weighted mean of the
    available sources with a positive weight; a source without one (still
    being scored) is left out of it. Only when no available source has a
    weight does the period take the median of them all. A period with no
    source is None in both returned lists.

    Household, 20 Oct 2026: adding two sources priced on other contracts
    (another retailer's price, a generic network tariff's estimate) moved
    the blend 6.5c above the provider's price before either had a single
    settled sample, because one unscored source sent every period back to
    the median. A source now has to earn its place.

    `spike_threshold` (household, 18 Oct 2026; dry run on #1786): a period
    where ANY source forecasts at or above it takes the highest source
    instead. Missing a real spike costs far more than acting on a false one,
    and a median or weighted mean drops a spike only one source carries: in
    the dry run the median lost $257 on a real $15 spike only LocalVolts saw.
    None or <= 0 = off.

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
        top = max(avail.values())
        if (
            spike_threshold is not None
            and spike_threshold > 0
            and top >= spike_threshold
        ):
            values.append(top)
        elif w and any(w.get(name, 0.0) > 0.0 for name in avail):
            use = [name for name in avail if w.get(name, 0.0) > 0.0]
            total = sum(w[name] for name in use)
            values.append(sum(avail[name] * w[name] for name in use) / total)
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


def retail_from_rrp_series(
    grid_times: Sequence[datetime],
    aemo_pts: Sequence[tuple[datetime, float]] | None,
    fees_by_5min: dict[int, float] | None,
    bucket_of: Callable[[datetime], int],
    scale: Callable[[datetime], float | None],
    rrp_now: float | None = None,
) -> list[float | None]:
    """nimbus #1786 (Mark): the retail price built from AEMO's regional
    reference price (RRP), as the contract defines it --
        sell = RRP x MLF x DLF + network export tariff
        buy  = (RRP x MLF x DLF + network import tariff + retailer usage fees)
               x 1.1 (GST)
    -- as RRP x `scale(t)` (MLF x DLF, x 1.1 on import) plus `fees_by_5min`,
    the learned tariff-and-fees term for the 5-minute slot (GST included on
    import). `rrp_now`, the current interval's dispatch RRP, prices the first
    period instead of the forecast: the retailer's own current row can still be
    a forecast level (#1786, 19 Oct). None where AEMO has no point yet or
    `scale` has no factor."""
    if not aemo_pts:
        return [None] * len(grid_times)
    pts = sorted(aemo_pts, key=lambda p: p[0])
    fees = fees_by_5min or {}
    out: list[float | None] = []
    j = -1
    for i, gt in enumerate(grid_times):
        while j + 1 < len(pts) and pts[j + 1][0] <= gt:
            j += 1
        k = scale(gt)
        if i == 0 and rrp_now is not None:
            rrp = rrp_now
        else:
            rrp = float(pts[j][1]) if j >= 0 else None
        if k is None or rrp is None:
            out.append(None)
        else:
            out.append(rrp * k + float(fees.get(bucket_of(gt), 0.0)))
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
        self.bias: dict[str, dict[str, float]] = {}
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
                signed = forecast - settled
                b = self.bias.setdefault(source, {}).get(bucket)
                # Scored after removing the offset learned so far, so a
                # steady contract premium is not counted as forecast error.
                # The blend is scored as it is (BLEND_SOURCE).
                err = (
                    abs(signed - (b or 0.0)) if source != BLEND_SOURCE else abs(signed)
                )
                n = self.samples.setdefault(source, {}).get(bucket, 0)
                prev = self.mae.setdefault(source, {}).get(bucket)
                self.mae[source][bucket] = (
                    err if prev is None else (1 - EWMA_ALPHA) * prev + EWMA_ALPHA * err
                )
                self.bias[source][bucket] = (
                    signed if b is None else (1 - EWMA_ALPHA) * b + EWMA_ALPHA * signed
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
        """Inverse-MAE weights, normalised, over those of `sources`
        (default: every source with any samples) with at least MIN_SAMPLES
        settled samples in this bucket; a source with fewer is still being
        scored and has no weight. None when no source has earned one."""
        names = list(sources) if sources is not None else sorted(self.mae)
        names = [n for n in names if n != BLEND_SOURCE]
        inv: dict[str, float] = {}
        for name in names:
            if self.samples.get(name, {}).get(bucket, 0) < MIN_SAMPLES:
                continue
            inv[name] = 1.0 / max(self.mae[name][bucket], _MAE_FLOOR)
        if not inv:
            return None
        total = sum(inv.values())
        return {name: v / total for name, v in inv.items()}

    def offset(self, source: str, bucket: str | None) -> float:
        """The standing offset to remove from `source`'s forecast at this
        lead: its learned bias once it has MIN_SAMPLES settled samples in
        the bucket, else 0."""
        if bucket is None or self.samples.get(source, {}).get(bucket, 0) < MIN_SAMPLES:
            return 0.0
        return self.bias.get(source, {}).get(bucket, 0.0)

    def debiased(
        self,
        now: datetime,
        grid_times: Sequence[datetime],
        series: dict[str, list[float | None]],
    ) -> dict[str, list[float | None]]:
        """Each source's series with its standing offset removed, period by
        period at that period's lead."""
        buckets = [lead_bucket((gt - now).total_seconds() / 60.0) for gt in grid_times]
        out: dict[str, list[float | None]] = {}
        for name, s in series.items():
            out[name] = [
                None
                if v is None
                else float(v)
                - self.offset(name, buckets[i] if i < len(buckets) else None)
                for i, v in enumerate(s)
            ]
        return out

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
            "bias": {s: dict(b) for s, b in self.bias.items()},
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
            t.bias = {
                str(s): {str(b): float(x) for b, x in bs.items()}
                for s, bs in (data.get("bias") or {}).items()
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
            "bias_by_source_and_lead": {
                s: {b: round(v, 5) for b, v in self.bias.get(s, {}).items()}
                for s in sources
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
    spike_threshold: float | None = None,
) -> tuple[list[float | None], dict[str, Any], bool]:
    """One side's whole cycle: consolidate with the weights earned so far,
    score the interval that just ended, record this solve's forecasts for
    the future intervals (never the current settled block), and summarise.
    Returns `(values, published summary, whether anything was scored)`."""
    names = sorted(series)
    # Blend with each source's standing offset removed; spikes are still
    # judged on the sources' own prices.
    values, _, method = consolidate(
        tracker.debiased(now, grid_times, series),
        tracker.weights_by_period(now, grid_times, names),
        None,
    )
    # How far apart the sources' own prices are, offsets and all.
    _, spread, _ = consolidate(series)
    if spike_threshold is not None and spike_threshold > 0:
        for i in range(len(values)):
            top = max(_available(series, i).values(), default=None)
            if top is not None and top >= spike_threshold:
                values[i] = top
    scored = tracker.observe_settled(now, settled)
    for name, s in series.items():
        tracker.record_series(
            name, now, grid_times, period_hours, s, first_index=n_settled
        )
    tracker.record_series(
        BLEND_SOURCE, now, grid_times, period_hours, values, first_index=n_settled
    )
    head = spread[n_settled] if n_settled < len(spread) else None
    summary = {
        "sources": names,
        "method": method,
        "spread_now": round(head, 5) if head is not None else None,
        "now": now_slot(series, values, grid_times, settled),
        "next": next_slots(series, values, grid_times, n_settled),
        **tracker.summary(),
    }
    return values, summary, scored


#: How many future periods each source's own price is published for: as
#: many as the Solver tab's full-width Price sources table fits at the
#: narrowest four-column screen (household, 19 Oct 2026: "fill in as many
#: slots as the table would fit across 4 columns"). 24 -> 30 (household,
#: 20 Oct 2026): at 24 the right fifth of the table stood empty on their
#: screen.
NEXT_SLOTS = 30


def _r(value: float | None) -> float | None:
    return round(float(value), 4) if value is not None else None


def now_slot(
    series: dict[str, list[float | None]],
    values: Sequence[float | None],
    grid_times: Sequence[datetime],
    settled: float | None,
) -> dict[str, Any]:
    """The interval running now: the price that settles for it (what the
    plan uses), each source's own price for it and the blend (household,
    21 Oct 2026: the table started 5 min late, "the first 5 min is
    missing"). Empty when the plan has no periods."""
    if not grid_times:
        return {}
    sources = {name: _r(s[0]) if s else None for name, s in sorted(series.items())}
    # The provider's own series covers only the periods its forecast does,
    # and the running interval is settled, so its slot is empty: the price
    # that settles for it is the provider's price for now (household, 21 Oct
    # 2026: the Now column's provider cell and Gap were blank).
    if "primary" in sources and sources["primary"] is None:
        sources["primary"] = _r(settled)
    return {
        "time": grid_times[0].isoformat(),
        "settled": _r(settled),
        "sources": sources,
        "consolidated": _r(values[0]) if values else None,
    }


def next_slots(
    series: dict[str, list[float | None]],
    values: Sequence[float | None],
    grid_times: Sequence[datetime],
    n_settled: int,
) -> dict[str, Any]:
    """Each source's own price and the blend for the next `NEXT_SLOTS`
    periods after the settled block (household, 19 Oct 2026, with Mark
    Purcell: "show the differences between sources"). Small on purpose: it
    rides on the plan's attributes."""
    stop = min(len(grid_times), n_settled + NEXT_SLOTS)
    span = range(n_settled, stop)
    return {
        "times": [grid_times[i].isoformat() for i in span],
        "sources": {
            name: [_r(s[i]) if i < len(s) else None for i in span]
            for name, s in sorted(series.items())
        },
        "consolidated": [_r(values[i]) if i < len(values) else None for i in span],
    }


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
