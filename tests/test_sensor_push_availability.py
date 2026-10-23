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
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor


def _fake_entry(entry_id: str = "test-entry-abc") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _construct(cls=sensor.NimbusSolverBatteryForecastSensor):
    entry = _fake_entry()
    instance = cls.__new__(cls)
    cls.__init__(instance, entry, sw_version="0.73.0")
    return instance


# --- should_poll: must stay False -------------------------------------------


def test_should_poll_is_false():
    """nimbus issue #302: this class has no update()/async_update() at
    all -- it's a pure push entity, updated only via update_from_solver().
    Leaving should_poll at its default True meant HA's own default
    entity-platform scan interval (15s on a real installed core) polled
    it anyway, force-refreshing a push-only entity that never asked to
    be polled -- confirmed as the real cause of a live flap on the
    reference household's own NUC1 (sensor.nimbus_solver_quality_report
    alternating between its real value and a bare "unknown" every ~30s).

    nimbus issue #362 (Mark Purcell, codebase review) correctly flagged
    the fix's own code comment as misleadingly calling this "DIAG:
    temporary" when #302's own closed conclusion says the opposite --
    this must never be reverted or reintroduced as an actual temporary
    flag; this test locks in that it stays False permanently.
    """
    assert sensor._NimbusSolverPushSensor._attr_should_poll is False


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


# --- issue #83: the recheck must NEVER write on a no-op tick --------------
# --- (Mark Purcell's real v0.73.1 flap: unconditional async_write_ha_    --
# --- state() on every tick raced against real pushes on a 60s cadence,   --
# --- publishing native_value=None/"unknown" whenever the recheck landed  --
# --- between two solves.) ---------------------------------------------


def test_recheck_before_first_push_does_not_write_or_log():
    """The exact clobbering path from issue #83: a brand-new instance
    (no push has ever landed, self._state is None) whose very first
    recheck tick fires before update_from_solver() ever has. `available`
    is honestly True here (pre-first-solve, not "broken") -- there is no
    transition to report and, critically, nothing to correct in the
    state machine yet either. Zero writes, zero log lines.

    Real fix (issue #302, 2026-08-31): `_was_available` now starts at
    `True` in __init__ (matching what `available` already,
    definitionally, returns for a freshly-constructed instance), not a
    `None` sentinel -- see that assignment's own comment for the real,
    live bug this closes (a staleness transition that had ALREADY
    happened by the time of the very first recheck tick used to be
    silently swallowed as "just establishing a baseline"). This test's
    own outcome (zero writes on a genuinely unchanged first tick) is
    unaffected by that fix -- True still correctly equals True here --
    only the now-obsolete "was None, becomes True" intermediate
    assertion needed updating."""
    instance = _construct()
    instance.hass = None
    instance.async_write_ha_state = MagicMock()
    assert instance._was_available is True  # sanity: already the correct initial value

    instance._async_recheck_availability(now=None)

    assert instance._was_available is True  # unchanged -- nothing transitioned
    instance.async_write_ha_state.assert_not_called()


def test_recheck_is_a_noop_while_available_stays_unchanged():
    """The real flap scenario: a solve landed, the entity is fresh and
    available, and the periodic recheck ticks one or more times before
    the NEXT solve lands (this timer's whole point -- it runs 5x more
    often than the staleness threshold). None of those in-between ticks
    should ever touch the state machine."""
    instance = _construct()
    instance.hass = None
    instance.async_write_ha_state = MagicMock()
    instance.update_from_solver(1.5, {"forecast": []})

    instance._async_recheck_availability(now=None)  # baseline tick
    instance.async_write_ha_state.assert_not_called()

    instance._async_recheck_availability(now=None)  # still available -- no-op
    instance._async_recheck_availability(now=None)  # still available -- no-op
    instance.async_write_ha_state.assert_not_called()


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


# --- issue #82: both hass.add_job()/async_track_time_interval targets  ----
# --- must genuinely be event-loop-safe (@callback), not just "work"    ----
# --- under a stub that doesn't model real HA's thread-dispatch logic.  ----


def test_update_from_solver_is_marked_hass_callback():
    """Real, live-breaking regression (issue #82, found on Mark Purcell's
    real v0.73.0 install): update_from_solver() is invoked via
    solver_writer.ha_post_state()'s hass.add_job(functools.partial(
    handler, ...)) -- real HA's add_job() inspects its target for the
    _hass_callback marker to decide whether to run it directly on the
    event loop or dispatch it to the executor THREAD POOL. Undecorated,
    every single solve tick sent this method to a worker thread, where
    its own async_write_ha_state() call (genuinely requires the event
    loop) raised RuntimeError silently on every call -- both headline
    sensors stuck at `unknown` forever, no visible crash, just "Future
    exception was never retrieved" buried in the log.

    Checks the exact attribute (_hass_callback) real HA's own @callback
    decorator sets -- confirmed directly against HA core's current
    source, not guessed -- via the test stub's own now-faithful
    replica (see _ha_stubs.py's own comment on why a plain identity
    lambda was a real, proven gap here, not a harmless simplification).
    """
    assert (
        getattr(
            sensor.NimbusSolverBatteryForecastSensor.update_from_solver,
            "_hass_callback",
            False,
        )
        is True
    )
    assert (
        getattr(
            sensor.NimbusHouseholdLoadTotalForecastSensor.update_from_solver,
            "_hass_callback",
            False,
        )
        is True
    )


def test_recheck_availability_is_marked_hass_callback():
    """Same real bug class, second instance: _async_recheck_availability
    is registered directly as async_track_time_interval's own callback
    and also ends with async_write_ha_state() -- undecorated, this would
    have hit the identical crash on every single periodic re-check tick,
    in code that had never been live-tested before this fix (shipped the
    same session)."""
    assert (
        getattr(
            sensor.NimbusSolverBatteryForecastSensor._async_recheck_availability,
            "_hass_callback",
            False,
        )
        is True
    )
