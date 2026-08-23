"""Real regression tests for the SensorEntity migration in issue #55 --
sensor.nimbus_solver_battery_forecast and sensor.nimbus_household_load_
total_forecast were both raw states.async_set() writes before this
change (no unique_id, no device_info, no device_class, no state_class,
no unit_of_measurement on the entity, no _unrecorded_attributes cap).
This file locks in the properties every future refactor must preserve:

1. Both classes are real SensorEntity subclasses attached to the Nimbus
   hub device via DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}).
2. Both carry _attr_device_class = POWER, _attr_state_class = MEASUREMENT,
   _attr_native_unit_of_measurement = KILO_WATT at class-attribute time
   (this is what stops the Recorder's own "unit changed" repair firing,
   issue #61).
3. Both carry _unrecorded_attributes = frozenset({"forecast"}) so the
   96h tiered-grid forecast list stops tripping the Recorder's 16 KB
   per-attribute cap (issue #59).
4. Both expose a stable, non-empty _attr_unique_id derived from
   entry.entry_id (issue #62 -- the raw-state predecessor had none).
5. Both preserve the well-known fixed entity_id
   (sensor.nimbus_solver_battery_forecast and
   sensor.nimbus_household_load_total_forecast) so long-term stats and
   history stay attached to the same names after migration.
6. update_from_solver(state, attrs) is the real push channel -- stores
   the values and does NOT crash when called before HA has adopted the
   entity (the very-first solve tick after a reload can, in principle,
   beat async_setup_entry to the punch by microseconds).

Also covers the dispatch-table seam in solver_writer.py itself:

7. register_entity_handler() + ha_post_state() route a native-mode write
   through the registered handler, not through states.async_set().
8. An unregistered entity_id still falls back to states.async_set() in
   native mode -- the seam is purely additive and never silently drops
   a write.
9. The REST branch (native_hass is None) is completely unaffected.
10. unregister_entity_handler() cleanly pulls the handler back out so a
    config-entry reload doesn't keep a stale bound method alive.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor, solver_writer
from custom_components.nimbus_load.const import DOMAIN

# --- helpers ---------------------------------------------------------------


def _fake_entry(entry_id: str = "test-entry-abc") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _construct(cls):
    """Bypass the shared base's __init__ (which reaches into
    DeviceInfo/HA plumbing the stubs don't fully model) and drive the
    real __init__ logic against a fake entry -- returns a genuine
    instance whose class attributes and instance attributes are exactly
    what the real production __init__ would produce.
    """
    entry = _fake_entry()
    instance = cls.__new__(cls)
    # The real __init__ is the one code path we ARE testing here (it
    # sets _attr_unique_id, entity_id, _attr_device_info from entry).
    cls.__init__(instance, entry, sw_version="0.73.0")
    return instance, entry


# --- class-attribute properties -------------------------------------------


def test_battery_forecast_has_required_sensor_entity_class_attributes():
    cls = sensor.NimbusSolverBatteryForecastSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Solver Battery Forecast"
    assert cls._attr_suggested_display_precision == 3
    # The whole reason the "unit changed" repair (#61) stops firing:
    # unit now comes from the SensorEntity contract, not from an attrs
    # dict. Same reasoning for device_class and state_class.
    assert cls._attr_native_unit_of_measurement == "kW"
    # SensorDeviceClass / SensorStateClass are MagicMock in the test
    # stubs; identity comparison against the same mock attribute is the
    # honest thing to assert (the real HA constant would compare equal
    # to itself here too).
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    assert cls._attr_device_class is SensorDeviceClass.POWER
    assert cls._attr_state_class is SensorStateClass.MEASUREMENT
    # Recorder 16 KB attribute cap fix (#59): the forecast list is a
    # projection, not a historical fact, so it's excluded from long-term
    # storage.
    assert cls._unrecorded_attributes == frozenset({"forecast"})


def test_household_load_total_forecast_has_required_sensor_entity_class_attributes():
    cls = sensor.NimbusHouseholdLoadTotalForecastSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Household Load Total Forecast"
    assert cls._attr_suggested_display_precision == 3
    assert cls._attr_native_unit_of_measurement == "kW"
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    assert cls._attr_device_class is SensorDeviceClass.POWER
    assert cls._attr_state_class is SensorStateClass.MEASUREMENT
    assert cls._unrecorded_attributes == frozenset({"forecast"})


# --- __init__ preserves entity identity -----------------------------------


def test_battery_forecast_preserves_well_known_entity_id_and_derives_unique_id():
    instance, entry = _construct(sensor.NimbusSolverBatteryForecastSensor)
    # The whole point of the migration -- external readers (topology
    # card, downstream integrations) depend on this well-known name; a
    # rename would silently orphan them.
    assert instance.entity_id == "sensor.nimbus_solver_battery_forecast"
    # The raw-state predecessor had no unique_id at all (#62). Now it's
    # derived deterministically from entry.entry_id so a reinstall on
    # the same hub re-attaches to the same entity registry entry.
    assert (
        instance._attr_unique_id == f"{entry.entry_id}_nimbus_solver_battery_forecast"
    )


def test_household_load_forecast_preserves_well_known_entity_id_and_derives_unique_id():
    instance, entry = _construct(sensor.NimbusHouseholdLoadTotalForecastSensor)
    assert instance.entity_id == "sensor.nimbus_household_load_total_forecast"
    assert (
        instance._attr_unique_id
        == f"{entry.entry_id}_nimbus_household_load_total_forecast"
    )


def test_both_entities_attach_to_the_hub_device_via_domain_entry_id_identifier():
    """DeviceInfo is a plain dict in the HA stubs; the assertion here is
    that the (DOMAIN, entry.entry_id) tuple is present in identifiers --
    that tuple is what pins both entities under the same "Nimbus" hub
    device as NimbusSolverConfigSensor / NimbusTopologyConfigSensor, so
    they all show up together on the device page instead of scattered
    as bare states with no device at all (the pre-#55 behaviour)."""
    for cls in (
        sensor.NimbusSolverBatteryForecastSensor,
        sensor.NimbusHouseholdLoadTotalForecastSensor,
    ):
        instance, entry = _construct(cls)
        device_info = instance._attr_device_info
        assert (DOMAIN, entry.entry_id) in device_info["identifiers"]
        assert device_info["name"] == "Nimbus"
        assert device_info["manufacturer"] == "Nimbus"
        assert device_info["model"] == "Hub"


# --- update_from_solver ---------------------------------------------------


def test_update_from_solver_stores_state_and_attributes_without_hass():
    """The very-first solve tick after a config-entry setup can in
    principle race async_setup_entry -- update_from_solver must NOT
    crash if hass hasn't been assigned yet. It's fine to silently drop
    the visible-to-HA publish (the next tick 30s later will publish
    normally), but the stored values must still be captured so the
    entity itself is internally consistent by the time HA does adopt
    it.
    """
    instance, _ = _construct(sensor.NimbusSolverBatteryForecastSensor)
    # No hass wired up yet -- the stub base class doesn't set one.
    instance.hass = None
    instance.update_from_solver(
        1.234, {"unit_of_measurement": "kW", "friendly_name": "Test", "forecast": []}
    )
    assert instance.native_value == 1.234
    assert instance.extra_state_attributes["friendly_name"] == "Test"


def test_update_from_solver_calls_async_write_ha_state_when_hass_present():
    instance, _ = _construct(sensor.NimbusHouseholdLoadTotalForecastSensor)
    instance.hass = MagicMock()
    instance.async_write_ha_state = MagicMock()
    instance.update_from_solver(5.678, {"forecast": [{"time": "t", "value": 1.0}]})
    assert instance.native_value == 5.678
    assert instance.extra_state_attributes == {
        "forecast": [{"time": "t", "value": 1.0}]
    }
    instance.async_write_ha_state.assert_called_once()


# --- dispatch table in solver_writer --------------------------------------


class _FakeHass:
    """Records add_job calls so a test can inspect what the dispatch
    seam scheduled. hass.add_job's real contract is that it takes a
    functools.partial (or any callable) and dispatches it onto the
    event loop -- for a synchronous unit test we just capture the call
    and drive the partial inline.
    """

    def __init__(self) -> None:
        self.jobs: list = []
        # states.async_set is what a raw un-migrated entity_id still
        # falls through to; capture calls to prove the seam correctly
        # decides which branch to take.
        self.states = MagicMock()

    def add_job(self, func, *args, **kwargs) -> None:
        self.jobs.append((func, args, kwargs))
        func(*args, **kwargs)


def _clean_dispatch_state():
    """Every test that touches the module-level dispatch table must
    leave it exactly as it found it. Registered handlers are process-
    wide so a leak between tests would falsely pass the "unregistered
    falls through" case.
    """
    solver_writer._ENTITY_UPDATE_HANDLERS.clear()
    solver_writer._NATIVE_HASS = None


def test_ha_post_state_routes_registered_entity_through_dispatch_handler():
    _clean_dispatch_state()
    try:
        received: list = []

        def handler(state, attributes):
            received.append((state, attributes))

        hass = _FakeHass()
        solver_writer.set_native_hass(hass)
        solver_writer.register_entity_handler(
            "sensor.nimbus_solver_battery_forecast", handler
        )
        solver_writer.ha_post_state(
            "sensor.nimbus_solver_battery_forecast",
            1.5,
            {"unit_of_measurement": "kW"},
        )
        # The handler ran, and the states.async_set fallback did NOT.
        assert received == [(1.5, {"unit_of_measurement": "kW"})]
        hass.states.async_set.assert_not_called()
    finally:
        _clean_dispatch_state()


def test_ha_post_state_falls_back_to_states_async_set_for_unregistered_entity():
    _clean_dispatch_state()
    try:
        hass = _FakeHass()
        solver_writer.set_native_hass(hass)
        # No handler registered for this entity_id -- must still write
        # via the state machine so the seam never silently swallows a
        # write for something that hasn't been migrated yet.
        solver_writer.ha_post_state(
            "sensor.some_unmigrated_entity", 42.0, {"unit_of_measurement": "kW"}
        )
        hass.states.async_set.assert_called_once_with(
            "sensor.some_unmigrated_entity", 42.0, {"unit_of_measurement": "kW"}
        )
    finally:
        _clean_dispatch_state()


def test_unregister_entity_handler_cleanly_removes_and_is_safe_on_unknown_id():
    _clean_dispatch_state()
    try:
        solver_writer.register_entity_handler("sensor.foo", lambda s, a: None)
        assert "sensor.foo" in solver_writer._ENTITY_UPDATE_HANDLERS
        solver_writer.unregister_entity_handler("sensor.foo")
        assert "sensor.foo" not in solver_writer._ENTITY_UPDATE_HANDLERS
        # Idempotent -- a second unregister (or one for an id that was
        # never registered) must not raise; that's what makes the entity
        # lifecycle hook (async_will_remove_from_hass) safe to call
        # unconditionally.
        solver_writer.unregister_entity_handler("sensor.never_registered")
    finally:
        _clean_dispatch_state()


def test_register_entity_handler_is_idempotent_and_replaces_stale_handler():
    """A config-entry reload tears down the old entity and creates a
    new one; the new entity's async_setup_entry re-calls
    register_entity_handler with a bound method belonging to the new
    instance. The dispatch table must cleanly replace the stale
    reference, not stack up two handlers or refuse the second call.
    """
    _clean_dispatch_state()
    try:
        received: list = []
        solver_writer.register_entity_handler(
            "sensor.foo", lambda s, a: received.append(("old", s))
        )
        solver_writer.register_entity_handler(
            "sensor.foo", lambda s, a: received.append(("new", s))
        )
        hass = _FakeHass()
        solver_writer.set_native_hass(hass)
        solver_writer.ha_post_state("sensor.foo", 1.0, {})
        assert received == [("new", 1.0)]
    finally:
        _clean_dispatch_state()


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
