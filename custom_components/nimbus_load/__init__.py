"""The Nimbus integration -- a self-retraining ML forecaster.

One hub config entry, any number of "load" or "power_signal" subentries
(see config_flow.py / flows/load_subentry.py / flows/signal_subentry.py)
-- each one gets its own NimbusCoordinator, its own persisted model, and
its own forecast sensor + device, so adding one (there can be many --
built for a real 18-circuit-breaker household) never means a full new
integration setup, and each one's health is independently visible in the
device registry. Both subentry types share the exact same coordinator/
forecasting engine -- the only real difference is which config fields
each one's own "+ Add" form asks for (a load's optional schedule/
expected-load fields don't apply to a Battery/Solar/Grid power signal).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_utc_time_change,
)
from homeassistant.loader import async_get_integration

from . import frontend, health, services, solver_runtime
from .const import (
    CONF_LOAD_SENSOR,
    CONF_SOLVE_ON_PRICE_CHANGE,
    CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
    DEFAULT_SOLVE_ON_PRICE_CHANGE,
    DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    DOMAIN,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)
from .coordinator import NimbusConfigEntry, NimbusCoordinator
from .sensor import object_id_from_source

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.NUMBER, Platform.SWITCH]

# Nimbus issue #244 (Mark Purcell, 2026-08-27): a plain 1-minute
# `async_track_time_interval` has no phase relationship to the NEM 5-minute
# settlement boundary (:00/:05/:10/...), so its phase drifts freely with
# whatever second HA happened to start the integration on. Live-measured
# evidence (24h of `sensor.amber_express_amber_feed_in_price.last_changed`,
# 273 ticks): the settled AEMO tick lands in a tight [15s, 30s) window past
# each boundary 89% of the time (median 20.4s). A cron with no relationship
# to that window regularly solves just BEFORE the tick arrives, then waits
# up to another full interval to pick it up -- issue #244's own measured
# median expected loss was ~30s of stale-price dispatch per 5-minute block.
#
# Fix: phase-lock to the boundary instead of running on a free-running
# interval -- solve at :00:30, :05:30, :10:30, ... (30s past every NEM
# boundary, comfortably past the p90 tick-arrival window above). This is
# issue #244's own "Option A": fewer solves overall (12/hour vs 60/hour)
# AND a solve that's (almost) always looking at the current block's real
# settled price rather than last block's stale one.
#
# solver_runtime.async_run_solve()'s own PID-file overlap guard makes this
# safe even if a cycle occasionally runs long -- unchanged by this fix.
_SOLVER_CRON_MINUTES = list(range(0, 60, 5))
_SOLVER_CRON_SECOND = 30

_FORECASTABLE_SUBENTRY_TYPES = (SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL)

_LOGGER = logging.getLogger(__name__)

# Module-level, NOT hass.data[DOMAIN] -- this project deliberately moved off
# that pattern to entry.runtime_data for Quality Scale Bronze (see
# coordinator.py's own comment next to NimbusConfigEntry). Same idempotent-
# registration technique solver_writer.py's own _ENTITY_UPDATE_HANDLERS
# already uses for an identical class of problem: keyed by entry_id, holds
# the unsub callable for _periodic_solve's own async_track_utc_time_change
# registration below, so a second async_setup_entry() call for the SAME
# entry_id can cancel the first one's timer before registering its own.
#
# Nimbus issue #211 (live devhub recurrence, 2026-08-27): sensor.nimbus_
# solver_battery_forecast / sensor.nimbus_household_load_total_forecast
# writing at ~2x/minute, a few seconds apart, every single minute --
# CONFIRMED via a live `ha core logs -f` capture to be two genuine, back-
# to-back solves (different computed values each time), not a single
# solve double-writing (solver_writer.py has exactly one ha_post_state()
# call site per entity). Two full solves a minute apart is exactly what
# TWO independent, live async_track_time_interval registrations for the
# SAME config entry would produce -- and solves finish in ~1-2s (per
# issue #85's own captured diagnostic dump), well under the then-1-minute
# interval (since superseded by issue #244's phase-locked cron above),
# so the PID-lock overlap guard in solver_writer.py's acquire_lock()
# never even sees them as concurrent; each one just runs to completion
# and pushes its own real result a few seconds after the other.
#
# Same root mechanism #210 already fixed for retrain (this project's own
# test_coordinator_setup_does_not_block_on_retrain.py docstring: HA
# abandoning/retrying a slow async_setup_entry() while the original
# attempt's own coroutine keeps running in the background on an executor
# job, eventually finishing and re-registering everything a second time)
# -- just a different slow step tripping the same abandon-and-retry path,
# since #210 only backgrounded the retrain call specifically. The
# abandoned attempt's own hass.config_entries.async_forward_entry_setups()
# call is a silent no-op the second time (platforms already forwarded),
# which is why this doesn't reproduce the loud "does not generate unique
# IDs" error #210 fixed -- but nothing stopped it from reaching the
# _periodic_solve registration below and creating a second, independent,
# permanently-live timer. Explains why a prior `reload_config_entry`
# didn't fix this live: if whatever makes setup slow is a standing
# condition (not a one-off timing fluke), the reload's own fresh
# async_setup_entry() call can retrigger the identical race immediately.
_solver_timer_unsub: dict[str, Callable[[], None]] = {}

# Same idempotent-unsub pattern as _solver_timer_unsub above -- both to
# handle a hub reload re-entering async_setup_entry with a listener
# already registered for the same entry_id (see the CONF_SOLVE_ON_PRICE_
# CHANGE registration block below and _solver_timer_unsub's own comment
# above for the full "why entry.async_on_unload isn't enough on its own"
# reasoning), and to make the on-load state cleanly inspectable from
# tests. Value is None when the toggle is off for that entry.
_price_watcher_unsub: dict[str, Callable[[], None] | None] = {}

# Set of price-sensor entity IDs we're already listening on for each
# entry_id -- so a hub reload that hasn't changed the configured price
# sensors is a no-op re-register, rather than re-registering identical
# listeners on every reload. See the CONF_SOLVE_ON_PRICE_CHANGE block
# below.
_price_watcher_entities: dict[str, tuple[str, ...]] = {}


async def _async_rename_stale_forecast_entities(
    hass: HomeAssistant, entry: NimbusConfigEntry
) -> None:
    """A load/signal subentry's forecast entity_id is derived from its
    CURRENT source sensor at CREATION time (sensor.py's own
    object_id_from_source(), used in NimbusForecastSensor.__init__) but
    never re-evaluated after that -- reconfiguring the subentry's own
    source sensor to something else updates its friendly_name (HA derives
    that live from the device+entity name) but leaves the entity_id
    itself silently stuck at the OLD source's name.

    2026-08-20, real household-hit confusion this exists to close: after
    reconfiguring the "Whole House" power signal from sensor.logger_load_
    power over to sensor.cb_total_combined_power_adjusted_kw, its forecast
    entity kept showing up as "CB Total Combined Power Adjusted (kW)
    Forecast" while still living at sensor.nimbus_logger_load_power_
    forecast -- confusing enough that manually finding the right entity
    to rename (vs. the similarly-named raw source sensor, a real,
    different, non-Nimbus entity) was a genuine, easy-to-get-wrong task.

    Runs on every hub setup, which already happens automatically after
    every subentry add/edit/remove (see _async_update_listener below) --
    so a reconfigure's stale entity_id gets caught and fixed on the very
    same reload that already follows it, with no separate trigger needed.

    A real entity registry RENAME (er.async_update_entity(...,
    new_entity_id=...)), not a delete+recreate -- the unique_id (subentry-
    id-based, stable across this whole operation), area, labels, and any
    long-term statistics all move with it automatically. Only raw
    historical state rows already written under the old entity_id stay
    tagged with it -- the same accepted limitation ANY HA entity rename
    has, including the manual "change entity ID" UI action.

    Deliberately conservative: skips (leaves the stale name in place)
    rather than risk a broken rename if the entity isn't registered yet
    (first-ever setup, nothing to rename) or if something else is already
    using the correct target name (a real, if unlikely, collision --
    this project has been bitten by silent naming collisions before, so
    this checks explicitly rather than assume it can't happen).
    """
    registry = er.async_get(hass)
    for subentry in entry.subentries.values():
        if subentry.subentry_type not in _FORECASTABLE_SUBENTRY_TYPES:
            continue
        suffix = (
            "_signal_forecast"
            if subentry.subentry_type == SUBENTRY_TYPE_SIGNAL
            else "_load_forecast"
        )
        unique_id = f"{subentry.subentry_id}{suffix}"
        current_entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if current_entity_id is None:
            continue
        correct_entity_id = (
            f"sensor.{object_id_from_source(subentry.data[CONF_LOAD_SENSOR])}"
        )
        if current_entity_id == correct_entity_id:
            continue
        if registry.async_get(correct_entity_id) is not None:
            _LOGGER.warning(
                "Nimbus: %s should rename to %s (source sensor was reconfigured) "
                "but that entity_id is already taken by something else -- leaving "
                "the old name in place rather than risk a collision",
                current_entity_id,
                correct_entity_id,
            )
            continue
        _LOGGER.info(
            "Nimbus: renaming %s -> %s (source sensor was reconfigured)",
            current_entity_id,
            correct_entity_id,
        )
        registry.async_update_entity(current_entity_id, new_entity_id=correct_entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: NimbusConfigEntry) -> bool:
    """Set up the Nimbus hub -- one coordinator per load/power_signal subentry.

    entry.runtime_data is a dict keyed by subentry_id, not a single
    coordinator -- sensor.py iterates it to create one entity per load/
    signal. A hub with zero subentries yet (right after first install,
    before anything's been added via "+ Add") is valid and simply sets up
    nothing further until the first one exists.
    """
    # Always-on WARNING+/ERROR log capture (2026-08-25) -- see health.py's
    # own module docstring. Installed first, before anything else in this
    # function has a chance to log -- idempotent (safe across every
    # reload this function can run through, see install_log_buffer_
    # handler()'s own guard).
    health.install_log_buffer_handler()

    # nimbus_load.retrain (issue #195) -- idempotent, safe on every reload.
    services.async_register_services(hass)

    # Ship the switchboard-topology-card Lovelace resource -- served
    # over HTTP and registered as an extra JS module via
    # frontend.add_extra_js_url(), so a fresh HACS install gets the card
    # without a `www/` file copy or a manual Settings -> Dashboards ->
    # Resources step (issue #79). Works identically for storage-mode
    # and YAML-mode Lovelace. Non-fatal: a failure here (missing asset,
    # unexpected exception) still leaves the forecaster, sensors, and
    # solver fully functional -- the user can add the resource manually
    # the same way as before.
    try:
        integration = await async_get_integration(hass, DOMAIN)
        await frontend.async_register_frontend(hass, integration.version)
    except Exception:
        _LOGGER.exception(
            "Nimbus: topology-card frontend registration failed, "
            "the integration itself will still run -- the card can be "
            "registered manually via Settings -> Dashboards -> Resources"
        )

    # Runs before anything else -- a rename must land BEFORE the sensor
    # platform tries to add NimbusForecastSensor with its own freshly-
    # derived entity_id, so the platform sees "already exactly this" and
    # reconciles cleanly instead of a rename racing entity creation.
    try:
        await _async_rename_stale_forecast_entities(hass, entry)
    except Exception:
        # level reasoning: cosmetic entity-naming drift is real but never
        # worth taking the whole hub down over if something unexpected
        # happens here. Logged loudly, not silently swallowed.
        _LOGGER.exception(
            "Nimbus: stale forecast entity_id rename check failed, continuing setup anyway"
        )

    forecastable_subentries = [
        s
        for s in entry.subentries.values()
        if s.subentry_type in _FORECASTABLE_SUBENTRY_TYPES
    ]

    coordinators: dict[str, NimbusCoordinator] = {}
    for subentry in forecastable_subentries:
        coordinator = NimbusCoordinator(hass, entry, subentry)
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
        coordinators[subentry.subentry_id] = coordinator

    entry.runtime_data = coordinators
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Native Solver runtime (2026-08-22) -- see solver_runtime.py's own
    # module docstring for the full "why this exists" story. Scheduled
    # unconditionally, at the hub level (there is only ever one hub) --
    # solver_runtime.async_run_solve() itself already handles "Solver
    # settings not configured yet" gracefully (a clear, expected log
    # line, not an error), so scheduling it before the wizard's been run
    # is safe, not premature. entry.async_on_unload() with the interval
    # tracker's own returned unsubscribe callable is the same, already-
    # proven pattern as the update listener two lines above -- correctly
    # cancels the timer on unload/reload, no leaked callback.
    # Idempotent: cancel any timer already registered for this entry_id
    # before creating a new one -- see _solver_timer_unsub's own comment
    # above for why a second one can otherwise end up coexisting with the
    # first. Both async_track_time_interval() and async_track_utc_time_
    # change()'s own returned unsub are safe to call more than once (a
    # plain listener-removal, no-ops if already removed), so the entry.
    # async_on_unload() registration below still applies cleanly on top
    # of this.
    old_unsub = _solver_timer_unsub.pop(entry.entry_id, None)
    if old_unsub is not None:
        old_unsub()

    async def _periodic_solve(now) -> None:
        await solver_runtime.async_run_solve(hass)

    # Phase-locked to the NEM 5-minute boundary + 30s (issue #244), not a
    # free-running interval -- see _SOLVER_CRON_MINUTES/_SOLVER_CRON_SECOND's
    # own comment above for why. AEST (this project's only real deployment
    # timezone so far) is a whole-hour, no-DST offset from UTC, so matching
    # on UTC minute/second here lands on the same wall-clock :00/:05/...
    # boundary a local-time match would -- local=False (the default) is
    # deliberately NOT switched to local=True, since that would need to
    # re-derive on every DST transition in a timezone that has one.
    unsub_periodic_solve = async_track_utc_time_change(
        hass,
        _periodic_solve,
        minute=_SOLVER_CRON_MINUTES,
        second=_SOLVER_CRON_SECOND,
    )
    _solver_timer_unsub[entry.entry_id] = unsub_periodic_solve
    entry.async_on_unload(unsub_periodic_solve)

    # Optional native state-change trigger on the configured price
    # sensors (issue #256) -- purely additive on top of the periodic
    # cron above. Default OFF, byte-identical behaviour on every install
    # that hasn't enabled it via the Solver: Grid Prices step. See CONF_
    # SOLVE_ON_PRICE_CHANGE's own comment in const.py for the full
    # measured-evidence "why the periodic cron alone misses intra-block
    # revisions" reasoning.
    _configure_price_watcher(hass, entry)

    # One immediate cycle at setup too, in the background -- so a fresh
    # install (or a restart) doesn't sit with an empty forecast for up to
    # a full 5-minute cron period before anything shows up.
    hass.async_create_task(solver_runtime.async_run_solve(hass))

    return True


def _configured_price_sensors(entry: NimbusConfigEntry) -> tuple[str, ...]:
    """The set of import/export price sensors the wizard currently has
    configured for this hub (in canonical order for deterministic set-
    change comparison). Optional _2/_3 secondary sources are included
    when set; empty/None values are dropped so they don't turn into a
    listener on a non-existent entity_id. Returned as a tuple so it can
    key into _price_watcher_entities directly.
    """
    keys = (
        CONF_SOLVER_IMPORT_PRICE_SENSOR,
        CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
        CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
        CONF_SOLVER_EXPORT_PRICE_SENSOR,
        CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
        CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
    )
    return tuple(
        entity_id
        for entity_id in (entry.options.get(k) for k in keys)
        if isinstance(entity_id, str) and entity_id
    )


def _configure_price_watcher(hass: HomeAssistant, entry: NimbusConfigEntry) -> None:
    """(Re)register the state-change listener that triggers an on-demand
    solve whenever any configured price sensor's state updates. Runs
    from async_setup_entry, so a hub reload that changed the toggle or
    the price-sensor set picks up the change immediately.

    Idempotent -- always cancels any previously registered listener
    first, so re-entering async_setup_entry a second time for the same
    entry_id can't leak duplicate listeners. Cheap no-op fast path
    when the toggle is off AND no listener was previously registered.
    """
    enabled = bool(
        entry.options.get(CONF_SOLVE_ON_PRICE_CHANGE, DEFAULT_SOLVE_ON_PRICE_CHANGE)
    )
    price_entities = _configured_price_sensors(entry) if enabled else ()

    prev_entities = _price_watcher_entities.get(entry.entry_id, ())
    prev_unsub = _price_watcher_unsub.get(entry.entry_id)
    # Fast path: nothing changed -- avoid a needless unsub/re-register cycle
    # on hub reloads unrelated to this feature.
    if prev_entities == price_entities and (prev_unsub is not None) == bool(
        price_entities
    ):
        return

    if prev_unsub is not None:
        prev_unsub()
        _price_watcher_unsub[entry.entry_id] = None
        _price_watcher_entities[entry.entry_id] = ()

    if not price_entities:
        return

    debounce_s = float(
        entry.options.get(
            CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
            DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
        )
    )
    # A small in-flight box so a burst of state_changed events within the
    # debounce window coalesces into a single solve. asyncio.Handle is
    # HA's own scheduler primitive -- created here by hass.loop.call_later
    # rather than asyncio.get_event_loop() so a test harness that swaps
    # the loop still lands on the harness's own loop.
    pending: dict[str, object] = {"handle": None}

    def _fire_solve() -> None:
        pending["handle"] = None
        hass.async_create_task(solver_runtime.async_run_solve(hass))

    @callback
    def _on_price_change(event) -> None:
        # Cancel any pending fire and reschedule -- the same coalescing
        # pattern HA's own async_debounce helper uses internally. Not
        # imported here directly because the helper's exact module path
        # has moved across HA releases and this is a two-line inline
        # equivalent that stays stable across those moves.
        handle = pending.get("handle")
        if handle is not None:
            handle.cancel()  # type: ignore[attr-defined]
        pending["handle"] = hass.loop.call_later(debounce_s, _fire_solve)

    unsub = async_track_state_change_event(hass, list(price_entities), _on_price_change)

    def _combined_unsub() -> None:
        """Cancel any pending debounced solve as well as the listener
        itself, so a hub unload right after a state-change burst can't
        leave a stray callback still fired for a torn-down hub.
        """
        handle = pending.get("handle")
        if handle is not None:
            handle.cancel()  # type: ignore[attr-defined]
            pending["handle"] = None
        unsub()

    _price_watcher_unsub[entry.entry_id] = _combined_unsub
    _price_watcher_entities[entry.entry_id] = price_entities
    entry.async_on_unload(_combined_unsub)


async def _async_update_listener(hass: HomeAssistant, entry: NimbusConfigEntry) -> None:
    """Reload the whole hub when a load subentry is added, edited, or
    removed. Home Assistant's own subentry flow (async_create_entry /
    async_update_and_abort / subentry deletion) triggers this automatically
    -- this is what makes adding load #2 through #18 take effect immediately
    with no restart, matching HAEO's own hot-add behaviour for its elements.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NimbusConfigEntry) -> bool:
    """Unload the Nimbus hub and every one of its load coordinators."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        for coordinator in entry.runtime_data.values():
            coordinator.async_unload()
        # Symmetric with _solver_timer_unsub's own registration above --
        # a genuinely removed (not just reloaded) entry shouldn't leave a
        # stale, already-cancelled-by-entry.async_on_unload unsub sitting
        # in this module-level dict forever.
        _solver_timer_unsub.pop(entry.entry_id, None)
        # Symmetric with the price-watcher registration in async_setup_
        # entry above -- the entry.async_on_unload registration already
        # cancelled the listener, we just don't want a stale key sitting
        # in these module-level dicts forever after a genuine removal.
        _price_watcher_unsub.pop(entry.entry_id, None)
        _price_watcher_entities.pop(entry.entry_id, None)
    return unload_ok
