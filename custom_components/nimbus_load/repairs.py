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


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    if data and data.get("kind") == FIX_FLOW_KIND:
        return SetupFillRepairFlow(dict(data))
    return ConfirmRepairFlow()
