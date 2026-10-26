"""Spec DC-000 F07: the exact-once accounting oracle, independent of
production accounting code.

The spec's synthetic hour: PV 16 kW; whole-house consumption 14 kW, of which
an AC EV charges 10 kW; a DC EV outside the house total charges 2 kW; BESS
idle. Grid import is 0 kW by either valid representation. Adding the AC EV on
top of the house total gives 10 kW and must fail.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.device_contract.oracles import (
    Flow,
    Segment,
    ac_side_kw,
    decompose,
    site_charge_kwh,
    site_grid_kw,
    site_grid_kwh,
)
from tests.device_contract.oracles_limits import (
    Member,
    SharedLimit,
    combined_kw,
    within,
    within_copied_limits,
)

TOL = 1e-9
F07 = json.loads(
    (
        Path(__file__).parent / "fixtures" / "F07_bess_ev_accounting" / "fixture.json"
    ).read_text(encoding="utf-8")
)


def _flows(**overrides: float) -> list[Flow]:
    return [
        Flow(
            f["name"],
            f["role"],
            overrides.get(f["name"], f["kw"]),
            f.get("included_in"),
        )
        for f in F07["data_semantics"]["flows"]
    ]


def test_manifest_numbers_are_the_specs():
    # test_manifest_numbers_are_the_specs
    kw = {f["name"]: f["kw"] for f in F07["data_semantics"]["flows"]}
    assert kw == {"pv": 16.0, "house": 14.0, "ev_ac": -10.0, "ev_dc": -2.0, "bess": 0.0}
    assert F07["comparison"]["absolute_tolerance"] == TOL

    # test_inclusive_representation_balances: Keep the house at 14 kW; add only the excluded DC EV.
    assert abs(site_grid_kw(_flows()) - F07["expected"]["grid_import_kw"]) <= TOL

    # test_decomposed_representation_balances_identically: House residual 14 - 10 = 4 kW, then the AC EV and the DC EV once.
    flows = _flows()
    house = next(f for f in flows if f.name == "house")
    ac = next(f for f in flows if f.name == "ev_ac")
    rest = [f for f in flows if f.name not in ("house", "ev_ac")]
    parts = decompose(house, [ac])
    assert abs(parts[0].kw - F07["expected"]["decomposed_house_residual_kw"]) <= TOL
    assert abs(site_grid_kw([*parts, *rest])) <= TOL
    assert abs(site_grid_kw([*parts, *rest]) - site_grid_kw(flows)) <= TOL

    # test_the_double_counted_sum_fails_the_expectation: 14 + 10 + 2 - 16 = 10 kW: the AC EV added on top of a total that
    double = [Flow(f.name, f.role, f.kw, None) for f in _flows()]
    got = site_grid_kw(double)
    assert abs(got - F07["expected"]["invalid_double_count_grid_kw"]) <= TOL
    assert abs(got - F07["expected"]["grid_import_kw"]) > TOL

    # test_changing_the_ac_ev_schedule_replaces_its_embedded_baseline: The AC EV now charges 6 kW: remove its embedded 10 kW, add 6 kW.
    flows = _flows()
    house = next(f for f in flows if f.name == "house")
    old_ac = next(f for f in flows if f.name == "ev_ac")
    residual, _ = decompose(house, [old_ac])
    rest = [f for f in flows if f.name not in ("house", "ev_ac")]
    grid = site_grid_kw([residual, Flow("ev_ac", "battery", -6.0), *rest])
    assert abs(grid - (4.0 + 6.0 + 2.0 - 16.0)) <= TOL  # -4 kW: exporting
    suppressed = site_grid_kw([residual, *rest])
    assert abs(suppressed - grid) > TOL

    # test_variants
    for overrides, expected in [
        ({"pv": 20.0}, -4.0),  # export variant
        ({"ev_ac": 0.0, "ev_dc": 0.0, "house": 4.0}, -12.0),  # no EV flow
        ({"bess": 3.0}, -3.0),  # BESS discharging
    ]:
        assert abs(site_grid_kw(_flows(**overrides)) - expected) <= TOL

    # test_declared_losses_apply_on_the_ac_side: A 90 % efficient DC charger putting 2 kW into the pack draws 2.222 kW
    dc_ac = ac_side_kw(-2.0, 0.9)
    assert abs(dc_ac - (-2.0 / 0.9)) <= TOL
    assert abs(site_grid_kw(_flows(ev_dc=dc_ac)) - (2.0 / 0.9 - 2.0)) <= TOL

    # test_partial_connection_counts_only_the_connected_part
    v = VARIANTS["partial_interval_connection"]
    segs = _segments(v["segments"])
    assert abs(site_grid_kwh(segs) - v["expected"]["grid_import_kwh"]) <= TOL
    assert (
        abs(site_charge_kwh(segs, "ev_dc") - v["expected"]["ev_dc_site_charge_kwh"])
        <= TOL
    )
    # Treating the EV as connected all hour at its connected rate is wrong.
    connected = segs[1]
    whole = site_grid_kwh([Segment(1.0, connected.flows)])
    assert abs(whole - v["expected"]["invalid_connected_all_interval_grid_kwh"]) <= TOL
    assert abs(whole - site_grid_kwh(segs)) > TOL


# F07 variants (DC-000 §F07: partial-interval connection, two vehicles on one
# charger, trip and reconnection) ------------------------------------------

VARIANTS = F07["variants"]


def _as_flows(raw: list[dict]) -> tuple[Flow, ...]:
    return tuple(Flow(f["name"], f["role"], f["kw"], f.get("included_in")) for f in raw)


def _segments(raw: list[dict]) -> list[Segment]:
    return [Segment(s["hours"], _as_flows(s["flows"])) for s in raw]


def test_partial_connection_matches_the_interval_mean():
    # test_partial_connection_matches_the_interval_mean: The site balance is linear, so the time-weighted segments equal the
    segs = _segments(VARIANTS["partial_interval_connection"]["segments"])
    assert abs(site_grid_kwh(segs) - site_grid_kw(_flows())) <= TOL

    # test_two_vehicles_on_one_charger_count_once_and_share_one_limit
    v = VARIANTS["two_vehicles_one_charger"]
    exp = v["expected"]
    c = v["charger"]
    limit = SharedLimit(
        c["equipment"], c["quantity"], tuple(c["devices"]), c["limit_kw"]
    )
    both = _as_flows(v["both_connected"])
    assert abs(site_grid_kw(both) - exp["both_connected_grid_kw"]) <= TOL
    members = _members(both, c["devices"])
    assert (
        abs(combined_kw(c["quantity"], members) - exp["both_connected_charger_kw"])
        <= TOL
    )
    assert within(limit, members) is exp["both_connected_within_charger"]
    # The rating copied onto each vehicle would let 14 kW through a 12 kW charger.
    assert (
        within_copied_limits(members, c["limit_kw"])
        is exp["both_connected_within_copied_limits"]
    )
    one = _as_flows(v["only_ev_a_drawing"])
    assert abs(site_grid_kw(one) - exp["only_ev_a_grid_kw"]) <= TOL
    assert within(limit, _members(one, c["devices"])) is exp["only_ev_a_within_charger"]

    # test_trip_and_reconnection_keep_driving_off_the_site
    v = VARIANTS["trip_and_reconnection"]
    exp = v["expected"]
    segs = _segments(v["segments"])
    grid = site_grid_kwh(segs)
    assert abs(grid - exp["grid_import_kwh"]) <= TOL
    charged = site_charge_kwh(segs, "ev")
    assert abs(charged - exp["ev_site_charge_kwh"]) <= TOL
    pack_delta = v["pack_kwh"]["end"] - v["pack_kwh"]["start"]
    # Lossless by declaration: what the site put in, less what the pack
    # gained, left on the road.
    assert abs((charged - pack_delta) - exp["driving_kwh_lossless"]) <= TOL
    house_only = sum(f.kw * s.hours for s in segs for f in s.flows if f.name == "house")
    from_pack = house_only + pack_delta
    assert abs(from_pack - exp["invalid_pack_delta_as_site_grid_kwh"]) <= TOL
    assert abs(from_pack - grid) > TOL
    held = site_grid_kwh([Segment(s.hours, segs[0].flows) for s in segs])
    assert abs(held - exp["invalid_setpoint_held_while_away_grid_kwh"]) <= TOL
    assert abs(held - grid) > TOL


def _members(flows: tuple[Flow, ...], names: list[str]) -> list[Member]:
    by = {f.name: f for f in flows}
    return [Member(n, by[n].role, by[n].kw) for n in names]
