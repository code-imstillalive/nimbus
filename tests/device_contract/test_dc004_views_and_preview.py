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

import pytest

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


def test_an_option_and_number_conflict_is_reported_not_silently_kept():
    # test_an_option_and_number_conflict_is_reported_not_silently_kept
    snap = copy.deepcopy(F10)
    snap["options"]["solver_battery_capacity_kwh"] = 38.0
    pv = di.preview(snap)
    assert [c.code for c in pv.conflicts] == ["conflict_setting_copies"]
    assert not pv.clean

    # test_view_writes_lists_only_what_differs_and_never_adds_a_subentry: DC-004 step 5b: an active commit writes only the owned keys whose
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

    # test_a_live_number_writes_through_to_the_document_only_where_owned: DC-004 step 5c: a contract-owned number commits a new revision; the
    doc = di.import_legacy(F10).document
    edited = di.with_number(doc, "solver_battery_capacity_kwh", 55.0)
    assert edited is not None and edited is not doc
    view = di.legacy_view(edited)
    assert view["numbers"]["solver_battery_capacity_kwh"] == 55.0
    before = di.legacy_view(doc)
    assert {
        k: v for k, v in view["numbers"].items() if k != "solver_battery_capacity_kwh"
    } == {
        k: v for k, v in before["numbers"].items() if k != "solver_battery_capacity_kwh"
    }
    assert (
        view["options"] == before["options"]
        and view["subentries"] == before["subentries"]
    )
    limit = di.with_number(doc, "solver_grid_max_import_kw", 25.0)
    assert di.legacy_view(limit)["numbers"]["solver_grid_max_import_kw"] == 25.0
    assert di.with_number(doc, "solver_price_spike_threshold", 1.0) is None
    assert "solver_price_spike_threshold" not in di.owned_number_keys()
    assert di.import_legacy(F10).document == doc  # the input is untouched

    # test_keep_my_change_adopts_one_key_and_leaves_the_others_to_the_document: "Keep my change" for one drifted key: the new document holds that
    doc = di.import_legacy(F10).document
    edited = copy.deepcopy(F10)
    edited["options"]["grid_sensor"] = "sensor.meter2"
    pool = next(s for s in edited["subentries"] if s["subentry_id"] == "l1")
    pool["data"]["schedule_start_hour"] = 9
    kept = di.adopt(doc, edited, "options", None, "grid_sensor")
    assert not kept.conflicts and not kept.problems
    view = di.legacy_view(kept.document)
    assert view["options"]["grid_sensor"] == "sensor.meter2"
    assert view["subentries"]["l1"]["schedule_start_hour"] == 8
    # The grid sensor was imported from the option and from the Grid Power
    # Signal: keeping the change moves both, so the two never disagree.
    assert view["subentries"]["s1"]["load_sensor"] == "sensor.meter2"
    left = {
        (d["part"], d["subentry_id"], d["key"]) for d in di.drift(kept.document, edited)
    }
    assert left == {
        ("subentries", "l1", "schedule_start_hour"),
        ("subentries", "s1", "load_sensor"),
    }

    # test_keep_my_change_on_a_drifted_solver_number: Mark Purcell, #1882 review: a `numbers.` path fell into the subentry
    doc = di.import_legacy(F10).document
    edited = copy.deepcopy(F10)
    edited["numbers"]["solver_battery_capacity_kwh"] = 33.0
    assert ("numbers", None, "solver_battery_capacity_kwh") in {
        (d["part"], d["subentry_id"], d["key"]) for d in di.drift(doc, edited)
    }
    kept = di.adopt(doc, edited, "numbers", None, "solver_battery_capacity_kwh")
    assert not kept.conflicts and not kept.problems
    assert (
        di.legacy_view(kept.document)["numbers"]["solver_battery_capacity_kwh"] == 33.0
    )
    assert di.drift(kept.document, edited) == []

    # test_a_settings_path_that_is_none_of_the_three_is_refused_plainly
    for bad in ("numbers", "subentry:l1", "battery.capacity"):
        with pytest.raises(ValueError, match="not a settings path"):
            di._value_at(F10, bad)
