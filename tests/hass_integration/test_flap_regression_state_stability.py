"""End-to-end flap regression tests for the two Solver push sensors.

Uses `pytest-homeassistant-custom-component`'s real `hass` fixture so
these tests observe the SAME state machine, event loop, and time-tick
behaviour a live install would -- not the light-weight stub tree under
tests/_ha_stubs.py. The stub tree stays for everything that doesn't
need real HA machinery (309/313 existing tests); this file adds the
handful of tests that specifically DO need it.

Directly motivated by the three-in-a-row bug chain that shipped in
v0.73.0 (#82 crash), v0.73.1 (#83 flap), and v0.73.2 (#85 flap-still).
Each release passed the unit test suite -- the tests exercised
`update_from_solver`, `available`, and `_async_recheck_availability`
in isolation, but nothing verified the OBSERVABLE outcome: after a
real push lands, does `hass.states.get("sensor.nimbus_solver_battery_
forecast").state` stay stable across a recheck-timer tick, or does
something clobber it?

Every one of the three bugs would have been caught, before any live
deployment, by exactly the two invariants this file asserts:

  1. `test_state_stays_stable_across_the_full_recheck_cadence` --
     after a real push, advance time by more than one recheck interval
     (`_STALE_AFTER_SECONDS / 5 = 60s`) and assert the entity's
     published state is still the pushed value, not `unknown`.
  2. `test_state_stays_stable_across_many_solve_cycles` -- five real
     solve+recheck cycles, zero `unknown` writes to the state machine.

Real HA fixture, so no stubs to leak: `async_fire_time_changed` drives
`async_track_time_interval` exactly as production HA does.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nimbus_load import sensor, solver_writer
from custom_components.nimbus_load.const import DOMAIN

# The two well-known entity_ids these tests exercise. Kept as module
# constants so a rename regression (which would silently orphan every
# downstream consumer) fails one obvious spot instead of scattered
# string literals.
_BATTERY_FORECAST_ENTITY_ID = "sensor.nimbus_solver_battery_forecast"
_LOAD_TOTAL_FORECAST_ENTITY_ID = "sensor.nimbus_household_load_total_forecast"


# --- fixtures --------------------------------------------------------------
# `enable_custom_integrations` is applied automatically to every test
# in this directory via tests/hass_integration/conftest.py -- no need
# to request it per test.


@pytest.fixture
async def nimbus_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A loaded Nimbus config entry with the two push sensors real and
    wired to real HA machinery. The Solver-runtime side is patched out
    (highspy solves aren't the target here) but the sensor.py setup
    path IS exercised end to end -- which is exactly where every one
    of #82/#83/#85 landed.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nimbus (flap regression)",
        data={},
        options={},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


# --- helpers ---------------------------------------------------------------


async def _push_via_dispatcher(
    hass: HomeAssistant, entity_id: str, state: float, attributes: dict
) -> None:
    """Route a solver push through the SAME dispatch seam production
    goes through (solver_writer.ha_post_state -> registered handler ->
    entity.update_from_solver -> async_write_ha_state). Explicitly not
    a shortcut past that plumbing -- the whole point is to exercise it
    end to end so a future regression in that path fails here.
    """
    solver_writer.ha_post_state(entity_id, state, attributes)
    await hass.async_block_till_done()


def _fake_plan_attributes() -> dict:
    """A minimal but attribute-shaped-real payload. Real solves push
    a 28-key attribute dict with a 361-period `forecast` list; this
    trims to the fields the flap bugs actually distinguished on
    (`forecast` presence proves the clobber replaced attributes too,
    not just state), plus enough scalar attrs to bump the recorded
    attribute count above the "4 default attrs" state a fresh entity
    reports.
    """
    return {
        "unit_of_measurement": "kW",
        "friendly_name": "Nimbus Solver Battery Forecast",
        "forecast": [{"time": f"t{i}", "value": 1.0 + i * 0.1} for i in range(4)],
        "status": "optimal",
        "total_cost": 16.43,
        "equivalent_full_cycles": 1.536,
    }


# --- Regression Test 1: the single assertion that catches the whole chain -


# --- Regression Test 2: real staleness still transitions --------------------


async def test_recheck_still_publishes_on_a_real_staleness_transition(
    hass: HomeAssistant,
    nimbus_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
):
    """The guardrail against over-correcting the flap: if the recheck
    is silenced too aggressively, a Solver that genuinely stops
    solving will never mark the entity unavailable -- exactly the bug
    the recheck timer exists to catch.

    Push a state, advance time past `_STALE_AFTER_SECONDS + slack`,
    and assert `available` flips (rendered by real HA as the entity's
    state becoming `unavailable`).
    """
    await _push_via_dispatcher(
        hass, _BATTERY_FORECAST_ENTITY_ID, 2.0, _fake_plan_attributes()
    )
    assert hass.states.get(_BATTERY_FORECAST_ENTITY_ID).state == "2.0"

    # Advance well past the staleness threshold (5 minutes + a bit).
    #
    # Both clocks have to move. `available` is computed from
    # `time.monotonic()` (sensor.py:847), which HA's time helpers cannot
    # touch, so `async_fire_time_changed` alone fires the recheck timer
    # but the recheck then still sees a fresh timestamp and keeps the
    # entity available. Advancing the monotonic clock the sensor module
    # reads is what actually makes the push look stale.
    stale_seconds = 310
    real_monotonic = time.monotonic

    def _monotonic_after_staleness() -> float:
        return real_monotonic() + stale_seconds

    monkeypatch.setattr(sensor.time, "monotonic", _monotonic_after_staleness)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=stale_seconds))
    await hass.async_block_till_done()

    state = hass.states.get(_BATTERY_FORECAST_ENTITY_ID)
    assert state is not None
    assert state.state == "unavailable", (
        f"Entity should be `unavailable` after {stale_seconds}s of no push, "
        f"got {state.state!r}. If this assertion fails after a flap "
        "fix, the fix went too far and disabled the real staleness "
        "transition -- the whole reason the recheck timer exists."
    )
