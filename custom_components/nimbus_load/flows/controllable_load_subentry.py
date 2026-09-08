"""Controllable Load subentry flow -- adds one LP-scheduled load to an
already-installed Nimbus hub (nimbus issue #486, sub-issue 10 of Mark
Purcell's controllable-loads spec #476).

Distinct from a plain Load subentry (flows/load_subentry.py): a Load is
forecasted but always treated by the Solver as fixed, unavoidable demand.
A Controllable Load is something the Solver actually decides the timing
or level of -- a pool pump the Solver can shed under price pressure
(kind=sheddable, SheddableLoadConfig), or a hot-water system with a real
deadline (kind=deferrable, AdequacyLoadConfig, #477's soft-shortfall
version).

Single flat step, deliberately -- see SUBENTRY_TYPE_CONTROLLABLE_LOAD's
own comment in const.py for why only these two kinds are offered yet.
Every field beyond `kind` itself is only ever read by solver_writer.py
if the selected kind actually uses it (see build_controllable_loads()
there) -- leaving a sheddable load's deferrable_* fields blank (or vice
versa) is a no-op, not a validation error, so one schema covering both
kinds is simpler than a second "pick kind, then a kind-specific step"
round trip for what is, today, a two-way branch.
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
    CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
    CONF_CONTROLLABLE_LOAD_KIND,
    CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
    CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
    CONF_CONTROLLABLE_LOAD_NAME,
    CONF_CONTROLLABLE_LOAD_POWER_SENSOR,
    CONF_DEFERRABLE_DEADLINE_HOUR,
    CONF_DEFERRABLE_DONE_ENTITY,
    CONF_DEFERRABLE_DONE_WHEN,
    CONF_DEFERRABLE_EARLIEST_HOUR,
    CONF_DEFERRABLE_MAX_POWER_KW,
    CONF_DEFERRABLE_SHORTFALL_PRICE,
    CONF_DEFERRABLE_TARGET_KWH,
    CONF_DEFERRABLE_VALUE_PER_KWH,
    CONF_SHEDDABLE_MIN_FRACTION,
    CONF_SHEDDABLE_NOMINAL_KW,
    CONF_SHEDDABLE_SHED_COST,
    CONTROLLABLE_LOAD_KIND_DEFERRABLE,
    CONTROLLABLE_LOAD_KIND_SHEDDABLE,
)

# Matches DEFAULT_SHED_COST / DEFAULT_ADEQUACY_SHORTFALL_PRICE
# (solver/elements.py) -- shown here as the wizard's own default so a
# household filling this in sees the same "behaves like a hard
# constraint at any real price" reference value the solver itself uses
# when nothing else is specified, not an arbitrary placeholder.
_DEFAULT_SHED_COST = 2.00
_DEFAULT_SHORTFALL_PRICE = 10.00

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
_DOLLAR_PER_KWH_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="$/kWh"
    )
)
# Same 24hr-decimal, no-AM/PM-ambiguity choice as load_subentry.py's own
# _HOUR_SELECTOR -- see that module's comment for the real bug (silent
# midnight-vs-noon mixup on a 12-hour TimeSelector) this avoids.
_HOUR_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=23.75,
        step=0.25,
        mode=selector.NumberSelectorMode.BOX,
        unit_of_measurement="24hr decimal, e.g. 12.5 = 12:30pm, 0 = midnight",
    )
)


def _optional_field(
    schema_dict: dict[Any, Any], key: str, default: Any, field_selector: Any
) -> None:
    """vol.Optional() with no `default=` kwarg when the field has never
    been set -- same None-default-crashes-the-frontend fix as
    load_subentry.py's own schema builder (confirmed live 2026-08-15,
    ha-selector-number calls .toString() on the default with no
    null-check). Kept as one shared helper here since this schema has
    six such fields, not two."""
    if default is not None:
        schema_dict[vol.Optional(key, default=default)] = field_selector
    else:
        schema_dict[vol.Optional(key)] = field_selector


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    schema_dict: dict[Any, Any] = {
        vol.Required(
            CONF_CONTROLLABLE_LOAD_NAME,
            default=defaults.get(CONF_CONTROLLABLE_LOAD_NAME),
        ): selector.TextSelector(),
        vol.Required(
            CONF_CONTROLLABLE_LOAD_KIND,
            default=defaults.get(
                CONF_CONTROLLABLE_LOAD_KIND, CONTROLLABLE_LOAD_KIND_SHEDDABLE
            ),
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    CONTROLLABLE_LOAD_KIND_SHEDDABLE,
                    CONTROLLABLE_LOAD_KIND_DEFERRABLE,
                ],
                translation_key=CONF_CONTROLLABLE_LOAD_KIND,
            )
        ),
    }
    _optional_field(
        schema_dict,
        CONF_CONTROLLABLE_LOAD_POWER_SENSOR,
        defaults.get(CONF_CONTROLLABLE_LOAD_POWER_SENSOR),
        selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
    )
    # nimbus issue #534: the real device this load is commanded through --
    # no domain restriction on the selector itself (switch/water_heater
    # today, climate expected later, per dispatch_commanded_state()'s own
    # domain-pluggable design in solver_writer.py).
    _optional_field(
        schema_dict,
        CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
        defaults.get(CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY),
        selector.EntitySelector(),
    )
    _optional_field(
        schema_dict,
        CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
        defaults.get(CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES),
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"
            )
        ),
    )
    _optional_field(
        schema_dict,
        CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
        defaults.get(CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY),
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, step=1, mode=selector.NumberSelectorMode.BOX
            )
        ),
    )
    # kind=sheddable fields
    _optional_field(
        schema_dict,
        CONF_SHEDDABLE_NOMINAL_KW,
        defaults.get(CONF_SHEDDABLE_NOMINAL_KW),
        _KW_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_SHEDDABLE_MIN_FRACTION,
        defaults.get(CONF_SHEDDABLE_MIN_FRACTION),
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=1, step=0.05, mode=selector.NumberSelectorMode.BOX
            )
        ),
    )
    _optional_field(
        schema_dict,
        CONF_SHEDDABLE_SHED_COST,
        defaults.get(CONF_SHEDDABLE_SHED_COST, _DEFAULT_SHED_COST),
        _DOLLAR_PER_KWH_SELECTOR,
    )
    # kind=deferrable fields
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_MAX_POWER_KW,
        defaults.get(CONF_DEFERRABLE_MAX_POWER_KW),
        _KW_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_TARGET_KWH,
        defaults.get(CONF_DEFERRABLE_TARGET_KWH),
        _KWH_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_EARLIEST_HOUR,
        defaults.get(CONF_DEFERRABLE_EARLIEST_HOUR),
        _HOUR_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_DEADLINE_HOUR,
        defaults.get(CONF_DEFERRABLE_DEADLINE_HOUR),
        _HOUR_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_SHORTFALL_PRICE,
        defaults.get(CONF_DEFERRABLE_SHORTFALL_PRICE, _DEFAULT_SHORTFALL_PRICE),
        _DOLLAR_PER_KWH_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_VALUE_PER_KWH,
        defaults.get(CONF_DEFERRABLE_VALUE_PER_KWH),
        _DOLLAR_PER_KWH_SELECTOR,
    )
    # nimbus issue #480: a binary_sensor's own "on" state IS the done
    # condition (done_when left blank); any other domain needs done_when
    # too, to know what "done" means for a numeric reading. No domain
    # restriction on the EntitySelector itself -- binary_sensor and a
    # plain numeric sensor (tank temperature, etc.) are both real,
    # expected choices.
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_DONE_ENTITY,
        defaults.get(CONF_DEFERRABLE_DONE_ENTITY),
        selector.EntitySelector(),
    )
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_DONE_WHEN,
        defaults.get(CONF_DEFERRABLE_DONE_WHEN),
        selector.TextSelector(),
    )
    return vol.Schema(schema_dict)


class NimbusControllableLoadSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one controllable load under the Nimbus hub.
    Same self.source-driven reconfigure-vs-fresh-add pattern as every
    other subentry flow in this project -- see load_subentry.py's own
    async_step_user docstring for the real HA-behavior finding this is
    built on (async_step_reconfigure is not actually a separate
    invocation path for subentry flows)."""

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
            title = user_input[CONF_CONTROLLABLE_LOAD_NAME]
            if subentry is not None:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema(current_data))
