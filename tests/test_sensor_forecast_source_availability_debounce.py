"""nimbus issue #740 (Mark Purcell, live finding, real household):
NimbusForecastSensor.available checked the source sensor's LIVE state at
the exact instant of each coordinator tick's write -- a single momentary
unavailable/unknown reading (an ordinary Zigbee/Modbus blip, not a real
disconnection) was enough to flip `available` False for that whole tick,
which strips ALL extra_state_attributes (including `forecast`) via HA's
own entity base class. Confirmed live: recurred 10 times across ~9 hours
of otherwise-normal overnight operation, each time knocking
sensor.nimbus_offer_curve/quality_report to "unknown" for a full tick
because solver_writer.py's own read landed in exactly that window.

Fix: _handle_coordinator_update() now tracks
_consecutive_unavailable_source_ticks, and `available` only goes False
once that counter reaches _SOURCE_UNAVAILABLE_DEBOUNCE_TICKS -- a single
bad tick no longer trips it, while a genuinely sustained disconnection
still does within a few ticks.

Imports and exercises the REAL class (not a reimplementation) against
tests/_ha_stubs.py's stand-in homeassistant.* modules, following the
exact construction pattern test_sensor_signal_role_attribute.py already
uses for this class.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor
from custom_components.nimbus_load.const import CONF_LOAD_SENSOR, SUBENTRY_TYPE_LOAD


def _fake_subentry(subentry_id: str = "load1") -> MagicMock:
    s = MagicMock()
    s.subentry_id = subentry_id
    s.subentry_type = SUBENTRY_TYPE_LOAD
    s.data = {CONF_LOAD_SENSOR: "sensor.sigen_plant_consumed_power"}
    s.title = "Test Load"
    return s


def _make_sensor() -> sensor.NimbusForecastSensor:
    coordinator = MagicMock()
    coordinator.data = {"forecast": [{"time": "t", "value": 1.0}]}
    coordinator.last_update_success = True
    instance = sensor.NimbusForecastSensor(
        coordinator, _fake_subentry(), sw_version="1.0.0"
    )
    instance.hass = MagicMock()
    instance.async_write_ha_state = MagicMock()
    return instance


def _set_source_state(instance, state: str | None) -> None:
    if state is None:
        instance.hass.states.get.return_value = None
    else:
        fake_state = MagicMock()
        fake_state.state = state
        instance.hass.states.get.return_value = fake_state


def test_available_before_any_tick_is_true():
    instance = _make_sensor()
    assert instance.available is True


def test_a_single_unavailable_tick_does_not_trip_availability():
    """The real #740 fix: one momentary blip must not knock this entity
    (and its `forecast` attribute) unavailable for the whole tick."""
    instance = _make_sensor()
    _set_source_state(instance, "unavailable")
    instance._handle_coordinator_update()
    assert instance.available is True
    assert instance.extra_state_attributes["forecast"]


def test_two_consecutive_unavailable_ticks_trips_availability():
    """A sustained disconnection (not just one blip) must still be
    caught -- this is the real failure mode the original Silver fix
    (2026-08-22) was built to close, and this debounce must not
    regress it into "confidently stale forever" territory."""
    instance = _make_sensor()
    _set_source_state(instance, "unavailable")
    instance._handle_coordinator_update()
    instance._handle_coordinator_update()
    assert instance.available is False


def test_unknown_state_behaves_the_same_as_unavailable():
    instance = _make_sensor()
    _set_source_state(instance, "unknown")
    instance._handle_coordinator_update()
    instance._handle_coordinator_update()
    assert instance.available is False


def test_missing_source_entity_behaves_the_same_as_unavailable():
    instance = _make_sensor()
    _set_source_state(instance, None)
    instance._handle_coordinator_update()
    instance._handle_coordinator_update()
    assert instance.available is False


def test_a_recovering_tick_between_two_blips_resets_the_counter():
    """Two SEPARATE single-tick blips, with a healthy tick in between,
    must never accumulate into a false trip -- only truly CONSECUTIVE
    bad ticks count."""
    instance = _make_sensor()
    _set_source_state(instance, "unavailable")
    instance._handle_coordinator_update()
    assert instance.available is True

    _set_source_state(instance, "on")
    instance._handle_coordinator_update()
    assert instance.available is True

    _set_source_state(instance, "unavailable")
    instance._handle_coordinator_update()
    assert instance.available is True  # still just one consecutive tick


def test_recovery_after_a_genuine_trip_is_immediate():
    """No debounce needed on the way back up -- a single healthy tick
    after a genuine (2+ tick) disconnection immediately restores
    availability, matching the pre-existing "no stale-forever" intent."""
    instance = _make_sensor()
    _set_source_state(instance, "unavailable")
    instance._handle_coordinator_update()
    instance._handle_coordinator_update()
    assert instance.available is False

    _set_source_state(instance, "on")
    instance._handle_coordinator_update()
    assert instance.available is True
    assert instance.extra_state_attributes["forecast"]


def test_coordinator_update_failure_still_overrides_regardless_of_counter():
    """A genuinely failed coordinator refresh must still win immediately
    -- the debounce counter only governs the source-sensor-liveness
    check, not this separate, pre-existing failure mode."""
    instance = _make_sensor()
    instance.coordinator.last_update_success = False
    _set_source_state(instance, "on")
    instance._handle_coordinator_update()
    assert instance.available is False
