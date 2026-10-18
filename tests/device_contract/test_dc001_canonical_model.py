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


def test_dc_r01_f01_the_minimum_site_validates() -> None:
    assert dc.validate(_minimal()) == []


def test_dc_r01_f01_an_unbound_grid_power_still_validates_and_carries_no_zero() -> None:
    doc = _minimal()
    del doc["devices"][0]["bindings"]["power"]
    assert dc.validate(doc) == []
    assert "power" not in doc["devices"][0]["bindings"]  # absent, never 0


def test_dc_r01_f01_removing_the_grid_gives_exactly_grid_missing() -> None:
    doc = _minimal()
    doc["devices"] = doc["devices"][1:]
    doc["relationships"] = []
    assert _codes(doc) == ["grid_missing"]


# --- DC-R02 / F06 -----------------------------------------------------------


def test_dc_r02_f06_a_rename_keeps_every_device_id() -> None:
    doc = _minimal()
    renamed = copy.deepcopy(doc)
    renamed["devices"][0]["bindings"]["power"]["source"]["entity_id"] = "sensor.renamed"
    assert dc.validate(renamed) == []
    assert [d["id"] for d in renamed["devices"]] == [d["id"] for d in doc["devices"]]
    src = renamed["devices"][0]["bindings"]["power"]["source"]
    assert src["registry_id"] == "r_grid"  # the reference of record


def test_dc_r02_f06_reordering_devices_changes_neither_ids_nor_validation() -> None:
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


# --- DC-R16 / F11 -----------------------------------------------------------


def _derived(device_id, role, depends_on):
    return {
        "source": {"kind": "derived", "calculation": "sum", "depends_on": depends_on},
        "provenance": {"origin": "derived", "confirmed": True},
    }


def test_dc_r16_f11_a_self_dependency_is_a_cycle() -> None:
    doc = _minimal()
    doc["devices"][1]["bindings"]["energy"] = _derived(
        "load_house", "energy", ["load_house.energy"]
    )
    assert "dependency_cycle" in _codes(doc)


def test_dc_r16_f11_a_three_cycle_is_a_cycle() -> None:
    doc = _minimal()
    doc["devices"][1]["bindings"]["energy"] = _derived(
        "load_house", "energy", ["grid_main.import_energy"]
    )
    doc["devices"][0]["bindings"]["import_energy"] = _derived(
        "grid_main", "import_energy", ["grid_main.export_energy"]
    )
    doc["devices"][0]["bindings"]["export_energy"] = _derived(
        "grid_main", "export_energy", ["load_house.energy"]
    )
    assert _codes(doc).count("dependency_cycle") == 1


def test_dc_r16_f11_an_acyclic_chain_validates() -> None:
    doc = _minimal()
    doc["devices"][1]["bindings"]["energy"] = _derived(
        "load_house", "energy", ["grid_main.power"]
    )
    assert dc.validate(doc) == []


# --- #1574 "Shared hardware" ------------------------------------------------


def test_shared_hardware_one_integration_three_logical_devices() -> None:
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


# --- F02, DC-001's structural slice ------------------------------------------


def _solar(i, entity, boundary):
    return {
        "id": f"solar_{i}",
        "kind": "solar",
        "boundary": {"id": boundary, "domain": "dc", "phases": "all"},
        "bindings": {"power": _binding(entity, "generation")},
    }


def test_f02_two_inverters_as_two_solar_devices_validate() -> None:
    doc = _with(
        _with(_minimal(), _solar(1, "sensor.inv1", "b_inv1")),
        _solar(2, "sensor.inv2", "b_inv2"),
    )
    assert dc.validate(doc) == []


def test_f02_one_inverters_strings_as_children_of_its_total_validate() -> None:
    doc = _minimal()
    for i, (entity, boundary) in enumerate(
        (
            ("sensor.inv1_total", "b_total"),
            ("sensor.inv1_s1", "b_s1"),
            ("sensor.inv1_s2", "b_s2"),
        )
    ):
        doc = _with(doc, _solar(i, entity, boundary))
    for child in ("solar_1", "solar_2"):
        doc["relationships"].append(
            {
                "kind": "included_in_measurement",
                "child": child,
                "parent": "solar_0",
                "quantity": "generation",
                "confirmed": True,
            }
        )
    assert dc.validate(doc) == []


def test_f02_the_same_total_on_two_solar_devices_is_a_duplicate() -> None:
    doc = _with(
        _with(_minimal(), _solar(1, "sensor.total", "b1")),
        _solar(2, "sensor.total", "b2"),
    )
    assert _codes(doc) == ["binding_source_duplicate"]


def test_f02_a_shared_inverter_limit_validates_and_a_capacity_needs_a_basis() -> None:
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


def test_f02_an_undeclared_string_plus_total_pair_validates_here_and_is_dc003s() -> (
    None
):
    """Two different sensors, a string and its total, undeclared: nothing
    structural says one contains the other. DC-001 hands this to DC-003's
    overlap and energy-balance check (DC-001 "Structural validation")."""
    doc = _with(
        _with(_minimal(), _solar(1, "sensor.inv1_total", "b_t")),
        _solar(2, "sensor.inv1_s1", "b_s"),
    )
    assert dc.validate(doc) == []


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
)


def test_a_magnitude_role_is_positive_in_its_own_direction() -> None:
    # Mark Purcell, #1728: the kind's sign applies to its signed `power`
    # role only. A Grid export_power and a Battery charge_power are
    # positive for export and charge.
    doc = _minimal()
    grid = doc["devices"][0]
    grid["bindings"]["import_power"] = _binding("sensor.imp", "import", "r_imp")
    grid["bindings"]["export_power"] = _binding("sensor.exp", "export", "r_exp")
    assert _codes(doc) == []
    grid["bindings"]["export_power"]["normalization"]["positive_direction"] = "import"
    assert _codes(doc) == ["sign_not_canonical"]


def test_a_role_without_a_flow_has_no_sign_to_check() -> None:
    doc = _minimal()
    doc["devices"][0]["bindings"]["import_price"] = _binding(
        "sensor.price", "import", "r_price"
    )
    assert _codes(doc) == []


@pytest.mark.parametrize("code", _CODES)
def test_every_problem_code_has_a_negative_case(code) -> None:
    assert code in _codes(_bad(code)), (code, dc.validate(_bad(code)))


def test_the_spec_table_and_the_tests_name_the_same_codes() -> None:
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


# --- round trip ----------------------------------------------------------------


@pytest.mark.parametrize("make", [_minimal, lambda: _bad("grid_missing")])
def test_round_trip(make) -> None:
    doc = make()
    doc["site"]["x_unknown_optional"] = {"kept": True}  # unknown fields survive
    text = dc.serialise(doc)
    assert dc.serialise(dc.parse(text)) == text
    assert dc.parse(text)["site"]["x_unknown_optional"] == {"kept": True}


def test_parse_refuses_a_non_object() -> None:
    with pytest.raises(TypeError):
        dc.parse("[]")


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
    }
)


def test_no_module_imports_the_device_contract_yet() -> None:
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


def test_a_battery_must_state_its_capacity() -> None:
    # Household, #1728 (14 Oct): capacity is required on every battery, so
    # a battery found before anyone has entered its size fails with exactly
    # this code and nothing else.
    doc = _with(_minimal(), {"id": "battery_found", "kind": "battery"})
    assert _codes(doc) == ["capacity_missing"]
    doc["devices"][-1]["capacity"] = {"value": 10, "unit": "kWh", "basis": "usable"}
    assert _codes(doc) == []
