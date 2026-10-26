"""The device resolver (#1574): read what the Energy Dashboard already
states, and where it names no power sensor, find one by matching integrated
power history against the energy counter -- proposing, never deciding, and
saying so when the evidence cannot tell candidates or signs apart (#1837).

Synthetic histories only. Covers what lived in #1574, #1590 (entity order
never decides) and the device-resolver files.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import device_resolver as dr
import pytest

T0 = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
END = T0 + timedelta(hours=12)


def _power(fn, minutes=5, start=T0, end=END):
    """A power sensor's history, one reading every `minutes`."""
    n = int((end - start).total_seconds() // 60 // minutes)
    return [
        (start + timedelta(minutes=minutes * i), fn(i * minutes / 60)) for i in range(n)
    ]


def _counter(fn, start=T0, end=END, base=1000.0):
    """An energy counter integrating `fn`, read every 5 minutes."""
    out, total, t = [], base, start
    step = timedelta(minutes=5)
    while t <= end:
        out.append((t, total))
        total += fn((t - start).total_seconds() / 3600) * step.total_seconds() / 3600
        t += step
    return out


def solar(h):
    return max(0.0, 4.0 * math.sin(math.pi * h / 12))


def flat(h):
    return 2.0


# --- what the Energy Dashboard states ------------------------------------------------------


def test_each_kind_of_energy_source_is_read_with_its_own_fields():
    prefs = {
        "energy_sources": [
            {
                "type": "solar",
                "stat_energy_from": "sensor.pv_kwh",
                "stat_rate": "sensor.pv_w",
                "config_entry_solar_forecast": ["fc1", ""],
            },
            {
                "type": "battery",
                "name": "Home",
                "stat_energy_from": "sensor.out",
                "stat_energy_to": "sensor.in",
                "stat_soc": "sensor.soc",
                "power_config": {"stat_rate_inverted": "sensor.b"},
            },
            {
                "type": "grid",
                "stat_energy_from": "sensor.imp",
                "stat_energy_to": "sensor.exp",
                "entity_energy_price": "sensor.buy",
                "entity_energy_price_export": "sensor.sell",
            },
            {
                "type": "grid",
                "flow_from": [
                    {"stat_energy_from": "sensor.a", "entity_energy_price": "sensor.p"},
                    "junk",
                ],
                "flow_to": [{"stat_energy_to": "sensor.b"}],
                "power": [{"stat_rate": "sensor.g", "power_config": {"x": 1}}],
            },
            {"type": "gas"},
            "junk",
        ]
    }
    solar_src, battery, grid, legacy = dr.parse_energy_sources(prefs)
    assert (solar_src.kind, solar_src.rate, solar_src.forecast_entries) == (
        "solar",
        ("sensor.pv_w",),
        ("fc1",),
    )
    assert (battery.energy_from, battery.energy_to, battery.soc, battery.name) == (
        ("sensor.out",),
        ("sensor.in",),
        "sensor.soc",
        "Home",
    )
    assert battery.power_config == ({"stat_rate_inverted": "sensor.b"},)
    assert (grid.schema, grid.price_import, grid.price_export, grid.index) == (
        "unified",
        ("sensor.buy",),
        ("sensor.sell",),
        2,
    )
    assert (legacy.schema, legacy.energy_from, legacy.energy_to, legacy.rate) == (
        "legacy",
        ("sensor.a",),
        ("sensor.b",),
        ("sensor.g",),
    )
    assert legacy.power_config == ({"x": 1},)
    assert dr.parse_energy_sources(None) == []


def test_individual_devices_keep_what_contains_them():
    prefs = {
        "device_consumption": [
            {
                "stat_consumption": "sensor.pool",
                "included_in_stat": "sensor.house",
                "name": "",
            },
            {"stat_consumption": 5},
            {"stat_consumption": "sensor.ev", "stat_rate": "sensor.ev_w"},
        ]
    }
    pool, ev = dr.device_consumption(prefs)
    assert (pool.included_in, pool.name, pool.index) == ("sensor.house", None, 0)
    assert (ev.rate, ev.index) == ("sensor.ev_w", 2)
    assert dr.device_consumption_entities(prefs) == ["sensor.pool", "sensor.ev"]
    assert dr.entities_on_device(
        [
            {"device_id": "d", "entity_id": "sensor.a"},
            {"device_id": "d"},
            {"device_id": "x", "entity_id": "sensor.b"},
        ],
        "d",
    ) == ["sensor.a"]


# --- integrating history --------------------------------------------------------------------


def test_power_integrates_into_the_energy_it_delivered_split_by_direction():
    series = [
        (T0, 2.0),
        (T0 + timedelta(hours=1), -1.0),
        (T0 + timedelta(hours=2), "unavailable"),
    ]
    assert dr.integrate_parts(series, T0, T0 + timedelta(hours=3)) == (2.0, 1.0)
    assert dr.coverage(series, T0, T0 + timedelta(hours=3)) == pytest.approx(2 / 3)
    assert dr.coverage(series, T0, T0) == 0.0
    # The reading in force when the window opens counts from the window start.
    assert dr.integrate_parts(
        series, T0 + timedelta(minutes=30), T0 + timedelta(hours=1)
    ) == (1.0, 0.0)


@pytest.mark.parametrize(
    ("series", "rise"),
    [
        (
            [
                (T0, 10.0),
                (T0 + timedelta(minutes=30), 11.0),
                (T0 + timedelta(hours=1), 12.5),
            ],
            2.5,
        ),
        ([(T0 - timedelta(minutes=5), 10.0), (T0 + timedelta(minutes=30), 11.0)], 1.0),
        (
            [
                (T0, 10.0),
                (T0 + timedelta(minutes=30), 0.5),
                (T0 + timedelta(hours=1), 1.0),
            ],
            1.0,
        ),  # after a reset, from zero
        (
            [
                (T0 - timedelta(minutes=5), "unknown"),
                (T0 + timedelta(minutes=30), 11.0),
            ],
            None,
        ),
        ([(T0 + timedelta(minutes=30), 11.0)], None),
        (
            [
                (T0, 10.0),
                (T0 + timedelta(minutes=30), "x"),
                (T0 + timedelta(hours=1), 12.0),
            ],
            None,
        ),
    ],
)
def test_an_energy_counter_rises_by_what_was_delivered(series, rise):
    assert dr.counter_rise(series, T0, T0 + timedelta(hours=1)) == rise


# --- matching a power sensor to an energy counter ---------------------------------------------


def test_a_sensor_reporting_the_other_sign_is_matched_with_its_sign_flipped():
    candidates = {"sensor.inverted": _power(lambda h: -solar(h))}
    match = dr.match_power_sensor(candidates, T0, END, energy_out=_counter(solar))
    assert (match.status, match.sign) == (dr.CANDIDATE, -1)


@pytest.mark.parametrize(
    ("candidates", "energy_out", "status", "says"),
    [
        ({}, 5.0, dr.NO_CANDIDATES, "no power sensors"),
        ({"sensor.x": [(T0 + timedelta(hours=11), 1.0)]}, 5.0, dr.INSUFFICIENT, "90%"),
        ({"sensor.x": _power(flat)}, None, dr.INSUFFICIENT, "no energy counter"),
        ({"sensor.x": _power(flat)}, 0.1, dr.INSUFFICIENT, "barely moved"),
        ({"sensor.x": _power(flat)}, 500.0, dr.NO_MATCH, "off"),
        (
            {"sensor.x": _power(flat)},
            _counter(lambda h: 0.01),
            dr.INSUFFICIENT,
            "moved only",
        ),
        (
            {"sensor.x": _power(flat)},
            [
                (T0, 1.0),
                (T0 + timedelta(hours=1), 2.0),
                (T0 + timedelta(hours=1, minutes=5), "unavailable"),
            ],
            dr.INSUFFICIENT,
            "readable in only",
        ),
    ],
)
def test_without_enough_evidence_nothing_is_proposed_and_the_reason_is_given(
    candidates, energy_out, status, says
):
    match = dr.match_power_sensor(candidates, T0, END, energy_out=energy_out)
    assert match.status == status and match.entity_id is None
    assert says in match.reason


def test_a_total_alone_can_match_and_solar_never_runs_backwards():
    total = sum(p for p, _n in [dr.integrate_parts(_power(solar), T0, END)])
    match = dr.match_power_sensor(
        {"sensor.pv_w": _power(solar)}, T0, END, energy_out=total
    )
    assert match.status == dr.CANDIDATE
    two_way = {"sensor.mixed": _power(lambda h: 4.0 if h < 6 else -4.0)}
    assert (
        dr.match_power_sensor(two_way, T0, END, energy_out=24.0).status == dr.NO_MATCH
    )


# --- the site's energy balance -------------------------------------------------------------------


def _site(n=24):
    load = [2.0 + (i % 3) for i in range(n)]
    sol = [3.0 if 6 <= i < 18 else 0.0 for i in range(n)]
    bat = [0.5 if i % 2 else -0.5 for i in range(n)]
    grid = [load[i] - sol[i] - bat[i] for i in range(n)]
    return load, sol, bat, grid


def test_a_balanced_site_agrees():
    v = dr.check_energy_balance(*_site())
    assert (v.ok, v.explanation) == (True, "agree") and v.residual_kw == pytest.approx(
        0.0
    )


@pytest.mark.parametrize(
    ("change", "explanation"),
    [
        (
            lambda load, sol, bat, grid: (load, sol, [-b for b in bat], grid),
            "flip:battery",
        ),
        (
            lambda load, sol, bat, grid: (load, sol, bat, [g * 1000 for g in grid]),
            "scale:grid:/1000",
        ),
    ],
)
def test_one_change_that_closes_the_balance_is_named(change, explanation):
    v = dr.check_energy_balance(*change(*_site()))
    assert v.ok is False and explanation in v.hypotheses


def test_a_partial_solar_sensor_is_recognised():
    load, sol, bat, grid = _site()
    v = dr.check_energy_balance(load, [s * 0.5 for s in sol], bat, grid)
    assert "partial_solar" in v.hypotheses


@pytest.mark.parametrize(
    "series",
    [
        ([1.0] * 5, [0.0] * 5, [0.0] * 5, [1.0] * 5),  # too few samples
        ([1.0] * 24, [0.0] * 23, [0.0] * 24, [1.0] * 24),  # not aligned
        ([1.0] * 23 + [float("nan")], [0.0] * 24, [0.0] * 24, [1.0] * 24),
    ],
)
def test_too_little_or_misaligned_data_is_insufficient(series):
    assert dr.check_energy_balance(*series).explanation == dr.INSUFFICIENT


def test_a_balance_nothing_explains_says_so():
    load, sol, bat, grid = _site()
    v = dr.check_energy_balance(
        load, sol, bat, [g + 7.0 * ((i % 5) - 2) for i, g in enumerate(grid)]
    )
    assert v.explanation in ("unexplained", dr.AMBIGUOUS) and not v.ok
