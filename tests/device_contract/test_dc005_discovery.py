"""DC-005 step 1: the Energy Dashboard as a proposal (device_discovery.py).

Synthetic preferences only (household, 18 Oct 2026: "100% safe"). The shape
follows HA 2026.3+'s unified grid source and the #1528 reference case carried
onto #1574 on 18 Oct: grid, solar, battery and individual devices, two of them
inside a third.
"""

from __future__ import annotations

import ast
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
dr = _load("device_resolver")
dd = _load("device_discovery")

PREFS = {
    "energy_sources": [
        {
            "type": "grid",
            "stat_energy_from": "sensor.grid_import_kwh",
            "stat_energy_to": "sensor.grid_export_kwh",
            "entity_energy_price": "sensor.buy",
            "entity_energy_price_export": "sensor.sell",
            "stat_rate": "sensor.grid_power",
        },
        {
            "type": "solar",
            "stat_energy_from": "sensor.pv_kwh",
            "stat_rate": "sensor.pv_power",
            "config_entry_solar_forecast": ["forecast_entry_1"],
        },
        {
            "type": "battery",
            "stat_energy_from": "sensor.bat_out_kwh",
            "stat_energy_to": "sensor.bat_in_kwh",
            "stat_rate": "sensor.bat_power",
            "stat_soc": "sensor.bat_soc",
        },
    ],
    "device_consumption": [
        {
            "stat_consumption": "sensor.pool_circuit_kwh",
            "name": "Pool circuit",
            "stat_rate": "sensor.pool_circuit_power",
        },
        {
            "stat_consumption": "sensor.pool_pump_kwh",
            "name": "Pool pump",
            "included_in_stat": "sensor.pool_circuit_kwh",
        },
        {
            "stat_consumption": "sensor.heat_pump_kwh",
            "name": "Heat pump",
            "included_in_stat": "sensor.pool_circuit_kwh",
        },
        {"stat_consumption": "sensor.ev_charger_kwh", "name": "EV charger"},
    ],
}
SITE = {"name": "Synthetic", "timezone": "Australia/Brisbane", "currency": "AUD"}


def _device(document, device_id):
    return next(d for d in document["devices"] if d["id"] == device_id)


def _bindings(document):
    return [
        (d["id"], role, b)
        for d in document["devices"]
        for role, b in (d.get("bindings") or {}).items()
    ]


def _source_value(binding):
    source = binding["source"]
    return source.get("entity_id") or source.get("statistic_id")


def test_1528_a_full_energy_dashboard_gives_the_whole_site_with_no_typed_entity():
    # test_1528_a_full_energy_dashboard_gives_the_whole_site_with_no_typed_entity: #1528, carried onto #1574 18 Oct: grid, battery, solar and the loads,
    p = dd.propose_from_energy_dashboard(PREFS, site=SITE)
    doc = p.document
    kinds = {d["id"]: d["kind"] for d in doc["devices"]}
    assert kinds == {
        "grid": "grid",
        "load_house": "load",
        "solar": "solar",
        "battery": "battery",
        "load_pool_circuit": "load",
        "load_pool_pump": "load",
        "load_heat_pump": "load",
        "load_ev_charger": "load",
    }
    grid = _device(doc, "grid")["bindings"]
    assert {r: _source_value(b) for r, b in grid.items()} == {
        "import_energy": "sensor.grid_import_kwh",
        "export_energy": "sensor.grid_export_kwh",
        "power": "sensor.grid_power",
        "import_price": "sensor.buy",
        "export_price": "sensor.sell",
    }
    battery = _device(doc, "battery")["bindings"]
    assert {r: _source_value(b) for r, b in battery.items()} == {
        "discharge_energy": "sensor.bat_out_kwh",
        "charge_energy": "sensor.bat_in_kwh",
        "power": "sensor.bat_power",
        "soc": "sensor.bat_soc",
    }
    assert (
        _source_value(_device(doc, "solar")["bindings"]["power"]) == "sensor.pv_power"
    )
    inclusion = {
        (r["child"], r["parent"])
        for r in doc["relationships"]
        if r["kind"] == "included_in_measurement"
    }
    assert inclusion == {
        ("load_pool_circuit", "load_house"),
        ("load_pool_pump", "load_pool_circuit"),
        ("load_heat_pump", "load_pool_circuit"),
        ("load_ev_charger", "load_house"),
    }
    # Every source came from the preferences: nothing typed, nothing invented.
    named = {
        v
        for src in PREFS["energy_sources"]
        for k, v in src.items()
        if isinstance(v, str) and k.startswith(("stat_", "entity_"))
    } | {
        v
        for d in PREFS["device_consumption"]
        for k, v in d.items()
        if k in ("stat_consumption", "stat_rate")
    }
    assert {_source_value(b) for _d, _r, b in _bindings(doc)} == named

    # test_a_new_install_keeps_what_it_ticked_and_its_answers
    # DC-007 slice 1 (#1574): the household unticks the EV charger and
    # answers the battery's capacity; the document keeps the rest, drops the
    # charger's relationships, and every binding it kept is confirmed.
    capacity_q = next(
        i
        for i, q in enumerate(p.questions)
        if q["code"] == dd.Q_FACT_NEEDED and q.get("fact") == "capacity"
    )
    keep = {d["id"] for d in doc["devices"]} - {"load_ev_charger"}
    answered = dd.answer_proposal(
        p, keep, {capacity_q: {"value": 13.5, "basis": "usable"}, 999: "ignored"}
    )
    assert "load_ev_charger" not in {d["id"] for d in answered["devices"]}
    assert all(
        "load_ev_charger" not in (r.get("child"), r.get("parent"))
        for r in answered["relationships"]
    )
    assert _device(answered, "battery")["capacity"] == {
        "value": 13.5,
        "unit": "kWh",
        "basis": "usable",
    }
    assert all(b["provenance"]["confirmed"] for _d, _r, b in _bindings(answered))
    assert dd.dc.validate(answered) == []
    # The grid and the house stay even if unticked: DC-R01's minimal site.
    minimal = dd.answer_proposal(p, set(), {})
    assert {d["id"] for d in minimal["devices"]} == {"grid", "load_house"}
    assert p.document == doc  # the proposal itself is untouched

    # test_two_solar_sources_are_two_solar_devices: Two inverters are two devices (DC-001 / F02).
    prefs = {
        "energy_sources": [
            {"type": "solar", "stat_energy_from": "sensor.pv1_kwh"},
            {"type": "solar", "stat_energy_from": "sensor.pv2_kwh"},
        ]
    }
    p = dd.propose_from_energy_dashboard(prefs)
    solar = [d for d in p.document["devices"] if d["kind"] == "solar"]
    assert [d["id"] for d in solar] == ["solar", "solar_2"]
    assert [_source_value(d["bindings"]["energy"]) for d in solar] == [
        "sensor.pv1_kwh",
        "sensor.pv2_kwh",
    ]
    assert p.problems == []

    # test_included_in_an_unknown_statistic_is_a_finding_not_a_relationship
    prefs = {
        "device_consumption": [
            {"stat_consumption": "sensor.pump_kwh", "included_in_stat": "sensor.nope"}
        ]
    }
    p = dd.propose_from_energy_dashboard(prefs)
    assert not [
        r for r in p.document["relationships"] if r["kind"] == "included_in_measurement"
    ]
    assert [(f["code"], f["statistic_id"]) for f in p.findings] == [
        ("included_in_unknown_statistic", "sensor.nope")
    ]

    # test_a_fixed_price_is_reported_not_proposed
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [
                    {"stat_energy_from": "sensor.in_kwh", "number_energy_price": 0.28}
                ],
            }
        ]
    }
    p = dd.propose_from_energy_dashboard(prefs)
    assert "import_price" not in _device(p.document, "grid")["bindings"]
    assert [(f["code"], f["value"]) for f in p.findings] == [
        ("fixed_price_not_proposed", 0.28)
    ]

    # test_dc_r15_discovery_is_pure: No Home Assistant import, so it cannot call a service.
    tree = ast.parse((_PKG / "device_discovery.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not [m for m in imported if m.startswith("homeassistant")]
