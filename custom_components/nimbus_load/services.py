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
from datetime import datetime

import voluptuous as vol
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

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

# nimbus issue #316 (Mark Purcell): the built-in scoring path only ever
# scores "yesterday", from a once-a-day scheduled tick that has no
# operator-facing knob. When the scorer silently freezes (issue #312),
# the IV&V feedback loop is "wait for midnight, hope it recovers,
# otherwise wait another 24 h." Splitting the scoring engine from the
# scheduling policy lets any caller -- Developer Tools UI, a diagnostic
# automation, a fixture-based regression test -- score any real
# historical window on demand, without touching the daily scorer's
# own semantics or timing.
SERVICE_COMPUTE_QUALITY_REPORT = "compute_quality_report"

# unique_id is built as f"{subentry_id}{suffix}" -- see __init__.py's own
# _async_rename_stale_forecast_entities(), which constructs it the other
# direction. Kept as a plain tuple here rather than importing a shared
# constant from __init__.py, to avoid a circular import between the two
# modules for two string literals.
_FORECAST_UNIQUE_ID_SUFFIXES = ("_load_forecast", "_signal_forecast")

SERVICE_RETRAIN_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_ENTITY_ID): cv.entity_ids},
)


def _coerce_datetime(value: object) -> datetime:
    """Accept either a real datetime (from a services.yaml datetime
    selector, which HA already parses before dispatch) or an ISO
    format string (from a raw YAML service call), and return a
    timezone-aware datetime. Kept as a plain module-level function
    rather than cv.datetime so this schema builds cleanly under the
    tests/_ha_stubs.py minimal cv module too (which only exposes
    cv.entity_ids). Real HA's cv.datetime does the same thing and
    also normalises tz.

    nimbus issue #345 (Mark Purcell): this used to return a NAIVE
    datetime unmodified whenever `value` was already a `datetime`
    object OR an ISO string with no offset -- exactly what HA's own
    `datetime:` selector in services.yaml produces, and what a hand-
    written YAML service call produces too. Comparing that naive value
    against the timezone-AWARE `dt_util.now()` in
    async_handle_compute_quality_report() raised a bare `TypeError`
    outside that function's own try block, surfacing as an opaque
    service error instead of the intended ServiceValidationError --
    and had it not raised, a naive datetime would have mis-windowed
    the recorder query by the local UTC offset. Both a bare `datetime`
    input and a string input are now normalised: an already-aware value
    passes through `dt_util.as_utc()` unchanged in effect; a naive one
    is anchored to HA's own configured local timezone before conversion
    (the honest assumption for a value with no explicit offset, not UTC
    -- these values reach this schema from a HA `datetime:` selector or
    a household's own local-time YAML, never a UTC API payload).
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            raise vol.Invalid(f"could not parse {value!r} as an ISO 8601 datetime")
    else:
        raise vol.Invalid(
            f"expected a datetime or an ISO 8601 string, got {type(value).__name__}"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_utc(parsed)


SERVICE_COMPUTE_QUALITY_REPORT_SCHEMA = vol.Schema(
    {
        vol.Required("start"): _coerce_datetime,
        vol.Required("end"): _coerce_datetime,
        vol.Optional("allow_partial", default=True): bool,
    }
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
    #
    # nimbus issue #365 (Mark Purcell): return_exceptions=True so one
    # coordinator's own failure can't abort every OTHER coordinator's
    # already-in-flight retrain, leaving the service call itself raise a
    # single opaque error with no indication which load(s) actually
    # failed. Coordinator.py's own _async_retrain() now catches and logs
    # everything internally (see that method's own comment) rather than
    # ever propagating, so this is belt-and-suspenders today -- kept as
    # a real guarantee against the shape of bug regardless of whether
    # that stays true.
    if coordinators:
        await asyncio.gather(
            *(c._async_retrain() for c in coordinators), return_exceptions=True
        )


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


async def _async_handle_compute_quality_report(
    hass: HomeAssistant, call: ServiceCall
) -> dict:
    """Score an arbitrary [start, end] window using the same scoring
    engine main() uses for the daily "yesterday" scorer, and return
    the score dict as the service response.

    The scoring engine (solver_writer._compute_report_for_window) is
    genuinely blocking -- it reads real recorder history via urllib
    and runs a HiGHS MILP oracle solve. Runs in a worker via
    hass.async_add_executor_job(), same pattern solver_runtime.
    async_run_solve() already uses.

    Raises ServiceValidationError on shape errors (end <= start,
    window in the future, etc.), HomeAssistantError on genuine
    scoring failures (either power sensor missing, oracle infeasible,
    real history not available yet).
    """
    from homeassistant.exceptions import HomeAssistantError

    from . import solver_writer

    start: datetime = call.data["start"]
    end: datetime = call.data["end"]
    allow_partial: bool = call.data.get("allow_partial", True)

    if end <= start:
        raise ServiceValidationError(
            f"nimbus_load.compute_quality_report: end ({end.isoformat()}) "
            f"must be strictly after start ({start.isoformat()})"
        )
    now = dt_util.now()
    if end > now:
        raise ServiceValidationError(
            f"nimbus_load.compute_quality_report: end ({end.isoformat()}) "
            f"must not be in the future (now is {now.isoformat()})"
        )

    def _blocking() -> dict | None:
        cfg = solver_writer.fetch_solver_config()
        return solver_writer._compute_report_for_window(
            cfg, start, end, allow_partial=allow_partial
        )

    try:
        result = await hass.async_add_executor_job(_blocking)
    except Exception as e:
        raise HomeAssistantError(
            f"nimbus_load.compute_quality_report: scoring failed for window "
            f"[{start.isoformat()}, {end.isoformat()}]: {e}"
        ) from e

    if result is None:
        raise HomeAssistantError(
            f"nimbus_load.compute_quality_report: cannot score window "
            f"[{start.isoformat()}, {end.isoformat()}] -- check that the "
            f"solar/battery/load power sensors are configured, real recorder "
            f"history exists for this window, and (with allow_partial=False) "
            f"the window is at least 24 hours long"
        )

    window_hours = round((end - start).total_seconds() / 3600.0, 4)
    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_hours": window_hours,
        **result,
    }


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

    if not hass.services.has_service(DOMAIN, SERVICE_COMPUTE_QUALITY_REPORT):

        async def _handle_compute_quality_report(call: ServiceCall):
            return await _async_handle_compute_quality_report(hass, call)

        hass.services.async_register(
            DOMAIN,
            SERVICE_COMPUTE_QUALITY_REPORT,
            _handle_compute_quality_report,
            schema=SERVICE_COMPUTE_QUALITY_REPORT_SCHEMA,
            supports_response=True,
        )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Removes all three Nimbus services -- the counterpart to
    `async_register_services()` above, called from `__init__.py`'s own
    `async_unload_entry()` on a successful unload.

    nimbus issue #365 (Mark Purcell, codebase review), item 1: before
    this existed, removing the (only, `single_config_entry: true`) hub
    left `nimbus_load.solve_now`/`retrain`/`compute_quality_report`
    registered and callable forever, with `solver_runtime.async_run_
    solve()` still bound via a stale `set_native_hass()` call from the
    now-removed entry -- a solve with no entities, hitting `ha_post_
    state()`'s raw-state fallback. Safe to call unconditionally on every
    unload (including a plain reload, not just a genuine removal): this
    integration only ever has at most one entry, so there is never a
    surviving sibling entry that still needs these services, and
    `async_register_services()`'s own `has_service()` guard makes the
    brief unregister-then-immediately-re-register within a reload a
    complete no-op from a caller's point of view.
    """
    for service in (
        SERVICE_RETRAIN,
        SERVICE_SOLVE_NOW,
        SERVICE_COMPUTE_QUALITY_REPORT,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
