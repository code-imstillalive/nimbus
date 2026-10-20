"""DC-005 step 1: the Energy Dashboard as a proposal (device_discovery.py).

Synthetic preferences only (household, 18 Oct 2026: "100% safe"). The shape
follows HA 2026.3+'s unified grid source and the #1528 reference case carried
onto #1574 on 18 Oct: grid, solar, battery and individual devices, two of them
inside a third.
"""

from __future__ import annotations

import ast
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
    """#1528, carried onto #1574 18 Oct: grid, battery, solar and the loads,
    and their inclusion, from the Energy Dashboard alone."""
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


def test_dc_r13_every_proposed_binding_and_inclusion_is_unconfirmed():
    p = dd.propose_from_energy_dashboard(PREFS, site=SITE)
    for device_id, role, binding in _bindings(p.document):
        prov = binding["provenance"]
        assert prov["origin"] == "energy_dashboard", (device_id, role)
        assert prov["confirmed"] is False and prov["overridden"] is False
        assert prov["evidence"][0]["kind"] == "energy_dashboard"
    for rel in p.document["relationships"]:
        if rel["kind"] == "included_in_measurement":
            assert rel["confirmed"] is False
    assert p.document["site"]["control_policy"] == "observation_only"


def test_the_only_structural_gap_is_the_battery_capacity_and_it_is_asked():
    """The Energy Dashboard has no capacity and DC-001 requires one: the
    proposal shows it as incomplete and asks, rather than inventing one."""
    p = dd.propose_from_energy_dashboard(PREFS, site=SITE)
    assert [(x.code, x.path) for x in p.problems] == [
        ("capacity_missing", "devices[3].capacity")
    ]
    assert _device(p.document, "battery").get("capacity") is None
    assert [(q["code"], q["device"], q.get("fact")) for q in p.questions] == [
        ("fact_needed", "battery", "capacity")
    ]


def test_signs_are_the_contracts_and_energy_stays_a_statistic():
    p = dd.propose_from_energy_dashboard(PREFS, site=SITE)
    assert not [x for x in p.problems if x.code == "sign_not_canonical"]
    directions = {
        (d, r): b["normalization"]["positive_direction"]
        for d, r, b in _bindings(p.document)
        if "normalization" in b
    }
    assert directions == {
        ("grid", "power"): "import",
        ("solar", "power"): "generation",
        ("battery", "power"): "discharge",
        ("load_pool_circuit", "power"): "consumption",
    }
    for _d, role, binding in _bindings(p.document):
        if role.endswith("energy"):
            assert binding["source"] == {
                "kind": "recorder_statistic",
                "statistic_id": binding["source"]["statistic_id"],
                "interval_semantics": "hourly_change",
            }


def test_two_import_flows_on_an_older_grid_source_are_a_question_not_a_pick():
    """List position is not evidence (Mark, #1590): both are listed, neither
    is bound."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [
                    {"stat_energy_from": "sensor.peak_kwh"},
                    {"stat_energy_from": "sensor.offpeak_kwh"},
                ],
                "flow_to": [{"stat_energy_to": "sensor.export_kwh"}],
            }
        ]
    }
    p = dd.propose_from_energy_dashboard(prefs)
    grid = _device(p.document, "grid")["bindings"]
    assert "import_energy" not in grid
    assert _source_value(grid["export_energy"]) == "sensor.export_kwh"
    (q,) = p.questions
    assert (q["code"], q["device"], q["role"]) == (
        "several_sources",
        "grid",
        "import_energy",
    )
    assert [c["source"] for c in q["candidates"]] == [
        "sensor.peak_kwh",
        "sensor.offpeak_kwh",
    ]


def test_two_grid_sources_never_make_a_second_grid():
    prefs = {
        "energy_sources": [
            {"type": "grid", "stat_energy_from": "sensor.a_kwh"},
            {"type": "grid", "stat_energy_from": "sensor.b_kwh"},
        ]
    }
    p = dd.propose_from_energy_dashboard(prefs)
    assert [d["id"] for d in p.document["devices"] if d["kind"] == "grid"] == ["grid"]
    assert [q["role"] for q in p.questions] == ["import_energy"]


def test_the_same_source_named_twice_is_one_candidate_not_a_question():
    prefs = {
        "energy_sources": [
            {"type": "grid", "stat_energy_from": "sensor.a_kwh"},
            {"type": "grid", "stat_energy_from": "sensor.a_kwh"},
        ]
    }
    p = dd.propose_from_energy_dashboard(prefs)
    assert p.questions == []
    assert _source_value(_device(p.document, "grid")["bindings"]["import_energy"]) == (
        "sensor.a_kwh"
    )


def test_two_solar_sources_are_two_solar_devices():
    """Two inverters are two devices (DC-001 / F02)."""
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


def test_included_in_an_unknown_statistic_is_a_finding_not_a_relationship():
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


def test_a_fixed_price_is_reported_not_proposed():
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


def test_dc_r01_no_energy_dashboard_is_grid_and_house_and_says_so():
    """Absence is never a zero: the minimum site, unbound, with a finding."""
    p = dd.propose_from_energy_dashboard(None, site=SITE)
    assert [d["id"] for d in p.document["devices"]] == ["grid", "load_house"]
    assert all(not d["bindings"] for d in p.document["devices"])
    assert p.problems == []
    assert [f["code"] for f in p.findings] == ["no_energy_dashboard"]


def test_registry_ids_are_kept_for_rediscovery():
    """DC-005 decision 2: rediscovery matches by registry id, so it is
    recorded wherever the HA side supplies one."""
    p = dd.propose_from_energy_dashboard(
        PREFS, registry_ids={"sensor.bat_power": "reg_bat_power"}
    )
    power = _device(p.document, "battery")["bindings"]["power"]["source"]
    assert power["registry_id"] == "reg_bat_power"
    assert (
        "registry_id" not in _device(p.document, "grid")["bindings"]["power"]["source"]
    )


def test_discovery_is_deterministic_and_does_not_touch_its_input():
    before = copy.deepcopy(PREFS)
    a = dd.propose_from_energy_dashboard(PREFS, site=SITE)
    b = dd.propose_from_energy_dashboard(PREFS, site=SITE)
    assert dc.serialise(a.document) == dc.serialise(b.document)
    assert (a.questions, a.findings) == (b.questions, b.findings)
    assert before == PREFS


def test_dc_r15_discovery_is_pure():
    """No Home Assistant import, so it cannot call a service."""
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
