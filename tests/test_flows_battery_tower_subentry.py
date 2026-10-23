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
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import (
    CONF_BATTERY_TOWER_SOC_SENSOR,
)
from custom_components.nimbus_load.flows.battery_tower_subentry import (
    NimbusBatteryTowerSubentryFlowHandler,
    _energy_dashboard_soc_suggestion,
    _schema,
)


def _soc_state(device_class: str, unit: str):
    st = MagicMock()
    st.attributes = {"device_class": device_class, "unit_of_measurement": unit}
    return st


def _hass_with_state(entity_id: str, state) -> MagicMock:
    hass = MagicMock()
    hass.states.get = lambda eid: state if eid == entity_id else None
    return hass


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


# nimbus issue #554: _energy_dashboard_soc_suggestion() itself -- same
# type-safety/graceful-degradation contract as hub_options.py's own
# sibling _energy_dashboard_switchboard_suggestions().
def test_energy_dashboard_suggests_a_real_battery_soc_sensor():
    hass = _hass_with_state("sensor.real_soc", _soc_state("battery", "%"))
    manager = MagicMock(
        data={"energy_sources": [{"type": "battery", "stat_soc": "sensor.real_soc"}]}
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_soc_suggestion(hass))
    assert result == "sensor.real_soc"


def test_energy_dashboard_ignores_wrong_device_class():
    hass = _hass_with_state("sensor.not_soc", _soc_state("power", "%"))
    manager = MagicMock(
        data={"energy_sources": [{"type": "battery", "stat_soc": "sensor.not_soc"}]}
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_soc_suggestion(hass))
    assert result is None


def test_energy_dashboard_ignores_wrong_unit():
    hass = _hass_with_state("sensor.not_soc", _soc_state("battery", "kWh"))
    manager = MagicMock(
        data={"energy_sources": [{"type": "battery", "stat_soc": "sensor.not_soc"}]}
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_soc_suggestion(hass))
    assert result is None


def test_energy_dashboard_ignores_non_battery_sources():
    hass = _hass_with_state("sensor.solar", _soc_state("battery", "%"))
    manager = MagicMock(
        data={"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.solar"}]}
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(_energy_dashboard_soc_suggestion(hass))
    assert result is None


def test_energy_dashboard_lookup_failure_degrades_to_none_not_a_crash():
    hass = MagicMock()
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = asyncio.run(_energy_dashboard_soc_suggestion(hass))
    assert result is None


def test_fresh_add_form_uses_energy_dashboard_suggestion_when_unset():
    # Full async_step_user() path: a fresh tower (no saved SoC sensor)
    # should have the Energy Dashboard suggestion folded into the
    # rendered form's own suggested_value.
    flow = _make_flow(source="user")
    manager = MagicMock(
        data={"energy_sources": [{"type": "battery", "stat_soc": "sensor.real_soc"}]}
    )
    flow.hass.states.get = lambda eid: (
        _soc_state("battery", "%") if eid == "sensor.real_soc" else None
    )
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        result = asyncio.run(flow.async_step_user(None))
    schema = result["data_schema"].schema
    soc_key = next(k for k in schema if str(k) == CONF_BATTERY_TOWER_SOC_SENSOR)
    assert soc_key.description["suggested_value"] == "sensor.real_soc"


def test_reconfigure_with_saved_value_never_calls_the_energy_dashboard():
    # Safeguard 2: a real saved value always wins, and the lookup isn't
    # even worth doing in that case -- confirmed by asserting the patched
    # manager is never actually called, not just that its result is
    # ignored.
    flow = _make_flow(source="reconfigure")
    fake_subentry = MagicMock(data={CONF_BATTERY_TOWER_SOC_SENSOR: "sensor.old_soc"})
    flow._get_reconfigure_subentry = MagicMock(return_value=fake_subentry)
    flow.hass.states.get.return_value = None
    mock_manager_fn = AsyncMock()
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=mock_manager_fn,
    ):
        result = asyncio.run(flow.async_step_user(None))
    mock_manager_fn.assert_not_called()
    schema = result["data_schema"].schema
    soc_key = next(k for k in schema if str(k) == CONF_BATTERY_TOWER_SOC_SENSOR)
    assert soc_key.description["suggested_value"] == "sensor.old_soc"
