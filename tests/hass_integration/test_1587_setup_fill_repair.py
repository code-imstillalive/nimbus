"""nimbus #1587 against a real Home Assistant: a setup-fill offer is a
fixable Repair, and submitting it through HA's own repairs flow manager
sets exactly that one field.

What the stub suite cannot prove: that HA finds `repairs.py` as Nimbus's
repairs platform, hands `async_create_fix_flow` the issue's own data, and
deletes the issue once the flow finishes.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.setup_builder import async_refresh_setup_fills

TARGET = "solver_battery_power_sensor"


async def test_submitting_the_offer_sets_the_one_field(hass: HomeAssistant):
    assert await async_setup_component(hass, "repairs", {})
    hass.states.async_set(
        "sensor.test_battery_power", "2.5", {"unit_of_measurement": "kW"}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Nimbus",
        data={},
        options={"battery_sensor": "sensor.test_battery_power"},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await async_refresh_setup_fills(hass, entry)
    issue_id = f"{entry.entry_id}_setup_fill_{TARGET}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None and issue.is_fixable
    assert entry.options.get(TARGET) is None  # offered, not written

    manager = hass.data["repairs"]["flow_manager"]
    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert result["type"] == "form"
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["sensor"] == "sensor.test_battery_power"

    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options[TARGET] == "sensor.test_battery_power"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
