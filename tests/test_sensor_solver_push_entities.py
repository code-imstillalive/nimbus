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
from custom_components.nimbus_load.const import DOMAIN

from custom_components.nimbus_load import sensor, solver_writer

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


# ===========================================================================
# Family-A completion (2026-08-29, issue #55 follow-up):
# NimbusSolverQualityReportSensor, NimbusEfficiencyBacktestSensor, and
# NimbusCounterfactualSocSensor are the three remaining raw-REST-fallback
# parent sensors migrated in this round of #55. Same property tests as the
# two forecast sensors above, plus explicit coverage for:
#
# 11. Each new parent carries the correct native unit of measurement (%,
#     with device_class matching the parent's own semantics -- BATTERY for
#     counterfactual_soc, None for the two percentage-only sensors).
# 12. Each new parent's DeviceInfo has a distinct sub-device identifier
#     (DOMAIN, f"{entry_id}_quality" / _backtest / _counterfactual) AND
#     `via_device` pointing at the hub identifier -- this is what makes the
#     sub-device get rendered as a child of the Nimbus hub in the device
#     registry, instead of a stray unrelated device.
# 13. update_from_solver stores the correct native_value (the parent's own
#     canonical scalar: epr / configured_efficiency_percent / real_soc_
#     close_pct) and fans out to every registered flattened child via the
#     matching dispatch_to_flattened_* helper in sensor_flattened.py.
# 14. Each flattened child's own update_from_parent extracts the correct
#     slice of the parent payload and reports it as native_value.
# ===========================================================================


from custom_components.nimbus_load import sensor_flattened

# --- class-attribute properties -------------------------------------------


def test_quality_report_has_required_sensor_entity_class_attributes():
    cls = sensor.NimbusSolverQualityReportSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Solver Quality Report"
    assert cls._attr_native_unit_of_measurement == "%"
    assert cls._attr_suggested_display_precision == 1
    # EPR is a percentage -- no matching HA device_class.
    assert cls._attr_device_class is None
    from homeassistant.components.sensor import SensorStateClass

    assert cls._attr_state_class is SensorStateClass.MEASUREMENT
    # This parent doesn't carry a `forecast` array (all attrs are scalar);
    # explicit empty set beats inheriting the base's forecast-only default.
    assert cls._unrecorded_attributes == frozenset()


def test_efficiency_backtest_has_required_sensor_entity_class_attributes():
    cls = sensor.NimbusEfficiencyBacktestSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Efficiency Backtest"
    assert cls._attr_native_unit_of_measurement == "%"
    assert cls._attr_suggested_display_precision == 1
    assert cls._attr_device_class is None
    from homeassistant.components.sensor import SensorStateClass
    from homeassistant.const import EntityCategory

    assert cls._attr_state_class is SensorStateClass.MEASUREMENT
    # Retrospective validation, not a primary user-facing signal.
    assert cls._attr_entity_category is EntityCategory.DIAGNOSTIC
    assert cls._unrecorded_attributes == frozenset()


def test_counterfactual_soc_has_required_sensor_entity_class_attributes():
    cls = sensor.NimbusCounterfactualSocSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Counterfactual SoC"
    assert cls._attr_native_unit_of_measurement == "%"
    assert cls._attr_suggested_display_precision == 1
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
    from homeassistant.const import EntityCategory

    # State-of-charge percentage IS a battery reading -- HA's BATTERY
    # device_class handles the 0-100 percent contract exactly.
    assert cls._attr_device_class is SensorDeviceClass.BATTERY
    assert cls._attr_state_class is SensorStateClass.MEASUREMENT
    assert cls._attr_entity_category is EntityCategory.DIAGNOSTIC
    assert cls._unrecorded_attributes == frozenset()


# --- __init__ preserves entity identity + sub-device DeviceInfo -----------


def test_quality_report_preserves_well_known_entity_id_and_derives_unique_id():
    instance, entry = _construct(sensor.NimbusSolverQualityReportSensor)
    # External readers (Lovelace cards, downstream automations) depend on
    # this well-known name -- the whole point of the migration is that
    # the entity_id stays the same, only its class/DeviceInfo change.
    assert instance.entity_id == "sensor.nimbus_solver_quality_report"
    assert instance._attr_unique_id == f"{entry.entry_id}_nimbus_solver_quality_report"


def test_efficiency_backtest_preserves_well_known_entity_id_and_derives_unique_id():
    instance, entry = _construct(sensor.NimbusEfficiencyBacktestSensor)
    assert instance.entity_id == "sensor.nimbus_efficiency_backtest"
    assert instance._attr_unique_id == f"{entry.entry_id}_nimbus_efficiency_backtest"


def test_counterfactual_soc_preserves_well_known_entity_id_and_derives_unique_id():
    instance, entry = _construct(sensor.NimbusCounterfactualSocSensor)
    assert instance.entity_id == "sensor.nimbus_counterfactual_soc"
    assert instance._attr_unique_id == f"{entry.entry_id}_nimbus_counterfactual_soc"


def test_new_parents_attach_to_sub_devices_with_via_device_pointing_at_hub():
    """Each Family-A-completion parent lives on its OWN sub-device
    (identifier suffix _quality / _backtest / _counterfactual), NOT on
    the hub directly -- and each sub-device is linked back to the hub
    via `via_device` so HA's device registry renders the parent/child
    relationship natively (frontend shows "Part of Nimbus" on the
    sub-device page and includes it in the hub's device tree).

    Backward compatible with the two forecast sensors above (which stay
    on the hub device via (DOMAIN, entry.entry_id) unchanged) -- the
    hub identifier itself is never rewritten, only referenced from each
    sub-device's via_device.
    """
    cases = [
        (sensor.NimbusSolverQualityReportSensor, "_quality"),
        (sensor.NimbusEfficiencyBacktestSensor, "_backtest"),
        (sensor.NimbusCounterfactualSocSensor, "_counterfactual"),
    ]
    for cls, suffix in cases:
        instance, entry = _construct(cls)
        device_info = instance._attr_device_info
        # Sub-device identifier is (DOMAIN, entry_id + suffix), NOT the
        # bare hub identifier -- otherwise every sub-device would merge
        # back into the hub and defeat the whole point of the layout.
        expected_id = (DOMAIN, f"{entry.entry_id}{suffix}")
        assert expected_id in device_info["identifiers"], (
            f"{cls.__name__}: expected sub-device identifier {expected_id}, "
            f"got {device_info['identifiers']}"
        )
        # And the hub identifier itself must NOT be in this sub-device's
        # identifiers (that's what via_device is for -- if the hub
        # identifier were here too, HA would merge them into one device).
        assert (DOMAIN, entry.entry_id) not in device_info["identifiers"]
        # via_device is the HA-native "child device linked to parent"
        # mechanism -- must point at the hub identifier tuple exactly.
        assert device_info["via_device"] == (DOMAIN, entry.entry_id)
        assert device_info["manufacturer"] == "Nimbus"


# --- update_from_solver + fan-out -----------------------------------------


def test_quality_report_update_from_solver_stores_epr_and_fans_out_to_children():
    """The parent's native_value must be the epr scalar (its canonical
    state) and each flattened child must pick up its own slice from the
    same attribute dict via dispatch_to_flattened_quality().
    """
    instance, entry = _construct(sensor.NimbusSolverQualityReportSensor)
    instance.hass = None  # first-tick race: hass may not be wired yet
    children = sensor_flattened.create_flattened_entities_quality(entry, "0.94.24")
    instance._flattened_entities = children
    # Canned payload matches the real publish_daily_quality_report shape
    # (see solver_writer.py L3696) -- epr as state, the 10 scalar attrs
    # as fan-out targets.
    attrs = {
        "epr": 87.3,
        "theoretical_maximum_yield": 12.345,
        "value_captured": 10.789,
        "uplift_available": 1.556,
        "j_ref": -8.400,
        "j_ach": -7.200,
        "j_star": -10.000,
        "regret_dollars": 2.800,
        "tracking_fidelity": 92.1,
        "tracking_cost": 0.045,
    }
    instance.update_from_solver(87.3, attrs)
    assert instance.native_value == 87.3
    # Every child should have pulled its own slice from `attrs`.
    by_suffix = {c._spec.entity_id_suffix: c for c in children}
    assert by_suffix["epr"].native_value == 87.3
    assert by_suffix["theoretical_maximum_yield"].native_value == 12.345
    assert by_suffix["value_captured"].native_value == 10.789
    assert by_suffix["uplift_available"].native_value == 1.556
    assert by_suffix["j_ref"].native_value == -8.400
    assert by_suffix["j_ach"].native_value == -7.200
    assert by_suffix["j_star"].native_value == -10.000
    assert by_suffix["regret_dollars"].native_value == 2.800
    assert by_suffix["tracking_fidelity"].native_value == 92.1
    assert by_suffix["tracking_cost"].native_value == 0.045


def test_efficiency_backtest_update_from_solver_stores_state_and_fans_out():
    instance, entry = _construct(sensor.NimbusEfficiencyBacktestSensor)
    instance.hass = None
    children = sensor_flattened.create_flattened_entities_backtest(entry, "0.94.24")
    instance._flattened_entities = children
    attrs = {
        "configured_efficiency_percent": 92.0,
        "best_candidate_cost": -6.410,
        "worst_candidate_cost": -3.220,
    }
    instance.update_from_solver(92.0, attrs)
    assert instance.native_value == 92.0
    by_suffix = {c._spec.entity_id_suffix: c for c in children}
    assert by_suffix["configured_efficiency_percent"].native_value == 92.0
    assert by_suffix["best_candidate_cost"].native_value == -6.410
    assert by_suffix["worst_candidate_cost"].native_value == -3.220


def test_counterfactual_soc_update_from_solver_stores_state_and_fans_out():
    instance, entry = _construct(sensor.NimbusCounterfactualSocSensor)
    instance.hass = None
    children = sensor_flattened.create_flattened_entities_counterfactual(
        entry, "0.94.24"
    )
    instance._flattened_entities = children
    attrs = {
        "real_soc_anchor_pct": 45.0,
        "nimbus_only_soc_close_pct": 78.5,
        "real_soc_close_pct": 76.2,
    }
    instance.update_from_solver(76.2, attrs)
    assert instance.native_value == 76.2
    by_suffix = {c._spec.entity_id_suffix: c for c in children}
    assert by_suffix["real_soc_anchor_pct"].native_value == 45.0
    assert by_suffix["nimbus_only_soc_close_pct"].native_value == 78.5
    assert by_suffix["real_soc_close_pct"].native_value == 76.2


def test_update_from_solver_does_not_crash_when_flattened_entities_empty():
    """A very-first solve tick after config-entry setup can in principle
    beat async_setup_entry to the punch by microseconds -- the parent
    class must gracefully skip fan-out when _flattened_entities is still
    the default empty list from __init__.
    """
    for cls in (
        sensor.NimbusSolverQualityReportSensor,
        sensor.NimbusEfficiencyBacktestSensor,
        sensor.NimbusCounterfactualSocSensor,
    ):
        instance, _ = _construct(cls)
        instance.hass = None
        assert instance._flattened_entities == []
        # Must not raise.
        instance.update_from_solver(1.0, {})
        assert instance.native_value == 1.0


# --- flattened children have correct sub-device DeviceInfo ---------------


def test_flattened_children_attach_to_correct_sub_device_via_device_hub():
    """The three factories (create_flattened_entities_quality/_backtest/
    _counterfactual) must each attach their children to the matching
    sub-device identifier, NOT to the hub -- and each child's via_device
    must point at the hub so the frontend renders "Part of Nimbus" on
    the sub-device page.
    """
    entry = _fake_entry()
    cases = [
        (
            sensor_flattened.create_flattened_entities_quality,
            "_quality",
            "Nimbus Quality",
            "nimbus_quality",
        ),
        (
            sensor_flattened.create_flattened_entities_backtest,
            "_backtest",
            "Nimbus Backtest",
            "nimbus_backtest",
        ),
        (
            sensor_flattened.create_flattened_entities_counterfactual,
            "_counterfactual",
            "Nimbus Counterfactual",
            "nimbus_counterfactual",
        ),
    ]
    for factory, suffix, name, entity_id_prefix in cases:
        children = factory(entry, "0.94.24")
        assert children, f"{factory.__name__} produced no children"
        expected_id = (DOMAIN, f"{entry.entry_id}{suffix}")
        for child in children:
            device_info = child._attr_device_info
            assert expected_id in device_info["identifiers"]
            assert (DOMAIN, entry.entry_id) not in device_info["identifiers"]
            assert device_info["via_device"] == (DOMAIN, entry.entry_id)
            assert device_info["name"] == name
            # Entity IDs must be namespaced under the family prefix so bare
            # source_keys (like "epr" or "real_soc_close_pct") never collide
            # across parents.
            assert child.entity_id.startswith(f"sensor.{entity_id_prefix}_")
            assert child._attr_unique_id.startswith(
                f"{entry.entry_id}_{entity_id_prefix}_"
            )
