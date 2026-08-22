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

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
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
    CONF_SOLVER_FLAT_FEE_RATE,
    CONF_SOLVER_MAX_CHARGE_KW,
    CONF_SOLVER_MAX_DISCHARGE_KW,
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
    CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_RISK_AVERSION,
    CONF_SOLVER_P2P_BLOCK_3_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_3_START_HOUR,
    CONF_SOLVER_P2P_BONUS_PRICE,
    CONF_SOLVER_P2P_BONUS_VOLUME_KWH,
    CONF_SOLVER_SALVAGE_VALUE,
    CONF_SOLVER_DEGRADATION_COST_PER_KWH,
    DEFAULT_SOLVER_CHARGE_COST,
    DEFAULT_SOLVER_DEGRADATION_COST_PER_KWH,
    DEFAULT_SOLVER_DISCHARGE_COST,
    DEFAULT_SOLVER_EFFICIENCY_PERCENT,
    DEFAULT_SOLVER_FLAT_FEE_RATE,
    DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
    DEFAULT_SOLVER_NETWORK_FEE_RATE,
    DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
    DEFAULT_SOLVER_MAX_SOC_PERCENT,
    DEFAULT_SOLVER_MIN_SOC_PERCENT,
    DEFAULT_SOLVER_P2P_BLOCK_END_HOUR,
    DEFAULT_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    DEFAULT_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    DEFAULT_SOLVER_RISK_AVERSION,
    DEFAULT_SOLVER_P2P_BLOCK_RATE_KW,
    DEFAULT_SOLVER_P2P_BLOCK_START_HOUR,
    DEFAULT_SOLVER_P2P_BONUS_PRICE,
    DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH,
    DEFAULT_SOLVER_SALVAGE_VALUE,
    DEFAULT_SOLVER_SOH_PERCENT,
    DOMAIN,
)

# These entities are plain, locally-restored settings (RestoreNumber) --
# no hub/API to overload, so there's no reason to serialize updates.
PARALLEL_UPDATES = 0


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
    # entity-device-class (Gold): only set where HA's own DEVICE_CLASS_UNITS
    # table (homeassistant/components/number/const.py, checked live against
    # HA core's real current source 2026-08-22, not guessed) genuinely
    # accepts this field's exact unit string. Deliberately left None on
    # every $/kWh, "%", and "hour" field below -- none of those have a real
    # matching NumberDeviceClass (MONETARY expects a plain ISO4217 currency
    # unit, not a per-kWh rate; BATTERY means "% currently charged", not
    # "SoH"/"min or max SoC config"/"efficiency"; DURATION's own accepted
    # unit strings are HA's UnitOfTime enum values like "h", not our own
    # literal "hour" string) -- forcing one in would risk a rejected/
    # warned-about device_class at entity registration, not just be a
    # missed nicety.
    device_class: NumberDeviceClass | None = None


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
    _SolverNumberDescription(CONF_SOLVER_BATTERY_CAPACITY_KWH, "Battery Capacity", 0.1, 0.1, 2000, 0.1, "kWh", device_class=NumberDeviceClass.ENERGY_STORAGE),
    _SolverNumberDescription(CONF_SOLVER_BATTERY_SOH_PERCENT, "Battery State of Health", DEFAULT_SOLVER_SOH_PERCENT, 1, 100, 0.1, "%"),
    _SolverNumberDescription(CONF_SOLVER_BATTERY_MIN_SOC_PERCENT, "Battery Min SoC", DEFAULT_SOLVER_MIN_SOC_PERCENT, 0, 100, 0.1, "%"),
    _SolverNumberDescription(CONF_SOLVER_BATTERY_MAX_SOC_PERCENT, "Battery Max SoC", DEFAULT_SOLVER_MAX_SOC_PERCENT, 0, 100, 0.1, "%"),
    _SolverNumberDescription(CONF_SOLVER_MAX_CHARGE_KW, "Max Charge Power", 0.1, 0.1, 1000, 0.1, "kW", device_class=NumberDeviceClass.POWER),
    _SolverNumberDescription(CONF_SOLVER_MAX_DISCHARGE_KW, "Max Discharge Power", 0.1, 0.1, 1000, 0.1, "kW", device_class=NumberDeviceClass.POWER),
    _SolverNumberDescription(CONF_SOLVER_EFFICIENCY_PERCENT, "Round-Trip Efficiency", DEFAULT_SOLVER_EFFICIENCY_PERCENT, 50, 100, 0.1, "%"),
    _SolverNumberDescription(CONF_SOLVER_GRID_MAX_IMPORT_KW, "Grid Max Import", 0.1, 0.1, 1000, 0.1, "kW", device_class=NumberDeviceClass.POWER),
    _SolverNumberDescription(CONF_SOLVER_GRID_MAX_EXPORT_KW, "Grid Max Export", 0.1, 0.1, 1000, 0.1, "kW", device_class=NumberDeviceClass.POWER),
    _SolverNumberDescription(CONF_SOLVER_CHARGE_COST, "Charge Cost", DEFAULT_SOLVER_CHARGE_COST, 0, 10, 0.001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_DISCHARGE_COST, "Discharge Cost", DEFAULT_SOLVER_DISCHARGE_COST, 0, 10, 0.001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_SALVAGE_VALUE, "Salvage Value", DEFAULT_SOLVER_SALVAGE_VALUE, 0, 10, 0.001, "$/kWh"),
    # Real economic cycle-wear cost (2026-08-22, Track B2). See const.py's
    # own CONF_SOLVER_DEGRADATION_COST_PER_KWH comment for the full
    # "(replacement cost) / (2 * capacity * rated EFC)" derivation --
    # 0.0 (the default) is a genuine no-op.
    _SolverNumberDescription(CONF_SOLVER_DEGRADATION_COST_PER_KWH, "Battery Degradation Cost", DEFAULT_SOLVER_DEGRADATION_COST_PER_KWH, 0, 10, 0.001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BONUS_PRICE, "P2P Bonus Price", DEFAULT_SOLVER_P2P_BONUS_PRICE, 0, 10, 0.001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BONUS_VOLUME_KWH, "P2P Bonus Volume", DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH, 0, 10000, 0.1, "kWh", device_class=NumberDeviceClass.ENERGY),
    # P2P fixed-rate delivery blocks (2026-08-21) -- up to 3 independent
    # windows, each holding export at a constant, user-set rate rather than
    # letting the LP chase price within it. rate_kw=0 means "not
    # configured" (see const.py's own comment for the full reasoning).
    # end_hour uses 24 (not 23) as its max so a window can genuinely reach
    # midnight, matching how this household's own real window is expressed
    # (17-24, i.e. 5pm through the end of the day).
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_1_RATE_KW, "P2P Block 1 Rate", DEFAULT_SOLVER_P2P_BLOCK_RATE_KW, 0, 1000, 0.1, "kW", device_class=NumberDeviceClass.POWER),
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_1_START_HOUR, "P2P Block 1 Start Hour", DEFAULT_SOLVER_P2P_BLOCK_START_HOUR, 0, 23, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_1_END_HOUR, "P2P Block 1 End Hour", DEFAULT_SOLVER_P2P_BLOCK_END_HOUR, 0, 24, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_2_RATE_KW, "P2P Block 2 Rate", DEFAULT_SOLVER_P2P_BLOCK_RATE_KW, 0, 1000, 0.1, "kW", device_class=NumberDeviceClass.POWER),
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_2_START_HOUR, "P2P Block 2 Start Hour", DEFAULT_SOLVER_P2P_BLOCK_START_HOUR, 0, 23, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_2_END_HOUR, "P2P Block 2 End Hour", DEFAULT_SOLVER_P2P_BLOCK_END_HOUR, 0, 24, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_3_RATE_KW, "P2P Block 3 Rate", DEFAULT_SOLVER_P2P_BLOCK_RATE_KW, 0, 1000, 0.1, "kW", device_class=NumberDeviceClass.POWER),
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_3_START_HOUR, "P2P Block 3 Start Hour", DEFAULT_SOLVER_P2P_BLOCK_START_HOUR, 0, 23, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_P2P_BLOCK_3_END_HOUR, "P2P Block 3 End Hour", DEFAULT_SOLVER_P2P_BLOCK_END_HOUR, 0, 24, 1, "hour"),
    # Real per-kWh import FEES on top of the raw spot price -- network
    # TOU tariff + a flat always-on charge (certificates, etc.), same
    # shape as the P2P blocks above (2026-08-22, direct household
    # demand: "how do they configure fees column... I TOLD U NO
    # HARDCODED INPUTS - this has to work as user setting"). See const.py's
    # own comment on CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE for the full
    # "how a flat/2-tier/3-tier retailer each configures this" story.
    #
    # step=0.000001 (not 0.0001) on every rate field below: a real,
    # bill-verified Energex NTC 6900 tariff (2026-08-22, direct household
    # bill cross-check) needs a full 6 significant digits to enter exactly
    # (e.g. Peak 0.214863 $/kWh, Shoulder 0.066759, Off-peak 0.004774,
    # Certificates 0.008246) -- the old 4-decimal step silently rounded
    # every one of these to a slightly-wrong value (0.2149 vs 0.214863,
    # a real ~$0.006/day-scale error compounded across every peak-hour
    # kWh) with no visible warning. "we should have matching precision."
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE, "Network Fee Default Rate", DEFAULT_SOLVER_NETWORK_FEE_RATE, 0, 10, 0.000001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_1_RATE, "Network Fee Block 1 Rate", DEFAULT_SOLVER_NETWORK_FEE_RATE, 0, 10, 0.000001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_1_START_HOUR, "Network Fee Block 1 Start Hour", DEFAULT_SOLVER_NETWORK_FEE_START_HOUR, 0, 23, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_1_END_HOUR, "Network Fee Block 1 End Hour", DEFAULT_SOLVER_NETWORK_FEE_END_HOUR, 0, 24, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_2_RATE, "Network Fee Block 2 Rate", DEFAULT_SOLVER_NETWORK_FEE_RATE, 0, 10, 0.000001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_2_START_HOUR, "Network Fee Block 2 Start Hour", DEFAULT_SOLVER_NETWORK_FEE_START_HOUR, 0, 23, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_2_END_HOUR, "Network Fee Block 2 End Hour", DEFAULT_SOLVER_NETWORK_FEE_END_HOUR, 0, 24, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_3_RATE, "Network Fee Block 3 Rate", DEFAULT_SOLVER_NETWORK_FEE_RATE, 0, 10, 0.000001, "$/kWh"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_3_START_HOUR, "Network Fee Block 3 Start Hour", DEFAULT_SOLVER_NETWORK_FEE_START_HOUR, 0, 23, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_NETWORK_FEE_3_END_HOUR, "Network Fee Block 3 End Hour", DEFAULT_SOLVER_NETWORK_FEE_END_HOUR, 0, 24, 1, "hour"),
    _SolverNumberDescription(CONF_SOLVER_FLAT_FEE_RATE, "Flat Fee Rate (e.g. Certificates)", DEFAULT_SOLVER_FLAT_FEE_RATE, 0, 10, 0.000001, "$/kWh"),
    # Risk-aversion dials (2026-08-21) -- 0.0 = trust the point forecast
    # completely, 1.0 = fully hedge toward the pessimistic bound. Three
    # separate dials on purpose (household ask: "more flexibility the
    # better") -- Risk Aversion hedges solar/load forecast error; Import/
    # Export Price Risk Aversion each hedge their own side of the price
    # forecast independently (2026-08-21 split, per Mark Purcell's direct
    # feedback: a single shared price_risk_aversion scalar forces charge
    # and discharge hedging to move together even though they're
    # economically opposite decisions -- hedging "import might be more
    # expensive than forecast" should never also force hedging "export
    # might be worth less than forecast" by the same amount, and vice
    # versa). Trusting one kind of forecast doesn't mean trusting another.
    _SolverNumberDescription(CONF_SOLVER_RISK_AVERSION, "Risk Aversion", DEFAULT_SOLVER_RISK_AVERSION, 0, 1, 0.05, None),
    _SolverNumberDescription(CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION, "Import Price Risk Aversion", DEFAULT_SOLVER_IMPORT_PRICE_RISK_AVERSION, 0, 1, 0.05, None),
    _SolverNumberDescription(CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION, "Export Price Risk Aversion", DEFAULT_SOLVER_EXPORT_PRICE_RISK_AVERSION, 0, 1, 0.05, None),
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
        self._attr_device_class = desc.device_class
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
