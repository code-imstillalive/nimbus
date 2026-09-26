"""IV&V finding (4fd40c3..22205ad pass, 2026-09-26, head issue #1289, this
finding #1296): `_run_one_cycle()`'s `except RuntimeError` handler
(`solver_runtime.py:662-667`) logs `sensor.nimbus_solver_config`'s "Nimbus
Solver is not configured yet" message at WARNING **unconditionally, every
time it is raised** -- with no tiering at all.

That is inconsistent with how this exact file already treats two sibling
"expected, self-healing startup condition" cases:

* The neighbouring `except urllib.error.HTTPError` branch a few lines below
  (nimbus #365 item 6, pinned by
  `tests/test_solver_runtime_startup_config_sensor_race.py`) exists
  specifically because a startup race between the timer and the `sensor`
  platform finishing setup is expected and must not be reported as a real
  failure.
* `_run_one_cycle()`'s own lock-skip handling (nimbus #945, same file,
  lines ~619-654) explicitly downgrades a single, self-healing overlap to
  DEBUG and only escalates to WARNING once the condition has genuinely
  persisted ("a persistent skip/succeed alternation is not harmless even
  though each skip is").

The RuntimeError path has no equivalent. `sensor.py`'s own #85 comment
names the exact false-positive this produces: a `number.nimbus_solver_*`
entity that is briefly `unknown` during `RestoreEntity` on startup makes
`sensor.nimbus_solver_config` report `state != "configured"` for one tick,
which self-heals on the very next cycle -- yet today's flat WARNING treats
that identically to a genuinely unconfigured install that will never
recover on its own. This is the "#757/#773 signal buried in noise" pattern
this same file's own #945 fix was written to avoid, recurring in its
untouched sibling branch.

Reuses the exact `_make_sw`/`_ensure_ready` patching harness already
established by `test_solver_runtime_startup_config_sensor_race.py` for the
neighbouring branch.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_runtime


def _not_configured_error() -> RuntimeError:
    return RuntimeError(
        "Nimbus Solver is not configured yet. Open the Nimbus hub's own "
        '"Configure" button in Home Assistant, choose "Solver settings", '
        "and fill in every required field before running this writer."
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


class TestASelfHealingNotConfiguredRaceDoesNotWarn(unittest.TestCase):
    @pytest.mark.xfail(
        reason="nimbus #1296: the RuntimeError('not configured yet') handler "
        "in _run_one_cycle() warns unconditionally on every occurrence, with "
        "no tiering for a single self-healing tick -- unlike this same "
        "file's own #365 HTTPError race handling and #945 lock-skip tiering, "
        "both written for exactly this class of expected, transient startup "
        "condition (see sensor.py's #85 comment for the exact race that "
        "produces a one-tick 'unconfigured' false positive)",
        strict=True,
    )
    def test_a_single_immediately_self_healing_occurrence_is_not_a_warning(self):
        _reset_module_state()
        hass = MagicMock()
        sw = _make_sw(main_side_effect=_not_configured_error())
        with (
            patch.object(solver_runtime, "_ensure_ready", return_value=sw),
            patch.object(solver_runtime, "_LOGGER") as mock_logger,
        ):
            result = solver_runtime._run_one_cycle(hass)

        assert result is False
        mock_logger.warning.assert_not_called()
        mock_logger.debug.assert_called()


if __name__ == "__main__":
    unittest.main()
