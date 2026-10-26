"""The Solver's push sensors: what each publishes, when it goes stale, and
what survives a restart (#1837).

Every class is driven through the same life: created, added, pushed a plan,
left without one past five minutes, pushed again, restarted, removed. The
per-class facts (entity id, unit, device, restore) are a table, so a new
sensor joins by adding a row. Covers what lived in #85, #289/#292, #335,
#343, the Repairs state-class report (31 Aug 2026) and the push-sensor files.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs
from _module_time import ModuleTime

install_ha_stubs()

sm = importlib.import_module("custom_components.nimbus_load.sensor")

ENTRY = SimpleNamespace(entry_id="hub1")

# class name: (entity id, unit, restored across a restart, device suffix)
PUSH = {
    "NimbusSolverBatteryForecastSensor": (
        "sensor.nimbus_solver_battery_forecast",
        "kW",
        False,
        "",
    ),
    "NimbusHouseholdLoadTotalForecastSensor": (
        "sensor.nimbus_household_load_total_forecast",
        "kW",
        False,
        "",
    ),
    "NimbusDispatchDryRunSensor": (
        "sensor.nimbus_solver_dispatch_dry_run",
        "kW",
        False,
        "",
    ),
    "NimbusOfferCurveSensor": ("sensor.nimbus_offer_curve", "kW", False, ""),
    "NimbusMirrorTemperatureForecastSensor": (
        "sensor.nimbus_mirror_temperature_forecast",
        "°C",
        False,
        "",
    ),
    "NimbusMirrorHumidityForecastSensor": (
        "sensor.nimbus_mirror_humidity_forecast",
        "%",
        False,
        "",
    ),
    "NimbusSolverQualityReportSensor": (
        "sensor.nimbus_solver_quality_report",
        "%",
        True,
        "_quality",
    ),
    "NimbusFlexSignalsSensor": ("sensor.nimbus_flex_signals", "kW", False, "_flex"),
    "NimbusFlexReportSensor": ("sensor.nimbus_flex_report", "kW", True, "_flex"),
    "NimbusFlexTelemetrySensor": ("sensor.nimbus_flex_telemetry", None, False, "_flex"),
    "NimbusEfficiencyBacktestSensor": (
        "sensor.nimbus_efficiency_backtest",
        None,
        True,
        "_backtest",
    ),
    "NimbusCounterfactualSocSensor": (
        "sensor.nimbus_counterfactual_soc",
        "%",
        True,
        "_counterfactual",
    ),
}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(sm, "time", ModuleTime(c))
    return c


def _make(name, *, last_state=None):
    cls = getattr(sm, name)
    sensor = cls(ENTRY, "0.94.485")
    writes = []
    sensor.hass = SimpleNamespace(config=SimpleNamespace(currency="AUD"))
    sensor.async_write_ha_state = lambda: writes.append(sensor.native_value)

    async def last():
        return last_state

    sensor.async_get_last_state = last
    return sensor, writes


def test_a_pushed_plan_is_published_and_goes_stale_after_five_minutes(clock, caplog):
    for name in sorted(PUSH):
        caplog.clear()
        sensor, writes = _make(name)
        assert (
            sensor.available and sensor.native_value is None
        )  # nothing yet is not "stale"
        sensor.update_from_solver(1.5, {"forecast": [1], "k": 2})
        assert writes == [1.5]
        assert sensor.extra_state_attributes == {
            "forecast": [1],
            "k": 2,
            "nimbus_version": "0.94.485",
        }
        with caplog.at_level(logging.INFO, logger=sm._LOGGER.name):
            clock.now += sensor._STALE_AFTER_SECONDS - 1
            sensor._async_recheck_availability(None)
            assert sensor.available and len(writes) == 1  # no change, no write
            clock.now += 1
            sensor._async_recheck_availability(None)
            sensor._async_recheck_availability(None)
            assert not sensor.available and len(writes) == 2
            sensor.update_from_solver(2.0, {"nimbus_version": "solver's own"})
            sensor._async_recheck_availability(None)
        assert sensor.available and writes[-1] == 2.0
        assert sensor.extra_state_attributes == {"nimbus_version": "solver's own"}
        messages = [(r.levelno, r.getMessage()) for r in caplog.records]
        assert (
            sum(
                lvl == logging.WARNING and "not received a fresh Solver plan" in m
                for lvl, m in messages
            )
            == 1
        )
        assert (
            sum(lvl == logging.INFO and "available again" in m for lvl, m in messages)
            == 1
        )


def test_only_the_daily_reports_restore_their_last_value_after_a_restart(monkeypatch):
    for name in sorted(PUSH):
        with monkeypatch.context() as mp:
            _entity_id, _unit, restore, _device = PUSH[name]
            ticks = []
            mp.setattr(
                sm,
                "async_track_time_interval",
                lambda hass, cb, every: ticks.append(every) or (lambda: None),  # noqa: B023
            )
            last = SimpleNamespace(
                state="42.5",
                attributes={
                    "unit_of_measurement": "%",
                    "friendly_name": "x",
                    "history": {"d": 1},
                },
            )
            sensor, _ = _make(name, last_state=last)
            asyncio.run(sensor.async_added_to_hass())
            if restore:
                assert sensor.native_value == 42.5
                assert sensor._attrs == {"history": {"d": 1}}
            else:
                assert sensor.native_value is None
            assert ticks == [sm.timedelta(seconds=sensor._STALE_AFTER_SECONDS / 5)]
            assert len(sensor._on_remove_callbacks) == 1


def test_a_restore_with_nothing_usable_leaves_the_sensor_empty(monkeypatch):
    for state in [
        None,
        SimpleNamespace(state="unavailable", attributes={}),
        SimpleNamespace(state="n/a", attributes={}),
    ]:
        with monkeypatch.context() as mp:
            mp.setattr(sm, "async_track_time_interval", lambda *a: lambda: None)
            sensor, _ = _make("NimbusSolverQualityReportSensor", last_state=state)
            asyncio.run(sensor.async_added_to_hass())
            assert sensor.native_value is None and sensor._attrs == {}


def test_a_failing_restore_never_blocks_setup(monkeypatch):
    monkeypatch.setattr(sm, "async_track_time_interval", lambda *a: lambda: None)
    sensor, _ = _make("NimbusFlexReportSensor")

    async def broken():
        raise RuntimeError("store unreadable")

    sensor.async_get_last_state = broken
    asyncio.run(sensor.async_added_to_hass())
    assert sensor.native_value is None


def test_a_parent_sensor_hands_each_push_to_its_flattened_children(monkeypatch):
    for name, dispatch in [
        ("NimbusSolverQualityReportSensor", "dispatch_to_flattened_quality"),
        ("NimbusFlexSignalsSensor", "dispatch_to_flattened_flex"),
        ("NimbusFlexReportSensor", "dispatch_to_flattened_flex_report"),
        ("NimbusEfficiencyBacktestSensor", "dispatch_to_flattened_backtest"),
        ("NimbusCounterfactualSocSensor", "dispatch_to_flattened_counterfactual"),
        ("NimbusSolverBatteryForecastSensor", "dispatch_to_flattened"),
    ]:
        with monkeypatch.context() as mp:
            seen = []
            mp.setattr(
                sm.sensor_flattened,
                dispatch,
                lambda children, attrs: seen.append((children, attrs)),  # noqa: B023
            )
            sensor, _ = _make(name)
            sensor.update_from_solver(1.0, {"a": 1})
            assert seen == []  # no children yet: nothing to hand on
            sensor._flattened_entities = ["child"]
            sensor.update_from_solver(1.0, {"a": 1})
            assert seen == [(["child"], {"a": 1})]


def test_the_reports_keep_their_oversized_or_churning_fields_out_of_the_recorder():
    """The recorder drops a state's whole attribute dict past 16 KB, and a
    field recomputed every poll writes a fresh row every poll (#362)."""
    assert sm.NimbusSolverQualityReportSensor._unrecorded_attributes == frozenset(
        {
            "j_ref_hourly",
            "j_ach_hourly",
            "j_star_hourly",
            "hourly_regret",
            "soc_discrepancy_hourly",
        }
    )
    assert sm.NimbusHealthReportSensor._unrecorded_attributes == frozenset(
        {"recent_errors", "recent_warnings", "subentry_status", "generated_at"}
    )
