"""nimbus issue #590 (Mark Purcell, real household ask reading the #534
heat pump's own device page: "I don't know if it is scheduled, what time
and for how long. how much will it cost, what are the forecasts..."):
real tests for the seven schedule-view sensors added alongside
NimbusControllableLoadStateSensor -- next_start/next_end/planned_
duration/planned_energy/delivered_today/target_today/status. Same
stub-based pattern as test_sensor_controllable_load_state.py.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import load_run_state, sensor

_SCHEDULE_SENSOR_CLASSES = [
    sensor.NimbusControllableLoadNextStartSensor,
    sensor.NimbusControllableLoadNextEndSensor,
    sensor.NimbusControllableLoadPlannedDurationSensor,
    sensor.NimbusControllableLoadPlannedEnergySensor,
    sensor.NimbusControllableLoadDeliveredTodaySensor,
    sensor.NimbusControllableLoadTargetTodaySensor,
    sensor.NimbusControllableLoadStatusSensor,
]


def _fake_subentry(subentry_id: str, title: str, data: dict) -> MagicMock:
    s = MagicMock()
    s.subentry_id = subentry_id
    s.subentry_type = "controllable_load"
    s.title = title
    s.data = data
    return s


def _fake_entry(entry_id: str = "test_entry") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def test_entity_id_unique_id_and_device_are_per_subentry_for_every_sensor():
    entry = _fake_entry()
    subentry = _fake_subentry("s1", "Hot Water Heat Pump", {})
    expected_suffixes = [
        "next_start",
        "next_end",
        "planned_duration",
        "planned_energy",
        "delivered_today",
        "target_today",
        "status",
    ]
    for cls, suffix in zip(_SCHEDULE_SENSOR_CLASSES, expected_suffixes):
        s = cls(MagicMock(), entry, subentry, "1.0.0")
        assert s.entity_id == f"sensor.nimbus_hot_water_heat_pump_{suffix}", (
            cls,
            s.entity_id,
        )
        assert s._attr_unique_id == f"s1_{suffix}"
        assert s._attr_device_info["identifiers"] == {("nimbus_load", "s1")}
        assert s._attr_device_info["name"] == "Hot Water Heat Pump"


def test_all_seven_share_one_device_with_commanded_state_sensor():
    entry = _fake_entry()
    subentry = _fake_subentry("s1", "Hot Water Heat Pump", {})
    state_sensor = sensor.NimbusControllableLoadStateSensor(
        MagicMock(), entry, subentry, "1.0.0"
    )
    for cls in _SCHEDULE_SENSOR_CLASSES:
        s = cls(MagicMock(), entry, subentry, "1.0.0")
        assert (
            s._attr_device_info["identifiers"]
            == (state_sensor._attr_device_info["identifiers"])
        )


def _write_state(
    hass, entry_id: str, subentry_id: str, state: load_run_state.LoadRunState
):
    StubStore = sensor.Store
    StubStore._shared_data.clear()
    store = load_run_state.LoadRunStateStore(
        store=StubStore(hass, 1, f"nimbus_load_{entry_id}_load_run_state")
    )
    asyncio.run(store.async_write(subentry_id, state))


def test_schedule_sensors_read_the_derived_view():
    hass = MagicMock()
    entry = _fake_entry("entry_sched")
    subentry = _fake_subentry(
        "s2",
        "Hot Water Heat Pump",
        {
            "controllable_load_kind": "deferrable",
            "controllable_load_max_activations_per_day": 3,
        },
    )
    # async_update() computes `now` itself (datetime.now(UTC)) -- built
    # relative to the real current time, comfortably in the future, so
    # every period here is genuinely "upcoming" regardless of when this
    # test actually runs (avoids the flakiness a fixed 2026-09-09 grid
    # would have once real wall-clock time passes it).
    base = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=10)
    times = [base + timedelta(minutes=30 * i) for i in range(6)]
    forecast = load_run_state.build_time_value_series(
        times, [0.0, 0.65, 0.65, 0.0, 0.0, 0.0]
    )
    delivered = load_run_state.build_time_value_series(
        times, [0.0, 0.325, 0.65, 0.65, 0.65, 0.65]
    )
    _write_state(
        hass,
        "entry_sched",
        "s2",
        load_run_state.LoadRunState(
            plan_forecast=forecast,
            plan_delivered_kwh_forecast=delivered,
            plan_target_kwh=0.65,
            plan_shortfall_kwh=0.0,
            commanded_state=False,
            delivered_today_kwh=0.03,
            day_key="2026-09-09",
        ),
    )

    results = {}
    for cls in _SCHEDULE_SENSOR_CLASSES:
        s = cls(hass, entry, subentry, "1.0.0")
        asyncio.run(s.async_update())
        results[cls.__name__] = s.native_value

    assert results["NimbusControllableLoadNextStartSensor"] == times[1]
    assert results["NimbusControllableLoadNextEndSensor"] == times[3]
    assert results["NimbusControllableLoadPlannedDurationSensor"] == 1.0
    assert results["NimbusControllableLoadPlannedEnergySensor"] == 0.65
    assert results["NimbusControllableLoadDeliveredTodaySensor"] == 0.03
    assert results["NimbusControllableLoadTargetTodaySensor"] == 0.65
    assert results["NimbusControllableLoadStatusSensor"] == (
        f"scheduled {times[1]:%H:%M}–{times[3]:%H:%M}"
    )


def test_status_sensor_reads_a_real_tank_temperature_on_done():
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(
        state="eco", attributes={"current_temperature": 60.3}
    )
    entry = _fake_entry("entry_done")
    subentry = _fake_subentry(
        "s3",
        "Hot Water Heat Pump",
        {
            "controllable_load_kind": "deferrable",
            "deferrable_done_entity": "water_heater.hws_l1",
        },
    )
    _write_state(
        hass,
        "entry_done",
        "s3",
        load_run_state.LoadRunState(
            plan_forecast=[],
            plan_target_kwh=2.0,
            commanded_state=False,
            delivered_today_kwh=2.0,
            day_key="2026-09-09",
        ),
    )
    s = sensor.NimbusControllableLoadStatusSensor(hass, entry, subentry, "1.0.0")
    asyncio.run(s.async_update())
    assert s.native_value == "done (tank 60 °C)"


def test_never_configured_load_reads_a_safe_default_view():
    hass = MagicMock()
    entry = _fake_entry("entry_fresh")
    subentry = _fake_subentry(
        "s4", "Never Sampled", {"controllable_load_kind": "deferrable"}
    )
    s = sensor.NimbusControllableLoadStatusSensor(hass, entry, subentry, "1.0.0")
    asyncio.run(s.async_update())
    assert s.native_value == "outside window"
