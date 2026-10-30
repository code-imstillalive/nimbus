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
from solver import lp as _solver_lp
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


def test_a_call_that_runs_out_of_time_is_named_with_its_own_limit(monkeypatch):
    # test_a_call_that_runs_out_of_time_is_named_with_its_own_limit
    _solver_lp._lex_calibration_failed_until = 0.0
    result = sd.build_stochastic_plan(_inputs(), time_limit_seconds=1e-9)
    assert result.status != "optimal"
    failed = [c for c in result.joint_calls if c["status"] != "optimal"]
    assert failed and failed[-1]["time_limit_seconds"] is not None
    assert failed[-1]["raw_status"] == result.raw_status
    json.dumps(_summary(result)["joint_calls"], allow_nan=False)

    # test_a_call_refused_by_the_spent_budget_is_recorded_too
    _solver_lp._lex_calibration_failed_until = 0.0
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
        "joint_solve_seconds": None,  # refused before any solve ran
    }

    # test_a_scenario_that_cannot_be_built_is_recorded_too
    _solver_lp._lex_calibration_failed_until = 0.0
    with monkeypatch.context() as mp:
        real = network._build_plan_once

        def build(**kw):
            if kw.get("lp_solve_hook").__class__.__name__ == "_Capture":
                return None  # the scenario's problem is never captured
            return real(**kw)

        mp.setattr(network, "_build_plan_once", build)
        result = sd.build_stochastic_plan(_inputs())
        assert result.status != "optimal"
        last = result.joint_calls[-1]
        assert last["raw_status"] == result.raw_status
        assert last["raw_status"].endswith("_not_built")
        assert last["time_limit_seconds"] is None and last["status"] == "error"

    # test_a_call_names_its_own_solve_time_and_a_kept_incumbents_gap: #2025: devhub's failing call took 63 s against a 20 s limit, which the
    _solver_lp._lex_calibration_failed_until = 0.0
    with monkeypatch.context() as mp:
        from solver import lp

        real = lp.LPProblem.solve

        def solve(self, **kw):
            result = real(self, **kw)
            self.accepted_mip_gap = 4e-4
            return result

        mp.setattr(lp.LPProblem, "solve", solve)
        result = sd.build_stochastic_plan(_inputs())
        last = result.joint_calls[-1]
        assert last["mip_gap"] == 4e-4
        assert 0.0 <= last["joint_solve_seconds"] <= last["solve_seconds"]

    # test_the_solution_is_read_once_not_once_per_column: #2199, #2025. A
    # production joint solve has ~16,000 columns; read one at a time,
    # each read copied the whole solution, tens of seconds outside HiGHS's
    # time limit. Planted: a read per column fails here.
    _solver_lp._lex_calibration_failed_until = 0.0
    with monkeypatch.context() as mp:
        import highspy

        reads = []
        real_get = highspy.Highs.getSolution

        def get_solution(self):
            reads.append(1)
            return real_get(self)

        mp.setattr(highspy.Highs, "getSolution", get_solution)
        mp.setattr(
            highspy.Highs,
            "val",
            lambda self, var: get_solution(self).col_value[var.index],
        )
        result = sd.build_stochastic_plan(_inputs())
        assert result.status == "optimal"
        # One read per column would be at least the joint's own columns.
        n = result.n_joint_variables
        assert len(reads) < n // 2, f"{len(reads)} solution reads, {n} columns"
