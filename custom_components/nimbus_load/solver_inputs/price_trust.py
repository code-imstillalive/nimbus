"""Trust a forecast price more the closer it gets (nimbus #2534).

## Why

Past the configured price source's own horizon, the plan is priced by a
secondary source or the AEMO extension. A regional forecast a day out can
carry a spike at full height: on 13 Nov 2026 AEMO's predispatch put 98.6c
spot on the next evening, and the plan, the Prices ahead chart and the
next day's summary counted on selling at about $1/kWh. Predispatch rows
reach Nimbus uncalibrated (the regional sensor marks them `passthrough`),
so `fetch_aemo_forecast()`'s reliance on the calibrated field does not
protect the first day.

Household decision (13 Nov 2026): a forecast price counts for more the
closer it gets. A day-ahead spike counts for part of its height; as later
forecasts keep it, it counts for more, and fully once it is close.

## What it does

For each period NOT priced by the primary source (the primary's own rows
and the settled block are never touched):

    used = typical + w * (forecast - typical)

- `typical`: the primary source's own median price at that 5-minute of
  day over the history already fetched for the price bands.
- `w` in [0, 1]:
  - **Spike regime** (forecast at or above the existing spike threshold,
    `price_error_history.spike_threshold()`): the chance a forecast spike
    at that lead settles as one. Measured hits and misses from
    `price_error_history` at that lead bucket, shrunk towards a lead
    prior `HALF_TRUST_HOURS / (HALF_TRUST_HOURS + lead_hours)` with
    `PRIOR_STRENGTH` pseudo-observations. With no graded spikes yet the
    prior alone decides: about 0.92 at 30 minutes, 0.5 at 6 hours and
    0.18 at 27 hours.
  - **Below the threshold**: the usual inverse-variance weight,
    `s_typ^2 / (s_typ^2 + s_fc^2)`. `s_fc` comes from the measured
    q10-q90 spread of that source's error at that lead, and `s_typ` from
    the primary history's own q10-q90 spread at that time of day. A
    source without enough graded pairs is left as it is (`w = 1`), so an
    ordinary forecast only moves where the measurement says it misses.

Pure: no Home Assistant import, no I/O. The writer supplies the summary,
the typical prices and the spreads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

#: Lead (hours) at which a spike with no graded history counts for half.
HALF_TRUST_HOURS = 6.0
#: Pseudo-observations the lead prior is worth against measured hits and
#: misses (the same strength as the Beta(1, 1) behind `p_hit`).
PRIOR_STRENGTH = 2.0
#: q90 - q10 of a normal distribution, in standard deviations.
_Q10_Q90_SIGMAS = 2.563
#: The period labels this adjusts, and the scored sources that could have
#: priced such a period. Every other label is left alone: "primary" (the
#: configured source's own rows and the settled block) and "consolidated"
#: (#1786 live, which already weighs the sources by their measured error).
SOURCES_BY_LABEL: dict[str, tuple[str, ...]] = {
    "secondary": ("secondary_2", "secondary_3"),
    "fallback": ("aemo_markup", "retail_from_rrp"),
    # price_freshness.FALLBACK_LABEL: a stale primary, priced from AEMO (#1793).
    "aemo_fallback": ("aemo_markup", "retail_from_rrp"),
}

# price_error_history's summary layout (COLUMNS / SPIKE_COLUMNS).
_N, _D10, _D90 = 0, 2, 4
_HITS, _MISSES = 0, 1


def lead_prior(lead_hours: float) -> float:
    """The chance a forecast spike `lead_hours` ahead settles as one, before
    any have been graded: 1 at the period, half at `HALF_TRUST_HOURS`."""
    return HALF_TRUST_HOURS / (HALF_TRUST_HOURS + max(0.0, lead_hours))


def bucket_of(
    lead_minutes: float, buckets: Sequence[tuple[str, float, float, float]]
) -> str | None:
    for name, lo, hi, _rep in buckets:
        if lo <= lead_minutes < hi:
            return name
    return None


def spike_weight(lead_hours: float, cells: Sequence[Mapping[str, Any] | None]) -> float:
    """Measured hits and misses across `cells` (one per source that could
    have priced the period, at this lead bucket), shrunk towards the lead
    prior."""
    hits = misses = 0
    for cell in cells:
        spike = (cell or {}).get("spike")
        if spike:
            hits += int(spike[_HITS] or 0)
            misses += int(spike[_MISSES] or 0)
    prior = lead_prior(lead_hours)
    return (hits + PRIOR_STRENGTH * prior) / (hits + misses + PRIOR_STRENGTH)


def normal_weight(
    cells: Sequence[Mapping[str, Any] | None], typical_sigma: float | None
) -> float:
    """Inverse-variance weight of the forecast against the typical price,
    from the widest measured error spread among `cells`; 1 when nothing
    has been measured."""
    if not typical_sigma or typical_sigma <= 0:
        return 1.0
    sigmas = []
    for cell in cells:
        row = (cell or {}).get("below")
        if row and row[_D10] is not None and row[_D90] is not None:
            sigmas.append((float(row[_D90]) - float(row[_D10])) / _Q10_Q90_SIGMAS)
    if not sigmas:
        return 1.0
    s_fc = max(sigmas)
    return typical_sigma**2 / (typical_sigma**2 + s_fc**2)


def apply(
    prices: Sequence[float],
    labels: Sequence[str],
    grid_times: Sequence[datetime],
    now: datetime,
    *,
    typical: Mapping[int, float],
    spread: Mapping[int, float],
    side_summary: Mapping[str, Mapping[str, Mapping[str, Any]]],
    threshold: float | None,
    buckets: Sequence[tuple[str, float, float, float]],
    bucket_5min: Callable[[datetime], int],
) -> tuple[list[float], list[float | None]]:
    """`prices` with each non-primary period moved towards its typical price
    by its trust weight, and the weight used per period (None where the
    period was left alone)."""
    out = [float(p) for p in prices]
    weights: list[float | None] = [None] * len(out)
    for i, (price, label, t) in enumerate(zip(prices, labels, grid_times)):
        sources = SOURCES_BY_LABEL.get(label)
        if sources is None:
            continue
        slot = bucket_5min(t)
        typ = typical.get(slot)
        if typ is None:
            continue
        lead_min = (t - now).total_seconds() / 60.0
        bucket = bucket_of(lead_min, buckets) or buckets[-1][0]
        cells = [side_summary.get(s, {}).get(bucket) for s in sources]
        if threshold is not None and price >= threshold:
            w = spike_weight(lead_min / 60.0, cells)
        else:
            w = normal_weight(cells, spread.get(slot))
        w = min(1.0, max(0.0, w))
        if w < 1.0:
            out[i] = typ + w * (float(price) - typ)
        weights[i] = round(w, 4)
    return out, weights


def typical_and_spread(
    history: Sequence[tuple[datetime, float]],
    bucket_5min: Callable[[datetime], int],
) -> tuple[dict[int, float], dict[int, float]]:
    """The median price per 5-minute of day and its spread (q10-q90 as a
    standard deviation), from the primary source's own history."""
    by: dict[int, list[float]] = {}
    for t, v in history:
        by.setdefault(bucket_5min(t), []).append(float(v))
    typical: dict[int, float] = {}
    spread: dict[int, float] = {}
    for slot, vals in by.items():
        vals.sort()
        n = len(vals)
        typical[slot] = (
            vals[n // 2] if n % 2 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
        )
        q10 = vals[int(0.1 * (n - 1))]
        q90 = vals[round(0.9 * (n - 1))]
        spread[slot] = (q90 - q10) / _Q10_Q90_SIGMAS
    return typical, spread
