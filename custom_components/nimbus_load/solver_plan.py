"""Plan assembly: every `solver_inputs/*` output plus `cfg` resolved into the
`elements.*Config` objects `network.build_plan()` consumes, and the solve itself
(nimbus issue #1303, spec 004 -- Phase 4 of #1298).

## This phase cannot be proved the way Phases 1-3 were

Phases 1-3 relocated existing NAMED functions, so they could be *proved*: strip
the inserted prefixes and the bytes match; after `ruff format`, `ast.dump()`
matches. Identity.

Phase 4 has no pre-existing function to compare against. It lifts a contiguous
span of `main()`'s own body into a new one, and **the parameterisation is the
change** -- names that were `main()` locals become arguments. No "strip the
prefixes and diff" applies. Safety here rests entirely on behavioural
equivalence via the golden master, plus the deliberate mutation check in
`tests/test_1303_plan_assembly_extraction.py`. #1380's review made exactly this
point and asked for it to be stated where the code lives.

## Why a result object rather than `-> network.Plan`

Spec 004 declares the return as `network.Plan`. Measured against `main()` at
`b181403`, that is seven outputs short: the rest of `main()` reads **eight**
names this span binds, and all but `plan` itself feed `publish_plan()`'s own
argument list. A `-> network.Plan`-only signature raises `NameError` at the first
post-span read. None of the eight is also bound before the span, so nothing
would silently fall back to a stale value -- the omission is loud, not subtle.

`PlanAssembly` is a `@dataclass(frozen=True)` because that is already this
codebase's convention for a sub-system's own output: `resolve_soc_envelope()`,
`build_load_arrays()` and `build_price_arrays()` all return one. (Spec 004 itself
notes that `build_solar_arrays()` returning a plain tuple is the inconsistent
one.)

## Why `previous_plan` is a parameter and not a call

`main()` used to call `load_previous_plan()` *inside* this span. That function
stays in `solver_writer.py` -- layer 4, above this module -- so calling it from
here would be an upward dependency needing an `import-linter` exception.
Hoisting the call into `main()` and passing the result removes it entirely, and
is observationally identical: everything the call now crosses
(`elements.*Config` construction, `terminal_value_breakpoints_for`,
`midnight_boundary_period_indices`, `fetch_p2p_fixed_export_kw`) is pure
computation, verified to do no I/O. The only write to `PLAN_STATE_PATH` is
`save_plan_state()`, which runs later inside `publish_plan()`.

**The consequence worth stating: this module has NO `_solver_writer()` seam.**
Unlike every other module extracted by #1298, nothing here reaches back up into
`solver_writer.py`, so there is no deferred-import trap to get wrong and
`nimbus-layers` gains no new `ignore_imports` exception.

## The flat keyword-only inputs

Confirmed by #1380's review ("keep the flat keyword-only signature") over a
`PlanAssemblyInputs` bundle, which would not reduce coupling and would invent a
type with no reuse. Keyword-only so call-site order cannot drift -- but note that
keyword-only does **not** protect against a *transposition* between two
same-shaped arguments, and 18 of them sit in a group where a swap is
type-correct: 8 per-period price arrays, 3 load, 3 solar, 4 scalar kW. That is
what the mutation check exists for.

One correction to spec 004's own grouping while we are here: it lists
`import_limit_kw`/`export_limit_kw` among "4 scalar kW limits". They are not
scalars. `resolve_envelope_limit_kw()` returns `list[float]` and
`GridConfig.import_limit_kw` is declared `float | NDArray[np.float64]`, because
#493's dynamic operating envelopes made them per-period arrays. Annotating them
`float` type-checks against nothing real -- mypy caught it immediately.

## Why mypy still reports six errors at `main()`'s call to this function

`LoadArrays` and `PriceArrays` (`solver_inputs/load.py`, `solver_inputs/prices.py`)
declare **every field as `object`**, so `main()`'s `load_kw`, `load_lower_kw`,
`load_upper_kw`, `export_bonus_price` and `p2p_recent_volume_kwh` are all
`object` locals. Passing them into honestly-typed parameters is an error that did
not exist while the code was inline, because inline there was no typed boundary
to cross -- `np.array(x)` accepts `object` happily.

Those six are upstream looseness becoming VISIBLE, not looseness introduced here,
and the fix is to tighten those two dataclasses -- which spec 004's own Non-goals
explicitly exclude from this phase. Annotating these parameters `object` to make
the count go down would hide exactly the information the mutation check depends
on, so it is deliberately not done. CI's strict `solver/`+`ml/` gate is unchanged
at 6 pre-existing findings; the whole-package job is advisory (`|| true`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

try:
    from . import solver_shared
    from .solver import elements, lp, network
    from .solver_inputs import extra_batteries as extra_batteries_inputs
    from .solver_inputs.battery_soc import SocEnvelope
    from .solver_inputs.controllable_loads import build_controllable_loads
    from .solver_shared import _cfg_num, _local, fetch_p2p_fixed_export_kw
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]
    from solver import elements, lp, network  # type: ignore[no-redef]
    from solver_inputs import (  # type: ignore[no-redef]
        extra_batteries as extra_batteries_inputs,
    )
    from solver_inputs.battery_soc import SocEnvelope  # type: ignore[no-redef]
    from solver_inputs.controllable_loads import (  # type: ignore[no-redef]
        build_controllable_loads,
    )
    from solver_shared import (  # type: ignore[no-redef]
        _cfg_num,
        _local,
        fetch_p2p_fixed_export_kw,
    )


# Confidence-aware dispatch (2026-08-17, real "keep building the Solver's
# own real inputs" ask). Nimbus's own Forecaster sensors already carry a
# real, genuine lower/upper confidence band per forecast point
# (confirmed live: sensor.nimbus_combined_total_dc_power_forecast's own
# forecast array has {time, value, lower, upper} keys) -- built and
# validated as part of the Forecaster's own GBRT-quantile / calibrated-
# residual machinery, not invented for this. network.py's own
# build_plan() has had a fully-built, tested risk_aversion mechanism for
# exactly this since before this writer ever ran (see its own
# "CONFIDENCE-AWARE DISPATCH" docstring section) -- it was simply never
# wired up: this writer used to call resample_forecast(..., "value", ...)
# only, silently discarding the real lower/upper fields sitting right
# there in the same response.
#
# 0.25 is a real, deliberately MODEST choice, not the mechanism's own
# default-safe 0.0 (would waste real, already-computed data) or its
# extreme 1.0 (would plan for the full pessimistic bound every period,
# regardless of how tight/confident that period's own real forecast is
# -- this project's own many prior sessions of P2P dispatch tuning were
# all done against risk_aversion=0.0 behaviour; jumping straight to an
# aggressive setting risks visibly changing already-tuned dispatch
# timing on the very first deploy of a brand-new, never-live-tested
# mechanism). A real, tunable lever going forward -- raise it if the
# Solver is later found under-provisioning against real forecast misses,
# lower it (back to 0.0) if 0.25 is ever found overly conservative.
#
# 2026-08-21 (task #128): this is now only the FALLBACK default, read
# once at deploy time -- the LIVE value comes from
# number.nimbus_solver_risk_aversion (dashboard-editable, per the direct
# household ask for "a flexible sliding charging urgency control"),
# fetched fresh from cfg on every solve. See main()'s own risk_aversion=
# read for the exact fallback logic (falls back to this constant only if
# the live entity is somehow unavailable, not on every run).
RISK_AVERSION = 0.25


def midnight_boundary_period_indices(grid_times: list[datetime]) -> list[int]:
    """Real, direct fix for the 2026-08-22 finding (shadow-mode chart
    evidence): the Solver's own plan kept discharging for ~1hr PAST the
    real P2P window's close, at essentially unchanged export price.
    Root cause: terminal_value_breakpoints only ever protected soc at
    the horizon's own true FINAL period (see terminal_value_period_
    indices' own docstring, nimbus repo elements.py) -- every OTHER day
    boundary in this multi-day horizon had nothing telling the LP
    tomorrow has its own P2P opportunity too, so with discharge_cost
    held at a real, deliberately tiny $0.01/kWh, any export price above
    that stayed "profitable" forever and it just kept selling toward
    the floor.

    Returns the period index immediately BEFORE each real local
    midnight -- i.e. soc[idx] represents the battery's state at the
    exact moment a real day (and this household's own real P2P window)
    closes, the correct anchor for "how much should be held back going
    into tomorrow". grid_times[t].hour is already real local AEST (see
    build_tiered_grid -- 'now' is built from LOCAL_TZ, not UTC), so
    no timezone conversion is needed here. Works correctly regardless of
    which tier a given midnight falls in -- the 5-min Tier1 region and
    the 1-hour Tier2 region both break exactly on real hour boundaries,
    so "the period right before an hour-0 period" is always well-
    defined either way.
    """
    indices = []
    for t in range(len(grid_times) - 1):
        if _local(grid_times[t]).hour != 0 and _local(grid_times[t + 1]).hour == 0:
            indices.append(t)
    return indices


def terminal_value_breakpoints_for(
    base_rate: float, min_soc_kwh: float, max_soc_kwh: float
) -> list:
    """Concave piecewise-linear terminal value (Solver audit item #7,
    Nimbus PR #35) -- switched on live 2026-08-19, replacing the flat
    salvage_value mechanism above. Proven on 2 real household nights
    (2026-08-16/17) to be ~$3.12-3.15/day MORE profitable AND to avoid
    the flat mechanism's own confirmed real pathology: driving straight
    to a hard SoC corner every night (100% or the floor) with zero
    smooth transition -- the exact same class of behaviour HAEO itself
    was caught live doing on 2026-08-19 (different root cause -- a
    drifted 100% efficiency setting -- but the identical symptom).

    Same 3-segment shape already validated this session in
    scripts/research/forward_value_comparison.py's own real-data
    comparison, calibrated here to the SAME average $/kWh as whatever
    flat salvage_value rate it replaces (base_rate), so switching this
    on reflects a change in CURVATURE, not a change in how much total
    terminal value is being modeled -- the household isn't being handed
    a different valuation, just a smoother one.
    """
    above_floor = max_soc_kwh - min_soc_kwh
    return [
        (above_floor * 0.15, base_rate * 2.2),
        (above_floor * 0.55, base_rate * 1.0),
        (above_floor * 0.30, base_rate * 0.35),
    ]


@dataclass(frozen=True)
class PlanAssembly:
    """What `assemble_and_solve_plan()` produces that the rest of `main()` still
    needs. Measured, not guessed: these are exactly the names the span binds and
    the code after it reads -- see this module's own docstring for why spec 004's
    `-> network.Plan` was seven outputs short.

    `solve_started` is a `time.monotonic()` stamp taken at the same point in the
    sequence as before, so the solve duration `publish_plan()` derives from it
    still covers the cfg reads and `build_controllable_loads()` exactly as it did
    inline.
    """

    plan: network.Plan
    grid: elements.GridConfig
    all_batteries: list[elements.BatteryConfig]
    fleet_capacity_kwh: float
    solve_started: float
    risk_aversion: float
    import_price_risk_aversion: float
    export_price_risk_aversion: float


def assemble_and_solve_plan(
    cfg: dict,
    *,
    now: datetime,
    grid_times: list[datetime],
    period_hours_arr: list[float],
    n_periods: int,
    soc_envelope: SocEnvelope,
    capacity_kwh: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    charge_cost: float,
    discharge_cost_arr: NDArray[np.float64],
    salvage_value: float,
    spike_override_kw: float | None,
    import_price: list[float],
    export_price: list[float],
    import_limit_kw: list[float],
    export_limit_kw: list[float],
    export_bonus_price: list[float],
    p2p_recent_volume_kwh: float,
    import_price_upper: list[float] | None,
    export_price_lower: list[float] | None,
    solar_kw: list[float],
    solar_lower_kw: list[float],
    solar_upper_kw: list[float],
    load_kw: list[float],
    load_lower_kw: list[float],
    load_upper_kw: list[float],
    previous_plan: network.Plan | None,
) -> PlanAssembly:
    """Builds every `elements.*Config` from the resolved inputs and solves.

    Moved verbatim from `main()` (`solver_writer.py:12469`-`12712` at `b181403`). The
    body below is byte-for-byte that span apart from four deliberate edits, each
    applied with an exact-occurrence assertion by the build script:
    `previous_plan = load_previous_plan()` dropped (it is a parameter now), the
    two `_LOGGER` uses qualified as `solver_shared._LOGGER`, the four unpacked
    SoC scalars read off `soc_envelope` instead, and the `return` appended.
    """
    battery = elements.BatteryConfig(
        name="home",  # nimbus issue #467: single real household battery, see battery_cfg's own comment above
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=soc_envelope.initial_soc_kwh,
        min_soc_kwh=soc_envelope.min_soc_kwh,
        max_soc_kwh=soc_envelope.max_soc_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        # solver_efficiency_percent is a single ROUND-TRIP figure (see
        # hub_options.py's own field help text: "combined battery-
        # chemistry + inverter conversion losses"), but BatteryConfig
        # wants separate charge_efficiency/discharge_efficiency -- split
        # geometrically (charge_eff = discharge_eff = sqrt(round_trip)),
        # the standard, defensible simplification when only one combined
        # number is known. min(..., 0.999) is a real, defensive clamp --
        # the config-flow's own help text already warns against ever
        # entering 100%, but a stale/mistaken 100% entry would otherwise
        # crash the solver's own structural degeneracy guard rather than
        # just quietly degrade to "solver treats this as effectively
        # lossless," so this floor is deliberately kept even though a
        # correctly-filled-in form should never actually need it.
        #
        # Named here (not just inlined) so the SAME value can be surfaced
        # on the pushed diagnostic entity below -- nimbus issue #168 (Mark
        # Purcell, 2026-08-25): "a user reading solver_efficiency_percent
        # = 95 would reasonably interpret it as one-way... and get the
        # arithmetic wrong" without this documented on the entity itself.
        charge_efficiency=soc_envelope.charge_discharge_efficiency,
        discharge_efficiency=soc_envelope.charge_discharge_efficiency,
        charge_cost=charge_cost,
        discharge_cost=discharge_cost_arr,
        salvage_value=salvage_value,  # required field, but overridden by terminal_value_breakpoints below when set
        terminal_value_breakpoints=terminal_value_breakpoints_for(
            salvage_value, soc_envelope.min_soc_kwh, soc_envelope.max_soc_kwh
        ),
        # Every real day boundary in the horizon, plus the true final
        # period -- see midnight_boundary_period_indices()'s own
        # docstring above for the real 2026-08-22 finding this fixes.
        terminal_value_period_indices=sorted(
            set(midnight_boundary_period_indices(grid_times) + [len(grid_times) - 1])
        ),
        # Real economic cycle-wear cost (Track B2, 2026-08-22). 0.0
        # (unconfigured, the default) is a genuine no-op -- see
        # BatteryConfig's own degradation_cost_per_kwh docstring.
        degradation_cost_per_kwh=_cfg_num(cfg, "solver_degradation_cost_per_kwh", 0.0),
        spike_override_discharge_kw=spike_override_kw,
    )
    fixed_export_kw = fetch_p2p_fixed_export_kw(cfg, grid_times)
    grid = elements.GridConfig(
        import_price=np.array(import_price),
        export_price=np.array(export_price),
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
        export_bonus_price=np.array(export_bonus_price),
        export_bonus_volume_kwh=p2p_recent_volume_kwh,
        fixed_export_kw=np.array(fixed_export_kw)
        if fixed_export_kw is not None
        else None,
        import_price_upper=np.array(import_price_upper)
        if import_price_upper is not None
        else None,
        export_price_lower=np.array(export_price_lower)
        if export_price_lower is not None
        else None,
    )
    solar = elements.SolarConfig(
        forecast_kw=np.array(solar_kw),
        lower_kw=np.array(solar_lower_kw),
        upper_kw=np.array(solar_upper_kw),
    )
    loads = [
        elements.LoadConfig(
            name="household_load_summed_18",
            forecast_kw=np.array(load_kw),
            lower_kw=np.array(load_lower_kw),
            upper_kw=np.array(load_upper_kw),
        )
    ]
    periods = elements.PeriodGrid(hours=np.array(period_hours_arr), start=grid_times[0])

    # Plan-to-plan stability (2026-08-16, see PLAN_STATE_PATH's own
    # comment). proximal_weight now reads live from cfg (2026-09-08,
    # number.nimbus_solver_proximal_weight_kw, dashboard-editable) --
    # falls back to network.py's own DEFAULT_PROXIMAL_WEIGHT_KW, the
    # exact value this always silently used before, so an already-
    # configured household sees zero behaviour change until they
    # actually tune it. Same real gap, same fix, as smoothness_weight
    # just below. max_rate_kw deliberately NOT used here -- a hard
    # cap risks suppressing the legitimate, large, real swing at the
    # actual 5pm P2P transition, and this Solver still only observes, it
    # doesn't control anything, so there's no real inverter to protect
    # from a rate-of-change perspective the way max_rate_kw exists for.
    solve_started = time.monotonic()
    # risk_aversion / import+export price_risk_aversion (2026-08-21, task
    # #128) -- now read live from cfg (number.nimbus_solver_risk_aversion
    # / _import_price_risk_aversion / _export_price_risk_aversion,
    # dashboard-editable), replacing the old hardcoded RISK_AVERSION=0.25
    # module constant. Falls back to that same 0.25 default for
    # risk_aversion (matches the constant's own original value exactly --
    # a no-op change for an already-configured household on first deploy)
    # and 0.0 (a complete no-op) for both price dials, which never
    # existed as a constant before. Split into two independent cfg reads
    # the same day this was first wired up (see nimbus's own network.py
    # docstring / number.py comment for the full "one shared scalar
    # forces charge/discharge hedging to move together" reasoning) --
    # this writer only ever had the single-scalar version live for a
    # brief window before the split, never a real production concern.
    risk_aversion = float(
        cfg.get("solver_risk_aversion")
        if cfg.get("solver_risk_aversion") is not None
        else RISK_AVERSION
    )
    import_price_risk_aversion = _cfg_num(cfg, "solver_import_price_risk_aversion", 0.0)
    export_price_risk_aversion = _cfg_num(cfg, "solver_export_price_risk_aversion", 0.0)
    proximal_weight = _cfg_num(
        cfg, "solver_proximal_weight_kw", network.DEFAULT_PROXIMAL_WEIGHT_KW
    )
    # smoothness_weight (2026-08-20, real household finding: "why nimbus
    # decided to make such decisions and charge in bursts not
    # continuously") -- mechanism 4, same value/reasoning as
    # proximal_weight (mechanism 1) just above, just applied within this
    # solve's own timeline instead of across solves. Locally validated
    # (both repo's own scratchpad and nimbus's own committed tests):
    # eliminates a real, reconstructed degenerate burst at byte-identical
    # total_cost, and does NOT smear a genuine, large, real transition
    # (an 80kW price-step scenario, on or off, within $0.07 either way).
    # 2026-09-08 (real household finding, NUC1's own first day of live
    # dispatch -- see network.py's own _add_intraplan_smoothness_penalty
    # docstring for the exact confirmed-live symptom): now reads live
    # from cfg (number.nimbus_solver_intraplan_smoothness_weight_kw,
    # dashboard-editable) instead of always silently passing network.py's
    # own DEFAULT_SMOOTHNESS_WEIGHT_KW constant -- falls back to that same
    # constant, so an already-configured household sees zero behaviour
    # change until they actually tune it up.
    smoothness_weight = _cfg_num(
        cfg,
        "solver_intraplan_smoothness_weight_kw",
        network.DEFAULT_SMOOTHNESS_WEIGHT_KW,
    )
    # nimbus issue #692 (household, real live plan mishaps: a critically-
    # low battery sitting idle through a perfectly good charging price,
    # only charging later at an equal or worse one): the battery's own
    # earliness tie-break, same live-dashboard-first pattern as
    # proximal_weight/smoothness_weight just above -- falls back to
    # network.py's own default, so an already-configured household sees
    # zero behaviour change until they actually tune it.
    battery_charge_earliness_budget_kw = _cfg_num(
        cfg,
        "solver_battery_charge_earliness_budget_kw",
        network.DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
    )
    # nimbus issue #486: real controllable_load subentries (sheddable/
    # deferrable/thermal kinds -- see build_controllable_loads()'s own
    # docstring), replacing the hardcoded empty lists this call used
    # to pass. Native-mode-only, a real no-op ([], [], []) in standalone/
    # cron mode -- see that function's own docstring for why.
    sheddable_loads, adequacy_loads, thermal_loads = build_controllable_loads(
        now, grid_times, n_periods, import_price
    )
    # nimbus issue #563: real battery_participant subentries, in
    # addition to the household's own single "home" battery above --
    # see build_extra_batteries()'s own docstring for the full "upgrade
    # is a no-op with zero subentries" story. Availability gating and the
    # shared-charger power constraint (items 2/3, deferred out of #566)
    # now live in BatteryConfig/network.py -- periods is passed through
    # so a configured departure_hour can be resolved to a real period
    # index against THIS solve's own horizon. "home" (batteries[0]) is
    # the only participant this P2P fixed-export window logic below ever
    # targets -- see BatteryConfig's own docstring / build_plan()'s own
    # "batteries" docstring paragraph for that explicit #467 stage-1
    # decision.
    all_batteries = [battery, *extra_batteries_inputs.build_extra_batteries(periods)]
    solver_shared._LOGGER.debug(
        "Nimbus #757 diag: main() all_batteries after merge = %s (periods=%r)",
        [b.name for b in all_batteries],
        "set" if periods is not None else None,
    )
    # nimbus issue #569 (Mark Purcell, found live within hours of #563
    # landing): plan.battery_soc_kwh is the SUMMED aggregate across every
    # battery (per #467 stage 1's own contract), but every percentage
    # derived from it downstream (soc_pct, equivalent_full_cycles) was
    # still dividing by capacity_kwh -- the "home" battery ALONE, a
    # holdover from before #563 ever existed. Real live symptom: with a
    # 40.3 kWh home pack + two 60 kWh EVs (160.3 kWh fleet), soc_pct read
    # 296% instead of ~75%, and a real household automation
    # (automation.nimbus_battery_soc_control_ecoflow) started rejecting
    # every write to number.ecoflow_backup_reserve_level (outside its
    # valid 22-100 range) every single solve. Fixed at the source: a real
    # fleet-total capacity, computed once here from the same battery list
    # build_plan() itself just solved against, threaded through publish_
    # plan() as its own parameter rather than overloading capacity_kwh
    # (which stays the home battery's own capacity for whatever legitimately
    # still needs just that -- see publish_plan()'s own parameter list).
    fleet_capacity_kwh = sum(b.capacity_kwh for b in all_batteries)
    # nimbus issue #494 (Signals 5/7 of #489): opt-in, off by default --
    # see const.py's own comment on CONF_SOLVER_OFFER_CURVE_ENABLED for
    # why. Same live-switch-first read as auto_include_known_solar (which
    # moved to solver_inputs/solar.py's build_solar_arrays() in #735
    # stage 1, so it is no longer "above" in this function).
    offer_curve_enabled = bool(cfg.get("solver_offer_curve_enabled"))
    # nimbus issue #496 (Signals 7/7 of #489): opt-in, off by default --
    # see const.py's own comment on CONF_SOLVER_FLEX_SIGNALS_ENABLED for
    # why this one is deliberately NOT just "same reasoning as offer
    # curve" -- a real, live capacity concern (#773), not only convention.
    flex_signals_enabled = bool(cfg.get("solver_flex_signals_enabled"))
    # nimbus issue #696, Stage 2: default TRUE (unlike offer_curve_
    # enabled above) -- see const.py's own comment on CONF_SOLVER_
    # CALIBRATED_OBJECTIVE_ENABLED for the full "household's own
    # explicit, repeated ask to make this the real default now" story.
    # network.build_plan()'s own solve_options= docstring covers the
    # one real scope boundary this switch doesn't override: a solve
    # that ends up a MIP (adequacy loads present, semi-continuous
    # default on) silently keeps today's hand-tuned-magnitude behavior
    # regardless of this switch's state.
    calibrated_objective_enabled = bool(
        cfg.get("solver_calibrated_objective_enabled", True)
    )
    solve_options = lp.CalibratedOptions() if calibrated_objective_enabled else None
    plan = network.build_plan(
        periods=periods,
        grid=grid,
        batteries=all_batteries,
        solar=solar,
        loads=loads,
        sheddable_loads=sheddable_loads,
        adequacy_loads=adequacy_loads,
        thermal_loads=thermal_loads,
        previous_plan=previous_plan,
        risk_aversion=risk_aversion,
        import_price_risk_aversion=import_price_risk_aversion,
        export_price_risk_aversion=export_price_risk_aversion,
        proximal_weight=proximal_weight,
        smoothness_weight=smoothness_weight,
        battery_charge_earliness_budget_kw=battery_charge_earliness_budget_kw,
        compute_offer_curve=offer_curve_enabled,
        compute_signals=flex_signals_enabled,
        solve_options=solve_options,
    )
    solver_shared._LOGGER.debug(
        "Nimbus #757 diag: plan.batteries immediately after build_plan() returns = %s (status=%r)",
        [b.name for b in plan.batteries],
        plan.status,
    )

    return PlanAssembly(
        plan=plan,
        grid=grid,
        all_batteries=all_batteries,
        fleet_capacity_kwh=fleet_capacity_kwh,
        solve_started=solve_started,
        risk_aversion=risk_aversion,
        import_price_risk_aversion=import_price_risk_aversion,
        export_price_risk_aversion=export_price_risk_aversion,
    )
