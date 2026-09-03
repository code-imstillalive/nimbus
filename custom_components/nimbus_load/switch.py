"""Switch platform for Nimbus -- live, dashboard-editable Solver toggles.

2026-08-22, direct household ask, after a sharp catch: nimbus_solver_
forecast_writer.py (the sibling 116KAT-HA-AI repo's own Solver writer)
was silently including two hardcoded, known-integration solar sources
(Open-Meteo, Solcast) OUTSIDE the 3 real solar_forecast_sensor_1/2/3
config fields entirely -- dressed up as "auto-detect", but with no way
to see it happening or turn it off. "then what is the purposed of
having 3 inputs since it forces user ot autodetect... that feels
wrong." Household's own design, verbatim: "user should pick 1... or
1+2... or 1+2+3 to get their desired blend... or yes can tick auto
detect from existing... for ease... otherwise it should be just a
setting they chose."

Exactly one switch entity today -- see const.py's own comment on
CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR for the full "default False, not
True" and "these two anchors aren't equally portable" reasoning.

Same restore-and-seed-once pattern as number.py's NimbusSolverNumber
(see that module's own docstring for the full "why not entry.options"
reasoning -- a full hub reload on every dashboard toggle would be bad
UX for something this light). HA has no built-in RestoreSwitch the way
it has RestoreNumber for numbers -- SwitchEntity + RestoreEntity
together is the standard equivalent.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import (
    CONF_SOLVE_ON_PRICE_CHANGE,
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    CONF_SOLVER_DISPATCH_DRY_RUN,
    DEFAULT_SOLVE_ON_PRICE_CHANGE,
    DEFAULT_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    DEFAULT_SOLVER_DISPATCH_DRY_RUN,
    DOMAIN,
)

# Same reasoning as number.py: a plain restored local toggle, no hub to
# overload by parallelizing.
PARALLEL_UPDATES = 0

# nimbus issue #342 (Mark Purcell): this module's own docstring above
# claims "same restore-and-seed-once pattern as number.py", but number.py
# ALSO has a durable Store backstop (see that module's own _SharedNumberStore
# docstring for the 2026-09-02 incident it exists to survive) -- this
# platform had none at all, bare RestoreEntity + seed-from-options only. A
# restore-state miss (the same genuine, still-not-fully-diagnosed HA-core
# startup timing race number.py's own comment describes) silently flips
# e.g. switch.nimbus_solve_on_price_change back to its class default with
# zero real fallback. A SEPARATE storage file/instance from number.py's own
# _SharedNumberStore, deliberately -- these are a different type (bool, not
# float) and are set via independent platform setup calls with no shared
# object between them; a genuinely shared single Store instance would need
# its own asyncio.Lock shared across both platforms too, real added
# complexity for a benefit that doesn't apply here (switch and number keys
# never overlap, so there's no real cross-entity write race to protect
# against, only the same single-entity restore-vs-Store freshness compare
# number.py already solves).
_STORAGE_VERSION = 1


@dataclass
class _SharedSwitchStore:
    """One Store + one lock, shared by every NimbusSolverSwitch instance
    for a given config entry -- same reasoning as number.py's own
    _SharedNumberStore (all switch keys live in the same small JSON file,
    so writes must be serialized)."""

    store: Store[dict[str, Any]]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def _async_read_entry(self, key: str) -> tuple[bool, float] | None:
        """Returns (value, written_at), or None if this key has never
        been written."""
        try:
            data = await self.store.async_load()
        except Exception:  # noqa: BLE001 -- a corrupt/unreadable store file
            # must never block this entity from falling through to its
            # own next fallback (entry.options / class default); it's a
            # durability BACKSTOP, not a required dependency.
            return None
        if not data or key not in data:
            return None
        entry = data[key]
        try:
            return bool(entry["value"]), float(entry["written_at"])
        except (TypeError, ValueError, KeyError):
            return None

    async def async_write(self, key: str, value: bool) -> None:
        async with self.lock:
            try:
                data = await self.store.async_load() or {}
            except Exception:  # noqa: BLE001 -- same reasoning as above
                data = {}
            data[key] = {"value": value, "written_at": time.time()}
            await self.store.async_save(data)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # Same independent sw_version read as number.py/sensor.py -- see
    # number.py's own async_setup_entry for why this isn't passed
    # between platform modules.
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version) if integration.version else None
    shared_store = _SharedSwitchStore(
        store=Store(
            hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_solver_switches"
        )
    )
    async_add_entities(
        [
            NimbusSolverSwitch(
                entry,
                CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
                "Auto-Include Known Solar Integrations",
                DEFAULT_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
                sw_version,
                shared_store,
            ),
            NimbusSolverSwitch(
                entry,
                CONF_SOLVER_DISPATCH_DRY_RUN,
                "Dispatch Dry Run",
                DEFAULT_SOLVER_DISPATCH_DRY_RUN,
                sw_version,
                shared_store,
            ),
            # Issue #232 follow-up: this used to live in the config-flow
            # wizard's solver_grid step. Moved out to a live switch entity
            # for exactly the same reason NimbusSolverNumber exists --
            # this is a runtime feature toggle a household will want to
            # flip on/off (e.g. temporarily silence the extra solves
            # during a debug window) without going through Settings ->
            # Devices & services -> Configure. __init__.py's own
            # _configure_price_watcher reads this switch's live state
            # (falling back to entry.options for a not-yet-migrated
            # install, until this entity's own restore/seed lands), and
            # NimbusSolverSwitch.async_turn_on/off below re-invoke that
            # function on every toggle so the listener registers/tears-
            # down live with no hub reload.
            NimbusSolverSwitch(
                entry,
                CONF_SOLVE_ON_PRICE_CHANGE,
                "Solve on Price Change",
                DEFAULT_SOLVE_ON_PRICE_CHANGE,
                sw_version,
                shared_store,
            ),
        ]
    )


class NimbusSolverSwitch(SwitchEntity, RestoreEntity):
    """One live, dashboard-editable Solver on/off toggle. See this
    module's own docstring for why these are plain restored local
    state, never written back into entry.options."""

    _attr_has_entity_name = True
    # Gold entity-category (2026-08-23) -- same reasoning as
    # NimbusSolverNumber's own CONFIG marking: this is a Solver tuning
    # toggle, not a primary reading or a diagnostic.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        key: str,
        name: str,
        default: bool,
        sw_version: str | None,
        shared_store: _SharedSwitchStore,
    ) -> None:
        self._entry = entry
        self._key = key
        self._default = default
        self._shared_store = shared_store
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        # Fixed entity_id, same technique/reasoning as NimbusSolverNumber's
        # own entity_id assignment in number.py -- one of these per hub
        # per field, a fixed, predictable name is correct here.
        self.entity_id = f"switch.nimbus_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )
        self._attr_is_on = default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state in ("on", "off"):
            restored_value = last_state.state == "on"
            # nimbus issue #342: same freshness compare as number.py's own
            # NimbusSolverNumber.async_added_to_hass() -- see that
            # method's own comment for the full "why" (a restore-state
            # dump can genuinely be staler than this Store's own last
            # write). Only backfill when the restore isn't older than
            # what the Store already holds, so a genuinely newer Store
            # entry always survives a stale restore.
            restored_at = last_state.last_updated.timestamp()
            stored_entry = await self._shared_store._async_read_entry(self._key)
            if stored_entry is not None and stored_entry[1] > restored_at:
                self._attr_is_on = stored_entry[0]
                return
            self._attr_is_on = restored_value
            await self._shared_store.async_write(self._key, restored_value)
            return
        # No restored state -- try this integration's OWN durable Store
        # next (nimbus issue #342), before ever falling through to a
        # stale wizard-time entry.options value or the hardcoded default.
        stored_entry = await self._shared_store._async_read_entry(self._key)
        if stored_entry is not None:
            self._attr_is_on = stored_entry[0]
            return
        # No restored state AND no Store entry -- this entity has never
        # existed before on this install. Seed from whatever's already in
        # entry.options (same convention as number.py), falling through
        # to _default (set in __init__ above) for a genuinely fresh
        # install.
        seeded = self._entry.options.get(self._key)
        if isinstance(seeded, bool):
            self._attr_is_on = seeded
            await self._shared_store.async_write(self._key, seeded)

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()
        await self._shared_store.async_write(self._key, True)
        self._reconfigure_dependents()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
        await self._shared_store.async_write(self._key, False)
        self._reconfigure_dependents()

    def _reconfigure_dependents(self) -> None:
        """Toggling CONF_SOLVE_ON_PRICE_CHANGE must (un)register the
        price-change state listener live, no hub reload -- the whole
        point of moving this out of the wizard. Deferred import to
        avoid a circular import between __init__ and switch.py. No-op
        for every other switch key so this stays a plain live toggle
        with zero side-effects.
        """
        if self._key != CONF_SOLVE_ON_PRICE_CHANGE:
            return
        from . import _configure_price_watcher

        _configure_price_watcher(self.hass, self._entry)
