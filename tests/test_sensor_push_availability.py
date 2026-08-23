"""Silver `entity-unavailable` / `log-when-unavailable` coverage for
_NimbusSolverPushSensor -- the sibling NimbusForecastSensor already had
this fix (2026-08-22) via CoordinatorEntity's own automatic re-poll-on-
a-schedule behaviour; the two Solver push sensors (Battery Forecast,
Household Load Total Forecast) had no coordinator at all and no
`available` override, so a Solver that stopped solving would leave them
confidently reporting a stale plan forever. Fixed 2026-08-23.

Real correctness point this file specifically locks in, not just "the
property returns the right bool": HA's state machine only reflects
`available`'s current value when something calls
`async_write_ha_state()` -- a pure elapsed-time check with NO periodic
self-driven re-evaluation would never actually transition a real,
already-published entity to unavailable once the Solver genuinely stops
calling update_from_solver() (there'd be nothing left to trigger a
fresh read of the property at all). `async_added_to_hass()` registering
its own `async_track_time_interval` timer is what closes that gap --
tested directly below, not just the `available` property in isolation.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: E402, F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs  # noqa: E402

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor  # noqa: E402


def _fake_entry(entry_id: str = "test-entry-abc") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _construct(cls=sensor.NimbusSolverBatteryForecastSensor):
    entry = _fake_entry()
    instance = cls.__new__(cls)
    cls.__init__(instance, entry, sw_version="0.73.0")
    return instance


# --- available: pre-first-solve is "unknown", not "unavailable" -----------


def test_available_is_true_before_any_solve_has_ever_happened():
    """No push yet -- native_value is None (HA shows "unknown"), and
    available must stay True. "Unavailable" specifically means "this
    entity's data source is broken"; "hasn't started yet" is a
    genuinely different, more honest signal HA already has a state for
    (unknown), so this must not be conflated with real staleness."""
    instance = _construct()
    assert instance.native_value is None
    assert instance.available is True


# --- available: fresh push is available ------------------------------------


def test_available_is_true_immediately_after_a_fresh_push():
    instance = _construct()
    instance.hass = None  # update_from_solver must not require hass
    instance.update_from_solver(1.5, {"forecast": []})
    assert instance.available is True


# --- available: staleness threshold ----------------------------------------


def test_available_becomes_false_once_stale_after_seconds_elapses():
    instance = _construct()
    instance.hass = None
    instance.update_from_solver(1.5, {"forecast": []})
    assert instance.available is True
    # Simulate real elapsed time without an actual sleep -- back-date
    # the recorded timestamp past the real threshold.
    instance._last_updated = time.monotonic() - (instance._STALE_AFTER_SECONDS + 1)
    assert instance.available is False


def test_available_stays_true_just_under_the_threshold():
    """A boundary check, not just "eventually goes false" -- confirms
    the comparison is a real elapsed-time check against the documented
    constant, not an off-by-one or a different accidental threshold."""
    instance = _construct()
    instance.hass = None
    instance.update_from_solver(1.5, {"forecast": []})
    instance._last_updated = time.monotonic() - (instance._STALE_AFTER_SECONDS - 5)
    assert instance.available is True


def test_a_second_fresh_push_resets_staleness():
    instance = _construct()
    instance.hass = None
    instance.update_from_solver(1.5, {"forecast": []})
    instance._last_updated = time.monotonic() - (instance._STALE_AFTER_SECONDS + 1)
    assert instance.available is False
    # A real solve landing again must genuinely recover the entity, not
    # leave it permanently stuck once it's gone stale once.
    instance.update_from_solver(2.5, {"forecast": []})
    assert instance.available is True


# --- log-when-unavailable: transition logging, not per-tick ----------------


def test_recheck_logs_on_genuine_transition_to_unavailable(caplog):
    import logging

    instance = _construct()
    instance.hass = None
    instance.async_write_ha_state = MagicMock()
    instance.update_from_solver(1.5, {"forecast": []})
    instance._was_available = True  # simulate "already known available"

    instance._last_updated = time.monotonic() - (instance._STALE_AFTER_SECONDS + 1)
    with caplog.at_level(logging.WARNING):
        instance._async_recheck_availability(now=None)
    assert instance._was_available is False
    assert any(
        "has not received a fresh Solver plan" in r.message for r in caplog.records
    )
    instance.async_write_ha_state.assert_called_once()


def test_recheck_does_not_log_again_on_a_second_still_unavailable_tick():
    """The whole point of log-when-unavailable -- once genuinely
    unavailable, repeated re-checks (this timer fires every
    _STALE_AFTER_SECONDS / 5) must NOT spam a warning every tick, only
    on the initial transition."""
    instance = _construct()
    instance.hass = None
    instance.async_write_ha_state = MagicMock()
    instance.update_from_solver(1.5, {"forecast": []})
    instance._last_updated = time.monotonic() - (instance._STALE_AFTER_SECONDS + 1)
    instance._was_available = True

    logged: list[str] = []
    import logging

    class _Capture(logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())

    handler = _Capture()
    sensor._LOGGER.addHandler(handler)
    sensor._LOGGER.setLevel(logging.WARNING)
    try:
        instance._async_recheck_availability(now=None)  # transition -> logs once
        instance._async_recheck_availability(now=None)  # still unavailable -> silent
        instance._async_recheck_availability(now=None)  # still unavailable -> silent
    finally:
        sensor._LOGGER.removeHandler(handler)

    unavailable_logs = [m for m in logged if "has not received a fresh" in m]
    assert len(unavailable_logs) == 1


def test_recheck_logs_recovery_on_transition_back_to_available():
    instance = _construct()
    instance.hass = None
    instance.async_write_ha_state = MagicMock()
    instance.update_from_solver(1.5, {"forecast": []})
    instance._was_available = False  # simulate "was previously stale"

    logged: list[str] = []
    import logging

    class _Capture(logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())

    handler = _Capture()
    sensor._LOGGER.addHandler(handler)
    sensor._LOGGER.setLevel(logging.INFO)
    try:
        instance._async_recheck_availability(now=None)
    finally:
        sensor._LOGGER.removeHandler(handler)

    assert any("is available again" in m for m in logged)
    assert instance._was_available is True


# --- async_added_to_hass: the real self-driven re-check registration ------


def test_async_added_to_hass_registers_a_removable_periodic_timer():
    """The real correctness point this whole fix depends on: without a
    self-driven re-check, `available`'s new logic would only ever be
    consulted on the next real solve -- which never comes once the
    Solver genuinely stops. async_on_remove must have been called with
    something the entity can clean up on removal."""
    instance = _construct()
    instance.hass = MagicMock()
    asyncio.run(instance.async_added_to_hass())
    assert hasattr(instance, "_on_remove_callbacks")
    assert len(instance._on_remove_callbacks) == 1
    # The registered callback is async_track_time_interval's own return
    # value (a MagicMock stand-in for the real unsub callable) -- must
    # itself be callable, matching the real HA contract.
    assert callable(instance._on_remove_callbacks[0])


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            if "caplog" in t.__code__.co_varnames[: t.__code__.co_argcount]:
                continue  # caplog fixture only runs under pytest
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
