"""Real regression test for a real, live-found bug (2026-08-23): a Solver
settings wizard field can be genuinely, correctly SAVED into
entry.options by flows/hub_options.py (present in _SOLVER_WIZARD_SCHEMA_
KEYS) while NimbusSolverConfigSensor (sensor.py) silently never exposes
it at all, because it was never added to _SOLVER_ALL_KEYS. Since
nimbus_solver_forecast_writer.py's fetch_solver_config() reads config
ONLY through this sensor's own attributes (config_entries.options isn't
exposed over HA's plain REST API -- see that function's own docstring),
a field missing from _SOLVER_ALL_KEYS is invisible to the writer no
matter how many times a household resubmits the wizard. Real, live
symptom this caused: solver_load_forecast_entities/solver_whole_house_
cross_check_sensor both showed correctly pre-filled in the wizard on
reopen (proof entry.options genuinely had the data) while sensor.
nimbus_solver_config's own attributes showed them as None/missing,
every single time, across multiple genuine resubmissions.

This test asserts every key flows/hub_options.py's wizard can actually
SAVE also appears in sensor.py's own _SOLVER_ALL_KEYS -- so a future
field added to one but not the other fails a test immediately, instead
of silently reproducing this exact multi-hour live debugging session.

Imports and exercises the REAL constants (not a reimplementation)
against tests/_ha_stubs.py's stand-in homeassistant.* modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import number, sensor
from custom_components.nimbus_load.const import (
    CONF_HOUSEHOLD_MODE,
    CONF_SOLVE_ON_PRICE_CHANGE,
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    CONF_SOLVER_CALIBRATED_OBJECTIVE_ENABLED,
    CONF_SOLVER_DISPATCH_DRY_RUN,
    CONF_SOLVER_FLEX_SIGNALS_ENABLED,
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_OFFER_CURVE_ENABLED,
    CONF_SOLVER_PRICE_SPIKE_OVERRIDE_ARMED,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
)
from custom_components.nimbus_load.flows.hub_options import (
    _SOLVER_WIZARD_SCHEMA_KEYS,
)


def test_every_wizard_saveable_key_is_exposed_by_the_bridge_sensor():
    missing = [
        key for key in _SOLVER_WIZARD_SCHEMA_KEYS if key not in sensor._SOLVER_ALL_KEYS
    ]
    assert missing == [], (
        f"{missing} can be saved by the Solver wizard but are never exposed by "
        "NimbusSolverConfigSensor -- fetch_solver_config() (the writer script's "
        "only channel to read config) can never see these fields regardless of "
        "how many times the wizard is resubmitted. Add them to sensor.py's own "
        "_SOLVER_ALL_KEYS."
    )


def test_the_two_specific_fields_from_the_real_2026_08_23_incident_are_present():
    assert CONF_SOLVER_LOAD_FORECAST_ENTITIES in sensor._SOLVER_ALL_KEYS
    assert CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR in sensor._SOLVER_ALL_KEYS


def test_every_number_entity_key_is_resolved_as_a_live_number_not_entry_options():
    """nimbus issue #538's own fix added two brand new number.py fields
    (CONF_SOLVER_SOC_DISCREPANCY_MAX_THRESHOLD_PCT/_MEAN_THRESHOLD_PCT).
    Auditing this exact "saved in one place, silently unreachable from
    the writer" mistake class (same shape as the real 2026-08-23
    incident above, just for a live number entity instead of a
    wizard-only field) for every OTHER number.py field while adding
    them found two real, pre-existing, live bugs: CONF_SOLVER_FIXED_
    DAILY_CHARGE and CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS were
    both missing from _SOLVER_NUMBER_ENTITY_KEYS (and from
    _SOLVER_ALL_KEYS entirely) -- any household adjusting "Fixed Daily
    Charge" or "Post-Window Self-Consume Hours" from the dashboard had
    ZERO effect on the actual solve, silently, forever. Both fixed
    alongside this test. No test previously guarded this pairing at
    all; if a future number.py field is added to _DESCRIPTIONS but the
    matching sensor.py entry is missed, this fails immediately instead
    of the field silently always resolving from (missing) entry.options
    via sensor.py's own _resolve() fallback.

    Deliberately NOT asserting every number.py key also appears in
    _SOLVER_ALL_KEYS: CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S is a real,
    legitimate exception -- __init__.py reads that one live entity
    directly via hass.states.get(), never through fetch_solver_config(),
    so its absence from the bridge sensor's own output is intentional,
    not a bug.
    """
    all_number_keys = [desc.key for desc in number._DESCRIPTIONS]
    missing_from_number_entity_keys = [
        key for key in all_number_keys if key not in sensor._SOLVER_NUMBER_ENTITY_KEYS
    ]
    assert missing_from_number_entity_keys == [], (
        f"{missing_from_number_entity_keys} are real number.nimbus_solver_* "
        "entities (number.py's own _DESCRIPTIONS) but sensor.py's _resolve() "
        "would read them from entry.options instead of the live entity -- "
        "add them to sensor.py's own _SOLVER_NUMBER_ENTITY_KEYS."
    )


def test_every_switch_entity_key_solver_writer_reads_via_cfg_is_resolved_live():
    """nimbus issue #496 (Signals 7/7 of #489), found live on devhub
    2026-09-14: switch.py gained CONF_SOLVER_FLEX_SIGNALS_ENABLED, wired
    into solver_writer.py's main() via cfg.get("solver_flex_signals_
    enabled"), but never added to sensor.py's own _SOLVER_SWITCH_ENTITY_
    KEYS -- the exact same "saved/toggled in one place, silently
    unreachable from the writer" bug class test_every_number_entity_
    key_is_resolved... above already guards for number.py, just never
    extended to switch.py. Real live symptom: the switch flipped on,
    three real solve cycles completed, sensor.nimbus_flex_signals stayed
    "unknown" the entire time -- compute_signals was silently always
    False regardless of the switch's real state.

    Unlike number.py, switch.py has no single declarative _DESCRIPTIONS
    list to iterate automatically (each NimbusSolverSwitch is
    instantiated individually in async_setup_entry) -- this hardcodes
    the current real set of switch keys solver_writer.py's own cfg.get()
    calls actually read, same explicit-list convention this file's own
    test_the_two_specific_fields_from_the_real_2026_08_23_incident_are_
    present already uses. CONF_SOLVER_DISPATCH_DRY_RUN is a real,
    legitimate exception (same shape as CONF_SOLVE_ON_PRICE_CHANGE_
    DEBOUNCE_S's own documented exception above): its own consumer reads
    the live switch entity directly, never through fetch_solver_config()
    -- confirmed via a direct grep, zero cfg.get("solver_dispatch_dry_
    run") call sites exist in solver_writer.py.
    """
    keys_solver_writer_reads_via_cfg = (
        CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
        CONF_SOLVE_ON_PRICE_CHANGE,
        CONF_SOLVER_OFFER_CURVE_ENABLED,
        CONF_SOLVER_FLEX_SIGNALS_ENABLED,
        CONF_SOLVER_PRICE_SPIKE_OVERRIDE_ARMED,
        CONF_SOLVER_CALIBRATED_OBJECTIVE_ENABLED,
    )
    missing = [
        key
        for key in keys_solver_writer_reads_via_cfg
        if key not in sensor._SOLVER_SWITCH_ENTITY_KEYS
    ]
    assert missing == [], (
        f"{missing} are switches solver_writer.py's main() reads via "
        "cfg.get(...) but sensor.py's _SOLVER_SWITCH_ENTITY_KEYS doesn't "
        "resolve them from the live entity -- the switch would be a "
        "real, silent no-op no matter what a household sets it to. Add "
        "them to sensor.py's own _SOLVER_SWITCH_ENTITY_KEYS."
    )
    # And the reverse: DISPATCH_DRY_RUN staying OUT is deliberate, not a
    # second copy of the same bug in the other direction -- assert it
    # explicitly so a future refactor that adds it can't silently pass
    # this test while actually being redundant/wrong.
    assert CONF_SOLVER_DISPATCH_DRY_RUN not in sensor._SOLVER_SWITCH_ENTITY_KEYS


def test_household_mode_select_key_is_resolved_live_and_exposed():
    """nimbus issue #485: the select equivalent of the switch guard
    above, written at the same time as the entity rather than after an
    incident.

    v0.94.283 shipped a switch that was a silent no-op for exactly this
    reason -- added to switch.py, wired into solver_writer.py's main()
    via cfg.get(...), but never added to sensor.py's own resolution
    tables, so fetch_solver_config() never saw it and the solve read
    False forever no matter what the household set. A new entity DOMAIN
    is the highest-risk moment for repeating that, because both tables
    have to be updated and neither failure is visible at runtime --
    the entity works perfectly, it just never reaches the solve.

    Both halves are asserted separately: presence in _SOLVER_ALL_KEYS
    (or fetch_solver_config() omits the key entirely) and presence in
    _SOLVER_SELECT_ENTITY_KEYS (or it resolves from entry.options,
    which for an entity-backed value is always None).
    """
    assert CONF_HOUSEHOLD_MODE in sensor._SOLVER_ALL_KEYS, (
        "CONF_HOUSEHOLD_MODE is missing from _SOLVER_ALL_KEYS, so "
        "fetch_solver_config() will never expose it and the solve-time "
        "resolver has nothing to read."
    )
    assert CONF_HOUSEHOLD_MODE in sensor._SOLVER_SELECT_ENTITY_KEYS, (
        "CONF_HOUSEHOLD_MODE is in _SOLVER_ALL_KEYS but not "
        "_SOLVER_SELECT_ENTITY_KEYS, so it resolves from entry.options "
        "instead of the live select entity -- always None, a silent "
        "no-op exactly like the v0.94.283 switch incident."
    )
