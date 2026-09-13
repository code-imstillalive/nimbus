"""nimbus issue #476/#484/#534: real tests for
NimbusControllableLoadStateSensor -- the sensor that closes the "no
consumer yet" gap #484's own docstring flagged. Same pattern as
test_sensor_health_report.py/test_sensor_topology_config.py: exercises
the REAL class against tests/_ha_stubs.py's stand-in homeassistant.*
modules.

nimbus issue #828: rewritten to construct a real
sensor.NimbusLoadRunStateCoordinator and set its own `.data` directly
(same established technique test_sensor_signal_role_attribute.py already
uses for NimbusForecastSensor's own NimbusCoordinator) instead of
writing through a stub Store and calling async_update() -- the sensor
itself no longer does any I/O of its own, so there's nothing left to
exercise that way. The coordinator's own real Store-read logic
(_async_update_data()) has its own dedicated tests in
test_load_run_state_coordinator.py.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import load_run_state, sensor


def _fake_subentry(
    subentry_id: str, subentry_type: str, title: str, data: dict
) -> MagicMock:
    s = MagicMock()
    s.subentry_id = subentry_id
    s.subentry_type = subentry_type
    s.title = title
    s.data = data
    return s


def _fake_entry(entry_id: str = "test_entry") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator_with(data: dict) -> sensor.NimbusLoadRunStateCoordinator:
    coordinator = sensor.NimbusLoadRunStateCoordinator(MagicMock(), _fake_entry())
    coordinator.data = data
    return coordinator


def test_entity_id_unique_id_and_device_are_per_subentry():
    entry = _fake_entry()
    subentry = _fake_subentry("s1", "controllable_load", "Pool Pump", {})
    s = sensor.NimbusControllableLoadStateSensor(
        MagicMock(), MagicMock(), entry, subentry, "1.0.0"
    )
    # entity_id is derived from the load's own TITLE (a real, findable,
    # HA-valid name), never the raw subentry_id -- see the ULID-shaped
    # test below for the real bug (#579) this asserts against.
    assert s.entity_id == "sensor.nimbus_pool_pump_commanded_state"
    # unique_id keeps the subentry_id -- stable across a title rename,
    # exactly like every other subentry-scoped entity in this file.
    assert s._attr_unique_id == "s1_commanded_state"
    assert s._attr_device_info["identifiers"] == {("nimbus_load", "s1")}
    assert s._attr_device_info["name"] == "Pool Pump"


def test_entity_id_is_valid_even_when_subentry_id_is_a_ulid():
    # nimbus issue #579 (Mark Purcell, first live use, v0.94.185): a real
    # subentry_id is a ULID -- upper-case, e.g. "01M20H3DYJ8DRBGP04KFSDBN6Z"
    # -- and using it raw in entity_id produced an invalid entity_id HA
    # only tolerated with a deprecation warning (removed in 2027.2.0) and
    # an unreadable name. The fix derives entity_id from subentry.title
    # instead; a ULID subentry_id must never appear in entity_id at all.
    entry = _fake_entry()
    ulid = "01M20H3DYJ8DRBGP04KFSDBN6Z"
    subentry = _fake_subentry(ulid, "controllable_load", "Hot Water Heat Pump", {})
    s = sensor.NimbusControllableLoadStateSensor(
        MagicMock(), MagicMock(), entry, subentry, "1.0.0"
    )
    assert s.entity_id == "sensor.nimbus_hot_water_heat_pump_commanded_state"
    assert s.entity_id == s.entity_id.lower()
    assert ulid not in s.entity_id
    assert ulid.lower() not in s.entity_id
    # unique_id is unaffected -- still the real, stable ULID.
    assert s._attr_unique_id == f"{ulid}_commanded_state"


def test_title_slug_handles_punctuation_and_empty_title():
    entry = _fake_entry()
    subentry = _fake_subentry(
        "s2", "controllable_load", "HWS L1 (Heat Pump) - 3.7kW!", {}
    )
    s = sensor.NimbusControllableLoadStateSensor(
        MagicMock(), MagicMock(), entry, subentry, "1.0.0"
    )
    assert s.entity_id == "sensor.nimbus_hws_l1_heat_pump_3_7kw_commanded_state"

    subentry_blank = _fake_subentry("s3", "controllable_load", "", {})
    s_blank = sensor.NimbusControllableLoadStateSensor(
        MagicMock(), MagicMock(), entry, subentry_blank, "1.0.0"
    )
    assert s_blank.entity_id == "sensor.nimbus_load_commanded_state"


def test_native_value_reads_commanded_state_as_on_off_string():
    entry = _fake_entry("entry_x")
    subentry = _fake_subentry(
        "s2",
        "controllable_load",
        "HWS L1",
        {
            "controllable_load_device_entity": "water_heater.hws_l1_sg_ready",
            "controllable_load_min_hold_minutes": 15,
            "controllable_load_max_activations_per_day": 3,
        },
    )
    coordinator = _coordinator_with(
        {
            "s2": load_run_state.LoadRunState(
                commanded_state=True,
                currently_on=True,
                delivered_today_kwh=1.5,
                activations_today=2,
                day_key="2026-09-08",
            )
        }
    )
    s = sensor.NimbusControllableLoadStateSensor(
        coordinator, MagicMock(), entry, subentry, "1.0.0"
    )

    assert s.native_value == "on"
    attrs = s.extra_state_attributes
    assert attrs["commanded_state"] is True
    assert attrs["currently_on"] is True
    assert attrs["delivered_today_kwh"] == 1.5
    assert attrs["activations_today"] == 2
    assert attrs["device_entity"] == "water_heater.hws_l1_sg_ready"
    assert attrs["min_hold_minutes"] == 15
    assert attrs["max_activations_per_day"] == 3


def test_no_persisted_state_reads_off_defaults():
    entry = _fake_entry("entry_y")
    subentry = _fake_subentry("s3", "controllable_load", "Never Sampled", {})
    coordinator = _coordinator_with({})
    s = sensor.NimbusControllableLoadStateSensor(
        coordinator, MagicMock(), entry, subentry, "1.0.0"
    )
    assert s.native_value == "off"
    assert s.extra_state_attributes["commanded_state"] is False


def test_coordinator_not_yet_refreshed_reads_off_defaults():
    # nimbus issue #828: coordinator.data is None before the coordinator's
    # own first refresh completes -- NimbusLoadRunStateCoordinator.get()
    # must still return a safe default, not crash on `None.get(...)`.
    entry = _fake_entry("entry_never_refreshed")
    subentry = _fake_subentry("s9", "controllable_load", "Fresh Load", {})
    coordinator = sensor.NimbusLoadRunStateCoordinator(MagicMock(), entry)
    assert coordinator.data is None
    s = sensor.NimbusControllableLoadStateSensor(
        coordinator, MagicMock(), entry, subentry, "1.0.0"
    )
    assert s.native_value == "off"


def test_exposes_the_plan_forecast_fields():
    # nimbus issue #581: the seven new plan_* fields flow through
    # extra_state_attributes exactly like every other LoadRunState field
    # already does (via the same **state.to_dict() spread) -- no new
    # sensor-side wiring needed beyond what #578 already built.
    entry = _fake_entry("entry_z")
    subentry = _fake_subentry("s4", "controllable_load", "HWS L1", {})
    coordinator = _coordinator_with(
        {
            "s4": load_run_state.LoadRunState(
                plan_forecast=[{"time": "t0", "value": 2.0}],
                plan_delivered_kwh_forecast=[{"time": "t0", "value": 1.0}],
                plan_target_kwh=5.0,
                plan_shortfall_kwh=0.0,
                plan_earliest_period=2,
                plan_deadline_period=12,
            )
        }
    )
    s = sensor.NimbusControllableLoadStateSensor(
        coordinator, MagicMock(), entry, subentry, "1.0.0"
    )
    attrs = s.extra_state_attributes
    assert attrs["plan_forecast"] == [{"time": "t0", "value": 2.0}]
    assert attrs["plan_delivered_kwh_forecast"] == [{"time": "t0", "value": 1.0}]
    assert attrs["plan_target_kwh"] == 5.0
    assert attrs["plan_earliest_period"] == 2
    assert attrs["plan_deadline_period"] == 12
    assert attrs["plan_nominal_kw"] is None


def test_plan_forecast_fields_are_excluded_from_recorder_history():
    # Same nimbus issue #362 reasoning already applied to
    # NimbusHealthReportSensor -- these seven fields refresh every solve
    # cycle and would otherwise write a non-dedupable recorder row every
    # poll for no real long-term-history value.
    expected = {
        "plan_forecast",
        "plan_delivered_kwh_forecast",
        "plan_target_kwh",
        "plan_shortfall_kwh",
        "plan_earliest_period",
        "plan_deadline_period",
        "plan_nominal_kw",
    }
    assert expected <= sensor.NimbusControllableLoadStateSensor._unrecorded_attributes


def test_state_sensor_stays_a_primary_undifferentiated_entity():
    """nimbus issue #774: commanded_state is the other of the device
    page's two primary answers (alongside Status, checked in
    test_sensor_controllable_load_schedule.py) -- unaffected by that
    issue's own reclassification of the eleven supporting-detail sensors
    to diagnostic."""
    assert sensor.NimbusControllableLoadStateSensor._attr_entity_category is None
