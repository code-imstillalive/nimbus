"""Tests for publish_weather_forecast_mirrors() (solver_writer.py).

Real regression coverage for a mistake caught and corrected live the
same day this shipped (2026-08-25): a first version hardcoded a
weather.pirateweather/weather.home entity-id preference order directly
in code. Corrected to the project's own standing rule ("NOTHING IN
NIMBUS SHOULD EVER BE HARD CODED - EVERYTHING IS INTEGRATION FIELD
INPUT OR WIZARD") -- the source entity now comes exclusively from
cfg["solver_weather_forecast_sensor"] (CONF_SOLVER_WEATHER_FORECAST_
SENSOR), matching CONF_TEMPERATURE_FORECAST_SENSOR's own established
dual-shape (weather.*/sensor.*) convention.
"""

import unittest
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


class TestNotConfigured(unittest.TestCase):
    def test_blank_field_is_a_clean_noop(self):
        with (
            patch.object(solver_writer, "ha_call_service_with_response") as call,
            patch.object(solver_writer, "ha_get") as get,
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_weather_forecast_mirrors({})
        call.assert_not_called()
        get.assert_not_called()
        post.assert_not_called()


class TestWeatherDomain(unittest.TestCase):
    def test_publishes_both_temp_and_humidity_when_both_present(self):
        cfg = {"solver_weather_forecast_sensor": "weather.pirateweather"}
        response = {
            "weather.pirateweather": {
                "forecast": [
                    {
                        "datetime": "2026-08-25T00:00:00+00:00",
                        "temperature": 20.0,
                        "humidity": 70,
                    },
                    {
                        "datetime": "2026-08-25T01:00:00+00:00",
                        "temperature": 21.0,
                        "humidity": 65,
                    },
                ]
            }
        }
        with (
            patch.object(
                solver_writer,
                "ha_call_service_with_response",
                return_value=response,
            ) as call,
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_weather_forecast_mirrors(cfg)
        call.assert_called_once_with(
            "weather",
            "get_forecasts",
            {"entity_id": "weather.pirateweather", "type": "hourly"},
        )
        self.assertEqual(post.call_count, 2)
        temp_call, humidity_call = post.call_args_list
        self.assertEqual(temp_call.args[0], "sensor.nimbus_mirror_temperature_forecast")
        self.assertEqual(temp_call.args[1], 20.0)
        self.assertEqual(temp_call.args[2]["source"], "weather.pirateweather")
        self.assertEqual(len(temp_call.args[2]["forecast"]), 2)
        self.assertEqual(
            humidity_call.args[0], "sensor.nimbus_mirror_humidity_forecast"
        )
        self.assertEqual(humidity_call.args[1], 70.0)

    def test_no_humidity_field_publishes_temperature_only(self):
        cfg = {"solver_weather_forecast_sensor": "weather.home"}
        response = {
            "weather.home": {
                "forecast": [
                    {"datetime": "2026-08-25T00:00:00+00:00", "temperature": 19.5}
                ]
            }
        }
        with (
            patch.object(
                solver_writer, "ha_call_service_with_response", return_value=response
            ),
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_weather_forecast_mirrors(cfg)
        post.assert_called_once()
        self.assertEqual(
            post.call_args.args[0], "sensor.nimbus_mirror_temperature_forecast"
        )

    def test_service_call_failure_publishes_nothing(self):
        cfg = {"solver_weather_forecast_sensor": "weather.home"}
        with (
            patch.object(
                solver_writer, "ha_call_service_with_response", return_value=None
            ),
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_weather_forecast_mirrors(cfg)
        post.assert_not_called()


class TestSensorDomain(unittest.TestCase):
    def test_reads_forecast_attribute_directly_no_service_call(self):
        cfg = {"solver_weather_forecast_sensor": "sensor.my_own_weather_forecast"}
        state = {
            "attributes": {
                "forecast": [
                    {"datetime": "2026-08-25T00:00:00+00:00", "temperature": 18.0}
                ]
            }
        }
        with (
            patch.object(solver_writer, "ha_call_service_with_response") as call,
            patch.object(solver_writer, "ha_get", return_value=state),
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_weather_forecast_mirrors(cfg)
        call.assert_not_called()
        post.assert_called_once()
        self.assertEqual(post.call_args.args[1], 18.0)

    def test_missing_state_is_a_clean_noop(self):
        cfg = {"solver_weather_forecast_sensor": "sensor.gone"}
        with (
            patch.object(
                solver_writer,
                "ha_get",
                side_effect=solver_writer.urllib.error.URLError("not found"),
            ),
            patch.object(solver_writer, "ha_post_state") as post,
        ):
            solver_writer.publish_weather_forecast_mirrors(cfg)
        post.assert_not_called()
