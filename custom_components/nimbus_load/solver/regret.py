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
              + (c_chg + c_deg)*p_chg(t)*dt(t)  +  (c_dis + c_deg)*p_dis(t)*dt(t) ]
        - v_salv * E(T)

c_deg (`degradation_cost_per_kwh`) mirrors network.py's own live LP
objective exactly -- added to BOTH charge and discharge cost terms,
zero by default (a complete no-op for any install that doesn't
configure it).

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
- No explicit capacity-fade / cycle-life CURVE (only a flat, linear
  $/kWh-throughput friction term, `degradation_cost_per_kwh` --
  identical in form to `c_chg`/`c_dis` and, since nimbus issue #336
  (Mark Purcell's live-dashboard finding, 2026-09-04), genuinely
  included in J below rather than silently dropped. Before that fix,
  this evaluator priced `c_chg`/`c_dis` only, while `network.py`'s own
  live LP additionally added `degradation_cost_per_kwh` to both -- a
  real install with that field configured nonzero (e.g. 3c/kWh) had its
  real dispatch optimizing against a DIFFERENT, more expensive J than
  the one this file's own regret/EPR numbers claimed to measure).
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
    cost_per_period: NDArray[np.float64]
    """Per-period contribution to total_cost, BEFORE the one-time
    salvage_value adjustment (that term applies once, to the final SoC,
    not to any single period -- attributing it to one arbitrary period
    would be misleading, so total_cost = sum(cost_per_period) -
    salvage_value * final_soc_kwh, not sum(cost_per_period) alone). This
    is what hourly_regret_breakdown() below bins to build an hourly
    picture -- added 2026-08-17 specifically to reproduce Mark Purcell's
    own "report regret hourly, never the ratio" chart pattern, which
    needs each trajectory's own per-period cost, not just its total.
    """


def _terminal_value_credit(
    final_soc_kwh: float,
    min_soc_kwh: float,
    breakpoints: list[tuple[float, float]],
) -> float:
    """Evaluate the SAME piecewise-linear concave terminal-value curve
    network.py's own LP applies (see network.py's own terminal-value
    comment block for the "why concave, why non-increasing rates"
    reasoning) -- but as a plain, non-LP arithmetic evaluation of one
    already-known final_soc_kwh value, for scoring a REAL, already-
    realized trajectory (this module has no LP of its own to build a
    segment variable into).

    Fills segments in order (lowest index first) up to final_soc_kwh -
    min_soc_kwh. Since network.py's own LP always prefers filling the
    highest-rate segment first when maximizing revenue under an equality
    fill constraint (guaranteed by BatteryConfig's own non-increasing-
    rate validation), this reproduces exactly the credit the LP itself
    would assign for the SAME final_soc_kwh, not an approximation of it.
    """
    remaining = max(0.0, final_soc_kwh - min_soc_kwh)
    credit = 0.0
    for width, rate in breakpoints:
        segment = min(width, remaining)
        credit += segment * rate
        remaining -= segment
        if remaining <= 0.0:
            break
    return credit


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
    terminal_value_breakpoints: list[tuple[float, float]] | None = None,
    battery_min_soc_kwh: float | None = None,
    degradation_cost_per_kwh: float = 0.0,
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

    terminal_value_breakpoints/battery_min_soc_kwh (2026-08-29, real fix
    for a genuinely invalid EPR found live -- a night where an incident
    left the real dispatch's own final_soc_kwh anomalously near-full):
    the flat `salvage_value * final_soc_kwh` credit below massively
    over-rewards an anomalously-full ending relative to what a perfect-
    foresight oracle -- correctly selling some of that energy for real
    money during the day instead of just holding it -- can ever match,
    since the oracle is scored the SAME way and has no reason to chase a
    flat-rate credit that exceeds real achievable spot/P2P prices. This
    let a real achieved trajectory beat even a fully unconstrained
    oracle at spot-only economics, which should be structurally
    impossible and is exactly what produced EPR > 100%. Mirrors
    network.py's own already-shipped concave terminal_value_breakpoints
    mechanism (2026-08-18) -- when given (non-None), this SAME curve
    (not a separate flat rate) prices final_soc_kwh here too, so the
    real-achieved and oracle evaluations are judged on an identical,
    already-diminishing-near-full curve instead of one side's own
    incidental full-battery ending being priced at a flat rate the LP
    itself would never actually pay. Both None (the default) is a
    complete no-op, byte-identical to every scenario/test predating this
    pair -- the flat `salvage_value * final_soc_kwh` term is unchanged.

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
    # nimbus issue #336 (Mark Purcell's live-dashboard finding,
    # 2026-09-04): degradation_cost_per_kwh added to BOTH charge and
    # discharge cost arrays, mirroring network.py's own live LP
    # objective exactly (`(charge_cost_arr[t] + battery.degradation_
    # cost_per_kwh) * hours[t]`, same for discharge) -- see this
    # function's own docstring and the module's "J, stated explicitly"
    # section for the full story. Zero by default: a complete no-op for
    # any install that doesn't configure this field.
    charge_cost_arr = (
        np.broadcast_to(np.asarray(charge_cost, dtype=np.float64), (n,))
        + degradation_cost_per_kwh
    )
    discharge_cost_arr = (
        np.broadcast_to(np.asarray(discharge_cost, dtype=np.float64), (n,))
        + degradation_cost_per_kwh
    )
    solar_used = solar_real_kw
    net_needed = (
        load_real_kw + charge_committed_kw - discharge_committed_kw - solar_used
    )
    grid_import = np.maximum(0.0, net_needed)
    grid_export = np.maximum(0.0, -net_needed)
    solar_curtailed = np.zeros_like(solar_real_kw)

    cost_per_period = (
        import_price_real * grid_import * hours
        - export_price_real * grid_export * hours
        + charge_cost_arr * charge_committed_kw * hours
        + discharge_cost_arr * discharge_committed_kw * hours
    )
    if terminal_value_breakpoints is not None:
        if battery_min_soc_kwh is None:
            msg = (
                "terminal_value_breakpoints was given but battery_min_soc_kwh is "
                "None -- the concave curve needs the real floor to evaluate against"
            )
            raise ValueError(msg)
        terminal_credit = _terminal_value_credit(
            final_soc_kwh, battery_min_soc_kwh, terminal_value_breakpoints
        )
    else:
        terminal_credit = salvage_value * final_soc_kwh
    cost = float(np.sum(cost_per_period) - terminal_credit)
    return RealizedCost(
        total_cost=cost,
        cost_per_period=cost_per_period,
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
    plan = build_plan(
        periods=periods, grid=grid, battery=battery, solar=solar, loads=[load]
    )
    if not plan.is_optimal:
        msg = f"Oracle solve failed (status={plan.status}) -- this should not happen with real, already-realized data unless the scenario is genuinely infeasible"
        raise RuntimeError(msg)
    return (
        plan.battery_charge_kw,
        plan.battery_discharge_kw,
        float(plan.battery_soc_kwh[-1]),
    )


def hourly_regret_breakdown(
    *,
    timestamps: list,
    actual_cost_per_period: NDArray[np.float64],
    oracle_cost_per_period: NDArray[np.float64],
) -> dict[int, float]:
    """Bins (actual - oracle) cost per period into 24 hour-of-day buckets --
    reproduces Mark Purcell's own regret chart (2026-08-17, "Rust = value
    left on the table. Teal = the optimum spending there to earn it back
    later. Report regret hourly, never the ratio."), which showed real,
    useful structure a single daily regret number hides entirely: which
    specific hours the real controller actually lost money in (a positive
    bucket -- "rust"), versus hours where the perfect-foresight optimum
    spent MORE than actual, deliberately, to set up a bigger saving later
    in the day (a negative bucket -- "teal"). A day can have a small net
    regret while still containing large, genuinely actionable hourly
    swings in both directions that cancel out in the daily total -- this
    is deliberately NOT just "total regret / 24", which would hide that
    structure completely.

    timestamps: one per period, same length/order as both cost arrays --
    only .hour is read from each, so any real datetime-like object works.
    actual_cost_per_period / oracle_cost_per_period: RealizedCost.
    cost_per_period from two evaluate_realized_cost() calls against the
    SAME realized ground truth (same load/solar/price), one for the
    actual/committed trajectory, one for oracle_dispatch()'s own -- the
    one-time salvage_value adjustment in each trajectory's own total_cost
    is deliberately excluded here (see RealizedCost.cost_per_period's own
    docstring for why), so summing every hour's bucket reproduces the
    day's regret EXCLUDING that one-time term, not the full total_cost
    difference -- a real, honest gap between this function's own sum and
    the "true" daily regret whenever salvage_value is nonzero, stated
    here explicitly rather than left to be discovered by a mismatched
    reconciliation later.

    Returns {hour: regret_dollars} for every hour 0-23 that has at least
    one period, positive = rust (actual worse than oracle that hour),
    negative = teal (actual better than oracle that hour, i.e. the
    oracle chose to spend more there). An hour with zero net difference
    is simply absent from the returned dict, not included as 0.0 --
    matches Mark's own chart, which shows no bar at all for a genuinely
    flat hour rather than a zero-height one.
    """
    if not (
        len(timestamps) == len(actual_cost_per_period) == len(oracle_cost_per_period)
    ):
        msg = "timestamps, actual_cost_per_period, and oracle_cost_per_period must be the same length"
        raise ValueError(msg)
    buckets: dict[int, float] = {}
    diff = actual_cost_per_period - oracle_cost_per_period
    for ts, d in zip(timestamps, diff, strict=True):
        buckets[ts.hour] = buckets.get(ts.hour, 0.0) + float(d)
    return {h: round(v, 6) for h, v in buckets.items() if abs(v) > 1e-9}
