"""Regression tests for issue #283 (Mark Purcell) -- three real defects in
the flattened per-attribute sensor fan-out (sensor_flattened.py):

1. 39 FlattenedAttrSpec rows across the four fan-out tables combined
   device_class=MONETARY or ENERGY with state_class=MEASUREMENT. HA
   core's own DEVICE_CLASS_STATE_CLASSES only allows state_class='total'
   for MONETARY, and 'total'/'total_increasing' for ENERGY -- MEASUREMENT
   is invalid for both, and HA logs a repair-flow warning on every
   restart for each offending entity. Mark's own report found 24 of
   these; a full scan of this file (re-run after PR #284 landed 9 more
   "current_*" rows mid-fix) found 39: 6 more in the original Family A
   table (total_cost, total_cost_with_fixed_costs, the three cost_band_*
   rows, cost_breakdown_grid_net) plus the 9 new current_* rows PR #284
   introduced. Fixed by dropping device_class on all of them (these are
   per-solve/per-day/per-current-period POINT-IN-TIME values, not
   genuine cumulative meters -- see the file's own inline comments -- so
   MEASUREMENT is the semantically correct state_class, not
   TOTAL_INCREASING as one alternative fix would suggest).
2. FLATTENED_ATTRS_QUALITY had two entities (uplift_available,
   regret_dollars) computed from the byte-identical formula
   (j_ach - j_star) in epr.py -- a real duplicate-valued sensor pair.
   uplift_available was removed; regret_dollars is canonical.
3. tracking_fidelity was published with unit_of_measurement="%" despite
   tracking.py's own tracking_fidelity being a genuine 0-1 fraction
   (1.0 - gap_energy / commanded_activity), never rescaled to 0-100.
   Fixed by dropping the unit rather than rescaling the value.

This test file intentionally does NOT try to import the real HA
DEVICE_CLASS_STATE_CLASSES compatibility table -- the real `homeassistant`
pip package installed on this dev machine is a different version than
what's actually deployed (confirmed: importing custom_components.nimbus_load
directly against it fails with `ImportError: cannot import name
'ConfigSubentry'`), so it can't be trusted as a stand-in here. The
MONETARY/ENERGY-vs-MEASUREMENT incompatibility itself was verified
directly against the real installed homeassistant package in a one-off
check (not part of this repo's own test suite) before this fix was made.
What this file DOES lock in, using the existing install_ha_stubs()
convention: no spec row combines a real-money/real-energy device_class
with state_class=MEASUREMENT ever again, regardless of which HA version
the eventual deploy target runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.nimbus_load import sensor_flattened

_ALL_TABLES = {
    "FLATTENED_ATTRS": sensor_flattened.FLATTENED_ATTRS,
    "FLATTENED_ATTRS_QUALITY": sensor_flattened.FLATTENED_ATTRS_QUALITY,
    "FLATTENED_ATTRS_BACKTEST": sensor_flattened.FLATTENED_ATTRS_BACKTEST,
    "FLATTENED_ATTRS_COUNTERFACTUAL": sensor_flattened.FLATTENED_ATTRS_COUNTERFACTUAL,
    "FLATTENED_ATTRS_CURRENT": sensor_flattened.FLATTENED_ATTRS_CURRENT,
}

# The real HA core constraint (verified directly against the real,
# deployed-version homeassistant package, not this dev machine's stubbed
# or mismatched-version copy): these two device_classes never allow
# state_class=MEASUREMENT.
_NEVER_MEASUREMENT = (SensorDeviceClass.MONETARY, SensorDeviceClass.ENERGY)


def test_no_monetary_or_energy_device_class_combined_with_measurement():
    offenders = []
    for table_name, table in _ALL_TABLES.items():
        for spec in table:
            if (
                spec.device_class in _NEVER_MEASUREMENT
                and spec.state_class is SensorStateClass.MEASUREMENT
            ):
                offenders.append(f"{table_name}.{spec.entity_id_suffix}")
    assert offenders == [], (
        f"{len(offenders)} spec row(s) combine a MONETARY/ENERGY device_class "
        f"with state_class=MEASUREMENT (issue #283, defect 1): {offenders}"
    )


def test_previously_broken_entities_now_have_no_device_class():
    # The exact 38 entity_id_suffix values confirmed broken before the fix
    # (Mark's reported 24 + 6 more found by a full scan of this file, + 9
    # more current_* rows PR #284 introduced mid-fix). Locks in that the
    # fix landed on every one of them, not just Mark's reported subset.
    previously_broken_suffixes = {
        "total_cost",
        "total_cost_with_fixed_costs",
        "cost_band_lower",
        "cost_band_upper",
        "cost_band_width",
        "cost_breakdown_grid_net",
        "cost_breakdown_degradation",
        "cost_breakdown_charge_fee",
        "cost_breakdown_discharge_fee",
        "cost_breakdown_terminal_value_credit",
        "total_charge_kwh",
        "total_discharge_kwh",
        "total_throughput_kwh",
        "ac_bus_losses_kwh",
        "p2p_recent_avg_volume_kwh",
        "binding_constraint_shadow_price",
        "energy_shadow_price_now",
        "p2p_volume_cap_shadow_price",
        "degradation_cost_per_kwh",
        "salvage_value",
        "theoretical_maximum_yield",
        "value_captured",
        "j_ref",
        "j_ach",
        "j_star",
        "regret_dollars",
        "tracking_cost",
        "best_candidate_cost",
        "worst_candidate_cost",
        "current_import_price",
        "current_export_price",
        "current_bonus_price",
        "current_net_cost",
        "current_flow_battery_cost_basis",
        "current_savings_pv",
        "current_savings_battery",
        "current_savings_combined",
        "current_savings_interaction",
    }
    by_suffix = {
        spec.entity_id_suffix: spec for table in _ALL_TABLES.values() for spec in table
    }
    missing = previously_broken_suffixes - set(by_suffix)
    assert not missing, (
        f"expected suffixes not found in any table (renamed/removed?): {missing}"
    )
    assert len(previously_broken_suffixes) == 38, (
        "sanity check on this test's own fixture -- update the count if the "
        "fixture list above is intentionally edited"
    )
    for suffix in previously_broken_suffixes:
        spec = by_suffix[suffix]
        assert spec.device_class is None, (
            f"{suffix} still has device_class={spec.device_class!r} -- "
            "issue #283 fix requires dropping it"
        )
        assert spec.state_class is SensorStateClass.MEASUREMENT, (
            f"{suffix} should keep state_class=MEASUREMENT (issue #283 fix "
            "drops device_class, not state_class)"
        )
        assert spec.unit_of_measurement is not None, (
            f"{suffix} lost its unit_of_measurement -- dropping device_class "
            "must not also drop the unit"
        )


def test_uplift_available_duplicate_sensor_removed():
    suffixes = {
        spec.entity_id_suffix for spec in sensor_flattened.FLATTENED_ATTRS_QUALITY
    }
    assert "uplift_available" not in suffixes, (
        "uplift_available should be removed -- byte-identical formula to "
        "regret_dollars (issue #283, defect 2)"
    )
    assert "regret_dollars" in suffixes, (
        "regret_dollars must remain as the canonical sensor"
    )


def test_tracking_fidelity_has_no_percent_unit():
    by_suffix = {
        spec.entity_id_suffix: spec for spec in sensor_flattened.FLATTENED_ATTRS_QUALITY
    }
    spec = by_suffix["tracking_fidelity"]
    assert spec.unit_of_measurement is None, (
        f"tracking_fidelity should have no unit -- it's a genuine 0-1 "
        f"fraction, not a percentage (issue #283, defect 3), got "
        f"{spec.unit_of_measurement!r}"
    )
