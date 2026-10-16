"""Calibrated solar p10 / p90 for the solve (nimbus issue #1807).

## Why

`SolarConfig.lower_kw` / `upper_kw` are an ENVELOPE: the min and max across
the blended sources, widened by half their spread (`solar.py`). On the
reference household on 16 Oct 2026 the lower bound was 0 in every period and
the upper bound 6-17 kW at midnight. The two-stage stochastic dispatch
(#1805) needs a p10 / p90 -- values the actual falls below / above about 10%
of the time each -- not an envelope, or its p10 scenario is "no sun all day".

The envelope is left exactly as it was for its existing consumers (the solar
risk band, `network._risk_adjusted`). This module adds a second, separately
named band, `SolarConfig.p10_kw` / `p90_kw`.

## Which method, and why: measured, not chosen

Measured on the reference household before this was built (read-only: the
production recorder's PV sensor, devhub's recorded Solcast `detailedForecast`
and devhub's `export_decision_inputs` horizons, 10-16 Oct 2026, 42,918 blended
daylight forecasts). Share of actual 30-minute PV below p10 / above p90, both
should be ~10%:

==========================================================  ========  ========
method                                                      below     above
==========================================================  ========  ========
Solcast's own p10/p90 against Solcast's own p50               9.2%     29.6%
blend p50 x Solcast's p10/p50 and p90/p50 ratios             17.4%     22.7%
Solcast's absolute p10/p90, widened to hold the blend        14.0%     16.6%
empirical actual/p50 quantiles by lead                       11.3%     11.8%
  ... of which periods with p50 >= 2 kW                       3.8%      7.4%
  ... of which periods with p50 < 2 kW                       26.9%     21.0%
empirical by lead AND p50 size (< 2 kW, >= 2 kW): adopted    11.5%     12.7%
  ... of which periods with p50 >= 2 kW                      11.7%     13.4%
  ... of which periods with p50 < 2 kW                       10.9%     11.5%
==========================================================  ========  ========

(Empirical rows are leave-one-day-out: each day graded against quantiles
fitted on the other six.) The lead-only version looked calibrated overall but
was not, conditionally: dawn and dusk (small p50, ratio anywhere from 0 to
several) dragged its q10 down to ~0.06, so a midday p10 sat near zero -- the
envelope's own fault again. Splitting by p50 size fixes that and narrows the
mean band from 4.65 to 3.88 kW. Splitting by hour of day instead made it
worse (14.2% / 14.0% by 3-hour block, 17.3% / 16.0% by hour): a week does not
hold enough days per cell. Solcast's absolute band (third row) is the
fallback while the history is still too short; with neither, both fields are
None and nothing changes.

`SIZE_SPLIT_KW` is an absolute 2 kW, measured on a ~40 kW reference
household. On a much smaller system most daylight periods fall in the lower
class, which degrades to the lead-only method rather than breaking.

## The history

Same "record a prediction now, grade it once reality catches up" shape as the
#128 solar delivery ratio and #1786's price error tracker, and the same
bounded-memory trick as the latter: for each future 30-minute interval, one
forecast is kept per lead bucket -- the one issued closest to the bucket's
representative lead. When an interval has ended, every kept forecast for it is
graded against the time-weighted mean of `solver_solar_power_sensor` over that
interval (one recorder read per solve, covering every interval that ended),
and filed under its lead bucket and the size class of the p50 it was issued
with. Samples are kept `KEEP_DAYS` days.

The forecast recorded is the published `solar_kw` (after the #1784 running-low
gate), period 0 excluded because the live anchor owns it. A ratio is kept only
where p50 is at least `NIGHT_KW`.

## Invariants

- p10 <= p50 <= p90 in every period.
- Period 0 (the live measurement) and every period with p50 < `NIGHT_KW`:
  p10 = p90 = p50. The issue's wording was "p10 = p90 = 0"; the p50 itself is
  used because a literal 0 above a p50 of, say, 0.03 kW would break p10 <= p50
  <= p90. Either way the night carries no spread.

Never raises: any failure publishes `method: None` with a reason.

Prior art (CHECK PRIOR ART directive): EMHASS's `pv_bias_calibration.py`
tunes a P10/P50 weight once per day, with no quantile band for the optimiser.
HAEO has none. Nimbus's own prior art is #1786's `SourceErrorTracker`, whose
pending/bucket layout this copies.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import numpy as np

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]

INTERVAL_SECONDS = 1800

#: (name, lower hours inclusive, upper hours exclusive, representative lead).
#: The buckets the calibration measurement used.
LEAD_BUCKETS: tuple[tuple[str, float, float, float], ...] = (
    ("0-1 h", 0.0, 1.0, 0.5),
    ("1-4 h", 1.0, 4.0, 2.5),
    ("4-12 h", 4.0, 12.0, 8.0),
    ("12-24 h", 12.0, 24.0, 18.0),
    ("24-48 h", 24.0, 48.0, 36.0),
    ("48-96 h", 48.0, 96.0, 72.0),
)
MAX_LEAD_HOURS = 96.0
#: p50 size classes: the ratio's spread differs sharply below and above this.
SIZE_SPLIT_KW = 2.0
SIZE_LOW = "p50 < 2 kW"
SIZE_HIGH = "p50 >= 2 kW"
#: Below this p50 a period is night: no spread, and no ratio recorded.
NIGHT_KW = 0.05
#: Settled samples a bucket needs (and distinct days) before its own
#: quantiles are used; below that the pooled quantiles stand in.
MIN_SAMPLES = 50
MIN_DAYS = 3
KEEP_DAYS = 21
MAX_SAMPLES_PER_BUCKET = 1500
#: A ratio above this is a unit mix-up, not a plant (same bound as #128).
MAX_PLAUSIBLE_RATIO = 20.0
#: Overdue intervals older than this are dropped ungraded.
MAX_SETTLE_AGE = timedelta(days=2)

METHOD_EMPIRICAL = "empirical"
METHOD_SOLCAST = "solcast"


def lead_bucket(lead_hours: float) -> str | None:
    """The bucket a lead falls in; beyond the last, the last; None if < 0."""
    if lead_hours < 0:
        return None
    for name, lo, hi, _rep in LEAD_BUCKETS:
        if lo <= lead_hours < hi:
            return name
    return LEAD_BUCKETS[-1][0]


def size_class(p50_kw: float) -> str:
    return SIZE_HIGH if p50_kw >= SIZE_SPLIT_KW else SIZE_LOW


def cell_key(bucket: str, size: str) -> str:
    """The samples key for one (lead bucket, p50 size) cell."""
    return f"{bucket} | {size}"


CELLS: tuple[tuple[str, str], ...] = tuple(
    (b[0], size) for b in LEAD_BUCKETS for size in (SIZE_LOW, SIZE_HIGH)
)


def _rep_lead(bucket: str) -> float:
    return next(b[3] for b in LEAD_BUCKETS if b[0] == bucket)


def interval_means(
    grid_times: list[datetime],
    period_hours: list[float],
    values: list[float],
    *,
    first_index: int = 1,
) -> dict[int, float]:
    """Duration-weighted mean of a per-period series over every 30-minute
    interval the periods from `first_index` on cover completely, keyed by
    `epoch // INTERVAL_SECONDS`."""
    acc: dict[int, list[float]] = {}
    for i in range(first_index, min(len(grid_times), len(values), len(period_hours))):
        t0 = grid_times[i].timestamp()
        t1 = t0 + float(period_hours[i]) * 3600.0
        k = int(t0 // INTERVAL_SECONDS)
        while k * INTERVAL_SECONDS < t1:
            a = max(t0, k * INTERVAL_SECONDS)
            b = min(t1, (k + 1) * INTERVAL_SECONDS)
            if b > a:
                slot = acc.setdefault(k, [0.0, 0.0])
                slot[0] += float(values[i]) * (b - a)
                slot[1] += b - a
            k += 1
    return {k: s / d for k, (s, d) in acc.items() if d >= INTERVAL_SECONDS * 0.99}


def _quantiles(ratios: list[float]) -> tuple[float, float]:
    q10, q90 = np.quantile(np.asarray(ratios, dtype=float), [0.1, 0.9])
    return min(float(q10), 1.0), max(float(q90), 1.0)


class SolarErrorHistory:
    """Per-lead, per-size record of `actual / p50` for the blended solar
    forecast.

    `pending[interval][bucket] = [lead_hours, p50_kw]`; `samples[cell_key]` is
    a list of `[local_day, ratio]`, the cell being the lead bucket and the
    p50's size class.
    """

    def __init__(self) -> None:
        self.pending: dict[int, dict[str, list[float]]] = {}
        self.samples: dict[str, list[list[Any]]] = {}

    # -- recording ------------------------------------------------------

    def record(
        self,
        now: datetime,
        grid_times: list[datetime],
        period_hours: list[float],
        solar_kw: list[float],
    ) -> None:
        """Keep, per future interval and bucket, the forecast closest to the
        bucket's representative lead. Period 0 (the live anchor) is skipped."""
        now_ts = now.timestamp()
        for k, p50 in interval_means(grid_times, period_hours, solar_kw).items():
            if p50 < NIGHT_KW:
                continue
            lead = (k * INTERVAL_SECONDS - now_ts) / 3600.0
            if lead < 0 or lead > MAX_LEAD_HOURS:
                continue
            bucket = lead_bucket(lead)
            if bucket is None:
                continue
            slot = self.pending.setdefault(k, {}).get(bucket)
            rep = _rep_lead(bucket)
            if slot is None or abs(lead - rep) <= abs(slot[0] - rep):
                self.pending[k][bucket] = [round(lead, 4), round(float(p50), 4)]

    # -- settling -------------------------------------------------------

    def due(self, now: datetime) -> list[int]:
        """Pending intervals that have ended, oldest first. Ones older than
        MAX_SETTLE_AGE are dropped instead (nothing to grade them with)."""
        now_ts = now.timestamp()
        oldest = now_ts - MAX_SETTLE_AGE.total_seconds()
        for k in [k for k in self.pending if (k + 1) * INTERVAL_SECONDS < oldest]:
            del self.pending[k]
        return sorted(k for k in self.pending if (k + 1) * INTERVAL_SECONDS <= now_ts)

    def settle(self, actual_by_interval: dict[int, float | None], tz: Any) -> int:
        """Grade every pending forecast whose interval has an actual. An
        interval with no actual (None) is dropped ungraded. Returns how many
        samples were added."""
        added = 0
        for k, actual in actual_by_interval.items():
            buckets = self.pending.pop(k, None)
            if not buckets or actual is None:
                continue
            day = datetime.fromtimestamp(k * INTERVAL_SECONDS, tz).date().isoformat()
            for bucket, (_lead, p50) in buckets.items():
                if p50 < NIGHT_KW:
                    continue
                ratio = max(0.0, float(actual)) / p50
                if ratio > MAX_PLAUSIBLE_RATIO:
                    continue
                key = cell_key(bucket, size_class(p50))
                self.samples.setdefault(key, []).append([day, round(ratio, 4)])
                added += 1
        return added

    def prune(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
        for bucket in list(self.samples):
            kept = [s for s in self.samples[bucket] if s[0] >= cutoff]
            self.samples[bucket] = kept[-MAX_SAMPLES_PER_BUCKET:]

    # -- quantiles ------------------------------------------------------

    def _all(self) -> list[list[Any]]:
        return [s for b in self.samples.values() for s in b]

    def quantile_table(self) -> dict[tuple[str, str], tuple[float, float]] | None:
        """{(bucket, size): (q10, q90)} for EVERY cell: its own where it has
        enough samples, else its size class pooled over all leads, else
        everything pooled -- or None while even the pooled history is too
        short."""
        pooled = self._all()
        if not _enough(pooled):
            return None
        pooled_q = _quantiles([s[1] for s in pooled])
        by_size: dict[str, tuple[float, float]] = {}
        for size in (SIZE_LOW, SIZE_HIGH):
            xs = [
                s
                for b, *_ in LEAD_BUCKETS
                for s in self.samples.get(cell_key(b, size), [])
            ]
            by_size[size] = _quantiles([s[1] for s in xs]) if _enough(xs) else pooled_q
        table: dict[tuple[str, str], tuple[float, float]] = {}
        for bucket, size in CELLS:
            own = self.samples.get(cell_key(bucket, size), [])
            table[(bucket, size)] = (
                _quantiles([s[1] for s in own]) if _enough(own) else by_size[size]
            )
        return table

    def calibration(self) -> dict[str, Any]:
        """Per cell: samples, days, the cell's own quantiles and their coverage
        measured leave-one-day-out (each day graded against quantiles fitted
        on the other days) -- the number that should read ~10% each side."""
        out: dict[str, Any] = {}
        for bucket, size in CELLS:
            own = self.samples.get(cell_key(bucket, size), [])
            entry: dict[str, Any] = {
                "samples": len(own),
                "days": len({s[0] for s in own}),
            }
            if _enough(own):
                q10, q90 = _quantiles([s[1] for s in own])
                below, above = loo_coverage(own)
                entry.update(
                    q10_ratio=round(q10, 4),
                    q90_ratio=round(q90, 4),
                    below_p10_pct=below,
                    above_p90_pct=above,
                )
            out[cell_key(bucket, size)] = entry
        return out

    # -- persistence ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending": {str(k): dict(v) for k, v in self.pending.items()},
            "samples": {b: list(v) for b, v in self.samples.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> SolarErrorHistory:
        """Tolerant: anything malformed comes back empty."""
        h = cls()
        if not isinstance(data, dict):
            return h
        try:
            for k, v in (data.get("pending") or {}).items():
                h.pending[int(k)] = {
                    str(b): [float(x[0]), float(x[1])] for b, x in v.items()
                }
            for b, v in (data.get("samples") or {}).items():
                h.samples[str(b)] = [[str(s[0]), float(s[1])] for s in v]
        except (TypeError, ValueError, AttributeError, IndexError, KeyError):
            return cls()
        return h


def _enough(samples: list[list[Any]]) -> bool:
    return len(samples) >= MIN_SAMPLES and len({s[0] for s in samples}) >= MIN_DAYS


def loo_coverage(samples: list[list[Any]]) -> tuple[float | None, float | None]:
    """(% below p10, % above p90) over `samples`, each day graded against
    quantiles fitted on the remaining days. None when fewer than two days."""
    days = sorted({s[0] for s in samples})
    if len(days) < 2:
        return None, None
    n = below = above = 0
    for d in days:
        train = [s[1] for s in samples if s[0] != d]
        test = [s[1] for s in samples if s[0] == d]
        q10, q90 = _quantiles(train)
        below += sum(1 for r in test if r < q10 - 1e-9)
        above += sum(1 for r in test if r > q90 + 1e-9)
        n += len(test)
    return round(100.0 * below / n, 1), round(100.0 * above / n, 1)


def build_bands(
    *,
    now: datetime,
    grid_times: list[datetime],
    solar_kw: list[float],
    table: dict[tuple[str, str], tuple[float, float]] | None,
    solcast_band: tuple[list[float], list[float], list[float]] | None,
) -> tuple[list[float] | None, list[float] | None, str | None]:
    """(p10, p90, method). Empirical when `table` is available; otherwise
    Solcast's own p10/p90 widened to contain the blend p50 when the install
    has a Solcast band with real spread; otherwise (None, None, None)."""
    method: str | None
    if table is not None:
        method = METHOD_EMPIRICAL
    elif solcast_band is not None and _has_spread(solcast_band):
        method = METHOD_SOLCAST
    else:
        return None, None, None
    p10: list[float] = []
    p90: list[float] = []
    now_ts = now.timestamp()
    for i, p50 in enumerate(solar_kw):
        p50 = float(p50)
        if i == 0 or p50 < NIGHT_KW:
            p10.append(p50)
            p90.append(p50)
            continue
        if method == METHOD_EMPIRICAL:
            assert table is not None
            bucket = lead_bucket((grid_times[i].timestamp() - now_ts) / 3600.0)
            q10, q90 = (
                table[(bucket, size_class(p50))] if bucket is not None else (1.0, 1.0)
            )
            lo, hi = p50 * q10, p50 * q90
        else:
            assert solcast_band is not None
            _v, s_lo, s_hi = solcast_band
            lo = float(s_lo[i]) if i < len(s_lo) else p50
            hi = float(s_hi[i]) if i < len(s_hi) else p50
        # Rounded first, then clamped, so rounding can never cross the p50.
        p10.append(min(round(max(0.0, lo), 4), p50))
        p90.append(max(round(hi, 4), p50))
    return p10, p90, method


def _has_spread(band: tuple[list[float], list[float], list[float]]) -> bool:
    value, lower, upper = band
    return any(
        lower[i] < value[i] - 1e-9 or upper[i] > value[i] + 1e-9
        for i in range(min(len(value), len(lower), len(upper)))
    )


# -- the solve-cycle entry point --------------------------------------------


def _load(sw: Any) -> SolarErrorHistory:
    cached = getattr(sw, "_SOLAR_QUANTILES_CACHE", None)
    if isinstance(cached, SolarErrorHistory):
        return cached
    data: Any = None
    try:
        with open(sw.SOLAR_QUANTILES_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, TypeError):
        data = None
    hist = SolarErrorHistory.from_dict(data)
    sw._SOLAR_QUANTILES_CACHE = hist
    return hist


def _save(sw: Any, hist: SolarErrorHistory) -> None:
    try:
        with open(sw.SOLAR_QUANTILES_PATH, "w", encoding="utf-8") as f:
            json.dump(hist.to_dict(), f)
    except OSError:
        pass


def _actuals(sw: Any, sensor: str, keys: list[int], tz: Any) -> dict[int, float | None]:
    """Time-weighted mean kW of `sensor` over each interval in `keys`, from
    one recorder read spanning all of them."""
    if not keys:
        return {}
    starts = [datetime.fromtimestamp(k * INTERVAL_SECONDS, tz) for k in keys]
    end = starts[-1] + timedelta(seconds=INTERVAL_SECONDS)
    hist = sw.fetch_entity_history_range(sensor, starts[0], end)
    if not hist:
        return dict.fromkeys(keys)
    means = sw.resample_history_mean(hist, starts, INTERVAL_SECONDS / 3600.0)
    scale = float(sw._kw_scale_factor(sensor))
    return {k: float(m) * scale for k, m in zip(keys, means, strict=True)}


def update_and_build(
    sw: Any,
    cfg: dict,
    now: datetime,
    grid_times: list[datetime],
    period_hours: list[float],
    solar_kw: list[float],
    solcast_band: tuple[list[float], list[float], list[float]] | None,
) -> dict[str, Any]:
    """Grade what has settled, record this solve's forecast, and return
    `{"p10": [...] | None, "p90": [...] | None, "calibration": {...}}`.
    Never raises."""
    try:
        hist = _load(sw)
        sensor = cfg.get("solver_solar_power_sensor")
        added = 0
        if sensor:
            due = hist.due(now)
            if due:
                added = hist.settle(_actuals(sw, sensor, due, now.tzinfo), now.tzinfo)
            hist.prune(now)
            hist.record(now, grid_times, list(period_hours), list(solar_kw))
            # Saved when something was graded, and on the first solve after a
            # restart -- the pending forecasts are rebuilt every solve anyway.
            if added or not getattr(sw, "_SOLAR_QUANTILES_SAVED", False):
                _save(sw, hist)
                sw._SOLAR_QUANTILES_SAVED = True
        table = hist.quantile_table()
        p10, p90, method = build_bands(
            now=now,
            grid_times=grid_times,
            solar_kw=list(solar_kw),
            table=table,
            solcast_band=solcast_band,
        )
        if method is None:
            reason = (
                "no_solar_power_sensor"
                if not sensor
                else "history_too_short_no_solcast"
            )
        else:
            reason = "ok" if method == METHOD_EMPIRICAL else "history_too_short"
        return {
            "p10": p10,
            "p90": p90,
            "calibration": {
                "method": method,
                "reason": reason,
                "target_below_p10_pct": 10.0,
                "target_above_p90_pct": 10.0,
                "keep_days": KEEP_DAYS,
                "min_samples": MIN_SAMPLES,
                "min_days": MIN_DAYS,
                "pending_intervals": len(hist.pending),
                "buckets": hist.calibration(),
            },
        }
    except Exception as e:  # noqa: BLE001 -- a diagnostic band must never break the solve
        solver_shared._LOGGER.warning("Nimbus #1807: solar quantile band failed: %s", e)
        return {
            "p10": None,
            "p90": None,
            "calibration": {"method": None, "reason": f"error: {e}"},
        }
