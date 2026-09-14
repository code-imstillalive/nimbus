"""Cross-check AEMO's own 30-minute forecast against AEMO's own realised
5-minute prices for the same window.

nimbus issue #452 (Mark Purcell). The current-interval half shipped
already: `check_aemo_p5min_disagreement()` in `solver_writer.py` compares
the retail commodity price against the live wholesale price, correcting
for the household's own typical time-of-day markup. This is the other
half, and it asks a genuinely different question.

Mark's own instruction, after confirming what his install actually
publishes: *"If the 5 minute actual interval price and 30 minute
forecasts are all you have, then you should work with that. Compare the
30 minute forecast with the relevant 6x 5 minute intervals for a like
with like comparison."*

**Wholesale against wholesale, so there is no offset term at all.** Both
sides are AEMO's own numbers in $/kWh for the same half-hour, which makes
this a direct data-quality signal (is AEMO's forecast tracking AEMO's own
outcome?) rather than the retail-markup comparison the shipped check
performs. Neither subsumes the other.

**Why the CURRENT window, in progress, rather than a completed one.**
Scoring a finished half-hour against the forecast made before it is the
more natural framing, and it is not implementable today: the 30-minute
forecast lives in an entity ATTRIBUTE, and this project's own history
fetch passes `no_attributes=True` to the recorder, so there is no stored
record of what the forecast said for a window that has since elapsed.
Capturing that is a real, separate piece of work that overlaps #495/#496's
own recording. Flagged on the issue rather than silently building the
easier thing and calling it done.

Two honest properties of the in-progress shape, stated here so they are
not a surprise to a reader later:

- It is **weak in the first five minutes** of each window, where exactly
  one realised sample exists, and strongest by minute 25. `n_samples` is
  returned rather than hidden so a caller can weight or suppress on it;
  this module deliberately does not decide that for them.
- It validates a forecast against outcomes, which is a different claim
  from "the price is wrong". A persistent disagreement here means AEMO's
  own predispatch is running away from its own dispatch, which is worth
  surfacing but is not a Nimbus fault.

Pure functions over already-fetched data, with no `homeassistant` import
anywhere -- same shape as `nem_region.py` (#495), and for the same
reason: the parsing and the arithmetic are the part worth testing
directly, and the entity reads belong to the caller.
"""

from __future__ import annotations

from datetime import datetime

# A forecast entry from `sensor.aemo_nem_<region>_current_30min_forecast`'s
# own `forecast` attribute, confirmed live by Mark Purcell on his own
# install:
#
#     - start_time: '2026-09-13T12:00:00+10:00'
#       end_time:   '2026-09-13T12:30:00+10:00'
#       price:      -0.0085
#
# Times arrive as ISO strings with a real offset; prices as floats in
# $/kWh (the entity's own `unit_of_measurement`).
_START = "start_time"
_END = "end_time"
_PRICE = "price"


def _parse_time(value: object) -> datetime | None:
    """A timezone-aware datetime from one forecast entry's own time field.

    Returns None for anything unparseable or naive rather than guessing a
    timezone. A naive timestamp compared against an aware `now` raises at
    runtime, and silently assuming UTC (or local) would produce a
    confidently wrong bucket half the time -- the same "refuse rather
    than guess" posture `nem_region.postcode_prefix()` takes.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parse_price(value: object) -> float | None:
    """A real numeric price, or None. Explicitly rejects bools, which are
    `int` subclasses in Python and would otherwise sail through as 0.0/1.0
    -- a real hazard when reading attribute dicts of unknown provenance."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def select_current_bucket(
    entries: list[dict] | None, now: datetime
) -> tuple[datetime, datetime, float] | None:
    """The `(start, end, price)` of the forecast bucket containing `now`,
    or None when there isn't exactly one usable candidate.

    **Found by containment, not by taking `forecast[0]`.** The real
    entity does appear to put the current bucket first, and relying on
    that would work right up until it didn't -- a coordinator refresh
    landing mid-window, or a feed that keeps one elapsed bucket, would
    silently compare the wrong half-hour against the right actuals and
    report a disagreement that is purely an alignment bug. Containment
    cannot make that mistake.

    A half-open `[start, end)` test, so the instant a window closes it
    belongs to the next bucket and never to both.
    """
    if not entries:
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        start = _parse_time(entry.get(_START))
        end = _parse_time(entry.get(_END))
        price = _parse_price(entry.get(_PRICE))
        if start is None or end is None or price is None:
            continue
        if end <= start:
            # A malformed or zero-width window can never contain `now`
            # and would make the half-open test meaningless.
            continue
        if start <= now < end:
            return start, end, price
    return None


def mean_realised_price(
    samples: list[tuple[datetime, float]] | None,
    start: datetime,
    end: datetime,
) -> tuple[float | None, int]:
    """`(mean, n)` over the realised 5-minute samples inside
    `[start, end)`, or `(None, 0)` when none fall in the window.

    Same half-open convention as `select_current_bucket()` so a sample
    landing exactly on a boundary is counted once, by the window it
    starts, and never by both.

    Returns the count alongside the mean deliberately. One sample and six
    samples produce equally confident-looking means, and the difference
    between them is the whole reason this check is weak early in a window
    -- a caller that cannot see `n` cannot weight it.
    """
    if not samples:
        return None, 0
    inside = [
        value
        for when, value in samples
        if isinstance(when, datetime)
        and when.tzinfo is not None
        and start <= when < end
    ]
    if not inside:
        return None, 0
    return sum(inside) / len(inside), len(inside)


def compare_forecast_to_actuals(
    entries: list[dict] | None,
    samples: list[tuple[datetime, float]] | None,
    now: datetime,
    threshold_dollars: float,
    *,
    min_samples: int = 1,
) -> dict | None:
    """The whole check: AEMO's own forecast for the half-hour containing
    `now`, against the mean of AEMO's own realised 5-minute prices so far
    within that same half-hour.

    Returns None -- no flag, nothing to compare -- when the forecast
    carries no bucket containing `now`, when no realised samples have
    landed in it yet, or when fewer than `min_samples` have. Never
    raises: this runs inside a real solve cycle, and a diagnostic must
    never be the reason dispatch fails (the same posture every other
    external read in this project takes).

    `min_samples` defaults to 1 rather than to something safer on
    purpose. One sample is a genuinely weak comparison, but it is a real
    one, and the honest place to decide whether it is good enough is the
    caller -- who can see `n_samples` in the result. Defaulting it higher
    here would quietly discard the early-window signal for everyone.
    """
    bucket = select_current_bucket(entries, now)
    if bucket is None:
        return None
    start, end, forecast_price = bucket

    # Only the part of the window that has actually elapsed -- comparing
    # a whole-window forecast against samples up to `now` is the point,
    # so the upper bound is `now`, not `end`.
    realised, n_samples = mean_realised_price(samples, start, min(now, end))
    if realised is None or n_samples < min_samples:
        return None

    disagreement = realised - forecast_price
    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "forecast_price": round(forecast_price, 4),
        "realised_mean_price": round(realised, 4),
        "n_samples": n_samples,
        "disagreement_dollars": round(disagreement, 4),
        "threshold_dollars": round(threshold_dollars, 4),
        "flagged": abs(disagreement) > threshold_dollars,
    }
