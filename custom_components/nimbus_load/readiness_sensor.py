"""`sensor.nimbus_device_readiness` (DC-006 slice 1b, nimbus #1574, DC-R14).

State: how many of the four functions (observe, forecast, plan, score) are
ready. Attributes: each function's result from `device_readiness.evaluate`
(state, missing roles with what would satisfy each, data that is stale or
unavailable, the reason), plus the device setup's mode and revision.
Refreshed every five minutes; reads only, writes nothing.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN

_REFRESH = timedelta(minutes=5)


class NimbusDeviceReadinessSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:list-status"
    # Every function's detail is an attribute; history needs only the count.
    _unrecorded_attributes = frozenset({"functions"})

    def __init__(self, entry: Any, sw_version: str | None) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_device_readiness"
        self.entity_id = "sensor.nimbus_device_readiness"
        self._attr_name = "Device Readiness"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self._async_refresh()
        self.async_write_ha_state()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, _REFRESH)
        )

    async def _async_tick(self, *_: Any) -> None:
        await self._async_refresh()
        self.async_write_ha_state()

    async def _async_refresh(self) -> None:
        from . import device_contract_ha

        result = await device_contract_ha.async_readiness(self.hass, self._entry)
        self._attr_native_value = result["ready"]
        self._attr_extra_state_attributes = {
            "of": 4,
            "mode": result["mode"],
            "revision": result["revision"],
            **{
                f"{name}_state": r.get("state")
                for name, r in result["functions"].items()
            },
            "functions": result["functions"],
        }
