"""Sensor platform for Nimbus.

One SensorEntity per "load" subentry (not per config entry -- the hub entry
can own many loads): native_value is the current predicted load (kW), and
the `forecast` attribute is a list of {"time": ..., "value": ...} points --
the same shape HAEO's own native forecast sensors already use
(custom_components/haeo/core/data/loader/extractors/haeo.py), so this
sensor can be wired directly into a HAEO Load element's forecast source
without any transformation.

Each entity is added with config_subentry_id set, which is what makes each
load show up as its own separate device in the HA UI -- e.g. HWS L1, HWS
L3, and Pool all independently visible (and independently able to show
`unavailable` if that one load's data goes bad), not folded into one
combined device.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_FORECAST,
    ATTR_MODEL_TRAINED_AT,
    ATTR_TRAINING_POINTS,
    CONF_LOAD_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_LOAD,
)
from .coordinator import NimbusCoordinator


def _object_id_from_source(load_sensor_entity_id: str) -> str:
    """Turn 'sensor.logger_load_power' into
    'nimbus_logger_load_power_forecast' -- a clean, predictable,
    source-derived slug, rather than letting Home Assistant auto-combine
    the device title and entity name into whatever it lands on (confirmed
    live 2026-08-14: produced
    sensor.load_sensor_logger_load_power_load_forecast, an unusable mess).

    The "nimbus_" prefix is deliberate, not decorative: confirmed live the
    same day that a bare "<source>_forecast" pattern collides with a
    completely unrelated pre-existing forecast sensor from a different
    integration (sensor.logger_load_power_forecast already existed) --
    without the prefix, Home Assistant would have silently registered
    Nimbus's own sensor under a "_2" suffix instead of erroring, exactly
    the kind of quiet collision this project has been bitten by before.
    The prefix also makes it unambiguous which forecast sensor is
    Nimbus's, given this project already runs several other forecasters
    in parallel (Solcast, Open-Meteo, ha_power_predictor, etc.).

    Naturally unique across every load Nimbus itself creates, since each
    one's source sensor is already unique.
    """
    object_id = load_sensor_entity_id.split(".", 1)[-1]
    return f"nimbus_{object_id}_forecast"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinators: dict[str, NimbusCoordinator] = hass.data[DOMAIN][entry.entry_id]
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_LOAD:
            continue
        coordinator = coordinators.get(subentry.subentry_id)
        if coordinator is None:
            continue
        async_add_entities(
            [NimbusForecastSensor(coordinator, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class NimbusForecastSensor(CoordinatorEntity[NimbusCoordinator], SensorEntity):
    """The published load forecast for one load subentry."""

    _attr_has_entity_name = True
    _attr_name = "Forecast"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT

    def __init__(self, coordinator: NimbusCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{subentry.subentry_id}_load_forecast"
        # Setting entity_id directly, not _attr_suggested_object_id.
        # Confirmed live 2026-08-14, twice, that _attr_suggested_object_id
        # is NOT respected here: with _attr_has_entity_name = True, Home
        # Assistant derives the entity_id from the device-name + entity-
        # name combination FIRST, and only falls back to suggested_object_id
        # after that -- so the "fix" silently never took effect, on either
        # the whole-house load or a genuinely brand-new one, and both had
        # to be renamed by hand. Setting entity_id directly is the one
        # mechanism the entity platform never overrides -- if it's already
        # set when the entity is added, generation is skipped entirely.
        self.entity_id = f"sensor.{_object_id_from_source(subentry.data[CONF_LOAD_SENSOR])}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
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
