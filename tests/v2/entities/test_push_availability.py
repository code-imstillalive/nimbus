"""The solver's push sensors go unavailable when the solver stops solving, and
log it once (#1837).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
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


# --- available: fresh push is available ------------------------------------


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


# --- issue #83: the recheck must NEVER write on a no-op tick --------------
# --- (Mark Purcell's real v0.73.1 flap: unconditional async_write_ha_    --
# --- state() on every tick raced against real pushes on a 60s cadence,   --
# --- publishing native_value=None/"unknown" whenever the recheck landed  --
# --- between two solves.) ---------------------------------------------


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
