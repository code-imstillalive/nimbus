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
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR,
    CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR,
    CONF_SWITCHBOARD_BATTERY_POWER_SENSOR,
    CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_GRID_METER_SENSOR,
    CONF_SWITCHBOARD_HOUSE_LOAD_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_TRAIN_DAYS,
    DEFAULT_FORECAST_HORIZON_HOURS,
    DEFAULT_RETRAIN_HOUR_LOCAL,
    DEFAULT_TRAIN_DAYS,
)


def _forecaster_schema(defaults: dict[str, Any]) -> vol.Schema:
    # Real fix (2026-08-22, direct household report: "its not letting me
    # delete anything it remains there even after deleting"). Every
    # Optional ENTITY field below was built with `default=defaults.get(
    # key)` -- the classic, well-documented HA config-flow trap: a
    # voluptuous `default=` isn't just a display hint, it's what
    # voluptuous itself SUPPLIES during validation whenever the
    # submitted payload omits that key. When a user clears an entity
    # picker and submits, the frontend omits the key -- and voluptuous
    # silently refills it right back in with the OLD value from
    # `default=`, so the field can never actually go blank. The correct
    # pattern for a genuinely clearable field is `description={
    # "suggested_value": ...}` -- a pure frontend pre-fill hint that
    # does NOT get injected back into validation, so a real blank
    # submission stays genuinely blank. (`vol.Required` fields below
    # are unaffected -- HA won't let a required field submit truly
    # empty anyway, so "sticky" is the correct behaviour there.)
    return vol.Schema(
        {
            vol.Optional(
                CONF_TEMPERATURE_SENSOR, description={"suggested_value": defaults.get(CONF_TEMPERATURE_SENSOR)}
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_TEMPERATURE_FORECAST_SENSOR,
                description={"suggested_value": defaults.get(CONF_TEMPERATURE_FORECAST_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Optional -- humidity is a real, validated contributor to
            # forecast accuracy (2026-08-14 backtest), but not every
            # household has a humidity sensor wired up; ml/model.py already
            # defaults to a neutral 50% when this isn't configured, so
            # leaving it unset degrades gracefully rather than breaking.
            vol.Optional(
                CONF_HUMIDITY_SENSOR, description={"suggested_value": defaults.get(CONF_HUMIDITY_SENSOR)}
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Optional -- HAEO's own solar-curtailment status entity
            # (switch.solar_curtailment on the real system this was built
            # against). Domain is deliberately "switch", not "sensor" --
            # this is a genuinely different entity type than every other
            # field on this form.
            vol.Optional(
                CONF_CURTAILMENT_SENSOR, description={"suggested_value": defaults.get(CONF_CURTAILMENT_SENSOR)}
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="switch")),
            # Optional -- REAL MEASURED power sensors only (this
            # household's own Modbus/inverter readings), never an
            # optimizer's own plan/forecast entity. Point these at
            # whatever your own system calls its battery/grid/solar
            # power sensors -- there's no assumed naming here, unlike
            # the entities this was originally (wrongly) built against.
            vol.Optional(
                CONF_BATTERY_SENSOR, description={"suggested_value": defaults.get(CONF_BATTERY_SENSOR)}
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_GRID_SENSOR, description={"suggested_value": defaults.get(CONF_GRID_SENSOR)}
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_SOLAR_SENSOR, description={"suggested_value": defaults.get(CONF_SOLAR_SENSOR)}
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


def _entity_multi(domain: str = "sensor") -> selector.EntitySelector:
    """Real, native HA multi-entity picker -- for the granular, optional
    per-circuit load-summation list (2026-08-23, issue #56's own fix).
    Genuinely empty by default; picking zero entities is a complete no-op,
    not a degraded mode -- see CONF_SOLVER_LOAD_FORECAST_ENTITIES's own
    comment in const.py."""
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain, multiple=True))


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
            # const.py for why. Blank is a complete no-op, byte-identical
            # to every install before this field existed --
            # description={"suggested_value": ...}, NOT default=, so it
            # can genuinely be cleared once set (see this schema
            # function's own sibling _forecaster_schema's top-of-function
            # comment for the full "why default= traps a field" story).
            vol.Optional(
                CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
                description={"suggested_value": defaults.get(CONF_SOLVER_SOLAR_FORECAST_SENSOR_2)},
            ): _entity(),
            # Optional THIRD solar source (2026-08-22) -- see CONF_SOLVER_
            # SOLAR_FORECAST_SENSOR_3's own comment in const.py for why.
            # Same complete-no-op-when-blank guarantee.
            vol.Optional(
                CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
                description={"suggested_value": defaults.get(CONF_SOLVER_SOLAR_FORECAST_SENSOR_3)},
            ): _entity(),
            vol.Required(
                CONF_SOLVER_LOAD_FORECAST_SENSOR, default=defaults.get(CONF_SOLVER_LOAD_FORECAST_SENSOR),
            ): _entity(),
            # Both new, optional, real-bug-fix fields (2026-08-23, issue
            # #56) -- blank/empty is a complete no-op on every install
            # that doesn't set them, same suggested_value (not default=)
            # pattern as the optional solar sources above so they can
            # genuinely be cleared once set.
            vol.Optional(
                CONF_SOLVER_LOAD_FORECAST_ENTITIES,
                description={"suggested_value": defaults.get(CONF_SOLVER_LOAD_FORECAST_ENTITIES)},
            ): _entity_multi(),
            vol.Optional(
                CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
                description={"suggested_value": defaults.get(CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR)},
            ): _entity(),
        }
    )


def _switchboard_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Every field genuinely Optional -- the topology dashboard card's
    own top-of-diagram sensors (2026-08-23, direct household ask: "i do
    want my stuff ot work via wizard, and not ot be hard wired... topology
    card must also follow suit"). description={"suggested_value": ...},
    not default=, on every field -- same already-fixed reasoning as
    async_step_forecaster's own top-of-function comment (a genuinely
    cleared field must actually stay cleared, not silently reappear next
    time this form opens)."""
    schema_dict: dict[Any, Any] = {}
    for key in (
        CONF_SWITCHBOARD_GRID_METER_SENSOR,
        CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR,
        CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR,
        CONF_SWITCHBOARD_BATTERY_POWER_SENSOR,
        CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR,
        CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR,
        CONF_SWITCHBOARD_HOUSE_LOAD_ENERGY_DAILY_SENSOR,
        CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR,
        CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR,
        CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR,
    ):
        schema_dict[vol.Optional(key, description={"suggested_value": defaults.get(key)})] = _entity()
    return vol.Schema(schema_dict)


_SWITCHBOARD_SCHEMA_KEYS = (
    CONF_SWITCHBOARD_GRID_METER_SENSOR,
    CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_BATTERY_POWER_SENSOR,
    CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_HOUSE_LOAD_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR,
    CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR,
)


# Explicit key lists for the "always take from this submission, never
# silently fall back to whatever's already stored" merge fix (2026-08-22
# -- see async_step_forecaster's own comment for the full story). Kept
# as plain constants, one per schema, so they can't silently drift out
# of sync with whichever fields each schema function actually defines --
# any future field added to a schema needs adding here too, deliberately
# (a missed key here just means that ONE new field keeps the old,
# safer "never touched by this form" merge behaviour, not a crash).
_FORECASTER_SCHEMA_KEYS = (
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_CURTAILMENT_SENSOR,
    CONF_BATTERY_SENSOR,
    CONF_GRID_SENSOR,
    CONF_SOLAR_SENSOR,
    CONF_FORECAST_HORIZON_HOURS,
    CONF_RETRAIN_HOUR_LOCAL,
    CONF_TRAIN_DAYS,
)
_SOLVER_WIZARD_SCHEMA_KEYS = (
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
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
            menu_options=["forecaster", "solver_battery", "switchboard"],
        )

    async def async_step_forecaster(self, user_input: dict[str, Any] | None = None) -> Any:
        if user_input is not None:
            # MERGE onto the existing options (2026-08-17, real, flagged
            # risk: "the Nimbus hub's shared Battery/Solar/Grid sensor
            # config can be silently cleared by a stale options-form
            # resubmission") -- but a plain `{**old, **user_input}` spread
            # has its OWN real bug (2026-08-22, direct household report:
            # "its not letting me delete anything... i have found open
            # meteo came back... why?"): when an Optional field is
            # genuinely cleared in the UI, voluptuous can validate that
            # as the key being ABSENT from user_input entirely (not
            # present-with-value-None) -- and a plain spread treats
            # "absent" as "untouched", so the OLD value silently survives
            # forever, and the very next time this form opens, its own
            # `description={"suggested_value": ...}` hint reads that same
            # never-actually-cleared old value straight back out of
            # config_entry.options, making a cleared field visually
            # "come back". Fix: for every key THIS schema actually
            # defines, always take `user_input.get(key)` explicitly
            # (correctly resolves to None whether the key was submitted
            # as None or omitted entirely) -- submitting this form means
            # every field it displays becomes exactly what's shown,
            # including genuinely blank. Only keys OUTSIDE this schema
            # (dashboard number.nimbus_solver_* values, the Solver
            # wizard's own separate fields) are preserved untouched from
            # the existing options -- the real risk the original 2026-08-
            # 17 fix was protecting against, still fully intact.
            merged = dict(self.config_entry.options)
            for key in _FORECASTER_SCHEMA_KEYS:
                merged[key] = user_input.get(key)
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="forecaster", data_schema=_forecaster_schema(dict(self.config_entry.options))
        )

    async def async_step_switchboard(self, user_input: dict[str, Any] | None = None) -> Any:
        """Single-step form, same one-shot save shape as
        async_step_forecaster above (not a multi-step chain like the
        Solver wizard -- 10 genuinely independent fields, no reason to
        split across screens). Same explicit-key merge discipline as
        every other options-flow save in this file."""
        if user_input is not None:
            merged = dict(self.config_entry.options)
            for key in _SWITCHBOARD_SCHEMA_KEYS:
                merged[key] = user_input.get(key)
            return self.async_create_entry(title="", data=merged)

        return self.async_show_form(
            step_id="switchboard", data_schema=_switchboard_schema(dict(self.config_entry.options))
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
            # Same explicit-key-list fix as async_step_forecaster (see its
            # own comment for the full story) -- self._solver_data now
            # holds whatever was actually submitted across all 3 Solver
            # wizard steps; every key the wizard's own 3 schemas define
            # gets taken explicitly from it (None if genuinely cleared or
            # never touched this run), so a cleared optional source
            # actually stays cleared. Everything else in config_entry.
            # options (Forecaster settings, every number.nimbus_solver_*
            # dashboard value) is preserved untouched -- the real risk the
            # original merge-not-replace fix was protecting against.
            merged = dict(self.config_entry.options)
            for key in _SOLVER_WIZARD_SCHEMA_KEYS:
                merged[key] = self._solver_data.get(key)
            # Dismiss config_flow.py's first-run "not configured yet" nudge,
            # if it's still showing -- this is the step that actually seeds
            # number.py's placeholder entities with real values (via the
            # hub reload this options-flow completion triggers), so it's
            # the right moment to clear it. Wrapped for the same reason as
            # its creation -- never let this block a real save.
            try:
                await self.hass.services.async_call(
                    "persistent_notification", "dismiss", {"notification_id": "nimbus_setup_incomplete"},
                )
            except Exception:  # noqa: BLE001
                pass
            return self.async_create_entry(title="", data=merged)
        return self.async_show_form(
            step_id="solver_sources", data_schema=_solver_sources_schema(dict(self.config_entry.options))
        )
