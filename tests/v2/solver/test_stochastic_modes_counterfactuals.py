"""The two-stage stochastic dispatch, the inverter modes a plan implies, and
the simple dispatch policies Nimbus is scored against (#1837).

The stochastic plan must not peek: one decision before the scenarios
diverge, and each scenario's own continuation after. With identical
scenarios it is the deterministic plan. Covers what lived in the A2
stochastic, #1797 stochastic shadow, shadow-mode and counterfactual files.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from solver.counterfactuals import (
    no_control_dispatch,
    tune_two_threshold,
    two_threshold_dispatch,
)
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.modes import shadow_modes_for_plan, summarize_mode_transitions
from solver.network import build_plan
from solver.stochastic import build_stochastic_plan

START = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
N = 8


def _battery(**kw):
    base = {
        "name": "home",
        "capacity_kwh": 10.0,
        "initial_soc_kwh": 5.0,
        "min_soc_kwh": 1.0,
        "max_soc_kwh": 9.5,
        "max_charge_kw": 4.0,
        "max_discharge_kw": 4.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.006,
        "discharge_cost": 0.006,
        "salvage_value": 0.15,
    }
    return BatteryConfig(**{**base, **kw})


def _grid(prices):
    return GridConfig(
        import_price=prices,
        export_price=np.full(N, 0.05),
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )


PRICES = np.array([0.10, 0.10, 0.10, 0.45, 0.45, 0.30, 0.20, 0.10])
LOAD = np.full(N, 1.5)


def _stochastic(scenarios, weights, start=2, **kw):
    return build_stochastic_plan(
        periods=PeriodGrid(np.full(N, 1.0), start=START),
        grid=_grid(PRICES),
        battery=_battery(),
        solar_scenarios=scenarios,
        scenario_weights=weights,
        stochastic_start_period=start,
        load_kw=LOAD,
        **kw,
    )


def test_identical_scenarios_continue_identically():
    solar = np.array([0, 0, 1, 2, 2, 1, 0, 0], dtype=float)
    plan = _stochastic([solar, solar.copy()], [0.5, 0.5])
    assert plan.status == "optimal"
    for a, b in (
        (plan.stage2_charge_kw[0], plan.stage2_charge_kw[1]),
        (plan.stage2_discharge_kw[0], plan.stage2_discharge_kw[1]),
        (plan.stage2_soc_kwh[0], plan.stage2_soc_kwh[1]),
    ):
        np.testing.assert_allclose(a, b, atol=1e-5)


def test_the_first_stage_is_one_decision_and_each_scenario_continues_on_its_own():
    dull = np.zeros(N)
    sunny = np.array([0, 0, 3, 4, 4, 3, 0, 0], dtype=float)
    plan = _stochastic([dull, sunny], [0.6, 0.4])
    assert plan.status == "optimal"
    assert len(plan.stage1_charge_kw) == 2 == len(plan.stage1_soc_kwh)
    assert len(plan.stage2_charge_kw) == 2 and all(
        len(c) == N - 2 for c in plan.stage2_charge_kw
    )
    assert np.isfinite(plan.expected_total_cost)
    # The sunny scenario needs less from the grid after the split.
    assert plan.stage2_grid_import_kw[1].sum() < plan.stage2_grid_import_kw[0].sum()
    for soc in plan.stage2_soc_kwh:
        assert soc.min() >= 1.0 - 1e-6 and soc.max() <= 9.5 + 1e-6


@pytest.mark.parametrize(
    ("scenarios", "weights", "start", "says"),
    [
        ([np.zeros(N)], [1.0], 2, "at least 2 scenarios"),
        ([np.zeros(N), np.zeros(N)], [1.0], 2, "expected 2"),
        ([np.zeros(N), np.zeros(N)], [0.7, 0.7], 2, "sum to exactly 1.0"),
        ([np.zeros(N), np.zeros(N - 1)], [0.5, 0.5], 2, "PeriodGrid mismatch"),
        ([np.zeros(N), np.zeros(N)], [0.5, 0.5], N, "must be within"),
    ],
)
def test_a_badly_formed_problem_is_refused_before_solving(
    scenarios, weights, start, says
):
    with pytest.raises(ValueError, match=says):
        _stochastic(scenarios, weights, start)


# --- the inverter modes a plan implies ---------------------------------------------------------


def test_each_period_maps_to_the_inverters_own_mode_and_transitions_are_listed():
    det = build_plan(
        periods=PeriodGrid(np.full(N, 1.0), start=START),
        grid=_grid(PRICES),
        batteries=[_battery()],
        solar=SolarConfig(forecast_kw=np.zeros(N)),
        loads=[LoadConfig(name="house", forecast_kw=LOAD)],
    )
    readings = shadow_modes_for_plan(det)
    assert len(readings) == N
    for r, c, d in zip(
        readings, det.battery_charge_kw, det.battery_discharge_kw, strict=True
    ):
        if r.mode == "vpp_discharge":
            assert (r.ems_code, r.command, r.setpoint_kw) == (
                4,
                "Discharge",
                pytest.approx(d),
            )
        elif r.mode == "vpp_charge":
            assert (r.ems_code, r.command, r.setpoint_kw) == (
                4,
                "Charge",
                pytest.approx(c),
            )
        else:
            assert (r.ems_code, r.command, r.setpoint_kw) == (1, "Stop", 0.0)
    assert {r.mode for r in readings} >= {"vpp_charge", "vpp_discharge"}
    transitions = summarize_mode_transitions(readings)
    assert transitions and all(
        readings[i - 1].mode == a and readings[i].mode == b for i, a, b in transitions
    )
    assert summarize_mode_transitions(readings[:1]) == []


def test_a_plan_that_did_not_solve_has_no_modes():
    class Failed:
        is_optimal = False
        status = "infeasible"

    with pytest.raises(ValueError, match="non-optimal"):
        shadow_modes_for_plan(Failed())


# --- what Nimbus is scored against ---------------------------------------------------------------


def _policy(low, high, **kw):
    args = {
        "price_kwh": PRICES,
        "hours": np.full(N, 1.0),
        "initial_soc_kwh": 5.0,
        "min_soc_kwh": 1.0,
        "max_soc_kwh": 9.5,
        "max_charge_kw": 4.0,
        "max_discharge_kw": 4.0,
        "charge_efficiency": 0.9,
        "discharge_efficiency": 0.9,
    }
    return two_threshold_dispatch(
        **{**args, **kw}, threshold_low=low, threshold_high=high
    )


def test_doing_nothing_keeps_the_battery_where_it_was():
    d = no_control_dispatch(n_periods=3, initial_soc_kwh=4.2)
    assert d.final_soc_kwh == 4.2 and not d.charge_kw.any() and not d.discharge_kw.any()


def test_the_threshold_policy_charges_cheap_and_discharges_dear_within_its_limits():
    d = _policy(0.15, 0.40)
    # 4 kW, then the last 1 kW of room, then full.
    assert (
        list(d.charge_kw[:3]) == pytest.approx([4.0, 1.0, 0.0])
        and d.discharge_kw[3] > 0
    )
    # Charged to the top, then sold down to the floor.
    assert d.charge_kw[:3].sum() * 0.9 == pytest.approx(4.5)
    assert d.discharge_kw[3:5].sum() / 0.9 == pytest.approx(8.5)
    assert d.final_soc_kwh == pytest.approx(
        1.0 + 4.0 * 0.9 * 1.0 / 1.0 * 1
    )  # the last cheap hour recharges 3.6 kWh
    assert (
        _policy(0.15, 0.40, hours=np.zeros(N)).final_soc_kwh == 5.0
    )  # zero-length periods move nothing


def test_tuning_finds_the_cheapest_thresholds_on_its_grid():
    def cost(d):
        return float(np.sum((d.charge_kw - d.discharge_kw) * PRICES))

    low, high, best, value = tune_two_threshold(
        price_kwh=PRICES,
        hours=np.full(N, 1.0),
        initial_soc_kwh=5.0,
        min_soc_kwh=1.0,
        max_soc_kwh=9.5,
        max_charge_kw=4.0,
        max_discharge_kw=4.0,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        evaluate_fn=cost,
        n_grid=8,
    )
    assert low < high and value == pytest.approx(cost(best))
    assert value <= cost(no_control_dispatch(n_periods=N, initial_soc_kwh=5.0))
    with pytest.raises(ValueError, match="constant"):
        tune_two_threshold(
            price_kwh=np.full(3, 0.2),
            hours=np.ones(3),
            initial_soc_kwh=1,
            min_soc_kwh=0,
            max_soc_kwh=2,
            max_charge_kw=1,
            max_discharge_kw=1,
            charge_efficiency=1,
            discharge_efficiency=1,
            evaluate_fn=cost,
            n_grid=1,
        )
