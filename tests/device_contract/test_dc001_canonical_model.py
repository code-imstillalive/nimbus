"""DC-001 acceptance (docs/specs/device-contract/001-canonical-device-model.md,
"Acceptance"): the canonical device model's structural validation, identity
and round-trip. Each test names the requirement and fixture it covers.

Pure: device_contract.py imports nothing from Home Assistant.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[2] / "custom_components" / "nimbus_load"
_spec = importlib.util.spec_from_file_location("_dc001", _PKG / "device_contract.py")
dc = importlib.util.module_from_spec(_spec)
sys.modules["_dc001"] = dc  # dataclasses look the module up while defining
_spec.loader.exec_module(dc)  # type: ignore[union-attr]


def _binding(entity_id, direction=None, registry_id=None):
    source = {"kind": "entity_state", "entity_id": entity_id}
    if registry_id:
        source["registry_id"] = registry_id
    out = {"source": source, "provenance": {"origin": "manual", "confirmed": True}}
    if direction:
        out["normalization"] = {
            "target_unit": "kW",
            "positive_direction": direction,
            "native_positive_direction": direction,
        }
    return out


def _minimal():
    """F01: one Grid and one aggregate whole-house Load (DC-R01)."""
    return {
        "schema_version": 1,
        "site": {
            "id": "site_home",
            "name": "Home",
            "timezone": "Australia/Brisbane",
            "currency": "AUD",
            "control_policy": "observation_only",
        },
        "devices": [
            {
                "id": "grid_main",
                "kind": "grid",
                "name": "Grid connection",
                "boundary": {
                    "id": "point_of_connection",
                    "domain": "ac",
                    "phases": "all",
                },
                "bindings": {"power": _binding("sensor.grid", "import", "r_grid")},
                "constraints": {},
            },
            {
                "id": "load_house",
                "kind": "load",
                "name": "Whole house",
                "load_class": "aggregate",
                "boundary": {"id": "total_end_use", "domain": "ac", "phases": "all"},
                "bindings": {
                    "power": _binding("sensor.house", "consumption", "r_house")
                },
                "flexibility": {"mode": "none"},
                "constraints": {},
            },
        ],
        "relationships": [
            {"kind": "supplies", "from": "grid_main", "to": "load_house"}
        ],
        "accounting": {
            "demand": {
                "mode": "inclusive",
                "root_load_id": "load_house",
                "separate_load_ids": [],
            }
        },
        "shared_constraints": [],
    }


def _codes(doc):
    return sorted(p.code for p in dc.validate(doc))


def _with(doc, device):
    doc = copy.deepcopy(doc)
    doc["devices"].append(device)
    return doc


# --- DC-R01 / F01 -----------------------------------------------------------


# --- DC-R02 / F06 -----------------------------------------------------------


def test_dc_r02_f06_reordering_devices_changes_neither_ids_nor_validation():
    # test_dc_r02_f06_reordering_devices_changes_neither_ids_nor_validation
    doc = _minimal()
    reordered = copy.deepcopy(doc)
    reordered["devices"].reverse()
    assert dc.validate(reordered) == dc.validate(doc) == []
    assert sorted(d["id"] for d in reordered["devices"]) == sorted(
        d["id"] for d in doc["devices"]
    )
    # The canonical forms differ only in list order.
    assert dc.serialise(reordered) != dc.serialise(doc)
    back = dc.parse(dc.serialise(reordered))
    back["devices"].reverse()
    assert dc.serialise(back) == dc.serialise(doc)

    # --- #1574 "Shared hardware" ------------------------------------------------
    # test_shared_hardware_one_integration_three_logical_devices
    doc = _with(
        _minimal(),
        {
            "id": "solar_roof",
            "kind": "solar",
            "name": "Roof",
            "boundary": {"id": "inverter_1_pv", "domain": "dc", "phases": "all"},
            "bindings": {"power": _binding("sensor.inv1_pv", "generation", "r_pv")},
        },
    )
    doc = _with(
        doc,
        {
            "id": "battery_home",
            "kind": "battery",
            "name": "Home battery",
            "boundary": {"id": "inverter_1_batt", "domain": "dc", "phases": "all"},
            "capacity": {"value": 122.2, "unit": "kWh", "basis": "usable"},
            "bindings": {"power": _binding("sensor.inv1_batt", "discharge", "r_batt")},
        },
    )
    doc["relationships"].append(
        {
            "kind": "shares_equipment",
            "devices": ["solar_roof", "battery_home"],
            "equipment": "inverter_1",
        }
    )
    assert dc.validate(doc) == []
    assert sorted(d["kind"] for d in doc["devices"]) == [
        "battery",
        "grid",
        "load",
        "solar",
    ]

    # test_f02_a_shared_inverter_limit_validates_and_a_capacity_needs_a_basis
    doc = _with(_minimal(), _solar(1, "sensor.inv1", "b1"))
    battery = {
        "id": "battery_home",
        "kind": "battery",
        "boundary": {"id": "b_batt", "domain": "dc", "phases": "all"},
        "capacity": {"value": 40.3, "unit": "kWh"},
    }
    doc = _with(doc, battery)
    doc["shared_constraints"].append(
        {"devices": ["solar_1", "battery_home"], "quantity": "ac_power", "limit_kw": 10}
    )
    assert _codes(doc) == ["capacity_basis_missing"]

    # test_every_problem_code_has_a_negative_case
    for code in _CODES:
        assert code in _codes(_bad(code)), (code, dc.validate(_bad(code)))

    # test_the_spec_table_and_the_tests_name_the_same_codes
    spec = (
        Path(__file__).resolve().parents[2]
        / "docs/specs/device-contract/001-canonical-device-model.md"
    ).read_text(encoding="utf-8")
    table = spec[
        spec.index("| code | rule |") : spec.index("What it deliberately does")
    ]
    import re

    # The codes are the first column only; the rule column names fields too.
    named = {
        code
        for line in table.splitlines()
        if line.startswith("| `")
        for code in re.findall(r"`([a-z_]+)`", line.split("|")[1])
    }
    assert named == set(_CODES)

    # test_parse_refuses_a_non_object
    with pytest.raises(TypeError):
        dc.parse("[]")

    # test_no_module_imports_the_device_contract_yet
    offenders = [
        str(p.relative_to(_PKG))
        for p in _PKG.rglob("*.py")
        if p.name not in _DC_BUILDERS
        and (
            "device_contract" in p.read_text(encoding="utf-8")
            or "device_import" in p.read_text(encoding="utf-8")
            or "device_storage" in p.read_text(encoding="utf-8")
        )
    ]
    assert offenders == []


# --- DC-R16 / F11 -----------------------------------------------------------


def _derived(device_id, role, depends_on):
    return {
        "source": {"kind": "derived", "calculation": "sum", "depends_on": depends_on},
        "provenance": {"origin": "derived", "confirmed": True},
    }


# --- F02, DC-001's structural slice ------------------------------------------


def _solar(i, entity, boundary):
    return {
        "id": f"solar_{i}",
        "kind": "solar",
        "boundary": {"id": boundary, "domain": "dc", "phases": "all"},
        "bindings": {"power": _binding(entity, "generation")},
    }


# --- one negative test per problem code (DC-000 non-vacuity) -----------------


def _bad(code):
    doc = _minimal()
    devices = doc["devices"]
    if code == "schema_version_unsupported":
        doc["schema_version"] = 2
    elif code == "id_invalid":
        devices[0]["id"] = "Grid-Main"
        doc["relationships"] = []
    elif code == "id_duplicate":
        devices[1]["boundary"]["id"] = "grid_main"
    elif code == "reference_unresolved":
        doc["relationships"][0]["to"] = "load_nowhere"
    elif code == "role_not_allowed_for_kind":
        devices[1]["bindings"]["soc"] = _binding("sensor.x")
    elif code == "sign_not_canonical":
        devices[0]["bindings"]["power"]["normalization"]["positive_direction"] = (
            "export"
        )
    elif code == "source_fields_missing":
        devices[0]["bindings"]["power"]["source"] = {"kind": "recorder_statistic"}
    elif code == "capacity_missing":
        devices.append({"id": "b", "kind": "battery"})
    elif code == "capacity_basis_missing":
        devices.append(
            {"id": "b", "kind": "battery", "capacity": {"value": 10, "unit": "kWh"}}
        )
    elif code == "constraint_key_not_allowed_for_kind":
        devices[1]["constraints"]["soc_min"] = 10
    elif code == "binding_source_duplicate":
        devices.append(
            {
                "id": "load_2",
                "kind": "load",
                "bindings": {"power": _binding("sensor.house", None, "r_house")},
            }
        )
    elif code == "soc_bounds_inverted":
        devices.append(
            {
                "id": "b",
                "kind": "battery",
                "capacity": {"value": 10, "unit": "kWh", "basis": "usable"},
                "constraints": {"soc_min": 90, "soc_max": 10},
            }
        )
    elif code == "relationship_self":
        doc["relationships"].append(
            {
                "kind": "included_in_measurement",
                "child": "load_house",
                "parent": "load_house",
            }
        )
    elif code == "inclusion_cycle":
        devices.append({"id": "load_pool", "kind": "load"})
        doc["relationships"] += [
            {
                "kind": "included_in_measurement",
                "child": "load_pool",
                "parent": "load_house",
            },
            {
                "kind": "included_in_measurement",
                "child": "load_house",
                "parent": "load_pool",
            },
        ]
    elif code == "dependency_cycle":
        devices[1]["bindings"]["energy"] = _derived(
            "load_house", "energy", ["load_house.energy"]
        )
    elif code == "combine_not_allowed":
        devices[0]["bindings"]["power"] = {
            "sources": [{"kind": "entity_state", "entity_id": "sensor.grid"}],
            "combine": "mean",
        }
    elif code == "combine_unknown":
        devices.append(
            {
                "id": "solar_1",
                "kind": "solar",
                "bindings": {
                    "generation_forecast": {
                        "sources": [
                            {
                                "kind": "entity_attribute",
                                "entity_id": "sensor.solar_forecast",
                                "attribute_path": "forecast",
                                "adapter": {"name": "nimbus_forecast", "version": 1},
                            }
                        ],
                        "combine": "median",
                    }
                },
            }
        )
    elif code == "accounting_kind_mismatch":
        doc["accounting"]["demand"]["root_load_id"] = "grid_main"
    elif code == "grid_missing":
        doc["devices"] = devices[1:]
        doc["relationships"] = []
    return doc


_CODES = (
    "schema_version_unsupported",
    "id_invalid",
    "id_duplicate",
    "reference_unresolved",
    "role_not_allowed_for_kind",
    "sign_not_canonical",
    "source_fields_missing",
    "capacity_missing",
    "capacity_basis_missing",
    "constraint_key_not_allowed_for_kind",
    "binding_source_duplicate",
    "soc_bounds_inverted",
    "relationship_self",
    "inclusion_cycle",
    "dependency_cycle",
    "accounting_kind_mismatch",
    "grid_missing",
    "combine_not_allowed",
    "combine_unknown",
)


# --- round trip ----------------------------------------------------------------


# --- no consumer reads it yet (until DC-006) ------------------------------------


#: The modules allowed to use the contract before DC-006. DC-004 builds and
#: keeps documents (import, store) and lets the household preview it and set
#: its mode (device_contract_ha, registered in services.py); none of them
#: feeds a document to a consumer. The solve, the cards and the forecaster
#: may not import any of them.
_DC_BUILDERS = frozenset(
    {
        "device_contract.py",
        "device_import.py",
        "device_storage.py",
        "device_contract_ha.py",
        "services.py",
        # DC-004 step 5b: the start-up recovery call, nothing else.
        "__init__.py",
        # DC-004 step 5c: number write-through and the drift Repair's flow.
        "number.py",
        "repairs.py",
        # The Device Setup Mode select (DC-004, 0.94.467).
        "select.py",
        # Readiness per function (DC-006).
        "device_readiness.py",
        # The readiness sensor (DC-006 slice 1b).
        "readiness_sensor.py",
        # Discovery's proposal (DC-005).
        "device_discovery.py",
        # DC-006 observation slice (#1893): which power sensors' units must
        # be declared before `active`, and the declaration. Validation only.
        "device_units.py",
        # DC-006 slice 2 (#1574): the Forecaster reads its battery, grid and
        # solar power inputs from the document, in `active` only.
        "coordinator.py",
        # DC-006 slice 3 (#1574): forecast adapters and `combine: mean` read
        # the combine roles from the contract.
        "device_forecasts.py",
        # DC-007 slice 2 (#1574): Configure -> Device setup, through the
        # device_contract_ha services only.
        "device_setup.py",
        # DC-006 slice 3 part 2 (#1574): in `active` the Solver reads its solar
        # forecast through the contract. sensor.py hands the binding over in
        # the Solver config; solver_inputs/solar.py reads it.
        "sensor.py",
        "solar.py",
    }
)
