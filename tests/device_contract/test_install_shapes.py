"""Every known install shape goes through the device import whole (#2394).

A fix checked on one install's shape of data can miss another: on 5 Nov
0.94.520's circuit-name fix reached controllable loads, the test box's
shape, but not a tester's circuits, which are power signals (#2285). Mark
Purcell asked for the fix to be checked on every known shape in CI.

Three shapes, structure only, generic entity ids, no names or readings:

- **test box:** controllable loads, and plain power signals on sensors the
  hub already reads (its battery and grid meters);
- **reference household (generic):** circuits as Loads with run-hold and
  schedule settings, temperature and humidity signals, inverters, PV
  strings and battery towers;
- **Mark Purcell's install:** one hybrid system, circuits as power signals,
  a second whole-house meter carrying the Whole house role, two EVs as
  battery participants, and one other Load.

For each: the import reports no problem, drops no subentry (each is mapped
or kept with a reason), never draws a whole-house meter as a circuit, and
its settings view writes nothing back over today's settings.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "nimbus_load"
sys.path.insert(0, str(_PKG))  # device_import's fallback `import device_contract`


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("device_contract")
di = _load("device_import")

_HUB_OPTIONS = {
    "grid_sensor": "sensor.grid_power",
    "solver_import_price_sensor": "sensor.buy",
    "solver_export_price_sensor": "sensor.sell",
    "solver_battery_soc_sensor": "sensor.bat_soc",
    "solver_battery_power_sensor": "sensor.bat_power",
    "battery_sensor": "sensor.bat_power",
    "solver_battery_power_positive_is_charge": True,
    "solar_sensor": "sensor.pv_power",
    "solver_solar_forecast_sensor": "sensor.pv_forecast",
    "solver_load_forecast_sensor": "sensor.house_forecast",
    "solver_whole_house_cross_check_sensor": "sensor.house_power",
}
_HUB_NUMBERS = {
    "solver_battery_capacity_kwh": 40.0,
    "solver_battery_min_soc_percent": 5.0,
    "solver_battery_max_soc_percent": 100.0,
    "solver_max_charge_kw": 10.0,
    "solver_max_discharge_kw": 10.0,
    "solver_grid_max_import_kw": 20.0,
    "solver_grid_max_export_kw": 10.0,
}


def _sub(sid: str, kind: str, title: str, **data: Any) -> dict[str, Any]:
    return {"subentry_id": sid, "subentry_type": kind, "title": title, "data": data}


def _circuit_load(sid: str, n: int) -> dict[str, Any]:
    return _sub(
        sid,
        "load",
        f"Circuit {n}",
        load_sensor=f"sensor.circuit_{n}_power",
        expected_load_kw=1.5,
        running_threshold_kw=0.2,
        run_hold_hours=2.0,
        run_hold_learned_when_off=True,
        schedule_start_hour=9,
        schedule_end_hour=15,
    )


def _signal(sid: str, title: str, sensor: str, role: str) -> dict[str, Any]:
    return _sub(sid, "power_signal", title, load_sensor=sensor, signal_role=role)


TEST_BOX = [
    _sub(
        "tb1",
        "controllable_load",
        "Hot water",
        controllable_load_name="Hot water",
        controllable_load_kind="deferrable",
        controllable_load_power_sensor="sensor.hot_water_power",
        controllable_load_device_entity="switch.hot_water",
    ),
    _sub(
        "tb2",
        "controllable_load",
        "Pool pump",
        controllable_load_name="Pool pump",
        controllable_load_kind="sheddable",
        controllable_load_power_sensor="sensor.pool_power",
        controllable_load_device_entity="switch.pool_pump",
    ),
    _signal("tb3", "Battery meter", "sensor.bat_power", "other"),
    _signal("tb4", "Grid meter", "sensor.grid_power", "other"),
]

REFERENCE_HOUSEHOLD = [
    *(_circuit_load(f"rh{n}", n) for n in range(1, 5)),
    _signal("rs1", "Outdoor temperature", "sensor.outdoor_temp", "temperature"),
    _signal("rs2", "Outdoor humidity", "sensor.outdoor_humidity", "humidity"),
    _signal("rs3", "Battery power", "sensor.bat_power", "other"),
    *(
        _sub(
            f"ps{n}",
            "power_source",
            f"Inverter {n}",
            power_source_name=f"Inverter {n}",
            power_source_dc_sensor=f"sensor.inverter_{n}_dc",
            power_source_battery_sensor=f"sensor.inverter_{n}_battery",
        )
        for n in (1, 2)
    ),
    *(
        _sub(
            f"pv{n}",
            "pv_string",
            f"String {n}",
            pv_string_entity=f"sensor.string_{n}_power",
            pv_string_label=f"String {n}",
            pv_string_power_source="ps1",
        )
        for n in (1, 2)
    ),
    *(
        _sub(
            f"bt{n}",
            "battery_tower",
            f"Tower {n}",
            battery_tower_power_source="ps1",
            battery_tower_soc_sensor=f"sensor.tower_{n}_soc",
        )
        for n in (1, 2)
    ),
]

MARK_PURCELL = [
    _signal("mw1", "Second house meter", "sensor.second_house_power", "whole_house"),
    *(
        _signal(f"mc{n}", f"Circuit {n}", f"sensor.circuit_{n}_w", "other")
        for n in (1, 2, 3)
    ),
    *(
        _sub(
            f"ev{n}",
            "battery_participant",
            f"EV {n}",
            battery_participant_name=f"EV {n}",
            battery_participant_soc_sensor=f"sensor.ev_{n}_soc",
            battery_participant_power_sensor=f"sensor.ev_{n}_power",
            battery_participant_capacity_kwh=75.0,
            # The wizard always saves this (a required field); both senses.
            battery_participant_power_positive_is_charge=(n == 1),
        )
        for n in (1, 2)
    ),
    _sub("ml1", "load", "Other loads", load_sensor="sensor.other_loads_power"),
]

SHAPES = {
    "test_box": TEST_BOX,
    "reference_household": REFERENCE_HOUSEHOLD,
    "mark_purcell": MARK_PURCELL,
}


def _history_ids(node: Any) -> set[str]:
    """Every subentry id the document keeps a history link to, anywhere."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "history_subentry_id" and isinstance(value, str):
                found.add(value)
            else:
                found |= _history_ids(value)
    elif isinstance(node, list):
        for item in node:
            found |= _history_ids(item)
    return found


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_every_install_shape_imports_whole(shape):
    subs = SHAPES[shape]
    snapshot = {
        "site": {
            "name": "Synthetic",
            "timezone": "Australia/Brisbane",
            "currency": "AUD",
        },
        "options": dict(_HUB_OPTIONS),
        "numbers": dict(_HUB_NUMBERS),
        "subentries": subs,
        "setup_builder_done": [],
    }
    res = di.import_legacy(snapshot)
    doc = res.document
    assert res.problems == [], shape

    # Nothing dropped: each subentry is mapped (a history link) or kept,
    # with a reason.
    ids = {s["subentry_id"] for s in subs}
    kept = {u["subentry_id"] for u in res.unmapped}
    mapped = _history_ids(doc)
    assert ids <= mapped | kept, (shape, sorted(ids - mapped - kept))

    # A whole-house meter is never a circuit (#2299): only the house's own
    # device binds a sensor the household marked as the whole house.
    whole_house = {_HUB_OPTIONS["solver_whole_house_cross_check_sensor"]} | {
        s["data"]["load_sensor"]
        for s in subs
        if s["subentry_type"] == "power_signal"
        and s["data"].get("signal_role") == "whole_house"
    }
    for device in doc["devices"]:
        if device["id"] == "load_house":
            continue
        for binding in (device.get("bindings") or {}).values():
            source = (binding.get("source") or {}).get("entity_id")
            assert source not in whole_house, (shape, device["id"], source)

    # The settings view writes nothing back over today's settings.
    writes = di.view_writes(di.legacy_view(doc), snapshot)
    assert writes == {"options": {}, "numbers": {}, "subentries": {}}, (shape, writes)
