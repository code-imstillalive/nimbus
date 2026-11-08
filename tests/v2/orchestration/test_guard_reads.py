"""The dispatch guard's two reads of the house (#2393).

`solver_dispatch/guard.py` is at 92% on main's CI. Two of the reads it acts
on had no test of their own; the dispatch tests replace them:
- the hourly temperature forecast for a thermal load's ambient projection,
  from a `weather.*` entity's `get_forecasts` or a `sensor.*`'s `forecast`
  attribute (HACS review, hacs/default#12099);
- whether a device's own setting already is what Nimbus commanded, so a
  device set as commanded that still isn't running is not re-sent (#2297,
  Mark Purcell).
Synthetic states only.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from solver_dispatch import guard

HOURLY = [{"datetime": "2026-11-09T05:00:00+10:00", "temperature": 21.0}]


class FakeHass:
    def __init__(self, states=None, response=None, raises=False):
        self._states = states or {}
        self._response = response
        self._raises = raises
        self.calls = []
        self.states = SimpleNamespace(get=self._states.get)
        self.services = SimpleNamespace(async_call=self._call)

    async def _call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, data, kwargs))
        if self._raises:
            raise RuntimeError("weather integration not loaded")
        return self._response


def hourly(hass, entity_id):
    return asyncio.run(guard.async_hourly_forecast(hass, entity_id))


def test_the_hourly_forecast_comes_from_the_weather_action_or_a_sensors_attribute():
    hass = FakeHass(response={"weather.home": {"forecast": HOURLY}})
    assert hourly(hass, "weather.home") == HOURLY
    ((domain, service, data, kwargs),) = hass.calls
    assert (domain, service, data) == (
        "weather",
        "get_forecasts",
        {"entity_id": "weather.home", "type": "hourly"},
    )
    assert kwargs == {"blocking": True, "return_response": True}

    sensor = SimpleNamespace(attributes={"forecast": HOURLY})
    assert hourly(FakeHass({"sensor.outdoor": sensor}), "sensor.outdoor") == HOURLY

    # No entity, a failed action, an empty answer or a missing sensor is no
    # forecast: the ambient projection is optional, never a reason to fail.
    assert hourly(FakeHass(), "") is None
    assert hourly(FakeHass(raises=True), "weather.home") is None
    assert (
        hourly(FakeHass(response={"weather.home": {"forecast": []}}), "weather.home")
        is None
    )
    assert hourly(FakeHass(response=None), "weather.home") is None
    assert hourly(FakeHass(), "sensor.outdoor") is None


def state(value, **attributes):
    return SimpleNamespace(state=value, attributes=attributes)


def test_whether_the_device_already_is_as_commanded_is_read_per_domain():
    matches = guard.device_setting_matches
    assert matches(state("on"), "switch.pump", True, None) is True
    assert matches(state("on"), "switch.pump", False, None) is False
    # A water heater's mode lives in its operation_mode attribute.
    heater = state("heat_pump", operation_mode="performance")
    assert matches(heater, "water_heater.hws", True, None) is True
    assert matches(heater, "water_heater.hws", False, None) is False
    assert matches(state("eco"), "water_heater.hws", False, None) is True
    # A climate entity's ON is its configured hvac_mode; OFF is "off".
    assert matches(state("heat"), "climate.ac", True, "heat") is True
    assert matches(state("cool"), "climate.ac", True, "heat") is False
    assert matches(state("off"), "climate.ac", False, "heat") is True

    # Nothing to compare is None, never a guess either way.
    assert matches(None, "switch.pump", True, None) is None
    assert matches(state("heat"), "climate.ac", True, None) is None
    assert matches(state("on"), "light.porch", True, None) is None
    for unreadable in (None, "", "unknown", "unavailable"):
        assert matches(state(unreadable), "switch.pump", True, None) is None
