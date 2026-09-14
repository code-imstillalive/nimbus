"""Select platform for Nimbus -- the household mode.

nimbus issue #485 (Mark Purcell, Loads spec 9/10 of #476): "Specific
modes of operation for the electric household." Away for a week: no HWS
deadline, pool pump at a maintenance quota, pool heater off. Guests:
bigger HWS target, earlier deadline. Neither reference project has an
equivalent -- HAEO's "scenarios" are snapshot tests, EMHASS leaves it to
the caller's runtime params.

Exactly one entity today: `select.nimbus_household_mode`. Settable by
hand or by an automation (presence, calendar), read at solve time, so a
change takes effect on the next solve cycle with no reload.

**Deliberately not a wizard field.** Mark's own instruction on that issue
was verbatim: "Reuse existing pattern, don't use more wizard fields."
An earlier recap of the settled design reintroduced a per-load
`mode_overrides_enabled` flag; that was caught and dropped, because it
IS a wizard field -- the one thing that instruction most directly
rejected -- and because it is redundant: "no mode-suffixed entity set
for this field" is already a perfectly good off-switch, and #449
separately tracks that wizard as too large at 26 fields.

Same restore-and-seed-once + durable-Store-backstop pattern as
switch.py's NimbusSolverSwitch and number.py's NimbusSolverNumber -- see
number.py's own _SharedNumberStore docstring for the 2026-09-02 incident
that backstop exists to survive. A separate Store file from both, for
the same reason switch.py keeps its own: different value type,
independent platform setup, no shared object, and no overlapping keys to
race over.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import (
    CONF_HOUSEHOLD_MODE,
    DEFAULT_HOUSEHOLD_MODE,
    DOMAIN,
    HOUSEHOLD_MODES,
)

# Same reasoning as number.py/switch.py: a plain restored local value,
# no hub to overload by parallelizing.
PARALLEL_UPDATES = 0

_STORAGE_VERSION = 1


@dataclass
class _SharedSelectStore:
    """One Store + one lock shared by every NimbusSelect instance for a
    given config entry -- same reasoning as switch.py's own
    _SharedSwitchStore (all select keys live in one small JSON file, so
    writes must be serialized)."""

    store: Store[dict[str, Any]]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def async_read(self, key: str) -> tuple[str, float] | None:
        """(value, written_at), or None if this key was never written."""
        try:
            data = await self.store.async_load()
        except Exception:  # noqa: BLE001 -- a corrupt/unreadable store file
            # must never block this entity from falling through to its
            # own next fallback (restored state / default). It is a
            # durability BACKSTOP, not a required dependency -- same
            # posture as switch.py's own reader.
            return None
        if not data or key not in data:
            return None
        entry = data[key]
        try:
            return str(entry["value"]), float(entry["written_at"])
        except (TypeError, ValueError, KeyError):
            return None

    async def async_write(self, key: str, value: str) -> None:
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
    # Same independent sw_version read as number.py/switch.py/sensor.py
    # -- see number.py's own async_setup_entry for why this isn't passed
    # between platform modules.
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version) if integration.version else None
    shared_store = _SharedSelectStore(
        store=Store(hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_selects")
    )
    async_add_entities(
        [
            NimbusSelect(
                entry,
                CONF_HOUSEHOLD_MODE,
                "Household Mode",
                list(HOUSEHOLD_MODES),
                DEFAULT_HOUSEHOLD_MODE,
                sw_version,
                shared_store,
            ),
        ]
    )


class NimbusSelect(SelectEntity, RestoreEntity):
    """One live, dashboard-editable Nimbus choice. See this module's own
    docstring for why this is plain restored local state, never written
    back into entry.options."""

    _attr_has_entity_name = True
    # Same CONFIG marking as NimbusSolverSwitch/NimbusSolverNumber: this
    # is a setting a household chooses, not a reading or a diagnostic.
    _attr_entity_category = EntityCategory.CONFIG
    # nimbus issue #365 (Mark Purcell): no async_update() exists here --
    # state comes from real user selections and RestoreEntity on
    # startup, never from polling anything. Without this, HA's platform
    # 30s poll cadence calls a nonexistent update path every cycle.
    _attr_should_poll = False

    def __init__(
        self,
        entry: ConfigEntry,
        key: str,
        name: str,
        options: list[str],
        default: str,
        sw_version: str | None,
        shared_store: _SharedSelectStore,
    ) -> None:
        self._entry = entry
        self._key = key
        self._default = default
        self._shared_store = shared_store
        self._attr_options = options
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        # Fixed entity_id, same technique/reasoning as
        # NimbusSolverSwitch's own assignment -- one of these per hub, a
        # fixed predictable name is correct and external readers
        # (solver_writer's own bridge read) depend on it.
        self.entity_id = f"select.nimbus_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )
        self._attr_current_option = default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        restored_value: str | None = None
        if restored is not None and restored.state in self._attr_options:
            restored_value = restored.state

        # nimbus issue #342: same freshness compare as switch.py's and
        # number.py's own async_added_to_hass -- a restore-state dump can
        # genuinely be staler than this Store's own last write (the
        # not-fully-diagnosed HA-core startup timing race those modules
        # document), so the Store wins when it has a value and the
        # restore either missed or disagrees.
        stored = await self._shared_store.async_read(self._key)
        if stored is not None and stored[0] in self._attr_options:
            self._attr_current_option = stored[0]
        elif restored_value is not None:
            self._attr_current_option = restored_value
        else:
            self._attr_current_option = self._default

    async def async_select_option(self, option: str) -> None:
        # HA validates `option` against _attr_options before calling
        # this, but assert it anyway rather than persist something the
        # solve-time resolver could never match -- an unknown mode would
        # silently make every override unreachable, which is exactly the
        # failure shape this module's own docstring warns about.
        if option not in self._attr_options:
            raise ValueError(
                f"{option!r} is not a valid Nimbus household mode; "
                f"expected one of {self._attr_options}"
            )
        self._attr_current_option = option
        self.async_write_ha_state()
        await self._shared_store.async_write(self._key, option)
