"""Hub-level options for Nimbus -- settings shared across every load.

Set once via the hub's own "Configure" (not "+ Add", which is for loads),
applies to all of them: the same house has one outdoor temperature sensor
and one weather forecast, and there's rarely a reason to retrain 18 loads
on 18 different schedules. Only `load_sensor` genuinely differs per load,
so that's the only field left on the per-load subentry form
(flows/load_subentry.py) -- everything here used to be re-entered on every
single one of 18 loads, which was real, unnecessary friction.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, OptionsFlow, OptionsFlowWithConfigEntry
from homeassistant.helpers import selector
import voluptuous as vol

from ..const import (
    CONF_FORECAST_HORIZON_HOURS,
    CONF_HUMIDITY_SENSOR,
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
            vol.Optional(
                CONF_TEMPERATURE_SENSOR, default=defaults.get(CONF_TEMPERATURE_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_TEMPERATURE_FORECAST_SENSOR,
                default=defaults.get(CONF_TEMPERATURE_FORECAST_SENSOR),
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Optional -- humidity is a real, validated contributor to
            # forecast accuracy (2026-08-14 backtest), but not every
            # household has a humidity sensor wired up; ml/model.py already
            # defaults to a neutral 50% when this isn't configured, so
            # leaving it unset degrades gracefully rather than breaking.
            vol.Optional(
                CONF_HUMIDITY_SENSOR, default=defaults.get(CONF_HUMIDITY_SENSOR)
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


class NimbusHubOptionsFlow(OptionsFlowWithConfigEntry):
    """Edit the settings shared by every load, reached via the hub's own
    "Configure" button (not the per-load "+ Add"/edit)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init", data_schema=_schema(dict(self.config_entry.options))
        )
