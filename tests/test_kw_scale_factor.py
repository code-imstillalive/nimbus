"""Real tests for solver_writer._kw_scale_factor() -- the fix for a
real, confirmed-live bug found on devhub 2026-08-28: compute_daily_
quality_report() and compute_efficiency_backtest_report() both treat a
configured *_power_sensor's raw historical values as already being kW,
with no check against the entity's own declared unit. A household
pointing solver_solar_power_sensor at a native Watts sensor (confirmed
live: sensor.combined_total_dc_power reports unit_of_measurement="W")
silently fed solar values ~1000x too large into both reports, producing
impossible economics (confirmed live: theoretical_maximum_yield around
-$1280 for one real household-day).

Imports the REAL function directly and monkeypatches solver_writer.
ha_get (not urllib itself) -- same convention as this project's other
pure-function tests that need to fake a single HA API call without a
full HA stub environment.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import _solver_path  # noqa: F401
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
    with patch.object(solver_writer, "ha_get", _mock_ha_get("W")):
        assert _kw_scale_factor("sensor.combined_total_dc_power") == 0.001


def test_kw_sensor_scales_by_1():
    with patch.object(solver_writer, "ha_get", _mock_ha_get("kW")):
        assert _kw_scale_factor("sensor.logger_battery_power") == 1.0


def test_no_unit_defaults_to_1_not_zero():
    # No unit at all -- must default to the historical assumption (kW),
    # never silently zero out real data.
    with patch.object(solver_writer, "ha_get", _mock_ha_get(None)):
        assert _kw_scale_factor("sensor.mystery_power") == 1.0


def test_unrelated_unit_defaults_to_1():
    # Only "W" is specifically corrected -- this deliberately does not
    # guess at every possible power unit HA could report, only the one
    # real, confirmed-live mismatch.
    with patch.object(solver_writer, "ha_get", _mock_ha_get("A")):
        assert _kw_scale_factor("sensor.some_current_sensor") == 1.0


def test_lookup_failure_defaults_to_1_not_crash():
    def _raise(entity_id):
        raise solver_writer.urllib.error.URLError("boom")

    with patch.object(solver_writer, "ha_get", _raise):
        assert _kw_scale_factor("sensor.unreachable") == 1.0


def test_real_world_regression_solar_1000x_bug():
    """The actual reproduction of the live bug: 9.3 kW of real solar,
    misread as 9300 (raw Watts) without the fix. With the fix applied,
    scaling by _kw_scale_factor() recovers the real kW value."""
    raw_watts_value = 9300.0
    with patch.object(solver_writer, "ha_get", _mock_ha_get("W")):
        scale = _kw_scale_factor("sensor.combined_total_dc_power")
    corrected_kw = raw_watts_value * scale
    assert abs(corrected_kw - 9.3) < 1e-9


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
