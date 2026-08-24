"""Real regression test, found live on devhub (2026-08-25): devhub runs
Nimbus in NATIVE mode (custom_components.nimbus_load.solver_runtime,
in-process, not the standalone REST/subprocess writer) --
ha_call_service_with_response()'s first version returned None
unconditionally whenever _NATIVE_HASS was set, silently no-op'ing
publish_weather_forecast_mirrors() with no diagnostic at all. Fixed with
the same asyncio.run_coroutine_threadsafe() bridge fetch_price_history()'s
own native branch already uses to call an async, event-loop-owned HA API
from this file's own sync executor-thread context.

Runs a REAL background asyncio event loop in a thread (not a mock loop)
so this actually exercises run_coroutine_threadsafe(), the one part of
the fix a plain MagicMock can't stand in for.
"""

import asyncio
import threading
import time
import unittest

import _solver_path  # noqa: F401
import solver_writer


class _FakeServices:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = []

    async def async_call(
        self, domain, service, data, blocking=True, return_response=True
    ):
        self.calls.append((domain, service, data, blocking, return_response))
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeNativeHass:
    def __init__(self, loop, response=None, exc=None):
        self.loop = loop
        self.services = _FakeServices(response=response, exc=exc)


class TestHaCallServiceWithResponseNativeMode(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        # Give the loop a moment to actually start running before any
        # run_coroutine_threadsafe() call -- real, not a magic number:
        # thread startup is genuinely async from this constructor's own
        # point of view.
        for _ in range(50):
            if self.loop.is_running():
                break
            time.sleep(0.01)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def tearDown(self):
        solver_writer._NATIVE_HASS = None
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)
        self.loop.close()

    def test_native_mode_returns_the_real_service_response(self):
        response = {
            "weather.home": {"forecast": [{"datetime": "t", "temperature": 21.0}]}
        }
        solver_writer._NATIVE_HASS = _FakeNativeHass(self.loop, response=response)
        result = solver_writer.ha_call_service_with_response(
            "weather", "get_forecasts", {"entity_id": "weather.home", "type": "hourly"}
        )
        self.assertEqual(result, response)

    def test_native_mode_passes_through_domain_service_and_blocking_response_flags(
        self,
    ):
        solver_writer._NATIVE_HASS = _FakeNativeHass(self.loop, response={})
        solver_writer.ha_call_service_with_response(
            "weather", "get_forecasts", {"entity_id": "weather.home", "type": "hourly"}
        )
        calls = solver_writer._NATIVE_HASS.services.calls
        self.assertEqual(len(calls), 1)
        domain, service, data, blocking, return_response = calls[0]
        self.assertEqual(domain, "weather")
        self.assertEqual(service, "get_forecasts")
        self.assertEqual(data, {"entity_id": "weather.home", "type": "hourly"})
        self.assertTrue(blocking)
        self.assertTrue(return_response)

    def test_native_mode_service_call_failure_returns_none_not_a_crash(self):
        solver_writer._NATIVE_HASS = _FakeNativeHass(
            self.loop, exc=RuntimeError("does not support 'hourly' forecast")
        )
        result = solver_writer.ha_call_service_with_response(
            "weather", "get_forecasts", {"entity_id": "weather.home", "type": "hourly"}
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
