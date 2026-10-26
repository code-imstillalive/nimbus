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


def test_the_unmutated_site_balances():
    # test_the_unmutated_site_balances
    # test_the_unmutated_site_balances
    assert abs(site_grid_kw(SITE)) <= TOL

    # test_accounting_mutation_adding_the_ac_ev_twice_fails
    mutated = [*SITE, Flow("ev_ac_again", "battery", -10.0)]
    assert abs(site_grid_kw(mutated)) > TOL

    # test_unit_mutation_watts_read_as_kilowatts_is_detected
    mutated = [f if f.name != "house" else Flow("house", "load", 14000.0) for f in SITE]
    assert abs(site_grid_kw(mutated)) > TOL

    # test_identity_mutations_are_rejected
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

    # test_production_mutation_obligations_remain_visibly_pending
    reg = json.loads((HERE / "acceptance_registry.json").read_text(encoding="utf-8"))
    for fid in ("F06", "F09", "F10", "F12"):  # discovery, safety, revision
        assert reg["fixtures"][fid]["status"]["production_obligation"] == "pending", fid

    # test_unit_mutation_case_folding_mw_is_detected
    from tests.device_contract.oracles_signals import power_kw

    # A case-folding reader would make 2.5e9 mW and 2.5e9 MW the same thing.
    assert power_kw(2.5e9, "mW") != power_kw(2.5e9, "MW")

    # test_coverage_mutation_dropping_an_interval_is_reported
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

    # test_inclusion_mutation_adding_a_child_on_top_fails
    from tests.device_contract.oracles_structure import Node, inclusive_demand_kw

    nodes = [Node("house", 14.0), Node("pool", 2.0, inside="house")]
    flattened = [Node("house", 14.0), Node("pool", 2.0)]  # inclusion lost
    assert abs(inclusive_demand_kw(flattened) - inclusive_demand_kw(nodes)) > TOL

    # test_dependency_mutation_closing_a_cycle_is_rejected
    # test_dependency_mutation_closing_a_cycle_is_rejected
    from tests.device_contract.oracles_structure import dependency_problems

    graph = {"a": {"b"}, "b": set()}
    assert dependency_problems(graph) == []
    graph["b"] = {"a"}
    assert any("cycle" in p for p in dependency_problems(graph))

    # test_limit_mutation_copying_the_rating_onto_each_member_is_detected
    from tests.device_contract.oracles_limits import (
        Member,
        SharedLimit,
        within,
        within_copied_limits,
    )

    members = [Member("hws_a", "load", 3.7), Member("hws_b", "load", 3.7)]
    shared = SharedLimit("hws_circuit", "circuit_power", ("hws_a", "hws_b"), 5.0)
    # Copied per member, each heater passes; stated once, the pair does not.
    assert within_copied_limits(members, 5.0)
    assert not within(shared, members)

    # test_limit_mutation_netting_a_charger_hides_an_overload
    from tests.device_contract.oracles_limits import Member, combined_kw

    members = [Member("ev_a", "battery", 15.0), Member("ev_b", "battery", -15.0)]
    netted = abs(sum(m.kw for m in members))
    assert netted <= 25.0  # a netting reader would see an idle charger
    assert combined_kw("charger_power", members) > 25.0

    # test_limit_mutation_stating_a_limit_twice_is_rejected
    from tests.device_contract.oracles_limits import SharedLimit, limit_problems

    kinds = {"pv": "solar", "bat": "battery"}
    once = [SharedLimit("inv", "inverter_ac_power", ("pv", "bat"), 10.0)]
    assert limit_problems(once, kinds) == []
    twice = [*once, SharedLimit("inv", "inverter_ac_power", ("pv", "bat"), 8.0)]
    assert any("twice" in p for p in limit_problems(twice, kinds))

    # test_reconciliation_mutation_a_double_count_turns_agrees_into_disagrees
    from tests.device_contract.oracles_accounting import Meter, reconcile

    meter = Meter(50.0, 40.0, "positive_is_import")
    assert reconcile(51.0, 39.0, meter, 0.0).status == "agrees"
    assert reconcile(51.0 + 10.0, 39.0, meter, 0.0).status == "disagrees"

    # test_reconciliation_mutation_equal_errors_cannot_hide_in_a_net
    from tests.device_contract.oracles_accounting import Meter, reconcile

    meter = Meter(50.0, 40.0, "positive_is_import")
    # +12 kWh on both directions leaves the net exactly right.
    got = reconcile(62.0, 52.0, meter, 0.0)
    assert (62.0 - 52.0) == (50.0 - 40.0)
    assert got.status == "disagrees"

    # test_reconciliation_mutation_a_declared_sign_is_not_rescued
    from tests.device_contract.oracles_accounting import Meter, reconcile

    backwards = Meter(3.0, 12.0, "positive_is_import")
    legacy = Meter(3.0, 12.0, None)
    assert reconcile(12.0, 3.0, legacy, 0.0).status == "agrees"
    assert reconcile(12.0, 3.0, backwards, 0.0).status == "disagrees"

    # test_residual_mutation_clamping_breaks_the_inclusive_total
    from tests.device_contract.oracles_accounting import (
        decomposition_verdict,
        residual_series,
    )

    series = {"house": [14.0, 14.0], "circuit": [6.0, 6.0], "pool": [2.0, 6.6]}
    inside = {"house": None, "circuit": "house", "pool": "circuit"}
    res = residual_series(series, inside)
    clamped = {k: [max(0.0, v) for v in vals] for k, vals in res.items()}
    assert abs(sum(r[1] for r in res.values()) - 14.0) <= TOL
    assert abs(sum(r[1] for r in clamped.values()) - 14.0) > TOL
    assert decomposition_verdict(series, inside).negative == {"circuit": [1]}

    # test_boundary_mutation_moving_a_child_to_dc_is_rejected
    # test_boundary_mutation_moving_a_child_to_dc_is_rejected
    from tests.device_contract.oracles_accounting import Party, inclusion_problems

    house = Party("house", "load", "ac")
    ev = Party("ev", "battery", "ac")
    assert inclusion_problems(ev, house, "consumption") == []
    dc_ev = Party("ev", "battery", "dc")
    assert inclusion_problems(dc_ev, house, "consumption") == [
        "inclusion_boundary_mismatch"
    ]
    magnitude_house = Party("house", "load", "ac", signed=False)
    assert inclusion_problems(ev, magnitude_house, "consumption") == [
        "inclusion_sign_ambiguous"
    ]

    # test_mobility_mutation_reading_at_home_as_connected_is_detected
    from tests.device_contract.oracles_accounting import (
        VehicleInterval,
        vehicle_site_kwh,
    )

    honest = [VehicleInterval(1.0, -7.0, None, at_home=True)]
    as_if_connected = [VehicleInterval(1.0, -7.0, True, at_home=True)]
    assert vehicle_site_kwh(honest) == "mobility_unresolved"
    assert vehicle_site_kwh(as_if_connected) == 7.0

    # test_overlap_mutation_shuffling_the_child_removes_the_evidence
    import math

    from tests.device_contract.oracles_accounting import possible_undeclared_inclusion

    parent = [
        10 * math.sin(math.pi * (h - 6) / 12) if 6 < h < 18 else 0.0 for h in range(24)
    ]
    child = [0.45 * p for p in parent]
    hours = [1.0] * 24
    assert possible_undeclared_inclusion(child, parent, hours)
    shuffled = child[12:] + child[:12]  # same values, wrong hours
    assert not possible_undeclared_inclusion(shuffled, parent, hours)


# F04, F05, F09, F11 negative controls -----------------------------------


# F02 shared-limit negative controls ---------------------------------------


# DC-003 accounting oracles: reconciliation, residuals, boundaries, mobility,
# overlap ----------------------------------------------------------------------
