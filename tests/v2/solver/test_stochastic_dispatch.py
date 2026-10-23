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
import json
from datetime import UTC, datetime

import numpy as np
import pytest
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


@pytest.mark.parametrize(
    ("hours", "k"),
    [
        ([1 / 60] * 3 + [5 / 60] * 10 + [0.5] * 4, 8),  # the tiered grid: to minute 28
        ([0.5] * 4, 1),
        ([1.0] * 4, 1),  # at least one period
        ([], 0),
    ],
)
def test_stage_1_is_every_period_that_ends_in_the_first_half_hour(hours, k):
    assert sd.stage1_period_count(np.array(hours)) == k


def test_no_sun_no_spread_and_the_band_brackets_the_forecast():
    f = np.array([0.0, 2.0, 3.0, 0.01])
    solar = SolarConfig(
        forecast_kw=f,
        lower_kw=np.array([0.0, 1.5, 1.0, 0.0]),
        upper_kw=np.array([8.0, 2.5, 6.0, 9.0]),
    )
    sc = sd.solar_scenarios(solar, k=1)
    p10, p50, p90 = (np.asarray(s.forecast_kw) for s in sc.solar)
    assert p50.tolist() == f.tolist()
    # Stage 1 and the dark periods are the forecast in every scenario.
    assert p10[[0, 3]].tolist() == p90[[0, 3]].tolist() == f[[0, 3]].tolist()
    assert (p10 <= p50).all() and (p50 <= p90).all() and (p10 >= 0).all()
    assert (p10[1], p90[1]) == (1.5, 2.5)
    assert sc.no_sun_periods == 1 and sc.band == "envelope"
    assert all(s.lower_kw is None and s.upper_kw is None for s in sc.solar)


def test_a_calibrated_band_is_preferred_and_no_band_or_no_spread_is_no_scenarios():
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


@pytest.mark.parametrize("weights", [(0.5, 0.5), (0.2, 0.2, 0.2), (0.0, 0.5, 0.5)])
def test_scenario_weights_must_be_three_positive_and_sum_to_one(weights):
    with pytest.raises(ValueError, match="weights"):
        sd.build_stochastic_plan(_inputs(), weights=weights)


# --- the joint solve ---------------------------------------------------------------------------


def test_every_scenario_makes_the_same_first_decision(solved):
    assert solved.status == "optimal" and solved.k == 1
    stage1 = {
        label: kw[: solved.k] for label, kw in solved.scenario_stage1_battery_kw.items()
    }
    assert set(stage1) == set(sd.SCENARIO_LABELS)
    first = stage1["p50"]
    for kw in stage1.values():
        assert kw == pytest.approx(first, abs=1e-6)
    assert solved.nonanticipativity_max_gap <= sd.NONANTICIPATIVITY_TOLERANCE
    assert solved.stage1_battery_kw == pytest.approx(sd.net_battery_kw(solved.plan))
    # The published plan is the p50 trajectory.
    assert solved.scenario_battery_kw["p50"][0] == pytest.approx(
        solved.stage1_battery_kw
    )


def test_the_expected_cost_is_the_weighted_sum_and_more_sun_never_costs_more(solved):
    costs = solved.scenario_costs
    assert solved.expected_cost == pytest.approx(
        sum(
            w * costs[s]
            for s, w in zip(sd.SCENARIO_LABELS, sd.SCENARIO_WEIGHTS, strict=True)
        )
    )
    assert costs["p90"] <= costs["p50"] + 1e-6 <= costs["p10"] + 2e-6


def test_holding_the_deterministic_first_decision_never_beats_the_stochastic_plan(
    solved,
):
    """The value of the stochastic solution is never negative."""
    deterministic = build_plan(**_inputs())
    fixed = sd.fixed_stage1_from_plan(deterministic, solved.k)
    assert set(fixed) == {"battery_charge_home_0", "battery_discharge_home_0"}
    held = sd.build_stochastic_plan(_inputs(), fixed_stage1=fixed)
    assert held.status == "optimal"
    assert held.expected_cost >= solved.expected_cost - 1e-6
    assert held.stage1_battery_kw == pytest.approx(
        sd.net_battery_kw(deterministic), abs=1e-6
    )


@pytest.mark.parametrize(
    ("requested", "weight", "mode"),
    [
        (None, 0.3, "primary"),
        (object(), 0.3, "blended"),
        (object(), None, "primary_only"),
        (object(), 0.0, "primary_only"),
    ],
)
def test_the_joint_solve_is_one_phase_never_lexicographic(requested, weight, mode):
    options, label = sd.joint_options(requested, weight)
    assert label == mode
    assert (options is None) is (mode != "blended")
    if options is not None:
        assert options.blend_weight == weight


# --- what is published -------------------------------------------------------------------------


def test_the_published_summary_paths_and_detail_are_plain_json(solved):
    inputs = _inputs()
    deterministic = build_plan(**inputs)
    summary = sd.summary_attribute(
        solved,
        mode="shadow",
        deterministic_plan=deterministic,
        deterministic_solve_seconds=0.5,
        drives_dispatch=False,
        computed_at="2026-09-20T00:00:00+00:00",
        grid=inputs["grid"],
        hours=inputs["periods"].hours,
    )
    json.dumps(summary)
    assert summary["status"] == "optimal" and summary["drives_dispatch"] is False
    assert summary["scenarios"] == list(sd.SCENARIO_LABELS)
    assert summary["expected_grid_cost"] == pytest.approx(
        sum(
            w * solved.scenario_grid_cost[s]
            for s, w in zip(sd.SCENARIO_LABELS, sd.SCENARIO_WEIGHTS, strict=True)
        ),
        abs=1e-4,
    )
    assert summary["deterministic_grid_cost"] == pytest.approx(
        sd.plan_grid_money(deterministic, inputs["grid"], inputs["periods"].hours),
        abs=1e-4,
    )
    paths = sd.paths_attribute(solved)
    json.dumps(paths)
    assert len(paths["ends"]) == N
    assert all(0 <= v <= 100 for path in paths["soc_pct"].values() for v in path)
    detail = sd.detail_attribute(solved)
    json.dumps(detail)
    assert len(detail["period_hours"]) == N and set(detail["scenario_soc_kwh"]) == set(
        sd.SCENARIO_LABELS
    )
    nothing = sd.StochasticResult(
        status="no_band",
        plan=None,
        k=1,
        weights=sd.SCENARIO_WEIGHTS,
        labels=sd.SCENARIO_LABELS,
        solve_seconds=0.0,
    )
    assert sd.paths_attribute(nothing) == sd.detail_attribute(nothing) == {}


def test_grid_money_is_what_the_bill_would_say():
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


def test_stage_1_solar_is_the_forecast_even_where_the_band_is_wide():
    f = np.array([3.0, 3.0, 3.0])
    solar = SolarConfig(forecast_kw=f, lower_kw=np.zeros(3), upper_kw=f * 3)
    p10, _p50, p90 = (
        np.asarray(s.forecast_kw) for s in sd.solar_scenarios(solar, k=2).solar
    )
    assert p10[:2].tolist() == p90[:2].tolist() == [3.0, 3.0]
    assert (p10[2], p90[2]) == (0.0, 9.0)


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


@pytest.mark.parametrize(
    ("weights", "first_kw"),
    [((0.7, 0.2, 0.1), -5.0), ((1 / 3, 1 / 3, 1 / 3), 0.0), ((0.2, 0.2, 0.6), 0.0)],
)
def test_the_first_decision_minimises_the_weighted_cost(weights, first_kw):
    """Charge now only if the sunless day is likely enough; and no other
    first decision, with the best recourse in each scenario, costs less."""
    best = sd.build_stochastic_plan(_hedge(), weights=weights)
    assert best.status == "optimal"
    assert best.stage1_battery_kw == pytest.approx(first_kw, abs=1e-6)
    for charge in (0.0, 1.0, 2.5, 5.0):
        held = sd.build_stochastic_plan(
            _hedge(),
            weights=weights,
            fixed_stage1={
                "battery_charge_home_0": charge,
                "battery_discharge_home_0": 0.0,
            },
        )
        assert held.status == "optimal"
        assert held.expected_cost >= best.expected_cost - 1e-6, charge
