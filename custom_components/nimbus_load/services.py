"""nimbus_load.retrain -- force an immediate retrain of one or more load/
power-signal coordinators, without waiting for the daily scheduled retrain
(coordinator.py's own async_track_time_change(hour=CONF_RETRAIN_HOUR_LOCAL,
...) wiring).

Nimbus issue #195 (Mark Purcell): every PR that changes anything upstream
of the trained model -- feature engineering, damping, seasonal-blend
construction, the candidate-model set, the validation split -- is only
actually live on an install after that install's own next scheduled
retrain hour. Verifying a fix landed correctly (or wanting to fold in the
latest training window without waiting) previously meant either waiting
up to 24h, or deleting the persisted .pkl and restarting HA to force the
"nothing on disk yet" bootstrap path -- invasive, and easy to get wrong.

Registered once, hub-level, from __init__.py's async_setup_entry (guarded
by hass.services.has_service() so a second hub setup/reload doesn't try
to register it twice) -- matching this file's own established pattern for
other logically-hub-wide things done from async_setup_entry (see
frontend.async_register_frontend / health.install_log_buffer_handler()
in __init__.py, both idempotent for the same reason).
"""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from . import solver_runtime
from .const import DOMAIN
from .coordinator import NimbusCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_RETRAIN = "retrain"

# nimbus issue #232 (Mark Purcell): "Simplest, don't use cron, use a
# service call to run optimisation." Direct response to the whole class
# of cron-phase-alignment problems #244/#247/#251 were built to
# mitigate -- rather than guessing when a real settlement tick is likely
# to have landed and waiting a bounded amount, an automation that
# genuinely watches the real price sensor's own state change can call
# this the instant a tick actually arrives, with zero guessing at all.
# Doesn't replace the periodic timer (a household still wants a solve
# even on a period with no price change, e.g. a real SoC/load update) --
# this is purely additive, an on-demand trigger alongside it.
SERVICE_SOLVE_NOW = "solve_now"

# unique_id is built as f"{subentry_id}{suffix}" -- see __init__.py's own
# _async_rename_stale_forecast_entities(), which constructs it the other
# direction. Kept as a plain tuple here rather than importing a shared
# constant from __init__.py, to avoid a circular import between the two
# modules for two string literals.
_FORECAST_UNIQUE_ID_SUFFIXES = ("_load_forecast", "_signal_forecast")

SERVICE_RETRAIN_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_ENTITY_ID): cv.entity_ids},
)


def _all_coordinators(hass: HomeAssistant) -> dict[str, NimbusCoordinator]:
    """Every Nimbus coordinator across every hub config entry, keyed by
    subentry_id. In practice this integration is set up as a single hub,
    but nothing here assumes that -- merging across every entry is no
    more code and doesn't silently break if that ever changes.
    """
    merged: dict[str, NimbusCoordinator] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        merged.update(getattr(entry, "runtime_data", None) or {})
    return merged


def _coordinator_for_entity_id(
    hass: HomeAssistant,
    entity_id: str,
    all_coordinators: dict[str, NimbusCoordinator],
) -> NimbusCoordinator | None:
    """Resolve any entity owned by a Nimbus subentry back to its
    coordinator, via the entity registry's own unique_id -- the same
    subentry_id-derived shape __init__.py's rename helper already relies
    on, just read in the opposite direction.
    """
    registry = er.async_get(hass)
    registry_entry = registry.async_get(entity_id)
    if registry_entry is None or registry_entry.unique_id is None:
        return None
    for suffix in _FORECAST_UNIQUE_ID_SUFFIXES:
        if registry_entry.unique_id.endswith(suffix):
            subentry_id = registry_entry.unique_id[: -len(suffix)]
            return all_coordinators.get(subentry_id)
    return None


async def _async_handle_retrain(hass: HomeAssistant, call: ServiceCall) -> None:
    all_coordinators = _all_coordinators(hass)
    requested_entity_ids: list[str] | None = call.data.get(ATTR_ENTITY_ID)

    if not requested_entity_ids:
        targets = list(all_coordinators.values())
        if not targets:
            _LOGGER.warning(
                "Nimbus: retrain service called with no entity_id and no "
                "coordinators configured yet -- nothing to do"
            )
        return await _retrain_all(targets)

    targets = []
    unresolved: list[str] = []
    for entity_id in requested_entity_ids:
        coordinator = _coordinator_for_entity_id(hass, entity_id, all_coordinators)
        if coordinator is None:
            unresolved.append(entity_id)
        else:
            targets.append(coordinator)

    if unresolved:
        raise ServiceValidationError(
            f"Not a Nimbus load/power-signal entity, or not found: "
            f"{', '.join(unresolved)}"
        )

    await _retrain_all(targets)


async def _retrain_all(coordinators: list[NimbusCoordinator]) -> None:
    # gather, not a plain loop -- retrains are independent per coordinator
    # (each one already self-guards re-entrancy via its own _retraining
    # flag) and this is a synchronous-triggered-by-a-user service call,
    # not the daily scheduler -- no reason to make someone retraining 18
    # loads wait for them one at a time.
    if coordinators:
        await asyncio.gather(*(c._async_retrain() for c in coordinators))


async def _async_handle_solve_now(hass: HomeAssistant, call: ServiceCall) -> None:
    """Runs exactly one solve cycle, right now, reusing the identical
    `solver_runtime.async_run_solve()` call the periodic timer's own
    `_periodic_solve()` callback makes in __init__.py -- not a separate
    code path, so this can never drift from what the scheduled solve
    actually does. `async_run_solve()` already has its own internal
    concurrency guard (see its own docstring), so a service call landing
    while a periodic solve is mid-flight is a safe no-op, not a race.
    """
    ok = await solver_runtime.async_run_solve(hass)
    if not ok:
        _LOGGER.warning(
            "Nimbus: solve_now service call did not produce a successful "
            "solve -- check sensor.nimbus_solver_battery_forecast's own "
            "status attribute for the real reason"
        )


def async_register_services(hass: HomeAssistant) -> None:
    """Idempotent -- safe to call from every async_setup_entry run (hub
    add, edit, reload). HA's own service registry is domain-scoped, not
    entry-scoped, so a second registration for the same (domain, service)
    would otherwise raise on a hub reload.
    """
    if not hass.services.has_service(DOMAIN, SERVICE_RETRAIN):

        async def _handle_retrain(call: ServiceCall) -> None:
            await _async_handle_retrain(hass, call)

        hass.services.async_register(
            DOMAIN, SERVICE_RETRAIN, _handle_retrain, schema=SERVICE_RETRAIN_SCHEMA
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SOLVE_NOW):

        async def _handle_solve_now(call: ServiceCall) -> None:
            await _async_handle_solve_now(hass, call)

        hass.services.async_register(DOMAIN, SERVICE_SOLVE_NOW, _handle_solve_now)
