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

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.helpers import selector

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
    # to give at all. nimbus issue #339: these MUST use
    # description={"suggested_value": ...}, never default=. A
    # `default=None` is injected by voluptuous whenever the picker is
    # left blank and then handed to EntitySelector, which rejects it
    # ("Entity None is neither a valid entity ID nor a valid UUID") --
    # so a PV-only source could never be created, and a set sensor could
    # never be cleared (the saved default was re-injected on omission).
    # Same fix hub_options.py already carries for #113/#114.
    schema_dict[_optional_entity(CONF_POWER_SOURCE_BATTERY_SENSOR, defaults)] = (
        selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    )
    schema_dict[_optional_entity(CONF_POWER_SOURCE_DC_SENSOR, defaults)] = (
        selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    )
    return vol.Schema(schema_dict)


def _optional_entity(key: str, defaults: dict[str, Any]) -> vol.Optional:
    """An optional picker that pre-fills the saved value as a suggestion
    (submitted back unchanged if the user leaves it alone) but injects
    nothing when blank, so it validates cleanly and can be cleared."""
    return vol.Optional(key, description={"suggested_value": defaults.get(key)})


class NimbusPowerSourceSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one Power Source under the Nimbus hub."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Same self.source-driven reconfigure-detection as every other
        Nimbus subentry flow (see load_subentry.py's own identical
        comment for why)."""
        subentry = (
            self._get_reconfigure_subentry()
            if self.source == SOURCE_RECONFIGURE
            else None
        )
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
