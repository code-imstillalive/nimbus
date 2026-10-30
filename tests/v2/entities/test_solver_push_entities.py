"""The solver's push sensors are real entities: unique id, device, classes,
unit and a recorder cap (#55) (#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
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


def test_battery_forecast_has_required_sensor_entity_class_attributes():
    # test_battery_forecast_has_required_sensor_entity_class_attributes
    cls = sensor.NimbusSolverBatteryForecastSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Solver Battery Forecast"
    assert cls._attr_suggested_display_precision == 3
    # Why these are class attributes (#61): the unit comes from the
    # SensorEntity contract, not from an attrs dict. Same reasoning for
    # device_class and state_class.
    #
    # These assertions have never been in doubt. What HAS been re-derived
    # wrong twice is the CONSEQUENCE claimed around them, so the class
    # docstring in sensor.py now carries the verified version and this
    # comment deliberately does not restate it -- see it and #890.
    #
    # The one line worth having here: setting the unit as a class
    # attribute only reaches the recorder for states written BY the
    # entity (HA passes `_unrecorded_attributes` via `state.state_info`).
    # A stale or external writer produces a state with no `state_info`,
    # so nothing is excluded, the 16 KB cap trips, and every attribute is
    # dropped -- which looks exactly like these class attributes not
    # working, and is not.
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
    # storage. "batteries" (nimbus issue #563 item 5) is the same kind
    # of per-cycle-churning series, per-participant.
    assert cls._unrecorded_attributes == frozenset(
        {
            "forecast",
            "batteries",
            "stochastic_paths",
            "price_consolidation",
            "rrp_crosscheck",
        }  # #1797, #1786, #2190
    )

    # test_household_load_total_forecast_has_required_sensor_entity_class_attributes
    cls = sensor.NimbusHouseholdLoadTotalForecastSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Household Load Total Forecast"
    assert cls._attr_suggested_display_precision == 3
    assert cls._attr_native_unit_of_measurement == "kW"
    from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

    assert cls._attr_device_class is SensorDeviceClass.POWER
    assert cls._attr_state_class is SensorStateClass.MEASUREMENT
    # "batteries" only ever appears on the battery-forecast sibling
    # above (this sensor never sets it), but both share the same base
    # class's frozenset -- harmless, unused key here.
    assert cls._unrecorded_attributes == frozenset(
        {
            "forecast",
            "batteries",
            "stochastic_paths",
            "price_consolidation",
        }  # #1797, #1786
    )

    # --- class-attribute properties -------------------------------------------
    # test_quality_report_has_required_sensor_entity_class_attributes
    cls = sensor.NimbusSolverQualityReportSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Solver Quality Report"
    assert cls._attr_native_unit_of_measurement == "%"
    assert cls._attr_suggested_display_precision == 1
    # EPR is a percentage -- no matching HA device_class.
    assert cls._attr_device_class is None
    from homeassistant.components.sensor import SensorStateClass

    assert cls._attr_state_class is SensorStateClass.MEASUREMENT
    # nimbus issue #362 (Mark Purcell): the parent's real payload carries
    # j_ref_hourly/j_ach_hourly/j_star_hourly/hourly_regret (24 ISO-keyed
    # rows x 7 floats each) -- a realistic payload measured at 14,609
    # bytes against the recorder's 16,384-byte cap. These are excluded,
    # matching what the flattened children already exclude; every other
    # (small, scalar) attribute stays recorded. soc_discrepancy_hourly
    # (nimbus issue #681) joins them for the identical reason -- another
    # real per-hour array this payload's own size budget has no margin
    # for.
    assert cls._unrecorded_attributes == frozenset(
        {
            "j_ref_hourly",
            "j_ach_hourly",
            "j_star_hourly",
            "hourly_regret",
            "soc_discrepancy_hourly",
        }
    )

    # test_efficiency_backtest_has_required_sensor_entity_class_attributes
    cls = sensor.NimbusEfficiencyBacktestSensor
    assert cls._attr_has_entity_name is True
    assert cls._attr_name == "Efficiency Backtest"
    # nimbus issue #1232: this asserted `== "%"`. The native state is
    # `spread_dollars`, a CURRENCY amount -- the old `"%"` came from a
    # docstring that wrongly described the state as the configured
    # efficiency, and with state_class MEASUREMENT it had HA recording
    # long-term statistics in percent for a currency value.
    #
    # Explicitly None rather than absent: dropping the attribute made it
    # inherit `_NimbusSolverPushSensor`'s own UnitOfPower.KILO_WATT, so a
    # reader of the class attribute got "kW" for a currency sensor. Pinned
    # here because a property override does not prevent that fallthrough.
    assert cls._attr_native_unit_of_measurement is None
    assert cls._attr_native_unit_of_measurement != "%"
    from homeassistant.const import UnitOfPower

    assert cls._attr_native_unit_of_measurement != UnitOfPower.KILO_WATT
    # The real unit comes from the install's own currency, never hardcoded.
    assert isinstance(cls.native_unit_of_measurement, property)
    assert cls._attr_suggested_display_precision == 2
    assert cls._attr_device_class is None
    from homeassistant.components.sensor import SensorStateClass
    from homeassistant.const import EntityCategory

    assert cls._attr_state_class is SensorStateClass.MEASUREMENT
    # Retrospective validation, not a primary user-facing signal.
    assert cls._attr_entity_category is EntityCategory.DIAGNOSTIC
    assert cls._unrecorded_attributes == frozenset()

    # test_counterfactual_soc_has_required_sensor_entity_class_attributes
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


# --- __init__ preserves entity identity -----------------------------------


# --- update_from_solver ---------------------------------------------------


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
    solver_writer._ENTITY_REAL_IDS.clear()
    solver_writer.NATIVE.hass = None


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


# --- _resolve_hub_device_id (nimbus issue #335 follow-up, 2026-09-03) ------
