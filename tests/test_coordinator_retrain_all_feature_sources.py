"""Regression test for a real, live-confirmed bug found 2026-08-31: every
call site in _async_retrain() fetching temp/humidity/curtailment/battery/
grid/solar history called `self._async_fetch_history(...)`, a method name
that stopped existing the moment #257/#259 (2026-08-28) renamed it to
`_async_fetch_recorder_history` and introduced the training-source-aware
`_async_fetch_training_history` wrapper. Only the load_events call site got
migrated at the time -- these six did not.

Confirmed live on devhub: `sensor.nimbus_archerfield_temp_forecast` and
`sensor.nimbus_archerfield_humidity_forecast` (power_signal subentries)
never trained (training_points stuck at 0, model_trained_at stuck at
None) across two separate restarts. temp_sensor/humidity_sensor are
shared HUB-level options (see coordinator.py's own "config accessors"
comment), so ANY subentry on an install with a temperature/humidity
sensor configured hit an immediate AttributeError inside _async_retrain(),
before train_model() was ever reached -- which is exactly why neither of
train_model()'s own "No load history available"/"Only N usable training
points" warnings ever appeared in the log for an affected subentry. A
subentry with a pre-#257 .pkl already on disk masked this silently
(_trained is not None, so async_setup() never calls _async_retrain() at
startup at all) -- the bug was real for every subentry, just not yet
visible for ones with an existing cached model.

This test drives the REAL _async_retrain() end-to-end (only the executor
job boundaries -- recorder fetch and the ML training job itself -- are
stubbed) with every one of the six optional feature sensors configured,
proving the method resolves cleanly and _train_model_job() actually
receives non-empty event lists for all six features, not just load_events.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import homeassistant.util as _ha_util

_ha_util.dt.as_local = lambda x: x
_ha_util.dt.utcnow = lambda: datetime.now(UTC)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load.const import (
    CONF_BATTERY_SENSOR,
    CONF_CURTAILMENT_SENSOR,
    CONF_GRID_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    SUBENTRY_TYPE_LOAD,
)
from custom_components.nimbus_load.coordinator import NimbusCoordinator


class _FakeState:
    def __init__(self, state: str, last_changed: datetime) -> None:
        self.state = state
        self.last_changed = last_changed
        self.attributes = {"unit_of_measurement": "kW"}


def _make_coordinator() -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()

    # Generic executor (used for _train_model_job and _save_model_to_disk)
    # -- runs synchronously so the real train/save calls under test execute.
    async def _run_generic_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    coord.hass.async_add_executor_job = AsyncMock(side_effect=_run_generic_executor)

    # Recorder's own executor -- both _async_fetch_recorder_history and
    # _async_fetch_lts_history go through get_instance(hass), never
    # hass.async_add_executor_job directly. Same passthrough technique as
    # test_coordinator_lts_hybrid_training.py.
    async def _run_recorder_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder_executor = MagicMock()
    recorder_executor.async_add_executor_job = AsyncMock(
        side_effect=_run_recorder_executor
    )
    coordinator_module.get_instance = MagicMock(return_value=recorder_executor)

    coord.entry = MagicMock()
    coord.entry.options = {
        CONF_TEMPERATURE_SENSOR: "sensor.temp",
        CONF_HUMIDITY_SENSOR: "sensor.humidity",
        CONF_CURTAILMENT_SENSOR: "sensor.curtailment",
        CONF_BATTERY_SENSOR: "sensor.battery",
        CONF_GRID_SENSOR: "sensor.grid",
        CONF_SOLAR_SENSOR: "sensor.solar",
    }
    coord.subentry = MagicMock()
    coord.subentry.subentry_id = "test-subentry-all-features"
    coord.subentry.subentry_type = SUBENTRY_TYPE_LOAD
    coord.subentry.data = {CONF_LOAD_SENSOR: "sensor.load"}
    coord._retraining = False
    coord._trained = None
    coord._save_model_to_disk = MagicMock()
    coord.async_request_refresh = AsyncMock()
    return coord


def _install_significant_states() -> None:
    """One real history point per entity, keyed by entity_id -- proves each
    of the six call sites actually reached get_significant_states with the
    RIGHT entity_id, not just that no exception was raised.
    """
    now = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)

    def _get_significant_states(hass, start, end, entity_ids, **kwargs):
        entity_id = entity_ids[0]
        if entity_id == "sensor.curtailment":
            return {entity_id: [_FakeState("on", now)]}
        return {entity_id: [_FakeState("2.5", now)]}

    coordinator_module.get_significant_states = MagicMock(
        side_effect=_get_significant_states
    )


def test_retrain_fetches_all_six_optional_features_without_crashing():
    """The real bug: this used to raise
    `AttributeError: 'NimbusCoordinator' object has no attribute
    '_async_fetch_history'` the moment it hit the temp_events fetch,
    before train_model() (here, _train_model_job) was ever called.
    """
    _install_significant_states()
    coord = _make_coordinator()

    captured: dict[str, list] = {}

    def _fake_train_model_job(
        load_events,
        temp_events,
        humidity_events,
        curtailment_events,
        start,
        end,
        schedule_start_hour,
        schedule_end_hour,
        battery_events,
        grid_events,
        solar_events,
    ):
        captured["load_events"] = load_events
        captured["temp_events"] = temp_events
        captured["humidity_events"] = humidity_events
        captured["curtailment_events"] = curtailment_events
        captured["battery_events"] = battery_events
        captured["grid_events"] = grid_events
        captured["solar_events"] = solar_events

    coordinator_module._train_model_job = _fake_train_model_job

    asyncio.run(coord._async_retrain())

    # The real assertion: every one of the six previously-broken call sites
    # actually fetched real data, not an empty list from a swallowed crash.
    for key in (
        "load_events",
        "temp_events",
        "humidity_events",
        "curtailment_events",
        "battery_events",
        "grid_events",
        "solar_events",
    ):
        assert captured.get(key), f"{key} was empty -- fetch never ran"

    # Curtailment's binary encoding came through correctly too.
    assert captured["curtailment_events"] == [
        (datetime(2026, 8, 31, 0, 0, tzinfo=UTC), 1.0)
    ]
