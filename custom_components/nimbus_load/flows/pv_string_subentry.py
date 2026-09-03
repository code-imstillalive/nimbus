"""PV String subentry flow -- adds one real physical PV string/array for
the topology dashboard card. Reached via the "+ Add" button on the
Nimbus hub's own device page, same mechanism as Load/Power Signal.

Pure wiring/topology metadata -- NOT a forecasting target. power_source
is deliberately OPTIONAL, not a required parent link -- see const.py's
own comment above SUBENTRY_TYPE_POWER_SOURCE for why (a real PV system
can be wired through a specific inverter, or be its own wholly
independent source, e.g. Mark Purcell's own separate SolarEdge unit).
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
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_LABEL,
    CONF_PV_STRING_POWER_SOURCE,
    SUBENTRY_TYPE_POWER_SOURCE,
)


def _power_source_options(entry: Any) -> list[dict[str, str]]:
    """Every currently-configured Power Source subentry, as a real,
    live-built dropdown option list -- keyed by subentry_id (stable,
    survives a rename) with the subentry's own current title as the
    human-readable label. Genuinely empty if no Power Source has been
    added yet -- not an error, the field stays selectable-but-blank."""
    return [
        {"value": sub.subentry_id, "label": sub.title}
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_TYPE_POWER_SOURCE
    ]


def _schema(defaults: dict[str, Any], entry: Any) -> vol.Schema:
    # nimbus issue #339: no `default=` on the entity/select pickers. A
    # `default=None` (fresh add) or a stale saved value is injected by
    # voluptuous on omission and rejected by the selector -- so a PV
    # string could not be created without a Power Source, a set parent
    # could not be cleared, and after the parent Power Source was deleted
    # the string could never be reconfigured again ("value must be one
    # of []"). suggested_value pre-fills without ever being injected.
    entity_key = (
        vol.Required(CONF_PV_STRING_ENTITY, default=defaults.get(CONF_PV_STRING_ENTITY))
        if defaults.get(CONF_PV_STRING_ENTITY) is not None
        else vol.Required(CONF_PV_STRING_ENTITY)
    )
    schema_dict: dict[Any, Any] = {
        entity_key: selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(
            CONF_PV_STRING_LABEL, default=defaults.get(CONF_PV_STRING_LABEL, "")
        ): selector.TextSelector(),
    }
    options = _power_source_options(entry)
    saved_parent = defaults.get(CONF_PV_STRING_POWER_SOURCE)
    if saved_parent not in {o["value"] for o in options}:
        saved_parent = None  # parent deleted since -- don't re-inject it
    schema_dict[
        vol.Optional(
            CONF_PV_STRING_POWER_SOURCE,
            description={"suggested_value": saved_parent},
        )
    ] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )
    return vol.Schema(schema_dict)


class NimbusPvStringSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one PV string under the Nimbus hub."""

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
            title = self._derive_title(
                user_input[CONF_PV_STRING_ENTITY], user_input.get(CONF_PV_STRING_LABEL)
            )
            if subentry is not None:
                return self.async_update_and_abort(
                    entry, subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=_schema(current_data, entry)
        )

    def _derive_title(self, entity_id: str, label: str | None) -> str:
        """Prefer the user's own free-text label if given (e.g. "West
        array") -- otherwise fall back to the source sensor's own
        friendly name, same convention as load_subentry.py's own
        _derive_title."""
        if label:
            return label
        state = self.hass.states.get(entity_id)
        if state is not None:
            friendly = state.attributes.get("friendly_name")
            if friendly:
                return friendly
        return entity_id
