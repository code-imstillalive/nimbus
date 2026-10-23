"""Regression test for nimbus issue #365 (Mark Purcell, codebase review):
_async_retrain() is scheduled via a bare hass.async_create_task() (see
async_setup()'s own docstring for why -- backgrounding the cold-start
retrain so it can't block hub setup) with no except clause at all --
only a bare `try: ... finally: self._retraining = False`. Any real
failure inside it (a bad history fetch, a training bug) used to surface
ONLY as an "unretrieved task exception" asyncio logs on its own,
completely invisible to health.py's own WARNING+ ring buffer -- exactly
how a real AttributeError (2026-08-31, see coordinator.py's own comment
on that incident) stayed silent for days while sensor.nimbus_health_
report kept reporting 0 errors.

This test drives the REAL _async_retrain() end-to-end (same technique as
test_coordinator_retrain_all_feature_sources.py) with the training job
itself forced to raise, and proves:
1. The exception never escapes _async_retrain() (it's scheduled via
   hass.async_create_task(), so an escaping exception would only ever
   surface as an invisible unretrieved-task warning).
2. It's logged via _LOGGER.exception() -- which reaches
   NimbusLogBufferHandler exactly the way every other real WARNING+ in
   this integration does.
3. self._retraining still resets to False afterward (the finally clause
   still runs) -- a stuck _retraining=True would silently block every
   future retrain attempt for this coordinator forever.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

import homeassistant.util as _ha_util

_ha_util.dt.as_local = lambda x: x
_ha_util.dt.utcnow = lambda: datetime.now(UTC)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import coordinator as coordinator_module
from custom_components.nimbus_load.const import CONF_LOAD_SENSOR, SUBENTRY_TYPE_LOAD
from custom_components.nimbus_load.coordinator import NimbusCoordinator
from custom_components.nimbus_load.ml.model import TrainedModel


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


def test_a_training_failure_is_logged_via_logger_exception():
    coord = _make_coordinator(training_job_raises=True)
    with patch.object(coordinator_module, "_LOGGER") as mock_logger:
        asyncio.run(coord._async_retrain())
    mock_logger.exception.assert_called_once()
    # The subentry_id must be identifiable in the log call so a real
    # household can tell WHICH load's retrain failed.
    assert "test-subentry-retrain-failure" in mock_logger.exception.call_args[0]


def test_retraining_flag_resets_to_false_even_after_a_failure():
    coord = _make_coordinator(training_job_raises=True)
    asyncio.run(coord._async_retrain())
    assert coord._retraining is False


def test_no_regression_for_the_success_path():
    coord = _make_coordinator(training_job_raises=False)
    with patch.object(coordinator_module, "_LOGGER") as mock_logger:
        asyncio.run(coord._async_retrain())
    mock_logger.exception.assert_not_called()
    assert coord._retraining is False


# --- nimbus issue #373: last_retrain_error, the operator-visible signal ----


def test_a_training_failure_sets_last_retrain_error():
    """The only operator-facing signal a retrain failure had before this
    was a log line -- last_update_success stays True regardless (it's
    THIS background task, not the coordinator's own update, that
    failed). Published on the forecast entity's own attributes (see
    _async_update_data()) so it's visible without reading the log."""
    coord = _make_coordinator(training_job_raises=True)
    asyncio.run(coord._async_retrain())
    assert coord._last_retrain_error is not None
    assert "simulated training failure" in coord._last_retrain_error


def test_a_successful_retrain_clears_a_prior_last_retrain_error():
    """A stale error attribute lingering after the underlying problem is
    actually fixed would be its own false alarm -- a genuine success
    must clear it. _train_model_job is patched to return a real
    TrainedModel directly (the fake single-point history this file's own
    harness provides is nowhere near the >= 500 points a genuine
    train_model() call requires) -- this test is about the clearing
    logic in _async_retrain() itself, not train_model()'s own behaviour,
    which has its own dedicated test coverage elsewhere."""
    coord = _make_coordinator(training_job_raises=False)
    coord._last_retrain_error = "RuntimeError: some earlier failure"
    fake_trained = TrainedModel(
        model_type="knn",
        x_mean=np.zeros(3),
        x_std=np.ones(3),
        x_train=np.zeros((5, 3)),
        y_train=np.zeros(5),
        gbrt=None,
        trained_at=datetime.now(UTC),
        training_points=5,
    )
    with patch.object(
        coordinator_module, "_train_model_job", return_value=fake_trained
    ):
        asyncio.run(coord._async_retrain())
    assert coord._last_retrain_error is None
    assert coord._trained is fake_trained
