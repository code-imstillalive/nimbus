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
import pytest

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

    legacy = do.legacy_inputs(F10["options"], read)
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


def test_an_unavailable_sensor_is_zero_today_and_missing_in_the_contract():
    """Not in DC-006's expected list: today an unavailable reading is fed to
    the Forecaster as 0.0 kW; the contract says missing (DC-R04: missing is
    never zero). Needs a decision before the slice merges."""
    states = {**NORMAL, "sensor.meter_power": _st("unavailable", "W")}
    _legacy, contract, diff = _gate(states)
    assert contract["grid_power"]["reason"] == "unavailable"
    assert {
        "input": "grid_power",
        "field": "value",
        "legacy": 0.0,
        "contract": None,
    } in diff


@pytest.mark.parametrize(
    ("unit", "reason"), [(None, "unit_missing"), ("kw", "unit_not_exact")]
)
def test_a_unitless_or_misspelt_sensor_is_read_today_and_refused_in_the_contract(
    unit, reason
):
    """The F09 divergences, decided by the household on #1893: refused until
    the household declares the unit in its Repair."""
    states = {**NORMAL, "sensor.meter_power": _st("1.5", unit)}
    _legacy, contract, diff = _gate(states)
    assert contract["grid_power"]["reason"] == reason
    assert {
        "input": "grid_power",
        "field": "value",
        "legacy": 1.5,
        "contract": None,
    } in diff


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
