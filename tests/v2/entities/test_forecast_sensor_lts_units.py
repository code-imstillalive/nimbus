"""A forecast sensor's long-term statistics unit is set right, so no unit-
changed repair appears (#263) (#1837).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.sensor import _remediate_forecast_lts_unit

ENTITY_ID = "sensor.nimbus_sigen_plant_consumed_power_forecast"


def _make_recorder(metadata: dict, instant_done: bool = True) -> MagicMock:
    """A fake Recorder instance: async_add_executor_job just calls the
    given callable directly (no real thread pool needed for a test),
    and async_update_statistics_metadata synchronously invokes on_done
    -- matching this project's own already-proven "fire-and-forget
    completes instantly" test convention (test_push_availability.py)."""
    recorder = MagicMock()

    async def _fake_executor_job(fn):
        return fn()

    recorder.async_add_executor_job = MagicMock(side_effect=_fake_executor_job)

    def _fake_update(entity_id, *, new_unit_of_measurement, on_done):
        if instant_done:
            on_done()

    recorder.async_update_statistics_metadata = MagicMock(side_effect=_fake_update)
    return recorder


def _run_remediation(hass, metadata: dict, instant_done: bool = True):
    recorder = _make_recorder(metadata, instant_done=instant_done)
    with (
        patch("homeassistant.components.recorder.get_instance", return_value=recorder),
        patch(
            "homeassistant.components.recorder.statistics.get_metadata",
            return_value=metadata,
        ),
    ):

        async def _go():
            hass.loop = asyncio.get_running_loop()
            await _remediate_forecast_lts_unit(hass, ENTITY_ID, "kW")

        asyncio.run(_go())
    return recorder


def test_no_lts_row_yet_does_nothing():
    # Brand-new entity, never statistics-eligible before -- nothing to fix.
    hass = MagicMock()
    recorder = _run_remediation(hass, metadata={})
    recorder.async_update_statistics_metadata.assert_not_called()


def test_corrects_empty_string_unit():
    hass = MagicMock()
    metadata = {ENTITY_ID: (123, {"unit_of_measurement": ""})}
    recorder = _run_remediation(hass, metadata)
    recorder.async_update_statistics_metadata.assert_called_once()
    args, kwargs = recorder.async_update_statistics_metadata.call_args
    assert args[0] == ENTITY_ID
    assert kwargs["new_unit_of_measurement"] == "kW"


def test_a_genuinely_different_real_unit_is_never_touched():
    # Real safety property: this function must only ever relabel a
    # genuinely EMPTY row, never reinterpret an existing real unit --
    # that would be a silent, wrong data reinterpretation, not a fix.
    hass = MagicMock()
    metadata = {ENTITY_ID: (123, {"unit_of_measurement": "W"})}
    recorder = _run_remediation(hass, metadata)
    recorder.async_update_statistics_metadata.assert_not_called()


def test_a_recorder_error_never_propagates():
    # This is a cosmetic, one-time cleanup -- it must never be capable
    # of blocking or crashing real entity setup, regardless of cause.
    #
    # nimbus issue #360 (Mark Purcell, codebase review): "must not raise"
    # alone is a blind assertion -- it passes just as well if the
    # function silently short-circuits before ever reaching get_instance
    # (e.g. a future refactor that accidentally returns early) as it
    # does for the real, intended behaviour of "genuinely tried, hit a
    # real error, swallowed it." Asserting get_instance was actually
    # called closes that gap.
    hass = MagicMock()
    with patch(
        "homeassistant.components.recorder.get_instance",
        side_effect=RuntimeError("boom"),
    ) as mock_get_instance:

        async def _go():
            hass.loop = asyncio.get_running_loop()
            await _remediate_forecast_lts_unit(hass, ENTITY_ID, "kW")  # must not raise

        asyncio.run(_go())
    mock_get_instance.assert_called_once_with(hass)
