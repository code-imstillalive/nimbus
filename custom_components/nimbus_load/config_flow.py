"""Config flow for Nimbus.

Deliberately minimal on first setup -- one required entity picker, two
optional ones, everything else defaulted. The knobs that exist purely for
tuning (horizon, retrain hour, training window) live in the Options flow so
they don't get in the way of a first-run setup.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_FORECAST_HORIZON_HOURS,
    CONF_LOAD_SENSOR,
    CONF_RETRAIN_HOUR_LOCAL,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_TRAIN_DAYS,
    DEFAULT_FORECAST_HORIZON_HOURS,
    DEFAULT_RETRAIN_HOUR_LOCAL,
    DEFAULT_TRAIN_DAYS,
    DOMAIN,
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LOAD_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(CONF_TEMPERATURE_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(CONF_TEMPERATURE_FORECAST_SENSOR): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor")
        ),
    }
)


class NimbusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Nimbus."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """First (and only, for setup) step: pick the sensor to learn from."""
        errors: dict[str, str] = {}
        if user_input is not None:
            load_sensor = user_input[CONF_LOAD_SENSOR]
            # One Nimbus instance per load sensor -- prevents accidentally
            # configuring the same sensor twice.
            await self.async_set_unique_id(load_sensor)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Nimbus ({load_sensor})",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NimbusOptionsFlow:
        """Return the options flow for tuning after initial setup."""
        return NimbusOptionsFlow()


class NimbusOptionsFlow(config_entries.OptionsFlow):
    """Post-setup tuning knobs: forecast horizon, retrain time, training window.

    Relies on the base OptionsFlow class's own `config_entry` property
    (current Home Assistant config_entries API) rather than storing it
    manually in __init__, which is deprecated on recent HA versions.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_FORECAST_HORIZON_HOURS,
                    default=options.get(
                        CONF_FORECAST_HORIZON_HOURS, DEFAULT_FORECAST_HORIZON_HOURS
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
                vol.Optional(
                    CONF_RETRAIN_HOUR_LOCAL,
                    default=options.get(
                        CONF_RETRAIN_HOUR_LOCAL, DEFAULT_RETRAIN_HOUR_LOCAL
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
                vol.Optional(
                    CONF_TRAIN_DAYS,
                    default=options.get(CONF_TRAIN_DAYS, DEFAULT_TRAIN_DAYS),
                ): vol.All(vol.Coerce(int), vol.Range(min=7, max=180)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
