"""DC-005 step 4: rediscovery against a stored document, with DC-000's F06
cases: rename, reorder, repeated discovery, equal candidates and replaced
equipment (device_discovery.rediscover). Synthetic only."""

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
dr = _load("device_resolver")
dd = _load("device_discovery")

PREFS = {
    "energy_sources": [
        {
            "type": "grid",
            "stat_energy_from": "sensor.grid_in_kwh",
            "stat_rate": "sensor.grid_power",
        },
        {
            "type": "battery",
            "stat_energy_from": "sensor.bat_out_kwh",
            "stat_energy_to": "sensor.bat_in_kwh",
            "stat_rate": "sensor.bat_power",
            "stat_soc": "sensor.bat_soc",
        },
        {
            "type": "solar",
            "stat_energy_from": "sensor.pv_kwh",
            "stat_rate": "sensor.pv_w",
        },
    ],
    "device_consumption": [
        {
            "stat_consumption": "sensor.pool_kwh",
            "name": "Pool",
            "stat_rate": "sensor.pool_w",
        }
    ],
}
REGS = {
    "sensor.grid_power": "r_grid",
    "sensor.bat_power": "r_bat",
    "sensor.bat_soc": "r_soc",
    "sensor.pv_w": "r_pv",
    "sensor.pool_w": "r_pool",
}


def _propose(prefs=PREFS, regs=REGS):
    return dd.propose_from_energy_dashboard(prefs, registry_ids=regs)


def _stored(prefs=PREFS, regs=REGS):
    """A stored document as the household would have it after confirming the
    first discovery: every binding confirmed."""
    doc = copy.deepcopy(_propose(prefs, regs).document)
    for device in doc["devices"]:
        for binding in device["bindings"].values():
            binding["provenance"]["confirmed"] = True
    return doc


def _kinds(r):
    return [d["kind"] for d in r.differences]


def test_dc_r02_f06_a_renamed_entity_is_the_same_source_and_says_so():
    prefs = copy.deepcopy(PREFS)
    prefs["energy_sources"][1]["stat_rate"] = "sensor.battery_power_renamed"
    regs = {**REGS, "sensor.battery_power_renamed": "r_bat"}
    regs.pop("sensor.bat_power")
    r = dd.rediscover(
        _stored(), _propose(prefs, regs), present_registry_ids=frozenset(regs.values())
    )
    assert r.differences == [
        {
            "kind": "renamed",
            "device": "battery",
            "role": "power",
            "registry_id": "r_bat",
            "stored_entity_id": "sensor.bat_power",
            "entity_id": "sensor.battery_power_renamed",
        }
    ]
    assert not [q for q in r.questions if q["code"] == "replaced_equipment"]


def test_dc_r02_f06_replaced_equipment_is_a_question_not_a_swap():
    prefs = copy.deepcopy(PREFS)
    prefs["energy_sources"][1]["stat_rate"] = "sensor.new_battery_power"
    regs = {**REGS, "sensor.new_battery_power": "r_bat_new"}
    present = frozenset(v for v in regs.values() if v != "r_bat")
    r = dd.rediscover(_stored(), _propose(prefs, regs), present_registry_ids=present)
    (q,) = [q for q in r.questions if q["code"] == "replaced_equipment"]
    assert (q["device"], q["role"]) == ("battery", "power")
    assert q["stored_source"]["registry_id"] == "r_bat"
    assert q["candidate"]["source"] == "sensor.new_battery_power"
    assert r.differences == []


def test_a_new_device_is_proposed_and_a_missing_one_is_kept():
    stored = _stored()
    prefs = copy.deepcopy(PREFS)
    prefs["device_consumption"] = [
        {
            "stat_consumption": "sensor.spa_kwh",
            "name": "Spa",
            "stat_rate": "sensor.spa_w",
        }
    ]
    r = dd.rediscover(stored, _propose(prefs))
    assert [(d["kind"], d["device"]["name"]) for d in r.differences] == [
        ("new_device", "Spa")
    ]
    assert [
        (f["code"], f["device"]) for f in r.findings if f["code"] == "not_seen"
    ] == [("not_seen", "load_pool")]


def test_a_discovered_device_matching_two_stored_ones_is_asked():
    stored = _stored()
    pool = next(d for d in stored["devices"] if d["id"] == "load_pool")
    twin = copy.deepcopy(pool)
    twin["id"] = "load_pool_twin"
    stored["devices"].append(twin)
    r = dd.rediscover(stored, _propose())
    (q,) = [q for q in r.questions if q["code"] == "device_match_ambiguous"]
    assert q["stored_devices"] == ["load_pool", "load_pool_twin"]
