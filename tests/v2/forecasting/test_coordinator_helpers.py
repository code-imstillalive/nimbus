"""The coordinator's pure helpers and its graceful readings: schedule hours,
humidity and measured power in any unit (#1837).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.const import (
    CONF_HUMIDITY_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    SUBENTRY_TYPE_LOAD,
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


def test_parse_time_to_hour_bare_float_is_already_a_decimal_hour():
    # test_parse_time_to_hour_bare_float_is_already_a_decimal_hour
    # test_parse_time_to_hour_bare_float_is_already_a_decimal_hour
    # The real 2026-08-15 live crash this guards against: a load
    # reconfigured under the old NumberSelector-based flow still has a
    # raw float (e.g. 8.0) sitting in its subentry data, and calling
    # .split(':') on it unconditionally raised
    # "'float' object has no attribute 'split'" on every coordinator
    # refresh cycle.
    assert _parse_time_to_hour(8.0) == 8.0

    # test_parse_time_to_hour_empty_string_is_none
    assert _parse_time_to_hour("") is None

    # test_parse_time_to_hour_garbage_string_returns_none_not_raises
    assert _parse_time_to_hour("not-a-time") is None

    # test_nearest_temp_target_before_first_point_clamps_to_first
    forecast = [(_T0 + timedelta(hours=1), 20.0), (_T0 + timedelta(hours=2), 22.0)]
    assert _nearest_temp(forecast, _T0, fallback=0.0) == 20.0

    # test_nearest_temp_target_after_last_point_clamps_to_last
    forecast = [(_T0, 20.0), (_T0 + timedelta(hours=1), 22.0)]
    assert _nearest_temp(forecast, _T0 + timedelta(hours=5), fallback=0.0) == 22.0

    # test_step_lookup_exact_match_on_a_point_returns_that_point
    forecast = [(_T0, 0.0), (_T0 + timedelta(hours=1), 1.0)]
    assert _step_lookup(forecast, _T0 + timedelta(hours=1), fallback=-1.0) == 1.0

    # test_current_humidity_sensor_state_missing_returns_fallback
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_HUMIDITY_SENSOR: "sensor.missing"})
    # #1540: the feature accessor compares against the own load sensor.
    coord.subentry = MagicMock(data={CONF_LOAD_SENSOR: "sensor.some_load"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = None
    assert coord._current_humidity() == DEFAULT_FALLBACK_HUMIDITY_PCT

    # test_current_humidity_unparseable_state_returns_fallback_not_raises
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_HUMIDITY_SENSOR: "sensor.humidity"})
    # #1540: the feature accessor compares against the own load sensor.
    coord.subentry = MagicMock(data={CONF_LOAD_SENSOR: "sensor.some_load"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(state="unavailable")
    assert coord._current_humidity() == DEFAULT_FALLBACK_HUMIDITY_PCT

    # test_current_measured_power_unparseable_state_returns_zero_not_raises
    # test_current_measured_power_unparseable_state_returns_zero_not_raises
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(state="unknown", attributes={})
    assert coord._current_measured_power("sensor.battery") == 0.0

    # test_current_measured_power_nan_state_returns_zero_not_nan: nimbus issue #353: float("nan") does not raise, unlike a genuinely
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        state="nan", attributes={"unit_of_measurement": "kW"}
    )
    assert coord._current_measured_power("sensor.battery") == 0.0

    # test_current_measured_power_kw_reading_passes_through_unchanged
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        state="13.4", attributes={"unit_of_measurement": "kW"}
    )
    assert coord._current_measured_power("sensor.battery") == 13.4

    # test_current_measured_power_unconvertible_unit_falls_back_to_raw_value
    # A genuinely unknown/unconvertible unit shouldn't crash the whole
    # coordinator cycle -- treat the raw value as-is (with a logged
    # warning, not asserted here) rather than raise.
    coord = _make_bare_coordinator()
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        state="42", attributes={"unit_of_measurement": "furlongs"}
    )
    assert coord._current_measured_power("sensor.weird") == 42.0


# -- _nearest_temp (interpolated) --------------------------------------------


# -- _step_lookup (NOT interpolated -- boolean/curtailment-style signals) ---


# -- _current_humidity (bound method, real graceful-degradation logic) ------


# -- _current_measured_power (bound method, real 2026-08-15 unit-mismatch bug) --


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
