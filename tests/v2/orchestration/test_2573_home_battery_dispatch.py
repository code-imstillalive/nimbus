"""With a car or second battery as a participant, the house inverter's dispatch
follows the HOME battery's own plan, not the fleet total (#2573).

`sensor.nimbus_solver_battery_forecast`'s state stays the fleet aggregate;
`home_battery_kw` carries the home battery's own period-0 power, the dispatch
signals (hold, solar charge, absorb surplus) are worked out from it, and each
battery gets its own planned-power sensor (Mark Purcell's review: one sensor
per battery, as the SoC sensors).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from custom_components.nimbus_load import solver_publish

bs = importlib.import_module("custom_components.nimbus_load.sensor_battery_soc")
const = importlib.import_module("custom_components.nimbus_load.const")


def _battery(name, charge, discharge):
    return SimpleNamespace(
        name=name,
        charge_kw=np.asarray(charge, dtype=np.float64),
        discharge_kw=np.asarray(discharge, dtype=np.float64),
        soc_kwh=np.zeros(len(charge)),
    )


def test_the_home_battery_is_the_fleet_less_its_participants():
    # Synthetic: the car charges 7 kW while the home battery holds, then the
    # home battery discharges 3 kW while the car is idle.
    plan = SimpleNamespace(
        batteries=[
            _battery("home", [0.0, 0.0], [0.0, 3.0]),
            _battery("Car", [7.0, 0.0], [0.0, 0.0]),
        ]
    )
    fleet = np.array([-7.0, 3.0])
    assert list(solver_publish.home_battery_net_kw(fleet, plan)) == [0.0, 3.0]

    # A single-battery site: exactly the fleet figure, whatever the list holds.
    for single in (
        SimpleNamespace(batteries=[]),
        SimpleNamespace(batteries=[_battery("home", [2.0, 0.0], [0.0, 1.0])]),
        SimpleNamespace(),
    ):
        assert list(solver_publish.home_battery_net_kw(fleet, single)) == [
            -7.0,
            3.0,
        ]

    # The car charging from the grid while the home battery is idle is a
    # hold for the house inverter; on the fleet figure it read as a charge.
    hold, solar = solver_publish.dispatch_flags(
        home_kw=0.0, grid_import_kw=7.5, grid_export_kw=0.0, grid_to_battery_kw=7.0
    )
    assert (hold, solar) == (True, False)
    hold, _ = solver_publish.dispatch_flags(
        home_kw=-7.0, grid_import_kw=7.5, grid_export_kw=0.0, grid_to_battery_kw=7.0
    )
    assert hold is False


def _participant(sid, name):
    return SimpleNamespace(
        subentry_id=sid,
        subentry_type=const.SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        data={const.CONF_BATTERY_PARTICIPANT_NAME: name},
    )


def test_one_planned_power_sensor_per_battery():
    entry = SimpleNamespace(entry_id="hub1", subentries={"a": _participant("a", "Car")})
    home, car = bs.create_power(entry, "1")
    assert home.entity_id == "sensor.nimbus_solver_battery_0_kw"
    assert car.entity_id == "sensor.nimbus_solver_battery_1_kw"
    for s in (home, car):
        s.hass = None
    parent = {
        "home_battery_kw": 0.0,
        "batteries": [
            {"name": "home", "forecast": [{"charge_kw": 0.0, "discharge_kw": 0.0}]},
            {"name": "Car", "forecast": [{"charge_kw": 7.0, "discharge_kw": 0.0}]},
        ],
    }
    home.update_from_parent({}, parent_attributes=parent)
    car.update_from_parent({}, parent_attributes=parent)
    assert home.native_value == 0.0
    assert car.native_value == -7.0
    assert car.extra_state_attributes["battery_name"] == "Car"

    # No participant: no per-battery power sensors at all.
    assert bs.create_power(SimpleNamespace(entry_id="h", subentries={}), "1") == []
