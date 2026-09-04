"""Real regression test for nimbus repo issue #123 (Mark Purcell, a real
independent installer's own live health-check, 2026-08-24, direct
follow-up to #113): modern Home Assistant (2024.x+) no longer exposes a
`forecast` state attribute on `weather.*` entities at all -- the only way
to reach one is the `weather.get_forecasts` service call
(`supports_response=SupportsResponse.ONLY`, confirmed live against HA
core's own source). `_async_fetch_temperature_forecast()` used to read
`state.attributes.get("forecast", [])` unconditionally, which silently
returned `[]` for every `weather.*` entity on any current HA install --
no error, no warning, just a temperature feature that trained as
permanent dead weight.

Covers both real paths this function now branches on (a plain `sensor.*`
template that already publishes a `forecast` attribute -- the original,
still-fully-supported path -- and a `weather.*` entity, fetched via the
service call), plus the new empty-result warning (logged once per
coordinator instance, not once per tick) and the service call's own
honest-fallback-over-crash handling for a real, not theoretical, failure
(the entity doesn't support hourly forecasts at all).

Also covers issue #137 (Mark Purcell, real repro, direct follow-up to
#123, same day): the `weather.get_forecasts` code-path landed reachable
but incomplete -- it never tz-normalised the returned datetimes, and
some real weather integrations (his own `weather.noosa_heads_hourly`)
emit genuinely naive ones. This is exactly the gap he predicted in his
own report ("was the round-trip run against a real weather
integration, or only synthetic tz-aware fixtures?") -- confirmed
honestly: only synthetic tz-aware fixtures, until these tests.

Also covers issue #269 (Mark Purcell, real repro on ha.purcell.id.au,
2026-08-28, direct follow-up to #123): a single transient
weather.get_forecasts failure -- most commonly the HA-restart startup
race between this coordinator's first tick and the weather integration's
own first successful fetch -- silently degraded that cycle's training to
zero temperature signal, and the old one-shot warning flag made a LATER
chronic failure invisible after its first occurrence. Two independent
mitigations, both from Mark's own real proposal: (A) cache the last
real, non-empty forecast per entity_id and fall back to a future-only
trimmed slice of it on failure, rather than training on nothing; (B)
replace the one-shot flag with a state-change tracker that warns on
every success->failure transition and logs an INFO on every recovery.
The state-change tests deliberately verify a genuine, considered
improvement over Mark's own literal sketch -- his version treated the
very first-ever fetch as "no state change to log" regardless of outcome,
which would silently drop the original, already-relied-on "warn on the
first empty result" behaviour for a household whose sensor never once
works.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import coordinator
from custom_components.nimbus_load.const import CONF_TEMPERATURE_FORECAST_SENSOR
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
    # #269 state -- see coordinator.py's own __init__ for what each means.
    coord._temp_forecast_cache = []
    coord._temp_forecast_cache_entity = None
    coord._last_temp_forecast_ok = None
    return coord


# -- sensor.* path, unchanged/still-supported --------------------------------


def test_no_sensor_configured_returns_empty_no_hass_call_at_all():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={})  # CONF_TEMPERATURE_FORECAST_SENSOR unset
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert result == []


def test_sensor_domain_still_reads_the_forecast_attribute_directly():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(
        options={CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.my_template"}
    )
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        attributes={
            "forecast": [
                {"datetime": "2026-08-24T10:00:00+00:00", "temperature": 21.0},
                {"datetime": "2026-08-24T11:00:00+00:00", "temperature": 22.5},
            ]
        }
    )
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert [t for _, t in result] == [21.0, 22.5]
    # The sensor.* path must never touch hass.services at all -- it's a
    # plain attribute read, same as before this fix.
    coord.hass.services.async_call.assert_not_called()


def test_sensor_domain_missing_state_returns_empty_not_raises():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.gone"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = None
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert result == []


# -- weather.* path, the real #123 fix ----------------------------------------


def test_weather_domain_calls_get_forecasts_hourly_and_parses_the_response():
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(
        options={CONF_TEMPERATURE_FORECAST_SENSOR: "weather.noosa_heads_hourly"}
    )
    coord.hass = MagicMock()
    # Real, confirmed response shape (HA core's own entity_service_call
    # aggregation keys a single-entity target's result by its own
    # entity_id -- {entity_id: {"forecast": [...]}}, not returned flat).
    coord.hass.services.async_call = AsyncMock(
        return_value={
            "weather.noosa_heads_hourly": {
                "forecast": [
                    {
                        "datetime": "2026-08-24T10:00:00+00:00",
                        "condition": "sunny",
                        "temperature": 21.1,
                        "precipitation": 0.0,
                    },
                    {
                        "datetime": "2026-08-24T11:00:00+00:00",
                        "condition": "sunny",
                        "temperature": 23.4,
                        "precipitation": 0.0,
                    },
                ]
            }
        }
    )
    result = asyncio.run(coord._async_fetch_temperature_forecast())
    assert [t for _, t in result] == [21.1, 23.4]
    coord.hass.services.async_call.assert_called_once_with(
        "weather",
        "get_forecasts",
        {"type": "hourly"},
        target={"entity_id": "weather.noosa_heads_hourly"},
        blocking=True,
        return_response=True,
    )


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


def test_steady_empty_result_warns_once_not_once_per_tick(caplog):
    """A sensor that's EMPTY from the very first tick and stays that way:
    still warns exactly once (the first-ever failure IS a real state
    change worth logging -- see coordinator.py's own comment for why
    this deliberately differs from Mark's literal proposed sketch, which
    would have logged nothing at all for this exact case), then stays
    silent on every subsequent tick with no further change.
    """
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(
        options={CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.always_empty"}
    )
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(attributes={"forecast": []})
    with caplog.at_level(logging.WARNING):
        asyncio.run(coord._async_fetch_temperature_forecast())
        asyncio.run(coord._async_fetch_temperature_forecast())
        asyncio.run(coord._async_fetch_temperature_forecast())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "always_empty" in warnings[0].getMessage()
    assert coord._last_temp_forecast_ok is False


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


def test_start_failing_after_a_period_of_success_warns_again(caplog):
    """Case 3 from issue #269: succeed once, then fail -- must warn
    AGAIN even though the empty-result warning already fired once
    conceptually 'for this instance' under the old one-shot design. A
    chronic failure starting well after startup must stay loud.
    """
    coord = _make_bare_coordinator()
    coord.entry = MagicMock(options={CONF_TEMPERATURE_FORECAST_SENSOR: "sensor.flaky"})
    coord.hass = MagicMock()
    coord.hass.states.get.return_value = MagicMock(
        attributes={
            "forecast": [{"datetime": "2026-08-24T10:00:00+00:00", "temperature": 21.0}]
        }
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(coord._async_fetch_temperature_forecast())  # succeeds, no warning
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]

    coord.hass.states.get.return_value = MagicMock(attributes={"forecast": []})
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(coord._async_fetch_temperature_forecast())  # now fails
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "stopped yielding" in warnings[0].getMessage()
    assert coord._last_temp_forecast_ok is False


def test_recovery_logs_info_and_a_later_failure_re_warns(caplog):
    """Case 4 from issue #269: fail once, then succeed -- one INFO on
    recovery, and confirms a LATER failure re-fires the WARNING (state-
    change on both edges, not a one-shot that's now permanently spent).
    """
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


# -- #269 mitigation A: cache the last-good forecast across ticks -------------


def test_cache_hit_rescues_a_transient_failure():
    """Populate the cache with a successful fetch, then make the SAME
    entity fail -- the cached, still-future values are returned instead
    of an empty list, so that cycle's training isn't degraded to zero
    signal by a purely transient failure.
    """
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
