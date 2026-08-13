"""Sensor platform for Nimbus.

One SensorEntity per config entry: native_value is the current predicted
load (kW), and the `forecast` attribute is a list of {"time": ..., "value":
...} points -- the same shape HAEO's own native forecast sensors already
use (custom_components/haeo/core/data/loader/extractors/haeo.py), so this
sensor can be wired directly into a HAEO Load element's forecast source
without any transformation.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_FORECAST, ATTR_MODEL_TRAINED_AT, ATTR_TRAINING_POINTS, DOMAIN
from .coordinator import NimbusCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: NimbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NimbusForecastSensor(coordinator, entry)])


class NimbusForecastSensor(CoordinatorEntity[NimbusCoordinator], SensorEntity):
    """The published load forecast."""

    _attr_has_entity_name = True
    _attr_name = "Load Forecast"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT

    def __init__(self, coordinator: NimbusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_load_forecast"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Nimbus",
            model="Load Forecaster",
        )

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get("state") if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            ATTR_FORECAST: data.get("forecast", []),
            ATTR_MODEL_TRAINED_AT: data.get("trained_at"),
            ATTR_TRAINING_POINTS: data.get("training_points", 0),
        }
