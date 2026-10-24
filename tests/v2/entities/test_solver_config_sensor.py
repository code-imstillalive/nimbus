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

import pytest

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


def _sensor(monkeypatch, states, *, options=None, renamed=None):
    monkeypatch.setattr(sm.er, "async_get", lambda hass: Registry(renamed))
    entry = SimpleNamespace(
        entry_id="hub1",
        options={key: f"sensor.{key}" for key in OPTIONS} | (options or {}),
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


def test_with_every_required_setting_resolved_the_solver_is_configured(monkeypatch):
    s = _sensor(monkeypatch, _numbers())
    assert s.entity_id == "sensor.nimbus_solver_config"
    assert s.native_value == "configured"
    attrs = s.extra_state_attributes
    assert attrs["unresolved_required_keys"] == []
    assert attrs[NUMBERS[0]] == 5.0
    assert attrs[OPTIONS[0]] == f"sensor.{OPTIONS[0]}"
    assert set(sm._SOLVER_ALL_KEYS) <= set(attrs)


@pytest.mark.parametrize("value", ["unknown", "unavailable", "lots"])
def test_a_setting_still_restoring_or_unreadable_is_unresolved(monkeypatch, value):
    states = _numbers()
    states[f"number.nimbus_{NUMBERS[0]}"] = SimpleNamespace(state=value)
    s = _sensor(monkeypatch, states)
    assert s.native_value == "unconfigured"
    assert s.extra_state_attributes["unresolved_required_keys"] == [NUMBERS[0]]


def test_an_empty_option_is_unresolved(monkeypatch):
    s = _sensor(monkeypatch, _numbers(), options={OPTIONS[0]: ""})
    assert s.extra_state_attributes["unresolved_required_keys"] == [OPTIONS[0]]


def test_a_renamed_entity_is_found_through_the_registry(monkeypatch):
    states = _numbers()
    moved = states.pop(f"number.nimbus_{NUMBERS[0]}")
    states["number.my_battery_size"] = SimpleNamespace(state="13.5")
    s = _sensor(
        monkeypatch, states, renamed={f"hub1_{NUMBERS[0]}": "number.my_battery_size"}
    )
    assert s.extra_state_attributes[NUMBERS[0]] == 13.5
    assert moved.state == "5"


@pytest.mark.parametrize(
    ("domain", "keys", "state", "value"),
    [
        ("time", sm._SOLVER_TIME_ENTITY_KEYS, "05:30:00", 5.5),
        ("time", sm._SOLVER_TIME_ENTITY_KEYS, "17", 17.0),
        ("time", sm._SOLVER_TIME_ENTITY_KEYS, "x:y", None),
        ("time", sm._SOLVER_TIME_ENTITY_KEYS, "unknown", None),
        ("switch", sm._SOLVER_SWITCH_ENTITY_KEYS, "on", True),
        ("switch", sm._SOLVER_SWITCH_ENTITY_KEYS, "off", False),
        ("switch", sm._SOLVER_SWITCH_ENTITY_KEYS, "unavailable", None),
        ("select", sm._SOLVER_SELECT_ENTITY_KEYS, "away", "away"),
        ("select", sm._SOLVER_SELECT_ENTITY_KEYS, "unknown", None),
    ],
)
def test_each_kind_of_setting_reads_in_the_form_the_solve_uses(
    monkeypatch, domain, keys, state, value
):
    key = min(set(keys) & set(sm._SOLVER_ALL_KEYS))
    states = _numbers() | {f"{domain}.nimbus_{key}": SimpleNamespace(state=state)}
    s = _sensor(monkeypatch, states)
    assert s.extra_state_attributes[key] == value
    missing = _sensor(monkeypatch, _numbers())
    assert missing.extra_state_attributes[key] is None


def test_unconfigured_is_warned_once_per_change_with_the_keys_named(
    monkeypatch, caplog
):
    states = _numbers()
    s = _sensor(monkeypatch, states)
    with caplog.at_level(logging.INFO, logger=sm._LOGGER.name):
        asyncio.run(s.async_update())
        states.pop(f"number.nimbus_{NUMBERS[0]}")
        asyncio.run(s.async_update())
        asyncio.run(s.async_update())
        states.update(_numbers())
        asyncio.run(s.async_update())
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(warnings) == 1 and NUMBERS[0] in warnings[0]
    assert len(infos) == 2  # configured at the start and again at the end


def test_location_meters_and_known_sources_are_carried(monkeypatch):
    s = _sensor(
        monkeypatch,
        _numbers(),
        options={
            "switchboard_grid_meter_sensor": "sensor.meter",
            "grid_sensor": "sensor.grid",
        },
    )
    monkeypatch.setattr(
        sm.sensor_discovery,
        "resolve_geocoded_region_and_prefix",
        lambda states: ("QLD1", "410"),
    )
    monkeypatch.setattr(
        sm.sensor_discovery,
        "discover_aemo_30min_forecast_sensor",
        lambda states: "sensor.aemo",
    )
    monkeypatch.setattr(
        sm.known_sources, "resolve", lambda hass: {"solcast": ["sensor.sc"]}
    )
    attrs = s.extra_state_attributes
    assert (attrs["region"], attrs["postcode_prefix"]) == ("QLD1", "410")
    assert attrs["aemo_30min_forecast_sensor"] == "sensor.aemo"
    assert (
        attrs["switchboard_grid_meter_sensor"] == "sensor.meter"
        and attrs["grid_sensor"] == "sensor.grid"
    )
    assert attrs["known_sources"] == {"solcast": ["sensor.sc"]}

    def broken(hass):
        raise RuntimeError("registry")

    monkeypatch.setattr(sm.known_sources, "resolve", broken)
    assert s.extra_state_attributes["known_sources"] == sm.known_sources.EMPTY


# --- every setting the solve reads reaches it (#485, 2026-08-23) -----------------------------
# The solver reads its settings only through this sensor's attributes. A key
# saved by the wizard or set on a live entity but missing from these tables
# is a setting that silently does nothing, and each has happened.

_number = importlib.import_module("custom_components.nimbus_load.number")
_hub_options = importlib.import_module(
    "custom_components.nimbus_load.flows.hub_options"
)
_const = importlib.import_module("custom_components.nimbus_load.const")


def test_every_key_the_wizard_saves_is_exposed():
    assert [
        k
        for k in _hub_options._SOLVER_WIZARD_SCHEMA_KEYS
        if k not in sm._SOLVER_ALL_KEYS
    ] == []


def test_every_number_entity_is_read_live_not_from_saved_options():
    keys = [d.key for d in _number._DESCRIPTIONS]
    assert [k for k in keys if k not in sm._SOLVER_NUMBER_ENTITY_KEYS] == []


def test_every_switch_the_solve_reads_is_read_live():
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


def test_the_household_mode_is_exposed_and_read_live():
    assert _const.CONF_HOUSEHOLD_MODE in sm._SOLVER_ALL_KEYS
    assert _const.CONF_HOUSEHOLD_MODE in sm._SOLVER_SELECT_ENTITY_KEYS


def test_every_live_setting_is_exposed_to_the_solve():
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
