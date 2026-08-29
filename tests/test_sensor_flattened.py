"""Regression tests for the Family A flattened per-attribute sensor
fan-out (see custom_components/nimbus_load/sensor_flattened.py).

Locks in the invariants every future change to that module MUST
preserve:

1. Every FlattenedAttrSpec in FLATTENED_ATTRS produces a real
   SensorEntity with the correct class attributes wired from its
   spec (unique_id / entity_id / device_class / state_class /
   unit_of_measurement / entity_category / suggested_display_
   precision / name).
2. Every entity attaches to the SAME Nimbus hub device as the parent
   forecast sensor (same DOMAIN + entry.entry_id DeviceInfo
   identifier), so they cluster under one device page.
3. Every entity's `entity_id` is unique (no accidental collisions
   between spec rows).
4. Every entity's `_attr_unique_id` is unique.
5. `dispatch_to_flattened()` correctly updates every entity's
   native_value from a real parent attribute dict, including the
   dotted-path extraction for `cost_band.*` and `cost_breakdown.*`.
6. A missing source key in the parent payload leaves the previous
   value in place -- does NOT overwrite it with None.
7. A missing `cost_band` / `cost_breakdown` dict in the parent
   payload leaves every dotted-path child alone (same "leave
   previous value" contract).
8. The staleness/availability contract matches the sibling
   _NimbusSolverPushSensor: True before the first push, True while
   fresh, False after _STALE_AFTER_SECONDS.
9. Category rule (by what it measures) is respected: monetary +
   energy + power + battery-health signals are PRIMARY (entity_category
   is None); LP internals + config echoes + shadow prices are
   DIAGNOSTIC.
10. The full fan-out routes through NimbusSolverBatteryForecastSensor.
    update_from_solver() when _flattened_entities is populated -- and
    is a no-op when it's empty (very-first solve race safety).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor, sensor_flattened
from custom_components.nimbus_load.const import DOMAIN


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


def test_every_spec_row_produces_one_entity():
    entities, _ = _build_entities()
    assert len(entities) == len(sensor_flattened.FLATTENED_ATTRS)


def test_entity_ids_are_unique():
    entities, _ = _build_entities()
    entity_ids = [e.entity_id for e in entities]
    assert len(set(entity_ids)) == len(entity_ids), (
        "duplicate entity_id in FLATTENED_ATTRS -- suffix collision"
    )


def test_unique_ids_are_unique():
    entities, _ = _build_entities()
    unique_ids = [e._attr_unique_id for e in entities]
    assert len(set(unique_ids)) == len(unique_ids), (
        "duplicate _attr_unique_id in FLATTENED_ATTRS -- suffix collision"
    )


def test_every_entity_id_uses_the_nimbus_solver_prefix():
    entities, _ = _build_entities()
    for e in entities:
        assert e.entity_id.startswith("sensor.nimbus_solver_"), e.entity_id


def test_every_unique_id_is_scoped_to_the_config_entry():
    entities, entry = _build_entities()
    for e in entities:
        # Prevents an accidental hardcoded unique_id that would clash on
        # a second install of Nimbus in the same HA (unlikely but real:
        # unit-test fixture reuse would catch this immediately).
        assert e._attr_unique_id.startswith(f"{entry.entry_id}_nimbus_solver_"), (
            e._attr_unique_id
        )


# --- device-info attachment ------------------------------------------------


def test_every_entity_attaches_to_the_hub_device():
    entities, entry = _build_entities()
    for e in entities:
        di = e._attr_device_info
        assert (DOMAIN, entry.entry_id) in di["identifiers"]
        assert di["name"] == "Nimbus"
        assert di["manufacturer"] == "Nimbus"
        assert di["model"] == "Hub"
        assert di["sw_version"] == "0.94.18"


# --- spec-to-entity attribute mapping --------------------------------------


def test_class_attributes_match_spec_for_every_row():
    entities, _ = _build_entities()
    spec_by_suffix = {s.entity_id_suffix: s for s in sensor_flattened.FLATTENED_ATTRS}
    for e in entities:
        suffix = e.entity_id.removeprefix("sensor.nimbus_solver_")
        spec = spec_by_suffix[suffix]
        assert e._attr_name == spec.name
        assert e._attr_entity_category is spec.entity_category
        assert e._attr_device_class is spec.device_class
        assert e._attr_state_class is spec.state_class
        assert e._attr_native_unit_of_measurement == spec.unit_of_measurement


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
    }


def test_dispatch_updates_every_entity_from_real_payload():
    entities, _ = _build_entities()
    payload = _real_looking_parent_payload()
    sensor_flattened.dispatch_to_flattened(entities, payload)
    by_id = {e.entity_id: e for e in entities}

    # Straight-lookup sanity spread across concept-groups
    assert by_id["sensor.nimbus_solver_total_cost"].native_value == 9.8685
    assert by_id["sensor.nimbus_solver_equivalent_full_cycles"].native_value == 3.239
    assert by_id["sensor.nimbus_solver_solve_seconds"].native_value == 1.45
    assert by_id["sensor.nimbus_solver_lp_status"].native_value == "optimal"

    # Dotted-path extraction (cost_band.*)
    assert by_id["sensor.nimbus_solver_cost_band_lower"].native_value == -4.4046
    assert by_id["sensor.nimbus_solver_cost_band_upper"].native_value == 17.4151
    assert by_id["sensor.nimbus_solver_cost_band_width"].native_value == 21.8198

    # Dotted-path extraction (cost_breakdown.*)
    assert (
        by_id["sensor.nimbus_solver_cost_breakdown_grid_net"].native_value == 0.4653
    )
    assert (
        by_id["sensor.nimbus_solver_cost_breakdown_degradation"].native_value == 7.7732
    )
    assert (
        by_id[
            "sensor.nimbus_solver_cost_breakdown_terminal_value_credit"
        ].native_value
        == -0.9611
    )


def test_dispatch_covers_every_entity_no_stragglers():
    """Every entity must actually receive a value from the real
    payload -- if a spec row's source_key is unknown to the payload
    fixture, this test catches it as a spec bug rather than silently
    letting a new entity ship with permanently-unavailable state.
    """
    entities, _ = _build_entities()
    payload = _real_looking_parent_payload()
    sensor_flattened.dispatch_to_flattened(entities, payload)
    for e in entities:
        assert e.native_value is not None, (
            f"{e.entity_id} did not extract a value from the real payload -- "
            f"FLATTENED_ATTRS spec has source_key={e._spec.source_key!r} that "
            "the parent payload fixture doesn't provide. Either the spec is wrong "
            "or the fixture is stale."
        )


def test_missing_key_leaves_previous_value_in_place():
    entities, _ = _build_entities()
    payload = _real_looking_parent_payload()
    sensor_flattened.dispatch_to_flattened(entities, payload)
    by_id = {e.entity_id: e for e in entities}
    total_cost = by_id["sensor.nimbus_solver_total_cost"]
    # A partial-solve payload that just omits total_cost. Value must NOT
    # be clobbered to None -- that would break the staleness contract
    # (the whole point of _SENTINEL_MISSING inside _extract).
    partial = {k: v for k, v in payload.items() if k != "total_cost"}
    sensor_flattened.dispatch_to_flattened(entities, partial)
    assert total_cost.native_value == 9.8685  # unchanged


def test_missing_dotted_parent_leaves_children_alone():
    entities, _ = _build_entities()
    payload = _real_looking_parent_payload()
    sensor_flattened.dispatch_to_flattened(entities, payload)
    by_id = {e.entity_id: e for e in entities}
    lower = by_id["sensor.nimbus_solver_cost_band_lower"]
    # Payload where cost_band isn't published at all -- children of
    # cost_band.* must NOT overwrite their existing values, matching
    # the same-key-missing behaviour above.
    partial = {k: v for k, v in payload.items() if k != "cost_band"}
    sensor_flattened.dispatch_to_flattened(entities, partial)
    assert lower.native_value == -4.4046  # unchanged


def test_present_but_none_is_a_real_value_not_a_missing_key():
    """A parent that publishes source_key=None (e.g. a signal was
    explicitly nulled out by the solver on a legitimate code path)
    must land as native_value=None on the child, NOT be dropped as
    "missing". Distinct behaviours; the sentinel exists precisely for
    this case.
    """
    entities, _ = _build_entities()
    payload = _real_looking_parent_payload()
    sensor_flattened.dispatch_to_flattened(entities, payload)
    by_id = {e.entity_id: e for e in entities}
    e = by_id["sensor.nimbus_solver_binding_constraint_now"]
    assert e.native_value is not None
    sensor_flattened.dispatch_to_flattened(entities, {"binding_constraint_now": None})
    assert e.native_value is None


# --- staleness / availability ---------------------------------------------


def test_available_is_true_before_first_push():
    entities, _ = _build_entities()
    for e in entities:
        assert e.available is True


def test_available_is_true_immediately_after_push():
    entities, _ = _build_entities()
    sensor_flattened.dispatch_to_flattened(entities, _real_looking_parent_payload())
    for e in entities:
        assert e.available is True


def test_available_flips_to_false_after_stale_threshold():
    entities, _ = _build_entities()
    sensor_flattened.dispatch_to_flattened(entities, _real_looking_parent_payload())
    # Simulate 6 minutes elapsed since the last push (threshold is 5min).
    for e in entities:
        e._last_updated = time.monotonic() - (6 * 60)
    for e in entities:
        assert e.available is False


# --- category rule (by what it measures) ----------------------------------


def test_costs_and_savings_are_primary_sensors():
    """Total cost + fixed-cost variant should NOT be DIAGNOSTIC -- these
    are the numbers a user genuinely cares about tracking day-to-day.
    """
    entities, _ = _build_entities()
    by_id = {e.entity_id: e for e in entities}
    assert by_id["sensor.nimbus_solver_total_cost"]._attr_entity_category is None
    assert (
        by_id[
            "sensor.nimbus_solver_total_cost_with_fixed_costs"
        ]._attr_entity_category
        is None
    )
    assert by_id["sensor.nimbus_solver_total_charge_kwh"]._attr_entity_category is None
    assert (
        by_id["sensor.nimbus_solver_total_discharge_kwh"]._attr_entity_category is None
    )
    assert (
        by_id["sensor.nimbus_solver_equivalent_full_cycles"]._attr_entity_category
        is None
    )


def test_lp_internals_and_shadow_prices_are_diagnostic():
    """Solve runtime, LP status, binding constraint, and shadow prices
    are debugging signals -- must be DIAGNOSTIC so a typical user's
    entity registry doesn't get polluted with LP internals.
    """
    from homeassistant.const import EntityCategory

    entities, _ = _build_entities()
    by_id = {e.entity_id: e for e in entities}
    for entity_id in (
        "sensor.nimbus_solver_solve_seconds",
        "sensor.nimbus_solver_lp_status",
        "sensor.nimbus_solver_binding_constraint_now",
        "sensor.nimbus_solver_binding_constraint_shadow_price",
        "sensor.nimbus_solver_energy_shadow_price_now",
        "sensor.nimbus_solver_p2p_volume_cap_shadow_price",
        "sensor.nimbus_solver_charge_efficiency",
        "sensor.nimbus_solver_discharge_efficiency",
        "sensor.nimbus_solver_degradation_cost_per_kwh",
        "sensor.nimbus_solver_risk_aversion",
        "sensor.nimbus_solver_salvage_value",
        "sensor.nimbus_solver_n_periods",
        "sensor.nimbus_solver_n_clamped_periods",
    ):
        assert (
            by_id[entity_id]._attr_entity_category is EntityCategory.DIAGNOSTIC
        ), f"{entity_id} should be DIAGNOSTIC per the by-what-it-measures rule"


# --- integration with the parent sensor's fan-out --------------------------


def test_parent_update_from_solver_fans_out_to_flattened_children():
    """The whole point of the module: a single push into the parent's
    update_from_solver must correctly update every flattened child in
    the same event-loop turn.
    """
    # Build a real parent instance with a real _flattened_entities list.
    entry = _fake_entry()
    parent = sensor.NimbusSolverBatteryForecastSensor.__new__(
        sensor.NimbusSolverBatteryForecastSensor
    )
    sensor.NimbusSolverBatteryForecastSensor.__init__(
        parent, entry, sw_version="0.94.18"
    )
    parent.hass = MagicMock()
    parent.async_write_ha_state = MagicMock()

    # Attach real flattened entities (same wiring async_setup_entry does).
    parent._flattened_entities = sensor_flattened.create_flattened_entities(
        entry, sw_version="0.94.18"
    )
    # Same hass=None convention as _build_entities() above -- the real
    # HA lifecycle attaches hass; the fan-out is guarded on `hass is
    # not None` so tests run without needing HA runtime.
    for e in parent._flattened_entities:
        e.hass = None

    parent.update_from_solver(-1.979, _real_looking_parent_payload())

    # Parent's own state landed.
    assert parent.native_value == -1.979

    # Every flattened child also updated in the same call.
    by_id = {e.entity_id: e for e in parent._flattened_entities}
    assert by_id["sensor.nimbus_solver_total_cost"].native_value == 9.8685
    assert by_id["sensor.nimbus_solver_cost_band_lower"].native_value == -4.4046
    assert by_id["sensor.nimbus_solver_lp_status"].native_value == "optimal"


def test_parent_update_is_safe_before_flattened_entities_are_wired():
    """Race safety: async_setup_entry constructs the parent first, then
    attaches _flattened_entities. If the very-first solve tick beats
    that attachment by microseconds, update_from_solver must not
    crash.
    """
    entry = _fake_entry()
    parent = sensor.NimbusSolverBatteryForecastSensor.__new__(
        sensor.NimbusSolverBatteryForecastSensor
    )
    sensor.NimbusSolverBatteryForecastSensor.__init__(
        parent, entry, sw_version="0.94.18"
    )
    parent.hass = MagicMock()
    parent.async_write_ha_state = MagicMock()

    # _flattened_entities defaults to [] -- no attachment yet.
    assert parent._flattened_entities == []

    # No crash.
    parent.update_from_solver(-1.979, _real_looking_parent_payload())
    assert parent.native_value == -1.979


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
