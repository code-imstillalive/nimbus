"""Real test of resample_generic_price_forecast() (solver_writer.py).

Direct regression coverage for a real bug found live while wiring up
2026-08-25's blended multi-source price forecasting: this function only
ever accepted a `value` key, so pointing a new
solver_import/export_price_sensor_2/_3 field directly at Mark Purcell's
own NEM PD7 sensor (sensor.nem_pd7day_qld1_nem_spot_price_forecast,
already used elsewhere in this file via fetch_aemo_forecast() for its
`calibrated` field) silently produced zero usable points -- every one of
its forecast entries carries `calibrated`/`raw_value`/`spike_credible`,
never a bare `value`. Caught and fixed before it shipped, not after a
live report.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


def _grid_times(n: int, start: datetime) -> list[datetime]:
    from datetime import timedelta

    return [start + timedelta(minutes=15 * i) for i in range(n)]


class TestResampleGenericPriceForecastValueKey:
    def test_existing_value_shaped_forecast_still_works(self):
        # The original, still-primary shape (Amber-style: {time, value}).
        start = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        state = {
            "attributes": {
                "forecast": [
                    {"time": "2026-08-25T12:00:00+00:00", "value": 0.30},
                    {"time": "2026-08-25T12:30:00+00:00", "value": 0.45},
                ]
            }
        }
        with patch.object(solver_writer, "ha_get", return_value=state):
            result = solver_writer.resample_generic_price_forecast(
                "sensor.amber_forecast", _grid_times(3, start)
            )
        assert result == [0.30, 0.30, 0.45]


class TestResampleGenericPriceForecastCalibratedFallback:
    def test_nem_pd7_shaped_forecast_uses_calibrated_not_raw_value(self):
        # Real NEM PD7 shape: no `value` key at all -- `calibrated` and
        # `raw_value` instead. `calibrated` must win; `raw_value` is
        # exactly the "false spike prediction" field NEM PD7 exists to
        # correct (see fetch_aemo_forecast()'s own docstring).
        start = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        state = {
            "attributes": {
                "forecast": [
                    {
                        "time": "2026-08-25T12:00:00+00:00",
                        "raw_value": 8.999,
                        "calibrated": 0.105355,
                        "spike_credible": False,
                    },
                    {
                        "time": "2026-08-25T12:30:00+00:00",
                        "raw_value": 0.12,
                        "calibrated": 0.11,
                        "spike_credible": True,
                    },
                ]
            }
        }
        with patch.object(solver_writer, "ha_get", return_value=state):
            result = solver_writer.resample_generic_price_forecast(
                "sensor.nem_pd7day_qld1_nem_spot_price_forecast", _grid_times(3, start)
            )
        assert result == [0.105355, 0.105355, 0.11]

    def test_value_key_preferred_over_calibrated_when_both_present(self):
        # A hybrid/future sensor carrying both keys must not silently
        # switch behaviour for anything already relying on `value`.
        start = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        state = {
            "attributes": {
                "forecast": [
                    {
                        "time": "2026-08-25T12:00:00+00:00",
                        "value": 0.20,
                        "calibrated": 0.99,
                    },
                ]
            }
        }
        with patch.object(solver_writer, "ha_get", return_value=state):
            result = solver_writer.resample_generic_price_forecast(
                "sensor.hybrid_forecast", _grid_times(1, start)
            )
        assert result == [0.20]

    def test_neither_key_present_drops_the_point_not_crash(self):
        start = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        state = {
            "attributes": {
                "forecast": [
                    {"time": "2026-08-25T12:00:00+00:00", "spike_credible": True},
                ]
            }
        }
        with patch.object(solver_writer, "ha_get", return_value=state):
            result = solver_writer.resample_generic_price_forecast(
                "sensor.malformed", _grid_times(1, start)
            )
        assert result is None
