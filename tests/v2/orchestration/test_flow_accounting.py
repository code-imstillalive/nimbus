"""Where every kW of a period went, and what each flow was worth (#264,
#629) (#1837).

Each period's solar, load, charge and discharge split into the seven
physical flows plus losses and curtailment, in merit order, conserving
every terminal and never inventing an export the plan did not make. The
economics then price each flow and split the saving into solar, battery
and their interaction, which always add up.
"""

from __future__ import annotations

import numpy as np
import pytest
import solver_publish as publish
from hypothesis import given, settings
from hypothesis import strategies as st

FLOWS = (
    "pv_to_load",
    "pv_to_battery",
    "pv_to_grid",
    "battery_to_load",
    "battery_to_grid",
    "grid_to_load",
    "grid_to_battery",
    "battery_to_losses",
)
KW = st.floats(min_value=0.0, max_value=20.0, allow_nan=False)


def _conserves(flow, solar, load, charge, discharge, export):
    tol = 1e-6
    pv = (
        flow["pv_to_load"]
        + flow["pv_to_battery"]
        + flow["pv_to_grid"]
        + flow["pv_to_curtailment"]
    )
    assert pv == pytest.approx(solar, abs=tol)
    assert flow["pv_to_load"] + flow["battery_to_load"] + flow[
        "grid_to_load"
    ] == pytest.approx(load, abs=tol)
    assert flow["pv_to_battery"] + flow["grid_to_battery"] == pytest.approx(
        charge, abs=tol
    )
    assert flow["battery_to_load"] + flow["battery_to_grid"] + flow[
        "battery_to_losses"
    ] == (pytest.approx(discharge, abs=tol))
    assert (
        flow["pv_to_grid"] + flow["battery_to_grid"] <= export + tol
    )  # no phantom export
    assert all(v >= -tol for v in flow.values())


@settings(max_examples=300, deadline=None)
@given(solar=KW, load=KW, charge=KW, discharge=KW, export=KW)
def test_every_terminal_is_conserved_and_no_export_is_invented(
    solar, load, charge, discharge, export
):
    flow = publish._flow_decomposition(
        solar, load, charge, discharge, grid_export_kw_i=export
    )
    _conserves(flow, solar, load, charge, discharge, export)


def test_the_flows_follow_the_merit_order():
    for args, export, expect in [
        # Solar serves the load, then the battery, then the grid.
        (
            (10.0, 2.0, 5.0, 0.0),
            3.0,
            {"pv_to_load": 2.0, "pv_to_battery": 5.0, "pv_to_grid": 3.0},
        ),
        ((10.0, 2.0, 12.0, 0.0), 0.0, {"pv_to_battery": 8.0, "grid_to_battery": 4.0}),
        # The battery serves the load before it is credited with an export.
        ((2.0, 5.0, 0.0, 10.0), 7.0, {"battery_to_load": 3.0, "battery_to_grid": 7.0}),
        ((0.0, 10.0, 0.0, 4.0), 0.0, {"battery_to_load": 4.0, "grid_to_load": 6.0}),
        # A wash trade shows both legs; with no export the discharge is lost.
        ((0.0, 0.0, 3.0, 3.0), 0.0, {"grid_to_battery": 3.0, "battery_to_losses": 3.0}),
        # #629: what the export limit kept off the grid is losses or
        # curtailment, never a phantom export.
        (
            (0.931, 3.481, 0.0, 2.897),
            0.0,
            {"battery_to_grid": 0.0, "battery_to_losses": 0.347},
        ),
        (
            (2.761, 2.428, 0.0, 0.428),
            0.0,
            {"pv_to_curtailment": 0.333, "battery_to_losses": 0.428},
        ),
        (
            (6.0, 2.0, 0.0, 7.0),
            6.0,
            {"pv_to_grid": 4.0, "battery_to_grid": 2.0, "battery_to_losses": 5.0},
        ),
    ]:
        flow = publish._flow_decomposition(*args, grid_export_kw_i=export)
        for key, value in expect.items():
            assert flow[key] == pytest.approx(value, abs=1e-6), key


def test_solar_held_back_by_forecast_risk_is_its_own_margin_not_curtailment():
    flows = publish._flow_decomposition_counted(
        [6.0], [4.0], [2.0], [0.0], [0.0], [2.0]
    )
    assert flows[0]["pv_risk_margin"] == pytest.approx(2.0)
    assert flows[0]["pv_to_curtailment"] == pytest.approx(0.0)
    unrisked = publish._flow_decomposition_counted(
        [6.0], [], [2.0], [0.0], [0.0], [4.0]
    )
    assert unrisked[0]["pv_risk_margin"] == 0.0


def _flows(**kw):
    return {**dict.fromkeys(FLOWS, 0.0), **kw}


def _economics(flows, imp, exp, hours=None, rte=0.9, soc=0.0):
    n = len(flows)
    return publish._compute_flow_economics(
        flows,
        np.asarray(imp, dtype=float),
        np.asarray(exp, dtype=float),
        np.asarray(hours if hours is not None else [0.25] * n, dtype=float),
        round_trip_efficiency=rte,
        initial_soc_kwh=soc,
    )


@settings(max_examples=100, deadline=None)
@given(
    st.lists(
        st.fixed_dictionaries({k: st.floats(0, 5) for k in FLOWS}),
        min_size=1,
        max_size=6,
    ),
    st.floats(0.05, 0.6),
)
def test_the_saving_always_splits_into_solar_battery_and_their_interaction(
    flows, price
):
    n = len(flows)
    for r in _economics(flows, [price] * n, [price / 4] * n, soc=5.0):
        parts = r["savings_pv"] + r["savings_battery"] + r["savings_interaction"]
        assert r["savings_combined"] == pytest.approx(parts, abs=2e-4)


def test_without_a_battery_or_without_solar_that_part_saves_nothing():
    # test_without_a_battery_or_without_solar_that_part_saves_nothing
    (pv_only,) = _economics(
        [_flows(pv_to_load=3.0, pv_to_grid=2.0, grid_to_load=1.0)], [0.3], [0.05]
    )
    assert pv_only["savings_battery"] == pv_only["savings_interaction"] == 0.0
    assert pv_only["savings_combined"] == pytest.approx(pv_only["savings_pv"])
    no_solar = _economics(
        [_flows(grid_to_load=2.0, grid_to_battery=1.0), _flows(battery_to_load=1.0)],
        [0.3, 0.3],
        [0.05, 0.05],
    )
    assert all(r["savings_pv"] == 0.0 for r in no_solar)

    # test_stored_energy_costs_its_weighted_average_and_keeps_it_while_idle
    blended = _economics(
        [_flows(pv_to_battery=4.0), _flows(grid_to_battery=4.0)],
        [0.3, 0.5],
        [0.05, 0.05],
        [1.0, 1.0],
        rte=1.0,
    )
    assert blended[0]["flow_battery_cost_basis"] == pytest.approx(0.0)
    assert blended[1]["flow_battery_cost_basis"] == pytest.approx(0.25)
    idle = _economics(
        [_flows(grid_to_battery=4.0), _flows(), _flows(battery_to_load=2.0)],
        [0.4] * 3,
        [0.05] * 3,
        [1.0] * 3,
        rte=1.0,
    )
    assert idle[1]["flow_battery_cost_basis"] == pytest.approx(0.4)
    assert idle[2]["flow_battery_cost_basis"] == pytest.approx(0.4)

    # test_cheap_charge_sold_dear_shows_the_margin_not_the_raw_export_price
    arbitrage = _economics(
        [_flows(grid_to_battery=4.0), _flows(battery_to_grid=4.0)],
        [0.1, 0.1],
        [0.05, 0.8],
        [1.0, 1.0],
        rte=1.0,
    )
    assert arbitrage[1]["flow_price_battery_to_grid"] == pytest.approx(0.70)
    assert arbitrage[1]["savings_battery"] > 0
