"""Reload-instance regression tests for the two Solver push sensors.

Every config-entry reload runs `async_unload_entry` -> `async_setup_
entry` in sequence. The setup path unconditionally creates fresh
`NimbusSolverBatteryForecastSensor` and `NimbusHouseholdLoadTotal-
ForecastSensor` instances and calls `async_add_entities([...])`. HA is
supposed to tear down the OLD instances via `async_unload_platforms`
(which calls `async_will_remove_from_hass` on each entity) before the
new ones land -- but if that hook silently doesn't run for these
entities (missing platform in `PLATFORMS`, an error swallowed inside
`async_will_remove_from_hass`, or a stub in HA machinery that never
adopted the new lifecycle), two live instances would end up sharing
the same `entity_id`. Both would have their own periodic recheck
timer registered, both would try to publish state. The one that never
received a real push would publish `native_value=None` (rendered as
`unknown`) every recheck tick, clobbering whatever the fresh instance
just wrote -- exactly the empirical trace observed in issue #85
(state flip number -> unknown, ~2s apart, matching the solve+recheck
offset).

This file locks in the invariant: after a reload, exactly ONE entity
instance drives each well-known entity_id. Directly catches the
current leading hypothesis for #85 and any future regression of the
same shape.

Uses `pytest-homeassistant-custom-component` for the same reason
tests/test_flap_regression_state_stability.py does: instance identity
across reloads requires the real HA lifecycle to run, and the stub
tree deliberately doesn't model that.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.nimbus_load import solver_writer
from custom_components.nimbus_load.const import DOMAIN

_BATTERY_FORECAST_ENTITY_ID = "sensor.nimbus_solver_battery_forecast"
_LOAD_TOTAL_FORECAST_ENTITY_ID = "sensor.nimbus_household_load_total_forecast"


# `enable_custom_integrations` is applied automatically via
# tests/hass_integration/conftest.py's autouse fixture.


@pytest.fixture
async def nimbus_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Nimbus (reload regression)", data={}, options={}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _live_instances_for(hass: HomeAssistant, entity_id: str) -> list:
    """Enumerate all live entity Python objects that claim this
    entity_id, via HA's own entity_platform machinery. This is exactly
    the observability #85 needs -- if the value ever comes back != 1,
    the stale-instance hypothesis is confirmed.

    Walks every loaded entity platform (each config entry has its own
    set of platforms) and collects every entity whose `entity_id`
    string matches. Uses `id()` equality on the returned list to
    confirm distinct Python objects, not just distinct registry rows.
    """
    matches = []
    for domain_platforms in hass.data.get("entity_platform", {}).values():
        for platform in domain_platforms:
            for entity in platform.entities.values():
                if entity.entity_id == entity_id:
                    matches.append(entity)
    return matches


def _registered_handler_for(entity_id: str):
    """The current dispatch-table handler for this entity_id, or None
    if unregistered. Instance identity of `handler.__self__` is the
    other independent probe of "which entity is live" -- if it points
    at a torn-down instance, ha_post_state will route pushes to a
    ghost.
    """
    handler = solver_writer._ENTITY_UPDATE_HANDLERS.get(entity_id)
    return handler


# --- Regression Test 1: exactly one instance after setup -------------------


async def test_exactly_one_instance_per_push_entity_after_initial_setup(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
):
    """Baseline: on a fresh setup with no reloads, each well-known
    entity_id is driven by exactly one Python object. Any regression
    that duplicates entities during setup (e.g. adding both entities
    twice via a code copy-paste, or adding them via two different
    platforms) fails here first.
    """
    for entity_id in (
        _BATTERY_FORECAST_ENTITY_ID,
        _LOAD_TOTAL_FORECAST_ENTITY_ID,
    ):
        instances = _live_instances_for(hass, entity_id)
        assert len(instances) == 1, (
            f"{entity_id} has {len(instances)} live entity instances "
            f"after initial setup, expected 1: {instances}"
        )


# --- Regression Test 2: exactly one instance after a reload ----------------


async def test_exactly_one_instance_per_push_entity_after_reload(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
):
    """The current #85 hypothesis in one assertion.

    Trigger a real config-entry reload (which internally runs the
    full `async_unload_entry` -> `async_setup_entry` pair, same code
    path as `_async_update_listener` triggers on an options save).
    Then count live entity instances per entity_id.

    If a stale second instance survives, its own periodic recheck
    timer will publish `native_value=None` every 60s and clobber the
    fresh instance's pushes -- the exact observed symptom in the
    live install.
    """
    # Capture pre-reload instance identities so we can prove the new
    # ones are actually NEW (defence against a false-pass where the
    # test happens to count the same instance twice via two platforms).
    pre_reload_ids = {
        entity_id: id(_live_instances_for(hass, entity_id)[0])
        for entity_id in (
            _BATTERY_FORECAST_ENTITY_ID,
            _LOAD_TOTAL_FORECAST_ENTITY_ID,
        )
    }

    # A real reload -- same code path as options-save-triggered
    # `_async_update_listener` -> `hass.config_entries.async_reload`.
    assert await hass.config_entries.async_reload(nimbus_entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in (
        _BATTERY_FORECAST_ENTITY_ID,
        _LOAD_TOTAL_FORECAST_ENTITY_ID,
    ):
        instances = _live_instances_for(hass, entity_id)
        assert len(instances) == 1, (
            f"{entity_id} has {len(instances)} live entity instances "
            f"after ONE reload. Expected 1. Stale instances found: "
            f"{[id(i) for i in instances]}, pre-reload was "
            f"{pre_reload_ids[entity_id]}. This is the #85 stale-"
            "instance regression -- async_will_remove_from_hass "
            "didn't tear down the old entity before setup added a "
            "new one."
        )
        assert id(instances[0]) != pre_reload_ids[entity_id], (
            f"{entity_id} instance identity did NOT change across a "
            "reload -- either the test isn't triggering a real "
            "reload, or the entity is being reused across setups "
            "(which would also be wrong -- reload MUST produce a "
            "fresh instance)."
        )


async def test_exactly_one_instance_survives_many_reloads(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
):
    """A stronger version -- five reloads in a row must not leak. If
    each reload leaks one instance, this test finds five extras after
    the fifth call; the single-reload test above might pass on a bug
    that only shows up on the second reload (e.g. some cleanup path
    that runs the first time but is idempotent-skipped the second).
    """
    for i in range(5):
        assert await hass.config_entries.async_reload(nimbus_entry.entry_id), (
            f"Reload #{i + 1} returned False"
        )
        await hass.async_block_till_done()

    for entity_id in (
        _BATTERY_FORECAST_ENTITY_ID,
        _LOAD_TOTAL_FORECAST_ENTITY_ID,
    ):
        instances = _live_instances_for(hass, entity_id)
        assert len(instances) == 1, (
            f"{entity_id} has {len(instances)} live instances after "
            f"5 reloads -- {len(instances) - 1} leaked."
        )


# --- Regression Test 3: dispatch handler follows the live instance ---------


async def test_dispatch_handler_points_at_live_instance_after_reload(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
):
    """Independent probe of the same invariant, from the OTHER side.

    `solver_writer._ENTITY_UPDATE_HANDLERS[entity_id]` is a bound
    method whose `__self__` is one specific entity instance. After a
    reload, that instance must be the currently-live one -- otherwise
    every real solve push routes to a ghost instance that isn't in
    hass.states and whose async_write_ha_state is a no-op.

    Confirmed via the empirical trace in #85: pushes visibly reached
    the state machine (recorder logs the >16 KB warning every solve),
    which means the handler was routing correctly at least for the
    push -- but if a SECOND stale instance's recheck timer is what
    clobbers the value, this test still passes and the state-stability
    test is what catches it. This test catches the OTHER shape of the
    same bug: handler pointing at a torn-down entity.
    """
    assert await hass.config_entries.async_reload(nimbus_entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in (
        _BATTERY_FORECAST_ENTITY_ID,
        _LOAD_TOTAL_FORECAST_ENTITY_ID,
    ):
        handler = _registered_handler_for(entity_id)
        assert handler is not None, (
            f"No dispatch handler for {entity_id} after reload -- "
            "`register_entity_handler` wasn't called on the new "
            "instance, or unregister ran too late."
        )
        # `handler.__self__` is the bound instance for `update_from_
        # solver`; identity-check it against the live entity list.
        live = _live_instances_for(hass, entity_id)
        assert len(live) == 1
        assert handler.__self__ is live[0], (
            f"Dispatch handler for {entity_id} points at a torn-"
            "down entity instance -- pushes will silently no-op."
        )


# --- Regression Test 4: pushes still work after a reload -------------------


async def test_state_stays_stable_after_reload(
    hass: HomeAssistant, nimbus_entry: MockConfigEntry
):
    """End-to-end proof: a real push after a reload must land AND
    stay stable across a recheck tick. If a stale second instance
    exists, the recheck-tick assertion here fails the same way
    tests/test_flap_regression_state_stability.py does -- but this
    variant specifically exercises the reload path first, which is
    exactly the scenario #85 lives in on the live install.
    """
    assert await hass.config_entries.async_reload(nimbus_entry.entry_id)
    await hass.async_block_till_done()

    solver_writer.ha_post_state(
        _BATTERY_FORECAST_ENTITY_ID,
        3.14,
        {
            "unit_of_measurement": "kW",
            "friendly_name": "Nimbus Solver Battery Forecast",
            "forecast": [{"time": "t0", "value": 1.0}],
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get(_BATTERY_FORECAST_ENTITY_ID)
    assert state is not None
    assert state.state == "3.14"

    # One recheck cycle -- the tick that clobbers on the live install.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    state = hass.states.get(_BATTERY_FORECAST_ENTITY_ID)
    assert state is not None
    assert state.state != STATE_UNKNOWN, (
        "Post-reload recheck clobbered the fresh push -- the #85 "
        "regression signature."
    )
    assert state.state == "3.14"
