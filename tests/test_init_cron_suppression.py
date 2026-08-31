"""Real test of issue #295's cron-suppression fix (Mark Purcell,
2026-08-31): the phase-locked periodic solve callback (_periodic_solve,
defined inside async_setup_entry -- not a top-level name, hence the
capture-via-patched-async_track_utc_time_change technique below) must
skip its own solve when a solve already completed within _CRON_SUPPRESS_
WINDOW_S seconds (any trigger source -- cron, price_change, or startup),
and must run normally otherwise: on a fresh install where nothing has
ever solved, and once the suppression window has genuinely elapsed.

Captures the REAL _periodic_solve callback the same way test_init_
periodic_solve_timer_idempotent.py already captures it for a different
assertion (idempotent registration, not suppression behaviour) -- via a
patched async_track_utc_time_change whose side_effect records the real
callback it was handed, then calling that callback directly. Exercises
the real function, not a reimplementation.
"""

import asyncio
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import custom_components.nimbus_load as nimbus_init
from custom_components.nimbus_load import solver_runtime


def _reset_solver_runtime_state() -> None:
    solver_runtime._last_solve_completed_monotonic = None
    solver_runtime._price_latency_sensor = None


def _make_entry(entry_id: str) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    # Empty subentries -- the coordinator-setup loop this test isn't
    # about never runs at all, same as test_init_periodic_solve_timer_
    # idempotent.py's own _make_entry().
    entry.subentries = {}
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    return entry


def _close_coro_task(coro, *_args, **_kwargs) -> MagicMock:
    # Same test-harness hygiene as test_init_periodic_solve_timer_
    # idempotent.py's own _close_coro_task() -- the startup-solve retry
    # task this test isn't exercising still gets scheduled by real
    # async_setup_entry(); closing (not awaiting) its coroutine avoids a
    # "coroutine was never awaited" warning without needing it to
    # actually run.
    coro.close()
    return MagicMock()


def _make_hass() -> MagicMock:
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=None)
    hass.async_create_task = MagicMock(side_effect=_close_coro_task)
    hass.async_create_background_task = MagicMock(side_effect=_close_coro_task)
    return hass


def _setup_and_fire_periodic_solve(entry_id: str, run_solve: AsyncMock) -> None:
    """Runs the real async_setup_entry() once, captures the real
    _periodic_solve closure it registers via async_track_utc_time_
    change, then invokes that captured closure -- ALL inside the same
    patch context, since solver_runtime.async_run_solve is only patched
    for the lifetime of the `with` block below (patch.object auto-
    restores on exit). Calling the captured closure AFTER this function
    returns would silently exercise the REAL async_run_solve() against a
    MagicMock hass, which fails with "object MagicMock can't be used in
    'await' expression" -- caught live writing this test.

    Every other real side effect (log buffer, service registration,
    frontend, integration lookup) is mocked out, matching test_init_
    periodic_solve_timer_idempotent.py's own _run_setup_entry_twice_
    for_same_entry() pattern (same reasoning documented there for
    patch.object() over direct module-attribute assignment: these are
    real, shared module objects across the whole pytest session).
    """
    entry = _make_entry(entry_id)
    hass = _make_hass()
    captured: dict[str, object] = {}

    def _capture(_hass, callback, **_kwargs):
        captured["callback"] = callback
        return MagicMock()

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(nimbus_init.health, "install_log_buffer_handler")
        )
        stack.enter_context(
            patch.object(nimbus_init.services, "async_register_services")
        )
        stack.enter_context(
            patch.object(
                nimbus_init.frontend,
                "async_register_frontend",
                new=AsyncMock(return_value=None),
            )
        )
        stack.enter_context(
            patch.object(
                nimbus_init,
                "async_get_integration",
                new=AsyncMock(return_value=MagicMock()),
            )
        )
        stack.enter_context(
            patch.object(nimbus_init.solver_runtime, "async_run_solve", run_solve)
        )
        stack.enter_context(
            patch.object(
                nimbus_init,
                "async_track_utc_time_change",
                new=MagicMock(side_effect=_capture),
            )
        )
        asyncio.run(nimbus_init.async_setup_entry(hass, entry))
        asyncio.run(captured["callback"](None))


def test_cron_runs_normally_on_a_fresh_install_with_nothing_solved_yet():
    _reset_solver_runtime_state()
    run_solve = AsyncMock(return_value=True)

    _setup_and_fire_periodic_solve("entry_cron_fresh", run_solve)

    run_solve.assert_called_once()
    # time_since_last_solve() was None going in, so the cron must run --
    # and a real, successful run must record its own completion so a
    # LATER cron tick (or this same one, if re-fired quickly) can be
    # correctly suppressed.
    assert solver_runtime.time_since_last_solve() is not None


def test_cron_is_suppressed_when_a_solve_completed_just_now():
    _reset_solver_runtime_state()
    # Simulate an event-driven (or a previous cron) solve that JUST
    # completed, well inside _CRON_SUPPRESS_WINDOW_S (60s) -- the exact
    # real-world shape issue #295 reports: a cron tick landing 5s after
    # an event-driven solve already produced the same plan.
    solver_runtime.record_solve_completed(trigger_source="cron")
    run_solve = AsyncMock(return_value=True)

    _setup_and_fire_periodic_solve("entry_cron_suppressed", run_solve)

    # The redundant cron tick must be skipped entirely -- not run and
    # discarded, not run at all.
    run_solve.assert_not_called()


def test_cron_runs_again_once_the_suppression_window_has_elapsed():
    _reset_solver_runtime_state()
    # A solve that completed well OUTSIDE the 60s suppression window --
    # e.g. the cron tick 5 minutes ago, or an event-driven solve much
    # earlier in the same 5-min block. Directly manipulating the
    # monotonic timestamp (rather than sleeping 61 real seconds) keeps
    # this test fast while exercising the exact same comparison the real
    # code performs.
    solver_runtime.record_solve_completed(trigger_source="cron")
    solver_runtime._last_solve_completed_monotonic = time.monotonic() - 61.0
    run_solve = AsyncMock(return_value=True)

    _setup_and_fire_periodic_solve("entry_cron_elapsed", run_solve)

    run_solve.assert_called_once()


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
