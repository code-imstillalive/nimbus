"""DC-004 legacy import, step 1: hub-level settings (device_import.py).

Synthetic snapshots only. Household, 18 Oct 2026: "I need to be safe. 100%
safe" -- no real install's settings in this public repo.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "nimbus_load"
sys.path.insert(0, str(_PKG))  # device_import's fallback `import device_contract`


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dc = _load("device_contract")
di = _load("device_import")

SITE = {"name": "Synthetic", "timezone": "Australia/Brisbane", "currency": "AUD"}


def _snapshot(**overrides):
    snap = {
        "site": SITE,
        "options": {
            "grid_sensor": "sensor.meter_power",
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
        },
        "numbers": {
            "solver_battery_capacity_kwh": 40.0,
            "solver_battery_min_soc_percent": 5.0,
            "solver_battery_max_soc_percent": 100.0,
            "solver_max_charge_kw": 10.0,
            "solver_max_discharge_kw": 10.0,
            "solver_grid_max_import_kw": 20.0,
            "solver_grid_max_export_kw": 10.0,
        },
        "subentries": [],
        "setup_builder_done": [],
    }
    for k, v in overrides.items():
        snap[k] = v
    return snap


def _device(doc, ident):
    return next(d for d in doc["devices"] if d["id"] == ident)


def test_a_full_hub_imports_to_a_valid_document():
    res = di.import_legacy(_snapshot())
    assert res.problems == [] and res.conflicts == [] and res.unmapped == []
    doc = res.document
    assert {d["id"] for d in doc["devices"]} == {
        "grid",
        "load_house",
        "battery",
        "solar",
    }
    assert _device(doc, "battery")["capacity"] == {
        "value": 40.0,
        "unit": "kWh",
        "basis": "usable",
    }
    assert _device(doc, "grid")["constraints"] == {
        "import_limit": 20.0,
        "export_limit": 10.0,
    }
    power = _device(doc, "battery")["bindings"]["power"]
    assert power["source"]["entity_id"] == "sensor.bat_power"
    assert power["normalization"]["native_positive_direction"] == "charge"
    assert power["normalization"]["positive_direction"] == "discharge"
    assert power["provenance"]["origin"] == "legacy_import"
    assert power["provenance"]["source_path"] == "options.solver_battery_power_sensor"


def test_deterministic_and_does_not_mutate_its_input():
    snap = _snapshot()
    before = copy.deepcopy(snap)
    a = dc.serialise(di.import_legacy(snap).document)
    b = dc.serialise(di.import_legacy(copy.deepcopy(snap)).document)
    assert a == b and snap == before


def test_two_different_sensors_for_one_role_are_a_named_conflict_not_a_choice():
    snap = _snapshot()
    snap["options"]["battery_sensor"] = "sensor.other_battery"
    res = di.import_legacy(snap)
    (c,) = [c for c in res.conflicts if c.code == "conflict_role_sources"]
    assert c.path == "devices.battery.bindings.power"
    assert dict(c.sources) == {
        "options.solver_battery_power_sensor": "sensor.bat_power",
        "options.battery_sensor": "sensor.other_battery",
    }
    assert "power" not in _device(res.document, "battery")["bindings"]


def test_an_empty_field_is_filled_from_the_other_without_a_conflict():
    snap = _snapshot()
    snap["options"]["solver_solar_power_sensor"] = None
    res = di.import_legacy(snap)
    power = _device(res.document, "solar")["bindings"]["power"]
    assert power["provenance"]["source_path"] == "options.solar_sensor"
    assert res.conflicts == []


def test_an_option_and_its_live_number_disagreeing_is_a_conflict():
    snap = _snapshot()
    snap["options"]["solver_battery_capacity_kwh"] = 38.0
    res = di.import_legacy(snap)
    (c,) = res.conflicts
    assert c.code == "conflict_setting_copies" and c.path == "devices.battery.capacity"
    assert "capacity" not in _device(res.document, "battery")
    assert [p.code for p in res.problems] == ["capacity_missing"]


def test_a_placeholder_number_is_not_set():
    snap = _snapshot()
    snap["numbers"]["solver_max_charge_kw"] = 0.1
    res = di.import_legacy(snap)
    assert "charge_limit" not in _device(res.document, "battery")["constraints"]


def test_a_minimal_site_is_grid_and_house_only():
    res = di.import_legacy(_snapshot(options={}, numbers={}))
    assert [d["id"] for d in res.document["devices"]] == ["grid", "load_house"]
    assert res.problems == []


def _sub(sid, kind, title, **data):
    return {"subentry_id": sid, "subentry_type": kind, "title": title, "data": data}


def test_every_subentry_is_mapped_or_kept_never_dropped():
    subs = [
        _sub("a1", "load", "Pool pump", load_sensor="sensor.pool_power"),
        _sub("b2", "power_source", "Inverter 1"),
        _sub(
            "c3",
            "power_signal",
            "House",
            load_sensor="sensor.house",
            signal_role="other",
        ),
        _sub("d4", "pv_string", "String 1"),
        _sub("e5", "battery_tower", "Tower 1"),
        _sub("f6", "mystery", "Future type"),
    ]
    res = di.import_legacy(_snapshot(subentries=subs))
    mapped = {d.get("history_subentry_id") for d in res.document["devices"]}
    kept = {u["subentry_id"]: u["code"] for u in res.unmapped}
    assert mapped & {s["subentry_id"] for s in subs} == {"a1"}
    assert kept == {
        "b2": "unmapped_topology_equipment",
        "c3": "unmapped_signal_role",
        "d4": "unmapped_topology_equipment",
        "e5": "unmapped_topology_equipment",
        "f6": "unmapped_subentry",
    }
    assert res.problems == []


def test_a_load_becomes_a_circuit_with_its_schedule_and_no_assumed_inclusion():
    res = di.import_legacy(
        _snapshot(
            subentries=[
                _sub(
                    "a1",
                    "load",
                    "Pool pump",
                    load_sensor="sensor.pool_power",
                    schedule_start_hour=8,
                    schedule_end_hour=15,
                    expected_load_kw=1.3,
                )
            ]
        )
    )
    pool = _device(res.document, "pool_pump")
    assert pool["load_class"] == "circuit"
    assert pool["bindings"]["power"]["source"]["entity_id"] == "sensor.pool_power"
    assert pool["constraints"]["fixed_hours"] == {
        "start": 8,
        "end": 15,
        "expected_kw": 1.3,
    }
    assert not [
        r
        for r in res.document["relationships"]
        if r.get("kind") == "included_in_measurement"
    ]
    assert res.document["accounting"]["demand"]["root_load_id"] == "load_house"


def test_two_loads_with_one_name_get_suffixed_ids_in_creation_order():
    subs = [_sub("a1", "load", "Pool"), _sub("a2", "load", "Pool")]
    res = di.import_legacy(_snapshot(subentries=subs))
    ids = [d["id"] for d in res.document["devices"] if d.get("history_subentry_id")]
    assert ids == ["pool", "pool_2"]


def test_a_power_signal_matching_the_hub_sensor_adds_a_history_link_only():
    sub = _sub(
        "g1",
        "power_signal",
        "Grid",
        load_sensor="sensor.meter_power",
        signal_role="grid",
    )
    res = di.import_legacy(_snapshot(subentries=[sub]))
    power = _device(res.document, "grid")["bindings"]["power"]
    assert power["source"]["entity_id"] == "sensor.meter_power"
    assert power["history_subentry_id"] == "g1"
    assert res.conflicts == []


def test_a_power_signal_on_another_sensor_is_a_conflict():
    sub = _sub(
        "g1",
        "power_signal",
        "Grid",
        load_sensor="sensor.other_meter",
        signal_role="grid",
    )
    res = di.import_legacy(_snapshot(subentries=[sub]))
    (c,) = res.conflicts
    assert c.code == "conflict_role_sources" and c.path == "devices.grid.bindings.power"
    assert "power" not in _device(res.document, "grid")["bindings"]


def test_an_ev_participant_is_its_own_battery():
    sub = _sub(
        "ev1",
        "battery_participant",
        "Car",
        battery_participant_name="Car",
        battery_participant_capacity_kwh=60,
        battery_participant_soc_sensor="sensor.car_soc",
        battery_participant_power_sensor="sensor.car_power",
        battery_participant_power_positive_is_charge=True,
        battery_participant_max_charge_kw=11,
    )
    res = di.import_legacy(_snapshot(subentries=[sub]))
    car = _device(res.document, "car")
    assert car["kind"] == "battery" and car["capacity"]["value"] == 60
    assert (
        car["bindings"]["power"]["normalization"]["native_positive_direction"]
        == "charge"
    )
    assert car["constraints"] == {"charge_limit": 11}
    assert res.problems == []


def test_control_is_authorised_only_where_a_device_entity_exists():
    with_entity = _sub(
        "h1",
        "controllable_load",
        "Hot water",
        controllable_load_name="Hot water",
        controllable_load_device_entity="switch.hws",
    )
    without = _sub("h2", "controllable_load", "Dryer", controllable_load_name="Dryer")
    res = di.import_legacy(_snapshot(subentries=[without]))
    assert res.document["site"]["control_policy"] == "observation_only"
    assert "control" not in _device(res.document, "dryer")
    res = di.import_legacy(_snapshot(subentries=[without, with_entity]))
    assert res.document["site"]["control_policy"] == "authorised"
    assert _device(res.document, "hot_water")["control"]["entity_id"] == "switch.hws"
    assert "control" not in _device(res.document, "dryer")


def test_a_repair_confirmed_field_says_so():
    res = di.import_legacy(
        _snapshot(setup_builder_done=["solver_solar_forecast_sensor"])
    )
    prov = _device(res.document, "solar")["bindings"]["generation_forecast"][
        "provenance"
    ]
    assert prov["confirmed"] is True and prov["confirmed_by"] == "setup_builder"


def test_import_grants_no_control():
    assert (
        di.import_legacy(_snapshot()).document["site"]["control_policy"]
        == "observation_only"
    )


def test_the_module_makes_no_home_assistant_import():
    text = Path(di.__file__).read_text(encoding="utf-8")
    assert "homeassistant" not in text
