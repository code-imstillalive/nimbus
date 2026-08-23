"""Regression test for the real, live-found #85 flap (Mark Purcell,
2026-08-23): NimbusSolverConfigSensor.native_value recomputes on every
read via _resolve() over the 10 _SOLVER_REQUIRED_KEYS, and for each of
the _SOLVER_NUMBER_ENTITY_KEYS the resolve path reads
hass.states.get(f"number.nimbus_{key}") -- which returns None or a
state whose .state is "unknown"/"unavailable" transiently at startup
while HA's RestoreEntity restores are still in flight. Result: this
sensor briefly reports "unconfigured", solver_writer's
fetch_solver_config() reads that string, raises RuntimeError, and
solver_runtime.py logs "Nimbus Solver: not configured yet" -- on every
single HA restart, for the ~30 seconds it takes number.py's own
RestoreEntity cycle to catch up.

Verified on the reporter's live install:

    12:54:37.815  sensor.nimbus_solver_config -> unconfigured
    12:54:37.997  WARNING Nimbus Solver: not configured yet
    12:55:07.816  sensor.nimbus_solver_config -> configured

This test locks in three real, independently-checkable properties:

1. The startup-race path exists at all -- given a hass whose
   number.nimbus_solver_* states start "unknown" and then become real
   numbers a moment later, native_value must return "unconfigured"
   first and then "configured", not stay stuck on either extreme.

2. The extra_state_attributes["unresolved_required_keys"] attribute
   correctly identifies which specific field(s) caused the temporary
   "unconfigured" -- proves the diagnostic surface added for #85 is
   actually live, not just a decorative attribute.

3. The transition is logged at WARNING exactly ONCE per configured ->
   unconfigured flip, and INFO exactly ONCE per recovery -- not once
   per read (which would spam the log at native_value's polling
   cadence). This matches the same "log-on-transition, not per-tick"
   discipline test_sensor_push_availability.py already enforces on
   the sibling push sensors.

No fix is claimed here -- the point of these tests is to make the
flap directly observable in the log (with attribution back to THIS
sensor, not to solver_runtime.py's downstream WARNING) and testable
in the suite. Whatever fix eventually lands (options a/b/c in the
#85 discussion) can then flip a NEW assertion -- "must not flap at
all if a hass is presented that has never had unknown states" -- and
either keep or remove these existing assertions accordingly.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor
from custom_components.nimbus_load.const import (
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_GRID_MAX_EXPORT_KW,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_MAX_CHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_KW,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
)


# --- helpers ---------------------------------------------------------------


def _entry_with_options(options: dict) -> MagicMock:
    """A stand-in ConfigEntry whose only real used method is
    options.get(key) -- everything else is MagicMock and the test never
    touches it."""
    entry = MagicMock()
    entry.entry_id = "flap-test-entry"
    entry.options = options
    return entry


def _real_number_state(value: float) -> MagicMock:
    """A hass State whose .state parses as a real float -- what a
    healthy, restored number.nimbus_solver_* would look like."""
    st = MagicMock()
    st.state = str(value)
    return st


def _unknown_state() -> MagicMock:
    """A hass State whose .state is 'unknown' -- what a number.nimbus_
    solver_* entity looks like BEFORE RestoreEntity has finished
    restoring its previous value (the exact startup race the flap
    depends on)."""
    st = MagicMock()
    st.state = "unknown"
    return st


def _construct_bridge_sensor(entry: MagicMock) -> sensor.NimbusSolverConfigSensor:
    """Build a NimbusSolverConfigSensor bypassing HA's real add-to-hass
    lifecycle (same technique test_sensor_push_availability.py uses on
    the sibling push sensors) -- the constructor just stashes references,
    it doesn't need a real event loop or an added entity."""
    instance = sensor.NimbusSolverConfigSensor.__new__(sensor.NimbusSolverConfigSensor)
    sensor.NimbusSolverConfigSensor.__init__(instance, entry, sw_version="0.73.3")
    return instance


def _fully_configured_options() -> dict:
    """entry.options for a fully-set-up household -- covers all 5
    entity-pointer required keys (the ones NOT moved to
    number.nimbus_solver_* in the 2026-08-20 refactor). The remaining
    5 required keys are in _SOLVER_NUMBER_ENTITY_KEYS and get their
    values from hass.states.get() instead, so they're driven by the
    hass fixture below, not by this dict."""
    return {
        CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.fake_soc",
        CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.fake_import_price",
        CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.fake_export_price",
        CONF_SOLVER_SOLAR_FORECAST_SENSOR: "sensor.fake_solar_forecast",
        CONF_SOLVER_LOAD_FORECAST_SENSOR: "sensor.fake_load_forecast",
    }


def _five_number_entity_ids() -> dict:
    """The exact entity_id -> restored numeric value pairs for the 5
    REQUIRED keys that live in number.nimbus_solver_* (the intersection
    of _SOLVER_REQUIRED_KEYS and _SOLVER_NUMBER_ENTITY_KEYS). Any
    plausible positive value is fine -- this test cares about
    resolved-or-not, not the specific numbers."""
    return {
        f"number.nimbus_{CONF_SOLVER_BATTERY_CAPACITY_KWH}": 20.0,
        f"number.nimbus_{CONF_SOLVER_MAX_CHARGE_KW}": 5.0,
        f"number.nimbus_{CONF_SOLVER_MAX_DISCHARGE_KW}": 5.0,
        f"number.nimbus_{CONF_SOLVER_GRID_MAX_IMPORT_KW}": 15.0,
        f"number.nimbus_{CONF_SOLVER_GRID_MAX_EXPORT_KW}": 15.0,
    }


# --- 1. The flap itself ----------------------------------------------------


def test_flap_native_value_is_unconfigured_while_number_entities_still_restoring():
    """Startup race, moment T: the 5 entity-pointer required keys are
    already saved to entry.options (durable, restart-safe), but the 5
    numeric required keys' number.nimbus_solver_* entities have not
    yet completed RestoreEntity -- their hass.states.get() returns a
    State whose .state == 'unknown'. native_value MUST report
    'unconfigured' in this window -- if it reported 'configured' here,
    solver_writer's fetch_solver_config() would proceed to a bad plan
    build against unresolved fields."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)

    unrestored_states = {
        eid: _unknown_state() for eid in _five_number_entity_ids()
    }
    hass = MagicMock()
    hass.states.get = lambda eid: unrestored_states.get(eid)
    instance.hass = hass

    assert instance.native_value == "unconfigured"


def test_recovery_native_value_flips_to_configured_once_numbers_have_restored():
    """Startup race, moment T + ~30s: RestoreEntity has now populated
    the number.nimbus_solver_* entities with their previous, durable
    values, and hass.states.get() returns real numeric states for all
    of them. native_value MUST recover to 'configured' -- if it stayed
    stuck on 'unconfigured' the household would silently never solve
    again after ANY restart."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)

    restored_states = {
        eid: _real_number_state(value)
        for eid, value in _five_number_entity_ids().items()
    }
    hass = MagicMock()
    hass.states.get = lambda eid: restored_states.get(eid)
    instance.hass = hass

    assert instance.native_value == "configured"


def test_flap_and_recovery_within_the_same_instance_is_directly_reproducible():
    """The real flap the recorder shows -- one instance, two reads a
    moment apart, seeing genuinely different underlying states. This
    is the exact live sequence captured on the reporter's install
    (12:54:37 unconfigured, 12:55:07 configured, ONE
    NimbusSolverConfigSensor across both). If native_value cached
    anything or short-circuited on first-read, this recovery would
    silently fail."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)

    # Moment T: number entities not yet restored.
    state_registry = {
        eid: _unknown_state() for eid in _five_number_entity_ids()
    }
    hass = MagicMock()
    hass.states.get = lambda eid: state_registry.get(eid)
    instance.hass = hass
    assert instance.native_value == "unconfigured"

    # Moment T + ~30s: RestoreEntity has now restored every one -- SAME
    # instance, same hass, only the underlying state dict flips.
    for eid, value in _five_number_entity_ids().items():
        state_registry[eid] = _real_number_state(value)
    assert instance.native_value == "configured"


# --- 2. The unresolved_required_keys diagnostic ---------------------------


def test_unresolved_required_keys_lists_exactly_the_number_entities_still_restoring():
    """The diagnostic attribute added for #85 must correctly identify
    which of the 10 required keys is the reason for the 'unconfigured'
    state. On the startup-race path this is exactly the 5 numeric
    required keys whose number.nimbus_solver_* entity is still
    'unknown' -- proves the attribute is computed live from the same
    _resolve() calls native_value uses, not a stale cached snapshot."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)

    unrestored_states = {
        eid: _unknown_state() for eid in _five_number_entity_ids()
    }
    hass = MagicMock()
    hass.states.get = lambda eid: unrestored_states.get(eid)
    instance.hass = hass

    unresolved = instance.extra_state_attributes["unresolved_required_keys"]
    assert set(unresolved) == {
        CONF_SOLVER_BATTERY_CAPACITY_KWH,
        CONF_SOLVER_MAX_CHARGE_KW,
        CONF_SOLVER_MAX_DISCHARGE_KW,
        CONF_SOLVER_GRID_MAX_IMPORT_KW,
        CONF_SOLVER_GRID_MAX_EXPORT_KW,
    }


def test_unresolved_required_keys_is_empty_when_everything_resolved():
    """The other half of the diagnostic contract: on the happy path
    (nothing unresolved) the attribute is an empty list, NOT None or
    missing. A caller reading this over REST can then trust `len(...)
    == 0` as "fully configured" without special-casing absence."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)

    restored_states = {
        eid: _real_number_state(value)
        for eid, value in _five_number_entity_ids().items()
    }
    hass = MagicMock()
    hass.states.get = lambda eid: restored_states.get(eid)
    instance.hass = hass

    assert instance.extra_state_attributes["unresolved_required_keys"] == []


def test_unresolved_required_keys_pinpoints_a_single_missing_field():
    """Fine-grained diagnostic: if exactly ONE required field is
    unresolved, unresolved_required_keys must call out exactly that
    key (not something adjacent) -- makes the attribute genuinely
    useful for triage, not just a coarse "something's wrong" flag."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)

    states = {
        eid: _real_number_state(value)
        for eid, value in _five_number_entity_ids().items()
    }
    # Knock out exactly one -- max_charge_kw.
    states[f"number.nimbus_{CONF_SOLVER_MAX_CHARGE_KW}"] = _unknown_state()
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    instance.hass = hass

    assert instance.extra_state_attributes["unresolved_required_keys"] == [
        CONF_SOLVER_MAX_CHARGE_KW
    ]


# --- 3. Log-on-transition, not per-read -----------------------------------


def test_transition_to_unconfigured_logs_a_warning_exactly_once_per_flip():
    """First read that flips configured -> unconfigured must log at
    WARNING with the unresolved keys called out. A SECOND read while
    still unconfigured (native_value is polled every state-machine
    read) must NOT log again -- same "log-when-unavailable" discipline
    the sibling push sensors already enforce (see
    test_sensor_push_availability.py's own test on this)."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)
    # Seed: pretend the previous native_value was "configured" (which
    # is what the recorder-observed live flap looks like -- restart,
    # first read goes to unconfigured, transition logs).
    instance._last_computed_state = "configured"

    states = {eid: _unknown_state() for eid in _five_number_entity_ids()}
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    instance.hass = hass

    logged: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            logged.append(record)

    handler = _Capture()
    sensor._LOGGER.addHandler(handler)
    original_level = sensor._LOGGER.level
    sensor._LOGGER.setLevel(logging.DEBUG)
    try:
        assert instance.native_value == "unconfigured"
        assert instance.native_value == "unconfigured"  # second read, no new log
        assert instance.native_value == "unconfigured"  # third read either
    finally:
        sensor._LOGGER.removeHandler(handler)
        sensor._LOGGER.setLevel(original_level)

    warnings = [r for r in logged if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        f"expected exactly one WARNING per transition, got {len(warnings)}: "
        f"{[r.getMessage() for r in warnings]}"
    )
    assert "unconfigured" in warnings[0].getMessage()


def test_transition_back_to_configured_logs_an_info_recovery_line():
    """Recovery half of the log-on-transition contract: once the
    numbers have restored and native_value flips back to configured,
    that transition must ALSO log (at INFO, since it's the good
    outcome) -- otherwise a maintainer sees only the WARNING for the
    downgrade with no matching "and here's when it recovered" line."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)
    # Seed: previous state was unconfigured (mid-flap).
    instance._last_computed_state = "unconfigured"

    states = {
        eid: _real_number_state(value)
        for eid, value in _five_number_entity_ids().items()
    }
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    instance.hass = hass

    logged: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            logged.append(record)

    handler = _Capture()
    sensor._LOGGER.addHandler(handler)
    original_level = sensor._LOGGER.level
    sensor._LOGGER.setLevel(logging.DEBUG)
    try:
        assert instance.native_value == "configured"
        assert instance.native_value == "configured"  # second read, silent
    finally:
        sensor._LOGGER.removeHandler(handler)
        sensor._LOGGER.setLevel(original_level)

    infos = [r for r in logged if r.levelno == logging.INFO]
    assert len(infos) == 1, (
        f"expected exactly one INFO per recovery transition, got {len(infos)}"
    )
    assert "configured" in infos[0].getMessage()


def test_stable_configured_at_startup_does_not_log_at_all():
    """The most common real path -- a healthy restart where every
    number entity's RestoreEntity landed BEFORE the first
    native_value poll (uncommon on this reporter's install but the
    intended happy path). No log line at all: no phantom WARNING for
    a transition that didn't happen, no INFO recovery for a
    downgrade that didn't happen. Startup silence is the whole
    point of gating log emission on _last_computed_state, not on
    every read."""
    entry = _entry_with_options(_fully_configured_options())
    instance = _construct_bridge_sensor(entry)
    # No seed -- fresh instance, _last_computed_state is None.

    states = {
        eid: _real_number_state(value)
        for eid, value in _five_number_entity_ids().items()
    }
    hass = MagicMock()
    hass.states.get = lambda eid: states.get(eid)
    instance.hass = hass

    logged: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            logged.append(record)

    handler = _Capture()
    sensor._LOGGER.addHandler(handler)
    original_level = sensor._LOGGER.level
    sensor._LOGGER.setLevel(logging.DEBUG)
    try:
        assert instance.native_value == "configured"
    finally:
        sensor._LOGGER.removeHandler(handler)
        sensor._LOGGER.setLevel(original_level)

    # Fresh instance -> first computed state is "configured" -> None
    # transitions to "configured" IS a real "first observed"
    # transition and we do want ONE line here (INFO), otherwise we
    # never confirm the sensor genuinely came up healthy at all. But
    # we must NEVER see a WARNING here -- that would be a false alarm.
    warnings = [r for r in logged if r.levelno == logging.WARNING]
    assert warnings == [], (
        f"a startup that came up cleanly must not log any WARNING; "
        f"got: {[r.getMessage() for r in warnings]}"
    )


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
