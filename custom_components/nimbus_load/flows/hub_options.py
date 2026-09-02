"""Hub-level options for Nimbus -- settings shared across every load.

⚠️ Programmatic callers (scripts, MCP tools, anything driving this flow
over the API rather than a human filling in the UI form): every field
below uses `vol.Optional(key, description={"suggested_value": ...})`,
never `default=`. That means a key genuinely ABSENT from your submitted
`user_input` is ALSO absent from the validated result -- and this step
has no way to tell "the user cleared this field" apart from "this
caller only meant to patch a different field and left this one alone."
A real UI submission always carries every field (the frontend pre-fills
each one from `suggested_value`), so this is invisible there. A
programmatic PARTIAL patch is not safe here: submitting only the 2-3
keys you want to change will silently null every other field on that
step (issue #113/#114, confirmed live -- reproduced via the options-flow
API leaving `temperature_sensor`/`battery_sensor`/`grid_sensor`/
`solar_sensor` wiped after a submission that only meant to touch
`curtailment_sensor`). This is deliberate, documented behavior, not a
bug to work around here -- see #121's own resolution. The fix belongs
in the CALLER: read `entry.options` first, merge your partial dict on
top of it yourself (`{**entry.options, **your_partial_dict}`), and call
`hass.config_entries.async_update_entry(entry, options=merged)`
directly, bypassing this flow entirely. That path can't collide with a
real UI submission, since it's a fully separate code path with an
unambiguous merge contract. Never call this flow's own step handlers
with anything less than the complete set of fields you want preserved.

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

import voluptuous as vol
from homeassistant.config_entries import OptionsFlowWithConfigEntry
from homeassistant.helpers import selector

from ..const import (
    ATTR_SIGNAL_ROLE,
    ATTR_SUBENTRY_TYPE,
    CONF_BATTERY_SENSOR,
    CONF_CURTAILMENT_SENSOR,
    CONF_FORECAST_HORIZON_HOURS,
    CONF_GRID_SENSOR,
    CONF_HUMIDITY_SENSOR,
    CONF_HYBRID_RECENT_DAYS,
    CONF_RETRAIN_HOUR_LOCAL,
    CONF_SOLAR_SENSOR,
    CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE,
    CONF_SOLVER_BATTERY_POWER_SENSOR,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY,
    CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR,
    CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR,
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
    CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR,
    CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
    CONF_SOLVER_SOLAR_POWER_SENSOR,
    CONF_SOLVER_WEATHER_FORECAST_SENSOR,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR,
    CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR,
    CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_HOUSE_LOAD_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR,
    CONF_TEMPERATURE_FORECAST_SENSOR,
    CONF_TEMPERATURE_SENSOR,
    CONF_TRAIN_DAYS,
    CONF_TRAINING_SOURCE,
    DEFAULT_FORECAST_HORIZON_HOURS,
    DEFAULT_HYBRID_RECENT_DAYS,
    DEFAULT_RETRAIN_HOUR_LOCAL,
    DEFAULT_TRAIN_DAYS,
    DEFAULT_TRAINING_SOURCE,
    SIGNAL_ROLE_BATTERY,
    SIGNAL_ROLE_GRID,
    SIGNAL_ROLE_SOLAR,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_SIGNAL,
    TRAINING_SOURCE_CHOICES,
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
                CONF_TEMPERATURE_SENSOR,
                description={"suggested_value": defaults.get(CONF_TEMPERATURE_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # domain=["sensor", "weather"] (2026-08-24, nimbus #123, Mark
            # Purcell's own real repro): "sensor" alone rejected the
            # natural choice for most installs -- modern HA weather
            # entities no longer carry a "forecast" state attribute at
            # all (removed 2024.x+), so pointing straight at one only
            # works because coordinator.py's own
            # _async_fetch_temperature_forecast() now detects a
            # weather.* entity_id and calls weather.get_forecasts
            # (hourly) internally instead of reading a nonexistent
            # attribute. "sensor" stays accepted too, for anyone who's
            # already built their own forecast-shaped template sensor
            # (or whose weather integration genuinely still uses the
            # old attribute pattern) -- fully backward compatible,
            # zero change for an already-working sensor.* config.
            vol.Optional(
                CONF_TEMPERATURE_FORECAST_SENSOR,
                description={
                    "suggested_value": defaults.get(CONF_TEMPERATURE_FORECAST_SENSOR)
                },
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "weather"])
            ),
            # Optional -- humidity is a real, validated contributor to
            # forecast accuracy (2026-08-14 backtest), but not every
            # household has a humidity sensor wired up; ml/model.py already
            # defaults to a neutral 50% when this isn't configured, so
            # leaving it unset degrades gracefully rather than breaking.
            vol.Optional(
                CONF_HUMIDITY_SENSOR,
                description={"suggested_value": defaults.get(CONF_HUMIDITY_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            # Optional -- HAEO's own solar-curtailment status entity
            # (switch.solar_curtailment on the real system this was built
            # against). Domain is deliberately "switch", not "sensor" --
            # this is a genuinely different entity type than every other
            # field on this form.
            vol.Optional(
                CONF_CURTAILMENT_SENSOR,
                description={"suggested_value": defaults.get(CONF_CURTAILMENT_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="switch")),
            # Optional -- REAL MEASURED power sensors only (this
            # household's own Modbus/inverter readings), never an
            # optimizer's own plan/forecast entity. Point these at
            # whatever your own system calls its battery/grid/solar
            # power sensors -- there's no assumed naming here, unlike
            # the entities this was originally (wrongly) built against.
            vol.Optional(
                CONF_BATTERY_SENSOR,
                description={"suggested_value": defaults.get(CONF_BATTERY_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_GRID_SENSOR,
                description={"suggested_value": defaults.get(CONF_GRID_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_SOLAR_SENSOR,
                description={"suggested_value": defaults.get(CONF_SOLAR_SENSOR)},
            ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_FORECAST_HORIZON_HOURS,
                default=defaults.get(
                    CONF_FORECAST_HORIZON_HOURS, DEFAULT_FORECAST_HORIZON_HOURS
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=168,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="hours",
                )
            ),
            vol.Optional(
                CONF_RETRAIN_HOUR_LOCAL,
                default=defaults.get(
                    CONF_RETRAIN_HOUR_LOCAL, DEFAULT_RETRAIN_HOUR_LOCAL
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=23,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="hour of day (0-23)",
                )
            ),
            vol.Optional(
                CONF_TRAIN_DAYS,
                default=defaults.get(CONF_TRAIN_DAYS, DEFAULT_TRAIN_DAYS),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=7,
                    max=180,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="days",
                )
            ),
            vol.Optional(
                CONF_TRAINING_SOURCE,
                default=defaults.get(CONF_TRAINING_SOURCE, DEFAULT_TRAINING_SOURCE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(TRAINING_SOURCE_CHOICES),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key=CONF_TRAINING_SOURCE,
                )
            ),
            # Only meaningful when training_source=hybrid, but exposed unconditionally
            # -- a dependent-field "only show when X" would need a two-step flow and
            # isn't worth the complexity for one small numeric knob that's a no-op
            # when hybrid isn't selected.
            vol.Optional(
                CONF_HYBRID_RECENT_DAYS,
                default=defaults.get(
                    CONF_HYBRID_RECENT_DAYS, DEFAULT_HYBRID_RECENT_DAYS
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1,
                    max=30,
                    step=1,
                    mode=selector.NumberSelectorMode.BOX,
                    unit_of_measurement="days",
                )
            ),
        }
    )


def _entity(
    domain: str = "sensor", include_entities: list[str] | None = None
) -> selector.EntitySelector:
    """`include_entities`, when given a non-empty list, restricts the
    picker's own dropdown to exactly those candidates (2026-08-24, real
    HA behaviour, directly verified: a non-empty list genuinely narrows
    what's offered; an empty list falls through to "no restriction" --
    but every caller here still passes `None`, never `[]`, in that case,
    so this file's own on-disk behaviour never has to depend on that
    empty-list nuance holding forever). Deliberately NOT enforced at
    validation time -- an already-saved value outside the current
    candidate list is left completely alone (the picker just won't
    offer it again as a NEW choice), same "restrict the suggestion,
    never silently override a real saved value" discipline as every
    other safeguard in this file."""
    config: dict[str, Any] = {"domain": domain}
    if include_entities:
        config["include_entities"] = include_entities
    return selector.EntitySelector(selector.EntitySelectorConfig(**config))


def _entity_multi(
    domain: str = "sensor", include_entities: list[str] | None = None
) -> selector.EntitySelector:
    """Real, native HA multi-entity picker -- for the granular, optional
    per-circuit load-summation list (2026-08-23, issue #56's own fix).
    Genuinely empty by default; picking zero entities is a complete no-op,
    not a degraded mode -- see CONF_SOLVER_LOAD_FORECAST_ENTITIES's own
    comment in const.py. `include_entities` -- see _entity()'s own
    docstring above, same mechanism, same safety guarantee."""
    config: dict[str, Any] = {"domain": domain, "multiple": True}
    if include_entities:
        config["include_entities"] = include_entities
    return selector.EntitySelector(selector.EntitySelectorConfig(**config))


def _solver_battery_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SOLVER_BATTERY_SOC_SENSOR,
                default=defaults.get(CONF_SOLVER_BATTERY_SOC_SENSOR),
            ): _entity(),
            # Optional, unset by default (2026-08-24, nimbus #125) -- see
            # const.py's own CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY comment
            # for the full "hardcoded entity silently overrode a portable
            # install's configured max discharge" story. `description={
            # "suggested_value": ...}`, NOT `default=`, matching this
            # form's own Optional-field convention (see
            # async_step_forecaster's own 2026-08-22/2026-08-24 comments)
            # -- a genuinely optional pointer field must stay clearable.
            vol.Optional(
                CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY,
                description={
                    "suggested_value": defaults.get(
                        CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY
                    )
                },
            ): _entity(domain="number"),
        }
    )


def _solver_grid_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_SOLVER_IMPORT_PRICE_SENSOR,
                default=defaults.get(CONF_SOLVER_IMPORT_PRICE_SENSOR),
            ): _entity(),
            # Optional second/third import price sources (2026-08-25) --
            # see CONF_SOLVER_IMPORT_PRICE_SENSOR_2/_3's own comment in
            # const.py for why (e.g. a real AEMO wholesale forecast
            # blended with a retailer's own forecast, such as Amber).
            # description={"suggested_value": ...}, NOT default=, so a
            # configured source can genuinely be cleared -- same
            # convention as the optional solar sources below.
            vol.Optional(
                CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_IMPORT_PRICE_SENSOR_2)
                },
            ): _entity(),
            vol.Optional(
                CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_IMPORT_PRICE_SENSOR_3)
                },
            ): _entity(),
            vol.Required(
                CONF_SOLVER_EXPORT_PRICE_SENSOR,
                default=defaults.get(CONF_SOLVER_EXPORT_PRICE_SENSOR),
            ): _entity(),
            # Optional second/third export price sources -- same
            # blend-don't-pick-one reasoning, mirrored for export.
            vol.Optional(
                CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_EXPORT_PRICE_SENSOR_2)
                },
            ): _entity(),
            vol.Optional(
                CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_EXPORT_PRICE_SENSOR_3)
                },
            ): _entity(),
            # 2026-08-29, issue #232 follow-up: native price-triggered
            # solving (issue #256) now lives on a dashboard-editable
            # switch.nimbus_solve_on_price_change + paired
            # number.nimbus_solve_on_price_change_debounce_s -- see
            # switch.py's own module docstring for the full "why not
            # the wizard" story. Same reasoning as every other tuning
            # knob that moved out into number.py in the 2026-08-20
            # refactor: a runtime toggle a household will want to flip
            # off during a debug window shouldn't force a config-flow
            # round-trip through Settings -> Devices & services.
        }
    )


def _solver_sources_schema(
    defaults: dict[str, Any],
    single_load_forecast_candidates: list[str] | None = None,
    summable_load_forecast_candidates: list[str] | None = None,
) -> vol.Schema:
    """The two `*_candidates` params (2026-08-24, "Group A" of the wizard-
    simplification scoping) narrow the two load-forecast fields' own
    pickers down to Nimbus's real, live forecast output -- see
    _discover_nimbus_load_forecast_candidates()'s own docstring, this
    module's one real source of what belongs in each list. Both default
    to None (== "no restriction", the exact original behaviour) so any
    OTHER caller of this function (there are none live today, but this
    keeps the function itself honestly self-contained rather than
    silently depending on its one real caller always doing discovery
    first) still gets a fully working, if unrestricted, form."""
    return vol.Schema(
        {
            vol.Required(
                CONF_SOLVER_SOLAR_FORECAST_SENSOR,
                default=defaults.get(CONF_SOLVER_SOLAR_FORECAST_SENSOR),
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
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_SOLAR_FORECAST_SENSOR_2)
                },
            ): _entity(),
            # Optional THIRD solar source (2026-08-22) -- see CONF_SOLVER_
            # SOLAR_FORECAST_SENSOR_3's own comment in const.py for why.
            # Same complete-no-op-when-blank guarantee.
            vol.Optional(
                CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_SOLAR_FORECAST_SENSOR_3)
                },
            ): _entity(),
            # Restricted (2026-08-24, Group A) to Nimbus's own real
            # per-signal/per-load forecast entities -- deliberately
            # EXCLUDES sensor.nimbus_household_load_total_forecast (the
            # Solver's own OUTPUT), which is exactly the entity a fresh
            # install with no load subentries configured would otherwise
            # be tempted to point this AT, producing the real, confirmed
            # circular-reference bug issue #118 fixed defensively (see
            # this file's own #118 dated comment history, and
            # _discover_nimbus_load_forecast_candidates()'s docstring
            # below for the precise "why not the aggregate" reasoning).
            vol.Required(
                CONF_SOLVER_LOAD_FORECAST_SENSOR,
                default=defaults.get(CONF_SOLVER_LOAD_FORECAST_SENSOR),
            ): _entity(include_entities=single_load_forecast_candidates),
            # Both new, optional, real-bug-fix fields (2026-08-23, issue
            # #56) -- blank/empty is a complete no-op on every install
            # that doesn't set them, same suggested_value (not default=)
            # pattern as the optional solar sources above so they can
            # genuinely be cleared once set. Restricted (2026-08-24,
            # Group A) to genuine per-circuit Load-subentry forecasts --
            # this field's own real job is summing individual circuits,
            # so that's exactly what its own picker now offers.
            vol.Optional(
                CONF_SOLVER_LOAD_FORECAST_ENTITIES,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_LOAD_FORECAST_ENTITIES)
                },
            ): _entity_multi(include_entities=summable_load_forecast_candidates),
            vol.Optional(
                CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
                description={
                    "suggested_value": defaults.get(
                        CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR
                    )
                },
            ): _entity(),
            # Built-in EPR/regret/tracking quality scoring (2026-08-25) --
            # see these two fields' own comments in const.py for the full
            # "real measured, not forecast" reasoning. Blank on either is
            # a clean no-op (compute_daily_quality_report() simply never
            # runs), same convention as every other optional field here.
            vol.Optional(
                CONF_SOLVER_SOLAR_POWER_SENSOR,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_SOLAR_POWER_SENSOR)
                },
            ): _entity(),
            vol.Optional(
                CONF_SOLVER_BATTERY_POWER_SENSOR,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_BATTERY_POWER_SENSOR)
                },
            ): _entity(),
            # Real bug fix (Mark Purcell, issue #299, 2026-08-31) -- see
            # this field's own comment in const.py for the full "SigEnergy
            # reports the opposite battery-power sign" story. Off (the
            # default) is byte-for-byte identical to every install before
            # this field existed.
            vol.Optional(
                CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE,
                description={
                    "suggested_value": defaults.get(
                        CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE
                    )
                },
            ): selector.BooleanSelector(),
            # Optional retailer-specific settlement hook -- see this
            # field's own comment in const.py for the full "why this one
            # genuinely can't be made retailer-agnostic by reading
            # recorder history alone" reasoning.
            vol.Optional(
                CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR,
                description={
                    "suggested_value": defaults.get(
                        CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR
                    )
                },
            ): _entity(),
            # Real hardcoded-foreign-entity audit (2026-09-02) -- see each
            # field's own comment in const.py for the full real-bug
            # story. All four blank by default (a clean no-op, same
            # convention as every other optional field here): the
            # richer price-forecast-array path, the AEMO-anchored far-
            # horizon extrapolation, the empirical retail-markup offset,
            # and the real live P2P-matched rate all simply don't run,
            # falling back to whatever CONF_SOLVER_IMPORT_PRICE_SENSOR/
            # EXPORT_PRICE_SENSOR's own simpler generic path already
            # provides -- never a crash, never a degraded mode.
            vol.Optional(
                CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
                description={
                    "suggested_value": defaults.get(
                        CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR
                    )
                },
            ): _entity(),
            vol.Optional(
                CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
                description={
                    "suggested_value": defaults.get(
                        CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR
                    )
                },
            ): _entity(),
            vol.Optional(
                CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR,
                description={
                    "suggested_value": defaults.get(
                        CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR
                    )
                },
            ): _entity(),
            vol.Optional(
                CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR,
                description={
                    "suggested_value": defaults.get(
                        CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR
                    )
                },
            ): _entity(),
            # Dashboard temp/humidity mirror -- see this field's own
            # comment in const.py for why it's a separate field rather
            # than reusing the Forecaster-level temperature_forecast_
            # sensor. Same dual-domain picker as that field (built
            # directly, not via _entity(), which is typed str-only) --
            # modern weather.* entities are the natural choice for most
            # installs, see CONF_TEMPERATURE_FORECAST_SENSOR's own
            # comment in _forecaster_schema above.
            vol.Optional(
                CONF_SOLVER_WEATHER_FORECAST_SENSOR,
                description={
                    "suggested_value": defaults.get(CONF_SOLVER_WEATHER_FORECAST_SENSOR)
                },
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "weather"])
            ),
        }
    )


def _discover_nimbus_load_forecast_candidates(
    hass: Any,
) -> tuple[list[str], list[str]]:
    """ "Group A" of the 2026-08-24 wizard-simplification scoping (direct
    Mark Purcell critique, relayed: entity-pointer fields are confusing
    to fill in cold, since a fresh install has no way to know which of
    its own dozens of live entities are the RIGHT kind of candidate).
    Mirrors the already-proven, already-live pattern topology-card-v4.js
    uses client-side (_discoverLoads()/_discoverPowerSignalsByRole()) --
    scan hass.states for Nimbus's own real, live forecast entities
    (every one is tagged with ATTR_SUBENTRY_TYPE at publish time, see
    sensor.py's NimbusForecastSensor), then use that same tag (plus, for
    power signals, the explicit ATTR_SIGNAL_ROLE -- never guessed from
    naming, same reasoning as CONF_SIGNAL_ROLE's own const.py comment)
    to sort them into the two genuinely different things this wizard
    step's two entity-pointer fields each actually want.

    Returns (single_load_forecast_candidates, summable_load_forecast_
    candidates) -- deliberately two separate lists, not one, because the
    two real fields these feed have two different, non-overlapping real
    answers:

    - solver_load_forecast_sensor (single-select, "point at ONE whole-
      house load forecast") wants a Power Signal subentry whose role is
      genuinely "other" (i.e. explicitly NOT battery/solar/grid -- a
      household's own "Whole House Load"/"CB Total Combined Power"-style
      signal), or, less commonly, a single Load subentry directly.
      Deliberately, precisely EXCLUDES sensor.nimbus_household_load_
      total_forecast (the Solver's own per-solve OUTPUT, a
      _NimbusSolverPushSensor, never tagged with ATTR_SUBENTRY_TYPE at
      all -- confirmed via direct source read, 2026-08-24) -- pointing
      this field at that entity is exactly the real, confirmed circular-
      reference bug issue #118 fixed defensively (this function's own
      job is to stop an installer from ever being OFFERED that footgun
      in the first place, not just catch it after the fact).
    - solver_load_forecast_entities (multi-select, "sum these individual
      circuits") wants real, individual Load-subentry forecasts only --
      exactly what this field's own summation logic (sum_load_
      forecasts()) is built to consume.

    Returns ([], []) on any failure (hass not fully ready, an
    unexpected attribute shape) -- same graceful-degradation convention
    as this module's sibling _energy_dashboard_switchboard_suggestions()
    above: a discovery failure must only ever mean "show every entity,
    unrestricted" (both _entity()/_entity_multi() treat an empty list as
    "no restriction"), never break the wizard step itself.
    """
    single: list[str] = []
    summable: list[str] = []
    try:
        for state in hass.states.async_all("sensor"):
            subentry_type = state.attributes.get(ATTR_SUBENTRY_TYPE)
            if subentry_type == SUBENTRY_TYPE_LOAD:
                summable.append(state.entity_id)
                single.append(state.entity_id)
            elif subentry_type == SUBENTRY_TYPE_SIGNAL:
                role = state.attributes.get(ATTR_SIGNAL_ROLE)
                if role not in (
                    SIGNAL_ROLE_BATTERY,
                    SIGNAL_ROLE_SOLAR,
                    SIGNAL_ROLE_GRID,
                ):
                    single.append(state.entity_id)
    except Exception:  # noqa: BLE001 -- see docstring: a discovery failure must degrade to "no restriction", never break the wizard
        return [], []
    return single, summable


def _type_safe_entity_suggestions(hass: Any) -> dict[str, str]:
    """ "Group B" of the 2026-08-24 wizard-simplification scoping -- a
    real, live device_class scan for the handful of RAW HARDWARE fields
    (temperature/humidity/battery-SoC) where Nimbus has no self-
    describing tag of its own to lean on (unlike Group A's Nimbus
    forecast entities, which always carry ATTR_SUBENTRY_TYPE, a 100%
    reliable signal Nimbus itself controls). Deliberately a SUGGESTION
    (suggested_value, exactly `_energy_dashboard_switchboard_suggestions`'
    own already-proven mechanism above), never a picker restriction --
    a hard include_entities/device_class filter on these fields would
    risk permanently hiding a household's own genuinely-correct sensor
    if it happens to be an untagged template sensor (a real, common
    real-world shape, and worse than the current no-restriction status
    quo, not better).

    Narrower scope than a naive "device_class-filter everything raw"
    approach, and deliberately so -- reasoned through explicitly rather
    than assumed:

    - CONF_TEMPERATURE_SENSOR / CONF_HUMIDITY_SENSOR (device_class
      temperature/humidity): only suggested when EXACTLY ONE live
      sensor of that device_class exists system-wide. Multiple matches
      (a household with several climate sensors) is a real, common
      case -- guessing one arbitrarily would be actively misleading, so
      it's left unsuggested instead, identical to today's behaviour.
    - CONF_SOLVER_BATTERY_SOC_SENSOR (device_class battery): same
      exactly-one rule. Real, honest limitation: device_class=battery
      is also the standard HA convention for low-battery-level
      diagnostics on completely unrelated devices (door sensors,
      remotes, ...) -- on a household with several such devices this
      will essentially always come back empty, which is fine (empty
      means "no regression from today", never "a wrong guess").
    - CONF_BATTERY_SENSOR / CONF_GRID_SENSOR / CONF_SOLAR_SENSOR
      (Forecaster's own shared power sensors) and CONF_SOLVER_IMPORT_
      PRICE_SENSOR / CONF_SOLVER_EXPORT_PRICE_SENSOR are deliberately
      EXCLUDED from this function entirely, not merely rarer -- even
      when device_class narrows candidates to exactly one, there is no
      way to tell FROM device_class alone whether one lone power sensor
      is battery vs grid vs solar, or whether one lone monetary sensor
      is the import vs export price. Suggesting the same entity for two
      different fields (or the wrong one of a pair) would be
      confidently, silently wrong -- worse than staying silent.

    Returns {} on any failure (same graceful-degradation convention as
    every other real-live-data helper in this module).
    """
    try:
        candidates: dict[str, list[str]] = {
            "temperature": [],
            "humidity": [],
            "battery": [],
        }
        for state in hass.states.async_all("sensor"):
            device_class = state.attributes.get("device_class")
            if device_class in candidates:
                candidates[device_class].append(state.entity_id)
        suggestions: dict[str, str] = {}
        if len(candidates["temperature"]) == 1:
            suggestions[CONF_TEMPERATURE_SENSOR] = candidates["temperature"][0]
        if len(candidates["humidity"]) == 1:
            suggestions[CONF_HUMIDITY_SENSOR] = candidates["humidity"][0]
        if len(candidates["battery"]) == 1:
            suggestions[CONF_SOLVER_BATTERY_SOC_SENSOR] = candidates["battery"][0]
    except Exception:  # noqa: BLE001 -- see docstring: a discovery failure must degrade to "no suggestion", never break the wizard
        return {}
    return suggestions


async def _energy_dashboard_switchboard_suggestions(hass: Any) -> dict[str, str]:
    """Real HA Energy Dashboard config (Settings -> Energy), read in-
    process, as a genuine starting-point SUGGESTION for 5 of the 6
    daily-kWh switchboard fields -- never silently trusted, never a
    `default=` (a locked-in value the household can't tell was auto-
    picked), always folded into `_switchboard_schema()`'s own
    `suggested_value` mechanism: visibly pre-filled, still fully
    editable, still needs an explicit form submit before it's ever
    saved -- the exact same mechanism every other field in this wizard
    already uses. 2026-08-23, direct request (Mark Purcell, relayed):
    "Grab the entities from energy dash to start population of
    wizard" -- and the direct household follow-up worth answering
    honestly, not glossing over: "how would we know its correctness?"
    We don't claim to -- see the two safeguards below.

    Safeguard 1 (type-safety, cheap and real): only ever suggests an
    entity whose device_class == "energy" and state_class in ("total",
    "total_increasing") -- catches "wrong KIND of sensor entirely"
    before it's ever proposed. This project has a real, documented
    precedent for exactly the failure this guards against
    (topology_map.yaml's own comment: sensor.grid_active_power LOOKS
    like the obvious grid sensor but is actually a HAEO plan/forecast
    sensor, not a real measurement).

    Safeguard 2 (never silent): the caller only uses a suggestion for a
    field that's genuinely unset in the household's already-saved
    options -- a real saved value always wins, a suggestion never
    overwrites it. And a suggestion is only ever a `suggested_value`,
    visibly sitting in the form for a human to look at and confirm (or
    fix) before it's ever submitted -- never applied without that.

    What safeguard 1 CANNOT catch (a semantic mismatch -- the right
    KIND of sensor, but genuinely the wrong one) is exactly what
    safeguard 2 is for: a human still has to look at it.

    Real, honest limitation: HA's Energy Dashboard's own configured
    source stat is typically a LIFETIME cumulative total (state_class
    total_increasing, never resets) -- not literally "today's kWh" the
    way e.g. sensor.inverter_import_energy_daily (a daily-resetting
    utility_meter) already is on this household's own real install.
    Suggesting it anyway is still worth it as a starting point -- it
    names the right underlying physical sensor, which is most of the
    real friction in filling this field out cold -- but the household
    may still need a separate daily-resetting utility_meter helper
    built FROM this suggestion (Settings -> Helpers -> Utility Meter),
    not this sensor plugged in directly. No auto-suggestion exists for
    house_load_energy_daily -- HA's Energy Dashboard has no single
    whole-house consumption stat (only per-device, via its own separate
    device_consumption list), inventing one here would be a guess, not
    a suggestion.

    Uses homeassistant.components.energy.data.async_get_manager() --
    genuinely internal HA core API, not a stable, documented public
    contract the way config_entries/entity_registry are (confirmed
    against this repo's own general HA-core familiarity, NOT verified
    against a live HA instance -- no live HA available in this dev
    environment). Wrapped in one broad except for exactly this reason:
    any failure (component not loaded, API shape changed since this was
    written, nothing configured at all) must degrade to "no
    suggestions" silently, the same graceful-degradation convention
    used everywhere else in this codebase -- never break the wizard.
    """
    suggestions: dict[str, str] = {}
    try:
        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(hass)
        sources = (manager.data or {}).get("energy_sources", [])

        def _ok(entity_id: str | None) -> str | None:
            if not entity_id:
                return None
            state = hass.states.get(entity_id)
            if state is None:
                return None
            attrs = state.attributes
            if attrs.get("device_class") != "energy":
                return None
            if attrs.get("state_class") not in ("total", "total_increasing"):
                return None
            return entity_id

        for source in sources:
            source_type = source.get("type")
            if source_type == "grid":
                for flow in source.get("flow_from", []):
                    candidate = _ok(flow.get("stat_energy_from"))
                    if candidate:
                        suggestions.setdefault(
                            CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR, candidate
                        )
                for flow in source.get("flow_to", []):
                    candidate = _ok(flow.get("stat_energy_to"))
                    if candidate:
                        suggestions.setdefault(
                            CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR, candidate
                        )
            elif source_type == "solar":
                candidate = _ok(source.get("stat_energy_from"))
                if candidate:
                    suggestions.setdefault(
                        CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR, candidate
                    )
            elif source_type == "battery":
                # from-battery == discharge, to-battery == charge (HA's
                # own Energy Dashboard convention, matches this file's
                # own real switchboard field names below).
                discharge_candidate = _ok(source.get("stat_energy_from"))
                if discharge_candidate:
                    suggestions.setdefault(
                        CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR,
                        discharge_candidate,
                    )
                charge_candidate = _ok(source.get("stat_energy_to"))
                if charge_candidate:
                    suggestions.setdefault(
                        CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR, charge_candidate
                    )
    except Exception:  # noqa: BLE001 -- see docstring: an Energy Dashboard read failure must never break the wizard, only skip the suggestion
        return {}
    return suggestions


# 2026-08-23, direct Mark Purcell critique of the original single
# 10-field form ("Complex too many entities... if it's optional don't
# show it") plus the household's own sharper follow-up ("nimbus
# entities should be auto detected by topo card and only the daily
# summaries should be a part of a wizard"): grid_meter and
# battery_power are GONE from this form entirely, not just moved to a
# later step -- topology-card-v4.js now auto-discovers both directly
# from whichever Power Signal subentry carries CONF_SIGNAL_ROLE
# "grid"/"battery" (see const.py's own comment on CONF_SIGNAL_ROLE for
# why role has to be explicit, not guessed from naming). What's left
# here is genuinely everything Nimbus has no equivalent for: prices,
# and the 6 daily-kWh accumulator totals -- a household that wants
# none of it can submit this form completely blank and the diagram
# still works off the auto-detected Grid/Battery/Loads alone.
_SWITCHBOARD_SCHEMA_KEYS = (
    CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR,
    CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_HOUSE_LOAD_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR,
    CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR,
    CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR,
)


def _switchboard_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Everything the topology card can show beyond the auto-detected
    Grid/Battery/Loads: prices and the 6 daily-kWh headline stats.
    Every field genuinely optional -- submitting this form completely
    blank is a valid, working configuration."""
    schema_dict: dict[Any, Any] = {}
    for key in _SWITCHBOARD_SCHEMA_KEYS:
        schema_dict[
            vol.Optional(key, description={"suggested_value": defaults.get(key)})
        ] = _entity()
    return vol.Schema(schema_dict)


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
    CONF_TRAINING_SOURCE,
    CONF_HYBRID_RECENT_DAYS,
)
_SOLVER_WIZARD_SCHEMA_KEYS = (
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    CONF_SOLVER_SOLAR_POWER_SENSOR,
    CONF_SOLVER_BATTERY_POWER_SENSOR,
    CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE,
    CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR,
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR,
    CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR,
    CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR,
    CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR,
    CONF_SOLVER_WEATHER_FORECAST_SENSOR,
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

    async def async_step_forecaster(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
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
            #
            # 2026-08-24 (nimbus #121, Mark Purcell's own independent
            # install, real repro): a PARTIAL programmatic update -- an
            # MCP tool calling this options-flow step directly with only
            # 2 of the 6 real keys in user_input, intending to touch just
            # those 2 -- got the other 4 silently wiped to None by this
            # exact loop, since `user_input.get(key)` resolves to None
            # for a key that's genuinely absent for an entirely different
            # reason (the caller never meant to touch it) than the one
            # this loop was built to handle (the real UI submitted the
            # full form and the user genuinely cleared that field).
            #
            # This is a REAL, structural ambiguity, not something a
            # smarter merge can quietly resolve -- confirmed directly
            # (voluptuous test, 2026-08-24): for this file's own
            # `vol.Optional(key, description={"suggested_value": ...})`
            # pattern with NO default=, a key that's truly absent from
            # user_input is ALSO absent from voluptuous's own validated
            # result. Given the already-established, directly-verified
            # 2026-08-22 finding above (a real EntitySelector field,
            # cleared via the actual HA frontend, submits with its key
            # OMITTED, not present-as-None or present-as-""), "key
            # absent" on THIS form is the one and only signal a real UI
            # submission has for "the user cleared this field" -- and a
            # genuine partial patch has no other signal for "leave this
            # one alone" either. Both real, legitimate callers rely on
            # the identical absent-key signal to mean the OPPOSITE thing,
            # and this step function has no way to see caller intent,
            # only the raw dict -- so switching this loop to `if key in
            # user_input: merged[key] = user_input[key]` (the fix that
            # looks obviously correct for the partial-update case) would
            # silently bring the *original* 2026-08-22 bug straight back
            # for every real UI user who clears a field.
            #
            # The correct fix is NOT here -- it's in what the CALLER uses
            # for a genuine partial patch. Home Assistant's own
            # `hass.config_entries.async_update_entry(entry, options=
            # {...})` REPLACES options with exactly the dict given (HA
            # itself does zero merging -- confirmed via
            # `inspect.signature`), so a real partial-patch caller should
            # compute `{**entry.options, **your_partial_dict}` itself and
            # pass that directly to `async_update_entry`, bypassing this
            # options-flow step (and its inherent, load-bearing
            # full-form-submission semantics) entirely. That path can
            # never conflict with a real UI submission, since it's a
            # completely separate code path with its own, unambiguous
            # merge contract. Do NOT "fix" this loop by switching to `if
            # key in user_input` -- it would trade one real, reported bug
            # for a worse, silent regression of an already-shipped one.
            merged = dict(self.config_entry.options)
            for key in _FORECASTER_SCHEMA_KEYS:
                merged[key] = user_input.get(key)
            return self.async_create_entry(title="", data=merged)

        # Type-safe suggestions (2026-08-24, "Group B") fill in ONLY the
        # gaps -- an already-saved real value always wins, a fresh
        # suggestion never overwrites it. Same {**suggestions, **saved}
        # precedence as async_step_switchboard below.
        existing = dict(self.config_entry.options)
        suggestions = _type_safe_entity_suggestions(self.hass)
        form_defaults = {**suggestions, **existing}
        return self.async_show_form(
            step_id="forecaster",
            data_schema=_forecaster_schema(form_defaults),
        )

    async def async_step_switchboard(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Everything the topology card can show beyond what's now
        auto-detected (Grid/Battery power via Power Signal role, every
        Load) -- prices and the 6 daily-kWh headline stats, the only
        things left with no Nimbus equivalent. Every field genuinely
        optional; submitting this blank is a completely valid
        configuration (the diagram still works off auto-detection
        alone). Same explicit-key merge discipline as every other
        options-flow save in this file -- see async_step_forecaster's
        own comment for why that matters."""
        if user_input is not None:
            merged = dict(self.config_entry.options)
            for key in _SWITCHBOARD_SCHEMA_KEYS:
                merged[key] = user_input.get(key)
            return self.async_create_entry(title="", data=merged)

        # Energy Dashboard suggestions (2026-08-23) fill in ONLY the
        # gaps -- an already-saved real value always wins, a fresh
        # suggestion never overwrites it. {**suggestions, **saved} is
        # deliberate: saved keys on the right win the dict-merge.
        existing = dict(self.config_entry.options)
        suggestions = await _energy_dashboard_switchboard_suggestions(self.hass)
        form_defaults = {**suggestions, **existing}
        return self.async_show_form(
            step_id="switchboard", data_schema=_switchboard_schema(form_defaults)
        )

    async def async_step_solver_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            self._solver_data.update(user_input)
            return await self.async_step_solver_grid()
        # Same Group B suggestion mechanism as async_step_forecaster above
        # -- fills the SoC field ONLY when unambiguous, never overwrites
        # an already-saved real value.
        existing = dict(self.config_entry.options)
        suggestions = _type_safe_entity_suggestions(self.hass)
        form_defaults = {**suggestions, **existing}
        return self.async_show_form(
            step_id="solver_battery",
            data_schema=_solver_battery_schema(form_defaults),
        )

    async def async_step_solver_grid(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        if user_input is not None:
            self._solver_data.update(user_input)
            return await self.async_step_solver_sources()
        return self.async_show_form(
            step_id="solver_grid",
            data_schema=_solver_grid_schema(dict(self.config_entry.options)),
        )

    async def async_step_solver_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
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
                    "persistent_notification",
                    "dismiss",
                    {"notification_id": "nimbus_setup_incomplete"},
                )
            except Exception:  # noqa: BLE001, S110 -- see comment above: dismissing a notification must never block a real save; nothing to log or react to beyond that
                pass
            return self.async_create_entry(title="", data=merged)
        single_candidates, summable_candidates = (
            _discover_nimbus_load_forecast_candidates(self.hass)
        )
        return self.async_show_form(
            step_id="solver_sources",
            data_schema=_solver_sources_schema(
                dict(self.config_entry.options), single_candidates, summable_candidates
            ),
        )
