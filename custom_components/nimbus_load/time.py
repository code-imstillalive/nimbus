"""Time platform for Nimbus -- Solver settings that are a time of day.

Household, 11 Oct 2026: the battery reserve's window read "Hold from 0 h,
Release at 5.5 h" -- a number box cannot say 05:30. These are now HA time
entities, shown and picked as a clock time (00:00 -> 05:30).

The Solver still receives hours as a float (5.5), so nothing downstream
changes: `sensor.py`'s config bridge converts the time entity's "HH:MM:SS"
state, exactly as it reads a number entity's state.

Same durability layers as number.py's NimbusSolverNumber, in the same
order: the entity's own restored state, then a small Store file, then the
config entry's options, then the default. On first start after this change
there is no restored time state yet, so the value comes from the number
Store the old number entity wrote on every edit -- the household's saved
window carries over -- and the old number entity is removed from the
registry so the two never coexist.
"""

from __future__ import annotations

import time as _time
from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import (
    CONF_SOLVER_BATTERY_RESERVE_END_HOUR,
    CONF_SOLVER_BATTERY_RESERVE_START_HOUR,
    CONF_SOLVER_CHARGE_BY_HOUR,
    CONF_SOLVER_P2P_BLOCK_1_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_1_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_2_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_2_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_3_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_3_START_HOUR,
    DEFAULT_SOLVER_CHARGE_BY_HOUR,
    DOMAIN,
)

PARALLEL_UPDATES = 0

_STORAGE_VERSION = 1

# (key, name). The key is also the Solver config key, the unique-id suffix
# and the old number entity's key, so its saved value can be carried over.
TIME_SETTINGS: tuple[tuple[str, str], ...] = (
    # Household, 14 Oct 2026: "Charge By" sits with "Charge To" (number.py),
    # beside Battery Max SoC. See const.py.
    (CONF_SOLVER_CHARGE_BY_HOUR, "Charge By"),
    (CONF_SOLVER_BATTERY_RESERVE_START_HOUR, "Battery Reserve From"),
    (CONF_SOLVER_BATTERY_RESERVE_END_HOUR, "Battery Reserve Until"),
    # Household, 12 Oct 2026 (asked for before, #565): P2P blocks to the
    # minute, e.g. 20:30. A block ending at midnight shows as 00:00.
    (CONF_SOLVER_P2P_BLOCK_1_START_HOUR, "P2P Block 1 From"),
    (CONF_SOLVER_P2P_BLOCK_1_END_HOUR, "P2P Block 1 Until"),
    (CONF_SOLVER_P2P_BLOCK_2_START_HOUR, "P2P Block 2 From"),
    (CONF_SOLVER_P2P_BLOCK_2_END_HOUR, "P2P Block 2 Until"),
    (CONF_SOLVER_P2P_BLOCK_3_START_HOUR, "P2P Block 3 From"),
    (CONF_SOLVER_P2P_BLOCK_3_END_HOUR, "P2P Block 3 Until"),
)


# A setting whose first value is not midnight. Every other time setting
# starts at 00:00, which for them means "unset".
TIME_DEFAULTS: dict[str, float] = {
    CONF_SOLVER_CHARGE_BY_HOUR: DEFAULT_SOLVER_CHARGE_BY_HOUR,
}


def hours_to_time(hours: float) -> time:
    """5.5 -> 05:30. 24 (end of day) is shown as 00:00."""
    minutes = round(float(hours) * 60) % (24 * 60)
    return time(minutes // 60, minutes % 60)


def time_to_hours(value: time) -> float:
    """05:30 -> 5.5."""
    return value.hour + value.minute / 60


def state_to_hours(state: str) -> float | None:
    """A time entity's state ("05:30:00") as hours, None if unreadable."""
    try:
        return time_to_hours(time.fromisoformat(state))
    except (TypeError, ValueError):
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version) if integration.version else None
    own_store: Store[dict[str, Any]] = Store(
        hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_solver_times"
    )
    number_store: Store[dict[str, Any]] = Store(
        hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_solver_numbers"
    )
    registry = er.async_get(hass)
    for key, _name in TIME_SETTINGS:
        old = registry.async_get_entity_id("number", DOMAIN, f"{entry.entry_id}_{key}")
        if old is not None:
            registry.async_remove(old)
    async_add_entities(
        NimbusSolverTime(entry, key, name, sw_version, own_store, number_store)
        for key, name in TIME_SETTINGS
    )


class NimbusSolverTime(RestoreEntity, TimeEntity):
    """One time-of-day Solver setting, edited as a clock time."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        key: str,
        name: str,
        sw_version: str | None,
        own_store: Store[dict[str, Any]],
        number_store: Store[dict[str, Any]],
    ) -> None:
        self._entry = entry
        self._key = key
        self._own_store = own_store
        self._number_store = number_store
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self.entity_id = f"time.nimbus_{key}"
        self._attr_name = name
        self._attr_native_value = hours_to_time(TIME_DEFAULTS.get(key, 0.0))
        # The P2P block times sit on the "Nimbus P2P" sub-device beside the
        # block rates (number.py, nimbus #465), where they always were.
        p2p = key.startswith("solver_p2p_")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_p2p" if p2p else entry.entry_id)},
            name="Nimbus P2P" if p2p else "Nimbus",
            manufacturer="Nimbus",
            model="Sub-device" if p2p else "Hub",
            sw_version=sw_version,
        )

    async def _read_hours(self, store: Store[dict[str, Any]]) -> float | None:
        try:
            data = await store.async_load()
        except Exception:  # noqa: BLE001 -- a durability backstop only
            return None
        entry = (data or {}).get(self._key)
        if isinstance(entry, dict):
            entry = entry.get("value")
        try:
            return None if entry is None else float(entry)
        except (TypeError, ValueError):
            return None

    async def _write_hours(self, hours: float) -> None:
        try:
            data = await self._own_store.async_load() or {}
        except Exception:  # noqa: BLE001
            data = {}
        data[self._key] = {"value": hours, "written_at": _time.time()}
        await self._own_store.async_save(data)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and (hours := state_to_hours(last.state)) is not None:
            self._attr_native_value = hours_to_time(hours)
            return
        for store in (self._own_store, self._number_store):
            hours = await self._read_hours(store)
            if hours is not None:
                self._attr_native_value = hours_to_time(hours)
                await self._write_hours(hours)
                return
        seeded = self._entry.options.get(self._key)
        if seeded is not None:
            try:
                self._attr_native_value = hours_to_time(float(seeded))
            except (TypeError, ValueError):
                return

    async def async_set_value(self, value: time) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        await self._write_hours(time_to_hours(value))
