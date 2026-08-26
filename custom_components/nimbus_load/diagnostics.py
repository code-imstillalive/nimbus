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

from .coordinator import NimbusConfigEntry

TO_REDACT: tuple[str, ...] = ()


_SOLVER_ENTITY_ID = "sensor.nimbus_solver_battery_forecast"
_HOUSEHOLD_LOAD_ENTITY_ID = "sensor.nimbus_household_load_total_forecast"
_SOLVER_CONFIG_ENTITY_ID = "sensor.nimbus_solver_config"


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


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NimbusConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a Nimbus hub config entry."""
    coordinators = entry.runtime_data

    subentries: list[dict[str, Any]] = []
    for subentry_id, coordinator in coordinators.items():
        subentry = coordinator.subentry
        data = coordinator.data or {}
        # Full real forecast array, not just point-count/first/last time
        # (2026-08-24 -- see this module's own top docstring for why the
        # earlier "already visible on the entity, would bloat this dump"
        # exclusion didn't hold up). first/last time kept alongside the
        # full array as a cheap, still-useful at-a-glance summary.
        forecast = data.get("forecast") or []
        subentries.append(
            {
                "subentry_id": subentry_id,
                "subentry_type": subentry.subentry_type,
                "title": subentry.title,
                "config": async_redact_data(dict(subentry.data), TO_REDACT),
                "coordinator": {
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
                },
            }
        )

    return {
        "entry": {
            "title": entry.title,
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "subentries": subentries,
        "solver": _solver_diagnostics(hass),
        "solver_config": _solver_config_diagnostics(hass),
    }
