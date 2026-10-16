"""nimbus #1797: two-stage stochastic dispatch inside the main solve.

Covers the model (`solver/stochastic_dispatch.py`: scenarios, stage 1,
non-anticipativity, the weighted objective, a hand-built hedge), the LP
plumbing it needs (`LPProblem.absorb()`, `build_plan(lp_solve_hook=)`), the
orchestration (`solver_plan.run_stochastic_dispatch()`: shadow cadence, drive,
every fallback) and the real `main()`: with the switch off the plan is
byte-identical to a run without the stochastic solve at all, and with it on
the published plan is the stochastic one.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
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
def _fresh_stochastic_state():
    solver_plan.reset_stochastic_state()
    yield
    solver_plan.reset_stochastic_state()


def _det_and_stoch():
    kw = _tiered_inputs()
    det = network.build_plan(**kw)
    stoch = sd.build_stochastic_plan(kw)
    assert det.status == stoch.status == "optimal"
    return kw, det, stoch


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


def test_drive_falls_back_when_too_slow_then_cools_down():
    kw, det, stoch = _det_and_stoch()
    slow = dataclasses.replace(stoch, solve_seconds=31.0)
    with patch.object(sd, "build_stochastic_plan", return_value=slow) as spy:
        published, summary = solver_plan.run_stochastic_dispatch(
            {"solver_stochastic_dispatch": True},
            now=T0,
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
        assert published is det
        assert summary["fallback_reason"] == "stochastic_too_slow"
        published, summary = solver_plan.run_stochastic_dispatch(
            {"solver_stochastic_dispatch": True},
            now=T0 + timedelta(seconds=15),
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
        assert spy.call_count == 1
        assert published is det
        assert summary["fallback_reason"] == "cooldown_after_slow_solve"


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
