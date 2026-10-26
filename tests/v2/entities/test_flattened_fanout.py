"""The flattened per-attribute sensors: the invariants their fan-out must keep
(#1837).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import sensor_flattened

# --- helpers ---------------------------------------------------------------


def _fake_entry(entry_id: str = "test-entry-flat") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _build_entities():
    entry = _fake_entry()
    entities = sensor_flattened.create_flattened_entities(entry, sw_version="0.94.18")
    # Match the sibling _NimbusSolverPushSensor test-suite convention:
    # hass is None until the real HA lifecycle attaches it -- this is
    # what protects the very-first-solve race in production. Every
    # dispatch path in sensor_flattened.py is guarded on `self.hass is
    # not None`, so tests must explicitly opt in to a hass mock only
    # when they mean to check the async_write_ha_state() side-effect.
    for e in entities:
        e.hass = None
    return entities, entry


# --- spec table coverage ---------------------------------------------------


# --- device-info attachment ------------------------------------------------


# --- spec-to-entity attribute mapping --------------------------------------


# --- fan-out extraction ----------------------------------------------------


def _real_looking_parent_payload() -> dict:
    """Mirrors sensor.nimbus_solver_battery_forecast's real attribute
    shape on a healthy solve -- shape captured directly from a running
    v0.94.18 install. Keeping this as a real fixture rather than
    generating from FLATTENED_ATTRS itself makes the tests catch drift
    in either direction (an added spec row with no real backing key,
    or a real payload key no spec covers).
    """
    return {
        "total_cost": 9.8685,
        "total_cost_with_fixed_costs": 17.7226,
        "total_charge_kwh": 132.19,
        "total_discharge_kwh": 126.92,
        "total_throughput_kwh": 259.11,
        "ac_bus_losses_kwh": 6.644,
        "equivalent_full_cycles": 3.239,
        "load_summed_18_now_kw": 4.792,
        "load_whole_house_cross_check_now_kw": 5.235,
        "p2p_match_fraction": 0.0,
        "p2p_recent_avg_volume_kwh": 0.0,
        "solve_seconds": 1.45,
        "horizon_hours": 96.7,
        "load_forecast_coverage_hours": 96.0,
        "n_periods": 361,
        "n_clamped_periods": 0,
        "status": "optimal",
        "binding_constraint_now": "Grid import at zero (not economical right now)",
        "binding_constraint_shadow_price": 0.0071,
        "energy_shadow_price_now": 0.0012,
        "p2p_volume_cap_shadow_price": -0.0002,
        "charge_efficiency": 0.9747,
        "discharge_efficiency": 0.9747,
        "degradation_cost_per_kwh": 0.03,
        "risk_aversion": 0.05,
        "salvage_value": 0.05,
        "import_price_risk_aversion": 0.0,
        "export_price_risk_aversion": 0.0,
        "solar_delivery_ratio": 1.083,
        "solar_delivery_sample_count": 14,
        "cost_band": {"lower": -4.4046, "upper": 17.4151, "width": 21.8198},
        "cost_breakdown": {
            "grid_net": 0.4653,
            "degradation": 7.7732,
            "charge_fee": 1.3219,
            "discharge_fee": 1.2692,
            "terminal_value_credit": -0.9611,
        },
        # nimbus issue #849: six numeric attributes the parent has always
        # published but that had no flattened child, so they had no
        # history anywhere (the parent stores NO attribute history at all
        # -- HA's 16 KB gate evaluates the full payload before
        # _unrecorded_attributes drops `forecast`/`batteries`). Values
        # below are the real shapes read off the reference household, not
        # invented: envelope limits mirror the configured grid maxima,
        # the risk effects are small signed adjustments, and the live
        # whole-house reading sits near the summed-circuit figure.
        "envelope_import_limit_kw": 20.0,
        "envelope_export_limit_kw": 12.0,
        "solar_risk_effect_now_kw": -0.125,
        "import_price_risk_effect_now": 0.0031,
        "export_price_risk_effect_now": -0.0017,
        "load_whole_house_live_now_kw": 1.685,
    }


# --- staleness / availability ---------------------------------------------


# --- nimbus issue #362 finding 3: should_poll=False + a real, self-driven --
# --- staleness recheck, not just the `available` property in isolation  ---


def test_should_poll_is_false():
    """Before this fix, these ~77 entities had no _attr_should_poll at
    all -- SensorEntity's own default of True meant HA's platform-wide
    30s poll incidentally re-evaluated `available` for every one of
    them, a needless state write every 30s per entity purely as a side
    effect of never having set this explicitly."""
    assert sensor_flattened._FlattenedAttributeSensor._attr_should_poll is False


def test_recheck_availability_is_marked_hass_callback():
    """Same real bug class as issue #82 (see test_push_availability.py's
    own coverage of the sibling class): registered directly as async_
    track_time_interval's own callback, so it must be @callback-marked
    or HA's job-type detection would route every tick to the executor
    thread pool instead of the event loop."""
    job = sensor_flattened._FlattenedAttributeSensor._async_recheck_availability
    assert getattr(job, "_hass_callback", False) is True


# --- category rule (by what it measures) ----------------------------------


# --- integration with the parent sensor's fan-out --------------------------


# ===========================================================================
# Family B (v0.94.20 CHANGELOG deferred item): per-row forecast fields on
# sensor.nimbus_solver_battery_forecast, restricted to the current-period row
# (forecast[0]). Mirrors the Family-A test suite above.
# ===========================================================================


# --- helpers ---------------------------------------------------------------


def _build_entities_current():
    entry = _fake_entry("test-entry-flat-current")
    entities = sensor_flattened.create_flattened_entities_current(
        entry, sw_version="0.94.25"
    )
    for e in entities:
        e.hass = None
    return entities, entry


def _real_looking_forecast_row() -> dict:
    """Mirrors forecast[0] on a healthy v0.94.24 solve -- captured
    directly from a running production install. See
    _real_looking_parent_payload() above for the same rationale (catches
    spec/payload drift in either direction)."""
    return {
        "time": "2026-08-29T20:30:00+10:00",
        "battery_kw": 2.047,
        "battery_kw_after_efficiency": 2.1,
        "soc_pct": 75.36,
        "grid_import_kw": 0.0,
        "grid_export_kw": 0.0,
        "export_bonus_kw": 0.0,
        "import_price": 0.3155,
        "import_price_raw": 0.3155,
        "export_price": 0.0658,
        "export_price_raw": 0.0658,
        "bonus_price": 0.0,
        "load_kw": 2.047,
        "solar_kw": 0.0,
        "dispatch_direction": "discharge",
        "dispatch_hold": False,
        "dispatch_solar_charge": False,
        "dispatch_source_a_label": "Load",
        "dispatch_source_a_pct": 100.0,
        "dispatch_source_b_label": "Grid",
        "dispatch_source_b_pct": 0.0,
        "flow_pv_to_load_kw": 0.0,
        "flow_pv_to_battery_kw": 0.0,
        "flow_pv_to_grid_kw": 0.0,
        "flow_battery_to_load_kw": 2.047,
        "flow_battery_to_grid_kw": 0.0,
        "flow_grid_to_load_kw": 0.0,
        "flow_grid_to_battery_kw": 0.0,
        "flow_price_pv_to_load": 0.3155,
        "flow_price_pv_to_battery": 0.2997,
        "flow_price_pv_to_grid": 0.0658,
        "flow_price_battery_to_load": 0.2997,
        "flow_price_battery_to_grid": -0.2655,
        "flow_price_grid_to_load": -0.3155,
        "flow_price_grid_to_battery": -0.3155,
        "flow_battery_cost_basis": 0.3155,
        "savings_pv": 0.0,
        "savings_battery": 0.0511,
        "savings_combined": 0.0538,
        "savings_interaction": 0.0027,
        "hours": 0.0833,
        "net_cost": 0.0,
    }


# --- spec table coverage ---------------------------------------------------


# --- device-info attachment ------------------------------------------------


# --- spec-to-entity attribute mapping --------------------------------------


# --- fan-out extraction (via dispatch_to_flattened_current) ----------------


def test_current_dispatch_covers_every_entity_no_stragglers():
    """Every spec row's source_key must exist in the real production
    row -- catches a typo or a source_key referencing a field the LP
    doesn't publish."""
    row = _real_looking_forecast_row()
    for spec in sensor_flattened.FLATTENED_ATTRS_CURRENT:
        assert spec.source_key in row, (
            f"FLATTENED_ATTRS_CURRENT references {spec.source_key!r} which is "
            "not present in the real forecast[0] row -- typo or a field the "
            "LP doesn't publish."
        )


# --- category classification (primary vs diagnostic) ----------------------


# --- Flex signals (nimbus issue #496, Signals 7/7 of #489) -----------------


def _build_entities_flex():
    entry = _fake_entry()
    entities = sensor_flattened.create_flattened_entities_flex(
        entry, sw_version="0.94.282"
    )
    for e in entities:
        e.hass = None
    return entities, entry


def test_flex_source_keys_are_unique():
    source_keys = [spec.source_key for spec in sensor_flattened.FLATTENED_ATTRS_FLEX]
    assert len(set(source_keys)) == len(source_keys), (
        "duplicate source_key in FLATTENED_ATTRS_FLEX -- one row would "
        "silently overwrite another's dispatch"
    )
