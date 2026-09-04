"""Real test of flows/load_subentry.py -- _schema()'s real 2026-08-15
None-default crash-avoidance logic ("Cannot read properties of null
(reading 'toString')" in HA's own ha-selector-number frontend component),
plus NimbusLoadSubentryFlowHandler's real reconfigure-vs-new routing and
title-derivation logic.

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
    CONF_EXPECTED_LOAD_KW,
    CONF_LOAD_SENSOR,
    CONF_SCHEDULE_END_HOUR,
    CONF_SCHEDULE_START_HOUR,
)
from custom_components.nimbus_load.flows.load_subentry import (
    NimbusLoadSubentryFlowHandler,
    _schema,
)


def _find_marker(schema: vol.Schema, key: str):
    return next(k for k in schema.schema if k == key)


def _make_flow(source: str = "user") -> NimbusLoadSubentryFlowHandler:
    flow = NimbusLoadSubentryFlowHandler.__new__(NimbusLoadSubentryFlowHandler)
    flow.source = source
    flow.hass = MagicMock()
    return flow


# -- _schema(): the real 2026-08-15 None-default frontend crash guard -------


def test_load_sensor_is_required_and_always_carries_a_default_even_when_none():
    # Unlike the schedule/expected-load fields below, CONF_LOAD_SENSOR is
    # unconditionally given a default= in the real code -- EntitySelector
    # doesn't have the null-default crash NumberSelector has (confirmed by
    # load_subentry.py's own comment), so this is a deliberate difference,
    # not an oversight this test should "fix."
    schema = _schema({})
    marker = _find_marker(schema, CONF_LOAD_SENSOR)
    assert type(marker).__name__ == "Required"
    assert marker.default() is None


def test_schedule_start_hour_omits_default_entirely_when_never_configured():
    # The real crash this guards against: passing default=None to a
    # NumberSelector field crashes ha-selector-number's own frontend
    # rendering ("Cannot read properties of null (reading 'toString')").
    # The fix omits the default= kwarg entirely rather than passing None.
    schema = _schema({})
    marker = _find_marker(schema, CONF_SCHEDULE_START_HOUR)
    assert marker.default is vol.UNDEFINED


def test_schedule_start_hour_carries_a_real_default_once_genuinely_configured():
    schema = _schema({CONF_SCHEDULE_START_HOUR: 8.0})
    marker = _find_marker(schema, CONF_SCHEDULE_START_HOUR)
    assert marker.default is not vol.UNDEFINED
    assert marker.default() == 8.0


def test_schedule_end_hour_same_none_default_guard():
    schema = _schema({})
    marker = _find_marker(schema, CONF_SCHEDULE_END_HOUR)
    assert marker.default is vol.UNDEFINED


def test_expected_load_kw_same_none_default_guard():
    schema = _schema({})
    marker = _find_marker(schema, CONF_EXPECTED_LOAD_KW)
    assert marker.default is vol.UNDEFINED


def test_expected_load_kw_carries_real_default_once_configured():
    schema = _schema({CONF_EXPECTED_LOAD_KW: 3.7})
    marker = _find_marker(schema, CONF_EXPECTED_LOAD_KW)
    assert marker.default() == 3.7


def test_expected_load_kw_selector_has_no_step_constraint():
    # The real 2026-08-15 HTML5-number-input finding: a `step` constraint
    # here would silently reject logically-valid values like 1.3 (binary
    # floating point can't represent 0.05 exactly, so 1.3 isn't an exact
    # multiple of a 0.05 step in the browser's own eyes). Confirmed by
    # asserting "step" is genuinely absent from the config, not just 0.
    schema = _schema({CONF_EXPECTED_LOAD_KW: 1.3})
    marker = _find_marker(schema, CONF_EXPECTED_LOAD_KW)
    selector_instance = schema.schema[marker]
    assert "step" not in selector_instance.config


def test_schedule_hour_selector_uses_quarter_hour_steps_no_am_pm():
    # The real 2026-08-15 finding this field's own selector choice guards
    # against: a 12-hour TimeSelector silently saved "12:30" as 00:30
    # (12:30 AM) instead of the intended 12:30 PM. A 0-23.75 decimal-hour
    # NumberSelector has no AM/PM concept at all.
    schema = _schema({})
    marker = _find_marker(schema, CONF_SCHEDULE_START_HOUR)
    selector_instance = schema.schema[marker]
    assert selector_instance.config["min"] == 0
    assert selector_instance.config["max"] == 23.75
    assert selector_instance.config["step"] == 0.25


# -- _derive_title: friendly_name preferred, entity_id as fallback ----------


def test_derive_title_uses_friendly_name_when_present():
    flow = _make_flow()
    flow.hass.states.get.return_value = MagicMock(
        attributes={"friendly_name": "Logger Load Power"}
    )
    assert flow._derive_title("sensor.logger_load_power") == "Logger Load Power"


def test_derive_title_falls_back_to_entity_id_when_state_missing():
    flow = _make_flow()
    flow.hass.states.get.return_value = None
    assert flow._derive_title("sensor.unknown_thing") == "sensor.unknown_thing"


def test_derive_title_falls_back_to_entity_id_when_no_friendly_name():
    flow = _make_flow()
    flow.hass.states.get.return_value = MagicMock(attributes={})
    assert flow._derive_title("sensor.no_name") == "sensor.no_name"


# -- async_step_user: real, live-bitten reconfigure-vs-new routing ----------


def test_fresh_add_with_no_input_shows_the_form():
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_fresh_add_with_input_creates_a_new_entry_not_update_and_abort():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = None
    result = asyncio.run(flow.async_step_user({CONF_LOAD_SENSOR: "sensor.new_load"}))
    assert result["type"] == "create_entry"
    assert result["data"] == {CONF_LOAD_SENSOR: "sensor.new_load"}


def test_reconfigure_source_calls_get_reconfigure_subentry_not_treated_as_new():
    # The real 2026-08-14 bug this guards against: assuming "user" always
    # meant "brand new" raised "ValueError: Source is reconfigure,
    # expected user" from async_create_entry the moment someone tried to
    # edit an existing load. self.source, not which method got called, is
    # the real signal for which case this is.
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_LOAD_SENSOR: "sensor.existing"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock(return_value=MagicMock())
    flow.hass.states.get.return_value = None
    result = asyncio.run(flow.async_step_user({CONF_LOAD_SENSOR: "sensor.existing"}))
    flow._get_reconfigure_subentry.assert_called_once()
    assert result["type"] == "update_and_abort"


def test_reconfigure_with_no_input_shows_form_prefilled_from_existing_data():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(
        data={CONF_LOAD_SENSOR: "sensor.existing", CONF_SCHEDULE_START_HOUR: 11.0}
    )
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    marker = _find_marker(result["data_schema"], CONF_SCHEDULE_START_HOUR)
    assert marker.default() == 11.0  # prefilled from the existing subentry, not blank


def test_step_reconfigure_alias_delegates_to_step_user():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_LOAD_SENSOR: "sensor.existing"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow._get_entry = MagicMock(return_value=MagicMock())
    flow.hass.states.get.return_value = None
    result = asyncio.run(
        flow.async_step_reconfigure({CONF_LOAD_SENSOR: "sensor.existing"})
    )
    assert result["type"] == "update_and_abort"
