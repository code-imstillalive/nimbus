"""Spec DC-000 negative controls: a deliberate mutation must make the
reference expectation fail or be rejected. Production obligations (discovery,
safety, revision) stay visibly pending in the registry until their owning spec
implements them."""

from __future__ import annotations

import json
from pathlib import Path

from tests.device_contract.oracles import Flow, site_grid_kw, validate_inclusion

HERE = Path(__file__).parent
TOL = 1e-9
SITE = [
    Flow("pv", "solar", 16.0),
    Flow("house", "load", 14.0),
    Flow("ev_ac", "battery", -10.0, included_in="house"),
    Flow("ev_dc", "battery", -2.0),
]


def test_the_unmutated_site_balances() -> None:
    assert abs(site_grid_kw(SITE)) <= TOL


def test_accounting_mutation_adding_the_ac_ev_twice_fails() -> None:
    mutated = [*SITE, Flow("ev_ac_again", "battery", -10.0)]
    assert abs(site_grid_kw(mutated)) > TOL


def test_unit_mutation_watts_read_as_kilowatts_is_detected() -> None:
    mutated = [f if f.name != "house" else Flow("house", "load", 14000.0) for f in SITE]
    assert abs(site_grid_kw(mutated)) > TOL


def test_identity_mutations_are_rejected() -> None:
    assert validate_inclusion(SITE) == []
    dup = [*SITE, Flow("pv", "solar", 1.0)]
    assert any("duplicate" in p for p in validate_inclusion(dup))
    cycle = [
        Flow("a", "load", 1.0, included_in="b"),
        Flow("b", "load", 1.0, included_in="a"),
    ]
    assert any("cycle" in p for p in validate_inclusion(cycle))
    dangling = [Flow("a", "load", 1.0, included_in="nowhere")]
    assert any("undeclared" in p for p in validate_inclusion(dangling))
    self_ref = [Flow("a", "load", 1.0, included_in="a")]
    assert any("cycle" in p for p in validate_inclusion(self_ref))


def test_production_mutation_obligations_remain_visibly_pending() -> None:
    reg = json.loads((HERE / "acceptance_registry.json").read_text(encoding="utf-8"))
    for fid in ("F06", "F09", "F10", "F12"):  # discovery, safety, revision
        assert reg["fixtures"][fid]["status"]["production_obligation"] == "pending", fid


# F04, F05, F09, F11 negative controls -----------------------------------


def test_unit_mutation_case_folding_mw_is_detected() -> None:
    from tests.device_contract.oracles_signals import power_kw

    # A case-folding reader would make 2.5e9 mW and 2.5e9 MW the same thing.
    assert power_kw(2.5e9, "mW") != power_kw(2.5e9, "MW")


def test_coverage_mutation_dropping_an_interval_is_reported() -> None:
    from datetime import datetime

    from tests.device_contract.oracles_signals import PriceInterval, price_over

    t = datetime.fromisoformat
    full = [
        PriceInterval(
            t("2026-10-05T17:00:00+10:00"), t("2026-10-05T17:30:00+10:00"), 0.2
        ),
        PriceInterval(
            t("2026-10-05T17:30:00+10:00"), t("2026-10-05T18:00:00+10:00"), 0.2
        ),
    ]
    window = (t("2026-10-05T17:00:00+10:00"), t("2026-10-05T18:00:00+10:00"))
    assert price_over(full, *window) == (0.2, 1.0)
    # Same price, half the coverage: a reader that ignores coverage would
    # not notice the missing half hour.
    assert price_over(full[:1], *window) == (0.2, 0.5)


def test_inclusion_mutation_adding_a_child_on_top_fails() -> None:
    from tests.device_contract.oracles_structure import Node, inclusive_demand_kw

    nodes = [Node("house", 14.0), Node("pool", 2.0, inside="house")]
    flattened = [Node("house", 14.0), Node("pool", 2.0)]  # inclusion lost
    assert abs(inclusive_demand_kw(flattened) - inclusive_demand_kw(nodes)) > TOL


def test_dependency_mutation_closing_a_cycle_is_rejected() -> None:
    from tests.device_contract.oracles_structure import dependency_problems

    graph = {"a": {"b"}, "b": set()}
    assert dependency_problems(graph) == []
    graph["b"] = {"a"}
    assert any("cycle" in p for p in dependency_problems(graph))
