"""Independent reference oracles for signal semantics (Spec DC-000 F04, F09).

Like `oracles.py`, nothing here imports Nimbus production code. These state
what a correct reading of a power, energy or price signal is, so the
production resolvers (DC-002, DC-005, DC-006) can later be checked against
them rather than against their own output.

Intervals are half-open `[start, end)` and every instant is timezone-aware.
"Missing" is `None` and is never the same as a measured zero.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

# Power units to kW. Case-sensitive on purpose: "mW" (milliwatt) and "MW"
# (megawatt) differ by 10^9, so a lookup that folds them together is a real
# defect.
# The set is Home Assistant's UnitOfPower, so an HA power sensor always has
# one of these exact spellings.
POWER_TO_KW = {
    "mW": 1e-6,
    "W": 1e-3,
    "kW": 1.0,
    "MW": 1e3,
    "GW": 1e6,
    "TW": 1e9,
    "BTU/h": 0.00029307107,
}
ENERGY_UNITS = {"mWh", "Wh", "kWh", "MWh", "GWh"}


class UnitError(ValueError):
    """A unit that is unknown, or of the wrong dimension for the quantity."""


def power_kw(value: float | None, unit: str) -> float | None:
    """A power reading in kW. Energy units are refused as power, unknown units
    are refused, and a missing or non-finite value stays missing."""
    if unit in ENERGY_UNITS:
        raise UnitError(f"{unit!r} is energy, not power")
    if unit not in POWER_TO_KW:
        raise UnitError(f"unknown power unit {unit!r}")
    if value is None or not math.isfinite(value):
        return None
    return value * POWER_TO_KW[unit]


def reading(state: str) -> float | None:
    """A recorded HA state as a number. `unavailable`/`unknown`/empty and
    non-finite values are missing; a recorded "0" is a measured zero."""
    if state in ("unavailable", "unknown", ""):
        return None
    try:
        value = float(state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class Row:
    """One recorder row: HA stores a sensor's state only when it changes."""

    at: datetime
    state: str


def value_at(rows: Sequence[Row], instant: datetime) -> float | None:
    """The sensor's value at `instant`: the latest row at or before it. A held
    value is still a measurement (the recorder writes on change only); only a
    missing row or a non-numeric state is missing."""
    _aware(instant)
    current: float | None = None
    for row in rows:
        _aware(row.at)
        if row.at > instant:
            break
        current = reading(row.state)
    return current


def counter_delta(rows: Sequence[Row], start: datetime, end: datetime) -> float | None:
    """Energy accumulated by a `total_increasing` counter over `[start, end)`.

    The baseline is the value in force at `start`. A reading exactly at `end`
    belongs to this interval, so the next interval's baseline is that same
    reading and each change is counted once. A drop is a meter reset: the
    counter restarted from zero, so the post-reset reading is new energy, not
    a negative delta. Missing at either edge, or anywhere in between, is
    missing for the whole interval.
    """
    _interval(start, end)
    base = value_at(rows, start)
    if base is None:
        return None
    total = 0.0
    last = base
    for row in rows:
        if row.at <= start or row.at > end:
            continue
        value = reading(row.state)
        if value is None:
            return None
        total += value - last if value >= last else value
        last = value
    return total


@dataclass(frozen=True)
class PriceInterval:
    """A published price over `[start, end)`, in $/kWh, sign as published."""

    start: datetime
    end: datetime
    price: float


def price_over(
    intervals: Iterable[PriceInterval], start: datetime, end: datetime
) -> tuple[float | None, float]:
    """Time-weighted price over `[start, end)` and the fraction of it covered.

    Only published intervals contribute: a gap is not filled, a 30-minute
    price is not invented from a neighbour, and a negative price is kept as
    it is. Overlapping published intervals are refused rather than averaged.
    Returns `(None, 0.0)` when nothing covers the window.
    """
    _interval(start, end)
    window = (end - start).total_seconds()
    spans: list[tuple[datetime, datetime]] = []
    weighted = 0.0
    covered = 0.0
    for iv in intervals:
        _interval(iv.start, iv.end)
        lo, hi = max(iv.start, start), min(iv.end, end)
        if lo >= hi:
            continue
        for a, b in spans:
            if lo < b and a < hi:
                raise ValueError("overlapping published price intervals")
        spans.append((lo, hi))
        seconds = (hi - lo).total_seconds()
        weighted += iv.price * seconds
        covered += seconds
    if covered == 0:
        return None, 0.0
    return weighted / covered, covered / window


def price_with_fees(
    published: float | None, fees: float, fees_included: bool
) -> float | None:
    """The price a household pays: network/retail fees added exactly once.
    A feed that already includes them is not charged them again, and a
    missing direction stays missing rather than becoming fees-only."""
    if published is None:
        return None
    return published if fees_included else published + fees


def declared_rating_kw(
    declared: float | None, history_kw: Iterable[float]
) -> float | None:
    """A device's rating is what is declared. The historical peak is evidence
    of use, not of capability, so it never stands in for a missing rating."""
    del history_kw
    return declared


def _aware(instant: datetime) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"naive datetime {instant!r}")


def _interval(start: datetime, end: datetime) -> None:
    _aware(start)
    _aware(end)
    if not start < end:
        raise ValueError("empty or reversed interval")
