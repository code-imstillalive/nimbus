"""nimbus #1574: the device resolver behind the two-step setup.

Validated by hand on the reference household's real history (2026-10-03..05,
read-only): among the 247 power sensors on its Modbus device, the resolver
picked each inverter's battery power sensor (sign correct, 1.0-2.6% error,
runner-up 12-48% off) and inverter 1's PV power (1.0-1.9%), and asked rather
than guessed where two sensors are the same reading. These tests reproduce the
traps that check exposed, synthetically.
"""

from __future__ import annotations

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


def _counter_from(series, part=+1, reset_at_midnight=True):
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


# --- Energy Dashboard schemas (#1589) ----------------------------------------


def test_parses_the_current_flat_grid_schema():
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "stat_energy_from": "sensor.imp",
                "stat_energy_to": "sensor.exp",
                "entity_energy_price": "sensor.buy",
                "entity_energy_price_export": "sensor.sell",
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.pv1",
                "config_entry_solar_forecast": ["abc"],
            },
            {
                "type": "solar",
                "stat_energy_from": "sensor.pv2",
                "config_entry_solar_forecast": [],
            },
            {
                "type": "battery",
                "stat_energy_from": "sensor.dis",
                "stat_energy_to": "sensor.chg",
            },
            {"type": "gas", "stat_energy_from": "sensor.gas"},
        ],
        "device_consumption": [{"stat_consumption": "sensor.pool"}, {"name": "x"}],
    }
    srcs = dr.parse_energy_sources(prefs)
    assert [s.kind for s in srcs] == ["grid", "solar", "solar", "battery"]
    g = srcs[0]
    assert (g.energy_from, g.energy_to, g.price_import, g.price_export) == (
        "sensor.imp",
        "sensor.exp",
        "sensor.buy",
        "sensor.sell",
    )
    assert srcs[1].forecast_entries == ["abc"]
    assert dr.device_consumption_entities(prefs) == ["sensor.pool"]


def test_parses_the_older_flow_list_grid_schema():
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [
                    {
                        "stat_energy_from": "sensor.imp",
                        "entity_energy_price": "sensor.buy",
                    }
                ],
                "flow_to": [
                    {
                        "stat_energy_to": "sensor.exp",
                        "entity_energy_price": "sensor.sell",
                    }
                ],
            }
        ]
    }
    g = dr.parse_energy_sources(prefs)[0]
    assert (g.energy_from, g.energy_to, g.price_import, g.price_export) == (
        "sensor.imp",
        "sensor.exp",
        "sensor.buy",
        "sensor.sell",
    )


def test_empty_or_missing_prefs():
    assert dr.parse_energy_sources(None) == []
    assert dr.parse_energy_sources({"energy_sources": []}) == []


# --- integration and counters ------------------------------------------------------


def test_integrate_holds_values_between_changes():
    s = [(START, 2.0), (START + timedelta(hours=1), -1.0)]
    pos, neg = dr.integrate_parts(s, START, START + timedelta(hours=3))
    assert (round(pos, 6), round(neg, 6)) == (2.0, 2.0)


def test_counter_rise_handles_a_midnight_reset():
    s = [
        (START, 10.0),
        (START + timedelta(hours=1), 12.0),
        (START + timedelta(hours=2), 0.5),
        (START + timedelta(hours=3), 1.5),
    ]
    assert dr.counter_rise(s, START, START + timedelta(hours=4)) == 2.0 + 0.5 + 1.0


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
    assert (m.entity_id, m.sign) == ("sensor.battery", +1)
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
    assert (m.entity_id, m.sign) == ("sensor.battery_charge_positive", -1)


def test_hourly_shape_separates_solar_from_a_house_load_with_the_same_daily_total():
    """The real trap: on 5 Oct the house load integrated to within 3.3% of
    inverter 1's PV total. A daily-total match nearly tied; the hourly
    profile does not."""
    pv = _series(solar)
    pv_total = dr.integrate_parts(pv, START, END)[0]
    flat = pv_total / 24.0
    cands = {"sensor.pv": pv, "sensor.house_same_total": _series(lambda h: flat)}
    m = dr.match_power_sensor(cands, START, END, energy_out=_counter_from(pv, +1))
    assert m.entity_id == "sensor.pv"
    runner_up = dict(m.ranking)["sensor.house_same_total"]
    assert runner_up > 0.5, runner_up


def test_two_identical_readings_ask_rather_than_guess():
    pv = _series(solar)
    m = dr.match_power_sensor(
        {"sensor.mppt1": pv, "sensor.string1": list(pv)},
        START,
        END,
        energy_out=_counter_from(pv, +1),
    )
    assert m.entity_id is None
    assert "both match" in m.reason


def test_no_match_says_so():
    m = dr.match_power_sensor(
        {"sensor.house": _series(house)},
        START,
        END,
        energy_out=_counter_from(_series(solar), +1),
    )
    assert m.entity_id is None and "no power sensor matches" in m.reason


def test_a_daily_total_alone_still_works():
    batt = _series(battery)
    dis = dr.integrate_parts(batt, START, END)[0]
    chg = dr.integrate_parts(batt, START, END)[1]
    m = dr.match_power_sensor(
        {"sensor.battery": batt, "sensor.house": _series(house)},
        START,
        END,
        energy_out=dis,
        energy_in=chg,
    )
    assert m.entity_id == "sensor.battery"


# --- the devices check each other (design §3.3) ----------------------------------


def _aligned():
    hours = [h / 4 for h in range(96)]
    load = [house(h) for h in hours]
    pv = [solar(h) for h in hours]
    bat = [battery(h) for h in hours]
    grid = [load[i] - pv[i] - bat[i] for i in range(96)]
    return load, pv, bat, grid


def test_balance_agrees():
    assert dr.check_energy_balance(*_aligned()).explanation == "agree"


def test_balance_finds_a_flipped_battery():
    load, pv, bat, grid = _aligned()
    v = dr.check_energy_balance(load, pv, [-b for b in bat], grid)
    assert v.explanation == "flip:battery"


def test_balance_finds_a_watts_sensor_read_as_kw():
    load, pv, bat, grid = _aligned()
    v = dr.check_energy_balance([x * 1000 for x in load], pv, bat, grid)
    assert v.explanation == "scale:load:/1000"


def test_balance_finds_partial_solar():
    load, pv, bat, grid = _aligned()
    v = dr.check_energy_balance(load, [x * 0.5 for x in pv], bat, grid)
    assert v.explanation == "partial_solar"


def test_entities_on_device():
    reg = [
        {"entity_id": "sensor.a", "device_id": "d1"},
        {"entity_id": "sensor.b", "device_id": "d2"},
        {"entity_id": "sensor.c", "device_id": "d1"},
    ]
    assert dr.entities_on_device(reg, "d1") == ["sensor.a", "sensor.c"]
