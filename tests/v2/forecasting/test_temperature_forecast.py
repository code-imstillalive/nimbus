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


def test_sensor_domain_missing_state_returns_empty_not_raises():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.gone"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = None
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert result == []


# -- weather.* path, the real #123 fix ----------------------------------------


def test_weather_domain_entity_not_in_response_returns_empty():
    # A real, plausible shape if the response somehow omits the entity
    # (defensive -- shouldn't happen for a single-entity target, but
    # must not crash if it does).
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.home"})
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock(return_value={})
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert result == []


def test_weather_domain_naive_datetime_no_longer_crashes_and_is_treated_as_local():
    # Mark's own exact #137 repro: weather.noosa_heads_hourly returns
    # NAIVE datetime strings (no offset suffix) whose numbers are
    # already local (AEST) wall-clock time -- confirmed by him against
    # real live weather at the time ("sunny 22C matches now, not the
    # middle of the AEST night that 15:00 UTC would be"). Before the
    # fix this crashed the whole coordinator tick outright
    # (TypeError: can't compare offset-naive and offset-aware
    # datetimes) the moment out.sort() tried to compare a naive entry
    # against an aware one, or any downstream bisect_right() call
    # compared this against the always-aware `target`.
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(
        options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.noosa_heads_hourly"}
    )
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock(
        return_value={
            "weather.noosa_heads_hourly": {
                "forecast": [
                    # Real shape from his own raw service-call dump --
                    # no "+HH:MM"/"Z" suffix at all.
                    {"datetime": "2026-08-24T15:00:00", "temperature": 22.0},
                    {"datetime": "2026-08-24T16:00:00", "temperature": 21.5},
                ]
            }
        }
    )
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    # Must not have crashed to get here at all -- the real #137 bug.
    ts0, temp0 = result[0]
    assert temp0 == 22.0
    # Genuinely tz-AWARE now (not still naive) -- required for the
    # sort()/bisect_right() calls this feeds into to work at all.
    assert ts0.tzinfo is not None
    # The real, correct interpretation: "15:00" means 15:00 AEST, a pure
    # relabel with ZERO numeric shift -- NOT 15:00 UTC-then-converted
    # (which would land on 01:00 the NEXT day in AEST, the exact wrong
    # answer Mark's own first-suggested fix, dt_util.as_local(), would
    # have silently produced -- see _normalize_forecast_timestamp's own
    # docstring for why that specific suggestion had a real bug in it).
    assert ts0.hour == 15
    assert ts0.astimezone(UTC).hour == 5  # 15:00 AEST == 05:00 UTC


def test_normalize_forecast_timestamp_naive_gets_relabelled_not_converted():
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


def test_normalize_forecast_timestamp_already_aware_passes_through_unchanged():
    aware = datetime(2026, 8, 24, 5, 0, 0, tzinfo=UTC)
    result = _normalize_forecast_timestamp(aware)
    assert result == aware
    assert result.tzinfo is UTC


def test_weather_domain_mixed_naive_and_aware_entries_sort_without_crashing():
    # A real, plausible shape for an integration that's inconsistent
    # about it (or a genuine boundary between two different upstream
    # data sources) -- must not crash regardless of which entries are
    # naive vs aware, or in what order they arrive.
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.mixed"})
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock(
        return_value={
            "weather.mixed": {
                "forecast": [
                    {"datetime": "2026-08-24T18:00:00+00:00", "temperature": 19.0},
                    {"datetime": "2026-08-24T15:00:00", "temperature": 22.0},
                ]
            }
        }
    )
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert len(result) == 2
    # Genuinely sorted by real chronological order (15:00 AEST ==
    # 05:00 UTC, which IS before 18:00 UTC) -- not just "didn't crash".
    assert [t for _, t in result] == [22.0, 19.0]


def test_weather_domain_unsupported_forecast_type_degrades_not_crashes():
    # The real failure HA core raises when an entity doesn't support
    # hourly forecasts at all (HomeAssistantError from
    # async_get_forecasts_service) -- must degrade to "no forecast this
    # cycle", never take the whole coordinator tick down with it.
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(
        options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.daily_only"}
    )
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock(
        side_effect=RuntimeError("does not support 'hourly' forecast")
    )
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert result == []


# -- #269 mitigation B: warn on state-change, not just the first-ever tick ---


def test_a_genuinely_healthy_result_never_warns(caplog):
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(
        options={CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.healthy"}
    )
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        attributes={
            "forecast": [{"datetime": "2026-08-24T10:00:00+00:00", "temperature": 21.0}]
        }
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(coord._async_fetch_temperature_forecast())
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert coord._last_temp_forecast_ok is True


# -- #269 mitigation A: cache the last-good forecast across ticks -------------


def test_cache_never_leaks_across_a_reconfigured_source_entity():
    """Case 2 from issue #269: cache populated under weather.a, then the
    household reconfigures to weather.b, which also fails -- must return
    [] (the fresh, correct behaviour for a brand-new source with nothing
    of its own cached yet), never the OLD entity's stale values.
    """
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.a"})
    coord.hass = MagicMock()
    coord.hass.services.async_call = AsyncMock(
        return_value={
            "weather.a": {
                "forecast": [
                    {"datetime": "2099-01-01T10:00:00+00:00", "temperature": 21.0}
                ]
            }
        }
    )
    good = asyncio.run(coord._async_fetch_temperature_forecast())
    assert [t for _, t in good] == [21.0]

    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.b"})
    coord.hass.services.async_call = AsyncMock(
        side_effect=RuntimeError("weather.b down")
    )
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert result == []


def test_cache_trims_to_future_only_not_stale_past_entries():
    """A cached forecast can genuinely go stale (every one of its
    entries is now in the past) if a source stays down long enough --
    the fallback must only ever return entries still >= "now", never
    resurrect points the real world has already passed."""
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.home"})
    coord.hass = MagicMock()
    past = (coordinator.dt_util.utcnow() - timedelta(hours=2)).isoformat()
    future = (coordinator.dt_util.utcnow() + timedelta(hours=2)).isoformat()
    coord.hass.services.async_call = AsyncMock(
        return_value={
            "weather.home": {
                "forecast": [
                    {"datetime": past, "temperature": 10.0},
                    {"datetime": future, "temperature": 20.0},
                ]
            }
        }
    )
    asyncio.run(coord._async_fetch_temperature_forecast())
    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("down"))
    rescued = asyncio.run(coord._async_fetch_temperature_forecast())
    assert [t for _, t in rescued] == [20.0]
