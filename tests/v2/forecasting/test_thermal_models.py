"""The LP's thermal recursion against the published temperature
projection (#897, #481, #864) (#1837).

Two equations for one tank. Since #897 the LP loses only the idle
fraction of each period, so the two agree wherever the load runs at full
power, and the remaining gap has an exact closed form. The ambient
covariate applies to the projection only. These pin the difference, not
an agreement that does not hold: deciding which model is right changes
dispatch on a real hot water system.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import thermal_forecast as tf
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
    ThermalLoadConfig,
)
from solver.network import build_plan

N, HOURS, DECAY, RATE, MAX_KW = 24, 1.0, 0.3, 8.0, 3.0
T0 = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def lp():
    plan = build_plan(
        periods=PeriodGrid(np.full(N, HOURS)),
        grid=GridConfig(
            import_price=np.full(N, 0.30),
            export_price=np.full(N, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[
            BatteryConfig(
                name="battery",
                capacity_kwh=30.0,
                initial_soc_kwh=15.0,
                min_soc_kwh=2.0,
                max_soc_kwh=30.0,
                max_charge_kw=15.0,
                max_discharge_kw=15.0,
                charge_efficiency=0.99,
                discharge_efficiency=0.99,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.10,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(N)),
        loads=[LoadConfig(name="load", forecast_kw=np.ones(N))],
        thermal_loads=[
            ThermalLoadConfig(
                name="hws",
                max_power_kw=MAX_KW,
                initial_temperature_c=40.0,
                target_temperature_c=60.0,
                earliest_period=0,
                deadline_period=N - 1,
                heating_rate_c_per_kwh=RATE,
                idle_decay_c_per_hour=DECAY,
            )
        ],
    )
    assert plan.status == "optimal"
    return plan.thermal_loads[0]


def _project(power_kw, ambient_c=None, loss=None):
    rows = [
        {"time": (T0 + timedelta(hours=HOURS * i)).isoformat(), "value": float(p)}
        for i, p in enumerate(power_kw)
    ]
    extra = {}
    if ambient_c is not None:
        extra = {
            "loss_coeff_per_h": loss,
            "ambient_forecast": [{**r, "value": ambient_c} for r in rows],
        }
    projected = tf.project_temperature_forecast(
        rows,
        start_temperature=40.0,
        heating_rate_c_per_kwh=RATE,
        idle_decay_c_per_hour=DECAY,
        on_threshold_kw=0.05,
        **extra,
    )
    return np.array([float(r["value"]) for r in projected])


def test_the_two_models_differ_only_by_the_idle_fraction_of_each_period(lp):
    # test_the_two_models_differ_only_by_the_idle_fraction_of_each_period
    power = np.asarray(lp.power_kw, dtype=float)
    on = power > 0.05
    assert on.any(), "the LP must heat, or the comparison compares nothing"
    gap = _project(power) - np.asarray(lp.temperature_c, dtype=float)
    lp_loss = DECAY * HOURS * (1.0 - power / MAX_KW)
    shown_loss = np.where(on, 0.0, DECAY * HOURS)
    np.testing.assert_allclose(gap, np.cumsum(lp_loss - shown_loss), atol=0.05)
    first_on = int(np.argmax(on))
    np.testing.assert_allclose(gap[:first_on], 0.0, atol=1e-6)  # idle: identical
    full = np.flatnonzero(np.isclose(power, MAX_KW))
    assert full.size, "the fixture must run the load at full power"
    for t in full:  # a full-power period adds nothing to the gap
        assert gap[t] == pytest.approx(gap[t - 1] if t else 0.0, abs=1e-6)

    # test_the_gap_is_smaller_than_the_model_897_replaced: The LP used to lose a full period's decay while heating too.
    power = np.asarray(lp.power_kw, dtype=float)
    gap = _project(power)[-1] - float(lp.temperature_c[-1])
    assert 0.0 <= gap < DECAY * HOURS * float(np.sum(power > 0.05))

    # test_the_ambient_covariate_moves_the_projection_and_not_the_lp
    power = np.asarray(lp.power_kw, dtype=float)
    flat = _project(power)
    ambient = _project(power, ambient_c=20.0, loss=0.02)
    assert ambient[-1] != pytest.approx(flat[-1], abs=0.01)
    assert ambient[-1] != pytest.approx(float(lp.temperature_c[-1]), abs=0.01)
    np.testing.assert_allclose(
        _project(power, ambient_c=20.0, loss=None), flat, atol=1e-9
    )
