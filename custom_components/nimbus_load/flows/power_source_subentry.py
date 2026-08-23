"""Power Source subentry flow -- adds one real hardware unit (an inverter,
a hybrid battery/inverter, a battery-only BMS) that connects to the
switchboard, for the topology dashboard card. Reached via the "+ Add"
button on the Nimbus hub's own device page, same mechanism as Load/Power
Signal.

Pure wiring/topology metadata -- NOT a forecasting target. No ML model,
no coordinator, no _forecast sensor. See const.py's own comment above
SUBENTRY_TYPE_POWER_SOURCE for the full "why Power Source, not Inverter"
reasoning (a real household's own hardware can be PV-only, battery-only,
or hybrid; PV/battery towers optionally link to one of these, they don't
have to).
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.helpers import selector
import voluptuous as vol

from ..const import (
    CONF_POWER_SOURCE_BATTERY_SENSOR,
    CONF_POWER_SOURCE_DC_SENSOR,
    CONF_POWER_SOURCE_NAME,
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    schema_dict: dict[Any, Any] = {
        vol.Required(
            CONF_POWER_SOURCE_NAME, default=defaults.get(CONF_POWER_SOURCE_NAME, "")
        ): selector.TextSelector(),
    }
    # Both genuinely optional -- a PV-only power source (e.g. Mark
    # Purcell's own separate SolarEdge unit) has no real battery power
    # to give at all. Same None-default crash avoidance already proven
    # in load_subentry.py (2026-08-15 finding: an EntitySelector is safe
    # with default=None, unlike a NumberSelector).
    schema_dict[vol.Optional(
        CONF_POWER_SOURCE_BATTERY_SENSOR, default=defaults.get(CONF_POWER_SOURCE_BATTERY_SENSOR)
    )] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    schema_dict[vol.Optional(
        CONF_POWER_SOURCE_DC_SENSOR, default=defaults.get(CONF_POWER_SOURCE_DC_SENSOR)
    )] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    return vol.Schema(schema_dict)


class NimbusPowerSourceSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one Power Source under the Nimbus hub."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Same self.source-driven reconfigure-detection as every other
        Nimbus subentry flow (see load_subentry.py's own identical
        comment for why)."""
        subentry = self._get_reconfigure_subentry() if self.source == SOURCE_RECONFIGURE else None
        return await self._async_step(user_input, subentry=subentry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self.async_step_user(user_input)

    async def _async_step(
        self, user_input: dict[str, Any] | None, subentry: Any
    ) -> SubentryFlowResult:
        current_data = dict(subentry.data) if subentry is not None else {}

        if user_input is not None:
            title = user_input[CONF_POWER_SOURCE_NAME]
            if subentry is not None:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema(current_data))
