"""nimbus #1195 (Mark Purcell): `weather.get_forecasts` raised a full
traceback three times within a second of every restart, for a condition
that is expected, transient and self-correcting.

**The measured report.** On his install, every single restart produced
three copies of:

    HomeAssistantError: Service call requested response data but did not
    match any entities

`weather.home` genuinely exists and is healthy — the failure is a startup
ordering race. The weather integration has not registered/populated the
entity yet when the first post-restart coordinator cycle reaches
`_async_fetch_weather_forecast_via_service()`, so HA core's own
entity-service resolution correctly finds nothing to target and raises.
Three times rather than once because three subentries each configure the
same entity and each hits the race independently.

**This was already handled.** The call site wrapped it, degraded to "no
forecast this cycle", and recovered on a later cycle with an INFO. Mark
filed it *for the record* and said explicitly he would be happy to be told
it needs no action. So the fix is deliberately scoped to the noise, not to
the behaviour: check the entity before calling the service instead of
after it raises.

The properties that carry it, each pinned below:

1. **No state, or an `unavailable`/`unknown` state, means the service is
   never called at all** — so HA core never raises and there is no
   traceback to format.
2. **A healthy entity is completely unaffected** — the service is still
   called with exactly the same arguments, and the forecast still parses.
   This is the control: a guard that also changes the working path is not
   this fix.
3. **The `except` path still works.** The guard is a narrowing, not a
   replacement: a state existing is necessary but not sufficient for HA's
   service resolution (registry and platform readiness are separate), so
   anything that still fails must be reported exactly as before.
4. **The caller's own diagnostic still fires.** This is the load-bearing
   one. `temperature_forecast_sensor '%s' is configured but yielded 0
   forecast entries` is the only signal a household gets when the
   configured entity_id is simply *wrong*, and #269's own comment records
   why skipping it on the first tick was a regression. Quieting the
   duplicate traceback must not quiet the diagnostic — the #945 discipline.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import CONF_TEMPERATURE_FORECAST_SENSOR
from custom_components.nimbus_load.coordinator import NimbusCoordinator

WEATHER = "weather.home"


def _coord(state_obj, service=None):
    """A bare coordinator whose weather entity has the given state object."""
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: WEATHER})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = state_obj
    coord.hass.services.async_call = service or AsyncMock(return_value={})
    return coord


def _fetch(coord):
    return asyncio.run(coord._async_fetch_weather_forecast_via_service(WEATHER))


class TestTheServiceIsNotCalledWhenThereIsNothingToTarget:
    """Property 1 — the traceback is removed at source."""

    def test_no_state_at_all_skips_the_call(self):
        svc = AsyncMock(return_value={})
        coord = _coord(None, svc)
        assert _fetch(coord) == []
        svc.assert_not_called()

    def test_unavailable_skips_the_call(self):
        svc = AsyncMock(return_value={})
        coord = _coord(MagicMock(state="unavailable"), svc)
        assert _fetch(coord) == []
        svc.assert_not_called()

    def test_unknown_skips_the_call(self):
        svc = AsyncMock(return_value={})
        coord = _coord(MagicMock(state="unknown"), svc)
        assert _fetch(coord) == []
        svc.assert_not_called()

    def test_the_skip_is_debug_not_warning(self, caplog):
        """The whole point: an expected, self-correcting startup condition
        must not be logged as though something went wrong."""
        coord = _coord(None)
        with caplog.at_level(logging.DEBUG):
            _fetch(coord)
        assert any(
            r.levelno == logging.DEBUG and "1195" in r.getMessage()
            for r in caplog.records
        ), "expected a DEBUG line naming the issue"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "the skip must not warn"
        )


class TestAHealthyEntityIsUnaffected:
    """Property 2 — the control. A guard that changes the working path is
    not this fix."""

    def test_the_service_is_still_called_with_identical_arguments(self):
        svc = AsyncMock(return_value={WEATHER: {"forecast": [{"temperature": 21.0}]}})
        coord = _coord(MagicMock(state="sunny"), svc)
        assert _fetch(coord) == [{"temperature": 21.0}]
        svc.assert_called_once_with(
            "weather",
            "get_forecasts",
            {"type": "hourly"},
            target={"entity_id": WEATHER},
            blocking=True,
            return_response=True,
        )

    def test_an_odd_but_present_state_string_still_calls(self):
        """Only the two sentinel states skip. A weather entity's state is a
        condition string, and the set of real conditions is not this
        function's business to enumerate."""
        svc = AsyncMock(return_value={WEATHER: {"forecast": []}})
        coord = _coord(MagicMock(state="exceptional"), svc)
        _fetch(coord)
        svc.assert_called_once()


class TestTheExceptPathStillReports:
    """Property 3 — the guard narrows, it does not replace."""

    def test_a_raising_service_on_a_healthy_entity_still_warns_and_returns_empty(
        self, caplog
    ):
        svc = AsyncMock(side_effect=RuntimeError("hourly not supported"))
        coord = _coord(MagicMock(state="sunny"), svc)
        with caplog.at_level(logging.DEBUG):
            assert _fetch(coord) == []
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a genuine failure must still be reported"
        assert "weather.get_forecasts failed" in warnings[0].getMessage()


class TestTheCallersDiagnosticSurvives:
    """Property 4 — the load-bearing control.

    Quieting the duplicate traceback must not quiet the one signal a
    household gets when the configured entity_id is simply wrong.
    """

    def test_a_misconfigured_entity_still_produces_the_callers_warning(self, caplog):
        coord = NimbusCoordinator.__new__(NimbusCoordinator)
        coord.entry = MagicMock(
            options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.typo"}
        )
        coord.hass = MagicMock()
        # Never exists, on any cycle -- a typo, not a startup race.
        coord.hass.states.get.return_value = None
        coord.hass.services.async_call = AsyncMock(return_value={})
        coord._temp_forecast_cache = []
        coord._temp_forecast_cache_entity = None
        coord._last_temp_forecast_ok = None

        with caplog.at_level(logging.DEBUG):
            result = asyncio.run(coord._async_fetch_temperature_forecast())

        assert result == []
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("yielded 0 forecast entries" in m for m in warnings), (
            "the caller's own misconfiguration warning must still fire -- "
            "#1195 removes a duplicate traceback, not the diagnostic"
        )
