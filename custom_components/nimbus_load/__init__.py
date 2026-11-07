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

import asyncio
import logging
from collections.abc import Callable

# Not `import time`: loading the `time` platform (time.py) sets this
# package's attribute `time` to that submodule, replacing the stdlib module
# here -- `time.monotonic()` then raised AttributeError and setup failed
# (devhub, 11 Oct 2026).
from time import monotonic

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
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    DEFAULT_SOLVE_ON_PRICE_CHANGE,
    DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    DOMAIN,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)
from .coordinator import NimbusConfigEntry, NimbusCoordinator
from .sensor import object_id_from_source

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    # nimbus issue #485: select.nimbus_household_mode, the one
    # hub-level mode entity. See select.py's own docstring for why
    # this is an entity rather than a wizard field.
    Platform.SELECT,
    # Household, 11 Oct 2026: time-of-day settings (the reserve's window)
    # as clock times. See time.py.
    Platform.TIME,
]

# #244 (Mark): solve at :00:30, :05:30, ... -- 30 s past each NEM boundary, after the
# settled price usually lands (15-30 s, 89% of 273 measured ticks). A free-running
# interval often solved just before it. The overlap guard in async_run_solve() covers a
# long cycle.
_SOLVER_CRON_MINUTES = list(range(0, 60, 5))
_SOLVER_CRON_SECOND = 30

# #295 (Mark): skip a cron solve if any solve finished within 60 s, so it no longer
# repeats an event-driven solve. The cron is a watchdog guaranteeing one solve per
# 5-minute block, not a heartbeat.
_CRON_SUPPRESS_WINDOW_S = 60

# Startup retry (2026-08-30): the first solve can race the restore of the required
# number.nimbus_solver_* entities and see "unconfigured". 6 tries 15 s apart (90 s)
# outlast that race; an install with no wizard run gives up and waits for the cron.
_STARTUP_RETRY_ATTEMPTS = 6
_STARTUP_RETRY_DELAY_SECONDS = 15

_FORECASTABLE_SUBENTRY_TYPES = (SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL)

_LOGGER = logging.getLogger(__name__)

# Periodic-solve timer unsub by entry_id, module-level (not hass.data; see
# NimbusConfigEntry). #211: an abandoned-and-retried slow async_setup_entry() (the #210
# mechanism) registered a second live timer, giving two solves a minute; a new setup
# cancels the old timer first.
_solver_timer_unsub: dict[str, Callable[[], None]] = {}

# The P2P block-start solve trigger (see _P2P_BLOCK_START_SECOND below),
# held and cancelled exactly like _solver_timer_unsub above.
_p2p_start_timer_unsub: dict[str, Callable[[], None]] = {}

# Solve at :05 of a P2P block's start minute (lead time included): neither the cron nor
# the price watcher reliably plans a minute like 16:59 (measured 1-5 Oct 2026: the
# battery reached block rate 14-32 s late once prices stopped republishing). :05 keeps
# the minute's plan period almost whole.
_P2P_BLOCK_START_SECOND = 5

# #1678: inside an active P2P block, also solve at :20, :35 and :50 unless any solve
# finished within _P2P_BLOCK_REPLAN_MIN_GAP_S. Between solves every house-load step
# lands on the meter (10.3-12.8 kW against a 12 kW block at the normal cadence; flat at
# ~15 s). Outside a block nothing changes.
_P2P_BLOCK_REPLAN_SECONDS = (5, 20, 35, 50)
_P2P_BLOCK_REPLAN_MIN_GAP_S = 14.0

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

# entry_ids that already have a price-watcher cleanup hook registered
# with entry.async_on_unload() for the CURRENT load of that entry
# (nimbus issue #337). One hook per load, not one per (re)configure --
# every dashboard toggle of switch.nimbus_solve_on_price_change used to
# append another already-fired unsub to entry._on_unload, each of which
# would then raise at unload time. Cleared by async_unload_entry.
_price_watcher_unload_hooked: set[str] = set()

# Startup-retry task by entry_id (#312), so async_unload_entry() can cancel it before
# platform teardown; async_on_unload runs too late, and a waking retry raw-wrote a state
# that collided with the next setup's entity.
_startup_solve_tasks: dict[str, asyncio.Task[None]] = {}

# Re-entrancy guard (2026-09-01): HA can abandon a slow async_setup_entry() and retry it
# while the first still runs; two runs for one entry_id raced entity registration ("does
# not generate unique IDs"), so numbers sometimes reset to placeholders. A second call
# waits for the first and returns its result, whichever step is slow.
_setup_tasks: dict[str, asyncio.Task[bool]] = {}


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


# Switches a release added and a later one removed: their registry rows are
# deleted on setup so they do not linger as "unavailable". Keyed by the
# switch's config key; the unique_id is f"{entry_id}_{key}".
# - solver_bank_solar_first: v0.94.464, removed for v0.94.465 (Mark Purcell
#   on #1844, 18 Oct 2026: "A switch corrupts the plan"; household decision).
RETIRED_SWITCH_KEYS: tuple[str, ...] = ("solver_bank_solar_first",)


def _async_remove_retired_switches(
    hass: HomeAssistant, entry: NimbusConfigEntry
) -> None:
    """Delete this entry's registry rows for switches no release creates any
    more. Only exact unique_ids built from `RETIRED_SWITCH_KEYS`."""
    registry = er.async_get(hass)
    for key in RETIRED_SWITCH_KEYS:
        entity_id = registry.async_get_entity_id(
            "switch", DOMAIN, f"{entry.entry_id}_{key}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)
            _LOGGER.info("Nimbus: removed the retired switch %s", entity_id)


async def _async_report_orphaned_forecast_entities(
    hass: HomeAssistant, entry: NimbusConfigEntry
) -> None:
    """Report forecast entities whose subentry no longer exists (nimbus #1270).

    Home Assistant already handles the normal case: `async_remove_subentry()`
    calls `ent_reg.async_clear_config_subentry(...)`, so a subentry removed
    today takes its entities with it. `test_stale_devices_cleanup` states that
    mechanism's own limit exactly -- *"HA's automatic cleanup can't recover
    devices it was never told belong to a subentry."* Entities registered
    before `config_subentry_id` was threaded through (#645/#680 document the
    number.py side of that same gap) carry no subentry link at all, so when
    their subentry went away core had nothing to clear, and nothing has
    revisited them since -- IF such an entity exists at all, which is the part
    that is not established.

    **No confirmed instance exists.** The entities that motivated #1270 --
    `sensor.nimbus_mirror_{temperature,humidity}_forecast` on production and
    devhub, both reading `unknown` -- were diagnosed as orphans and are not.
    Each carries a `nimbus_version` attribute stamped with the version running
    at the time it was read (0.94.422 on production, 0.94.424 on devhub minutes
    after that release), which an entity with no provider cannot do. This
    function therefore guards a condition that is real in principle and
    unobserved in practice, which is why it reports rather than deletes.

    **The discriminator is the unique_id, deliberately not
    `config_subentry_id`.** Filtering on the subentry link would miss every
    orphan this exists to remove, because the missing link IS the symptom.
    `NimbusForecastSensor`'s unique_id embeds the owning subentry id verbatim
    (`f"{subentry.subentry_id}{suffix}"`, see the rename pass above), so it
    still names the owner of an entity that has lost its link.

    **Reported rather than removed -- and that is a deliberate retreat.** The
    entities that motivated #1270 turned out NOT to be orphans: they carry a
    `nimbus_version` attribute stamped with the currently-running version, so
    the live code is publishing them, and their unique_id shape
    (`<entry_id>_<prefix>_<suffix>`, sensor_flattened.py) is not this class at
    all. They read `unknown` because their input is empty, which is an entirely
    different condition. So there is no confirmed instance of the condition
    below, and deleting user-visible registry rows on speculation is the wrong
    trade however well-guarded the deletion is.

    The reasoning that still holds, and why this is worth reporting at all: an
    orphan keeps its `entity_id` reserved, so an entity that should own that
    name is bumped to `_2` -- an id change on a live entity, which is what
    dashboards and automations reference. The rename pass above cannot repair
    that; it explicitly skips when the target name is taken, and an orphan is
    exactly something taking it. And a household cannot fix it by hand, because
    HA greys out delete for an entity belonging to a loaded config entry. If a
    real instance appears, this WARNING makes it findable and promoting it to an
    `async_remove()` is a one-word change made with evidence in hand.

    **The diagnostic rule this cost:** `unknown` means a provider with no value.
    `unavailable` with no attributes means no provider. Checking whether
    anything still publishes an entity -- one attribute -- settles "is this an
    orphan" before any of the rest matters.

    Three guards, each for a failure mode rather than a hypothetical:

    1. **Bail out if `entry.subentries` is empty.** A partially-loaded entry
       must never be read as "every subentry was removed". This is the one that
       would turn a transient into a mass delete.
    2. **Only forecast-suffixed unique_ids are considered.** Hub-scoped
       entities (the Solver push sensors, the config sensors) carry no such
       suffix and so are structurally unreachable here -- the same separation
       `test_stale_devices_cleanup` already guards on the registration side.
    3. **Only this config entry's own entities.**
    """
    if not entry.subentries:
        # Guard 1. Not a tidy early-out: an empty subentries mapping during a
        # partial load would otherwise mean "orphan everything".
        return

    registry = er.async_get(hass)
    known_subentry_ids = set(entry.subentries)
    suffixes = ("_signal_forecast", "_load_forecast")

    # nimbus issue #1294 (Mark Purcell, IV&V #1289): collected rather than
    # only logged per-entity, so the notification below can be ONE message
    # naming every orphan. An install that somehow grew twenty would
    # otherwise raise twenty notifications for a single underlying cause.
    orphans: list[tuple[str, str]] = []

    # list() so this stays correct if the WARNING is ever promoted to a
    # real removal, which would mutate the registry underneath the loop.
    for registry_entry in list(
        er.async_entries_for_config_entry(registry, entry.entry_id)
    ):
        unique_id = registry_entry.unique_id or ""
        suffix = next((s for s in suffixes if unique_id.endswith(s)), None)
        if suffix is None:
            continue  # Guard 2 -- not a forecast entity, not ours to sweep.
        owning_subentry_id = unique_id[: -len(suffix)]
        if not owning_subentry_id or owning_subentry_id in known_subentry_ids:
            continue
        # Reported, not removed: no confirmed orphan exists (#1270), so deleting
        # registry rows would be speculation. WARNING so a real one is findable.
        _LOGGER.warning(
            "Nimbus: forecast entity %s appears orphaned -- its subentry (%s) "
            "is not in this config entry, so nothing provides it and it is "
            "holding that entity_id reserved, which can force a live entity "
            "onto a _2 suffix. Not removed automatically (nimbus issue #1270); "
            "please report this log line on that issue",
            registry_entry.entity_id,
            owning_subentry_id,
        )
        orphans.append((registry_entry.entity_id, owning_subentry_id))

    if not orphans:
        # #1320 (Mark): dismiss the notification when the condition clears, as the #416
        # sibling pair does (#1294 only stopped re-creating it). Dismissing an id that
        # isn't shown is a no-op, and this runs once per setup, so no sentinel.
        try:
            await hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": "nimbus_orphaned_forecast_entities"},
                blocking=False,
            )
        except Exception:
            # Courtesy only, exactly as the create path is: a diagnostic must
            # never be the reason setup fails.
            _LOGGER.debug(
                "Nimbus: could not dismiss the orphaned-forecast-entity "
                "notification; harmless, it will be dismissed on a later setup",
                exc_info=True,
            )
        return

    # #1294: a persistent notification, not only a log line: an orphan forces a live
    # entity onto a `_2` id and the household can't delete it. Stable id so a reload
    # replaces it; dismissed above when clear (#1320). Still not a removal (see #1270).
    listed = "\n".join(
        f"- {entity_id} (subentry {subentry_id})" for entity_id, subentry_id in orphans
    )
    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Nimbus: orphaned forecast entities",
                "message": (
                    f"{len(orphans)} Nimbus forecast "
                    f"{'entity' if len(orphans) == 1 else 'entities'} "
                    "no longer have a subentry providing them:\n\n"
                    f"{listed}\n\n"
                    "Each one keeps its entity_id reserved, which can push a "
                    "live entity onto a `_2` suffix -- and Home Assistant "
                    "greys out delete for an entity belonging to a loaded "
                    "config entry, so this cannot be cleared by hand.\n\n"
                    "Nimbus does NOT remove these automatically. Please "
                    "report this on nimbus issue #1270 -- there is no "
                    "confirmed instance of this condition yet, and a real one "
                    "is what decides whether automatic removal is safe."
                ),
                "notification_id": "nimbus_orphaned_forecast_entities",
            },
            blocking=False,
        )
    except Exception:
        # A diagnostic must never be the reason setup fails. The caller already
        # wraps this whole function, but a notification is the least important
        # thing here and should not consume that allowance.
        _LOGGER.debug(
            "Nimbus: could not raise the orphaned-forecast-entity "
            "notification; the WARNING above still records it",
            exc_info=True,
        )


def _run_once_started(hass: HomeAssistant, entry: NimbusConfigEntry, job) -> None:
    """Run `job(event)` once, on HA's "homeassistant_started", with an unload
    hook that is safe whether or not the event has fired (nimbus #1688).

    `hass.bus.async_listen_once()` removes its own listener when the event
    fires. Registering its unsub with `entry.async_on_unload()` as-is means a
    later reload calls it a second time, and HA core logs "Unable to remove
    unknown job listener" as an ERROR, once per listener, on every reload
    after a cold start. The unload hook here only unsubscribes a listener
    that has not fired yet.

    "homeassistant_started" is homeassistant.const.EVENT_HOMEASSISTANT_STARTED,
    spelled out so the unit-test stubs need no new constant.
    """
    fired = False

    async def _run(event: object = None) -> None:
        nonlocal fired
        fired = True
        await job(event)

    unsub = hass.bus.async_listen_once("homeassistant_started", _run)

    def _cancel_if_pending() -> None:
        if not fired:
            unsub()

    entry.async_on_unload(_cancel_if_pending)


async def async_setup_entry(hass: HomeAssistant, entry: NimbusConfigEntry) -> bool:
    """Public entry point -- see _setup_tasks' own module-level comment for
    the full "why a re-entrancy guard, not just another backgrounded slow
    step" reasoning. A second, genuinely concurrent call for the SAME
    entry_id (HA's own abandon-and-retry behaviour, not a normal reload --
    a reload always waits for the prior async_unload_entry() to finish
    first, so it can never race this guard) waits for the first attempt's
    own real result instead of duplicating every side effect it caused.
    """
    if (
        existing := _setup_tasks.get(entry.entry_id)
    ) is not None and not existing.done():
        _LOGGER.warning(
            "Nimbus: async_setup_entry re-entered for entry %s while a "
            "previous attempt is still in flight (this is HA's own "
            "abandon-and-retry behaviour on a slow setup, not a normal "
            "reload -- see _setup_tasks' own module-level comment). "
            "Waiting for the original attempt instead of duplicating "
            "entity/timer setup a second time.",
            entry.entry_id,
        )
        return await existing

    # asyncio.create_task, not hass.async_create_task: this task is awaited right below,
    # so HA's await of async_setup_entry() tracks it, and tests that stub
    # hass.async_create_task as fire-and-forget keep working.
    task = asyncio.create_task(_async_setup_entry_impl(hass, entry))
    _setup_tasks[entry.entry_id] = task
    try:
        return await task
    finally:
        # Only clear OUR OWN reference -- a task that finished after being
        # replaced (shouldn't happen given the .done() check above, but
        # kept as a defensive no-op rather than an assumption) must never
        # accidentally clear a newer, still-in-flight task's own entry.
        if _setup_tasks.get(entry.entry_id) is task:
            del _setup_tasks[entry.entry_id]


async def _async_setup_entry_impl(
    hass: HomeAssistant, entry: NimbusConfigEntry
) -> bool:
    """The real setup body -- one coordinator per load/power_signal subentry.

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

    # Register the topology card's JS module so a HACS install needs no manual resource
    # (#79). Non-fatal: the forecaster and Solver work without it.
    try:
        integration = await async_get_integration(hass, DOMAIN)
        await frontend.async_register_frontend(hass, integration.version)
    except Exception:
        _LOGGER.exception(
            "Nimbus: topology-card frontend registration failed, "
            "the integration itself will still run -- the card can be "
            "registered manually via Settings -> Dashboards -> Resources"
        )

    # nimbus #1550: if LocalVolts v2 is installed, say so when a pricing
    # field is still empty, and flag a matched-rate sensor set without a price
    # forecast array. Notifications only; nothing is configured. Deferred to
    # HA's own "started" event when needed, so LocalVolts v2 has loaded.
    async def _pricing_check(_event: object = None) -> None:
        try:
            from homeassistant.helpers.storage import Store

            from .pricing_autodetect import async_notify_pricing_setup

            # What the household was already asked, across restarts.
            store = Store(hass, 1, f"{DOMAIN}.asked_{entry.entry_id}")
            asked = set((await store.async_load() or {}).get("asked", []))
            before = set(asked)
            await async_notify_pricing_setup(hass, dict(entry.options), asked)
            if asked != before:
                await store.async_save({"asked": sorted(asked)})
        except Exception:
            _LOGGER.exception("Nimbus: pricing setup check failed")

    if hass.is_running:
        await _pricing_check()
    else:
        _run_once_started(hass, entry, _pricing_check)

    # Runs before anything else -- a rename must land BEFORE the sensor
    # platform tries to add NimbusForecastSensor with its own freshly-
    # derived entity_id, so the platform sees "already exactly this" and
    # reconciles cleanly instead of a rename racing entity creation.
    try:
        _async_remove_retired_switches(hass, entry)
        await _async_rename_stale_forecast_entities(hass, entry)
        # nimbus #1270. Runs AFTER the rename, deliberately: clearing an
        # orphan frees the entity_id it was squatting, but the rename pass
        # above has already run for this setup, so the freed name is taken up
        # on the NEXT reload rather than this one. That ordering is the
        # conservative one -- a rename and a removal racing over the same id
        # within a single pass is exactly the collision the rename pass
        # already refuses to risk.
        await _async_report_orphaned_forecast_entities(hass, entry)
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

    # Coordinators set up concurrently: they share no state, and the sequential loop was
    # slow enough to trip HA's abandon-and-retry (see _setup_tasks). The default
    # gather() keeps the old semantics: the first failure aborts setup.
    async def _setup_one(subentry) -> tuple[str, NimbusCoordinator]:
        coordinator = NimbusCoordinator(hass, entry, subentry)
        await coordinator.async_setup()
        await coordinator.async_config_entry_first_refresh()
        return subentry.subentry_id, coordinator

    results = await asyncio.gather(
        *(_setup_one(subentry) for subentry in forecastable_subentries)
    )
    coordinators: dict[str, NimbusCoordinator] = dict(results)

    entry.runtime_data = coordinators

    # Unload ordering (2026-09-01, confirmed with DEBUG logs; the 0.1 s settle sleep of
    # v0.94.47 didn't work and was removed): async_on_unload callbacks run after
    # async_unload_entry() returns, so an old timer, price watcher or retry task could
    # solve after an entity's handler was unregistered and raw-write a ghost state the
    # next entity collides with. async_unload_entry() now cancels them before platform
    # teardown, and ha_post_state() skips the raw fallback for natively handled
    # entity_ids.

    # Diagnostic logging (2026-08-31, nimbus issue #312) -- kept in place:
    # if a collision is ever seen again, this call's own start/end/duration
    # is the first thing to check against the error's own timestamp.
    _forward_started = monotonic()
    _LOGGER.debug(
        "Nimbus: forwarding entry %s to platforms %s", entry.entry_id, PLATFORMS
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.debug(
        "Nimbus: platform forwarding for entry %s completed in %.3fs",
        entry.entry_id,
        monotonic() - _forward_started,
    )

    # nimbus #1574 (DC-004 step 5b): an entry in the device model's `active`
    # mode regenerates its settings if a commit was interrupted. After the
    # platforms, so the number entities exist; before the update listener, so
    # its own write does not reload mid-setup. If it wrote anything, one
    # reload follows so every component reads the recovered settings, and that
    # second start finds nothing to do. Every entry today is `legacy`, where
    # this reads one store and returns.
    from . import device_contract_ha

    # DC-007 slice 1 (#1574): a hub created by the new-install setup carries
    # the site the household confirmed; its first setup commits it, then the
    # entry forgets it. A refusal leaves the hub in `legacy`, as before the
    # flow, and a Repair says why.
    pending = (entry.data or {}).get("pending_device_document")
    if isinstance(pending, dict):
        try:
            adopted = await device_contract_ha.async_adopt_new_install(
                hass, entry, pending
            )
        except Exception:  # an install fault must not stop the rest of setup
            _LOGGER.exception("Nimbus: the confirmed site could not be saved")
            adopted = {"ok": False, "code": "error"}
        hass.config_entries.async_update_entry(
            entry,
            data={
                k: v for k, v in entry.data.items() if k != "pending_device_document"
            },
        )
        if not adopted.get("ok"):
            from homeassistant.helpers import issue_registry as ir

            ir.async_create_issue(
                hass,
                DOMAIN,
                f"new_install_setup_failed_{entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="new_install_setup_failed",
                translation_placeholders={"reason": str(adopted.get("code"))},
            )

    try:
        recovered = await device_contract_ha.async_recover(hass, entry)
    except Exception:  # a recovery fault must not stop the rest of setup
        _LOGGER.exception(
            "Nimbus: device setup recovery failed; it runs again at the next start"
        )
        recovered = "error"
    if recovered == "regenerated":
        hass.config_entries.async_schedule_reload(entry.entry_id)
    else:
        # Step 5c: every settings edit reloads the entry, so this one check
        # catches a drifted owned key whichever form, subentry or service
        # edited it.
        try:
            await device_contract_ha.async_check_drift(hass, entry)
        except Exception:  # a drift check fault must not stop the rest of setup
            _LOGGER.exception("Nimbus: device setup drift check failed")
        # DC-006 observation slice (#1893): a power sensor with no unit, or a
        # misspelt one, asks the household to declare it before `active`.
        try:
            await device_contract_ha.async_check_units(hass, entry)
        except Exception:  # a unit check fault must not stop the rest of setup
            _LOGGER.exception("Nimbus: device setup unit check failed")

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # No dashboard is created or written at setup (HACS review,
    # hacs/default#12099; household 26 Oct 2026). The standard tabs are
    # returned on request by nimbus_load.get_dashboard -- see
    # forecaster_dashboard.py.

    # Native Solver timer, hub-level, scheduled even before the wizard runs
    # (async_run_solve handles "not configured"). Idempotent: cancel any timer already
    # registered for this entry_id (see _solver_timer_unsub).
    old_unsub = _solver_timer_unsub.pop(entry.entry_id, None)
    if old_unsub is not None:
        old_unsub()

    async def _periodic_solve(now) -> None:
        # Issue #295: suppress this tick if an event-driven (or startup)
        # solve already covered this 5-min block -- see
        # _CRON_SUPPRESS_WINDOW_S's own comment above for the reasoning.
        # time_since_last_solve() returns None on a fresh install/restart
        # (nothing has ever solved yet), which correctly never suppresses.
        since_last_solve = solver_runtime.time_since_last_solve()
        if since_last_solve is not None and since_last_solve < _CRON_SUPPRESS_WINDOW_S:
            _LOGGER.debug(
                "Nimbus Solver: skipping phase-locked cron tick -- a solve "
                "already completed %.1fs ago (issue #295)",
                since_last_solve,
            )
            return
        if await solver_runtime.async_run_solve(hass):
            solver_runtime.record_solve_completed(trigger_source="cron")

    # Phase-locked to NEM boundary + 30 s (#244). Matched on UTC: AEST is a whole-hour
    # offset, and local time would need re-deriving at every DST change.
    unsub_periodic_solve = async_track_utc_time_change(
        hass,
        _periodic_solve,
        minute=_SOLVER_CRON_MINUTES,
        second=_SOLVER_CRON_SECOND,
    )
    _solver_timer_unsub[entry.entry_id] = unsub_periodic_solve
    entry.async_on_unload(unsub_periodic_solve)

    # P2P block-start trigger (see _P2P_BLOCK_START_SECOND's comment above).
    # Checked every minute rather than scheduled per block, so a block or
    # lead time edited from the dashboard takes effect at once, with no
    # re-registration. The check is one state read; a solve runs only in a
    # block's start minute.
    old_p2p_unsub = _p2p_start_timer_unsub.pop(entry.entry_id, None)
    if old_p2p_unsub is not None:
        old_p2p_unsub()

    async def _p2p_block_start_solve(now) -> None:
        state = hass.states.get("sensor.nimbus_solver_config")
        if state is None or state.state != "configured":
            return
        from .solver_shared import (
            _local,
            p2p_block_active,
            p2p_block_start_minutes,
        )

        local = _local(now)
        minute = local.hour * 60 + local.minute
        cfg = dict(state.attributes)
        if local.second == _P2P_BLOCK_START_SECOND and minute in (
            p2p_block_start_minutes(cfg)
        ):
            _LOGGER.debug(
                "Nimbus Solver: P2P block starts at %02d:%02d, solving now",
                local.hour,
                local.minute,
            )
            if await solver_runtime.async_run_solve(hass):
                solver_runtime.record_solve_completed(trigger_source="p2p_block_start")
            return
        # nimbus #1678: re-plan every ~15 s while a block is active.
        if not p2p_block_active(cfg, minute):
            return
        since = solver_runtime.time_since_last_solve()
        if since is not None and since < _P2P_BLOCK_REPLAN_MIN_GAP_S:
            return
        if await solver_runtime.async_run_solve(hass):
            solver_runtime.record_solve_completed(trigger_source="p2p_block_replan")

    unsub_p2p_start = async_track_utc_time_change(
        hass, _p2p_block_start_solve, second=list(_P2P_BLOCK_REPLAN_SECONDS)
    )
    _p2p_start_timer_unsub[entry.entry_id] = unsub_p2p_start
    entry.async_on_unload(unsub_p2p_start)

    # Optional native state-change trigger on the configured price
    # sensors (issue #256) -- purely additive on top of the periodic
    # cron above. Default OFF, byte-identical behaviour on every install
    # that hasn't enabled it via the Solver: Grid Prices step. See CONF_
    # SOLVE_ON_PRICE_CHANGE's own comment in const.py for the full
    # measured-evidence "why the periodic cron alone misses intra-block
    # revisions" reasoning.
    _configure_price_watcher(hass, entry)

    # One immediate solve at setup, in the background, retried (see
    # _STARTUP_RETRY_ATTEMPTS) since it can race the number restore. Cancelled on
    # unload, and a background task so async_block_till_done() doesn't wait out the
    # retries (both found in CI 2026-08-30).
    # #1562: say so when a power input is an energy counter (Wh/kWh). Run once HA has
    # started, when every sensor's unit is known.
    from homeassistant.helpers.event import async_track_time_interval

    from .power_input_check import async_notify_energy_unit_inputs
    from .setup_builder import async_refresh_setup_fills
    from .setup_health import REFRESH_INTERVAL, async_refresh_health

    async def _energy_unit_check(_event: object = None) -> None:
        # nimbus #1574 stage 1: offer, as one-click Repairs, to fill the
        # Solver's empty battery/solar power and whole-house cross-check from
        # sensors the user already confirmed elsewhere. Writes nothing until
        # the household submits (Mark's review of #1587). No Power Signals are
        # created. Same "once HA has started" timing as the unit check below:
        # units must be known.
        await async_refresh_setup_fills(hass, entry)
        await async_notify_energy_unit_inputs(hass, entry)
        # nimbus #1574 stage 2: setup gaps as Repairs that clear themselves.
        await async_refresh_health(hass, entry)

    async def _health_tick(_now: object = None) -> None:
        await async_refresh_setup_fills(hass, entry)
        await async_refresh_health(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(hass, _health_tick, REFRESH_INTERVAL)
    )

    if getattr(hass, "is_running", False) is True:
        hass.async_create_task(_energy_unit_check())
    else:
        _run_once_started(hass, entry, _energy_unit_check)

    startup_solve_task = hass.async_create_background_task(
        _async_run_solve_with_startup_retries(hass),
        name="nimbus_load_startup_solve_retry",
    )
    # Tracked so async_unload_entry() can cancel it directly and promptly,
    # before platform teardown -- see _startup_solve_tasks' own module-
    # level comment. entry.async_on_unload() below is kept too, as a
    # defensive second cancellation for a genuine entry REMOVAL (not just
    # a reload) -- cancelling an already-cancelled/finished task is a safe
    # no-op, so keeping both is harmless.
    _startup_solve_tasks[entry.entry_id] = startup_solve_task

    def _cancel_startup_solve_task() -> None:
        # A wrapper returning None: HA schedules any truthy on-unload return value as a
        # job, and Task.cancel() returns True while running.
        startup_solve_task.cancel()

    entry.async_on_unload(_cancel_startup_solve_task)

    return True


async def _async_run_solve_with_startup_retries(hass: HomeAssistant) -> None:
    """Call async_run_solve() once immediately, then retry a bounded
    number of times on failure (short delay between attempts) before
    giving up and leaving it to the regular periodic cron.

    Deliberately does not distinguish WHY a cycle failed (Solver not yet
    configured, a real solve error, anything else async_run_solve()
    itself already logs) -- a genuinely-unconfigured fresh install just
    retries harmlessly a few times over _STARTUP_RETRY_ATTEMPTS *
    _STARTUP_RETRY_DELAY_SECONDS before falling silent, exactly matching
    today's behaviour from that point on (silent until the wizard's run,
    picked up by the next periodic cron tick once it is). The only
    thing this changes is closing the real, observed gap where a
    transient startup race got only one chance instead of several.
    """
    for attempt in range(_STARTUP_RETRY_ATTEMPTS):
        if await solver_runtime.async_run_solve(hass):
            # Issue #295: a startup solve counts toward the same
            # suppression window as a cron/price-change one -- a restart
            # landing right before a phase-locked cron boundary shouldn't
            # trigger an immediately-redundant re-solve either.
            solver_runtime.record_solve_completed(trigger_source="startup")
            return
        if attempt < _STARTUP_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(_STARTUP_RETRY_DELAY_SECONDS)


def _configured_price_sensors(entry: NimbusConfigEntry) -> tuple[str, ...]:
    """The price sensor(s) that trigger an on-demand solve on state
    change -- the PRIMARY import price sensor only.

    nimbus issue #651 (Mark Purcell): this used to also watch the _2/_3
    secondary/tertiary import and export price sensors. Those secondary
    sources (e.g. a household's own Energex/pd7day blend input) update on
    their own schedule, typically offset by a minute or so from the
    primary retailer's real NEM boundary -- watching them created an
    extra, off-boundary solve every cycle whose tier-0 (sub-5-minute)
    periods have no proximal tether to either the plan before or after
    it, landing on an arbitrary LP vertex (issue #635's own residual,
    still present after that fix). The secondary/tertiary sources still
    feed the LP as real inputs on every regular solve -- this only
    changes what triggers an *extra*, off-cycle one. Restricting the
    trigger to the primary import sensor removes that off-boundary solve
    entirely, so there's no tier-0 grid left to misalign in the first
    place.
    """
    entity_id = entry.options.get(CONF_SOLVER_IMPORT_PRICE_SENSOR)
    if isinstance(entity_id, str) and entity_id:
        return (entity_id,)
    return ()


def _cancel_price_watcher(entry_id: str) -> None:
    """Tear down the price-watcher listener (and any pending debounced
    solve) for one entry. Idempotent by construction -- pops the unsub
    out of the module registry, so a second call finds nothing to do.

    nimbus issue #337: this is called from BOTH async_unload_entry()
    (first, before platform teardown -- see that function's own
    docstring) AND from the entry.async_on_unload() hook HA core runs
    afterwards. HA's own async_track_state_change_event() unsub is NOT
    safe to call twice (`callbacks[key].remove(job)` on a defaultdict
    raises ValueError the second time), and ConfigEntry.async_unload()
    turns that exception into FAILED_UNLOAD -- the hub simply never
    came back on a reload with the price watcher enabled.
    """
    _price_watcher_unload_hooked.discard(entry_id)
    _price_watcher_entities.pop(entry_id, None)
    unsub = _price_watcher_unsub.pop(entry_id, None)
    if unsub is not None:
        unsub()


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
    # #232: read the live switch so a toggle applies without a reload; fall back to
    # entry.options before the switch entity exists (seeded the same way).
    switch_state = hass.states.get(f"switch.nimbus_{CONF_SOLVE_ON_PRICE_CHANGE}")
    if switch_state is not None and switch_state.state in ("on", "off"):
        enabled = switch_state.state == "on"
    else:
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

    # Same live-entity-first / options-fallback pattern as the enabled
    # toggle above -- see that block's own comment for the reasoning.
    debounce_state = hass.states.get(
        f"number.nimbus_{CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S}"
    )
    debounce_s = DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S
    if debounce_state is not None and debounce_state.state not in (
        None,
        "unknown",
        "unavailable",
    ):
        try:
            debounce_s = float(debounce_state.state)
        except (TypeError, ValueError):
            pass
    else:
        debounce_s = float(
            entry.options.get(
                CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
                DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
            )
        )
    # Debounce box: a burst of state_changed events coalesces into one solve (call_later
    # on hass.loop, so test harness loops work). entity_id and changed_at keep the last
    # trigger for the latency sensor (#294); trigger_kind is #633.
    pending: dict[str, object] = {
        "handle": None,
        "entity_id": None,
        "changed_at": None,
        "trigger_kind": "price_change",
    }

    async def _run_price_change_solve() -> None:
        triggering_entity = pending.get("entity_id")
        price_change_at = pending.get("changed_at")
        trigger_kind = pending.get("trigger_kind", "price_change")
        context = {
            "trigger_source": trigger_kind,
            "triggering_entity": triggering_entity,
            "price_change_at": price_change_at,
            "debounce_s": debounce_s,
        }
        if await solver_runtime.async_run_solve(hass):
            solver_runtime.record_solve_completed(**context)
        else:
            # A cycle was running: the follow-up it runs answers this change,
            # and records its latency (#294, #945).
            solver_runtime.defer_price_trigger(**context)

    def _fire_solve() -> None:
        pending["handle"] = None
        hass.async_create_task(_run_price_change_solve())

    @callback
    def _on_price_change(event) -> None:
        # Cancel any pending fire and reschedule -- the same coalescing
        # pattern HA's own async_debounce helper uses internally. Not
        # imported here directly because the helper's exact module path
        # has moved across HA releases and this is a two-line inline
        # equivalent that stays stable across those moves.
        handle = pending.get("handle")
        real_change_pending = (
            handle is not None and pending.get("trigger_kind") == "price_change"
        )
        if handle is not None:
            handle.cancel()  # type: ignore[attr-defined]
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        # #633 (Mark): attribute-only updates also fire state_changed, with last_changed
        # still at the previous real change; only a real price change counts for
        # latency. No old_state counts as real (fail-open).
        is_real_price_change = (
            old_state is None or new_state is None or old_state.state != new_state.state
        )
        if real_change_pending and not is_real_price_change:
            # An attribute-only update right after a real change, inside the
            # same debounce window, keeps the change it follows: an
            # integration that writes the state, then its attributes a
            # second later (an MQTT-fed copy does) otherwise recorded no
            # latency at all (#294).
            pending["handle"] = hass.loop.call_later(debounce_s, _fire_solve)
            return
        pending["entity_id"] = event.data.get("entity_id")
        pending["trigger_kind"] = (
            "price_change" if is_real_price_change else "price_attributes"
        )
        if new_state is None:
            pending["changed_at"] = None
        elif is_real_price_change:
            pending["changed_at"] = new_state.last_changed
        else:
            # solver_runtime.record_solve_completed() already treats any
            # trigger_source other than "price_change" as a no-op for
            # the latency sensor -- price_change_at is never read for
            # this branch, but last_updated (this event's own real
            # timestamp, not a stale prior value) is still the honest
            # choice over last_changed if that ever changes.
            pending["changed_at"] = new_state.last_updated
        pending["handle"] = hass.loop.call_later(debounce_s, _fire_solve)

    listener_unsub: Callable[[], None] | None = async_track_state_change_event(
        hass, list(price_entities), _on_price_change
    )

    def _combined_unsub() -> None:
        """Cancel any pending debounced solve as well as the listener
        itself, so a hub unload right after a state-change burst can't
        leave a stray callback still fired for a torn-down hub.

        Idempotent (nimbus issue #337): the underlying HA listener unsub
        raises if called twice, so it is dropped after its first call.
        """
        nonlocal listener_unsub
        handle = pending.get("handle")
        if handle is not None:
            handle.cancel()  # type: ignore[attr-defined]
            pending["handle"] = None
        if listener_unsub is not None:
            listener_unsub()
            listener_unsub = None

    _price_watcher_unsub[entry.entry_id] = _combined_unsub
    _price_watcher_entities[entry.entry_id] = price_entities
    # One cleanup hook per load of this entry (nimbus issue #337). The
    # hook consults the registry at unload time rather than capturing
    # this particular listener, so a later re-configure (dashboard
    # toggle) neither leaks a stale hook nor needs a new one. It stays
    # registered as a safety net for the one path where HA processes
    # entry._on_unload WITHOUT ever calling async_unload_entry(): a
    # setup that fails after this point (ConfigEntryNotReady).
    if entry.entry_id not in _price_watcher_unload_hooked:
        _price_watcher_unload_hooked.add(entry.entry_id)
        entry_id = entry.entry_id
        entry.async_on_unload(lambda: _cancel_price_watcher(entry_id))


async def _async_update_listener(hass: HomeAssistant, entry: NimbusConfigEntry) -> None:
    """Reload the whole hub when a load subentry is added, edited, or
    removed. Home Assistant's own subentry flow (async_create_entry /
    async_update_and_abort / subentry deletion) triggers this automatically
    -- this is what makes adding load #2 through #18 take effect immediately
    with no restart, matching HAEO's own hot-add behaviour for its elements.

    nimbus #1574 (DC-004 step 5b): not while the device model is writing its
    views. It writes a number, subentry fields and the options in turn, and a
    reload mid-way would start from half-written settings; it reloads once
    itself when it has finished, and only if a setting changed.
    """
    from . import device_contract_ha

    if device_contract_ha.writing_views(hass, entry.entry_id):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: NimbusConfigEntry) -> bool:
    """Unload the Nimbus hub and every one of its load coordinators.

    Real root cause found (2026-09-01, see async_setup_entry's own comment
    above for the full story): stop every one of OUR OWN solve triggers
    (periodic cron, price-watcher, startup-retry task) FIRST, before
    tearing down platforms -- not via entry.async_on_unload(), which HA
    core only processes after this function has already returned. Doing
    it here, first, closes the real window where an old trigger could
    fire between "entities torn down" and "trigger actually cancelled"
    and poison solver_writer's raw-fallback state for the entity the next
    setup is about to (re-)create.
    """
    old_timer_unsub = _solver_timer_unsub.pop(entry.entry_id, None)
    if old_timer_unsub is not None:
        old_timer_unsub()
    old_p2p_unsub = _p2p_start_timer_unsub.pop(entry.entry_id, None)
    if old_p2p_unsub is not None:
        old_p2p_unsub()
    _cancel_price_watcher(entry.entry_id)
    old_startup_task = _startup_solve_tasks.pop(entry.entry_id, None)
    if old_startup_task is not None:
        old_startup_task.cancel()

    # #365 (Mark), item 4: wait for a solve already on its worker thread, so it finishes
    # against a fully set-up entry before platforms are torn down.
    await solver_runtime.wait_for_in_flight_solve()

    # nimbus issue #1657 gate 2: write the decision inputs still held in
    # memory (up to five minutes of them) before the entry goes. Never raises.
    from .decision_inputs_store import async_flush as _flush_decision_inputs

    await _flush_decision_inputs(hass, entry.entry_id)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        for coordinator in entry.runtime_data.values():
            coordinator.async_unload()
        # #365 (Mark), item 4: clear solver_runtime's module globals on unload, so a
        # remove-then-re-add starts clean (see reset_module_state()).
        solver_runtime.reset_module_state()
        # #365 (Mark), item 1: single_config_entry, so unload removes the services;
        # setup re-registers them idempotently on reload.
        services.async_unregister_services(hass)
    return unload_ok
