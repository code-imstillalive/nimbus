"""Controllable Load subentry flow -- adds one LP-scheduled load to an
already-installed Nimbus hub (nimbus issue #486, sub-issue 10 of Mark
Purcell's controllable-loads spec #476).

Distinct from a plain Load subentry (flows/load_subentry.py): a Load is
forecasted but always treated by the Solver as fixed, unavoidable demand.
A Controllable Load is something the Solver actually decides the timing
or level of -- a pool pump the Solver can shed under price pressure
(kind=sheddable, SheddableLoadConfig), a hot-water system with a real
deadline (kind=deferrable, AdequacyLoadConfig, #477's soft-shortfall
version), or a load whose "must heat every day" guarantee is a genuine
HARD LP constraint on a real temperature state variable (kind=thermal,
ThermalLoadConfig, nimbus issue #774).

Single flat step, deliberately -- see SUBENTRY_TYPE_CONTROLLABLE_LOAD's
own comment in const.py for why only these three kinds are offered yet.
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
    CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE,
    CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
    CONF_CONTROLLABLE_LOAD_KIND,
    CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
    CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
    CONF_CONTROLLABLE_LOAD_NAME,
    CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES,
    CONF_DEFERRABLE_DEADLINE_HOUR,
    CONF_DEFERRABLE_DONE_WHEN,
    CONF_DEFERRABLE_EARLIEST_HOUR,
    CONF_DEFERRABLE_MAX_KWH_PER_DAY,
    CONF_DEFERRABLE_MAX_POWER_KW,
    CONF_DEFERRABLE_SHORTFALL_PRICE,
    CONF_DEFERRABLE_TARGET_KWH,
    CONF_DEFERRABLE_VALUE_PER_KWH,
    CONF_SHEDDABLE_MIN_FRACTION,
    CONF_SHEDDABLE_NOMINAL_KW,
    CONF_SHEDDABLE_SHED_COST,
    CONF_THERMAL_COMFORT_FLOOR_C,
    CONF_THERMAL_COMFORT_FLOOR_COST,
    CONF_THERMAL_DEADLINE_HOUR,
    CONF_THERMAL_EARLIEST_HOUR,
    CONF_THERMAL_HEATING_RATE_C_PER_KWH,
    CONF_THERMAL_IDLE_DECAY_C_PER_HOUR,
    CONF_THERMAL_MAX_POWER_KW,
    CONF_THERMAL_TARGET_TEMPERATURE_C,
    CONTROLLABLE_LOAD_KIND_DEFERRABLE,
    CONTROLLABLE_LOAD_KIND_SHEDDABLE,
    CONTROLLABLE_LOAD_KIND_THERMAL,
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
_CELSIUS_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="°C"
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
                    CONTROLLABLE_LOAD_KIND_THERMAL,
                ],
                translation_key=CONF_CONTROLLABLE_LOAD_KIND,
            )
        ),
    }
    # nimbus issue #809 (Mark Purcell): the wizard used to ask for FOUR
    # separate entity-selector fields -- this one, plus thermal's own
    # temperature entity and deferrable's own done entity -- when in the
    # overwhelmingly common real case (confirmed live: Mark's own "Hot
    # Water Heat Pump" load) every one of them is the identical entity,
    # typed in three or four times over. Real power sensor is no longer
    # asked here at all (there's no single real device this load's
    # commanded entity always shares with its own power draw the way
    # temperature/done-condition do -- a plug/CT power sensor is
    # frequently a genuinely different entity_id) -- it stays a valid,
    # optional data key, settable via the more advanced `nimbus_load.
    # set_controllable_load` service for a household that wants that
    # extra monitoring/tracking fidelity, just not asked by this wizard's
    # own first-run form.
    #
    # The real device this load is commanded through -- no domain
    # restriction on the selector itself (switch/water_heater/climate,
    # per dispatch_commanded_state()'s own domain-pluggable design in
    # solver_writer.py). The one entity field this wizard still asks for.
    _optional_field(
        schema_dict,
        CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
        defaults.get(CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY),
        selector.EntitySelector(),
    )
    # nimbus issue #756: only meaningful when the device entity above is
    # climate.* -- a fixed, real dropdown over HA's own HVACMode enum
    # (never inferred from the entity's own supported hvac_modes, which
    # this static schema has no live access to at wizard-build time, and
    # would risk silently offering a mode the real device doesn't
    # actually support). Left blank for a switch/water_heater device
    # entity, or for a climate device that should only ever be turned
    # OFF by this load (no ambiguity there -- "off" needs no configured
    # mode). See CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE's own
    # const.py comment for why no safe default exists.
    _optional_field(
        schema_dict,
        CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE,
        defaults.get(CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE),
        selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["heat", "cool", "heat_cool", "auto", "dry", "fan_only"],
                translation_key=CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE,
            )
        ),
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
    # nimbus issue #875: how long this load may visibly disagree with its
    # own command before Nimbus re-sends it. 0 disables re-sending for this
    # load; unset uses the 15-minute default. A re-send never counts
    # against the activations/day cap above.
    _optional_field(
        schema_dict,
        CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES,
        defaults.get(CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES),
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="min"
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
    _optional_field(
        schema_dict,
        CONF_DEFERRABLE_MAX_KWH_PER_DAY,
        defaults.get(CONF_DEFERRABLE_MAX_KWH_PER_DAY),
        _KWH_SELECTOR,
    )
    # kind=thermal fields (nimbus issue #774) -- see solver.elements.
    # ThermalLoadConfig's own docstring for the full design.
    #
    # nimbus issue #809: no longer a separate wizard field -- the live
    # temperature reading defaults to this load's own device_entity
    # above (see solver_writer.py's build_controllable_loads() for the
    # actual resolution), which is already restricted to a real
    # commandable entity and, for a genuine thermal load, is a water_
    # heater/climate the overwhelming majority of the time anyway.
    _optional_field(
        schema_dict,
        CONF_THERMAL_MAX_POWER_KW,
        defaults.get(CONF_THERMAL_MAX_POWER_KW),
        _KW_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_THERMAL_TARGET_TEMPERATURE_C,
        defaults.get(CONF_THERMAL_TARGET_TEMPERATURE_C),
        _CELSIUS_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_THERMAL_EARLIEST_HOUR,
        defaults.get(CONF_THERMAL_EARLIEST_HOUR),
        _HOUR_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_THERMAL_DEADLINE_HOUR,
        defaults.get(CONF_THERMAL_DEADLINE_HOUR),
        _HOUR_SELECTOR,
    )
    # Tier 3 of Mark's own objective hierarchy -- see CONF_THERMAL_
    # COMFORT_FLOOR_C's own const.py comment. Both optional, left blank
    # means no mid-day reheat pressure at all.
    _optional_field(
        schema_dict,
        CONF_THERMAL_COMFORT_FLOOR_C,
        defaults.get(CONF_THERMAL_COMFORT_FLOOR_C),
        _CELSIUS_SELECTOR,
    )
    _optional_field(
        schema_dict,
        CONF_THERMAL_COMFORT_FLOOR_COST,
        defaults.get(CONF_THERMAL_COMFORT_FLOOR_COST),
        _DOLLAR_PER_KWH_SELECTOR,
    )
    # Optional overrides of thermal_forecast.py's own learn_thermal_
    # rates() -- left blank (the default) uses the learned value exactly
    # as the dashboard's own temperature-forecast display already does.
    _optional_field(
        schema_dict,
        CONF_THERMAL_HEATING_RATE_C_PER_KWH,
        defaults.get(CONF_THERMAL_HEATING_RATE_C_PER_KWH),
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="°C/kWh",
            )
        ),
    )
    _optional_field(
        schema_dict,
        CONF_THERMAL_IDLE_DECAY_C_PER_HOUR,
        defaults.get(CONF_THERMAL_IDLE_DECAY_C_PER_HOUR),
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="°C/h"
            )
        ),
    )
    # nimbus issue #480: a binary_sensor's own "on" state IS the done
    # condition (done_when left blank); any other domain needs done_when
    # too, to know what "done" means for a numeric reading.
    #
    # nimbus issue #809: no longer a separate wizard field for the done
    # ENTITY itself -- defaults to this load's own device_entity above
    # (solver_writer.py's build_controllable_loads()), the overwhelmingly
    # common real case (a water_heater/climate load's own done condition
    # is its own current_temperature). done_when (below) stays a real,
    # separate, still-configurable field -- a household with a genuinely
    # different done SENSOR (a separate binary_sensor or numeric sensor,
    # not the commanded device itself) sets it via the more advanced
    # `nimbus_load.set_controllable_load` service instead of this wizard.
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
