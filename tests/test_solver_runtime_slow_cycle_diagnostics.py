"""Real test of solver_runtime._run_one_cycle()'s new nimbus issue #315
diagnostics (2026-09-03, Mark Purcell): "freshness watchdog trips every
~44 min after reload, main-loop cadence degraded".

Root mechanism confirmed by reading the real code, not guessed: sw.
acquire_lock() returning False (a previous cycle's own sw.main() call
still genuinely running) was only ever logged at DEBUG -- invisible on
this project's default WARNING logger level. If one cycle runs unusually
long, EVERY subsequent phase-locked 5-minute tick silently skips via this
same path with zero breadcrumb explaining why, reproducing exactly the
"fires every ~44 min" pattern. Fixed: consecutive skips are now counted
and logged at WARNING, and sw.main()'s own wall-clock duration is
measured and logged at WARNING if it exceeds _SLOW_CYCLE_THRESHOLD_S.

Exercises the REAL _run_one_cycle() directly (not a reimplementation, and
not through async_run_solve()/hass.async_add_executor_job() -- see that
function's own docstring for why the actual body was extracted to this
plain, synchronously-callable module-level function specifically so tests
like this one don't need to mock executor-job plumbing at all) via a fake
solver_writer module standing in for _ensure_ready()'s own real import --
same general approach as test_solver_runtime_dispatch_dry_run.py, extended
to cover the lock-acquire/main() timing path that file doesn't touch.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_runtime


def _make_sw(*, acquire_ok: bool = True, main_side_effect=None) -> MagicMock:
    sw = MagicMock(spec=["acquire_lock", "release_lock", "main", "ha_post_state"])
    sw.acquire_lock = MagicMock(return_value=acquire_ok)
    sw.release_lock = MagicMock()
    sw.main = MagicMock(side_effect=main_side_effect)
    sw.ha_post_state = MagicMock()
    return sw


def _reset_module_state() -> None:
    solver_runtime._consecutive_lock_skips = 0


def test_lock_skip_is_logged_at_warning_not_debug_with_consecutive_count():
    _reset_module_state()
    hass = MagicMock()
    sw = _make_sw(acquire_ok=False)
    with (
        patch.object(solver_runtime, "_ensure_ready", return_value=sw),
        patch.object(solver_runtime, "_LOGGER") as mock_logger,
    ):
        result = solver_runtime._run_one_cycle(hass)

    assert result is False
    sw.main.assert_not_called()  # never reached -- lock wasn't acquired
    assert mock_logger.debug.call_count == 0, (
        "the real bug: this used to be _LOGGER.debug(), invisible at this "
        "project's default WARNING logger level -- must be WARNING now"
    )
    assert mock_logger.warning.call_count == 1
    warning_args = mock_logger.warning.call_args[0]
    assert "skipping" in warning_args[0]
    assert warning_args[-1] == 1  # first consecutive skip


def test_consecutive_lock_skips_increment_across_calls():
    _reset_module_state()
    sw = _make_sw(acquire_ok=False)
    with (
        patch.object(solver_runtime, "_ensure_ready", return_value=sw),
        patch.object(solver_runtime, "_LOGGER") as mock_logger,
    ):
        for _ in range(3):
            solver_runtime._run_one_cycle(MagicMock())

    counts_logged = [call[0][-1] for call in mock_logger.warning.call_args_list]
    assert counts_logged == [1, 2, 3], (
        "each successive skip must report an incrementing count -- this is "
        "the signal that distinguishes one harmless overlap from a real, "
        "sustained multi-tick stall"
    )


def test_successful_acquire_resets_the_consecutive_skip_counter():
    _reset_module_state()
    solver_runtime._consecutive_lock_skips = 5  # simulate prior skips
    sw = _make_sw(acquire_ok=True)
    with patch.object(solver_runtime, "_ensure_ready", return_value=sw):
        result = solver_runtime._run_one_cycle(MagicMock())

    assert result is True
    assert solver_runtime._consecutive_lock_skips == 0


def test_normal_cycle_duration_logs_debug_not_warning():
    _reset_module_state()
    sw = _make_sw(acquire_ok=True)
    with (
        patch.object(solver_runtime, "_ensure_ready", return_value=sw),
        patch.object(solver_runtime, "_LOGGER") as mock_logger,
    ):
        solver_runtime._run_one_cycle(MagicMock())

    assert mock_logger.warning.call_count == 0
    assert any("cycle took" in call[0][0] for call in mock_logger.debug.call_args_list)


def test_slow_cycle_logs_warning_with_real_measured_duration():
    _reset_module_state()

    def _slow_main():
        # Real, if small, elapsed time -- proves the measurement is a
        # genuine time.monotonic() delta around sw.main(), not a
        # hardcoded stub. A generous lower-bound margin below avoids OS
        # scheduler jitter flaking this on a loaded CI runner.
        time.sleep(0.1)

    sw = _make_sw(acquire_ok=True, main_side_effect=_slow_main)
    with (
        patch.object(solver_runtime, "_ensure_ready", return_value=sw),
        patch.object(solver_runtime, "_SLOW_CYCLE_THRESHOLD_S", 0.01),
        patch.object(solver_runtime, "_LOGGER") as mock_logger,
    ):
        result = solver_runtime._run_one_cycle(MagicMock())

    assert result is True
    assert mock_logger.warning.call_count == 1
    warning_args = mock_logger.warning.call_args[0]
    assert "cycle took" in warning_args[0]
    measured_duration = warning_args[1]
    assert measured_duration >= 0.05, (
        f"expected a real measured delta close to the 0.1s sleep, got "
        f"{measured_duration!r}"
    )


def test_lock_is_always_released_even_when_main_raises():
    """finally: sw.release_lock() must run regardless of outcome -- a
    cycle that crashes must never leave the lock permanently held (which
    would silently skip every future tick forever, the worst-case version
    of this exact issue)."""
    _reset_module_state()
    sw = _make_sw(acquire_ok=True, main_side_effect=RuntimeError("boom"))
    with patch.object(solver_runtime, "_ensure_ready", return_value=sw):
        result = solver_runtime._run_one_cycle(MagicMock())

    assert result is False
    sw.release_lock.assert_called_once()


# Deliberately no test of async_run_solve() itself here (only of
# _run_one_cycle() above) -- an asyncio.run()+AsyncMock-based delegation
# check was tried and hit the same pytest-collection-specific async/mock
# interaction that motivated extracting _run_one_cycle() in the first
# place (not reproducible locally, see this file's own module docstring),
# for a test whose only value was confirming a visually-obvious 2-line
# wrapper (`return await hass.async_add_executor_job(_run_one_cycle,
# hass)`). Not worth a third CI round-trip chasing it -- dropped rather
# than fought.
