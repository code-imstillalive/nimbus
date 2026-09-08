"""Real test of flows/battery_participant_subentry.py -- nimbus issue #563.
Same None-default crash-avoidance pattern and reconfigure-vs-new/field-
preservation regression coverage as test_flows_controllable_load_subentry.py.

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
    CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
    CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
    CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
    CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
    CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
    CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
    CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
    CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
    CONF_BATTERY_PARTICIPANT_NAME,
    CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
    CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
    CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
    CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
)
from custom_components.nimbus_load.flows.battery_participant_subentry import (
    NimbusBatteryParticipantSubentryFlowHandler,
    _schema,
)


def _find_marker(schema: vol.Schema, key: str):
    return next(k for k in schema.schema if k == key)


def _make_flow(
    source: str = "user",
) -> NimbusBatteryParticipantSubentryFlowHandler:
    flow = NimbusBatteryParticipantSubentryFlowHandler.__new__(
        NimbusBatteryParticipantSubentryFlowHandler
    )
    flow.source = source
    flow.hass = MagicMock()
    return flow


_MINIMAL_INPUT = {
    CONF_BATTERY_PARTICIPANT_NAME: "ev_m3p",
    CONF_BATTERY_PARTICIPANT_CAPACITY_KWH: 60.0,
    CONF_BATTERY_PARTICIPANT_SOC_SENSOR: "sensor.m3p_t_battery_level",
    CONF_BATTERY_PARTICIPANT_POWER_SENSOR: "sensor.sigen_inverter_dc_charger_output_power",
    CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE: True,
    CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW: 11.0,
    CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW: 11.0,
    CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT: 20.0,
    CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT: 95.0,
    CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT: 92.0,
}


# -- _schema(): required fields, None-default frontend crash guard ----------


def test_required_fields_are_required():
    schema = _schema({})
    for key in (
        CONF_BATTERY_PARTICIPANT_NAME,
        CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
        CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
        CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
        CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
        CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
    ):
        marker = _find_marker(schema, key)
        assert type(marker).__name__ == "Required", f"{key} should be required"


def test_min_max_soc_default_to_the_real_sane_reference_values():
    schema = _schema({})
    assert (
        _find_marker(schema, CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT).default() == 10.0
    )
    assert (
        _find_marker(schema, CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT).default()
        == 100.0
    )


def test_power_positive_is_charge_defaults_true():
    schema = _schema({})
    marker = _find_marker(schema, CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE)
    assert marker.default() is True


def test_salvage_value_same_none_default_guard():
    schema = _schema({})
    marker = _find_marker(schema, CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE)
    assert marker.default is vol.UNDEFINED


def test_degradation_cost_same_none_default_guard():
    schema = _schema({})
    marker = _find_marker(schema, CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH)
    assert marker.default is vol.UNDEFINED


def test_charge_limit_entity_same_none_default_guard():
    schema = _schema({})
    marker = _find_marker(schema, CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY)
    assert marker.default is vol.UNDEFINED


def test_charge_limit_entity_selector_scoped_to_number_domain():
    schema = _schema({})
    marker = _find_marker(schema, CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY)
    selector_instance = schema.schema[marker]
    assert selector_instance.config["domain"] == "number"


# -- async_step_user: reconfigure-vs-new routing -----------------------------


def test_fresh_add_with_no_input_shows_the_form():
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_fresh_add_with_input_creates_a_new_entry_not_update_and_abort():
    _schema({})(_MINIMAL_INPUT)
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(dict(_MINIMAL_INPUT)))
    assert result["type"] == "create_entry"
    assert result["title"] == "ev_m3p"
    assert result["data"] == _MINIMAL_INPUT


def test_reconfigure_source_calls_get_reconfigure_subentry_not_treated_as_new():
    _schema({})(_MINIMAL_INPUT)
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data=dict(_MINIMAL_INPUT))
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock()
    result = asyncio.run(flow.async_step_user(dict(_MINIMAL_INPUT)))
    flow._get_reconfigure_subentry.assert_called_once()
    assert result["type"] == "update_and_abort"


def test_step_reconfigure_alias_delegates_to_step_user():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data=dict(_MINIMAL_INPUT))
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock()
    result = asyncio.run(flow.async_step_reconfigure(dict(_MINIMAL_INPUT)))
    assert result["type"] == "update_and_abort"


# -- Real #339-style field-preservation regression --------------------------


def test_reconfigure_with_no_input_prefills_every_field_from_existing_data():
    existing = dict(_MINIMAL_INPUT)
    existing[CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE] = 0.12
    existing[CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY] = "number.m3p_t_charge_limit"
    flow = _make_flow(source="reconfigure")
    flow._get_reconfigure_subentry = MagicMock(return_value=MagicMock(data=existing))
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    schema = result["data_schema"]
    for key, value in existing.items():
        marker = _find_marker(schema, key)
        assert marker.default() == value, f"{key} did not round-trip"


def test_reconfigure_submission_of_one_changed_field_preserves_the_rest():
    existing = dict(_MINIMAL_INPUT)
    changed = dict(existing)
    changed[CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT] = 80.0  # the one edited field
    flow = _make_flow(source="reconfigure")
    flow._get_reconfigure_subentry = MagicMock(return_value=MagicMock(data=existing))
    flow._get_entry = MagicMock()
    result = asyncio.run(flow.async_step_user(changed))
    assert result["type"] == "update_and_abort"
    assert result["data"][CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT] == 80.0
    assert (
        result["data"][CONF_BATTERY_PARTICIPANT_CAPACITY_KWH]
        == existing[CONF_BATTERY_PARTICIPANT_CAPACITY_KWH]
    )
    assert (
        result["data"][CONF_BATTERY_PARTICIPANT_NAME]
        == existing[CONF_BATTERY_PARTICIPANT_NAME]
    )
