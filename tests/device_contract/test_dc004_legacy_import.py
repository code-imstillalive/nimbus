"""DC-004 legacy import, step 1: hub-level settings (device_import.py).

Synthetic snapshots only. Household, 18 Oct 2026: "I need to be safe. 100%
safe" -- no real install's settings in this public repo.
"""

from __future__ import annotations

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


def _sub(sid, kind, title, **data):
    return {"subentry_id": sid, "subentry_type": kind, "title": title, "data": data}


def test_every_subentry_is_mapped_or_kept_never_dropped():
    # test_every_subentry_is_mapped_or_kept_never_dropped
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

    # test_two_loads_with_one_name_get_suffixed_ids_in_creation_order
    subs = [_sub("a1", "load", "Pool"), _sub("a2", "load", "Pool")]
    res = di.import_legacy(_snapshot(subentries=subs))
    ids = [d["id"] for d in res.document["devices"] if d.get("history_subentry_id")]
    assert ids == ["pool", "pool_2"]

    # test_a_power_signal_on_another_sensor_is_a_conflict
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

    # test_an_ev_participant_is_its_own_battery
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

    # test_control_is_authorised_only_where_a_device_entity_exists
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

    # test_the_module_makes_no_home_assistant_import
    text = Path(di.__file__).read_text(encoding="utf-8")
    assert "homeassistant" not in text


def test_the_solar_forecast_imports_as_the_members_the_solver_reads():
    """DC-006 slice 3 (#1574): the Solar `generation_forecast` binding is
    the Solver's own member list (`device_forecasts.legacy_solar_binding`),
    read through forecast adapters, and each configured member goes back to
    its own settings key; an auto-included integration belongs to none."""
    known = {"solcast": ["sensor.sc_today", "sensor.sc_tomorrow"], "open_meteo": []}

    # One configured sensor: one source, the binding's own provenance.
    res = di.import_legacy(_snapshot())
    binding = _device(res.document, "solar")["bindings"]["generation_forecast"]
    assert binding["source"]["kind"] == "entity_attribute"
    assert binding["source"]["entity_id"] == "sensor.pv_forecast"
    assert binding["source"]["adapter"] == {"name": "nimbus_forecast", "version": 1}
    assert [a["name"] for a in binding["source"]["also_adapters"]] == [
        "solcast",
        "open_meteo_solar",
    ]
    assert binding["provenance"]["source_path"] == (
        "options.solver_solar_forecast_sensor"
    )
    assert res.problems == []
    view = di.legacy_view(res.document)
    assert view["options"]["solver_solar_forecast_sensor"] == "sensor.pv_forecast"

    # Two configured sensors and auto-include: three members, mean.
    snap = _snapshot()
    snap["options"] = {
        **snap["options"],
        "solver_solar_forecast_sensor_3": "sensor.pv_forecast_b",
        "solver_auto_include_known_solar": True,
    }
    snap["known_sources"] = known
    res = di.import_legacy(snap)
    binding = _device(res.document, "solar")["bindings"]["generation_forecast"]
    assert binding["combine"] == "mean"
    members = binding["sources"]
    assert [m["entity_id"] for m in members] == [
        "sensor.pv_forecast",
        "sensor.sc_today",
        "sensor.pv_forecast_b",
    ]
    assert [(m.get("provenance") or {}).get("source_path") for m in members] == [
        "options.solver_solar_forecast_sensor",
        None,
        "options.solver_solar_forecast_sensor_3",
    ]
    assert res.problems == []
    view = di.legacy_view(res.document)
    assert view["options"]["solver_solar_forecast_sensor"] == "sensor.pv_forecast"
    assert view["options"]["solver_solar_forecast_sensor_3"] == "sensor.pv_forecast_b"
    assert "solver_solar_forecast_sensor_2" not in view["options"]
    # The settings already are the view: nothing drifts after an import.
    assert di.drift(res.document, snap) == []
    assert "options.solver_solar_forecast_sensor_3" in di.owned_keys(snap)

    # A changed sensor 3 is drift on that key alone.
    moved = {**snap, "options": {**snap["options"]}}
    moved["options"]["solver_solar_forecast_sensor_3"] = "sensor.other"
    assert [(d["key"], d["current"]) for d in di.drift(res.document, moved)] == [
        ("solver_solar_forecast_sensor_3", "sensor.other")
    ]

    # test_the_load_total_imports_as_the_forecast_the_solver_reads
    # DC-006 slice 3, the load total (#1574): the whole-house
    # `demand_forecast` is read by the forecast adapters, as the Solver reads
    # it (the `forecast` attribute), and still goes back to its own key.
    res = di.import_legacy(_snapshot())
    demand = _device(res.document, "load_house")["bindings"]["demand_forecast"]
    assert demand["source"] == {
        "kind": "entity_attribute",
        "entity_id": "sensor.house_forecast",
        "attribute_path": "forecast",
        "adapter": {"name": "nimbus_forecast", "version": 1},
    }
    assert demand["provenance"]["source_path"] == "options.solver_load_forecast_sensor"
    assert res.problems == []
    view = di.legacy_view(res.document)
    assert view["options"]["solver_load_forecast_sensor"] == "sensor.house_forecast"
    assert di.drift(res.document, _snapshot()) == []

    # test_a_generic_price_sensor_also_imports_the_forecast_the_solver_reads
    # DC-006 slice 4 (#1574): a price sensor no provider profile reads also
    # gives `<role>_forecast`, its `forecast` attribute through the generic
    # adapter in its own unit. A provider's sensor, or one the snapshot says
    # nothing about, gets none; with no unit it is imported without one, so
    # it is refused when read. It has no settings key: the view is unchanged.
    snap = _snapshot(
        price_sources={
            "sensor.buy": {"unit": "c/kWh", "profile": False},
            "sensor.sell": {"unit": "c/kWh", "profile": True},
        }
    )
    res = di.import_legacy(snap)
    grid = _device(res.document, "grid")["bindings"]
    assert grid["import_price_forecast"] == {
        "source": {
            "kind": "entity_attribute",
            "entity_id": "sensor.buy",
            "attribute_path": "forecast",
            "adapter": {"name": "generic_price_forecast", "version": 1},
        },
        "provenance": {
            "origin": "legacy_import",
            "derived_from": "import_price",
            "confirmed": False,
        },
        "normalization": {"quality": "forecast", "native_unit": "c/kWh"},
    }
    assert "export_price_forecast" not in grid
    assert grid["import_price"]["source"] == {
        "kind": "entity_state",
        "entity_id": "sensor.buy",
    }
    assert res.problems == [] and res.conflicts == []
    assert di.legacy_view(res.document) == di.legacy_view(
        di.import_legacy(_snapshot()).document
    )
    assert di.drift(res.document, snap) == []
    unitless = di.import_legacy(
        _snapshot(price_sources={"sensor.buy": {"unit": None, "profile": False}})
    )
    forecast = _device(unitless.document, "grid")["bindings"]["import_price_forecast"]
    assert forecast["normalization"] == {"quality": "forecast"}
    unknown = _device(di.import_legacy(_snapshot()).document, "grid")["bindings"]
    assert "import_price_forecast" not in unknown
