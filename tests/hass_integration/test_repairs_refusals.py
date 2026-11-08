"""Nimbus's fixable Repairs refuse what no longer applies (#2393: repairs.py's coverage, one behaviour per
test): a stale "use this sensor" offer never writes, a Repair whose hub has gone aborts instead of
failing, and an unknown kind is only confirmed.
"""

from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nimbus_load.const import DOMAIN
from custom_components.nimbus_load.repairs import (
    DeviceContractDriftFlow,
    DeviceUnitFlow,
    async_create_fix_flow,
)
from custom_components.nimbus_load.setup_builder import async_refresh_setup_fills

TARGET = "solver_battery_power_sensor"


async def test_a_stale_setup_offer_is_refused_and_writes_nothing(hass: HomeAssistant):
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
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # between the offer and the Submit, the sensor stopped being a power sensor
    hass.states.async_set(
        "sensor.test_battery_power", "40", {"unit_of_measurement": "%"}
    )
    manager = hass.data["repairs"]["flow_manager"]
    result = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    result = await manager.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] == "abort" and result["reason"] == "no_longer_applies"
    assert entry.options.get(TARGET) is None


async def test_a_repair_for_a_hub_that_has_gone_is_refused(hass: HomeAssistant):
    gone = {
        "entry_id": "no-such-entry",
        "part": "hub",
        "key": "x",
        "device_id": "battery",
        "role": "power",
    }
    for flow, step in (
        (
            DeviceContractDriftFlow({**gone, "kind": "device_contract_drift"}),
            "async_step_keep",
        ),
        (
            DeviceContractDriftFlow({**gone, "kind": "device_contract_drift"}),
            "async_step_restore",
        ),
        (DeviceUnitFlow({**gone, "kind": "device_unit_undeclared"}), "async_step_init"),
    ):
        flow.hass = hass
        result = await getattr(flow, step)(
            {"unit": "W"} if isinstance(flow, DeviceUnitFlow) else None
        )
        assert result["type"] == "abort" and result["reason"] == "no_longer_applies", (
            flow,
            step,
        )


async def test_an_unknown_repair_kind_is_only_confirmed(hass: HomeAssistant):
    flow = await async_create_fix_flow(hass, "some_issue", {"kind": "not_a_nimbus_fix"})
    assert type(flow) is ConfirmRepairFlow
    assert (
        type(await async_create_fix_flow(hass, "some_issue", None)) is ConfirmRepairFlow
    )
