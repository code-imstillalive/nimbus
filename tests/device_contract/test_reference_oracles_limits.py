"""Spec DC-000 F02: shared equipment and shared limits, read from the
fixture's manifest and independent of production code.

A shared limit is a combined quantity constrained once (DC-R09). The same
rating copied onto each member as its own limit lets their combined quantity
exceed it, and the fixture names the intervals where that happens.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.device_contract.oracles import Flow, site_grid_kw
from tests.device_contract.oracles_limits import (
    Member,
    SharedLimit,
    combined_kw,
    limit_problems,
    within,
    within_copied_limits,
)
from tests.device_contract.oracles_structure import Device, site_problems

TOL = 1e-9
F02 = json.loads(
    (
        Path(__file__).parent / "fixtures" / "F02_shared_equipment" / "fixture.json"
    ).read_text(encoding="utf-8")
)
CASES = F02["cases"]
EXP = F02["expected"]
KINDS: dict[str, str] = CASES["kinds"]
INTERVALS = {i["name"]: i for i in CASES["intervals"]}


def _limit(raw: dict) -> SharedLimit:
    return SharedLimit(
        raw["equipment"], raw["quantity"], tuple(raw["devices"]), raw["limit_kw"]
    )


def _members(interval: dict) -> list[Member]:
    return [Member(n, KINDS[n], kw) for n, kw in interval["kw"].items()]


def _flows(raw: list[dict]) -> list[Flow]:
    return [Flow(f["name"], f["role"], f["kw"], f.get("included_in")) for f in raw]


def test_manifest_is_f02_and_names_its_requirements():
    # test_manifest_is_f02_and_names_its_requirements
    assert F02["identity"]["fixture"] == "F02"
    assert set(F02["identity"]["requirements"]) == {"DC-R02", "DC-R03", "DC-R09"}
    assert F02["comparison"]["absolute_tolerance"] == TOL
    assert set(INTERVALS) == set(EXP["combined_kw"]) == set(EXP["within_shared_limit"])

    # test_one_integration_gives_separate_logical_devices: Grid, Solar and Battery supplied by one inverter are three logical
    case = CASES["shared_hardware"]
    devices = [Device(d["id"], d["role"], d.get("fields", {})) for d in case["devices"]]
    assert site_problems(devices) == EXP["shared_hardware_problems"]
    assert len({d.id for d in devices}) == len(devices)

    # test_the_site_limits_are_valid
    limits = [_limit(raw) for raw in CASES["limits"].values()]
    assert limit_problems(limits, KINDS) == []

    # test_combined_quantity_and_shared_limit
    for name in sorted(INTERVALS):
        interval = INTERVALS[name]
        limit = _limit(CASES["limits"][interval["limit"]])
        members = _members(interval)
        assert (
            abs(combined_kw(limit.quantity, members) - EXP["combined_kw"][name]) <= TOL
        )
        assert within(limit, members) is EXP["within_shared_limit"][name]

    # test_a_limit_copied_onto_each_member_misses_exactly_the_named_intervals
    for name in sorted(INTERVALS):
        interval = INTERVALS[name]
        limit = _limit(CASES["limits"][interval["limit"]])
        members = _members(interval)
        missed = within_copied_limits(members, limit.limit_kw) and not within(
            limit, members
        )
        assert missed is (name in EXP["copied_limits_miss"])

    # test_a_charging_and_a_discharging_vehicle_do_not_cancel: Both load the charger: 15 kW out and 15 kW in is 30 kW through it,
    members = _members(INTERVALS["heavy_v2h_and_charging"])
    assert abs(sum(m.kw for m in members)) <= TOL
    assert abs(combined_kw("charger_power", members) - 30.0) <= TOL

    # test_invalid_shared_limits_are_rejected
    for name in sorted(CASES["invalid_limits"]):
        limits = [_limit(raw) for raw in CASES["invalid_limits"][name]]
        needle = EXP["invalid_limit_needles"][name]
        assert any(needle in p for p in limit_problems(limits, KINDS)), name

    # test_a_missing_member_reading_is_not_a_zero
    limit = _limit(CASES["limits"]["charger"])
    with pytest.raises(ValueError, match="no reading"):
        within(limit, [Member("ev_a", "battery", -20.0)])


def test_strings_inside_their_total_count_once():
    # test_strings_inside_their_total_count_once
    flows = _flows(CASES["string_and_total"])
    assert abs(site_grid_kw(flows) - EXP["string_and_total_grid_kw"]) <= TOL
    double = [Flow(f.name, f.role, f.kw) for f in flows]
    got = site_grid_kw(double)
    assert abs(got - EXP["string_and_total_double_count_grid_kw"]) <= TOL
    assert abs(got - EXP["string_and_total_grid_kw"]) > TOL

    # test_two_inverters_are_both_counted
    flows = _flows(CASES["two_inverters"])
    assert abs(site_grid_kw(flows) - EXP["two_inverters_grid_kw"]) <= TOL
    partial = [f for f in flows if f.name != "inverter_2"]
    assert abs(site_grid_kw(partial) - EXP["two_inverters_one_missed_grid_kw"]) <= TOL

    # test_undeclared_overlap_is_evidence_only_where_it_should_be: DC-003 decision 5: a string inside its total is flagged; half a day,
    for name in sorted(PAIRS):
        from tests.device_contract.oracles_accounting import (
            possible_undeclared_inclusion,
        )

        pair = PAIRS[name]
        n = pair["first_hours"]
        child = OVERLAP["series"][pair["child"]][:n]
        parent = OVERLAP["series"][pair["parent"]][:n]
        hours = [OVERLAP["hours"]] * n
        assert possible_undeclared_inclusion(child, parent, hours) is pair["flagged"]


OVERLAP = F02["undeclared_overlap"]
PAIRS = {p["name"]: p for p in OVERLAP["pairs"]}
