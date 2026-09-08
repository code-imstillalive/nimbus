"""nimbus issue #476/#484/#534: real tests for
NimbusControllableLoadStateSensor -- the sensor that closes the "no
consumer yet" gap #484's own docstring flagged. Same pattern as
test_sensor_health_report.py/test_sensor_topology_config.py: exercises
the REAL class against tests/_ha_stubs.py's stand-in homeassistant.*
modules, including its own real _StubStore for the async LoadRunStateStore
reads this sensor performs in async_update().
"""

import asyncio
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


def test_entity_id_unique_id_and_device_are_per_subentry():
    entry = _fake_entry()
    subentry = _fake_subentry("s1", "controllable_load", "Pool Pump", {})
    s = sensor.NimbusControllableLoadStateSensor(MagicMock(), entry, subentry, "1.0.0")
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
    s = sensor.NimbusControllableLoadStateSensor(MagicMock(), entry, subentry, "1.0.0")
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
    s = sensor.NimbusControllableLoadStateSensor(MagicMock(), entry, subentry, "1.0.0")
    assert s.entity_id == "sensor.nimbus_hws_l1_heat_pump_3_7kw_commanded_state"

    subentry_blank = _fake_subentry("s3", "controllable_load", "", {})
    s_blank = sensor.NimbusControllableLoadStateSensor(
        MagicMock(), entry, subentry_blank, "1.0.0"
    )
    assert s_blank.entity_id == "sensor.nimbus_load_commanded_state"


def test_async_update_reads_commanded_state_as_on_off_string():
    # sensor.Store, not a fresh `from homeassistant.helpers.storage import
    # Store` here -- in the full suite, an earlier-collected test file can
    # already have imported custom_components.nimbus_load.sensor (caching
    # it in sys.modules with whatever Store class was live at THAT import
    # time), while install_ha_stubs() re-running in THIS file installs a
    # fresh stub module/class into sys.modules -- a plain re-import here
    # would then bind to a DIFFERENT class object (a different, empty
    # _shared_data dict) than the one sensor.py's own async_update()
    # actually uses. sensor.Store is the exact class object sensor.py
    # itself resolved, so writing through it is guaranteed to be visible
    # to the real async_update() call below regardless of import order.
    StubStore = sensor.Store
    StubStore._shared_data.clear()
    hass = MagicMock()
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
    store = load_run_state.LoadRunStateStore(
        store=StubStore(hass, 1, "nimbus_load_entry_x_load_run_state")
    )
    asyncio.run(
        store.async_write(
            "s2",
            load_run_state.LoadRunState(
                commanded_state=True,
                currently_on=True,
                delivered_today_kwh=1.5,
                activations_today=2,
                day_key="2026-09-08",
            ),
        )
    )

    s = sensor.NimbusControllableLoadStateSensor(hass, entry, subentry, "1.0.0")
    asyncio.run(s.async_update())

    assert s.native_value == "on"
    attrs = s.extra_state_attributes
    assert attrs["commanded_state"] is True
    assert attrs["currently_on"] is True
    assert attrs["delivered_today_kwh"] == 1.5
    assert attrs["activations_today"] == 2
    assert attrs["device_entity"] == "water_heater.hws_l1_sg_ready"
    assert attrs["min_hold_minutes"] == 15
    assert attrs["max_activations_per_day"] == 3


def test_async_update_with_no_persisted_state_reads_off_defaults():
    # See the comment in the previous test for why this must be
    # sensor.Store, not a fresh top-level re-import.
    sensor.Store._shared_data.clear()
    hass = MagicMock()
    entry = _fake_entry("entry_y")
    subentry = _fake_subentry("s3", "controllable_load", "Never Sampled", {})
    s = sensor.NimbusControllableLoadStateSensor(hass, entry, subentry, "1.0.0")
    asyncio.run(s.async_update())
    assert s.native_value == "off"
    assert s.extra_state_attributes["commanded_state"] is False
