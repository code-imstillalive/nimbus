"""A load's or signal's forecast sensor: named from its source, typed by its
role, and honest about a source that has gone away (#1837).

Covers what lived in #740, #1206, the signal-role and forecast-sensor files.
"""

from __future__ import annotations

import importlib
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sm = importlib.import_module("custom_components.nimbus_load.sensor")
const = importlib.import_module("custom_components.nimbus_load.const")


def _sensor(*, kind=None, role=None, data=None, source_state="1.2"):
    sub = SimpleNamespace(
        subentry_id="sub1",
        subentry_type=kind or const.SUBENTRY_TYPE_LOAD,
        title="Pool pump",
        data={
            const.CONF_LOAD_SENSOR: "sensor.pool_pump_power",
            **({const.CONF_SIGNAL_ROLE: role} if role else {}),
        },
    )
    coord = SimpleNamespace(last_update_success=True, data=data)
    s = sm.NimbusForecastSensor(coord, sub, "0.94.485")
    states = {
        "sensor.pool_pump_power": None
        if source_state is None
        else SimpleNamespace(state=source_state)
    }
    s.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    s._states = states
    s.writes = 0

    def write():
        s.writes += 1

    s.async_write_ha_state = write
    return s


def test_a_load_forecast_is_named_from_its_source_and_measured_in_kw():
    s = _sensor()
    assert s.entity_id == f"sensor.{sm.object_id_from_source('sensor.pool_pump_power')}"
    assert s.entity_id.endswith("_forecast")
    assert s._attr_unique_id == "sub1_load_forecast"
    assert s._attr_native_unit_of_measurement == "kW"
    assert s._attr_device_info["model"] == "Load Forecaster"
    assert s._attr_device_info["name"] == "Pool pump"


@pytest.mark.parametrize(
    ("role", "unit", "device_class"),
    [
        ("temperature", "°C", "TEMPERATURE"),
        ("humidity", "%", "HUMIDITY"),
        ("battery", "kW", "POWER"),
    ],
)
def test_a_signal_forecast_takes_its_roles_unit(role, unit, device_class):
    s = _sensor(kind=const.SUBENTRY_TYPE_SIGNAL, role=role)
    assert s._attr_unique_id == "sub1_signal_forecast"
    assert s._attr_device_info["model"] == "Power Signal Forecaster"
    assert s._attr_native_unit_of_measurement == unit
    assert s._attr_device_class is getattr(sm.SensorDeviceClass, device_class)


def test_the_forecast_and_its_provenance_are_published_as_given():
    now = datetime.now(UTC)
    forecast = [
        {"time": (now + timedelta(minutes=15 * i)).isoformat(), "value": 2.0}
        for i in range(8)
    ]
    data = {
        "state": 2.0,
        "forecast": forecast,
        "mode": "learned",
        "forecast_origin": "learned",
        "training_points": 900,
    }
    s = _sensor(data=data)
    attrs = s.extra_state_attributes
    assert s.native_value == 2.0
    assert attrs[const.ATTR_FORECAST] == forecast
    assert attrs[const.ATTR_TRAINING_POINTS] == 900
    assert attrs[const.ATTR_SOURCE_SENSOR] == "sensor.pool_pump_power"
    assert attrs[const.ATTR_MODE] == "learned"
    # A power forecast also carries its energy summary; a temperature does not.
    temp = _sensor(kind=const.SUBENTRY_TYPE_SIGNAL, role="temperature", data=data)
    summary = set(attrs) - set(temp.extra_state_attributes)
    assert summary and summary == set(sm.forecast_stats.energy_summary(forecast, now))


def test_before_any_data_the_sensor_is_empty_and_unavailable():
    s = _sensor(data=None)
    assert s.native_value is None and not s.available
    assert s.extra_state_attributes[const.ATTR_FORECAST] == []
    assert s.extra_state_attributes[const.ATTR_MODE] == "unscheduled"


def test_a_failed_update_is_unavailable():
    s = _sensor(data={"state": 1.0})
    s.coordinator.last_update_success = False
    assert not s.available


def test_a_source_gone_for_two_refreshes_makes_the_forecast_unavailable(caplog):
    s = _sensor(data={"state": 1.0})
    with caplog.at_level(logging.INFO, logger=sm._LOGGER.name):
        s._handle_coordinator_update()
        assert s.available
        s._states["sensor.pool_pump_power"] = SimpleNamespace(state="unavailable")
        s._handle_coordinator_update()
        assert s.available  # one blip is not an outage
        s._handle_coordinator_update()
        assert not s.available
        s._states["sensor.pool_pump_power"] = None
        s._handle_coordinator_update()
        s._states["sensor.pool_pump_power"] = SimpleNamespace(state="0.8")
        s._handle_coordinator_update()
    assert s.available and s.writes == 5
    messages = [r.getMessage() for r in caplog.records]
    assert sum("is now unavailable" in m for m in messages) == 1
    assert sum("available again" in m for m in messages) == 1


def test_both_recursive_validation_metrics_are_published():
    data = {
        "state": 1.0,
        "forecast": [],
        "validation_recursive_mae": {"knn": 0.2, "gbrt": 0.1, "naive": 0.3},
        "validation_recursive_mae_by_horizon": {"96": {"knn": 0.4}},
    }
    attrs = _sensor(data=data).extra_state_attributes
    assert (
        attrs[const.ATTR_VALIDATION_RECURSIVE_MAE] == data["validation_recursive_mae"]
    )
    assert (
        attrs[const.ATTR_VALIDATION_RECURSIVE_MAE_BY_HORIZON]
        == data["validation_recursive_mae_by_horizon"]
    )
