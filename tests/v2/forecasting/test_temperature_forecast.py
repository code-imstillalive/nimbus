"""A temperature forecast is read through `weather.get_forecasts`, since a
current weather entity carries none in its attributes (#123) (#1837).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import coordinator
from custom_components.nimbus_load.const import (
    CONF_LOAD_SENSOR,
    CONF_TEMPERATURE_FORECAST_SENSOR,
)
from custom_components.nimbus_load.coordinator import (
    NimbusCoordinator,
    _normalize_forecast_timestamp,
)

# tests/_ha_stubs.py registers homeassistant.util.dt as a bare
# MagicMock() (shared stand-in, since no other current test needs real
# datetime parsing) -- coordinator.dt_util.parse_datetime(...) would
# return another auto-generated MagicMock per call, and two such mocks
# can't be `<` compared, which is exactly what out.sort(key=...) needs
# to do. Wire a real ISO-8601 parser onto it for this file's own tests,
# which are the first to actually exercise the datetime-parsing path.
# datetime.fromisoformat() genuinely reproduces the real #137 bug shape
# on a suffix-less string ("2026-08-24T15:00:00", no "+HH:MM"/"Z") --
# real Python returns a NAIVE datetime for that input, byte-for-byte the
# same shape Mark's own weather.noosa_heads_hourly integration emits.
coordinator.dt_util.parse_datetime = datetime.fromisoformat
# Real HA sets this from hass.config.time_zone at startup -- AEST
# (UTC+10, no DST) matches both this repo's own reference household and
# Mark's own real #137 report ("15:00" in his install's naive strings
# means 15:00 AEST, confirmed against real live weather at the time).
coordinator.dt_util.DEFAULT_TIME_ZONE = timezone(timedelta(hours=10))
# #269's own cache-trimming logic needs a real "now" to compare cached
# forecast timestamps against -- the shared stub otherwise leaves
# dt_util.utcnow() as an auto-generated MagicMock, which can't be
# compared (`>=`) against a real datetime at all.
coordinator.dt_util.utcnow = lambda: datetime.now(UTC)

_T0 = datetime(2026, 8, 24, 9, 0, 0, tzinfo=UTC)
_AEST = timezone(timedelta(hours=10))


def _make_bare_coordinator() -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    # #1540: the feature accessors compare against this subentry's own load
    # sensor, which none of these tests' sources are.
    coord.subentry = MagicMock(data={CONF_LOAD_SENSOR: "sensor.some_load"})
    # #269 state -- see coordinator.py's own __init__ for what each means.
    coord._temp_forecast_cache = []
    coord._temp_forecast_cache_entity = None
    coord._last_temp_forecast_ok = None
    return coord


# -- sensor.* path, unchanged/still-supported --------------------------------


# -- weather.* path, the real #123 fix ----------------------------------------


def test_weather_domain_entity_not_in_response_returns_empty(caplog):
    # test_weather_domain_entity_not_in_response_returns_empty
    # test_weather_domain_entity_not_in_response_returns_empty
    # A real, plausible shape if the response somehow omits the entity
    # (defensive -- shouldn't happen for a single-entity target, but
    # must not crash if it does).
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.home"})
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock(return_value={})
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert result == []

    # test_normalize_forecast_timestamp_naive_gets_relabelled_not_converted
    # Direct, isolated proof of the actual fix -- a pure .replace(tzinfo=...),
    # zero numeric shift, regardless of what other tests exercise around it.
    # Deliberately naive (no tzinfo=) -- that's the exact real-world input
    # shape this test exists to exercise, not an oversight.
    naive = datetime(2026, 8, 24, 15, 0, 0)  # noqa: DTZ001
    result = _normalize_forecast_timestamp(naive)
    assert result.tzinfo is not None
    assert result.hour == 15
    assert result.minute == 0
    assert result.day == 24

    # test_recovery_logs_info_and_a_later_failure_re_warns: Case 4 from issue #269: fail once, then succeed -- one INFO on
    caplog.clear()
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.flaky"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(attributes={"forecast": []})
    with caplog.at_level(logging.WARNING):
        asyncio.run(coord._async_fetch_temperature_forecast())  # first-ever failure
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    coord.hass.states.get.return_value = MagicMock(
        attributes={
            "forecast": [{"datetime": "2026-08-24T10:00:00+00:00", "temperature": 21.0}]
        }
    )
    caplog.clear()
    with caplog.at_level(logging.INFO):
        asyncio.run(coord._async_fetch_temperature_forecast())  # recovers
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    assert "recovered" in infos[0].getMessage()
    assert coord._last_temp_forecast_ok is True

    coord.hass.states.get.return_value = MagicMock(attributes={"forecast": []})
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(coord._async_fetch_temperature_forecast())  # fails again
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "stopped yielding" in warnings[0].getMessage()

    # test_cache_hit_rescues_a_transient_failure: Populate the cache with a successful fetch, then make the SAME
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.home"})
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock(
        return_value={
            "weather.home": {
                "forecast": [
                    {"datetime": "2099-01-01T10:00:00+00:00", "temperature": 21.0},
                    {"datetime": "2099-01-01T11:00:00+00:00", "temperature": 22.5},
                ]
            }
        }
    )
    good = asyncio.run(coord._async_fetch_temperature_forecast())
    assert [t for _, t in good] == [21.0, 22.5]

    # Now the same entity starts failing outright.
    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("transient"))
    rescued = asyncio.run(coord._async_fetch_temperature_forecast())
    assert [t for _, t in rescued] == [21.0, 22.5]


# -- #269 mitigation B: warn on state-change, not just the first-ever tick ---


# -- #269 mitigation A: cache the last-good forecast across ticks -------------
