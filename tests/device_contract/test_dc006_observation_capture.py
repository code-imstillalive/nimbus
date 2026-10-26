"""DC-006 slice 2's equivalence gate (DC-000 addition 1, #1574): the
observation consumers' inputs on F10, in `legacy` and in `active`, as plain
values (`device_observation`). Synthetic states only.

These tests pin what the gate measures today, before any consumer moves:
each difference listed here must be approved (`approved_changes.json`) or
removed before the Forecaster reads the contract."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import device_observation as do

from tests.device_contract.test_dc004_views_and_preview import F10, di

NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def _st(value, unit):
    when = NOW.isoformat()
    attrs = {} if unit is None else {"unit_of_measurement": unit}
    return {
        "state": value,
        "attributes": attrs,
        "last_changed": when,
        "last_updated": when,
        "last_reported": when,
    }


def _gate(states):
    doc = di.import_legacy(copy.deepcopy(F10)).document

    def read(entity_id):
        return states.get(entity_id)

    legacy = do.legacy_inputs(F10["options"], read, "load", "sensor.pool_power")
    contract = do.contract_inputs(doc, read, NOW)
    return legacy, contract, do.differences(legacy, contract)


NORMAL = {
    "sensor.meter_power": _st("1500", "W"),
    "sensor.bat_power": _st("2.0", "kW"),
    "sensor.pv_power": _st("3000", "W"),
}


def test_both_sides_read_the_same_entities_and_units():
    legacy, contract, _diff = _gate(NORMAL)
    for name in do.OBSERVATION_INPUTS:
        assert legacy[name]["entity_id"] == contract[name]["entity_id"]
    assert legacy["grid_power"]["value"] == contract["grid_power"]["value"] == 1.5
    assert legacy["solar_power"]["value"] == contract["solar_power"]["value"] == 3.0


def test_the_battery_sign_is_the_one_difference_on_a_healthy_site():
    """F10's battery sensor is positive when charging
    (`solver_battery_power_positive_is_charge`). The Forecaster reads it as it
    is; the contract turns it to its canonical discharge-positive sign. DC-006
    expects this difference ("sign from `normalization`"). It is also a feature
    the Forecaster's models were trained on, so approving it is a decision
    about the models, not only the reading (#1574, 20 Oct)."""
    _legacy, _contract, diff = _gate(NORMAL)
    assert diff == [
        {"input": "battery_power", "field": "value", "legacy": 2.0, "contract": -2.0}
    ]


def test_the_legacy_side_matches_the_forecasters_own_reader():
    """`legacy_value` follows `coordinator._current_measured_power`: 0.0 for
    nothing to read, a non-number or a non-finite value; kW passed through;
    other power units converted; a non-power unit left as it is."""
    assert do.legacy_value(None) == 0.0
    assert do.legacy_value(_st("abc", "W")) == 0.0
    assert do.legacy_value(_st("nan", "W")) == 0.0
    assert do.legacy_value(_st("2.5", "kW")) == 2.5
    assert do.legacy_value(_st("2500", "W")) == 2.5
    assert do.legacy_value(_st("2.5", "A")) == 2.5
    assert do.legacy_value(_st("2.5", None)) == 2.5


# --- Mark Purcell's two decisions on #1574 (21 Oct 2026) ----------------------


def test_a_missing_reading_is_held_up_to_the_bindings_max_age_then_absent():
    """Decision 2: never a fabricated 0. The last good reading is carried as
    `held_stale` within the binding's max age (DC-002's default here: F10
    declares none), then the input is `absent`."""
    from datetime import timedelta

    doc = di.import_legacy(copy.deepcopy(F10)).document
    hold = do.ObservationHold()
    first = do.contract_inputs(doc, NORMAL.get, NOW, hold)
    assert first["grid_power"] == {
        "entity_id": "sensor.meter_power",
        "value": 1.5,
        "reason": None,
        "state": "valid",
    }
    down = {**NORMAL, "sensor.meter_power": _st("unavailable", "W")}
    age = do.max_age_s({})
    soon = do.contract_inputs(doc, down.get, NOW + timedelta(seconds=age), hold)
    assert (soon["grid_power"]["value"], soon["grid_power"]["state"]) == (
        1.5,
        "held_stale",
    )
    late = do.contract_inputs(doc, down.get, NOW + timedelta(seconds=age + 1), hold)
    assert (late["grid_power"]["value"], late["grid_power"]["state"]) == (
        None,
        "absent",
    )


def test_the_max_age_is_the_bindings_own_when_it_declares_one():
    declared = {
        "normalization": {"freshness": {"mode": "sampled", "max_interval_s": 90}}
    }
    assert do.max_age_s(declared) == 90.0
    assert do.max_age_s({}) == do.db.DEFAULT_MAX_AGE_S


def test_each_model_is_fed_the_sign_it_was_trained_with():
    """Decision 1: the contract's battery power is discharge-positive (-2.0
    for F10's charge-positive sensor at 2.0 kW); a model trained on the
    sensor's own sign keeps getting +2.0 until it retrains."""
    doc = di.import_legacy(copy.deepcopy(F10)).document
    contract = do.contract_inputs(doc, NORMAL.get, NOW)
    canonical = contract["battery_power"]["value"]
    assert canonical == -2.0
    assert do.feature_value(doc, "battery_power", canonical, do.TRAINED_NATIVE) == 2.0
    assert (
        do.feature_value(doc, "battery_power", canonical, do.TRAINED_CANONICAL) == -2.0
    )
    # Inputs whose sign the contract does not change are passed through.
    assert do.feature_value(doc, "grid_power", 1.5, do.TRAINED_NATIVE) == 1.5
    assert do.feature_value(doc, "battery_power", None, do.TRAINED_NATIVE) is None


def test_both_decisions_are_in_the_approved_changes_ledger():
    import json
    from pathlib import Path

    ledger = json.loads(
        (Path(__file__).parent / "approved_changes.json").read_text(encoding="utf-8")
    )
    fields = {c["field"] for c in ledger["changes"] if c["fixture"] == "F10"}
    assert {"forecaster_battery_feature", "forecaster_observation_value"} <= fields


# --- Mark Purcell's #1947 finding: the Forecaster's own filters -----------------


def _legacy(states, subentry_type="load", target="sensor.pool_power", **options):
    return do.legacy_inputs(
        {**F10["options"], **options}, states.get, subentry_type, target
    )


def test_a_power_signal_reads_no_observation_input_at_all():
    """`coordinator._grid_sensor` and its siblings return None for any power
    signal subentry: every input is absent, read as 0.0 kW."""
    got = _legacy(NORMAL, subentry_type=do.SUBENTRY_TYPE_SIGNAL)
    assert all(v == {"entity_id": None, "value": 0.0} for v in got.values())


def test_a_model_is_never_fed_its_own_target():
    """#1540: the grid sensor that is this load model's own target is absent."""
    got = _legacy(NORMAL, target="sensor.meter_power")
    assert got["grid_power"] == {"entity_id": None, "value": 0.0}
    assert got["solar_power"]["entity_id"] == "sensor.pv_power"


def test_an_energy_unit_sensor_is_left_out():
    """#1562: a sensor reporting an energy unit is not read as power."""
    states = {**NORMAL, "sensor.pv_power": _st("12.5", "kWh")}
    got = _legacy(states)
    assert got["solar_power"] == {"entity_id": None, "value": 0.0}
    assert got["grid_power"]["value"] == 1.5
