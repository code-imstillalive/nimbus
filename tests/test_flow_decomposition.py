"""Real tests of solver_writer._flow_decomposition() and
solver_writer._compute_flow_economics() -- the seven-flow merit-order
decomposition + shadow-price/savings model added 2026-08-28 for nimbus
issue #264 (Mark Purcell), extending v0.94.15's 2-way
_dispatch_source_breakdown() to all four real bus terminals.

Imports the REAL functions directly (not a reimplementation) --
solver_writer.py has no homeassistant.* imports at module scope, so no
HA stubs are needed here, matching every other pure-function test in
this directory (see test_dispatch_source_breakdown.py).
"""

from __future__ import annotations

import _solver_path  # noqa: F401
import numpy as np
import solver_writer

_flow_decomposition = solver_writer._flow_decomposition
_compute_flow_economics = solver_writer._compute_flow_economics


def _assert_invariants(
    flow: dict[str, float],
    solar_kw_i: float,
    load_kw_i: float,
    charge_kw_i: float,
    discharge_kw_i: float,
) -> None:
    """The four invariants that hold by construction, always -- see
    _flow_decomposition()'s own docstring."""
    tol = 1e-9
    assert (
        abs(
            (flow["pv_to_load"] + flow["pv_to_battery"] + flow["pv_to_grid"])
            - solar_kw_i
        )
        < tol
    )
    assert (
        abs(
            (flow["pv_to_load"] + flow["battery_to_load"] + flow["grid_to_load"])
            - load_kw_i
        )
        < tol
    )
    assert abs((flow["pv_to_battery"] + flow["grid_to_battery"]) - charge_kw_i) < tol
    assert (
        abs((flow["battery_to_load"] + flow["battery_to_grid"]) - discharge_kw_i) < tol
    )
    # Every flow is a real physical magnitude -- never negative.
    for k, v in flow.items():
        assert v >= -tol, f"{k}={v} is negative"


def test_charging_entirely_from_solar_surplus():
    flow = _flow_decomposition(10.0, 2.0, 5.0, 0.0)
    _assert_invariants(flow, 10.0, 2.0, 5.0, 0.0)
    assert flow["pv_to_battery"] == 5.0
    assert flow["grid_to_battery"] == 0.0
    assert flow["pv_to_load"] == 2.0
    assert flow["pv_to_grid"] == 3.0


def test_charging_split_between_solar_surplus_and_grid_topup():
    flow = _flow_decomposition(10.0, 2.0, 12.0, 0.0)
    _assert_invariants(flow, 10.0, 2.0, 12.0, 0.0)
    assert flow["pv_to_battery"] == 8.0
    assert flow["grid_to_battery"] == 4.0
    assert flow["pv_to_grid"] == 0.0


def test_charging_entirely_from_grid_when_no_solar():
    flow = _flow_decomposition(0.0, 1.5, 6.964, 0.0)
    _assert_invariants(flow, 0.0, 1.5, 6.964, 0.0)
    assert flow["pv_to_battery"] == 0.0
    assert flow["grid_to_battery"] == 6.964
    assert flow["grid_to_load"] == 1.5


def test_discharging_entirely_to_load_when_load_exceeds_discharge():
    flow = _flow_decomposition(0.0, 10.0, 0.0, 4.0)
    _assert_invariants(flow, 0.0, 10.0, 0.0, 4.0)
    assert flow["battery_to_load"] == 4.0
    assert flow["battery_to_grid"] == 0.0
    assert flow["grid_to_load"] == 6.0


def test_discharging_split_between_load_and_export():
    flow = _flow_decomposition(2.0, 5.0, 0.0, 10.0)
    _assert_invariants(flow, 2.0, 5.0, 0.0, 10.0)
    assert flow["battery_to_load"] == 3.0
    assert flow["battery_to_grid"] == 7.0
    assert flow["pv_to_load"] == 2.0


def test_idle_battery_all_grid_and_solar_only():
    flow = _flow_decomposition(5.0, 5.0, 0.0, 0.0)
    _assert_invariants(flow, 5.0, 5.0, 0.0, 0.0)
    assert flow["pv_to_load"] == 5.0
    assert flow["pv_to_battery"] == 0.0
    assert flow["battery_to_load"] == 0.0


def test_simultaneous_charge_and_discharge_wash_trade_surfaces_both_flows():
    """The real reason this function takes separate charge_kw_i/
    discharge_kw_i instead of the issue's own single net_battery_kw
    sketch -- a same-period wash trade (#245's own known LP degeneracy,
    both charge AND discharge nonzero in the same period) must show up
    as a real, visible battery_to_grid AND grid_to_battery pair, not be
    silently netted away before this function ever sees it."""
    # No solar, no load -- purely a wash trade: 3kW charge + 3kW
    # discharge in the same period, nothing else going on.
    flow = _flow_decomposition(0.0, 0.0, 3.0, 3.0)
    _assert_invariants(flow, 0.0, 0.0, 3.0, 3.0)
    assert flow["grid_to_battery"] == 3.0, (
        "wash-trade charge must be visible, not netted to zero"
    )
    assert flow["battery_to_grid"] == 3.0, (
        "wash-trade discharge must be visible, not netted to zero"
    )


def test_zero_everywhere_is_all_zero_flows():
    flow = _flow_decomposition(0.0, 0.0, 0.0, 0.0)
    _assert_invariants(flow, 0.0, 0.0, 0.0, 0.0)
    assert all(v == 0.0 for v in flow.values())


# ─────────────────────────────────────────────────────────────
# _compute_flow_economics()
# ─────────────────────────────────────────────────────────────


def _one_period_flow(**overrides) -> dict[str, float]:
    base = {
        "pv_to_load": 0.0,
        "pv_to_battery": 0.0,
        "pv_to_grid": 0.0,
        "battery_to_load": 0.0,
        "battery_to_grid": 0.0,
        "grid_to_load": 0.0,
        "grid_to_battery": 0.0,
    }
    base.update(overrides)
    return base


def test_savings_identity_holds_every_period():
    """Combined == PV + Battery + Interaction, by construction, every
    single period -- the whole point of building combined_savings from
    this same flow decomposition's own reconstructed quantities rather
    than a second, independently-passed copy of the real LP arrays."""
    flows = [
        _one_period_flow(pv_to_load=2.0, pv_to_grid=1.0, grid_to_load=1.0),
        _one_period_flow(grid_to_battery=3.0),
        _one_period_flow(battery_to_load=2.0, battery_to_grid=1.0),
        _one_period_flow(pv_to_battery=2.0, pv_to_load=1.0),
    ]
    import_price = np.array([0.30, 0.35, 0.40, 0.10])
    export_price = np.array([0.05, 0.06, 0.07, 0.02])
    period_hours = np.array([0.25, 0.25, 0.25, 0.25])

    results = _compute_flow_economics(
        flows,
        import_price,
        export_price,
        period_hours,
        round_trip_efficiency=0.90,
        initial_soc_kwh=5.0,
    )
    for r in results:
        combined = r["savings_combined"]
        recon = r["savings_pv"] + r["savings_battery"] + r["savings_interaction"]
        # Each of the four fields is independently rounded to 4dp before
        # being returned -- exact equality is lost to that rounding, but
        # never by more than 3 rounding steps (2e-4).
        assert abs(combined - recon) < 2e-4, (
            f"identity broken: combined={combined} vs pv+battery+interaction={recon}"
        )


def test_pv_only_household_battery_savings_is_zero():
    """No battery activity at all in any period -> battery_savings and
    interaction_savings are both zero every period, combined_savings ==
    pv_savings exactly (a pure-PV, no-battery counterfactual sanity
    check)."""
    flows = [
        _one_period_flow(pv_to_load=3.0, pv_to_grid=2.0, grid_to_load=1.0),
    ]
    import_price = np.array([0.30])
    export_price = np.array([0.05])
    period_hours = np.array([0.25])

    results = _compute_flow_economics(
        flows,
        import_price,
        export_price,
        period_hours,
        round_trip_efficiency=0.90,
        initial_soc_kwh=0.0,
    )
    r = results[0]
    assert r["savings_battery"] == 0.0
    assert r["savings_interaction"] == 0.0
    assert abs(r["savings_combined"] - r["savings_pv"]) < 1e-9


def test_no_solar_household_pv_savings_is_zero():
    flows = [
        _one_period_flow(grid_to_load=2.0, grid_to_battery=1.0),
        _one_period_flow(battery_to_load=1.0),
    ]
    import_price = np.array([0.30, 0.30])
    export_price = np.array([0.05, 0.05])
    period_hours = np.array([0.25, 0.25])

    results = _compute_flow_economics(
        flows,
        import_price,
        export_price,
        period_hours,
        round_trip_efficiency=0.90,
        initial_soc_kwh=0.0,
    )
    for r in results:
        assert r["savings_pv"] == 0.0


def test_wacog_cost_basis_blends_pv_at_zero_and_grid_at_import_price():
    """Period 0: charge 4kWh entirely from PV (over 1 real hour) -- the
    running cost basis should end at exactly $0/kWh (starting from an
    initial_soc of 0, so there's nothing else to blend against).
    Period 1: charge 4kWh entirely from grid at $0.50/kWh, blending
    against the existing $0/kWh 4kWh -- new basis should be the simple
    average, $0.25/kWh (4kWh@$0 + 4kWh@$0.50, over 8kWh total)."""
    flows = [
        _one_period_flow(pv_to_battery=4.0),
        _one_period_flow(grid_to_battery=4.0),
    ]
    import_price = np.array([0.30, 0.50])
    export_price = np.array([0.05, 0.05])
    period_hours = np.array([1.0, 1.0])

    results = _compute_flow_economics(
        flows,
        import_price,
        export_price,
        period_hours,
        round_trip_efficiency=1.0,  # isolate the cost-basis math from loss
        initial_soc_kwh=0.0,
    )
    assert abs(results[0]["flow_battery_cost_basis"] - 0.0) < 1e-9
    assert abs(results[1]["flow_battery_cost_basis"] - 0.25) < 1e-9


def test_wacog_cost_basis_persists_across_a_pure_idle_period():
    """A period with no charge and no discharge must leave the running
    cost basis completely unchanged -- it's inventory sitting still, not
    a transaction."""
    flows = [
        _one_period_flow(grid_to_battery=4.0),
        _one_period_flow(),  # idle
        _one_period_flow(battery_to_load=2.0),
    ]
    import_price = np.array([0.40, 0.40, 0.40])
    export_price = np.array([0.05, 0.05, 0.05])
    period_hours = np.array([1.0, 1.0, 1.0])

    results = _compute_flow_economics(
        flows,
        import_price,
        export_price,
        period_hours,
        round_trip_efficiency=1.0,
        initial_soc_kwh=0.0,
    )
    assert (
        results[0]["flow_battery_cost_basis"] == results[1]["flow_battery_cost_basis"]
    )
    assert (
        results[1]["flow_battery_cost_basis"] == results[2]["flow_battery_cost_basis"]
    )


def test_battery_to_grid_arbitrage_margin_reflects_cheap_charge_expensive_export():
    """Charge cheap from grid at $0.10/kWh, later discharge-to-export at
    $0.80/kWh -- the Battery->Grid shadow price (export_price - loss -
    charge_price_at_source) should be strongly positive, reflecting a
    real arbitrage margin, not just the raw export price."""
    flows = [
        _one_period_flow(grid_to_battery=4.0),
        _one_period_flow(battery_to_grid=4.0),
    ]
    import_price = np.array([0.10, 0.10])
    export_price = np.array([0.05, 0.80])
    period_hours = np.array([1.0, 1.0])

    results = _compute_flow_economics(
        flows,
        import_price,
        export_price,
        period_hours,
        round_trip_efficiency=1.0,
        initial_soc_kwh=0.0,
    )
    price = results[1]["flow_price_battery_to_grid"]
    # export_price(0.80) - loss(0, rt_eff=1.0) - cost_basis(0.10) == 0.70
    assert abs(price - 0.70) < 1e-9
    assert results[1]["savings_battery"] > 0


def test_forecast_periods_carry_the_new_fields_shape():
    flow = _flow_decomposition(3.0, 1.0, 3.0, 0.0)
    assert set(flow.keys()) == {
        "pv_to_load",
        "pv_to_battery",
        "pv_to_grid",
        "battery_to_load",
        "battery_to_grid",
        "grid_to_load",
        "grid_to_battery",
    }
    econ = _compute_flow_economics(
        [flow],
        np.array([0.30]),
        np.array([0.05]),
        np.array([0.25]),
        round_trip_efficiency=0.90,
        initial_soc_kwh=0.0,
    )[0]
    assert set(econ.keys()) == {
        "flow_price_pv_to_load",
        "flow_price_pv_to_battery",
        "flow_price_pv_to_grid",
        "flow_price_battery_to_load",
        "flow_price_battery_to_grid",
        "flow_price_grid_to_load",
        "flow_price_grid_to_battery",
        "flow_battery_cost_basis",
        "savings_pv",
        "savings_battery",
        "savings_combined",
        "savings_interaction",
    }
