"""Real tests for _remediate_forecast_lts_unit() -- nimbus issue #263
(Mark Purcell): every subentry-created sensor.nimbus_*_forecast entity
triggers HA's "unit has changed" long-term-statistics repair dialog on
first restart after being seeded into LTS, even though the entity has
declared a real kW unit since v0.1.0.

Verified against real HA recorder internals (installed homeassistant
2025.1.4) before writing the fix this locks in -- NOT the issue's own
originally-sketched fix. `recorder.async_change_statistics_unit(...,
old_unit_of_measurement="")` was confirmed, by direct testing against
the real installed package, to raise `HomeAssistantError` immediately
(it calls `can_convert_units("", "kW")` first, which is `False` -- an
empty string has no known unit family to convert FROM). The real,
correct mechanism -- confirmed by reading
`homeassistant/components/recorder/websocket_api.py`'s own
`ws_update_statistics_metadata` handler, the literal code behind the
Statistics page's "change unit" fix button in HA's own UI -- is
`Recorder.async_update_statistics_metadata(new_unit_of_measurement=...)`,
a raw metadata relabel with no `can_convert_units` gate at all.

These tests exercise the REAL function (not a reimplementation)
against mocked `get_instance`/`get_metadata`, via this project's own
established `tests/_ha_stubs.py` stand-in `homeassistant.*` modules.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.sensor import _remediate_forecast_lts_unit

ENTITY_ID = "sensor.nimbus_sigen_plant_consumed_power_forecast"


def _make_recorder(metadata: dict, instant_done: bool = True) -> MagicMock:
    """A fake Recorder instance: async_add_executor_job just calls the
    given callable directly (no real thread pool needed for a test),
    and async_update_statistics_metadata synchronously invokes on_done
    -- matching this project's own already-proven "fire-and-forget
    completes instantly" test convention (test_sensor_push_availability.py)."""
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


def test_corrects_none_unit():
    hass = MagicMock()
    metadata = {ENTITY_ID: (123, {"unit_of_measurement": None})}
    recorder = _run_remediation(hass, metadata)
    recorder.async_update_statistics_metadata.assert_called_once()


def test_already_correct_unit_is_left_alone():
    hass = MagicMock()
    metadata = {ENTITY_ID: (123, {"unit_of_measurement": "kW"})}
    recorder = _run_remediation(hass, metadata)
    recorder.async_update_statistics_metadata.assert_not_called()


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
    hass = MagicMock()
    with patch(
        "homeassistant.components.recorder.get_instance",
        side_effect=RuntimeError("boom"),
    ):

        async def _go():
            hass.loop = asyncio.get_running_loop()
            await _remediate_forecast_lts_unit(hass, ENTITY_ID, "kW")  # must not raise

        asyncio.run(_go())


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
