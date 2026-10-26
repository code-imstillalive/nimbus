"""DC-006 slice 1: readiness per function (device_readiness.py, DC-R14).
Synthetic documents only."""

from __future__ import annotations

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
rd = _load("device_readiness")


def _binding(entity_id):
    return {"source": {"kind": "entity_state", "entity_id": entity_id}}


def _doc(grid=None, house=None, battery=None, extra=()):
    devices = [
        {
            "id": "grid",
            "kind": "grid",
            "boundary": {"id": "poc", "domain": "ac", "phases": "all"},
            "bindings": grid or {},
            "constraints": {},
        },
        {
            "id": "load_house",
            "kind": "load",
            "load_class": "aggregate",
            "boundary": {"id": "end_use", "domain": "ac", "phases": "all"},
            "bindings": house or {},
            "constraints": {},
        },
    ]
    if battery is not None:
        devices.append(battery)
    devices.extend(extra)
    return {
        "schema_version": dc.SCHEMA_VERSION,
        "site": {"id": "site", "name": "S", "control_policy": "observation_only"},
        "devices": devices,
        "relationships": [{"kind": "supplies", "from": "grid", "to": "load_house"}],
        "accounting": {"demand": {"mode": "inclusive", "root_load_id": "load_house"}},
        "shared_constraints": [],
    }


GRID = {
    "power": _binding("sensor.grid"),
    "import_price": _binding("sensor.buy"),
    "export_price": _binding("sensor.sell"),
}
HOUSE = {"power": _binding("sensor.house")}


def _states(results):
    return {f: r["state"] for f, r in results.items()}


def test_observing_needs_grid_power_or_both_energies():
    # test_observing_needs_grid_power_or_both_energies
    assert rd.evaluate(_doc())["observe"]["state"] == "needs"
    energies = {
        "import_energy": {
            "source": {
                "kind": "recorder_statistic",
                "statistic_id": "a",
                "interval_semantics": "hourly_change",
            }
        },
        "export_energy": {
            "source": {
                "kind": "recorder_statistic",
                "statistic_id": "b",
                "interval_semantics": "hourly_change",
            }
        },
    }
    assert rd.evaluate(_doc(grid=energies))["observe"]["state"] == "ready"
    one = {"import_energy": energies["import_energy"]}
    assert rd.evaluate(_doc(grid=one))["observe"]["state"] == "needs"

    # test_dc_r14_f11_a_training_model_is_a_status_not_a_fault
    r = rd.evaluate(_doc(grid=GRID, house=HOUSE), models={"load_house": "training"})
    assert r["forecast"]["state"] == "training"
    assert r["plan"]["state"] == "training"
    assert "missing" not in r["plan"]
    r = rd.evaluate(_doc(grid=GRID, house=HOUSE), models={"load_house": "trained"})
    assert r["forecast"]["state"] == "ready" and r["plan"]["state"] == "ready"

    # test_a_battery_without_capacity_is_a_need_not_a_crash
    battery = {
        "id": "battery",
        "kind": "battery",
        "bindings": {"soc": _binding("sensor.soc"), "power": _binding("sensor.bp")},
        "constraints": {},
    }
    r = rd.evaluate(
        _doc(grid=GRID, house=HOUSE, battery=battery), models={"load_house": "trained"}
    )
    assert [(m["device"], m["role"]) for m in r["plan"]["missing"]] == [
        ("battery", "capacity")
    ]
    assert r["observe"]["state"] == "ready"

    # test_a_structurally_broken_document_is_unsupported_everywhere
    doc = _doc(grid=GRID, house=HOUSE)
    doc["devices"][1]["id"] = "grid"  # duplicate id
    r = rd.evaluate(doc)
    assert set(_states(r).values()) == {"unsupported"}
    assert r["plan"]["problems"]

    # test_dc_r10_score_is_marked_not_corrected_when_exchange_disagrees
    ok = {"models": {"load_house": "trained"}, "scored_days": 3}
    doc = _doc(grid=GRID, house=HOUSE)
    assert rd.evaluate(doc, **ok, reconciliation="agrees")["score"] == {
        "state": "ready",
        "reliability": None,
    }
    bad = rd.evaluate(doc, **ok, reconciliation="disagrees")["score"]
    assert (bad["state"], bad["reliability"]) == ("needs", "site_exchange_disagrees")
    unchecked = rd.evaluate(doc, **ok)["score"]
    assert unchecked["reliability"] == "site_exchange_not_checked"
    assert rd.evaluate(doc, models={"load_house": "trained"})["score"]["state"] == (
        "training"
    )

    # test_the_verdict_names_are_dc003s
    from tests.device_contract import oracles_accounting as oracles

    assert set(rd._RECONCILE_BLOCKS) == {
        oracles.DISAGREES,
        oracles.UNSUPPORTED,
        oracles.INSUFFICIENT_COVERAGE,
    }
