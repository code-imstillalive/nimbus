"""Real test of flows/battery_tower_subentry.py -- same reconfigure-vs-new
routing pattern as the other Nimbus subentry flows, plus the real,
specific-to-this-flow title-derivation logic (strip a trailing " SoC"/
" Soc"/" soc" suffix from the SoC sensor's own friendly name, since no
single field cleanly names a battery tower the way a load/PV string's own
source sensor does).

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
    CONF_BATTERY_TOWER_SOC_SENSOR,
)
from custom_components.nimbus_load.flows.battery_tower_subentry import (
    NimbusBatteryTowerSubentryFlowHandler,
    _schema,
)


def _fake_entry(subentries: dict) -> MagicMock:
    entry = MagicMock()
    entry.subentries = subentries
    return entry


def _make_flow(
    source: str = "user", entry: MagicMock | None = None
) -> NimbusBatteryTowerSubentryFlowHandler:
    flow = NimbusBatteryTowerSubentryFlowHandler.__new__(
        NimbusBatteryTowerSubentryFlowHandler
    )
    flow.source = source
    flow.hass = MagicMock()
    flow._get_entry = MagicMock(
        return_value=entry if entry is not None else _fake_entry({})
    )
    return flow


def test_fresh_add_with_no_input_shows_the_form():
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user(None))
    assert result["type"] == "form"
    assert result["step_id"] == "user"


def test_fresh_add_with_no_soc_sensor_still_creates_an_entry():
    # Every field is genuinely Optional -- a household mid-way through
    # the wizard shouldn't hit a hard validation error.
    flow = _make_flow(source="user")
    result = asyncio.run(flow.async_step_user({}))
    assert result["type"] == "create_entry"
    assert result["title"] == "Battery Tower"


def test_title_strips_trailing_soc_suffix():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = MagicMock(
        attributes={"friendly_name": "Battery Tower 2 SoC"}
    )
    assert flow._derive_title("sensor.battery_tower_2_soc") == "Battery Tower 2"


def test_title_falls_back_to_generic_label_when_no_soc_entity():
    flow = _make_flow(source="user")
    assert flow._derive_title(None) == "Battery Tower"


def test_title_falls_back_to_generic_label_when_state_missing():
    flow = _make_flow(source="user")
    flow.hass.states.get.return_value = None
    assert flow._derive_title("sensor.battery_tower_2_soc") == "Battery Tower"


def test_reconfigure_source_updates_existing_entry_not_creates_new():
    user_input = {CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.old_soc"}
    # nimbus issue #360 (Mark Purcell, codebase review): calling
    # async_step_user() directly (below) bypasses FlowManager.
    # async_configure()'s own real schema validation entirely -- confirm
    # this fixture is genuinely something the real schema would accept.
    _schema({}, _fake_entry({}))(user_input)
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.old_soc"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow.hass.states.get.return_value = None
    result = asyncio.run(flow.async_step_user(user_input))
    flow._get_reconfigure_subentry.assert_called_once()
    assert result["type"] == "update_and_abort"
