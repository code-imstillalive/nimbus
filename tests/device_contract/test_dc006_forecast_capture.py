"""DC-006 slice 3's equivalence gate (DC-000 addition 1, #1574): the Solver's
solar forecast as today's `build_solar_arrays` reads it, against the same
members resolved through the contract (`device_forecasts`), on identical
synthetic states.

Both sides stop at what is read: the running-low gate (#1784) and the live
anchor transform the forecast afterwards and are the consumer's, so the
legacy side runs with no live sensor and the gate switched off. Each
difference listed here must be approved (`approved_changes.json`) or removed
before the Solver reads its solar forecast through the contract."""

from __future__ import annotations

import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import device_contract as dc
import device_forecasts as dfc
import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))
import _solver_path  # noqa: F401
import solver_writer
from solver_inputs import solar

NOW = datetime(2026, 10, 22, 0, 0, tzinfo=UTC)
GRID = [NOW + timedelta(minutes=30 * i) for i in range(8)]
#: The near-term grid: 5-minute periods, where Open-Meteo's instants are
#: read at each period's midpoint (#2004), unlike a held value.
FINE = [NOW + timedelta(minutes=5 * i) for i in range(30)]


def _t(i):
    return (NOW + timedelta(minutes=30 * i)).isoformat()


def generic(values, bounds=True):
    rows = []
    for i, v in enumerate(values):
        row = {"time": _t(i), "value": v}
        if bounds:
            row |= {"lower": v * 0.8, "upper": v * 1.2}
        rows.append(row)
    return {"forecast": rows}


def solcast(values, start=0):
    return {
        "detailedForecast": [
            {
                "period_start": _t(start + i),
                "pv_estimate": v,
                "pv_estimate10": v * 0.7,
                "pv_estimate90": v * 1.3,
            }
            for i, v in enumerate(values)
        ]
    }


def meteo(values, start=0):
    return {"watts": {_t(start + i): v * 1000.0 for i, v in enumerate(values)}}


KNOWN = {
    "solcast": ["sensor.sc_today", "sensor.sc_tomorrow"],
    "open_meteo": ["sensor.om_today", "sensor.om_tomorrow"],
}

STATES = {
    "sensor.nimbus_solar_forecast": generic([0.0, 1.0, 2.5, 4.0, 4.5, 3.0, 1.0, -0.1]),
    "sensor.sc_today": solcast([0.2, 1.4, 2.8]),
    "sensor.sc_tomorrow": solcast([4.1, 4.4, 2.6, 1.2, 0.1], start=3),
    "sensor.om_today": meteo([0.1, 1.1, 2.2, 3.9]),
    "sensor.om_tomorrow": meteo([4.8, 3.3, 0.9, 0.0], start=4),
}


def _legacy(options, auto_include, states, grid=GRID):
    """Today's `build_solar_arrays`, read only: no live anchor, no gate."""
    cfg = {
        **options,
        "known_sources": KNOWN,
        "solver_auto_include_known_solar": auto_include,
    }

    def ha_get(entity_id):
        if entity_id not in states:
            raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)
        return {"attributes": states[entity_id]}

    with (
        patch.object(solver_writer, "entity_exists", lambda e: e in states),
        patch.object(solver_writer, "ha_get", ha_get),
        patch.object(solar, "_apply_running_low_gate", lambda *a, **k: None),
    ):
        return solar.build_solar_arrays(cfg, grid, len(grid), now=NOW)


def _contract(options, auto_include, states, grid=GRID):
    binding = dfc.legacy_solar_binding(options, auto_include, KNOWN)
    return dfc.resolve_forecast(
        binding,
        states.get,
        grid,
        solver_writer.resample_forecast,
        resample_instants=solver_writer.resample_instants,
    )


def _same(legacy, contract):
    value, lower, upper = legacy
    assert contract.state == "ok"
    for got, want in (
        (contract.value, value),
        (contract.lower, lower),
        (contract.upper, upper),
    ):
        assert list(got) == pytest.approx(list(want), abs=1e-12)


CASES = {
    "one configured Nimbus forecast": (
        {"solver_solar_forecast_sensor": "sensor.nimbus_solar_forecast"},
        False,
    ),
    "auto-include only": ({}, True),
    "configured plus auto-include": (
        {"solver_solar_forecast_sensor": "sensor.nimbus_solar_forecast"},
        True,
    ),
    "Solcast configured, auto-include on (#546 dedup)": (
        {
            "solver_solar_forecast_sensor": "sensor.sc_today",
            "solver_solar_forecast_sensor_2": "sensor.nimbus_solar_forecast",
        },
        True,
    ),
    "Solcast configured, auto-include off": (
        {"solver_solar_forecast_sensor": "sensor.sc_tomorrow"},
        False,
    ),
    "Open-Meteo configured, auto-include off": (
        {"solver_solar_forecast_sensor_3": "sensor.om_today"},
        False,
    ),
}


@pytest.mark.parametrize("grid", [GRID, FINE], ids=["30-minute", "5-minute"])
@pytest.mark.parametrize("case", list(CASES))
def test_the_contract_reads_what_today_reads(case, grid):
    options, auto_include = CASES[case]
    _same(
        _legacy(options, auto_include, STATES, grid),
        _contract(options, auto_include, STATES, grid),
    )


def test_open_meteo_is_placed_where_the_solver_places_it():
    """#2004's offset lives in two places; they must agree."""
    assert dfc.OPEN_METEO_WATTS_LEAD == solver_writer.OPEN_METEO_WATTS_LEAD


def test_the_members_are_today_s_members_in_today_s_order():
    options = {"solver_solar_forecast_sensor": "sensor.nimbus_solar_forecast"}
    forecast = _contract(options, True, STATES)
    assert [(m.adapter, m.entity_ids, m.state) for m in forecast.members] == [
        ("nimbus_forecast", ("sensor.nimbus_solar_forecast",), "ok"),
        ("open_meteo_solar", ("sensor.om_today", "sensor.om_tomorrow"), "ok"),
        ("solcast", ("sensor.sc_today", "sensor.sc_tomorrow"), "ok"),
    ]


def test_a_failed_member_is_dropped_on_both_sides_never_zero():
    options = {
        "solver_solar_forecast_sensor": "sensor.nimbus_solar_forecast",
        "solver_solar_forecast_sensor_2": "sensor.gone",
    }
    _same(_legacy(options, True, STATES), _contract(options, True, STATES))
    gone = _contract(options, True, STATES).members[-1]
    assert (gone.state, gone.reason, gone.value) == ("missing", "unavailable", None)


def test_a_missing_anchor_drops_the_whole_integration_as_today():
    states = {k: v for k, v in STATES.items() if k != "sensor.sc_today"}
    _same(_legacy({}, True, states), _contract({}, True, states))
    reasons = [m.reason for m in _contract({}, True, states).members]
    assert reasons == [None, "unavailable"]


# --- differences the gate finds: each needs a decision before the slice merges ---


def test_difference_no_source_is_zero_today_and_missing_in_the_contract():
    """Today, with no member, the Solver solves on a flat 0.0 kW solar and
    logs a warning; the contract says missing (DC-R04: missing is never
    zero). The Solver's own fallback stays the consumer's to decide."""
    options = {"solver_solar_forecast_sensor": "sensor.gone"}
    value, _lower, _upper = _legacy(options, False, STATES)
    assert value == [0.0] * len(GRID)
    forecast = _contract(options, False, STATES)
    assert (forecast.state, forecast.reason, forecast.value) == (
        "missing",
        "no_member_resolved",
        None,
    )


# --- the binding itself (DC-001) --------------------------------------------------


def _doc_with(binding, kind="solar", role="generation_forecast"):
    return {
        "schema_version": 1,
        "site": {"currency": "AUD", "timezone": "Australia/Brisbane"},
        "devices": [
            {"id": "solar", "kind": kind, "name": "Solar", "bindings": {role: binding}}
        ],
        "relationships": [],
    }


def _codes(binding, **kw):
    return {p.code for p in dc.validate(_doc_with(binding, **kw))}


def test_today_s_binding_validates():
    binding = dfc.legacy_solar_binding(
        {"solver_solar_forecast_sensor": "sensor.nimbus_solar_forecast"}, True, KNOWN
    )
    assert binding is not None and len(binding["sources"]) == 3
    assert not {
        "source_fields_missing",
        "combine_unknown",
        "combine_not_allowed",
    } & _codes(binding)


def test_continued_by_belongs_to_an_entity_attribute_source():
    binding = {
        "source": {
            "kind": "entity_state",
            "entity_id": "sensor.a",
            "continued_by": ["sensor.b"],
        }
    }
    assert "source_fields_missing" in _codes(binding)


def test_one_source_stays_the_ordinary_binding():
    binding = dfc.legacy_solar_binding(
        {"solver_solar_forecast_sensor": "sensor.nimbus_solar_forecast"}, False, KNOWN
    )
    assert set(binding) == {"source"}
    assert dfc.legacy_solar_binding({}, False, KNOWN) is None
    assert (
        dfc.resolve_forecast(
            None,
            STATES.get,
            GRID,
            solver_writer.resample_forecast,
            resample_instants=solver_writer.resample_instants,
        ).reason
        == "unbound"
    )
