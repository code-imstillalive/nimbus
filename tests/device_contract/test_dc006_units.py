"""DC-006 observation slice (#1574): the power sensors whose unit the
household must declare, and the declaration itself (`device_units`).

Household, 19 Oct 2026, on #1893: a unitless or misspelt power sensor is
refused by the contract; the household declares W or kW in a Repair, or
fixes the sensor, and `active` waits until every one is settled."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import device_bindings as db
import device_contract as dc
import device_units as du
import pytest


def _doc(**grid_norm):
    power = {
        "source": {"kind": "entity_state", "entity_id": "sensor.grid"},
        "provenance": {"origin": "manual", "confirmed": True, "overridden": False},
    }
    if grid_norm:
        power["normalization"] = grid_norm
    return {
        "schema_version": 1,
        "site": {
            "id": "site_home",
            "timezone": "Australia/Brisbane",
            "currency": "AUD",
        },
        "devices": [
            {
                "id": "grid",
                "kind": "grid",
                "boundary": {"id": "grid_boundary", "domain": "ac", "phases": "all"},
                "bindings": {"power": power},
            }
        ],
    }


NOW = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)


def _reader(unit=None, state="1.5"):
    attrs = {} if unit is None else {"unit_of_measurement": unit}
    when = NOW.isoformat()
    return lambda _eid: {
        "state": state,
        "attributes": attrs,
        "last_changed": when,
        "last_updated": when,
        "last_reported": when,
    }


@pytest.mark.parametrize(
    ("unit", "reason"),
    [(None, "unit_missing"), ("", "unit_missing"), ("kw", "unit_not_exact")],
)
def test_a_unitless_or_misspelt_power_sensor_needs_a_declaration(unit, reason):
    got = du.undeclared(_doc(), _reader(unit))
    assert got == [
        {
            "device_id": "grid",
            "role": "power",
            "entity_id": "sensor.grid",
            "stated_unit": unit,
            "reason": reason,
        }
    ]


@pytest.mark.parametrize("unit", ["W", "kW", "A", "kWh"])
def test_an_exact_unit_or_another_dimension_needs_none(unit):
    """An exact unit is fine; A or kWh is a wrong sensor, which no declaration
    can settle (it is refused, and readiness says why)."""
    assert du.undeclared(_doc(), _reader(unit)) == []


@pytest.mark.parametrize("state", ["unavailable", "unknown", ""])
def test_an_unreadable_sensor_is_left_for_the_next_check(state):
    assert du.undeclared(_doc(), _reader(None, state)) == []
    assert du.undeclared(_doc(), lambda _e: None) == []


def test_a_declared_unit_settles_it_and_resolves():
    doc = du.declare(_doc(), "grid", "power", "W")
    assert doc is not None
    binding = doc["devices"][0]["bindings"]["power"]
    assert binding["normalization"]["native_unit"] == "W"
    assert binding["provenance"]["overridden"] is True
    assert list(dc.validate(doc)) == []
    assert du.undeclared(doc, _reader(None)) == []
    got = db.resolve(doc, "grid", "power", db.Readers(state=_reader(None, "1500")), NOW)
    assert got.value == pytest.approx(1.5) and got.provenance["unit_declared"] == "W"


def test_declare_leaves_the_input_alone_and_refuses_what_it_cannot_do():
    original = _doc()
    before = copy.deepcopy(original)
    assert du.declare(original, "grid", "power", "kW") is not None
    assert original == before
    assert du.declare(original, "grid", "power", "kw") is None
    assert du.declare(original, "grid", "power", "MW") is None
    assert du.declare(original, "battery", "power", "kW") is None
    assert du.declare(original, "grid", "soc", "kW") is None
    # A non-power role is refused even when that binding exists.
    with_energy = copy.deepcopy(original)
    grid = with_energy["devices"][0]["bindings"]
    grid["import_energy"] = copy.deepcopy(grid["power"])
    assert du.declare(with_energy, "grid", "import_energy", "kW") is None


def test_a_declaration_survives_a_re_import_of_the_same_sensor():
    """Moving to `active` re-imports the document from today's settings."""
    declared = du.declare(_doc(), "grid", "power", "kW")
    carried = du.carry_declared(declared, _doc())
    assert carried["devices"][0]["bindings"]["power"]["normalization"] == {
        "native_unit": "kW"
    }
    # A different sensor in the same role starts again.
    other = _doc()
    other["devices"][0]["bindings"]["power"]["source"]["entity_id"] = "sensor.meter2"
    carried = du.carry_declared(declared, other)
    assert "normalization" not in carried["devices"][0]["bindings"]["power"]
    # Nothing to carry from no document.
    assert du.carry_declared(None, _doc()) == _doc()
