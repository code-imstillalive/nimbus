"""Real test of flows/controllable_load_subentry.py -- nimbus issue #486.
Same None-default crash-avoidance pattern as load_subentry.py's own test
(tests/test_flows_load_subentry.py), plus NimbusControllableLoadSubentry
FlowHandler's real reconfigure-vs-new routing and field-preservation
(mirrors #339's own regression standard: submitting the wizard to
change one field must leave every other field intact).

Imports and exercises the REAL functions/methods (not a reimplementation)
against real `voluptuous` and tests/_ha_stubs.py's stand-in homeassistant.*
modules.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import voluptuous as vol

from custom_components.nimbus_load.const import (
    CONF_CONTROLLABLE_LOAD_KIND,
    CONF_CONTROLLABLE_LOAD_NAME,
    CONF_CONTROLLABLE_LOAD_POWER_SENSOR,
    CONF_DEFERRABLE_DEADLINE_HOUR,
    CONF_DEFERRABLE_DONE_ENTITY,
    CONF_DEFERRABLE_DONE_WHEN,
    CONF_DEFERRABLE_EARLIEST_HOUR,
    CONF_DEFERRABLE_MAX_POWER_KW,
    CONF_DEFERRABLE_SHORTFALL_PRICE,
    CONF_DEFERRABLE_TARGET_KWH,
    CONF_DEFERRABLE_VALUE_PER_KWH,
    CONF_SHEDDABLE_MIN_FRACTION,
    CONF_SHEDDABLE_NOMINAL_KW,
    CONF_SHEDDABLE_SHED_COST,
    CONTROLLABLE_LOAD_KIND_SHEDDABLE,
)
from custom_components.nimbus_load.flows.controllable_load_subentry import (
    NimbusControllableLoadSubentryFlowHandler,
    _schema,
)


def _find_marker(schema: vol.Schema, key: str):
    return next(k for k in schema.schema if k == key)


def _make_flow(source: str = "user") -> NimbusControllableLoadSubentryFlowHandler:
    flow = NimbusControllableLoadSubentryFlowHandler.__new__(
        NimbusControllableLoadSubentryFlowHandler
    )
    flow.source = source
    flow.hass = MagicMock()
    return flow


_MINIMAL_SHEDDABLE_INPUT = {
    CONF_CONTROLLABLE_LOAD_NAME: "Pool Pump",
    CONF_CONTROLLABLE_LOAD_KIND: CONTROLLABLE_LOAD_KIND_SHEDDABLE,
}


# -- _schema(): None-default frontend crash guard, same as load_subentry ----


def test_name_and_kind_are_required():
    schema = _schema({})
    name_marker = _find_marker(schema, CONF_CONTROLLABLE_LOAD_NAME)
    kind_marker = _find_marker(schema, CONF_CONTROLLABLE_LOAD_KIND)
    assert type(name_marker).__name__ == "Required"
    assert type(kind_marker).__name__ == "Required"


def test_power_sensor_omits_default_entirely_when_never_configured():
    schema = _schema({})
    marker = _find_marker(schema, CONF_CONTROLLABLE_LOAD_POWER_SENSOR)
    assert marker.default is vol.UNDEFINED


def test_sheddable_nominal_kw_same_none_default_guard():
    schema = _schema({})
    marker = _find_marker(schema, CONF_SHEDDABLE_NOMINAL_KW)
    assert marker.default is vol.UNDEFINED


def test_sheddable_nominal_kw_carries_real_default_once_configured():
    schema = _schema({CONF_SHEDDABLE_NOMINAL_KW: 1.5})
    marker = _find_marker(schema, CONF_SHEDDABLE_NOMINAL_KW)
    assert marker.default() == 1.5


def test_sheddable_shed_cost_defaults_to_the_real_solver_reference_value():
    # DEFAULT_SHED_COST (solver/elements.py) -- shown as the wizard's own
    # default so an unconfigured shed_cost still behaves like a real,
    # high reference cost rather than a silent 0.
    schema = _schema({})
    marker = _find_marker(schema, CONF_SHEDDABLE_SHED_COST)
    assert marker.default() == 2.00


def test_deferrable_shortfall_price_defaults_to_the_real_solver_reference_value():
    schema = _schema({})
    marker = _find_marker(schema, CONF_DEFERRABLE_SHORTFALL_PRICE)
    assert marker.default() == 10.00


def test_deferrable_earliest_hour_selector_uses_quarter_hour_steps_no_am_pm():
    # Same real 2026-08-15 finding load_subentry.py's own schedule-hour
    # fields guard against -- see that file's own comment for the exact
    # 12-hour-TimeSelector midnight/noon bug this sidesteps.
    schema = _schema({})
    marker = _find_marker(schema, CONF_DEFERRABLE_EARLIEST_HOUR)
    selector_instance = schema.schema[marker]
    assert selector_instance.config["min"] == 0
    assert selector_instance.config["max"] == 23.75
    assert selector_instance.config["step"] == 0.25


def test_deferrable_value_per_kwh_same_none_default_guard():
    schema = _schema({})
    marker = _find_marker(schema, CONF_DEFERRABLE_VALUE_PER_KWH)
    assert marker.default is vol.UNDEFINED


def test_deferrable_done_entity_and_done_when_same_none_default_guard():
    # nimbus issue #480 -- both fields are optional, never defaulted to
    # a real value once configured (same crash-avoidance reasoning as
    # every other optional field on this schema).
    schema = _schema({})
    assert _find_marker(schema, CONF_DEFERRABLE_DONE_ENTITY).default is vol.UNDEFINED
    assert _find_marker(schema, CONF_DEFERRABLE_DONE_WHEN).default is vol.UNDEFINED


# -- async_step_user: reconfigure-vs-new routing, same pattern as Load ------


def test_fresh_add_with_no_input_shows_the_form():
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_fresh_add_with_input_creates_a_new_entry_not_update_and_abort():
    # nimbus issue #360 -- same real-schema-validation confirmation as
    # load_subentry.py's own equivalent test.
    _schema({})(_MINIMAL_SHEDDABLE_INPUT)
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(dict(_MINIMAL_SHEDDABLE_INPUT)))
    assert result["type"] == "create_entry"
    assert result["title"] == "Pool Pump"
    assert result["data"] == _MINIMAL_SHEDDABLE_INPUT


def test_reconfigure_source_calls_get_reconfigure_subentry_not_treated_as_new():
    _schema({})(_MINIMAL_SHEDDABLE_INPUT)
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data=dict(_MINIMAL_SHEDDABLE_INPUT))
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock()
    result = asyncio.run(flow.async_step_user(dict(_MINIMAL_SHEDDABLE_INPUT)))
    flow._get_reconfigure_subentry.assert_called_once()
    assert result["type"] == "update_and_abort"


def test_step_reconfigure_alias_delegates_to_step_user():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data=dict(_MINIMAL_SHEDDABLE_INPUT))
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock()
    result = asyncio.run(flow.async_step_reconfigure(dict(_MINIMAL_SHEDDABLE_INPUT)))
    assert result["type"] == "update_and_abort"


# -- Real #339-style field-preservation regression --------------------------


def test_reconfigure_with_no_input_prefills_every_field_from_existing_data():
    # The real #339 regression this guards against: a subentry options
    # form silently wiping fields the user didn't touch on the next
    # submission, because the schema wasn't prefilled from the existing
    # data in the first place. Confirms every kind's own real field
    # round-trips through the form's own defaults, not just the ones a
    # human happened to look at.
    existing = {
        CONF_CONTROLLABLE_LOAD_NAME: "HWS L1",
        CONF_CONTROLLABLE_LOAD_KIND: "deferrable",
        CONF_CONTROLLABLE_LOAD_POWER_SENSOR: "sensor.hws_l1_power",
        CONF_DEFERRABLE_MAX_POWER_KW: 3.7,
        CONF_DEFERRABLE_TARGET_KWH: 5.0,
        CONF_DEFERRABLE_EARLIEST_HOUR: 1.0,
        CONF_DEFERRABLE_DEADLINE_HOUR: 6.0,
        CONF_DEFERRABLE_SHORTFALL_PRICE: 12.0,
        CONF_DEFERRABLE_VALUE_PER_KWH: 0.08,
        CONF_DEFERRABLE_DONE_ENTITY: "binary_sensor.hws_l1_at_temp",
        CONF_DEFERRABLE_DONE_WHEN: ">= 60",
    }
    flow = _make_flow(source="reconfigure")
    flow._get_reconfigure_subentry = MagicMock(return_value=MagicMock(data=existing))
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    schema = result["data_schema"]
    for key, value in existing.items():
        marker = _find_marker(schema, key)
        assert marker.default() == value, f"{key} did not round-trip"


def test_reconfigure_submission_of_one_changed_field_preserves_the_rest():
    # The other half of #339: actually SUBMITTING a change to one field
    # must carry every other existing field through into the new data,
    # not just show them correctly prefilled beforehand.
    existing = {
        CONF_CONTROLLABLE_LOAD_NAME: "HWS L1",
        CONF_CONTROLLABLE_LOAD_KIND: "deferrable",
        CONF_DEFERRABLE_MAX_POWER_KW: 3.7,
        CONF_DEFERRABLE_TARGET_KWH: 5.0,
        CONF_DEFERRABLE_SHORTFALL_PRICE: 12.0,
    }
    changed = dict(existing)
    changed[CONF_DEFERRABLE_TARGET_KWH] = 6.5  # the one field being edited
    flow = _make_flow(source="reconfigure")
    flow._get_reconfigure_subentry = MagicMock(return_value=MagicMock(data=existing))
    flow._get_entry = MagicMock()
    result = asyncio.run(flow.async_step_user(changed))
    assert result["type"] == "update_and_abort"
    assert result["data"][CONF_DEFERRABLE_TARGET_KWH] == 6.5
    assert result["data"][CONF_DEFERRABLE_SHORTFALL_PRICE] == 12.0
    assert result["data"][CONF_DEFERRABLE_MAX_POWER_KW] == 3.7
    assert result["data"][CONF_CONTROLLABLE_LOAD_NAME] == "HWS L1"


def test_sheddable_min_fraction_round_trips_through_reconfigure():
    existing = {
        CONF_CONTROLLABLE_LOAD_NAME: "Pool Pump",
        CONF_CONTROLLABLE_LOAD_KIND: CONTROLLABLE_LOAD_KIND_SHEDDABLE,
        CONF_SHEDDABLE_NOMINAL_KW: 1.5,
        CONF_SHEDDABLE_MIN_FRACTION: 0.2,
    }
    flow = _make_flow(source="reconfigure")
    flow._get_reconfigure_subentry = MagicMock(return_value=MagicMock(data=existing))
    result = asyncio.run(flow.async_step_user(None))
    marker = _find_marker(result["data_schema"], CONF_SHEDDABLE_MIN_FRACTION)
    assert marker.default() == 0.2
