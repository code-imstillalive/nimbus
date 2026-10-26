"""DC-006 observation slice (#1574): the power sensors whose unit the
household must declare, and the declaration itself (`device_units`).

Household, 19 Oct 2026, on #1893: a unitless or misspelt power sensor is
refused by the contract; the household declares W or kW in a Repair, or
fixes the sensor, and `active` waits until every one is settled."""

from __future__ import annotations

import copy
from datetime import UTC, datetime

import device_units as du


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


def test_an_exact_unit_or_another_dimension_needs_none():
    """An exact unit is fine; A or kWh is a wrong sensor, which no declaration
    can settle (it is refused, and readiness says why)."""
    for unit in ["W", "kW", "A", "kWh"]:
        assert du.undeclared(_doc(), _reader(unit)) == []


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
