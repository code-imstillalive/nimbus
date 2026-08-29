"""Flattened per-attribute SensorEntity fan-out for
sensor.nimbus_solver_battery_forecast (Family A: top-level scalars).

The parent sensor.nimbus_solver_battery_forecast (see
NimbusSolverBatteryForecastSensor in sensor.py) already publishes a rich
~40-key attribute dict on every solve. That's great as a single blob for
Lovelace's own attribute picker, but it means every downstream
consumer -- a Lovelace card, a downstream automation, an HA template
sensor, another integration reading via WebSocket -- has to reach into
`state_attr('sensor.nimbus_solver_battery_forecast', '<key>')` for
anything but the primary state (battery_kw), which:

- Doesn't participate in HA's history graphs or long-term statistics
  (LTS records the state, not each individual attribute -- so
  total_cost, equivalent_full_cycles, solve_seconds, and everything
  else Nimbus computes get thrown away on every recorder purge cycle
  despite being genuinely worth trending over weeks/months).
- Can't have a device_class or unit set (the parent's contract is that
  the WHOLE sensor is POWER/kW; individual attributes have to be
  reinterpreted at every read site).
- Doesn't get its own entity page, so a user can't just say "graph
  total_cost over the last 30 days" without building a template sensor
  first.

This module solves that with a purely-additive fan-out:

1. FLATTENED_ATTRS below declares each attribute that deserves its own
   entity, with its category (primary/diagnostic), device_class, unit,
   state_class, and display precision.
2. `create_flattened_entities()` returns one SensorEntity per row.
3. `dispatch_to_flattened()` is called from
   NimbusSolverBatteryForecastSensor.update_from_solver() after that
   entity has published its own state -- it iterates the registered
   flattened entities and updates each with the appropriate slice of
   `attributes`. Same solve cycle, same push, same worker thread; the
   fan-out is on the HA event loop (all update_from_solver calls are
   already @callback), so it's fast and lock-free.

Every flattened entity:
- Attaches to the SAME Nimbus hub device as the parent (same DeviceInfo
  identifiers), so they all live under the one "Nimbus" device page.
- Preserves a fixed, well-known entity_id (sensor.nimbus_solver_<key>)
  so a future move to a different fan-out mechanism keeps LTS attached.
- Derives its unique_id from entry.entry_id + the attribute key, so a
  reinstall on the same hub re-attaches to the same entity registry
  row.
- Sets device_class / state_class / unit_of_measurement at
  class-attribute time (same reasoning as issue #61 / #263: unit must
  come from the SensorEntity contract, not from an attrs dict).

Family A (this module): top-level scalar attributes only. The 40
per-row forecast fields (grid_import_kw, flow_pv_to_battery_kw, ...)
are a separate concern -- their state is time-shifted (row matching
"now") and their forecast attribute is a projection of the parent's
own forecast array. Deferred to a follow-up PR so this one stays
reviewable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo

# HA core exposes PERCENTAGE / UnitOfEnergy / UnitOfTime as constants,
# but the rest of this integration (see sensor.py: only UnitOfPower is
# imported) has been consistent in importing units on demand. Keep this
# module's imports as narrow as sensor.py's own so it stays compatible
# with the test-suite HA stubs (tests/_ha_stubs.py only stubs the four
# names above from homeassistant.const). All other unit strings are
# hardcoded to the real HA core values -- what statistics recorders and
# the frontend actually see on the wire.
_PERCENT = "%"
_KWH = "kWh"
_HOURS = "h"
_SECONDS = "s"

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


# The AUD unit isn't exposed as a homeassistant.const constant the way
# UnitOfPower.KILO_WATT is -- monetary units are user-configurable and
# come from hass.config.currency at runtime. For a plain SensorEntity
# with a fixed unit_of_measurement (which is what we want here for
# solve-time constants like total_cost that never change unit within
# a running install), passing "AUD" as a string is the honest fit --
# the same pattern statistics.energy.* uses when it hardcodes "kWh".
# Australian residential Nimbus installs are the only real-world
# deployment target today; the parent NimbusSolverConfigSensor already
# hardcodes AUD elsewhere.
_AUD = "AUD"
_AUD_PER_KWH = "AUD/kWh"


@dataclass(frozen=True)
class FlattenedAttrSpec:
    """One row of the declarative table below.

    Every field maps directly to a SensorEntity class attribute (or
    __init__ argument in the case of the source-attribute key). The
    factory `_make_flattened_entity()` below builds one dynamic class
    per row so each entity has correct static class attributes visible
    to HA's own entity registry, device-info discovery, and Recorder.
    """

    # Source attribute key on sensor.nimbus_solver_battery_forecast.
    # This is the field the flattened entity pulls its native_value
    # from every solve.
    source_key: str
    # Human-friendly entity name (appended after "Nimbus" from
    # DeviceInfo when _attr_has_entity_name is True). Matches the
    # existing "Solver Battery Forecast" / "Household Load Total
    # Forecast" / "Solver Dispatch (Dry Run)" naming.
    name: str
    # Entity ID suffix -- final entity_id is f"sensor.nimbus_solver_{suffix}".
    entity_id_suffix: str
    # None = primary user-facing sensor; DIAGNOSTIC = LP internals,
    # solver runtime, config echoes. See the module docstring's
    # category-by-what-they-measure rule.
    entity_category: EntityCategory | None
    device_class: SensorDeviceClass | None
    state_class: SensorStateClass | None
    unit_of_measurement: str | None
    # 3 for kW / kWh / AUD (matches parent), fewer for percentages,
    # None for enums / string states.
    suggested_display_precision: int | None


# ---------------------------------------------------------------------------
# Family A: top-level scalar attributes
# ---------------------------------------------------------------------------
#
# Ordered by concept-group for readability, not by importance:
#   - Costs & savings (primary, monetary)
#   - Energy totals (primary, energy)
#   - Battery health signals (primary)
#   - Load-side signals (primary)
#   - Solve runtime & LP state (diagnostic)
#   - Efficiency & config echoes (diagnostic)
#   - Shadow prices (diagnostic, monetary)
#
# Deliberately excluded (see module docstring):
#   - forecast (array, not a scalar; belongs to Family B)
#   - friendly_name / device_class / state_class / unit_of_measurement (HA metadata)
#   - source_sensor / signal_role / battery_kw_side / battery_kw_sign_convention
#     / efficiency_convention / price_blend_algorithm (string constants; better
#     as parent attributes than as their own sensors)
#   - failed_load_entities / load_forecast_warnings / load_forecast_source_*
#     (surfaced via NimbusHealthReportSensor instead)
#   - generated_at (already reflected in each child's own last_updated
#     stamp)
#   - cost_band, cost_breakdown (dict-valued; flattened here into
#     component scalars below)

FLATTENED_ATTRS: tuple[FlattenedAttrSpec, ...] = (
    # --- Costs & savings (primary, monetary) --------------------------------
    FlattenedAttrSpec(
        source_key="total_cost",
        name="Solver Total Cost",
        entity_id_suffix="total_cost",
        entity_category=None,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="total_cost_with_fixed_costs",
        name="Solver Total Cost (with fixed costs)",
        entity_id_suffix="total_cost_with_fixed_costs",
        entity_category=None,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    # cost_band and cost_breakdown are dict-valued on the parent -- decomposed
    # into scalar sensors so each component can be graphed and trended
    # independently.
    FlattenedAttrSpec(
        source_key="cost_band.lower",
        name="Solver Cost Band Lower",
        entity_id_suffix="cost_band_lower",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="cost_band.upper",
        name="Solver Cost Band Upper",
        entity_id_suffix="cost_band_upper",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="cost_band.width",
        name="Solver Cost Band Width",
        entity_id_suffix="cost_band_width",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="cost_breakdown.grid_net",
        name="Solver Cost Breakdown Grid Net",
        entity_id_suffix="cost_breakdown_grid_net",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="cost_breakdown.degradation",
        name="Solver Cost Breakdown Degradation",
        entity_id_suffix="cost_breakdown_degradation",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="cost_breakdown.charge_fee",
        name="Solver Cost Breakdown Charge Fee",
        entity_id_suffix="cost_breakdown_charge_fee",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="cost_breakdown.discharge_fee",
        name="Solver Cost Breakdown Discharge Fee",
        entity_id_suffix="cost_breakdown_discharge_fee",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="cost_breakdown.terminal_value_credit",
        name="Solver Cost Breakdown Terminal Value Credit",
        entity_id_suffix="cost_breakdown_terminal_value_credit",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    # --- Energy totals (primary) --------------------------------------------
    FlattenedAttrSpec(
        source_key="total_charge_kwh",
        name="Solver Total Charge Energy",
        entity_id_suffix="total_charge_kwh",
        entity_category=None,
        device_class=SensorDeviceClass.ENERGY,
        # MEASUREMENT rather than TOTAL_INCREASING -- this is a
        # per-plan total (recomputed every solve, overwritten), not a
        # cumulative meter. TOTAL_INCREASING would break the recorder's
        # own monotonicity assumption on every re-solve.
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_KWH,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="total_discharge_kwh",
        name="Solver Total Discharge Energy",
        entity_id_suffix="total_discharge_kwh",
        entity_category=None,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_KWH,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="total_throughput_kwh",
        name="Solver Total Throughput Energy",
        entity_id_suffix="total_throughput_kwh",
        entity_category=None,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_KWH,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="ac_bus_losses_kwh",
        name="Solver AC Bus Losses",
        entity_id_suffix="ac_bus_losses_kwh",
        entity_category=None,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_KWH,
        suggested_display_precision=3,
    ),
    # --- Battery health (primary) -------------------------------------------
    FlattenedAttrSpec(
        source_key="equivalent_full_cycles",
        name="Solver Equivalent Full Cycles",
        entity_id_suffix="equivalent_full_cycles",
        entity_category=None,
        device_class=None,  # no HA-native device_class for "cycles"
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement="cycles",
        suggested_display_precision=3,
    ),
    # --- Load-side signals (primary) ----------------------------------------
    FlattenedAttrSpec(
        source_key="load_summed_18_now_kw",
        name="Solver Load Summed (Now)",
        entity_id_suffix="load_summed_now_kw",
        entity_category=None,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="load_whole_house_cross_check_now_kw",
        name="Solver Load Whole-House Cross-Check (Now)",
        entity_id_suffix="load_whole_house_cross_check_now_kw",
        entity_category=None,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=3,
    ),
    # --- P2P signals (primary) ----------------------------------------------
    FlattenedAttrSpec(
        source_key="p2p_match_fraction",
        name="Solver P2P Match Fraction",
        entity_id_suffix="p2p_match_fraction",
        entity_category=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_PERCENT,
        suggested_display_precision=1,
    ),
    FlattenedAttrSpec(
        source_key="p2p_recent_avg_volume_kwh",
        name="Solver P2P Recent Average Volume",
        entity_id_suffix="p2p_recent_avg_volume_kwh",
        entity_category=None,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_KWH,
        suggested_display_precision=3,
    ),
    # --- Solve runtime (diagnostic) ------------------------------------------
    FlattenedAttrSpec(
        source_key="solve_seconds",
        name="Solver Solve Duration",
        entity_id_suffix="solve_seconds",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_SECONDS,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="horizon_hours",
        name="Solver Horizon",
        entity_id_suffix="horizon_hours",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_HOURS,
        suggested_display_precision=1,
    ),
    FlattenedAttrSpec(
        source_key="load_forecast_coverage_hours",
        name="Solver Load Forecast Coverage",
        entity_id_suffix="load_forecast_coverage_hours",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_HOURS,
        suggested_display_precision=1,
    ),
    FlattenedAttrSpec(
        source_key="n_periods",
        name="Solver Plan Periods",
        entity_id_suffix="n_periods",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,
        suggested_display_precision=0,
    ),
    FlattenedAttrSpec(
        source_key="n_clamped_periods",
        name="Solver Clamped Periods",
        entity_id_suffix="n_clamped_periods",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,
        suggested_display_precision=0,
    ),
    # --- LP state (diagnostic) ----------------------------------------------
    FlattenedAttrSpec(
        source_key="status",
        name="Solver LP Status",
        entity_id_suffix="lp_status",
        entity_category=EntityCategory.DIAGNOSTIC,
        # ENUM would be strictly correct here but requires declaring
        # options up-front and the exact string set from highspy is a
        # library-internals detail we don't want to lock in. Plain
        # string state is the honest fit.
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        suggested_display_precision=None,
    ),
    FlattenedAttrSpec(
        source_key="binding_constraint_now",
        name="Solver Binding Constraint",
        entity_id_suffix="binding_constraint_now",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=None,
        unit_of_measurement=None,
        suggested_display_precision=None,
    ),
    # --- Shadow prices (diagnostic, monetary/kWh) ---------------------------
    FlattenedAttrSpec(
        source_key="binding_constraint_shadow_price",
        name="Solver Binding Constraint Shadow Price",
        entity_id_suffix="binding_constraint_shadow_price",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD_PER_KWH,
        suggested_display_precision=4,
    ),
    FlattenedAttrSpec(
        source_key="energy_shadow_price_now",
        name="Solver Energy Shadow Price (Now)",
        entity_id_suffix="energy_shadow_price_now",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD_PER_KWH,
        suggested_display_precision=4,
    ),
    FlattenedAttrSpec(
        source_key="p2p_volume_cap_shadow_price",
        name="Solver P2P Volume Cap Shadow Price",
        entity_id_suffix="p2p_volume_cap_shadow_price",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD_PER_KWH,
        suggested_display_precision=4,
    ),
    # --- Efficiency & config echoes (diagnostic) ----------------------------
    FlattenedAttrSpec(
        source_key="charge_efficiency",
        name="Solver Charge Efficiency",
        entity_id_suffix="charge_efficiency",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,  # ratio 0-1, no unit
        suggested_display_precision=4,
    ),
    FlattenedAttrSpec(
        source_key="discharge_efficiency",
        name="Solver Discharge Efficiency",
        entity_id_suffix="discharge_efficiency",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,
        suggested_display_precision=4,
    ),
    FlattenedAttrSpec(
        source_key="degradation_cost_per_kwh",
        name="Solver Degradation Cost",
        entity_id_suffix="degradation_cost_per_kwh",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD_PER_KWH,
        suggested_display_precision=4,
    ),
    FlattenedAttrSpec(
        source_key="risk_aversion",
        name="Solver Risk Aversion",
        entity_id_suffix="risk_aversion",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="salvage_value",
        name="Solver Salvage Value",
        entity_id_suffix="salvage_value",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD_PER_KWH,
        suggested_display_precision=4,
    ),
    FlattenedAttrSpec(
        source_key="import_price_risk_aversion",
        name="Solver Import Price Risk Aversion",
        entity_id_suffix="import_price_risk_aversion",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="export_price_risk_aversion",
        name="Solver Export Price Risk Aversion",
        entity_id_suffix="export_price_risk_aversion",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="solar_delivery_ratio",
        name="Solver Solar Delivery Ratio",
        entity_id_suffix="solar_delivery_ratio",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="solar_delivery_sample_count",
        name="Solver Solar Delivery Sample Count",
        entity_id_suffix="solar_delivery_sample_count",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=None,
        suggested_display_precision=0,
    ),
)


# ---------------------------------------------------------------------------
# Base entity class
# ---------------------------------------------------------------------------


class _FlattenedAttributeSensor(SensorEntity):
    """One SensorEntity per FLATTENED_ATTRS row -- a filtered projection
    of sensor.nimbus_solver_battery_forecast's own attribute dict.

    Shares the same staleness/availability pattern as
    _NimbusSolverPushSensor (see sensor.py) -- becomes `unavailable`
    when no fresh solve has landed in _STALE_AFTER_SECONDS. That's the
    honest behaviour: if the parent stopped publishing (solver import
    failure, an unhandled exception, the periodic timer somehow getting
    cancelled), every child should stop reporting a stale value too.

    Not a subclass of _NimbusSolverPushSensor because these entities
    are NOT independent push targets in solver_writer.py's dispatch
    table -- they're driven synchronously by the parent's own
    update_from_solver(), which fans out via `dispatch_to_flattened()`
    below. That keeps the whole scheme purely additive: one dispatch-
    table entry (the parent's), N children fanning out from that single
    push.
    """

    _attr_has_entity_name = True
    _STALE_AFTER_SECONDS = 5 * 60  # matches _NimbusSolverPushSensor

    def __init__(self, entry, sw_version: str | None, spec: FlattenedAttrSpec) -> None:
        self._entry = entry
        self._spec = spec
        self._attr_unique_id = f"{entry.entry_id}_nimbus_solver_{spec.entity_id_suffix}"
        self.entity_id = f"sensor.nimbus_solver_{spec.entity_id_suffix}"
        self._attr_name = spec.name
        self._attr_entity_category = spec.entity_category
        self._attr_device_class = spec.device_class
        self._attr_state_class = spec.state_class
        self._attr_native_unit_of_measurement = spec.unit_of_measurement
        if spec.suggested_display_precision is not None:
            self._attr_suggested_display_precision = spec.suggested_display_precision
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Nimbus",
            manufacturer="Nimbus",
            model="Hub",
            sw_version=sw_version,
        )
        self._state: Any = None
        self._last_updated: float | None = None

    @property
    def available(self) -> bool:
        """Same staleness contract as _NimbusSolverPushSensor: True
        before the first solve (plain "unknown" state); False once
        _STALE_AFTER_SECONDS has passed since the last successful
        parent push."""
        if self._state is None:
            return True
        if self._last_updated is None:
            return True
        return (time.monotonic() - self._last_updated) < self._STALE_AFTER_SECONDS

    @property
    def native_value(self) -> Any:
        return self._state

    @callback
    def update_from_parent(self, attributes: dict) -> None:
        """Called by dispatch_to_flattened() below on every parent push.

        Pulls this entity's own slice out of `attributes` -- either a
        direct key lookup (source_key without a dot) or a nested lookup
        (dotted path, one level deep, for cost_band.* and
        cost_breakdown.*). Silently drops the update if the parent
        payload doesn't carry this key (a partial solve, a config
        change disabling a code path) -- leaves the previous value in
        place, which the staleness contract above will eventually flip
        to `unavailable` if it stops flowing entirely.

        @callback is the actual fix, not decoration -- see the same
        note on _NimbusSolverPushSensor.update_from_solver() in
        sensor.py (issue #82 root cause).
        """
        value = self._extract(attributes)
        if value is _SENTINEL_MISSING:
            return
        self._state = value
        self._last_updated = time.monotonic()
        if self.hass is not None:
            self.async_write_ha_state()

    def _extract(self, attributes: dict) -> Any:
        """Support dotted source_keys like `cost_band.lower` for one
        level of nesting -- deeper nesting isn't needed by any current
        FLATTENED_ATTRS row and adding it would just be dead code.
        Returns _SENTINEL_MISSING (module-private) when the key path
        isn't present so `update_from_parent` can distinguish "missing"
        from "present-but-None" (which would be a real value published
        by the parent -- e.g. load_forecast_source_error can legitimately
        be None on a healthy solve).
        """
        key = self._spec.source_key
        if "." not in key:
            return attributes.get(key, _SENTINEL_MISSING)
        top, sub = key.split(".", 1)
        parent = attributes.get(top)
        if not isinstance(parent, dict):
            return _SENTINEL_MISSING
        return parent.get(sub, _SENTINEL_MISSING)


# Module-private sentinel -- distinguishes "key not present in parent
# payload" from "key is present and its value is None". _extract()
# above and update_from_parent() both need that distinction.
_SENTINEL_MISSING: Any = object()


# ---------------------------------------------------------------------------
# Factory + fan-out
# ---------------------------------------------------------------------------


def create_flattened_entities(
    entry, sw_version: str | None
) -> list[_FlattenedAttributeSensor]:
    """Instantiate one entity per FLATTENED_ATTRS row.

    Called once by sensor.py's async_setup_entry, in the same "one per
    hub" position as the existing NimbusSolverBatteryForecastSensor /
    NimbusHouseholdLoadTotalForecastSensor / NimbusDispatchDryRunSensor
    creation lines.
    """
    return [
        _FlattenedAttributeSensor(entry, sw_version, spec) for spec in FLATTENED_ATTRS
    ]


def dispatch_to_flattened(
    entities: list[_FlattenedAttributeSensor], attributes: dict
) -> None:
    """Fan out the parent's own attribute dict to every flattened child.

    Called by NimbusSolverBatteryForecastSensor.update_from_solver()
    AFTER it publishes its own state -- same event-loop cycle, same
    solve tick, no queueing, no locking (all update_from_parent calls
    are @callback and run synchronously).

    Missing keys are handled silently by each child's own _extract().
    """
    for entity in entities:
        entity.update_from_parent(attributes)
