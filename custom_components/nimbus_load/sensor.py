"""Sensor platform for Nimbus.

One SensorEntity per "load" or "power_signal" subentry (not per config
entry -- the hub entry can own many of either): native_value is the
current predicted power (kW), and the `forecast` attribute is a list of
{"time": ..., "value": ...} points -- a generic, self-describing shape,
not tied to any specific downstream consumer.

Each entity is added with config_subentry_id set, which is what makes each
load/signal show up as its own separate device in the HA UI -- e.g. HWS L1,
HWS L3, Pool, and (2026-08-15) Battery/Solar/Grid all independently
visible (and independently able to show `unavailable` if that one's data
goes bad), not folded into one combined device.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import (
    EntityCategory,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.loader import async_get_integration

from . import health, sensor_flattened
from .const import (
    ATTR_FORECAST,
    ATTR_MASE_SCALE_POINTS,
    ATTR_MODE,
    ATTR_MODEL_TRAINED_AT,
    ATTR_RESAMPLE_MINUTES,
    ATTR_SIGNAL_ROLE,
    ATTR_SOURCE_SENSOR,
    ATTR_SUBENTRY_TYPE,
    ATTR_TRAINING_POINTS,
    ATTR_TRAINING_SPAN_DAYS,
    ATTR_VALIDATION_MAE,
    ATTR_VALIDATION_MASE,
    CONF_BATTERY_TOWER_POWER_SOURCE,
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_BATTERY_TOWER_SOH_SENSOR,
    CONF_BATTERY_TOWER_TEMPERATURE_SENSOR,
    CONF_BATTERY_TOWER_VOLTAGE_SENSOR,
    CONF_LOAD_SENSOR,
    CONF_POWER_SOURCE_BATTERY_SENSOR,
    CONF_POWER_SOURCE_DC_SENSOR,
    CONF_POWER_SOURCE_NAME,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_LABEL,
    CONF_PV_STRING_POWER_SOURCE,
    CONF_SIGNAL_ROLE,
    CONF_SOLVE_ON_PRICE_CHANGE,
    CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
    CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
    CONF_SOLVER_BATTERY_POWER_SENSOR,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_BATTERY_SOH_PERCENT,
    CONF_SOLVER_CHARGE_COST,
    CONF_SOLVER_DEGRADATION_COST_PER_KWH,
    CONF_SOLVER_DISCHARGE_COST,
    CONF_SOLVER_EFFICIENCY_PERCENT,
    CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
    CONF_SOLVER_FLAT_FEE_RATE,
    CONF_SOLVER_GRID_MAX_EXPORT_KW,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
    CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW,
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_MAX_CHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY,
    CONF_SOLVER_NETWORK_FEE_1_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_1_RATE,
    CONF_SOLVER_NETWORK_FEE_1_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_2_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_2_RATE,
    CONF_SOLVER_NETWORK_FEE_2_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_3_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_3_RATE,
    CONF_SOLVER_NETWORK_FEE_3_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE,
    CONF_SOLVER_P2P_BLOCK_1_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_1_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_1_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_2_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_2_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_2_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_3_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_3_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_3_START_HOUR,
    CONF_SOLVER_P2P_BONUS_PRICE,
    CONF_SOLVER_P2P_BONUS_VOLUME_KWH,
    CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR,
    CONF_SOLVER_RISK_AVERSION,
    CONF_SOLVER_SALVAGE_VALUE,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
    CONF_SOLVER_SOLAR_POWER_SENSOR,
    CONF_SOLVER_WEATHER_FORECAST_SENSOR,
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
    DOMAIN,
    SIGNAL_ROLE_OTHER,
    SUBENTRY_TYPE_BATTERY_TOWER,
    SUBENTRY_TYPE_LOAD,
    SUBENTRY_TYPE_POWER_SOURCE,
    SUBENTRY_TYPE_PV_STRING,
    SUBENTRY_TYPE_SIGNAL,
)
from .coordinator import NimbusConfigEntry, NimbusCoordinator

# All real fields a topology-metadata subentry can carry, keyed by its
# own subentry_type -- see NimbusTopologyConfigSensor's own docstring
# for why these need bridging out to a plain sensor the same way the
# Solver's own hub-level options do (config_entries.subentries is not
# exposed via HA's plain REST API either, same root constraint).
_POWER_SOURCE_KEYS = (
    CONF_POWER_SOURCE_NAME,
    CONF_POWER_SOURCE_BATTERY_SENSOR,
    CONF_POWER_SOURCE_DC_SENSOR,
)
_PV_STRING_KEYS = (
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_LABEL,
    CONF_PV_STRING_POWER_SOURCE,
)
_BATTERY_TOWER_KEYS = (
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_BATTERY_TOWER_SOH_SENSOR,
    CONF_BATTERY_TOWER_VOLTAGE_SENSOR,
    CONF_BATTERY_TOWER_TEMPERATURE_SENSOR,
    CONF_BATTERY_TOWER_POWER_SOURCE,
)
_SWITCHBOARD_KEYS = (
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

# All real work (the retrain/inference cycle) happens once per coordinator,
# already serialized by its own async_track_time_change/interval scheduling
# -- entity updates here are just reading already-computed coordinator.data,
# so there's no hub call to protect by limiting concurrency.
PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)

_FORECASTABLE_SUBENTRY_TYPES = (SUBENTRY_TYPE_LOAD, SUBENTRY_TYPE_SIGNAL)

# The full set of Solver settings NimbusSolverConfigSensor exposes, below.
# Required ones (state == "configured" needs every one of these to have a
# real value) vs. the full set (everything, required + optional, published
# as attributes either way).
_SOLVER_REQUIRED_KEYS = (
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_MAX_CHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_KW,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_GRID_MAX_EXPORT_KW,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
)
_SOLVER_ALL_KEYS = _SOLVER_REQUIRED_KEYS + (
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
    # Same exact bug class as the SOLAR_FORECAST_SENSOR_2/_3 note directly
    # below this line (2026-08-25, direct household ask: "u also are
    # missing my blended price forecasts... in case we can feed it more
    # than one... e.g. aemo... and amber") -- added to
    # _SOLVER_WIZARD_SCHEMA_KEYS and genuinely saved into entry.options,
    # but this bridge sensor must ALSO expose them or
    # fetch_solver_config() never sees them. Caught immediately by this
    # file's own test_sensor_solver_config_keys.py before it ever shipped.
    CONF_SOLVER_IMPORT_PRICE_SENSOR_2,
    CONF_SOLVER_IMPORT_PRICE_SENSOR_3,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_2,
    CONF_SOLVER_EXPORT_PRICE_SENSOR_3,
    # Real bug found live (2026-08-23): both added to _SOLVER_WIZARD_
    # SCHEMA_KEYS (flows/hub_options.py) and genuinely saved into
    # entry.options by the wizard, but never added HERE -- meaning this
    # bridge sensor never exposed them at all, so
    # nimbus_solver_forecast_writer.py's fetch_solver_config() (which
    # reads config ONLY through this sensor's own attributes, never
    # entry.options directly -- see that function's own docstring for
    # why) could never see them regardless of how many times the wizard
    # was resubmitted. The real, saved value in entry.options was never
    # the problem; this sensor silently omitting it from what it exposes
    # was.
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    # 2026-08-24, nimbus #125: same exact class of bug as the 2026-08-23
    # note directly above this block (SOLAR_FORECAST_SENSOR_2/_3) --
    # saved into entry.options by the wizard's own solver_battery step,
    # but not exposed here means fetch_solver_config() (this bridge
    # sensor's own only real consumer) can never see it either. Caught
    # by this file's own real test_sensor_solver_config_keys.py, which
    # exists specifically to catch this exact class of mistake.
    CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    CONF_SOLVER_SOLAR_POWER_SENSOR,
    CONF_SOLVER_WEATHER_FORECAST_SENSOR,
    CONF_SOLVER_BATTERY_POWER_SENSOR,
    CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR,
    CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW,
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    CONF_SOLVER_BATTERY_SOH_PERCENT,
    CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
    CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
    CONF_SOLVER_EFFICIENCY_PERCENT,
    CONF_SOLVER_CHARGE_COST,
    CONF_SOLVER_DISCHARGE_COST,
    CONF_SOLVER_SALVAGE_VALUE,
    CONF_SOLVER_DEGRADATION_COST_PER_KWH,
    CONF_SOLVER_P2P_BONUS_PRICE,
    CONF_SOLVER_P2P_BONUS_VOLUME_KWH,
    CONF_SOLVER_P2P_BLOCK_1_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_1_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_1_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_2_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_2_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_2_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_3_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_3_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_3_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE,
    CONF_SOLVER_NETWORK_FEE_1_RATE,
    CONF_SOLVER_NETWORK_FEE_1_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_1_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_2_RATE,
    CONF_SOLVER_NETWORK_FEE_2_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_2_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_3_RATE,
    CONF_SOLVER_NETWORK_FEE_3_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_3_END_HOUR,
    CONF_SOLVER_FLAT_FEE_RATE,
    CONF_SOLVER_RISK_AVERSION,
    CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION,
)
# 2026-08-20: these 14 plain-numeric fields moved off entry.options entirely
# -- they're now LIVE, dashboard-editable number.nimbus_solver_* entities
# (number.py), so a household can tune discharge cost/salvage/grid limits
# the same way they already tune everything else on a dashboard, instead of
# re-opening the config-flow wizard every time. See number.py's own module
# docstring for the full "why not just write these back into entry.options"
# reasoning (avoids a full hub reload on every dashboard tweak). The 5
# entity-pointer fields (SoC/price/forecast sensors) are genuinely one-time
# "which entity is this" setup choices, not something anyone would slide on
# a dashboard -- those stay wizard-only, sourced from entry.options exactly
# as before.
_SOLVER_NUMBER_ENTITY_KEYS = (
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_SOH_PERCENT,
    CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
    CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
    CONF_SOLVER_MAX_CHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_KW,
    CONF_SOLVER_EFFICIENCY_PERCENT,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_GRID_MAX_EXPORT_KW,
    CONF_SOLVER_CHARGE_COST,
    CONF_SOLVER_DISCHARGE_COST,
    CONF_SOLVER_SALVAGE_VALUE,
    CONF_SOLVER_DEGRADATION_COST_PER_KWH,
    CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW,
    CONF_SOLVER_P2P_BONUS_PRICE,
    CONF_SOLVER_P2P_BONUS_VOLUME_KWH,
    CONF_SOLVER_P2P_BLOCK_1_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_1_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_1_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_2_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_2_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_2_END_HOUR,
    CONF_SOLVER_P2P_BLOCK_3_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_3_START_HOUR,
    CONF_SOLVER_P2P_BLOCK_3_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE,
    CONF_SOLVER_NETWORK_FEE_1_RATE,
    CONF_SOLVER_NETWORK_FEE_1_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_1_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_2_RATE,
    CONF_SOLVER_NETWORK_FEE_2_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_2_END_HOUR,
    CONF_SOLVER_NETWORK_FEE_3_RATE,
    CONF_SOLVER_NETWORK_FEE_3_START_HOUR,
    CONF_SOLVER_NETWORK_FEE_3_END_HOUR,
    CONF_SOLVER_FLAT_FEE_RATE,
    CONF_SOLVER_RISK_AVERSION,
    CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    # Issue #232 follow-up: paired live number entity for the price-
    # change debounce window (see switch.py's own
    # NimbusSolverSwitch entry for the paired toggle).
    CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
)
# 2026-08-22: switch.py's own one live boolean toggle -- same
# "resolve from a live entity, not entry.options" mechanism as
# _SOLVER_NUMBER_ENTITY_KEYS above, just a different entity domain
# (switch.nimbus_{key}, "on"/"off" state -> bool) since HA has no
# combined number-or-boolean entity type. See switch.py's own module
# docstring for the full "why this exists" story.
_SOLVER_SWITCH_ENTITY_KEYS = (
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    # Issue #232 follow-up: moved out of the wizard's solver_grid step
    # into switch.nimbus_solve_on_price_change (see switch.py's own
    # NimbusSolverSwitch registration for the full "why not the wizard"
    # story). The bridge sensor still exposes it via this same resolve
    # path, so diagnostics and any downstream reader that already reads
    # sensor.nimbus_solver_config's attributes keeps working with no
    # change on their side.
    CONF_SOLVE_ON_PRICE_CHANGE,
)


def object_id_from_source(load_sensor_entity_id: str) -> str:
    """Turn 'sensor.logger_load_power' into
    'nimbus_logger_load_power_forecast' -- a clean, predictable,
    source-derived slug, rather than letting Home Assistant auto-combine
    the device title and entity name into whatever it lands on (confirmed
    live 2026-08-14: produced
    sensor.load_sensor_logger_load_power_load_forecast, an unusable mess).

    The "nimbus_" prefix is deliberate, not decorative: confirmed live the
    same day that a bare "<source>_forecast" pattern collides with a
    completely unrelated pre-existing forecast sensor from a different
    integration (sensor.logger_load_power_forecast already existed) --
    without the prefix, Home Assistant would have silently registered
    Nimbus's own sensor under a "_2" suffix instead of erroring, exactly
    the kind of quiet collision this project has been bitten by before.
    The prefix also makes it unambiguous which forecast sensor is
    Nimbus's, given this project already runs several other forecasters
    in parallel (Solcast, Open-Meteo, ha_power_predictor, etc.).

    Naturally unique across every load Nimbus itself creates, since each
    one's source sensor is already unique.
    """
    object_id = load_sensor_entity_id.split(".", 1)[-1]
    return f"nimbus_{object_id}_forecast"


async def _remediate_forecast_lts_unit(
    hass: HomeAssistant, entity_id: str, expected_unit: str
) -> None:
    """Silently correct a nimbus forecast entity's long-term-statistics
    metadata if it was seeded with an empty/missing unit (nimbus issue
    #263, Mark Purcell) -- so the user never sees HA's "unit has changed"
    repair dialog for an entity that has always reported a real unit.

    2026-08-28, verified against real HA recorder internals (installed
    homeassistant 2025.1.4) before writing this, not assumed from the
    issue's own sketch: the originally-proposed fix
    (`recorder.async_change_statistics_unit`) is NOT the right tool here
    -- it internally calls `can_convert_units(old_unit, new_unit)` first,
    and `can_convert_units("", "kW")` is confirmed `False` (empty string
    has no known unit family to convert FROM), so calling it with the
    exact `old_unit_of_measurement=""` this function is meant to fix
    would raise `HomeAssistantError` immediately, not repair anything.

    The real, correct mechanism (confirmed by reading
    `homeassistant/components/recorder/websocket_api.py`'s own
    `ws_update_statistics_metadata` handler -- the literal code behind
    the Statistics page's "change unit" fix button in HA's own UI) is
    `Recorder.async_update_statistics_metadata(new_unit_of_measurement=...)`.
    This is a raw metadata relabel with no `can_convert_units` gate at
    all -- the correct semantics for "this row's unit was never
    correctly recorded in the first place," as opposed to
    `async_change_statistics_unit`'s actual job (numerically RESCALING
    already-stored statistic values from one real unit to another,
    e.g. W history being reinterpreted as kW).

    Deliberately never touches a row that already holds any OTHER real
    unit -- only ever relabels a genuinely empty/`None` one. Wrapped in
    a broad try/except: this is a one-time cosmetic cleanup, and must
    never be capable of blocking or crashing real entity setup.
    """
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import get_metadata

        recorder = get_instance(hass)
        metadata = await recorder.async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids={entity_id})
        )
        entry = metadata.get(entity_id)
        if entry is None:
            return  # no LTS row exists yet for this entity_id -- nothing to fix
        _metadata_id, meta = entry
        stored_unit = meta.get("unit_of_measurement")
        if stored_unit not in (None, ""):
            return  # already correct, or a genuinely different unit -- never touch that

        done = asyncio.Event()
        recorder.async_update_statistics_metadata(
            entity_id,
            new_unit_of_measurement=expected_unit,
            on_done=lambda: hass.loop.call_soon_threadsafe(done.set),
        )
        async with asyncio.timeout(10):
            await done.wait()
        _LOGGER.info(
            "Nimbus: corrected empty long-term-statistics unit for %s -> %s",
            entity_id,
            expected_unit,
        )
    except Exception:  # never let a cosmetic LTS fixup break real entity setup
        _LOGGER.exception(
            "Nimbus: LTS unit remediation failed for %s -- harmless, HA's own "
            "'unit has changed' repair dialog may still appear once",
            entity_id,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NimbusConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinators = entry.runtime_data
    # Real, live version, read from this integration's own manifest.json --
    # single source of truth, no more hand-syncing a version string in a
    # second place (this exact staleness bit a downstream dashboard card,
    # topology-card-v4.js's own footer, showing "0.6.0" long after the
    # real running version had moved on to 0.29.0). Attached to each
    # device's own sw_version below so any consumer (HA's own device page,
    # or a card reading hass.devices directly) can read the real version
    # live instead of needing its own separately-maintained copy.
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version) if integration.version else None
    for subentry in entry.subentries.values():
        if subentry.subentry_type not in _FORECASTABLE_SUBENTRY_TYPES:
            continue
        coordinator = coordinators.get(subentry.subentry_id)
        if coordinator is None:
            continue
        forecast_entity = NimbusForecastSensor(coordinator, subentry, sw_version)
        async_add_entities(
            [forecast_entity],
            config_subentry_id=subentry.subentry_id,
        )
        # nimbus issue #263 (Mark Purcell): fire-and-forget, non-blocking --
        # a one-time LTS metadata cleanup must never delay real entity setup.
        # See _remediate_forecast_lts_unit's own docstring for the verified
        # mechanism and why the issue's own originally-sketched fix would
        # have raised instead of repairing anything.
        hass.async_create_task(
            _remediate_forecast_lts_unit(
                hass, forecast_entity.entity_id, UnitOfPower.KILO_WATT
            )
        )

    # One per hub, NOT per subentry (added straight to the top-level
    # config entry, no config_subentry_id) -- the Solver's own config is
    # hub-wide, not per-load. Created fresh every time this function runs,
    # which itself re-runs on every options save (see __init__.py's own
    # _async_update_listener -> async_reload) -- so this sensor's own
    # attributes are always current with zero extra update-listener
    # plumbing needed here.
    async_add_entities([NimbusSolverConfigSensor(entry, sw_version)])

    # Same "one per hub" reasoning as NimbusSolverConfigSensor above --
    # topology-card-v4.js's own live discovery (_discoverLoads()) works
    # for Load subentries because every one of those publishes a real
    # sensor.nimbus_*_forecast entity to scan hass.states for. Power
    # Source / PV String / Battery Tower subentries publish NOTHING --
    # they're pure wiring metadata, no coordinator, no forecast, by
    # design (see const.py's own comment above SUBENTRY_TYPE_POWER_
    # SOURCE) -- so without this bridge the topology card would have no
    # way to see them at all.
    async_add_entities([NimbusTopologyConfigSensor(entry, sw_version)])

    # Always-on health report (2026-08-25) -- same "one per hub" pattern
    # as the two bridge sensors above. See NimbusHealthReportSensor's
    # own docstring.
    async_add_entities([NimbusHealthReportSensor(entry, sw_version)])

    # Price-response-latency sensor (issue #294) -- same "one per hub"
    # pattern as the two entities above. Registered with solver_runtime
    # (not solver_writer's dispatch table -- this sensor's data never
    # comes from a solve's own output, it comes from __init__.py's price-
    # watcher/cron scheduling code, a different call path entirely) AFTER
    # async_add_entities, same reasoning as the register_entity_handler()
    # calls further down: the entity must be known to hass before the
    # first price-change solve can publish through it.
    price_latency_sensor = NimbusSolverPriceResponseLatencySensor(entry, sw_version)
    async_add_entities([price_latency_sensor])
    # Re-imports (cheap, already cached in sys.modules) the same module
    # the mirror/push-sensor registration further down this function
    # imports under this identical name -- no conflict, deliberately not
    # hoisted to module top, see that later import's own comment for why.
    from . import solver_runtime

    solver_runtime.register_price_latency_sensor(price_latency_sensor)

    # Hub-level Solver-output entities (2026-08-23, issue #55) --
    # migrated off solver_writer.ha_post_state()'s raw states.async_set()
    # fallback onto real SensorEntity classes. The dispatch table over
    # in solver_writer.py routes writes for these two entity_ids through
    # each entity's own update_from_solver() instead, so they get proper
    # unique_id / device_info / device_class / unrecorded_attributes
    # treatment (fixes #59, #61, #62 as a side effect). Register the
    # handlers AFTER async_add_entities so the entities are known to
    # hass by the time the first solve tick fires. Handlers persist for
    # the lifetime of this config-entry setup; a reload cleanly
    # re-registers replacement instances (see register_entity_handler()'s
    # own "idempotent" docstring in solver_writer.py).
    battery_forecast = NimbusSolverBatteryForecastSensor(entry, sw_version)
    household_load_forecast = NimbusHouseholdLoadTotalForecastSensor(entry, sw_version)
    # Dry-run dispatch evidence trail (2026-08-28) -- same registration
    # pattern as the two lines above, see NimbusDispatchDryRunSensor's
    # own docstring for why this needs to be a real recorded entity, not
    # a log line.
    dispatch_dry_run = NimbusDispatchDryRunSensor(entry, sw_version)
    # Flattened per-attribute fan-out (2026-08-29) -- every top-level
    # scalar attribute of sensor.nimbus_solver_battery_forecast becomes
    # its own SensorEntity so it participates in HA history, LTS, and
    # per-entity graphs on the same footing as the existing solver_
    # config bridge sensor. See sensor_flattened.py for the declarative
    # table (Family A: top-level scalars only) and the category rules.
    # Purely additive: the parent sensor keeps every attribute it
    # publishes today; the flattened children are updated by a
    # synchronous fan-out inside the parent's own update_from_solver()
    # override (see _flattened_entities slot on that class below).
    flattened_entities = sensor_flattened.create_flattened_entities(entry, sw_version)
    battery_forecast._flattened_entities = flattened_entities
    # Family B (2026-08-29, v0.94.20 CHANGELOG deferred item): the same
    # fan-out pattern applied to the current-period row of the parent's
    # `forecast` list. Attaches to the hub device alongside Family A --
    # both families share the parent sensor's update lifecycle and
    # neither needs a separate push registration. See FLATTENED_ATTRS_
    # CURRENT in sensor_flattened.py for the declarative table.
    flattened_current_entities = sensor_flattened.create_flattened_entities_current(
        entry, sw_version
    )
    battery_forecast._flattened_current_entities = flattened_current_entities
    # Issue #290 fix (2026-08-30) -- the two weather-mirror dashboard
    # sensors, same registration treatment as the sensors above. See
    # NimbusMirrorTemperatureForecastSensor's own docstring for why
    # these were never migrated when the others were.
    mirror_temperature_forecast = NimbusMirrorTemperatureForecastSensor(
        entry, sw_version
    )
    mirror_humidity_forecast = NimbusMirrorHumidityForecastSensor(entry, sw_version)
    async_add_entities(
        [
            battery_forecast,
            household_load_forecast,
            dispatch_dry_run,
            mirror_temperature_forecast,
            mirror_humidity_forecast,
        ]
        + flattened_entities
        + flattened_current_entities
    )
    # Deferred import (same reasoning as solver_runtime.py's own
    # _ensure_ready(): solver_writer imports the pure-Python `solver`
    # and `ml` packages via a bare `from solver import ...` at module
    # top, which resolves against sys.path -- fine at real HA runtime
    # where solver_runtime.py sets that up before ever touching this
    # code path, but importing it at THIS module's top would drag that
    # requirement into every unit test that only wants to exercise the
    # NimbusForecastSensor / NimbusSolverConfigSensor / NimbusTopology-
    # ConfigSensor classes above. Moving it inside async_setup_entry --
    # which is only ever called by real HA -- keeps the test surface
    # unchanged and matches the pattern solver_runtime.py already uses).
    #
    # Real bug fixed here (issue #89, Mark Purcell, 2026-08-23): this is
    # genuinely the FIRST place solver_writer.py can get imported in the
    # whole process on a fresh start -- BEFORE solver_runtime.py's own
    # _ensure_ready() ever runs (that's only reached later, from the
    # periodic solve loop). Without the env vars set first, solver_
    # writer.py's own module-level sys.path.insert() fell back to this
    # HOUSEHOLD's own hardcoded NUC path -- wrong on every other install,
    # crashing with ModuleNotFoundError: No module named 'ml'/'solver' on
    # Mark's real HACS install, every single restart. set_default_env_
    # vars() is a cheap, pure os.environ.setdefault()/hass.config.path()
    # helper -- no disk I/O, no solver_writer import of its own -- safe
    # to call directly here on the event loop, unlike the actual
    # solver_writer import/solve cycle (which genuinely does need the
    # worker-thread treatment _ensure_ready()'s own callers already give
    # it, for unrelated reasons -- see that function's own docstring).
    from . import solver_runtime

    solver_runtime.set_default_env_vars(hass)
    from . import solver_writer

    # real_entity_id= (2026-08-31): the dispatch key stays the literal
    # string (unchanged, still correct for ha_post_state()'s own lookup)
    # -- passing entity.entity_id alongside it lets solver_writer.py's
    # own idempotency self-reads (ha_get(resolve_real_entity_id(...)))
    # find THIS entity even when the literal name is already claimed by
    # something else in this HA instance (e.g. a remote_homeassistant
    # mirror of another Nimbus install using the same sensor names --
    # confirmed live on devhub). See register_entity_handler()'s and
    # resolve_real_entity_id()'s own docstrings in solver_writer.py for
    # the full incident this fixes.
    solver_writer.register_entity_handler(
        "sensor.nimbus_solver_battery_forecast",
        battery_forecast.update_from_solver,
        battery_forecast.entity_id,
    )
    solver_writer.register_entity_handler(
        "sensor.nimbus_household_load_total_forecast",
        household_load_forecast.update_from_solver,
        household_load_forecast.entity_id,
    )
    solver_writer.register_entity_handler(
        "sensor.nimbus_solver_dispatch_dry_run",
        dispatch_dry_run.update_from_solver,
        dispatch_dry_run.entity_id,
    )
    solver_writer.register_entity_handler(
        "sensor.nimbus_mirror_temperature_forecast",
        mirror_temperature_forecast.update_from_solver,
        mirror_temperature_forecast.entity_id,
    )
    solver_writer.register_entity_handler(
        "sensor.nimbus_mirror_humidity_forecast",
        mirror_humidity_forecast.update_from_solver,
        mirror_humidity_forecast.entity_id,
    )

    # Family-A completion (2026-08-29, issue #55 follow-up) -- the three
    # remaining raw-REST-fallback parent sensors: quality_report (daily),
    # efficiency_backtest (weekly), counterfactual_soc (daily). Same
    # dispatch-table seam as the three sensors above; each parent gets
    # its own sub-device (via_device -> hub) so the 16 new scalar
    # children fan out under a dedicated device page instead of piling
    # onto the hub. See NimbusSolverQualityReportSensor / Nimbus-
    # EfficiencyBacktestSensor / NimbusCounterfactualSocSensor below
    # and the FLATTENED_ATTRS_QUALITY / _BACKTEST / _COUNTERFACTUAL
    # tables in sensor_flattened.py for the declarative fan-out.
    quality_report = NimbusSolverQualityReportSensor(entry, sw_version)
    efficiency_backtest = NimbusEfficiencyBacktestSensor(entry, sw_version)
    counterfactual_soc = NimbusCounterfactualSocSensor(entry, sw_version)

    flattened_quality = sensor_flattened.create_flattened_entities_quality(
        entry, sw_version
    )
    flattened_backtest = sensor_flattened.create_flattened_entities_backtest(
        entry, sw_version
    )
    flattened_counterfactual = (
        sensor_flattened.create_flattened_entities_counterfactual(entry, sw_version)
    )

    quality_report._flattened_entities = flattened_quality
    efficiency_backtest._flattened_entities = flattened_backtest
    counterfactual_soc._flattened_entities = flattened_counterfactual

    async_add_entities(
        [quality_report, efficiency_backtest, counterfactual_soc]
        + flattened_quality
        + flattened_backtest
        + flattened_counterfactual
    )

    # real_entity_id= -- see the matching comment on the five
    # register_entity_handler() calls above; same fix, same reason.
    solver_writer.register_entity_handler(
        "sensor.nimbus_solver_quality_report",
        quality_report.update_from_solver,
        quality_report.entity_id,
    )
    solver_writer.register_entity_handler(
        "sensor.nimbus_efficiency_backtest",
        efficiency_backtest.update_from_solver,
        efficiency_backtest.entity_id,
    )
    solver_writer.register_entity_handler(
        "sensor.nimbus_counterfactual_soc",
        counterfactual_soc.update_from_solver,
        counterfactual_soc.entity_id,
    )


class NimbusForecastSensor(CoordinatorEntity[NimbusCoordinator], SensorEntity):
    """The published forecast for one load or power-signal subentry."""

    _attr_has_entity_name = True
    _attr_name = "Forecast"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    # Confirmed live 2026-08-15: without this, HA's own history-graph
    # tooltips (and any UI that computes a rolling average across
    # several already-rounded points, e.g. "5-minute aggregated") show
    # raw binary floating-point noise -- "0.152000000000000020" instead
    # of "0.152". round(v, 3) in the coordinator only cleans up the
    # value AT THE MOMENT it's published; averaging several such values
    # together elsewhere reintroduces the noise, since 0.152 has no
    # exact binary representation to begin with. This tells every HA
    # frontend surface (not just this one) to always DISPLAY at most 3
    # decimal places, regardless of what the underlying float actually
    # is -- fixes the display everywhere at once instead of chasing
    # every individual UI that might recompute an average.
    _attr_suggested_display_precision = 3
    # Recorder's own 16 KB per-attribute limit (issue #59) -- real fix
    # #99: PR #77 added this to _NimbusSolverPushSensor (below) but
    # missed this class, so subentry-published load/signal forecasts
    # (the ones an installer actually configures -- a real household's
    # own 18 loads, or Mark's own confirmed-live #99 report showing
    # this warning firing hundreds of times for his signal forecasts)
    # kept hitting the same "exceeds maximum size of 16384 bytes"
    # warning #77 was meant to close everywhere. Same reasoning as the
    # sibling class: the forecast is a projection, not a historical
    # fact worth keeping in the long-term stats database.
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self,
        coordinator: NimbusCoordinator,
        subentry: ConfigSubentry,
        sw_version: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        # nimbus issue #263 (Mark Purcell) -- belt-and-braces instance-level
        # assignment, NOT the actual fix for the reported "unit has changed"
        # repair. Verified directly: Python already resolves the class-scope
        # _attr_* declarations above via normal attribute lookup on every
        # `self.native_unit_of_measurement` read, from the very first state
        # write onward -- there is no timing window where these three
        # class-level values fail to resolve correctly. The real fix for
        # the reported symptom is _remediate_forecast_lts_unit (called from
        # async_setup_entry below), which corrects an already-stale, empty
        # long-term-statistics metadata row left over from before this
        # unit was ever declared. This instance-level copy is kept only as
        # cheap, harmless defensiveness against a future refactor (e.g. a
        # subclass computing its own unit dynamically) that might rely on
        # `self._attr_*` rather than the class attribute.
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
        # Deliberately NOT changed to a generic suffix for existing load
        # subentries -- an already-deployed entity's unique_id must never
        # change, or Home Assistant treats it as a brand new entity and
        # orphans the old one (losing its history/registry entry). Signal
        # subentries are new as of this same change, so they get their
        # own distinct, accurate suffix from day one instead.
        suffix = (
            "_signal_forecast"
            if subentry.subentry_type == SUBENTRY_TYPE_SIGNAL
            else "_load_forecast"
        )
        self._attr_unique_id = f"{subentry.subentry_id}{suffix}"
        # Exposed as a live attribute (2026-08-15) so anything downstream
        # (e.g. a dashboard chart script) can tell a load forecast apart
        # from a power-signal forecast generically -- by reading this
        # attribute at runtime, not by hardcoding entity names. Same
        # design principle already applied to ATTR_MODE.
        self._subentry_type = subentry.subentry_type
        # Explicit role (2026-08-23, see const.py's CONF_SIGNAL_ROLE for
        # the full "why not inferred from naming" reasoning) -- exposed
        # as a live attribute the same way subentry_type already is, so
        # the topology dashboard card can auto-discover "which power
        # signal is Grid/Battery/Solar" directly from hass.states, zero
        # config file needed. Meaningless-but-harmless on a load
        # subentry (never has this field set, defaults to "other").
        self._signal_role = subentry.data.get(CONF_SIGNAL_ROLE, SIGNAL_ROLE_OTHER)
        # Silver `entity-unavailable` (2026-08-22, real Mark Purcell audit
        # finding, confirmed correct against this module's own docstring
        # goal above -- "independently able to show unavailable if that
        # one's data goes bad" was always the intent, just never actually
        # wired up): stored so available() below can check the REAL,
        # LIVE source sensor, not just whether the coordinator's last
        # update technically succeeded.
        self._source_sensor = subentry.data[CONF_LOAD_SENSOR]
        # Same Silver finding's log-when-unavailable pairing -- tracks
        # whether the last _handle_coordinator_update() call found this
        # entity available, so a genuine state CHANGE logs exactly once
        # in each direction rather than either staying silent or
        # spamming a log line on every coordinator refresh regardless of
        # whether anything actually changed.
        self._was_available: bool | None = None
        # Setting entity_id directly, not _attr_suggested_object_id.
        # Confirmed live 2026-08-14, twice, that _attr_suggested_object_id
        # is NOT respected here: with _attr_has_entity_name = True, Home
        # Assistant derives the entity_id from the device-name + entity-
        # name combination FIRST, and only falls back to suggested_object_id
        # after that -- so the "fix" silently never took effect, on either
        # the whole-house load or a genuinely brand-new one, and both had
        # to be renamed by hand. Setting entity_id directly is the one
        # mechanism the entity platform never overrides -- if it's already
        # set when the entity is added, generation is skipped entirely.
        self.entity_id = (
            f"sensor.{object_id_from_source(subentry.data[CONF_LOAD_SENSOR])}"
        )
        model = (
            "Power Signal Forecaster"
            if subentry.subentry_type == SUBENTRY_TYPE_SIGNAL
            else "Load Forecaster"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Nimbus",
            model=model,
            sw_version=sw_version,
        )

    @property
    def available(self) -> bool:
        """Silver `entity-unavailable` fix (2026-08-22). Overrides
        CoordinatorEntity's own default (`coordinator.last_update_success`
        alone) -- that default only catches a coordinator update that
        actively FAILED, not the real, distinct case this was built to
        catch: the coordinator's last update genuinely succeeded (maybe
        hours ago, maybe just before a retrain), but the real, live
        SOURCE sensor has since gone unavailable. Without this, the
        forecast entity keeps confidently reporting its last-known value
        forever -- a silently wrong dashboard reading, worse than an
        honest "unavailable," which is exactly the real bug this closes.
        """
        if not self.coordinator.last_update_success:
            return False
        if self.coordinator.data is None:
            return False
        source_state = self.hass.states.get(self._source_sensor)
        return not (
            source_state is None or source_state.state in ("unavailable", "unknown")
        )

    def _handle_coordinator_update(self) -> None:
        """Same Silver fix's log-when-unavailable pairing -- logs exactly
        once on a genuine transition in either direction, never per-tick.
        `available` above is a pure property (no side effects, safe to
        call as often as HA likes) -- this hook is the one place that
        only fires once per real coordinator refresh, so it's the
        correct place to detect and log a CHANGE rather than a snapshot.
        """
        now_available = self.available
        if self._was_available is not None and now_available != self._was_available:
            if now_available:
                _LOGGER.info("Nimbus: %s is available again", self.entity_id)
            else:
                _LOGGER.info(
                    "Nimbus: %s is now unavailable (source sensor %s unavailable, or "
                    "the last coordinator update failed)",
                    self.entity_id,
                    self._source_sensor,
                )
        self._was_available = now_available
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        return self.coordinator.data.get("state") if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            ATTR_FORECAST: data.get("forecast", []),
            ATTR_MODE: data.get("mode", "unscheduled"),
            ATTR_MODEL_TRAINED_AT: data.get("trained_at"),
            ATTR_TRAINING_POINTS: data.get("training_points", 0),
            ATTR_VALIDATION_MAE: data.get("validation_mae", {}),
            ATTR_VALIDATION_MASE: data.get("validation_mase", {}),
            ATTR_MASE_SCALE_POINTS: data.get("mase_scale_points", 0),
            ATTR_RESAMPLE_MINUTES: data.get("resample_minutes", 0),
            ATTR_TRAINING_SPAN_DAYS: data.get("training_span_days", 0.0),
            ATTR_SUBENTRY_TYPE: self._subentry_type,
            ATTR_SIGNAL_ROLE: self._signal_role,
            ATTR_SOURCE_SENSOR: self._source_sensor,
        }


class NimbusSolverConfigSensor(SensorEntity):
    """Bridges the hub's own Solver settings (flows/hub_options.py's
    "Solver settings" wizard, entry.options) out to a plain sensor an
    EXTERNAL host script can read -- see this class's own module-level
    comment above _SOLVER_REQUIRED_KEYS for exactly why this bridge
    exists (config_entries.options is not exposed via HA's plain REST
    API, confirmed live 2026-08-20).

    2026-08-20, direct household ask: "close this gap [installability]
    ... or get rid of it totally - need its own installer and inputs
    period." Before this sensor existed, nimbus_solver_forecast_writer.py
    (the sibling 116KAT-HA-AI repo's standalone Solver script) read its
    battery/grid config from a set of ad-hoc input_number.nimbus_solver_*
    helpers that had to be hand-created via a separate YAML package file
    -- a real installer for someone else (Mark Purcell, or anyone) would
    have needed to know that undocumented convention existed at all. This
    sensor is what lets the writer read the SAME config a normal HA user
    fills in through the config-flow UI, with no YAML, no hand-editing.

    One per hub, not per load/signal subentry -- there's exactly one real
    battery/grid/inverter per household, this isn't a per-load setting.
    """

    _attr_has_entity_name = True
    _attr_name = "Solver Config"
    _attr_entity_category = None  # a real, actively-read data source, not a diagnostic

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        self._entry = entry
        # Issue #85 instrumentation (Mark Purcell, 2026-08-23): track the
        # LAST computed native_value so a real transition (configured ->
        # unconfigured or back) can be logged at WARNING, and a no-op
        # re-read stays silent. Without this, ANY startup race where a
        # required number.nimbus_solver_* is briefly unknown during
        # RestoreEntity is invisible in logs -- it only appears as a
        # confusing "not configured yet" WARNING from solver_runtime.py,
        # with no direct signal that this sensor was the one that
        # temporarily said unconfigured. See #85 for the full trace.
        self._last_computed_state: str | None = None
        self._attr_unique_id = f"{entry.entry_id}_solver_config"
        # Fixed entity_id (same technique/reasoning as NimbusForecastSensor's
        # own entity_id assignment above) -- there's only ever one of these
        # per hub, so a fixed, predictable name is correct here, not a
        # HA-derived one.
        self.entity_id = "sensor.nimbus_solver_config"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )

    def _resolve(self, key: str):
        """One field's real current value, from whichever place is now
        authoritative for it -- see this class's own module-level comment
        above _SOLVER_NUMBER_ENTITY_KEYS for the full "two different
        sources, on purpose" reasoning.
        """
        if key in _SOLVER_NUMBER_ENTITY_KEYS:
            state = self.hass.states.get(f"number.nimbus_{key}")
            if state is None or state.state in (None, "unknown", "unavailable"):
                return None
            try:
                return float(state.state)
            except ValueError:
                return None
        if key in _SOLVER_SWITCH_ENTITY_KEYS:
            state = self.hass.states.get(f"switch.nimbus_{key}")
            if state is None or state.state in (None, "unknown", "unavailable"):
                return None
            return state.state == "on"
        return self._entry.options.get(key)

    def _unresolved_required_keys(self) -> list[str]:
        """The REQUIRED keys whose _resolve() currently returns None/"" --
        i.e. the specific fields that force native_value == "unconfigured"
        right now. Broken out from native_value so log messages and the
        extra_state_attributes `unresolved_required_keys` attribute both
        see the same, live-computed list, no drift. Real diagnostic
        value on issue #85: when this sensor flips to unconfigured
        during a startup race, this list tells you exactly which of the
        10 required fields lost -- almost always one of the
        _SOLVER_NUMBER_ENTITY_KEYS still restoring via RestoreEntity,
        never entry.options-backed (those are durable across restart).
        """
        return [k for k in _SOLVER_REQUIRED_KEYS if self._resolve(k) in (None, "")]

    @property
    def native_value(self) -> str:
        """ "configured" only once every REQUIRED Solver field has a real
        value -- lets an external caller check this ONE field before
        attempting to build a plan, instead of discovering a missing
        field halfway through a solve with a confusing KeyError.

        Issue #85 instrumentation (2026-08-23): every REAL transition
        (configured <-> unconfigured) is logged at WARNING with the
        list of unresolved required keys, so a startup race
        (RestoreEntity still restoring number.nimbus_solver_* entities
        while this sensor is polled) is directly observable in the log,
        instead of only surfacing as a confusing "not configured yet"
        WARNING from solver_runtime.py with no attribution back to
        which sensor state actually triggered it. No behavioural
        change -- same string returned, same required-keys check, same
        cadence.
        """
        unresolved = self._unresolved_required_keys()
        new_state = "configured" if not unresolved else "unconfigured"

        # Log ONLY on a real transition, not every read -- native_value
        # is polled on every state-machine read (potentially many per
        # second under load), and a stable "configured" or a stable
        # "unconfigured" doesn't need per-read logging.
        if self._last_computed_state != new_state:
            if new_state == "unconfigured":
                _LOGGER.warning(
                    "nimbus_solver_config transitioned to unconfigured -- "
                    "unresolved required key(s): %s "
                    "(if this happened on HA startup, see nimbus issue #85 -- "
                    "these are almost always number.nimbus_solver_* entities "
                    "still restoring via RestoreEntity, and the sensor will "
                    "self-recover within a few seconds)",
                    unresolved,
                )
            else:
                _LOGGER.info(
                    "nimbus_solver_config transitioned to configured "
                    "(all %d required keys now resolved)",
                    len(_SOLVER_REQUIRED_KEYS),
                )
            self._last_computed_state = new_state

        return new_state

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {key: self._resolve(key) for key in _SOLVER_ALL_KEYS}
        # Issue #85 diagnostic (2026-08-23): also expose which required
        # keys are unresolved RIGHT NOW, so a caller reading this sensor
        # over REST can see exactly why native_value is "unconfigured"
        # without needing HA logs. On the happy path this is [], and on
        # the startup-race path it lists the specific fields still
        # settling -- see this class's own docstring for the full flap
        # story.
        attrs["unresolved_required_keys"] = self._unresolved_required_keys()
        return attrs


class NimbusTopologyConfigSensor(SensorEntity):
    """Bridges Power Source / PV String / Battery Tower subentries
    (config_entries.subentries) plus the hub-level switchboard fields
    (entry.options) out to a plain sensor -- same root reason
    NimbusSolverConfigSensor exists (2026-08-23): neither
    config_entries.subentries nor .options is exposed via HA's plain
    REST API, and topology-card-v4.js's own live-discovery mechanism
    (_discoverLoads()) only works for subentry types that publish a
    real forecast sensor to scan hass.states for -- these three
    genuinely don't (pure wiring metadata, no coordinator at all).

    One per hub, not per subentry -- unlike NimbusForecastSensor (one
    real entity per load, since each load genuinely has its own
    forecast to publish), there's nothing per-subentry worth its own
    HA entity here; the topology card needs the whole wiring picture
    in one read, not N separate small sensors it would have to
    reassemble itself.
    """

    _attr_has_entity_name = True
    _attr_name = "Topology Config"
    _attr_entity_category = None  # a real, actively-read data source, not a diagnostic

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_topology_config"
        self.entity_id = "sensor.nimbus_topology_config"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )

    @property
    def native_value(self) -> int:
        """Count of configured Power Source (inverter) subentries --
        the one number that answers "is there anything here at all"
        without a caller needing to inspect the attribute lists
        first."""
        return sum(
            1
            for s in self._entry.subentries.values()
            if s.subentry_type == SUBENTRY_TYPE_POWER_SOURCE
        )

    @property
    def extra_state_attributes(self) -> dict:
        power_sources, pv_strings, battery_towers = [], [], []
        for subentry in self._entry.subentries.values():
            if subentry.subentry_type == SUBENTRY_TYPE_POWER_SOURCE:
                power_sources.append(
                    {
                        "subentry_id": subentry.subentry_id,
                        **{k: subentry.data.get(k) for k in _POWER_SOURCE_KEYS},
                    }
                )
            elif subentry.subentry_type == SUBENTRY_TYPE_PV_STRING:
                pv_strings.append(
                    {
                        "subentry_id": subentry.subentry_id,
                        **{k: subentry.data.get(k) for k in _PV_STRING_KEYS},
                    }
                )
            elif subentry.subentry_type == SUBENTRY_TYPE_BATTERY_TOWER:
                # Unlike Power Source (power_source_name) and PV String
                # (pv_string_label), Battery Tower has no free-text name
                # field of its own in _BATTERY_TOWER_KEYS -- its only real
                # identity is subentry.title, auto-derived at creation
                # time from the SoC sensor's own friendly_name (see
                # flows/battery_tower_subentry.py's _derive_title, e.g.
                # "Battery Tower 2 SoC" -> "Battery Tower 2"). Found and
                # fixed 2026-08-23 after a real live migration: without
                # this, the frontend has nothing but subentry_id to label
                # a tower with, and fell back to a synthetic per-inverter
                # position index ("Tower 1"/"Tower 2") that silently
                # discarded the real physical tower number (e.g. towers
                # 2 & 4 both landing on the SAME inverter both displayed
                # as "Tower 1"/"Tower 2", losing which was actually which).
                battery_towers.append(
                    {
                        "subentry_id": subentry.subentry_id,
                        "title": subentry.title,
                        **{k: subentry.data.get(k) for k in _BATTERY_TOWER_KEYS},
                    }
                )
        return {
            "power_sources": power_sources,
            "pv_strings": pv_strings,
            "battery_towers": battery_towers,
            "switchboard": {k: self._entry.options.get(k) for k in _SWITCHBOARD_KEYS},
        }


class NimbusHealthReportSensor(SensorEntity):
    """Always-on "what's failing, what's flatlined, what's not running"
    health report (2026-08-25, direct ask: "at all times log any errors
    from nimbus - in full and extra detailed diagnostics file... i wanna
    know what fails and what flatlines and what is not running").

    One per hub, same "pure wiring/status metadata, no coordinator of
    its own" pattern as NimbusTopologyConfigSensor above -- this reads
    the health.py log buffer (always populated, independent of whether
    this entity itself has ever been polled) plus every forecastable
    subentry's own already-published coordinator data, so its own
    attributes are current on every read with no separate update-
    listener plumbing needed here either.

    native_value is a plain ERROR-level count from the last WARNING+
    entries this process has captured -- a real household can eyeball
    "0" and move on, or see a nonzero count and know to look at
    recent_errors before digging into any one subentry's own forecast
    sensor.
    """

    _attr_has_entity_name = True
    _attr_name = "Health Report"
    _attr_entity_category = None  # a real, actively-read data source, not a diagnostic

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_health_report"
        self.entity_id = "sensor.nimbus_health_report"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )

    @property
    def native_value(self) -> int:
        return health.count_recent_log_entries(min_level=logging.ERROR)

    @property
    def extra_state_attributes(self) -> dict:
        coordinators = self._entry.runtime_data or {}
        # "Not running" -- a subentry whose model has NEVER trained at
        # all (cold-start still in progress, or genuinely stuck: e.g.
        # the solar-sensor 1000x-unit bug found live on devhub, which
        # keeps a signal at exactly 0 training points forever, not just
        # slowly). Deliberately a plain boolean fact (trained_at is
        # None), not a guessed time-based threshold -- this project has
        # already been burned once by a hardcoded staleness threshold
        # elsewhere; "has it EVER produced a real model" needs no
        # threshold to be a genuine, unambiguous signal.
        never_trained = []
        subentry_status = []
        for subentry in self._entry.subentries.values():
            if subentry.subentry_type not in _FORECASTABLE_SUBENTRY_TYPES:
                continue
            coordinator = coordinators.get(subentry.subentry_id)
            data = (coordinator.data if coordinator else None) or {}
            status = {
                "subentry_id": subentry.subentry_id,
                "title": subentry.title,
                "mode": data.get("mode", "unscheduled"),
                "training_points": data.get("training_points", 0),
                "model_trained_at": data.get("trained_at"),
                "forecast_point_count": len(data.get("forecast", [])),
                # 2026-08-25, nimbus issue #187 (Mark Purcell, real-
                # install ask): a positive "am I watching, what's my
                # current ratio" signal, not just a silent WARNING that
                # only appears once something is already wrong.
                "residual_drift_status": data.get("residual_drift_status"),
            }
            subentry_status.append(status)
            if status["model_trained_at"] is None:
                never_trained.append(
                    {"subentry_id": subentry.subentry_id, "title": subentry.title}
                )
        return {
            "recent_errors": health.get_recent_log_entries(
                min_level=logging.ERROR, limit=20
            ),
            "recent_warnings": health.get_recent_log_entries(
                min_level=logging.WARNING, limit=20
            ),
            "never_trained": never_trained,
            "subentry_status": subentry_status,
            "generated_at": datetime.now(UTC).isoformat(),
        }


class NimbusSolverPriceResponseLatencySensor(SensorEntity):
    """Issue #294 (Mark Purcell, 2026-08-31): a first-class, continuously
    observable version of the "REST-poll two sensors and diff timestamps"
    measurement Mark had to do by hand to verify issue #232's
    `solve_on_price_change` fix. Without this, that latency is only ever
    knowable by manually correlating a price sensor's `last_changed`
    against `sensor.nimbus_solver_battery_forecast.last_updated` -- fine
    for a one-off verification, useless as an ongoing health signal or a
    regression-bisection tool after touching `_configure_price_watcher`,
    `solver_runtime.async_run_solve`, or the phase-locked cron scheduler.

    Updated ONLY on an event-driven (price_change) solve -- a cron- or
    startup-triggered solve leaves this sensor at its last event-driven
    value, per Mark's own explicit design in #294 ("the sensor sits at
    its last event-driven value"), since neither of those has a
    meaningful "time since the price actually changed" to report. See
    solver_runtime.record_solve_completed()'s own docstring for where
    that distinction is actually enforced -- this class just renders
    whatever it's handed.

    One per hub -- there is only ever one price-response pipeline per
    hub (issue #244's phase-locked cron + issue #256's optional price
    watcher both operate hub-wide, not per-subentry).

    `state_class: measurement` (Mark's own explicit "preferred" choice
    over a bare attribute on an existing sensor) is what makes this
    recordable into HA's long-term statistics, so a plain ApexCharts/
    history-graph card can chart it with zero Grafana/InfluxDB detour --
    see #294's own "Rejected for that reason" note on the attribute-only
    alternative.
    """

    _attr_has_entity_name = True
    _attr_name = "Solver Price Response Latency"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1
    _attr_entity_category = None  # a real, actively-read health signal

    # ~4h of real history at the observed ~5-min event-driven-solve
    # cadence (one price tick per NEM boundary) -- enough for a
    # meaningful p50/p90/max without the deque growing unbounded across
    # a long-running process. Mark's own proposal only ever said "rolling
    # stats (optional)" with no specific window; this is a deliberately
    # conservative, small choice, easy to widen later if a real need for
    # a longer lookback ever surfaces.
    _ROLLING_WINDOW = 50

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_solver_price_response_latency"
        self.entity_id = "sensor.nimbus_solver_price_response_latency"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )
        self._latency_s: float | None = None
        self._last_price_change_at: str | None = None
        self._last_solve_at: str | None = None
        self._trigger_source: str | None = None
        self._triggering_entity: str | None = None
        self._debounce_s: float | None = None
        self._recent: deque[float] = deque(maxlen=self._ROLLING_WINDOW)

    @property
    def native_value(self) -> float | None:
        return self._latency_s

    @property
    def extra_state_attributes(self) -> dict:
        recent = sorted(self._recent)
        p50 = p90 = latency_max = None
        if recent:
            p50 = recent[len(recent) // 2]
            p90 = recent[min(len(recent) - 1, int(len(recent) * 0.9))]
            latency_max = recent[-1]
        return {
            "last_price_change_at": self._last_price_change_at,
            "last_solve_at": self._last_solve_at,
            "trigger_source": self._trigger_source,
            "triggering_entity": self._triggering_entity,
            "debounce_s": self._debounce_s,
            "p50_recent": p50,
            "p90_recent": p90,
            "max_recent": latency_max,
            "sample_count": len(recent),
        }

    @callback
    def record(
        self,
        *,
        latency_s: float | None,
        trigger_source: str,
        triggering_entity: str | None,
        price_change_at: datetime | None,
        solve_at: datetime | None,
        debounce_s: float | None,
    ) -> None:
        """Called by solver_runtime.record_solve_completed() -- always
        from the event loop (see that function's own docstring for why
        this needs no thread-hop, unlike _NimbusSolverPushSensor's own
        update_from_solver(), which is reached via hass.add_job() from a
        genuinely different call context)."""
        self._latency_s = round(latency_s, 3) if latency_s is not None else None
        self._last_price_change_at = (
            price_change_at.isoformat() if price_change_at is not None else None
        )
        self._last_solve_at = solve_at.isoformat() if solve_at is not None else None
        self._trigger_source = trigger_source
        self._triggering_entity = triggering_entity
        self._debounce_s = debounce_s
        if latency_s is not None:
            self._recent.append(latency_s)
        # Same "entity not added to hass yet" guard as _NimbusSolverPush
        # Sensor.update_from_solver() -- see that method's own docstring
        # for why this is a real, expected race, not a defensive
        # afterthought.
        if self.hass is not None:
            self.async_write_ha_state()


class _NimbusSolverPushSensor(SensorEntity):
    """Shared base for the hub-level Solver-output sensors migrated in
    issue #55.

    Both subclasses are pushed into HA on each solve via
    solver_writer.ha_post_state() -> the dispatch table registered in
    async_setup_entry() below, which routes through update_from_solver()
    here instead of the raw states.async_set() fallback. This is the
    "PURE INTEGRATION seam" that solver_writer.py's module docstring
    already advertises -- see _ENTITY_UPDATE_HANDLERS over there for the
    full story.

    Held here (not folded into NimbusSolverConfigSensor) because Solver
    Config resolves its own value/attributes live from entry.options and
    a handful of number.*/switch.* helpers -- it is pull-based and does
    not need a push channel at all. The two entities below are pure
    solve outputs (the LP produces them fresh every cycle), so a push
    handler is the honest fit for them.

    _attr_device_class / _attr_state_class / _attr_native_unit_of_
    measurement are set at class-attribute time (subclass overrides
    below) so the Recorder's own "unit changed" repair (see issue #61)
    stops firing -- the unit now comes from the SensorEntity contract,
    not from a raw attribute the state machine happens to have been
    handed.
    """

    _attr_has_entity_name = True
    # Real, live-confirmed bug (2026-08-31): this class never set
    # should_poll, so it silently inherited Entity's own default of
    # True. This is a pure push entity -- update_from_solver() is the
    # ONLY thing that should ever change its state, and it defines no
    # update()/async_update() method at all (both are no-ops if HA's
    # own polling calls them). Confirmed live on the reference
    # household's NUC1: sensor.nimbus_solver_quality_report alternated
    # between a real value (28 attrs) and a bare "unknown" (3 class-
    # level attrs only -- friendly_name/state_class/unit_of_measurement,
    # exactly what a freshly-constructed instance's own
    # extra_state_attributes={} would show) on a clean, repeating
    # ~15-30s cadence, independent of and out of phase with the real
    # 1-minute solve cycle -- HA's own default entity-platform scan
    # interval (15s) matches this almost exactly. should_poll=False
    # stops HA from ever calling async_update_ha_state(force_refresh=
    # True) on this entity at all, closing off that path regardless of
    # the exact mechanism by which a poll cycle was producing (or
    # exposing) a blank state.
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    # Same finding as NimbusForecastSensor above: without this, HA's own
    # history-graph tooltips (and any UI computing a rolling average
    # across already-rounded points) show raw binary floating-point
    # noise ("0.152000000000000020" instead of "0.152").
    _attr_suggested_display_precision = 3
    # Recorder's own 16 KB per-attribute limit (see issue #59) -- the
    # 96h tiered-grid forecast list, at 15-minute resolution for the
    # first 24h and hourly after that, regularly exceeds that cap. The
    # forecast is a projection, not a historical fact worth keeping in
    # the long-term stats database, so unrecording it silences the real
    # bytes-truncated warning without losing anything a user actually
    # needs later.
    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{self._UNIQUE_ID_SUFFIX}"
        # Fixed entity_id (same technique/reasoning as NimbusSolverConfigSensor
        # and NimbusForecastSensor above) -- external readers depend on
        # the well-known name, and preserving the existing entity_id
        # here is the whole reason this migration is safe (long-term
        # stats and history keep flowing to the same entity_id).
        self.entity_id = f"sensor.{self._UNIQUE_ID_SUFFIX}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )
        # None until the first solve completes -- HA is fine with None
        # here (the entity shows as "unknown" until the first push, no
        # different from the raw state machine's behaviour on a fresh
        # HA restart before the first solve tick fires).
        self._state: float | None = None
        self._attrs: dict = {}
        # Silver `entity-unavailable` (2026-08-23): the sibling
        # NimbusForecastSensor already went through this exact fix
        # (2026-08-22) for a different data path -- these two push
        # sensors were the one place it was never applied, and they're
        # exactly the shape of bug that rule exists for: a plain
        # SensorEntity with no coordinator at all, no `available`
        # override, so a Solver that stops solving (highspy import
        # failure, an unhandled exception, the periodic timer somehow
        # getting cancelled) leaves this entity confidently reporting
        # its LAST successful plan forever, with nothing on screen
        # distinguishing that from a fresh, genuinely-current one.
        # `_last_updated` (monotonic, not wall-clock -- only elapsed
        # time matters here, and monotonic sidesteps any clock-skew/DST
        # edge case) is stamped on every real push; `available` below
        # compares against it.
        self._last_updated: float | None = None
        self._was_available: bool | None = None

    # Sized off __init__.py's own _SOLVER_INTERVAL (1 minute, the native
    # in-process runtime's real cadence -- this entity class is only
    # ever reached via that path, see the class docstring's own "only
    # while _NATIVE_HASS is set" note). 5x gives real headroom for one
    # or two slow/transient cycles (a genuinely busy host, a brief
    # coordinator hiccup) without false-flagging, while still catching
    # a truly stopped Solver well within a user-relevant window.
    _STALE_AFTER_SECONDS = 5 * 60

    @property
    def available(self) -> bool:
        """True before the first solve (a plain, honest "unknown" state
        -- distinct from "unavailable", which specifically means "this
        entity's data source is broken", not "hasn't started yet").
        False once a real staleness threshold has passed since the last
        successful push -- see _STALE_AFTER_SECONDS above."""
        if self._state is None:
            return True
        if self._last_updated is None:
            return True
        return (time.monotonic() - self._last_updated) < self._STALE_AFTER_SECONDS

    @property
    def native_value(self) -> float | None:
        return self._state

    @property
    def extra_state_attributes(self) -> dict:
        return self._attrs

    @callback
    def update_from_solver(self, state, attributes: dict) -> None:
        """Called on the event loop by solver_writer.ha_post_state()'s
        dispatch table (via hass.add_job) after each solve. Stores the
        fresh state and attributes and asks HA to publish them.

        @callback is not decoration, it's the actual fix for a real,
        live-breaking bug (issue #82, found by Mark Purcell's own real
        v0.73.0 install -- both push sensors stuck at `unknown` forever,
        every single solve tick crashing). Root cause: hass.add_job()
        inspects its target's own HassJob type to decide how to run it --
        a coroutine gets scheduled as a task, something marked @callback
        runs directly via loop.call_soon(), but a PLAIN, undecorated sync
        method (what this was, the whole time since PR #77 first shipped
        it) gets treated as potentially-blocking and dispatched to HA's
        executor THREAD POOL instead. Once there, this method's own
        async_write_ha_state() call below -- which genuinely requires the
        event loop -- raised RuntimeError on every single call, silently
        (HA logs "Future exception was never retrieved," not a crash
        anyone would necessarily notice without watching the log). This
        method has always been fast, non-blocking, pure state-machine
        work (matching async_write_ha_state()'s own @callback contract
        on Entity) -- @callback is the textbook-correct fix, not new
        behaviour.

        Silently drops the update if the entity has not been added to
        hass yet -- the very-first solve after a config-entry setup can
        in principle beat async_setup_entry to the punch by microseconds,
        and there is no honest way to publish a state through an entity
        HA doesn't know exists yet. The next solve tick (30 seconds
        later) will find the entity properly added and publish normally.
        The dispatch table is only ever queried while _NATIVE_HASS is
        set, so hass is guaranteed available here.
        """
        # #85 diagnostic (2026-08-23, not yet root-caused): Mark's own
        # empirical trace shows a real solve's rich write getting
        # clobbered to state=None/4-attrs ~2s later, on a v0.73.2
        # install where #83's fix is confirmed present and where the
        # recheck's own _LOGGER.info/.warning calls (unconditional on
        # every write path in that function -- see its own docstring)
        # NEVER fire. That rules out _async_recheck_availability as the
        # source under this code. If the clobber is a call to THIS
        # method with a genuinely empty/None payload, this line proves
        # it directly -- if it's something else entirely (a raw
        # states.async_set() bypassing this method, a second, distinct
        # entity instance never wired through this dispatch table at
        # all), this line's silence during the clobber is itself the
        # proof. DEBUG-level, additive only -- safe to ship without
        # knowing the answer yet.
        _LOGGER.debug(
            "Nimbus #85 trace: update_from_solver id=%x entity_id=%s state=%r "
            "attrs_keys=%s has_forecast=%s",
            id(self),
            self.entity_id,
            state,
            sorted(attributes.keys()) if attributes else attributes,
            "forecast" in attributes if attributes else False,
        )
        self._state = state
        self._attrs = attributes
        self._last_updated = time.monotonic()
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Real, easy-to-miss correctness point behind the staleness
        check above: HA's state machine is a cache -- `available`'s
        return value only ever reaches `hass.states` (and Recorder
        history) when something calls `async_write_ha_state()`.
        `update_from_solver()` is the only such call site, and it's only
        ever invoked BY a successful solve. If the Solver genuinely stops
        solving (the exact failure this whole fix targets), nothing
        would ever call `async_write_ha_state()` again, `available`
        would never get a chance to be re-evaluated, and the entity
        would keep showing its last cached state forever regardless of
        what the property itself would now return -- the original bug,
        completely un-fixed by the property alone. This periodic,
        self-driven re-check (same interval as _STALE_AFTER_SECONDS'
        own margin, /5, so a genuine staleness transition is caught
        within roughly one fifth of its own threshold, not up to a
        whole extra threshold late) exists purely to force that
        re-evaluation on a schedule independent of whether the Solver is
        still alive at all.

        NimbusForecastSensor's own sibling fix doesn't need this because
        it's a CoordinatorEntity -- the coordinator keeps re-polling on
        its own schedule REGARDLESS of success/failure, and each attempt
        already triggers a state re-write via _handle_coordinator_update.
        This class has no coordinator, so it needs its own equivalent.
        """
        # #85 diagnostic (2026-08-23): if this fires more than once for
        # the same entity_id with a DIFFERENT id=, that's direct,
        # conclusive proof of a duplicate live instance (Mark's own
        # leading hypothesis) -- a genuinely different HA-core lifecycle
        # bug (an entity added twice, an old one never torn down)
        # rather than anything in this method's own logic.
        _LOGGER.debug(
            "Nimbus #85 trace: async_added_to_hass id=%x entity_id=%s",
            id(self),
            self.entity_id,
        )
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_recheck_availability,
                timedelta(seconds=self._STALE_AFTER_SECONDS / 5),
            )
        )

    @callback
    def _async_recheck_availability(self, now) -> None:
        """Issue #83 fix (2026-08-23, Mark Purcell): this used to call
        async_write_ha_state() unconditionally on every tick. That's
        exactly what caused the flap he found in v0.73.1 -- a periodic
        re-publish of `native_value` (== self._state) racing against
        update_from_solver()'s own real pushes has no reason to fire at
        all unless something has actually changed, and doing so purely
        to "catch a staleness transition" is a correctness bug wearing a
        watchdog's clothes: this recheck's ONLY job is to force
        `available`'s property getter to be re-evaluated on a schedule
        independent of solver activity (see async_added_to_hass's own
        docstring) -- publishing state on a tick where nothing changed
        was never needed for that, and publishing it before the first
        real push (self._state is None) actively clobbers whatever a
        concurrent/stale entity instance may have already written.

        Now: exit early, no write at all, unless `available` has
        genuinely flipped since the last check (both the "log the
        transition" and the "actually publish it" concerns collapse into
        the same guard, which is also just a more honest read of what
        this method is for). native_value/extra_state_attributes are
        completely unaffected either way -- self._state/self._attrs are
        never touched here, only ever by update_from_solver().

        @callback for the same real reason as update_from_solver() above
        (see its own comment for the full issue #82 story) -- this
        method is registered directly as async_track_time_interval's own
        callback. Undecorated, HA's own job-type detection would have
        routed every single tick to the executor thread pool too.
        """
        now_available = self.available
        # #85 diagnostic (2026-08-23, Mark Purcell's own suggested trace
        # point, placed at the very top so it fires on EVERY tick, not
        # just a write): if two different id=s show up for the same
        # entity_id across consecutive ticks, that's direct proof of a
        # duplicate live instance. was_available/state/last_updated are
        # this INSTANCE's own values at tick time -- compare against the
        # id= to see whether a "stale" instance (state=None forever,
        # available always True per this property's own None-check) is
        # ticking alongside the real one.
        _LOGGER.debug(
            "Nimbus #85 trace: recheck tick id=%x entity_id=%s state=%r "
            "last_updated_age_s=%r was_avail=%r now_avail=%r",
            id(self),
            self.entity_id,
            self._state,
            None
            if self._last_updated is None
            else round(time.monotonic() - self._last_updated, 1),
            self._was_available,
            now_available,
        )
        if self._was_available is None:
            # First-ever tick after this instance was added -- record a
            # baseline, but there is nothing to "transition" from yet,
            # and no earlier publish of ours exists to correct.
            self._was_available = now_available
            return
        if now_available == self._was_available:
            return  # nothing changed -- exactly the flap this exists to avoid
        self._was_available = now_available
        if now_available:
            _LOGGER.info("Nimbus: %s is available again", self.entity_id)
        else:
            _LOGGER.warning(
                "Nimbus: %s has not received a fresh Solver plan in over "
                "%d seconds -- marking unavailable rather than continue "
                "showing a stale plan",
                self.entity_id,
                self._STALE_AFTER_SECONDS,
            )
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Symmetric partner to async_setup_entry's own
        register_entity_handler() call for this entity -- HA calls this
        automatically on both a plain unload AND on a config-entry
        reload (which is really an unload-then-setup pair), so pulling
        the handler out of the dispatch table here keeps the seam clean
        without needing a second hook in __init__.py's async_unload_
        entry(). Without this, a reload would leave the OLD entity's
        bound method sitting in the dispatch table until the NEW entity's
        async_setup_entry replaces it -- which register_entity_handler()
        is deliberately idempotent about, so no visible bug, but this is
        the honest cleanup regardless.

        Same deferred-import reasoning as async_setup_entry above --
        this method only runs under real HA, where solver_writer's own
        `from solver import ...` sys.path setup has long since happened.
        The periodic re-check timer registered above is cancelled
        automatically via async_on_remove -- no explicit unsub needed
        here.
        """
        # #85 diagnostic (2026-08-23): if a "stale instance" is ever
        # created, this line NOT appearing for its own id= (while
        # async_added_to_hass's matching id= trace DID appear) is the
        # direct proof it was never properly torn down -- the whole
        # premise behind Mark's own leading hypothesis for this bug.
        _LOGGER.debug(
            "Nimbus #85 trace: async_will_remove_from_hass id=%x entity_id=%s",
            id(self),
            self.entity_id,
        )
        from . import solver_writer

        solver_writer.unregister_entity_handler(self.entity_id)
        await super().async_will_remove_from_hass()


class NimbusSolverBatteryForecastSensor(_NimbusSolverPushSensor):
    """The Solver's own proposed battery power for the current period,
    plus the full 96h tiered-horizon plan (in `forecast` attribute) and
    every LP diagnostic worth exposing (status, total_cost, binding
    constraint, shadow prices, ...).

    Before issue #55 this was written directly into HA's state machine
    as a raw dict by solver_writer.ha_post_state("sensor.nimbus_solver_
    battery_forecast", ...) -- no device, no unique_id, no device_class,
    no unit_of_measurement outside the attrs blob. This class is what
    finally makes it a first-class entity attached to the Nimbus hub
    device (fixes #55 point 1, #59 forecast-attribute size, #61 unit-
    change repair, #62 missing unique_id all in one).
    """

    _UNIQUE_ID_SUFFIX = "nimbus_solver_battery_forecast"
    _attr_name = "Solver Battery Forecast"

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        super().__init__(entry, sw_version)
        # Populated by async_setup_entry immediately after construction;
        # empty list until then. update_from_solver() below guards on
        # this so a very-first solve tick that races setup can't crash
        # even if the fan-out hasn't been wired in yet.
        self._flattened_entities: list = []
        # Family B (v0.94.20 CHANGELOG deferred item): per-column
        # fan-out of forecast[0]. Same lifecycle contract as
        # _flattened_entities above; empty list until async_setup_entry
        # wires it, guarded on non-empty before dispatching.
        self._flattened_current_entities: list = []

    @callback
    def update_from_solver(self, state, attributes: dict) -> None:
        """Override to fan the same attribute dict out to every
        flattened child entity registered under this parent (see
        sensor_flattened.py). The parent's own publish happens FIRST
        via super() so that HA sees the canonical sensor.nimbus_solver_
        battery_forecast entity update before any fan-out -- any
        exception during fan-out therefore can't corrupt the parent's
        own state.
        """
        super().update_from_solver(state, attributes)
        if self._flattened_entities:
            sensor_flattened.dispatch_to_flattened(self._flattened_entities, attributes)
        if self._flattened_current_entities:
            # Family B: slice forecast[0] out of the same attributes and
            # fan out its ~24 columns to first-class scalars. Order after
            # Family A is deliberate -- if the parent ever grows a
            # top-level scalar that shadows a forecast[0] column, Family
            # A wins (its dispatch runs first) and this call is a no-op
            # for that shadowed name. Currently no such name collision
            # exists; the test suite proves it.
            sensor_flattened.dispatch_to_flattened_current(
                self._flattened_current_entities, attributes
            )


class NimbusHouseholdLoadTotalForecastSensor(_NimbusSolverPushSensor):
    """The per-solve reconciliation of all 18 per-circuit load forecasts
    into one whole-house total (native_value = summed_18_now_kw), plus
    the full 96h horizon (in `forecast` attribute) with the same tiered
    resolution as the battery-forecast sibling above.

    Before issue #55 this was written directly into HA's state machine
    as a raw dict by solver_writer.ha_post_state("sensor.nimbus_house
    hold_load_total_forecast", ...) -- see the sibling class above for
    the full "why migrate" story.

    Extra attributes carried alongside `forecast` include source_entities
    (the real list of 18 per-circuit forecast entities being summed) and
    failed_load_entities (any that were unavailable this run and safely
    defaulted to 0.0 kW, exposed so the topology card can cross-reference
    against its own per-load health dots -- see solver_writer.py's own
    comment above this write site for the full reasoning).
    """

    _UNIQUE_ID_SUFFIX = "nimbus_household_load_total_forecast"
    _attr_name = "Household Load Total Forecast"


class NimbusDispatchDryRunSensor(_NimbusSolverPushSensor):
    """Real-dispatch groundwork, phase 1 (2026-08-27/28) -- durable
    evidence trail for what solver_runtime.py's own dry-run observation
    (see that module's _log_dispatch_dry_run docstring) actually WAS,
    over time, via HA's own native recorder + long-term statistics.

    Before this class existed, the dry-run observation was a single
    _LOGGER.info() call and nothing else -- confirmed live on devhub
    2026-08-28 that this produced ZERO durable evidence: the switch was
    genuinely on, the Solver was genuinely solving on its normal
    schedule, but nimbus_load's effective logger level (WARNING by
    default on a fresh install) sits above INFO, so not one of those
    log lines had ever actually been emitted. A "dry run" with no
    reviewable history isn't testing anything -- this class is the
    fix: same _NimbusSolverPushSensor base as the two #55-migrated
    forecast sensors, so it gets a real unique_id, device link, and
    POWER/KILO_WATT/MEASUREMENT device/state class -- which means HA's
    own History graphs AND long-term statistics (kept indefinitely,
    survives the recorder's purge window) both work on this natively,
    with no bespoke rolling-JSON-buffer mechanism needed at all.

    Only ever updated while switch.nimbus_solver_dispatch_dry_run is
    on -- see _log_dispatch_dry_run's own guard in solver_runtime.py.
    Flipping the switch off simply stops new points from landing;
    already-recorded history is untouched either way, exactly like
    disabling any other sensor's own polling would be. The extra
    attributes (soc_pct, grid_import_kw, grid_export_kw, import_price,
    export_price) give full context for "what was true at this exact
    plan-decision" without needing to cross-reference the much larger
    battery_forecast sensor's own `forecast` array by timestamp.

    Still purely observational -- nothing about adding a recorded
    history changes _log_dispatch_dry_run's own contract that there is
    no hass.services.call() anywhere near this path.
    """

    _UNIQUE_ID_SUFFIX = "nimbus_solver_dispatch_dry_run"
    _attr_name = "Solver Dispatch (Dry Run)"


class NimbusMirrorTemperatureForecastSensor(_NimbusSolverPushSensor):
    """Real fix for issue #290 (Mark Purcell, 2026-08-30): before this
    class existed, solver_writer.publish_weather_forecast_mirrors()
    wrote sensor.nimbus_mirror_temperature_forecast as a raw
    ha_post_state() with no registered SensorEntity handler at all --
    every single push (every 5-minute solve cycle) fell through to the
    #85-instrumented raw states.async_set() fallback, logging a
    WARNING every time (~288/day) purely from this one entity having
    never been migrated, unlike every other push sensor in this file.

    Same _NimbusSolverPushSensor base as the #55-migrated sensors
    above, overriding only device_class/unit/precision for a real
    temperature value instead of the base class's POWER/kW defaults.
    No dedicated sub-device (unlike the Family-A parents) -- this is a
    small, purely cosmetic dashboard mirror (see that publish
    function's own docstring: "never referenced by the actual LP
    solve"), so it stays on the shared hub device like its
    battery/household-load siblings.
    """

    _UNIQUE_ID_SUFFIX = "nimbus_mirror_temperature_forecast"
    _attr_name = "Mirror Temperature Forecast"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1


class NimbusMirrorHumidityForecastSensor(_NimbusSolverPushSensor):
    """Same #290 fix as NimbusMirrorTemperatureForecastSensor above, for
    sensor.nimbus_mirror_humidity_forecast -- see that class's own
    docstring for the full "why this class exists at all" story.
    """

    _UNIQUE_ID_SUFFIX = "nimbus_mirror_humidity_forecast"
    _attr_name = "Mirror Humidity Forecast"
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 1


class NimbusSolverQualityReportSensor(_NimbusSolverPushSensor):
    """Family-A completion, parent 1 (2026-08-29, issue #55 follow-up):
    the daily EPR (Effective Performance Ratio) report -- primary
    user-facing signal for "how well is Nimbus actually doing against
    the theoretical oracle over the last full trading day".

    Before this class existed, solver_writer.publish_daily_quality_report()
    (~L3696 in that module) wrote sensor.nimbus_solver_quality_report as
    a raw ha_post_state() -- states.async_set() with a fixed unit_of_
    measurement of "%" pinned in the attrs dict. That works for a
    Lovelace card that only ever reads the current value, but fails the
    same three ways the pre-#55 forecast sensors failed: the Recorder's
    own "unit changed" repair fires every restart if any downstream
    template ever re-derives the sensor without carrying the unit
    forward (issue #61's exact shape); no unique_id means a reinstall
    creates a duplicate rather than re-attaching to the existing
    registry row (#62); and the 10 scalar attributes it carries alongside
    the state (theoretical_maximum_yield, value_captured, uplift_available,
    j_ref/j_ach/j_star, regret_dollars, tracking_fidelity, tracking_cost,
    plus the parent's own `epr` state) can't be graphed independently
    without a template sensor built for each -- the same "each attribute
    should be its own entity" argument that motivated Family-A in the
    first place.

    Native state is `epr` (%, 0-100, one decimal): the parent's
    canonical scalar. Sub-device DeviceInfo ((DOMAIN, entry.entry_id +
    "_quality") with via_device pointing at the hub) so this parent AND
    its 10 flattened children (see FLATTENED_ATTRS_QUALITY in
    sensor_flattened.py) group under a dedicated "Nimbus Quality"
    device page instead of piling onto the hub -- the hub already has
    40+ entities from Family-A alone, adding 11 more per parent would
    push it past 70 and genuinely hurt the UX.

    _flattened_entities is populated by async_setup_entry immediately
    after construction; empty list until then. update_from_solver()
    below guards on this so a very-first solve tick that races setup
    can't crash even if the fan-out hasn't been wired in yet -- same
    reasoning as NimbusSolverBatteryForecastSensor above.
    """

    _UNIQUE_ID_SUFFIX = "nimbus_solver_quality_report"
    _attr_name = "Solver Quality Report"
    # EPR is a percentage, not one of HA's device-classed measurement types
    # (POWER/ENERGY/etc.), so device_class stays None -- same as the four
    # existing "% ratio without a matching device_class" flattened children
    # (e.g. Solver P2P Match Fraction).
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 1
    # The base class defaults _unrecorded_attributes to frozenset({"forecast"})
    # because it was built around the two forecast parents -- this parent
    # doesn't publish a `forecast` array (its attributes are all scalar), so
    # the default is harmless but also honestly not what this class needs.
    # Cleared to the empty set so a future attribute rename in
    # publish_daily_quality_report() doesn't silently create a
    # never-recorded scalar just because it happens to be called `forecast`.
    _unrecorded_attributes = frozenset()

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        super().__init__(entry, sw_version)
        # Sub-device replacement -- see class docstring for why. The base
        # class already set self._attr_device_info to the hub identifier;
        # we override it here so this parent lives on its own device page.
        # via_device is the HA-native mechanism for "child device linked to
        # parent" -- the frontend renders "Part of Nimbus" on the sub-device
        # page and includes it in the hub's device tree.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_quality")},
            name="Nimbus Quality",
            manufacturer="Nimbus",
            model="Sub-device",
            sw_version=sw_version,
            via_device=(DOMAIN, entry.entry_id),
        )
        # Populated by async_setup_entry immediately after construction;
        # empty list until then. update_from_solver() below guards on
        # this so a very-first solve tick that races setup can't crash
        # even if the fan-out hasn't been wired in yet.
        self._flattened_entities: list = []

    @callback
    def update_from_solver(self, state, attributes: dict) -> None:
        """Override to fan the same attribute dict out to every flattened
        Quality child registered under this parent (see
        FLATTENED_ATTRS_QUALITY in sensor_flattened.py). The parent's own
        publish happens FIRST via super() so HA sees the canonical
        sensor.nimbus_solver_quality_report update before any fan-out --
        any exception during fan-out therefore can't corrupt the parent's
        own state (same pattern as NimbusSolverBatteryForecastSensor
        above).
        """
        super().update_from_solver(state, attributes)
        if self._flattened_entities:
            sensor_flattened.dispatch_to_flattened_quality(
                self._flattened_entities, attributes
            )


class NimbusEfficiencyBacktestSensor(_NimbusSolverPushSensor):
    """Family-A completion, parent 2 (2026-08-29, issue #55 follow-up):
    the weekly efficiency backtest -- diagnostic signal for "given last
    week's real prices and loads, what round-trip efficiency configuration
    would have minimised cost, and how far off is the currently configured
    value from that best candidate".

    Before this class existed, solver_writer.publish_efficiency_backtest_
    report() (~L3901 in that module) wrote sensor.nimbus_efficiency_
    backtest as a raw ha_post_state() -- same three shortcomings as the
    quality report above (see that class's docstring for the full "why
    migrate" story; #55, #59, #61, #62 all apply).

    Native state is `configured_efficiency_percent` (%, one decimal):
    the currently configured round-trip efficiency this backtest is
    scoring against. Sub-device "Nimbus Backtest" (via_device -> hub) so
    this parent AND its two flattened children (best_candidate_cost /
    worst_candidate_cost, both AUD) group under a dedicated device page.

    _attr_entity_category = DIAGNOSTIC because this is a retrospective
    validation of a config value, not a primary user-facing signal --
    a user should see it under the sub-device's Diagnostic section, not
    on the main sensor list. Same categorisation rule as every other
    DIAGNOSTIC flattened child (LP status, solve_seconds, shadow prices,
    etc.).
    """

    _UNIQUE_ID_SUFFIX = "nimbus_efficiency_backtest"
    _attr_name = "Efficiency Backtest"
    # Configured efficiency is a percentage -- no matching HA device_class.
    _attr_device_class = None
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Same "no forecast array on this parent" reasoning as
    # NimbusSolverQualityReportSensor above.
    _unrecorded_attributes = frozenset()

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        super().__init__(entry, sw_version)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_backtest")},
            name="Nimbus Backtest",
            manufacturer="Nimbus",
            model="Sub-device",
            sw_version=sw_version,
            via_device=(DOMAIN, entry.entry_id),
        )
        self._flattened_entities: list = []

    @callback
    def update_from_solver(self, state, attributes: dict) -> None:
        """Override to fan out to every flattened Backtest child. See
        NimbusSolverQualityReportSensor.update_from_solver() above for
        the full reasoning."""
        super().update_from_solver(state, attributes)
        if self._flattened_entities:
            sensor_flattened.dispatch_to_flattened_backtest(
                self._flattened_entities, attributes
            )


class NimbusCounterfactualSocSensor(_NimbusSolverPushSensor):
    """Family-A completion, parent 3 (2026-08-29, issue #55 follow-up):
    the daily "Nimbus-only" state-of-charge counterfactual -- diagnostic
    signal for "what would the battery SoC have looked like at end of
    day if Nimbus alone were driving it, ignoring any external override
    or manual dispatch".

    Before this class existed, solver_writer.publish_nimbus_only_soc_
    counterfactual() (~L4205 in that module) wrote sensor.nimbus_
    counterfactual_soc as a raw ha_post_state() -- see NimbusSolver-
    QualityReportSensor above for the full "why migrate" story (#55,
    #59, #61, #62).

    Native state is `real_soc_close_pct` (%, 0-100, one decimal): the
    real battery's end-of-day SoC, the primary datum this counterfactual
    is scored against. device_class = BATTERY so HA's own battery-tile
    card and any device-class-specific formatter picks it up natively --
    a real state-of-charge percentage IS a battery reading, and HA's
    BATTERY device_class handles the 0-100 percent contract exactly.

    Sub-device "Nimbus Counterfactual" (via_device -> hub) so this
    parent AND its three flattened children (real_soc_anchor_pct /
    nimbus_only_soc_close_pct / real_soc_close_pct, all BATTERY-classed
    percentages) group under a dedicated device page.

    _attr_entity_category = DIAGNOSTIC for the same reason as the
    backtest parent above -- a retrospective counterfactual is analysis
    context, not a live user-facing signal.
    """

    _UNIQUE_ID_SUFFIX = "nimbus_counterfactual_soc"
    _attr_name = "Counterfactual SoC"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"
    _attr_suggested_display_precision = 1
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset()

    def __init__(self, entry: NimbusConfigEntry, sw_version: str | None) -> None:
        super().__init__(entry, sw_version)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_counterfactual")},
            name="Nimbus Counterfactual",
            manufacturer="Nimbus",
            model="Sub-device",
            sw_version=sw_version,
            via_device=(DOMAIN, entry.entry_id),
        )
        self._flattened_entities: list = []

    @callback
    def update_from_solver(self, state, attributes: dict) -> None:
        """Override to fan out to every flattened Counterfactual child.
        See NimbusSolverQualityReportSensor.update_from_solver() above
        for the full reasoning."""
        super().update_from_solver(state, attributes)
        if self._flattened_entities:
            sensor_flattened.dispatch_to_flattened_counterfactual(
                self._flattened_entities, attributes
            )
