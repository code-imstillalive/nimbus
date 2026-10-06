"""Device resolver for the two-step setup (nimbus #1574, design
docs/design/two-step-setup.md §3; Mark's review: *"devices with entities under
each; power, energy, limits, forecasts"*).

Home Assistant's Energy Dashboard names **energy** totals only. Read live from
the reference household (HA 2026.7.4, `energy/get_prefs`) it holds two solar
and two battery sources, one per inverter, no power fields and no battery SoC.
The power sensor behind each source has to be found on the source's **device**,
and a hybrid inverter carries PV, battery, grid and load power all on one
device. So "the one power sensor on the device" is not enough, and choosing by
entity name is a guess.

This module pairs each energy total with the power sensor whose own history
**integrates to it**. A candidate whose positive part over a window matches
the rise in `daily_battery_discharge_inv1` is that battery's power sensor, and
the same comparison says which way its sign runs. That is evidence, not a name
match, and it is the design's §3.4 "energy is a second witness" check doing the
selection.

**Evidence, not proof** (Mark's device contract, #1574). Everything this
module returns is a *candidate* for the user to confirm, never a mapping applied
on its own: "same Home Assistant device" is a candidate-selection hint, and a
match must also have an unambiguous role and measurement boundary, compatible
units, sign convention and timestamps. A balance residual can *suggest* a
mismatch; it does not uniquely prove a bad sensor or justify a calibration
correction. Callers present these results as "here is what Nimbus found -- is
this right?".

Everything here is pure (no Home Assistant), so real installs' history replays
through it in tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

# A candidate is accepted only when its integrated energy is within this
# relative error of the counter, AND clearly better than the runner-up.
MATCH_TOLERANCE = 0.15
RUNNER_UP_MARGIN = 2.0  # the best's error must be at most half the second's

Series = Sequence[tuple[datetime, float]]


# --- Energy Dashboard preferences ---------------------------------------------


@dataclass
class EnergySource:
    """One Energy Dashboard source, normalised across schema versions."""

    kind: str  # "grid" | "solar" | "battery"
    energy_from: str | None = None  # grid import / solar production / battery discharge
    energy_to: str | None = None  # grid export / battery charge
    price_import: str | None = None
    price_export: str | None = None
    forecast_entries: list[str] = field(default_factory=list)


def parse_energy_sources(prefs: Mapping[str, Any] | None) -> list[EnergySource]:
    """Energy Dashboard `energy_sources`, normalised (#1589).

    Reads both grid schemas: the current flat one (`stat_energy_from`,
    `stat_energy_to`, `entity_energy_price`, `entity_energy_price_export`,
    seen on HA 2026.7) and the older `flow_from` / `flow_to` lists (HA 2025.1's
    `energy/data.py`). Gas and water are ignored.
    """
    out: list[EnergySource] = []
    for src in (prefs or {}).get("energy_sources") or []:
        if not isinstance(src, Mapping):
            continue
        kind = src.get("type")
        if kind == "solar":
            fc = src.get("config_entry_solar_forecast") or []
            out.append(
                EnergySource(
                    kind="solar",
                    energy_from=src.get("stat_energy_from"),
                    forecast_entries=[e for e in fc if isinstance(e, str)],
                )
            )
        elif kind == "battery":
            out.append(
                EnergySource(
                    kind="battery",
                    energy_from=src.get("stat_energy_from"),
                    energy_to=src.get("stat_energy_to"),
                )
            )
        elif kind == "grid":
            if "flow_from" in src or "flow_to" in src:
                flows_from = [
                    f for f in src.get("flow_from") or [] if isinstance(f, Mapping)
                ]
                flows_to = [
                    f for f in src.get("flow_to") or [] if isinstance(f, Mapping)
                ]
                # one EnergySource per import/export pair, in order
                for i in range(max(len(flows_from), len(flows_to), 1)):
                    ff = flows_from[i] if i < len(flows_from) else {}
                    ft = flows_to[i] if i < len(flows_to) else {}
                    out.append(
                        EnergySource(
                            kind="grid",
                            energy_from=ff.get("stat_energy_from"),
                            energy_to=ft.get("stat_energy_to"),
                            price_import=ff.get("entity_energy_price"),
                            price_export=ft.get("entity_energy_price"),
                        )
                    )
            else:
                out.append(
                    EnergySource(
                        kind="grid",
                        energy_from=src.get("stat_energy_from"),
                        energy_to=src.get("stat_energy_to"),
                        price_import=src.get("entity_energy_price"),
                        price_export=src.get("entity_energy_price_export"),
                    )
                )
    return out


def as_flow_lists(source: Mapping[str, Any]) -> Mapping[str, Any]:
    """A grid source in the older `flow_from` / `flow_to` list form, whichever
    schema it arrived in (#1589), so code written against the list form keeps
    working on current Home Assistant. Non-grid sources pass through."""
    if not isinstance(source, Mapping) or source.get("type") != "grid":
        return source
    if "flow_from" in source or "flow_to" in source:
        return source
    return {
        **source,
        "flow_from": [
            {
                "stat_energy_from": source.get("stat_energy_from"),
                "entity_energy_price": source.get("entity_energy_price"),
            }
        ],
        "flow_to": [
            {
                "stat_energy_to": source.get("stat_energy_to"),
                "entity_energy_price": source.get("entity_energy_price_export"),
            }
        ],
    }


def device_consumption_entities(prefs: Mapping[str, Any] | None) -> list[str]:
    """The Energy Dashboard's individual devices (energy totals), in order."""
    return [
        d["stat_consumption"]
        for d in (prefs or {}).get("device_consumption") or []
        if isinstance(d, Mapping) and isinstance(d.get("stat_consumption"), str)
    ]


# --- integrating history ---------------------------------------------------------


def integrate_parts(
    series: Series, start: datetime, end: datetime
) -> tuple[float, float]:
    """(positive kWh, negative kWh as a positive number) of a kW series over
    [start, end), holding each value until the next change -- Home Assistant
    records a sensor only when it changes, so hold, never interpolate."""
    pos = neg = 0.0
    pts = sorted(series, key=lambda p: p[0])
    value = None
    for t, v in pts:
        if t <= start:
            value = v
    cursor = start
    for t, v in pts:
        if t <= start:
            continue
        if t >= end:
            break
        if value is not None:
            hours = (t - cursor).total_seconds() / 3600.0
            if value > 0:
                pos += value * hours
            else:
                neg += -value * hours
        value, cursor = v, t
    if value is not None and end > cursor:
        hours = (end - cursor).total_seconds() / 3600.0
        if value > 0:
            pos += value * hours
        else:
            neg += -value * hours
    return pos, neg


def counter_rise(series: Series, start: datetime, end: datetime) -> float | None:
    """kWh a `total_increasing` counter rose over [start, end). A drop is a
    reset (a daily counter at midnight): the new value counts from zero."""
    pts = sorted((p for p in series if start <= p[0] < end), key=lambda p: p[0])
    before = [p for p in series if p[0] < start]
    if before:
        pts = [max(before, key=lambda p: p[0])] + pts
    if len(pts) < 2:
        return None
    rise = 0.0
    for (_, a), (_, b) in pairwise(pts):
        rise += (b - a) if b >= a else b
    return rise


def _rel_err(got: float, want: float) -> float:
    if want <= 0:
        return 0.0 if got <= 0.05 else float("inf")
    return abs(got - want) / want


# --- pairing power with energy ---------------------------------------------------


@dataclass
class PowerMatch:
    entity_id: str | None
    sign: int  # +1: the sensor's own sign matches the role's convention; -1: flipped
    error: float  # relative error of the best match
    ranking: list[tuple[str, float]]  # (entity_id, error), best first
    reason: str = ""


def _hourly(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    from datetime import timedelta

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
        return 0.0 if sum(cand) <= 0.05 else float("inf")
    return sum(abs(c - w) for c, w in zip(cand, want, strict=False)) / total


def match_power_sensor(
    candidates: Mapping[str, Series],
    start: datetime,
    end: datetime,
    *,
    energy_out: float | Series | None,
    energy_in: float | Series | None = None,
) -> PowerMatch:
    """The candidate power sensor whose integrated history matches the
    source's energy counters over [start, end) -- a CANDIDATE for the user to
    confirm, not a mapping (Mark, #1574).

    `energy_out` is the counter the role's POSITIVE direction fills: solar
    production, battery discharge, grid import. `energy_in` is the opposite
    direction (battery charge, grid export), or None for solar. Each candidate
    is tried with its own sign and flipped; the sign that fits is reported.

    Pass the counters' own HISTORY (a Series) rather than a total, and the
    match is scored hour by hour (`_profile_error`), which separates sensors
    whose daily totals happen to agree. A plain float scores the total only.
    """
    hours = _hourly(start, end)

    def _rises(counter: float | Series | None) -> list[float] | None:
        if counter is None or isinstance(counter, (int, float)):
            return None
        return [counter_rise(counter, a, b) or 0.0 for a, b in hours]

    out_h, in_h = _rises(energy_out), _rises(energy_in)
    if out_h is not None or in_h is not None:
        scored_h: list[tuple[float, str, int]] = []
        for eid, series in candidates.items():
            parts = [integrate_parts(series, a, b) for a, b in hours]
            pos_h = [p for p, _ in parts]
            neg_h = [n for _, n in parts]
            for sign, o, i in ((+1, pos_h, neg_h), (-1, neg_h, pos_h)):
                err = _profile_error(o, out_h) if out_h is not None else 0.0
                if in_h is not None:
                    err = max(err, _profile_error(i, in_h))
                elif sum(i) > max(0.1, 0.1 * sum(out_h or [0.0])):
                    err = max(err, 1.0)  # solar never runs backwards
                scored_h.append((err, eid, sign))
        return _pick(scored_h)
    out_total = float(energy_out) if isinstance(energy_out, (int, float)) else None
    in_total = float(energy_in) if isinstance(energy_in, (int, float)) else None
    scored: list[tuple[float, str, int]] = []
    for eid, series in candidates.items():
        pos, neg = integrate_parts(series, start, end)
        for sign, out_part, in_part in ((+1, pos, neg), (-1, neg, pos)):
            err = _rel_err(out_part, out_total) if out_total is not None else 0.0
            if in_total is not None:
                err = max(err, _rel_err(in_part, in_total))
            elif in_part > max(0.1, 0.1 * (out_total or 0.0)):
                err = max(err, 1.0)  # solar never runs backwards
            scored.append((err, eid, sign))
    return _pick(scored)


def _pick(scored: list[tuple[float, str, int]]) -> PowerMatch:
    scored.sort()
    best_per_entity: dict[str, tuple[float, int]] = {}
    for err, eid, sign in scored:
        best_per_entity.setdefault(eid, (err, sign))
    ranking = sorted(
        ((eid, e) for eid, (e, _s) in best_per_entity.items()), key=lambda x: x[1]
    )
    if not ranking:
        return PowerMatch(None, +1, float("inf"), [], "no power sensors on the device")
    best_eid, best_err = ranking[0]
    if best_err > MATCH_TOLERANCE:
        return PowerMatch(
            None,
            +1,
            best_err,
            ranking,
            f"no power sensor matches the energy counter (best {best_err:.0%} off)",
        )
    if (
        len(ranking) > 1
        and ranking[1][1] < best_err * RUNNER_UP_MARGIN
        and ranking[1][1] <= MATCH_TOLERANCE
    ):
        return PowerMatch(
            None,
            +1,
            best_err,
            ranking,
            f"{best_eid} and {ranking[1][0]} both match; pick one",
        )
    return PowerMatch(best_eid, best_per_entity[best_eid][1], best_err, ranking)


# --- the devices check each other (design §3.3) -----------------------------------


@dataclass
class BalanceVerdict:
    """Corroborating evidence only (Mark, #1574): `explanation` is the single
    change that would make the balance close, offered as a *hypothesis* for
    the user to confirm, never applied automatically. It is meaningful only
    when the four series share a measurement boundary, are time-aligned, and
    vary enough over the window to tell the hypotheses apart."""

    ok: bool
    explanation: str  # "agree" | "flip:<role>" | "scale:<role>:<factor>" | "partial_solar" | "unexplained"
    residual_kw: float


def check_energy_balance(
    load: Sequence[float],
    solar: Sequence[float],
    battery: Sequence[float],
    grid: Sequence[float],
    *,
    tolerance_kw: float = 0.5,
) -> BalanceVerdict:
    """grid ≈ load − solar − battery (battery positive = discharge, grid
    positive = import), on aligned samples in kW. If it does not hold, find
    the single change that makes it hold: a flipped sign on one role, one role
    ×1000 or ÷1000, or solar that is only part of the total."""

    def mean_abs_residual(l, s, b, g):
        n = min(len(l), len(s), len(b), len(g))
        if n == 0:
            return float("inf")
        return sum(abs(g[i] - (l[i] - s[i] - b[i])) for i in range(n)) / n

    base = mean_abs_residual(load, solar, battery, grid)
    if base <= tolerance_kw:
        return BalanceVerdict(True, "agree", base)

    roles = {
        "load": list(load),
        "solar": list(solar),
        "battery": list(battery),
        "grid": list(grid),
    }
    trials: list[tuple[float, str]] = []
    for role, values in roles.items():
        flipped = {**roles, role: [-v for v in values]}
        trials.append(
            (
                mean_abs_residual(
                    flipped["load"],
                    flipped["solar"],
                    flipped["battery"],
                    flipped["grid"],
                ),
                f"flip:{role}",
            )
        )
        for factor in (1000.0, 0.001):
            scaled = {**roles, role: [v * factor for v in values]}
            label = "x1000" if factor > 1 else "/1000"
            trials.append(
                (
                    mean_abs_residual(
                        scaled["load"],
                        scaled["solar"],
                        scaled["battery"],
                        scaled["grid"],
                    ),
                    f"scale:{role}:{label}",
                )
            )
    # partial solar: the residual is consistently "more solar than measured"
    n = min(len(load), len(solar), len(battery), len(grid))
    missing = [(load[i] - solar[i] - battery[i]) - grid[i] for i in range(n)]
    if (
        n
        and all(m >= -tolerance_kw for m in missing)
        and sum(missing) / n > tolerance_kw
        and max(solar[:n] or [0]) > 0
    ):
        trials.append((tolerance_kw, "partial_solar"))
    trials.sort()
    best_res, best = trials[0]
    if best_res <= tolerance_kw:
        return BalanceVerdict(False, best, best_res)
    return BalanceVerdict(False, "unexplained", base)


def entities_on_device(
    registry_entries: Iterable[Mapping[str, Any]], device_id: str
) -> list[str]:
    """Entity ids registered to `device_id` (entity registry rows as dicts)."""
    return [
        e["entity_id"]
        for e in registry_entries
        if e.get("device_id") == device_id and e.get("entity_id")
    ]
