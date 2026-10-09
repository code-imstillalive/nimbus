"""`apply_commanded_state_guard()` -- the controllable-load dispatch guard,
moved verbatim out of `solver_writer.py`.

nimbus issue #1305, Phase 6 of #1298's decomposition -- the phase #1298 itself
calls "the highest-risk, done deliberately last", on cited evidence: #726, #741,
#733, #769, #770/#782 and the shared-charger widening bug fixed in #1140 all
lived in exactly this guard logic. So this is a pure relocation, verified
mechanically: strip every inserted `sw.` and each moved block is byte-for-byte
the original.

## What moved, and what it actually looks like

`apply_commanded_state_guard` is 1,300 lines but **not a flat body**. It is a
428-line outer scope plus six nested definitions:

| nested | kind | lines |
|---|---|---:|
| `_plan_cost_forecast` | def | 19 |
| `_raw_shadow_price_series` | def | 45 |
| `_plan_shadow_price_forecast` | def | 14 |
| `_async_fetch_thermal_history` | async def | 81 |
| `_async_fetch_ambient_history` | async def | 53 |
| `_update_all` | async def | **872** |

The outer function is **sync**. It builds `_update_all` as a closure over its own
locals and runs it on HA's own loop:

    future = _asyncio.run_coroutine_threadsafe(_update_all(), solver_shared.NATIVE.hass.loop)
    future.result(timeout=10)

wrapped in the broad `except Exception` that logs #1019's WARNING. So the
injected `hass` is needed for `.loop`, not only for state reads. Since #1437 it
is read from `solver_shared.NATIVE`, a stable-identity holder, rather than
through the deferred `solver_writer` seam.

That closure structure is very likely *why* the function is 1,300 lines: the
nested bodies capture the outer scope, so splitting them out is a real refactor
rather than a move. This phase does not attempt it.

## The import rule

See this package's `__init__.py` -- the seam, the per-name measurements that make
it load-bearing, and the `_LOGGER` question, where spec 006's Invariants were
right and this module's first draft was wrong.

`_fetch_weather_hourly_forecast` stays in `solver_writer.py` and is reached from
here, exactly as spec 006's Facade decision specifies: it has a second,
non-dispatch caller, and its own blocker `ha_call_service_with_response` is
was blocked on `_NATIVE_HASS` (Phase 7b) until #1437 replaced that rebound
name with `solver_shared.NATIVE`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
from numpy.typing import NDArray

try:
    from .. import solver_shared
    from ..solver import network
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]
    from solver import network  # type: ignore[no-redef]


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by name)
    -- see this package's `__init__.py` for the full reasoning. In short: a
    module-scope `from ..solver_writer import x` would both be circular and bind
    `x` at import time, silently defeating every
    `patch.object(solver_writer, "x", ...)` in the suite."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


# nimbus issue #712/#713: log-once-per-(load, day) dedup for the
# hardware-floor-crossing warning below -- same #313/#314 discipline as
# every other recurring per-cycle warning in this module. Keyed by
# (subentry_id, day_key) -- the same identifier this function's own
# other warnings already log by, not a load's display name -- so a
# genuinely NEW day's own crossing still gets its own warning even if
# yesterday's was already reported.
_FLOOR_CROSSING_WARNED: set[tuple[str, str]] = set()

# nimbus issue #875, gap found by Mark Purcell's IV&V of PR #930. Same
# (subentry_id, day_key) shape and the same reasoning as the floor-crossing
# set just above: the condition it reports holds for every remaining solve
# of the day once it is true, and this codebase has already had to clean up
# per-cycle log spam twice (v0.94.301/302's overlap guard, #773's
# diagnostic quieted to two-tier DEBUG/WARNING). Keyed per load so one
# exhausted device cannot mute another's warning.
_REAFFIRM_CAP_WARNED: set[tuple[str, str]] = set()


async def dispatch_commanded_state(
    hass,
    entity_id: str,
    commanded_state: bool,
    climate_on_hvac_mode: str | None = None,
) -> None:
    """nimbus issue #476/#534: the real output/actuation layer -- the
    piece #484's own docstring flagged as missing ("this bookkeeping has
    no consumer yet... no sensor exposes commanded_state today"). Calls
    the right HA service for `entity_id`'s own domain, decided by a plain
    string split on the entity_id itself (no config field needed for
    "which domain is this" -- HA entity_ids are already domain-prefixed).

    switch.*: turn_on/turn_off, unconditional -- no per-domain nuance.

    water_heater.*: set_operation_mode("performance"/"eco"), per #534
    item 3's own real device investigation (a DIY SG-Ready bridge whose
    two modes are exactly these two literal strings) -- deliberately NOT
    a per-load configurable mode-string pair in this pass; every real
    water_heater device #534 investigated uses this exact convention, and
    adding a speculative override for hardware nobody has yet would be
    guessing ahead of a real need.

    climate.* (nimbus issue #756, follow-up to #534): set_hvac_mode --
    "off" unconditionally when commanded_state is False (universally
    present in HA's own HVACMode enum, zero ambiguity), and
    `climate_on_hvac_mode` when True. Deliberately NOT climate.turn_on/
    turn_off: checked live against a real reference-household climate
    entity, its own `supported_features` did not advertise the
    TURN_ON/TURN_OFF capability bits, so those services are not a safe
    universal substitute. Deliberately NOT a guessed single "on" mode
    either -- the same real entity's own `hvac_modes` included both
    `heat` and `cool`, so there is no safe universal default; see
    CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE's own const.py comment.
    A climate device_entity with `climate_on_hvac_mode` left unset logs a
    WARNING and does not dispatch on an ON transition (never guesses) --
    OFF transitions are unaffected, since "off" needs no configured mode.

    An unrecognized domain (anything other than switch/water_heater/
    climate) logs a WARNING and is a safe no-op, never a crash -- a
    household who configures a device_entity in an unsupported domain
    finds out from the log, not from a silently-ignored command.

    Raises on a genuine service-call failure (a bad entity_id, the
    service unavailable) -- the caller (apply_commanded_state_guard())
    wraps each load's own dispatch in its own try/except so one load's
    failure never blocks another's, matching this file's established
    best-effort posture elsewhere.
    """
    domain = entity_id.split(".", 1)[0]
    if domain == "switch":
        service = "turn_on" if commanded_state else "turn_off"
        await hass.services.async_call(
            "switch", service, {"entity_id": entity_id}, blocking=False
        )
    elif domain == "water_heater":
        mode = "performance" if commanded_state else "eco"
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": entity_id, "operation_mode": mode},
            blocking=False,
        )
    elif domain == "climate":
        if commanded_state:
            if not climate_on_hvac_mode:
                solver_shared._LOGGER.warning(
                    "Nimbus: controllable load device entity '%s' is a "
                    "climate entity with no configured ON hvac_mode "
                    "(CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE) -- not "
                    "dispatched (#756). Configure one in the Controllable "
                    "Load wizard to enable ON commands for this device.",
                    entity_id,
                )
                return
            hvac_mode = climate_on_hvac_mode
        else:
            hvac_mode = "off"
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode},
            blocking=False,
        )
    else:
        solver_shared._LOGGER.warning(
            "Nimbus: controllable load device entity '%s' has an unsupported "
            "domain '%s' for dispatch -- switch, water_heater, and climate "
            "are supported today",
            entity_id,
            domain,
        )


def _resolve_reaffirm_after_seconds(data: dict) -> float | None:
    """How long this load may be seen disagreeing with its own command
    before Nimbus re-sends it (nimbus issue #875).

    Per load, not global -- the household's own reasoning on #769, which
    applies identically here: "each load can have its own urgency."

    Returns None when nothing is configured, leaving the default to
    load_run_state.reaffirm_allowed() itself rather than resolving it
    here -- the default belongs beside the behaviour it governs, and
    this keeps the helper free of any cross-module import.

    **0 disables re-sending for this load**, restoring exactly the
    pre-#875 edge-triggered-only behaviour, which is the escape hatch
    for a device that must never be commanded twice. A negative or
    unparseable value is treated as unset rather than as 0, so a typo
    fails towards the working default instead of silently switching the
    feature off.
    """
    # Dual-mode, and NOT optional. A relative-only import looked safe
    # here -- this helper is reachable only from apply_commanded_state_
    # guard(), which is native-only -- and that reasoning is wrong: the
    # test harness imports solver_writer as a BARE module and calls that
    # guard directly. A relative import then raises ImportError, which
    # the guard's own whole-function handler swallows at DEBUG, aborting
    # every dispatch for that cycle silently. Caught by three existing
    # #741/#484 tests going red, which is exactly what they are for.
    try:
        from ..const import CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES as _KEY
    except ImportError:  # bare-module import shape
        from const import CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES as _KEY

    raw = data.get(_KEY)
    if raw is None:
        return None
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        return None
    if minutes < 0:
        return None
    return minutes * 60.0


def apply_commanded_state_guard(
    plan: network.Plan,
    now: datetime,
    grid_times: list[datetime],
    period_hours_arr: NDArray[np.float64] | None = None,
    import_price_arr: NDArray[np.float64] | list[float] | None = None,
    tariff_attributed_cost_by_subentry: dict[str, float] | None = None,
    cfg: dict | None = None,
) -> None:
    """nimbus issue #484/#534: the relay-chatter guard, now with a real
    output stage. Reads each Controllable Load's own real, just-solved
    period-0 power off `plan` (its `subentry_id`, threaded through from
    build_controllable_loads()'s own config objects via elements.py/
    network.py), decides the raw new commanded state (on if period-0
    power exceeds the same on-threshold load_run_state.py's own power
    sampling uses, for a consistent on/off reading between the measured
    and the commanded side), persists the DEBOUNCED result via
    load_run_state.decide_commanded_state() -- see that function's own
    docstring for the actual guarantee -- and, when that subentry has a
    configured CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY AND commanded_state
    just genuinely CHANGED, actually dispatches it via
    dispatch_commanded_state() above. A load with no device entity
    configured is scored/persisted exactly as before this issue -- never
    physically commanded, a real no-op.

    Per-load min_hold_minutes (CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES)
    overrides the shared DEFAULT_MIN_HYSTERESIS_PERIODS-based debounce for
    just that load when set. A real ON dispatch is additionally gated by
    load_run_state.activation_allowed() against CONF_CONTROLLABLE_LOAD_
    MAX_ACTIVATIONS_PER_DAY -- when the cap is already reached, the
    solver's own desired commanded_state is still persisted (so a
    consumer honestly sees "wants ON, capped" rather than a silent lie),
    but no real service call is issued this cycle; record_activation()
    only increments on an actual, successful dispatch, never a blocked
    one. An OFF transition is never capped -- #534's own cap is
    specifically on "performance activations", not on releasing a load.

    nimbus issue #581 (Mark Purcell, real use the day after #578/#579
    shipped): also now persists each load's own FULL per-period plan
    (not just period 0) -- the household's own real ask was "chart the
    day-ahead plan for the heat pump," and the LP already computes this
    every cycle, it was simply being discarded. See LoadRunState's own
    plan_forecast/plan_delivered_kwh_forecast/plan_target_kwh/plan_
    shortfall_kwh/plan_earliest_period/plan_deadline_period/plan_
    nominal_kw fields (load_run_state.py) for exactly what's persisted
    per kind. This refreshes every solve cycle regardless of whether
    commanded_state itself changed (a fresh day-ahead schedule is the
    whole point), unlike the change-gated dispatch/write logic above --
    NimbusControllableLoadStateSensor (sensor.py) excludes these fields
    from long-term recorder history via _unrecorded_attributes (same
    #362 "churns every poll, not worth recording" reasoning already
    applied to NimbusHealthReportSensor's own generated_at/subentry_
    status), so this does not create #362's own class of recorder-churn
    bug.

    Best-effort and silent on any WHOLE-FUNCTION failure (a device_entity
    dispatch failure for one load is caught per-load below and does not
    escalate here) -- same posture as _sample_load_run_state(). Native
    mode only, same reasoning as build_controllable_loads() itself -- a
    no-op when solver_shared.NATIVE.hass is None (standalone/cron mode, or
    plan.sheddable_loads/adequacy_loads are always empty there anyway
    since build_controllable_loads() already returns ([], [])
    unconditionally in that mode).

    `cfg` (nimbus issue #481, optional -- omitted is a complete no-op,
    identical to every pre-#481 caller): the same solver config dict
    main() already threads into publish_plan()/publish_weather_forecast_
    mirrors(). Read here only for CONF_SOLVER_WEATHER_FORECAST_SENSOR --
    when configured, a thermal-forecast-eligible load also learns and
    applies the ambient-scaled loss coefficient (thermal_forecast.py's
    own learn_thermal_rates()/project_temperature_forecast() ambient
    parameters) instead of relying solely on the flat idle-decay rate;
    when not configured (or `cfg` itself is None), every thermal-forecast
    call site here behaves exactly as before this issue.
    """
    # nimbus #1305 (spec 006, Phase 6): every name that used to resolve in
    # solver_writer's own namespace is reached through this seam, at CALL
    # time -- see this package's __init__.py for why, and what breaks if a
    # module-scope `from ..solver_writer import x` is used instead.
    sw = _solver_writer()
    if solver_shared.NATIVE.hass is None or len(grid_times) < 2:
        return
    entries_with_ids = (
        [
            (
                sl.subentry_id,
                sl.served_kw[0] if len(sl.served_kw) else 0.0,
                "sheddable",
                sl,
            )
            for sl in plan.sheddable_loads
            if sl.subentry_id is not None
        ]
        + [
            (
                al.subentry_id,
                al.power_kw[0] if len(al.power_kw) else 0.0,
                "adequacy",
                al,
            )
            for al in plan.adequacy_loads
            if al.subentry_id is not None
        ]
        + [
            # nimbus issue #774: same shape as the adequacy entries above --
            # ThermalLoadPlan.power_kw is this load's own real driving
            # decision variable, the same role AdequacyLoadPlan.power_kw
            # plays for the debounce/dispatch logic below. getattr (not a
            # direct plan.thermal_loads read): this file's own bare-
            # SimpleNamespace test fakes predate this field, same
            # defensive posture as every other optional-attribute read on
            # `plan` in this function (see _raw_shadow_price_series()'s
            # own comment for the precedent).
            (tl.subentry_id, tl.power_kw[0] if len(tl.power_kw) else 0.0, "thermal", tl)
            for tl in getattr(plan, "thermal_loads", [])
            if tl.subentry_id is not None
        ]
    )
    if not entries_with_ids:
        return
    try:
        from homeassistant.helpers.storage import Store as _Store

        try:
            from .. import done_condition, load_run_state, thermal_forecast
            from ..const import (
                CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE,
                CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
                CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
                CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
                CONF_DEFERRABLE_DEADLINE_HOUR,
                CONF_DEFERRABLE_DONE_ENTITY,
                CONF_DEFERRABLE_DONE_WHEN,
                CONF_DEFERRABLE_EARLIEST_HOUR,
                CONF_DEFERRABLE_MAX_POWER_KW,
                CONF_DEFERRABLE_TARGET_KWH,
                CONF_SHEDDABLE_NOMINAL_KW,
                CONF_THERMAL_DEADLINE_HOUR,
                CONF_THERMAL_EARLIEST_HOUR,
                DOMAIN,
            )
        except ImportError:
            import done_condition
            import load_run_state
            import thermal_forecast
            from const import (
                CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE,
                CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
                CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
                CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
                CONF_DEFERRABLE_DEADLINE_HOUR,
                CONF_DEFERRABLE_DONE_ENTITY,
                CONF_DEFERRABLE_DONE_WHEN,
                CONF_DEFERRABLE_EARLIEST_HOUR,
                CONF_DEFERRABLE_MAX_POWER_KW,
                CONF_DEFERRABLE_TARGET_KWH,
                CONF_SHEDDABLE_NOMINAL_KW,
                CONF_THERMAL_DEADLINE_HOUR,
                CONF_THERMAL_EARLIEST_HOUR,
                DOMAIN,
            )

        entries = solver_shared.NATIVE.hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return
        hub_entry_id = entries[0].entry_id
        # getattr, not direct access: existing callers/tests of this
        # function (predating #534) construct a minimal fake entry with
        # no .subentries attribute at all -- a real HA ConfigEntry always
        # has one, but this stays defensive rather than assume every
        # caller's fake matches the real shape.
        hub_subentries = getattr(entries[0], "subentries", {}) or {}
        period_seconds = (grid_times[1] - grid_times[0]).total_seconds()
        default_min_hysteresis_seconds = (
            period_seconds * load_run_state.DEFAULT_MIN_HYSTERESIS_PERIODS
        )
        day_key = now.strftime("%Y-%m-%d")
        n_periods = len(grid_times)

        def _plan_cost_forecast(
            power_series: NDArray[np.float64] | list[float],
        ) -> list[dict[str, object]] | None:
            # nimbus issue #591: per-period PLANNED cost (power_kw *
            # period_hours * that period's own blended import_price),
            # aligned one-to-one with plan_forecast -- None (not a
            # zero-filled series) whenever either input this cycle needs
            # isn't available, so derive_schedule_view() never mistakes
            # "not computed" for "genuinely free."
            if period_hours_arr is None or import_price_arr is None:
                return None
            n = min(len(power_series), len(period_hours_arr), len(import_price_arr))
            cost_values = [
                float(power_series[i])
                * float(period_hours_arr[i])
                * float(import_price_arr[i])
                for i in range(n)
            ]
            return load_run_state.build_time_value_series(grid_times[:n], cost_values)

        def _raw_shadow_price_series() -> list[float]:
            # nimbus issue #613 (item 1 of 3 -- exposure only, NOT the
            # earliness-timing behavior change items 2/3 describe): the
            # whole-system marginal cost of a kWh for every period this
            # load's own plan_forecast covers, straight off the LP's own
            # power_balance_t{i} dual -- "already extracts duals...
            # this is exposure, not new solver work." `getattr` (not a
            # direct `plan.duals` read) because `plan` here is
            # `network.Plan`, whose own dataclass default is `{}`, but
            # this file's own bare-SimpleNamespace test fakes predate
            # this field and don't set it -- same defensive posture as
            # every other optional-attribute read on `plan` in this
            # function. Shared, unrounded source for both
            # _plan_shadow_price_forecast() (the published series) and
            # item 3's own status-reason computation, which needs the
            # real, unrounded lambda(0) to compare against.
            #
            # nimbus issue #685 (Mark Purcell): this read power_balance_
            # t{i}'s own raw dual directly, with no hours[i] division --
            # the exact same #662 bug (the dual comes out in "$ per kW
            # of RHS," an implicit x hours[i] relative to the true
            # $/kWh marginal price) in a THIRD call site #662's own fix
            # never touched, since #613 (which added this function)
            # shipped before #662 was even found. Confirmed live against
            # Mark's own real numbers: every one of 7 checked periods
            # was off from sensor.nimbus_solver_battery_forecast's own
            # correctly-scaled shadow_price by exactly x12 (1/hours[i]
            # for these 5-minute periods). Same correction as the
            # power_balance_t{i} shadow_price field a few hundred lines
            # above (period_hours_arr[i] division) -- falls back to the
            # grid's own uniform period_seconds when period_hours_arr
            # isn't supplied (bare-SimpleNamespace tests predating this
            # parameter), since production always passes a real one
            # (see build_plan()'s own publish_plan() call).
            duals = getattr(plan, "duals", {}) or {}
            fallback_hours = period_seconds / 3600.0
            return [
                duals.get(f"power_balance_t{i}", 0.0)
                / (
                    float(period_hours_arr[i])
                    if period_hours_arr is not None and i < len(period_hours_arr)
                    else fallback_hours
                )
                for i in range(n_periods)
            ]

        def _plan_shadow_price_forecast() -> list[dict[str, object]]:
            return load_run_state.build_time_value_series(
                grid_times,
                _raw_shadow_price_series(),
                # build_time_value_series()'s own 3dp default would
                # collapse real, meaningful precision here -- a $/kWh
                # shadow price this small (Mark's own real numbers:
                # 0.13c/kWh vs 0.06c/kWh, i.e. 0.0013 vs 0.0006 $/kWh)
                # needs the same 4dp this file's own energy_shadow_
                # price_now field already uses, or two genuinely
                # different marginal costs round to the identical
                # published value.
                round_ndigits=4,
            )

        async def _async_fetch_thermal_history(
            done_entity: str, power_sensor: str, start: datetime, end: datetime
        ) -> list[tuple[datetime, float, float]]:
            # nimbus issue #592: the async-native sibling of
            # fetch_entity_attribute_history_range()/fetch_entity_
            # history_range() (this file, near resample_history_nearest())
            # -- those two are SYNC wrappers that internally dispatch onto
            # _NATIVE_HASS.loop via run_coroutine_threadsafe().result(),
            # correct for a sync caller running in an executor thread
            # (_sample_load_run_state()'s own caller context) but a real
            # deadlock risk called from HERE: _update_all() is itself a
            # coroutine already running ON that same loop (dispatched via
            # run_coroutine_threadsafe further down this function), so a
            # blocking .result() call from inside it would wait on the
            # loop it's blocking. Awaits the recorder's own executor job
            # directly instead -- safe from an already-async context.
            try:
                from homeassistant.components.recorder import (
                    get_instance as _recorder_get_instance,
                )
                from homeassistant.components.recorder import (
                    history as _recorder_history,
                )
            except ImportError:
                return []
            try:
                temp_changes = await _recorder_get_instance(
                    solver_shared.NATIVE.hass
                ).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    solver_shared.NATIVE.hass,
                    start,
                    end,
                    done_entity,
                    False,  # no_attributes=False -- current_temperature lives there
                )
                power_changes = await _recorder_get_instance(
                    solver_shared.NATIVE.hass
                ).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    solver_shared.NATIVE.hass,
                    start,
                    end,
                    power_sensor,
                    True,  # no_attributes -- plain numeric state is enough
                )
            except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
                solver_shared._LOGGER.debug(
                    "Nimbus: #592 thermal history fetch failed for %s/%s",
                    done_entity,
                    power_sensor,
                    exc_info=True,
                )
                return []
            temp_points: list[tuple[datetime, float]] = []
            for s in temp_changes.get(done_entity, []):
                raw = s.attributes.get("current_temperature")
                if raw is None:
                    continue
                try:
                    temp_points.append(
                        (s.last_changed.astimezone(sw.LOCAL_TZ), float(raw))
                    )
                except (TypeError, ValueError):
                    continue
            temp_points.sort(key=lambda x: x[0])
            power_points: list[tuple[datetime, float]] = []
            for s in power_changes.get(power_sensor, []):
                try:
                    v = float(s.state)
                except (TypeError, ValueError):
                    continue
                scale = solver_shared.power_scale_or_none(  # nimbus #1570, #1643
                    s.attributes.get("unit_of_measurement")
                )
                if scale is None:
                    continue  # not a power unit: no reading, not kW
                v *= scale
                power_points.append((s.last_changed.astimezone(sw.LOCAL_TZ), v))
            power_points.sort(key=lambda x: x[0])
            power_resampled = sw.resample_history_nearest(
                power_points, [t for t, _ in temp_points]
            )
            return [(t, temp, p) for (t, temp), p in zip(temp_points, power_resampled)]

        async def _async_fetch_ambient_history(
            entity_id: str, start: datetime, end: datetime
        ) -> list[tuple[datetime, float]]:
            # nimbus issue #481: same async-native recorder-history
            # pattern as _async_fetch_thermal_history() just above (same
            # deadlock reasoning -- _update_all() is itself already
            # running on _NATIVE_HASS.loop), just reading a weather
            # entity's own real `temperature` attribute instead of a
            # load's own done_entity/power_sensor pair. Used only to
            # LEARN the ambient-scaled loss coefficient from real past
            # weather (thermal_forecast.learn_thermal_rates()'s own
            # ambient_history parameter) -- the forward FORECAST used for
            # projection is a completely separate fetch
            # (_fetch_weather_hourly_forecast(), a live weather.
            # get_forecasts call, not recorder history).
            try:
                from homeassistant.components.recorder import (
                    get_instance as _recorder_get_instance,
                )
                from homeassistant.components.recorder import (
                    history as _recorder_history,
                )
            except ImportError:
                return []
            try:
                changes = await _recorder_get_instance(
                    solver_shared.NATIVE.hass
                ).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    solver_shared.NATIVE.hass,
                    start,
                    end,
                    entity_id,
                    False,  # no_attributes=False -- temperature lives there
                )
            except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
                solver_shared._LOGGER.debug(
                    "Nimbus: #481 ambient history fetch failed for %s",
                    entity_id,
                    exc_info=True,
                )
                return []
            points: list[tuple[datetime, float]] = []
            for s in changes.get(entity_id, []):
                raw = s.attributes.get("temperature")
                if raw is None:
                    continue
                try:
                    points.append((s.last_changed.astimezone(sw.LOCAL_TZ), float(raw)))
                except (TypeError, ValueError):
                    continue
            points.sort(key=lambda x: x[0])
            return points

        async def _update_all() -> None:
            store = load_run_state.LoadRunStateStore(
                store=_Store(
                    solver_shared.NATIVE.hass,
                    1,
                    f"{DOMAIN}_{hub_entry_id}_load_run_state",
                )
            )
            for subentry_id, period0_kw, load_kind, load_plan in entries_with_ids:
                # nimbus issue #1019, the other half. Every controllable load
                # used to share one try/except -- the outermost one, far below
                # -- so a single raise abandoned the cycle for EVERY load not
                # yet processed. Loads already dispatched stayed dispatched;
                # the rest were simply never commanded. At a 5-minute cadence
                # the next cycle usually recovers, so it presented as
                # intermittent missed dispatch rather than an outage.
                #
                # v0.94.350 made that failure audible (DEBUG -> WARNING). This
                # makes it survivable: one bad load now costs itself and
                # nothing else.
                #
                # The body below is byte-identical to before, re-indented one
                # level and nothing more -- verified mechanically rather than
                # by eye, because #873 established that a mechanical move of a
                # block containing `if` can silently re-parent a following
                # `elif` while passing ruff, mypy and its own tests.
                try:
                    raw_new_state = (
                        float(period0_kw) > load_run_state.DEFAULT_ON_THRESHOLD_KW
                    )
                    subentry = hub_subentries.get(subentry_id)
                    # nimbus issue #645: same live-tuning overlay as build_
                    # controllable_loads() -- covers both reads below
                    # (min_hold_minutes here, max_activations_per_day
                    # further down this same loop iteration's own data).
                    data = (
                        sw._resolve_controllable_load_tuning(subentry.data, subentry)
                        if subentry is not None
                        else {}
                    )
                    min_hold_minutes = data.get(CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES)
                    min_hysteresis_seconds = (
                        float(min_hold_minutes) * 60.0
                        if min_hold_minutes is not None
                        else default_min_hysteresis_seconds
                    )
                    prev = await store.async_read(subentry_id)
                    # nimbus issue #595 (Mark Purcell, real finding: a 0.65kW
                    # heat pump cycled on/off every 15-35 min the first live
                    # morning, burning the daily activation cap before the
                    # cheap window even arrived). The LP's own adequacy-load
                    # plan is jagged at 5-minute resolution -- unlike the
                    # battery, it has no switching cost/smoothness term, so a
                    # single dip below threshold is common even while the
                    # load is genuinely still wanted "on" a few periods
                    # later. A dip that resumes within this load's own hold
                    # window should never register as a real OFF at all:
                    # committing to OFF and immediately re-arming ON a few
                    # minutes later defeats the entire point of the
                    # hysteresis guard below, and burns a real activation for
                    # nothing. Look ahead through the SAME plan already in
                    # hand this cycle (load_plan.power_kw/served_kw) for up
                    # to min_hysteresis_seconds -- if the load wants on again
                    # inside that window, treat this dip as noise (stay "raw
                    # on") rather than a genuine sustained off.
                    #
                    # Deliberately one-sided: only suppresses OFF when
                    # already commanded ON. Never accelerates an OFF->ON
                    # transition -- pre-empting "on" early would risk an
                    # activation the plan hasn't actually committed to yet.
                    if (
                        prev.commanded_state
                        and not raw_new_state
                        and period_hours_arr is not None
                    ):
                        period_series = (
                            load_plan.served_kw
                            if load_kind == "sheddable"
                            else load_plan.power_kw
                        )
                        lookahead_hours_needed = min_hysteresis_seconds / 3600.0
                        hours_elapsed = 0.0
                        for i in range(1, len(period_series)):
                            prior_hours = (
                                float(period_hours_arr[i - 1])
                                if i - 1 < len(period_hours_arr)
                                else 0.0
                            )
                            hours_elapsed += prior_hours
                            if hours_elapsed > lookahead_hours_needed:
                                break
                            if (
                                float(period_series[i])
                                > load_run_state.DEFAULT_ON_THRESHOLD_KW
                            ):
                                raw_new_state = True
                                break
                    new = load_run_state.decide_commanded_state(
                        prev,
                        raw_new_state=raw_new_state,
                        now=now,
                        min_hysteresis_seconds=min_hysteresis_seconds,
                    )
                    # nimbus issue #581: publish this cycle's own full plan
                    # series regardless of whether commanded_state itself
                    # changed -- see this function's own docstring.
                    # nimbus issue #873 (Mark Purcell, 2026-09-16: "Build it").
                    # The last_idle_temperature sampler and the heating-rate/idle-
                    # decay fitter used to sit INSIDE the `load_kind == "adequacy"`
                    # branch below, so a kind=thermal load never learned anything and
                    # ran forever on the generic fallback constants -- on the
                    # reference household, the one real hot-water system scheduled
                    # off 8.0 degC/kWh and 0.5 degC/h rather than its own measured
                    # tank. Invisible from outside until #940 (v0.94.335) began
                    # publishing thermal_heating_rate_origin, which reads `fallback`
                    # beside a null learned rate.
                    #
                    # Nothing here was ever adequacy-specific: done_entity,
                    # power_sensor, the temperature read, the settling check and the
                    # history fetch are all generic, and the weather entity comes off
                    # cfg. A parallel kind=thermal copy was ruled out on Mark's own
                    # reasoning -- the async context already exists here, and a
                    # second copy would join the exact drift class #357 already pays
                    # for.
                    #
                    # Deliberately a RELOCATION, not a loosening: every precondition
                    # is unchanged, including the temperature gate the fitter sits
                    # under without reading. Dropping that because it looks
                    # incidental would be a real behaviour change smuggled in as a
                    # refactor.
                    start_temperature = None
                    live_temperature = None
                    heating_rate = None
                    decay_rate = None
                    loss_coeff = None
                    weather_entity_id = None
                    done_entity = data.get(CONF_DEFERRABLE_DONE_ENTITY) or data.get(
                        CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY
                    )
                    # nimbus issue #768 (Mark Purcell): unset here meant thermal
                    # rate learning silently stayed on generic fallback
                    # constants -- auto-discover from the load's own device.
                    power_sensor = sw.resolve_controllable_load_power_sensor(data)
                    if (
                        done_entity
                        and power_sensor
                        and done_entity.split(".", 1)[0]
                        in done_condition.ATTRIBUTE_DONE_DOMAINS
                    ):
                        live_temperature = done_condition.read_current_temperature(
                            solver_shared.NATIVE.hass, done_entity
                        )
                        # nimbus issue #609 (Mark Purcell, real finding:
                        # current_temperature reads ~10-11 degC LOW while
                        # the compressor is actively running on the #534
                        # SG Ready bridge -- a device-side reporting
                        # artifact, not a real physical drop, confirmed
                        # by every idle reading before/after a run
                        # agreeing with itself while every in-run reading
                        # is depressed). A live reading is only trusted
                        # once the load is confirmed idle AND has been
                        # off for at least thermal_forecast.SETTLING_
                        # MINUTES -- otherwise the last known-good idle
                        # reading anchors the projection instead.
                        settled = not new.currently_on and (
                            new.off_since is None
                            or (now.timestamp() - new.off_since)
                            >= thermal_forecast.SETTLING_MINUTES * 60.0
                        )
                        if settled and live_temperature is not None:
                            new = replace(new, last_idle_temperature=live_temperature)
                        start_temperature = (
                            live_temperature
                            if settled
                            else (
                                new.last_idle_temperature
                                if new.last_idle_temperature is not None
                                else live_temperature
                            )
                        )
                        if start_temperature is not None:
                            heating_rate = new.thermal_heating_rate_c_per_kwh
                            decay_rate = new.thermal_idle_decay_c_per_hour
                            loss_coeff = new.thermal_loss_coeff_per_h
                            # nimbus issue #481: resolved once per load
                            # per cycle, used by both the (day-key-gated)
                            # ambient-history learning fetch below and
                            # the (every-cycle) ambient-forecast
                            # projection fetch further down.
                            weather_entity_id = (cfg or {}).get(
                                "solver_weather_forecast_sensor"
                            )
                            # Recorder history is a real DB query -- only
                            # relearn once per calendar day (#592's own
                            # "on each retrain" ask), not every solve.
                            # nimbus issue #618 (Mark Purcell, real finding
                            # the day the #609/#610/#611 learner redesign
                            # shipped): thermal_rates_learned_day_key was
                            # already stamped today by the OLD (#592-era)
                            # learner before this cycle's code even landed,
                            # so the day-key gate alone silently skipped
                            # relearning under the NEW idle-to-idle logic
                            # until tomorrow -- thermal_rates_source stayed
                            # at its "never learned" "" default and the
                            # stale 8 C/kWh figure carried forward despite
                            # today's recorder already having everything
                            # the new learner needs. A never-yet-labeled
                            # thermal_rates_source ("" -- #610's own
                            # contract is only ever "learned"/"fallback"
                            # after a real run) also forces a relearn, so
                            # any future learner-logic change self-heals
                            # on its very next solve instead of waiting up
                            # to a full day.
                            if (
                                new.thermal_rates_learned_day_key != day_key
                                or not new.thermal_rates_source
                            ):
                                history_end = now
                                history_start = now - timedelta(days=3)
                                thermal_history = await _async_fetch_thermal_history(
                                    done_entity,
                                    power_sensor,
                                    history_start,
                                    history_end,
                                )
                                # nimbus issue #481: real outdoor-
                                # temperature history over the SAME
                                # window, only when the household has a
                                # weather source configured -- graceful
                                # no-op (ambient_history stays empty,
                                # learn_thermal_rates() returns loss_
                                # coeff_per_h=None, thermal_loss_coeff_
                                # per_h below stays None) for any install
                                # that hasn't configured one.
                                ambient_history: list[tuple[datetime, float]] = []
                                if weather_entity_id:
                                    ambient_history = (
                                        await _async_fetch_ambient_history(
                                            weather_entity_id,
                                            history_start,
                                            history_end,
                                        )
                                    )
                                learned = thermal_forecast.learn_thermal_rates(
                                    thermal_history,
                                    on_threshold_kw=load_run_state.DEFAULT_ON_THRESHOLD_KW,
                                    ambient_history=ambient_history,
                                )
                                heating_rate = learned.heating_rate_c_per_kwh
                                decay_rate = learned.idle_decay_c_per_hour
                                loss_coeff = learned.loss_coeff_per_h
                                new = replace(
                                    new,
                                    thermal_heating_rate_c_per_kwh=heating_rate,
                                    thermal_idle_decay_c_per_hour=decay_rate,
                                    thermal_loss_coeff_per_h=loss_coeff,
                                    thermal_rates_learned_day_key=day_key,
                                    # nimbus issue #610: "the published
                                    # attributes do not say which [a
                                    # learned rate from a default]."
                                    thermal_rates_source=(
                                        "learned" if learned.is_learned else "fallback"
                                    ),
                                )
                    if load_kind == "sheddable" and period_hours_arr is not None:
                        nominal_kw = data.get(CONF_SHEDDABLE_NOMINAL_KW)
                        new = replace(
                            new,
                            plan_forecast=load_run_state.build_time_value_series(
                                grid_times, load_plan.served_kw
                            ),
                            plan_cost_forecast=_plan_cost_forecast(load_plan.served_kw),
                            plan_shadow_price_forecast=_plan_shadow_price_forecast(),
                            plan_nominal_kw=(
                                float(nominal_kw) if nominal_kw is not None else None
                            ),
                        )
                    elif load_kind == "adequacy" and period_hours_arr is not None:
                        earliest_hour = data.get(CONF_DEFERRABLE_EARLIEST_HOUR)
                        deadline_hour = data.get(CONF_DEFERRABLE_DEADLINE_HOUR)
                        earliest_period = (
                            sw._resolve_hour_to_period_index(
                                grid_times, now, float(earliest_hour), is_deadline=False
                            )
                            if earliest_hour is not None
                            else 0
                        )
                        deadline_period = (
                            sw._resolve_hour_to_period_index(
                                grid_times, now, float(deadline_hour), is_deadline=True
                            )
                            if deadline_hour is not None
                            else n_periods - 1
                        )
                        # nimbus issue #582's own same-day-in-progress fix,
                        # duplicated here rather than shared -- this function
                        # resolves its OWN copy of earliest/deadline_period
                        # (for display only, not for the actual LP window,
                        # which build_controllable_loads() resolves
                        # separately) and would otherwise report the wrong
                        # (tomorrow) earliest_period for the exact same real
                        # case #582 fixed for the LP's own window: a same-day
                        # window already open right now. See that function's
                        # own comment for the full reasoning. KNOWN DRIFT
                        # RISK, flagged rather than silently left inconsistent:
                        # a future change to #582's own logic needs to be
                        # ported here too, or better, both call sites should
                        # be refactored onto one shared helper.
                        # nimbus issue #582, extracted to one helper by #485 --
                        # this logic previously existed as four identical copies.
                        earliest_period = sw._earliest_period_for_same_day_window(
                            now=now,
                            earliest_hour=earliest_hour,
                            deadline_hour=deadline_hour,
                            earliest_period=earliest_period,
                            deadline_period=deadline_period,
                        )
                        target_kwh = data.get(CONF_DEFERRABLE_TARGET_KWH)
                        delivered_kwh_cumulative = np.cumsum(
                            np.asarray(load_plan.power_kw, dtype=np.float64)
                            * np.asarray(period_hours_arr[: len(load_plan.power_kw)])
                        )
                        # nimbus issue #483: getattr, not direct attribute
                        # access -- this file's own bare-SimpleNamespace test
                        # fakes (_fake_load_plan(), same reasoning as the #774
                        # thermal_loads getattr elsewhere in this file) predate
                        # both of these fields.
                        marginal_cost = getattr(load_plan, "marginal_cost", 0.0)
                        profit_horizon = getattr(load_plan, "profit_horizon", None)
                        # nimbus issue #483, item 2: looked up by subentry_id
                        # from the dict main() computed once, up front, over
                        # every adequacy load in the same solve -- not a
                        # per-load LP-native value like marginal_cost/
                        # profit_horizon above (see compute_tariff_
                        # attributed_cost()'s own docstring for why it lives
                        # outside network.py). None when the caller didn't
                        # pass the dict at all (every existing test fixture
                        # predating this field) -- a real no-op, not a
                        # silent 0.0 masquerading as "genuinely computed and
                        # zero."
                        tariff_attributed_cost = (
                            tariff_attributed_cost_by_subentry.get(subentry_id)
                            if tariff_attributed_cost_by_subentry is not None
                            else None
                        )
                        new = replace(
                            new,
                            plan_forecast=load_run_state.build_time_value_series(
                                grid_times, load_plan.power_kw
                            ),
                            plan_cost_forecast=_plan_cost_forecast(load_plan.power_kw),
                            plan_shadow_price_forecast=_plan_shadow_price_forecast(),
                            plan_delivered_kwh_forecast=(
                                load_run_state.build_time_value_series(
                                    grid_times, delivered_kwh_cumulative
                                )
                            ),
                            plan_target_kwh=(
                                float(target_kwh) if target_kwh is not None else None
                            ),
                            plan_shortfall_kwh=float(load_plan.shortfall_kwh),
                            # nimbus issue #483: rounded to 4dp at this publish
                            # boundary, matching every other headline $ figure
                            # this project publishes (total_cost, cost_
                            # breakdown, cost_band -- see solver_writer.py's
                            # own total_cost fix, nimbus issue #756-golden-CI-
                            # flake) -- HiGHS's LP solve is not bit-for-bit
                            # deterministic run to run, and leaving this one
                            # unrounded would leak that same noise straight
                            # through. Internal math (network.py's own
                            # marginal_cost/profit_horizon computation) is
                            # untouched -- only the published value changes.
                            plan_marginal_cost=round(marginal_cost, 4),
                            plan_profit_horizon=(
                                round(profit_horizon, 4)
                                if profit_horizon is not None
                                else None
                            ),
                            plan_tariff_attributed_cost=(
                                round(tariff_attributed_cost, 4)
                                if tariff_attributed_cost is not None
                                else None
                            ),
                            plan_status_reason=load_run_state.compute_load_status_reason(
                                power_kw=load_plan.power_kw,
                                shadow_price=_raw_shadow_price_series(),
                                grid_times=grid_times,
                                earliest_period=earliest_period,
                                deadline_period=deadline_period,
                            ),
                            plan_earliest_period=earliest_period,
                            plan_deadline_period=deadline_period,
                        )
                        # nimbus issue #592 (Mark Purcell, part of #589 --
                        # "will the tank be at 60 by lunchtime?"): a
                        # water_heater/climate load with a real done_entity
                        # and power_sensor configured gets a projected
                        # temperature forecast, groundwork for the full #481
                        # thermal kind. See thermal_forecast.py's own module
                        # docstring for the full design and what's
                        # deliberately NOT part of this (model-based source
                        # marking, using the crossing to shorten the LP's
                        # own schedule ahead of time).
                        # nimbus issue #809: done_entity defaults to this same
                        # load's own device_entity -- see build_controllable_
                        # loads()'s own matching comment for the reasoning;
                        # duplicated here rather than shared, same accepted
                        # drift-risk tradeoff already flagged for this
                        # function's own duplicate of the #582 same-day fix.
                        # nimbus issue #873: the sampler and fitter moved above the
                        # load_kind branches; only the projection below is genuinely
                        # adequacy-specific, so it keeps its own gates.
                        if (
                            done_entity
                            and power_sensor
                            and done_entity.split(".", 1)[0]
                            in done_condition.ATTRIBUTE_DONE_DOMAINS
                            and start_temperature is not None
                        ):
                            # nimbus issue #611 (Mark Purcell: "the
                            # forecast is projected from plan_forecast,
                            # so it shows [cooling] ... while [real
                            # power] is actually going into the tank" --
                            # #595's own guard can hold commanded_state
                            # ON through a hold window a fresh solve's
                            # own plan_forecast[0] doesn't yet reflect).
                            # Only period 0 is overridden with the
                            # load's own configured max_power_kw when
                            # actually commanded on -- every later
                            # period still projects from the real plan.
                            max_power_kw = data.get(CONF_DEFERRABLE_MAX_POWER_KW)
                            override_power = (
                                float(max_power_kw)
                                if new.commanded_state and max_power_kw is not None
                                else None
                            )
                            # nimbus issue #640 (Mark Purcell, live
                            # verification of #610: with the heat pump
                            # idle in "eco" mode, `temperature` reads the
                            # 45 degC eco setpoint -- the floor the unit
                            # maintains BETWEEN runs, not the ceiling of
                            # a run -- which clamped a genuine 2 kWh/16
                            # degC reheat down to a flat line and made
                            # the tank look like it could never reach its
                            # own 60 degC done line). Clamp at the
                            # heater's real operating ceiling instead:
                            # "max_temp" first (the unit's own physical
                            # maximum, e.g. 65 here, still a real,
                            # never-invented attribute read off the
                            # entity, never hardcoded); "temperature" as
                            # a fallback for an entity that only
                            # publishes the current target and has no
                            # separate max_temp; and, when the entity
                            # exposes neither, the load's own configured
                            # done_when threshold (#610's own "at minimum
                            # the done_when threshold" fallback) so a
                            # done line the projection needs to actually
                            # reach is never clamped below itself.
                            ceiling_temperature = None
                            done_state_obj = solver_shared.NATIVE.hass.states.get(
                                done_entity
                            )
                            if done_state_obj is not None:
                                for _attr in ("max_temp", "temperature"):
                                    _raw = done_state_obj.attributes.get(_attr)
                                    if _raw is not None:
                                        try:
                                            ceiling_temperature = float(_raw)
                                        except (TypeError, ValueError):
                                            ceiling_temperature = None
                                        break
                            if ceiling_temperature is None:
                                done_when = data.get(CONF_DEFERRABLE_DONE_WHEN)
                                if done_when is not None:
                                    try:
                                        _, ceiling_temperature = (
                                            done_condition.parse_done_when(done_when)
                                        )
                                    except (ValueError, TypeError):
                                        ceiling_temperature = None
                            # nimbus issue #481: the forward ambient
                            # forecast for the PROJECTION step -- a
                            # separate, live weather.get_forecasts fetch
                            # from the recorder-history one just above
                            # (used only to LEARN loss_coeff, not to
                            # project it forward). Same weather_entity_id
                            # resolved above; graceful no-op (empty list)
                            # when unconfigured or the fetch fails, same
                            # posture as publish_weather_forecast_
                            # mirrors()'s own use of this helper.
                            ambient_forecast_points: list[dict[str, object]] = []
                            if weather_entity_id:
                                _hourly = sw._fetch_weather_hourly_forecast(
                                    weather_entity_id
                                )
                                if _hourly:
                                    ambient_forecast_points = [
                                        {
                                            "time": p["datetime"],
                                            "value": float(p["temperature"]),
                                        }
                                        for p in _hourly
                                        if isinstance(p, dict)
                                        and p.get("datetime") is not None
                                        and p.get("temperature") is not None
                                    ]
                            new = replace(
                                new,
                                temperature_forecast=thermal_forecast.project_temperature_forecast(
                                    new.plan_forecast or [],
                                    start_temperature=start_temperature,
                                    heating_rate_c_per_kwh=(
                                        heating_rate
                                        if heating_rate is not None
                                        else thermal_forecast.DEFAULT_HEATING_RATE_C_PER_KWH
                                    ),
                                    idle_decay_c_per_hour=(
                                        decay_rate
                                        if decay_rate is not None
                                        else thermal_forecast.DEFAULT_IDLE_DECAY_C_PER_HOUR
                                    ),
                                    on_threshold_kw=load_run_state.DEFAULT_ON_THRESHOLD_KW,
                                    ceiling_temperature=ceiling_temperature,
                                    override_first_period_power_kw=override_power,
                                    loss_coeff_per_h=loss_coeff,
                                    ambient_forecast=ambient_forecast_points,
                                ),
                            )
                            # nimbus issue #712/#713 (Mark Purcell, real
                            # live finding): the deferrable model's own
                            # kWh-target/deadline framing has no
                            # representation of the device's own
                            # PHYSICAL thermal floor at all -- confirmed
                            # live, two consecutive nights, the tank
                            # falling past its eco-mode floor and the
                            # compressor self-triggering hours before
                            # the next scheduled ON period. Not
                            # attempting a dispatch-changing fix here
                            # (#713's own text: worth checking whether
                            # pulling the earliest start forward is
                            # worth the price difference, a real,
                            # separate economic design question) --
                            # this is #712's own "at minimum" ask: a
                            # WARNING + a flag a household/future
                            # automation can act on. floor_temperature
                            # mirrors ceiling_temperature's own read
                            # order (min_temp first -- the unit's own
                            # real physical/eco floor, never invented;
                            # done_when's own threshold as a last
                            # resort for a device with no min_temp at
                            # all) so both bounds come from the exact
                            # same entity/config, never a second guess.
                            floor_temperature = None
                            if done_state_obj is not None:
                                _raw_floor = done_state_obj.attributes.get("min_temp")
                                if _raw_floor is not None:
                                    try:
                                        floor_temperature = float(_raw_floor)
                                    except (TypeError, ValueError):
                                        floor_temperature = None
                            if floor_temperature is None:
                                done_when = data.get(CONF_DEFERRABLE_DONE_WHEN)
                                if done_when is not None:
                                    try:
                                        _, floor_temperature = (
                                            done_condition.parse_done_when(done_when)
                                        )
                                    except (ValueError, TypeError):
                                        floor_temperature = None
                            floor_crossing = None
                            if (
                                floor_temperature is not None
                                and new.temperature_forecast
                            ):
                                floor_crossing = thermal_forecast.find_floor_crossing(
                                    new.temperature_forecast, floor_temperature
                                )
                            new = replace(
                                new,
                                floor_crossing_forecast_time=(
                                    str(floor_crossing["time"])
                                    if floor_crossing is not None
                                    else None
                                ),
                                floor_crossing_forecast_temperature=(
                                    float(floor_crossing["value"])  # type: ignore[arg-type]
                                    if floor_crossing is not None
                                    else None
                                ),
                            )
                            if floor_crossing is not None:
                                _floor_warn_key = (subentry_id, day_key)
                                if _floor_warn_key not in _FLOOR_CROSSING_WARNED:
                                    _FLOOR_CROSSING_WARNED.add(_floor_warn_key)
                                    solver_shared._LOGGER.warning(
                                        "Nimbus: controllable load '%s' is "
                                        "forecast to cross its own hardware "
                                        "floor (%.1f) at %s, before its own "
                                        "planned schedule reaches it -- the "
                                        "device may self-trigger outside "
                                        "the solved plan (nimbus issue "
                                        "#712/#713)",
                                        subentry_id,
                                        floor_temperature,
                                        floor_crossing["time"],
                                    )
                    elif load_kind == "thermal" and period_hours_arr is not None:
                        # nimbus issue #774: same publish shape as the
                        # adequacy branch above, but simpler -- no windowed/
                        # done-entity/floor-crossing machinery (out of scope
                        # for v1, see ThermalLoadConfig's own docstring).
                        earliest_hour = data.get(CONF_THERMAL_EARLIEST_HOUR)
                        deadline_hour = data.get(CONF_THERMAL_DEADLINE_HOUR)
                        earliest_period = (
                            sw._resolve_hour_to_period_index(
                                grid_times, now, float(earliest_hour), is_deadline=False
                            )
                            if earliest_hour is not None
                            else 0
                        )
                        deadline_period = (
                            sw._resolve_hour_to_period_index(
                                grid_times, now, float(deadline_hour), is_deadline=True
                            )
                            if deadline_hour is not None
                            else n_periods - 1
                        )
                        # nimbus issue #582's own same-day-in-progress fix,
                        # duplicated here too -- same accepted drift-risk
                        # tradeoff already flagged on the adequacy branch
                        # above and in build_controllable_loads() itself.
                        # nimbus issue #582, extracted to one helper by #485 --
                        # this logic previously existed as four identical copies.
                        earliest_period = sw._earliest_period_for_same_day_window(
                            now=now,
                            earliest_hour=earliest_hour,
                            deadline_hour=deadline_hour,
                            earliest_period=earliest_period,
                            deadline_period=deadline_period,
                        )
                        new = replace(
                            new,
                            plan_forecast=load_run_state.build_time_value_series(
                                grid_times, load_plan.power_kw
                            ),
                            plan_cost_forecast=_plan_cost_forecast(load_plan.power_kw),
                            plan_shadow_price_forecast=_plan_shadow_price_forecast(),
                            # nimbus issue #774: the LP's own real, solved
                            # temperature trajectory -- for kind=thermal this
                            # REPLACES the display-only thermal_forecast.py
                            # projection a kind=deferrable load still uses
                            # above (re-deriving the same physics model the LP
                            # already solved with could only ever diverge from
                            # it, see ThermalLoadPlan's own docstring).
                            plan_temperature_forecast=load_run_state.build_time_value_series(
                                grid_times, load_plan.temperature_c
                            ),
                            # nimbus issue #940: the rates the LP was
                            # actually built with, echoed off the plan rather
                            # than re-resolved here. Until now a household
                            # could see only the LEARNED rates, which are
                            # None on every kind=thermal load (#873) while
                            # the LP scheduled real hot water on the 8.0/0.5
                            # module defaults.
                            thermal_effective_heating_rate_c_per_kwh=(
                                load_plan.heating_rate_c_per_kwh
                            ),
                            thermal_effective_idle_decay_c_per_hour=(
                                load_plan.idle_decay_c_per_hour
                            ),
                            thermal_heating_rate_origin=load_plan.heating_rate_origin,
                            thermal_idle_decay_origin=load_plan.idle_decay_origin,
                            # Not applicable to this kind -- explicitly reset
                            # rather than left stale, so a load migrated from
                            # kind=deferrable doesn't keep showing an old
                            # target/shortfall figure that no longer applies.
                            plan_target_kwh=None,
                            plan_shortfall_kwh=None,
                            # nimbus issue #483: AdequacyLoadPlan-only fields
                            # (a kind=thermal load has no equivalent computed
                            # yet -- ThermalLoadPlan doesn't carry a lambda-
                            # based cost today, a real future extension, not
                            # attempted in this pass), same explicit-reset
                            # reasoning as plan_target_kwh/plan_shortfall_kwh
                            # just above.
                            plan_marginal_cost=None,
                            plan_profit_horizon=None,
                            plan_tariff_attributed_cost=None,
                            plan_earliest_period=earliest_period,
                            plan_deadline_period=deadline_period,
                        )
                    device_entity = data.get(CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY)
                    climate_on_hvac_mode = data.get(
                        CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE
                    )
                    if device_entity and new.commanded_state != prev.commanded_state:
                        if new.commanded_state:
                            max_activations_raw = data.get(
                                CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY
                            )
                            max_activations = (
                                int(max_activations_raw)
                                if max_activations_raw is not None
                                else None
                            )
                            if load_run_state.activation_allowed(
                                new,
                                max_activations_per_day=max_activations,
                                day_key=day_key,
                            ):
                                try:
                                    await dispatch_commanded_state(
                                        solver_shared.NATIVE.hass,
                                        device_entity,
                                        True,
                                        climate_on_hvac_mode=climate_on_hvac_mode,
                                    )
                                    new = load_run_state.record_activation(
                                        new, day_key=day_key
                                    )
                                    new = replace(new, last_dispatch_failed=False)
                                except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
                                    solver_shared._LOGGER.warning(
                                        "Nimbus: dispatch ON failed for "
                                        "controllable load '%s' (%s) -- will retry "
                                        "next cycle (nimbus issue #875)",
                                        subentry_id,
                                        device_entity,
                                        exc_info=True,
                                    )
                                    # nimbus issue #875: the command did not go
                                    # out. Recording that is what makes the retry
                                    # possible at all -- before this, the failed
                                    # attempt was persisted as commanded, and
                                    # edge-triggering meant the next cycle saw no
                                    # transition and never tried again. One
                                    # transient failure cost the load its window.
                                    new = replace(new, last_dispatch_failed=True)
                            else:
                                solver_shared._LOGGER.warning(
                                    "Nimbus: controllable load '%s' wants ON but "
                                    "is capped at %s activations/day -- not "
                                    "dispatched this cycle",
                                    subentry_id,
                                    max_activations,
                                )
                        else:
                            try:
                                await dispatch_commanded_state(
                                    solver_shared.NATIVE.hass, device_entity, False
                                )
                                new = replace(new, last_dispatch_failed=False)
                            except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
                                solver_shared._LOGGER.warning(
                                    "Nimbus: dispatch OFF failed for "
                                    "controllable load '%s' (%s) -- will retry "
                                    "next cycle (nimbus issue #875)",
                                    subentry_id,
                                    device_entity,
                                    exc_info=True,
                                )
                                new = replace(new, last_dispatch_failed=True)
                    elif device_entity and load_run_state.reaffirm_allowed(
                        new,
                        now_ts=now.timestamp(),
                        reaffirm_after_seconds=_resolve_reaffirm_after_seconds(data),
                        day_key=day_key,
                    ):
                        # nimbus issue #875, household decision 2026-09-15: the
                        # device is not following a command already given (or the
                        # last send never went out). Re-send the SAME state --
                        # deliberately NOT via record_activation(), so this cannot
                        # consume one of #534's capped device-side activations. A
                        # reminder is not a new activation.
                        _why = (
                            "last dispatch failed"
                            if new.last_dispatch_failed
                            else "device has not followed the command"
                        )
                        try:
                            await dispatch_commanded_state(
                                solver_shared.NATIVE.hass,
                                device_entity,
                                new.commanded_state,
                                climate_on_hvac_mode=(
                                    climate_on_hvac_mode
                                    if new.commanded_state
                                    else None
                                ),
                            )
                            new = load_run_state.record_reaffirm(
                                new, now_ts=now.timestamp(), day_key=day_key
                            )
                            new = replace(new, last_dispatch_failed=False)
                            solver_shared._LOGGER.info(
                                "Nimbus: re-sent %s to controllable load '%s' (%s) "
                                "-- %s. Re-send %d of %d today; this does NOT "
                                "count against the activations/day cap.",
                                "ON" if new.commanded_state else "OFF",
                                subentry_id,
                                device_entity,
                                _why,
                                new.reaffirms_today,
                                load_run_state.DEFAULT_MAX_REAFFIRMS_PER_DAY,
                            )
                        except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
                            solver_shared._LOGGER.warning(
                                "Nimbus: re-send failed for controllable load '%s' (%s)",
                                subentry_id,
                                device_entity,
                                exc_info=True,
                            )
                            new = replace(new, last_dispatch_failed=True)
                    elif device_entity and load_run_state.reaffirm_allowed(
                        new,
                        now_ts=now.timestamp(),
                        reaffirm_after_seconds=_resolve_reaffirm_after_seconds(data),
                        # The whole point of this branch: ask the SAME question
                        # again with the cap lifted. True here while the capped
                        # call above returned False means the daily cap is the
                        # only thing standing between this load and a re-send.
                        max_reaffirms_per_day=None,
                        day_key=day_key,
                    ):
                        # nimbus issue #875, gap found by Mark Purcell's IV&V of
                        # PR #930: reaffirm_allowed() correctly returns False
                        # once the cap is spent, and then NOTHING happened --
                        # no else, no log. The sibling activation-cap branch a
                        # few lines above logs every time it blocks a dispatch;
                        # a spent reaffirm cap produced no signal at all, and
                        # the only trace left was command_divergence_seconds()
                        # quietly growing on a sensor attribute nobody is
                        # prompted to check. A device in a genuine argument
                        # with something else -- the exact scenario this cap
                        # exists to bound -- went quiet after 20 tries.
                        #
                        # Deliberately derived by re-asking reaffirm_allowed()
                        # with max_reaffirms_per_day=None rather than
                        # re-deriving the counter comparison here: the day-key
                        # rollover semantics live in load_run_state.py and a
                        # second copy of them in this file is precisely the
                        # drift #357 exists to catch. It also makes the branch
                        # exact -- the other three reasons that function
                        # returns False (re-sends disabled with 0, divergence
                        # below threshold, interval not yet elapsed) are all
                        # ordinary every-cycle states and must stay silent.
                        _cap_warn_key = (subentry_id, day_key)
                        if _cap_warn_key not in _REAFFIRM_CAP_WARNED:
                            _REAFFIRM_CAP_WARNED.add(_cap_warn_key)
                            solver_shared._LOGGER.warning(
                                "Nimbus: controllable load '%s' (%s) is still not "
                                "following its commanded state (%s), but the daily "
                                "re-send cap of %d is spent -- Nimbus will stop "
                                "re-sending to this load until tomorrow. Something "
                                "else may be writing to the device. Logged once "
                                "per load per day (nimbus issue #875).",
                                subentry_id,
                                device_entity,
                                "ON" if new.commanded_state else "OFF",
                                load_run_state.DEFAULT_MAX_REAFFIRMS_PER_DAY,
                            )
                    if new is not prev:
                        await store.async_write(subentry_id, new)
                except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
                    # Per-load, so the loop continues. Deliberately WARNING and
                    # deliberately naming the load: this is the level at which
                    # a household can act on it, and the outer handler cannot
                    # say WHICH load failed because by then the frame is gone.
                    solver_shared._LOGGER.warning(
                        "Nimbus: controllable load '%s' (%s) failed to be processed "
                        "this solve cycle and was NOT commanded -- every other load "
                        "is unaffected, and the next cycle retries this one from "
                        "scratch (nimbus issue #1019).",
                        subentry_id,
                        load_kind,
                        exc_info=True,
                    )
                    continue

        import asyncio as _asyncio

        future = _asyncio.run_coroutine_threadsafe(
            _update_all(), solver_shared.NATIVE.hass.loop
        )
        future.result(timeout=10)
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        # nimbus issue #1019. This handler must stay -- it is the last
        # thing between a controllable-load failure and the solve cycle
        # it runs inside, and dispatch must never take the solve down.
        #
        # But it was DEBUG, and that made it invisible. Every controllable
        # load is commanded through one coroutine with no per-load
        # isolation, so ANY raise -- an unavailable entity, a sensor
        # returning an unexpected type, a malformed subentry, a recorder
        # hiccup mid-fetch -- abandons the cycle for EVERY REMAINING
        # LOAD, silently. Loads already dispatched stay dispatched; the
        # rest are simply not commanded, with nothing said about it.
        #
        # At a 5-minute cadence the next cycle usually succeeds, so the
        # symptom is intermittent missed dispatch rather than an outage.
        # That is exactly the shape of #757 (ten investigations) and of
        # #315, where the ABSENCE of a warning was the evidence nobody
        # thought to check. A guard that cannot report its own failure is
        # indistinguishable from one that never runs.
        #
        # WARNING, not DEBUG, and it says what was lost. Per-load
        # isolation -- so one bad load costs one load rather than the
        # remainder of the cycle -- is the other half of #1019 and needs
        # a re-indent of the whole loop body, so it is deliberately not
        # bundled here.
        solver_shared._LOGGER.warning(
            "Nimbus: commanded-state guard failed this solve cycle -- any "
            "controllable load not yet processed was NOT commanded (see "
            "nimbus issue #1019; loads already dispatched are unaffected, "
            "and the next cycle retries from scratch)",
            exc_info=True,
        )
