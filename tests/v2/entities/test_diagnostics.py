"""The downloadable diagnostics: one file that says what this install is
configured as and what every part last did (#1837).

Covers what lived in #1658 (the plan this install posted), the flex,
offer-curve and telemetry sections and the diagnostics files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

dg = importlib.import_module("custom_components.nimbus_load.diagnostics")
const = importlib.import_module("custom_components.nimbus_load.const")
shared = importlib.import_module("custom_components.nimbus_load.solver_shared")
flattened = importlib.import_module("custom_components.nimbus_load.sensor_flattened")


def _state(state, **attrs):
    return SimpleNamespace(state=state, attributes=attrs)


def _hass(states):
    return SimpleNamespace(states=SimpleNamespace(get=states.get))


@pytest.fixture
def registry(monkeypatch):
    er = sys.modules["homeassistant.helpers.entity_registry"]
    ids = {}
    monkeypatch.setattr(
        er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get_entity_id=lambda d, p, uid: ids.get(uid)
        ),
        raising=False,
    )
    monkeypatch.setattr(shared.NATIVE, "hass", None)
    return ids


@pytest.fixture
def run_state(monkeypatch):
    lrs = importlib.import_module("custom_components.nimbus_load.load_run_state")

    class Store:
        def __init__(self, store):
            pass

        async def async_read(self, subentry_id):
            return SimpleNamespace(
                to_dict=lambda: {"subentry": subentry_id, "commanded": "on"}
            )

    monkeypatch.setattr(lrs, "LoadRunStateStore", Store)
    monkeypatch.setattr(dg, "Store", lambda *a: None)


def _entry(subentries=None, runtime=None):
    return SimpleNamespace(
        entry_id="hub1",
        title="Nimbus",
        options={"solver_import_price_sensor": "sensor.price"},
        subentries=subentries or {},
        runtime_data=runtime or {},
    )


def test_a_fresh_install_says_each_part_is_not_configured_yet(registry):
    out = asyncio.run(dg.async_get_config_entry_diagnostics(_hass({}), _entry()))
    assert out["entry"] == {
        "title": "Nimbus",
        "options": {"solver_import_price_sensor": "sensor.price"},
    }
    assert out["subentries"] == []
    assert out["solver"] == {"configured": False}
    assert out["solver_config"] == {"configured": False}
    assert out["offer_curve"] == {"enabled": False}
    assert out["flex_telemetry"] == {"enabled": False}
    assert (
        out["flex"]["report_entity_found"] is False
        and out["flex"]["signals_entity_found"] is False
    )
    assert all(v is None for v in out["flex"]["signals"].values())


def test_every_part_of_a_running_install_is_reported(registry, run_state):
    forecast = [{"time": "a", "value": 1}, {"time": "b", "value": 2}]
    coord = SimpleNamespace(
        last_update_success=True,
        data={"mode": "learned", "trained_at": "t", "forecast": forecast},
    )
    subs = {
        "load1": SimpleNamespace(
            subentry_type=const.SUBENTRY_TYPE_LOAD, title="House", data={"k": 1}
        ),
        "cl1": SimpleNamespace(
            subentry_type=const.SUBENTRY_TYPE_CONTROLLABLE_LOAD, title="Pool", data={}
        ),
        "ev": SimpleNamespace(
            subentry_type=const.SUBENTRY_TYPE_BATTERY_PARTICIPANT,
            title="Car",
            data={
                const.CONF_BATTERY_PARTICIPANT_SOC_SENSOR: "sensor.car_soc",
                const.CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY: "binary_sensor.car_home",
                const.CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY: "number.car_limit",
            },
        ),
        "other": SimpleNamespace(subentry_type="signal", title="Temp", data={}),
    }
    spec = flattened.FLATTENED_ATTRS_FLEX[0]
    registry[f"hub1_nimbus_flex_{spec.entity_id_suffix}"] = "sensor.flex_one"
    states = {
        "sensor.car_soc": _state("55"),
        "binary_sensor.car_home": _state("on"),
        "number.car_limit": _state("90"),
        dg._SOLVER_ENTITY_ID: _state("1.5", forecast=[1], extra=True),
        dg._HOUSEHOLD_LOAD_ENTITY_ID: _state(
            "2", forecast=[2], failed_load_entities=["sensor.x"]
        ),
        dg._SOLVER_CONFIG_ENTITY_ID: _state("configured", region="QLD1"),
        dg._FLEX_REPORT_ENTITY_ID: _state("3", days=1),
        dg._FLEX_SIGNALS_ENTITY_ID: _state("0", **{spec.source_key: 5, "kept": 1}),
        "sensor.flex_one": _state("7"),
        dg._OFFER_CURVE_ENTITY_ID: _state("0", curve=[1]),
        dg._FLEX_TELEMETRY_ENTITY_ID: _state(
            "2026-09-20T06:00:00Z",
            record="no",
            clamped_fields=("p",),
            nimbus_version="v",
        ),
    }
    out = asyncio.run(
        dg.async_get_config_entry_diagnostics(
            _hass(states), _entry(subs, {"load1": coord})
        )
    )
    by_id = {s["subentry_id"]: s for s in out["subentries"]}
    assert by_id["load1"]["coordinator"]["forecast_point_count"] == 2
    assert (
        by_id["load1"]["coordinator"]["forecast_first_time"],
        by_id["load1"]["coordinator"]["forecast_last_time"],
    ) == ("a", "b")
    assert by_id["cl1"]["load_run_state"] == {"subentry": "cl1", "commanded": "on"}
    assert by_id["ev"]["live_resolution"] == {
        "soc_sensor": "sensor.car_soc",
        "soc_sensor_state": "55",
        "available_entity": "binary_sensor.car_home",
        "available_entity_state": "on",
        "available": True,
        "charge_limit_entity": "number.car_limit",
        "charge_limit_entity_state": "90",
    }
    assert set(by_id["other"]) == {"subentry_id", "subentry_type", "title", "config"}
    solver = out["solver"]
    assert (
        solver["plan_source"] == "state_machine"
        and solver["state"] == "1.5"
        and solver["extra"] is True
    )
    assert solver["household_load_forecast"] == [2] and solver[
        "load_failed_entities"
    ] == ["sensor.x"]
    assert out["solver_config"] == {
        "configured": True,
        "native_value": "configured",
        "region": "QLD1",
    }
    assert out["flex"]["signals"][spec.entity_id_suffix] == {
        "entity_id": "sensor.flex_one",
        "state": "7",
    }
    assert (
        out["flex"]["kept"] == 1 and spec.source_key not in out["flex"]
    )  # flattened values are not repeated
    assert out["offer_curve"] == {"enabled": True, "state": "0", "curve": [1]}
    assert out["flex_telemetry"]["record"] is None and out["flex_telemetry"][
        "clamped_fields"
    ] == ["p"]


def test_the_plan_this_install_posted_wins_over_the_state_machine(registry):
    hass = _hass({dg._SOLVER_ENTITY_ID: _state("9", forecast=["mirrored"])})
    shared.NATIVE.hass = hass
    shared._LAST_POSTED[dg._SOLVER_ENTITY_ID] = (1.0, {"forecast": ["own"]})
    try:
        solver = dg._solver_diagnostics(hass)
    finally:
        shared.NATIVE.hass = None
        shared._LAST_POSTED.pop(dg._SOLVER_ENTITY_ID, None)
    assert solver["plan_source"] == "this_install"
    assert (solver["state"], solver["forecast"]) == (1.0, ["own"])


def test_a_participant_that_is_away_or_has_no_entities_is_reported_as_such():
    away = dg._battery_participant_diagnostics(
        _hass({"binary_sensor.car_home": _state("off")}),
        {const.CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY: "binary_sensor.car_home"},
    )
    assert away["available"] is False
    bare = dg._battery_participant_diagnostics(_hass({}), {})
    assert bare["available"] is True and bare["soc_sensor_state"] is None
