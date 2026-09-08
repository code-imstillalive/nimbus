"""Battery Participant subentry flow -- adds one ADDITIONAL, independently-
metered battery/EV to an already-installed Nimbus hub (nimbus issue #563,
the config surface for #467 stage 1's own `batteries: list[BatteryConfig]`
solver support).

Distinct from a Battery Tower subentry (flows/battery_tower_subentry.py):
a Battery Tower is pure topology/wiring metadata for the dashboard's
topology card, never read by the Solver. A Battery Participant is a real
LP dispatch participant -- see const.py's own SUBENTRY_TYPE_BATTERY_
PARTICIPANT comment for the full "why a separate type" reasoning.

The household's own single, existing hub-level battery (the Solver
settings wizard's own capacity/SoC/power fields) always stays the "home"
participant -- these subentries are ADDITIONAL participants on top of it,
never a replacement. Zero subentries is a real no-op (see solver_writer.py's
own build_extra_batteries()).
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
    CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
    CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
    CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
    CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
    CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
    CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
    CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
    CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
    CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
    CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
    CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
    CONF_BATTERY_PARTICIPANT_NAME,
    CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
    CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
    CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
    CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
    CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
    CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
)

_KW_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kW"
    )
)
_KWH_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="kWh"
    )
)
_PERCENT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=100,
        step=0.1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="%",
    )
)
_DOLLAR_PER_KWH_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="$/kWh"
    )
)
# Same convention as the existing Solver P2P block start/end hour
# fields (number.py's own P2P Block N Start/End Hour) -- a plain
# whole-hour integer, 0-23.
_HOUR_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=23,
        step=1,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="hour",
    )
)


def _optional_field(
    schema_dict: dict[Any, Any], key: str, default: Any, field_selector: Any
) -> None:
    """Same None-default-crashes-the-frontend fix as controllable_load_
    subentry.py's own helper -- see that module's comment (ha-selector-
    number calls .toString() on the default with no null-check)."""
    if default is not None:
        schema_dict[vol.Optional(key, default=default)] = field_selector
    else:
        schema_dict[vol.Optional(key)] = field_selector


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    schema_dict: dict[Any, Any] = {
        vol.Required(
            CONF_BATTERY_PARTICIPANT_NAME,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_NAME),
        ): selector.TextSelector(),
        vol.Required(
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_CAPACITY_KWH),
        ): _KWH_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_SOC_SENSOR),
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Required(
            CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_POWER_SENSOR),
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Required(
            CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
            default=defaults.get(
                CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE, True
            ),
        ): selector.BooleanSelector(),
        vol.Required(
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW),
        ): _KW_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW),
        ): _KW_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT, 10.0),
        ): _PERCENT_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT, 100.0),
        ): _PERCENT_SELECTOR,
        vol.Required(
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            default=defaults.get(CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT, 95.0),
        ): _PERCENT_SELECTOR,
    }
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
        defaults.get(CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE),
        _DOLLAR_PER_KWH_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
        defaults.get(CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH),
        _DOLLAR_PER_KWH_SELECTOR,
    )
    # #563 item 4: an optional live number entity whose current value
    # overrides this participant's own max_soc_percent for that solve --
    # see const.py's own comment on this field for the real "car usually
    # charged to 80%, occasionally set to 100%" reasoning.
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
        defaults.get(CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY),
        selector.EntitySelector(selector.EntitySelectorConfig(domain="number")),
    )
    # #563 item 2: availability gating -- an optional binary_sensor
    # (e.g. "located at home", "charge cable connected") whose CURRENT
    # state gates this participant's whole-solve charge/discharge -- see
    # const.py's own comment on this field for the real EV case this
    # exists for.
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
        defaults.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY),
        selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
    )
    # #563 item 2, the departure-deadline half -- both fields optional,
    # and must be set together (see build_extra_batteries()'s own
    # reconciliation -- setting only one is treated as "neither set",
    # not an error, since BatteryConfig.__post_init__ itself requires
    # both-or-neither).
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
        defaults.get(CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR),
        _HOUR_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
        defaults.get(CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT),
        _PERCENT_SELECTOR,
    )
    # #563 item 3: the shared-charger power constraint -- a free-text
    # group name (two participants with the SAME name share one real
    # physical charger) plus that group's own kW ceiling. See const.py's
    # own comment on these fields for the real "two Teslas on one Sigen
    # DC charger" case.
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
        defaults.get(CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP),
        selector.TextSelector(),
    )
    _optional_field(
        schema_dict,
        CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
        defaults.get(CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW),
        _KW_SELECTOR,
    )
    return vol.Schema(schema_dict)


class NimbusBatteryParticipantSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one additional battery/EV participant under
    the Nimbus hub. Same self.source-driven reconfigure-vs-fresh-add
    pattern as every other subentry flow in this project -- see
    load_subentry.py's own async_step_user docstring for the real
    HA-behavior finding this is built on."""

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

        if user_input is not None:
            title = user_input[CONF_BATTERY_PARTICIPANT_NAME]
            if subentry is not None:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema(current_data))
