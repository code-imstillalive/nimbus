"""nimbus #1574: the device resolver behind the two-step setup.

Validated by hand on the reference household's real history (2026-10-03..05,
read-only): among the 247 power sensors on its Modbus device, the resolver
picked each inverter's battery power sensor (sign correct, 1.0-2.6% error,
runner-up 12-48% off) and inverter 1's PV power (1.0-1.9%), and asked rather
than guessed where two sensors are the same reading. These tests reproduce the
traps that check exposed, synthetically, plus every case in Mark's review of
#1590: an exact tie, missing evidence, an idle series, sources and devices kept
as the Energy Dashboard states them, and ambiguous balance explanations.
"""

from __future__ import annotations

import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.append(
    str(Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load")
)
import device_resolver as dr

START = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)
END = START + timedelta(days=1)


def _series(fn, minutes=1440, step=5):
    """kW series from fn(hour_of_day) at `step`-minute changes."""
    return [
        (START + timedelta(minutes=m), fn(m / 60.0)) for m in range(0, minutes, step)
    ]


def _counter_from(series, part=+1):
    """A daily energy counter that genuinely follows `series` (kWh)."""
    total, out = 0.0, []
    for (t, v), (t2, _) in zip(series, [*series[1:], (END, 0.0)], strict=False):
        out.append((t, round(total, 4)))
        x = v if part > 0 else -v
        if x > 0:
            total += x * (t2 - t).total_seconds() / 3600.0
    out.append((END - timedelta(seconds=1), round(total, 4)))
    return out


def solar(h):
    return max(0.0, 8.0 * (1 - abs(h - 12) / 6)) if 6 <= h <= 18 else 0.0


def house(h):
    return 2.0 + (1.5 if 17 <= h <= 22 else 0.0)


def battery(h):  # +discharge evening, -charge midday
    return 6.0 if 17 <= h < 23 else (-5.0 if 9 <= h < 14 else 0.0)


# --- Energy Dashboard preferences ------------------------------------------------


def test_parses_the_unified_schema_with_power_soc_and_names():
    """HA 2026.3+: one flat grid source per connection; stat_rate is HA's own
    power statistic; stat_soc (2026.6+) the battery's state of charge."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "stat_energy_from": "sensor.imp",
                "stat_energy_to": "sensor.exp",
                "entity_energy_price": "sensor.buy",
                "entity_energy_price_export": "sensor.sell",
                "stat_rate": "sensor.grid_power",
                "name": "Main meter",
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.pv1",
                "stat_rate": "sensor.pv1_power",
                "config_entry_solar_forecast": ["abc"],
            },
            {
                "type": "battery",
                "stat_energy_from": "sensor.dis",
                "stat_energy_to": "sensor.chg",
                "stat_rate": "energy_battery_power_inverted",
                "power_config": {"stat_rate_inverted": "sensor.batt_power"},
                "stat_soc": "sensor.batt_soc",
            },
            {"type": "gas", "stat_energy_from": "sensor.gas"},
        ],
    }
    grid, pv, bat = dr.parse_energy_sources(prefs)
    assert (grid.kind, grid.index, grid.schema, grid.name) == (
        "grid",
        0,
        "unified",
        "Main meter",
    )
    assert grid.energy_from == ("sensor.imp",)
    assert grid.energy_to == ("sensor.exp",)
    assert grid.price_import == ("sensor.buy",)
    assert grid.price_export == ("sensor.sell",)
    assert grid.rate == ("sensor.grid_power",)
    assert pv.rate == ("sensor.pv1_power",)
    assert pv.forecast_entries == ("abc",)
    assert bat.soc == "sensor.batt_soc"
    assert bat.power_config == ({"stat_rate_inverted": "sensor.batt_power"},)
    assert all(s.provenance == "energy_dashboard" for s in (grid, pv, bat))


def test_a_legacy_grid_keeps_every_flow_on_one_source_unpaired():
    """Mark, #1590: list position is not evidence that an import and an
    export flow belong to one connection."""
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [
                    {
                        "stat_energy_from": "sensor.imp_a",
                        "entity_energy_price": "sensor.buy",
                    },
                    {"stat_energy_from": "sensor.imp_b"},
                ],
                "flow_to": [{"stat_energy_to": "sensor.exp"}],
                "power": [{"stat_rate": "sensor.grid_power"}],
            }
        ]
    }
    (grid,) = dr.parse_energy_sources(prefs)
    assert grid.schema == "legacy"
    assert grid.energy_from == ("sensor.imp_a", "sensor.imp_b")
    assert grid.energy_to == ("sensor.exp",)
    assert grid.price_import == ("sensor.buy",)
    assert grid.rate == ("sensor.grid_power",)


def test_two_unified_grid_sources_stay_two_sources_with_their_own_index():
    prefs = {
        "energy_sources": [
            {"type": "grid", "stat_energy_from": "sensor.a"},
            {"type": "solar", "stat_energy_from": "sensor.pv"},
            {"type": "grid", "stat_energy_from": "sensor.b"},
        ]
    }
    grids = [s for s in dr.parse_energy_sources(prefs) if s.kind == "grid"]
    assert [(g.index, g.energy_from) for g in grids] == [
        (0, ("sensor.a",)),
        (2, ("sensor.b",)),
    ]


def test_device_consumption_keeps_containment_as_stated():
    prefs = {
        "device_consumption": [
            {"stat_consumption": "sensor.house"},
            {
                "stat_consumption": "sensor.pool",
                "name": "Pool",
                "stat_rate": "sensor.pool_power",
                "included_in_stat": "sensor.house",
            },
            {"name": "no stat"},
        ]
    }
    house_d, pool = dr.device_consumption(prefs)
    assert house_d.included_in is None
    assert (pool.stat, pool.index, pool.name, pool.rate, pool.included_in) == (
        "sensor.pool",
        1,
        "Pool",
        "sensor.pool_power",
        "sensor.house",
    )
    assert pool.provenance == "energy_dashboard"
    assert dr.device_consumption_entities(prefs) == ["sensor.house", "sensor.pool"]


def test_empty_or_missing_prefs():
    assert dr.parse_energy_sources(None) == []
    assert dr.parse_energy_sources({"energy_sources": []}) == []
    assert dr.device_consumption(None) == []


# --- integration, coverage and counters --------------------------------------------


def test_integrate_holds_values_between_changes():
    s = [(START, 2.0), (START + timedelta(hours=1), -1.0)]
    pos, neg = dr.integrate_parts(s, START, START + timedelta(hours=3))
    assert (round(pos, 6), round(neg, 6)) == (2.0, 2.0)


def test_a_gap_is_not_zero():
    """An unavailable stretch (None) adds nothing and lowers coverage; a
    measured 0 kW adds nothing but counts as covered."""
    end = START + timedelta(hours=4)
    gap = [
        (START, 2.0),
        (START + timedelta(hours=1), None),
        (START + timedelta(hours=3), 2.0),
    ]
    zero = [
        (START, 2.0),
        (START + timedelta(hours=1), 0.0),
        (START + timedelta(hours=3), 2.0),
    ]
    assert dr.integrate_parts(gap, START, end) == dr.integrate_parts(zero, START, end)
    assert dr.coverage(gap, START, end) == 0.5
    assert dr.coverage(zero, START, end) == 1.0


def test_non_finite_values_are_gaps():
    end = START + timedelta(hours=2)
    s = [(START, float("nan")), (START + timedelta(hours=1), float("inf"))]
    assert dr.coverage(s, START, end) == 0.0
    assert dr.integrate_parts(s, START, end) == (0.0, 0.0)


def test_history_starting_late_is_not_covered_before_it_starts():
    s = [(START + timedelta(hours=2), 1.0)]
    assert dr.coverage(s, START, START + timedelta(hours=4)) == 0.5


def test_counter_rise_handles_a_midnight_reset():
    s = [
        (START, 10.0),
        (START + timedelta(hours=1), 12.0),
        (START + timedelta(hours=2), 0.5),
        (START + timedelta(hours=3), 1.5),
    ]
    assert dr.counter_rise(s, START, START + timedelta(hours=4)) == 2.0 + 0.5 + 1.0


def test_an_unreadable_counter_is_missing_not_zero():
    assert dr.counter_rise([], START, END) is None
    assert dr.counter_rise([(START, None), (END, None)], START, END) is None


def test_a_counter_that_held_its_value_rose_zero():
    """Found on the reference household's real history: HA records a counter
    only when it changes, so an hour with no discharge has no new reading.
    That hour is measured at zero, not missing."""
    s = [(START - timedelta(hours=3), 7.5)]
    assert dr.counter_rise(s, START, START + timedelta(hours=1)) == 0.0


def test_a_counter_unavailable_in_the_window_is_missing():
    s = [
        (START, 7.5),
        (START + timedelta(minutes=20), None),
        (START + timedelta(minutes=40), 8.0),
    ]
    assert dr.counter_rise(s, START, START + timedelta(hours=1)) is None
    s = [(START - timedelta(hours=1), None)]
    assert dr.counter_rise(s, START, START + timedelta(hours=1)) is None


# --- pairing ---------------------------------------------------------------------------


def test_battery_power_is_picked_with_its_sign():
    batt = _series(battery)
    cands = {
        "sensor.battery": batt,
        "sensor.house": _series(house),
        "sensor.pv": _series(solar),
    }
    m = dr.match_power_sensor(
        cands,
        START,
        END,
        energy_out=_counter_from(batt, +1),
        energy_in=_counter_from(batt, -1),
    )
    assert (m.status, m.entity_id, m.sign) == (dr.CANDIDATE, "sensor.battery", +1)
    assert m.error < 0.05


def test_a_battery_sensor_with_the_opposite_sign_is_found_and_flagged():
    batt = _series(battery)
    flipped = [(t, -v) for t, v in batt]
    m = dr.match_power_sensor(
        {"sensor.battery_charge_positive": flipped, "sensor.house": _series(house)},
        START,
        END,
        energy_out=_counter_from(batt, +1),
        energy_in=_counter_from(batt, -1),
    )
    assert (m.status, m.entity_id, m.sign) == (
        dr.CANDIDATE,
        "sensor.battery_charge_positive",
        -1,
    )


def test_hourly_shape_separates_solar_from_a_house_load_with_the_same_daily_total():
    """The real trap: on 5 Oct the house load integrated to within 3.3% of
    inverter 1's PV total. A daily-total match nearly tied; the hourly
    profile does not."""
    pv = _series(solar)
    pv_total = dr.integrate_parts(pv, START, END)[0]
    flat = pv_total / 24.0
    cands = {"sensor.pv": pv, "sensor.house_same_total": _series(lambda h: flat)}
    m = dr.match_power_sensor(cands, START, END, energy_out=_counter_from(pv, +1))
    assert (m.status, m.entity_id) == (dr.CANDIDATE, "sensor.pv")
    assert dict(m.ranking)["sensor.house_same_total"] > 0.5


def test_two_identical_readings_ask_rather_than_guess():
    pv = _series(solar)
    m = dr.match_power_sensor(
        {"sensor.mppt1": pv, "sensor.string1": list(pv)},
        START,
        END,
        energy_out=_counter_from(pv, +1),
    )
    assert (m.status, m.entity_id) == (dr.AMBIGUOUS, None)


def test_an_exact_zero_error_tie_asks_whatever_the_order():
    """Mark's reproduction: two identical 1 kW series, energy_out=1.0 over
    one hour, returned sensor.a by entity order."""
    one_hour = START + timedelta(hours=1)
    series = [(START, 1.0)]
    for order in (("sensor.a", "sensor.b"), ("sensor.b", "sensor.a")):
        m = dr.match_power_sensor(
            {eid: list(series) for eid in order}, START, one_hour, energy_out=1.0
        )
        assert (m.status, m.entity_id) == (dr.AMBIGUOUS, None), order


def test_a_near_tie_asks():
    batt = _series(battery)
    nudged = [(t, v * 1.01) for t, v in batt]
    m = dr.match_power_sensor(
        {"sensor.a": batt, "sensor.b": nudged},
        START,
        END,
        energy_out=_counter_from(batt, +1),
        energy_in=_counter_from(batt, -1),
    )
    assert m.status == dr.AMBIGUOUS


def test_missing_evidence_is_never_a_perfect_match():
    """Mark's reproduction: one candidate with no samples and energy_out=[]
    returned error 0.0 with sign -1."""
    m = dr.match_power_sensor({"sensor.a": []}, START, END, energy_out=[])
    assert (m.status, m.entity_id, m.sign) == (dr.INSUFFICIENT, None, None)


def test_counters_readable_in_too_few_hours_are_insufficient():
    """A counter that went unavailable (None) after four hours: the rest of
    the day is missing evidence, not zero energy."""
    pv = _series(solar)
    cut = START + timedelta(hours=4)
    counter = [p for p in _counter_from(pv, +1) if p[0] < cut] + [(cut, None)]
    m = dr.match_power_sensor({"sensor.pv": pv}, START, END, energy_out=counter)
    assert m.status == dr.INSUFFICIENT
    assert "readable in only" in m.reason


def test_adjacent_hours_count_every_change_once():
    batt = _series(battery)
    counter = _counter_from(batt, +1)
    hours = [
        (START + timedelta(hours=h), START + timedelta(hours=h + 1)) for h in range(24)
    ]
    total = sum(dr.counter_rise(counter, a, b) for a, b in hours)
    assert abs(total - dr.counter_rise(counter, START, END)) < 1e-6


def test_an_idle_series_endorses_no_candidate_or_sign():
    """Mark: a genuinely idle zero series returned sign -1 although both
    signs are indistinguishable."""
    idle = _series(lambda h: 0.0)
    m = dr.match_power_sensor(
        {"sensor.idle": idle},
        START,
        END,
        energy_out=_counter_from(idle, +1),
        energy_in=_counter_from(idle, -1),
    )
    assert (m.status, m.entity_id, m.sign) == (dr.INSUFFICIENT, None, None)
    m = dr.match_power_sensor({"sensor.idle": idle}, START, END, energy_out=0.0)
    assert (m.status, m.sign) == (dr.INSUFFICIENT, None)


def test_a_candidate_with_gaps_is_not_compared():
    batt = _series(battery)
    holed = [(t, None if 6 <= i < 60 else v) for i, (t, v) in enumerate(batt)]
    m = dr.match_power_sensor(
        {"sensor.holed": holed},
        START,
        END,
        energy_out=_counter_from(batt, +1),
        energy_in=_counter_from(batt, -1),
    )
    assert m.status == dr.INSUFFICIENT


def test_a_direction_the_window_cannot_tell_apart_is_insufficient():
    """Equal charge and discharge, at the same rate: the sensor matches, but
    nothing in the window says which sign is which."""
    sym = _series(lambda h: 4.0 if h < 12 else -4.0)
    m = dr.match_power_sensor(
        {"sensor.sym": sym},
        START,
        END,
        energy_out=48.0,
        energy_in=48.0,
    )
    assert (m.status, m.entity_id, m.sign) == (dr.INSUFFICIENT, None, None)
    assert "direction" in m.reason


def test_no_match_says_so():
    m = dr.match_power_sensor(
        {"sensor.house": _series(house)},
        START,
        END,
        energy_out=_counter_from(_series(solar), +1),
    )
    assert (m.status, m.entity_id) == (dr.NO_MATCH, None)
    assert "no power sensor matches" in m.reason


def test_no_candidates_says_so():
    assert (
        dr.match_power_sensor({}, START, END, energy_out=5.0).status == dr.NO_CANDIDATES
    )


def test_a_daily_total_alone_still_works():
    batt = _series(battery)
    dis, chg = dr.integrate_parts(batt, START, END)
    m = dr.match_power_sensor(
        {"sensor.battery": batt, "sensor.house": _series(house)},
        START,
        END,
        energy_out=dis,
        energy_in=chg,
    )
    assert (m.status, m.entity_id) == (dr.CANDIDATE, "sensor.battery")


# --- the devices check each other (design §3.3) ----------------------------------


def _aligned():
    hours = [h / 4 for h in range(96)]
    load = [house(h) for h in hours]
    pv = [solar(h) for h in hours]
    bat = [battery(h) for h in hours]
    grid = [load[i] - pv[i] - bat[i] for i in range(96)]
    return load, pv, bat, grid


def test_balance_agrees():
    v = dr.check_energy_balance(*_aligned())
    assert (v.ok, v.explanation) == (True, "agree")


def test_balance_finds_a_flipped_battery():
    load, pv, bat, grid = _aligned()
    v = dr.check_energy_balance(load, pv, [-b for b in bat], grid)
    assert (v.explanation, v.hypotheses) == ("flip:battery", ["flip:battery"])


def test_balance_finds_a_watts_sensor_read_as_kw():
    load, pv, bat, grid = _aligned()
    v = dr.check_energy_balance([x * 1000 for x in load], pv, bat, grid)
    assert v.explanation == "scale:load:/1000"


def test_balance_finds_partial_solar():
    load, pv, bat, grid = _aligned()
    v = dr.check_energy_balance(load, [x * 0.5 for x in pv], bat, grid)
    assert v.explanation == "partial_solar"


def test_balance_reports_ambiguity_instead_of_a_cause():
    """Mark, #1590: a residual suggests, it does not uniquely prove. With no
    solar and no battery, a grid reading -load is closed equally well by
    flipping the grid or by flipping the load, so neither is named."""
    load = [2.0 + (i % 5) * 0.3 for i in range(24)]
    zeros = [0.0] * 24
    v = dr.check_energy_balance(load, zeros, zeros, [-x for x in load])
    assert v.explanation == dr.AMBIGUOUS
    assert {"flip:grid", "flip:load"} <= set(v.hypotheses)
    assert v.ok is False


def test_balance_refuses_misaligned_or_too_short_or_non_finite_input():
    load, pv, bat, grid = _aligned()
    assert (
        dr.check_energy_balance(load, pv, bat, grid[:-1]).explanation == dr.INSUFFICIENT
    )
    short = [x[:5] for x in (load, pv, bat, grid)]
    assert dr.check_energy_balance(*short).explanation == dr.INSUFFICIENT
    bad = list(grid)
    bad[3] = math.nan
    assert dr.check_energy_balance(load, pv, bat, bad).explanation == dr.INSUFFICIENT


def test_entities_on_device():
    reg = [
        {"entity_id": "sensor.a", "device_id": "d1"},
        {"entity_id": "sensor.b", "device_id": "d2"},
        {"entity_id": "sensor.c", "device_id": "d1"},
    ]
    assert dr.entities_on_device(reg, "d1") == ["sensor.a", "sensor.c"]
