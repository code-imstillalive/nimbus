"""Regression test for nimbus issue #365 (Mark Purcell, codebase review),
item 6: `_run_one_cycle()` used to have no specific handling for the
shaped `urllib.error.HTTPError(404)` `ha_get()`'s native branch raises
when `sensor.nimbus_solver_config` doesn't exist yet -- a genuine,
expected startup race between the startup-solve task (`__init__.py`) and
the `sensor` platform's own `async_setup_entry` finishing. It fell
through to the generic `except Exception: _LOGGER.exception(...)`,
dumping a full traceback on every startup-retry attempt until the sensor
platform caught up, even though this is a real, self-recovering, expected
condition, not a genuine solve failure.

Also corrects a real misreading in this project's own CLAUDE.md history:
the original theory ("`_NATIVE_HASS` genuinely isn't registered yet") is
not what the code does -- `_ensure_ready()` always calls
`set_native_hass()` before `sw.main()` runs, on every attempt including
the first. The 404 is real and correctly reported; it just means this ONE
entity doesn't exist yet, not that native mode itself is unregistered.

Exercises the REAL `_run_one_cycle()` directly, same fake-`solver_writer`
convention as `test_solver_runtime_slow_cycle_diagnostics.py`.
"""

from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_runtime


def _config_sensor_404() -> urllib.error.HTTPError:
    # Byte-identical shape to solver_writer.py's own _native_http_error()
    # -- a real, well-formed HTTPError, not a synthetic ad-hoc exception.
    msg = "Entity sensor.nimbus_solver_config not found"
    return urllib.error.HTTPError(
        url="native://sensor.nimbus_solver_config",
        code=404,
        msg=msg,
        hdrs=None,
        fp=io.BytesIO(msg.encode("utf-8")),
    )


def _make_sw(*, main_side_effect=None) -> MagicMock:
    sw = MagicMock(spec=["acquire_lock", "release_lock", "main", "ha_post_state"])
    sw.acquire_lock = MagicMock(return_value=True)
    sw.release_lock = MagicMock()
    sw.main = MagicMock(side_effect=main_side_effect)
    sw.ha_post_state = MagicMock()
    return sw


def _reset_module_state() -> None:
    solver_runtime._consecutive_lock_skips = 0


class TestStartupConfigSensor404IsHandledCleanly:
    def test_config_sensor_404_logs_one_warning_not_a_traceback(self):
        _reset_module_state()
        hass = MagicMock()
        sw = _make_sw(main_side_effect=_config_sensor_404())
        with (
            patch.object(solver_runtime, "_ensure_ready", return_value=sw),
            patch.object(solver_runtime, "_LOGGER") as mock_logger,
        ):
            result = solver_runtime._run_one_cycle(hass)

        assert result is False
        mock_logger.exception.assert_not_called()
        mock_logger.warning.assert_called_once()
        message = mock_logger.warning.call_args[0][0]
        assert "sensor.nimbus_solver_config" in message
        assert "not registered yet" in message

    def test_a_different_404_still_gets_the_full_traceback(self):
        # A 404 for some OTHER entity is a genuinely different situation
        # (not the specific, known, self-recovering startup race this
        # fix targets) -- must still surface as a real, loud exception,
        # not be silently swallowed by an over-broad match.
        _reset_module_state()
        hass = MagicMock()
        other_404 = urllib.error.HTTPError(
            url="native://sensor.some_other_entity",
            code=404,
            msg="Entity sensor.some_other_entity not found",
            hdrs=None,
            fp=io.BytesIO(b"not found"),
        )
        sw = _make_sw(main_side_effect=other_404)
        with (
            patch.object(solver_runtime, "_ensure_ready", return_value=sw),
            patch.object(solver_runtime, "_LOGGER") as mock_logger,
        ):
            result = solver_runtime._run_one_cycle(hass)

        assert result is False
        mock_logger.exception.assert_called_once()

    def test_a_500_for_the_config_sensor_itself_still_gets_the_full_traceback(self):
        # Only the specific (404, this exact url) combination is treated
        # as the known startup race -- any other status code for the
        # same entity is a real, different failure.
        _reset_module_state()
        hass = MagicMock()
        real_error = urllib.error.HTTPError(
            url="native://sensor.nimbus_solver_config",
            code=500,
            msg="boom",
            hdrs=None,
            fp=io.BytesIO(b"boom"),
        )
        sw = _make_sw(main_side_effect=real_error)
        with (
            patch.object(solver_runtime, "_ensure_ready", return_value=sw),
            patch.object(solver_runtime, "_LOGGER") as mock_logger,
        ):
            result = solver_runtime._run_one_cycle(hass)

        assert result is False
        mock_logger.exception.assert_called_once()
