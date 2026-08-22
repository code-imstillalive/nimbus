"""Hub-level options for Nimbus -- settings shared across every load.

Set once via the hub's own "Configure" (not "+ Add", which is for loads),
applies to all of them: the same house has one outdoor temperature sensor
and one weather forecast, and there's rarely a reason to retrain 18 loads
on 18 different schedules. Only `load_sensor` genuinely differs per load,
so that's the only field left on the per-load subentry form
(flows/load_subentry.py) -- everything here used to be re-entered on every
single one of 18 loads, which was real, unnecessary friction.

2026-08-20: "Configure" now opens a MENU (Forecaster settings vs Solver
settings) rather than a single form -- the Solver's own real config
surface is substantial enough on its own that cramming it into the same
single screen as the Forecaster's shared sensors would stop being
"logical, simple, clean" per the household's own explicit ask.

2026-08-20, same day, second real ask: "now we just need the dashboard to
allow changing of all of these inputs... grid limits, efficiencies...
cost charges... salvage... etc" -- the 14 plain-numeric Solver fields
(battery capacity/SoH/min-max SoC, max charge/discharge, efficiency, grid
import/export limits, charge/discharge cost, salvage value, P2P bonus
price/volume) moved OUT of this wizard entirely and into their own live,
dashboard-editable number.nimbus_solver_* entities (see number.py's own
module docstring for the full reasoning). What's left here is genuinely
just the 5 entity-POINTER fields -- "which sensor is your SoC/price/
forecast" -- the kind of one-time "what's this called on my system"
choice a wizard is actually right for, not something anyone would slide
on a dashboard. This shrank the wizard from 6 steps to 3 (Battery -> Grid
-> Sources); Power/Policy/P2P no longer exist as separate steps since
every field they used to hold now lives on a dashboard instead.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import OptionsFlowWithConfigEntry
from homeassistant.helpers import selector
import voluptuous as vol

from ..const import (
    CONF_BATTERY_SENSOR,
    CONF_CURTAILMENT_SENSOR,
    CONF_FORECAST_HORIZON_HOURS,
    CONF_GRID_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_RETRAIN_HOUR_LOCAL,
    CONF_SOLAR_SENSOR,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_TRAIN_DAYS,
    DEFAULT_FORECAST_HORIZON_HOURS,
    DEFAULT_RETRAIN_HOUR_LOCAL,
    DEFAULT_TRAIN_DAYS,
)


def _forecaster_schema(defaults: dict[str, Any]) -> vol.Schema:
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
            # Optional -- HAEO's own solar-curtailment status entity
            # (switch.solar_curtailment on the real system this was built
            # against). Domain is deliberately "switch", not "sensor" --
            # this is a genuinely different entity type than every other
            # field on this form.
            vol.Optional(
                CONF_CURTAILMENT_SENSOR, default=defaults.get(CONF_CURTAILMENT_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="switch")),
            # Optional -- REAL MEASURED power sensors only (this
            # household's own Modbus/inverter readings), never an
            # optimizer's own plan/forecast entity. Point these at
            # whatever your own system calls its battery/grid/solar
            # power sensors -- there's no assumed naming here, unlike
            # the entities this was originally (wrongly) built against.
            vol.Optional(
                CONF_BATTERY_SENSOR, default=defaults.get(CONF_BATTERY_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_GRID_SENSOR, default=defaults.get(CONF_GRID_SENSOR)
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_SOLAR_SENSOR, default=defaults.get(CONF_SOLAR_SENSOR)
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


def _entity(domain: str = "sensor") -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _solver_battery_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SOLVER_BATTERY_SOC_SENSOR, default=defaults.get(CONF_SOLVER_BATTERY_SOC_SENSOR),
            ): _entity(),
        }
    )


def _solver_grid_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SOLVER_IMPORT_PRICE_SENSOR, default=defaults.get(CONF_SOLVER_IMPORT_PRICE_SENSOR),
            ): _entity(),
            vol.Required(
                CONF_SOLVER_EXPORT_PRICE_SENSOR, default=defaults.get(CONF_SOLVER_EXPORT_PRICE_SENSOR),
            ): _entity(),
        }
    )


def _solver_sources_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SOLVER_SOLAR_FORECAST_SENSOR, default=defaults.get(CONF_SOLVER_SOLAR_FORECAST_SENSOR),
            ): _entity(),
            # Optional second solar source (2026-08-22) -- see
            # CONF_SOLVER_SOLAR_FORECAST_SENSOR_2's own comment in
            # const.py for why. Blank (the default) is a complete no-op,
            # byte-identical to every install before this field existed.
            vol.Optional(
                CONF_SOLVER_SOLAR_FORECAST_SENSOR_2, default=defaults.get(CONF_SOLVER_SOLAR_FORECAST_SENSOR_2),
            ): _entity(),
            vol.Required(
                CONF_SOLVER_LOAD_FORECAST_SENSOR, default=defaults.get(CONF_SOLVER_LOAD_FORECAST_SENSOR),
            ): _entity(),
        }
    )


class NimbusHubOptionsFlow(OptionsFlowWithConfigEntry):
    """Edit the settings shared by every load, reached via the hub's own
    "Configure" button (not the per-load "+ Add"/edit).

    2026-08-20: now a menu -- Forecaster settings (the original single
    form, unchanged) vs Solver settings (a 3-step wizard: Battery -> Grid
    -> Sources -- SoC/price/forecast entity pointers only, see this
    module's own top-of-file docstring for why the 14 plain-numeric
    fields that used to live here moved to number.py instead). Each
    Solver step accumulates into self._solver_data and chains to the
    next; only the final step actually saves, same MERGE-not-replace
    discipline as the original Forecaster form (see the comment on
    async_step_forecaster below for why that matters).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._solver_data: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        return self.async_show_menu(
            step_id="init",
            menu_options=["forecaster", "solver_battery"],
        )

    async def async_step_forecaster(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            # MERGE onto the existing options, never a blind replace
            # (2026-08-17, real, flagged risk: "the Nimbus hub's shared
            # Battery/Solar/Grid sensor config can be silently cleared by
            # a stale options-form resubmission"). async_create_entry's
            # own `data=` argument REPLACES config_entry.options wholesale
            # -- if a submitted payload is ever missing a key this schema
            # currently defines (a stale cached frontend form from before
            # a field was added, a future field added to a schema an
            # in-flight flow instance doesn't yet know about, or any other
            # future path that stores an options key this flow doesn't
            # itself render), a plain `data=user_input` would silently
            # DROP that key from the live config rather than leave it
            # untouched. Spreading the existing options first means only
            # fields THIS submission actually included ever change.
            return self.async_create_entry(title="", data={**self.config_entry.options, **user_input})

        return self.async_show_form(
            step_id="forecaster", data_schema=_forecaster_schema(dict(self.config_entry.options))
        )

    async def async_step_solver_battery(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            self._solver_data.update(user_input)
            return await self.async_step_solver_grid()
        return self.async_show_form(
            step_id="solver_battery", data_schema=_solver_battery_schema(dict(self.config_entry.options))
        )

    async def async_step_solver_grid(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            self._solver_data.update(user_input)
            return await self.async_step_solver_sources()
        return self.async_show_form(
            step_id="solver_grid", data_schema=_solver_grid_schema(dict(self.config_entry.options))
        )

    async def async_step_solver_sources(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            self._solver_data.update(user_input)
            # Same merge-not-replace discipline as async_step_forecaster --
            # only what this whole 3-step wizard actually collected changes;
            # everything else in config_entry.options (Forecaster settings,
            # and every number.nimbus_solver_* entity's own separately-
            # tracked value, included) is left untouched.
            return self.async_create_entry(title="", data={**self.config_entry.options, **self._solver_data})
        return self.async_show_form(
            step_id="solver_sources", data_schema=_solver_sources_schema(dict(self.config_entry.options))
        )
