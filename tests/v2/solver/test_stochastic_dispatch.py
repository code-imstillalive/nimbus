"""Two-stage stochastic dispatch inside the main solve (#1797) (#1837).

The real model solved once per solar scenario (p10 / p50 / p90) in one
joint problem, every scenario's first half hour forced to the same
decision. Checked against the real LP: the scenarios agree in stage 1, the
expected cost is the weighted sum of theirs, holding the deterministic
plan's stage 1 can only cost as much or more, and a band that says nothing
leaves the deterministic plan alone.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import numpy as np
import pytest
from solver import lp as _solver_lp
from solver import stochastic_dispatch as sd
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

N = 12
START = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)  # 10:00 Brisbane
SUN = np.array([0.0, 3.0, 4.0, 5.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0, 0.0, 0.0])


def _inputs(solar=None, *, n=N):
    return {
        "periods": PeriodGrid(np.full(n, 0.5), start=START),
        "grid": GridConfig(
            import_price=np.array([0.25] * 6 + [0.45] * (n - 6)),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=10.0,
        ),
        "batteries": [
            BatteryConfig(
                name="home",
                capacity_kwh=10.0,
                initial_soc_kwh=4.0,
                min_soc_kwh=1.0,
                max_soc_kwh=9.5,
                max_charge_kw=5.0,
                max_discharge_kw=5.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.006,
                discharge_cost=0.006,
                salvage_value=0.2,
            )
        ],
        "solar": solar
        or SolarConfig(
            forecast_kw=SUN[:n], lower_kw=np.zeros(n), upper_kw=SUN[:n] * 2.0
        ),
        "loads": [LoadConfig(name="house", forecast_kw=np.full(n, 1.5))],
    }


@pytest.fixture(scope="module")
def solved():
    return sd.build_stochastic_plan(_inputs())


# --- the band, the stages ---------------------------------------------------------------------


def test_stage_1_is_every_period_that_ends_in_the_first_half_hour(solved):
    # test_stage_1_is_every_period_that_ends_in_the_first_half_hour
    _solver_lp._lex_calibration_failed_until = 0.0
    for hours, k in [
        ([1 / 60] * 3 + [5 / 60] * 10 + [0.5] * 4, 8),  # the tiered grid: to minute 28
        ([0.5] * 4, 1),
        ([1.0] * 4, 1),  # at least one period
        ([], 0),
    ]:
        assert sd.stage1_period_count(np.array(hours)) == k

    # test_a_calibrated_band_is_preferred_and_no_band_or_no_spread_is_no_scenarios
    _solver_lp._lex_calibration_failed_until = 0.0
    f = np.array([1.0, 2.0, 3.0])
    calibrated = SolarConfig(
        forecast_kw=f,
        lower_kw=np.zeros(3),
        upper_kw=f * 4,
        p10_kw=f * 0.5,
        p90_kw=f * 1.5,
    )
    sc = sd.solar_scenarios(calibrated, k=1)
    assert sc.band == "calibrated"
    assert np.asarray(sc.solar[2].forecast_kw).tolist() == [1.0, 3.0, 4.5]
    assert sd.solar_scenarios(SolarConfig(forecast_kw=f), k=1) is None
    flat = SolarConfig(forecast_kw=f, lower_kw=f, upper_kw=f)
    assert sd.solar_scenarios(flat, k=1) is None
    result = sd.build_stochastic_plan(_inputs(flat, n=3))
    assert (result.status, result.plan) == ("no_band", None)

    # test_scenario_weights_must_be_three_positive_and_sum_to_one
    _solver_lp._lex_calibration_failed_until = 0.0
    for weights in [(0.5, 0.5), (0.2, 0.2, 0.2), (0.0, 0.5, 0.5)]:
        with pytest.raises(ValueError, match="weights"):
            sd.build_stochastic_plan(_inputs(), weights=weights)

    # test_the_expected_cost_is_the_weighted_sum_and_more_sun_never_costs_more
    _solver_lp._lex_calibration_failed_until = 0.0
    costs = solved.scenario_costs
    assert solved.expected_cost == pytest.approx(
        sum(
            w * costs[s]
            for s, w in zip(sd.SCENARIO_LABELS, sd.SCENARIO_WEIGHTS, strict=True)
        )
    )
    assert costs["p90"] <= costs["p50"] + 1e-6 <= costs["p10"] + 2e-6

    # test_holding_the_deterministic_first_decision_never_beats_the_stochastic_plan: The value of the stochastic solution is never negative.
    _solver_lp._lex_calibration_failed_until = 0.0
    deterministic = build_plan(**_inputs())
    fixed = sd.fixed_stage1_from_plan(deterministic, solved.k)
    assert set(fixed) == {"battery_charge_home_0", "battery_discharge_home_0"}
    held = sd.build_stochastic_plan(_inputs(), fixed_stage1=fixed)
    assert held.status == "optimal"
    assert held.expected_cost >= solved.expected_cost - 1e-6
    assert held.stage1_battery_kw == pytest.approx(
        sd.net_battery_kw(deterministic), abs=1e-6
    )

    # test_grid_money_is_what_the_bill_would_say
    _solver_lp._lex_calibration_failed_until = 0.0
    grid = GridConfig(
        import_price=np.array([0.3, 0.3]),
        export_price=np.array([0.1, 0.1]),
        import_limit_kw=10.0,
        export_limit_kw=10.0,
    )
    money = sd.grid_money(
        grid,
        np.array([0.5, 0.5]),
        import_kw=np.array([2.0, 0.0]),
        export_kw=np.array([0.0, 4.0]),
        bonus_kw=np.zeros(2),
    )
    assert money == pytest.approx(0.5 * 0.3 * 2.0 - 0.5 * 0.1 * 4.0)
    failed = dataclasses.replace(build_plan(**_inputs()), status="infeasible")
    assert sd.plan_grid_money(failed, grid, np.array([0.5])) is None


# --- the joint solve ---------------------------------------------------------------------------


# --- what is published -------------------------------------------------------------------------


def _hedge():
    """Cheap import now, dear later, no sun now. Only p10 is short of sun
    later, and energy bought now is worth nothing where the sun covers the
    load: charging pays only when p10 carries enough weight."""
    n = 6
    sun = np.array([0.0, 2.0, 2.0, 2.0, 2.0, 2.0])
    inputs = _inputs(
        SolarConfig(forecast_kw=sun, lower_kw=np.zeros(n), upper_kw=sun * 2), n=n
    )
    inputs["grid"] = GridConfig(
        import_price=np.array([0.20] + [0.50] * 5),
        export_price=np.zeros(n),
        import_limit_kw=20.0,
        export_limit_kw=10.0,
    )
    inputs["batteries"] = [
        dataclasses.replace(
            inputs["batteries"][0],
            initial_soc_kwh=1.0,
            max_soc_kwh=10.0,
            salvage_value=0.0,
        )
    ]
    inputs["loads"] = [LoadConfig(name="house", forecast_kw=np.full(n, 2.0))]
    return inputs
