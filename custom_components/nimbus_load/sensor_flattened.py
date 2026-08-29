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


# ===========================================================================
# Family-A completion (2026-08-29, issue #55 follow-up): the three remaining
# raw-REST-fallback parent sensors -- sensor.nimbus_solver_quality_report,
# sensor.nimbus_efficiency_backtest, sensor.nimbus_counterfactual_soc --
# were still on solver_writer.ha_post_state()'s raw states.async_set()
# path when the first pass of #55 shipped (that pass only migrated the two
# forecast sensors and the dry-run one, all three of which the LP recomputes
# every solve tick; the three below run on a slower cadence -- the quality
# and counterfactual daily, the backtest weekly -- and were left behind
# specifically because the migration budget for that PR was already at its
# reviewable limit).
#
# Same purely-additive fan-out shape as FLATTENED_ATTRS above, one tuple per
# parent so the three concerns stay independently readable. Sub-device
# DeviceInfo (identifiers=(DOMAIN, f"{entry.entry_id}_quality") etc.) is set
# by the _FlattenedAttributeSensorSubDevice base further below -- unlike the
# Family-A children above (which attach directly to the hub device), these
# children attach to the same sub-device as their parent so a user landing
# on e.g. the "Nimbus Quality" device page sees the parent + its 10 scalars
# in one place instead of scattered across a 40+ entity hub page.
#
# Deliberately excluded from FLATTENED_ATTRS_QUALITY below (called out in the
# PR body so they can be added later without another round of design):
# real_p2p_dollars, real_p2p_volume_kwh, latest_date, generated_at. No
# downstream user need identified yet for these four; keeping them as parent
# attributes only for now.
# ---------------------------------------------------------------------------


FLATTENED_ATTRS_QUALITY: tuple[FlattenedAttrSpec, ...] = (
    # --- Primary: Effective Performance Ratio (mirrors the parent's own state) ---
    FlattenedAttrSpec(
        source_key="epr",
        name="Quality EPR",
        entity_id_suffix="epr",
        entity_category=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_PERCENT,
        suggested_display_precision=1,
    ),
    # --- Yield / value captured / uplift available (diagnostic, monetary) ------
    FlattenedAttrSpec(
        source_key="theoretical_maximum_yield",
        name="Quality Theoretical Maximum Yield",
        entity_id_suffix="theoretical_maximum_yield",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="value_captured",
        name="Quality Value Captured",
        entity_id_suffix="value_captured",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="uplift_available",
        name="Quality Uplift Available",
        entity_id_suffix="uplift_available",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    # --- J_ref / J_ach / J_star -- the EPR building blocks (diagnostic) --------
    FlattenedAttrSpec(
        source_key="j_ref",
        name="Quality J_ref (reference cost)",
        entity_id_suffix="j_ref",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="j_ach",
        name="Quality J_ach (achieved cost)",
        entity_id_suffix="j_ach",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="j_star",
        name="Quality J_star (oracle cost)",
        entity_id_suffix="j_star",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="regret_dollars",
        name="Quality Regret",
        entity_id_suffix="regret_dollars",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    # --- Tracking (diagnostic) --------------------------------------------------
    FlattenedAttrSpec(
        source_key="tracking_fidelity",
        name="Quality Tracking Fidelity",
        entity_id_suffix="tracking_fidelity",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_PERCENT,
        suggested_display_precision=1,
    ),
    FlattenedAttrSpec(
        source_key="tracking_cost",
        name="Quality Tracking Cost",
        entity_id_suffix="tracking_cost",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
)


FLATTENED_ATTRS_BACKTEST: tuple[FlattenedAttrSpec, ...] = (
    # --- Primary: configured efficiency vs the swept candidates ---------------
    FlattenedAttrSpec(
        source_key="configured_efficiency_percent",
        name="Backtest Configured Efficiency",
        entity_id_suffix="configured_efficiency_percent",
        entity_category=None,
        device_class=None,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_PERCENT,
        suggested_display_precision=1,
    ),
    # --- Best/worst candidate costs (diagnostic, monetary) --------------------
    FlattenedAttrSpec(
        source_key="best_candidate_cost",
        name="Backtest Best Candidate Cost",
        entity_id_suffix="best_candidate_cost",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
    FlattenedAttrSpec(
        source_key="worst_candidate_cost",
        name="Backtest Worst Candidate Cost",
        entity_id_suffix="worst_candidate_cost",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_AUD,
        suggested_display_precision=3,
    ),
)


FLATTENED_ATTRS_COUNTERFACTUAL: tuple[FlattenedAttrSpec, ...] = (
    # All three are SoC-close percentages -- BATTERY device_class so HA's own
    # device-class-specific formatters (e.g. the battery-tile card) work
    # natively. All diagnostic because the parent's own state is the primary
    # user-facing scalar; these are the anchor/close pair that produced it.
    FlattenedAttrSpec(
        source_key="real_soc_anchor_pct",
        name="Counterfactual Real SoC Anchor",
        entity_id_suffix="real_soc_anchor_pct",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_PERCENT,
        suggested_display_precision=1,
    ),
    FlattenedAttrSpec(
        source_key="nimbus_only_soc_close_pct",
        name="Counterfactual Nimbus-only SoC Close",
        entity_id_suffix="nimbus_only_soc_close_pct",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_PERCENT,
        suggested_display_precision=1,
    ),
    FlattenedAttrSpec(
        source_key="real_soc_close_pct",
        name="Counterfactual Real SoC Close",
        entity_id_suffix="real_soc_close_pct",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        unit_of_measurement=_PERCENT,
        suggested_display_precision=1,
    ),
)


# ---------------------------------------------------------------------------
# Sub-device variant of the flattened base
# ---------------------------------------------------------------------------


class _FlattenedAttributeSensorSubDevice(_FlattenedAttributeSensor):
    """Same fan-out contract as _FlattenedAttributeSensor above, with one
    difference: DeviceInfo is set to a sub-device linked to the Nimbus hub
    via `via_device` instead of using the hub's own identifier directly.

    Purpose (Family-A completion, 2026-08-29): the three parent sensors this
    round of #55 migrates (quality_report, efficiency_backtest, counter-
    factual_soc) each produce 3-10 scalar children of their own -- adding
    those 16 straight to the hub device would push it past 50+ entities and
    make it genuinely harder for a user to find anything on that page. HA's
    own `via_device` mechanism handles the "child device linked to a parent"
    case natively (the frontend puts a "part of Nimbus" line on each sub-
    device page and includes them in the hub's own device tree), so this is
    the honest fit -- one sub-device per parent, its parent + N scalars
    grouped together, hub kept clean.

    Deliberately a subclass rather than adding keyword args to the base
    class's __init__: the existing 36 Family-A children stay on the hub
    device unchanged (backward compatible, zero risk of a stray identifier
    change orphaning an entity registry row), and the diff here is fully
    localised to the three new tables above.

    entity_id namespace: passes an `entity_id_prefix` too so each family gets
    its own sensor.nimbus_quality_*, sensor.nimbus_backtest_*, sensor.nimbus_
    counterfactual_* prefix -- avoids sensor.nimbus_solver_* collisions with
    the Family-A children on the hub. unique_id gets the same prefix baked
    in for the same reason (a bare "epr" or "real_soc_close_pct" could in
    principle appear on more than one parent later; the prefix keeps them
    globally unique from day one).
    """

    def __init__(
        self,
        entry,
        sw_version: str | None,
        spec: FlattenedAttrSpec,
        device_identifier: tuple[str, str],
        device_name: str,
        entity_id_prefix: str,
    ) -> None:
        # Drive the base __init__ first so all the class-attribute plumbing
        # (unique_id format, entity_category, device_class, state_class,
        # unit, precision, staleness state) is set exactly the same way as
        # for a hub-device flattened child. Then override the two fields
        # that make this a sub-device: entity_id namespace and DeviceInfo.
        super().__init__(entry, sw_version, spec)
        # Re-derive unique_id and entity_id with the family-specific prefix
        # so a bare source_key like "epr" doesn't collide across parents
        # (the whole reason these are grouped under sub-devices anyway).
        self._attr_unique_id = (
            f"{entry.entry_id}_{entity_id_prefix}_{spec.entity_id_suffix}"
        )
        self.entity_id = f"sensor.{entity_id_prefix}_{spec.entity_id_suffix}"
        # DeviceInfo replacement: sub-device identifier + via_device pinning
        # it as a child of the Nimbus hub. HA's device registry uses
        # `via_device` to render the parent/child relationship natively.
        self._attr_device_info = DeviceInfo(
            identifiers={device_identifier},
            name=device_name,
            manufacturer="Nimbus",
            model="Sub-device",
            sw_version=sw_version,
            via_device=(DOMAIN, entry.entry_id),
        )


# ---------------------------------------------------------------------------
# Factories + fan-out (one pair per Family-A-completion parent)
# ---------------------------------------------------------------------------


def create_flattened_entities_quality(
    entry, sw_version: str | None
) -> list[_FlattenedAttributeSensorSubDevice]:
    """One SensorEntity per FLATTENED_ATTRS_QUALITY row, all attached to
    the "Nimbus Quality" sub-device (via_device -> hub). Same "one per
    hub" call site in sensor.py's async_setup_entry as the existing
    create_flattened_entities() above.
    """
    device_identifier = (DOMAIN, f"{entry.entry_id}_quality")
    return [
        _FlattenedAttributeSensorSubDevice(
            entry,
            sw_version,
            spec,
            device_identifier=device_identifier,
            device_name="Nimbus Quality",
            entity_id_prefix="nimbus_quality",
        )
        for spec in FLATTENED_ATTRS_QUALITY
    ]


def create_flattened_entities_backtest(
    entry, sw_version: str | None
) -> list[_FlattenedAttributeSensorSubDevice]:
    """One SensorEntity per FLATTENED_ATTRS_BACKTEST row, all attached to
    the "Nimbus Backtest" sub-device (via_device -> hub).
    """
    device_identifier = (DOMAIN, f"{entry.entry_id}_backtest")
    return [
        _FlattenedAttributeSensorSubDevice(
            entry,
            sw_version,
            spec,
            device_identifier=device_identifier,
            device_name="Nimbus Backtest",
            entity_id_prefix="nimbus_backtest",
        )
        for spec in FLATTENED_ATTRS_BACKTEST
    ]


def create_flattened_entities_counterfactual(
    entry, sw_version: str | None
) -> list[_FlattenedAttributeSensorSubDevice]:
    """One SensorEntity per FLATTENED_ATTRS_COUNTERFACTUAL row, all
    attached to the "Nimbus Counterfactual" sub-device (via_device ->
    hub).
    """
    device_identifier = (DOMAIN, f"{entry.entry_id}_counterfactual")
    return [
        _FlattenedAttributeSensorSubDevice(
            entry,
            sw_version,
            spec,
            device_identifier=device_identifier,
            device_name="Nimbus Counterfactual",
            entity_id_prefix="nimbus_counterfactual",
        )
        for spec in FLATTENED_ATTRS_COUNTERFACTUAL
    ]


def dispatch_to_flattened_quality(
    entities: list[_FlattenedAttributeSensorSubDevice], attributes: dict
) -> None:
    """Fan out the Quality parent's attribute dict to every child.

    Called by NimbusSolverQualityReportSensor.update_from_solver() AFTER
    it publishes its own state -- same event-loop cycle, same push, no
    queueing or locking (all update_from_parent calls are @callback and
    run synchronously). Missing keys are handled silently by each child's
    own _extract().
    """
    for entity in entities:
        entity.update_from_parent(attributes)


def dispatch_to_flattened_backtest(
    entities: list[_FlattenedAttributeSensorSubDevice], attributes: dict
) -> None:
    """Fan out the Backtest parent's attribute dict to every child. See
    dispatch_to_flattened_quality() above for the full contract."""
    for entity in entities:
        entity.update_from_parent(attributes)


def dispatch_to_flattened_counterfactual(
    entities: list[_FlattenedAttributeSensorSubDevice], attributes: dict
) -> None:
    """Fan out the Counterfactual parent's attribute dict to every
    child. See dispatch_to_flattened_quality() above for the full
    contract."""
    for entity in entities:
        entity.update_from_parent(attributes)
