"""The weather forecast mirrored for the dashboard (#1837).

The source is whatever the configured weather field names, never a
literal in code: a weather.* entity is asked through get_forecasts, a
sensor.* is read off its own forecast attribute. Humidity is published
only when the source carries it. Dashboard-only; any failure publishes
nothing.
"""

from __future__ import annotations

import urllib.error

import pytest
import solver_writer as sw

TEMP = "sensor.nimbus_mirror_temperature_forecast"
HUMIDITY = "sensor.nimbus_mirror_humidity_forecast"


@pytest.fixture
def ha(monkeypatch):
    seen = {"calls": [], "gets": [], "posts": {}, "response": None, "state": None}

    def call(domain, service, data):
        seen["calls"].append((domain, service, data))
        return seen["response"]

    def get(eid):
        seen["gets"].append(eid)
        if isinstance(seen["state"], Exception):
            raise seen["state"]
        return seen["state"]

    monkeypatch.setattr(sw, "ha_call_service_with_response", call)
    monkeypatch.setattr(sw, "ha_get", get)
    monkeypatch.setattr(
        sw,
        "ha_post_state",
        lambda eid, state, attrs: seen["posts"].__setitem__(eid, (state, attrs)),
    )
    return seen


def _publish(source):
    sw.publish_weather_forecast_mirrors({"solver_weather_forecast_sensor": source})


def test_a_weather_entity_is_asked_for_its_hourly_forecast_and_both_are_mirrored(ha):
    ha["response"] = {
        "weather.any": {
            "forecast": [
                {
                    "datetime": "2026-08-25T00:00:00+00:00",
                    "temperature": 20.04,
                    "humidity": 70,
                },
                {
                    "datetime": "2026-08-25T01:00:00+00:00",
                    "temperature": 21.0,
                    "humidity": 65,
                },
                {
                    "datetime": "2026-08-25T02:00:00+00:00",
                    "humidity": 60,
                },  # no temperature
                "not a row",
            ]
        }
    }
    _publish("weather.any")
    assert ha["calls"] == [
        ("weather", "get_forecasts", {"entity_id": "weather.any", "type": "hourly"})
    ]
    temp, attrs = ha["posts"][TEMP]
    assert temp == 20.0 and attrs["source"] == "weather.any"
    assert [p["value"] for p in attrs["forecast"]] == [20.0, 21.0]
    assert attrs["forecast"][0]["time"] == "2026-08-25T00:00:00+00:00"
    assert attrs["unit_of_measurement"] == "°C" and attrs["generated_at"]
    humidity, h_attrs = ha["posts"][HUMIDITY]
    assert (
        humidity == 70.0
        and len(h_attrs["forecast"]) == 3
        and h_attrs["unit_of_measurement"] == "%"
        and h_attrs["source"] == "weather.any"
    )


def test_a_sensor_source_is_read_off_its_own_attribute_without_a_service_call(ha):
    ha["state"] = {
        "attributes": {
            "forecast": [{"datetime": "2026-08-25T00:00:00+00:00", "temperature": 18.0}]
        }
    }
    _publish("sensor.own_forecast")
    assert ha["calls"] == [] and ha["gets"] == ["sensor.own_forecast"]
    assert ha["posts"][TEMP][0] == 18.0


def test_a_failed_read_publishes_nothing(ha):
    for source, response, state in [
        ("weather.any", None, None),
        ("sensor.gone", None, urllib.error.URLError("not found")),
    ]:
        ha["response"], ha["state"] = response, state
        _publish(source)
        assert ha["posts"] == {}
