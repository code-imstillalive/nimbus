"""Fix flows for Nimbus's Repairs (nimbus #1574 stage 1, #1587).

Home Assistant finds this module as the integration's `repairs` platform and
calls `async_create_fix_flow` when the household presses Submit on a fixable
Nimbus Repair (homeassistant/components/repairs/issue_handler.py, identical
in HA 2026.7.4 and 2026.9.3). HA deletes the issue itself when the flow
finishes, and keeps it when the flow aborts.

The only fixable Nimbus Repairs are setup_builder's offers: "use this sensor
for this empty Solver field". Submitting applies exactly that one field,
after re-checking that it is still empty and the sensor is still a power
sensor, so a stale offer can never overwrite anything.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .setup_builder import FIX_FLOW_KIND, async_confirm_setup_fill


class SetupFillRepairFlow(ConfirmRepairFlow):
    """Confirm one setup fill; the confirm step shows the issue's own text."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._fill = data

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> Any:
        if user_input is None:
            return await super().async_step_confirm(None)
        applied = await async_confirm_setup_fill(
            self.hass,
            str(self._fill.get("entry_id")),
            str(self._fill.get("target")),
            str(self._fill.get("sensor")),
        )
        if not applied:
            return self.async_abort(reason="no_longer_applies")
        return self.async_create_entry(data={})


class DeviceContractDriftFlow(RepairsFlow):
    """One drifted device-setup key (DC-004 step 5c): keep the change as a new
    revision, or restore the document's value."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._drift = data

    async def async_step_init(self, user_input: dict[str, str] | None = None) -> Any:
        return self.async_show_menu(step_id="init", menu_options=["keep", "restore"])

    async def _apply(self, action: str) -> Any:
        from . import device_contract_ha

        entry = self.hass.config_entries.async_get_entry(
            str(self._drift.get("entry_id"))
        )
        if entry is None:
            return self.async_abort(reason="no_longer_applies")
        do = (
            device_contract_ha.async_keep
            if action == "keep"
            else device_contract_ha.async_restore
        )
        problem = await do(
            self.hass,
            entry,
            str(self._drift.get("part")),
            self._drift.get("subentry_id"),
            str(self._drift.get("key")),
        )
        if problem:
            return self.async_abort(reason=problem)
        return self.async_create_entry(data={})

    async def async_step_keep(self, user_input: dict[str, str] | None = None) -> Any:
        return await self._apply("keep")

    async def async_step_restore(self, user_input: dict[str, str] | None = None) -> Any:
        return await self._apply("restore")


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    if data and data.get("kind") == FIX_FLOW_KIND:
        return SetupFillRepairFlow(dict(data))
    if data and data.get("kind") == "device_contract_drift":
        return DeviceContractDriftFlow(dict(data))
    return ConfirmRepairFlow()
