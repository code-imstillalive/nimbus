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


def test_a_unitless_or_misspelt_sensor_is_read_today_and_refused_in_the_contract():
    """The F09 divergences, decided by the household on #1893: refused until
    the household declares the unit in its Repair."""
    for unit, reason in [(None, "unit_missing"), ("kw", "unit_not_exact")]:
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


# --- Mark Purcell's two decisions on #1574 (21 Oct 2026) ----------------------


def test_the_max_age_is_the_bindings_own_when_it_declares_one():
    declared = {
        "normalization": {"freshness": {"mode": "sampled", "max_interval_s": 90}}
    }
    assert do.max_age_s(declared) == 90.0
    assert do.max_age_s({}) == do.db.DEFAULT_MAX_AGE_S


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


def test_an_energy_unit_sensor_is_left_out():
    """#1562: a sensor reporting an energy unit is not read as power."""
    states = {**NORMAL, "sensor.pv_power": _st("12.5", "kWh")}
    got = _legacy(states)
    assert got["solar_power"] == {"entity_id": None, "value": 0.0}
    assert got["grid_power"]["value"] == 1.5
