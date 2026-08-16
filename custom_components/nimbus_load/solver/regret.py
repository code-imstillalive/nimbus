"""Regret-based evaluation -- the objective a closed-loop MPC system
should actually be judged on, not MAE/MASE against a raw forecast.

## Why this file exists (direct response to real, substantive feedback)

Mark Purcell, 2026-08-16: "The end game is to minimise cost maximise
benefits regardless of how many times the kettle is turned on... Change
the metric. The objective is regret, not MAE... R = J_ach - J*... Random
noise is cheap, systematic peak-timing bias is expensive, and MAE scores
them identically. That's the defect."

This is correct and this project's own prior Forecaster validation
(MAE/MASE against a naive-seasonal baseline) never actually tested it --
a low point-forecast MAE says nothing about downstream economic outcome
once a rolling re-solve loop (rolling.py) exists to absorb forecast
error before it's ever actually dispatched. This file builds the actual
regret framework and, as a first concrete test, answers Mark's own
stated prediction directly against real data: does regret respond
mostly to systematic timing bias, and comparatively little to zero-mean
noise, once the closed loop (rolling.py) is doing its job?

## J, stated explicitly, with units (Mark's ask #1 -- "state J explicitly
with units, plus the terms you omit. Anything absent from J cannot
appear in regret.")

    J = sum_t [ p_buy(t)*g_import(t)*dt(t)  -  p_sell(t)*g_export(t)*dt(t)
              + c_chg*p_chg(t)*dt(t)  +  c_dis*p_dis(t)*dt(t) ]
        - v_salv * E(T)

All monetary terms in $ (AUD, matching this household's real currency);
p_buy/p_sell in $/kWh; g_import/g_export/p_chg/p_dis in kW; dt(t) in
hours; c_chg/c_dis in $/kWh; v_salv in $/kWh; E(T) in kWh (battery
stored energy at the final period). This is EXACTLY network.py's own LP
objective (BatteryConfig/GridConfig cost terms, `Plan.total_cost`) --
not a new formula, the existing one, made explicit as J per Mark's ask.

**Terms this J deliberately omits (stated plainly, per Mark's own
instruction -- these CANNOT appear in any regret number computed here,
and any claim this file makes is only ever a claim about the terms
actually IN J, not about total household welfare):**
- No demand-charge / peak-kW tariff term (this household's real Energex
  tariff, per this project's own CLAUDE.md, is TOU energy-rate based;
  not independently re-verified here as demand-charge-free).
- No battery degradation/cycle-life cost beyond the linear c_chg/c_dis
  friction terms already in J -- no explicit capacity-fade curve.
- No comfort/inconvenience cost for sheddable loads (this household has
  zero real sheddable loads configured today; SheddableLoadConfig's own
  shed_cost would need real calibration if that changed).
- No explicit P2P settlement-probability term -- export price is
  treated as certain within J, even though this project's own real P2P
  matching is confirmed pro-rata/probabilistic (see the sibling
  116KAT-HA-AI repo's own CLAUDE.md).
- No explicit reliability/blackout-risk cost -- min_soc's own hard
  floor is the only protection against depletion; nothing in J prices
  the RISK of running close to it.
- Curtailed solar has zero cost/benefit in J (realistic for a household
  with no FIT on curtailed generation, per this project's own documented
  solar-priority order, but worth stating: curtailment is "free" here).

## The realized-cost evaluator's real physical subtlety

Regret compares different DISPATCH TRAJECTORIES against the SAME
realized ground truth. For a trajectory produced by a plan built on a
WRONG load forecast, only the battery's own charge/discharge setpoint
is genuinely "committed" -- a real Sungrow inverter executes exactly the
kW it's told, regardless of whether the household's real load matches
what the plan assumed. grid_import/grid_export are NOT independently
committed; they are what the real physical balance equation FORCES,
given the REAL load/solar and the battery's own committed setpoint.
`evaluate_realized_cost()` below recomputes them from the real balance
equation rather than trusting whatever grid_import/export a
forecast-based plan itself reported (which balances against the WRONG
load, not reality) -- this is the one place a naive "just read the
plan's own numbers" evaluation would silently get regret wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from .elements import BatteryConfig, GridConfig, LoadConfig, PeriodGrid, SolarConfig
from .network import build_plan


@dataclass(frozen=True)
class RealizedCost:
    """One dispatch trajectory's real economic outcome, evaluated
    against REALIZED (ground-truth) price/load/solar -- see this
    module's own docstring for J's exact definition and omitted terms.
    """

    total_cost: float
    grid_import_kw: NDArray[np.float64]
    grid_export_kw: NDArray[np.float64]
    solar_used_kw: NDArray[np.float64]
    solar_curtailed_kw: NDArray[np.float64]


def evaluate_realized_cost(
    *,
    hours: NDArray[np.float64],
    load_real_kw: NDArray[np.float64],
    solar_real_kw: NDArray[np.float64],
    import_price_real: NDArray[np.float64],
    export_price_real: NDArray[np.float64],
    charge_committed_kw: NDArray[np.float64],
    discharge_committed_kw: NDArray[np.float64],
    charge_cost: float | NDArray[np.float64],
    discharge_cost: float | NDArray[np.float64],
    final_soc_kwh: float,
    salvage_value: float,
    grid_import_limit_kw: float,
    grid_export_limit_kw: float,
) -> RealizedCost:
    """J evaluated against REALIZED ground truth for a given (committed
    battery) dispatch trajectory. grid_import/export and solar_used are
    RECOMPUTED from the real balance equation, not read off whatever a
    forecast-based plan itself reported -- see module docstring.

    charge_cost/discharge_cost accept a per-period array as well as a
    scalar (2026-08-16, added for the first real reconciliation run --
    this household's real cost schedule is genuinely time-varying,
    $0.09/kWh discharge_cost 7am-5pm vs $0.01/kWh 5pm-7am; scoring a
    real day against a single flat scalar would misprice roughly two
    thirds of it). Mirrors the same float|array support already added
    to BatteryConfig.charge_cost/discharge_cost and network.py's own
    build_plan() -- this function was the one place in the package that
    hadn't caught up to that shape yet.

    A real balance can genuinely violate `grid_import_limit_kw`/
    `grid_export_limit_kw` (unlike an LP, reality does not refuse an
    infeasible plan -- the physical result is whatever it is). This is
    reported via the returned arrays exceeding the nominal limit, not
    silently clipped -- an honest reflection of what a bad enough
    forecast error would actually do to a real household meter.

    solar_used is ALWAYS the full solar_real_kw, never capped
    (2026-08-16, real bug found and fixed via the first real
    reconciliation run against an actual settled day -- see regret.py's
    own module docstring for the finding). The previous formula,
    `min(solar_real, max(0, load + charge - discharge))`, treated any
    solar beyond (load + charge - discharge) as curtailed -- e.g. solar
    =8kW, load=1kW, charge=5kW, discharge=0 gave solar_used=6, silently
    discarding the remaining 2kW as "curtailed" instead of exported.
    That's wrong for a REALIZED trajectory on a household that doesn't
    curtail in normal operation (curtailment here is a deliberate,
    last-resort LP planning choice, per this project's own documented
    solar-priority order -- not something that happens routinely in
    reality, and this evaluator has no business inventing it after the
    fact). Confirmed live against 2026-08-15's real settlement: the old
    formula overcounted grid_import by ~33kWh and undercounted
    grid_export by ~36kWh for that single day -- solar was being
    silently "curtailed away" on paper for most of the daylight hours.
    solar_curtailed is therefore always zero here -- a REAL measured
    solar reading already reflects whatever actually happened
    (including any genuine real-world curtailment baked into the meter
    itself); this evaluator's job is to balance what's ALREADY real, not
    to re-decide curtailment after the fact.
    """
    n = len(hours)
    charge_cost_arr = np.broadcast_to(np.asarray(charge_cost, dtype=np.float64), (n,))
    discharge_cost_arr = np.broadcast_to(np.asarray(discharge_cost, dtype=np.float64), (n,))
    solar_used = solar_real_kw
    net_needed = load_real_kw + charge_committed_kw - discharge_committed_kw - solar_used
    grid_import = np.maximum(0.0, net_needed)
    grid_export = np.maximum(0.0, -net_needed)
    solar_curtailed = np.zeros_like(solar_real_kw)

    cost = float(
        np.sum(
            import_price_real * grid_import * hours
            - export_price_real * grid_export * hours
            + charge_cost_arr * charge_committed_kw * hours
            + discharge_cost_arr * discharge_committed_kw * hours
        )
        - salvage_value * final_soc_kwh
    )
    return RealizedCost(
        total_cost=cost,
        grid_import_kw=grid_import,
        grid_export_kw=grid_export,
        solar_used_kw=solar_used,
        solar_curtailed_kw=solar_curtailed,
    )


def oracle_dispatch(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar: SolarConfig,
    load: LoadConfig,
) -> tuple[NDArray[np.float64], NDArray[np.float64], float]:
    """J* -- the perfect-foresight optimum: build_plan() given the REAL,
    realized load/solar/price directly, as if perfectly known in
    advance. By construction this achieves the minimum possible J over
    the same feasible region as any other controller evaluated in this
    module -- nothing can beat it, only match it. Returns
    (charge_kw, discharge_kw, final_soc_kwh) -- the oracle's own
    committed battery trajectory (grid/solar_used are NOT returned here;
    re-derive via evaluate_realized_cost() for a consistent comparison
    against every other controller, even though for the oracle
    specifically they're already exactly equal to the plan's own numbers
    by construction -- using the same evaluator for every controller,
    oracle included, is what makes the comparison honest).
    """
    plan = build_plan(periods=periods, grid=grid, battery=battery, solar=solar, loads=[load])
    if not plan.is_optimal:
        msg = f"Oracle solve failed (status={plan.status}) -- this should not happen with real, already-realized data unless the scenario is genuinely infeasible"
        raise RuntimeError(msg)
    return plan.battery_charge_kw, plan.battery_discharge_kw, float(plan.battery_soc_kwh[-1])
