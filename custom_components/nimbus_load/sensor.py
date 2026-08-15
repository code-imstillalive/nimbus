"""Sensor platform for Nimbus.

One SensorEntity per "load" or "power_signal" subentry (not per config
entry -- the hub entry can own many of either): native_value is the
current predicted power (kW), and the `forecast` attribute is a list of
{"time": ..., "value": ...} points -- a generic, self-describing shape,
not tied to any specific downstream consumer.

Each entity is added with config_subentry_id set, which is what makes each
load/signal show up as its own separate device in the HA UI -- e.g. HWS L1,
HWS L3, Pool, and (2026-08-15) Battery/Solar/Grid all independently
visible (and independently able to show `unavailable` if that one's data
goes bad), not folded into one combined device.
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
    ATTR_MODE,
    ATTR_MODEL_TRAINED_AT,
    ATTR_SUBENTRY_TYPE,
    ATTR_TRAINING_POINTS,
    ATTR_VALIDATION_MAE,
    ATTR_VALIDATION_MASE,
    CONF_LOAD_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)
from .coordinator import NimbusCoordinator

_FORECASTABLE_SUBENTRY_TYPES = (SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL)


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
        if subentry.subentry_type not in _FORECASTABLE_SUBENTRY_TYPES:
            continue
        coordinator = coordinators.get(subentry.subentry_id)
        if coordinator is None:
            continue
        async_add_entities(
            [NimbusForecastSensor(coordinator, subentry)],
            config_subentry_id=subentry.subentry_id,
        )


class NimbusForecastSensor(CoordinatorEntity[NimbusCoordinator], SensorEntity):
    """The published forecast for one load or power-signal subentry."""

    _attr_has_entity_name = True
    _attr_name = "Forecast"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    # Confirmed live 2026-08-15: without this, HA's own history-graph
    # tooltips (and any UI that computes a rolling average across
    # several already-rounded points, e.g. "5-minute aggregated") show
    # raw binary floating-point noise -- "0.152000000000000020" instead
    # of "0.152". round(v, 3) in the coordinator only cleans up the
    # value AT THE MOMENT it's published; averaging several such values
    # together elsewhere reintroduces the noise, since 0.152 has no
    # exact binary representation to begin with. This tells every HA
    # frontend surface (not just this one) to always DISPLAY at most 3
    # decimal places, regardless of what the underlying float actually
    # is -- fixes the display everywhere at once instead of chasing
    # every individual UI that might recompute an average.
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: NimbusCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator)
        # Deliberately NOT changed to a generic suffix for existing load
        # subentries -- an already-deployed entity's unique_id must never
        # change, or Home Assistant treats it as a brand new entity and
        # orphans the old one (losing its history/registry entry). Signal
        # subentries are new as of this same change, so they get their
        # own distinct, accurate suffix from day one instead.
        suffix = "_signal_forecast" if subentry.subentry_type == SUBENTRY_TYPE_SIGNAL else "_load_forecast"
        self._attr_unique_id = f"{subentry.subentry_id}{suffix}"
        # Exposed as a live attribute (2026-08-15) so anything downstream
        # (e.g. a dashboard chart script) can tell a load forecast apart
        # from a power-signal forecast generically -- by reading this
        # attribute at runtime, not by hardcoding entity names. Same
        # design principle already applied to ATTR_MODE.
        self._subentry_type = subentry.subentry_type
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
        model = (
            "Power Signal Forecaster"
            if subentry.subentry_type == SUBENTRY_TYPE_SIGNAL
            else "Load Forecaster"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Nimbus",
            model=model,
        )

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get("state") if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            ATTR_FORECAST: data.get("forecast", []),
            ATTR_MODE: data.get("mode", "unscheduled"),
            ATTR_MODEL_TRAINED_AT: data.get("trained_at"),
            ATTR_TRAINING_POINTS: data.get("training_points", 0),
            ATTR_VALIDATION_MAE: data.get("validation_mae", {}),
            ATTR_VALIDATION_MASE: data.get("validation_mase", {}),
            ATTR_SUBENTRY_TYPE: self._subentry_type,
        }
