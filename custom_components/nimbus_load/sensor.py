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

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.loader import async_get_integration

from .const import (
    ATTR_FORECAST,
    ATTR_MODE,
    ATTR_MODEL_TRAINED_AT,
    ATTR_SUBENTRY_TYPE,
    ATTR_TRAINING_POINTS,
    ATTR_VALIDATION_MAE,
    ATTR_VALIDATION_MASE,
    CONF_LOAD_SENSOR,
    CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
    CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_BATTERY_SOH_PERCENT,
    CONF_SOLVER_CHARGE_COST,
    CONF_SOLVER_DISCHARGE_COST,
    CONF_SOLVER_EFFICIENCY_PERCENT,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_GRID_MAX_EXPORT_KW,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_LOAD_FORECAST_ENTITIES,
    CONF_SOLVER_LOAD_FORECAST_SENSOR,
    CONF_SOLVER_MAX_CHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_KW,
    CONF_SOLVER_FLAT_FEE_RATE,
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
    CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_RISK_AVERSION,
    CONF_SOLVER_SALVAGE_VALUE,
    CONF_SOLVER_DEGRADATION_COST_PER_KWH,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_2,
    CONF_SOLVER_SOLAR_FORECAST_SENSOR_3,
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
    CONF_BATTERY_TOWER_POWER_SOURCE,
    CONF_BATTERY_TOWER_SOC_SENSOR,
    CONF_BATTERY_TOWER_SOH_SENSOR,
    CONF_BATTERY_TOWER_TEMPERATURE_SENSOR,
    CONF_BATTERY_TOWER_VOLTAGE_SENSOR,
    CONF_POWER_SOURCE_BATTERY_SENSOR,
    CONF_POWER_SOURCE_DC_SENSOR,
    CONF_POWER_SOURCE_NAME,
    CONF_PV_STRING_ENTITY,
    CONF_PV_STRING_LABEL,
    CONF_PV_STRING_POWER_SOURCE,
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
_POWER_SOURCE_KEYS = (CONF_POWER_SOURCE_NAME, CONF_POWER_SOURCE_BATTERY_SENSOR, CONF_POWER_SOURCE_DC_SENSOR)
_PV_STRING_KEYS = (CONF_PV_STRING_ENTITY, CONF_PV_STRING_LABEL, CONF_PV_STRING_POWER_SOURCE)
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
    CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR,
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
# 2026-08-22: switch.py's own one live boolean toggle -- same
# "resolve from a live entity, not entry.options" mechanism as
# _SOLVER_NUMBER_ENTITY_KEYS above, just a different entity domain
# (switch.nimbus_{key}, "on"/"off" state -> bool) since HA has no
# combined number-or-boolean entity type. See switch.py's own module
# docstring for the full "why this exists" story.
_SOLVER_SWITCH_ENTITY_KEYS = (CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR,)


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
        async_add_entities(
            [NimbusForecastSensor(coordinator, subentry, sw_version)],
            config_subentry_id=subentry.subentry_id,
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

    def __init__(
        self, coordinator: NimbusCoordinator, subentry: ConfigSubentry, sw_version: str | None = None
    ) -> None:
        super().__init__(coordinator)
        # Deliberately NOT changed to a generic suffix for existing load
        # subentries -- an already-deployed entity's unique_id must never
        # change, or Home Assistant treats it as a brand new entity and
        # orphans the old one (losing its history/registry entry). Signal
        # subentries are new as of this same change, so they get their
        # own distinct, accurate suffix from day one instead.
        suffix = "_signal_forecast" if subentry.subentry_type == SUBENTRY_TYPE_SIGNAL else "_load_forecast"
        self._attr_unique_id = f"{subentry.subentry_id}{suffix}"
        # Exposed as a live attribute (2026-08-15) so anything downstream
        # (e.g. a dashboard chart script) can tell a load forecast apart
        # from a power-signal forecast generically -- by reading this
        # attribute at runtime, not by hardcoding entity names. Same
        # design principle already applied to ATTR_MODE.
        self._subentry_type = subentry.subentry_type
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
        self.entity_id = f"sensor.{object_id_from_source(subentry.data[CONF_LOAD_SENSOR])}"
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
        if source_state is None or source_state.state in ("unavailable", "unknown"):
            return False
        return True

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
                    self.entity_id, self._source_sensor,
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
            ATTR_SUBENTRY_TYPE: self._subentry_type,
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

    @property
    def native_value(self) -> str:
        """"configured" only once every REQUIRED Solver field has a real
        value -- lets an external caller check this ONE field before
        attempting to build a plan, instead of discovering a missing
        field halfway through a solve with a confusing KeyError."""
        if all(self._resolve(k) not in (None, "") for k in _SOLVER_REQUIRED_KEYS):
            return "configured"
        return "unconfigured"

    @property
    def extra_state_attributes(self) -> dict:
        return {key: self._resolve(key) for key in _SOLVER_ALL_KEYS}


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
            1 for s in self._entry.subentries.values() if s.subentry_type == SUBENTRY_TYPE_POWER_SOURCE
        )

    @property
    def extra_state_attributes(self) -> dict:
        power_sources, pv_strings, battery_towers = [], [], []
        for subentry in self._entry.subentries.values():
            if subentry.subentry_type == SUBENTRY_TYPE_POWER_SOURCE:
                power_sources.append(
                    {"subentry_id": subentry.subentry_id, **{k: subentry.data.get(k) for k in _POWER_SOURCE_KEYS}}
                )
            elif subentry.subentry_type == SUBENTRY_TYPE_PV_STRING:
                pv_strings.append(
                    {"subentry_id": subentry.subentry_id, **{k: subentry.data.get(k) for k in _PV_STRING_KEYS}}
                )
            elif subentry.subentry_type == SUBENTRY_TYPE_BATTERY_TOWER:
                battery_towers.append(
                    {"subentry_id": subentry.subentry_id, **{k: subentry.data.get(k) for k in _BATTERY_TOWER_KEYS}}
                )
        return {
            "power_sources": power_sources,
            "pv_strings": pv_strings,
            "battery_towers": battery_towers,
            "switchboard": {k: self._entry.options.get(k) for k in _SWITCHBOARD_KEYS},
        }
