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

import pytest

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

# nimbus issue #591 (third ask): NimbusControllableLoadCostAvoidedTodaySensor
# joins the same per-subentry device but is checked separately below (it
# needs a configured hass.states.get("sensor.nimbus_solver_battery_forecast")
# return, unlike the plain MagicMock() every test above already uses) --
# not folded into _SCHEDULE_SENSOR_CLASSES so the entity_id/device-sharing
# tests above stay unaffected.
_COST_AVOIDED_SENSOR_CLASS = sensor.NimbusControllableLoadCostAvoidedTodaySensor


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
    # nimbus issue #639: the tank must have genuinely reached its own
    # setpoint (the "temperature" attribute here, done_when unset) for
    # "done" wording -- not merely have reached today's kWh target.
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(
        state="eco",
        attributes={"current_temperature": 60.3, "temperature": 60.0},
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


def test_status_sensor_says_target_met_not_done_when_tank_is_below_its_own_line():
    # nimbus issue #639 (Mark Purcell, live verification): the real bug
    # -- a load released on its kWh target showed "done (tank 52 °C)"
    # right beside a device-page "done at 60 °C" line. No "temperature"
    # setpoint attribute and no done_when configured here means the
    # sensor genuinely can't tell whether the tank's own done condition
    # fired, so it must not claim "done".
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(
        state="eco", attributes={"current_temperature": 52.0}
    )
    entry = _fake_entry("entry_target_met")
    subentry = _fake_subentry(
        "s3b",
        "Hot Water Heat Pump",
        {
            "controllable_load_kind": "deferrable",
            "deferrable_done_entity": "water_heater.hws_l1",
        },
    )
    _write_state(
        hass,
        "entry_target_met",
        "s3b",
        load_run_state.LoadRunState(
            plan_forecast=[],
            plan_target_kwh=2.0,
            commanded_state=False,
            delivered_today_kwh=3.54,
            day_key="2026-09-09",
        ),
    )
    s = sensor.NimbusControllableLoadStatusSensor(hass, entry, subentry, "1.0.0")
    asyncio.run(s.async_update())
    assert s.native_value == "target met (3.5 of 2.0 kWh, tank 52 °C)"


def test_status_sensor_uses_an_explicit_done_when_over_the_setpoint_fallback():
    # A configured done_when (">= 60") is evaluated directly against the
    # live tank reading, same as solver_writer.py's own
    # _evaluate_done_condition() -- the setpoint ("temperature")
    # attribute is only ever a fallback for an unset done_when.
    hass = MagicMock()
    hass.states.get.return_value = SimpleNamespace(
        state="eco", attributes={"current_temperature": 61.0}
    )
    entry = _fake_entry("entry_done_when")
    subentry = _fake_subentry(
        "s3c",
        "Hot Water Heat Pump",
        {
            "controllable_load_kind": "deferrable",
            "deferrable_done_entity": "water_heater.hws_l1",
            "deferrable_done_when": ">= 60",
        },
    )
    _write_state(
        hass,
        "entry_done_when",
        "s3c",
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
    assert s.native_value == "done (tank 61 °C)"


def test_never_configured_load_reads_a_safe_default_view():
    hass = MagicMock()
    entry = _fake_entry("entry_fresh")
    subentry = _fake_subentry(
        "s4", "Never Sampled", {"controllable_load_kind": "deferrable"}
    )
    s = sensor.NimbusControllableLoadStatusSensor(hass, entry, subentry, "1.0.0")
    asyncio.run(s.async_update())
    assert s.native_value == "outside window"


def test_cost_avoided_today_sensor_shares_entity_id_pattern_and_device():
    # nimbus issue #591 (third ask): same per-subentry wiring conventions
    # as the original seven -- own test, not folded into
    # _SCHEDULE_SENSOR_CLASSES above, since this class additionally needs
    # a configured battery-forecast state to produce a real value (see
    # the async_update test below).
    entry = _fake_entry()
    subentry = _fake_subentry("s1", "Hot Water Heat Pump", {})
    s = _COST_AVOIDED_SENSOR_CLASS(MagicMock(), entry, subentry, "1.0.0")
    assert s.entity_id == "sensor.nimbus_hot_water_heat_pump_cost_avoided_today"
    assert s._attr_unique_id == "s1_cost_avoided_today"
    assert s._attr_device_info["identifiers"] == {("nimbus_load", "s1")}


def test_cost_avoided_today_sensor_reads_none_without_a_battery_forecast_state():
    # hass.states.get(...) on a bare MagicMock() returns another MagicMock,
    # not None or a real dict -- _today_mean_import_price()'s own isinstance
    # guard must treat that as "not available" rather than crash trying to
    # iterate it.
    hass = MagicMock()
    entry = _fake_entry("entry_noforecast")
    subentry = _fake_subentry(
        "s5", "Hot Water Heat Pump", {"controllable_load_kind": "deferrable"}
    )
    _write_state(
        hass,
        "entry_noforecast",
        "s5",
        load_run_state.LoadRunState(
            commanded_state=False,
            delivered_today_kwh=0.325,
            cost_today=0.046,
            day_key="2026-09-09",
        ),
    )
    s = _COST_AVOIDED_SENSOR_CLASS(hass, entry, subentry, "1.0.0")
    asyncio.run(s.async_update())
    assert s.native_value is None


def test_cost_avoided_today_sensor_reads_the_real_forecast_mean_import_price():
    # nimbus issue #591: end-to-end through async_update() -- a real
    # sensor.nimbus_solver_battery_forecast state with today's own
    # import_price series, mean = (0.10 + 0.30) / 2 = 0.20 $/kWh.
    # delivered_today_kwh=0.325 at cost_today=0.046 -> 0.325*0.20-0.046
    # = $0.019 avoided.
    now = datetime.now(UTC)
    today_iso_1 = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_iso_2 = now.replace(hour=1, minute=0, second=0, microsecond=0).isoformat()

    def _states_get(entity_id):
        if entity_id == "sensor.nimbus_solver_battery_forecast":
            return SimpleNamespace(
                attributes={
                    "forecast": [
                        {"time": today_iso_1, "import_price": 0.10},
                        {"time": today_iso_2, "import_price": 0.30},
                    ]
                }
            )
        return None

    hass = MagicMock()
    hass.states.get.side_effect = _states_get
    entry = _fake_entry("entry_avoided")
    subentry = _fake_subentry(
        "s6", "Hot Water Heat Pump", {"controllable_load_kind": "deferrable"}
    )
    _write_state(
        hass,
        "entry_avoided",
        "s6",
        load_run_state.LoadRunState(
            commanded_state=False,
            delivered_today_kwh=0.325,
            cost_today=0.046,
            day_key="2026-09-09",
        ),
    )
    s = _COST_AVOIDED_SENSOR_CLASS(hass, entry, subentry, "1.0.0")
    asyncio.run(s.async_update())
    assert s.native_value == pytest.approx(0.019)
