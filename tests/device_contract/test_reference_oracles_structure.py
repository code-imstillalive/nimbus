"""Spec DC-000 F01, F05, F11: structural reference oracles, read from each
fixture's manifest and independent of production code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.device_contract.oracles_structure import (
    Device,
    Node,
    demand_with_schedule_kw,
    dependency_problems,
    forecast_ready,
    inclusive_demand_kw,
    observed_grid_kw,
    planner_supported,
    residuals_kw,
    revision_problems,
    site_problems,
    tree_problems,
)

TOL = 1e-9
FIX = Path(__file__).parent / "fixtures"


def _manifest(name: str) -> dict:
    return json.loads((FIX / name / "fixture.json").read_text(encoding="utf-8"))


F01 = _manifest("F01_minimal_site")
F05 = _manifest("F05_nested_inclusion")
F11 = _manifest("F11_dependencies_and_readiness")


def _devices(case: dict) -> list[Device]:
    return [Device(d["id"], d["role"], d.get("fields", {})) for d in case["devices"]]


def _flows(case: dict) -> list[tuple[str, float | None]]:
    return [(role, kw) for role, kw in case["flows"]]


# F01: minimal site ------------------------------------------------------


def test_f01_site_structure():
    # test_f01_site_structure
    for name in ["minimal", "unresolved_grid", "no_load", "battery_field_on_load"]:
        case, expected = F01["cases"][name], F01["expected"][name]
        assert site_problems(_devices(case)) == expected["problems"]

    # test_f01_grid_and_load_alone_need_nothing_battery_specific
    assert site_problems([Device("grid", "grid"), Device("house", "load")]) == []

    # test_f01_grid_observation
    for name in ["minimal", "unresolved_grid", "balanced_without_meter"]:
        case, expected = F01["cases"][name], F01["expected"][name]
        got = observed_grid_kw(case["grid_measured_kw"], _flows(case))
        if expected["grid_kw"] is None:
            assert got is None  # unresolved, never an invented zero
        else:
            assert got is not None and abs(got - expected["grid_kw"]) <= TOL

    # test_f05_tree_is_valid_and_demand_is_inclusive
    assert tree_problems(NODES) == []
    assert (
        abs(inclusive_demand_kw(NODES) - F05["expected"]["inclusive_demand_kw"]) <= TOL
    )

    # test_f05_residuals_count_each_load_once
    res = residuals_kw(NODES)
    assert res == pytest.approx(F05["expected"]["residuals_kw"], abs=TOL)
    assert abs(sum(res.values()) - inclusive_demand_kw(NODES)) <= TOL

    # test_f05_the_flat_sum_is_the_double_count
    flat = sum(n.kw for n in NODES)
    assert abs(flat - F05["expected"]["invalid_flat_sum_kw"]) <= TOL
    assert abs(flat - inclusive_demand_kw(NODES)) > TOL

    # test_f05_a_schedule_replaces_its_baseline_once
    got = demand_with_schedule_kw(NODES, "pool", F05["cases"]["pool_schedule_kw"])
    assert abs(got - F05["expected"]["demand_with_pool_schedule_kw"]) <= TOL
    # Suppressing the pool is not the correction: it changes demand.
    assert abs(demand_with_schedule_kw(NODES, "pool", 0.0) - got) > TOL

    # test_f05_cyclic_and_conflicting_trees_are_rejected
    assert any("cycle" in p for p in tree_problems(_nodes(F05["cases"]["cyclic"])))
    assert any(
        "children of circuit" in p
        for p in tree_problems(_nodes(F05["cases"]["conflicting"]))
    )


# F05: nested inclusion --------------------------------------------------


def _nodes(raw: list[dict]) -> list[Node]:
    return [Node(n["name"], n["kw"], n.get("inside")) for n in raw]


NODES = _nodes(F05["data_semantics"]["nodes"])


# F11: dependencies, revisions, readiness --------------------------------


def _graph(raw: dict) -> dict[str, set[str]]:
    return {k: set(v) for k, v in raw.items()}


def test_f11_a_valid_graph_has_no_problems():
    # test_f11_a_valid_graph_has_no_problems
    assert (
        dependency_problems(_graph(F11["cases"]["valid"])) == F11["expected"]["valid"]
    )

    # test_f11_bad_graphs_are_rejected
    for name, needle in [
        ("self", "itself"),
        ("cycle", "cycle"),
        ("undeclared", "undeclared"),
    ]:
        assert any(needle in p for p in dependency_problems(_graph(F11["cases"][name])))

    # test_f11_consumers_share_one_revision
    assert revision_problems(F11["cases"]["revisions_ok"]) == []
    assert (
        revision_problems(F11["cases"]["revisions_split"])
        == F11["expected"]["revisions_split"]
    )

    # test_f11_readiness_follows_capability
    for source, trained, ready in F11["cases"]["readiness"]:
        assert forecast_ready(source, trained) is ready

    # test_f11_several_grid_connections_are_explicitly_unsupported
    for connections, supported in F11["cases"]["grid_connections"]:
        assert planner_supported(connections) is supported
