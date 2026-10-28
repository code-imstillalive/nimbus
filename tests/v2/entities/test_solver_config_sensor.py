"""`sensor.nimbus_solver_config`: every Solver setting, read from its own
entity or the hub's options, in the form the solve uses (#1837).

Covers what lived in #85, #495, #954, #1296, #1466, the solver time
settings (11 Oct 2026) and the solver-config files.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.append(str(Path(__file__).resolve().parents[3]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sm = importlib.import_module("custom_components.nimbus_load.sensor")

NUMBERS = sorted(set(sm._SOLVER_REQUIRED_KEYS) & set(sm._SOLVER_NUMBER_ENTITY_KEYS))
OPTIONS = sorted(set(sm._SOLVER_REQUIRED_KEYS) - set(sm._SOLVER_NUMBER_ENTITY_KEYS))


class Registry:
    def __init__(self, renamed=None):
        self.renamed = renamed or {}

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.renamed.get(unique_id)


def _sensor(monkeypatch, states, *, options=None, renamed=None, subentries=None):
    monkeypatch.setattr(sm.er, "async_get", lambda hass: Registry(renamed))
    entry = SimpleNamespace(
        entry_id="hub1",
        options={key: f"sensor.{key}" for key in OPTIONS} | (options or {}),
        subentries=subentries or {},
    )
    s = sm.NimbusSolverConfigSensor(entry, "0.94.485")
    s.hass = SimpleNamespace(
        states=SimpleNamespace(
            get=lambda eid: states.get(eid), async_all=lambda domain: []
        )
    )
    return s


def _numbers(value="5"):
    return {f"number.nimbus_{key}": SimpleNamespace(state=value) for key in NUMBERS}


def test_a_setting_still_restoring_or_unreadable_is_unresolved(monkeypatch, caplog):
    # test_a_setting_still_restoring_or_unreadable_is_unresolved
    with monkeypatch.context() as mp:
        for value in ["unknown", "unavailable", "lots"]:
            with mp.context() as mp:
                states = _numbers()
                states[f"number.nimbus_{NUMBERS[0]}"] = SimpleNamespace(state=value)
                s = _sensor(mp, states)
                assert s.native_value == "unconfigured"
                assert s.extra_state_attributes["unresolved_required_keys"] == [
                    NUMBERS[0]
                ]

    # test_each_kind_of_setting_reads_in_the_form_the_solve_uses
    with monkeypatch.context() as mp:
        for domain, keys, state, value in [
            ("time", sm._SOLVER_TIME_ENTITY_KEYS, "05:30:00", 5.5),
            ("time", sm._SOLVER_TIME_ENTITY_KEYS, "17", 17.0),
            ("time", sm._SOLVER_TIME_ENTITY_KEYS, "x:y", None),
            ("time", sm._SOLVER_TIME_ENTITY_KEYS, "unknown", None),
            ("switch", sm._SOLVER_SWITCH_ENTITY_KEYS, "on", True),
            ("switch", sm._SOLVER_SWITCH_ENTITY_KEYS, "off", False),
            ("switch", sm._SOLVER_SWITCH_ENTITY_KEYS, "unavailable", None),
            ("select", sm._SOLVER_SELECT_ENTITY_KEYS, "away", "away"),
            ("select", sm._SOLVER_SELECT_ENTITY_KEYS, "unknown", None),
        ]:
            with mp.context() as mp:
                key = min(set(keys) & set(sm._SOLVER_ALL_KEYS))
                states = _numbers() | {
                    f"{domain}.nimbus_{key}": SimpleNamespace(state=state)
                }
                s = _sensor(mp, states)
                assert s.extra_state_attributes[key] == value
                missing = _sensor(mp, _numbers())
                assert missing.extra_state_attributes[key] is None

    # test_unconfigured_is_warned_once_per_change_with_the_keys_named
    caplog.clear()
    with monkeypatch.context() as mp:
        states = _numbers()
        s = _sensor(mp, states)
        with caplog.at_level(logging.INFO, logger=sm._LOGGER.name):
            asyncio.run(s.async_update())
            states.pop(f"number.nimbus_{NUMBERS[0]}")
            asyncio.run(s.async_update())
            asyncio.run(s.async_update())
            states.update(_numbers())
            asyncio.run(s.async_update())
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
        assert len(warnings) == 1 and NUMBERS[0] in warnings[0]
        assert len(infos) == 2  # configured at the start and again at the end

    # test_every_key_the_wizard_saves_is_exposed
    # test_every_key_the_wizard_saves_is_exposed
    assert [
        k
        for k in _hub_options._SOLVER_WIZARD_SCHEMA_KEYS
        if k not in sm._SOLVER_ALL_KEYS
    ] == []

    # test_every_field_the_wizard_shows_is_saved
    # nimbus #2136: the wizard's final merge keeps only
    # _SOLVER_WIZARD_SCHEMA_KEYS, so a field shown on a step but missing from
    # it is accepted and silently dropped -- the site postcode (#2133) was.
    shown = set()
    for schema in (
        _hub_options._solver_battery_schema({}),
        _hub_options._solver_grid_schema({}),
        _hub_options._solver_sources_schema({}),
    ):
        shown |= set(_hub_options._effective_schema_keys(schema))
    assert sorted(shown - set(_hub_options._SOLVER_WIZARD_SCHEMA_KEYS)) == []

    # test_without_a_geocoded_sensor_the_postcode_setting_gives_the_region
    # nimbus #2133: the reference household has no Companion App geocoded
    # location, so the flex telemetry record never had a region; a postcode
    # set once in Configure gives the region and prefix instead.
    with monkeypatch.context() as mp:
        attrs = _sensor(
            mp, _numbers(), options={sm.CONF_SITE_POSTCODE: "4000"}
        ).extra_state_attributes
        assert (attrs["region"], attrs["postcode_prefix"]) == ("QLD1", "400")
        attrs = _sensor(mp, _numbers()).extra_state_attributes
        assert (attrs["region"], attrs["postcode_prefix"]) == (None, None)

    # test_a_participant_that_departs_or_takes_trips_is_named_a_vehicle
    # #2140: flex telemetry marks these `ev`. A departure hour (0 counts) or a
    # trip calendar makes one; the per-100 km figure, which has a default,
    # does not, and neither does a participant with neither.
    def participant(name, **data):
        return SimpleNamespace(
            subentry_type=sm.SUBENTRY_TYPE_BATTERY_PARTICIPANT,
            data={sm.CONF_BATTERY_PARTICIPANT_NAME: name, **data},
        )

    subs = {
        "s1": participant("Car", **{sm.CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR: 0}),
        "s2": participant(
            "Van", **{sm.CONF_BATTERY_PARTICIPANT_TRIP_CALENDAR_ENTITY: "calendar.van"}
        ),
        "s3": participant("Shed battery", battery_participant_kwh_per_100km=18.0),
        "s4": SimpleNamespace(subentry_type=sm.SUBENTRY_TYPE_LOAD, data={}),
    }
    with monkeypatch.context() as mp:
        attrs = _sensor(mp, _numbers(), subentries=subs).extra_state_attributes
        assert attrs["flex_vehicle_participants"] == ["Car", "Van"]

    # test_every_number_entity_is_read_live_not_from_saved_options
    keys = [d.key for d in _number._DESCRIPTIONS]
    assert [k for k in keys if k not in sm._SOLVER_NUMBER_ENTITY_KEYS] == []

    # test_every_switch_the_solve_reads_is_read_live
    read_by_the_solve = (
        _const.CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
        _const.CONF_SOLVE_ON_PRICE_CHANGE,
        _const.CONF_SOLVER_OFFER_CURVE_ENABLED,
        _const.CONF_SOLVER_FLEX_SIGNALS_ENABLED,
        _const.CONF_SOLVER_PRICE_SPIKE_OVERRIDE_ARMED,
        _const.CONF_SOLVER_CALIBRATED_OBJECTIVE_ENABLED,
        _const.CONF_SOLVER_NOWCAST_MEASUREMENT_ENABLED,
    )
    assert [
        k for k in read_by_the_solve if k not in sm._SOLVER_SWITCH_ENTITY_KEYS
    ] == []
    # Dry run is read from its live switch directly, never through the solve.
    assert _const.CONF_SOLVER_DISPATCH_DRY_RUN not in sm._SOLVER_SWITCH_ENTITY_KEYS

    # test_the_household_mode_is_exposed_and_read_live
    assert _const.CONF_HOUSEHOLD_MODE in sm._SOLVER_ALL_KEYS
    assert _const.CONF_HOUSEHOLD_MODE in sm._SOLVER_SELECT_ENTITY_KEYS

    # test_every_live_setting_is_exposed_to_the_solve
    live = (
        set(sm._SOLVER_NUMBER_ENTITY_KEYS)
        | set(sm._SOLVER_TIME_ENTITY_KEYS)
        | set(sm._SOLVER_SWITCH_ENTITY_KEYS)
        | set(sm._SOLVER_SELECT_ENTITY_KEYS)
    )
    # Read by __init__.py's price watcher directly, not by the solve.
    not_solve_inputs = {"solve_on_price_change", "solve_on_price_change_debounce_s"}
    assert sorted(live - set(sm._SOLVER_ALL_KEYS) - not_solve_inputs) == []
    for key in (
        _const.CONF_SOLVER_LOAD_FORECAST_ENTITIES,
        _const.CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    ):  # the two fields of the 2026-08-23 incident
        assert key in sm._SOLVER_ALL_KEYS


# --- every setting the solve reads reaches it (#485, 2026-08-23) -----------------------------
# The solver reads its settings only through this sensor's attributes. A key
# saved by the wizard or set on a live entity but missing from these tables
# is a setting that silently does nothing, and each has happened.

_number = importlib.import_module("custom_components.nimbus_load.number")
_hub_options = importlib.import_module(
    "custom_components.nimbus_load.flows.hub_options"
)
_const = importlib.import_module("custom_components.nimbus_load.const")
