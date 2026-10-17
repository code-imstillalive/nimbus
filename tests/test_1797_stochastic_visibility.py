"""nimbus #1797, PR B: seeing the stochastic solve.

devhub's canonical plan sensor carries production's plan (a mirrored entity
overwrites the id), so the `stochastic` attribute cannot be read there. These
cover the paths that read the install's OWN solve instead: each scenario's
SoC and battery path, the in-process snapshot behind the response-only
`nimbus_load.get_stochastic_summary`, and the decision-input capture and
diagnostics download preferring what this process posted.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import _solver_path  # noqa: F401
import numpy as np
import pytest
import solver_plan
from solver import network
from solver import stochastic_dispatch as sd
from test_1797_stochastic_dispatch import T0, _tiered_inputs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(solver_plan, "STOCHASTIC_SHADOW_IN_BACKGROUND", False)
    solver_plan.reset_stochastic_state()
    yield
    solver_plan.reset_stochastic_state()


def test_each_scenario_carries_its_whole_soc_and_battery_path():
    kw = _tiered_inputs()
    res = sd.build_stochastic_plan(kw)
    assert res.status == "optimal"
    n = len(kw["periods"].hours)
    assert set(res.scenario_soc_kwh) == {"p10", "p50", "p90"}
    for label in ("p10", "p50", "p90"):
        assert len(res.scenario_soc_kwh[label]) == n
        assert len(res.scenario_battery_kw[label]) == n
        assert res.scenario_terminal_soc_kwh[label] == pytest.approx(
            res.scenario_soc_kwh[label][-1]
        )
    # p50's path is the published plan's.
    plan_net = res.plan.battery_discharge_kw - res.plan.battery_charge_kw
    assert np.allclose(res.scenario_battery_kw["p50"], plan_net, atol=1e-6)
    # Stage 1 is shared, so every scenario's first K battery values agree.
    for t in range(res.k):
        vals = [res.scenario_battery_kw[s][t] for s in ("p10", "p50", "p90")]
        assert max(vals) - min(vals) < 1e-5


def test_detail_is_plain_json_on_the_plan_grid():
    import json

    kw = _tiered_inputs()
    res = sd.build_stochastic_plan(kw)
    detail = sd.detail_attribute(res)
    json.dumps(detail)
    n = len(kw["periods"].hours)
    assert len(detail["period_hours"]) == n
    assert len(detail["period_starts"]) == n
    assert detail["period_starts"][0] == T0.isoformat()
    assert set(detail["scenario_soc_kwh"]) == {"p10", "p50", "p90"}


def test_no_detail_without_an_optimal_solve():
    res = sd.build_stochastic_plan(_tiered_inputs(), budget_seconds=0.0)
    assert sd.detail_attribute(res) == {}


def _shadow_once():
    kw = _tiered_inputs()
    det = network.build_plan(**kw)
    return solver_plan.run_stochastic_dispatch(
        {}, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
    )


def test_the_snapshot_holds_the_summary_and_the_detail_behind_it():
    _, summary = _shadow_once()
    snap = solver_plan.stochastic_snapshot()
    assert snap["mode"] == "shadow"
    assert snap["summary"] == summary
    assert set(snap["detail"]["scenario_soc_kwh"]) == {"p10", "p50", "p90"}
    assert snap["last_run"] == T0.isoformat()
    assert snap["shadow_running"] is False
    # A copy: a caller cannot reach into the live state.
    snap["summary"]["mode"] = "tampered"
    assert solver_plan.stochastic_snapshot()["summary"]["mode"] == "shadow"


def test_a_driven_cycle_records_its_detail():
    kw = _tiered_inputs()
    det = network.build_plan(**kw)
    stoch = dataclasses.replace(sd.build_stochastic_plan(kw), solar_band="calibrated")
    with patch.object(sd, "build_stochastic_plan", return_value=stoch):
        published, _ = solver_plan.run_stochastic_dispatch(
            {"solver_stochastic_dispatch": True},
            now=T0,
            plan=det,
            build_kwargs=kw,
            deterministic_seconds=0.5,
        )
    assert published is not det
    snap = solver_plan.stochastic_snapshot()
    assert snap["summary"]["mode"] == "drive"
    assert snap["detail"]["scenario_battery_kw"]["p50"]


# -- the HA-facing readers ----------------------------------------------------


def _ha():
    from _ha_stubs import install_ha_stubs

    install_ha_stubs()
    from custom_components.nimbus_load import (
        decision_inputs_store,
        diagnostics,
        services,
        solver_shared,
    )

    return decision_inputs_store, diagnostics, services, solver_shared


def test_the_service_returns_the_snapshot():
    _, _, services, _ = _ha()
    with patch.object(
        solver_plan, "stochastic_snapshot", return_value={"mode": "shadow"}
    ):
        from custom_components.nimbus_load import solver_plan as pkg_plan

        with patch.object(pkg_plan, "stochastic_snapshot", return_value={"mode": "x"}):
            out = asyncio.run(services._async_handle_get_stochastic_summary(None, None))
    assert out == {"mode": "x"}


def _hass_with_plan(attrs: dict, last_updated: datetime):
    state = SimpleNamespace(state="1.0", attributes=attrs, last_updated=last_updated)
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda eid: (
                state if eid == "sensor.nimbus_solver_battery_forecast" else None
            )
        )
    )


def test_capture_prefers_this_installs_own_post_over_a_mirrored_state(monkeypatch):
    store, _, _, solver_shared = _ha()
    mirrored = {"forecast": [{"time": "mirror"}]}
    hass = _hass_with_plan(mirrored, datetime(2026, 10, 17, 0, 0, tzinfo=UTC))
    own = {"forecast": [{"time": "own"}]}
    posted_at = datetime(2026, 10, 17, 0, 0, 30, tzinfo=UTC)
    monkeypatch.setattr(solver_shared.NATIVE, "hass", hass)
    monkeypatch.setitem(
        solver_shared._LAST_POSTED,
        "sensor.nimbus_solver_battery_forecast",
        ("1.0", own),
    )
    monkeypatch.setitem(
        solver_shared._LAST_POSTED_AT,
        "sensor.nimbus_solver_battery_forecast",
        posted_at,
    )
    attrs, at = store._own_plan(hass)
    assert attrs == own
    assert at == posted_at


def test_capture_falls_back_to_the_state_before_a_post_or_for_another_hass(
    monkeypatch,
):
    store, _, _, solver_shared = _ha()
    mirrored = {"forecast": [{"time": "mirror"}]}
    when = datetime(2026, 10, 17, 0, 0, tzinfo=UTC)
    hass = _hass_with_plan(mirrored, when)
    monkeypatch.setitem(
        solver_shared._LAST_POSTED,
        "sensor.nimbus_solver_battery_forecast",
        ("1.0", {"forecast": [{"time": "elsewhere"}]}),
    )
    monkeypatch.setitem(
        solver_shared._LAST_POSTED_AT, "sensor.nimbus_solver_battery_forecast", when
    )
    # The process posted into a DIFFERENT hass: not this one's plan.
    monkeypatch.setattr(solver_shared.NATIVE, "hass", object())
    attrs, at = store._own_plan(hass)
    assert attrs == mirrored
    assert at == when


def test_diagnostics_reads_this_installs_own_plan(monkeypatch):
    _, diagnostics, _, solver_shared = _ha()
    hass = _hass_with_plan({"forecast": [{"time": "mirror"}]}, datetime.now(UTC))
    monkeypatch.setattr(solver_shared.NATIVE, "hass", hass)
    monkeypatch.setitem(
        solver_shared._LAST_POSTED,
        "sensor.nimbus_solver_battery_forecast",
        ("2.0", {"forecast": [{"time": "own"}], "stochastic": {"mode": "shadow"}}),
    )
    out = diagnostics._solver_diagnostics(hass)
    assert out["plan_source"] == "this_install"
    assert out["state"] == "2.0"
    assert out["forecast"] == [{"time": "own"}]
    assert out["stochastic"] == {"mode": "shadow"}


def test_decision_inputs_keep_the_band_and_the_stochastic_summary():
    _ha()
    from custom_components.nimbus_load import decision_inputs as di

    assert "solar_p10_kw" in di.ROW_FIELDS
    assert "solar_p90_kw" in di.ROW_FIELDS
    assert "stochastic" in di.META_FIELDS


# -- the envelope chart's data ------------------------------------------------


def test_paths_are_percent_of_the_fleet_on_the_plan_periods():
    kw = _tiered_inputs()
    res = sd.build_stochastic_plan(kw)
    paths = sd.paths_attribute(res)
    n = len(kw["periods"].hours)
    cap = sum(b.capacity_kwh for b in kw["batteries"])
    assert set(paths["soc_pct"]) == {"p10", "p50", "p90"}
    for label, series in paths["soc_pct"].items():
        assert len(series) == n
        assert series[-1] == pytest.approx(
            100.0 * res.scenario_terminal_soc_kwh[label] / cap, abs=0.06
        )
    # p50's path is the published plan's own SoC.
    plan_pct = 100.0 * np.asarray(res.plan.battery_soc_kwh) / cap
    assert np.allclose(paths["soc_pct"]["p50"], plan_pct[-n:], atol=0.06)


def test_no_paths_without_an_optimal_solve():
    res = sd.build_stochastic_plan(_tiered_inputs(), budget_seconds=0.0)
    assert sd.paths_attribute(res) == {}


def test_published_paths_belong_to_the_published_summary():
    _, summary = _shadow_once()
    paths = solver_plan.stochastic_paths_for(summary)
    assert set(paths["soc_pct"]) == {"p10", "p50", "p90"}
    # A different summary object (another solve) never gets these paths.
    assert solver_plan.stochastic_paths_for(dict(summary)) is None
    assert solver_plan.stochastic_paths_for(None) is None


def test_the_paths_are_kept_out_of_the_recorder():
    _ha()
    from custom_components.nimbus_load import sensor

    owners = [
        c
        for c in vars(sensor).values()
        if isinstance(c, type)
        and "batteries" in getattr(c, "_unrecorded_attributes", frozenset())
    ]
    assert owners
    assert all("stochastic_paths" in c._unrecorded_attributes for c in owners)


# -- the tuning dials reach the solve ----------------------------------------


def test_the_dials_reach_the_stochastic_solve():
    kw = _tiered_inputs()
    det = network.build_plan(**kw)
    stoch = dataclasses.replace(sd.build_stochastic_plan(kw), solar_band="calibrated")
    cfg = {
        "solver_stochastic_dispatch": True,
        "solver_stochastic_stage1_minutes": 15,
        "solver_stochastic_tail_weight": 0.2,
        "solver_stochastic_drive_budget_seconds": 7,
    }
    with patch.object(sd, "build_stochastic_plan", return_value=stoch) as spy:
        solver_plan.run_stochastic_dispatch(
            cfg, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
        )
    kwargs = spy.call_args.kwargs
    assert kwargs["stage1_minutes"] == 15
    assert kwargs["weights"] == pytest.approx((0.2, 0.6, 0.2))
    assert kwargs["budget_seconds"] == 7


def test_the_dials_are_clamped_and_default_to_the_old_constants():
    s = solver_plan.stochastic_settings({})
    assert s["weights"] == pytest.approx((0.3, 0.4, 0.3))
    assert s["stage1_minutes"] == 30
    assert s["drive_budget_seconds"] == 10
    assert s["interval_seconds"] == 300
    wild = solver_plan.stochastic_settings(
        {
            "solver_stochastic_tail_weight": 0.9,
            "solver_stochastic_stage1_minutes": 0,
            "solver_stochastic_shadow_interval_minutes": 999,
        }
    )
    assert wild["weights"] == pytest.approx((0.45, 0.1, 0.45))
    assert wild["stage1_minutes"] == 5
    assert wild["interval_seconds"] == 3600


def test_the_check_interval_dial_sets_the_shadow_cadence():
    kw = _tiered_inputs()
    det = network.build_plan(**kw)
    stoch = sd.build_stochastic_plan(kw)
    cfg = {"solver_stochastic_shadow_interval_minutes": 2}
    with patch.object(sd, "build_stochastic_plan", return_value=stoch) as spy:
        for seconds in (0, 119, 120):
            solver_plan.run_stochastic_dispatch(
                cfg,
                now=T0 + timedelta(seconds=seconds),
                plan=det,
                build_kwargs=kw,
                deterministic_seconds=0.5,
            )
    assert spy.call_count == 2


def test_the_summary_reports_the_commit_window_used():
    res = sd.build_stochastic_plan(_tiered_inputs(), stage1_minutes=15)
    det = network.build_plan(**_tiered_inputs())
    s = sd.summary_attribute(
        res,
        mode="shadow",
        deterministic_plan=det,
        deterministic_solve_seconds=0.5,
        drives_dispatch=False,
    )
    assert s["stage1_minutes"] == 15


# -- sensor.nimbus_solver_stochastic ------------------------------------------


def _sensor_mod():
    _ha()
    from custom_components.nimbus_load import sensor_stochastic

    return sensor_stochastic


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (None, "Not running"),
        (
            {
                "status": "optimal",
                "stage1_battery_kw": -5.02,
                "deterministic_period0_battery_kw": -5.0,
            },
            "Agrees with the main plan",
        ),
        (
            {
                "status": "optimal",
                "stage1_battery_kw": -7.2,
                "deterministic_period0_battery_kw": -5.0,
            },
            "Charges 7.2 kW where the main plan charges 5.0 kW",
        ),
        (
            {
                "status": "optimal",
                "stage1_battery_kw": 3.0,
                "deterministic_period0_battery_kw": 1.5,
            },
            "Discharges 3.0 kW where the main plan discharges 1.5 kW",
        ),
        (
            {"status": "skipped", "fallback_reason": "no_solar_band"},
            "Main plan: no solar spread",
        ),
        (
            # Drive refused an optimal plan: the headline leads with why.
            {
                "status": "optimal",
                "fallback_reason": "solar_band_not_calibrated",
                "stage1_battery_kw": -7.2,
                "deterministic_period0_battery_kw": -5.0,
            },
            "Main plan: solar band not calibrated yet",
        ),
        (
            {"status": "error", "fallback_reason": "cooldown_after:stochastic_error"},
            "Main plan: waiting to retry",
        ),
        (
            {"status": "skipped", "fallback_reason": "error:RuntimeError"},
            "Main plan: check failed",
        ),
    ],
)
def test_the_headline(summary, expected):
    assert _sensor_mod().headline(summary) == expected


def test_the_headline_when_the_plans_point_different_ways():
    m = _sensor_mod()
    # devhub, 17 Oct: both charging -- the case a bare difference misread.
    assert (
        m.headline(
            {
                "status": "optimal",
                "stage1_battery_kw": -3.2,
                "deterministic_period0_battery_kw": -40.0,
            }
        )
        == "Charges 3.2 kW where the main plan charges 40.0 kW"
    )
    assert (
        m.headline(
            {
                "status": "optimal",
                "stage1_battery_kw": 0.0,
                "deterministic_period0_battery_kw": 6.0,
            }
        )
        == "Holds where the main plan discharges 6.0 kW"
    )


def test_chart_rows_put_each_solar_day_beside_the_main_plan():
    m = _sensor_mod()
    plan = {
        "forecast": [
            {"time": "t0", "hours": 0.5, "soc_pct": 50.0},
            {"time": "t1", "hours": 0.5, "soc_pct": 52.0},
        ],
        "stochastic_paths": {
            "soc_pct": {"p10": [49.0, 50.0], "p50": [50.0, 52.0], "p90": [51.0, 55.0]}
        },
    }
    rows = m.scenario_rows(plan)
    assert rows == [
        {
            "time": "t0",
            "hours": 0.5,
            "main": 50.0,
            "p10": 49.0,
            "p50": 50.0,
            "p90": 51.0,
        },
        {
            "time": "t1",
            "hours": 0.5,
            "main": 52.0,
            "p10": 50.0,
            "p50": 52.0,
            "p90": 55.0,
        },
    ]
    # Without a stochastic plan, the main plan alone: what is actually driving.
    alone = m.scenario_rows({"forecast": plan["forecast"]})
    assert alone == [
        {"time": "t0", "hours": 0.5, "main": 50.0},
        {"time": "t1", "hours": 0.5, "main": 52.0},
    ]


def test_the_sensor_takes_its_data_from_this_installs_publish():
    m = _sensor_mod()
    entry = SimpleNamespace(entry_id="E", title="Nimbus", options={}, data={})
    sensor = m.NimbusStochasticSensor(entry, "0.0")
    sensor.hass = None  # not added to HA: update_from_parent skips the write
    summary = {
        "status": "optimal",
        "stage1_battery_kw": -7.2,
        "deterministic_period0_battery_kw": -5.0,
    }
    parent = {
        "stochastic": summary,
        "forecast": [{"time": "t0", "hours": 0.5, "soc_pct": 50.0}],
        "stochastic_paths": {"soc_pct": {"p50": [50.0]}},
    }
    sensor.update_from_parent(parent["forecast"][0], parent)
    assert sensor.native_value == "Charges 7.2 kW where the main plan charges 5.0 kW"
    attrs = sensor.extra_state_attributes
    assert attrs["stochastic"] is summary
    assert attrs["forecast"][0]["p50"] == 50.0
    assert {"forecast", "stochastic"} <= set(sensor._unrecorded_attributes)
