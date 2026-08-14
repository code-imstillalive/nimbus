"""Load subentry flow -- adds one load to an already-installed Nimbus hub.

Reached via the "+ Add" button on the Nimbus hub's own device page, not via
"Add Integration" -- this is what makes adding many loads (18 circuit
breakers, in the case this was built for) fast: pick a sensor, submit, done,
repeat as many times as needed, no restart between them.

One screen, not a separate options step: the sensor pickers AND the tuning
knobs (forecast horizon, retrain hour, training window) are all on this same
form, with the tuning knobs already prefilled with sensible defaults so they
can be left alone entirely for a quick add.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigSubentryFlow, SubentryFlowResult
from homeassistant.helpers import selector
import voluptuous as vol

from ..const import (
    CONF_FORECAST_HORIZON_HOURS,
    CONF_LOAD_SENSOR,
    CONF_RETRAIN_HOUR_LOCAL,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_TRAIN_DAYS,
    DEFAULT_FORECAST_HORIZON_HOURS,
    DEFAULT_RETRAIN_HOUR_LOCAL,
    DEFAULT_TRAIN_DAYS,
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_LOAD_SENSOR, default=defaults.get(CONF_LOAD_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_TEMPERATURE_SENSOR, default=defaults.get(CONF_TEMPERATURE_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_TEMPERATURE_FORECAST_SENSOR,
                default=defaults.get(CONF_TEMPERATURE_FORECAST_SENSOR),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_FORECAST_HORIZON_HOURS,
                default=defaults.get(CONF_FORECAST_HORIZON_HOURS, DEFAULT_FORECAST_HORIZON_HOURS),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=168)),
            vol.Optional(
                CONF_RETRAIN_HOUR_LOCAL,
                default=defaults.get(CONF_RETRAIN_HOUR_LOCAL, DEFAULT_RETRAIN_HOUR_LOCAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
            vol.Optional(
                CONF_TRAIN_DAYS,
                default=defaults.get(CONF_TRAIN_DAYS, DEFAULT_TRAIN_DAYS),
            ): vol.All(vol.Coerce(int), vol.Range(min=7, max=180)),
        }
    )


class NimbusLoadSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one load under the Nimbus hub."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """New load -- reached via the hub device page's "+ Add" button."""
        return await self._async_step(user_input, subentry=None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit an existing load."""
        subentry = self._get_reconfigure_subentry()
        return await self._async_step(user_input, subentry=subentry)

    async def _async_step(
        self, user_input: dict[str, Any] | None, subentry: Any
    ) -> SubentryFlowResult:
        current_data = dict(subentry.data) if subentry is not None else {}

        if user_input is not None:
            title = f"Load ({user_input[CONF_LOAD_SENSOR]})"
            if subentry is not None:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema(current_data))
