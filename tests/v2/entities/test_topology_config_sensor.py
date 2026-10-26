"""`sensor.nimbus_topology_config`: what the Topology card draws -- each
inverter, PV string and battery (the home battery and every participant
apart), each controllable load, and the switchboard meters (#1837).

Covers what lived in #1531, #1702, the participant topology (EV apart from
the home battery) and the topology-config files.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sm = importlib.import_module("custom_components.nimbus_load.sensor")
const = importlib.import_module("custom_components.nimbus_load.const")


@pytest.fixture
def registry(monkeypatch):
    ids = {}
    monkeypatch.setattr(
        sm.er,
        "async_get",
        lambda hass: SimpleNamespace(
            async_get_entity_id=lambda domain, platform, uid: ids.get(uid)
        ),
    )
    return ids


def _sub(sid, kind, title="", **data):
    return SimpleNamespace(subentry_id=sid, subentry_type=kind, title=title, data=data)


def _sensor(options=None, subs=(), states=None):
    entry = SimpleNamespace(
        entry_id="hub1",
        options=options or {},
        subentries={s.subentry_id: s for s in subs},
    )
    s = sm.NimbusTopologyConfigSensor(entry, "1")
    s.hass = SimpleNamespace(states=SimpleNamespace(get=(states or {}).get))
    return s


CAR = _sub(
    "car",
    const.SUBENTRY_TYPE_BATTERY_PARTICIPANT,
    "Car",
    **{
        const.CONF_BATTERY_PARTICIPANT_NAME: "Car",
        const.CONF_BATTERY_PARTICIPANT_POWER_SENSOR: "sensor.car_w",
        const.CONF_BATTERY_PARTICIPANT_SOC_SENSOR: "sensor.car_soc",
        const.CONF_BATTERY_PARTICIPANT_CAPACITY_KWH: 60.0,
    },
)


def test_with_no_topology_set_up_the_home_system_is_drawn_from_the_solver_settings(
    registry,
):
    registry[f"hub1_{const.CONF_SOLVER_BATTERY_CAPACITY_KWH}"] = "number.cap"
    s = _sensor(
        {
            const.CONF_SOLVER_BATTERY_POWER_SENSOR: "sensor.bat_w",
            const.CONF_SOLVER_SOLAR_POWER_SENSOR: "sensor.pv_w",
            const.CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc",
        },
        [CAR],
        {"number.cap": SimpleNamespace(state="13.5")},
    )
    attrs = s.extra_state_attributes
    home, car = attrs["power_sources"]
    assert (
        home[const.CONF_POWER_SOURCE_NAME],
        home[const.CONF_POWER_SOURCE_BATTERY_SENSOR],
        home[const.CONF_POWER_SOURCE_DC_SENSOR],
    ) == ("Nimbus", "sensor.bat_w", "sensor.pv_w")
    assert [p[const.CONF_PV_STRING_ENTITY] for p in attrs["pv_strings"]] == [
        "sensor.pv_w"
    ]
    home_tower, car_tower = attrs["battery_towers"]
    # The EV is its own battery, never bundled with the home one.
    assert (
        home_tower["capacity_kwh"],
        home_tower[const.CONF_BATTERY_TOWER_SOC_SENSOR],
    ) == (13.5, "sensor.soc")
    assert (
        car_tower["title"],
        car_tower["capacity_kwh"],
        car_tower[const.CONF_BATTERY_TOWER_SOC_SENSOR],
    ) == ("Car", 60.0, "sensor.car_soc")
    assert (
        car_tower[const.CONF_BATTERY_TOWER_POWER_SOURCE]
        == car["subentry_id"]
        != home["subentry_id"]
    )
    assert s.native_value == 2


@pytest.mark.parametrize("capacity", [None, "unknown", "lots"])
def test_a_home_battery_without_a_readable_size_has_no_capacity(registry, capacity):
    states = (
        {}
        if capacity is None
        else {
            f"number.nimbus_{const.CONF_SOLVER_BATTERY_CAPACITY_KWH}": SimpleNamespace(
                state=capacity
            )
        }
    )
    s = _sensor(
        {
            const.CONF_SOLVER_BATTERY_POWER_SENSOR: "sensor.bat_w",
            const.CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.soc",
        },
        states=states,
    )
    (tower,) = s.extra_state_attributes["battery_towers"]
    assert tower["capacity_kwh"] is None
    assert s.extra_state_attributes["pv_strings"] == []


def test_participants_sharing_a_charger_are_drawn_on_it():
    van = _sub(
        "van",
        const.SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        **{const.CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP: "Garage"},
    )
    s = _sensor(subs=[van])
    (source,) = s.extra_state_attributes["power_sources"]
    (tower,) = s.extra_state_attributes["battery_towers"]
    assert source[const.CONF_POWER_SOURCE_NAME] == "Garage" and tower["title"] == "van"


def test_a_topology_set_up_in_the_wizard_is_used_as_is_plus_the_participants(registry):
    subs = [
        _sub(
            "inv",
            const.SUBENTRY_TYPE_POWER_SOURCE,
            "Inverter 1",
            **{const.CONF_POWER_SOURCE_NAME: "Inverter 1"},
        ),
        _sub(
            "east",
            const.SUBENTRY_TYPE_PV_STRING,
            "East",
            **{const.CONF_PV_STRING_ENTITY: "sensor.east"},
        ),
        _sub(
            "t1",
            const.SUBENTRY_TYPE_BATTERY_TOWER,
            "Tower 1",
            **{const.CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.t1"},
        ),
        CAR,
    ]
    s = _sensor({const.CONF_SOLVER_BATTERY_POWER_SENSOR: "sensor.not_drawn"}, subs)
    attrs = s.extra_state_attributes
    assert [p["subentry_id"] for p in attrs["power_sources"]] == ["inv", "derived_car"]
    assert [p["subentry_id"] for p in attrs["pv_strings"]] == ["east"]
    assert [t["title"] for t in attrs["battery_towers"]] == ["Tower 1", "Car"]


def test_controllable_loads_are_listed_by_name_with_their_own_sensors(registry):
    registry["hw_status"] = "sensor.nimbus_hot_water_status"
    subs = [
        _sub(
            "pool",
            const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
            "Pool",
            **{const.CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY: "switch.pool"},
        ),
        _sub(
            "hw",
            const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
            "x",
            **{const.CONF_CONTROLLABLE_LOAD_NAME: "Hot water"},
        ),
    ]
    loads = _sensor(subs=subs).extra_state_attributes["controllable_loads"]
    assert [c["name"] for c in loads] == ["Hot water", "Pool"]
    assert (
        loads[0]["status"] == "sensor.nimbus_hot_water_status"
        and loads[0]["commanded_state"] is None
    )
    assert loads[1]["device_entity"] == "switch.pool"
    unattached = _sensor(subs=subs)
    unattached.hass = None
    assert unattached._controllable_loads()[0]["status"] is None
