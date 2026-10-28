"""A controllable load's own sensors: what it was told to do, and its
schedule figures, each from one view of its run state (#1837).

Covers what lived in #482, #768, #828, #873, #1067 and the controllable-load
sensor files.
"""

from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sm = importlib.import_module("custom_components.nimbus_load.sensor")
lrs = importlib.import_module("custom_components.nimbus_load.load_run_state")
const = importlib.import_module("custom_components.nimbus_load.const")

NOW = datetime.now(UTC)
VIEW = lrs.ScheduleView(
    next_start=NOW + timedelta(hours=1),
    next_end=NOW + timedelta(hours=2),
    planned_duration_h=1.0,
    planned_energy_kwh=3.6,
    planned_cost=0.72,
    delivered_today_kwh=1.23456,
    cost_today=0.4,
    target_today_kwh=5.0,
    planned_today_kwh=3.6,
    status="scheduled",
    cost_avoided_today=0.15,
)
SCHEDULE = {
    "NimbusControllableLoadNextStartSensor": ("next_start", VIEW.next_start),
    "NimbusControllableLoadNextEndSensor": ("next_end", VIEW.next_end),
    "NimbusControllableLoadPlannedDurationSensor": ("planned_duration", 1.0),
    "NimbusControllableLoadPlannedEnergySensor": ("planned_energy", 3.6),
    "NimbusControllableLoadPlannedTodaySensor": ("planned_today", 3.6),
    "NimbusControllableLoadDeliveredTodaySensor": ("delivered_today", 1.235),
    "NimbusControllableLoadTargetTodaySensor": ("target_today", 5.0),
    "NimbusControllableLoadPlannedCostSensor": ("planned_cost", 0.72),
    "NimbusControllableLoadCostTodaySensor": ("cost_today", 0.4),
    "NimbusControllableLoadCostAvoidedTodaySensor": ("cost_avoided_today", 0.15),
    "NimbusControllableLoadStatusSensor": ("status", "scheduled"),
}


def _hass(states=None):
    return SimpleNamespace(
        states=SimpleNamespace(get=(states or {}).get),
        config=SimpleNamespace(currency="AUD"),
    )


def _sub(**data):
    return SimpleNamespace(
        subentry_id="hw1",
        title="Hot Water",
        data={const.CONF_CONTROLLABLE_LOAD_KIND: "deferrable", **data},
    )


def _coordinator(state=None):
    state = state or lrs.LoadRunState()
    return SimpleNamespace(get=lambda sid: state)


@pytest.fixture
def views(monkeypatch):
    seen = []

    def derive(state, **kw):
        seen.append(kw)
        return VIEW

    monkeypatch.setattr(sm.load_run_state, "derive_schedule_view", derive)
    return seen


def test_each_schedule_sensor_publishes_its_own_figure(views):
    for name in sorted(SCHEDULE):
        suffix, value = SCHEDULE[name]
        s = getattr(sm, name)(
            _coordinator(), _hass(), SimpleNamespace(entry_id="hub1"), _sub(), "1"
        )
        assert (
            s.entity_id == f"sensor.nimbus_hot_water_{suffix}"
            and s._attr_unique_id == f"hw1_{suffix}"
        )
        assert s.native_value == value
        assert s._attr_device_info["model"] == "Controllable Load"
        if "Cost" in name:
            assert s.native_unit_of_measurement == "AUD"


def test_the_schedule_view_is_told_the_kind_the_tank_and_todays_price(views):
    # The clock as the sensor will read it, not at import: a suite running
    # across midnight UTC put the two on different days.
    now = datetime.now(UTC)
    tank = SimpleNamespace(
        state="eco", attributes={"current_temperature": 58.5, "temperature": 60.0}
    )
    plan = SimpleNamespace(
        attributes={
            "forecast": [
                {"time": now.isoformat(), "import_price": 0.2},
                {"time": now.isoformat(), "import_price": 0.4},
                {"time": (now + timedelta(days=2)).isoformat(), "import_price": 9.0},
            ]
        }
    )
    hass = _hass(
        {"water_heater.tank": tank, "sensor.nimbus_solver_battery_forecast": plan}
    )
    sub = _sub(
        **{
            const.CONF_DEFERRABLE_DONE_ENTITY: "water_heater.tank",
            const.CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY: 3,
        }
    )
    status = sm.NimbusControllableLoadStatusSensor(_coordinator(), hass, None, sub, "1")
    assert status.native_value == "scheduled"
    (kw,) = views
    assert kw["load_kind"] == "deferrable" and kw["max_activations_per_day"] == 3
    assert kw["today_mean_import_price"] == pytest.approx(
        0.3
    )  # the far day is not today
    assert kw["tank_current_temperature"] == 58.5
    assert kw["done_condition_met"] is False  # 58.5 is below its own 60 setpoint


def test_todays_mean_import_price_reads_only_todays_plan():
    # test_todays_mean_import_price_reads_only_todays_plan
    for forecast, mean in [
        (None, None),
        ([], None),
        ([{"time": "bad", "import_price": 1}, {"import_price": 2}], None),
        ([{"time": NOW.isoformat(), "import_price": "0.5"}], 0.5),
    ]:
        states = (
            {}
            if forecast is None
            else {
                "sensor.nimbus_solver_battery_forecast": SimpleNamespace(
                    attributes={"forecast": forecast}
                )
            }
        )
        assert sm._today_mean_import_price(_hass(states), NOW) == mean

    # test_the_days_price_is_the_installs_own_day
    # At 09:30 in Brisbane (23:30 UTC the day before), today is Brisbane's 28
    # Oct: its 08:00 and 23:00 periods count, and the 27th's 23:00 doesn't.
    # Read by UTC date, the 27th's periods were averaged in every morning.
    hass = _hass(
        {
            "sensor.nimbus_solver_battery_forecast": SimpleNamespace(
                attributes={
                    "forecast": [
                        {"time": "2026-10-27T13:00:00+00:00", "import_price": 9.0},
                        {"time": "2026-10-27T22:00:00+00:00", "import_price": 0.2},
                        {"time": "2026-10-28T13:00:00+00:00", "import_price": 0.4},
                    ]
                }
            )
        }
    )
    hass.config.time_zone = "Australia/Brisbane"
    morning = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)
    assert sm._today_mean_import_price(hass, morning) == pytest.approx(0.3)

    # test_the_commanded_state_sensor_says_on_or_off_with_the_run_state_and_settings
    state = lrs.LoadRunState(
        commanded_state=True, commanded_since=NOW.timestamp(), currently_on=True
    )
    sub = _sub(
        **{
            const.CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY: "switch.hw",
            const.CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES: 15,
        }
    )
    s = sm.NimbusControllableLoadStateSensor(
        _coordinator(state), _hass(), None, sub, "1"
    )
    assert s.entity_id == "sensor.nimbus_hot_water_commanded_state"
    assert s.native_value == "on"
    attrs = s.extra_state_attributes
    assert attrs["device_entity"] == "switch.hw" and attrs["min_hold_minutes"] == 15
    assert attrs["commanded_state"] is True and "command_divergence_seconds" in attrs
    assert "plan_forecast" in s._unrecorded_attributes
    off = sm.NimbusControllableLoadStateSensor(_coordinator(), _hass(), None, sub, "1")
    assert off.native_value == "off"

    # test_the_temperature_forecast_shows_the_tank_now_and_the_planned_trajectory
    state = lrs.LoadRunState(
        plan_temperature_forecast=[{"time": NOW.isoformat(), "value": 55.0}],
        thermal_rates_source="learned",
        thermal_heating_rate_c_per_kwh=2.0,
    )
    tank = SimpleNamespace(state="heat_pump", attributes={"current_temperature": 52.5})
    sub = _sub(**{const.CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY: "water_heater.tank"})
    s = sm.NimbusControllableLoadTemperatureForecastSensor(
        _coordinator(state), _hass({"water_heater.tank": tank}), None, sub, "1"
    )
    assert s.entity_id == "sensor.nimbus_hot_water_temperature_forecast"
    assert s.native_value == 52.5
    attrs = s.extra_state_attributes
    assert attrs[const.ATTR_FORECAST] == [{"time": NOW.isoformat(), "value": 55.0}]
    assert (attrs["thermal_rates_source"], attrs["heating_rate_c_per_kwh"]) == (
        "learned",
        2.0,
    )
    bare = sm.NimbusControllableLoadTemperatureForecastSensor(
        _coordinator(), _hass(), None, _sub(), "1"
    )
    assert (
        bare.native_value is None
        and bare.extra_state_attributes[const.ATTR_FORECAST] == []
    )


@pytest.mark.parametrize(
    ("current", "taken", "moved"),
    [
        ("sensor.nimbus_hw1_commanded_state", False, True),  # the stale id: renamed
        ("sensor.nimbus_hot_water_commanded_state", False, False),  # already right
        ("sensor.something_custom", False, False),  # the household renamed it: left
        (None, False, False),
        ("sensor.nimbus_hw1_commanded_state", True, False),  # the right id is taken
    ],
)
def test_a_stale_commanded_state_id_is_renamed_only_when_safe(
    monkeypatch, current, taken, moved
):
    updates = []
    registry = SimpleNamespace(
        async_get_entity_id=lambda d, p, uid: (
            current if uid == "hw1_commanded_state" else None
        ),
        async_get=lambda eid: (
            object()
            if taken and eid == "sensor.nimbus_hot_water_commanded_state"
            else None
        ),
        async_update_entity=lambda eid, new_entity_id: updates.append(
            (eid, new_entity_id)
        ),
    )
    monkeypatch.setattr(sm.er, "async_get", lambda hass: registry)
    sm._migrate_stale_commanded_state_entity_id(None, _sub())
    assert updates == (
        [
            (
                "sensor.nimbus_hw1_commanded_state",
                "sensor.nimbus_hot_water_commanded_state",
            )
        ]
        if moved
        else []
    )


def test_every_per_period_series_a_load_publishes_stays_out_of_the_recorder():
    """#625: checked against the run state's own fields, so the next
    series added is caught too, not only the ones known today."""
    import dataclasses

    series = {
        f.name
        for f in dataclasses.fields(lrs.LoadRunState)
        if f.type.startswith("list[")
    }
    assert series, "the run state must publish at least one series"
    assert series <= sm.NimbusControllableLoadStateSensor._unrecorded_attributes
