"""The two-stage stochastic solve wired into the main solve (#1797) (#1837).

The model's scenarios, stage 1, non-anticipativity and weighted objective;
the LP plumbing it needs (`LPProblem.absorb()`, `build_plan(lp_solve_hook=)`);
the orchestration in `solver_plan.run_stochastic_dispatch()` (shadow cadence,
drive and every fallback); and the real `main()`: switched off, the plan is
byte-identical to a run without the stochastic solve, and on, the published
plan is the stochastic one.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import numpy as np
import pytest
import solver_plan
import solver_shared
import solver_writer
import test_main_golden_output_guardrail as guardrail
from _isolated_state import isolated_state_paths
from solver import network
from solver import stochastic_dispatch as sd
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.lp import LPProblem

T0 = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)


# -- a hand-built case where the hedge differs from p50 ---------------------
#
# Period 0 (30 min, stage 1): import is cheap, 0.12 $/kWh. Period 1 (1 h):
# solar is 0 / 5 / 12 kW (p10 / p50 / p90), export earns nothing. Period 2
# (1 h): the house needs 8 kWh and import costs 0.50. Battery 10 kWh, empty.
#
# Planned on p50, the battery buys 3 kWh now: 5 kWh of solar covers the rest.
# Hedged, it buys 8: a kWh bought now costs 0.12 and is needed in the 30% p10
# world at 0.50 (expected value 0.15 > 0.12); the p50 world only needs 3, and
# in the p90 world solar fills the battery anyway, so every kWh beyond 3 is
# worth exactly its p10 share.


def _hedge_inputs() -> dict:
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


def test_hedge_differs_from_the_p50_plan():
    kw = _hedge_inputs()
    det = network.build_plan(**kw)
    assert det.status == "optimal"
    assert det.battery_charge_kw[0] * 0.5 == pytest.approx(3.0, abs=0.05)

    res = sd.build_stochastic_plan(kw)
    assert res.status == "optimal"
    assert res.k == 1
    assert res.plan is not None
    assert res.plan.battery_charge_kw[0] * 0.5 == pytest.approx(8.0, abs=0.05)
    # The published period 0 is the shared decision.
    assert res.stage1_battery_kw == pytest.approx(-res.plan.battery_charge_kw[0])

    # Value of the stochastic solution: dispatch the p50 plan's stage 1, let
    # every scenario recourse optimally, and the expected cost is higher.
    eev = sd.build_stochastic_plan(kw, fixed_stage1=sd.fixed_stage1_from_plan(det, 1))
    assert eev.status == "optimal"
    assert eev.expected_cost - res.expected_cost == pytest.approx(0.1374, abs=0.002)


def test_expected_cost_is_the_weighted_sum_of_scenario_costs():
    res = sd.build_stochastic_plan(_hedge_inputs())
    w = dict(zip(sd.SCENARIO_LABELS, sd.SCENARIO_WEIGHTS, strict=True))
    assert res.expected_cost == pytest.approx(
        sum(w[k] * v for k, v in res.scenario_costs.items())
    )
    # The published plan is p50's: its total_cost is p50's own cost.
    assert res.plan.total_cost == pytest.approx(res.scenario_costs["p50"])


def test_weights_sum_to_one_and_bad_weights_are_refused():
    assert sum(sd.SCENARIO_WEIGHTS) == pytest.approx(1.0)
    assert sd.SCENARIO_WEIGHTS == (0.3, 0.4, 0.3)
    with pytest.raises(ValueError, match="sum to 1"):
        sd.build_stochastic_plan(_hedge_inputs(), weights=(0.3, 0.3, 0.3))
    with pytest.raises(ValueError, match="positive"):
        sd.build_stochastic_plan(_hedge_inputs(), weights=(0.0, 0.5, 0.5))


# -- non-anticipativity -------------------------------------------------------


def _tiered_inputs(n_thirty: int = 20) -> dict:
    """The reference household's grid shape: 3 x 1 min, 10 x 5 min, then 30
    min. Prices step up in the evening; solar band wide in daytime."""
    hours = np.array([1 / 60] * 3 + [5 / 60] * 10 + [0.5] * n_thirty)
    n = len(hours)
    starts_h = np.concatenate([[0.0], np.cumsum(hours)[:-1]])
    # Start at 08:00 local-ish: a sun bell over the first ~10 h.
    sun = np.clip(np.sin((starts_h + 0.0) / 10.0 * np.pi), 0.0, None) * 6.0
    imp = np.where(starts_h < 8.0, 0.10, 0.45)
    return {
        "periods": PeriodGrid(hours=hours, start=T0),
        "grid": GridConfig(
            import_price=imp,
            export_price=imp * 0.3,
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        "batteries": [
            BatteryConfig(
                name="home",
                capacity_kwh=20.0,
                initial_soc_kwh=6.0,
                min_soc_kwh=2.0,
                max_soc_kwh=19.0,
                max_charge_kw=8.0,
                max_discharge_kw=8.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.0,
                discharge_cost=0.02,
                salvage_value=0.10,
            )
        ],
        "solar": SolarConfig(
            forecast_kw=sun,
            lower_kw=sun * 0.3,
            upper_kw=sun * 1.6,
        ),
        "loads": [LoadConfig(name="house", forecast_kw=np.full(n, 1.5))],
    }


def test_stage1_is_the_periods_ending_in_the_first_30_minutes():
    hours = _tiered_inputs()["periods"].hours
    # Ends at 1, 2, 3, 8, 13, 18, 23, 28, 33 min: eight end by minute 30.
    assert sd.stage1_period_count(hours) == 8
    assert sd.stage1_period_count(np.full(10, 5 / 60)) == 6
    assert sd.stage1_period_count(np.full(4, 1.0)) == 1  # at least one
    assert sd.stage1_period_count(np.full(3, 1 / 60)) == 3  # at most n


def test_nonanticipativity_holds_and_stage1_is_equal_across_scenarios():
    res = sd.build_stochastic_plan(_tiered_inputs())
    assert res.status == "optimal"
    assert res.k == 8
    assert res.n_nonanticipativity_rows > 0
    assert res.nonanticipativity_max_gap == pytest.approx(0.0, abs=1e-6)
    stage1 = res.scenario_stage1_battery_kw
    assert set(stage1) == set(sd.SCENARIO_LABELS)
    for t in range(res.k):
        assert stage1["p10"][t] == pytest.approx(stage1["p50"][t], abs=1e-6)
        assert stage1["p90"][t] == pytest.approx(stage1["p50"][t], abs=1e-6)
    # Scenarios really are different worlds after stage 1.
    assert res.scenario_costs["p10"] > res.scenario_costs["p50"]
    assert res.scenario_costs["p50"] > res.scenario_costs["p90"]


def test_a_shared_chargers_direction_is_a_stage1_decision():
    """#2031: one direction for all of a charger's members; in stage 1 it is
    the same in every scenario, like a battery's own."""
    kw = _tiered_inputs()
    home = kw["batteries"][0]
    group = {"shared_charger_group": "garage", "shared_charger_max_kw": 6.0}
    kw["batteries"] = [
        dataclasses.replace(home, name="car_a", **group),
        dataclasses.replace(home, name="car_b", **group),
    ]
    capture = sd._Capture()
    network._build_plan_once(
        **kw, lp_solve_hook=capture, direction_binaries=network._ALL_DIRECTIONS
    )
    names = sd.stage1_decision_names(
        capture.problem, 2, batteries=kw["batteries"], load_names=[]
    )
    assert {"charger_dir_garage_0", "charger_dir_garage_1"} <= set(names)
    assert "charger_dir_garage_2" not in names


def test_stage1_decision_names_cover_the_decisions_and_nothing_else():
    kw = _tiered_inputs()
    capture = sd._Capture()
    network._build_plan_once(**kw, lp_solve_hook=capture)
    names = sd.stage1_decision_names(
        capture.problem, 2, batteries=kw["batteries"], load_names=[]
    )
    for t in (0, 1):
        for n in (
            f"battery_charge_home_{t}",
            f"battery_discharge_home_{t}",
            f"grid_import_{t}",
            f"grid_export_{t}",
            f"solar_used_{t}",
        ):
            assert n in names
    assert not any(n.endswith("_2") for n in names)
    assert not any(n.startswith("battery_soc_") for n in names)


def test_the_joint_problem_is_three_complete_copies_of_the_model():
    """Every feature carries over because nothing is re-implemented: the joint
    problem has exactly three times the deterministic model's variables, and
    every one of them exists under each scenario's prefix."""
    kw = _tiered_inputs()
    single = sd._Capture()
    network._build_plan_once(**kw, lp_solve_hook=single)
    res = sd.build_stochastic_plan(kw)
    assert res.n_joint_variables == 3 * single.problem.n_variables


# -- no band: a no-op -----------------------------------------------------------


def test_no_band_is_a_noop():
    kw = _hedge_inputs()
    kw["solar"] = SolarConfig(forecast_kw=np.array([0.0, 5.0, 0.0]))
    res = sd.build_stochastic_plan(kw)
    assert res.status == "no_band"
    assert res.plan is None


def test_zero_width_band_is_a_noop():
    kw = _hedge_inputs()
    f = np.array([0.0, 5.0, 0.0])
    kw["solar"] = SolarConfig(forecast_kw=f, lower_kw=f.copy(), upper_kw=f.copy())
    assert sd.build_stochastic_plan(kw).status == "no_band"


def test_a_band_only_at_night_is_a_noop():
    """No sun, no spread: an envelope reading 6-17 kW at midnight (measured on
    the reference household, 16 Oct 2026) is not a scenario."""
    kw = _hedge_inputs()
    f = np.array([0.0, 0.0, 0.0])
    kw["solar"] = SolarConfig(
        forecast_kw=f, lower_kw=f.copy(), upper_kw=np.array([0.0, 15.0, 15.0])
    )
    assert sd.build_stochastic_plan(kw).status == "no_band"


def test_scenarios_share_p50_in_stage1_and_drop_their_band():
    solar = SolarConfig(
        forecast_kw=np.array([2.0, 2.0, 4.0, 0.0]),
        lower_kw=np.array([1.0, 1.0, 1.0, 0.0]),
        upper_kw=np.array([3.0, 3.0, 9.0, 5.0]),
    )
    sc = sd.solar_scenarios(solar, k=2)
    assert sc is not None
    p10, p50, p90 = sc.solar
    assert list(p10.forecast_kw) == [2.0, 2.0, 1.0, 0.0]
    assert list(p50.forecast_kw) == [2.0, 2.0, 4.0, 0.0]
    assert list(p90.forecast_kw) == [2.0, 2.0, 9.0, 0.0]
    assert all(s.lower_kw is None and s.upper_kw is None for s in sc.solar)
    assert sc.no_sun_periods == 1


# -- the LP plumbing -------------------------------------------------------------


def test_absorb_into_an_empty_problem_reproduces_it():
    kw = _tiered_inputs()
    capture = sd._Capture()
    network._build_plan_once(**kw, lp_solve_hook=capture)
    p = capture.problem
    copy_p = LPProblem()
    copy_p.absorb(p, prefix="", cost_scale=1.0)
    a = p.solve()
    b = copy_p.solve()
    assert a.status == b.status == "optimal"
    assert a.objective == pytest.approx(b.objective)
    assert np.allclose(a.x, b.x)
    assert p.primary_cost_of(b) == pytest.approx(a.objective)


def test_absorb_refuses_an_unprefixed_merge_into_a_non_empty_problem():
    p = LPProblem()
    p.add_variable("x", ub=1.0)
    with pytest.raises(ValueError, match="empty"):
        p.absorb(p, prefix="", cost_scale=1.0)


def test_build_plan_without_a_hook_is_unchanged():
    kw = _tiered_inputs()
    a = network.build_plan(**kw)
    b = network.build_plan(**kw, lp_solve_hook=None)
    assert a.total_cost == b.total_cost
    assert np.array_equal(a.battery_charge_kw, b.battery_charge_kw)
    assert np.array_equal(a.battery_discharge_kw, b.battery_discharge_kw)


def test_a_failed_joint_solve_is_a_status_not_an_exception():
    kw = _tiered_inputs()
    res = sd.build_stochastic_plan(kw, time_limit_seconds=1e-9)
    assert res.status != "optimal"
    assert res.plan is None


# -- orchestration: run_stochastic_dispatch() ---------------------------------


@pytest.fixture(autouse=True)
def _fresh_stochastic_state(monkeypatch):
    # In-line shadow solves, so a test reads the summary from the call that
    # ran it; the background thread has its own tests below.
    monkeypatch.setattr(solver_plan, "STOCHASTIC_SHADOW_IN_BACKGROUND", False)
    solver_plan.reset_stochastic_state()
    yield
    assert solver_plan.wait_for_stochastic_shadow(timeout=60)
    solver_plan.reset_stochastic_state()


def _det_and_stoch():
    """The tiered case, deterministic and stochastic. The stochastic result
    is labelled calibrated: drive refuses an envelope band (see
    test_drive_refuses_an_envelope_band), and these tests are about what
    happens once it is allowed."""
    kw = _tiered_inputs()
    det = network.build_plan(**kw)
    stoch = sd.build_stochastic_plan(kw)
    assert det.status == stoch.status == "optimal"
    return kw, det, dataclasses.replace(stoch, solar_band="calibrated")


def test_shadow_keeps_the_deterministic_plan_and_publishes_a_summary():
    kw, det, _ = _det_and_stoch()
    published, summary = solver_plan.run_stochastic_dispatch(
        {}, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
    )
    assert published is det
    assert summary["mode"] == "shadow"
    assert summary["status"] == "optimal"
    assert summary["drives_dispatch"] is False
    assert summary["fallback_reason"] is None
    assert summary["stage1_periods"] == 8
    assert summary["weights"] == [0.3, 0.4, 0.3]
    assert summary["nonanticipativity_max_gap"] == pytest.approx(0.0, abs=1e-6)
    assert set(summary["scenario_cost"]) == {"p10", "p50", "p90"}


def test_shadow_runs_at_most_every_five_minutes_and_holds_its_summary():
    kw, det, stoch = _det_and_stoch()
    with patch.object(sd, "build_stochastic_plan", return_value=stoch) as spy:
        _, first = solver_plan.run_stochastic_dispatch(
            {}, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
        )
        _, held = solver_plan.run_stochastic_dispatch(
            {},
            now=T0 + timedelta(seconds=299),
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
        assert spy.call_count == 1
        assert held is first
        solver_plan.run_stochastic_dispatch(
            {},
            now=T0 + timedelta(seconds=300),
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
        assert spy.call_count == 2


def test_a_slow_shadow_solve_backs_off_for_an_hour():
    kw, det, stoch = _det_and_stoch()
    slow = dataclasses.replace(stoch, solve_seconds=25.0)
    with patch.object(sd, "build_stochastic_plan", return_value=slow) as spy:
        for minutes in (0, 6, 59):
            solver_plan.run_stochastic_dispatch(
                {},
                now=T0 + timedelta(minutes=minutes),
                plan=det,
                build_kwargs=kw,
                deterministic_seconds=0.5,
            )
        assert spy.call_count == 1
        solver_plan.run_stochastic_dispatch(
            {},
            now=T0 + timedelta(minutes=60),
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
        assert spy.call_count == 2


@pytest.mark.parametrize(
    ("seconds", "runs"), [(14.9, True), (15.0, False), (23.6, False)]
)
def test_a_main_plan_that_used_half_the_shadow_budget_skips_the_shadow(
    seconds, runs, caplog
):
    """#2025: the joint problem is three copies of the main one, so a main
    solve that alone took half the shadow budget leaves no room for it. That
    cycle publishes a skip naming why, logs it once, and dispatch is
    untouched; a faster main plan still gets its shadow."""
    kw, det, stoch = _det_and_stoch()
    with patch.object(sd, "build_stochastic_plan", return_value=stoch) as spy:
        for minute in (0, 6):
            published, summary = solver_plan.run_stochastic_dispatch(
                {},
                now=T0 + timedelta(minutes=minute),
                plan=det,
                build_kwargs=kw,
                deterministic_seconds=seconds,
            )
            assert published is det
    assert spy.call_count == (2 if runs else 0)
    if runs:
        assert summary["status"] == "optimal"
        return
    assert summary["status"] == "skipped"
    assert summary["fallback_reason"] == "deterministic_too_slow"
    assert summary["deterministic_solve_seconds"] == seconds
    (warning,) = [
        r for r in caplog.records if "deterministic_too_slow" in r.getMessage()
    ]
    assert f"took {seconds:.1f} s of the 30 s shadow budget" in warning.getMessage()


@pytest.mark.parametrize(
    ("budget", "seconds", "runs"),
    [(None, 4.9, True), (None, 5.0, False), (None, 9.2, False), (40.0, 19.9, True)],
)
def test_a_main_plan_that_used_half_the_drive_budget_skips_the_drive(
    budget, seconds, runs, caplog
):
    """#2047: drive mode gets the shadow's backstop against its own budget
    (10 s by default, or the configured one). The skipped cycle publishes the
    main plan with a named reason, logs once, and leaves the backoff alone:
    the next fast cycle drives at once."""
    kw, det, stoch = _det_and_stoch()
    cfg = {"solver_stochastic_dispatch": True}
    if budget is not None:
        cfg["solver_stochastic_drive_budget_seconds"] = budget
    with patch.object(sd, "build_stochastic_plan", return_value=stoch) as spy:
        for minute in (0, 6):
            published, summary = solver_plan.run_stochastic_dispatch(
                cfg,
                now=T0 + timedelta(minutes=minute),
                plan=det,
                build_kwargs=kw,
                deterministic_seconds=seconds,
            )
        assert spy.call_count == (2 if runs else 0)
        if runs:
            assert published is not det
            return
        assert published is det
        assert summary["mode"] == "drive"
        assert summary["status"] == "skipped"
        assert summary["fallback_reason"] == "deterministic_too_slow"
        assert summary["deterministic_solve_seconds"] == seconds
        (warning,) = [
            r for r in caplog.records if "deterministic_too_slow" in r.getMessage()
        ]
        assert f"took {seconds:.1f} s of the 10 s drive budget" in (
            warning.getMessage()
        )
        published, _ = solver_plan.run_stochastic_dispatch(
            cfg,
            now=T0 + timedelta(minutes=7),
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
        assert spy.call_count == 1
        assert published is not det


def test_drive_publishes_the_stochastic_plan():
    kw, det, stoch = _det_and_stoch()
    det = dataclasses.replace(det, offer_curve_import=[(0.1, 2.0)])
    with patch.object(sd, "build_stochastic_plan", return_value=stoch):
        published, summary = solver_plan.run_stochastic_dispatch(
            {"solver_stochastic_dispatch": True},
            now=T0,
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
    assert published is not det
    assert np.array_equal(published.battery_charge_kw, stoch.plan.battery_charge_kw)
    # The joint model has no offer curve; the deterministic one is kept.
    assert published.offer_curve_import == [(0.1, 2.0)]
    assert summary["mode"] == "drive"
    assert summary["drives_dispatch"] is True
    assert summary["signals_from"] == "deterministic"


def test_drive_runs_every_cycle():
    kw, det, stoch = _det_and_stoch()
    with patch.object(sd, "build_stochastic_plan", return_value=stoch) as spy:
        for s in (0, 15, 30):
            solver_plan.run_stochastic_dispatch(
                {"solver_stochastic_dispatch": True},
                now=T0 + timedelta(seconds=s),
                plan=det,
                build_kwargs=kw,
                deterministic_seconds=0.5,
            )
    assert spy.call_count == 3


@pytest.mark.parametrize(
    ("returned", "reason"),
    [
        ("infeasible", "stochastic_infeasible"),
        ("error", "stochastic_error"),
        ("no_band", "no_solar_band"),
    ],
)
def test_drive_falls_back_on_a_non_optimal_solve(returned, reason, caplog):
    kw, det, stoch = _det_and_stoch()
    bad = dataclasses.replace(stoch, status=returned, plan=None)
    with patch.object(sd, "build_stochastic_plan", return_value=bad):
        published, summary = solver_plan.run_stochastic_dispatch(
            {"solver_stochastic_dispatch": True},
            now=T0,
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
    assert published is det
    assert summary["fallback_reason"] == reason
    assert summary["drives_dispatch"] is False
    if returned == "no_band":
        # The ordinary no-op: no warning.
        assert "#1797" not in caplog.text
    else:
        assert f"fallback_reason={reason}" in caplog.text
        assert "publishes the deterministic plan" in caplog.text


def test_drive_falls_back_on_an_exception(caplog):
    kw, det, _ = _det_and_stoch()
    with patch.object(sd, "build_stochastic_plan", side_effect=RuntimeError("boom")):
        published, summary = solver_plan.run_stochastic_dispatch(
            {"solver_stochastic_dispatch": True},
            now=T0,
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
    assert published is det
    assert summary["fallback_reason"] == "error:RuntimeError"
    assert "boom" in caplog.text


def _drive(kw, det, seconds):
    return solver_plan.run_stochastic_dispatch(
        {"solver_stochastic_dispatch": True},
        now=T0 + timedelta(seconds=seconds),
        plan=det,
        build_kwargs=kw,
        deterministic_seconds=0.5,
    )


def test_drive_backs_off_exponentially_after_failures_and_logs_once(caplog):
    """The audit's probe: four 21 s joint timeouts in a row were four retries
    (a timeout came back under the old "slow" threshold). Any fallback now
    waits 5 min, then 10, 20, ..., and is logged once."""
    kw, det, stoch = _det_and_stoch()
    timed_out = dataclasses.replace(
        stoch, status="error", plan=None, raw_status="Time limit reached"
    )
    with patch.object(sd, "build_stochastic_plan", return_value=timed_out) as spy:
        published, summary = _drive(kw, det, 0)
        assert published is det
        assert summary["fallback_reason"] == "stochastic_error"
        _, summary = _drive(kw, det, 15)
        assert spy.call_count == 1
        assert summary["fallback_reason"] == "cooldown_after:stochastic_error"
        _drive(kw, det, 300)  # due: fails again
        assert spy.call_count == 2
        _drive(kw, det, 300 + 599)  # backoff now 10 min
        assert spy.call_count == 2
        _drive(kw, det, 300 + 600)
        assert spy.call_count == 3
    ours = [
        r
        for r in caplog.records
        if "fallback_reason=stochastic_error" in r.getMessage()
    ]
    assert len(ours) == 1


def test_drive_exceptions_back_off_and_log_once(caplog):
    kw, det, _ = _det_and_stoch()
    with patch.object(
        sd, "build_stochastic_plan", side_effect=RuntimeError("boom")
    ) as spy:
        for s in (0, 15, 30):
            published, _ = _drive(kw, det, s)
            assert published is det
        assert spy.call_count == 1
        _drive(kw, det, 300)
        assert spy.call_count == 2
    assert caplog.text.count("boom") == 1


def test_a_driven_plan_resets_the_backoff():
    kw, det, stoch = _det_and_stoch()
    bad = dataclasses.replace(stoch, status="error", plan=None)
    with patch.object(sd, "build_stochastic_plan", return_value=bad):
        _drive(kw, det, 0)
    with patch.object(sd, "build_stochastic_plan", return_value=stoch) as spy:
        published, _ = _drive(kw, det, 300)
        assert published is not det
        published, _ = _drive(kw, det, 315)
        assert published is not det
        assert spy.call_count == 2


def test_drive_refuses_an_envelope_band(caplog):
    """The envelope's p10 / p90 are nominal (p10 = 0 all day on 16 Oct), so
    the hedge computed on them is too: shadow reports it, drive does not
    dispatch it."""
    kw, det, stoch = _det_and_stoch()
    env = dataclasses.replace(stoch, solar_band="envelope")
    with patch.object(sd, "build_stochastic_plan", return_value=env):
        published, summary = _drive(kw, det, 0)
    assert published is det
    assert summary["fallback_reason"] == "solar_band_not_calibrated"
    assert summary["drives_dispatch"] is False
    solver_plan.reset_stochastic_state()
    with patch.object(sd, "build_stochastic_plan", return_value=env):
        _, summary = solver_plan.run_stochastic_dispatch(
            {}, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
        )
    assert summary["status"] == "optimal"
    assert summary["fallback_reason"] is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"thermal_guarantee_relaxed": ("hws",)}, "thermal_guarantee_relaxed"),
        ({"direction_exclusivity": "declined"}, "direction_declined"),
        ({"direction_exclusivity": "still_two_way"}, "direction_still_two_way"),
        ({"nonanticipativity_max_gap": 0.5}, "nonanticipativity_violated"),
        ({"expected_cost": float("nan")}, "scenario_cost_not_finite"),
    ],
)
def test_drive_never_dispatches_a_weaker_plan(change, reason):
    kw, det, stoch = _det_and_stoch()
    assert not det.thermal_guarantee_relaxed
    assert det.direction_exclusivity not in ("declined", "still_two_way")
    weaker = dataclasses.replace(stoch, **change)
    with patch.object(sd, "build_stochastic_plan", return_value=weaker):
        published, summary = _drive(kw, det, 0)
    assert published is det
    assert summary["fallback_reason"] == reason


def test_a_relaxed_deterministic_plan_does_not_block_an_equally_relaxed_one():
    kw, det, stoch = _det_and_stoch()
    det = dataclasses.replace(det, thermal_guarantee_relaxed=["hws"])
    same = dataclasses.replace(stoch, thermal_guarantee_relaxed=("hws",))
    with patch.object(sd, "build_stochastic_plan", return_value=same):
        published, summary = _drive(kw, det, 0)
    assert published is not det
    assert summary["fallback_reason"] is None


def test_drive_takes_every_signal_from_the_deterministic_plan():
    kw, det, stoch = _det_and_stoch()
    det = dataclasses.replace(
        det,
        battery_signals=["det-battery"],
        load_signals=["det-load"],
        grid_signals="det-grid",
        offer_curve_export=[(0.2, 1.0)],
    )
    with patch.object(sd, "build_stochastic_plan", return_value=stoch):
        published, _ = _drive(kw, det, 0)
    assert published.battery_signals == ["det-battery"]
    assert published.load_signals == ["det-load"]
    assert published.grid_signals == "det-grid"
    assert published.offer_curve_export == [(0.2, 1.0)]


def test_switching_off_never_republishes_the_drive_summary():
    """Audit probe: after switching drive off, the not-due shadow branch
    re-published the drive summary (mode "drive", drives_dispatch True) for
    up to 5 minutes while the deterministic plan was the one dispatched."""
    kw, det, stoch = _det_and_stoch()
    with patch.object(sd, "build_stochastic_plan", return_value=stoch):
        _, driven = _drive(kw, det, 0)
        assert driven["mode"] == "drive" and driven["drives_dispatch"] is True
        published, summary = solver_plan.run_stochastic_dispatch(
            {},
            now=T0 + timedelta(seconds=15),
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
    assert published is det
    assert summary["mode"] == "shadow"
    assert summary["drives_dispatch"] is False


def test_the_shadow_solve_runs_off_the_dispatch_path(monkeypatch):
    """The shadow solve used to run before publish, holding the dispatched
    plan back 3-14 s every 5 minutes. It runs on its own thread now: the
    cycle returns at once, and the summary arrives on a later cycle."""
    import threading

    monkeypatch.setattr(solver_plan, "STOCHASTIC_SHADOW_IN_BACKGROUND", True)
    kw, det, stoch = _det_and_stoch()
    release = threading.Event()

    def _slow(*_a, **_k):
        assert release.wait(10)
        return stoch

    with patch.object(sd, "build_stochastic_plan", side_effect=_slow) as spy:
        published, first = solver_plan.run_stochastic_dispatch(
            {}, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
        )
        assert published is det
        assert first["status"] == "skipped"
        assert first["fallback_reason"] == "shadow_solve_pending"
        # Still running: a second cycle neither waits nor starts another.
        _, again = solver_plan.run_stochastic_dispatch(
            {},
            now=T0 + timedelta(seconds=400),
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
        assert again["fallback_reason"] == "shadow_solve_pending"
        release.set()
        assert solver_plan.wait_for_stochastic_shadow(timeout=10)
        assert spy.call_count == 1
    _, done = solver_plan.run_stochastic_dispatch(
        {},
        now=T0 + timedelta(seconds=401),
        plan=det,
        build_kwargs=kw,
        deterministic_seconds=0.5,
    )
    assert done["status"] == "optimal"
    assert done["computed_at"] == T0.isoformat()


def test_a_shadow_job_finishing_after_a_switch_to_drive_is_dropped(monkeypatch):
    import threading

    monkeypatch.setattr(solver_plan, "STOCHASTIC_SHADOW_IN_BACKGROUND", True)
    kw, det, stoch = _det_and_stoch()
    release = threading.Event()

    def _slow(*_a, **_k):
        assert release.wait(10)
        return stoch

    with patch.object(sd, "build_stochastic_plan", side_effect=_slow):
        solver_plan.run_stochastic_dispatch(
            {}, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
        )
    with patch.object(
        sd,
        "build_stochastic_plan",
        return_value=dataclasses.replace(stoch, plan=None, status="error"),
    ):
        _, driven = _drive(kw, det, 15)
    release.set()
    assert solver_plan.wait_for_stochastic_shadow(timeout=10)
    assert solver_plan._STOCHASTIC_STATE["mode"] == "drive"
    # The late shadow job was dropped: the state holds drive's own summary.
    assert solver_plan._STOCHASTIC_STATE["summary"]["mode"] == "drive"
    assert driven["mode"] == "drive"


def test_a_non_optimal_deterministic_plan_is_left_alone():
    kw, det, _ = _det_and_stoch()
    failed = dataclasses.replace(det, status="infeasible")
    with patch.object(sd, "build_stochastic_plan") as spy:
        published, summary = solver_plan.run_stochastic_dispatch(
            {"solver_stochastic_dispatch": True},
            now=T0,
            plan=failed,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
    assert spy.call_count == 0
    assert published is failed
    assert summary["fallback_reason"] == "deterministic_not_optimal"


# -- the real main() ----------------------------------------------------------

_SOLAR = "sensor.test_solar_band"


def _solar_state() -> dict:
    """96 h of half-hourly solar from the guardrail fixture's start (20:00
    AEST), a 6 kW bell from 06:00 to 18:00 local, with a wide band."""
    start = datetime(2026, 9, 6, 10, 0, tzinfo=UTC)
    rows = []
    for i in range(200):
        t = start + timedelta(minutes=30 * i)
        local_h = (t.hour + 10 + t.minute / 60.0) % 24
        v = max(0.0, np.sin((local_h - 6.0) / 12.0 * np.pi)) * 6.0
        rows.append(
            {
                "time": t.isoformat(),
                "value": round(v, 3),
                "lower": round(v * 0.3, 3),
                "upper": round(v * 1.6, 3),
            }
        )
    return {
        "state": "0.0",
        "attributes": {"unit_of_measurement": "kW", "forecast": rows},
    }


def _run_main(cfg_extra: dict, stochastic_disabled: bool = False):
    attrs = copy.deepcopy(guardrail._SOLVER_CONFIG_ATTRS)
    attrs["solver_solar_forecast_sensor"] = _SOLAR
    attrs.update(cfg_extra)
    base = guardrail._make_ha_get()
    solar = _solar_state()

    def ha_get(entity_id):
        if entity_id == "sensor.nimbus_solver_config":
            return {"state": "configured", "attributes": attrs}
        if entity_id == _SOLAR:
            return solar
        return base(entity_id)

    posted: dict = {}

    def _post(entity_id, state, attrs=None):
        posted[entity_id] = (state, attrs)

    patches = [
        patch.object(solver_writer, "ha_get", side_effect=ha_get),
        patch.object(solver_shared, "ha_get", side_effect=ha_get),
        patch.object(solver_writer, "ha_post_state", side_effect=_post),
        isolated_state_paths(solver_writer),
    ]
    if stochastic_disabled:
        patches.append(
            patch.object(
                solver_plan,
                "run_stochastic_dispatch",
                side_effect=lambda cfg, *, plan, **_: (plan, None),
            )
        )
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        solver_writer.main()
    return posted["sensor.nimbus_solver_battery_forecast"][1]


_PLAN_KEYS = (
    "battery_kw",
    "grid_import_kw",
    "grid_export_kw",
    "soc_pct",
    "solar_kw",
    "import_price",
    "export_price",
    "net_cost",
    "shadow_price",
)


def _rows(attrs):
    return [{k: r.get(k) for k in _PLAN_KEYS} for r in attrs["forecast"]]


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_main_shadow_leaves_the_plan_byte_identical(freezer):
    shadow = _run_main({})
    without = _run_main({}, stochastic_disabled=True)
    assert _rows(shadow) == _rows(without)
    assert shadow["total_cost"] == without["total_cost"]
    assert without["stochastic"] is None
    s = shadow["stochastic"]
    assert s["mode"] == "shadow"
    assert s["status"] == "optimal", s
    assert s["drives_dispatch"] is False
    assert s["nonanticipativity_max_gap"] == pytest.approx(0.0, abs=1e-6)
    assert shadow["stochastic_period0_battery_kw"] == s["stage1_battery_kw"]


@pytest.mark.freeze_time("2026-09-06T10:00:00+00:00")
def test_main_drive_publishes_the_stochastic_period0(freezer):
    """Drive needs a calibrated band (#1807). The fixture has no solar
    history, so the measured p10 / p90 are supplied over the real
    update_and_build()'s result."""
    real = solver_writer.solar_quantiles.update_and_build

    def _calibrated(sw, cfg, now, grid_times, period_hours, solar_kw, band):
        out = dict(real(sw, cfg, now, grid_times, period_hours, solar_kw, band))
        f = np.asarray(solar_kw, dtype=float)
        out["p10"] = list(f * 0.6)
        out["p90"] = list(f * 1.3)
        return out

    with patch.object(
        solver_writer.solar_quantiles, "update_and_build", side_effect=_calibrated
    ):
        driven = _run_main({"solver_stochastic_dispatch": True})
    s = driven["stochastic"]
    assert s["mode"] == "drive"
    assert s["status"] == "optimal", s
    assert s["drives_dispatch"] is True
    assert driven["forecast"][0]["battery_kw"] == pytest.approx(
        s["stage1_battery_kw"], abs=1e-3
    )


def test_switch_is_declared_off_and_bridged_to_cfg():
    from _ha_stubs import install_ha_stubs

    install_ha_stubs()
    from custom_components.nimbus_load import const, sensor

    assert const.CONF_SOLVER_STOCHASTIC_DISPATCH == "solver_stochastic_dispatch"
    assert const.DEFAULT_SOLVER_STOCHASTIC_DISPATCH is False
    assert const.CONF_SOLVER_STOCHASTIC_DISPATCH in sensor._SOLVER_SWITCH_ENTITY_KEYS


# -- a joint timeout is one WARNING, never an ERROR (nimbus #1805) ------------


def _mip_inputs() -> dict:
    """The tiered case plus semi-continuous deferrable loads: binaries, as on
    devhub's test controllable loads."""
    from solver.elements import AdequacyLoadConfig

    kw = _tiered_inputs()
    kw["adequacy_loads"] = [
        AdequacyLoadConfig(
            name=f"load{i}",
            max_power_kw=2.0,
            target_kwh=3.0,
            deadline_period=25,
            shortfall_price=1.0,
        )
        for i in range(3)
    ]
    return kw


def test_a_joint_solve_that_hits_its_time_limit_logs_no_error(caplog):
    """devhub, 16 Oct 2026: the joint shadow MIP (4,128 binaries) hit the time
    limit inside the calibrated objective's phase 2 and solver/lp.py logged
    its #773 diagnostic at ERROR. The joint solve is single-phase now, its
    lp.py diagnostics are quiet, and the timeout is reported once by
    run_stochastic_dispatch() as a WARNING carrying its fallback_reason."""
    import logging

    from solver import lp

    # lp.py's calibration cooldown is module-wide: an earlier test that
    # failed a calibration leaves it set, the deterministic solve then
    # skips calibration, and there is no blend weight to hand on. CI hit
    # exactly that ordering ("primary_only" != "blended").
    lp._lex_calibration_failed_until = 0.0
    kw = _mip_inputs()
    kw["solve_options"] = lp.CalibratedOptions()
    det = network.build_plan(**kw)
    assert det.status == "optimal"
    assert det.calibration_weight_used is not None
    dumps_before = set(lp._LP_DUMPED_PHASES)
    cooldown_before = lp._lex_calibration_failed_until
    real = sd.build_stochastic_plan

    def _timed_out(build_kwargs, **kwargs):
        return real(build_kwargs, time_limit_seconds=1e-6, **kwargs)

    with (
        caplog.at_level(logging.DEBUG),
        patch.object(sd, "build_stochastic_plan", side_effect=_timed_out),
    ):
        _, summary = solver_plan.run_stochastic_dispatch(
            {}, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
        )
        # Same reason again: not logged twice.
        solver_plan.run_stochastic_dispatch(
            {},
            now=T0 + timedelta(minutes=5),
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
    assert summary["status"] != "optimal"
    assert summary["fallback_reason"] == f"stochastic_{summary['status']}"
    assert summary["objective_mode"] == "blended"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []
    lp_warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.endswith("solver.lp")
    ]
    assert lp_warnings == []
    ours = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "fallback_reason=" in r.getMessage()
    ]
    assert len(ours) == 1
    # Nothing dumped, and the live solve's calibration is not put on cooldown.
    assert set(lp._LP_DUMPED_PHASES) == dumps_before
    assert lp._lex_calibration_failed_until == cooldown_before


def test_the_joint_solve_never_runs_the_lex_or_calibrated_phases():
    from solver import lp

    assert sd.joint_options(None, 0.01) == (None, "primary")
    opts, mode = sd.joint_options(lp.CalibratedOptions(), 0.01)
    assert mode == "blended"
    assert isinstance(opts, lp.BlendedOptions)
    assert opts.blend_weight == 0.01
    assert sd.joint_options(lp.LexOptions(), None) == (None, "primary_only")
    kw = _mip_inputs()
    kw["solve_options"] = lp.CalibratedOptions()
    with patch.object(
        lp, "_ensure_optimal_value", side_effect=AssertionError("lex phase ran")
    ):
        res = sd.build_stochastic_plan(kw, blend_weight=1e-4)
    assert res.status == "optimal"
    assert res.objective_mode == "blended"


# -- #1807: calibrated p10 / p90 before the envelope ---------------------------


def _calibrated_solar_type():
    """SolarConfig with #1807's p10_kw / p90_kw: the real one once #1809 is
    merged, a test subclass carrying the same two fields before that."""
    if any(f.name == "p10_kw" for f in dataclasses.fields(SolarConfig)):
        return SolarConfig

    @dataclasses.dataclass(frozen=True)
    class _Calibrated(SolarConfig):
        p10_kw: np.ndarray | None = None
        p90_kw: np.ndarray | None = None

    return _Calibrated


def test_calibrated_quantiles_drive_the_scenarios_over_the_envelope():
    """16 Oct 2026: the envelope's lower bound was 0 in every period, so its
    p10 scenario was 'no sun all day'. With #1807's measured p10 / p90 present
    the scenarios use them, and the summary says so."""
    cal = _calibrated_solar_type()
    solar = cal(
        forecast_kw=np.array([2.0, 2.0, 4.0, 0.0]),
        lower_kw=np.array([0.0, 0.0, 0.0, 0.0]),
        upper_kw=np.array([9.0, 9.0, 17.0, 15.0]),
        p10_kw=np.array([2.0, 2.0, 1.4, 0.0]),
        p90_kw=np.array([2.0, 2.0, 5.2, 0.0]),
    )
    sc = sd.solar_scenarios(solar, k=2)
    assert sc is not None and sc.band == "calibrated"
    p10, _p50, p90 = sc.solar
    assert list(p10.forecast_kw) == [2.0, 2.0, 1.4, 0.0]
    assert list(p90.forecast_kw) == [2.0, 2.0, 5.2, 0.0]
    for s in sc.solar:
        assert s.lower_kw is None and s.upper_kw is None
        assert s.p10_kw is None and s.p90_kw is None
    # The night at index 3 has no calibrated spread, so it is not counted.
    assert sc.no_sun_periods == 0


def test_without_quantiles_the_envelope_is_used_and_labelled():
    solar = SolarConfig(
        forecast_kw=np.array([2.0, 2.0, 4.0, 0.0]),
        lower_kw=np.array([1.0, 1.0, 1.0, 0.0]),
        upper_kw=np.array([3.0, 3.0, 9.0, 5.0]),
    )
    sc = sd.solar_scenarios(solar, k=2)
    assert sc is not None and sc.band == "envelope"


# -- the joint solve's shadow prices are the expected marginal (audit #1) -----


def test_joint_duals_are_the_expected_marginal_not_the_p50_rows():
    """p50's own balance-row dual in the joint problem carries the other
    scenarios' marginals through non-anticipativity: measured 2.5x the
    expected marginal at period 0. With a band too narrow to change any
    decision, the stochastic plan's shadow prices must match the
    deterministic plan's."""
    kw = _tiered_inputs()
    sun = kw["solar"].forecast_kw
    kw["solar"] = SolarConfig(
        forecast_kw=sun, lower_kw=sun * 0.999, upper_kw=sun * 1.001
    )
    det = network.build_plan(**kw)
    res = sd.build_stochastic_plan(kw)
    assert res.status == "optimal"
    plan = res.plan
    assert not any("::" in k for k in plan.duals)
    assert not any("::" in k for k in plan.reduced_costs)
    assert set(plan.duals) == set(det.duals)
    for t in (0, 3, 8, 20):
        name = f"power_balance_t{t}"
        assert plan.duals[name] == pytest.approx(det.duals[name], rel=0.02, abs=1e-6), t


def test_expected_marginals_weight_each_scenario_copy():
    d = {"r": 1.0, "p10::r": 2.0, "p90::r": 3.0, "nonanticipativity::p10::x": 9.0}
    out = sd._expected_marginals(d, ["p10", "p90"], 0.4)
    assert out == {"r": pytest.approx(0.4 * 6.0)}


# -- budget, thermal retry, logging, lp.py side effects ----------------------


def test_the_budget_covers_the_whole_call():
    res = sd.build_stochastic_plan(_tiered_inputs(), budget_seconds=0.0)
    assert res.status != "optimal"
    assert res.raw_status == "stochastic_budget_exhausted"


def test_a_joint_solver_error_is_not_retried_with_thermal_relaxed():
    """A joint timeout used to trigger network.py's thermal relax-and-retry,
    spending the budget again and publishing a plan without the thermal
    guarantee. A solver error on the hook path now returns as it is."""
    from solver.elements import ThermalLoadConfig
    from solver.lp import LPResult

    kw = _tiered_inputs()
    n = len(kw["periods"].hours)
    kw["thermal_loads"] = [
        ThermalLoadConfig(
            name="hws",
            max_power_kw=3.0,
            initial_temperature_c=40.0,
            target_temperature_c=55.0,
            earliest_period=0,
            deadline_period=n - 1,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.3,
        )
    ]
    calls = []

    def hook(p, solve_kwargs, *, thermal_hard_deadline, direction_binaries):
        calls.append(thermal_hard_deadline)
        return LPResult(status="error", raw_status="Time limit reached")

    plan = network.build_plan(**kw, lp_solve_hook=hook)
    assert plan.status == "error"
    assert calls == [True]
    assert not plan.thermal_guarantee_relaxed


def test_the_cooldown_does_not_turn_a_blended_joint_solve_into_primary_only(caplog):
    import logging
    import time as _time

    from solver import lp

    kw = _mip_inputs()
    kw["solve_options"] = lp.CalibratedOptions()
    lp._lex_calibration_failed_until = _time.monotonic() + 600
    try:
        with caplog.at_level(
            logging.DEBUG, logger="custom_components.nimbus_load.solver.lp"
        ):
            res = sd.build_stochastic_plan(kw, blend_weight=1e-4)
    finally:
        lp._lex_calibration_failed_until = 0.0
    assert res.status == "optimal"
    assert res.objective_mode == "blended"
    assert "skipping the phased" not in caplog.text


def test_quiet_solves_do_not_touch_the_live_unknown_retry_counters(monkeypatch):
    from solver import lp

    monkeypatch.setattr(lp, "UNKNOWN_RETRIES", {"attempted": 0, "recovered": 0})
    lp._QUIET.on = True
    try:
        lp._note_unknown_retry("interior-point solve")
    finally:
        lp._QUIET.on = False
    assert lp.UNKNOWN_RETRIES == {"attempted": 0, "recovered": 0}


def test_summary_reports_terminal_soc_and_safety_fields():
    _kw, det, stoch = _det_and_stoch()
    s = sd.summary_attribute(
        stoch,
        mode="shadow",
        deterministic_plan=det,
        deterministic_solve_seconds=0.5,
        drives_dispatch=False,
    )
    assert set(s["scenario_terminal_soc_kwh"]) == {"p10", "p50", "p90"}
    assert s["thermal_guarantee_relaxed"] == []
    assert s["direction_exclusivity"] in ("not_needed", "enforced")
