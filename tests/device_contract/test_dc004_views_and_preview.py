"""DC-004 step 4: compatibility views and the dry run (device_import.py).

DC-R12 / F10: on a fully configured legacy site, import -> views changes no
owned key, so no consumer's input changes. Synthetic snapshots only
(household, 18 Oct 2026: "100% safe" -- no real install in this repo).
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "nimbus_load"
sys.path.insert(0, str(_PKG))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _PKG / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dc = _load("device_contract")
di = _load("device_import")


def _sub(sid, kind, title, **data):
    return {"subentry_id": sid, "subentry_type": kind, "title": title, "data": data}


#: F10, synthetic: every hub field and every mapped subentry type configured.
F10 = {
    "site": {"name": "Synthetic", "timezone": "Australia/Brisbane", "currency": "AUD"},
    "options": {
        "grid_sensor": "sensor.meter_power",
        "solver_import_price_sensor": "sensor.buy",
        "solver_export_price_sensor": "sensor.sell",
        "solver_battery_soc_sensor": "sensor.bat_soc",
        "solver_battery_power_sensor": "sensor.bat_power",
        "battery_sensor": "sensor.bat_power",
        "solver_battery_power_positive_is_charge": True,
        "solver_solar_power_sensor": None,
        "solar_sensor": "sensor.pv_power",
        "solver_solar_forecast_sensor": "sensor.pv_forecast",
        "solver_load_forecast_sensor": "sensor.house_forecast",
        "solver_whole_house_cross_check_sensor": "sensor.house_power",
        # a behaviour tunable: never owned, never touched
        "solver_offer_curve_enabled": True,
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
    "subentries": [
        _sub(
            "s1",
            "power_signal",
            "Grid",
            load_sensor="sensor.meter_power",
            signal_role="grid",
        ),
        _sub(
            "s2",
            "power_signal",
            "House",
            load_sensor="sensor.house",
            signal_role="other",
        ),
        _sub(
            "l1",
            "load",
            "Pool pump",
            load_sensor="sensor.pool_power",
            schedule_start_hour=8,
            schedule_end_hour=15,
            expected_load_kw=1.3,
        ),
        _sub("l2", "load", "Lights", load_sensor="sensor.lights_power"),
        _sub(
            "ev",
            "battery_participant",
            "Car",
            battery_participant_name="Car",
            battery_participant_capacity_kwh=60,
            battery_participant_soc_sensor="sensor.car_soc",
            battery_participant_power_sensor="sensor.car_power",
            battery_participant_power_positive_is_charge=True,
            battery_participant_min_soc_percent=20,
            battery_participant_max_soc_percent=90,
            battery_participant_max_charge_kw=11,
            battery_participant_max_discharge_kw=0,
        ),
        _sub(
            "c1",
            "controllable_load",
            "Hot water",
            controllable_load_name="Hot water",
            controllable_load_power_sensor="sensor.hws_power",
            controllable_load_device_entity="switch.hws",
        ),
        _sub("p1", "power_source", "Inverter 1", power_source_name="Inverter 1"),
    ],
    "setup_builder_done": [],
}


def test_f10_full_site_dry_run_changes_no_input():
    pv = di.preview(copy.deepcopy(F10))
    assert pv.effective_diff == []
    assert pv.conflicts == [] and pv.problems == []
    assert pv.clean


def test_the_view_writes_back_only_where_each_value_came_from():
    view = di.legacy_view(di.import_legacy(copy.deepcopy(F10)).document)
    # solar came from the Forecaster field; the empty Solver field stays empty
    assert view["options"]["solar_sensor"] == "sensor.pv_power"
    assert "solver_solar_power_sensor" not in view["options"]
    # both battery keys held the same sensor: both stay in step
    assert view["options"]["solver_battery_power_sensor"] == "sensor.bat_power"
    assert view["options"]["battery_sensor"] == "sensor.bat_power"
    # the grid Power Signal that confirmed the hub sensor keeps its key
    assert view["subentries"]["s1"]["load_sensor"] == "sensor.meter_power"


def test_the_view_never_touches_tunables_unmapped_or_equipment():
    snap = copy.deepcopy(F10)
    view = di.legacy_view(di.import_legacy(snap).document)
    assert "solver_offer_curve_enabled" not in view["options"]
    assert "s2" not in view["subentries"] and "p1" not in view["subentries"]
    owned = di.owned_keys(snap)
    assert "options.solver_offer_curve_enabled" not in owned
    assert not [k for k in owned if k.startswith(("subentry:s2.", "subentry:p1."))]


def test_import_view_import_is_a_fixed_point():
    """Applying the view to the snapshot and importing again gives the same
    document: the round trip loses nothing and invents nothing."""
    snap = copy.deepcopy(F10)
    doc = di.import_legacy(snap).document
    view = di.legacy_view(doc)
    applied = copy.deepcopy(snap)
    applied["options"].update(view["options"])
    applied["numbers"].update(view["numbers"])
    for sub in applied["subentries"]:
        sub["data"].update(view["subentries"].get(sub["subentry_id"], {}))
    assert dc.serialise(di.import_legacy(applied).document) == dc.serialise(doc)


def test_a_conflict_shows_in_the_dry_run_and_blocks():
    snap = copy.deepcopy(F10)
    snap["options"]["battery_sensor"] = "sensor.other_battery"
    pv = di.preview(snap)
    assert pv.conflicts and not pv.clean
    changed = {p for p, _b, _a in pv.effective_diff}
    assert {"options.battery_sensor", "options.solver_battery_power_sensor"} <= changed


def test_a_placeholder_number_is_not_a_change():
    snap = copy.deepcopy(F10)
    snap["numbers"]["solver_max_charge_kw"] = 0.1
    assert di.preview(snap).effective_diff == []


def test_an_option_and_number_conflict_is_reported_not_silently_kept():
    snap = copy.deepcopy(F10)
    snap["options"]["solver_battery_capacity_kwh"] = 38.0
    pv = di.preview(snap)
    assert [c.code for c in pv.conflicts] == ["conflict_setting_copies"]
    assert not pv.clean


def test_the_dry_run_does_not_mutate_its_input():
    snap = copy.deepcopy(F10)
    before = copy.deepcopy(snap)
    di.preview(snap)
    assert snap == before


def test_view_writes_lists_only_what_differs_and_never_adds_a_subentry():
    """DC-004 step 5b: an active commit writes only the owned keys whose
    value differs from the view. F10's own view against F10 is nothing; one
    changed option, number and subentry field are exactly those three; a
    subentry the install doesn't have is never created."""
    view = di.legacy_view(di.import_legacy(F10).document)
    assert di.view_writes(view, F10) == {"options": {}, "numbers": {}, "subentries": {}}
    edited = copy.deepcopy(view)
    edited["options"]["grid_sensor"] = "sensor.meter2"
    key = next(iter(edited["numbers"]))
    edited["numbers"][key] = 123.0
    sid = next(iter(edited["subentries"]))
    field = next(iter(edited["subentries"][sid]))
    edited["subentries"][sid][field] = "changed"
    edited["subentries"]["sub_not_here"] = {"x": 1}
    assert di.view_writes(edited, F10) == {
        "options": {"grid_sensor": "sensor.meter2"},
        "numbers": {key: 123.0},
        "subentries": {sid: {field: "changed"}},
    }
