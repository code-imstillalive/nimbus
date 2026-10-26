"""Receding-horizon replay and the reference household benchmark (#1837).

The rolling loop re-solves every interval from where the battery actually
got to, dispatches only each plan's first period, and keeps going through
an infeasible solve. The benchmark replays one fixed synthetic day so a
change to the solver shows up as a number. Covers what lived in #273 and
the rolling-refinement files.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.reference_benchmark import (
    REFERENCE_HOUSEHOLD_VERSION,
    build_reference_scenario,
    run_reference_benchmark,
)
from solver.rolling import (
    RollingInputs,
    RollingRefinementConfig,
    run_rolling_refinement,
)

T0 = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
N = 6
STEP = timedelta(minutes=30)


def _inputs(now, *, prices=None, load=1.0, initial_soc=5.0):
    prices = np.array([0.1, 0.1, 0.5, 0.5, 0.2, 0.2]) if prices is None else prices
    return RollingInputs(
        periods=PeriodGrid(np.full(N, 0.5), start=now),
        grid=GridConfig(
            import_price=prices,
            export_price=np.full(N, 0.05),
            import_limit_kw=10.0,
            export_limit_kw=10.0,
        ),
        battery=BatteryConfig(
            name="home",
            capacity_kwh=10.0,
            initial_soc_kwh=initial_soc,
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
            max_charge_kw=4.0,
            max_discharge_kw=4.0,
            charge_efficiency=0.95,
            discharge_efficiency=0.95,
            charge_cost=0.006,
            discharge_cost=0.006,
            salvage_value=0.15,
        ),
        solar=SolarConfig(forecast_kw=np.zeros(N)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(N, load))],
    )


def test_each_tick_dispatches_its_plans_first_period_and_the_next_starts_from_there():
    result = run_rolling_refinement(
        RollingRefinementConfig(start=T0, resolve_interval=STEP, n_resolves=4), _inputs
    )
    assert result.n_infeasible == 0 and len(result.ticks) == 4
    for tick in result.ticks:
        assert tick.dispatched_charge_kw == pytest.approx(
            float(tick.plan.battery_charge_kw[0])
        )
        assert tick.dispatched_soc_kwh == pytest.approx(
            float(tick.plan.battery_soc_kwh[0])
        )
    for prev, nxt in zip(result.ticks, result.ticks[1:], strict=False):
        assert nxt.solved_at - prev.solved_at == STEP
        # The next solve starts from the state of charge actually reached.
        assert nxt.plan.batteries[0].soc_kwh[0] == pytest.approx(nxt.dispatched_soc_kwh)
    assert result.dispatch_charge_kw[0] > 0  # cheap now, dear later: charge
    assert (
        len(result.dispatch_soc_kwh)
        == len(result.dispatch_grid_import_kw)
        == len(result.dispatch_grid_export_kw)
        == len(result.dispatch_discharge_kw)
        == 4
    )


def test_an_infeasible_tick_repeats_the_last_dispatch_and_the_loop_goes_on(monkeypatch):
    from solver import rolling

    real, calls = rolling.build_plan, {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        plan = real(**kw)
        if calls["n"] == 2:
            return dataclasses.replace(plan, status="infeasible")
        return plan

    monkeypatch.setattr(rolling, "build_plan", flaky)
    result = run_rolling_refinement(
        RollingRefinementConfig(start=T0, resolve_interval=STEP, n_resolves=3), _inputs
    )
    assert result.n_infeasible == 1
    first, failed, third = result.ticks
    assert not failed.plan.is_optimal
    assert (failed.dispatched_charge_kw, failed.dispatched_soc_kwh) == (
        first.dispatched_charge_kw,
        first.dispatched_soc_kwh,
    )
    assert third.plan.is_optimal


def _unanchored(now):
    return dataclasses.replace(_inputs(now), periods=PeriodGrid(np.full(N, 0.5)))


def _in_the_past(now):
    return dataclasses.replace(
        _inputs(now), periods=PeriodGrid(np.full(N, 0.5), start=now - STEP)
    )


def test_an_input_window_that_cannot_be_aligned_is_refused():
    for make, says in [
        (_unanchored, "start must be set"),
        (_in_the_past, "before this tick"),
    ]:
        with pytest.raises(ValueError, match=says):
            run_rolling_refinement(
                RollingRefinementConfig(start=T0, resolve_interval=STEP, n_resolves=1),
                make,
            )


def test_a_rolling_run_needs_a_positive_count_and_interval():
    for n, interval in [(0, STEP), (1, timedelta(0))]:
        with pytest.raises(ValueError):
            RollingRefinementConfig(start=T0, resolve_interval=interval, n_resolves=n)


def test_the_reference_day_is_fixed_and_scored():
    scenario = build_reference_scenario()
    again = build_reference_scenario()
    # The real day is not the forecast, and the forecast is not persistence.
    assert not np.array_equal(scenario["solar_real_kw"], scenario["solar_forecast_kw"])
    assert not np.array_equal(
        scenario["load_forecast_kw"], scenario["load_persistence_kw"]
    )
    for key, value in scenario.items():
        if isinstance(value, np.ndarray):
            np.testing.assert_array_equal(value, again[key])
    # The forecast is clipped at zero like real solar: dark at night.
    assert scenario["solar_forecast_kw"].min() == 0.0
    result = run_reference_benchmark()
    assert result.scenario_version == REFERENCE_HOUSEHOLD_VERSION
    # Built to be better than persistence: a value add at or below zero on
    # this day is the regression the benchmark exists to show.
    assert result.nimbus_value_add_dollars > 0
    assert result.nimbus_value_add_dollars == pytest.approx(
        run_reference_benchmark().nimbus_value_add_dollars
    )
