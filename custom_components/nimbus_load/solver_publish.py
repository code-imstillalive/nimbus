"""Publishing for the Solver cycle -- stage 3 of nimbus issue #735.

#735 proposes splitting `solver_writer.py`'s ~1,650-line `main()` into a
real module structure, with `solver_inputs/` for input gathering and
`solver_publish.py` for the publishes. This module is the start of the
latter, created for a reason the issue itself records: the load-input
block that stage 2 wants to extract has an `ha_post_state()` call sitting
*inside* it, so moving that block wholesale would put a publish inside an
inputs module and contradict the very split being built.

Hoisting the publish out first is therefore a prerequisite for stage 2,
not a detour -- and it is stage 3 work that has to happen anyway.

**Why this function takes fifteen explicit parameters rather than a
context object.** They were computed by walking the call's own AST rather
than read off the screen, because every blocker found on #735 so far
(#860, #861, and stage 2's own three traps) was invisible from reading
the code being moved. The list is exactly what the call reads, no more:
passing a bag would hide which of them this publish actually depends on,
which is the one thing this exercise exists to make explicit.

One of them is worth naming. `summed_18_now_kw` is deliberately **absent**
-- the `state` published here is `load_kw[0]` *after* the live cross-check
anchor overwrites it, while `summed_18_now_kw` is a snapshot taken before.
Publishing the snapshot as `state` while `forecast[0].value` used the
overwritten array is nimbus issue **#100**, a real bug found on a real
install. Anything that "simplifies" this signature by reintroducing that
variable reintroduces the bug with it.

See `solver_inputs/__init__.py` for why the `solver_writer` import below
is deferred and by-module; the reasoning is identical and load-bearing.
"""

from __future__ import annotations

# nimbus #1304 (spec 005): needed by publish_plan and its helpers below.
# Stdlib and third-party, never patched, so a direct import is correct --
# the same reasoning the datetime import above states.
import json
import math
import time

# nimbus issue #495: needed by publish_flex_telemetry_record() below, which
# moved here from solver_writer.py. Stdlib only, never patched, so a direct
# import is correct here -- unlike the solver_writer helpers, which go
# through the deferred `sw` accessor for the reason this module's own
# docstring and solver_inputs/__init__.py both set out.
from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray

# DOWNWARD imports into the solver/ package, which is the lowest layer in
# the nimbus-layers contract -- legal at module scope, no seam needed.
try:
    from .solver import lp, network
    from .solver.regret import evaluate_realized_cost
except ImportError:  # pragma: no cover - standalone/cron path
    from solver import lp, network  # type: ignore[no-redef]
    from solver.regret import evaluate_realized_cost  # type: ignore[no-redef]


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see `solver_inputs/__init__.py` for the full reasoning on
    both counts, and #861 for the concrete failure that made it
    load-bearing: relocating a function also relocates who its internal
    callers resolve, escaping every `patch.object(solver_writer, ...)` in
    the suite and turning a mocked call into a live HTTP request.

    Dual-mode try/except is the same shape every other project-internal
    import in `solver_writer` already uses: this code is loaded both as
    part of the real package and as a bare top-level module by the test
    harness (tests/_solver_path.py) and the standalone/cron deployment.
    """
    try:
        from . import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


def publish_household_load_total_forecast(
    *,
    cfg,
    grid_times,
    n_periods,
    now,
    load_kw,
    load_lower_kw,
    load_upper_kw,
    live_load_kw,
    whole_house_now_kw,
    load_forecast_entities,
    load_forecast_error,
    load_forecast_warnings,
    load_forecast_source_used,
    load_forecast_coverage_hours,
    failed_load_entities,
):
    """Publish `sensor.nimbus_household_load_total_forecast`.

    Moved verbatim out of `main()` -- the body below is the identical
    call, with only the two module-level names (`ha_post_state`,
    `_cfg_num`) rebound onto the deferred module handle. No value is
    recomputed, no ordering is changed, and in particular the
    `round(load_kw[0], 3)` state read is preserved exactly as it was
    (see this module's docstring for why that specific read matters).
    """
    sw = _solver_writer()

    # nimbus issue #937 -- see the three load_forecast_plus_*_kw keys
    # below for why these exist. Rounded to 3 dp to match every other
    # kW figure this publish emits, including forecast[i].value itself.
    def _lead(hours: float) -> float | None:
        v = sw.nowcast_skill.forecast_value_at_lead_hours(
            grid_times, load_kw, now, hours
        )
        return round(v, 3) if v is not None else None

    lead_1h = _lead(1.0)
    lead_6h = _lead(6.0)
    lead_24h = _lead(24.0)

    sw.ha_post_state(
        "sensor.nimbus_household_load_total_forecast",
        # Real bug found via a real-install health check (nimbus repo
        # #100, Mark Purcell): this sensor's own `state` was using
        # summed_18_now_kw -- a snapshot taken BEFORE the live cross-
        # check anchor above (2607-2614) can overwrite load_kw[0] --
        # while `forecast[0].value` below uses load_kw[0] AFTER that
        # same overwrite. Whenever a household configures the whole-
        # house cross-check sensor, this sensor's own headline `state`
        # and its own `forecast[0].value` would silently disagree --
        # two numbers a reasonable reader assumes are the same thing.
        # `sensor.nimbus_solver_config`'s own load_summed_18_now_kw
        # diagnostic (below) is DELIBERATELY left reading the pre-
        # anchor summed_18_now_kw -- its whole documented purpose is
        # comparing two genuinely independent forecasts, not a
        # forecast against an already-live-corrected value (see that
        # field's own comment). This fix is scoped to just this one
        # sensor's own internal state/forecast consistency.
        round(load_kw[0], 3),
        {
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
            "friendly_name": "Nimbus Household Load Total (Summed)",
            "forecast": [
                {
                    "time": grid_times[i].isoformat(),
                    "value": round(load_kw[i], 3),
                    "lower": round(load_lower_kw[i], 3),
                    "upper": round(load_upper_kw[i], 3),
                }
                for i in range(n_periods)
            ],
            "source_entities": load_forecast_entities,
            "load_forecast_source_used": load_forecast_source_used,
            # 2026-08-25, nimbus issue #187 (Mark Purcell, real-install
            # IV&V): v0.89.1's source_sensor/signal_role attributes only
            # ever reached NimbusForecastSensor (subentry-backed load/
            # power-signal sensors) -- this sensor is a genuinely
            # different class (_NimbusSolverPushSensor, a pure REST-
            # attribute mirror), so it never got either key at all, a
            # real missed code path, not an intentional scope boundary.
            # "other", matching every Load subentry's own signal_role
            # (there is no distinct SIGNAL_ROLE_LOAD in this project --
            # see const.py's own convention) -- this sensor's role is
            # definitionally a load aggregate. source_sensor is the
            # single real entity_id only when the single-sensor path is
            # actually active; when the richer multi-circuit summing
            # path is active there genuinely isn't one source to name,
            # so it's honestly None here -- source_entities above is
            # already the correct, richer answer for that case.
            "signal_role": "other",
            "source_sensor": cfg["solver_load_forecast_sensor"]
            if not load_forecast_entities
            else None,
            # NEW (2026-08-25, issue #112: "solver horizon 96.3h exceeds
            # subentry forecast horizon 48h") -- the REAL forecast
            # coverage this run's load_kw is backed by, in hours ahead
            # of `now`. None if it couldn't be determined. Compare
            # against horizon_hours (sensor.nimbus_solver_battery_
            # forecast, and the print() line below): whenever this is
            # smaller, every period beyond it is resample_forecast()'s
            # own flat-hold padding, not a real forecast -- see
            # compute_forecast_coverage_hours()'s own docstring.
            "load_forecast_coverage_hours": round(load_forecast_coverage_hours, 1)
            if load_forecast_coverage_hours is not None
            else None,
            "failed_load_entities": failed_load_entities,
            # NEW (2026-08-24, issue #105): entity_id -> the exact real
            # reason each failed_load_entities member was excluded --
            # was previously invisible on the multi-circuit summing
            # path (a bare "unavailable" in the log, nothing on this
            # sensor at all). See sum_load_forecasts()'s own docstring
            # for the full "why this exists" story.
            "load_forecast_warnings": load_forecast_warnings,
            "whole_house_cross_check_now_kw": round(whole_house_now_kw, 3)
            if whole_house_now_kw is not None
            else None,
            # nimbus issue #429: the genuine live meter reading, distinct
            # from whole_house_cross_check_now_kw above (that field is
            # the meter's own FORECAST model, not a live reading -- see
            # this block's own comment further up). None when
            # unconfigured or the read failed, same honest-absence
            # convention as every other optional diagnostic here.
            "whole_house_live_now_kw": round(live_load_kw, 3)
            if live_load_kw is not None
            else None,
            "inverter_self_consumption_kw": sw._cfg_num(
                cfg, "solver_inverter_self_consumption_kw", 0.0
            ),
            # None on success -- the exact human-readable reason on
            # failure, real proposal #2 from nimbus repo issue #66.
            "load_forecast_source_error": load_forecast_error,
            # nimbus issue #937: three scalars that make forecast error
            # as a FUNCTION OF LEAD TIME measurable from ordinary
            # recorder history. The full `forecast` array is
            # unrecorded (#625/#890's 16 KB cap), so today no historical
            # forecast exists at any lead time and #937 has only two
            # points on the curve: one-step-ahead, where the forecaster
            # beats persistence by ~50% on MAE, and day-ahead, where its
            # dispatch value was negative on 11 of 14 real days. The
            # shape between them separates "the model is wrong" from
            # "the recursive multi-step path degrades".
            #
            # Read them back by comparing the value recorded at time T
            # against the REAL load at T + the lead time. Same "recover
            # it from history" approach the household chose for #919 --
            # no new storage, and three floats are nothing against the
            # attribute budget. None beyond the published horizon rather
            # than clamped to the last period.
            "load_forecast_plus_1h_kw": lead_1h,
            "load_forecast_plus_6h_kw": lead_6h,
            "load_forecast_plus_24h_kw": lead_24h,
            "generated_at": now.isoformat(),
        },
    )


# nimbus issue #495 (Signals 6/7 of #489), moved here rather than added to
# solver_writer.py. The size ratchet correctly objected: this is a 64-line
# function and landing it in the god-module would have pushed the over-60
# count 61 -> 62, which is exactly what that gate exists to stop. Splitting
# it to get under the line would have been gaming the metric -- the body is
# ~40 lines and the count is inflated by a nine-parameter keyword-only
# signature and an eleven-line docstring, both of which earn their space.
#
# This module is where a publish belongs (#735 stage 3, #1304), so the fix
# is placement, not surgery. Moved verbatim: every name that resolved in
# solver_writer's namespace is reached as `sw.<name>`, and stripping those
# prefixes back off reproduces the original bytes exactly.
#
# It stays re-exported from solver_writer, so existing call sites and any
# `patch.object(solver_writer, "publish_flex_telemetry_record", ...)` keep
# resolving the identical object.
try:
    from . import flex_telemetry
except ImportError:  # pragma: no cover - standalone/cron path
    import flex_telemetry  # type: ignore[no-redef]
try:
    from . import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]


def publish_flex_telemetry_record(
    cfg: dict,
    plan,
    now: datetime,
    *,
    batteries,
    import_price: float,
    export_price: float,
    import_limit_kw: float,
    export_limit_kw: float,
    period_hours: float,
) -> None:
    """Pushes `sensor.nimbus_flex_telemetry` (#495) -- a no-op, reason
    logged at DEBUG, when no valid record can be built.

    The record is nested under ONE `record` attribute rather than spread.
    The schema declares `additionalProperties: false`, so a spread record
    plus the housekeeping attributes HA and this repo both add
    (`friendly_name`, `nimbus_version`, `generated_at`) would not validate
    as read -- a consumer would have to know which keys to strip, and get
    it right again whenever either side adds one. Nested, the thing to POST
    is `attributes.record`, whole. A deliberate, stated departure from
    #495's own "attributes = the full record" wording."""
    sw = _solver_writer()
    try:
        build = sw.build_flex_telemetry_record(
            cfg,
            plan,
            now,
            batteries=batteries,
            import_price=import_price,
            export_price=export_price,
            import_limit_kw=import_limit_kw,
            export_limit_kw=export_limit_kw,
            period_hours=period_hours,
        )
    except Exception as e:  # noqa: BLE001 - never take the solve down
        solver_shared._LOGGER.warning(
            "Nimbus flex telemetry: record build failed: %s", e
        )
        return
    if build.record is None:
        solver_shared._LOGGER.debug(
            "Nimbus flex telemetry: no record this cycle -- %s", build.reason
        )
        return
    if build.clamped_fields:
        solver_shared._LOGGER.warning(
            "Nimbus flex telemetry: field(s) clamped into the schema's own "
            "[%s, %s] $/kWh range: %s",
            flex_telemetry.PRICE_MIN,
            flex_telemetry.PRICE_MAX,
            ", ".join(build.clamped_fields),
        )
    sw.ha_post_state(
        sw.FLEX_TELEMETRY_ENTITY_ID,
        build.record["interval_start_utc"],
        {
            "friendly_name": "Nimbus Flex Telemetry",
            "record": build.record,
            # No attribute here shares a NAME with a record field, on
            # purpose: a duplicate would have two places to disagree, and
            # the whole reason the record is nested is that the attribute
            # dict and the record are different objects with different
            # contracts. `schema_version` lives in the record alone.
            "clamped_fields": list(build.clamped_fields),
            "generated_at": datetime.now(UTC).astimezone(sw.LOCAL_TZ).isoformat(),
        },
    )


# --------------------------------------------------------------------
# nimbus issue #1304 (spec 005, Phase 5 of #1298): publish_plan and its
# thirteen helpers, moved here verbatim from solver_writer.py.
#
# Two qualifications only, both required by spec 005 step 1:
#   _LOGGER.*       -> solver_shared._LOGGER.*  (this module's own
#                      convention, already used by
#                      publish_flex_telemetry_record above)
#   PLAN_STATE_PATH -> sw.PLAN_STATE_PATH
#
# PLAN_STATE_PATH deliberately does NOT move: its primary reader
# load_previous_plan() stays in solver_writer.py (Phase 4's target, not
# this one), and test_solver_writer_load_previous_plan.py patches it as
# solver_writer.PLAN_STATE_PATH against BOTH functions. A plain
# `from .solver_writer import PLAN_STATE_PATH` binds once at import time
# and would silently break that patch -- nimbus #861's exact failure
# mode. Reaching it through the deferred accessor keeps it working.
# --------------------------------------------------------------------
ENTITY_ID = "sensor.nimbus_solver_battery_forecast"

_PIN_MATCH_TOLERANCE_KW = 0.01

_LOAD_FORECAST_SOURCE_KEYS = (
    "load_forecast_source_policy",
    "load_forecast_source_selected",
    "load_forecast_persistence_weight",
    "load_forecast_source_reason",
    "load_forecast_source_days_scored",
    "load_forecast_source_days_persistence_won",
    "load_forecast_source_mean_value_add_dollars",
)


def _risk_aversion_effect_now(
    plan: network.Plan,
    solar_kw: list[float],
    import_price: list[float],
    export_price: list[float],
) -> dict[str, float | None]:
    """Real, live proof the three risk-aversion sliders (Load/Solar,
    Import Price, Export Price -- number.py's own _DESCRIPTIONS) are
    actually reaching the LP, not a UI-only control. 2026-09-07, direct
    household finding: "moved slider, nothing happened" is genuinely
    ambiguous between "the slider/write path is broken" and "the
    mechanism is a correct no-op right now because the underlying
    forecast band has ~zero width this period" -- nothing on any
    dashboard could tell those apart, so the household had no way to
    confirm the mechanism was even wired up correctly.

    Reads plan.effective_solar_kw/effective_import_price/
    effective_export_price (network.py's own _risk_adjusted()/
    _risk_adjusted_one_sided() output, exposed on Plan specifically for
    this) against the raw forecast/price arrays this SAME solve was fed
    -- period 0 only, "right now" being the only period a household
    looking at a live dashboard actually cares about. A nonzero
    raw-vs-effective gap is direct, physical proof the slider is having
    an effect this cycle; a zero gap at a nonzero slider value is
    equally real proof the band is currently zero-width, not that
    anything is broken.

    Returns every value None (not 0.0 -- a real gap of zero and "no
    data to compare" must stay distinguishable) whenever plan wasn't
    optimal or is a bare Plan() built without these fields (every
    existing test that constructs Plan directly, pre-2026-09-07).
    """
    if not plan.is_optimal or plan.effective_solar_kw.size == 0:
        return {
            "solar_risk_effect_now_kw": None,
            "import_price_risk_effect_now": None,
            "export_price_risk_effect_now": None,
        }
    return {
        "solar_risk_effect_now_kw": round(
            float(solar_kw[0]) - float(plan.effective_solar_kw[0]), 3
        ),
        "import_price_risk_effect_now": round(
            float(plan.effective_import_price[0]) - float(import_price[0]), 4
        ),
        "export_price_risk_effect_now": round(
            float(export_price[0]) - float(plan.effective_export_price[0]), 4
        ),
    }


def resolve_fixed_export_charge_clamp(
    fixed_export_kw: NDArray[np.float64] | None,
    *,
    gated_charge_kw: NDArray[np.float64],
    aggregate_charge_kw: NDArray[np.float64],
    discharge_kw: NDArray[np.float64],
    grid_import_kw: NDArray[np.float64],
) -> tuple[
    NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], NDArray[np.bool_]
]:
    """The P2P fixed-export charge backstop, as a pure function.

    Returns `(net_battery, charge_kw, grid_import_kw, violation_mask)`.

    Extracted 2026-09-15 for nimbus issue #923 -- same precedent as
    compute_binding_constraint_label() above, and for the same reason:
    this code had no test of its own, and it does not merely warn. It
    rewrites three published quantities, so a wrong premise here
    silently publishes wrong numbers rather than failing loudly.

    **`gated_charge_kw` must be the charge array of the battery the
    LP gate was actually applied to, not the fleet total.** network.py
    bounds charging during a commitment with

        charging_ub_during_fixed_window(t, grid, b.max_charge_kw)
        if b_idx == 0 else b.max_charge_kw

    -- a hard `ub=0` for battery 0 only. Every battery participant
    (#467: a second inverter, an EV) keeps its ordinary ceiling and may
    legitimately charge inside a committed window; `grid_export` is
    pinned there, so such a charge draws from solar or import rather
    than from committed export.

    `Plan.battery_charge_kw` is the documented SUMMED AGGREGATE across
    all of them. Comparing that aggregate against a per-battery bound is
    what #923 was: on a multi-battery install a participant's legal
    charge read as an impossible solve, and the clamp then erased it
    from the published plan and understated grid import by the same
    amount, every period of every committed window. The check was right
    when written (2026-08-22, one battery, aggregate == battery 0);
    #467 changed what the aggregate means and this was not revisited.

    Only the gated battery's own charge is removed. A participant's
    charge in the same period is real and survives into every published
    figure.
    """
    net_battery: NDArray[np.float64] = (discharge_kw - aggregate_charge_kw).astype(
        np.float64
    )
    no_violation: NDArray[np.bool_] = np.zeros(net_battery.shape, dtype=np.bool_)
    if fixed_export_kw is None:
        return net_battery, aggregate_charge_kw, grid_import_kw, no_violation

    violation = (~np.isnan(fixed_export_kw)) & (gated_charge_kw > 0.05)
    if not violation.any():
        return net_battery, aggregate_charge_kw, grid_import_kw, no_violation

    kept_charge_kw = np.maximum(0.0, aggregate_charge_kw - gated_charge_kw)
    return (
        np.where(violation, discharge_kw - kept_charge_kw, net_battery),
        np.where(violation, kept_charge_kw, aggregate_charge_kw),
        np.where(
            violation,
            np.maximum(0.0, grid_import_kw - gated_charge_kw),
            grid_import_kw,
        ),
        violation,
    )


def compute_binding_constraint_label(
    plan: network.Plan,
    export_limit_kw: float,
    import_limit_kw: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    period_0_hours: float,
    fixed_export_kw_now: float | None = None,
) -> tuple[str, float | None]:
    """ "What's binding RIGHT NOW (period 0)" -- Mark Purcell's audit item
    #3 (2026-08-18), deliberately a SMALL summary rather than the raw
    plan.duals/reduced_costs dicts (those can hold thousands of entries
    at real 365-period production scale, real risk of blowing past HA's
    16384-byte recorder attribute limit -- a repeatedly-hit constraint
    elsewhere in this project's own history).

    Extracted as its own standalone, directly-testable function
    (2026-08-24) -- same precedent as resolve_max_discharge_kw() above --
    specifically because of the real bug this exact refactor was built
    to fix and now has real unit-test coverage for, not just source-
    inspection:

    2026-08-24 fix (Mark Purcell, nimbus #125/#133, real repro): a
    nonzero reduced cost on e.g. battery_discharge_0 used to be labelled
    "Battery max discharge power" UNCONDITIONALLY -- but a real, nonzero
    LP reduced cost fires whenever a variable is pinned at EITHER of its
    own bounds, not only its upper/capacity bound (a core LP optimality
    property: a non-basic variable's reduced cost is only ever nonzero
    when it's sitting exactly at a bound -- lower OR upper). Mark's own
    plan showed the battery CHARGING at period 0 (not discharging at
    all) while this label still reported "Battery max discharge power"
    -- the true story was battery_discharge_0 pinned at its LOWER bound
    (0, a genuine "not economical to discharge right now" decision), not
    the 24kW ceiling his own config actually set (confirmed separately,
    by direct source read, that max_discharge_kw is applied UNSCALED as
    the LP variable's own upper bound -- `ub=battery.max_discharge_kw`
    at network.py's battery_discharge_{t} construction, no efficiency/
    SoC derating on the bound itself -- ruling out both of Mark's own
    suggested "second override path" hypotheses: a second hardcoded
    entity slug, confirmed absent via a repo-wide grep for "logger_";
    and SoC/efficiency scaling of the bound, confirmed absent by reading
    network.py's own variable construction directly).

    Genuinely ambiguous from the OLD label alone which of these two,
    very different real stories was true. Now disambiguated by checking
    the variable's own real SOLVED value (plan.battery_discharge_kw[0],
    etc.) against its two real bounds: only the genuine "pinned at the
    real ceiling" case keeps the original 4 label strings (byte-
    identical, no compatibility break for anyone already reading this
    field for THAT case); the "pinned at zero" case gets its own new,
    distinct, honest label instead of silently reusing the ceiling
    wording it was never actually describing.

    Returns (label, shadow_price_per_kwh) -- shadow_price is None only
    when nothing is currently binding (label == "Nothing currently
    binding"), matching this function's one and only caller's own
    existing external contract (the pushed sensor attribute shape).

    nimbus issue #662 (Mark Purcell): `period_0_hours` -- these 4
    variables' own LP objective coefficients are all scaled by period 0's
    own duration (`p.set_cost(grid_import[0], effective_import_price[0]
    * hours[0])`, same construction #662 itself diagnosed for the
    power_balance_t0 ROW dual), so each one's own raw reduced cost comes
    out in "$ per kW of bound," carrying the identical implicit
    `x hours[0]` factor -- dividing by it recovers the true $/kWh
    marginal value, the EXACT SAME correction `forced_import_cost`
    already applies (`reduced_costs[...] / hours[t]`, network.py). Found
    via the same #662 audit, same file, same mechanism -- not previously
    reported, but the identical bug class Mark's own issue names.
    """
    _BINDING_FAMILIES = {
        # key: (exact original ceiling label, short name for the "at
        # zero" case, real solved-value array, real configured limit)
        "grid_export_0": (
            "Grid export limit",
            "Grid export",
            plan.grid_export_kw,
            export_limit_kw,
        ),
        "grid_import_0": (
            "Grid import limit",
            "Grid import",
            plan.grid_import_kw,
            import_limit_kw,
        ),
        "battery_charge_0": (
            "Battery max charge power",
            "Battery charge",
            plan.battery_charge_kw,
            max_charge_kw,
        ),
        "battery_discharge_0": (
            "Battery max discharge power",
            "Battery discharge",
            plan.battery_discharge_kw,
            max_discharge_kw,
        ),
    }
    binding_now = None
    binding_now_value_per_kwh = None
    for var_key, (
        ceiling_label,
        short_name,
        values,
        limit_kw,
    ) in _BINDING_FAMILIES.items():
        val = plan.reduced_costs.get(var_key, 0.0)
        if abs(val) > 1e-6 and (
            binding_now_value_per_kwh is None
            or abs(val) > abs(binding_now_value_per_kwh)
        ):
            solved_value = float(values[0])
            if limit_kw > 1e-9 and solved_value >= limit_kw - 1e-6:
                # Genuinely at the real ceiling -- exact original wording,
                # byte-identical, no compatibility break for anyone
                # already reading this field for this specific case.
                binding_now = ceiling_label
            elif (
                var_key == "grid_export_0"
                and fixed_export_kw_now is not None
                and not math.isnan(fixed_export_kw_now)
                and abs(solved_value - float(fixed_export_kw_now))
                <= _PIN_MATCH_TOLERANCE_KW
            ):
                # nimbus issue #921: a THIRD bound this variable really
                # has, and the only one not in _BINDING_FAMILIES above.
                # p2p_export.grid_export_bounds() pins grid_export[t] to
                # lb == ub == the committed rate for every period under a
                # real P2P export commitment -- so the variable is at a
                # bound (nonzero reduced cost, exactly as LP optimality
                # says) at a value that is neither 0 nor export_limit_kw.
                # Before this branch existed that landed in the "shouldn't
                # happen" case below and told a correctly-configured P2P
                # household its solver was confused, every period of every
                # evening block -- the hours where the most money moves
                # and where someone is most likely to be reading this
                # field to understand the plan.
                #
                # The tolerance is the whole point of this second pass.
                # v0.94.324 shipped this branch testing `abs(solved -
                # pin) <= 1e-6` and, on the very install it was written
                # for, it never fired: the attribute still published
                # "12.00 kW (unexpected ...)" with a 12.0 kW commitment
                # genuinely in force that period. A small synthetic LP
                # returns a pinned variable at exactly its bound (checked
                # directly: delta 0.000e+00), which is what made 1e-6
                # look safe -- and that does not generalise to one
                # variable out of a ~12,000-column two-phase MIP.
                #
                # Still two-sided, deliberately. A value well BELOW the
                # commitment is as impossible as one well above it, since
                # grid_export_bounds() returns (pin, pin); and #694's
                # price-spike override, which relaxes the bounds to (pin,
                # export_limit_kw) at t=0, is exactly a case where export
                # rises above the commitment and must NOT be reported as
                # pinned. Both are real states worth surfacing, so the
                # test is "within a real tolerance of the pin", not "at
                # or below it".
                #
                # Reports the COMMITMENT, not the solved value: the
                # commitment is the exact configured number the household
                # recognises, and the solved value is the same quantity
                # plus whatever the solver's own residual is.
                binding_now = (
                    f"{short_name} pinned at {float(fixed_export_kw_now):.2f} kW "
                    "by P2P export commitment"
                )
            elif solved_value <= 1e-6:
                # Pinned at zero -- a real "not worth it right now"
                # economic decision, NOT a capacity constraint. Distinct
                # from the ceiling case on purpose (see docstring above).
                #
                # nimbus issue #951 (Mark Purcell, 48-hour IV&V #950):
                # this branch used to sit ABOVE the P2P-pin branch, which
                # left a residual of the exact bug class #921 was filed to
                # fix. `fetch_p2p_fixed_export_kw()` deliberately pins
                # export to 0.0 for `solver_post_window_self_consume_hours`
                # after midnight on any block configured with end_hour=24,
                # matching the real automation's own self-consume window --
                # so 0.0 is a genuine, reachable COMMITMENT, not an absence
                # of one. Evaluated first, it intercepted that case and
                # told a correctly-configured P2P household its solver saw
                # no economic reason to export, during hours when export
                # was in fact deterministically forbidden.
                #
                # Safe to demote below the pin branch, and this is the part
                # worth not re-deriving: a period with NO commitment never
                # reaches here carrying 0.0. `fetch_p2p_fixed_export_kw()`
                # returns None when no block is configured at all, and
                # defaults an unmatched period to float("nan") -- both of
                # which the pin branch's own guards reject. 0.0 appears
                # only for the real post-midnight pin, so hoisting the pin
                # check cannot relabel a genuine "not economical" period.
                binding_now = f"{short_name} at zero (not economical right now)"
            else:
                # Shouldn't happen for a variable with a genuinely
                # nonzero reduced cost (LP optimality: only ever nonzero
                # exactly at a bound) -- represented honestly rather
                # than assumed, matching this module's own "never paper
                # over an unexpected state" convention.
                #
                # The LP-optimality reasoning above is sound; what makes
                # this branch reachable is the unstated assumption that
                # 0 and limit_kw are a variable's ONLY bounds. #921 found
                # one that isn't (the P2P export pin, handled directly
                # above). Anything still landing here is a bound nothing
                # in this function models -- which is worth saying loudly
                # rather than smoothing over, so keep this branch.
                #
                # It now names this period's own P2P commitment when there
                # is one. v0.94.324's branch above failed silently on the
                # exact install it was written for, and the message it
                # fell through to gave no way to tell "no commitment this
                # period" from "a commitment the comparison rejected".
                # Carrying the number makes the next misfire diagnosable
                # from the published attribute alone.
                # Only on grid export -- the commitment bounds that one
                # variable, and naming it beside a battery's own binding
                # constraint would be a non-sequitur.
                _pin_note = (
                    f", P2P commitment {float(fixed_export_kw_now):.2f} kW"
                    if var_key == "grid_export_0"
                    and fixed_export_kw_now is not None
                    and not math.isnan(fixed_export_kw_now)
                    else ""
                )
                binding_now = (
                    f"{short_name} at {solved_value:.2f} kW "
                    f"(unexpected -- neither its 0 nor {limit_kw:.2f} kW bound"
                    f"{_pin_note})"
                )
            binding_now_value_per_kwh = round(val / period_0_hours, 4)
    if binding_now is None:
        binding_now = "Nothing currently binding"
    return binding_now, binding_now_value_per_kwh


def compute_cost_breakdown(
    net_costs: list[float],
    total_cost: float | None,
    degradation_cost_per_kwh: float,
    total_throughput_kwh: float,
    charge_cost: float,
    total_charge_kwh: float,
    discharge_cost_arr: NDArray[np.float64],
    battery_discharge_kw: NDArray[np.float64],
    period_hours: NDArray[np.float64],
    soc_penalty_cost: float = 0.0,
    grid_import_excess_penalty_cost: float = 0.0,
) -> dict[str, float]:
    """Named cost-component breakdown for the solver diagnostics dump
    (2026-08-25, nimbus issue #149 -- Mark Purcell's own executable
    reconciliation tests, run against both the v0.80 and v0.81.0 dumps:
    `total_cost` could not be reconstructed from anything else in the
    dump, off by exactly degradation+charge_fee+discharge_fee minus the
    #144 terminal-value credit).

    Extracted as its own standalone, directly-testable function (2026-08-25)
    -- same precedent as compute_binding_constraint_label() above.

    `grid_net` sums the caller's own already-computed per-period net_cost
    values (grid-only cash flow -- see that field's own comment at its
    construction site) rather than re-deriving effective/risk-adjusted
    prices a second time here -- guarantees this figure matches what the
    LP actually saw on the grid side, not an approximation of it.

    `degradation`/`charge_fee`/`discharge_fee` mirror network.py's own LP
    cost coefficients exactly: degradation_cost_per_kwh is applied
    additively to BOTH the charge and discharge legs (see build_plan's own
    "(charge_cost_arr[t] + battery.degradation_cost_per_kwh)" comment), so
    its total here is degradation_cost_per_kwh * total_throughput_kwh;
    discharge_fee sums discharge_cost_arr[t] per period rather than
    multiplying by a flat scalar, since a real household's own LocalVolts
    schedule (battery_discharge_cost_rate) makes it hour-varying, not a
    constant -- charge_cost IS a flat scalar in every branch that builds
    it, so charge_fee is the cheaper flat multiplication.

    `soc_penalty` (nimbus issue #781) is Plan.soc_penalty_cost passed
    straight through -- the real dollar total of every battery's own soft
    min/max-SoC violation penalty, computed directly from the same solved
    underfill/overfill variables and rate network.py's own build_plan()
    already used to cost them. Broken out as its OWN explicit term
    (not left inside the residual below) because it is deliberately
    LARGE ("dominant by construction," a bare $/kWh on the state
    violation applied every period, not scaled by hours[t]) and, before
    this field existed, had no explicit line item anywhere: a household's
    own real 3-battery solve showed a $1678 `terminal_value_credit` that
    was actually almost entirely this penalty (real batteries genuinely
    sitting below their configured floor), misrepresented as if it were
    salvage/terminal value earned rather than a "your battery is below
    its safety floor" warning sign.

    `grid_import_excess_penalty` (nimbus issue #788) is Plan.
    grid_import_excess_penalty_cost passed straight through -- the real
    dollar markup of network.py's own `import_excess_penalty_rate` on
    whatever grid_import_excess volume this cycle's release valve
    (nimbus issue #390) actually used. Broken out as its OWN explicit
    term for the identical reason soc_penalty was (#781): `grid_net`
    above only ever prices the COMBINED grid_import_kw (which already
    folds grid_import_excess_kw in) at the plain effective_import_price
    rate -- the penalty markup itself had no explicit line item anywhere
    before this field existed, so it fell entirely into the residual
    below, misrepresenting a real "the LP had to blow the configured
    import cap to stay feasible" warning sign as if it were terminal
    value/salvage credit. 0.0 (a genuine no-op) whenever the release
    valve was never needed this cycle, the common case.

    `terminal_value_credit` is deliberately the RESIDUAL (total_cost minus
    the six terms above, `soc_penalty` and `grid_import_excess_penalty`
    now included), not a re-implementation of
    terminal_value_breakpoints_for()'s own piecewise segment math in a
    second place -- residual-by-construction means this always reconciles
    exactly (Mark's own test #2: grid_net + degradation + charge_fee +
    discharge_fee + soc_penalty + grid_import_excess_penalty +
    terminal_value_credit == total_cost), and its value already IS what
    an operator wants to see (the real terminal-value/salvage credit's
    total economic effect, whatever combination of checkpoints produced
    it), without a second implementation that could silently drift from
    the LP's own real one over time.
    """
    grid_net_cost = sum(net_costs)
    degradation_cost = degradation_cost_per_kwh * total_throughput_kwh
    charge_fee_cost = charge_cost * total_charge_kwh
    discharge_fee_cost = sum(
        float(discharge_cost_arr[i])
        * float(battery_discharge_kw[i])
        * float(period_hours[i])
        for i in range(len(battery_discharge_kw))
    )
    terminal_value_credit = (total_cost or 0.0) - (
        grid_net_cost
        + degradation_cost
        + charge_fee_cost
        + discharge_fee_cost
        + soc_penalty_cost
        + grid_import_excess_penalty_cost
    )
    return {
        "grid_net": round(grid_net_cost, 4),
        "degradation": round(degradation_cost, 4),
        "charge_fee": round(charge_fee_cost, 4),
        "discharge_fee": round(discharge_fee_cost, 4),
        "soc_penalty": round(soc_penalty_cost, 4),
        "grid_import_excess_penalty": round(grid_import_excess_penalty_cost, 4),
        "terminal_value_credit": round(terminal_value_credit, 4),
    }


def periods_within_hours(period_hours: NDArray[np.float64], hours: float) -> int:
    """nimbus issue #630: the number of leading periods (from "now")
    whose cumulative duration is at most `hours` -- e.g. how many of a
    tiered grid's own periods fall inside the next 24 real hours, used
    to slice compute_cost_band()'s own inputs down to a shorter-horizon
    band. Always at least 1, so a grid whose very first period alone
    already exceeds `hours` (a coarse, late-horizon-only grid, or a
    pathological single-period plan) still gets a real, non-empty
    slice rather than an empty array."""
    cum = np.cumsum(period_hours)
    return max(1, int(np.searchsorted(cum, hours, side="right")))


def compute_cost_band(
    *,
    period_hours: NDArray[np.float64],
    load_lower_kw: NDArray[np.float64],
    load_upper_kw: NDArray[np.float64],
    solar_kw: NDArray[np.float64],
    import_price: NDArray[np.float64],
    export_price: NDArray[np.float64],
    charge_committed_kw: NDArray[np.float64],
    discharge_committed_kw: NDArray[np.float64],
    charge_cost: float,
    discharge_cost_arr: NDArray[np.float64],
    final_soc_kwh: float,
    salvage_value: float,
    import_limit_kw: float,
    export_limit_kw: float,
) -> dict[str, float] | None:
    """Cost-band diagnostic (2026-08-25, nimbus issue #147: "the load
    forecast's own uncertainty band is up to 8x the total cost being
    optimised, and the LP never sees it") -- re-costs the COMMITTED
    dispatch (this solve's own real charge/discharge decisions, held
    fixed) against the load forecast's own stated lower/upper
    confidence bounds instead of its point value, via
    evaluate_realized_cost() (regret.py) -- the same "hold dispatch
    fixed, recompute the real balance" technique compute_quality_
    report()'s own J_ach already uses, just swapping which load series
    it's evaluated against. Needs no LP change -- the LP already ran;
    this is read-only post-hoc analysis on its output.

    Deliberately prices export at the plain base export_price only, no
    P2P bonus term -- the SAME residual-only convention compute_
    quality_report()'s own J_ref/J_ach split already uses (see that
    module's docstring for why): evaluate_realized_cost() has no
    concept of GridConfig's own two-tier bonus mechanic, and a bonus-
    aware band would need real settled bonus $, not something
    available for a still-open future plan. This makes the returned
    band a real, honest LOWER BOUND on the true swing, not the full
    picture -- callers should surface that caveat alongside the field,
    not treat it as the complete answer.

    Returns {"lower", "upper", "width"} (all $, "width" = upper -
    lower), or None if the re-costing itself fails for any reason --
    this is a read-only diagnostic and must never break the real solve
    it's reporting on.
    """
    try:
        lower_cost = evaluate_realized_cost(
            hours=period_hours,
            load_real_kw=np.asarray(load_lower_kw),
            solar_real_kw=np.asarray(solar_kw),
            import_price_real=np.asarray(import_price),
            export_price_real=np.asarray(export_price),
            charge_committed_kw=charge_committed_kw,
            discharge_committed_kw=discharge_committed_kw,
            charge_cost=charge_cost,
            discharge_cost=discharge_cost_arr,
            final_soc_kwh=final_soc_kwh,
            salvage_value=salvage_value,
            grid_import_limit_kw=import_limit_kw,
            grid_export_limit_kw=export_limit_kw,
        ).total_cost
        upper_cost = evaluate_realized_cost(
            hours=period_hours,
            load_real_kw=np.asarray(load_upper_kw),
            solar_real_kw=np.asarray(solar_kw),
            import_price_real=np.asarray(import_price),
            export_price_real=np.asarray(export_price),
            charge_committed_kw=charge_committed_kw,
            discharge_committed_kw=discharge_committed_kw,
            charge_cost=charge_cost,
            discharge_cost=discharge_cost_arr,
            final_soc_kwh=final_soc_kwh,
            salvage_value=salvage_value,
            grid_import_limit_kw=import_limit_kw,
            grid_export_limit_kw=export_limit_kw,
        ).total_cost
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        # nimbus issue #363 (Mark Purcell, codebase review): swallow stays
        # (this is a read-only, best-effort diagnostic re-costing, never
        # worth breaking the real solve over), but now with a breadcrumb.
        solver_shared._LOGGER.debug(
            "Nimbus Solver: compute_cost_band failed", exc_info=True
        )
        return None
    return {
        "lower": round(lower_cost, 4),
        "upper": round(upper_cost, 4),
        "width": round(upper_cost - lower_cost, 4),
    }


def save_plan_state(
    plan: network.Plan, period_hours_arr: list[float], period_start: datetime
) -> None:
    """Persist this solve's own dispatch arrays for the NEXT run's
    load_previous_plan() to pick up. Best-effort -- a failure here
    should never take down an otherwise-successful solve."""
    sw = _solver_writer()
    try:
        with open(sw.PLAN_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "status": plan.status,
                    "period_start": period_start.isoformat(),
                    "period_hours": list(period_hours_arr),
                    "battery_charge_kw": plan.battery_charge_kw.tolist(),
                    "battery_discharge_kw": plan.battery_discharge_kw.tolist(),
                    "grid_import_kw": plan.grid_import_kw.tolist(),
                    "grid_export_kw": plan.grid_export_kw.tolist(),
                    # nimbus issue #467: this writer only ever solves one
                    # real battery ("home", see battery_cfg's own
                    # comment above) -- persisting its own name alongside
                    # the (already single-battery) aggregate arrays above
                    # is enough for load_previous_plan() to reconstruct a
                    # real Plan.batteries entry that keeps per-participant
                    # cross-solve stability (network.py's own #467
                    # plumbing) working across a restart, not just the
                    # pre-#467 aggregate-only continuity.
                    "battery_name": plan.batteries[0].name
                    if plan.batteries
                    else "home",
                },
                f,
            )
    except OSError as e:
        solver_shared._LOGGER.warning(
            "Nimbus Solver: could not save plan state (%s) -- next run will "
            "solve without stability continuity",
            e,
        )


def _dispatch_source_breakdown(
    battery_kw: float,
    solar_kw_i: float,
    load_kw_i: float,
    *,
    grid_export_kw_i: float | None = None,
) -> tuple[str, str, float, str, float]:
    """Real per-period source/destination breakdown for the plan table
    (2026-08-28, direct ask: "the plan table should also say where it
    is coming from -- such as solar, grid, battery... not just
    charging... it should say direction, and then from/to what
    source"). The LP itself has no per-source flow variables to read
    back (BatteryConfig is a single aggregate on a single copper-plate
    bus -- see its own docstring), so this is an honest MERIT-ORDER
    decomposition of the same flow balance the LP already solved, not a
    dual/shadow-price attribution: solar serves load first, any surplus
    charges the battery, anything still short comes from grid import;
    symmetrically on a discharge period, the battery serves load before
    any of it is attributed to export. Matches how a household actually
    reasons about "why is it charging/discharging right now."

    nimbus issue #629 (Mark Purcell): on a discharge period, "Grid" %
    used to be a pure residual (discharge minus whatever served load),
    same bug as _flow_decomposition()'s own pre-#629 battery_to_grid --
    implying a real export/grid-fed-charge that never happened whenever
    a genuine AC-bus loss (or, on a multi-battery fleet, energy this
    function has no visibility into moving to another participant)
    left a residual. `grid_export_kw_i`, when given, caps "Grid" on a
    discharge period at the LP's own real grid_export_kw for THIS
    period -- the same honest-cap technique as the seven-flow fix.
    Optional and defaults to None (the exact pre-#629 uncapped
    behavior) so every existing caller/test not yet passing a real
    figure is completely unaffected; the real production caller
    (this file's own per-period forecast loop) always passes it.
    b_pct can legitimately read below what full residual attribution
    would have shown once capped -- a and b no longer have to sum to
    100% the moment a real, honestly-unattributable residual exists,
    which is the entire point: summing to 100% by construction was
    exactly what made the old uncapped version misleading.

    Returns (direction, source_a_label, source_a_pct, source_b_label,
    source_b_pct). direction is "charge"/"discharge"/"idle".
    """
    _CHARGE_EPS = 1e-3
    if battery_kw <= -_CHARGE_EPS:
        charge_kw = -battery_kw
        solar_surplus = max(0.0, solar_kw_i - load_kw_i)
        from_solar = min(solar_surplus, charge_kw)
        from_grid = charge_kw - from_solar
        return (
            "charge",
            "Solar",
            round(from_solar / charge_kw * 100, 1),
            "Grid",
            round(from_grid / charge_kw * 100, 1),
        )
    if battery_kw >= _CHARGE_EPS:
        discharge_kw = battery_kw
        remaining_load = max(0.0, load_kw_i - solar_kw_i)
        to_load = min(discharge_kw, remaining_load)
        to_grid_residual = discharge_kw - to_load
        to_grid = (
            to_grid_residual
            if grid_export_kw_i is None
            else min(to_grid_residual, max(0.0, grid_export_kw_i))
        )
        return (
            "discharge",
            "Load",
            round(to_load / discharge_kw * 100, 1),
            "Grid",
            round(to_grid / discharge_kw * 100, 1),
        )
    return ("idle", "Load", 0.0, "Grid", 0.0)


def _flow_decomposition(
    solar_kw_i: float,
    load_kw_i: float,
    charge_kw_i: float,
    discharge_kw_i: float,
    *,
    grid_export_kw_i: float,
) -> dict[str, float]:
    """Real per-period seven-flow merit-order decomposition (nimbus issue
    #264, Mark Purcell) -- extends _dispatch_source_breakdown()'s 2-way
    split (against the battery only) to all four real bus terminals
    (Solar/Battery/Grid/Load), so every kW of the period's balance
    belongs to exactly one of the seven physical flows: PV->Load,
    PV->Battery, PV->Grid, Battery->Load, Battery->Grid, Grid->Load,
    Grid->Battery -- plus an eighth bucket, battery_to_losses (nimbus
    issue #629, see below).

    Same merit-order convention as _dispatch_source_breakdown() (solar
    serves load first, then battery charge, then export; battery
    discharge serves load before being attributed to export; grid tops
    up whatever's left) -- this is an honest decomposition of the same
    flow balance the LP already solved, not a dual/shadow-price
    attribution (the LP has no per-source flow variables to read back).

    Deliberately takes charge_kw_i/discharge_kw_i as TWO SEPARATE
    pre-netted arguments, not the issue's own originally-sketched single
    net_battery_kw -- net_battery already collapses simultaneous
    charge+discharge into one signed scalar BEFORE this function would
    ever see it, which would silently defeat the one real diagnostic
    value the issue's own References section claims for this
    decomposition ("Battery->Grid inside a same-period wash trade will
    show a nonzero magnitude, which is a useful diagnostic in itself" --
    #245/#238). With the two pre-net arrays instead (already computed
    separately in main() as corrected_battery_charge_kw /
    corrected_battery_discharge_kw, for battery_kw_after_efficiency),
    a genuine same-period wash-trade period keeps both nonzero here too,
    so it actually shows up as a real, visible flow rather than being
    silently netted away first.

    nimbus issue #629 (Mark Purcell, real 3-battery-fleet report):
    battery_to_grid used to be a pure residual (discharge_kw_i minus
    whatever served load), with nowhere else for it to go -- every
    discharge period showed a nonzero Battery->Grid slice even when
    grid_export_kw was genuinely 0.0 the entire time, and
    dispatch_source_a_pct implied a real export that never happened.
    `grid_export_kw_i` -- the LP's own real, already-published
    grid_export_kw for this period -- now caps how much of that residual
    can honestly be called Battery->Grid; anything left over is a real,
    separately-labeled battery_to_losses bucket instead of a phantom
    export. This is deliberately NOT a claim about WHERE that leftover
    physically went (this project's own ac_bus_losses_kwh figure is one
    real candidate; energy genuinely absorbed by another battery
    participant behind the shared bus, invisible to this 4-terminal
    Solar/Battery/Grid/Load model, is another -- see #629's own
    discussion) -- only an honest "not really export" label, which is
    the entire ask of the issue regardless of the exact physical
    destination.

    nimbus issue #641 (Mark Purcell, live verification of #629's own
    fix): the identical bug, one level up -- pv_to_grid was STILL a pure
    residual (whatever solar wasn't used for load/charge), so a period
    where the LP curtails real solar surplus (grid_export_kw genuinely
    0.0 while PV clearly has more to give than load+battery absorb --
    real captured evidence: 0.33-1.5 kW of untouched PV surplus with
    export at 0.0 across 6 of the first 60 periods on a 3-battery
    fleet's own forecast) still showed a nonzero PV->Grid. Fixed the
    same shape as #629, one step later: battery_to_grid is computed
    FIRST exactly as #629 already does (still against the RAW,
    pre-cap pv_to_grid -- unchanged, since Mark's own verification
    confirmed the battery side already holds correctly), and pv_to_grid
    is THEN capped at whatever of grid_export_kw_i battery_to_grid
    didn't already claim -- so the two together can never exceed the
    real export, regardless of which one merit-order would naively
    credit first. The honest remainder becomes pv_to_curtailment: real
    generation genuinely not stored, not exported, and not consumed --
    a physically real category of its own (curtailment), not a loss in
    the #629 sense (nothing was generated and then wasted in transit;
    it was simply never drawn from the panels' own real headroom).

    Invariants (asserted in tests/test_flow_decomposition.py against
    both synthetic cases and the real regression fixtures under
    tests/regression/fixtures/):
      pv_to_load + pv_to_battery + pv_to_grid + pv_to_curtailment == solar_kw_i
      pv_to_load + battery_to_load + grid_to_load == load_kw_i
      pv_to_battery + grid_to_battery == charge_kw_i
      battery_to_load + battery_to_grid + battery_to_losses == discharge_kw_i
    These hold by construction, always, regardless of input. Two further
    invariants (grid_to_load + grid_to_battery == grid_import_kw,
    pv_to_grid + battery_to_grid == grid_export_kw) are NOT pure
    algebraic identities of this function alone the way the four above
    are -- grid_to_load/grid_to_battery still depend on the real LP's
    own grid_import_kw satisfying the same merit-order assumption this
    function encodes on the charge side (empirical, verified against
    real captured fixtures in the regression suite, not asserted here);
    pv_to_grid + battery_to_grid == grid_export_kw, however, now holds
    BY CONSTRUCTION on the export side too (both are capped against
    grid_export_kw_i, #629 for the battery's own share and #641 for
    PV's), which is the whole point of both issues' own fix.
    """
    pv_to_load = min(solar_kw_i, load_kw_i)
    solar_after_load = solar_kw_i - pv_to_load
    pv_to_battery = min(solar_after_load, charge_kw_i)
    solar_after_battery = solar_after_load - pv_to_battery
    pv_to_grid_residual = solar_after_battery

    load_after_solar = load_kw_i - pv_to_load
    battery_to_load = min(discharge_kw_i, load_after_solar)
    battery_residual = discharge_kw_i - battery_to_load
    # nimbus issue #629: bound the grid-bound share of the battery's own
    # residual output by what the LP itself actually exported this
    # period, net of PV's own already-computed (still-uncapped) share of
    # it -- the remainder is a real, honestly-labeled loss, not a
    # phantom export. Deliberately uses pv_to_grid_residual (not the
    # #641 capped value below) -- Mark's own live verification confirmed
    # this half already holds correctly, unchanged by #641.
    battery_to_grid = min(
        battery_residual, max(0.0, grid_export_kw_i - pv_to_grid_residual)
    )
    battery_to_losses = battery_residual - battery_to_grid

    # nimbus issue #641: PV's own share of the real export is whatever
    # battery_to_grid (just computed) didn't already claim -- together
    # the two can never exceed grid_export_kw_i. The honest remainder is
    # curtailment: real generation genuinely not stored, exported, or
    # consumed this period.
    pv_to_grid = min(pv_to_grid_residual, max(0.0, grid_export_kw_i - battery_to_grid))
    pv_to_curtailment = pv_to_grid_residual - pv_to_grid

    load_after_battery = load_after_solar - battery_to_load
    grid_to_load = load_after_battery
    grid_to_battery = charge_kw_i - pv_to_battery

    return {
        "pv_to_load": pv_to_load,
        "pv_to_battery": pv_to_battery,
        "pv_to_grid": pv_to_grid,
        "battery_to_load": battery_to_load,
        "battery_to_grid": battery_to_grid,
        "grid_to_load": grid_to_load,
        "grid_to_battery": grid_to_battery,
        "battery_to_losses": battery_to_losses,
        "pv_to_curtailment": pv_to_curtailment,
    }


def _compute_flow_economics(
    flows: list[dict[str, float]],
    import_price: np.ndarray,
    export_price: np.ndarray,
    period_hours: np.ndarray,
    round_trip_efficiency: float,
    initial_soc_kwh: float,
) -> list[dict[str, float]]:
    """Per-period shadow prices on each of the seven flows from
    _flow_decomposition(), plus the PV / Battery / Combined / Interaction
    savings model (nimbus issue #264, Mark Purcell).

    Shadow price table (all $/kWh, from the issue):
      PV -> Load      = import_price                    (retail avoided)
      PV -> Battery   = import_price - rt_loss_cost      (deferred credit)
      PV -> Grid      = export_price                     (settlement)
      Battery -> Load = import_price - rt_loss_cost      (retail avoided, less loss already paid)
      Battery -> Grid = export_price - rt_loss_cost - charge_price_at_source  (arbitrage margin)
      Grid -> Load    = -import_price                    (pure cost)
      Grid -> Battery = -import_price                     (cost, deferred against a later discharge)
    where rt_loss_cost = import_price * (1 - round_trip_efficiency).

    charge_price_at_source -- the $/kWh cost basis attributed to energy
    LEAVING the battery on a discharge period -- is deliberately NOT a
    same-period lookup. A battery's SoC persists across periods (it is
    charged in one period and very often discharged many periods later);
    same-period charge+discharge is itself the anomalous wash-trade
    condition #245 targets, not the normal case the pricing table needs
    to be right for. This tracks a real weighted-average cost of goods
    (WACOG) basis for whatever energy currently sits in the battery,
    updated every period: PV-sourced charge blends in at $0/kWh,
    grid-sourced charge blends in at that period's own import_price, and
    every period's discharge draws the existing average down without
    changing it -- exactly like an inventory cost basis, which moves on
    a purchase, never on a sale.

    Tracks PRE-efficiency kWh throughout (the same convention
    _flow_decomposition()'s own charge_kw/discharge_kw already use) --
    round-trip loss is charged exactly once, via rt_loss_cost in the
    price table above; folding efficiency into the cost basis too would
    double-count the same loss twice.

    initial_soc_kwh's own real cost basis is genuinely unknowable (it
    was charged at some real historical price before this forecast
    horizon began) -- seeded at this horizon's own opening import_price,
    the same "replacement cost" convention already used elsewhere in
    this file for salvage/terminal value.

    Combined savings and the interaction term are built from this same
    flow decomposition's own reconstructed load_kw / grid_import_kw /
    grid_export_kw (invariants 2/5/6 on _flow_decomposition()'s own
    docstring) rather than a second, independently-passed copy of the
    real LP arrays -- so PV + Battery + Interaction == Combined holds
    exactly, by construction, every period, rather than only
    approximately whenever the two sources happen to agree.
    """
    n = len(flows)
    results: list[dict[str, float]] = []

    running_energy_kwh = max(float(initial_soc_kwh), 1e-9)
    running_cost_basis = float(import_price[0]) if n else 0.0

    for i in range(n):
        f = flows[i]
        hrs = float(period_hours[i])
        ip = float(import_price[i])
        ep = float(export_price[i])
        loss = ip * (1.0 - round_trip_efficiency)

        charge_kwh_pv = f["pv_to_battery"] * hrs
        charge_kwh_grid = f["grid_to_battery"] * hrs
        charge_kwh = charge_kwh_pv + charge_kwh_grid
        # nimbus issue #629: battery_to_losses is real energy that left
        # the battery's own SoC (discharge_kw_i, by construction, equals
        # battery_to_load + battery_to_grid + battery_to_losses) even
        # though it never reached load or grid -- must count toward the
        # SoC/WACOG drawdown below or running_energy_kwh silently drifts
        # from the real battery state on every period this bucket is
        # nonzero. `.get(..., 0.0)` keeps this function tolerant of an
        # older-shaped flow dict (e.g. a caller/test predating #629).
        discharge_kwh = (
            f["battery_to_load"]
            + f["battery_to_grid"]
            + f.get("battery_to_losses", 0.0)
        ) * hrs

        if charge_kwh > 1e-9:
            charge_price_this_period = (
                charge_kwh_pv * 0.0 + charge_kwh_grid * ip
            ) / charge_kwh
            new_energy = running_energy_kwh + charge_kwh
            running_cost_basis = (
                running_cost_basis * running_energy_kwh
                + charge_price_this_period * charge_kwh
            ) / new_energy
            running_energy_kwh = new_energy

        charge_price_at_source = running_cost_basis

        if discharge_kwh > 1e-9:
            running_energy_kwh = max(0.0, running_energy_kwh - discharge_kwh)

        price_pv_to_load = ip
        price_pv_to_battery = ip - loss
        price_pv_to_grid = ep
        price_battery_to_load = ip - loss
        price_battery_to_grid = ep - loss - charge_price_at_source
        price_grid_to_load = -ip
        price_grid_to_battery = -ip

        pv_savings = (
            f["pv_to_load"] * price_pv_to_load
            + f["pv_to_grid"] * price_pv_to_grid
            + f["pv_to_battery"] * price_pv_to_battery
        ) * hrs
        battery_savings = (
            f["battery_to_load"] * price_battery_to_load
            + f["battery_to_grid"] * price_battery_to_grid
            - f["grid_to_battery"] * loss
        ) * hrs

        load_kw_recon = f["pv_to_load"] + f["battery_to_load"] + f["grid_to_load"]
        grid_import_recon = f["grid_to_load"] + f["grid_to_battery"]
        grid_export_recon = f["pv_to_grid"] + f["battery_to_grid"]
        combined_savings = (
            load_kw_recon * ip - (grid_import_recon * ip - grid_export_recon * ep)
        ) * hrs
        interaction_savings = combined_savings - pv_savings - battery_savings

        results.append(
            {
                "flow_price_pv_to_load": round(price_pv_to_load, 4),
                "flow_price_pv_to_battery": round(price_pv_to_battery, 4),
                "flow_price_pv_to_grid": round(price_pv_to_grid, 4),
                "flow_price_battery_to_load": round(price_battery_to_load, 4),
                "flow_price_battery_to_grid": round(price_battery_to_grid, 4),
                "flow_price_grid_to_load": round(price_grid_to_load, 4),
                "flow_price_grid_to_battery": round(price_grid_to_battery, 4),
                "flow_battery_cost_basis": round(charge_price_at_source, 4),
                "savings_pv": round(pv_savings, 4),
                "savings_battery": round(battery_savings, 4),
                "savings_combined": round(combined_savings, 4),
                "savings_interaction": round(interaction_savings, 4),
            }
        )

    return results


def build_per_battery_forecast(
    plan, grid_times, n_periods: int, battery_capacity_by_name: dict[str, float]
) -> list[dict]:
    """nimbus issue #563 item 5: Plan.batteries[] (network.py's own
    BatteryPlan, #467 stage 1) already carries each real battery
    participant's own charge/discharge/SoC series -- this is exposure,
    not new solver work, the same posture #613's own item 1
    (shadow_price) already established for this file. Same per-period
    {"time": ...} shape as the aggregate "forecast" array
    publish_plan() itself already builds, one entry per participant, so
    a dashboard/quality-report reader can show the home pack and each
    EV/second battery separately instead of only the fleet-summed
    battery_kw/soc_pct every "forecast" row already has.

    `soc_pct` is derived from THIS participant's own real capacity
    (`battery_capacity_by_name[bp.name]`), never the fleet total --
    dividing by the wrong capacity is exactly the real #569 bug already
    fixed once for the aggregate soc_pct field, not something to
    reintroduce here. 0.0 (not a crash or a fabricated 100%) for a
    participant genuinely missing from `battery_capacity_by_name` or
    configured with a non-positive capacity.

    Deliberately NOT the "one flattened sub-device per battery" half of
    #563 item 5's own ask -- that's a real, separate new-entity-
    lifecycle piece, left open.
    """
    return [
        {
            "name": bp.name,
            "forecast": [
                {
                    "time": grid_times[i].isoformat(),
                    "charge_kw": round(float(bp.charge_kw[i]), 3),
                    "discharge_kw": round(float(bp.discharge_kw[i]), 3),
                    "soc_kwh": round(float(bp.soc_kwh[i]), 3),
                    "soc_pct": round(
                        float(bp.soc_kwh[i] / battery_capacity_by_name[bp.name] * 100),
                        2,
                    )
                    if battery_capacity_by_name.get(bp.name, 0.0) > 0
                    else 0.0,
                }
                for i in range(n_periods)
            ],
        }
        for bp in plan.batteries
    ]


def _load_forecast_source_attributes(decision) -> dict:
    """Flatten a `ForecastSourceDecision` into published scalars.

    `None` in, all seven keys present and `None` -- which is the standalone/cron
    path and any caller predating this parameter, not an error. The alternative
    (omit the keys) is the appear-and-vanish shape #589 exists about, and it
    would also make "this install cannot tell me" indistinguishable from "this
    install chose ml".
    """
    if decision is None:
        return dict.fromkeys(_LOAD_FORECAST_SOURCE_KEYS)
    mean = decision.mean_value_add_dollars
    return {
        "load_forecast_source_policy": decision.policy,
        "load_forecast_source_selected": decision.source,
        # Rounded to 4 dp for the same reason every dollar figure on the quality
        # report is: a blend weight of 0.7857142857142857 is noise in a payload
        # measured against the recorder's 16 KB cap.
        "load_forecast_persistence_weight": round(decision.persistence_weight, 4),
        "load_forecast_source_reason": decision.reason,
        "load_forecast_source_days_scored": decision.days_scored,
        "load_forecast_source_days_persistence_won": decision.days_persistence_won,
        "load_forecast_source_mean_value_add_dollars": (
            None if mean is None else round(mean, 4)
        ),
    }


def publish_plan(
    *,
    cfg,
    now,
    plan,
    previous_plan,
    solve_started,
    period_hours_arr,
    grid_times,
    n_periods,
    capacity_kwh,
    # nimbus issue #569: the real fleet total across every battery
    # participant (home + any battery_participant subentries) -- see
    # main()'s own comment where this is computed for why this had to
    # become its own parameter rather than overloading capacity_kwh
    # (which stays the home battery's own capacity for existing callers
    # that legitimately still need just that).
    fleet_capacity_kwh,
    # nimbus issue #563 item 5: real per-participant capacity, keyed by
    # BatteryConfig.name -- Plan.batteries[] itself carries kW/SoC but
    # not capacity (network.py stays grid-agnostic about the config
    # that produced a Plan), needed here to derive each participant's
    # own soc_pct rather than only the fleet-summed one every existing
    # "forecast" row already publishes. See main()'s own call site.
    battery_capacity_by_name,
    charge_discharge_efficiency,
    grid,
    import_limit_kw,
    export_limit_kw,
    # nimbus issue #493 (Signals 4/7 of #489, item 1): the plain
    # configured static limit, kept SEPARATE from import_limit_kw/
    # export_limit_kw above (which are now the real per-period envelope-
    # resolved arrays GridConfig itself uses for dispatch) -- compute_
    # cost_band()'s own read-only #630 diagnostic takes a single flat
    # limit for its whole window by design, out of this issue's own
    # scope to change.
    static_import_limit_kw,
    static_export_limit_kw,
    max_charge_kw,
    max_discharge_kw,
    charge_cost,
    discharge_cost_arr,
    salvage_value,
    risk_aversion,
    import_price_risk_aversion,
    export_price_risk_aversion,
    import_price,
    export_price,
    spot_import_source,
    spot_export_source,
    # nimbus issue #631: per-period "primary"/"secondary"/"fallback"
    # label, straight from blend_price_with_secondary_sources()'s own
    # real decision -- see that function's own docstring.
    import_price_source,
    export_price_source,
    export_bonus_price,
    load_kw,
    solar_kw,
    load_lower_kw,
    load_upper_kw,
    initial_soc_kwh,
    match_fraction,
    summed_18_now_kw,
    whole_house_now_kw,
    live_load_kw,
    load_forecast_coverage_hours,
    load_forecast_error,
    load_forecast_source_used,
    load_forecast_warnings,
    failed_load_entities,
    n_clamped,
    solar_delivery,
    p2p_recent_volume_kwh,
    price_spike_active,
    # nimbus issue #937 item 4: the ForecastSourceDecision this cycle acted on.
    # Last, with a default, so every existing caller (and the standalone/cron
    # copy, and this function's own tests) keeps working unchanged. A None
    # decision still publishes all seven keys as None rather than omitting them --
    # a consumer must never see a key appear and vanish between cycles (#589).
    load_forecast_source_decision=None,
) -> None:
    """Extracted from main() (nimbus issue #363 step 2, Mark Purcell's
    own approved staged-extraction plan -- "please go ahead with step 2,
    publish_plan() extraction, per your own outermost-first ordering").
    Pure move, zero behavior change -- guarded by the #363 step-1
    golden-output test (tests/test_main_golden_output_guardrail.py),
    which asserts byte-identical published output for a real solve.

    Takes the already-solved `plan` (network.build_plan()'s own return
    value) plus every real input the LP itself was fed, computes every
    derived/diagnostic value (cost breakdown, cost band, binding
    constraint, per-period flow decomposition, etc.), and publishes
    both sensor.nimbus_household_load_total_forecast and this
    project's own flagship sensor.nimbus_solver_battery_forecast.
    """
    sw = _solver_writer()
    solve_seconds = time.monotonic() - solve_started
    if plan.status == "optimal":
        save_plan_state(plan, period_hours_arr, grid_times[0])
    elif plan.solver_failed:
        # nimbus issue #356 (Mark Purcell): this is genuinely NOT the same
        # thing as a real infeasible model -- HiGHS gave up/hit a limit
        # without ever determining feasibility either way (see network.py's
        # own Plan.raw_status docstring). Named explicitly here so an
        # operator sees "the solver failed, here's HiGHS's own reason"
        # rather than being sent hunting for a modeling/config problem that
        # doesn't exist.
        #
        # nimbus issue #757: and do not publish it. _infeasible_plan()
        # builds a well-formed but ENTIRELY ZERO-FILLED Plan for any
        # non-optimal solve -- no batteries, no loads, every array
        # zeros(n). Publishing that overwrites a perfectly good live plan
        # with a plausible-looking blank one: sensor.nimbus_solver_battery_
        # forecast reads "0 kW everywhere, batteries=[]", which is
        # indistinguishable on a dashboard from a real solve that genuinely
        # decided to do nothing.
        #
        # That is not hypothetical. #757 ("battery participant silently
        # excluded from the solve") sat open through TEN separate
        # investigations that disagreed with each other, because a live
        # trace showed build_plan() returning batteries=['home','Test EV']
        # with status='optimal' on every cycle while 7 of 10 actual
        # publishes carried batteries=[] with status='error'. The
        # "exclusion" was never an exclusion -- it was failed solves
        # overwriting good ones. Whoever looked next saw whichever write
        # landed last.
        #
        # Skipping the publish is the honest outcome, not a silent one:
        # _NimbusSolverPushSensor.available goes False once
        # _STALE_AFTER_SECONDS (5 min) passes with no fresh push, so a
        # persistently failing solver surfaces as "unavailable" -- which is
        # exactly what it is -- rather than as a confident plan of zeros.
        # A transient single failure keeps the last good plan for under
        # five minutes, which is strictly better than replacing it with
        # nothing.
        #
        # Deliberately NOT extended to "infeasible". That one is a real
        # modelling ANSWER (HiGHS proved no feasible dispatch exists for
        # the constraints given), the household needs to see it, and #773's
        # own fallback path depends on it being published. "error" is the
        # only status where the solver never determined anything at all.
        # nimbus issue #773: the elapsed time is logged HERE because
        # this function returns before ever publishing it. #757's own
        # guard above is right to skip the publish, but solve_seconds
        # reaches sensor.nimbus_solver_solve_seconds ~750 lines below
        # this point -- so from v0.94.301 onward that sensor only ever
        # reported SUCCESSFUL solves, and the duration of a failing one
        # became invisible. That is exactly the number #773 needs: on a
        # real install these failures take ~60s (the per-call limit)
        # against a 0.6s healthy cycle, and a guard that hides the
        # symptom it protects against is a bad trade. Logged, not
        # published -- no plan is written, so nothing about the guard
        # itself changes.
        # nimbus issue #1179: name the calibration fallback IN THIS LINE.
        #
        # The 2026-09-20 episode produced 153 failed cycles in 2.7h and 44
        # "no blend weight preserves primary cost ... using minimum weight
        # 1.00e-12" warnings, 37 of which shared a timestamp-SECOND with a
        # failure. That is a correlation by clock, not by cycle, and it is
        # precisely why the blend-collapse hypothesis could be neither
        # confirmed nor dropped: blend warnings accompanied only 44 of the
        # 153, which is equally consistent with "one of several routes to
        # the same failure" and with "the warning is logged on a subset of
        # the cycles that take it".
        #
        # `Plan.calibration_min_weight_fallback` is threaded from
        # LPResult (see both dataclasses' own fields) and is set on
        # SUCCESSFUL solves too, so the next episode answers the question
        # with a base rate rather than with failures alone.
        # nimbus issue #1179, second half: name WHY the fallback fired, not
        # just that it did. The two reasons point in opposite causal
        # directions, and #1229's bool alone cannot separate them:
        #
        #   probe_not_optimal  -- the calibrator's own bracket probes did not
        #                         solve, so the model was already failing
        #                         BEFORE any weight was chosen. The blend
        #                         collapse is a symptom of this failure, not
        #                         its cause, and this line and the blend
        #                         warning are two views of one event.
        #   cost_not_preserved -- both probes solved and no weight in the
        #                         bracket held the primary cost. The only case
        #                         in which #1179's original hypothesis (that
        #                         handing HiGHS 1e-12 is itself what breaks the
        #                         solve) is even available.
        #
        # That distinction is what the 2026-09-20 episode could not answer:
        # blend warnings accompanied only 44 of 153 failures, which is equally
        # consistent with "one of several routes to the same failure" and with
        # "the warning is logged on a subset of the cycles that take it".
        # `probe_not_optimal` would explain the other 109 directly -- they
        # failed at a phase that never reaches calibration at all.
        # nimbus issue #1291 (Mark Purcell, IV&V #1289): report the weight
        # the calibrator actually settled on, not just whether it fell back.
        # #1179 put that number on LPResult for exactly this line and it
        # never arrived, so this warning has been describing the outcome
        # while withholding the one value that distinguishes "collapsed to
        # the floor" from "settled somewhere sane" -- which is the difference
        # between a candidate cause and a healthy cycle.
        weight_note = (
            f"weight={plan.calibration_weight_used:.3g}"
            if plan.calibration_weight_used is not None
            else "weight=unrecorded"
        )
        if plan.calibration_min_weight_fallback:
            reason = plan.calibration_fallback_reason or "unrecorded"
            calibration_note = (
                f"FELL BACK to the minimum weight 1e-12, reason={reason}, {weight_note}"
            )
            if plan.calibration_fallback_reason == "probe_not_optimal":
                calibration_note += (
                    " (so the model was ALREADY failing before a weight was "
                    "chosen -- this fallback is a symptom of this failure, not "
                    "its cause)"
                )
            elif plan.calibration_fallback_reason == "cost_not_preserved":
                calibration_note += (
                    " (both probes solved, so the collapsed weight is a real "
                    "calibration verdict and is a candidate CAUSE here)"
                )
        else:
            # The success branch says the number too. "found a usable weight"
            # without saying WHICH weight reopens the same inference gap one
            # sentence later, and the healthy-cycle distribution is what
            # makes the failing-cycle value interpretable at all.
            calibration_note = (
                f"found a usable weight (no minimum-weight fallback), {weight_note}"
            )
        # nimbus issue #1179: HiGHS's own numbers, in THIS line.
        #
        # The 2026-09-20 episode published 153 of these lines carrying
        # `raw_status="Unknown"` and an elapsed time, and nothing else.
        # "Unknown" is HiGHS declining to say what happened, so a line that
        # stops there records only that a failure occurred -- which is what
        # made the episode undiagnosable four days later when it had already
        # stopped recurring.
        #
        # `plan.iterations` is the number that decides #1179's own open
        # question. solver/lp.py's own comment above `_SLOW_LP_CALL_SECONDS`
        # states it: a stall with a huge iteration count is degeneracy or
        # cycling, a stall with a small one "is stuck somewhere that is not
        # the simplex loop at all -- presolve, a MIP branch-and-bound tree,
        # or numerical trouble", and "those two answers point at completely
        # different fixes". A 0.1s failure against a 1.16s healthy solve
        # predicts ~0 iterations, i.e. the model rejected before the simplex
        # loop; the prediction has never been checkable because this line
        # never printed the count, although `_infeasible_plan()` has carried
        # it onto the Plan since #356.
        #
        # `%s` for the elapsed time, not `%.1f`: at 0.1s a 1-decimal format
        # is at the edge of its own resolution, and this issue's whole
        # signature is how SMALL that number is.
        solver_shared._LOGGER.warning(
            "Nimbus: solve did not complete after %.3fs -- HiGHS solver "
            "failure (%s), not a genuinely infeasible model; keeping the "
            "previous published plan rather than overwriting it with an "
            "empty one (nimbus issue #757). This cycle's blend calibration "
            "%s. HiGHS: simplex_iterations=%s %s. Failing model: %s "
            "(nimbus issue #1179)",
            solve_seconds,
            plan.raw_status or "unknown reason",
            calibration_note,
            plan.iterations,
            lp.format_highs_info(plan.highs_info),
            plan.failing_model_path or "not captured",
        )
        if plan.failing_model_path is not None:
            # Separate line, and only when there is genuinely a file: the
            # perishability note is long, it is useless on the 152 later
            # occurrences that share one capture, and #773 already learned
            # (see `_dump_location_note()`'s own docstring) that announcing a
            # dump without saying it is temporary loses the artefact.
            solver_shared._LOGGER.warning(
                "Nimbus #1179: the failing model is at %s -- the real "
                "instance, which no synthetic reproduction of this failure "
                "has matched. Attach it to nimbus issue #1179. %s",
                plan.failing_model_path,
                lp.dump_location_note(),
            )
        return

    # Real fixed daily charges (Network Access + LV Fee), reported
    # honestly alongside the LP's own total_cost -- NOT fed into the LP
    # itself (a flat, dispatch-independent cost can't change an optimal
    # LP decision, only shift the objective by a constant, so there's
    # nothing for the solver to do with it). Prorated to this horizon's
    # own real span (sum of period_hours_arr, NOT n_periods*a-fixed-width
    # -- the tiered grid has two different period widths) / 24, not just
    # added flat.
    #
    # nimbus issue #348 (Mark Purcell, fixed 2026-09-04): a real wizard
    # field now (number.nimbus_solver_fixed_daily_charge), not a module
    # constant applied to every install regardless of their own real
    # retailer's actual daily supply charge. 1.95 as the literal default
    # matches const.py's own DEFAULT_SOLVER_FIXED_DAILY_CHARGE -- byte-
    # identical behaviour for every existing install until this field is
    # explicitly changed.
    horizon_days = sum(period_hours_arr) / 24.0
    fixed_daily_charge = sw._cfg_num(cfg, "solver_fixed_daily_charge", 1.95)
    total_cost_with_fixed_costs = (
        plan.total_cost or 0.0
    ) + horizon_days * fixed_daily_charge

    # Real battery throughput/cycling exposure (2026-08-21, direct Mark
    # Purcell finding, relayed via the household: "degradation isn't in
    # the objective, so 1.0 will look free when it isn't" -- risk_aversion/
    # price_risk_aversion both tend to bias toward MORE defensive
    # charge/discharge activity, and the LP's own $ total_cost has no way
    # to reflect the real wear that causes, since BatteryConfig has no
    # genuine degradation cost term (charge_cost/discharge_cost are small,
    # flat $/kWh throughput costs, not a cycle-depth-aware wear model).
    # Reported honestly alongside the dollar figures rather than hidden --
    # a household turning either risk dial up should be able to SEE the
    # real cycling cost of that choice, not just the (incomplete) $ total.
    hours_arr_np = np.array(period_hours_arr)
    total_charge_kwh = float(np.sum(plan.battery_charge_kw * hours_arr_np))
    total_discharge_kwh = float(np.sum(plan.battery_discharge_kw * hours_arr_np))
    total_throughput_kwh = total_charge_kwh + total_discharge_kwh
    # A "full cycle" = one full charge + one full discharge = 2x capacity
    # of throughput -- the standard, real-world battery-degradation unit
    # (manufacturer cycle-life ratings are quoted in full-equivalent-
    # cycles, not raw kWh moved), so this is directly comparable to a
    # real spec sheet, not an invented metric.
    # nimbus issue #569: fleet_capacity_kwh (sum across every battery
    # participant), not capacity_kwh (the home battery alone) -- a 60 kWh
    # car and a 40 kWh pack do not share a cycle-life spec, and this is
    # the whole-fleet throughput being related to the whole fleet's own
    # capacity, same reasoning as soc_pct below.
    equivalent_full_cycles = (
        total_throughput_kwh / (2.0 * fleet_capacity_kwh)
        if fleet_capacity_kwh > 0
        else 0.0
    )

    # Pre-collapse per-direction arrays for battery_kw_after_efficiency
    # below (2026-08-27, nimbus issue #229) -- kept separate from net_battery
    # rather than reconstructed from its sign, because a period can have LP
    # degeneracy noise on BOTH plan.battery_charge_kw[i] and
    # plan.battery_discharge_kw[i] simultaneously; collapsing to net first
    # and branching on its sign silently drops whichever direction's real
    # efficiency loss the sign discarded. Applying each direction's own
    # efficiency BEFORE summing (same approach Mark Purcell verified in
    # PR #231 against the sibling nimbus_solver_app writer -- 0.013%
    # residual vs 3.65% reconstructing from the post-collapse net value)
    # is what actually closes tightly against Δ(soc_pct·capacity).
    corrected_battery_discharge_kw = plan.battery_discharge_kw

    # DEFENSIVE SAFETY NET (2026-08-22) -- this file's own real, found
    # root cause. THIS module IS the native in-process solve path
    # (solver_runtime.py imports it exactly once, at container startup,
    # via a lazy module-level singleton that's never re-imported for the
    # life of the process) -- see the sibling standalone script's own
    # matching comment (116KAT-HA-AI repo, scripts/
    # nimbus_solver_forecast_writer.py) for the full incident writeup.
    # Short version: this container's most recent restart (2026-08-22
    # ~15:25 AEST, to deploy the native runtime itself) happened nearly 2
    # hours BEFORE the real network.py fix landed (commit 3f90c1f,
    # 17:20:03) -- so THIS path, specifically, has been the one silently
    # running the old, unfixed battery_charge bound every minute since,
    # racing the standalone cron writer's own always-current code. This
    # clamp stays in place permanently regardless of cause, as a genuine
    # backstop -- see the sibling file's own comment for exactly what it
    # corrects and why. A future container restart flushes this module's
    # own stale in-memory code (there's no other way to force a re-import
    # here); until then, disabling the Nimbus integration was used as an
    # immediate same-night mitigation, since that correctly cancels this
    # module's own timer (entry.async_on_unload, __init__.py) without a
    # restart.
    # nimbus issue #923: `plan.batteries[0]` is the battery the LP gate was
    # applied to; the fleet aggregate is not. See the function's docstring.
    (
        net_battery,
        corrected_battery_charge_kw,
        corrected_grid_import,
        _violation_mask,
    ) = resolve_fixed_export_charge_clamp(
        grid.fixed_export_kw,
        gated_charge_kw=(
            plan.batteries[0].charge_kw if plan.batteries else plan.battery_charge_kw
        ),
        aggregate_charge_kw=plan.battery_charge_kw,
        discharge_kw=plan.battery_discharge_kw,
        grid_import_kw=plan.grid_import_kw,
    )
    if _violation_mask.any():
        solver_shared._LOGGER.warning(
            "Nimbus Solver: solver returned %d period(s) with "
            "battery_charge_kw>0 on the home battery during a committed "
            "fixed_export_kw period -- mathematically should be impossible, "
            "applying defensive clamp before push. (Participant batteries "
            "are not gated here and are not counted, see nimbus issue #923.)",
            int(np.sum(_violation_mask)),
        )

    # Physical, post-efficiency energy rate at the battery terminals
    # (2026-08-27, nimbus issue #229, Mark Purcell) -- battery_kw above is
    # the LP's own pre-efficiency decision variable, which can't be
    # reconciled against soc_pct without knowing the applied efficiency
    # curve. Built from the (possibly defensively-corrected, see above)
    # per-direction arrays rather than net_battery's sign -- see that
    # variable's own comment for why.
    battery_kw_after_efficiency = (
        corrected_battery_discharge_kw / charge_discharge_efficiency
        - corrected_battery_charge_kw * charge_discharge_efficiency
    )

    # Real per-period source/destination breakdown (2026-08-28) -- see
    # _dispatch_source_breakdown()'s own module-level docstring for the
    # full rationale.
    dispatch_breakdown = [
        _dispatch_source_breakdown(
            net_battery[i],
            solar_kw[i],
            load_kw[i],
            grid_export_kw_i=float(plan.grid_export_kw[i]),
        )
        for i in range(n_periods)
    ]

    # Real per-period seven-flow decomposition + shadow prices + PV/
    # Battery/Combined savings model (2026-08-28, nimbus issue #264, Mark
    # Purcell) -- see _flow_decomposition()'s and _compute_flow_economics()'s
    # own module-level docstrings for the full rationale, including why
    # this deliberately extends the issue's own sketch (separate pre-net
    # charge/discharge arrays instead of a single net_battery_kw, and a
    # real cross-period WACOG cost basis instead of a same-period lookup).
    # Byte-identical, additive to the existing dispatch_source_a/b fields
    # above -- nothing already published changes shape or value.
    flow_decomp = [
        _flow_decomposition(
            solar_kw[i],
            load_kw[i],
            float(corrected_battery_charge_kw[i]),
            float(corrected_battery_discharge_kw[i]),
            grid_export_kw_i=float(plan.grid_export_kw[i]),
        )
        for i in range(n_periods)
    ]
    flow_econ = _compute_flow_economics(
        flow_decomp,
        import_price,
        export_price,
        period_hours_arr,
        round_trip_efficiency=charge_discharge_efficiency**2,
        initial_soc_kwh=initial_soc_kwh,
    )

    # Real per-period price/load/solar/net-cost fields added (2026-08-17,
    # direct ask: "still waiting for haeo like markdown table where I
    # can see forecasted costs fit load solar and soc% and period net")
    # -- previously ONLY battery/SoC/grid kW were pushed; a real forecast
    # TABLE needs the same real inputs the LP itself actually solved
    # against, not just its output. import_price/export_price/
    # export_bonus_price are all already computed above (this writer's
    # own real inputs, not re-derived); load_kw/solar_kw are the same
    # real per-period arrays already fed to LoadConfig/SolarConfig.
    # net_cost is the real grid-side cash flow for that period (import
    # cost minus base export revenue minus P2P bonus revenue) --
    # deliberately NOT including battery charge/discharge wear cost,
    # matching this project's own established "Net $" convention from
    # the HAEO forecast table this mirrors.
    forecast = [
        {
            "time": grid_times[i].isoformat(),
            "battery_kw": round(float(net_battery[i]), 3),
            # See battery_kw_after_efficiency's own definition above (nimbus
            # issue #229) for why this is built from the per-direction
            # arrays rather than net_battery[i] directly.
            "battery_kw_after_efficiency": round(
                float(battery_kw_after_efficiency[i]), 3
            ),
            # nimbus issue #569: fleet_capacity_kwh, not capacity_kwh --
            # plan.battery_soc_kwh[i] is the summed aggregate across every
            # battery participant (per #467 stage 1), so the denominator
            # has to be the fleet total too, or this reads well over 100%
            # the moment a second battery participant is configured.
            "soc_pct": round(
                float(plan.battery_soc_kwh[i] / fleet_capacity_kwh * 100), 2
            )
            if fleet_capacity_kwh > 0
            else 0.0,
            # import side uses corrected_grid_import (see the defensive
            # clamp above) -- keeps this consistent with battery_kw
            # rather than silently reflecting the RAW, uncorrected import
            # on any period the clamp touched.
            "grid_import_kw": round(float(corrected_grid_import[i]), 3),
            "grid_export_kw": round(float(plan.grid_export_kw[i]), 3),
            # How much of grid_export_kw[i] earned the real, undiluted
            # P2P premium (vs the base/spot rate) -- exposed directly so
            # a real dashboard can show WHERE the real committed volume
            # landed, not just infer it (see nimbus's own network.py
            # Plan.export_bonus_kw docstring).
            "export_bonus_kw": round(float(plan.export_bonus_kw[i]), 3),
            # nimbus issue #493 (Signals 4/7 of #489, item 1): the real
            # per-period bound the plan was actually solved against this
            # period -- the static configured limit at every period on
            # any install with no envelope entity configured (byte-
            # identical to before this issue), or the live/forecast-
            # resolved DNSP envelope value otherwise. See
            # resolve_envelope_limit_kw()'s own docstring.
            "envelope_import_limit_kw": round(float(import_limit_kw[i]), 3),
            "envelope_export_limit_kw": round(float(export_limit_kw[i]), 3),
            # nimbus issue #613 (Mark Purcell, item 1 of 3 -- explicitly
            # scoped to just this exposure piece, NOT the earliness-
            # timing behavior change items 2/3 describe, which need
            # their own separate design/verification pass): the whole-
            # system marginal cost of a kWh in THIS period, per the LP's
            # own power_balance_t{i} dual -- "already extracts duals...
            # this is exposure, not new solver work." Same 0.0 default-
            # on-missing convention energy_shadow_price_now (period 0's
            # own copy of this same number, kept for backward
            # compatibility) already uses.
            #
            # nimbus issue #662 (Mark Purcell): power_balance_t{i} is
            # built with every term at a plain +-1.0 coefficient (kW),
            # while the objective's own price terms are scaled by
            # period_hours_arr[i] (a $/kWh price x hours -> a real $
            # cost) -- so the row's own raw dual comes out in "$ per kW
            # of RHS," already carrying an implicit x hours[i] relative
            # to the true $/kWh marginal price. Dividing by period_
            # hours_arr[i] recovers it -- the EXACT SAME correction
            # forced_import_cost already applies to reduced_costs a few
            # hundred lines below (`reduced_costs[...] / hours[t]`),
            # just never extended to this sibling field. Confirmed live
            # against real numbers: 0.0186 (raw, pre-fix) / (5/60) =
            # 0.223, against a real contemporaneous import price of
            # 0.2134 -- a genuine marginal-price reading, not the
            # ~11.5x-too-small raw value.
            "shadow_price": round(
                plan.duals.get(f"power_balance_t{i}", 0.0) / period_hours_arr[i], 4
            ),
            "import_price": round(import_price[i], 4),
            # The raw commodity/spot price ALONE, before network TOU +
            # certificates are added on (2026-08-22, direct household
            # ask, after the real 8.4 vs 7.1c investigation: "normal
            # dumb folk user would look for buy price ot be what
            # localvolts_cost_flexup is... they would not get why you
            # added up costs to it... so maybe the table needs fees
            # column next ot cost?"). import_price above is UNCHANGED --
            # still the full landed cost, still what net_cost/the LP
            # itself actually uses -- this is purely an additional,
            # honest field so a dashboard can show Buy¢ = this (matches
            # what LocalVolts' own app shows) and Fees¢ = import_price
            # minus this, instead of one opaque combined number nobody
            # outside this codebase could verify against anything real.
            # True pre-blend source pass-through (2026-08-27, nimbus repo
            # issue #216, Mark Purcell) -- what the configured
            # solver_import_price_sensor itself said, before
            # blend_price_with_secondary_sources() folds in any
            # configured _sensor_2/_sensor_3. Previously this read
            # spot_import_raw AFTER blending, so on any install with a
            # secondary source configured, import_price_raw silently
            # stopped being a real "before any transformation" probe --
            # exactly Mark's own found-live gap ("on Config B... import_
            # price and import_price_raw are byte-identical... isn't
            # currently serving as a before-any-transformation
            # diagnostic"). On a single-source install (no _sensor_2/_3
            # configured) this is unchanged, byte-identical to before.
            "import_price_raw": round(spot_import_source[i], 4),
            # nimbus issue #631 (Mark Purcell, live finding: a single
            # 71.3 kWh charge block committed 20 hours ahead purely on a
            # secondary source's own price, with nothing published that
            # distinguished "known from the retailer's own near-term
            # forecast" from "extrapolated from a weekly tariff table" --
            # import_price and import_price_raw were byte-identical in
            # every row regardless of which source actually won that
            # period). "primary"/"secondary"/"fallback", one label per
            # period, straight from blend_price_with_secondary_sources()'s
            # own real per-period decision -- see that function's own
            # docstring. "primary" on every row of a single-source
            # install (the overwhelming majority today).
            "import_price_source": import_price_source[i],
            "export_price": round(export_price[i], 4),
            # Same true pre-blend pass-through as import_price_raw above,
            # for the export side -- new field (2026-08-27, nimbus repo
            # issue #216, Mark Purcell's refined ask #1: "Publish
            # export_price_raw (same shape as the existing
            # import_price_raw attribute)").
            "export_price_raw": round(spot_export_source[i], 4),
            # Same #631 per-period source label as import_price_source
            # above, for the export side.
            "export_price_source": export_price_source[i],
            "bonus_price": round(export_bonus_price[i], 4),
            "load_kw": round(load_kw[i], 3),
            "solar_kw": round(solar_kw[i], 3),
            "dispatch_direction": dispatch_breakdown[i][0],
            "dispatch_source_a_label": dispatch_breakdown[i][1],
            "dispatch_source_a_pct": dispatch_breakdown[i][2],
            "dispatch_source_b_label": dispatch_breakdown[i][3],
            "dispatch_source_b_pct": dispatch_breakdown[i][4],
            # Seven-flow decomposition + shadow prices + savings (nimbus
            # issue #264) -- see flow_decomp/flow_econ's own construction
            # above for the full rationale.
            "flow_pv_to_load_kw": round(flow_decomp[i]["pv_to_load"], 3),
            "flow_pv_to_battery_kw": round(flow_decomp[i]["pv_to_battery"], 3),
            "flow_pv_to_grid_kw": round(flow_decomp[i]["pv_to_grid"], 3),
            "flow_battery_to_load_kw": round(flow_decomp[i]["battery_to_load"], 3),
            "flow_battery_to_grid_kw": round(flow_decomp[i]["battery_to_grid"], 3),
            "flow_grid_to_load_kw": round(flow_decomp[i]["grid_to_load"], 3),
            "flow_grid_to_battery_kw": round(flow_decomp[i]["grid_to_battery"], 3),
            # nimbus issue #629 (Mark Purcell): the honest remainder of
            # the battery's own residual discharge once flow_battery_to_
            # grid_kw is capped at what the LP actually exported this
            # period -- never a phantom export again. See
            # _flow_decomposition()'s own docstring for what this
            # genuinely represents (and doesn't claim to represent).
            "flow_battery_to_losses_kw": round(flow_decomp[i]["battery_to_losses"], 3),
            # nimbus issue #641 (Mark Purcell, live verification of
            # #629): the identical bug one level up -- real PV surplus
            # the plan neither stores, exports, nor consumes this
            # period (the LP is curtailing, or the solar forecast
            # exceeds what the plan absorbs). See _flow_decomposition()'s
            # own docstring for the full reasoning.
            "flow_pv_to_curtailment_kw": round(flow_decomp[i]["pv_to_curtailment"], 3),
            **flow_econ[i],
            # Real per-period duration (2026-08-17, found while fixing a
            # real bug this same session: the daily-summary dashboard
            # card was hardcoding a flat 0.25h multiplier for every
            # period's own kWh contribution -- correct for the first 24h
            # (TIER1_PERIOD_HOURS=0.25) but WRONG for anything beyond it
            # (TIER2_PERIOD_HOURS=1.0), silently under-counting a coarse-
            # tier period's real kWh by 4x. "Today" is entirely inside
            # the fine tier so was unaffected, but "Tomorrow" spans BOTH
            # tiers -- exposing this field lets any consumer compute real
            # kWh sums correctly regardless of which tier a period falls
            # in, instead of assuming a fixed width.
            "hours": round(period_hours_arr[i], 4),
            "net_cost": round(
                import_price[i] * float(corrected_grid_import[i]) * period_hours_arr[i]
                - export_price[i] * float(plan.grid_export_kw[i]) * period_hours_arr[i]
                - export_bonus_price[i]
                * float(plan.export_bonus_kw[i])
                * period_hours_arr[i],
                4,
            ),
        }
        for i in range(n_periods)
    ]

    # Named cost-component breakdown (2026-08-25, nimbus issue #149) --
    # see compute_cost_breakdown()'s own docstring for the full reasoning.
    cost_breakdown = compute_cost_breakdown(
        net_costs=[period["net_cost"] for period in forecast],
        total_cost=plan.total_cost,
        degradation_cost_per_kwh=sw._cfg_num(
            cfg, "solver_degradation_cost_per_kwh", 0.0
        ),
        total_throughput_kwh=total_throughput_kwh,
        charge_cost=charge_cost,
        total_charge_kwh=total_charge_kwh,
        discharge_cost_arr=discharge_cost_arr,
        battery_discharge_kw=plan.battery_discharge_kw,
        period_hours=period_hours_arr,
        soc_penalty_cost=plan.soc_penalty_cost,
        grid_import_excess_penalty_cost=plan.grid_import_excess_penalty_cost,
    )

    # Cost-band diagnostic (2026-08-25, nimbus issue #147) -- see
    # compute_cost_band()'s own docstring for the full reasoning.
    cost_band = compute_cost_band(
        period_hours=period_hours_arr,
        load_lower_kw=np.array(load_lower_kw),
        load_upper_kw=np.array(load_upper_kw),
        solar_kw=np.array(solar_kw),
        import_price=np.array(import_price),
        export_price=np.array(export_price),
        charge_committed_kw=plan.battery_charge_kw,
        discharge_committed_kw=plan.battery_discharge_kw,
        charge_cost=charge_cost,
        discharge_cost_arr=discharge_cost_arr,
        final_soc_kwh=float(plan.battery_soc_kwh[-1]),
        salvage_value=salvage_value,
        # nimbus issue #493: this read-only #630 diagnostic takes a
        # single flat limit for its whole window by design -- see
        # main()'s own comment where static_import_limit_kw/
        # static_export_limit_kw are captured, kept deliberately
        # separate from the real per-period envelope-resolved arrays
        # GridConfig itself now uses for the actual dispatch decision.
        import_limit_kw=static_import_limit_kw,
        export_limit_kw=static_export_limit_kw,
    )
    # nimbus issue #630 (Mark Purcell: "a band 75 times wider than the
    # day's bill tells a household nothing" -- the full 96h band is real
    # (its own width is earned from the load forecast's own confidence
    # interval widening the further out a period sits), but a household
    # reading it next to "the next 24 hours cost $4.67" has no way to
    # tell that the $349 width is mostly coming from periods 2-4 days
    # out. Same compute_cost_band(), same inputs, just sliced to
    # whichever periods fall inside the first 24 real hours -- an
    # honest, cheap re-use of the exact same re-costing machinery, not a
    # new band formula. final_soc_kwh is the plan's own SoC AT the 24h
    # mark (not the 96h terminal SoC) so the salvage-value term prices
    # what the battery is actually worth at THIS band's own horizon end.
    n_24h = periods_within_hours(period_hours_arr, 24.0)
    cost_band_24h = compute_cost_band(
        period_hours=period_hours_arr[:n_24h],
        load_lower_kw=np.array(load_lower_kw)[:n_24h],
        load_upper_kw=np.array(load_upper_kw)[:n_24h],
        solar_kw=np.array(solar_kw)[:n_24h],
        import_price=np.array(import_price)[:n_24h],
        export_price=np.array(export_price)[:n_24h],
        charge_committed_kw=plan.battery_charge_kw[:n_24h],
        discharge_committed_kw=plan.battery_discharge_kw[:n_24h],
        charge_cost=charge_cost,
        discharge_cost_arr=discharge_cost_arr[:n_24h],
        final_soc_kwh=float(plan.battery_soc_kwh[n_24h - 1]),
        salvage_value=salvage_value,
        import_limit_kw=static_import_limit_kw,
        export_limit_kw=static_export_limit_kw,
    )
    # nimbus issue #630's second ask ("say on the sensor whether any
    # risk-aversion term is active"): a plain, honest boolean -- true
    # only when at least one of the three configured weights is genuinely
    # nonzero, so a reader doesn't have to cross-reference three separate
    # published numbers to answer "is anything actually being hedged
    # against right now."
    risk_aversion_active = bool(
        risk_aversion > 0.0
        or import_price_risk_aversion > 0.0
        or export_price_risk_aversion > 0.0
    )

    # Binding-constraint diagnostics (2026-08-18, Mark Purcell's audit
    # item #3; relabelled 2026-08-24, see compute_binding_constraint_
    # label()'s own docstring near resolve_max_discharge_kw for the
    # full "pinned at zero vs pinned at the real ceiling" story).
    # nimbus issue #493: export_limit_kw/import_limit_kw are now the real
    # per-period envelope-resolved arrays -- period 0's own value is the
    # correct "what's binding RIGHT NOW" bound to compare against
    # (matches this function's own existing period_hours_arr[0] usage
    # right below).
    # nimbus issue #921: period 0's own P2P commitment, when there is
    # one, so the label can name the pin instead of reporting its own
    # bound table's blind spot as an unexpected solver state.
    _fixed_export_now = (
        float(grid.fixed_export_kw[0])
        if grid.fixed_export_kw is not None and len(grid.fixed_export_kw) > 0
        else None
    )
    binding_now, binding_now_value_per_kwh = compute_binding_constraint_label(
        plan,
        export_limit_kw[0],
        import_limit_kw[0],
        max_charge_kw,
        max_discharge_kw,
        period_hours_arr[0],
        _fixed_export_now,
    )
    # Earliest export_bonus_cap_<date> entry (ISO date strings sort
    # correctly as plain strings) is always tonight's/the current cap --
    # None when the two-tier export bonus mechanism isn't active at all.
    _p2p_cap_keys = sorted(
        k
        for k in plan.duals
        if k.startswith("export_bonus_cap_") and k != "export_bonus_cap_global"
    )
    p2p_volume_cap_shadow_price = (
        round(plan.duals[_p2p_cap_keys[0]], 4) if _p2p_cap_keys else None
    )

    _diag_757_battery_forecast = build_per_battery_forecast(
        plan, grid_times, n_periods, battery_capacity_by_name
    )
    solver_shared._LOGGER.debug(
        "Nimbus #757 diag: about to ha_post_state(%s) with batteries=%s (plan id=%x)",
        ENTITY_ID,
        [b["name"] for b in _diag_757_battery_forecast],
        id(plan),
    )
    sw.ha_post_state(
        ENTITY_ID,
        round(float(net_battery[0]), 3),
        {
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
            "friendly_name": "Nimbus Solver Battery Forecast",
            # 2026-08-25, nimbus issue #189 (Mark Purcell, real-install
            # reproducer -- the follow-up to #187): this is the
            # "flagship diagnostic sensor" a Nimbus dashboard is most
            # likely to be built against, and it's a genuinely different
            # class again from both NimbusForecastSensor (fixed in
            # v0.89.1) and _NimbusSolverPushSensor's OTHER instance
            # (household load total, fixed in v0.92.1) -- this is the
            # Solver's own LP-derived dispatch plan, not a mirror or a
            # sum of upstream forecasts. "battery" (SIGNAL_ROLE_BATTERY's
            # own string value) is definitionally this entity's role.
            # source_sensor is the one real, measured entity this whole
            # plan is actually built around -- the live SoC reading the
            # LP solves forward from -- since there's no single upstream
            # "battery forecast" sensor the way load has
            # solver_load_forecast_sensor.
            "signal_role": "battery",
            "source_sensor": cfg["solver_battery_soc_sensor"],
            "forecast": forecast,
            # nimbus issue #563 item 5 -- see build_per_battery_
            # forecast()'s own docstring for the full reasoning.
            # nimbus issue #757 diag: reusing the exact same list computed
            # and logged just above, instead of a second, separate call --
            # guarantees the diagnostic log and the real published value
            # are provably the same object, not two independent
            # computations that could theoretically diverge.
            "batteries": _diag_757_battery_forecast,
            "status": plan.status,
            # nimbus issue #756-golden-CI-flake: rounded to match every
            # sibling KPI's own precision (total_cost_with_fixed_costs,
            # cost_breakdown, cost_band all round to 4dp) -- HiGHS's LP
            # solve is not bit-for-bit deterministic run to run (observed
            # directly: two CI runs of the identical frozen-time fixture
            # differed at ~1e-13, e.g. 26.987417226270225 vs
            # 26.98741722627023), which previously leaked straight through
            # this one unrounded field into the golden-output guardrail
            # test and any real dashboard/history consumer. Internal
            # residual math (compute_cost_breakdown()'s own
            # terminal_value_credit) still uses the raw, unrounded
            # plan.total_cost above -- only the published value changes.
            "total_cost": round(plan.total_cost, 4)
            if plan.total_cost is not None
            else None,
            "total_cost_with_fixed_costs": round(total_cost_with_fixed_costs, 4),
            "cost_breakdown": cost_breakdown,
            "cost_band": cost_band,
            # nimbus issue #630: the same band, real-costed against only
            # the periods inside the next 24 real hours -- see this
            # sensor's own construction above for why the 96h band alone
            # is uninformative next to a household's own "what will
            # today cost" question. None whenever the 96h band's own
            # re-costing failed (same honest-diagnostic contract as
            # cost_band itself).
            "cost_band_24h": cost_band_24h,
            "p2p_match_fraction": round(match_fraction, 4),
            "risk_aversion": risk_aversion,
            "import_price_risk_aversion": import_price_risk_aversion,
            "export_price_risk_aversion": export_price_risk_aversion,
            # nimbus issue #630's second ask: whether any of the three
            # risk-aversion weights immediately above is actually
            # nonzero right now -- so a reader can tell "these numbers
            # are shown but inactive" from "these numbers are shaping
            # the plan" without doing that comparison themselves.
            "risk_aversion_active": risk_aversion_active,
            # Nimbus issue #205 (Mark Purcell, 2026-08-26): asked for an
            # entity exposing the terminal-value stack so overnight
            # reserve size can be regressed against price data without
            # pulling the full diagnostic each session. salvage_value is
            # the configured $/kWh rate terminal_value_breakpoints_for()
            # derives its curve from (see solver/elements.py's own
            # BatteryConfig docstring); degradation_cost_per_kwh is the
            # other half of the marginal-discharge economics driving the
            # same reserve decision. Both are the flat, currently-active
            # per-solve values, not a historical series.
            "salvage_value": salvage_value,
            "degradation_cost_per_kwh": sw._cfg_num(
                cfg, "solver_degradation_cost_per_kwh", 0.0
            ),
            "total_charge_kwh": round(total_charge_kwh, 2),
            "total_discharge_kwh": round(total_discharge_kwh, 2),
            "total_throughput_kwh": round(total_throughput_kwh, 2),
            "equivalent_full_cycles": round(equivalent_full_cycles, 3),
            # Nimbus issue #168 (Mark Purcell, 2026-08-25): "a user reading
            # solver_efficiency_percent = 95 would reasonably interpret it
            # as one-way... and get the arithmetic wrong." These four
            # fields pin down the exact convention this entity's own
            # forecast[].battery_kw and totals above already use, verified
            # against real live data (residual < 0.5 kWh over 100+ kWh
            # throughput on Mark's own atomic snapshot):
            # battery_kw is AC-side (grid-side of the inverter), and
            # solver_efficiency_percent is a ROUND-TRIP figure applied as
            # sqrt(round_trip) to each direction independently -- see
            # charge_discharge_efficiency's own comment above.
            "battery_kw_side": "AC",
            # Nimbus issue #197 (Mark Purcell, 2026-08-26): battery_kw's own
            # sign wasn't documented anywhere, and its convention here is
            # the opposite of what a reader would naturally assume --
            # net_battery = discharge_kw - charge_kw above, so POSITIVE
            # means discharging and NEGATIVE means charging. Mark had to
            # reverse-derive this from his own SoC data before he could
            # trust an external analysis built on this field.
            "battery_kw_sign_convention": "positive_discharge_negative_charge",
            "efficiency_convention": "round_trip_symmetric_sqrt",
            # Nimbus issue #237 (Mark Purcell, 2026-08-27): "confirm and
            # expose the price-blend algorithm". Originally an unweighted
            # np.mean() across every source with real coverage at a
            # period, primary included -- confirmed live against Mark's
            # #236 repro (a genuinely-real Amber Express value averaged
            # 50/50 against a genuinely-real but far larger QLD1 PD7DAY
            # wholesale forecast, inverting the import/export price
            # relationship and causing an unphysical simultaneous-
            # import-and-export LP plan). Fixed same-day in #239
            # (primary-preferring): the primary now wins UNBLENDED
            # whenever it has real coverage, full stop -- a secondary
            # only ever fills a period where the primary itself lacks
            # real coverage yet (its one legitimate job, extending the
            # horizon past Amber's own ~24h reach). See
            # blend_price_with_secondary_sources()'s own docstring for
            # the full market-structure argument (import/export both
            # derive from the SAME underlying AEMO spot price for a
            # given region+interval -- they're not independent
            # estimators that can legitimately disagree while both are
            # live).
            "price_blend_algorithm": "primary_preferring_fallback_to_secondary_mean",
            "charge_efficiency": round(charge_discharge_efficiency, 4),
            "discharge_efficiency": round(charge_discharge_efficiency, 4),
            # $ value of the AC-bus losses these efficiencies imply over
            # this horizon's own total_charge_kwh/total_discharge_kwh --
            # matches Mark's own Kirchhoff reconciliation formula
            # (tc*(1-eff) + td*(1/eff-1)), the expected gap between
            # AC-side source/sink sums once real inverter losses are
            # accounted for.
            "ac_bus_losses_kwh": round(
                total_charge_kwh * (1 - charge_discharge_efficiency)
                + total_discharge_kwh * (1 / charge_discharge_efficiency - 1),
                3,
            ),
            "p2p_recent_avg_volume_kwh": round(p2p_recent_volume_kwh, 2),
            # nimbus issue #567: real-time visibility for the dashboard's
            # own "$$$" spike indicator -- True whenever a spike is
            # DETECTED (price threshold or alert entity), independent of
            # whether the household has armed the discharge override.
            "price_spike_active": price_spike_active,
            # Nimbus issue #128 (Mark Purcell): rolling actual-vs-forecast
            # solar ratio, catches implicit inverter AC-side clipping
            # #114's own curtailment switch can't see. None when
            # solver_solar_power_sensor isn't configured, or when no
            # prediction has resolved yet -- both honest no-ops, never
            # a fabricated 1.0.
            "solar_delivery_ratio": (solar_delivery or {}).get("solar_delivery_ratio"),
            "solar_delivery_sample_count": (solar_delivery or {}).get(
                "solar_delivery_sample_count", 0
            ),
            "solar_delivery_underperforming": (solar_delivery or {}).get(
                "solar_delivery_underperforming", False
            ),
            # nimbus issue #1259: the index-0-vs-index-1 solar
            # disagreement measurement, as ONE nested dict and only when
            # it actually ran. Absent rather than None when the switch is
            # off (the default), deliberately: an always-present null
            # would widen the attribute surface of this entity, and every
            # golden-master snapshot with it, for a measurement no
            # install has asked for yet. See update_solar_nowcast_
            # disagreement()'s own docstring for what each field means
            # and why the mechanism #1259 originally proposed was
            # rejected instead of built.
            **(
                {"solar_nowcast_check": (solar_delivery or {})["solar_nowcast_check"]}
                if (solar_delivery or {}).get("solar_nowcast_check") is not None
                else {}
            ),
            "load_summed_18_now_kw": round(summed_18_now_kw, 3),
            "load_whole_house_cross_check_now_kw": round(whole_house_now_kw, 3)
            if whole_house_now_kw is not None
            else None,
            # nimbus issue #429 (Mark Purcell): the two fields above are
            # DELIBERATELY forecast-vs-forecast (sum of 18 circuit models
            # vs the whole-house meter's own separate forecast model) --
            # genuinely useful for catching a missing/misconfigured
            # circuit, but neither is a live meter reading despite what
            # "cross_check" suggests (confirmed live: Mark's own report
            # read load_whole_house_cross_check_now_kw as "the real
            # whole-house meter", a real, understandable misreading given
            # the name). This is the genuine live reading, so a real
            # forecast-vs-reality check is finally possible; additive
            # only, doesn't change either existing field's own value.
            "load_whole_house_live_now_kw": round(live_load_kw, 3)
            if live_load_kw is not None
            else None,
            "failed_load_entities": failed_load_entities,
            # NEW (2026-08-24, issue #105) -- same dual-publication
            # convention as failed_load_entities/load_forecast_source_
            # error immediately below: present here AND on sensor.
            # nimbus_household_load_total_forecast.
            "load_forecast_warnings": load_forecast_warnings,
            # None on success -- real fix for nimbus repo issue #66
            # ("no attribute on sensor.nimbus_solver_battery_forecast
            # telling the operator the sensor shape they wired in was
            # rejected"). Present here (this entity) AND on sensor.
            # nimbus_household_load_total_forecast above -- the issue
            # named both.
            "load_forecast_source_error": load_forecast_error,
            # NEW (2026-08-25, nimbus issues #148/#116) -- present here AND
            # on sensor.nimbus_household_load_total_forecast above, same
            # dual-publication convention as the two fields immediately
            # above. See this field's own construction site (near
            # solver_load_forecast_entities, above) for the full reasoning.
            "load_forecast_source_used": load_forecast_source_used,
            # nimbus issue #937 item 4: WHICH forecast the LP consumed, as
            # distinct from which sensors it was read from just above.
            #
            # Seven keys rather than one nested dict, because these are scalars a
            # dashboard template and an apexcharts series can read directly, and
            # because `sensor_flattened.py`'s own rule (see its comment on
            # `load_forecast_source_used`) is that a flattened child needs a
            # scalar. Always present, always all seven -- #589.
            #
            # `load_forecast_persistence_weight` is the one that says whether
            # anything actually changed: 0.0 means the LP consumed the ML
            # forecast unaltered, which is every install that has not moved
            # `select.nimbus_solver_load_forecast_source_policy` off `off`.
            **_load_forecast_source_attributes(load_forecast_source_decision),
            # NEW (2026-08-25, issue #112) -- present here AND on sensor.
            # nimbus_household_load_total_forecast above (see that
            # field's own comment for the full reasoning). Directly
            # comparable to horizon_hours below: a smaller coverage
            # means part of this plan's own load input is
            # resample_forecast()'s flat-hold padding, not real.
            "load_forecast_coverage_hours": round(load_forecast_coverage_hours, 1)
            if load_forecast_coverage_hours is not None
            else None,
            "n_clamped_periods": n_clamped,
            "n_periods": n_periods,
            "horizon_hours": round(horizon_days * 24, 1),
            "solve_seconds": round(solve_seconds, 2),
            # nimbus issue #652 (Mark Purcell): sensor.nimbus_solver_
            # solve_seconds published duration alone, with no way to
            # tell "solve got slower because the problem got bigger" (a
            # real, legitimate reconfiguration -- Mark's own real case
            # was a 1-battery to 3-battery fleet change) from "solve got
            # slower because something regressed" without pulling raw
            # history and cross-referencing config-reload timestamps by
            # hand. Surfaced as extra_state_attributes on that flattened
            # sensor (see sensor_flattened.py's own attrs_source_key
            # mechanism) so a future jump can be attributed on sight.
            # Straight from the plan build_plan() itself just solved
            # against, so this can never drift from what actually ran.
            # nimbus issue #773 (2026-09-14): n_controllable_loads used to
            # sum only sheddable + adequacy loads, silently omitting
            # `thermal_loads` -- a first-class controllable-load kind
            # since #774/#800, and the kind the one real thermal load in
            # existence actually uses.
            #
            # Found live on devhub, not reasoned about: six controllable
            # loads were demonstrably in the plan (their own status
            # sensors reading "scheduled 09:00-15:30", "running", "done")
            # while this field reported 0. That is not a cosmetic
            # undercount -- #773's own triage reasoned directly from this
            # number ("whether the calibrated/secondary-cost lex phase has
            # an edge case that a large controllable-load + multi-battery-
            # participant combination can push into infeasibility"), so an
            # under-reporting field was actively misleading the
            # investigation into the problem shape.
            #
            # Broken out per kind rather than only corrected in total:
            # "which KIND of load grew" is the question a solve-time or
            # infeasibility regression actually needs answered, and a
            # single total cannot answer it. n_controllable_loads stays as
            # the honest sum so nothing consuming it breaks.
            "solve_diagnostics": {
                "n_batteries": len(plan.batteries),
                "n_periods": n_periods,
                "n_controllable_loads": len(plan.sheddable_loads)
                + len(plan.adequacy_loads)
                + len(plan.thermal_loads),
                "n_sheddable_loads": len(plan.sheddable_loads),
                "n_adequacy_loads": len(plan.adequacy_loads),
                "n_thermal_loads": len(plan.thermal_loads),
                # nimbus issue #1386: which thermal loads had their HARD
                # "must reach target by the deadline" guarantee relaxed this
                # cycle, because the hard-constrained solve came back
                # infeasible and network.py retried with a soft shortfall
                # price instead.
                #
                # network.py already logs a WARNING naming them, so this is
                # not about visibility in the moment -- it is about the
                # moment being gone. Production's own error log is truncated
                # at every container restart (measured on #1360: read at
                # 18:18 AEST it began at 16:09, 2 h 8 min of history), so a
                # log line cannot answer "how often has this been
                # happening". The recorder keeps this attribute, so it can.
                #
                # That matters because the natural cause of REPEATED
                # relaxation is configuration rather than weather --
                # thermal_max_power_kw too low for the window, a window too
                # narrow for the tank, or a learned heating_rate_c_per_kwh
                # that has drifted low. Each makes the constraint
                # permanently unsatisfiable, and each is absorbed silently by
                # the retry on every cycle: the household sees "hot water is
                # sometimes not hot" with nothing to look at. A rate visible
                # in history is what turns that into a diagnosable pattern.
                #
                # #774's argument for making the guarantee a HARD constraint
                # was that a bound cannot be traded away by a tie-break or an
                # objective-weight bug. That argument covers the bound and
                # says nothing about the retry -- which is the one event that
                # turns the guarantee soft for a cycle.
                #
                # A list rather than a count, because WHICH load matters on a
                # multi-load install and a count cannot say. Empty list =
                # nothing relaxed, which is the normal case and is
                # distinguishable from the field being absent on an older
                # release.
                "thermal_guarantee_relaxed": list(
                    getattr(plan, "thermal_guarantee_relaxed", []) or []
                ),
                # nimbus issue #485 acceptance criterion 3: "Diagnostics
                # show household_mode alongside the plan" -- so a plan can
                # be read knowing which mode produced it. Resolved live
                # from select.nimbus_household_mode via the solver-config
                # bridge; None on an install whose select entity has not
                # come up yet, which is honest rather than defaulting to
                # "home" and implying a mode was actually in force.
                "household_mode": cfg.get("household_mode"),
                # nimbus issue #1013. The dial derates capacity now, so
                # the number the solver actually planned against is no
                # longer the number on the dashboard -- and a household
                # that cannot see the difference cannot tell the fix
                # landed. Both are published, not just the result: a lone
                # "119.76" is indistinguishable from someone having
                # retyped the nameplate.
                "battery_soh_percent": sw._cfg_num(
                    cfg, "solver_battery_soh_percent", 100.0
                ),
                "battery_nameplate_capacity_kwh": round(
                    sw._cfg_num(cfg, "solver_battery_capacity_kwh", 0.0), 3
                ),
                "battery_effective_capacity_kwh": round(
                    sw.resolve_effective_capacity_kwh(cfg), 3
                ),
                # nimbus issue #1179: the blend-calibration outcome of the
                # cycle that produced this plan -- i.e. of a HEALTHY cycle,
                # since a failed one returns ~900 lines above this and
                # publishes nothing (#757).
                #
                # **This is the half that makes #1179 falsifiable, and it
                # was missing.** #1229 and #1291 both argue -- correctly --
                # that "the failures took the minimum-weight fallback"
                # explains nothing without the base rate among healthy
                # cycles, and both say the fields are "carried on success as
                # well as failure". They are carried onto `Plan`; nothing
                # then READ them on the success path. `solver_writer.py`
                # touches all three only inside the failure branch, so on a
                # deployed install the healthy-cycle distribution was
                # observable nowhere at all.
                #
                # Published here rather than logged, deliberately, and the
                # reason is measured: production's `/api/error_log` is
                # truncated at every container restart -- read 2026-09-27 at
                # 18:18 AEST it began at 16:09:58 AEST, the boot 2h 8m
                # earlier. An episode that recurs every few nights and lives
                # only in that log is routinely unreadable by the time
                # anyone looks. `solve_diagnostics` is the attribute payload
                # of `sensor.nimbus_solver_solve_seconds` (see
                # sensor_flattened.py's own `attrs_source_key`), which the
                # recorder keeps -- and that sensor's own history is already
                # how a #1179 investigation counts cycles, because a failed
                # solve never publishes it.
                #
                # Four scalars. No entity added, nothing renamed.
                "calibration_min_weight_fallback": plan.calibration_min_weight_fallback,
                "calibration_fallback_reason": plan.calibration_fallback_reason,
                "calibration_weight_used": plan.calibration_weight_used,
                # nimbus issue #1179: the HEALTHY cycle's own simplex
                # iteration count, under the SAME key name the failure
                # warning prints (`simplex_iterations=...`), so the two are
                # read side by side without a translation step.
                #
                # This is what makes a failing cycle's count mean anything.
                # Measured while building this: a small synthetic LP solves
                # optimally at simplex_iteration_count == 0, because presolve
                # finishes it -- so "0 iterations" is not self-evidently a
                # rejection. It is only a rejection relative to what this
                # install's own healthy cycles cost, which for the reference
                # household's 199-period model is tens of thousands (#773
                # measured 12k-214k). Without the baseline recorded next to
                # it, the discriminating number is still an inference.
                "simplex_iterations": plan.iterations,
            },
            "generated_at": now.isoformat(),
            "binding_constraint_now": binding_now,
            "binding_constraint_shadow_price": binding_now_value_per_kwh,
            # nimbus issue #662: same period_hours_arr[0] correction as
            # the per-period "shadow_price" field above -- see that
            # field's own comment for the full mechanism/verification.
            "energy_shadow_price_now": round(
                plan.duals.get("power_balance_t0", 0.0) / period_hours_arr[0], 4
            ),
            "p2p_volume_cap_shadow_price": p2p_volume_cap_shadow_price,
            # nimbus issue #493 (Signals 4/7 of #489, item 1): period 0's
            # own copy of the per-period envelope_import_limit_kw/
            # envelope_export_limit_kw fields above -- same "period 0
            # copy for backward-compatible one-glance reading" convention
            # energy_shadow_price_now already establishes.
            "envelope_import_limit_kw": round(float(import_limit_kw[0]), 3),
            "envelope_export_limit_kw": round(float(export_limit_kw[0]), 3),
            **_risk_aversion_effect_now(plan, solar_kw, import_price, export_price),
        },
    )
    cross_check_str = (
        f"{whole_house_now_kw:.2f}kW"
        if whole_house_now_kw is not None
        else "unavailable"
    )
    coverage_str = (
        f"{load_forecast_coverage_hours:.1f}h"
        if load_forecast_coverage_hours is not None
        else "unknown"
    )
    # Issue #389 (Mark Purcell, live install, v0.94.125): _infeasible_plan()
    # returns total_cost=None, and this status line crashed every cycle for
    # 41 minutes formatting it with .2f (TypeError: unsupported format
    # string passed to NoneType.__format__) -- the plan itself had already
    # been correctly pushed with status="infeasible" by this point, so the
    # ONLY thing failing was this trailing log line, but it took down the
    # rest of main() (quality report / counterfactual / efficiency backtest
    # sensor updates) with it every single cycle. total_cost_with_fixed_costs
    # is already None-safe (built via `plan.total_cost or 0.0` above), it's
    # plan.total_cost itself on this line that wasn't guarded.
    total_cost_str = f"{plan.total_cost:.2f}" if plan.total_cost is not None else "n/a"
    print(
        f"[{now.isoformat()}] pushed {ENTITY_ID}: status={plan.status} "
        f"n_periods={n_periods} horizon={horizon_days * 24:.1f}h "
        f"load_forecast_coverage={coverage_str} solve_time={solve_seconds:.2f}s "
        f"total_cost={total_cost_str} total_cost_with_fixed={total_cost_with_fixed_costs:.2f} "
        f"p2p_match_fraction={match_fraction:.3f} net_battery_now={net_battery[0]:.2f}kW "
        f"summed_18_loads_now={summed_18_now_kw:.2f}kW whole_house_cross_check={cross_check_str} "
        f"previous_plan_found={previous_plan is not None} "
        f"binding_now={binding_now!r} energy_shadow_price_now={plan.duals.get('power_balance_t0', 0.0) / period_hours_arr[0]:.4f} "
        f"p2p_volume_cap_shadow_price={p2p_volume_cap_shadow_price}"
    )
