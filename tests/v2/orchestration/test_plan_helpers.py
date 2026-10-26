"""solver_plan's period-0 stability, spike and stochastic-refusal helpers (#1837).

Each helper is driven through every outcome it can publish, on real plans
from ``build_plan`` (a battery that discharges into a dear period, then the
same site re-solved so it charges instead), with the pinned alternative
derived from the real plan. Covers the checks that lived in #1417, #1577,
#1654, #694, #1797's refusal rules and the 11-12 Oct 2026 spike-night
households cases.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
import solver_plan
import solver_publish as publish
from solver import pin_blockers
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

START = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)  # 16:00 Brisbane


def _plan(import_price0: float, *, export_price=0.05, start=START):
    n = 4
    prices = np.full(n, 0.30)
    prices[0] = import_price0
    return build_plan(
        periods=PeriodGrid(np.full(n, 0.5), start=start),
        grid=GridConfig(
            import_price=prices,
            export_price=np.full(n, export_price),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=10.0,
                initial_soc_kwh=5.0,
                min_soc_kwh=1.0,
                max_soc_kwh=9.5,
                max_charge_kw=4.0,
                max_discharge_kw=4.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.006,
                discharge_cost=0.006,
                salvage_value=0.25,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 2.0))],
    )


@pytest.fixture(scope="module")
def discharging():
    plan = _plan(0.90)
    assert solver_plan._deadband_class(solver_plan._period0_net_kw(plan)) == 1
    return plan


@pytest.fixture(scope="module")
def charging():
    plan = _plan(0.01)
    assert solver_plan._deadband_class(solver_plan._period0_net_kw(plan)) == -1
    return plan


def _pinned(plan, *, cost_delta=0.0, status="optimal"):
    return dataclasses.replace(
        plan, status=status, total_cost=float(plan.total_cost) + cost_delta
    )


# --- period0_direction_hysteresis (#1654) ----------------------------------


def test_hysteresis_off_and_without_a_previous_plan(charging, discharging):
    assert (
        solver_plan.period0_direction_hysteresis(
            charging, discharging, lambda kw: None, 0.0
        ).period0_hysteresis
        == "off"
    )
    assert (
        solver_plan.period0_direction_hysteresis(
            charging, None, lambda kw: None, 0.05
        ).period0_hysteresis
        == "no_previous"
    )
    failed = dataclasses.replace(charging, status="infeasible")
    assert (
        solver_plan.period0_direction_hysteresis(
            failed, discharging, lambda kw: None, 0.05
        ).period0_hysteresis
        == "no_previous"
    )


@pytest.mark.parametrize(
    ("cost_delta", "status", "want"),
    [
        (0.01, "optimal", "kept"),
        (0.0499, "optimal", "kept"),
        (0.0501, "optimal", "switched"),
        (0.01, "infeasible", "switched"),
    ],
)
def test_hysteresis_keeps_the_previous_direction_only_within_the_margin(
    charging, discharging, cost_delta, status, want
):
    seen = []

    def resolve(kw):
        seen.append(kw)
        return _pinned(
            discharging,
            cost_delta=float(charging.total_cost)
            - float(discharging.total_cost)
            + cost_delta,
            status=status,
        )

    out = solver_plan.period0_direction_hysteresis(charging, discharging, resolve, 0.05)
    assert out.period0_hysteresis == want
    # The pin is the previous plan's own period-0 kW.
    assert seen == [pytest.approx(solver_plan._period0_net_kw(discharging))]
    if want == "kept":
        assert solver_plan._period0_net_kw(out) == pytest.approx(
            solver_plan._period0_net_kw(discharging)
        )
        assert out.period0_hysteresis_delta == pytest.approx(cost_delta, abs=1e-4)
    elif status == "optimal":
        assert solver_plan._period0_net_kw(out) == pytest.approx(
            solver_plan._period0_net_kw(charging)
        )


def test_hysteresis_survives_a_failing_re_solve(charging, discharging, pin_logger_on):
    # test_hysteresis_survives_a_failing_re_solve
    def boom(kw):
        raise RuntimeError("re-solve failed")

    out = solver_plan.period0_direction_hysteresis(charging, discharging, boom, 0.05)
    assert out.period0_hysteresis == "switched"

    # test_a_blocker_check_that_raises_falls_through_to_the_re_solve
    def bad(kw):
        raise ValueError("cannot tell")

    out = solver_plan.period0_direction_hysteresis(
        charging,
        discharging,
        lambda kw: _pinned(
            discharging,
            cost_delta=float(charging.total_cost) - float(discharging.total_cost),
        ),
        0.05,
        pin_blocked=bad,
    )
    assert out.period0_hysteresis == "kept"

    # test_the_export_floor_refuses_a_pin_that_sells_more_below_it
    cheap = dataclasses.replace(
        charging,
        effective_export_price=np.full(len(charging.grid_export_kw), 0.02),
    )
    pinned = dataclasses.replace(
        discharging,
        grid_export_kw=np.asarray(charging.grid_export_kw, dtype=float) + 1.0,
        total_cost=float(charging.total_cost),
    )
    out = solver_plan.period0_direction_hysteresis(
        cheap, discharging, lambda kw: pinned, 0.05, export_floor=0.10
    )
    assert out.period0_hysteresis == "export_floor"
    # With the floor off, or the price above it, the same pin is kept.
    assert (
        solver_plan.period0_direction_hysteresis(
            cheap, discharging, lambda kw: pinned, 0.05, export_floor=0.0
        ).period0_hysteresis
        == "kept"
    )
    assert (
        solver_plan.period0_direction_hysteresis(
            cheap, discharging, lambda kw: pinned, 0.05, export_floor=0.01
        ).period0_hysteresis
        == "kept"
    )

    # test_a_pinned_charge_bought_from_the_grid_is_not_kept
    # nimbus #2066, 26 Oct 07:23: cloud took solar from 1.15 to 0.06 kW and
    # the kept solar charge became a charge from the grid at 15.1 c, for a
    # $0.0002 saving in mode changes.
    pinned = dataclasses.replace(
        charging,
        grid_import_kw=np.asarray(discharging.grid_import_kw, dtype=float) + 1.0,
        total_cost=float(discharging.total_cost),
    )
    out = solver_plan.period0_direction_hysteresis(
        discharging, charging, lambda kw: pinned, 0.05
    )
    assert out.period0_hysteresis == "grid_charge"
    assert solver_plan._period0_net_kw(out) == pytest.approx(
        solver_plan._period0_net_kw(discharging)
    )

    # test_the_crossing_instrument_measures_a_feasible_pin
    pinned = _pinned(discharging, cost_delta=0.25)
    rec = solver_plan.period0_crossing_delta(
        charging,
        discharging,
        lambda kw: pinned,
        inputs={"import_price": 0.0123456, "solar_kw": None},
        pin_participant="home",
    )
    assert rec["new_period0_kw"] == pytest.approx(
        solver_plan._period0_net_kw(charging), abs=1e-3
    )
    assert rec["prev_period0_kw"] == pytest.approx(
        solver_plan._period0_net_kw(discharging), abs=1e-3
    )
    assert rec["anchor_prev_index"] == 0
    assert rec["pinned_status"] == "optimal"
    assert rec["delta_objective"] == pytest.approx(
        float(pinned.total_cost) - float(charging.total_cost), abs=1e-4
    )
    assert rec["pinned_blockers"] is None
    assert rec["pinned_participant"] == "home"
    assert rec["inputs"] == {"import_price": 0.0123, "solar_kw": None}

    # test_an_infeasible_pin_names_what_blocked_it
    infeasible = _pinned(discharging, status="infeasible")
    rec = solver_plan.period0_crossing_delta(
        charging,
        discharging,
        lambda kw: infeasible,
        explain_infeasible=lambda kw: ["export pinned"],
    )
    assert rec["pinned_blockers"] == ["export pinned"]
    assert rec["delta_objective"] is None
    rec = solver_plan.period0_crossing_delta(
        charging, discharging, lambda kw: infeasible, explain_infeasible=lambda kw: []
    )
    assert rec["pinned_blockers"] == [pin_blockers.UNEXPLAINED]

    def bad(kw):
        raise ValueError("no")

    rec = solver_plan.period0_crossing_delta(
        charging, discharging, lambda kw: infeasible, explain_infeasible=bad
    )
    assert rec["pinned_blockers"] is None

    # test_no_crossing_no_previous_or_a_failing_re_solve_measures_nothing
    assert (
        solver_plan.period0_crossing_delta(charging, charging, lambda kw: charging)
        is None
    )
    assert (
        solver_plan.period0_crossing_delta(charging, None, lambda kw: charging) is None
    )

    def boom(kw):
        raise RuntimeError("x")

    assert solver_plan.period0_crossing_delta(charging, discharging, boom) is None

    # test_a_reserve_window_is_the_local_hours_inside_it
    # test_a_reserve_window_is_the_local_hours_inside_it
    times = _grid(24, 0)  # local 00:00 .. 23:00
    assert solver_plan.reserve_window_indices(times, 2.0, 2.0) is None
    assert solver_plan.reserve_window_indices(times, 0.0, 6.0) == frozenset(range(6))
    # Wrapping midnight: 22:00 to 03:00.
    assert solver_plan.reserve_window_indices(times, 22.0, 3.0) == frozenset(
        {22, 23, 0, 1, 2}
    )

    # test_a_spike_widens_a_block_period_to_its_export_limit
    fixed = [float("nan"), 10.0, 10.0, 0.0]
    ceiling = solver_plan.spike_export_ceiling(
        SPIKE_CFG, fixed, [0.3, 5.0, 0.4, 5.0], [15.0] * 4
    )
    assert (
        np.isnan(ceiling[0])
        and ceiling[1] == 15.0
        and np.isnan(ceiling[2])
        and np.isnan(ceiling[3])
    )
    # A limit at or below the pin is no widening at all.
    assert (
        solver_plan.spike_export_ceiling(
            SPIKE_CFG, fixed, [0.3, 5.0, 0.4, 5.0], [10.0] * 4
        )
        is None
    )
    # Unarmed, no threshold, or no block: None.
    assert (
        solver_plan.spike_export_ceiling(
            {**SPIKE_CFG, "solver_price_spike_override_armed": False},
            fixed,
            [5.0] * 4,
            [15.0] * 4,
        )
        is None
    )
    assert (
        solver_plan.spike_export_ceiling(
            {**SPIKE_CFG, "solver_price_spike_threshold": 0.0},
            fixed,
            [5.0] * 4,
            [15.0] * 4,
        )
        is None
    )
    assert (
        solver_plan.spike_export_ceiling(SPIKE_CFG, None, [5.0] * 4, [15.0] * 4) is None
    )

    # test_a_spike_night_lowers_its_block_to_the_matched_volume
    # Two hours of a 10 kW block = 20 kWh committed; 9 kWh matched.
    times = [
        datetime(2026, 9, 20, 7, 0, tzinfo=UTC) + timedelta(minutes=30 * i)
        for i in range(4)
    ]  # 17:00-18:30 local
    fixed = [10.0, 10.0, 10.0, 10.0]
    out = solver_plan.spike_night_block_floor(
        SPIKE_CFG, fixed, [0.3, 0.3, 5.0, 0.3], times, 9.0
    )
    share = (1 + solver_plan.SPIKE_NIGHT_P2P_MARGIN) * 9.0 / 20.0
    assert out == pytest.approx([10.0 * share] * 4)
    # No spike in the block's periods: unchanged.
    assert (
        solver_plan.spike_night_block_floor(SPIKE_CFG, fixed, [0.3] * 4, times, 9.0)
        == fixed
    )
    # Matched covers the commitment: unchanged.
    assert (
        solver_plan.spike_night_block_floor(SPIKE_CFG, fixed, [5.0] * 4, times, 30.0)
        == fixed
    )
    # Unarmed / no matched volume / no threshold: the input itself.
    assert (
        solver_plan.spike_night_block_floor(
            {**SPIKE_CFG, "solver_price_spike_override_armed": False},
            fixed,
            [5.0] * 4,
            times,
            9.0,
        )
        is fixed
    )
    assert (
        solver_plan.spike_night_block_floor(SPIKE_CFG, fixed, [5.0] * 4, times, 0.0)
        is fixed
    )
    assert (
        solver_plan.spike_night_block_floor(SPIKE_CFG, None, [5.0] * 4, times, 9.0)
        is None
    )

    # test_the_stochastic_plan_is_refused_for_each_named_reason
    for over, drive, want in [
        ({}, True, None),
        ({"status": "no_band"}, False, "no_solar_band"),
        ({"status": "error"}, False, "stochastic_error"),
        ({"plan": None}, False, "stochastic_optimal"),
        ({"expected_cost": None}, False, "scenario_cost_not_finite"),
        ({"scenario_costs": {"p10": float("inf")}}, False, "scenario_cost_not_finite"),
        ({"nonanticipativity_max_gap": None}, False, "nonanticipativity_violated"),
        ({"nonanticipativity_max_gap": 1.0}, False, "nonanticipativity_violated"),
        ({"thermal_guarantee_relaxed": True}, False, "thermal_guarantee_relaxed"),
        ({"direction_exclusivity": "declined"}, False, "direction_declined"),
        ({"direction_exclusivity": "still_two_way"}, False, "direction_still_two_way"),
        ({"solar_band": "raw"}, True, "solar_band_not_calibrated"),
        ({"solar_band": "raw"}, False, None),
    ]:
        assert solver_plan._refusal_reason(_result(**over), DET, drive=drive) == want

    # test_a_relaxation_the_deterministic_plan_shares_is_not_a_refusal
    det = SimpleNamespace(
        thermal_guarantee_relaxed=True, direction_exclusivity="declined"
    )
    assert (
        solver_plan._refusal_reason(
            _result(thermal_guarantee_relaxed=True, direction_exclusivity="declined"),
            det,
            drive=True,
        )
        is None
    )

    # test_a_bound_is_named_by_which_bound_the_variable_sits_at
    for export_kw, pin, label in [
        (5.0, None, "Grid export limit"),  # exactly at the ceiling
        (0.0, None, "Grid export at zero (not economical right now)"),
        (3.0, 3.0, "Grid export pinned at 3.00 kW by P2P export commitment"),
        (
            3.0,
            None,
            "Grid export at 3.00 kW (unexpected -- neither its 0 nor 5.00 kW bound)",
        ),
    ]:
        plan = _bound_plan(export_kw)
        assert publish.compute_binding_constraint_label(
            plan, 5.0, 10.0, 4.0, 4.0, 0.5, fixed_export_kw_now=pin
        ) == (label, 1.0)
        assert publish.compute_binding_constraint_label(
            _bound_plan(export_kw, 0.0), 5.0, 10.0, 4.0, 4.0, 0.5
        ) == ("Nothing currently binding", None)


# --- period0_crossing_delta (#1417, #1577) ---------------------------------


@pytest.fixture
def pin_logger_on():
    logger = solver_plan.PERIOD0_PIN_LOGGER
    old = logger.level
    logger.setLevel(logging.DEBUG)
    yield
    logger.setLevel(old)


# --- reserve_window_indices (#1654) ----------------------------------------


def _grid(n, start_local_hour):
    start = datetime(
        2026,
        9,
        20,
        int(start_local_hour) - 10
        if start_local_hour >= 10
        else int(start_local_hour) + 14,
        0,
        tzinfo=UTC,
    )
    if start_local_hour < 10:
        start -= timedelta(days=1)
    return [start + timedelta(hours=i) for i in range(n)]


# --- spike_export_ceiling / spike_night_block_floor (#694, 11-12 Oct) ------


SPIKE_CFG = {
    "solver_price_spike_override_armed": True,
    "solver_price_spike_threshold": 1.0,
    "solver_p2p_block_1_rate_kw": 10.0,
    "solver_p2p_block_1_start_hour": 17.0,
    "solver_p2p_block_1_end_hour": 19.0,
}


# --- _refusal_reason (#1797) ------------------------------------------------


def _result(**over):
    base = {
        "status": "optimal",
        "plan": object(),
        "scenario_costs": {"p10": 1.0, "p50": 1.0, "p90": 1.0},
        "expected_cost": 1.0,
        "nonanticipativity_max_gap": 0.0,
        "thermal_guarantee_relaxed": False,
        "direction_exclusivity": "not_needed",
        "solar_band": "calibrated",
    }
    base.update(over)
    return SimpleNamespace(**base)


DET = SimpleNamespace(
    thermal_guarantee_relaxed=False, direction_exclusivity="not_needed"
)


# --- what is binding now, and each battery's own forecast ------------------------------------


def _bound_plan(export_kw, reduced_cost=0.5):
    zeros = np.zeros(2)
    return SimpleNamespace(
        reduced_costs={"grid_export_0": reduced_cost},
        grid_export_kw=np.array([export_kw, 0.0]),
        grid_import_kw=zeros,
        battery_charge_kw=zeros,
        battery_discharge_kw=zeros,
    )


def test_a_previous_plan_that_cannot_be_aligned_has_no_anchor(charging, monkeypatch):
    def misaligned(periods, previous):
        raise ValueError("no period starts")

    monkeypatch.setattr(solver_plan.network, "_align_previous_periods", misaligned)
    assert solver_plan._anchor_target_kw(charging, charging) == (None, None)
