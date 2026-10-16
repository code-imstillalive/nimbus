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
from datetime import UTC, datetime
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
