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

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from .const import (
    CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
    CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
    CONF_SOLVER_BATTERY_SOH_PERCENT,
    CONF_SOLVER_CHARGE_COST,
    CONF_SOLVER_DEGRADATION_COST_PER_KWH,
    CONF_SOLVER_DISCHARGE_COST,
    CONF_SOLVER_EFFICIENCY_PERCENT,
    CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_FLAT_FEE_RATE,
    CONF_SOLVER_GRID_MAX_EXPORT_KW,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW,
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
    CONF_SOLVER_P2P_BLOCK_3_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_3_START_HOUR,
    CONF_SOLVER_P2P_BONUS_PRICE,
    CONF_SOLVER_P2P_BONUS_VOLUME_KWH,
    CONF_SOLVER_RISK_AVERSION,
    CONF_SOLVER_SALVAGE_VALUE,
    DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    DEFAULT_SOLVER_CHARGE_COST,
    DEFAULT_SOLVER_DEGRADATION_COST_PER_KWH,
    DEFAULT_SOLVER_DISCHARGE_COST,
    DEFAULT_SOLVER_EFFICIENCY_PERCENT,
    DEFAULT_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    DEFAULT_SOLVER_FLAT_FEE_RATE,
    DEFAULT_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    DEFAULT_SOLVER_INVERTER_SELF_CONSUMPTION_KW,
    DEFAULT_SOLVER_MAX_SOC_PERCENT,
    DEFAULT_SOLVER_MIN_SOC_PERCENT,
    DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
    DEFAULT_SOLVER_NETWORK_FEE_RATE,
    DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
    DEFAULT_SOLVER_P2P_BLOCK_END_HOUR,
    DEFAULT_SOLVER_P2P_BLOCK_RATE_KW,
    DEFAULT_SOLVER_P2P_BLOCK_START_HOUR,
    DEFAULT_SOLVER_P2P_BONUS_PRICE,
    DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH,
    DEFAULT_SOLVER_RISK_AVERSION,
    DEFAULT_SOLVER_SALVAGE_VALUE,
    DEFAULT_SOLVER_SOH_PERCENT,
    DOMAIN,
)

# These entities are plain, locally-restored settings (RestoreNumber) --
# no hub/API to overload, so there's no reason to serialize updates.
PARALLEL_UPDATES = 0

# nimbus issue: real, live incident 2026-09-02 (devhub restart) -- 14 of
# these 38 entities (grid limits, P2P block 1, all three network-fee
# tiers, min SoC, SoH, efficiency, charge cost) silently reset to their
# schema placeholder. Root cause: RestoreNumber's own restore-state has a
# genuine, still-not-fully-diagnosed HA-core startup timing race (the
# `restore_state` integration's cache isn't guaranteed warm by the time
# this platform's own async_added_to_hass() runs), and this module's own
# docstring above explains entry.options is DELIBERATELY never kept in
# sync with a dashboard edit (to avoid a full-hub reload on every value
# change) -- so a restore-state miss on any field that was ever only
# ever set from the dashboard (every P2P/network-fee/risk-aversion field,
# none of which are in the wizard) had ZERO real fallback and free-fell
# straight to _desc.default. This Store is a durable, independent third
# layer: written on every successful restore/seed AND on every dashboard
# edit, read as a fallback BEFORE ever reaching entry.options/default. A
# plain Store read is a direct JSON-file load with no comparable startup
# race, so it survives exactly the case RestoreNumber alone couldn't.
_STORAGE_VERSION = 1


@dataclass
class _SharedNumberStore:
    """One Store + one lock, shared by every NimbusSolverNumber instance
    for a given config entry -- all 38 fields live in the SAME small JSON
    file, so writes must be serialized (read-modify-write across
    independent entity instances would otherwise race if two fields are
    edited back-to-back quickly)."""

    store: Store[dict[str, Any]]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def async_read(self, key: str) -> float | None:
        entry = await self._async_read_entry(key)
        return entry[0] if entry is not None else None

    async def _async_read_entry(self, key: str) -> tuple[float, float] | None:
        """Returns (value, written_at) -- written_at is a time.time()-
        comparable epoch float, or None if this key has never been
        written (or the value on disk predates issue #342's fix, see
        async_write's own comment on the legacy bare-float shape)."""
        try:
            data = await self.store.async_load()
        except Exception:  # noqa: BLE001 -- a corrupt/unreadable store file
            # must never block this entity from falling through to its
            # own next fallback (entry.options / class default); it's a
            # durability BACKSTOP, not a required dependency.
            return None
        if not data or key not in data:
            return None
        entry = data[key]
        try:
            # nimbus issue #342 (Mark Purcell): legacy shape (a bare
            # float, written by every version of this Store before this
            # fix) has no timestamp to compare against a restore -- treat
            # it as maximally stale (0.0) rather than crash or silently
            # skip it, so a real, if untimed, value still beats a total
            # restore miss, but never wins a legitimate freshness compare
            # against ANY successful restore, however old.
            if isinstance(entry, dict):
                return float(entry["value"]), float(entry["written_at"])
            return float(entry), 0.0
        except (TypeError, ValueError, KeyError):
            return None

    async def async_write(self, key: str, value: float) -> None:
        async with self.lock:
            try:
                data = await self.store.async_load() or {}
            except Exception:  # noqa: BLE001 -- same reasoning as async_read
                data = {}
            # nimbus issue #342: was a bare `data[key] = value` -- no way
            # to tell which of a Store entry and a RestoreNumber restore
            # is actually newer, so async_added_to_hass() unconditionally
            # trusted whatever RestoreNumber returned and overwrote the
            # Store with it even when the Store already held a genuinely
            # NEWER, real user edit that RestoreNumber's own periodic
            # dump (STATE_DUMP_INTERVAL, 15 min) hadn't captured yet
            # before an unclean stop. Real failure scenario: a value set
            # at 10:00, last restore-state dump at 09:55, container killed
            # at 10:05 -- the correct 10:00 edit gets silently overwritten
            # by the stale 09:55 one, with entry.options never in sync
            # either (this module's own top docstring), making the edit
            # permanently unrecoverable. Recording when THIS layer's own
            # write happened lets async_added_to_hass() compare the two
            # sources honestly instead of blindly preferring one.
            data[key] = {"value": value, "written_at": time.time()}
            await self.store.async_save(data)


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
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_CAPACITY_KWH,
        "Battery Capacity",
        0.1,
        0.1,
        2000,
        0.1,
        "kWh",
        device_class=NumberDeviceClass.ENERGY_STORAGE,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_SOH_PERCENT,
        "Battery State of Health",
        DEFAULT_SOLVER_SOH_PERCENT,
        1,
        100,
        0.1,
        "%",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
        "Battery Min SoC",
        DEFAULT_SOLVER_MIN_SOC_PERCENT,
        0,
        100,
        0.1,
        "%",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
        "Battery Max SoC",
        DEFAULT_SOLVER_MAX_SOC_PERCENT,
        0,
        100,
        0.1,
        "%",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_MAX_CHARGE_KW,
        "Max Charge Power",
        0.1,
        0.1,
        1000,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_MAX_DISCHARGE_KW,
        "Max Discharge Power",
        0.1,
        0.1,
        1000,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_EFFICIENCY_PERCENT,
        "Round-Trip Efficiency",
        DEFAULT_SOLVER_EFFICIENCY_PERCENT,
        50,
        100,
        0.1,
        "%",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_GRID_MAX_IMPORT_KW,
        "Grid Max Import",
        0.1,
        0.1,
        1000,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_GRID_MAX_EXPORT_KW,
        "Grid Max Export",
        0.1,
        0.1,
        1000,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_CHARGE_COST,
        "Charge Cost",
        DEFAULT_SOLVER_CHARGE_COST,
        0,
        10,
        0.001,
        "$/kWh",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_DISCHARGE_COST,
        "Discharge Cost",
        DEFAULT_SOLVER_DISCHARGE_COST,
        0,
        10,
        0.001,
        "$/kWh",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_SALVAGE_VALUE,
        "Salvage Value",
        DEFAULT_SOLVER_SALVAGE_VALUE,
        0,
        10,
        0.001,
        "$/kWh",
    ),
    # Real economic cycle-wear cost (2026-08-22, Track B2). See const.py's
    # own CONF_SOLVER_DEGRADATION_COST_PER_KWH comment for the full
    # "(replacement cost) / (2 * capacity * rated EFC)" derivation --
    # 0.0 (the default) is a genuine no-op.
    _SolverNumberDescription(
        CONF_SOLVER_DEGRADATION_COST_PER_KWH,
        "Battery Degradation Cost",
        DEFAULT_SOLVER_DEGRADATION_COST_PER_KWH,
        0,
        10,
        0.001,
        "$/kWh",
    ),
    # Real portability bug found and fixed live (nimbus repo issue #100,
    # Mark Purcell). See const.py's own CONF_SOLVER_INVERTER_SELF_
    # CONSUMPTION_KW comment for the full "this used to be a hardcoded
    # 116KAT-HA-AI-specific constant, silently added to every OTHER
    # install's own load total" story. 0.0 (the default) is a genuine
    # no-op. min=0/max=5/step=0.001 -- a real inverter self-consumption
    # bias is a small correction (this project's own reference value is
    # 0.215 kW), not a hardware capacity number; the finer 0.001 step
    # (vs. the 0.1 step every kW hardware field above uses) is needed to
    # enter that reference value exactly.
    _SolverNumberDescription(
        CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW,
        "Inverter Self-Consumption",
        DEFAULT_SOLVER_INVERTER_SELF_CONSUMPTION_KW,
        0,
        5,
        0.001,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BONUS_PRICE,
        "P2P Bonus Price",
        DEFAULT_SOLVER_P2P_BONUS_PRICE,
        0,
        10,
        0.001,
        "$/kWh",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BONUS_VOLUME_KWH,
        "P2P Bonus Volume",
        DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH,
        0,
        10000,
        0.1,
        "kWh",
        device_class=NumberDeviceClass.ENERGY,
    ),
    # P2P fixed-rate delivery blocks (2026-08-21) -- up to 3 independent
    # windows, each holding export at a constant, user-set rate rather than
    # letting the LP chase price within it. rate_kw=0 means "not
    # configured" (see const.py's own comment for the full reasoning).
    # end_hour uses 24 (not 23) as its max so a window can genuinely reach
    # midnight, matching how this household's own real window is expressed
    # (17-24, i.e. 5pm through the end of the day).
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_1_RATE_KW,
        "P2P Block 1 Rate",
        DEFAULT_SOLVER_P2P_BLOCK_RATE_KW,
        0,
        1000,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_1_START_HOUR,
        "P2P Block 1 Start Hour",
        DEFAULT_SOLVER_P2P_BLOCK_START_HOUR,
        0,
        23,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_1_END_HOUR,
        "P2P Block 1 End Hour",
        DEFAULT_SOLVER_P2P_BLOCK_END_HOUR,
        0,
        24,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_2_RATE_KW,
        "P2P Block 2 Rate",
        DEFAULT_SOLVER_P2P_BLOCK_RATE_KW,
        0,
        1000,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_2_START_HOUR,
        "P2P Block 2 Start Hour",
        DEFAULT_SOLVER_P2P_BLOCK_START_HOUR,
        0,
        23,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_2_END_HOUR,
        "P2P Block 2 End Hour",
        DEFAULT_SOLVER_P2P_BLOCK_END_HOUR,
        0,
        24,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_3_RATE_KW,
        "P2P Block 3 Rate",
        DEFAULT_SOLVER_P2P_BLOCK_RATE_KW,
        0,
        1000,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_3_START_HOUR,
        "P2P Block 3 Start Hour",
        DEFAULT_SOLVER_P2P_BLOCK_START_HOUR,
        0,
        23,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_3_END_HOUR,
        "P2P Block 3 End Hour",
        DEFAULT_SOLVER_P2P_BLOCK_END_HOUR,
        0,
        24,
        1,
        "hour",
    ),
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
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE,
        "Network Fee Default Rate",
        DEFAULT_SOLVER_NETWORK_FEE_RATE,
        0,
        10,
        0.000001,
        "$/kWh",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_1_RATE,
        "Network Fee Block 1 Rate",
        DEFAULT_SOLVER_NETWORK_FEE_RATE,
        0,
        10,
        0.000001,
        "$/kWh",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_1_START_HOUR,
        "Network Fee Block 1 Start Hour",
        DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
        0,
        23,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_1_END_HOUR,
        "Network Fee Block 1 End Hour",
        DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
        0,
        24,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_2_RATE,
        "Network Fee Block 2 Rate",
        DEFAULT_SOLVER_NETWORK_FEE_RATE,
        0,
        10,
        0.000001,
        "$/kWh",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_2_START_HOUR,
        "Network Fee Block 2 Start Hour",
        DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
        0,
        23,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_2_END_HOUR,
        "Network Fee Block 2 End Hour",
        DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
        0,
        24,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_3_RATE,
        "Network Fee Block 3 Rate",
        DEFAULT_SOLVER_NETWORK_FEE_RATE,
        0,
        10,
        0.000001,
        "$/kWh",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_3_START_HOUR,
        "Network Fee Block 3 Start Hour",
        DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
        0,
        23,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_3_END_HOUR,
        "Network Fee Block 3 End Hour",
        DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
        0,
        24,
        1,
        "hour",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_FLAT_FEE_RATE,
        "Flat Fee Rate (e.g. Certificates)",
        DEFAULT_SOLVER_FLAT_FEE_RATE,
        0,
        10,
        0.000001,
        "$/kWh",
    ),
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
    _SolverNumberDescription(
        CONF_SOLVER_RISK_AVERSION,
        "Risk Aversion",
        DEFAULT_SOLVER_RISK_AVERSION,
        0,
        1,
        0.05,
        None,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION,
        "Import Price Risk Aversion",
        DEFAULT_SOLVER_IMPORT_PRICE_RISK_AVERSION,
        0,
        1,
        0.05,
        None,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION,
        "Export Price Risk Aversion",
        DEFAULT_SOLVER_EXPORT_PRICE_RISK_AVERSION,
        0,
        1,
        0.05,
        None,
    ),
    # Issue #232 follow-up: paired with switch.nimbus_solve_on_price_
    # change. Moved out of the wizard's solver_grid step for the same
    # "tune this from the dashboard, not from Settings" reason as every
    # entry above. Unit deliberately "s" (matching HA's UnitOfTime.SECONDS)
    # with no device_class -- see _SolverNumberDescription's own comment
    # for why NumberDeviceClass.DURATION is not a fit for this field's
    # unit string convention across the rest of this integration.
    _SolverNumberDescription(
        CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
        "Solve on Price Change Debounce",
        DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
        0.1,
        60,
        0.1,
        "s",
    ),
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
    shared_store = _SharedNumberStore(
        store=Store(hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_solver_numbers")
    )
    async_add_entities(
        [
            NimbusSolverNumber(entry, desc, sw_version, shared_store)
            for desc in _DESCRIPTIONS
        ]
    )


class NimbusSolverNumber(RestoreNumber, NumberEntity):
    """One live, dashboard-editable Solver setting. See this module's own
    docstring for why these are plain restored local state, never written
    back into entry.options."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    # Gold entity-category (2026-08-23): every one of these 38 entities IS
    # a Solver tuning knob, by this class's own definition -- unlike
    # entity-device-class (deliberately left unset per-field where HA has
    # no real matching device class, see _SolverNumberDescription's own
    # comment above), CONFIG is correct uniformly here, no per-field
    # judgment call needed. Groups these under "Configuration" in the HA
    # UI instead of cluttering the main entity list.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        desc: _SolverNumberDescription,
        sw_version: str | None,
        shared_store: _SharedNumberStore,
    ) -> None:
        self._entry = entry
        self._desc = desc
        self._shared_store = shared_store
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
            # nimbus issue #342 (Mark Purcell): this used to backfill the
            # Store from ANY successful restore unconditionally -- but
            # RestoreNumber's own restore-state dump (STATE_DUMP_INTERVAL,
            # every 15 min, plus on a clean shutdown) and this Store's own
            # write (synchronous, on every set) have genuinely different
            # cadences. A value set at 10:00, killed at 10:05 with the
            # last restore dump at 09:55, restores as the STALE 09:55
            # value -- and unconditionally backfilling then overwrote the
            # Store's own correct, newer value with it, permanently
            # (entry.options is deliberately never kept in sync either,
            # see this module's own top docstring). The restored STATE's
            # own last_updated (when HA itself last wrote that state, a
            # real proxy for "how current is this restored value") is now
            # compared against the Store's own written_at -- only backfill
            # when the restore is not older than what the Store already
            # holds, so a genuinely newer Store entry always survives a
            # stale restore.
            restored_state = await self.async_get_last_state()
            restored_at = (
                restored_state.last_updated.timestamp()
                if restored_state is not None
                else 0.0
            )
            stored_entry = await self._shared_store._async_read_entry(self._desc.key)
            if stored_entry is not None and stored_entry[1] > restored_at:
                self._attr_native_value = stored_entry[0]
                return
            self._attr_native_value = restored.native_value
            # Converges the Store toward full coverage of the TRUE live
            # state on every normal restart, so the next time RestoreNumber
            # itself loses the race, this fallback actually has real data
            # to serve instead of an empty file -- unchanged from before,
            # just no longer able to clobber a genuinely newer Store entry.
            await self._shared_store.async_write(self._desc.key, restored.native_value)
            return
        # RestoreNumber found nothing (a real, live, still-not-fully-
        # diagnosed HA-core startup timing race -- see the module-level
        # comment on _SharedNumberStore for the full 2026-09-02 incident
        # this exists to prevent). Try this integration's OWN durable
        # Store next, before ever falling through to a stale wizard-time
        # entry.options value or the hardcoded class default.
        stored_value = await self._shared_store.async_read(self._desc.key)
        if stored_value is not None:
            self._attr_native_value = stored_value
            return
        # No RestoreNumber state AND no Store entry -- this entity has
        # never existed before on this install. Seed from whatever's
        # already in entry.options (i.e. whatever the wizard was run
        # with), so rolling this platform out doesn't silently reset an
        # already-configured household's values back to a generic
        # default. A genuinely fresh install (never ran the wizard
        # either) falls through to _desc.default, set in __init__ above.
        seeded = self._entry.options.get(self._desc.key)
        if seeded is not None:
            try:
                self._attr_native_value = float(seeded)
            except (TypeError, ValueError):
                return
            await self._shared_store.async_write(
                self._desc.key, self._attr_native_value
            )

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        # Durable backstop -- see this module's own _SharedNumberStore
        # comment. Cheap, async, no hub reload (unlike writing into
        # entry.options, deliberately avoided per this module's own top
        # docstring).
        await self._shared_store.async_write(self._desc.key, value)
        # Same live-reconfigure hook NimbusSolverSwitch has for its own
        # CONF_SOLVE_ON_PRICE_CHANGE toggle -- editing the paired debounce
        # window from the dashboard must re-arm the listener with the new
        # window immediately, no hub reload. No-op for every other number
        # key so this stays a plain live edit with zero side-effects.
        if self._desc.key == CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S:
            from . import _configure_price_watcher

            _configure_price_watcher(self.hass, self._entry)
