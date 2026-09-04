"""Real test of flows/pv_string_subentry.py -- same reconfigure-vs-new
routing pattern as the other Nimbus subentry flows, plus the real,
specific-to-this-flow logic: building the "which Power Source" dropdown
live from the parent entry's own currently-configured subentries, and
title derivation preferring a user-typed label over the source sensor's
own friendly name.

Imports and exercises the REAL methods (not a reimplementation) against
real `voluptuous` and tests/_ha_stubs.py's stand-in homeassistant.* modules.
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
from custom_components.nimbus_load.const import (
    CONF_PV_STRING_ENTITY,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
)
from custom_components.nimbus_load.flows.pv_string_subentry import (
    NimbusPvStringSubentryFlowHandler,
    _power_source_options,
)


def _fake_entry(subentries: dict) -> MagicMock:
    entry = MagicMock()
    entry.subentries = subentries
    return entry


def _make_flow(
    source: str = "user", entry: MagicMock | None = None
) -> NimbusPvStringSubentryFlowHandler:
    flow = NimbusPvStringSubentryFlowHandler.__new__(NimbusPvStringSubentryFlowHandler)
    flow.source = source
    flow.hass = MagicMock()
    flow._get_entry = MagicMock(
        return_value=entry if entry is not None else _fake_entry({})
    )
    return flow


def test_power_source_options_includes_only_power_source_subentries():
    # A real, direct test of the dropdown-building logic: a Load
    # subentry sitting alongside a real Power Source must NOT leak into
    # the options list.
    ps1 = MagicMock(
        subentry_id="ps1", subentry_type=SUBENTRY_TYPE_POWER_SOURCE, title="Inverter 1"
    )
    load1 = MagicMock(
        subentry_id="load1", subentry_type=SUBENTRY_TYPE_LOAD, title="Pool Pump"
    )
    entry = _fake_entry({"ps1": ps1, "load1": load1})
    options = _power_source_options(entry)
    assert options == [{"value": "ps1", "label": "Inverter 1"}]


def test_power_source_options_empty_when_none_configured():
    entry = _fake_entry({})
    assert _power_source_options(entry) == []


def test_fresh_add_with_no_input_shows_the_form():
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_fresh_add_with_input_creates_a_new_entry():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = None
    result = asyncio.run(
        flow.async_step_user({CONF_PV_STRING_ENTITY: "sensor.string3_power_inv1"})
    )
    assert result["type"] == "create_entry"
    assert result["data"][CONF_PV_STRING_ENTITY] == "sensor.string3_power_inv1"


def test_title_prefers_user_label_over_friendly_name():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = MagicMock(
        attributes={"friendly_name": "String 3 Power"}
    )
    assert flow._derive_title("sensor.string3_power_inv1", "West array") == "West array"


def test_title_falls_back_to_friendly_name_when_no_label():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = MagicMock(
        attributes={"friendly_name": "String 3 Power"}
    )
    assert flow._derive_title("sensor.string3_power_inv1", None) == "String 3 Power"


def test_title_falls_back_to_entity_id_when_no_label_or_friendly_name():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = None
    assert (
        flow._derive_title("sensor.string3_power_inv1", "")
        == "sensor.string3_power_inv1"
    )


def test_reconfigure_source_updates_existing_entry_not_creates_new():
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_PV_STRING_ENTITY: "sensor.old_string"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow.hass.states.get.return_value = None
    result = asyncio.run(
        flow.async_step_user({CONF_PV_STRING_ENTITY: "sensor.old_string"})
    )
    flow._get_reconfigure_subentry.assert_called_once()
    assert result["type"] == "update_and_abort"
