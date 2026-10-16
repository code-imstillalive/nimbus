"""Signed price-forecast error history, by source, lead and regime (nimbus #1797).

## Why

The two-stage stochastic dispatch (#1805) needs price scenarios, and price
scenarios need to be calibrated from how this install's own price forecasts
have actually missed: how far, in which direction, at which lead, and
whether a forecast spike turned up. `price_consolidation.SourceErrorTracker`
(#1786) keeps only an EWMA of the ABSOLUTE error per (source, lead bucket),
which has no sign, no spread and no memory of spikes. Forecast history cannot
be backfilled, so this module starts recording the distribution now. Nothing
in dispatch reads it yet.

## What is recorded

For every source `consolidate_side()` already scores -- `primary` (the
configured retail price sensor), `secondary_2` / `secondary_3` (the optional
extra price sensors) and `aemo_markup` (AEMO's regional forecast plus the
learned retail markup) -- on each side (import, export), each 5-minute
interval's forecast is kept at up to five lead buckets (the forecast issued
closest to each bucket's representative lead, exactly as the tracker does),
and graded once the interval has settled against the same settled price the
tracker uses. The fifth bucket, `>24 h` (24-48 h), is beyond the tracker's
range; the tracker's own four are reused unchanged.

Each graded pair `(forecast, settled)` is filed by regime: forecast at or
above the spike threshold, or below it. The threshold is an existing setting,
never a new one: `number.nimbus_solver_price_spike_threshold` ($/kWh) when it
is set, else the P5MIN false-spike threshold
(`number.nimbus_solver_p5min_false_spike_threshold_mwh`, through
`p5min_guard.threshold_kwh`). The P5MIN figure is a wholesale price; the
series graded here are retail-level (wholesale plus markup), which differs by
cents against a threshold measured in dollars. With both set to 0 (off) every
pair is "below" and no spike counts are kept.

## Bounded memory

Three classes per (side, source, bucket), each capped at
`MAX_SAMPLES_PER_CELL` and pruned to `KEEP_DAYS`:

- `spike`: forecast at or above the threshold. All kept.
- `surprise`: forecast below, settled at or above. All kept.
- `normal`: both below. Only one 5-minute interval in `STRIDE` is kept,
  its position rotating through the half hour (`on_stride`), so the
  common case does not crowd out three weeks of history.

Rare events are therefore never subsampled. The below-regime quantiles use
only on-stride pairs (normal and surprise alike), so the subsampling does
not over-weight surprises. Samples carry the forecast and the settled price,
so a changed threshold re-files what is kept; it cannot recover off-stride
pairs that were never stored.

## What is computed, and where it goes

`summarise()`, a pure function: per (side, source, lead bucket, regime) the
sample count, distinct days, empirical q10/q50/q90 of `settled - forecast`
and of `settled / forecast`, and the share of each day's pairs falling
outside the q10/q90 band fitted on the other days (leave-one-day-out, as
#1807). The ratio is ill-defined where the forecast is near zero or negative,
so it is computed only over pairs whose forecast is at least
`RATIO_MIN_FORECAST`, and `ratio_ok` says whether those are at least
`RATIO_OK_SHARE` of the cell. Per (side, source, bucket): spike hits, misses
(false spikes) and surprises, and `p_hit`, the posterior mean of
P(settled spike | forecast spike) under a Beta(1, 1) prior.

That full table is written into the state file beside the samples. The
published attribute (`compact()`, inside `price_consolidation.error_history`
on `sensor.nimbus_solver_battery_forecast`, where #1786's per-source error
already lives) carries only, per (side, source, bucket), the below-threshold
count and difference quantiles and the spike counts and p_hit: about 50 bytes
a bucket, because that sensor is close to the recorder's 16 KB cap.

Prior art (CHECK PRIOR ART directive): EMHASS takes a price forecast as one
path with no record of its error; its chance-constrained MPC study is a
research prototype on load quantiles. HAEO has no forecast-error record.
Nimbus's own prior art is #1786's `SourceErrorTracker` (pending/bucket layout
and settle point, reused) and #1807's `SolarErrorHistory` (signed samples,
bounded memory, leave-one-day-out).

Never raises: any failure publishes `{"error": ...}` and the solve goes on.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import numpy as np

try:
    from .. import p5min_guard, solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import p5min_guard  # type: ignore[no-redef]
    import solver_shared  # type: ignore[no-redef]

try:
    from . import price_consolidation as pc
except ImportError:  # pragma: no cover - standalone/cron path
    import price_consolidation as pc  # type: ignore[no-redef]

INTERVAL_SECONDS = pc.INTERVAL_SECONDS

#: The tracker's four buckets, unchanged, plus one beyond its 24 h range.
LEAD_BUCKETS: tuple[tuple[str, float, float, float], ...] = (
    *pc.LEAD_BUCKETS,
    (">24 h", 1440.0, 2880.0, 2160.0),
)
MAX_LEAD_MINUTES = 2880.0

SIDES = ("import", "export")
CLS_SPIKE = "spike"
CLS_SURPRISE = "surprise"
CLS_NORMAL = "normal"
CLASSES = (CLS_SPIKE, CLS_SURPRISE, CLS_NORMAL)
REGIME_BELOW = "below"
REGIME_SPIKE = "at_or_above"

KEEP_DAYS = 21
#: One normal pair in STRIDE intervals is kept: 48 a day, 1,008 over KEEP_DAYS.
STRIDE = 6
MAX_SAMPLES_PER_CELL = 1100
#: Pairs a cell needs before its quantiles are published.
MIN_SAMPLES = 30
#: $/kWh. Below this the forecast is too near zero for a ratio to mean much.
RATIO_MIN_FORECAST = 0.01
RATIO_OK_SHARE = 0.9
#: The state file is rewritten at most this often (and on the first grade
#: after a restart); a restart loses at most this much history.
SAVE_EVERY = timedelta(minutes=30)

#: Column order of each published regime cell.
COLUMNS = (
    "n",
    "days",
    "d10",
    "d50",
    "d90",
    "d_below_pct",
    "d_above_pct",
    "r_n",
    "r10",
    "r50",
    "r90",
    "r_below_pct",
    "r_above_pct",
    "ratio_ok",
)
SPIKE_COLUMNS = ("hits", "misses", "surprises", "p_hit")
#: Column order of the published attribute, per (side, source, bucket): the
#: below-threshold regime's count and difference quantiles, then the spike
#: counts and p_hit. The full table (`summarise()`) is kept in the state file.
COMPACT_COLUMNS = ("n", "d10", "d50", "d90", *SPIKE_COLUMNS)


def lead_bucket(lead_minutes: float) -> str | None:
    """The bucket a lead in minutes falls in, or None outside 0-48 h."""
    for name, lo, hi, _rep in LEAD_BUCKETS:
        if lo <= lead_minutes < hi:
            return name
    return None


def _rep_lead(bucket: str) -> float:
    return next(b[3] for b in LEAD_BUCKETS if b[0] == bucket)


def on_stride(key: int) -> bool:
    """Whether interval `key` is the one normal pair kept in its half hour.
    The kept position rotates from one half hour to the next."""
    return key % STRIDE == (key // STRIDE) % STRIDE


def classify(forecast: float, settled: float, threshold: float | None) -> str:
    if threshold is None:
        return CLS_NORMAL
    if forecast >= threshold:
        return CLS_SPIKE
    if settled >= threshold:
        return CLS_SURPRISE
    return CLS_NORMAL


def spike_threshold(cfg: dict) -> tuple[float | None, str | None]:
    """The existing spike threshold in $/kWh and the setting it came from;
    (None, None) when both are off."""
    price = solver_shared._cfg_num(cfg, "solver_price_spike_threshold", 0.0)
    if price > 0:
        return float(price), "solver_price_spike_threshold"
    key = "solver_p5min_false_spike_threshold_mwh"
    kwh = p5min_guard.threshold_kwh(
        solver_shared._cfg_num(cfg, key, p5min_guard.DEFAULT_THRESHOLD_MWH)
    )
    if math.isfinite(kwh):
        return round(kwh, 6), key
    return None, None


Sample = list[float]  # [interval key, local day ordinal, forecast, settled]


class PriceErrorHistory:
    """Signed (forecast, settled) pairs by side, source, lead bucket and class.

    `pending[side][interval][source][bucket] = [lead_minutes, forecast]`;
    `samples[side][source][bucket][class]` is a list of
    `[interval_key, day_ordinal, forecast, settled]`.
    """

    def __init__(self) -> None:
        self.pending: dict[str, dict[int, dict[str, dict[str, list[float]]]]] = {
            s: {} for s in SIDES
        }
        self.samples: dict[str, dict[str, dict[str, dict[str, list[Sample]]]]] = {
            s: {} for s in SIDES
        }
        self.last_interval: dict[str, int | None] = dict.fromkeys(SIDES)
        self.last_settled: dict[str, float | None] = dict.fromkeys(SIDES)
        self.threshold: float | None = None
        #: `summarise()` of the current samples; None once they change.
        self.summary: dict[str, Any] | None = None

    # -- recording ------------------------------------------------------

    def record(
        self,
        side: str,
        source: str,
        now: datetime,
        interval_start: datetime,
        value: float | None,
    ) -> None:
        if value is None:
            return
        lead = (interval_start - now).total_seconds() / 60.0
        if lead < 0 or lead >= MAX_LEAD_MINUTES:
            return
        bucket = lead_bucket(lead)
        if bucket is None:
            return
        key = pc.interval_key(interval_start)
        rep = _rep_lead(bucket)
        by_source = self.pending[side].setdefault(key, {}).setdefault(source, {})
        slot = by_source.get(bucket)
        if slot is None or abs(lead - rep) <= abs(slot[0] - rep):
            by_source[bucket] = [round(lead, 3), float(value)]

    def record_series(
        self,
        side: str,
        source: str,
        now: datetime,
        grid_times: Sequence[datetime],
        period_hours: Sequence[float],
        series: Sequence[float | None],
        first_index: int = 0,
    ) -> None:
        """A per-period series, expanded onto every 5-minute interval each
        period covers, from `first_index` on (as the tracker does)."""
        horizon = now + timedelta(minutes=MAX_LEAD_MINUTES)
        for i in range(first_index, min(len(grid_times), len(series))):
            v = series[i]
            if v is None:
                continue
            start = grid_times[i]
            hours = period_hours[i] if i < len(period_hours) else 0.5
            end = start + timedelta(hours=float(hours))
            t = datetime.fromtimestamp(
                pc.interval_key(start) * INTERVAL_SECONDS, start.tzinfo
            )
            while t < end and t < horizon:
                self.record(side, source, now, t, v)
                t += timedelta(seconds=INTERVAL_SECONDS)

    # -- settling -------------------------------------------------------

    def _file(self, side: str, source: str, bucket: str, s: Sample) -> bool:
        cls = classify(s[2], s[3], self.threshold)
        if cls == CLS_NORMAL and not on_stride(int(s[0])):
            return False
        self.samples[side].setdefault(source, {}).setdefault(bucket, {}).setdefault(
            cls, []
        ).append(s)
        self.summary = None
        return True

    def settle(self, side: str, key: int, settled: float, tz: Any) -> int:
        """Grade every pending forecast for interval `key`, then drop it and
        anything older. Returns how many pairs were kept."""
        day = datetime.fromtimestamp(key * INTERVAL_SECONDS, tz).date().toordinal()
        kept = 0
        for source, buckets in self.pending[side].get(key, {}).items():
            for bucket, (_lead, forecast) in buckets.items():
                sample = [key, day, round(float(forecast), 5), round(settled, 5)]
                kept += self._file(side, source, bucket, sample)
        for k in [k for k in self.pending[side] if k <= key]:
            del self.pending[side][k]
        return kept

    def observe_settled(self, side: str, now: datetime, settled: float | None) -> int:
        """Called every solve with the current interval's settled price; the
        previous interval is graded with the last value seen for it as soon
        as a later one is observed (the tracker's own rule)."""
        key = pc.interval_key(now)
        kept = 0
        last = self.last_interval[side]
        if last is not None and key > last:
            prev = self.last_settled[side]
            if prev is not None:
                kept = self.settle(side, last, prev, now.tzinfo)
            self.last_settled[side] = None
        if last is None or key >= last:
            self.last_interval[side] = key
            if settled is not None:
                self.last_settled[side] = float(settled)
        for k in [k for k in self.pending[side] if k < key]:
            del self.pending[side][k]
        return kept

    def set_threshold(self, threshold: float | None) -> bool:
        """Adopt `threshold`; on a change, re-file every kept pair under it.
        Returns whether anything changed."""
        if threshold == self.threshold:
            return False
        self.threshold = threshold
        self.summary = None
        for side in SIDES:
            old = self.samples[side]
            self.samples[side] = {}
            for source, buckets in old.items():
                for bucket, classes in buckets.items():
                    pairs = sorted(
                        (s for c in classes.values() for s in c), key=lambda s: s[0]
                    )
                    for s in pairs:
                        self._file(side, source, bucket, s)
        return True

    def prune(self, now: datetime) -> None:
        self.summary = None
        cutoff = now.date().toordinal() - KEEP_DAYS
        for side in SIDES:
            for buckets in self.samples[side].values():
                for classes in buckets.values():
                    for cls in list(classes):
                        kept = [s for s in classes[cls] if s[1] > cutoff]
                        classes[cls] = kept[-MAX_SAMPLES_PER_CELL:]

    def pending_count(self) -> int:
        return sum(len(v) for v in self.pending.values())

    def sample_count(self) -> int:
        return sum(
            len(c)
            for side in SIDES
            for buckets in self.samples[side].values()
            for classes in buckets.values()
            for c in classes.values()
        )

    # -- persistence ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "last_interval": dict(self.last_interval),
            "last_settled": dict(self.last_settled),
            "pending": {
                side: {str(k): v for k, v in self.pending[side].items()}
                for side in SIDES
            },
            "samples": self.samples,
        }

    @classmethod
    def from_dict(cls, data: Any) -> PriceErrorHistory:
        """Tolerant: anything malformed comes back empty."""
        h = cls()
        if not isinstance(data, dict):
            return h
        try:
            t = data.get("threshold")
            h.threshold = float(t) if t is not None else None
            for side in SIDES:
                li = (data.get("last_interval") or {}).get(side)
                h.last_interval[side] = int(li) if li is not None else None
                ls = (data.get("last_settled") or {}).get(side)
                h.last_settled[side] = float(ls) if ls is not None else None
                for k, v in ((data.get("pending") or {}).get(side) or {}).items():
                    h.pending[side][int(k)] = {
                        str(src): {
                            str(b): [float(x[0]), float(x[1])] for b, x in bs.items()
                        }
                        for src, bs in v.items()
                    }
                for src, buckets in (
                    (data.get("samples") or {}).get(side) or {}
                ).items():
                    for b, classes in buckets.items():
                        for c, pairs in classes.items():
                            if c not in CLASSES:
                                continue
                            h.samples[side].setdefault(str(src), {}).setdefault(
                                str(b), {}
                            )[c] = [
                                [int(s[0]), int(s[1]), float(s[2]), float(s[3])]
                                for s in pairs
                            ]
        except (TypeError, ValueError, AttributeError, IndexError, KeyError):
            return cls()
        return h


# -- the distribution (pure) -------------------------------------------------


def _q3(values: np.ndarray) -> tuple[float, float, float]:
    q10, q50, q90 = np.quantile(values, [0.1, 0.5, 0.9])
    return float(q10), float(q50), float(q90)


def loo_band_coverage(
    days: Sequence[int], values: Sequence[float]
) -> tuple[float | None, float | None]:
    """(% below q10, % above q90) of `values`, each day graded against the
    q10/q90 fitted on the other days. None with fewer than two days."""
    d = np.asarray(days)
    v = np.asarray(values, dtype=float)
    unique = np.unique(d)
    if len(unique) < 2 or len(v) == 0:
        return None, None
    below = above = 0
    for day in unique:
        test = d == day
        q10, q90 = np.quantile(v[~test], [0.1, 0.9])
        below += int(np.sum(v[test] < q10 - 1e-9))
        above += int(np.sum(v[test] > q90 + 1e-9))
    return round(100.0 * below / len(v), 1), round(100.0 * above / len(v), 1)


def cell_stats(pairs: Sequence[Sample]) -> list[Any]:
    """One regime cell, in `COLUMNS` order. Quantiles and coverage are None
    below `MIN_SAMPLES` pairs (the ratio's below `MIN_SAMPLES` usable ones)."""
    n = len(pairs)
    days = [int(s[1]) for s in pairs]
    out: list[Any] = [n, len(set(days))] + [None] * (len(COLUMNS) - 2)
    if n >= MIN_SAMPLES:
        diff = [float(s[3] - s[2]) for s in pairs]
        out[2:5] = [round(x, 4) + 0.0 for x in _q3(np.asarray(diff))]
        out[5:7] = loo_band_coverage(days, diff)
    usable = [s for s in pairs if s[2] >= RATIO_MIN_FORECAST]
    out[7] = len(usable)
    if len(usable) >= MIN_SAMPLES:
        ratio = [float(s[3] / s[2]) for s in usable]
        out[8:11] = [round(x, 3) + 0.0 for x in _q3(np.asarray(ratio))]
        out[11:13] = loo_band_coverage([int(s[1]) for s in usable], ratio)
    out[13] = int(
        n > 0 and len(usable) >= MIN_SAMPLES and len(usable) >= RATIO_OK_SHARE * n
    )
    return out


def spike_stats(
    spikes: Sequence[Sample], surprises: Sequence[Sample], threshold: float
) -> list[Any]:
    """`SPIKE_COLUMNS`: hits and misses among forecast spikes, surprises, and
    the Beta(1, 1) posterior mean of a forecast spike settling as one."""
    hits = sum(1 for s in spikes if s[3] >= threshold)
    misses = len(spikes) - hits
    return [hits, misses, len(surprises), round((hits + 1) / (hits + misses + 2), 4)]


def summarise(hist: PriceErrorHistory) -> dict[str, Any]:
    """The full table: per (side, source, bucket) with any pairs, each
    regime's `cell_stats()` row and, with a threshold, `spike_stats()`."""
    out: dict[str, Any] = {}
    for side in SIDES:
        by_source: dict[str, Any] = {}
        for source in sorted(hist.samples[side]):
            by_bucket: dict[str, Any] = {}
            for bucket, *_ in LEAD_BUCKETS:
                classes = hist.samples[side][source].get(bucket)
                if not classes:
                    continue
                spikes = classes.get(CLS_SPIKE, [])
                surprises = classes.get(CLS_SURPRISE, [])
                below = [
                    s
                    for s in (*classes.get(CLS_NORMAL, []), *surprises)
                    if on_stride(int(s[0]))
                ]
                entry: dict[str, Any] = {}
                if below:
                    entry[REGIME_BELOW] = cell_stats(below)
                if spikes:
                    entry[REGIME_SPIKE] = cell_stats(spikes)
                if hist.threshold is not None:
                    entry["spike"] = spike_stats(spikes, surprises, hist.threshold)
                if entry:
                    by_bucket[bucket] = entry
            if by_bucket:
                by_source[source] = by_bucket
        out[side] = by_source
    return out


def compact(full: dict[str, Any]) -> dict[str, Any]:
    """The published attribute: per (side, source, bucket), `COMPACT_COLUMNS`.
    Small enough for the battery-forecast sensor; the rest stays in the file."""
    out: dict[str, Any] = {}
    for side in SIDES:
        by_source: dict[str, Any] = {}
        for source, buckets in full.get(side, {}).items():
            rows: dict[str, Any] = {}
            for bucket, entry in buckets.items():
                below = entry.get(REGIME_BELOW) or [0] + [None] * (len(COLUMNS) - 1)
                spike = entry.get("spike") or [None] * len(SPIKE_COLUMNS)
                rows[bucket] = [below[0], *below[2:5], *spike]
            by_source[source] = rows
        out[side] = by_source
    return out


def _summary(hist: PriceErrorHistory) -> dict[str, Any]:
    if hist.summary is None:
        hist.summary = summarise(hist)
    return hist.summary


# -- the solve-cycle entry point --------------------------------------------


def _load(sw: Any) -> PriceErrorHistory:
    cached = getattr(sw, "_PRICE_ERROR_HISTORY_CACHE", None)
    if isinstance(cached, PriceErrorHistory):
        return cached
    data: Any = None
    try:
        with open(sw.PRICE_ERROR_HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        data = None
    hist = PriceErrorHistory.from_dict(data)
    sw._PRICE_ERROR_HISTORY_CACHE = hist
    return hist


def _save(sw: Any, hist: PriceErrorHistory) -> None:
    """The samples, plus the full `summarise()` table for anyone reading the
    file (`from_dict` ignores it)."""
    payload = hist.to_dict()
    payload["summary"] = {"columns": list(COLUMNS), **_summary(hist)}
    try:
        with open(sw.PRICE_ERROR_HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
    except OSError:
        pass


def update(
    sw: Any,
    cfg: dict,
    now: datetime,
    grid_times: Sequence[datetime],
    period_hours: Sequence[float],
    n_settled: int,
    series_by_side: dict[str, dict[str, list[float | None]]],
    settled_by_side: dict[str, float | None],
) -> dict[str, Any]:
    """Grade what settled, record this solve's forecasts, and return the
    published summary. Fed from `apply_price_consolidation()`, with the same
    series and settled prices the #1786 tracker scores. Never raises."""
    try:
        hist = _load(sw)
        threshold, threshold_from = spike_threshold(cfg)
        changed = hist.set_threshold(threshold)
        added = 0
        for side, series in series_by_side.items():
            added += hist.observe_settled(side, now, settled_by_side.get(side))
            for source, values in series.items():
                hist.record_series(
                    side, source, now, grid_times, period_hours, values, n_settled
                )
        if added or changed:
            hist.prune(now)
        last = getattr(sw, "_PRICE_ERROR_HISTORY_SAVED_AT", None)
        if (added or changed) and (last is None or now - last >= SAVE_EVERY):
            _save(sw, hist)
            sw._PRICE_ERROR_HISTORY_SAVED_AT = now
        return {
            "read_by_dispatch": False,
            "threshold": threshold,
            "threshold_from": threshold_from,
            "keep_days": KEEP_DAYS,
            "normal_stride": STRIDE,
            "min_samples": MIN_SAMPLES,
            "samples": hist.sample_count(),
            "pending_intervals": hist.pending_count(),
            "columns": list(COMPACT_COLUMNS),
            **compact(_summary(hist)),
        }
    except Exception as e:  # noqa: BLE001 -- a recording must never break the solve
        solver_shared._LOGGER.warning("Nimbus #1797: price error history failed: %s", e)
        return {"read_by_dispatch": False, "error": str(e)}
