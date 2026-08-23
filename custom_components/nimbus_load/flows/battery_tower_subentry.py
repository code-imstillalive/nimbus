"""Battery Tower subentry flow -- adds one real physical battery pack/
tower for the topology dashboard card. Reached via the "+ Add" button on
the Nimbus hub's own device page, same mechanism as Load/Power Signal.

Pure wiring/topology metadata -- NOT a forecasting target. Only the 4
fields topology-card-v4.js's own _batteryBox() actually renders (SoC,
SoH, Voltage, Temperature -- confirmed by reading that function
directly, 2026-08-23, rather than assuming this household's own old
hardcoded prefix convention -- Current/Status/lifetime-charge/lifetime-
discharge were real Sungrow register readings that were never actually
displayed). power_source is OPTIONAL, same reasoning as pv_string_
subentry.py's own identical field.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.helpers import selector
import voluptuous as vol

from ..const import (
    CONF_BATTERY_TOWER_POWER_SOURCE,
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_BATTERY_TOWER_SOH_SENSOR,
    CONF_BATTERY_TOWER_TEMPERATURE_SENSOR,
    CONF_BATTERY_TOWER_VOLTAGE_SENSOR,
    SUBENTRY_TYPE_POWER_SOURCE,
)

_TITLE_FALLBACK = "Battery Tower"


def _power_source_options(entry: Any) -> list[dict[str, str]]:
    """Same real, live dropdown-building helper as pv_string_subentry.py's
    own identical function -- kept as a separate copy (not a shared
    import) since each subentry flow file in this integration is
    deliberately self-contained, matching the existing load_subentry.py/
    signal_subentry.py convention."""
    return [
        {"value": sub.subentry_id, "label": sub.title}
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_POWER_SOURCE
    ]


def _schema(defaults: dict[str, Any], entry: Any) -> vol.Schema:
    entity_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )
    schema_dict: dict[Any, Any] = {
        # SoC is the single most important field for the diagram (the
        # visible fill-bar), but still genuinely Optional -- a household
        # mid-way through the wizard shouldn't hit a hard validation
        # error on a partially-filled-in tower.
        vol.Optional(
            CONF_BATTERY_TOWER_SOC_SENSOR,
            default=defaults.get(CONF_BATTERY_TOWER_SOC_SENSOR),
        ): entity_selector,
        vol.Optional(
            CONF_BATTERY_TOWER_SOH_SENSOR,
            default=defaults.get(CONF_BATTERY_TOWER_SOH_SENSOR),
        ): entity_selector,
        vol.Optional(
            CONF_BATTERY_TOWER_VOLTAGE_SENSOR,
            default=defaults.get(CONF_BATTERY_TOWER_VOLTAGE_SENSOR),
        ): entity_selector,
        vol.Optional(
            CONF_BATTERY_TOWER_TEMPERATURE_SENSOR,
            default=defaults.get(CONF_BATTERY_TOWER_TEMPERATURE_SENSOR),
        ): entity_selector,
    }
    schema_dict[
        vol.Optional(
            CONF_BATTERY_TOWER_POWER_SOURCE,
            default=defaults.get(CONF_BATTERY_TOWER_POWER_SOURCE),
        )
    ] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=_power_source_options(entry),
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    return vol.Schema(schema_dict)


class NimbusBatteryTowerSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one battery tower under the Nimbus hub."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
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
        entry = self._get_entry()

        if user_input is not None:
            title = self._derive_title(user_input.get(CONF_BATTERY_TOWER_SOC_SENSOR))
            if subentry is not None:
                return self.async_update_and_abort(
                    entry, subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=_schema(current_data, entry)
        )

    def _derive_title(self, soc_entity_id: str | None) -> str:
        """No single obviously-right field to name a battery tower after
        (unlike a load/PV string, which has one clear source sensor) --
        use the SoC sensor's own device name if given (usually something
        like "Battery Tower 2 SoC", strip the trailing " Soc" if present
        for a cleaner title), else a generic fallback the user can rename
        via the UI afterward.
        """
        if not soc_entity_id:
            return _TITLE_FALLBACK
        state = self.hass.states.get(soc_entity_id)
        if state is None:
            return _TITLE_FALLBACK
        friendly = state.attributes.get("friendly_name")
        if not friendly:
            return _TITLE_FALLBACK
        for suffix in (" SoC", " Soc", " soc"):
            if friendly.endswith(suffix):
                return friendly[: -len(suffix)]
        return friendly
