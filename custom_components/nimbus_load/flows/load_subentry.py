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

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigSubentryFlow, SubentryFlowResult
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
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=168, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="hours",
                )
            ),
            vol.Optional(
                CONF_RETRAIN_HOUR_LOCAL,
                default=defaults.get(CONF_RETRAIN_HOUR_LOCAL, DEFAULT_RETRAIN_HOUR_LOCAL),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="hour of day (0-23)",
                )
            ),
            vol.Optional(
                CONF_TRAIN_DAYS,
                default=defaults.get(CONF_TRAIN_DAYS, DEFAULT_TRAIN_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=7, max=180, step=1, mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="days",
                )
            ),
        }
    )


class NimbusLoadSubentryFlowHandler(ConfigSubentryFlow):
    """Add (or reconfigure) one load under the Nimbus hub."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Entry point for both a fresh "+ Add" and an edit of an existing
        load -- confirmed live 2026-08-14 that Home Assistant routes a
        reconfigure through this same method (async_step_reconfigure is not
        actually a separate invocation path for subentry flows the way it
        is for top-level config entries). The real signal for which case
        this is is `self.source`, not which method got called -- assuming
        "user" always meant "brand new" raised
        `ValueError: Source is reconfigure, expected user` from
        async_create_entry the moment someone tried to edit an existing
        load rather than add one.
        """
        subentry = self._get_reconfigure_subentry() if self.source == SOURCE_RECONFIGURE else None
        return await self._async_step(user_input, subentry=subentry)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Kept as an explicit alias in case a future HA version does route
        reconfigure here separately -- same self.source-driven logic either
        way, so it's correct regardless of which method actually fires.
        """
        return await self.async_step_user(user_input)

    async def _async_step(
        self, user_input: dict[str, Any] | None, subentry: Any
    ) -> SubentryFlowResult:
        current_data = dict(subentry.data) if subentry is not None else {}

        if user_input is not None:
            title = self._derive_title(user_input[CONF_LOAD_SENSOR])
            if subentry is not None:
                return self.async_update_and_abort(
                    self._get_entry(), subentry, title=title, data=user_input
                )
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema(current_data))

    def _derive_title(self, load_sensor: str) -> str:
        """Use the source sensor's own friendly name as the device title
        ("Logger Load Power") rather than a raw entity_id wrapped in text
        ("Load (sensor.logger_load_power)") -- confirmed live 2026-08-14
        that the raw-entity-id version combines with the entity's own name
        into an unusable auto-generated entity_id downstream. Falls back to
        the entity_id itself if the sensor has no friendly_name for some
        reason (still functional, just less pretty).
        """
        state = self.hass.states.get(load_sensor)
        if state is not None:
            friendly = state.attributes.get("friendly_name")
            if friendly:
                return friendly
        return load_sensor
