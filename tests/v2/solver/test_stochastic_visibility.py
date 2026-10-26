"""Seeing the stochastic solve from this install's own run (#1797) (#1837).

Each scenario's SoC and battery path, the in-process snapshot behind the
response-only `nimbus_load.get_stochastic_summary`, and the decision-input
capture and diagnostics preferring what this process posted over a state
another writer may own.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import solver_plan
from solver import network
from solver import stochastic_dispatch as sd
from test_stochastic_orchestration import T0, _tiered_inputs


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(solver_plan, "STOCHASTIC_SHADOW_IN_BACKGROUND", False)
    solver_plan.reset_stochastic_state()
    yield
    solver_plan.reset_stochastic_state()


def _shadow_once():
    kw = _tiered_inputs()
    det = network.build_plan(**kw)
    return solver_plan.run_stochastic_dispatch(
        {}, now=T0, plan=det, build_kwargs=kw, deterministic_seconds=0.5
    )


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


def _hass_with_plan(attrs: dict, last_updated: datetime):
    state = SimpleNamespace(state="1.0", attributes=attrs, last_updated=last_updated)
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda eid: (
                state if eid == "sensor.nimbus_solver_battery_forecast" else None
            )
        )
    )


# -- the envelope chart's data ------------------------------------------------


# -- the tuning dials reach the solve ----------------------------------------


# -- sensor.nimbus_solver_stochastic ------------------------------------------


def _sensor_mod():
    _ha()
    from custom_components.nimbus_load import sensor_stochastic

    return sensor_stochastic


# -- grid money: the figure the cards show ------------------------------------
