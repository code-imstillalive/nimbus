"""nimbus issue #1540: a forecast is never given its own source sensor as a
feature.

A temperature signal whose source was also the hub's shared temperature
sensor trained with its own target as an input, and validated at a 0.03 degC
mean error four hours out: the model copied the input through. The battery,
grid and solar features already refused the subentry's own sensor;
temperature and humidity did not. These tests pin the rule for every feature
kind, and the generic one fails for any feature accessor added later without
the guard.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import const
from custom_components.nimbus_load.const import (
    CONF_BATTERY_SENSOR,
    CONF_CURTAILMENT_SENSOR,
    CONF_GRID_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)
from custom_components.nimbus_load.coordinator import NimbusCoordinator
from custom_components.nimbus_load.ml.features import FEATURE_NAMES

OWN = "sensor.archerfield_temp"
OTHER = "sensor.someone_else"

# (feature kind, hub option key, coordinator accessor)
FEATURES = [
    ("battery", CONF_BATTERY_SENSOR, "_battery_sensor"),
    ("grid", CONF_GRID_SENSOR, "_grid_sensor"),
    ("solar", CONF_SOLAR_SENSOR, "_solar_sensor"),
    ("temperature", CONF_TEMPERATURE_SENSOR, "_temp_sensor"),
    ("humidity", CONF_HUMIDITY_SENSOR, "_humidity_sensor"),
    ("temperature forecast", CONF_TEMPERATURE_FORECAST_SENSOR, "_temp_forecast_sensor"),
    ("curtailment", CONF_CURTAILMENT_SENSOR, "_curtailment_sensor"),
]
POWER_FEATURES = {"battery", "grid", "solar"}


def _coordinator(options: dict, subentry_type: str, load_sensor: str = OWN):
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = None  # no unit known: not energy
    coord.entry = SimpleNamespace(options=options)
    coord.subentry = SimpleNamespace(
        subentry_id="s1",
        subentry_type=subentry_type,
        data={CONF_LOAD_SENSOR: load_sensor},
    )
    return coord


@pytest.mark.parametrize("subentry_type", [SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL])
@pytest.mark.parametrize(("kind", "key", "accessor"), FEATURES)
def test_own_source_is_never_a_feature(kind, key, accessor, subentry_type):
    coord = _coordinator({key: OWN}, subentry_type)
    assert getattr(coord, accessor) is None, (kind, subentry_type)


@pytest.mark.parametrize(("kind", "key", "accessor"), FEATURES)
def test_another_sensor_is_still_used(kind, key, accessor):
    """Positive control: the guard drops only the subentry's own sensor."""
    coord = _coordinator({key: OTHER}, SUBENTRY_TYPE_LOAD)
    assert getattr(coord, accessor) == OTHER, kind
    signal = _coordinator({key: OTHER}, SUBENTRY_TYPE_SIGNAL)
    # Power signals drop battery/grid/solar entirely (an older, separate rule).
    expected = None if kind in POWER_FEATURES else OTHER
    assert getattr(signal, accessor) == expected, kind


def test_temperature_signal_gets_no_temperature_forecast_either():
    """At predict time the forecast stands in for the temperature feature, so
    a signal that trained without temperature must not be fed one -- the
    #1540 reference case: temperature_sensor is the signal's own source."""
    options = {
        CONF_TEMPERATURE_SENSOR: OWN,
        CONF_HUMIDITY_SENSOR: "sensor.archerfield_humidity",
        CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.pirateweather_hourly_forecast",
    }
    coord = _coordinator(options, SUBENTRY_TYPE_SIGNAL)
    assert coord._temp_sensor is None
    assert coord._temp_forecast_sensor is None
    assert coord._humidity_sensor == "sensor.archerfield_humidity"

    # A load on the same hub keeps all three.
    load = _coordinator(options, SUBENTRY_TYPE_LOAD, "sensor.pool_pump_power")
    assert load._temp_sensor == OWN
    assert load._temp_forecast_sensor == "sensor.pirateweather_hourly_forecast"
    assert load._humidity_sensor == "sensor.archerfield_humidity"


def test_humidity_signal_drops_only_humidity():
    options = {
        CONF_TEMPERATURE_SENSOR: "sensor.archerfield_temp",
        CONF_HUMIDITY_SENSOR: "sensor.archerfield_humidity",
        CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.pirateweather_hourly_forecast",
    }
    coord = _coordinator(options, SUBENTRY_TYPE_SIGNAL, "sensor.archerfield_humidity")
    assert coord._humidity_sensor is None
    assert coord._temp_sensor == "sensor.archerfield_temp"
    assert coord._temp_forecast_sensor == "sensor.pirateweather_hourly_forecast"


def _feature_accessors() -> list[str]:
    return sorted(
        name
        for name, value in vars(NimbusCoordinator).items()
        if isinstance(value, property)
        and name.endswith("_sensor")
        and name != "_load_sensor"
    )


def test_every_feature_accessor_is_covered_and_guarded():
    """Generic: every `*_sensor` accessor on the coordinator, including one
    added after this test, refuses the subentry's own sensor. Every hub
    option ending in `_sensor` is pointed at that sensor at once, so a new
    accessor reading a new option is caught without editing this file."""
    accessors = _feature_accessors()
    assert set(accessors) >= {a for _, _, a in FEATURES}
    sensor_keys = [
        v
        for k, v in vars(const).items()
        if k.startswith("CONF_") and isinstance(v, str) and v.endswith("_sensor")
    ]
    options = dict.fromkeys(sensor_keys, OWN)
    for subentry_type in (SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL):
        coord = _coordinator(options, subentry_type)
        for name in accessors:
            assert getattr(coord, name) is None, (name, subentry_type)


# --- a model persisted before the guard is replaced at startup -------------


def _model_with(column: str, values) -> SimpleNamespace:
    x = np.zeros((len(values), len(FEATURE_NAMES)))
    x[:, FEATURE_NAMES.index(column)] = values
    return SimpleNamespace(x_train=x)


def test_a_model_that_learned_from_its_own_source_is_flagged_for_retrain():
    coord = _coordinator({CONF_TEMPERATURE_SENSOR: OWN}, SUBENTRY_TYPE_SIGNAL)
    coord._trained = _model_with("temp_c", [-1.0, 0.0, 1.5])
    assert coord._trained_on_own_source() is True


def test_a_model_retrained_without_it_is_not_flagged_again():
    coord = _coordinator({CONF_TEMPERATURE_SENSOR: OWN}, SUBENTRY_TYPE_SIGNAL)
    coord._trained = _model_with("temp_c", [0.0, 0.0, 0.0])
    assert coord._trained_on_own_source() is False


def test_a_load_using_the_hub_temperature_is_not_flagged():
    coord = _coordinator(
        {CONF_TEMPERATURE_SENSOR: OWN}, SUBENTRY_TYPE_LOAD, "sensor.pool_pump_power"
    )
    coord._trained = _model_with("temp_c", [-1.0, 0.0, 1.5])
    assert coord._trained_on_own_source() is False


def test_no_model_or_an_unreadable_one_is_not_flagged():
    coord = _coordinator({CONF_HUMIDITY_SENSOR: OWN}, SUBENTRY_TYPE_SIGNAL)
    for trained in (None, MagicMock(), SimpleNamespace(x_train=np.zeros((0, 3)))):
        coord._trained = trained
        assert coord._trained_on_own_source() is False
