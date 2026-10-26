"""The hub's own front page: one-word status, the health report behind it,
and how fast a price change reaches a new plan (#1837).

Covers what lived in #294/#295, the health-report and status files.
"""

from __future__ import annotations

import importlib
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sm = importlib.import_module("custom_components.nimbus_load.sensor")
health = importlib.import_module("custom_components.nimbus_load.health")
const = importlib.import_module("custom_components.nimbus_load.const")


def _state(state, **attrs):
    return SimpleNamespace(state=state, attributes=attrs)


def _status(states):
    s = sm.NimbusStatusSensor(SimpleNamespace(entry_id="hub1"), "1")
    s.hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    return s


CONFIGURED = {"sensor.nimbus_solver_config": _state("configured")}


def test_the_status_is_the_first_thing_wrong_or_working_well():
    for states, word, reason in [
        ({}, "Not yet configured", "Solver settings wizard"),
        (
            {"sensor.nimbus_solver_config": _state("unconfigured")},
            "Not yet configured",
            "wizard",
        ),
        (
            {
                **CONFIGURED,
                "sensor.nimbus_health_report": _state(
                    "0", never_trained=[{"title": t} for t in "ABCDE"]
                ),
            },
            "Learning",
            "A, B, C (+2 more)",
        ),
        (
            {**CONFIGURED, "sensor.nimbus_health_report": _state("3")},
            "Needs attention",
            "3 recent error(s)",
        ),
        (
            {
                **CONFIGURED,
                "sensor.nimbus_health_report": _state("x"),
                "sensor.nimbus_solver_battery_forecast": _state(
                    "1", status="infeasible"
                ),
            },
            "Needs attention",
            "'infeasible'",
        ),
        (
            {
                **CONFIGURED,
                "sensor.nimbus_solver_battery_forecast": _state("1", status="optimal"),
            },
            "Working well",
            "No known issues",
        ),
    ]:
        s = _status(states)
        assert s.native_value == word
        assert reason in s.extra_state_attributes["reason"]
        assert s.entity_id == "sensor.nimbus_status"


def test_the_health_report_counts_errors_and_lists_each_forecasters_state():
    health.reset_log_buffer_for_tests()
    health.install_log_buffer_handler()
    logging.getLogger("custom_components.nimbus_load.x").error("broken")
    subs = {
        "a": SimpleNamespace(
            subentry_id="a", subentry_type=const.SUBENTRY_TYPE_LOAD, title="Pool"
        ),
        "b": SimpleNamespace(
            subentry_id="b", subentry_type=const.SUBENTRY_TYPE_SIGNAL, title="Temp"
        ),
        "c": SimpleNamespace(
            subentry_id="c",
            subentry_type=const.SUBENTRY_TYPE_CONTROLLABLE_LOAD,
            title="HW",
        ),
    }
    runtime = {
        "a": SimpleNamespace(
            data={"trained_at": "t", "forecast": [1, 2], "mode": "learned"}
        )
    }
    s = sm.NimbusHealthReportSensor(
        SimpleNamespace(entry_id="hub1", subentries=subs, runtime_data=runtime), "1"
    )
    try:
        assert s.native_value == 1
        attrs = s.extra_state_attributes
    finally:
        health.reset_log_buffer_for_tests()
    assert [e["message"] for e in attrs["recent_errors"]] == ["broken"]
    assert attrs["never_trained"] == [{"subentry_id": "b", "title": "Temp"}]
    status = {st["subentry_id"]: st for st in attrs["subentry_status"]}
    assert set(status) == {"a", "b"} and status["a"]["forecast_point_count"] == 2
    assert status["b"]["mode"] == "unscheduled"
    assert {"recent_errors", "subentry_status"} <= s._unrecorded_attributes


def test_price_response_latency_keeps_the_last_and_a_rolling_spread():
    s = sm.NimbusSolverPriceResponseLatencySensor(SimpleNamespace(entry_id="hub1"), "1")
    writes = []
    s.hass = SimpleNamespace()
    s.async_write_ha_state = lambda: writes.append(s.native_value)
    assert s.native_value is None and s.extra_state_attributes["sample_count"] == 0
    t0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)
    for i in range(1, 11):
        s.record(
            latency_s=float(i) + 0.0004,
            trigger_source="price_change",
            triggering_entity="sensor.p",
            price_change_at=t0,
            solve_at=t0 + timedelta(seconds=i),
            debounce_s=5.0,
        )
    s.record(
        latency_s=None,
        trigger_source="price_change",
        triggering_entity=None,
        price_change_at=None,
        solve_at=None,
        debounce_s=None,
    )
    attrs = s.extra_state_attributes
    assert s.native_value is None and writes[-2] == 10.0
    assert attrs["sample_count"] == 10 and attrs["max_recent"] == pytest.approx(10.0004)
    assert attrs["p50_recent"] == pytest.approx(6.0004) and attrs[
        "p90_recent"
    ] == pytest.approx(10.0004)
    assert (
        attrs["last_price_change_at"] is None
        and attrs["trigger_source"] == "price_change"
    )
    for _ in range(sm.NimbusSolverPriceResponseLatencySensor._ROLLING_WINDOW + 5):
        s.record(
            latency_s=1.0,
            trigger_source="x",
            triggering_entity=None,
            price_change_at=t0,
            solve_at=t0,
            debounce_s=None,
        )
    assert (
        s.extra_state_attributes["sample_count"]
        == sm.NimbusSolverPriceResponseLatencySensor._ROLLING_WINDOW
    )
