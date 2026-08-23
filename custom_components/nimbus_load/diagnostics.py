"""Diagnostics platform for Nimbus.

Lets a household attach a real, structured dump to a bug report (Settings ->
Devices & Services -> Nimbus -> the three-dot menu -> Download diagnostics)
instead of copy-pasting entity states by hand -- this is exactly how several
real Nimbus bugs this project has fixed were actually found and reported
(e.g. the 2026-08-15 stale-persisted-model crash, the 2026-08-22 wizard-skip
gap Mark Purcell hit on his own fresh install).

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


def _solver_diagnostics(hass: HomeAssistant) -> dict[str, Any]:
    """Health-at-a-glance for the Solver (both the standalone cron/HAOS-
    add-on path and the native in-process runtime push the exact same
    two entities, so this works identically either way) -- added
    2026-08-23 in direct response to Mark Purcell asking for exactly
    this to debug a real solver crash/flatline he hit on his own
    install (nimbus issue #63). The Solver has no in-memory Python
    object this file could reach into (solver_runtime.py's own
    async_run_solve() returns a bare True/False and keeps nothing
    else) -- its only durable, inspectable state IS these two live HA
    entities, so reading them directly is the correct source of truth,
    not a workaround.

    Deliberately excludes each entity's own large `forecast` array
    (already visible on the entity itself, and would bloat this dump)
    -- pulls only the fields that answer "is the Solver healthy and
    why/why not," same philosophy as the coordinator section below.
    Neither entity existing yet (Solver settings never configured, or
    the very first cycle hasn't run) resolves to `None`, not a crash.
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
        "status": solver_attrs.get("status"),
        "generated_at": solver_attrs.get("generated_at"),
        "solve_seconds": solver_attrs.get("solve_seconds"),
        "n_periods": solver_attrs.get("n_periods"),
        "n_clamped_periods": solver_attrs.get("n_clamped_periods"),
        "horizon_hours": solver_attrs.get("horizon_hours"),
        "total_cost": solver_attrs.get("total_cost"),
        "binding_constraint_now": solver_attrs.get("binding_constraint_now"),
        # Real, direct answer to "is my load forecast actually feeding
        # the solver, or silently falling back to something wrong" --
        # the exact question nimbus issue #66 was about.
        "load_forecast_source_error": load_attrs.get("load_forecast_source_error")
        or solver_attrs.get("load_forecast_source_error"),
        "load_failed_entities": load_attrs.get("failed_load_entities")
        or solver_attrs.get("failed_load_entities"),
        "load_summed_18_now_kw": solver_attrs.get("load_summed_18_now_kw"),
        "load_whole_house_cross_check_now_kw": solver_attrs.get(
            "load_whole_house_cross_check_now_kw"
        ),
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
        # Forecast point COUNT, not the raw array -- the array is already
        # live on the entity itself (small, redundant to duplicate here),
        # and this file exists to show HEALTH at a glance, not replicate
        # the forecast attribute.
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
                    "validation_mae": data.get("validation_mae"),
                    "validation_mase": data.get("validation_mase"),
                    "forecast_point_count": len(forecast),
                    "forecast_first_time": forecast[0]["time"] if forecast else None,
                    "forecast_last_time": forecast[-1]["time"] if forecast else None,
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
    }
