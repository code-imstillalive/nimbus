"""A power sensor's history is scaled by its own declared unit before it is
treated as kW (#1837).
"""

from __future__ import annotations

from unittest.mock import patch

import solver_shared
import solver_writer

_kw_scale_factor = solver_writer._kw_scale_factor


def _mock_ha_get(unit):
    def _fn(entity_id):
        return {
            "entity_id": entity_id,
            "state": "1.0",
            "attributes": {"unit_of_measurement": unit},
        }

    return _fn


def test_watts_sensor_scales_down_by_1000():
    with patch.object(solver_shared, "ha_get", _mock_ha_get("W")):
        assert _kw_scale_factor("sensor.combined_total_dc_power") == 0.001


def test_kw_sensor_scales_by_1():
    with patch.object(solver_shared, "ha_get", _mock_ha_get("kW")):
        assert _kw_scale_factor("sensor.logger_battery_power") == 1.0


def test_no_unit_defaults_to_1_not_zero():
    # No unit at all -- must default to the historical assumption (kW),
    # never silently zero out real data.
    with patch.object(solver_shared, "ha_get", _mock_ha_get(None)):
        assert _kw_scale_factor("sensor.mystery_power") == 1.0


def test_unrelated_unit_defaults_to_1():
    # Only "W" is specifically corrected -- this deliberately does not
    # guess at every possible power unit HA could report, only the one
    # real, confirmed-live mismatch.
    with patch.object(solver_shared, "ha_get", _mock_ha_get("A")):
        assert _kw_scale_factor("sensor.some_current_sensor") == 1.0


def test_lookup_failure_defaults_to_1_not_crash():
    def _raise(entity_id):
        raise solver_writer.urllib.error.URLError("boom")

    with patch.object(solver_shared, "ha_get", _raise):
        assert _kw_scale_factor("sensor.unreachable") == 1.0


def test_real_world_regression_solar_1000x_bug():
    """The actual reproduction of the live bug: 9.3 kW of real solar,
    misread as 9300 (raw Watts) without the fix. With the fix applied,
    scaling by _kw_scale_factor() recovers the real kW value."""
    raw_watts_value = 9300.0
    with patch.object(solver_shared, "ha_get", _mock_ha_get("W")):
        scale = _kw_scale_factor("sensor.combined_total_dc_power")
    corrected_kw = raw_watts_value * scale
    assert abs(corrected_kw - 9.3) < 1e-9
