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
reference household's own standalone script) needs no changes at all -- it already just reads
sensor.nimbus_solver_config's attributes.

Seeded ONCE, on first-ever creation only (no restored state found), from
whatever's already sitting in entry.options -- i.e. whatever the wizard was
already run with. This is what makes rolling this platform out onto an
already-configured household (like this one, which just finished the old
6-step wizard) NOT silently reset every value back to a generic default.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
    RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration

from . import sensor_flattened
from .const import (
    CONF_CONTROLLABLE_LOAD_KIND,
    CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
    CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
    CONF_DEFERRABLE_DEADLINE_HOUR,
    CONF_DEFERRABLE_EARLIEST_HOUR,
    CONF_DEFERRABLE_MAX_POWER_KW,
    CONF_DEFERRABLE_SHORTFALL_PRICE,
    CONF_DEFERRABLE_TARGET_KWH,
    CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    CONF_SOLVER_AEMO_P5MIN_DISAGREEMENT_THRESHOLD_DOLLARS,
    CONF_SOLVER_BATTERY_CAPACITY_KWH,
    CONF_SOLVER_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
    CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
    CONF_SOLVER_BATTERY_MIN_SOC_PERCENT,
    CONF_SOLVER_BATTERY_RESERVE_PERCENT,
    CONF_SOLVER_BATTERY_RESERVE_RELEASE_PRICE,
    CONF_SOLVER_BATTERY_SELL_MARGIN,
    CONF_SOLVER_BATTERY_SOH_PERCENT,
    CONF_SOLVER_CHARGE_BY_PERCENT,
    CONF_SOLVER_CHARGE_COST,
    CONF_SOLVER_CHARGE_POWER_AT_FULL_PERCENT,
    CONF_SOLVER_CHARGE_TAPER_START_PERCENT,
    CONF_SOLVER_DEGRADATION_COST_PER_KWH,
    CONF_SOLVER_DISCHARGE_COST,
    CONF_SOLVER_EFFICIENCY_PERCENT,
    CONF_SOLVER_EXPORT_FLOOR_PRICE,
    CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_FIXED_DAILY_CHARGE,
    CONF_SOLVER_FLAT_FEE_RATE,
    CONF_SOLVER_GRID_MAX_EXPORT_KW,
    CONF_SOLVER_GRID_MAX_IMPORT_KW,
    CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    CONF_SOLVER_INTRAPLAN_SMOOTHNESS_WEIGHT_KW,
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
    CONF_SOLVER_P2P_BLOCK_1_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_2_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_3_RATE_KW,
    CONF_SOLVER_P2P_BLOCK_LEAD_TIME_MINUTES,
    CONF_SOLVER_P2P_BONUS_PRICE,
    CONF_SOLVER_P2P_BONUS_VOLUME_KWH,
    CONF_SOLVER_P5MIN_FALSE_SPIKE_THRESHOLD_MWH,
    CONF_SOLVER_PERIOD0_HYSTERESIS,
    CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS,
    CONF_SOLVER_PRICE_SPIKE_DISCHARGE_KW,
    CONF_SOLVER_PRICE_SPIKE_THRESHOLD,
    CONF_SOLVER_PROXIMAL_WEIGHT_KW,
    CONF_SOLVER_RISK_AVERSION,
    CONF_SOLVER_SALVAGE_VALUE,
    CONF_SOLVER_SITE_IMPORT_LIMIT_KW,
    CONF_SOLVER_SOC_DISCREPANCY_MAX_THRESHOLD_PCT,
    CONF_SOLVER_SOC_DISCREPANCY_MEAN_THRESHOLD_PCT,
    CONF_SOLVER_SOLAR_RUNNING_LOW_CONFIRM_MINUTES,
    CONF_SOLVER_SOLAR_RUNNING_LOW_MIN_P50_KW,
    CONF_SOLVER_SOLAR_RUNNING_LOW_OFF_PERCENT,
    CONF_SOLVER_SOLAR_RUNNING_LOW_ON_PERCENT,
    CONF_SOLVER_STOCHASTIC_DRIVE_BUDGET_SECONDS,
    CONF_SOLVER_STOCHASTIC_SHADOW_INTERVAL_MINUTES,
    CONF_SOLVER_STOCHASTIC_STAGE1_MINUTES,
    CONF_SOLVER_STOCHASTIC_TAIL_WEIGHT,
    CONTROLLABLE_LOAD_KIND_DEFERRABLE,
    CONTROLLABLE_LOAD_KIND_SHEDDABLE,
    DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S,
    DEFAULT_SOLVER_AEMO_P5MIN_DISAGREEMENT_THRESHOLD_DOLLARS,
    DEFAULT_SOLVER_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
    DEFAULT_SOLVER_BATTERY_RESERVE_PERCENT,
    DEFAULT_SOLVER_BATTERY_RESERVE_RELEASE_PRICE,
    DEFAULT_SOLVER_BATTERY_SELL_MARGIN,
    DEFAULT_SOLVER_CHARGE_BY_PERCENT,
    DEFAULT_SOLVER_CHARGE_COST,
    DEFAULT_SOLVER_CHARGE_POWER_AT_FULL_PERCENT,
    DEFAULT_SOLVER_CHARGE_TAPER_START_PERCENT,
    DEFAULT_SOLVER_DEGRADATION_COST_PER_KWH,
    DEFAULT_SOLVER_DISCHARGE_COST,
    DEFAULT_SOLVER_EFFICIENCY_PERCENT,
    DEFAULT_SOLVER_EXPORT_FLOOR_PRICE,
    DEFAULT_SOLVER_EXPORT_PRICE_RISK_AVERSION,
    DEFAULT_SOLVER_FIXED_DAILY_CHARGE,
    DEFAULT_SOLVER_FLAT_FEE_RATE,
    DEFAULT_SOLVER_IMPORT_PRICE_RISK_AVERSION,
    DEFAULT_SOLVER_INTRAPLAN_SMOOTHNESS_WEIGHT_KW,
    DEFAULT_SOLVER_INVERTER_SELF_CONSUMPTION_KW,
    DEFAULT_SOLVER_MAX_SOC_PERCENT,
    DEFAULT_SOLVER_MIN_SOC_PERCENT,
    DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
    DEFAULT_SOLVER_NETWORK_FEE_RATE,
    DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
    DEFAULT_SOLVER_P2P_BLOCK_LEAD_TIME_MINUTES,
    DEFAULT_SOLVER_P2P_BLOCK_RATE_KW,
    DEFAULT_SOLVER_P2P_BONUS_PRICE,
    DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH,
    DEFAULT_SOLVER_P5MIN_FALSE_SPIKE_THRESHOLD_MWH,
    DEFAULT_SOLVER_PERIOD0_HYSTERESIS,
    DEFAULT_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS,
    DEFAULT_SOLVER_PRICE_SPIKE_DISCHARGE_KW,
    DEFAULT_SOLVER_PRICE_SPIKE_THRESHOLD,
    DEFAULT_SOLVER_PROXIMAL_WEIGHT_KW,
    DEFAULT_SOLVER_RISK_AVERSION,
    DEFAULT_SOLVER_SALVAGE_VALUE,
    DEFAULT_SOLVER_SITE_IMPORT_LIMIT_KW,
    DEFAULT_SOLVER_SOC_DISCREPANCY_MAX_THRESHOLD_PCT,
    DEFAULT_SOLVER_SOC_DISCREPANCY_MEAN_THRESHOLD_PCT,
    DEFAULT_SOLVER_SOH_PERCENT,
    DEFAULT_SOLVER_SOLAR_RUNNING_LOW_CONFIRM_MINUTES,
    DEFAULT_SOLVER_SOLAR_RUNNING_LOW_MIN_P50_KW,
    DEFAULT_SOLVER_SOLAR_RUNNING_LOW_OFF_PERCENT,
    DEFAULT_SOLVER_SOLAR_RUNNING_LOW_ON_PERCENT,
    DEFAULT_SOLVER_STOCHASTIC_DRIVE_BUDGET_SECONDS,
    DEFAULT_SOLVER_STOCHASTIC_SHADOW_INTERVAL_MINUTES,
    DEFAULT_SOLVER_STOCHASTIC_STAGE1_MINUTES,
    DEFAULT_SOLVER_STOCHASTIC_TAIL_WEIGHT,
    DOMAIN,
    SUBENTRY_TYPE_CONTROLLABLE_LOAD,
)

_LOGGER = logging.getLogger(__name__)

# One-time value changes applied on upgrade: key -> (marker, from, to). The
# marker is written once, so a household that sets the old value again keeps it.
# Self-consume after midnight 4 -> 0 (#1700): 4 was the reference household's
# value shipped as the default; 0 frees the hours after a midnight P2P block.
_ONE_TIME_VALUE_CHANGES: dict[str, tuple[str, float, float]] = {
    CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS: (
        "_migrated_post_window_self_consume_hours_4_to_0",
        4.0,
        0.0,
    ),
}

# These entities are plain, locally-restored settings (RestoreNumber) --
# no hub/API to overload, so there's no reason to serialize updates.
PARALLEL_UPDATES = 0

# Durable third layer for every Solver number (2026-09-02 incident: 14 values
# reset to placeholders). RestoreNumber can miss at startup and entry.options
# is never synced from the dashboard, so this Store is written on every
# restore, seed and edit, and read before falling back to options/default.
_STORAGE_VERSION = 1


@dataclass
class _SharedNumberStore:
    """One Store + one lock, shared by every NimbusSolverNumber instance
    for a given config entry -- every field lives in the SAME small JSON
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
            # #342: record when this layer wrote, so async_added_to_hass()
            # can keep a newer Store edit over a stale restore-state dump.
            data[key] = {"value": value, "written_at": time.time()}
            await self.store.async_save(data)


class _CurrencyUnitMixin:
    """Resolve a currency sentinel against HA's own configured currency.

    nimbus issue #1293 (Mark Purcell, IV&V #1289). #1253 removed the
    hardcoded currency labels from `sensor_flattened.py`'s 37 rows and
    its CHANGELOG entry then claimed "Zero hardcoded currency strings remain
    in the integration" -- which was false, because this file was never
    touched. Seventeen literal per-kWh and per-day unit strings, each with a
    dollar sign baked in, were assigned straight to
    `_attr_native_unit_of_measurement`, so a non-AUD household saw that sign
    on every Solver tuning knob: exactly the problem #1253 was opened to fix.

    The literals are deliberately not quoted in this docstring. #1293's own
    test greps this file's raw text for them, and it cannot tell a live unit
    string from prose describing one -- so spelling them out here would make
    the explanation fail the assertion it is explaining.

    A MIXIN rather than a property copied onto each class, because there are
    two unrelated entity classes here (`NimbusSolverNumber` and
    `NimbusControllableLoadNumber`) and a duplicated property is precisely
    how one of them silently stays unfixed -- which is the shape of this very
    finding one level up.

    Mirrors `_FlattenedAttributeSensor.native_unit_of_measurement` exactly,
    including the two deliberate non-obvious choices:

    * **a falsy currency returns None rather than a default.** Substituting
      one would re-introduce the hardcode this removes, just with a different
      string.
    * **before HA attaches `hass`, the sentinel is returned as-is.** It is
      deliberately not a plausible currency, so if it ever reached a UI it
      would be obviously wrong rather than quietly wrong.
    """

    @property
    def native_unit_of_measurement(self) -> str | None:
        unit = self._desc.unit
        if unit not in (_CURRENCY_PER_KWH, _CURRENCY_PER_DAY, _CURRENCY):
            return unit
        hass = getattr(self, "hass", None)
        if hass is None:
            return unit
        currency = hass.config.currency
        if not currency:
            return None
        if unit == _CURRENCY:
            return currency
        return f"{currency}/kWh" if unit == _CURRENCY_PER_KWH else f"{currency}/day"


#: Placeholders in the declarative tables below, resolved at read time by
#: `_CurrencyUnitMixin`. Deliberately implausible as real unit strings so a
#: leak is obvious on sight rather than mistaken for a currency (nimbus
#: issue #1293). Named to match sensor_flattened.py's own sentinels.
_CURRENCY_PER_KWH = "__nimbus_currency_per_kwh__"
_CURRENCY_PER_DAY = "__nimbus_currency_per_day__"
# A money amount, not a rate (nimbus #1654's direction change margin).
_CURRENCY = "__nimbus_currency__"
# A time of day, as an hour (5.5 = 05:30): no unit. "h"/"hour" read as a
# duration -- "Hold from 0 h" (household, 11 Oct 2026).
_CLOCK_HOUR = None


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
    # entity-device-class: set only where HA's DEVICE_CLASS_UNITS accepts the
    # exact unit. None on $/kWh, % and "hour" fields: no NumberDeviceClass
    # matches them, and a wrong one is rejected or warned at registration.
    device_class: NumberDeviceClass | None = None
    # nimbus issue #465: None (the default, every field but the 11 P2P
    # ones below) keeps this entity on the hub device, unchanged. "p2p"
    # moves it onto the "Nimbus P2P" sub-device instead -- see
    # NimbusSolverNumber.__init__'s own branch for how this field is
    # consumed, and sensor_flattened.resolve_hub_device_id()'s docstring
    # for the shared via_device/via_device_id resolution both this module
    # and sensor.py's own sub-device parents use.
    sub_device: str | None = None


# Bounds/units/defaults mirror flows/hub_options.py's _num() calls. The five
# household-specific hardware fields (capacity, charge/discharge, grid limits)
# use their min as a placeholder; a seeded or restored value always wins.
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
    # nimbus issue #1654: the soft reserve (see const.py). Off at 0 / 0.
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_RESERVE_PERCENT,
        "Battery Reserve",
        DEFAULT_SOLVER_BATTERY_RESERVE_PERCENT,
        0,
        100,
        0.5,
        "%",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_RESERVE_RELEASE_PRICE,
        "Battery Reserve Release Price",
        DEFAULT_SOLVER_BATTERY_RESERVE_RELEASE_PRICE,
        0,
        20,
        0.01,
        _CURRENCY_PER_KWH,
    ),
    # Household, 20 Oct 2026: "Don't sell below" (const.py).
    _SolverNumberDescription(
        CONF_SOLVER_EXPORT_FLOOR_PRICE,
        "Don't Sell Below",
        DEFAULT_SOLVER_EXPORT_FLOOR_PRICE,
        0,
        20,
        0.01,
        _CURRENCY_PER_KWH,
    ),
    # nimbus #2228: the cost on battery energy sold to the grid (const.py).
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_SELL_MARGIN,
        "Battery Sell Margin",
        DEFAULT_SOLVER_BATTERY_SELL_MARGIN,
        0,
        1,
        0.005,
        _CURRENCY_PER_KWH,
    ),
    # nimbus issue #1654, option A: period-0 direction hysteresis (const.py).
    _SolverNumberDescription(
        CONF_SOLVER_PERIOD0_HYSTERESIS,
        "Direction Change Margin",
        DEFAULT_SOLVER_PERIOD0_HYSTERESIS,
        0,
        1,
        0.005,
        _CURRENCY,
    ),
    # Household, 12 Oct 2026: the charge taper near full (see const.py).
    _SolverNumberDescription(
        CONF_SOLVER_CHARGE_TAPER_START_PERCENT,
        "Charge Taper Start",
        DEFAULT_SOLVER_CHARGE_TAPER_START_PERCENT,
        50,
        100,
        0.5,
        "%",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_CHARGE_POWER_AT_FULL_PERCENT,
        "Charge Power at Full",
        DEFAULT_SOLVER_CHARGE_POWER_AT_FULL_PERCENT,
        0,
        100,
        1,
        "%",
    ),
    # nimbus #1654: the reserve's window is a pair of TIME entities
    # (time.py), shown as a clock time, not numbers here.
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_MAX_SOC_PERCENT,
        "Battery Max SoC",
        DEFAULT_SOLVER_MAX_SOC_PERCENT,
        0,
        100,
        0.1,
        "%",
    ),
    # Household, 14 Oct 2026: the SoC the battery must hold by "Charge By"
    # (time.py) every day. 0 = off. See const.py.
    _SolverNumberDescription(
        CONF_SOLVER_CHARGE_BY_PERCENT,
        "Charge To",
        DEFAULT_SOLVER_CHARGE_BY_PERCENT,
        0,
        100,
        1,
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
    # nimbus #2257: compared with the live meter at dispatch, not planned
    # against. 0 = off.
    _SolverNumberDescription(
        CONF_SOLVER_SITE_IMPORT_LIMIT_KW,
        "Site Import Limit",
        DEFAULT_SOLVER_SITE_IMPORT_LIMIT_KW,
        0,
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
        _CURRENCY_PER_KWH,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_DISCHARGE_COST,
        "Discharge Cost",
        DEFAULT_SOLVER_DISCHARGE_COST,
        0,
        10,
        0.001,
        _CURRENCY_PER_KWH,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_SALVAGE_VALUE,
        "Salvage Value",
        DEFAULT_SOLVER_SALVAGE_VALUE,
        0,
        10,
        0.001,
        _CURRENCY_PER_KWH,
    ),
    # nimbus issue #348 (Mark Purcell): this used to be a hardcoded module
    # constant in solver_writer.py, applied to every install with no way
    # to change it. Its default was 1.95, the reference household's own
    # charge; since 9 Oct 2026 it is 0 ("not set") for a new install, and an
    # existing install keeps its stored value.
    _SolverNumberDescription(
        CONF_SOLVER_FIXED_DAILY_CHARGE,
        "Fixed Daily Charge",
        DEFAULT_SOLVER_FIXED_DAILY_CHARGE,
        0,
        50,
        0.01,
        _CURRENCY_PER_DAY,
    ),
    # Same story as Fixed Daily Charge above -- was SELF_CONSUME_HOURS_
    # AFTER_MIDNIGHT_CLOSE, a hardcoded solver_writer.py constant. Only
    # takes effect for a P2P block whose own end_hour reaches 24 (runs
    # through midnight) -- see fetch_p2p_fixed_export_kw()'s own
    # docstring for the full "why a hard pin, not just a soft nudge"
    # reasoning. Default 0 (no restriction; household 19 Oct 2026, promised
    # on #1700): an install that wants the hard pin sets its own hours.
    _SolverNumberDescription(
        CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS,
        "Post-Window Self-Consume Hours",
        DEFAULT_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS,
        0,
        24,
        1,
        "h",
        device_class=NumberDeviceClass.DURATION,
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
        _CURRENCY_PER_KWH,
    ),
    # #100: inverter self-consumption bias, 0.0 = no-op (was a hardcoded
    # household constant; see const.py). step 0.001 so 0.215 kW enters exactly.
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
        _CURRENCY_PER_KWH,
        sub_device="p2p",
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
        sub_device="p2p",
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
        sub_device="p2p",
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
        sub_device="p2p",
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
        sub_device="p2p",
    ),
    # #565: one lead time shared by all P2P blocks, moving each block's start
    # earlier to absorb dispatch lag. 0 = no-op. See fetch_p2p_fixed_export_kw().
    _SolverNumberDescription(
        CONF_SOLVER_P2P_BLOCK_LEAD_TIME_MINUTES,
        "P2P Block Lead Time",
        DEFAULT_SOLVER_P2P_BLOCK_LEAD_TIME_MINUTES,
        0,
        59,
        1,
        "min",
        sub_device="p2p",
    ),
    # Per-kWh import fees on top of spot: network TOU tiers plus a flat charge,
    # always a user setting (see const.py CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE).
    # step 0.000001: real tariffs need six decimals (e.g. 0.214863 $/kWh).
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE,
        "Network Fee Default Rate",
        DEFAULT_SOLVER_NETWORK_FEE_RATE,
        0,
        10,
        0.000001,
        _CURRENCY_PER_KWH,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_1_RATE,
        "Network Fee Block 1 Rate",
        DEFAULT_SOLVER_NETWORK_FEE_RATE,
        0,
        10,
        0.000001,
        _CURRENCY_PER_KWH,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_1_START_HOUR,
        "Network Fee Block 1 Start Hour",
        DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
        0,
        23,
        1,
        _CLOCK_HOUR,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_1_END_HOUR,
        "Network Fee Block 1 End Hour",
        DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
        0,
        24,
        1,
        _CLOCK_HOUR,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_2_RATE,
        "Network Fee Block 2 Rate",
        DEFAULT_SOLVER_NETWORK_FEE_RATE,
        0,
        10,
        0.000001,
        _CURRENCY_PER_KWH,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_2_START_HOUR,
        "Network Fee Block 2 Start Hour",
        DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
        0,
        23,
        1,
        _CLOCK_HOUR,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_2_END_HOUR,
        "Network Fee Block 2 End Hour",
        DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
        0,
        24,
        1,
        _CLOCK_HOUR,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_3_RATE,
        "Network Fee Block 3 Rate",
        DEFAULT_SOLVER_NETWORK_FEE_RATE,
        0,
        10,
        0.000001,
        _CURRENCY_PER_KWH,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_3_START_HOUR,
        "Network Fee Block 3 Start Hour",
        DEFAULT_SOLVER_NETWORK_FEE_START_HOUR,
        0,
        23,
        1,
        _CLOCK_HOUR,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_NETWORK_FEE_3_END_HOUR,
        "Network Fee Block 3 End Hour",
        DEFAULT_SOLVER_NETWORK_FEE_END_HOUR,
        0,
        24,
        1,
        _CLOCK_HOUR,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_FLAT_FEE_RATE,
        "Flat Fee Rate (e.g. Certificates)",
        DEFAULT_SOLVER_FLAT_FEE_RATE,
        0,
        10,
        0.000001,
        _CURRENCY_PER_KWH,
    ),
    # Risk-aversion dials, 0 = trust the forecast, 1 = fully hedge. Separate
    # import and export price dials (Mark): hedging the two sides are opposite
    # decisions and must not move together.
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
    # #538 (Mark): second SoC reliability test, max/mean disagreement against
    # the real SoC sensor in points, tunable per household.
    _SolverNumberDescription(
        CONF_SOLVER_SOC_DISCREPANCY_MAX_THRESHOLD_PCT,
        "SoC Discrepancy Max Threshold",
        DEFAULT_SOLVER_SOC_DISCREPANCY_MAX_THRESHOLD_PCT,
        0,
        100,
        0.5,
        "%",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_SOC_DISCREPANCY_MEAN_THRESHOLD_PCT,
        "SoC Discrepancy Mean Threshold",
        DEFAULT_SOLVER_SOC_DISCREPANCY_MEAN_THRESHOLD_PCT,
        0,
        100,
        0.5,
        "%",
    ),
    # nimbus issue #452 (Mark Purcell): threshold on the residual between
    # the current interval's real retail price and what the household's
    # own normal retail-markup pattern would predict from AEMO's live
    # P5MIN wholesale price -- see check_aemo_p5min_disagreement()'s own
    # docstring in solver_writer.py for the full computation.
    _SolverNumberDescription(
        CONF_SOLVER_AEMO_P5MIN_DISAGREEMENT_THRESHOLD_DOLLARS,
        "AEMO P5MIN Disagreement Threshold",
        DEFAULT_SOLVER_AEMO_P5MIN_DISAGREEMENT_THRESHOLD_DOLLARS,
        0,
        2,
        0.01,
        _CURRENCY_PER_KWH,
    ),
    # nimbus #1778: the P5MIN false-spike guard's threshold. AUD/MWh, not
    # the install's currency: it is compared with AEMO's own prices (#1293's
    # rule for genuinely single-currency market constants).
    _SolverNumberDescription(
        CONF_SOLVER_P5MIN_FALSE_SPIKE_THRESHOLD_MWH,
        "P5MIN False Spike Threshold",
        DEFAULT_SOLVER_P5MIN_FALSE_SPIKE_THRESHOLD_MWH,
        0,  # 0 = off (#1779 review)
        23200,
        100,
        "AUD/MWh",
    ),
    # nimbus #1797: the stochastic solve's tuning (const.py has what each does).
    _SolverNumberDescription(
        CONF_SOLVER_STOCHASTIC_STAGE1_MINUTES,
        "Stochastic Commit Window",
        DEFAULT_SOLVER_STOCHASTIC_STAGE1_MINUTES,
        5,
        120,
        5,
        "min",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_STOCHASTIC_TAIL_WEIGHT,
        "Stochastic Low High Day Weight",
        DEFAULT_SOLVER_STOCHASTIC_TAIL_WEIGHT,
        0.05,
        0.45,
        0.05,
        None,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_STOCHASTIC_DRIVE_BUDGET_SECONDS,
        "Stochastic Drive Time Limit",
        DEFAULT_SOLVER_STOCHASTIC_DRIVE_BUDGET_SECONDS,
        2,
        60,
        1,
        "s",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_STOCHASTIC_SHADOW_INTERVAL_MINUTES,
        "Stochastic Check Every",
        DEFAULT_SOLVER_STOCHASTIC_SHADOW_INTERVAL_MINUTES,
        1,
        60,
        1,
        "min",
    ),
    # Smoothness weight, exposed after 2026-09-08 mid-band jumps (LP
    # degeneracy). Kept below the smallest real price step, so it only breaks
    # ties between equal-cost schedules and never overrides a price signal.
    _SolverNumberDescription(
        CONF_SOLVER_INTRAPLAN_SMOOTHNESS_WEIGHT_KW,
        "Intra-Plan Smoothness Weight",
        DEFAULT_SOLVER_INTRAPLAN_SMOOTHNESS_WEIGHT_KW,
        0,
        0.1,
        0.001,
        _CURRENCY_PER_KWH,
    ),
    # Same real gap, same fix, for the sibling mechanism (network.py's
    # own _add_proximal_penalty) -- how strongly each NEW solve is pulled
    # toward the PREVIOUS solve's own already-committed plan, instead of
    # jumping to a different, equally-cheap vertex every solve cycle just
    # because the LP re-solved from scratch. Also always silently used
    # network.py's own hardcoded DEFAULT_PROXIMAL_WEIGHT_KW before this.
    _SolverNumberDescription(
        CONF_SOLVER_PROXIMAL_WEIGHT_KW,
        "Cross-Solve Proximal Weight",
        DEFAULT_SOLVER_PROXIMAL_WEIGHT_KW,
        0,
        0.1,
        0.001,
        _CURRENCY_PER_KWH,
    ),
    # #692: earliness tie-break for battery charging (as #613 gave loads), so a
    # low battery charges at the first good price. Default matches the
    # smoothness/proximal weights; the load-side size caused jagged charging.
    _SolverNumberDescription(
        CONF_SOLVER_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
        "Battery Charge Earliness Weight",
        DEFAULT_SOLVER_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
        0,
        0.1,
        0.001,
        _CURRENCY_PER_KWH,
    ),
    # nimbus issue #567: the PRIMARY price-spike trigger -- compared
    # directly against the household's own already-configured live
    # import price, no external alert integration required. 0 (default)
    # is off -- see const.py's own comment on CONF_SOLVER_PRICE_SPIKE_
    # THRESHOLD for the full mechanism (an optional binary_sensor alert
    # entity, wizard-only, can trigger alongside/instead of this).
    _SolverNumberDescription(
        CONF_SOLVER_PRICE_SPIKE_THRESHOLD,
        "Price Spike Threshold",
        DEFAULT_SOLVER_PRICE_SPIKE_THRESHOLD,
        0,
        20,
        0.01,
        _CURRENCY_PER_KWH,
        # device_class left at its None default -- same "no real
        # matching NumberDeviceClass for a $/kWh rate" reasoning as
        # every other $/kWh field in this table (see _SolverNumber
        # Description's own device_class docstring above).
    ),
    # #567: spike-override discharge rate; 0 = inert until the household sets
    # it. The max is headroom only; BatteryConfig validates the real ceiling.
    # #694: set it above every P2P block's rate, or an override during an
    # active block can make the solve infeasible.
    _SolverNumberDescription(
        CONF_SOLVER_PRICE_SPIKE_DISCHARGE_KW,
        "Price Spike Discharge Rate",
        DEFAULT_SOLVER_PRICE_SPIKE_DISCHARGE_KW,
        0,
        100,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    # nimbus issue #1784: the solar running-low gate (see const.py). The
    # minimum is also its off switch: 0 = off.
    _SolverNumberDescription(
        CONF_SOLVER_SOLAR_RUNNING_LOW_MIN_P50_KW,
        "Solar Running Low Minimum P50",
        DEFAULT_SOLVER_SOLAR_RUNNING_LOW_MIN_P50_KW,
        0,
        100,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
    ),
    _SolverNumberDescription(
        CONF_SOLVER_SOLAR_RUNNING_LOW_ON_PERCENT,
        "Solar Running Low On Below",
        DEFAULT_SOLVER_SOLAR_RUNNING_LOW_ON_PERCENT,
        1,
        100,
        1,
        "%",
    ),
    _SolverNumberDescription(
        CONF_SOLVER_SOLAR_RUNNING_LOW_OFF_PERCENT,
        "Solar Running Low Off Above",
        DEFAULT_SOLVER_SOLAR_RUNNING_LOW_OFF_PERCENT,
        1,
        100,
        1,
        "%",
    ),
    # 0 = no confirmation (on at the first low reading). See const.py.
    _SolverNumberDescription(
        CONF_SOLVER_SOLAR_RUNNING_LOW_CONFIRM_MINUTES,
        "Solar Running Low Confirm",
        DEFAULT_SOLVER_SOLAR_RUNNING_LOW_CONFIRM_MINUTES,
        0,
        60,
        1,
        "min",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # Version read independently of sensor.py (platform setup order is not
    # guaranteed); HA merges device info by identifier either way.
    integration = await async_get_integration(hass, DOMAIN)
    sw_version = str(integration.version) if integration.version else None
    # nimbus issue #465: resolved once here for the new "Nimbus P2P"
    # sub-device the 11 P2P number entities below move onto -- see
    # sensor_flattened.resolve_hub_device_id()'s own docstring for the
    # full history behind this exact call shape.
    hub_device_id = sensor_flattened.resolve_hub_device_id(hass, entry)
    shared_store = _SharedNumberStore(
        store=Store(hass, _STORAGE_VERSION, f"{DOMAIN}_{entry.entry_id}_solver_numbers")
    )
    async_add_entities(
        [
            NimbusSolverNumber(entry, desc, sw_version, shared_store, hub_device_id)
            for desc in _DESCRIPTIONS
        ]
    )
    # #645: per-load live numbers share the hub's Store (keys are
    # subentry-prefixed). #680 (Mark): add entities per subentry with
    # config_subentry_id, as sensor.py does, or HA moves the shared device
    # and its sensors drop out of the registry.
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_CONTROLLABLE_LOAD:
            continue
        this_loads_entities = [
            NimbusControllableLoadNumber(
                entry, subentry, desc, sw_version, shared_store
            )
            for desc in _CONTROLLABLE_LOAD_DESCRIPTIONS
            if subentry.data.get(CONF_CONTROLLABLE_LOAD_KIND) in desc.kinds
        ]
        if this_loads_entities:
            async_add_entities(
                this_loads_entities, config_subentry_id=subentry.subentry_id
            )


class NimbusSolverNumber(_CurrencyUnitMixin, RestoreNumber, NumberEntity):
    """One live, dashboard-editable Solver setting. See this module's own
    docstring for why these are plain restored local state, never written
    back into entry.options."""

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    # Gold entity-category (2026-08-23): every one of these entities IS
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
        hub_device_id: str | None = None,
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
        # No _attr_native_unit_of_measurement here: `_CurrencyUnitMixin`'s
        # own property is the single answer, so a currency sentinel cannot be
        # published raw by an __init__ that ran before `hass` was attached
        # (nimbus issue #1293).
        self._attr_device_class = desc.device_class
        # nimbus issue #465: desc.sub_device="p2p" (the 11 P2P fields
        # only) moves this entity onto the "Nimbus P2P" sub-device
        # instead of the hub -- same via_device/via_device_id mechanism
        # already proven for sensor.py's own Quality/Backtest/
        # Counterfactual sub-devices, unique_id/entity_id above are
        # completely unaffected (only device_info changes), so this is a
        # non-breaking move for these 11 already-deployed entities.
        if desc.sub_device == "p2p":
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, f"{entry.entry_id}_p2p")},
                name="Nimbus P2P",
                manufacturer="Nimbus",
                model="Sub-device",
                sw_version=sw_version,
                **sensor_flattened.resolve_via_device_field(  # type: ignore[typeddict-item]
                    hub_device_id, entry.entry_id
                ),
            )
        else:
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
        await self._async_restore_value()
        await self._async_apply_one_time_value_change()

    async def _async_apply_one_time_value_change(self) -> None:
        """See `_ONE_TIME_VALUE_CHANGES`. Runs once per install and key: the
        marker is written whether or not the value matched, so a later edit
        back to the old value is never undone."""
        change = _ONE_TIME_VALUE_CHANGES.get(self._desc.key)
        if change is None:
            return
        marker, old, new = change
        if await self._shared_store.async_read(marker) is not None:
            return
        current = self._attr_native_value
        if current is not None and float(current) == old:
            self._attr_native_value = new
            await self._shared_store.async_write(self._desc.key, new)
            _LOGGER.info(
                "Nimbus: %s changed once from %s to %s on upgrade (see the "
                "CHANGELOG); set it again to keep a different value",
                self._desc.key,
                old,
                new,
            )
        await self._shared_store.async_write(marker, 1.0)

    async def _async_restore_value(self) -> None:
        restored = await self.async_get_last_number_data()
        if restored is not None and restored.native_value is not None:
            # #342 (Mark): backfill the Store only when the restored state
            # is not older than the Store's written_at, so a newer edit
            # survives a stale restore-state dump.
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
        # nimbus #1574 (DC-004 step 5c): in the device model's `active` mode a
        # contract-owned number commits a new revision, so the slider edits the
        # document. A task, not awaited: the commit takes the entry's lock, and
        # a view write may be what set this value. Every other mode, and every
        # number the document doesn't own, returns at once.
        hass = getattr(self, "hass", None)
        if hass is not None:  # always set once HA has added the entity
            from . import device_contract_ha

            hass.async_create_task(
                device_contract_ha.async_number_changed(
                    hass, self._entry, self._desc.key, float(value)
                )
            )


def _slug_for_entity_id(title: str) -> str:
    """A plain, predictable slug for a human-typed title -- deliberately
    a verbatim duplicate of sensor.py's own private helper of the same
    name, not an import from it. sensor.py and number.py are independent
    platform modules with no existing import dependency between them in
    either direction (only via the shared sensor_flattened.py); reaching
    into another platform module's own underscore-prefixed helper would
    create exactly the kind of cross-platform coupling this project's
    own established pattern (small pure helpers duplicated where a
    genuine import boundary exists -- done_condition.py's own module
    docstring covers the same reasoning) deliberately avoids. Keep in
    sync with sensor.py's own copy if the slugging rule itself ever
    changes -- both must produce the SAME entity_id for the SAME title,
    since sensor.py's own commanded_state entity and this module's
    number entities live on the same load device and are meant to read
    as one obviously-related family."""
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug or "load"


@dataclass(frozen=True)
class _ControllableLoadNumberDescription:
    """One live, dashboard-editable Controllable Load tuning field --
    nimbus issue #645, the exact same "wizard for first-time setup,
    live entity for day-to-day tuning" split #603 already documents for
    the hub-level Solver settings above, applied per load instead of
    once per hub."""

    key: str  # CONF_DEFERRABLE_*/CONF_CONTROLLABLE_LOAD_* -- also this
    # entity's own entity_id suffix and the field build_controllable_
    # loads()/apply_commanded_state_guard() read via solver_writer.py's
    # own _resolve_controllable_load_tuning().
    name: str
    default: float
    min_value: float
    max_value: float
    step: float
    unit: str | None
    device_class: NumberDeviceClass | None = None
    # Which Controllable Load kind(s) this field applies to -- a
    # sheddable load has no earliest_hour/deadline_hour/target_kwh/
    # max_power_kw/shortfall_price concept at all (those are deferrable-
    # only, per flows/controllable_load_subentry.py's own schema), while
    # min_hold_minutes/max_activations_per_day are genuinely shared by
    # both kinds (the relay-chatter guard applies uniformly). Defaults
    # to "both kinds" so a shared field doesn't need to repeat itself.
    kinds: tuple[str, ...] = (
        CONTROLLABLE_LOAD_KIND_SHEDDABLE,
        CONTROLLABLE_LOAD_KIND_DEFERRABLE,
    )


# Bounds mirror flows/controllable_load_subentry.py's selectors; the two
# fields with no wizard max get generous headroom. #645 scope excludes the
# three sheddable-only fields.
_CONTROLLABLE_LOAD_DESCRIPTIONS: tuple[_ControllableLoadNumberDescription, ...] = (
    _ControllableLoadNumberDescription(
        CONF_DEFERRABLE_TARGET_KWH,
        "Target",
        0.0,
        0,
        1000,
        0.1,
        "kWh",
        device_class=NumberDeviceClass.ENERGY,
        kinds=(CONTROLLABLE_LOAD_KIND_DEFERRABLE,),
    ),
    _ControllableLoadNumberDescription(
        CONF_DEFERRABLE_MAX_POWER_KW,
        "Max Power",
        0.0,
        0,
        1000,
        0.1,
        "kW",
        device_class=NumberDeviceClass.POWER,
        kinds=(CONTROLLABLE_LOAD_KIND_DEFERRABLE,),
    ),
    _ControllableLoadNumberDescription(
        CONF_DEFERRABLE_EARLIEST_HOUR,
        "Earliest Hour",
        0.0,
        0,
        23.75,
        0.25,
        "24hr decimal",
        kinds=(CONTROLLABLE_LOAD_KIND_DEFERRABLE,),
    ),
    _ControllableLoadNumberDescription(
        CONF_DEFERRABLE_DEADLINE_HOUR,
        "Deadline Hour",
        23.75,
        0,
        23.75,
        0.25,
        "24hr decimal",
        kinds=(CONTROLLABLE_LOAD_KIND_DEFERRABLE,),
    ),
    _ControllableLoadNumberDescription(
        CONF_DEFERRABLE_SHORTFALL_PRICE,
        "Shortfall Price",
        10.0,
        0,
        1000,
        0.01,
        _CURRENCY_PER_KWH,
        kinds=(CONTROLLABLE_LOAD_KIND_DEFERRABLE,),
    ),
    _ControllableLoadNumberDescription(
        CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
        "Min Hold Minutes",
        0.0,
        0,
        1440,
        1,
        "min",
    ),
    # #645: the wizard's None (no cap) can't be shown by a number entity, so
    # the live entity defaults to 10/day; "unlimited" isn't available here.
    _ControllableLoadNumberDescription(
        CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
        "Max Activations Per Day",
        10.0,
        1,
        100,
        1,
        None,
    ),
)


class NimbusControllableLoadNumber(_CurrencyUnitMixin, RestoreNumber, NumberEntity):
    """One live, dashboard-editable Controllable Load tuning field --
    nimbus issue #645. Same restore-and-seed-once, durable-Store-backstop
    mechanism as NimbusSolverNumber above (see that class's own
    docstring and this module's own top docstring for the full "why not
    written back into the subentry" reasoning -- identical here: a
    dashboard slider on a Controllable Load must never trigger a hub
    reload either), keyed per (subentry, field) instead of per (entry,
    field), living on the load's own device instead of the hub.

    Reuses the SAME _SharedNumberStore instance the hub-level numbers
    already use (see async_setup_entry()'s own comment for why this is
    safe -- the key namespace never collides).
    """

    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        desc: _ControllableLoadNumberDescription,
        sw_version: str | None,
        shared_store: _SharedNumberStore,
    ) -> None:
        self._entry = entry
        self._subentry = subentry
        self._desc = desc
        # Store/unique_id key -- subentry_id (a ULID) + field, so this
        # never collides with the hub-level keys (plain CONF_SOLVER_*
        # strings) or with another load's own same-named field.
        self._store_key = f"{subentry.subentry_id}_{desc.key}"
        self._shared_store = shared_store
        self._attr_unique_id = self._store_key
        # nimbus issue #579's own real, live lesson (see sensor.py's own
        # commanded_state entity_id comment): a raw ULID in entity_id is
        # both invalid (HA-deprecated) and unreadable -- derive from the
        # load's own title instead, same _slug_for_entity_id() technique.
        self.entity_id = (
            f"number.nimbus_{_slug_for_entity_id(subentry.title)}_{desc.key}"
        )
        self._attr_name = desc.name
        self._attr_native_min_value = desc.min_value
        self._attr_native_max_value = desc.max_value
        self._attr_native_step = desc.step
        # No _attr_native_unit_of_measurement here: `_CurrencyUnitMixin`'s
        # own property is the single answer, so a currency sentinel cannot be
        # published raw by an __init__ that ran before `hass` was attached
        # (nimbus issue #1293).
        self._attr_device_class = desc.device_class
        # Same per-load sub-device every other Controllable Load entity
        # (commanded_state, the #590 schedule-view sensors) already
        # attaches to -- (DOMAIN, subentry.subentry_id) groups all of
        # them onto that one load's own device page.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            manufacturer="Nimbus",
            model="Controllable Load",
            sw_version=sw_version,
        )
        self._attr_native_value = desc.default

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored = await self.async_get_last_number_data()
        if restored is not None and restored.native_value is not None:
            # Same freshness-compare-against-the-Store logic as
            # NimbusSolverNumber's own async_added_to_hass() -- see that
            # method's own comment for the full 2026-09-02 incident this
            # protects against.
            restored_state = await self.async_get_last_state()
            restored_at = (
                restored_state.last_updated.timestamp()
                if restored_state is not None
                else 0.0
            )
            stored_entry = await self._shared_store._async_read_entry(self._store_key)
            if stored_entry is not None and stored_entry[1] > restored_at:
                self._attr_native_value = stored_entry[0]
                return
            self._attr_native_value = restored.native_value
            await self._shared_store.async_write(self._store_key, restored.native_value)
            return
        stored_value = await self._shared_store.async_read(self._store_key)
        if stored_value is not None:
            self._attr_native_value = stored_value
            return
        # No RestoreNumber state AND no Store entry -- this entity has
        # never existed before on this install. Seed from whatever's
        # already in the subentry's own wizard-saved data, so rolling
        # this platform out onto an already-configured load doesn't
        # silently reset its real values back to a generic default. A
        # genuinely fresh load (field never set in the wizard either)
        # falls through to desc.default, set in __init__ above.
        seeded = self._subentry.data.get(self._desc.key)
        if seeded is not None:
            try:
                self._attr_native_value = float(seeded)
            except (TypeError, ValueError):
                return
            await self._shared_store.async_write(
                self._store_key, self._attr_native_value
            )

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        await self._shared_store.async_write(self._store_key, value)
