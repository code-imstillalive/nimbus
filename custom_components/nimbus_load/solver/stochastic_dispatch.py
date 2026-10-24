"""Two-stage stochastic dispatch inside Nimbus's main solve (nimbus #1797).

`solver/stochastic.py` (22 Aug) is a separate, battery-and-solar-only LP with
no caller in the integration. This module instead runs the REAL model --
`network.build_plan()`, every feature it has -- once per solar scenario, all
in one joint problem, and ties the scenarios' near-term decisions together.

## The model

- **Scenarios.** Solar p10 / p50 / p90, from `SolarConfig.lower_kw` /
  `forecast_kw` / `upper_kw`, weighted 0.3 / 0.4 / 0.3 (Swanson's rule,
  `SCENARIO_WEIGHTS`). Everything else (prices, loads, batteries, P2P blocks,
  Charge By, reserve, controllable and thermal loads, stability terms,
  binaries) is the same in every scenario. No band, or a band of zero width
  once the rules below are applied, and this module does nothing: the caller
  keeps the deterministic plan.
- **One joint LP/MIP.** `build_plan()` builds the full deterministic model for
  the p50 scenario; `lp_solve_hook` (network.py) hands that built problem to
  `_JointSolve` instead of solving it. `_JointSolve` builds the p10 and p90
  models through the same `_build_plan_once()`, with the same per-call flags
  (thermal deadline, direction binaries), captures each problem unsolved, and
  absorbs all three into one `LPProblem`: p50 under its own names, p10/p90
  under a `p10::` / `p90::` prefix (`LPProblem.absorb()`). No model code is
  copied, so no feature can be missing from a scenario.
- **Objective.** Probability-weighted: p50's costs at 1, each other
  scenario's at `w_s / w_p50`. That is the expected cost divided by
  `w_p50`, so the minimiser is the expected-cost minimiser, and p50's own
  rows keep their own units: `Plan.total_cost`, the per-period duals and the
  published plan rows read as p50's. `LPProblem` has no constant term, so
  there is nothing to weight twice. The expected cost reported is
  `sum_s w_s * cost_s`, each `cost_s` the scenario's own primary objective
  at its own (unscaled) prices.
- **Non-anticipativity.** For every period in stage 1, `[0, K)`, each
  scenario's decisions equal p50's: battery charge/discharge (every
  battery), grid import/export (including #390's emergency import), solar
  use, every sheddable/adequacy/thermal load's power, and the adequacy and
  direction binaries (`stage1_decision_names()`). Solar is the p50 forecast
  in every scenario for t < K too, so stage 1 is genuinely one decision on
  one set of inputs, and the states (SoC, tank temperature) agree by
  construction rather than by constraint.
- **K = the periods that end inside the first 30 minutes**
  (`STAGE1_MINUTES`), at least one. Why 30 and not 1: with K = 1 the model
  assumes that one minute from now it will know which solar day it is in,
  and can re-plan for that day for free. It cannot: the solar forecasts
  Nimbus reads update on a 30-minute grid, and a cloud day is not revealed
  in a minute. The stage-1 decision must hold until new information can
  arrive, or the hedge collapses into "decide as p50, fix it later" and the
  model reproduces the deterministic answer. Longer than 30 minutes and the
  plan stops adapting even after the information exists (a robust plan, not
  a two-stage one). On the reference household's tiered grid (3 x 1 min,
  10 x 5 min, then 30 min) K = 8: the periods to minute 28.
- **Output.** The published plan is the p50 scenario's trajectory; its
  period 0 is the shared stage-1 decision, the thing dispatched.

## Two rules applied to the band before it becomes scenarios

1. **No sun, no spread.** Where the p50 forecast is below `NO_SUN_KW`, every
   scenario is the p50 value. Measured on the reference household's live
   inputs (16 Oct 2026, 14:37 AEST): `lower_kw` was 0 in every period and
   `upper_kw` reached 6-17 kW at midnight on every night of the horizon,
   because the band is the min/max envelope across every solar source plus
   half their spread (`solver_inputs/solar.py`), not a percentile. A p90
   scenario with 15 kW of solar at 3 am would hedge against a sunrise that
   cannot happen.
2. **The band brackets the forecast.** `lower <= forecast <= upper`, clipped
   at zero, as `SolarConfig` already requires.

Note what rule 1 does not fix: where there IS sun the envelope is still an
envelope (p10 = 0, upper ~3-4x p50 on 16 Oct), so the "p10/p90" labels are
nominal on that install. This module reads the band it is given; whether
that band is a fair p10/p90 is the Forecaster's question, not this one's.

## What this deliberately does not do

- **No lex / calibrated phases** on the joint problem (`joint_options()`):
  one blended solve at the deterministic solve's calibrated weight. On
  devhub's test controllable loads (4,128 joint binaries) the calibrated
  phase 2 timed out and lp.py logged its #773 ERROR (nimbus #1805); it would
  also have started lp.py's module-wide calibration cooldown, skipping the
  LIVE solve's calibration for 5 minutes. Measured on the reference
  household's live inputs (two cycles, five starting SoCs each): period 0
  identical to the calibrated joint solve in 10 of 10, stage 1 in 9 of 10
  (one tie-break 4.4 kW apart at 98% SoC), expected cost $0.0006-0.0033
  higher, solve 0.1-0.5 s faster; with two deferrable loads (a MIP)
  11.2 s / 14.2 s against 20.6 s calibrated. The joint problem is also
  `quiet_diagnostics`: its timeout is one WARNING from the caller, with a
  `fallback_reason`, not lp.py's diagnostics or a model dump.
- **No ranging, no offer curve** on the joint problem. Sweeping one
  scenario's period-0 price would price the curve at `w_p50` of the real
  change. The caller keeps the deterministic plan's signals.
- **No solar risk aversion.** The scenarios carry no band of their own, so
  `risk_aversion` (`_risk_adjusted`, network.py) is a no-op inside them: the
  scenarios ARE the solar uncertainty, and pessimism on top would count it
  twice. `risk_aversion` has no other application site -- plain and
  sheddable loads plan on the point forecast (network.py, since 12 Oct) --
  so with the stochastic plan driving, the household's "Solar forecast risk"
  dial does nothing, and the summary says so (`solar_risk_aversion`). The
  import / export price dials are a separate, one-sided price adjustment and
  still apply in every scenario: price is not a scenario here.
- **Load and price uncertainty** are not scenarios. Solar only, as #1797
  scoped it.

## Shadow prices of a joint solve

The joint problem holds one balance row per scenario per period, and p50's
own row dual is not the plan's marginal: in stage 1 the non-anticipativity
rows pass the other scenarios' marginals onto it (measured: 2.5x the
expected marginal at period 0). Every dual and reduced cost the joint solve
returns is therefore rewritten to the EXPECTED marginal before anything reads
it (`_expected_marginals()`): for a row or variable `n`,
`w_p50 * (d[n] + sum_s d[s::n])`, which is `sum_s w_s * lambda_s` in each
scenario's own units. Every consumer -- `energy_shadow_price_now`, the
adequacy marginal cost, the guard's shadow-price series, flex telemetry, the
forced-import reduced costs -- then reads the stochastic plan's marginal
value of energy in the same units as a deterministic plan's.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any

import numpy as np
from numpy.typing import NDArray

from . import network
from .elements import BatteryConfig, SolarConfig
from .lp import BlendedOptions, LPProblem, LPResult

_LOGGER = logging.getLogger(__name__)

# Swanson's rule: p10 / p50 / p90 at 0.3 / 0.4 / 0.3. Index 1 (p50) is the
# anchor, the scenario whose trajectory is published.
SCENARIO_LABELS: tuple[str, str, str] = ("p10", "p50", "p90")
SCENARIO_WEIGHTS: tuple[float, float, float] = (0.3, 0.4, 0.3)
ANCHOR_INDEX = 1
# Stage 1 is every period that ends within this many minutes of now. See the
# module docstring for why 30.
STAGE1_MINUTES = 30.0
# Below this p50 solar (kW), every scenario is the p50 value: night.
NO_SUN_KW = 0.05
# HiGHS time limit on each joint solve call. A slower joint solve comes back
# non-optimal and the caller falls back to the deterministic plan.
JOINT_TIME_LIMIT_SECONDS = 20.0
# A joint MIP that hits the time limit is kept when its incumbent is within
# this relative gap of proven optimal (0.1%). Measured on devhub: 3.7e-5 at
# 20 s (#1805). A wider gap still falls back to the deterministic plan.
JOINT_ACCEPT_MIP_GAP = 1e-3
# Largest |x - x'| over the non-anticipativity pairs that still counts as one
# shared stage-1 decision (kW / binary). Above it the solution does not honour
# its own constraint and is refused.
NONANTICIPATIVITY_TOLERANCE = 1e-5
_SCENARIO_SEPARATOR = "::"
_CAPTURED = "captured_for_stochastic_joint_solve"


def stage1_period_count(
    hours: NDArray[np.float64], minutes: float = STAGE1_MINUTES
) -> int:
    """K: how many periods end within `minutes` of the horizon's start (at
    least 1, at most every period)."""
    n = len(hours)
    if n == 0:
        return 0
    ends_min = np.cumsum(np.asarray(hours, dtype=np.float64)) * 60.0
    k = int(np.searchsorted(ends_min, minutes + 1e-9, side="right"))
    return max(1, min(k, n))


@dataclass(frozen=True)
class SolarScenarios:
    """The three scenario solar arrays (p10, p50, p90) and what the band
    rules changed."""

    solar: tuple[SolarConfig, SolarConfig, SolarConfig]
    no_sun_periods: int
    # "calibrated" when the scenarios came from #1807's measured p10 / p90,
    # "envelope" when they fell back to lower_kw / upper_kw.
    band: str = "envelope"


def _band(solar: SolarConfig) -> tuple[Any, Any, str]:
    """#1807's calibrated p10 / p90 when the solve has them, else the
    envelope. The envelope is the min/max across sources plus half their
    spread: 0 in every period and 6-17 kW at midnight on 16 Oct, so its p10
    scenario was "no sun all day". `getattr` so this reads correctly before
    and after SolarConfig carries the fields."""
    p10 = getattr(solar, "p10_kw", None)
    p90 = getattr(solar, "p90_kw", None)
    if p10 is not None and p90 is not None:
        return p10, p90, "calibrated"
    return solar.lower_kw, solar.upper_kw, "envelope"


def _bandless(solar: SolarConfig, forecast: NDArray[np.float64]) -> SolarConfig:
    """`solar` with `forecast` and no band of either kind: a scenario's
    forecast sits on the band's edge, and SolarConfig validates the band
    against the forecast."""
    extra: dict[str, Any] = {}
    if hasattr(solar, "p10_kw"):
        extra = {"p10_kw": None, "p90_kw": None}
    return replace(solar, forecast_kw=forecast, lower_kw=None, upper_kw=None, **extra)


def solar_scenarios(solar: SolarConfig, k: int) -> SolarScenarios | None:
    """p10/p50/p90 bandless `SolarConfig`s, or None when there is no band
    (lower or upper missing) or no spread survives the rules in the module
    docstring -- the no-op case."""
    band_lo, band_hi, band = _band(solar)
    if band_lo is None or band_hi is None:
        return None
    f = np.asarray(solar.forecast_kw, dtype=np.float64)
    lo = np.maximum(np.minimum(np.asarray(band_lo, dtype=np.float64), f), 0.0)
    hi = np.maximum(np.asarray(band_hi, dtype=np.float64), f)
    no_sun = f < NO_SUN_KW
    lo = np.where(no_sun, f, lo)
    hi = np.where(no_sun, f, hi)
    # Stage 1 shares one solar value: p50.
    lo[:k] = f[:k]
    hi[:k] = f[:k]
    if np.allclose(lo, f) and np.allclose(hi, f):
        return None
    return SolarScenarios(
        solar=(
            _bandless(solar, lo),
            _bandless(solar, f.copy()),
            _bandless(solar, hi),
        ),
        no_sun_periods=int(
            np.count_nonzero(
                no_sun[k:] & (np.asarray(band_hi, dtype=np.float64)[k:] > f[k:])
            )
        ),
        band=band,
    )


def stage1_decision_names(
    p: LPProblem,
    k: int,
    *,
    batteries: list[BatteryConfig],
    load_names: list[str],
) -> list[str]:
    """Every stage-1 decision variable `p` actually has, for t in [0, k).

    Built from the exact name templates network.py registers, not parsed out
    of variable names (a load called `pump_2` would make a parsed trailing
    integer ambiguous). A name the problem does not have (a direction binary
    on a period that cannot carry both directions, an adequacy binary under
    a continuous relaxation) is simply skipped."""
    templates = [
        "grid_import_{t}",
        "grid_export_{t}",
        "grid_import_excess_{t}",
        "solar_used_{t}",
        "grid_dir_{t}",
    ]
    for b in batteries:
        templates += [
            f"battery_charge_{b.name}_{{t}}",
            f"battery_discharge_{b.name}_{{t}}",
            f"battery_dir_{b.name}_{{t}}",
        ]
    for name in load_names:
        templates += [
            f"shed_{name}_{{t}}",
            f"adequacy_{name}_{{t}}",
            f"adequacy_on_{name}_{{t}}",
            f"adequacy_start_{name}_{{t}}",
            f"thermal_power_{name}_{{t}}",
        ]
    out: list[str] = []
    for t in range(k):
        for tmpl in templates:
            name = tmpl.format(t=t)
            if p.has_variable(name):
                out.append(name)
    return out


@dataclass
class _Capture:
    """An `lp_solve_hook` that keeps the built problem and solves nothing."""

    problem: LPProblem | None = None

    def __call__(
        self,
        p: LPProblem,
        solve_kwargs: dict[str, Any],
        *,
        thermal_hard_deadline: bool,
        direction_binaries: frozenset[str],
    ) -> LPResult:
        self.problem = p
        return LPResult(status="error", raw_status=_CAPTURED)


@dataclass
class _JointSolve:
    """The `lp_solve_hook` that solves the scenario-replicated problem.

    Called by `_build_plan_once()` with the anchor's (p50's) built problem.
    Every call -- `build_plan()` may solve more than once (thermal deadline
    retry, direction-exclusivity re-solves) -- rebuilds the other scenarios
    with that call's own flags, so every scenario always has the same
    structure as the anchor. The record below is overwritten only by an
    optimal joint solve, which is always the solve behind the plan
    `build_plan()` returns."""

    base_kwargs: dict[str, Any]
    others: list[tuple[str, float, SolarConfig]]
    anchor_label: str
    anchor_weight: float
    k: int
    time_limit_seconds: float
    fixed_stage1: dict[str, float] | None = None
    blend_weight: float | None = None
    # time.monotonic() after which no further joint call is started: the
    # budget covers every call build_plan() makes (thermal retry,
    # direction-exclusivity rounds), not each one. None: no budget.
    deadline: float | None = None
    scenario_costs: dict[str, float] = field(default_factory=dict)
    objective_mode: str | None = None
    n_nonanticipativity_rows: int = 0
    n_joint_variables: int = 0
    n_joint_solves: int = 0
    # Set by the optimal joint solve behind the returned plan only; a later
    # failed call (a declined direction round) cannot overwrite it.
    last_raw_status: str | None = None
    # The most recent failed call's status, for a non-optimal result.
    last_failure_raw_status: str | None = None
    scenario_terminal_soc_kwh: dict[str, float] = field(default_factory=dict)
    # Each scenario's whole path, fleet SoC (kWh, end of period) and net
    # battery kW (discharge positive), for the response-only service: the
    # plan attribute has no room for three more horizons.
    scenario_soc_kwh: dict[str, list[float]] = field(default_factory=dict)
    scenario_battery_kw: dict[str, list[float]] = field(default_factory=dict)
    # Each scenario's grid money over the horizon (grid_money()): what the
    # household would actually pay, unlike the LP objective's penalties.
    scenario_grid_cost: dict[str, float] = field(default_factory=dict)
    # Read back off the optimal joint solution, as checks rather than
    # assumptions: the largest |x - x'| over every non-anticipativity pair
    # (0 to solver tolerance when the constraint holds), and each scenario's
    # own net stage-1 battery kW (discharge positive), which must agree.
    nonanticipativity_max_gap: float | None = None
    scenario_stage1_battery_kw: dict[str, list[float]] = field(default_factory=dict)
    # #2025: one record per call build_plan() makes, in order, so a
    # time-limit fallback names the call that ran out rather than the total.
    calls: list[dict[str, Any]] = field(default_factory=list)

    def _record(
        self,
        started: float,
        limit: float | None,
        result: LPResult,
        *,
        thermal_hard_deadline: bool,
        direction_binaries: frozenset[str],
    ) -> None:
        gap = result.highs_info.get("mip_gap")
        self.calls.append(
            {
                "call": len(self.calls) + 1,
                "solve_seconds": round(time.monotonic() - started, 3),
                "time_limit_seconds": None if limit is None else round(limit, 3),
                "status": result.status,
                "raw_status": result.raw_status,
                "mip_gap": (
                    float(gap)
                    if isinstance(gap, (int, float)) and math.isfinite(gap)
                    else None
                ),
                "thermal_hard_deadline": bool(thermal_hard_deadline),
                "direction_binaries": len(direction_binaries),
            }
        )

    def __call__(
        self,
        p: LPProblem,
        solve_kwargs: dict[str, Any],
        *,
        thermal_hard_deadline: bool,
        direction_binaries: frozenset[str],
    ) -> LPResult:
        call_started = time.monotonic()
        remaining = None if self.deadline is None else self.deadline - time.monotonic()
        if remaining is not None and remaining <= 0.0:
            self.last_failure_raw_status = "stochastic_budget_exhausted"
            refused = LPResult(status="error", raw_status=self.last_failure_raw_status)
            self._record(
                call_started,
                0.0,
                refused,
                thermal_hard_deadline=thermal_hard_deadline,
                direction_binaries=direction_binaries,
            )
            return refused
        self.n_joint_solves += 1
        built: list[tuple[str, float, LPProblem]] = []
        for label, weight, solar in self.others:
            capture = _Capture()
            network._build_plan_once(
                **self.base_kwargs,
                solar=solar,
                thermal_hard_deadline=thermal_hard_deadline,
                direction_binaries=direction_binaries,
                lp_solve_hook=capture,
            )
            if capture.problem is None:
                self.last_failure_raw_status = f"scenario_{label}_not_built"
                unbuilt = LPResult(
                    status="error", raw_status=self.last_failure_raw_status
                )
                self._record(
                    call_started,
                    None,
                    unbuilt,
                    thermal_hard_deadline=thermal_hard_deadline,
                    direction_binaries=direction_binaries,
                )
                return unbuilt
            built.append((label, weight, capture.problem))

        joint = LPProblem()
        joint.absorb(p, prefix="", cost_scale=1.0)
        names = stage1_decision_names(
            p,
            self.k,
            batteries=self.base_kwargs["batteries"],
            load_names=_load_names(self.base_kwargs),
        )
        n_rows = 0
        pairs: list[tuple[str, str]] = []
        for label, weight, q in built:
            prefix = label + _SCENARIO_SEPARATOR
            joint.absorb(q, prefix=prefix, cost_scale=weight / self.anchor_weight)
            for name in names:
                if q.has_variable(name):
                    joint.add_eq_constraint(
                        {name: 1.0, prefix + name: -1.0},
                        0.0,
                        name=f"nonanticipativity{_SCENARIO_SEPARATOR}{prefix}{name}",
                    )
                    pairs.append((name, prefix + name))
                    n_rows += 1
        if self.fixed_stage1:
            for name, value in self.fixed_stage1.items():
                if joint.has_variable(name):
                    joint.add_eq_constraint(
                        {name: 1.0}, float(value), name=f"fixed_stage1::{name}"
                    )
        remaining = None if self.deadline is None else self.deadline - time.monotonic()
        joint.time_limit_seconds = (
            self.time_limit_seconds
            if remaining is None
            # The per-call limit always holds; the budget's remainder is
            # floored at 50 ms so a call it admitted is not refused at once.
            else min(self.time_limit_seconds, max(0.05, remaining))
        )
        joint.quiet_diagnostics = True
        joint.accept_time_limited_mip_gap = JOINT_ACCEPT_MIP_GAP
        # Fixed by the options and the blend weight, so the same on every
        # call; set before the solve so a failed one still reports it.
        options, self.objective_mode = joint_options(
            solve_kwargs.get("options"), self.blend_weight
        )
        result = joint.solve(**{**solve_kwargs, "options": options})
        self._record(
            call_started,
            joint.time_limit_seconds,
            result,
            thermal_hard_deadline=thermal_hard_deadline,
            direction_binaries=direction_binaries,
        )
        if result.status != "optimal":
            self.last_failure_raw_status = result.raw_status or result.status
            return result
        self.last_raw_status = result.raw_status or result.status
        costs = {self.anchor_label: p.primary_cost_of(result)}
        for label, _weight, q in built:
            costs[label] = q.primary_cost_of(
                result, within=joint, prefix=label + _SCENARIO_SEPARATOR
            )
        self.scenario_costs = costs
        self.n_nonanticipativity_rows = n_rows
        self.n_joint_variables = joint.n_variables
        self.nonanticipativity_max_gap = max(
            (
                abs(joint.value_of(result, a) - joint.value_of(result, b))
                for a, b in pairs
            ),
            default=0.0,
        )
        stage1: dict[str, list[float]] = {}
        for label in [self.anchor_label] + [b[0] for b in built]:
            prefix = "" if label == self.anchor_label else label + _SCENARIO_SEPARATOR
            stage1[label] = [
                sum(
                    joint.value_of(result, f"{prefix}battery_discharge_{b.name}_{t}")
                    - joint.value_of(result, f"{prefix}battery_charge_{b.name}_{t}")
                    for b in self.base_kwargs["batteries"]
                )
                for t in range(self.k)
            ]
        self.scenario_stage1_battery_kw = stage1
        n = len(self.base_kwargs["periods"].hours)
        soc_paths: dict[str, list[float]] = {}
        battery_paths: dict[str, list[float]] = {}
        for label in [self.anchor_label] + [b[0] for b in built]:
            prefix = "" if label == self.anchor_label else label + _SCENARIO_SEPARATOR
            soc = np.zeros(n)
            net = np.zeros(n)
            for bat in self.base_kwargs["batteries"]:
                names = [f"{prefix}battery_soc_{bat.name}_{t}" for t in range(n)]
                if all(joint.has_variable(x) for x in names):
                    soc += joint.values_of(result, names)
                dis = [f"{prefix}battery_discharge_{bat.name}_{t}" for t in range(n)]
                chg = [f"{prefix}battery_charge_{bat.name}_{t}" for t in range(n)]
                if all(joint.has_variable(x) for x in dis + chg):
                    net += joint.values_of(result, dis) - joint.values_of(result, chg)
            soc_paths[label] = [float(v) for v in soc]
            battery_paths[label] = [float(v) for v in net]
        self.scenario_soc_kwh = soc_paths
        self.scenario_battery_kw = battery_paths
        grid = self.base_kwargs["grid"]
        hours = np.asarray(self.base_kwargs["periods"].hours, dtype=np.float64)

        def _vals(prefix: str, tmpl: str) -> NDArray[np.float64]:
            names = [prefix + tmpl.format(t=t) for t in range(n)]
            if not all(joint.has_variable(x) for x in names):
                return np.zeros(n)
            return joint.values_of(result, names)

        money: dict[str, float] = {}
        for label in [self.anchor_label] + [b[0] for b in built]:
            prefix = "" if label == self.anchor_label else label + _SCENARIO_SEPARATOR
            money[label] = grid_money(
                grid,
                hours,
                import_kw=np.add(
                    _vals(prefix, "grid_import_{t}"),
                    _vals(prefix, "grid_import_excess_{t}"),
                    dtype=np.float64,
                ),
                export_kw=_vals(prefix, "grid_export_{t}"),
                bonus_kw=_vals(prefix, "export_bonus_{t}"),
            )
        self.scenario_grid_cost = money
        self.scenario_terminal_soc_kwh = {
            label: path[-1] if path else 0.0 for label, path in soc_paths.items()
        }
        # The plan is p50's: its total_cost is p50's own cost, not the
        # weighted joint objective. Its duals and reduced costs are the
        # expected marginals (module docstring).
        return replace(
            result,
            objective=costs[self.anchor_label],
            duals=_expected_marginals(
                result.duals, [b[0] for b in built], self.anchor_weight
            ),
            reduced_costs=_expected_marginals(
                result.reduced_costs, [b[0] for b in built], self.anchor_weight
            ),
        )


def _expected_marginals(
    values: dict[str, float], other_labels: list[str], anchor_weight: float
) -> dict[str, float]:
    """The joint solve's duals (or reduced costs) as the expected marginal of
    each anchor-named row (or variable): `w_p50 * (v[n] + sum_s v[s::n])`.

    The joint objective is `sum_s (w_s / w_p50) * cost_s`, so scenario s's own
    row carries `(w_s / w_p50) * lambda_s`; the sum over the scenarios' copies
    of one row, times `w_p50`, is `sum_s w_s * lambda_s`. Entries belonging to
    a scenario copy or to the joint-only rows (non-anticipativity, pins) are
    dropped, so the result has the deterministic model's keys."""
    prefixes = [label + _SCENARIO_SEPARATOR for label in other_labels]
    out: dict[str, float] = {}
    for name, value in values.items():
        if _SCENARIO_SEPARATOR in name:
            continue
        total = float(value)
        for prefix in prefixes:
            total += float(values.get(prefix + name, 0.0))
        out[name] = anchor_weight * total
    return out


def joint_options(
    requested: Any, blend_weight: float | None
) -> tuple[BlendedOptions | None, str]:
    """The single-phase objective the joint solve uses instead of whatever
    the deterministic solve was asked for, and its label.

    Never the lex / calibrated phases: their phase 2 re-solves the whole
    joint MIP against the secondary objective with the primary pinned, and
    on devhub's test controllable loads (4,128 binaries) that phase timed
    out (nimbus #1805). It would also share lp.py's module-wide calibration
    cooldown with the live solve. Instead:

    - the deterministic solve used no options -> none here either: the
      tie-breaks are already in the primary objective ("primary");
    - it used lex/calibrated and its calibrated blend weight is known ->
      ONE blended solve, primary + that weight x secondary ("blended"),
      so the tie-breaks (proximal anchor, smoothness, earliness) still act
      on the joint plan. Calibration chose that weight for the
      deterministic problem; the joint problem is three copies of it at
      scaled costs, so the same weight is the nearest measured value, not
      a re-calibrated one;
    - otherwise -> primary only, the tie-breaks dropped ("primary_only"),
      which is what lp.py itself falls back to after a failed phase."""
    if requested is None:
        return None, "primary"
    if blend_weight is not None and blend_weight > 0.0:
        return BlendedOptions(blend_weight=float(blend_weight)), "blended"
    return None, "primary_only"


def _load_names(kwargs: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("sheddable_loads", "adequacy_loads", "thermal_loads"):
        for cfg in kwargs.get(key) or []:
            names.append(cfg.name)
    return names


@dataclass(frozen=True)
class StochasticResult:
    """What one stochastic solve produced.

    `status` is "optimal", "no_band" (nothing to do: the caller keeps the
    deterministic plan) or the joint solve's non-optimal status. `plan` is
    the p50 trajectory with the shared stage-1 decision, set only when
    `status == "optimal"`."""

    status: str
    plan: network.Plan | None
    k: int
    weights: tuple[float, ...]
    labels: tuple[str, ...]
    solve_seconds: float
    expected_cost: float | None = None
    scenario_costs: dict[str, float] = field(default_factory=dict)
    stage1_battery_kw: float | None = None
    no_sun_periods: int = 0
    # SolarScenarios.band: "calibrated" (#1807) or "envelope".
    solar_band: str | None = None
    n_joint_solves: int = 0
    n_joint_variables: int = 0
    n_nonanticipativity_rows: int = 0
    raw_status: str | None = None
    objective_mode: str | None = None
    nonanticipativity_max_gap: float | None = None
    scenario_stage1_battery_kw: dict[str, list[float]] = field(default_factory=dict)
    # #2025: every joint call build_plan() made (_JointSolve.calls).
    joint_calls: list[dict[str, Any]] = field(default_factory=list)
    # The commit window this solve used (minutes); K is its period count.
    stage1_minutes: float = STAGE1_MINUTES
    # Every battery's SoC at the horizon's last period, per scenario: the
    # spread shows how much the terminal value is carrying (a cheap terminal
    # value lets a sunny scenario dump energy at the end).
    scenario_terminal_soc_kwh: dict[str, float] = field(default_factory=dict)
    scenario_soc_kwh: dict[str, list[float]] = field(default_factory=dict)
    scenario_battery_kw: dict[str, list[float]] = field(default_factory=dict)
    scenario_grid_cost: dict[str, float] = field(default_factory=dict)
    # Every battery's capacity, summed: scenario SoC paths as a % of it are
    # on the same scale as the plan rows' own (fleet) `soc_pct`.
    fleet_capacity_kwh: float = 0.0
    # Copied off the joint plan so the caller can refuse a plan weaker than
    # the deterministic one: a relaxed thermal deadline, or two-way flows.
    thermal_guarantee_relaxed: tuple[str, ...] = ()
    direction_exclusivity: str | None = None


def grid_money(
    grid: Any,
    hours: NDArray[np.float64],
    *,
    import_kw: NDArray[np.float64],
    export_kw: NDArray[np.float64],
    bonus_kw: NDArray[np.float64],
) -> float:
    """Money over the horizon at the grid's real prices, $ (positive is
    spent): import cost, minus export income, minus the P2P bonus earned.
    No risk adjustment, no LP penalty or tie-break term -- the figure a
    household would see on a bill, for one plan."""
    h = np.asarray(hours, dtype=np.float64)
    imp = np.asarray(grid.import_price, dtype=np.float64)
    exp = np.asarray(grid.export_price, dtype=np.float64)
    total = float(np.sum(h * (imp * import_kw - exp * export_kw)))
    bonus_price = getattr(grid, "export_bonus_price", None)
    if bonus_price is not None:
        total -= float(np.sum(h * np.asarray(bonus_price, dtype=np.float64) * bonus_kw))
    return total


def plan_grid_money(plan: network.Plan, grid: Any, hours: Any) -> float | None:
    """grid_money() of a solved plan (the main plan's figure, on the same
    footing as each scenario's), or None for a plan that did not solve."""
    if plan.status != "optimal" or not len(plan.grid_import_kw):
        return None
    bonus = getattr(plan, "export_bonus_kw", None)
    n = len(plan.grid_import_kw)
    return grid_money(
        grid,
        np.asarray(hours, dtype=np.float64)[:n],
        import_kw=np.asarray(plan.grid_import_kw, dtype=np.float64),
        export_kw=np.asarray(plan.grid_export_kw, dtype=np.float64),
        bonus_kw=np.zeros(n)
        if bonus is None or not len(bonus)
        else np.asarray(bonus, dtype=np.float64),
    )


def net_battery_kw(plan: network.Plan, t: int = 0) -> float:
    """Net battery power at period t, discharge positive -- the sign of the
    published plan rows' `battery_kw`."""
    return float(plan.battery_discharge_kw[t]) - float(plan.battery_charge_kw[t])


def fixed_stage1_from_plan(plan: network.Plan, k: int) -> dict[str, float]:
    """Stage-1 battery decisions of `plan` (every battery, t < k) as
    `{variable name: kW}`, for `build_stochastic_plan(fixed_stage1=...)`.

    Pinning these and re-solving the joint problem gives the expected cost
    of dispatching the deterministic plan's stage 1 with optimal recourse
    in each scenario (the "EEV" of stochastic programming); its gap to the
    stochastic optimum is the value of the stochastic solution. Battery
    decisions only: grid flows follow from them in each scenario."""
    out: dict[str, float] = {}
    for b in plan.batteries:
        for t in range(min(k, len(b.charge_kw))):
            out[f"battery_charge_{b.name}_{t}"] = float(b.charge_kw[t])
            out[f"battery_discharge_{b.name}_{t}"] = float(b.discharge_kw[t])
    return out


def build_stochastic_plan(
    build_kwargs: dict[str, Any],
    *,
    weights: tuple[float, float, float] = SCENARIO_WEIGHTS,
    stage1_minutes: float = STAGE1_MINUTES,
    time_limit_seconds: float = JOINT_TIME_LIMIT_SECONDS,
    fixed_stage1: dict[str, float] | None = None,
    blend_weight: float | None = None,
    budget_seconds: float | None = None,
) -> StochasticResult:
    """Solve the two-stage stochastic plan for the same inputs
    `network.build_plan(**build_kwargs)` would solve deterministically.

    `blend_weight` is the deterministic solve's calibrated weight
    (`Plan.calibration_weight_used`); see `joint_options()`.
    `fixed_stage1` pins named stage-1 variables (see
    `fixed_stage1_from_plan()`); measurement only, never the live path.
    `budget_seconds` bounds the whole call -- every joint solve
    `build_plan()` makes -- not just one HiGHS run; None leaves only the
    per-call `time_limit_seconds`.
    Raises ValueError on invalid weights; every solver-side failure is a
    non-optimal `status`, never an exception."""
    if len(weights) != len(SCENARIO_LABELS):
        msg = f"weights must have {len(SCENARIO_LABELS)} entries, got {len(weights)}"
        raise ValueError(msg)
    if any(w <= 0.0 for w in weights) or abs(sum(weights) - 1.0) > 1e-9:
        msg = f"weights must be positive and sum to 1, got {weights}"
        raise ValueError(msg)
    periods = build_kwargs["periods"]
    k = stage1_period_count(periods.hours, stage1_minutes)
    started = time.monotonic()
    scenarios = solar_scenarios(build_kwargs["solar"], k)
    if scenarios is None:
        return StochasticResult(
            status="no_band",
            plan=None,
            k=k,
            weights=tuple(weights),
            labels=SCENARIO_LABELS,
            stage1_minutes=float(stage1_minutes),
            solve_seconds=time.monotonic() - started,
        )
    base = {key: v for key, v in build_kwargs.items() if key != "solar"}
    # See the module docstring: no ranging or offer curve on the joint model.
    base["compute_offer_curve"] = False
    base["compute_signals"] = False
    others = [
        (SCENARIO_LABELS[i], weights[i], scenarios.solar[i])
        for i in range(len(SCENARIO_LABELS))
        if i != ANCHOR_INDEX
    ]
    hook = _JointSolve(
        base_kwargs=base,
        others=others,
        anchor_label=SCENARIO_LABELS[ANCHOR_INDEX],
        anchor_weight=weights[ANCHOR_INDEX],
        k=k,
        time_limit_seconds=time_limit_seconds,
        fixed_stage1=fixed_stage1,
        blend_weight=blend_weight,
        deadline=None if budget_seconds is None else started + budget_seconds,
    )
    plan = network.build_plan(
        **base, solar=scenarios.solar[ANCHOR_INDEX], lp_solve_hook=hook
    )
    seconds = time.monotonic() - started
    if plan.status != "optimal" or not hook.scenario_costs:
        return StochasticResult(
            status=plan.status if plan.status != "optimal" else "error",
            plan=None,
            k=k,
            weights=tuple(weights),
            labels=SCENARIO_LABELS,
            stage1_minutes=float(stage1_minutes),
            solve_seconds=seconds,
            no_sun_periods=scenarios.no_sun_periods,
            solar_band=scenarios.band,
            n_joint_solves=hook.n_joint_solves,
            raw_status=hook.last_failure_raw_status or hook.last_raw_status,
            objective_mode=hook.objective_mode,
            joint_calls=list(hook.calls),
        )
    expected = sum(
        weights[i] * hook.scenario_costs[SCENARIO_LABELS[i]]
        for i in range(len(SCENARIO_LABELS))
    )
    return StochasticResult(
        status="optimal",
        plan=plan,
        k=k,
        weights=tuple(weights),
        labels=SCENARIO_LABELS,
        stage1_minutes=float(stage1_minutes),
        solve_seconds=seconds,
        expected_cost=float(expected),
        scenario_costs=dict(hook.scenario_costs),
        stage1_battery_kw=net_battery_kw(plan),
        no_sun_periods=scenarios.no_sun_periods,
        solar_band=scenarios.band,
        n_joint_solves=hook.n_joint_solves,
        n_joint_variables=hook.n_joint_variables,
        n_nonanticipativity_rows=hook.n_nonanticipativity_rows,
        raw_status=hook.last_raw_status,
        objective_mode=hook.objective_mode,
        nonanticipativity_max_gap=hook.nonanticipativity_max_gap,
        scenario_stage1_battery_kw=dict(hook.scenario_stage1_battery_kw),
        scenario_terminal_soc_kwh=dict(hook.scenario_terminal_soc_kwh),
        scenario_soc_kwh=dict(hook.scenario_soc_kwh),
        scenario_battery_kw=dict(hook.scenario_battery_kw),
        scenario_grid_cost=dict(hook.scenario_grid_cost),
        fleet_capacity_kwh=float(sum(b.capacity_kwh for b in base["batteries"])),
        thermal_guarantee_relaxed=tuple(plan.thermal_guarantee_relaxed),
        direction_exclusivity=plan.direction_exclusivity,
        joint_calls=list(hook.calls),
    )


def summary_attribute(
    result: StochasticResult,
    *,
    mode: str,
    deterministic_plan: network.Plan,
    deterministic_solve_seconds: float | None,
    drives_dispatch: bool,
    fallback_reason: str | None = None,
    computed_at: str | None = None,
    grid: Any = None,
    hours: Any = None,
) -> dict[str, Any]:
    """The published `stochastic` attribute. Plain JSON types only.

    `grid` and `hours` (the solve's own) give the main plan's grid money on
    the same footing as the scenarios'; without them it is None."""
    det_kw = (
        net_battery_kw(deterministic_plan)
        if deterministic_plan.status == "optimal"
        and len(deterministic_plan.battery_charge_kw)
        else None
    )
    out: dict[str, Any] = {
        "mode": mode,
        "status": result.status,
        "drives_dispatch": drives_dispatch,
        "fallback_reason": fallback_reason,
        "scenarios": list(result.labels),
        "weights": [float(w) for w in result.weights],
        "stage1_periods": int(result.k),
        "stage1_minutes": result.stage1_minutes,
        "expected_cost": _rounded(result.expected_cost),
        "scenario_cost": {k: _rounded(v) for k, v in result.scenario_costs.items()},
        "deterministic_total_cost": _rounded(deterministic_plan.total_cost),
        "stage1_battery_kw": _rounded(result.stage1_battery_kw),
        "deterministic_period0_battery_kw": _rounded(det_kw),
        "stochastic_solve_seconds": _rounded(result.solve_seconds),
        "deterministic_solve_seconds": _rounded(deterministic_solve_seconds),
        "joint_solves": int(result.n_joint_solves),
        "joint_calls": [dict(c) for c in result.joint_calls],
        "joint_variables": int(result.n_joint_variables),
        "nonanticipativity_rows": int(result.n_nonanticipativity_rows),
        "nonanticipativity_max_gap": _rounded(result.nonanticipativity_max_gap),
        # Period 0 only per scenario: equal across scenarios is the shared
        # decision, read back off the solution rather than assumed.
        "scenario_period0_battery_kw": {
            k: _rounded(v[0]) for k, v in result.scenario_stage1_battery_kw.items() if v
        },
        "no_sun_periods": int(result.no_sun_periods),
        "solar_band": result.solar_band,
        "raw_status": result.raw_status,
        "objective_mode": result.objective_mode,
        "scenario_terminal_soc_kwh": {
            k: _rounded(v) for k, v in result.scenario_terminal_soc_kwh.items()
        },
        "thermal_guarantee_relaxed": list(result.thermal_guarantee_relaxed),
        "direction_exclusivity": result.direction_exclusivity,
        # Money at the real prices, $ over the horizon (grid_money()): the
        # LP's `scenario_cost` above carries its penalty and tie-break terms
        # and is not money.
        "scenario_grid_cost": {
            k: _rounded(v) for k, v in result.scenario_grid_cost.items()
        },
        "expected_grid_cost": _rounded(
            sum(
                w * result.scenario_grid_cost[k]
                for k, w in zip(result.labels, result.weights, strict=True)
                if k in result.scenario_grid_cost
            )
            if len(result.scenario_grid_cost) == len(result.labels)
            else None
        ),
        "deterministic_grid_cost": _rounded(
            plan_grid_money(deterministic_plan, grid, hours)
            if grid is not None and hours is not None
            else None
        ),
    }
    if computed_at is not None:
        out["computed_at"] = computed_at
    return out


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


def paths_attribute(result: StochasticResult) -> dict[str, Any]:
    """Each scenario's fleet SoC, % of every battery's capacity, one value
    per plan period -- the same periods, in the same order, as the plan's
    `forecast` rows, so a chart takes each row's time and end. Published as
    `stochastic_paths` (unrecorded, like `forecast`): 3 x ~200 numbers, about
    2.4 KB. Empty without an optimal joint solve."""
    if (
        result.status != "optimal"
        or result.plan is None
        or result.fleet_capacity_kwh <= 0.0
    ):
        return {}
    cap = result.fleet_capacity_kwh
    out: dict[str, Any] = {
        "soc_pct": {
            k: [round(100.0 * v / cap, 1) for v in path]
            for k, path in result.scenario_soc_kwh.items()
        }
    }
    # Household, 20 Oct 2026: each value's own time (its period's end). In
    # shadow the stochastic solve reruns every few minutes while the plan
    # re-plans every minute, so the plan's rows move under these paths:
    # matched by position they drifted hours out of line by day 2.
    grid = result.plan.periods
    starts = grid.period_starts
    if starts is not None and len(starts) == len(grid.hours):
        out["ends"] = [
            (t + timedelta(hours=float(h))).isoformat()
            for t, h in zip(starts, grid.hours, strict=True)
        ]
    return out


def detail_attribute(result: StochasticResult) -> dict[str, Any]:
    """The per-period detail behind the summary, for the response-only
    `nimbus_load.get_stochastic_summary` service: each scenario's fleet SoC
    and net battery kW over the horizon, with the period grid. Empty without
    an optimal joint solve. Plain JSON types only."""
    if result.status != "optimal" or result.plan is None:
        return {}
    grid = result.plan.periods
    starts = grid.period_starts
    return {
        "period_starts": None if starts is None else [t.isoformat() for t in starts],
        "period_hours": [round(float(h), 6) for h in grid.hours],
        "scenario_soc_kwh": {
            k: [round(v, 3) for v in path]
            for k, path in result.scenario_soc_kwh.items()
        },
        "scenario_battery_kw": {
            k: [round(v, 3) for v in path]
            for k, path in result.scenario_battery_kw.items()
        },
    }
