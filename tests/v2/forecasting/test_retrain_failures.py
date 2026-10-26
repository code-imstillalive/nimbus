"""A failed background retrain is logged where the health ring buffer sees it
(#365) (#1837).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import homeassistant.util as _ha_util

_ha_util.dt.as_local = lambda x: x
_ha_util.dt.utcnow = lambda: datetime.now(UTC)

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load.const import CONF_LOAD_SENSOR, SUBENTRY_TYPE_LOAD
from custom_components.nimbus_load.coordinator import NimbusCoordinator


class _FakeState:
    def __init__(self, state: str, last_changed: datetime) -> None:
        self.state = state
        self.last_changed = last_changed
        self.attributes = {"unit_of_measurement": "kW"}


def _make_coordinator(*, training_job_raises: bool) -> NimbusCoordinator:
    coord = NimbusCoordinator.__new__(NimbusCoordinator)
    coord.hass = MagicMock()

    async def _run_generic_executor(func, *args, **kwargs):
        if training_job_raises and func is coordinator_module._train_model_job:
            raise RuntimeError("simulated training failure")
        return func(*args, **kwargs)

    coord.hass.async_add_executor_job = AsyncMock(side_effect=_run_generic_executor)

    async def _run_recorder_executor(func, *args, **kwargs):
        return func(*args, **kwargs)

    recorder_executor = MagicMock()
    recorder_executor.async_add_executor_job = AsyncMock(
        side_effect=_run_recorder_executor
    )
    coordinator_module.get_instance = MagicMock(return_value=recorder_executor)

    now = datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
    coordinator_module.get_significant_states = MagicMock(
        return_value={"sensor.load": [_FakeState("2.5", now)]}
    )

    coord.entry = MagicMock()
    coord.entry.options = {}
    coord.subentry = MagicMock()
    coord.subentry.subentry_id = "test-subentry-retrain-failure"
    coord.subentry.subentry_type = SUBENTRY_TYPE_LOAD
    coord.subentry.data = {CONF_LOAD_SENSOR: "sensor.load"}
    coord._retraining = False
    coord._trained = None
    coord._last_retrain_error = None
    coord._save_model_to_disk = MagicMock()
    coord.async_request_refresh = AsyncMock()
    return coord


def test_a_training_failure_does_not_escape_async_retrain():
    coord = _make_coordinator(training_job_raises=True)
    # Must not raise -- if it did, hass.async_create_task's own
    # scheduling of this coroutine would turn it into an invisible
    # "Task exception was never retrieved" warning instead.
    asyncio.run(coord._async_retrain())


# --- nimbus issue #373: last_retrain_error, the operator-visible signal ----
