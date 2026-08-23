"""Track A2 -- a genuine two-stage stochastic LP for battery dispatch
under solar uncertainty. Deliberately a SEPARATE module from network.py,
NOT a modification of build_plan() -- see this repo's own commit history
for the full reasoning (build_plan() is 700+ lines of real-money-
adjacent production code, re-solved every 5 minutes; a feature the
plan itself scoped as "opt-in, side-by-side on the shadow-mode chart,
never the default until watched" doesn't belong woven through it).

Deliberately scoped v1, matching the plan's own "battery + solar
uncertainty only" staging -- NOT a general replacement for build_plan().
Missing on purpose, all real, all documented here rather than silently
absent: sheddable loads, adequacy loads, the two-tier P2P export bonus,
fixed_export_kw, SoC-dependent power curves, terminal_value_breakpoints
(uses plain flat salvage_value only), and every stability mechanism
(proximal/max_rate/smoothness) build_plan() has. If A2 is ever watched
and trusted enough to become more than a shadow-mode comparison, closing
these gaps is real, separate follow-up work, not something to assume
already covered.

## Why a real two-stage structure, not "solve twice"

A cheaper-looking alternative -- solve the near-term deterministically,
then solve each scenario's own continuation separately -- is NOT real
stochastic programming: the near-term (stage 1) decision would be made
completely blind to what might happen later, which defeats the entire
point (a stage-1 decision that HEDGES against plausible futures,
weighted by how likely each one is). This module instead builds ONE
joint LP: stage-1 variables are genuinely SHARED across every scenario
(one set, not one per scenario), and stage-2 variables are genuinely
scenario-indexed (one full set per scenario, continuing from the SAME
shared stage-1 ending point). The LP's own objective is the WEIGHTED
SUM of every scenario's own stage-2 cost plus the shared stage-1 cost --
i.e. the real expected total cost -- so minimizing it naturally finds
the stage-1 decision that's genuinely good on average across every
scenario, not just the one the deterministic solver happened to assume.

## Structure

- Periods [0, stochastic_start_period) are STAGE 1 -- one set of
  variables, built and costed exactly once, using solar_scenarios[0]'s
  own values for this range (validated: every scenario must agree here,
  since branching hasn't happened yet -- this isn't a real restriction,
  it reflects reality: the near future isn't uncertain in the same way
  the far future is).
- Periods [stochastic_start_period, n) are STAGE 2 -- one FULL set of
  variables PER SCENARIO, each continuing from soc[stochastic_start_
  period - 1] (the shared stage-1 ending SoC) via that scenario's own
  first SoC-dynamics equation -- no separate linking constraint needed,
  referencing the shared variable name IS the link.
- Objective = stage-1 cost (unweighted, it only happens once) + sum over
  scenarios of (scenario_weight * that scenario's own stage-2 cost).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from .elements import BatteryConfig, GridConfig, PeriodGrid
from .lp import LPProblem


@dataclass(frozen=True)
class StochasticPlan:
    """Real, inspectable result -- stage-1 dispatch (shared, what
    actually gets committed to right now) plus per-scenario stage-2
    dispatch (what each branch WOULD do if that future materialized),
    for a genuine side-by-side comparison against build_plan()'s own
    single-scenario forecast."""

    status: str
    expected_total_cost: float
    stage1_charge_kw: NDArray[np.float64]
    stage1_discharge_kw: NDArray[np.float64]
    stage1_soc_kwh: NDArray[np.float64]
    # One entry per scenario -- stage2_charge_kw[s] is that scenario's
    # own dispatch array for periods [stochastic_start_period, n).
    stage2_charge_kw: list[NDArray[np.float64]] = field(default_factory=list)
    stage2_discharge_kw: list[NDArray[np.float64]] = field(default_factory=list)
    stage2_soc_kwh: list[NDArray[np.float64]] = field(default_factory=list)
    scenario_cost: list[float] = field(default_factory=list)


def build_stochastic_plan(
    *,
    periods: PeriodGrid,
    grid: GridConfig,
    battery: BatteryConfig,
    solar_scenarios: list[NDArray[np.float64]],
    scenario_weights: list[float],
    stochastic_start_period: int,
    load_kw: NDArray[np.float64] | None = None,
) -> StochasticPlan:
    """Build and solve one genuine two-stage stochastic LP. Pure
    function, same discipline as build_plan() -- no I/O, no HA
    dependency, safe to call from anywhere including a plain local test.

    `solar_scenarios`: one full-horizon (length n) array per scenario --
    scenario 0's own values are used for every period BEFORE
    stochastic_start_period (validated: every scenario must agree there,
    see this module's own docstring). `scenario_weights` must sum to
    1.0 (validated) -- the real, honest probability each scenario is
    meant to represent, not an arbitrary blend.

    `load_kw`, if given, is a single DETERMINISTIC array (not scenario-
    varying) applied identically in every scenario -- this v1 only
    models genuine per-scenario UNCERTAINTY for solar, matching the real
    confidence-band data this household's own A0/A1 work already earns
    (see elements.py's own SolarConfig docstring). Defaults to zero
    (battery+grid+solar only, no load) if not given.
    """
    n = periods.n_periods
    hours = periods.hours
    n_scenarios = len(solar_scenarios)

    if n_scenarios < 2:
        msg = f"build_stochastic_plan needs at least 2 scenarios (got {n_scenarios}) -- use build_plan() directly for a single-scenario solve"
        raise ValueError(msg)
    if len(scenario_weights) != n_scenarios:
        msg = f"scenario_weights has {len(scenario_weights)} entries, expected {n_scenarios} (one per scenario)"
        raise ValueError(msg)
    if abs(sum(scenario_weights) - 1.0) > 1e-6:
        msg = f"scenario_weights must sum to exactly 1.0 (got {sum(scenario_weights)}) -- these are real probabilities, not an arbitrary blend"
        raise ValueError(msg)
    for i, sc in enumerate(solar_scenarios):
        if len(sc) != n:
            msg = f"solar_scenarios[{i}] has {len(sc)} periods, expected {n} (PeriodGrid mismatch)"
            raise ValueError(msg)
    if not (0 <= stochastic_start_period < n):
        # Strict < n, not <= -- stage 2 must always have at least one
        # real period. stochastic_start_period == n would mean zero
        # stochastic periods at all (a pointless call -- use build_plan()
        # directly), and would leave stage2_range empty, which the
        # terminal-value code above assumes never happens (t_range[-1]
        # on an empty range raises IndexError, not a clean ValueError).
        msg = f"stochastic_start_period ({stochastic_start_period}) must be within [0, {n}) -- stage 2 needs at least 1 real period"
        raise ValueError(msg)
    for i in range(1, n_scenarios):
        pre_branch = solar_scenarios[i][:stochastic_start_period]
        anchor = solar_scenarios[0][:stochastic_start_period]
        if not np.allclose(pre_branch, anchor):
            msg = (
                f"solar_scenarios[{i}] disagrees with solar_scenarios[0] before "
                f"stochastic_start_period ({stochastic_start_period}) -- every "
                "scenario must share identical values for periods that haven't "
                "branched yet, real uncertainty only exists from the branch "
                "point onward"
            )
            raise ValueError(msg)

    load = load_kw if load_kw is not None else np.zeros(n)
    if len(load) != n:
        msg = f"load_kw has {len(load)} periods, expected {n}"
        raise ValueError(msg)

    p = LPProblem()
    charge_cost_arr = np.broadcast_to(
        np.asarray(battery.charge_cost, dtype=np.float64), (n,)
    )
    discharge_cost_arr = np.broadcast_to(
        np.asarray(battery.discharge_cost, dtype=np.float64), (n,)
    )

    def _add_period_vars_and_constraints(
        suffix: str,
        t_range: range,
        solar_kw: NDArray[np.float64],
        weight: float,
        prev_soc_ref: str | float,
        apply_terminal_value: bool,
    ) -> tuple[list[str], list[str], list[str]]:
        """Shared helper -- builds one family of charge/discharge/soc/
        grid_import/grid_export variables + their SoC-dynamics/balance/
        wash-trade constraints for the given period range, at the given
        objective weight. Used once for stage 1 (weight=1.0, suffix="")
        and once per scenario for stage 2 (weight=scenario_weights[s],
        suffix=f"_s{s}"). `prev_soc_ref` is either battery.initial_soc_kwh
        (a real float, only for stage 1's own very first period) or the
        NAME of the variable holding the previous period's SoC (a string
        -- either the previous period in this same family, or, for
        stage 2's own first period, the SHARED stage-1 variable name --
        this is the real linking mechanism, no extra constraint needed.

        `apply_terminal_value`: real bug found and fixed while verifying
        this module against a hand-designed hedging scenario -- salvage_
        value must ONLY apply at the TRUE horizon end (stage 2's own
        final period, once per scenario), never at stage 1's own
        boundary period. Stage 1's last period is an INTERMEDIATE point
        in the real horizon, not a genuine end -- crediting it there
        would be a false "the horizon stops here" signal that doesn't
        reflect what's actually being modeled. Harmless when salvage_
        value happens to be 0.0 (as in this module's own verification
        scenario), but a real, silent bug for any nonzero value."""
        charge = {
            t: p.add_variable(f"charge{suffix}_{t}", lb=0.0, ub=battery.max_charge_kw)
            for t in t_range
        }
        discharge = {
            t: p.add_variable(
                f"discharge{suffix}_{t}", lb=0.0, ub=battery.max_discharge_kw
            )
            for t in t_range
        }
        soc = {
            t: p.add_variable(
                f"soc{suffix}_{t}", lb=battery.min_soc_kwh, ub=battery.max_soc_kwh
            )
            for t in t_range
        }
        grid_import = {
            t: p.add_variable(
                f"grid_import{suffix}_{t}", lb=0.0, ub=grid.import_limit_kw
            )
            for t in t_range
        }
        grid_export = {
            t: p.add_variable(
                f"grid_export{suffix}_{t}", lb=0.0, ub=grid.export_limit_kw
            )
            for t in t_range
        }
        solar_used = {
            t: p.add_variable(
                f"solar_used{suffix}_{t}", lb=0.0, ub=max(0.0, float(solar_kw[t]))
            )
            for t in t_range
        }

        for t in t_range:
            p.set_cost(grid_import[t], weight * float(grid.import_price[t]) * hours[t])
            p.set_cost(grid_export[t], -weight * float(grid.export_price[t]) * hours[t])
            p.set_cost(
                charge[t],
                weight
                * (float(charge_cost_arr[t]) + battery.degradation_cost_per_kwh)
                * hours[t],
            )
            p.set_cost(
                discharge[t],
                weight
                * (float(discharge_cost_arr[t]) + battery.degradation_cost_per_kwh)
                * hours[t],
            )

        for t in t_range:
            terms = {
                soc[t]: 1.0,
                charge[t]: -battery.charge_efficiency * hours[t],
                discharge[t]: hours[t] / battery.discharge_efficiency,
            }
            if t == t_range.start:
                if isinstance(prev_soc_ref, str):
                    terms[prev_soc_ref] = -1.0
                    p.add_eq_constraint(terms, 0.0)
                else:
                    p.add_eq_constraint(terms, prev_soc_ref)
            else:
                terms[soc[t - 1]] = -1.0
                p.add_eq_constraint(terms, 0.0)

        for t in t_range:
            p.add_eq_constraint(
                {
                    solar_used[t]: 1.0,
                    discharge[t]: 1.0,
                    grid_import[t]: 1.0,
                    charge[t]: -1.0,
                    grid_export[t]: -1.0,
                },
                float(load[t]),
            )
            # Same-period wash-trade prevention (see network.py's own
            # docstring for the full "two independent pathways" finding
            # -- both required, replicated here identically).
            p.add_ub_constraint(
                {grid_export[t]: 1.0, solar_used[t]: -1.0, discharge[t]: -1.0}, 0.0
            )
            draw_coeff = hours[t] / battery.discharge_efficiency
            if t == t_range.start:
                if isinstance(prev_soc_ref, str):
                    p.add_ub_constraint(
                        {discharge[t]: draw_coeff, prev_soc_ref: -1.0},
                        -battery.min_soc_kwh,
                    )
                else:
                    p.add_ub_constraint(
                        {discharge[t]: draw_coeff}, prev_soc_ref - battery.min_soc_kwh
                    )
            else:
                p.add_ub_constraint(
                    {discharge[t]: draw_coeff, soc[t - 1]: -1.0}, -battery.min_soc_kwh
                )

        # Terminal value ONLY at the true horizon end (apply_terminal_
        # value=True, stage 2 only) -- plain flat salvage_value (see this
        # module's own docstring for the real, stated "not terminal_
        # value_breakpoints" v1 simplification).
        if apply_terminal_value:
            last_t = t_range[-1]
            p.set_cost(soc[last_t], -weight * battery.salvage_value)

        return (
            [charge[t] for t in t_range],
            [discharge[t] for t in t_range],
            [soc[t] for t in t_range],
        )

    stage1_range = range(0, stochastic_start_period)
    stage1_names = None
    if stochastic_start_period > 0:
        stage1_names = _add_period_vars_and_constraints(
            "",
            stage1_range,
            solar_scenarios[0],
            weight=1.0,
            prev_soc_ref=battery.initial_soc_kwh,
            apply_terminal_value=False,
        )

    stage2_names: list[tuple[list[str], list[str], list[str]]] = []
    stage2_range = range(stochastic_start_period, n)
    for s in range(n_scenarios):
        if stochastic_start_period > 0:
            prev_ref: str | float = stage1_names[2][
                -1
            ]  # shared stage-1 final soc variable name
        else:
            prev_ref = battery.initial_soc_kwh
        names = _add_period_vars_and_constraints(
            f"_s{s}",
            stage2_range,
            solar_scenarios[s],
            weight=scenario_weights[s],
            prev_soc_ref=prev_ref,
            apply_terminal_value=True,
        )
        stage2_names.append(names)

    result = p.solve()

    if result.status != "optimal":
        return StochasticPlan(
            status=result.status,
            expected_total_cost=float("nan"),
            stage1_charge_kw=np.array([]),
            stage1_discharge_kw=np.array([]),
            stage1_soc_kwh=np.array([]),
        )

    def _extract(names: list[str]) -> NDArray[np.float64]:
        return p.values_of(result, names) if names else np.array([])

    stage1_charge = _extract(stage1_names[0]) if stage1_names else np.array([])
    stage1_discharge = _extract(stage1_names[1]) if stage1_names else np.array([])
    stage1_soc = _extract(stage1_names[2]) if stage1_names else np.array([])

    stage2_charge_all, stage2_discharge_all, stage2_soc_all = [], [], []
    for s in range(n_scenarios):
        c_names, d_names, soc_names = stage2_names[s]
        stage2_charge_all.append(_extract(c_names))
        stage2_discharge_all.append(_extract(d_names))
        stage2_soc_all.append(_extract(soc_names))

    return StochasticPlan(
        status=result.status,
        expected_total_cost=float(result.objective),
        stage1_charge_kw=stage1_charge,
        stage1_discharge_kw=stage1_discharge,
        stage1_soc_kwh=stage1_soc,
        stage2_charge_kw=stage2_charge_all,
        stage2_discharge_kw=stage2_discharge_all,
        stage2_soc_kwh=stage2_soc_all,
        scenario_cost=[],
    )
