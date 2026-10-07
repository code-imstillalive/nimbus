"""Device resolver for the two-step setup (nimbus #1574, design
docs/design/two-step-setup.md §3; Mark's device contract on #1574).

**Evidence, not proof.** Everything this module returns is a *candidate* or a
*hypothesis* for the household to confirm, never a mapping applied on its own.
"Same Home Assistant device" is a candidate-selection hint; a match must also
have an unambiguous role and measurement boundary, compatible units, sign
convention and timestamps. Where the evidence cannot tell candidates, signs or
explanations apart, the result says so (`ambiguous`, `insufficient_evidence`)
instead of picking one -- entity order never decides (Mark's review of #1590).

What the Energy Dashboard already states comes first
-----------------------------------------------------
Home Assistant's Energy Dashboard preferences carry more than energy totals
(`energy/data.py`, read in HA 2026.3.0, 2026.7.4 and 2026.9.3):

- `stat_rate` on a grid, battery or solar source is a **power** statistic in
  HA's own sign convention -- battery positive = discharging, grid positive =
  from the grid, solar = production. When the household chose an inverted or
  two-sensor power config, `stat_rate` is HA's own normalised template sensor
  and `power_config` holds what they picked.
- `stat_soc` on a battery (HA 2026.6+) is its state of charge.
- a device's `included_in_stat` says which other device's total already
  contains it.

`parse_energy_sources` keeps all of it, with where each value came from. Only
when a source names no power sensor does `match_power_sensor` look for one, by
pairing each candidate's integrated history with the source's energy counter.

The grid, kept as Home Assistant keeps it
-----------------------------------------
Since HA 2026.3 each grid source is one import/export connection, and HA
migrated the older `flow_from` / `flow_to` lists into such connections by list
position. A unified source is therefore HA's own pairing, kept as one source
with its own index; a source HA built by migration cannot be told from one the
household made, so separate grid sources are never claimed to be separate
physical connections. An older (pre-2026.3) grid source keeps all its import
and export flows on one source, unpaired: list position is not evidence that
two flows belong to one connection.

Pure (no Home Assistant), so real installs' history replays through it.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

# A candidate is accepted only when its error is within MATCH_TOLERANCE of the
# counter AND clearly better than the runner-up: the runner-up must be both
# RUNNER_UP_MARGIN times worse and more than TIE_ABS worse. TIE_ABS also
# decides whether a sensor's two signs can be told apart.
MATCH_TOLERANCE = 0.15
RUNNER_UP_MARGIN = 2.0
TIE_ABS = 0.02
# Evidence needed before anything is compared: candidate history covering at
# least MIN_COVERAGE of the window; counters readable in at least MIN_COVERAGE
# of the hours and in at least MIN_EVIDENCE_HOURS of them; and the counters
# moving by at least MIN_ENERGY_KWH, or there is nothing to match against.
MIN_COVERAGE = 0.9
MIN_EVIDENCE_HOURS = 6
MIN_ENERGY_KWH = 0.5
MIN_BALANCE_SAMPLES = 12

CANDIDATE = "candidate"
AMBIGUOUS = "ambiguous"
NO_MATCH = "no_match"
INSUFFICIENT = "insufficient_evidence"
NO_CANDIDATES = "no_candidates"

# A value of None is a gap (unavailable / unknown), never zero.
Series = Sequence[tuple[datetime, float | None]]

ENERGY_DASHBOARD = "energy_dashboard"


# --- Energy Dashboard preferences ---------------------------------------------


@dataclass(frozen=True)
class EnergySource:
    """One Energy Dashboard source, normalised across schema versions.

    Directions follow HA: `energy_from` is grid import / solar production /
    battery discharge, `energy_to` is grid export / battery charge. Every
    binding is a tuple, because an older grid source holds several flows; on
    current HA each holds at most one.
    """

    kind: str  # "grid" | "solar" | "battery"
    index: int  # position in energy_sources: which HA source this came from
    schema: str  # "unified" | "legacy" (grid only; "unified" otherwise)
    name: str | None = None
    energy_from: tuple[str, ...] = ()
    energy_to: tuple[str, ...] = ()
    price_import: tuple[str, ...] = ()
    price_export: tuple[str, ...] = ()
    # HA's power statistic(s), in HA's sign convention (module doc)
    rate: tuple[str, ...] = ()
    power_config: tuple[Mapping[str, Any], ...] = ()
    soc: str | None = None
    forecast_entries: tuple[str, ...] = ()
    provenance: str = ENERGY_DASHBOARD


def _strs(*values: Any) -> tuple[str, ...]:
    return tuple(v for v in values if isinstance(v, str) and v)


def _power(src: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[Mapping, ...]]:
    config = src.get("power_config")
    return _strs(src.get("stat_rate")), (
        (dict(config),) if isinstance(config, Mapping) else ()
    )


def parse_energy_sources(prefs: Mapping[str, Any] | None) -> list[EnergySource]:
    """Energy Dashboard `energy_sources`, normalised (module doc). Gas and
    water are ignored."""
    out: list[EnergySource] = []
    for index, src in enumerate((prefs or {}).get("energy_sources") or []):
        if not isinstance(src, Mapping):
            continue
        kind, name = src.get("type"), src.get("name")
        name = name if isinstance(name, str) else None
        if kind == "solar":
            rate, config = _power(src)
            out.append(
                EnergySource(
                    kind="solar",
                    index=index,
                    schema="unified",
                    name=name,
                    energy_from=_strs(src.get("stat_energy_from")),
                    rate=rate,
                    power_config=config,
                    forecast_entries=_strs(
                        *(src.get("config_entry_solar_forecast") or [])
                    ),
                )
            )
        elif kind == "battery":
            rate, config = _power(src)
            soc = src.get("stat_soc")
            out.append(
                EnergySource(
                    kind="battery",
                    index=index,
                    schema="unified",
                    name=name,
                    energy_from=_strs(src.get("stat_energy_from")),
                    energy_to=_strs(src.get("stat_energy_to")),
                    rate=rate,
                    power_config=config,
                    soc=soc if isinstance(soc, str) and soc else None,
                )
            )
        elif kind == "grid" and ("flow_from" in src or "flow_to" in src):
            flows_from = [
                f for f in src.get("flow_from") or [] if isinstance(f, Mapping)
            ]
            flows_to = [f for f in src.get("flow_to") or [] if isinstance(f, Mapping)]
            powers = [p for p in src.get("power") or [] if isinstance(p, Mapping)]
            out.append(
                EnergySource(
                    kind="grid",
                    index=index,
                    schema="legacy",
                    energy_from=_strs(*(f.get("stat_energy_from") for f in flows_from)),
                    energy_to=_strs(*(f.get("stat_energy_to") for f in flows_to)),
                    price_import=_strs(
                        *(f.get("entity_energy_price") for f in flows_from)
                    ),
                    price_export=_strs(
                        *(f.get("entity_energy_price") for f in flows_to)
                    ),
                    rate=_strs(*(p.get("stat_rate") for p in powers)),
                    power_config=tuple(
                        dict(p["power_config"])
                        for p in powers
                        if isinstance(p.get("power_config"), Mapping)
                    ),
                )
            )
        elif kind == "grid":
            rate, config = _power(src)
            out.append(
                EnergySource(
                    kind="grid",
                    index=index,
                    schema="unified",
                    name=name,
                    energy_from=_strs(src.get("stat_energy_from")),
                    energy_to=_strs(src.get("stat_energy_to")),
                    price_import=_strs(src.get("entity_energy_price")),
                    price_export=_strs(src.get("entity_energy_price_export")),
                    rate=rate,
                    power_config=config,
                )
            )
    return out


@dataclass(frozen=True)
class DeviceConsumption:
    """One Energy Dashboard individual device, with what HA says contains it."""

    stat: str  # energy total
    index: int
    name: str | None = None
    rate: str | None = None  # instantaneous power statistic, when set
    included_in: str | None = None  # the device whose total already contains it
    provenance: str = ENERGY_DASHBOARD


def device_consumption(prefs: Mapping[str, Any] | None) -> list[DeviceConsumption]:
    """The Energy Dashboard's individual devices, in order, keeping
    `included_in_stat` -- containment as the household stated it, never
    rediscovered from Home Assistant device ownership (Mark, #1590)."""
    out: list[DeviceConsumption] = []
    for index, d in enumerate((prefs or {}).get("device_consumption") or []):
        if not isinstance(d, Mapping) or not isinstance(d.get("stat_consumption"), str):
            continue

        def _opt(key: str, d: Mapping[str, Any] = d) -> str | None:
            value = d.get(key)
            return value if isinstance(value, str) and value else None

        out.append(
            DeviceConsumption(
                stat=d["stat_consumption"],
                index=index,
                name=_opt("name"),
                rate=_opt("stat_rate"),
                included_in=_opt("included_in_stat"),
            )
        )
    return out


def device_consumption_entities(prefs: Mapping[str, Any] | None) -> list[str]:
    """The individual devices' energy totals, in order."""
    return [d.stat for d in device_consumption(prefs)]


# --- integrating history ---------------------------------------------------------


def _finite(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _clean(series: Series) -> list[tuple[datetime, float | None]]:
    """Sorted, with every non-finite or unreadable value turned into a gap."""
    return sorted(((t, _finite(v)) for t, v in series), key=lambda p: p[0])


def _segments(series: Series, start: datetime, end: datetime):
    """(value or None, hours) for each held segment of [start, end). Home
    Assistant records a sensor only when it changes, so a value holds until
    the next one; before the first sample there is no value (a gap)."""
    pts = _clean(series)
    value: float | None = None
    for t, v in pts:
        if t <= start:
            value = v
    cursor = start
    for t, v in pts:
        if t <= start:
            continue
        if t >= end:
            break
        yield value, (t - cursor).total_seconds() / 3600.0
        value, cursor = v, t
    if end > cursor:
        yield value, (end - cursor).total_seconds() / 3600.0


def integrate_parts(
    series: Series, start: datetime, end: datetime
) -> tuple[float, float]:
    """(positive kWh, negative kWh as a positive number) of a kW series over
    [start, end), holding each value until the next change. Gaps add
    nothing -- see `coverage` for how much of the window was measured."""
    pos = neg = 0.0
    for value, hours in _segments(series, start, end):
        if value is None:
            continue
        if value > 0:
            pos += value * hours
        else:
            neg += -value * hours
    return pos, neg


def coverage(series: Series, start: datetime, end: datetime) -> float:
    """The fraction of [start, end) for which the series held a real value."""
    total = (end - start).total_seconds() / 3600.0
    if total <= 0:
        return 0.0
    held = sum(h for v, h in _segments(series, start, end) if v is not None)
    return held / total


def counter_rise(series: Series, start: datetime, end: datetime) -> float | None:
    """kWh a `total_increasing` counter rose over [start, end), or None when
    that cannot be read. A drop is a reset (a daily counter at midnight): the
    new value counts from zero.

    Home Assistant records a counter only when it changes, so a counter whose
    last reading holds through the window is MEASURED to have risen 0 -- that
    is evidence, not a gap. It is missing (None) only when no reading is
    known at the start and fewer than two arrive inside, or when it is
    unavailable at the start or at any point inside the window."""
    pts = _clean(series)
    before = [p for p in pts if p[0] <= start]
    # A reading AT `end` belongs here: it holds the energy up to `end`. With
    # the next window's baseline being that same reading, every change is
    # counted exactly once across adjacent windows.
    inside = [p for p in pts if start < p[0] <= end]
    if before:
        if before[-1][1] is None:
            return None  # unavailable as the window opens
        chain = [before[-1], *inside]
    else:
        if len(inside) < 2:
            return None
        chain = inside
    if any(v is None for _, v in chain):
        return None
    values = [v for _, v in chain]
    return sum((b - a) if b >= a else b for a, b in pairwise(values))


def _rel_err(got: float, want: float) -> float:
    return abs(got - want) / want if want > 0 else (0.0 if got <= 0.05 else math.inf)


# --- pairing power with energy ---------------------------------------------------


@dataclass
class PowerMatch:
    """A candidate for the household to confirm (module doc)."""

    status: str  # CANDIDATE | AMBIGUOUS | NO_MATCH | INSUFFICIENT | NO_CANDIDATES
    entity_id: str | None  # set only when status is CANDIDATE
    sign: int | None  # +1: the sensor's sign is the role's; -1: flipped; None: unknown
    error: float  # relative error of the best candidate
    ranking: list[tuple[str, float]] = field(default_factory=list)  # best first
    reason: str = ""


def _hourly(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out, t = [], start
    while t < end:
        out.append((t, min(t + timedelta(hours=1), end)))
        t += timedelta(hours=1)
    return out


def _profile_error(cand: Sequence[float], want: Sequence[float]) -> float:
    """Hour-by-hour mismatch as a fraction of the counter's total: a power
    sensor must not only integrate to the day's energy, it must rise when the
    counter rises. Solar is zero at night and a house load is not, so two
    sensors with the same daily total no longer tie."""
    total = sum(want)
    if total <= 0:
        return math.inf
    return sum(abs(c - w) for c, w in zip(cand, want, strict=True)) / total


def match_power_sensor(
    candidates: Mapping[str, Series],
    start: datetime,
    end: datetime,
    *,
    energy_out: float | Series | None,
    energy_in: float | Series | None = None,
) -> PowerMatch:
    """The candidate power sensor whose integrated history matches the
    source's energy counters over [start, end) -- a CANDIDATE, not a mapping.

    `energy_out` is the counter the role's POSITIVE direction fills: solar
    production, battery discharge, grid import. `energy_in` is the opposite
    direction (battery charge, grid export), or None for solar. Each candidate
    is tried with its own sign and flipped.

    Pass the counters' HISTORY (a Series) and the match is scored hour by
    hour, over the hours where every counter is readable; a plain float
    scores the window's total only. Missing evidence is never read as a
    measured zero: too little counter history, counters that barely moved, or
    candidate history with gaps gives `insufficient_evidence`."""
    hours = _hourly(start, end)
    eligible = {
        eid: series
        for eid, series in candidates.items()
        if coverage(series, start, end) >= MIN_COVERAGE
    }
    if not candidates:
        return PowerMatch(
            NO_CANDIDATES, None, None, math.inf, [], "no power sensors to compare"
        )
    if not eligible:
        return PowerMatch(
            INSUFFICIENT,
            None,
            None,
            math.inf,
            [],
            f"no candidate has history for {MIN_COVERAGE:.0%} of the window",
        )

    def _rises(counter: float | Series | None) -> list[float | None] | None:
        if counter is None or isinstance(counter, (int, float)):
            return None
        return [counter_rise(counter, a, b) for a, b in hours]

    out_h, in_h = _rises(energy_out), _rises(energy_in)
    scored: list[tuple[float, str, int]] = []
    if out_h is not None or in_h is not None:
        # Hours where every counter given as history is readable.
        use = [
            i
            for i in range(len(hours))
            if (out_h is None or out_h[i] is not None)
            and (in_h is None or in_h[i] is not None)
        ]
        if len(use) < MIN_EVIDENCE_HOURS or len(use) < MIN_COVERAGE * len(hours):
            return PowerMatch(
                INSUFFICIENT,
                None,
                None,
                math.inf,
                [],
                f"the energy counters are readable in only {len(use)} of {len(hours)} hours",
            )
        want_out = [out_h[i] for i in use] if out_h is not None else None
        want_in = [in_h[i] for i in use] if in_h is not None else None
        moved = sum(want_out or []) + sum(want_in or [])
        if moved < MIN_ENERGY_KWH:
            return PowerMatch(
                INSUFFICIENT,
                None,
                None,
                math.inf,
                [],
                f"the energy counters moved only {moved:.2f} kWh: nothing to match",
            )
        for eid, series in eligible.items():
            parts = [integrate_parts(series, *hours[i]) for i in use]
            pos_h = [p for p, _ in parts]
            neg_h = [n for _, n in parts]
            for sign, o, i in ((+1, pos_h, neg_h), (-1, neg_h, pos_h)):
                err = _profile_error(o, want_out) if want_out is not None else 0.0
                if want_in is not None:
                    err = max(err, _profile_error(i, want_in))
                elif sum(i) > max(0.1, 0.1 * sum(want_out or [0.0])):
                    err = max(err, 1.0)  # solar never runs backwards
                scored.append((err, eid, sign))
        return _pick(scored)

    out_total = _finite(energy_out) if energy_out is not None else None
    in_total = _finite(energy_in) if energy_in is not None else None
    if out_total is None and in_total is None:
        return PowerMatch(
            INSUFFICIENT, None, None, math.inf, [], "no energy counter given"
        )
    if (out_total or 0.0) + (in_total or 0.0) < MIN_ENERGY_KWH:
        return PowerMatch(
            INSUFFICIENT,
            None,
            None,
            math.inf,
            [],
            "the energy counters barely moved: nothing to match",
        )
    for eid, series in eligible.items():
        pos, neg = integrate_parts(series, start, end)
        for sign, out_part, in_part in ((+1, pos, neg), (-1, neg, pos)):
            err = _rel_err(out_part, out_total) if out_total is not None else 0.0
            if in_total is not None:
                err = max(err, _rel_err(in_part, in_total))
            elif in_part > max(0.1, 0.1 * (out_total or 0.0)):
                err = max(err, 1.0)  # solar never runs backwards
            scored.append((err, eid, sign))
    return _pick(scored)


def _indistinguishable(a: float, b: float) -> bool:
    """Two errors the evidence cannot tell apart: within TIE_ABS, or within
    RUNNER_UP_MARGIN of each other. An exact tie (0 and 0) is one."""
    lo, hi = min(a, b), max(a, b)
    return hi - lo <= TIE_ABS or hi <= lo * RUNNER_UP_MARGIN


def _pick(scored: list[tuple[float, str, int]]) -> PowerMatch:
    per_entity: dict[str, dict[int, float]] = {}
    for err, eid, sign in scored:
        per_entity.setdefault(eid, {})[sign] = err
    ranking = sorted(
        ((eid, min(errs.values())) for eid, errs in per_entity.items()),
        key=lambda x: (x[1], x[0]),
    )
    best_eid, best_err = ranking[0]
    if best_err > MATCH_TOLERANCE:
        return PowerMatch(
            NO_MATCH,
            None,
            None,
            best_err,
            ranking,
            f"no power sensor matches the energy counter (best {best_err:.0%} off)",
        )
    rivals = [
        eid
        for eid, err in ranking[1:]
        if err <= MATCH_TOLERANCE and _indistinguishable(best_err, err)
    ]
    if rivals:
        return PowerMatch(
            AMBIGUOUS,
            None,
            None,
            best_err,
            ranking,
            f"{best_eid} and {', '.join(rivals)} match equally well; pick one",
        )
    signs = per_entity[best_eid]
    if _indistinguishable(signs.get(+1, math.inf), signs.get(-1, math.inf)):
        return PowerMatch(
            INSUFFICIENT,
            None,
            None,
            best_err,
            ranking,
            f"{best_eid} matches, but its direction cannot be told apart over this window",
        )
    sign = +1 if signs.get(+1, math.inf) <= signs.get(-1, math.inf) else -1
    return PowerMatch(CANDIDATE, best_eid, sign, best_err, ranking)


# --- the devices check each other (design §3.3) -----------------------------------


@dataclass
class BalanceVerdict:
    """Corroborating evidence only (Mark, #1574). `hypotheses` lists every
    single change that makes the balance close; `explanation` names it only
    when there is exactly one. Several fitting at once is `ambiguous`, not a
    unique cause. Never applied automatically.

    The caller must pass series sharing one measurement boundary (whole site),
    time-aligned sample for sample; this checks only what it can (equal
    lengths, finite values, enough samples)."""

    ok: bool
    explanation: str  # "agree" | "flip:<role>" | "scale:<role>:<x1000|/1000>" | "partial_solar" | "ambiguous" | "unexplained" | "insufficient_evidence"
    residual_kw: float
    hypotheses: list[str] = field(default_factory=list)


def check_energy_balance(
    load: Sequence[float],
    solar: Sequence[float],
    battery: Sequence[float],
    grid: Sequence[float],
    *,
    tolerance_kw: float = 0.5,
) -> BalanceVerdict:
    """grid ≈ load − solar − battery (battery positive = discharge, grid
    positive = import), on aligned samples in kW. If it does not hold, list
    each single change that makes it hold: a flipped sign on one role, one
    role ×1000 or ÷1000, or solar that is only part of the total (scaled up
    by the factor that best fits). A change counts only if the corrected
    balance closes within `tolerance_kw`."""
    series = [list(load), list(solar), list(battery), list(grid)]
    n = len(series[0])
    if any(len(s) != n for s in series):
        return BalanceVerdict(
            False, INSUFFICIENT, math.inf, []
        )  # not aligned sample for sample
    if n < MIN_BALANCE_SAMPLES or any(_finite(v) is None for s in series for v in s):
        return BalanceVerdict(False, INSUFFICIENT, math.inf, [])

    def residual(l, s, b, g):
        return sum(abs(g[i] - (l[i] - s[i] - b[i])) for i in range(n)) / n

    base = residual(*series)
    if base <= tolerance_kw:
        return BalanceVerdict(True, "agree", base, [])

    roles = dict(zip(("load", "solar", "battery", "grid"), series, strict=True))
    fits: list[tuple[float, str]] = []
    for role, values in roles.items():
        for label, change in (
            (f"flip:{role}", lambda v: -v),
            (f"scale:{role}:x1000", lambda v: v * 1000.0),
            (f"scale:{role}:/1000", lambda v: v * 0.001),
        ):
            trial = {**roles, role: [change(v) for v in values]}
            res = residual(
                trial["load"], trial["solar"], trial["battery"], trial["grid"]
            )
            if res <= tolerance_kw:
                fits.append((res, label))
    # Partial solar: the measured solar is one part of the total. Fit the
    # factor k that best explains the balance (least squares on
    # grid = load - k*solar - battery) and count it only when k > 1 and the
    # corrected balance actually closes -- not merely because the residual
    # leans the right way, which a W/kW mix-up on another role also does.
    s2 = sum(v * v for v in solar)
    if s2 > 0:
        k = sum(solar[i] * (load[i] - battery[i] - grid[i]) for i in range(n)) / s2
        if k > 1.05:
            res = residual(load, [v * k for v in solar], battery, grid)
            if res <= tolerance_kw:
                fits.append((res, "partial_solar"))
    fits.sort()
    hypotheses = [label for _res, label in fits]
    if not hypotheses:
        return BalanceVerdict(False, "unexplained", base, [])
    if len(hypotheses) > 1:
        return BalanceVerdict(False, AMBIGUOUS, fits[0][0], hypotheses)
    return BalanceVerdict(False, hypotheses[0], fits[0][0], hypotheses)


def entities_on_device(
    registry_entries: Iterable[Mapping[str, Any]], device_id: str
) -> list[str]:
    """Entity ids registered to `device_id` (entity registry rows as dicts)."""
    return [
        e["entity_id"]
        for e in registry_entries
        if e.get("device_id") == device_id and e.get("entity_id")
    ]
