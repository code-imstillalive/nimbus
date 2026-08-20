"""Number platform for Nimbus -- live, dashboard-editable Solver settings.

2026-08-20, direct household ask: the config-flow wizard (flows/hub_options.py)
is right for FIRST-TIME setup, but real friction for anything tuned day-to-day
(discharge cost, salvage value, grid limits) -- going through Settings ->
Devices & services -> Configure every single time doesn't match how this
household already works with number.battery_charge_cost, number.grid_
export_limit, and every other live-tunable economic/hardware value elsewhere
in this project. "now we just need the dashboard to allow changing of all of
these inputs... grid limits, efficiencies... cost charges... salvage... etc"

These 14 entities become the LIVING source of truth for every Solver setting
that's a plain number (not a sensor pointer -- battery_soc_sensor/import_
price_sensor/export_price_sensor/solar_forecast_sensor/load_forecast_sensor
stay wizard-only in hub_options.py, deliberately: those are one-time "which
entity is this" setup choices, not something anyone would slide on a
dashboard).

Deliberately NOT written back into entry.options via
hass.config_entries.async_update_entry() -- that call fires __init__.py's
own _async_update_listener, which reloads the ENTIRE hub (every one of
potentially 18+ load/signal coordinators) on every single value change. A
dashboard slider drag triggering a multi-second full-hub reload on every tick
would be genuinely bad UX for something meant to feel as light as any other
number.* entity. Instead: plain RestoreNumber local state (the same
mechanism input_number helpers use under the hood) -- instant, survives a
restart, zero hub-reload side effect. sensor.py's own NimbusSolverConfigSensor
reads these entities' live states directly (not entry.options) for exactly
this field set, so the writer script (nimbus_solver_forecast_writer.py, the
sibling 116KAT-HA-AI repo) needs no changes at all -- it already just reads
sensor.nimbus_solver_config's attributes.

Seeded ONCE, on first-ever creation only (no restored state found), from
whatever's already sitting in entry.options -- i.e. whatever the wizard was
already run with. This is what makes rolling this platform out onto an
already-configured household (like this one, which just finished the old
6-step wizard) NOT silently reset every value back to a generic default.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.loader import async_get_integration

from .const import (
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
    CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
    CONF_SOLVER_BATTERY_SOH_PERCENT,
    CONF_SOLVER_CHARGE_COST,
    CONF_SOLVER_DISCHARGE_COST,
    CONF_SOLVER_EFFICIENCY_PERCENT,
    CONF_SOLVER_GRID_MAX_EXPORT_KW,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_MAX_CHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_KW,
    CONF_SOLVER_P2P_BONUS_PRICE,
    CONF_SOLVER_P2P_BONUS_VOLUME_KWH,
    CONF_SOLVER_SALVAGE_VALUE,
    DEFAULT_SOLVER_CHARGE_COST,
    DEFAULT_SOLVER_DISCHARGE_COST,
    DEFAULT_SOLVER_EFFICIENCY_PERCENT,
    DEFAULT_SOLVER_MAX_SOC_PERCENT,
    DEFAULT_SOLVER_MIN_SOC_PERCENT,
    DEFAULT_SOLVER_P2P_BONUS_PRICE,
    DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH,
    DEFAULT_SOLVER_SALVAGE_VALUE,
    DEFAULT_SOLVER_SOH_PERCENT,
    DOMAIN,
)


@dataclass(frozen=True)
class _SolverNumberDescription:
    key: str  # CONF_SOLVER_* -- also this entity's own suffix and the
    # sensor.nimbus_solver_config attribute name it corresponds to.
    name: str
    default: float
    min_value: float
    max_value: float
    step: float
    unit: str | None


# Real bounds/units/defaults mirrored exactly from flows/hub_options.py's own
# _num() calls for these same 14 fields, minus the 5 entity-pointer fields
# that stay wizard-only (see this module's own docstring for why). The 5
# fields with no real universal default (capacity/max-charge/max-discharge/
# grid-limits -- genuinely household-specific hardware numbers, same as the
# wizard's own vol.Required with no default) use their own min bound as a
# clearly-a-placeholder value here -- only ever actually seen on a genuinely
# fresh install that's never run the wizard AND never had this platform
# create an entity before, since async_added_to_hass() below always prefers
# a real seeded/restored value first.
_DESCRIPTIONS: tuple[_SolverNumberDescription, ...] = (
    _SolverNumberDescription(CONF_SOLVER_BATTERY_CAPACITY_KWH, "Battery Capacity", 0.1, 0.1, 2000, 0.1, "kWh"),
    _SolverNumberDescription(CONF_SOLVER_BATTERY_SOH_PERCENT, "Battery State of Health", DEFAULT_SOLVER_SOH_PERCENT, 1, 100, 0.1, "%"),
    _SolverNumberDescription(CONF_SOLVER_BATTERY_MIN_SOC_PERCENT, "Battery Min SoC", DEFAULT_SOLVER_MIN_SOC_PERCENT, 0, 100, 0.1, "%"),
    _SolverNumberDescription(CONF_SOLVER_BATTERY_MAX_SOC_PERCENT, "Battery Max SoC", DEFAULT_SOLVER_MAX_SOC_PERCENT, 0, 100, 0.1, "%"),
    _SolverNumberDescription(CONF_SOLVER_MAX_CHARGE_KW, "Max Charge Power", 0.1, 0.1, 1000, 0.1, "kW"),
    _SolverNumberDescription(CONF_SOLVER_MAX_DISCHARGE_KW, "Max Discharge Power", 0.1, 0.1, 1000, 0.1, "kW"),
    _SolverNumberDescription(CONF_SOLVER_EFFICIENCY_PERCENT, "Round-Trip Efficiency", DEFAULT_SOLVER_EFFICIENCY_PERCENT, 50, 100, 0.1, "%"),
    _SolverNumberDescription(CONF_SOLVER_GRID_MAX_IMPORT_KW, "Grid Max Import", 0.1, 0.1, 1000, 0.1, "kW"),
    _SolverNumberDescription(CONF_SOLVER_GRID_MAX_EXPORT_KW, "Grid Max Export", 0.1, 0.1, 1000, 0.1, "kW"),
    _SolverNumberDescription(CONF_SOLVER_CHARGE_COST, "Charge Cost", DEFAULT_SOLVER_CHARGE_COST, 0, 10, 0.001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_DISCHARGE_COST, "Discharge Cost", DEFAULT_SOLVER_DISCHARGE_COST, 0, 10, 0.001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_SALVAGE_VALUE, "Salvage Value", DEFAULT_SOLVER_SALVAGE_VALUE, 0, 10, 0.001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BONUS_PRICE, "P2P Bonus Price", DEFAULT_SOLVER_P2P_BONUS_PRICE, 0, 10, 0.001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BONUS_VOLUME_KWH, "P2P Bonus Volume", DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH, 0, 10000, 0.1, "kWh"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # Same real, live version read as sensor.py's own NimbusForecastSensor/
    # NimbusSolverConfigSensor -- kept independent (not passed between
    # platform modules) since Platform.NUMBER/Platform.SENSOR forward-setup
    # order isn't something to depend on; HA merges device info across
    # entities sharing the same identifier regardless of which one loads
    # first.
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version) if integration.version else None
    async_add_entities([NimbusSolverNumber(entry, desc, sw_version) for desc in _DESCRIPTIONS])


class NimbusSolverNumber(RestoreNumber, NumberEntity):
    """One live, dashboard-editable Solver setting. See this module's own
    docstring for why these are plain restored local state, never written
    back into entry.options."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, desc: _SolverNumberDescription, sw_version: str | None) -> None:
        self._entry = entry
        self._desc = desc
        self._attr_unique_id = f"{entry.entry_id}_{desc.key}"
        # Fixed entity_id, same technique/reasoning as NimbusSolverConfigSensor's
        # own entity_id assignment in sensor.py -- one of these per hub per
        # field, a fixed, predictable name is correct here, not an HA-derived
        # one from device+entity-name combination.
        self.entity_id = f"number.nimbus_{desc.key}"
        self._attr_name = desc.name
        self._attr_native_min_value = desc.min_value
        self._attr_native_max_value = desc.max_value
        self._attr_native_step = desc.step
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )
        self._attr_native_value = desc.default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored = await self.async_get_last_number_data()
        if restored is not None and restored.native_value is not None:
            self._attr_native_value = restored.native_value
            return
        # No restored state -- this entity has never existed before on this
        # install. Seed from whatever's already in entry.options (i.e.
        # whatever the wizard was run with), so rolling this platform out
        # doesn't silently reset an already-configured household's values
        # back to a generic default. A genuinely fresh install (never ran
        # the wizard either) falls through to _desc.default, set in
        # __init__ above.
        seeded = self._entry.options.get(self._desc.key)
        if seeded is not None:
            try:
                self._attr_native_value = float(seeded)
            except (TypeError, ValueError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
