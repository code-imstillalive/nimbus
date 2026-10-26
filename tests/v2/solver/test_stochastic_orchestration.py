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


# -- no band: a no-op -----------------------------------------------------------


# -- the LP plumbing -------------------------------------------------------------


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


def test_a_main_plan_that_used_half_the_drive_budget_skips_the_drive(caplog):
    """#2047: drive mode gets the shadow's backstop against its own budget
    (10 s by default, or the configured one). The skipped cycle publishes the
    main plan with a named reason, logs once, and leaves the backoff alone:
    the next fast cycle drives at once."""
    for budget, seconds, runs in [
        (None, 4.9, True),
        (None, 5.0, False),
        (None, 9.2, False),
        (40.0, 19.9, True),
    ]:
        caplog.clear()
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
                continue
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


def _drive(kw, det, seconds):
    return solver_plan.run_stochastic_dispatch(
        {"solver_stochastic_dispatch": True},
        now=T0 + timedelta(seconds=seconds),
        plan=det,
        build_kwargs=kw,
        deterministic_seconds=0.5,
    )


def test_drive_exceptions_back_off_and_log_once(caplog, monkeypatch):
    # test_drive_exceptions_back_off_and_log_once
    _solver_lp._lex_calibration_failed_until = 0.0
    assert solver_plan.wait_for_stochastic_shadow(timeout=60)
    solver_plan.reset_stochastic_state()
    caplog.clear()
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

    # test_a_shadow_job_finishing_after_a_switch_to_drive_is_dropped
    _solver_lp._lex_calibration_failed_until = 0.0
    assert solver_plan.wait_for_stochastic_shadow(timeout=60)
    solver_plan.reset_stochastic_state()
    with monkeypatch.context() as mp:
        import threading

        mp.setattr(solver_plan, "STOCHASTIC_SHADOW_IN_BACKGROUND", True)
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
    # test_a_joint_solve_that_hits_its_time_limit_logs_no_error: devhub, 16 Oct 2026: the joint shadow MIP (4,128 binaries) hit the time
    _solver_lp._lex_calibration_failed_until = 0.0
    assert solver_plan.wait_for_stochastic_shadow(timeout=60)
    solver_plan.reset_stochastic_state()
    caplog.clear()
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

    # test_a_joint_solver_error_is_not_retried_with_thermal_relaxed: A joint timeout used to trigger network.py's thermal relax-and-retry,
    _solver_lp._lex_calibration_failed_until = 0.0
    assert solver_plan.wait_for_stochastic_shadow(timeout=60)
    solver_plan.reset_stochastic_state()
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


# -- the joint solve's shadow prices are the expected marginal (audit #1) -----


# -- budget, thermal retry, logging, lp.py side effects ----------------------
