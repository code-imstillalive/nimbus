"""Thermal loads: learn a tank's rates from its own history, project its
temperature along a plan, and find when it crosses its floor (#1837).

A simulated hot-water tank with known physics: the learner must recover
its heating rate and its idle loss (flat, and against ambient), and the
projection must reproduce the simulation it was learned from. Covers what
lived in #481, #610 and the thermal forecast files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import thermal_forecast as tf

T0 = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
STEP_MIN = 5
HEAT_C_PER_KWH = 2.0
RUN_KW = 3.6


def _tank(days=3, *, decay=0.8, loss=None, ambient=15.0, start=55.0):
    """(time, temperature, power) every 5 minutes; heats 02:00-03:00 daily."""
    out, temp = [], start
    step_h = STEP_MIN / 60
    for i in range(days * 24 * 60 // STEP_MIN):
        t = T0 + timedelta(minutes=STEP_MIN * i)
        power = RUN_KW if t.hour == 2 else 0.0
        out.append((t, round(temp, 4), power))
        if power:
            temp += power * step_h * HEAT_C_PER_KWH
        elif loss is not None:
            temp -= loss * (temp - ambient) * step_h
        else:
            temp -= decay * step_h
    return out


def test_ambient_between_readings_is_interpolated_and_held_at_the_ends():
    amb = [(T0, 10.0), (T0 + timedelta(hours=2), 20.0)]
    assert tf._ambient_value_at(amb, T0 + timedelta(hours=1)) == 15.0
    assert tf._ambient_value_at(amb, T0 - timedelta(hours=1)) == 10.0
    assert tf._ambient_value_at(amb, T0 + timedelta(hours=5)) == 20.0
    assert tf._ambient_value_at([], T0) is None
    assert tf._ambient_value_at([(T0, 3.0), (T0, 4.0)], T0) in (3.0, 4.0)


def _plan(powers, start=T0, minutes=30):
    return [
        {"time": (start + timedelta(minutes=minutes * i)).isoformat(), "value": p}
        for i, p in enumerate(powers)
    ]


def test_a_projection_heats_when_on_and_decays_when_off_up_to_a_ceiling():
    plan = _plan([0.0, 4.0, 4.0, 0.0])
    out = tf.project_temperature_forecast(
        plan,
        start_temperature=50.0,
        heating_rate_c_per_kwh=2.0,
        idle_decay_c_per_hour=1.0,
        on_threshold_kw=0.5,
    )
    assert [e["value"] for e in out] == [49.5, 53.5, 57.5, 57.0]
    assert [e["time"] for e in out] == [p["time"] for p in plan]
    capped = tf.project_temperature_forecast(
        plan,
        start_temperature=50.0,
        heating_rate_c_per_kwh=2.0,
        idle_decay_c_per_hour=1.0,
        on_threshold_kw=0.5,
        ceiling_temperature=55.0,
    )
    assert [e["value"] for e in capped] == [49.5, 53.5, 55.0, 54.5]
    # The live reading replaces the plan's first period.
    now_on = tf.project_temperature_forecast(
        plan,
        start_temperature=50.0,
        heating_rate_c_per_kwh=2.0,
        idle_decay_c_per_hour=1.0,
        on_threshold_kw=0.5,
        override_first_period_power_kw=4.0,
    )
    assert now_on[0]["value"] == 54.0
    assert (
        tf.project_temperature_forecast(
            [],
            start_temperature=1,
            heating_rate_c_per_kwh=1,
            idle_decay_c_per_hour=1,
            on_threshold_kw=1,
        )
        == []
    )


def test_the_floor_crossing_and_its_naive_estimate():
    forecast = [
        {"time": "a", "value": 50.0},
        {"time": "b", "value": 44.9},
        {"time": "c", "value": 40.0},
    ]
    assert tf.find_floor_crossing(forecast, 45.0) == forecast[1]
    assert tf.find_floor_crossing(forecast, 30.0) is None
    grid = [T0 + timedelta(hours=h) for h in range(6)]
    assert tf.naive_floor_crossing_period(grid, T0, 50.0, 2.0, 45.0) == 3  # 2.5 h
    for args in (
        (None, 2.0, 45.0),
        (50.0, None, 45.0),
        (50.0, 2.0, None),
        (50.0, 0.0, 45.0),
        (44.0, 2.0, 45.0),
    ):
        assert tf.naive_floor_crossing_period(grid, T0, *args) is None
    assert (
        tf.naive_floor_crossing_period(grid, T0, 80.0, 2.0, 45.0) is None
    )  # past the horizon
    assert tf.naive_floor_crossing_period([], T0, 50.0, 2.0, 45.0) is None


def test_the_floor_is_the_configured_minimum_else_the_done_condition():
    parse = lambda text: (">=", float(text.split()[-1]))
    assert tf.resolve_floor_temperature(42, ">= 50", parse) == 42.0
    assert tf.resolve_floor_temperature("x", ">= 50", parse) == 50.0
    assert (
        tf.resolve_floor_temperature(
            None, "bad", lambda t: (_ for _ in ()).throw(ValueError())
        )
        is None
    )
    assert tf.resolve_floor_temperature(None, None, parse) is None


def _series(rows):
    """[(minutes, temperature, power)] from T0."""
    return [(T0 + timedelta(minutes=m), temp, p) for m, temp, p in rows]


IDLE_BEFORE = [(0, 50.0, 0.0), (30, 49.6, 0.0)]


def test_a_heating_run_that_cannot_be_measured_teaches_no_heating_rate():
    for rows in [
        # heating for one 5-minute sample: too short to trust
        IDLE_BEFORE + [(35, 49.5, 3.6), (40, 52.0, 0.0), (70, 51.6, 0.0)],
        # the history opens mid-run: no idle reading before it
        [(0, 50.0, 3.6), (30, 53.0, 3.6), (60, 56.0, 0.0), (90, 55.6, 0.0)],
        # it ends before the tank settles after the run
        IDLE_BEFORE
        + [(35, 49.5, 3.6), (65, 53.0, 3.6), (68, 53.2, 0.0), (69, 53.1, 0.0)],
        # it ran and the tank got no warmer
        IDLE_BEFORE
        + [(35, 49.5, 3.6), (65, 49.5, 3.6), (70, 49.0, 0.0), (100, 48.6, 0.0)],
    ]:
        rates = tf.learn_thermal_rates(_series(rows), on_threshold_kw=0.5)
        assert rates.heating_sample_count == 0
        assert rates.heating_rate_c_per_kwh == tf.DEFAULT_HEATING_RATE_C_PER_KWH
