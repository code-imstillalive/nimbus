"""Battery Tower subentry flow -- adds one real physical battery pack/
tower for the topology dashboard card. Reached via the "+ Add" button on
the Nimbus hub's own device page, same mechanism as Load/Power Signal.

Pure wiring/topology metadata -- NOT a forecasting target. Only the 4
fields nimbus-topology-card.js's own _batteryBox() actually renders (SoC,
SoH, Voltage, Temperature -- confirmed by reading that function
directly, 2026-08-23, rather than assuming this household's own old
hardcoded prefix convention -- Current/Status/lifetime-charge/lifetime-
discharge were real Sungrow register readings that were never actually
displayed). power_source is OPTIONAL, same reasoning as pv_string_
subentry.py's own identical field.
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
    CONF_BATTERY_TOWER_POWER_SOURCE,
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_BATTERY_TOWER_SOH_SENSOR,
    CONF_BATTERY_TOWER_TEMPERATURE_SENSOR,
    CONF_BATTERY_TOWER_VOLTAGE_SENSOR,
    SUBENTRY_TYPE_POWER_SOURCE,
)

_TITLE_FALLBACK = "Battery Tower"


async def _energy_dashboard_soc_suggestion(hass: Any) -> str | None:
    """nimbus issue #554: a real household that has already told HA's
    own Energy Dashboard (Settings -> Energy) which entity carries its
    battery's State of Charge (the `stat_soc` field on a `battery`
    energy source) shouldn't have to type the same entity_id again here
    -- same `suggested_value` discipline as every other field in this
    wizard: visible, editable, never saved without a submit. Kept
    self-contained in this file rather than imported from hub_options.py's
    own sibling `_energy_dashboard_switchboard_suggestions()`, matching
    this file's own stated "each subentry flow file is deliberately
    self-contained" convention (see _power_source_options()'s own
    docstring above).

    Returns the FIRST real, type-safe candidate found -- a real
    Energy Dashboard can have more than one `battery` source (a home
    pack and a separately-tracked EV charger, say) and there is no way
    to know mid-wizard which physical tower THIS subentry represents;
    same posture as every other suggestion in this codebase, a human
    still confirms or corrects it before it's ever saved. None (not a
    fabricated guess) when the Energy Dashboard isn't configured, has
    no battery source, or its `stat_soc` entity doesn't genuinely look
    like a real SoC reading.

    Uses homeassistant.components.energy.data.async_get_manager() --
    genuinely internal HA core API, not a stable public contract, same
    "must never break the wizard" reasoning as the switchboard
    suggestions -- any failure degrades to no suggestion, silently.
    """
    try:
        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(hass)
        sources = (manager.data or {}).get("energy_sources", [])
        for source in sources:
            if source.get("type") != "battery":
                continue
            entity_id = source.get("stat_soc")
            if not entity_id:
                continue
            state = hass.states.get(entity_id)
            if state is None:
                continue
            attrs = state.attributes
            if attrs.get("device_class") != "battery":
                continue
            if attrs.get("unit_of_measurement") != "%":
                continue
            return entity_id
    except Exception:  # noqa: BLE001 -- see docstring: must never break the wizard
        return None
    return None


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


def _schema(
    defaults: dict[str, Any], entry: Any, soc_suggestion: str | None = None
) -> vol.Schema:
    entity_selector = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor")
    )
    # nimbus issue #339: every picker here is Optional and pre-filled via
    # description={"suggested_value": ...}, never default=. A default=None
    # (fresh add) is injected by voluptuous when the field is left blank
    # and rejected by EntitySelector/SelectSelector, so a partially-filled
    # tower could never be saved and a set field could never be cleared;
    # a saved parent Power Source that has since been deleted would be
    # re-injected into a dropdown that no longer offers it ("value must
    # be one of []"), bricking reconfigure of this tower.
    schema_dict: dict[Any, Any] = {
        # SoC is the single most important field for the diagram (the
        # visible fill-bar), but still genuinely Optional -- a household
        # mid-way through the wizard shouldn't hit a hard validation
        # error on a partially-filled-in tower.
        #
        # nimbus issue #554: a real saved value always wins (same
        # safeguard-2 discipline as _energy_dashboard_switchboard_
        # suggestions() in hub_options.py) -- the Energy Dashboard
        # suggestion only ever fills in when this field is genuinely
        # unset, and it's still only a suggested_value the household
        # can edit or clear before ever submitting.
        vol.Optional(
            CONF_BATTERY_TOWER_SOC_SENSOR,
            description={
                "suggested_value": defaults.get(CONF_BATTERY_TOWER_SOC_SENSOR)
                or soc_suggestion
            },
        ): entity_selector,
        _optional(CONF_BATTERY_TOWER_SOH_SENSOR, defaults): entity_selector,
        _optional(CONF_BATTERY_TOWER_VOLTAGE_SENSOR, defaults): entity_selector,
        _optional(CONF_BATTERY_TOWER_TEMPERATURE_SENSOR, defaults): entity_selector,
    }
    options = _power_source_options(entry)
    saved_parent = defaults.get(CONF_BATTERY_TOWER_POWER_SOURCE)
    if saved_parent not in {o["value"] for o in options}:
        saved_parent = None  # parent deleted since -- don't re-inject it
    schema_dict[
        vol.Optional(
            CONF_BATTERY_TOWER_POWER_SOURCE,
            description={"suggested_value": saved_parent},
        )
    ] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    return vol.Schema(schema_dict)


def _optional(key: str, defaults: dict[str, Any]) -> vol.Optional:
    """Optional picker: saved value offered as a suggestion, nothing
    injected when blank -- see the #339 comment in _schema()."""
    return vol.Optional(key, description={"suggested_value": defaults.get(key)})


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

        # nimbus issue #554: only worth the Energy Dashboard lookup when
        # this tower doesn't already have a saved SoC sensor -- a real
        # saved value always wins anyway (see _schema()'s own comment),
        # so skip the async call entirely on a reconfigure of an
        # already-filled-in tower.
        soc_suggestion = None
        if not current_data.get(CONF_BATTERY_TOWER_SOC_SENSOR):
            soc_suggestion = await _energy_dashboard_soc_suggestion(self.hass)
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(current_data, entry, soc_suggestion),
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
