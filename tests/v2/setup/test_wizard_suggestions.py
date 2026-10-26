"""What the wizard pre-fills: sensors read from the Energy Dashboard (only
when their current state can be what the field needs), the one sensor of a
kind when there is exactly one, and the Nimbus forecasts a Solver load field
may use (#1837).

Covers what lived in #1067 (suggestions reach their form), #1589 (unified
Energy Dashboard grid), the battery-tower SoC suggestion and the
load-forecast candidate files.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

ho = importlib.import_module("custom_components.nimbus_load.flows.hub_options")
bt = importlib.import_module(
    "custom_components.nimbus_load.flows.battery_tower_subentry"
)
const = importlib.import_module("custom_components.nimbus_load.const")


def _state(entity_id, state="1.0", **attrs):
    return SimpleNamespace(entity_id=entity_id, state=state, attributes=attrs)


def _hass(*states):
    by_id = {s.entity_id: s for s in states}
    return SimpleNamespace(
        states=SimpleNamespace(
            get=by_id.get, async_all=lambda domain=None: list(states)
        )
    )


@pytest.fixture
def dashboard(monkeypatch):
    prefs = {"energy_sources": []}

    async def manager(hass):
        if prefs.get("broken"):
            raise RuntimeError("no energy dashboard")
        return SimpleNamespace(data=prefs)

    data = types.ModuleType("homeassistant.components.energy.data")
    data.async_get_manager = manager
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.energy",
        types.ModuleType("homeassistant.components.energy"),
    )
    monkeypatch.setitem(sys.modules, "homeassistant.components.energy.data", data)
    return prefs


ENERGY = {"device_class": "energy", "state_class": "total_increasing"}


def test_the_solver_pages_take_the_dashboards_battery_soc_and_grid_prices(dashboard):
    # test_the_solver_pages_take_the_dashboards_battery_soc_and_grid_prices
    dashboard["energy_sources"] = [
        {"type": "battery", "stat_soc": "sensor.soc"},
        {
            "type": "grid",
            "stat_energy_from": "sensor.imp",
            "entity_energy_price": "sensor.buy",
            "stat_energy_to": "sensor.exp",
            "entity_energy_price_export": "sensor.sell",
        },
    ]
    hass = _hass(
        _state("sensor.soc", "55", device_class="battery", unit_of_measurement="%"),
        _state("sensor.buy", "0.25"),
        _state("sensor.sell", "unavailable"),
    )
    got = asyncio.run(ho._energy_dashboard_solver_source_suggestions(hass))
    assert got == {
        const.CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc",
        const.CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.buy",
    }

    # test_a_soc_that_is_not_a_battery_percentage_is_not_suggested
    dashboard["energy_sources"] = [
        {"type": "battery", "stat_soc": "sensor.soc"},
        "junk",
    ]
    assert (
        asyncio.run(
            ho._energy_dashboard_solver_source_suggestions(
                _hass(_state("sensor.soc", "55", unit_of_measurement="kWh"))
            )
        )
        == {}
    )

    # test_the_switchboard_takes_energy_totals_and_monetary_prices_only
    dashboard["energy_sources"] = [
        {
            "type": "grid",
            "stat_energy_from": "sensor.imp",
            "entity_energy_price": "sensor.buy",
            "stat_energy_to": "sensor.exp",
            "entity_energy_price_export": "sensor.sell",
        },
        {"type": "solar", "stat_energy_from": "sensor.pv"},
        {
            "type": "battery",
            "stat_energy_from": "sensor.out",
            "stat_energy_to": "sensor.in",
        },
    ]
    hass = _hass(
        _state("sensor.imp", **ENERGY),
        _state("sensor.exp", device_class="energy", state_class="measurement"),
        _state("sensor.buy", device_class="monetary"),
        _state("sensor.sell", device_class="power"),
        _state("sensor.pv", **ENERGY),
        _state("sensor.out", **ENERGY),
        _state("sensor.in", **ENERGY),
    )
    assert asyncio.run(ho._energy_dashboard_switchboard_suggestions(hass)) == {
        const.CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR: "sensor.imp",
        const.CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR: "sensor.buy",
        const.CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR: "sensor.pv",
        const.CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR: "sensor.out",
        const.CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR: "sensor.in",
    }

    # test_without_an_energy_dashboard_the_forms_are_simply_empty
    dashboard["broken"] = True
    assert asyncio.run(ho._energy_dashboard_solver_source_suggestions(_hass())) == {}
    assert asyncio.run(ho._energy_dashboard_switchboard_suggestions(_hass())) == {}

    # test_the_one_sensor_of_a_kind_is_suggested_and_two_are_left_to_the_household
    # test_the_one_sensor_of_a_kind_is_suggested_and_two_are_left_to_the_household
    one = _hass(
        _state("sensor.t", device_class="temperature"),
        _state("sensor.h", device_class="humidity"),
        _state("sensor.b", device_class="battery"),
    )
    assert ho._type_safe_entity_suggestions(one) == {
        const.CONF_TEMPERATURE_SENSOR: "sensor.t",
        const.CONF_HUMIDITY_SENSOR: "sensor.h",
        const.CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.b",
    }
    two = _hass(
        _state("sensor.t1", device_class="temperature"),
        _state("sensor.t2", device_class="temperature"),
    )
    assert ho._type_safe_entity_suggestions(two) == {}
    broken = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: 1 / 0))
    assert ho._type_safe_entity_suggestions(broken) == {}

    # test_a_solver_load_field_offers_only_nimbus_load_forecasts
    hass = _hass(
        _state(
            "sensor.pool_fc", **{const.ATTR_SUBENTRY_TYPE: const.SUBENTRY_TYPE_LOAD}
        ),
        _state(
            "sensor.house_fc",
            **{
                const.ATTR_SUBENTRY_TYPE: const.SUBENTRY_TYPE_SIGNAL,
                const.ATTR_SIGNAL_ROLE: const.SIGNAL_ROLE_OTHER,
            },
        ),
        _state(
            "sensor.bat_fc",
            **{
                const.ATTR_SUBENTRY_TYPE: const.SUBENTRY_TYPE_SIGNAL,
                const.ATTR_SIGNAL_ROLE: const.SIGNAL_ROLE_BATTERY,
            },
        ),
        _state("sensor.other"),
    )
    single, summable = ho._discover_nimbus_load_forecast_candidates(hass)
    assert single == ["sensor.pool_fc", "sensor.house_fc"]
    assert summable == ["sensor.pool_fc"]
    broken = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: 1 / 0))
    assert ho._discover_nimbus_load_forecast_candidates(broken) == ([], [])


def test_a_battery_tower_soc_is_suggested_from_the_dashboard(dashboard):
    dashboard["energy_sources"] = [{"type": "battery", "stat_soc": "sensor.tower_soc"}]
    hass = _hass(
        _state(
            "sensor.tower_soc", "60", device_class="battery", unit_of_measurement="%"
        )
    )
    assert asyncio.run(bt._energy_dashboard_soc_suggestion(hass)) == "sensor.tower_soc"
    dashboard["broken"] = True
    assert asyncio.run(bt._energy_dashboard_soc_suggestion(hass)) is None
