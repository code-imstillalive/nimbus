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
absent: sheddable loads, adequacy loads, SoC-dependent power curves,
terminal_value_breakpoints (uses plain flat salvage_value only), and
every stability mechanism (proximal/max_rate/smoothness) build_plan()
has. If A2 is ever watched and trusted enough to become more than a
shadow-mode comparison, closing these gaps is real, separate follow-up
work, not something to assume already covered.

## P2P export commitment support (2026-08-31, devhub-only)

`fixed_export_kw` and the two-tier `export_bonus_price`/
`export_bonus_volume_kwh` mechanism (see `p2p_export.py`'s own module
docstring for the full real-household reasoning behind each) ARE now
supported here -- extracted verbatim from network.py's own real,
live-tested implementation into a separate shared module, specifically
so this module never has to touch network.py itself (real-money-adjacent
production code, re-solved every 5 minutes on NUC1/NUC2) to gain this
capability. Both mechanisms stay genuinely, independently optional --
`GridConfig.fixed_export_kw`/`export_bonus_price`/`export_bonus_volume_kwh`
all default to `None`, and every function in `p2p_export.py` is a
complete no-op when its own relevant field is `None` -- so this module
keeps working exactly as before for a household with no P2P plan at all,
and now ALSO reasons correctly about one with a fixed P2P commitment, per
the explicit household ask: "it should be smart to know how to balance it
with p2p in play as well as without it there at all... there will be a
variety of users... different plans different suppliers... the
integration must handle and allow for variables and various scenarios."

Deployment is deliberately devhub-only and fully reversible: this module
has zero callers in `nimbus_solver_forecast_writer.py` or any other
NUC1/NUC2-deployed script (confirmed by grep before this was built) --
the only real caller is `116KAT-HA-AI`'s own `scripts/
nimbus_stochastic_comparison_writer.py`, hardcoded against devhub's own
HA instance, never the NUC1/NUC2 VIP, writing a shadow-mode comparison
sensor only. Disabling this feature is a single action (stop that one
script's cron entry) -- network.py and every real NUC1/NUC2 automation
stay completely untouched regardless.

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

from . import p2p_export
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
    # Real, pre-existing gap closed 2026-08-31: this module previously
    # never exposed grid_import/grid_export at all, only battery
    # charge/discharge/soc -- meaning the P2P fixed_export_kw mechanism
    # (which pins grid_export[t] directly, not charge/discharge) would
    # have been unobservable from this result even once wired into the
    # LP. Same zero-filled-by-default convention as everything else here.
    stage1_grid_import_kw: NDArray[np.float64] = field(
        default_factory=lambda: np.array([])
    )
    stage1_grid_export_kw: NDArray[np.float64] = field(
        default_factory=lambda: np.array([])
    )
    stage2_grid_import_kw: list[NDArray[np.float64]] = field(default_factory=list)
    stage2_grid_export_kw: list[NDArray[np.float64]] = field(default_factory=list)
    # P2P export-bonus allocation (2026-08-31, see p2p_export.py's own
    # module docstring) -- zero-filled whenever GridConfig.export_bonus_*
    # isn't configured (the common case), same "represent honestly,
    # don't paper over" convention as network.py's own Plan.export_bonus_kw.
    stage1_export_bonus_kw: NDArray[np.float64] = field(
        default_factory=lambda: np.array([])
    )
    stage2_export_bonus_kw: list[NDArray[np.float64]] = field(default_factory=list)


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
    ) -> tuple[
        list[str],
        list[str],
        list[str],
        list[str],
        list[str],
        list[str],
        dict[int, str],
    ]:
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
        # P2P export commitment (2026-08-31, see p2p_export.py's own module
        # docstring) -- charge{suffix}[t]'s own ub and grid_export{suffix}[t]'s
        # own (lb, ub) both defer to grid.fixed_export_kw, exactly matching
        # network.py's own construction. Complete no-op (0.0/max_charge_kw
        # and (0.0, export_limit_kw), byte-identical to before this existed)
        # whenever grid.fixed_export_kw is None -- every scenario built
        # before this feature existed continues to work unchanged.
        charge = {
            t: p.add_variable(
                f"charge{suffix}_{t}",
                lb=0.0,
                ub=p2p_export.charging_ub_during_fixed_window(
                    t, grid, battery.max_charge_kw
                ),
            )
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
            t: p.add_variable(f"grid_export{suffix}_{t}", lb=lb, ub=ub)
            for t in t_range
            for lb, ub in [p2p_export.grid_export_bounds(t, grid, grid.export_limit_kw)]
        }
        solar_used = {
            t: p.add_variable(
                f"solar_used{suffix}_{t}", lb=0.0, ub=max(0.0, float(solar_kw[t]))
            )
            for t in t_range
        }

        # Two-tier export bonus (see p2p_export.py's own module docstring)
        # -- genuinely independent of fixed_export_kw above (a household
        # can have either, both, or neither configured). Empty dict (a
        # complete no-op everywhere below) whenever grid.export_bonus_*
        # isn't configured.
        has_bonus = p2p_export.has_export_bonus(grid)
        export_bonus = (
            {
                t: p2p_export.add_export_bonus_variable(
                    p, f"export_bonus{suffix}_{t}", grid.export_limit_kw
                )
                for t in t_range
            }
            if has_bonus
            else {}
        )

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
            if has_bonus:
                p2p_export.set_export_bonus_cost(
                    p, export_bonus[t], t, grid, hours, weight=weight
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
            if has_bonus:
                p2p_export.add_export_bonus_le_export_constraint(
                    p, export_bonus[t], grid_export[t]
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

        # Two-tier export bonus cumulative cap + tie-breaker (see
        # p2p_export.py's own module docstring) -- one call per family,
        # scoped to THIS family's own real period range only. `label=suffix`
        # gives each scenario's own cap row a distinct, readable NAME (e.g.
        # "export_bonus_cap_2026-08-20_s0" vs "..._s1") since every scenario
        # shares the same stage2_range and would otherwise all propose the
        # same name -- each scenario's own real LP variables are already
        # distinct (export_bonus_s0_* vs export_bonus_s1_*), so a name
        # collision alone can NEVER leak volume between scenarios (see
        # p2p_export.py's own add_export_bonus_cumulative_caps docstring --
        # lp.py's constraint names are purely for readability/dual-value
        # lookup, never required for correctness); `label` only prevents
        # one scenario's own shadow price from silently shadowing another's
        # in LPResult.duals. No-op (export_bonus is an empty dict) whenever
        # has_bonus is False.
        #
        # nimbus issue #354 (Mark Purcell): scoping this call to ONLY this
        # family's own t_range means a real calendar day that stage 1's
        # own periods and a scenario's stage-2 periods both fall on (the
        # day the stochastic_start_period branch point sits inside) gets
        # capped TWICE, independently, at the FULL real daily volume each
        # time -- a single scenario-world could then plan as if 2x the
        # real committed daily volume were available on that one day.
        # This per-family call is kept AS-IS deliberately (it's not wrong
        # on its own, and its own tie-breaker cost term is genuinely
        # per-family-correct) -- the real fix is the SUPPLEMENTARY,
        # cross-family constraint added once per scenario after both
        # stages are built below, which is the one that actually binds
        # the true combined volume on the shared day. See that section's
        # own comment for why a second, ADDITIVE constraint here (rather
        # than restructuring this call) is the safe way to close the gap
        # without also double-applying the tie-breaker's own cost term
        # (LPProblem.set_cost() is additive, not overwriting -- calling
        # add_export_bonus_cumulative_caps() with stage-1's shared
        # variables folded into more than one scenario's own call would
        # silently multiply their tie-break cost by the scenario count).
        if has_bonus:
            p2p_export.add_export_bonus_cumulative_caps(
                p, export_bonus, periods, grid, label=suffix
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
            [grid_import[t] for t in t_range],
            [grid_export[t] for t in t_range],
            [export_bonus[t] for t in t_range] if has_bonus else [],
            # nimbus issue #354: the raw {period_index: var_name} dict
            # (not just the list above) -- needed below to build the
            # cross-stage supplementary cap constraint, which must know
            # each variable's own real, absolute period index to group
            # correctly by real calendar day alongside stage 1's own.
            dict(export_bonus) if has_bonus else {},
        )

    stage1_range = range(stochastic_start_period)
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

    stage2_names: list[
        tuple[
            list[str],
            list[str],
            list[str],
            list[str],
            list[str],
            list[str],
            dict[int, str],
        ]
    ] = []
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

    # nimbus issue #354 (Mark Purcell): supplementary cross-stage P2P
    # export-bonus cap. Stage 1 and a given scenario's own stage 2 are
    # adjacent, non-overlapping period ranges -- the only real calendar
    # day they can ever share is the one containing the
    # stochastic_start_period boundary itself (or none at all, if the
    # branch happens to fall exactly at a real day boundary). The
    # per-family add_export_bonus_cumulative_caps() calls above each cap
    # ONLY their own half of that shared day at the FULL real daily
    # volume -- independently, so a single scenario-world could plan as
    # if stage-1's own (real, already-committed) volume for that day AND
    # a full separate allocation for its own stage-2 portion were both
    # available, effectively doubling the real cap on that one day. This
    # adds the ONE constraint actually missing: stage 1's real volume for
    # that day PLUS this scenario's own stage-2 volume for the same day,
    # bound to the real configured cap -- additive to (not a replacement
    # for) the per-family calls above, which is deliberate: see
    # _add_period_vars_and_constraints' own comment on why folding
    # stage-1's shared variables into more than one scenario's own
    # add_export_bonus_cumulative_caps() call would silently multiply
    # their tie-breaker cost (LPProblem.set_cost() is additive) instead
    # of just adding a redundant-but-harmless extra volume constraint.
    has_bonus = p2p_export.has_export_bonus(grid)
    if has_bonus and stage1_names is not None:
        stage1_bonus_dict = stage1_names[6]

        def _day_key(t: int) -> object:
            starts = periods.period_starts
            return starts[t].date() if starts is not None else None

        stage1_days: dict[object, list[int]] = {}
        for t in stage1_bonus_dict:
            stage1_days.setdefault(_day_key(t), []).append(t)

        for s in range(n_scenarios):
            stage2_bonus_dict = stage2_names[s][6]
            stage2_days: dict[object, list[int]] = {}
            for t in stage2_bonus_dict:
                stage2_days.setdefault(_day_key(t), []).append(t)

            for day_key in set(stage1_days) & set(stage2_days):
                combined_vars = {t: stage1_bonus_dict[t] for t in stage1_days[day_key]}
                combined_vars.update(
                    {t: stage2_bonus_dict[t] for t in stage2_days[day_key]}
                )
                terms = {
                    var_name: periods.hours[t] for t, var_name in combined_vars.items()
                }
                day_label = day_key.isoformat() if day_key is not None else "global"
                p.add_ub_constraint(
                    terms,
                    float(grid.export_bonus_volume_kwh),
                    name=f"export_bonus_cap_branchday_{day_label}_s{s}",
                )

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
    stage1_grid_import = _extract(stage1_names[3]) if stage1_names else np.array([])
    stage1_grid_export = _extract(stage1_names[4]) if stage1_names else np.array([])
    stage1_export_bonus = _extract(stage1_names[5]) if stage1_names else np.array([])

    stage2_charge_all, stage2_discharge_all, stage2_soc_all = [], [], []
    stage2_grid_import_all: list[NDArray[np.float64]] = []
    stage2_grid_export_all: list[NDArray[np.float64]] = []
    stage2_export_bonus_all: list[NDArray[np.float64]] = []
    for s in range(n_scenarios):
        c_names, d_names, soc_names, gi_names, ge_names, bonus_names, _bonus_dict = (
            stage2_names[s]
        )
        stage2_charge_all.append(_extract(c_names))
        stage2_discharge_all.append(_extract(d_names))
        stage2_soc_all.append(_extract(soc_names))
        stage2_grid_import_all.append(_extract(gi_names))
        stage2_grid_export_all.append(_extract(ge_names))
        stage2_export_bonus_all.append(_extract(bonus_names))

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
        stage1_grid_import_kw=stage1_grid_import,
        stage1_grid_export_kw=stage1_grid_export,
        stage2_grid_import_kw=stage2_grid_import_all,
        stage2_grid_export_kw=stage2_grid_export_all,
        stage1_export_bonus_kw=stage1_export_bonus,
        stage2_export_bonus_kw=stage2_export_bonus_all,
    )
