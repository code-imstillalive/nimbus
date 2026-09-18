"""Diagnostics platform for Nimbus.

Lets a household attach a real, structured dump to a bug report (Settings ->
Devices & Services -> Nimbus -> the three-dot menu -> Download diagnostics)
instead of copy-pasting entity states by hand -- this is exactly how several
real Nimbus bugs this project has fixed were actually found and reported
(e.g. the 2026-08-15 stale-persisted-model crash, the 2026-08-22 wizard-skip
gap Mark Purcell hit on his own fresh install).

2026-08-24, direct household + Mark Purcell instruction, after a full
session of real investigation repeatedly blocked on "please paste
sensor.X's attributes" round trips: "diagnostics must have everything in
it incl pre-set values" / "get more data into the diagnostic file so we
can actually understand the reason its making decisions rather than just
speculation without any data to backup." This file used to deliberately
EXCLUDE the Solver's own config values (capacity, max charge/discharge,
costs, risk_aversion -- none of it lived in entry.options at all, only on
live number.nimbus_solver_* entities, so it was invisible here) and every
forecast array (explicitly reasoned as "already visible on the entity
itself, and would bloat this dump"). Both exclusions are gone: this is a
downloaded debug file, not a live HA entity attribute subject to the
16384-byte recorder limit -- there's no real reason to hold data back
from it. A live entity's own forecast also keeps moving (every solve
cycle), so "already visible on the entity" was never actually true for a
diagnostics dump investigated any time after it was generated -- this
file is a genuine snapshot precisely because the entity isn't.

TO_REDACT is deliberately empty, not omitted -- Nimbus has no external
service credentials, API keys, or tokens anywhere in its config (confirmed
throughout quality_scale.yaml's own reauthentication-flow/inject-websession
exemptions: it reads local HA sensor states only). Entity IDs are left
unredacted on purpose, matching the standard HA convention for this class
of integration -- they're needed for real debugging and aren't secrets.
Kept as an explicit empty tuple (not skipping async_redact_data entirely)
so a future config field that IS sensitive flows through the redaction
path automatically rather than needing this file retrofitted.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import load_run_state
from .const import (
    CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
    CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
    CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_BATTERY_PARTICIPANT,
    SUBENTRY_TYPE_CONTROLLABLE_LOAD,
)
from .coordinator import NimbusConfigEntry

TO_REDACT: tuple[str, ...] = ()


_SOLVER_ENTITY_ID = "sensor.nimbus_solver_battery_forecast"
_HOUSEHOLD_LOAD_ENTITY_ID = "sensor.nimbus_household_load_total_forecast"
_SOLVER_CONFIG_ENTITY_ID = "sensor.nimbus_solver_config"
# nimbus issue #496: the flex family and the offer curve. The two
# parents are read by fixed id (they are hub-level singletons, like the
# three above); the flex SIGNAL children are not -- see
# _flex_diagnostics() for why those go through the entity registry.
_FLEX_REPORT_ENTITY_ID = "sensor.nimbus_flex_report"
_OFFER_CURVE_ENTITY_ID = "sensor.nimbus_offer_curve"


async def _controllable_load_diagnostics(
    hass: HomeAssistant, entry: NimbusConfigEntry, subentry_id: str
) -> dict[str, Any]:
    """nimbus issue #623 (Mark Purcell, real finding: reconfiguring the
    Hot Water Heat Pump had to reconstruct its current values from notes
    and entity attributes, because a controllable_load subentry has no
    forecast coordinator and was therefore entirely absent from this
    dump). The full persisted LoadRunState -- commanded state, hold,
    activations, delivered today, thermal rates, and the last-published
    plan_forecast -- IS this load's own durable, inspectable state, the
    same store NimbusControllableLoadStateSensor's own attributes read
    from (sensor.py). Reads the SAME Store apply_commanded_state_guard()
    writes to. A load that's never been solved yet (subentry just
    created) reads back LoadRunStateStore's own honest all-defaults
    fresh state, not a crash."""
    store = load_run_state.LoadRunStateStore(
        store=Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_load_run_state")
    )
    state = await store.async_read(subentry_id)
    return state.to_dict()


def _battery_participant_diagnostics(
    hass: HomeAssistant, data: dict[str, Any]
) -> dict[str, Any]:
    """nimbus issue #623: a battery_participant subentry has no
    persisted store the way a controllable_load does (build_extra_
    batteries() in solver_writer.py reads its soc_sensor/available_
    entity fresh from hass.states every solve, nothing durable) -- so
    "the last resolved availability/SoC the solve used" means reading
    those same two live entities the same way, right here. Deliberately
    NOT a duplicate of build_extra_batteries()'s own full BatteryConfig
    resolution (capacity clamping, departure-deadline period resolution,
    shared-charger grouping) -- this is a diagnostic read-out of the raw
    inputs a support request needs to see, not a second copy of the LP
    config builder to keep in sync with the real one."""
    soc_sensor = data.get(CONF_BATTERY_PARTICIPANT_SOC_SENSOR)
    soc_state = hass.states.get(soc_sensor) if soc_sensor else None
    available_entity = data.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY)
    available_state = hass.states.get(available_entity) if available_entity else None
    charge_limit_entity = data.get(CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY)
    charge_limit_state = (
        hass.states.get(charge_limit_entity) if charge_limit_entity else None
    )
    return {
        "soc_sensor": soc_sensor,
        "soc_sensor_state": soc_state.state if soc_state else None,
        "available_entity": available_entity,
        "available_entity_state": available_state.state if available_state else None,
        # Same resolution build_extra_batteries() itself uses: no entity
        # configured -> always available; anything but a live 'on' ->
        # not available.
        "available": (
            True
            if not available_entity
            else available_state is not None and available_state.state == "on"
        ),
        "charge_limit_entity": charge_limit_entity,
        "charge_limit_entity_state": (
            charge_limit_state.state if charge_limit_state else None
        ),
    }


def _solver_config_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """The Solver's own FULL resolved config -- every field
    _SOLVER_ALL_KEYS covers (sensor.py's own NimbusSolverConfigSensor),
    battery capacity/min-max SoC/efficiency/costs/salvage/degradation
    cost/P2P bonus blocks/network fee schedule/risk_aversion, ALL of it,
    not a curated subset.

    Reads the bridge sensor's own live attributes directly rather than
    re-deriving anything -- that sensor's own _resolve() already handles
    "read the live number.nimbus_solver_* entity if this key is
    dashboard-adjustable, otherwise read entry.options" per field, which
    is exactly the logic that would otherwise have to be duplicated
    here. Genuinely unconfigured (Solver settings wizard never run) ->
    an honest {"configured": False}, not a crash -- same convention as
    _solver_diagnostics() below.
    """
    state = hass.states.get(_SOLVER_CONFIG_ENTITY_ID)
    if state is None:
        return {"configured": False}
    return {"configured": True, "native_value": state.state, **dict(state.attributes)}


def _solver_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """Health-at-a-glance for the Solver PLUS its full real plan (both
    the standalone cron/HAOS-add-on path and the native in-process
    runtime push the exact same two entities, so this works identically
    either way) -- added 2026-08-23 in direct response to Mark Purcell
    asking for exactly this to debug a real solver crash/flatline he
    hit on his own install (nimbus issue #63). The Solver has no
    in-memory Python object this file could reach into
    (solver_runtime.py's own async_run_solve() returns a bare
    True/False and keeps nothing else) -- its only durable, inspectable
    state IS these two live HA entities, so reading them directly is
    the correct source of truth, not a workaround.

    Includes each entity's own full `forecast` array as of 2026-08-24
    (previously excluded -- see this module's own top docstring for
    why that reasoning didn't hold up). Neither entity existing yet
    (Solver settings never configured, or the very first cycle hasn't
    run) resolves to `None`, not a crash.

    2026-08-25 (nimbus issue #116, Mark Purcell): this used to hand-pick
    a curated subset of solver_attrs by name. That allowlist genuinely
    stopped tracking solver_writer.py's own output -- `cost_breakdown`
    (v0.82 #149) and `load_forecast_source_used` (v0.83 #148) both landed
    correctly on the real live entity but stayed `null` here because
    nobody remembered to add their names to this list too, producing a
    real false-negative: a diagnostic reader would see `null` on a field
    the changelog says shipped and reasonably conclude the fix hadn't
    landed. Fixed by spreading the entity's ENTIRE real attribute dict in
    first, so any current or future attribute solver_writer.py publishes
    is automatically visible here with zero maintenance -- this closes
    the whole class of bug, not just these two fields. The two explicit
    keys below stay as deliberate overrides layered on top of the spread
    (not a replacement for it): they merge in the household-load
    entity's own copy when the solver entity itself is missing or
    doesn't have that attribute, which a blind spread of solver_attrs
    alone can't express.
    """
    solver_state = hass.states.get(_SOLVER_ENTITY_ID)
    load_state = hass.states.get(_HOUSEHOLD_LOAD_ENTITY_ID)
    if solver_state is None and load_state is None:
        return {"configured": False}

    solver_attrs = solver_state.attributes if solver_state else {}
    load_attrs = load_state.attributes if load_state else {}
    return {
        "configured": True,
        "entity_found": solver_state is not None,
        "state": solver_state.state if solver_state else None,
        **dict(solver_attrs),
        # Real, direct answer to "is my load forecast actually feeding
        # the solver, or silently falling back to something wrong" --
        # the exact question nimbus issue #66 was about. Explicit
        # overrides (not covered by the spread above): fall back to the
        # household-load entity's own copy when the solver entity is
        # missing or doesn't carry this attribute.
        "load_forecast_source_error": load_attrs.get("load_forecast_source_error")
        or solver_attrs.get("load_forecast_source_error"),
        "load_failed_entities": load_attrs.get("failed_load_entities")
        or solver_attrs.get("failed_load_entities"),
        # Full real plan, not a slice or a summary -- see module
        # docstring: this is a snapshot, the live entity keeps moving.
        "forecast": solver_attrs.get("forecast", []),
        "household_load_forecast": load_attrs.get("forecast", []),
    }


def _flex_diagnostics(hass: HomeAssistant, entry: NimbusConfigEntry) -> dict[str, Any]:
    """The flex family, for nimbus issue #496's diagnostics criterion.

    Two different shapes, because the two sensors are built differently
    and a single approach captures nothing for one of them:

    - **`sensor.nimbus_flex_report`** carries its whole payload as
      attributes (`offered_*`, `realised_*`, `envelope_curtailment_kwh`,
      `price_response_curve`), so its entire dict is spread in the same
      way `_solver_diagnostics()` does. Deliberately a spread and not an
      allowlist: nimbus issue #116 is exactly the bug where a curated
      list here stopped tracking `solver_writer.py`'s real output and two
      shipped fields read `null` in diagnostics while live on the entity,
      which reads as "the fix didn't land".
    - **`sensor.nimbus_flex_signals`** carries **no payload attributes at
      all** -- measured live, its parent holds only entity metadata, and
      every per-signal value lives on a flattened child. Spreading the
      parent would silently capture nothing, so the children are read
      individually.

    The children are resolved through the **entity registry by
    unique_id**, never by building `sensor.nimbus_flex_<suffix>` as a
    string. Same reasoning Mark Purcell gave on #768 for the controllable
    load's power sensor: a name-built id works on the install it was
    written on and breaks wherever HA has suffixed the entity_id after a
    collision -- which is precisely the situation on any install carrying
    duplicate Nimbus entities. The suffix list comes from
    `sensor_flattened.FLATTENED_ATTRS_FLEX` itself rather than being
    retyped here, so a new flex signal appears in diagnostics with no
    maintenance.
    """
    from homeassistant.helpers import entity_registry as er

    from . import sensor_flattened

    report_state = hass.states.get(_FLEX_REPORT_ENTITY_ID)
    registry = er.async_get(hass)

    signals: dict[str, Any] = {}
    for spec in sensor_flattened.FLATTENED_ATTRS_FLEX:
        unique_id = f"{entry.entry_id}_nimbus_flex_{spec.entity_id_suffix}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id is None:
            signals[spec.entity_id_suffix] = None
            continue
        state = hass.states.get(entity_id)
        signals[spec.entity_id_suffix] = {
            "entity_id": entity_id,
            "state": state.state if state is not None else None,
        }

    return {
        "report_entity_found": report_state is not None,
        "report_state": report_state.state if report_state is not None else None,
        # Full spread, see docstring -- never an allowlist.
        "report": dict(report_state.attributes) if report_state is not None else None,
        "signals": signals,
    }


def _offer_curve_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """The offer curve, for the same #496 criterion -- this is where the
    sweep timings and the ranging output actually live.

    `sweep_seconds` is already published on this entity, so the "sweep
    timings" half of that criterion needs no new computation, only
    surfacing. `import_curve`/`export_curve`/`price_limits` come along in
    the same spread, which is what makes a degenerate or empty curve
    visible in a dump rather than only on a dashboard.

    Absent when the offer curve is switched off (it is opt-in, #494), so
    a missing entity resolves to `enabled: False` rather than a crash or
    a silently empty block.
    """
    state = hass.states.get(_OFFER_CURVE_ENTITY_ID)
    if state is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "state": state.state,
        **dict(state.attributes),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NimbusConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a Nimbus hub config entry.

    nimbus issue #623 (Mark Purcell, real finding on his own six-
    subentry install): this used to iterate `entry.runtime_data` (the
    forecast coordinators) directly, so `controllable_load`, `battery_
    participant`, `power_source`, `pv_string`, and `battery_tower`
    subentries -- every one of which has no forecast coordinator --
    never appeared at all. Now iterates `entry.subentries.values()`
    directly (every real subentry, regardless of type), and attaches a
    `coordinator` block only where `runtime_data` actually has one for
    that id -- byte-identical output for the two coordinator-backed
    types (`load`/`power_signal`), a real one for every other type
    instead of total silence."""
    coordinators = entry.runtime_data

    subentries: list[dict[str, Any]] = []
    for subentry_id, subentry in entry.subentries.items():
        entry_diag: dict[str, Any] = {
            "subentry_id": subentry_id,
            "subentry_type": subentry.subentry_type,
            "title": subentry.title,
            "config": async_redact_data(dict(subentry.data), TO_REDACT),
        }
        coordinator = coordinators.get(subentry_id)
        if coordinator is not None:
            data = coordinator.data or {}
            # Full real forecast array, not just point-count/first/last
            # time (2026-08-24 -- see this module's own top docstring
            # for why the earlier "already visible on the entity, would
            # bloat this dump" exclusion didn't hold up). first/last
            # time kept alongside the full array as a cheap, still-
            # useful at-a-glance summary.
            forecast = data.get("forecast") or []
            entry_diag["coordinator"] = {
                "last_update_success": coordinator.last_update_success,
                "mode": data.get("mode"),
                "trained_at": data.get("trained_at"),
                "training_points": data.get("training_points"),
                "model_type": data.get("model_type"),
                "validation_mae": data.get("validation_mae"),
                "validation_mase": data.get("validation_mase"),
                "forecast_point_count": len(forecast),
                "forecast_first_time": forecast[0]["time"] if forecast else None,
                "forecast_last_time": forecast[-1]["time"] if forecast else None,
                "forecast": forecast,
            }
        elif subentry.subentry_type == SUBENTRY_TYPE_CONTROLLABLE_LOAD:
            entry_diag["load_run_state"] = await _controllable_load_diagnostics(
                hass, entry, subentry_id
            )
        elif subentry.subentry_type == SUBENTRY_TYPE_BATTERY_PARTICIPANT:
            entry_diag["live_resolution"] = _battery_participant_diagnostics(
                hass, subentry.data
            )
        subentries.append(entry_diag)

    return {
        "entry": {
            "title": entry.title,
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "subentries": subentries,
        "solver": _solver_diagnostics(hass),
        "solver_config": _solver_config_diagnostics(hass),
        # nimbus issue #496's diagnostics criterion, the half that is not
        # blocked. The other half ("the last emitted telemetry record")
        # waits on #495's emitter, which does not exist yet -- the
        # vendored schema/telemetry.schema.json is referenced only by its
        # own drift test, so there is no record to dump.
        "flex": _flex_diagnostics(hass, entry),
        "offer_curve": _offer_curve_diagnostics(hass),
    }
