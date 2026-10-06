"""nimbus issue #1577: name the constraint an infeasible period-0 pin hit.

DIAGNOSTIC ONLY. Nothing here builds, changes or solves an LP. It is read
once, on the already-infeasible branch of #1417's period-0 pin instrument
(`solver_plan.period0_crossing_delta`), to say WHY "keep doing what the
previous plan published" was not available -- the row #1417's reading guide
was missing:

    delta ~ 0       a tie the anchor should have held
    delta large     a genuine re-plan, or a moved baseline
    infeasible      the old value no longer fits: the crossing was FORCED,
                    and `blocked_by` names the constraint that forced it

Every check is a period-0 restatement of a row or bound `network.build_plan()`
already builds for batteries[0] (the site each one mirrors is named on it),
evaluated with every OTHER variable at its most permissive value. So each
reported blocker is a PROOF of infeasibility on its own: it can under-report
(a multi-period coupling -- e.g. a later hard SoC floor -- is not a period-0
bound and comes back as `UNEXPLAINED`), but it cannot accuse a constraint that
was not actually violated. `tests/test_1577_infeasible_pin_reports_constraint.py`
pins both halves: each blocker on a pin the real LP rejects, and an empty list
on every pin the real LP accepts.

Why not HiGHS's IIS (`Highs.getIis`, present in highspy >= 1.15): it would
return row INDICES, and most of the battery rows it would name -- the SoC
recursion, the #328 reserve draw, the power-curve segments -- are added
without a `name=`, so they surface as `ub_<i>`/`eq_<i>`. Naming them would
change problem construction to serve a diagnostic, and an IIS needs its own
extra solve. The arithmetic below answers the documented cases (#1417's
2 Oct devhub table) without either.

Deliberately NOT a blocker: the grid IMPORT limit. `grid_import_excess[t]`
(#390) is an unbounded release valve on the supply side, so an import cap can
make a charge pin expensive but never infeasible.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from . import network, p2p_export
from .elements import BatteryConfig, GridConfig

# HiGHS's default primal feasibility tolerance is 1e-7; a margin above it so a
# pin that the LP accepts to tolerance is never reported as a blocker.
TOL = 1e-6

UNEXPLAINED = (
    "unexplained_at_period0: no single period-0 bound is violated -- a "
    "multi-period coupling (e.g. a later hard SoC floor) or a solver status"
)


def _effective0(forecast, lower, upper, risk_aversion, conservative) -> float:
    """Period 0 of a risk-adjusted series, as `network.build_plan()` used it.

    With `risk_aversion` given this IS `network._risk_adjusted()`, the function
    the LP's own rows are built from. With it None, the most permissive value
    the blend could have produced (it always lands between the forecast and
    its band), so a check built on it still never over-reports.
    """
    if risk_aversion is not None:
        return float(
            network._risk_adjusted(
                np.asarray(forecast, dtype=float),
                None if lower is None else np.asarray(lower, dtype=float),
                None if upper is None else np.asarray(upper, dtype=float),
                float(risk_aversion),
                conservative=conservative,
            )[0]
        )
    values = [float(np.asarray(forecast, dtype=float)[0])]
    band = upper if conservative == "upper" else None
    if band is not None:
        values.append(float(np.asarray(band, dtype=float)[0]))
    return max(values)


def _pin_applied(b: BatteryConfig) -> bool:
    """Mirror of the guard on `network.build_plan()`'s period0_pin rows: the
    pin is silently not applied where period 0 is gated or a spike override
    already fixes it, so nothing about it can be infeasible there."""
    if not b.available or b.spike_override_discharge_kw is not None:
        return False
    return not (b.unavailable_period_indices and 0 in b.unavailable_period_indices)


def period0_pin_blockers(
    pin_net_kw: float,
    *,
    batteries: Sequence[BatteryConfig],
    grid: GridConfig,
    hours0: float,
    solar=None,
    loads: Sequence = (),
    sheddable_loads: Sequence = (),
    adequacy_loads: Sequence = (),
    thermal_loads: Sequence = (),
    risk_aversion: float | None = None,
) -> list[str]:
    """Every period-0 constraint the pin on `batteries[0]` provably violates.

    `pin_net_kw` uses the instrument's convention: positive = discharge,
    negative = charge, pinned as charge = max(-pin, 0), discharge =
    max(pin, 0) -- `network.build_plan()`'s two `period0_pin_*` rows. Returns
    a list of `"<constraint>: <numbers>"` strings, empty when nothing at
    period 0 explains the infeasibility. Pure arithmetic; never solves.
    Pass the solve's own `risk_aversion` so load and solar are read exactly as
    the LP read them; without it they are bounded loosely (still sound).
    """
    b = batteries[0]
    if not _pin_applied(b):
        return []
    c = max(-float(pin_net_kw), 0.0)
    d = max(float(pin_net_kw), 0.0)
    h = float(hours0)
    out: list[str] = []

    # Variable bounds on battery_charge/discharge_<b>_0 (network.py, the
    # charge_vars/discharge_vars construction). batteries[0] is the one the
    # P2P fixed-window charge gate applies to.
    charge_ub = p2p_export.charging_ub_during_fixed_window(0, grid, b.max_charge_kw)
    if c > charge_ub + TOL:
        if charge_ub < b.max_charge_kw:
            out.append(
                f"p2p_fixed_window_charge_gate: charge {c:.3f} kW > "
                f"{charge_ub:.3f} kW allowed under a fixed export commitment"
            )
        else:
            out.append(
                f"battery_max_charge: charge {c:.3f} kW > "
                f"max_charge_kw {b.max_charge_kw:.3f}"
            )
    if d > b.max_discharge_kw + TOL:
        out.append(
            f"battery_max_discharge: discharge {d:.3f} kW > "
            f"max_discharge_kw {b.max_discharge_kw:.3f}"
        )

    # SoC-dependent power curves at t=0 (network.py "SoC-dependent power
    # curves"): one row per segment, soc[-1] = initial_soc_kwh.
    for label, power, curve in (
        ("charge_power_curve", c, b.charge_power_curve),
        ("discharge_power_curve", d, b.discharge_power_curve),
    ):
        if not curve:
            continue
        socs = [s for s, _pw in curve]
        powers = [pw for _s, pw in curve]
        ceiling = min(
            powers[i]
            + (powers[i + 1] - powers[i])
            / (socs[i + 1] - socs[i])
            * (b.initial_soc_kwh - socs[i])
            for i in range(len(curve) - 1)
        )
        if power > ceiling + TOL:
            out.append(
                f"{label}: {power:.3f} kW > {ceiling:.3f} kW available at "
                f"initial SoC {b.initial_soc_kwh:.3f} kWh"
            )

    # SoC recursion at t=0 against soc[0]'s HARD bounds [0, capacity_kwh]
    # (network.py "SoC dynamics"; #328 made min/max_soc soft, not these).
    soc0 = (
        b.initial_soc_kwh + c * b.charge_efficiency * h - d * h / b.discharge_efficiency
    )
    if soc0 > b.capacity_kwh + TOL:
        out.append(
            f"soc_capacity_headroom: soc[0] would reach {soc0:.3f} kWh > "
            f"capacity {b.capacity_kwh:.3f} kWh (initial {b.initial_soc_kwh:.3f})"
        )
    if soc0 < -TOL:
        out.append(f"soc_empty: soc[0] would reach {soc0:.3f} kWh < 0")

    # Same-period wash-trade guard (2) at t=0 (#328): period 0 may only draw
    # on SoC that existed above min_soc_kwh at its start.
    if d > 0.0:
        draw = d * h / b.discharge_efficiency
        reserve = max(0.0, b.initial_soc_kwh - b.min_soc_kwh)
        if draw > reserve + TOL:
            out.append(
                f"discharge_reserve: draws {draw:.3f} kWh > {reserve:.3f} kWh "
                f"above min_soc (initial {b.initial_soc_kwh:.3f}, "
                f"min {b.min_soc_kwh:.3f})"
            )

    # Shared-charger ceiling (network.py "shared_charger_<group>_t0"): the
    # minimum declared ceiling across the group, members' charge+discharge.
    if b.shared_charger_group is not None:
        declared = [
            m.shared_charger_max_kw
            for m in batteries
            if m.shared_charger_group == b.shared_charger_group
            and m.shared_charger_max_kw is not None
        ]
        if declared and c + d > min(declared) + TOL:
            out.append(
                f"shared_charger_{b.shared_charger_group}: {c + d:.3f} kW > "
                f"ceiling {min(declared):.3f} kW"
            )

    export_limit0 = float(np.asarray(grid.export_limit_kw, dtype=float).reshape(-1)[0])
    export_lb, export_ub = p2p_export.grid_export_bounds(0, grid, export_limit0)

    # Power balance at t=0 (power_balance_t0), discharge side: with solar
    # curtailed to 0 and no import, the pinned discharge must still be
    # absorbed by export plus every sink at its largest.
    if d > 0.0:
        sink = export_ub
        sink += sum(
            _effective0(x.forecast_kw, x.lower_kw, x.upper_kw, risk_aversion, "upper")
            for x in loads
        )
        sink += sum(
            _effective0(x.forecast_kw, x.lower_kw, x.upper_kw, risk_aversion, "upper")
            for x in sheddable_loads
        )
        sink += sum(float(x.max_power_kw) for x in adequacy_loads)
        sink += sum(float(x.max_power_kw) for x in thermal_loads)
        sink += sum(m.max_charge_kw for m in list(batteries)[1:])
        if d > sink + TOL:
            out.append(
                f"grid_export_limit: discharge {d:.3f} kW > {sink:.3f} kW "
                f"absorbable (export ceiling {export_ub:.3f} kW + loads at "
                f"their largest)"
            )

    # Wash-trade guard (1) at t=0 against a fixed export commitment's FLOOR:
    # export must be funded by solar or discharge, so a committed rate needs
    # at least that much from the pinned battery once solar and every other
    # battery are at their largest.
    if export_lb > 0.0:
        solar_max = (
            0.0
            if solar is None
            else _effective0(
                solar.forecast_kw,
                solar.lower_kw,
                solar.upper_kw,
                risk_aversion,
                "lower",
            )
        )
        others = sum(m.max_discharge_kw for m in list(batteries)[1:])
        if d + solar_max + others < export_lb - TOL:
            out.append(
                f"p2p_fixed_export_commitment: export floor {export_lb:.3f} kW > "
                f"discharge {d:.3f} + solar {solar_max:.3f} + other batteries "
                f"{others:.3f} kW"
            )
    return out
