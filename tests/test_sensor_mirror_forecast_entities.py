"""Real regression tests for issue #290 (Mark Purcell): sensor.nimbus_
mirror_temperature_forecast and sensor.nimbus_mirror_humidity_forecast
were both raw states.async_set() writes -- no unique_id, no device_info,
no device_class, no state_class, no unit_of_measurement on the entity --
every single push (every 5-minute solve cycle) fell through to the #85-
instrumented raw fallback, logging a WARNING every time purely because
these two entities had never been migrated onto real SensorEntity
classes, unlike every other push sensor in solver_writer.py.

Same shape as tests/test_sensor_solver_push_entities.py's own #55
regression coverage -- this file locks in the same properties for these
two, specifically:

1. Both classes are real SensorEntity subclasses attached to the shared
   Nimbus hub device (DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}))
   -- no dedicated sub-device, unlike the Family-A parents, since these
   are small, purely cosmetic dashboard mirrors.
2. Temperature carries device_class=TEMPERATURE, unit=°C; humidity
   carries device_class=HUMIDITY, unit=%.
3. Both preserve the well-known fixed entity_id (sensor.nimbus_mirror_
   temperature_forecast / sensor.nimbus_mirror_humidity_forecast) so
   history stays attached to the same names after migration.
4. register_entity_handler() + ha_post_state() route a native-mode write
   for these two entity_ids through the registered handler, not through
   states.async_set() -- the actual fix for #290's own WARNING spam.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor, solver_writer
from custom_components.nimbus_load.const import DOMAIN

# --- helpers ---------------------------------------------------------------


def _fake_entry(entry_id: str = "test-entry-mirror") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _construct(cls):
    """Same bypass-__init__-plumbing helper as test_sensor_solver_push_
    entities.py -- see that file's own docstring for why."""
    entry = _fake_entry()
    instance = cls.__new__(cls)
    cls.__init__(instance, entry, sw_version="0.94.28")
    return instance, entry


# --- class-attribute properties -------------------------------------------


def test_temperature_mirror_has_required_sensor_entity_class_attributes():
    cls = sensor.NimbusMirrorTemperatureForecastSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Mirror Temperature Forecast"
    assert cls._attr_device_class == sensor.SensorDeviceClass.TEMPERATURE
    assert cls._attr_native_unit_of_measurement == sensor.UnitOfTemperature.CELSIUS
    assert cls._attr_state_class == sensor.SensorStateClass.MEASUREMENT
    assert cls._unrecorded_attributes == frozenset({"forecast"})


def test_humidity_mirror_has_required_sensor_entity_class_attributes():
    cls = sensor.NimbusMirrorHumidityForecastSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Mirror Humidity Forecast"
    assert cls._attr_device_class == sensor.SensorDeviceClass.HUMIDITY
    assert cls._attr_native_unit_of_measurement == "%"
    assert cls._attr_state_class == sensor.SensorStateClass.MEASUREMENT
    assert cls._unrecorded_attributes == frozenset({"forecast"})


def test_temperature_mirror_entity_id_and_device_link():
    instance, entry = _construct(sensor.NimbusMirrorTemperatureForecastSensor)
    assert instance.entity_id == "sensor.nimbus_mirror_temperature_forecast"
    assert (
        instance._attr_unique_id
        == f"{entry.entry_id}_nimbus_mirror_temperature_forecast"
    )
    # Shared hub device, not a dedicated sub-device (unlike Family-A) --
    # confirms this deliberately stays on the same device page as
    # battery_forecast/household_load_forecast.
    assert instance._attr_device_info["identifiers"] == {(DOMAIN, entry.entry_id)}


def test_humidity_mirror_entity_id_and_device_link():
    instance, entry = _construct(sensor.NimbusMirrorHumidityForecastSensor)
    assert instance.entity_id == "sensor.nimbus_mirror_humidity_forecast"
    assert (
        instance._attr_unique_id == f"{entry.entry_id}_nimbus_mirror_humidity_forecast"
    )
    assert instance._attr_device_info["identifiers"] == {(DOMAIN, entry.entry_id)}


# --- dispatch-table routing (the actual #290 fix) --------------------------


def test_registered_mirror_handlers_route_through_the_handler_not_the_fallback():
    """The real regression this issue was filed for: ha_post_state() for
    these two entity_ids must route through the registered
    update_from_solver() handler, never fall through to the raw
    states.async_set() fallback (which is what produced the #290
    WARNING spam, ~288 lines/day for these two entities alone)."""
    temp_instance, _ = _construct(sensor.NimbusMirrorTemperatureForecastSensor)
    humidity_instance, _ = _construct(sensor.NimbusMirrorHumidityForecastSensor)
    temp_instance.hass = MagicMock()
    temp_instance.async_write_ha_state = MagicMock()
    humidity_instance.hass = MagicMock()
    humidity_instance.async_write_ha_state = MagicMock()

    fake_hass = MagicMock()
    fake_hass.add_job.side_effect = lambda fn: fn()
    solver_writer.register_entity_handler(
        "sensor.nimbus_mirror_temperature_forecast", temp_instance.update_from_solver
    )
    solver_writer.register_entity_handler(
        "sensor.nimbus_mirror_humidity_forecast", humidity_instance.update_from_solver
    )
    try:
        solver_writer.set_native_hass(fake_hass)
        solver_writer.ha_post_state(
            "sensor.nimbus_mirror_temperature_forecast", 21.5, {"forecast": []}
        )
        solver_writer.ha_post_state(
            "sensor.nimbus_mirror_humidity_forecast", 55.0, {"forecast": []}
        )
    finally:
        solver_writer.unregister_entity_handler(
            "sensor.nimbus_mirror_temperature_forecast"
        )
        solver_writer.unregister_entity_handler(
            "sensor.nimbus_mirror_humidity_forecast"
        )
        solver_writer.set_native_hass(None)

    # Routed through the real handler (update_from_solver), not the raw
    # states.async_set() fallback -- confirmed by the entity's own
    # stored state, not just a mock call count.
    assert temp_instance._state == 21.5
    assert humidity_instance._state == 55.0
    fake_hass.states.async_set.assert_not_called()


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
