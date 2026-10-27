"""Occupancy for the load forecast, against real Home Assistant (#1847): the
entity states read as home or away, departure and return times parsed in
local time, and the forecast's away times taking the away profile."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.nimbus_load import coordinator as coord
from custom_components.nimbus_load.const import (
    CONF_LOAD_SENSOR,
    CONF_OCCUPANCY_DEPARTURE,
    CONF_OCCUPANCY_ENTITY,
    CONF_OCCUPANCY_RETURN,
)
from custom_components.nimbus_load.ml import occupancy as occ


async def test_occupancy_states_and_trip_times_read_in_local_time(hass: HomeAssistant):
    value = coord.occupancy_value
    assert value("home") == 1.0 and value("on") == 1.0
    assert value("not_home") == 0.0 and value("off") == 0.0
    assert value("work") == 0.0  # a person in another zone
    assert value("2") == 1.0 and value("0") == 0.0  # a zone's count
    assert math.isnan(value("unavailable")) and math.isnan(value(None))

    parse = coord.parse_occupancy_datetime
    local = dt_util.get_default_time_zone()
    want = datetime(2026, 10, 28, 8, 0, tzinfo=local)
    # input_datetime: its timestamp attribute, or a naive state in local time.
    assert parse("2026-10-28 08:00:00", {"timestamp": want.timestamp()}) == want
    assert parse("2026-10-28 08:00:00", {}) == want
    # A date-only input_datetime: local midnight.
    assert parse("2026-10-28", {}) == datetime(2026, 10, 28, tzinfo=local)
    # A datetime entity or timestamp sensor: ISO with its own offset.
    assert parse(want.astimezone(dt_util.UTC).isoformat(), {}) == want
    assert parse("unknown", {}) is None and parse("soon", {}) is None


def _coordinator(hass, profile):
    c = object.__new__(coord.NimbusCoordinator)
    c.hass = hass
    c.entry = SimpleNamespace(
        options={
            CONF_OCCUPANCY_ENTITY: "input_boolean.home",
            CONF_OCCUPANCY_DEPARTURE: "input_datetime.leave",
            CONF_OCCUPANCY_RETURN: "input_datetime.back",
        }
    )
    c.subentry = SimpleNamespace(data={CONF_LOAD_SENSOR: "sensor.house"}, title="House")
    c._occupancy = {
        "entity_id": "input_boolean.home",
        "events": [],
        "profile": profile,
        "kept": True,
        "test": {"tested_on": None, "mae_with": None, "mae_without": None},
    }
    c._occupancy_bootstrapping = False
    return c


async def test_away_times_take_the_profile_and_home_times_keep_the_model(
    hass: HomeAssistant,
):
    now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    times = [now + timedelta(hours=h) for h in range(6)]
    preds = [3.0] * 6
    profile = occ.AwayProfile(
        kind="base_load", slot_minutes=60, kw=tuple([0.5] * 24), away_days=0
    )
    c = _coordinator(hass, profile)

    # Home, no trip: the model's forecast, unchanged.
    hass.states.async_set("input_boolean.home", "on")
    out, away = c._occupancy_adjust(times, preds)
    assert out == preds and not away.any()

    # A trip from +2 h to +4 h: the profile there, the model either side.
    hass.states.async_set(
        "input_datetime.leave", "x", {"timestamp": times[2].timestamp()}
    )
    hass.states.async_set(
        "input_datetime.back", "x", {"timestamp": times[4].timestamp()}
    )
    out, away = c._occupancy_adjust(times, preds)
    assert out == [3.0, 3.0, 0.5, 0.5, 3.0, 3.0]
    assert list(np.flatnonzero(away)) == [2, 3]
    summary = c._occupancy_summary(times, away)
    assert summary["status"] == "used" and summary["profile"] == "base_load"
    assert summary["away_from"] == times[2].isoformat()

    # Away now, back at +4 h.
    hass.states.async_set("input_boolean.home", "off")
    hass.states.async_set("input_datetime.leave", "unknown")
    out, away = c._occupancy_adjust(times, preds)
    assert out == [0.5, 0.5, 0.5, 0.5, 3.0, 3.0]

    # A load that doesn't keep the profile is left as it is.
    c._occupancy["kept"] = False
    out, away = c._occupancy_adjust(times, preds)
    assert out == preds and not away.any()
    assert c._occupancy_summary(times, away)["status"] == "not_kept"


async def test_before_its_profile_exists_the_forecast_is_unchanged(hass: HomeAssistant):
    c = _coordinator(hass, None)
    c._occupancy = None
    c.hass = MagicMock()
    c._async_occupancy_bootstrap = MagicMock(return_value=None)
    times = [dt_util.now()]
    out, away = c._occupancy_adjust(times, [2.0])
    assert out == [2.0] and not away.any()
    # It starts building the profile, once.
    assert c._occupancy_bootstrapping
    c.hass.async_create_task.assert_called_once()
    c._occupancy_adjust(times, [2.0])
    c.hass.async_create_task.assert_called_once()
    assert c._occupancy_summary(times, away) == {"status": "building"}
