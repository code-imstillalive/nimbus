"""Independent reference oracles for DC-003's accounting decisions (Spec DC-000
F02, F05, F07, F08).

Like `oracles.py`, nothing here imports Nimbus production code; in particular
the reconciliation verdict below is stated from DC-003's decisions, not read
from `solver/meter_reconciliation.py`.

DC-003 (docs/specs/device-contract/003-accounting-and-shared-constraints.md)
ended with nine decisions. Mark asked for DC-000's gaps to be filled
(#1574, 16 Oct 2026), and the decisions are applied here **as proposed**:

1. reconciliation tolerance: 10 % of metered throughput, 2 kWh floor, and no
   verdict when more than 10 % of the window's periods are stale (#1605's
   figures);
2. meter sign: the binding's declared sign; both are tried only for a legacy
   binding with none;
3. negative residual: below -max(0.1 kW, 5 % of the node's own reading); the
   decomposition is refused for a window in which more than 10 % of intervals
   are negative. **These two figures are unmeasured proposals;**
4. sign-ambiguous inclusion: a two-way child inside a parent that cannot read
   below zero is unsupported unless declared one-directional;
5. undeclared overlap: at least a day of paired readings, the candidate child
   at or below the parent within the reconciliation tolerance, correlation
   0.9 or more. Evidence only;
7. an EV with no connection evidence: `mobility_unresolved`. Being at home is
   not evidence.

If a decision changes, the oracle and its manifests change with it, through a
reviewed PR.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

# -- Decision 1 and 2: the reconciliation verdict -----------------------------

TOLERANCE_FRACTION = 0.10
TOLERANCE_FLOOR_KWH = 2.0
STALE_LIMIT_FRACTION = 0.10

AGREES = "agrees"
DISAGREES = "disagrees"
INSUFFICIENT_COVERAGE = "insufficient_coverage"
NOT_CHECKED = "not_checked"
UNSUPPORTED = "unsupported"
STATUSES = (AGREES, DISAGREES, INSUFFICIENT_COVERAGE, NOT_CHECKED, UNSUPPORTED)

POSITIVE_IS_IMPORT = "positive_is_import"
POSITIVE_IS_EXPORT = "positive_is_export"


@dataclass(frozen=True)
class Meter:
    """A grid meter's energy over the window, split by the direction of its
    own reading, and the sign its binding declares (None: a legacy binding
    that never said)."""

    positive_kwh: float
    negative_kwh: float
    declared_sign: str | None


@dataclass(frozen=True)
class Verdict:
    status: str
    import_residual_kwh: float | None = None
    export_residual_kwh: float | None = None
    tolerance_kwh: float | None = None
    meter_sign: str | None = None
    reason: str | None = None


def meter_energy(kw: Sequence[float], hours: Sequence[float]) -> tuple[float, float]:
    """(positive-direction kWh, negative-direction kWh) of a mean-power series,
    each direction kept separately so a net can never hide two errors."""
    if len(kw) != len(hours):
        raise ValueError("power and duration series differ in length")
    pos = sum(k * h for k, h in zip(kw, hours) if k > 0)
    neg = sum(-k * h for k, h in zip(kw, hours) if k < 0)
    return pos, neg


def _split(meter: Meter, sign: str) -> tuple[float, float]:
    if sign == POSITIVE_IS_IMPORT:
        return meter.positive_kwh, meter.negative_kwh
    if sign == POSITIVE_IS_EXPORT:
        return meter.negative_kwh, meter.positive_kwh
    raise ValueError(f"unknown meter sign {sign!r}")


def reconcile(
    reconstructed_import_kwh: float,
    reconstructed_export_kwh: float,
    meter: Meter | None,
    stale_fraction: float | None,
    unsupported_reason: str | None = None,
) -> Verdict:
    """The daily verdict on reconstructed against metered site exchange.

    Order matters and is part of the contract: a reconstruction that could
    not be built is `unsupported`; no meter is `not_checked`; a meter with
    unknown or too-stale coverage is `insufficient_coverage`; only then is a
    tolerance applied, per direction."""
    if unsupported_reason is not None:
        return Verdict(UNSUPPORTED, reason=unsupported_reason)
    if meter is None:
        return Verdict(NOT_CHECKED, reason="no grid measurement bound")
    if stale_fraction is None or stale_fraction > STALE_LIMIT_FRACTION:
        return Verdict(INSUFFICIENT_COVERAGE, reason="meter coverage too sparse")
    signs = (
        [meter.declared_sign]
        if meter.declared_sign is not None
        else [POSITIVE_IS_IMPORT, POSITIVE_IS_EXPORT]
    )
    tolerance = max(
        TOLERANCE_FLOOR_KWH,
        TOLERANCE_FRACTION * (meter.positive_kwh + meter.negative_kwh),
    )
    fits: list[tuple[float, Verdict]] = []
    for sign in signs:
        imp, exp = _split(meter, sign)
        r_imp = reconstructed_import_kwh - imp
        r_exp = reconstructed_export_kwh - exp
        worst = max(abs(r_imp), abs(r_exp))
        status = AGREES if worst <= tolerance else DISAGREES
        fits.append((worst, Verdict(status, r_imp, r_exp, tolerance, sign)))
    # A legacy meter (no declared sign) gets the better of the two readings.
    # Flipping swaps the directions without changing their sum, so a
    # reconstruction wrong in total stays wrong under either.
    return min(fits, key=lambda f: f[0])[1]


# -- Decision 3: per-interval residuals and the negative-residual rule --------

NEGATIVE_FLOOR_KW = 0.1  # unmeasured proposal (DC-003 decision 3)
NEGATIVE_FRACTION = 0.05  # unmeasured proposal (DC-003 decision 3)
REFUSE_ABOVE_FRACTION = 0.10  # unmeasured proposal (DC-003 decision 3)


def residual_series(
    series: dict[str, Sequence[float]], inside: dict[str, str | None]
) -> dict[str, list[float]]:
    """Each node's own residual, interval by interval: its reading less its
    direct children's. Never clamped. Every node is treated as separated, so
    the residuals sum to the top-level total in each interval."""
    lengths = {len(v) for v in series.values()}
    if len(lengths) != 1:
        raise ValueError("series are not aligned")
    (n,) = lengths
    out: dict[str, list[float]] = {}
    for name, values in series.items():
        kids = [c for c, p in inside.items() if p == name]
        out[name] = [values[t] - sum(series[c][t] for c in kids) for t in range(n)]
    return out


def is_negative(residual_kw: float, own_reading_kw: float) -> bool:
    """A residual below -max(0.1 kW, 5 % of the node's own reading)."""
    return residual_kw < -max(
        NEGATIVE_FLOOR_KW, NEGATIVE_FRACTION * abs(own_reading_kw)
    )


@dataclass(frozen=True)
class Decomposition:
    negative: dict[str, list[int]]  # node -> interval indices
    negative_interval_fraction: float
    refused: bool


def decomposition_verdict(
    series: dict[str, Sequence[float]], inside: dict[str, str | None]
) -> Decomposition:
    """Whether a window's decomposition stands. An interval counts as
    negative if any node's residual is; the decomposition is refused when
    more than 10 % of the window's intervals are. Inclusive demand is not
    affected either way."""
    res = residual_series(series, inside)
    negative: dict[str, list[int]] = {}
    bad: set[int] = set()
    for name, values in res.items():
        idx = [t for t, r in enumerate(values) if is_negative(r, series[name][t])]
        if idx:
            negative[name] = idx
            bad.update(idx)
    n = len(next(iter(series.values())))
    fraction = len(bad) / n
    return Decomposition(negative, fraction, fraction > REFUSE_ABOVE_FRACTION)


# -- Decision 4: inclusion boundaries -----------------------------------------

ALL_PHASES = frozenset({"L1", "L2", "L3"})
MEASURES = {"load": "consumption", "solar": "generation"}
CONTRIBUTES = {
    "consumption": frozenset({"load", "battery"}),
    "generation": frozenset({"solar"}),
}


@dataclass(frozen=True)
class Party:
    """One side of an inclusion. `signed`: the bound reading can go below
    zero. `one_directional`: a battery declared to flow one way only (an EV
    that never discharges)."""

    name: str
    kind: str
    domain: str  # "ac" | "dc"
    phases: frozenset[str] = ALL_PHASES
    signed: bool = True
    one_directional: bool = False


def inclusion_problems(child: Party, parent: Party, quantity: str) -> list[str]:
    """Why a declared `included_in_measurement` cannot hold arithmetically,
    as DC-003 result codes. Empty when it can."""
    problems: list[str] = []
    if child.domain != parent.domain or not child.phases <= parent.phases:
        problems.append("inclusion_boundary_mismatch")
    if MEASURES.get(parent.kind) != quantity or child.kind not in CONTRIBUTES.get(
        quantity, frozenset()
    ):
        problems.append("inclusion_quantity_mismatch")
    two_way = child.kind == "battery" and not child.one_directional
    if two_way and not parent.signed:
        problems.append("inclusion_sign_ambiguous")
    return problems


# -- Decision 7: mobility ------------------------------------------------------

MOBILITY_UNRESOLVED = "mobility_unresolved"


@dataclass(frozen=True)
class VehicleInterval:
    """A vehicle's pack power (canonical, + discharge) over an interval, and
    what is known about its connection. `connected` None means no evidence.
    `at_home` is carried to show it is never used."""

    hours: float
    kw: float
    connected: bool | None
    at_home: bool | None = None


def vehicle_site_kwh(intervals: Sequence[VehicleInterval]) -> float | str:
    """Energy the vehicle drew from the site over the window, or
    `mobility_unresolved` if any interval lacks connection evidence. Only
    connected intervals cross the site boundary."""
    if any(i.connected is None for i in intervals):
        return MOBILITY_UNRESOLVED
    return sum((-i.kw * i.hours for i in intervals if i.connected), 0.0)


# -- Decision 5: undeclared overlap -------------------------------------------

MIN_WINDOW_HOURS = 24.0
MIN_CORRELATION = 0.9


def possible_undeclared_inclusion(
    child_kw: Sequence[float], parent_kw: Sequence[float], hours: Sequence[float]
) -> bool:
    """Evidence that `child` is measured inside `parent` though nobody said
    so: at least a day of paired readings; the child's excess over the parent
    totals no more than the reconciliation tolerance on the parent's energy;
    and the two correlate at 0.9 or more. A constant series carries no
    evidence. Never a subtraction, never a block."""
    if not len(child_kw) == len(parent_kw) == len(hours):
        raise ValueError("series are not aligned")
    if sum(hours) < MIN_WINDOW_HOURS:
        return False
    excess = sum(max(0.0, c - p) * h for c, p, h in zip(child_kw, parent_kw, hours))
    parent_kwh = sum(abs(p) * h for p, h in zip(parent_kw, hours))
    if excess > max(TOLERANCE_FLOOR_KWH, TOLERANCE_FRACTION * parent_kwh):
        return False
    try:
        r = statistics.correlation(list(child_kw), list(parent_kw))
    except statistics.StatisticsError:
        return False
    return not math.isnan(r) and r >= MIN_CORRELATION
