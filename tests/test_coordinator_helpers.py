"""Real test of coordinator.py's module-level pure helpers plus two of its
graceful-degradation bound methods (_current_humidity/_current_measured_power)
-- the highest-value untested logic in the integration, given real, documented
live bug history in this exact area (2026-08-15: a bare NumberSelector float
schedule value crashing _parse_time_to_hour; a solar sensor reporting W while
battery/grid report kW breaking _current_measured_power's own unit handling).

Imports and exercises the REAL functions/methods (not a reimplementation)
against real-shaped mock objects and, for _current_humidity/
_current_measured_power specifically, a real NimbusCoordinator instance
constructed via __new__() to bypass DataUpdateCoordinator's own heavy
__init__ chain (which needs a real event loop/hass to run against) --
only the specific attributes each method under test actually reads are
set on it, nothing else.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import (
    CONF_HUMIDITY_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
)
from custom_components.nimbus_load.coordinator import (
    DEFAULT_FALLBACK_HUMIDITY_PCT,
    NimbusCoordinator,
    _nearest_temp,
    _parse_time_to_hour,
    _step_lookup,
)

_T0 = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


def _make_bare_coordinator() -> NimbusCoordinator:
    """A NimbusCoordinator instance with __init__ (and the real
    DataUpdateCoordinator chain it calls via super()) never run -- only
    the attributes a specific test actually sets exist on it. Deliberately
    NOT a general-purpose fixture; each test sets exactly what its own
    method under test reads, so a test can't accidentally pass by relying
    on some OTHER test's leftover setup.
    """
    return NimbusCoordinator.__new__(NimbusCoordinator)


# -- _parse_time_to_hour -----------------------------------------------------


def test_parse_time_to_hour_none_is_none():
    assert _parse_time_to_hour(None) is None


def test_parse_time_to_hour_hhmmss_string():
    assert _parse_time_to_hour("12:30:00") == 12.5


def test_parse_time_to_hour_hh_mm_string_no_seconds_field_needed():
    assert _parse_time_to_hour("08:15") == 8.25


def test_parse_time_to_hour_bare_float_is_already_a_decimal_hour():
    # The real 2026-08-15 live crash this guards against: a load
    # reconfigured under the old NumberSelector-based flow still has a
    # raw float (e.g. 8.0) sitting in its subentry data, and calling
    # .split(':') on it unconditionally raised
    # "'float' object has no attribute 'split'" on every coordinator
    # refresh cycle.
    assert _parse_time_to_hour(8.0) == 8.0


def test_parse_time_to_hour_bare_int_is_already_a_decimal_hour():
    assert _parse_time_to_hour(14) == 14.0


def test_parse_time_to_hour_empty_string_is_none():
    assert _parse_time_to_hour("") is None


def test_parse_time_to_hour_garbage_string_returns_none_not_raises():
    assert _parse_time_to_hour("not-a-time") is None


# -- _nearest_temp (interpolated) --------------------------------------------


def test_nearest_temp_empty_forecast_returns_fallback():
    assert _nearest_temp([], _T0, fallback=15.0) == 15.0


def test_nearest_temp_target_before_first_point_clamps_to_first():
    forecast = [(_T0 + timedelta(hours=1), 20.0), (_T0 + timedelta(hours=2), 22.0)]
    assert _nearest_temp(forecast, _T0, fallback=0.0) == 20.0


def test_nearest_temp_target_after_last_point_clamps_to_last():
    forecast = [(_T0, 20.0), (_T0 + timedelta(hours=1), 22.0)]
    assert _nearest_temp(forecast, _T0 + timedelta(hours=5), fallback=0.0) == 22.0


def test_nearest_temp_interpolates_linearly_at_the_midpoint():
    forecast = [(_T0, 20.0), (_T0 + timedelta(hours=2), 24.0)]
    result = _nearest_temp(forecast, _T0 + timedelta(hours=1), fallback=0.0)
    assert result == 22.0  # exactly halfway between 20 and 24


def test_nearest_temp_interpolates_at_a_quarter_point():
    forecast = [(_T0, 20.0), (_T0 + timedelta(hours=4), 28.0)]
    result = _nearest_temp(forecast, _T0 + timedelta(hours=1), fallback=0.0)
    assert result == 22.0  # 1/4 of the way from 20 to 28 is 22


def test_nearest_temp_exact_match_on_a_point_returns_that_point():
    forecast = [
        (_T0, 20.0),
        (_T0 + timedelta(hours=1), 22.0),
        (_T0 + timedelta(hours=2), 24.0),
    ]
    assert _nearest_temp(forecast, _T0 + timedelta(hours=1), fallback=0.0) == 22.0


# -- _step_lookup (NOT interpolated -- boolean/curtailment-style signals) ---


def test_step_lookup_empty_forecast_returns_fallback():
    assert _step_lookup([], _T0, fallback=0.0) == 0.0


def test_step_lookup_holds_flat_between_points_no_blending():
    forecast = [(_T0, 0.0), (_T0 + timedelta(hours=2), 1.0)]
    # Real point: at the 1-hour midpoint, a boolean-derived signal should
    # still read the PREVIOUS point's value exactly (0.0), never a
    # blended 0.5 -- that's the whole reason _step_lookup exists as a
    # separate function from _nearest_temp.
    assert _step_lookup(forecast, _T0 + timedelta(hours=1), fallback=-1.0) == 0.0


def test_step_lookup_target_before_first_point_clamps_to_first():
    forecast = [(_T0 + timedelta(hours=1), 1.0)]
    assert _step_lookup(forecast, _T0, fallback=-1.0) == 1.0


def test_step_lookup_target_after_last_point_holds_the_last_value():
    forecast = [(_T0, 0.0), (_T0 + timedelta(hours=1), 1.0)]
    assert _step_lookup(forecast, _T0 + timedelta(hours=10), fallback=-1.0) == 1.0


def test_step_lookup_exact_match_on_a_point_returns_that_point():
    forecast = [(_T0, 0.0), (_T0 + timedelta(hours=1), 1.0)]
    assert _step_lookup(forecast, _T0 + timedelta(hours=1), fallback=-1.0) == 1.0


# -- _current_humidity (bound method, real graceful-degradation logic) ------


def test_current_humidity_no_sensor_configured_returns_fallback():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={})  # CONF_HUMIDITY_SENSOR unset
    assert coord._current_humidity() == DEFAULT_FALLBACK_HUMIDITY_PCT


def test_current_humidity_sensor_state_missing_returns_fallback():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_HUMIDITY_SENSOR: "sensor.missing"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = None
    assert coord._current_humidity() == DEFAULT_FALLBACK_HUMIDITY_PCT


def test_current_humidity_unparseable_state_returns_fallback_not_raises():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_HUMIDITY_SENSOR: "sensor.humidity"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(state="unavailable")
    assert coord._current_humidity() == DEFAULT_FALLBACK_HUMIDITY_PCT


def test_current_humidity_real_reading_is_parsed_and_returned():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_HUMIDITY_SENSOR: "sensor.humidity"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(state="63.5")
    assert coord._current_humidity() == 63.5


# -- _current_measured_power (bound method, real 2026-08-15 unit-mismatch bug) --


def test_current_measured_power_entity_id_none_returns_zero():
    coord = _make_bare_coordinator()
    assert coord._current_measured_power(None) == 0.0


def test_current_measured_power_state_missing_returns_zero():
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = None
    assert coord._current_measured_power("sensor.battery") == 0.0


def test_current_measured_power_unparseable_state_returns_zero_not_raises():
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(state="unknown", attributes={})
    assert coord._current_measured_power("sensor.battery") == 0.0


def test_current_measured_power_nan_state_returns_zero_not_nan():
    """nimbus issue #353: float("nan") does not raise, unlike a genuinely
    unparseable state -- a template/REST/Modbus sensor without a numeric
    device_class/state_class can publish this. Feeding NaN into a live
    forecast's stale-flat-carry feature would poison every recursive step.
    """
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        state="nan", attributes={"unit_of_measurement": "kW"}
    )
    assert coord._current_measured_power("sensor.battery") == 0.0


def test_current_measured_power_kw_reading_passes_through_unchanged():
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        state="13.4", attributes={"unit_of_measurement": "kW"}
    )
    assert coord._current_measured_power("sensor.battery") == 13.4


def test_current_measured_power_no_unit_at_all_is_treated_as_already_kw():
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(state="5.0", attributes={})
    assert coord._current_measured_power("sensor.battery") == 5.0


def test_current_measured_power_watts_reading_gets_converted_to_kw():
    # The real, live 2026-08-15 finding this guards against: a solar
    # sensor reporting W while battery/grid sensors report kW. 5000W
    # should become 5.0kW, not be silently treated as 5000kW.
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        state="5000", attributes={"unit_of_measurement": "W"}
    )
    assert coord._current_measured_power("sensor.solar") == 5.0


def test_current_measured_power_megawatts_reading_gets_converted_to_kw():
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        state="2.5", attributes={"unit_of_measurement": "MW"}
    )
    assert coord._current_measured_power("sensor.grid") == 2500.0


def test_current_measured_power_unconvertible_unit_falls_back_to_raw_value():
    # A genuinely unknown/unconvertible unit shouldn't crash the whole
    # coordinator cycle -- treat the raw value as-is (with a logged
    # warning, not asserted here) rather than raise.
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        state="42", attributes={"unit_of_measurement": "furlongs"}
    )
    assert coord._current_measured_power("sensor.weird") == 42.0


# -- _seasonal_anchor (nimbus issue #360, finding 7, Mark Purcell) -----------
#
# The one decision in the "bug #6 Whole House eligibility" family that lives
# in coordinator.py, per Mark's own #360 comment: "a coordinator-level test
# that a load subentry whose source sensor is the whole-house meter gets
# seasonal_anchor=True and any other load gets False." No test exercised
# this property directly before now -- see _seasonal_anchor's own docstring
# for the full 2026-08-15/2026-09-02 bug history this property fixes.


def _make_load_coordinator(
    load_sensor: str, whole_house_sensor: str | None
) -> NimbusCoordinator:
    coord = _make_bare_coordinator()
    coord.subentry = MagicMock(
        subentry_type=SUBENTRY_TYPE_LOAD, data={CONF_LOAD_SENSOR: load_sensor}
    )
    options = {}
    if whole_house_sensor is not None:
        options[CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR] = whole_house_sensor
    coord.entry = MagicMock(options=options)
    return coord


def test_seasonal_anchor_true_for_power_signal_subentry_regardless_of_sensor():
    coord = _make_bare_coordinator()
    coord.subentry = MagicMock(
        subentry_type=SUBENTRY_TYPE_SIGNAL, data={CONF_LOAD_SENSOR: "sensor.battery"}
    )
    coord.entry = MagicMock(options={})
    assert coord._seasonal_anchor is True


def test_seasonal_anchor_true_for_load_matching_wizard_configured_whole_house_sensor():
    coord = _make_load_coordinator(
        load_sensor="sensor.cb_total_combined_power_adjusted_kw",
        whole_house_sensor="sensor.cb_total_combined_power_adjusted_kw",
    )
    assert coord._seasonal_anchor is True


def test_seasonal_anchor_false_for_a_load_that_is_not_the_whole_house_meter():
    coord = _make_load_coordinator(
        load_sensor="sensor.pool_pump_power",
        whole_house_sensor="sensor.cb_total_combined_power_adjusted_kw",
    )
    assert coord._seasonal_anchor is False


def test_seasonal_anchor_falls_back_to_legacy_literal_when_wizard_field_unset():
    # 2026-09-02 fix: an install upgrading from an older version that never
    # configured CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR must not
    # silently regress -- the legacy hardcoded sensor.logger_load_power
    # literal is still checked as a fallback.
    coord = _make_load_coordinator(
        load_sensor="sensor.logger_load_power", whole_house_sensor=None
    )
    assert coord._seasonal_anchor is True


def test_seasonal_anchor_false_for_other_load_when_wizard_field_unset():
    coord = _make_load_coordinator(
        load_sensor="sensor.ac_zone_1", whole_house_sensor=None
    )
    assert coord._seasonal_anchor is False
