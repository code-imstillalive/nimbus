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
    # test_should_poll_is_false: nimbus issue #302: this class has no update()/async_update() at
    assert sensor._NimbusSolverPushSensor._attr_should_poll is False

    # test_a_second_fresh_push_resets_staleness
    instance = _construct()
    instance.hass = None
    instance.update_from_solver(1.5, {"forecast": []})
    instance._last_updated = time.monotonic() - (instance._STALE_AFTER_SECONDS + 1)
    assert instance.available is False
    # A real solve landing again must genuinely recover the entity, not
    # leave it permanently stuck once it's gone stale once.
    instance.update_from_solver(2.5, {"forecast": []})
    assert instance.available is True

    # test_update_from_solver_is_marked_hass_callback: Real, live-breaking regression (issue #82, found on Mark Purcell's
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

    # test_recheck_availability_is_marked_hass_callback: Same real bug class, second instance: _async_recheck_availability
    assert (
        getattr(
            sensor.NimbusSolverBatteryForecastSensor._async_recheck_availability,
            "_hass_callback",
            False,
        )
        is True
    )


# --- available: pre-first-solve is "unknown", not "unavailable" -----------


# --- available: fresh push is available ------------------------------------


# --- available: staleness threshold ----------------------------------------


# --- log-when-unavailable: transition logging, not per-tick ----------------


# --- issue #83: the recheck must NEVER write on a no-op tick --------------
# --- (Mark Purcell's real v0.73.1 flap: unconditional async_write_ha_    --
# --- state() on every tick raced against real pushes on a 60s cadence,   --
# --- publishing native_value=None/"unknown" whenever the recheck landed  --
# --- between two solves.) ---------------------------------------------


# --- async_added_to_hass: the real self-driven re-check registration ------


# --- issue #82: both hass.add_job()/async_track_time_interval targets  ----
# --- must genuinely be event-loop-safe (@callback), not just "work"    ----
# --- under a stub that doesn't model real HA's thread-dispatch logic.  ----
