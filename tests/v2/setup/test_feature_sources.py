"""A forecast is never given its own source sensor as a feature (#1540)
(#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
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


def _feature_accessors() -> list[str]:
    return sorted(
        name
        for name, value in vars(NimbusCoordinator).items()
        if isinstance(value, property)
        and name.endswith("_sensor")
        and name != "_load_sensor"
    )


def test_every_feature_accessor_is_covered_and_guarded():
    # test_every_feature_accessor_is_covered_and_guarded: Generic: every `*_sensor` accessor on the coordinator, including one
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

    # test_a_model_that_learned_from_its_own_source_is_flagged_for_retrain
    coord = _coordinator({CONF_TEMPERATURE_SENSOR: OWN}, SUBENTRY_TYPE_SIGNAL)
    coord._trained = _model_with("temp_c", [-1.0, 0.0, 1.5])
    assert coord._trained_on_own_source() is True

    # test_a_model_retrained_without_it_is_not_flagged_again
    coord = _coordinator({CONF_TEMPERATURE_SENSOR: OWN}, SUBENTRY_TYPE_SIGNAL)
    coord._trained = _model_with("temp_c", [0.0, 0.0, 0.0])
    assert coord._trained_on_own_source() is False

    # test_no_model_or_an_unreadable_one_is_not_flagged
    coord = _coordinator({CONF_HUMIDITY_SENSOR: OWN}, SUBENTRY_TYPE_SIGNAL)
    for trained in (None, MagicMock(), SimpleNamespace(x_train=np.zeros((0, 3)))):
        coord._trained = trained
        assert coord._trained_on_own_source() is False


# --- a model persisted before the guard is replaced at startup -------------


def _model_with(column: str, values) -> SimpleNamespace:
    x = np.zeros((len(values), len(FEATURE_NAMES)))
    x[:, FEATURE_NAMES.index(column)] = values
    return SimpleNamespace(x_train=x)
