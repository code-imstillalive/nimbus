"""Every joint solve call the stochastic plan makes, recorded (#2025).

A plan can take several joint calls (a thermal retry, direction rounds),
so a fallback on the total time alone cannot say which call ran out. Each
call records its own seconds, time limit, status and MIP gap, and the
published summary carries them. Synthetic inputs only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np
from solver import network
from solver import stochastic_dispatch as sd
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)

T0 = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)


def _inputs() -> dict:
    return {
        "periods": PeriodGrid(hours=np.array([0.5, 1.0, 1.0]), start=T0),
        "grid": GridConfig(
            import_price=np.array([0.12, 0.50, 0.50]),
            export_price=np.array([0.0, 0.0, 0.0]),
            import_limit_kw=50.0,
            export_limit_kw=50.0,
        ),
        "batteries": [
            BatteryConfig(
                name="home",
                capacity_kwh=10.0,
                initial_soc_kwh=0.0,
                min_soc_kwh=0.0,
                max_soc_kwh=10.0,
                max_charge_kw=20.0,
                max_discharge_kw=20.0,
                charge_efficiency=0.999,
                discharge_efficiency=0.999,
                charge_cost=0.0,
                discharge_cost=0.01,
                salvage_value=0.0,
            )
        ],
        "solar": SolarConfig(
            forecast_kw=np.array([0.0, 5.0, 0.0]),
            lower_kw=np.array([0.0, 0.0, 0.0]),
            upper_kw=np.array([0.0, 12.0, 0.0]),
        ),
        "loads": [LoadConfig(name="house", forecast_kw=np.array([0.0, 0.0, 8.0]))],
    }


def _summary(result):
    kw = _inputs()
    return sd.summary_attribute(
        result,
        mode="shadow",
        deterministic_plan=network.build_plan(**kw),
        deterministic_solve_seconds=0.1,
        drives_dispatch=False,
    )


def test_an_optimal_plan_records_each_call_it_made():
    result = sd.build_stochastic_plan(_inputs())
    assert result.status == "optimal"
    calls = result.joint_calls
    assert len(calls) == result.n_joint_solves >= 1
    assert [c["call"] for c in calls] == list(range(1, len(calls) + 1))
    last = calls[-1]
    assert last["status"] == "optimal" and last["solve_seconds"] >= 0.0
    assert last["time_limit_seconds"] == sd.JOINT_TIME_LIMIT_SECONDS
    published = _summary(result)["joint_calls"]
    assert published == calls
    json.dumps(published, allow_nan=False)  # plain, finite JSON


def test_a_call_that_runs_out_of_time_is_named_with_its_own_limit():
    result = sd.build_stochastic_plan(_inputs(), time_limit_seconds=1e-9)
    assert result.status != "optimal"
    failed = [c for c in result.joint_calls if c["status"] != "optimal"]
    assert failed and failed[-1]["time_limit_seconds"] is not None
    assert failed[-1]["raw_status"] == result.raw_status
    json.dumps(_summary(result)["joint_calls"], allow_nan=False)


def test_a_call_refused_by_the_spent_budget_is_recorded_too():
    result = sd.build_stochastic_plan(_inputs(), budget_seconds=0.0)
    assert result.raw_status == "stochastic_budget_exhausted"
    assert result.joint_calls[-1] == {
        "call": len(result.joint_calls),
        "solve_seconds": result.joint_calls[-1]["solve_seconds"],
        "time_limit_seconds": 0.0,
        "status": "error",
        "raw_status": "stochastic_budget_exhausted",
        "mip_gap": None,
        "thermal_hard_deadline": True,  # the first attempt is the hard one
        "direction_binaries": 0,
    }
