"""Thermal loads: learn a tank's rates from its own history, project its
temperature along a plan, and find when it crosses its floor (#1837).

A simulated hot-water tank with known physics: the learner must recover
its heating rate and its idle loss (flat, and against ambient), and the
projection must reproduce the simulation it was learned from. Covers what
lived in #481, #610 and the thermal forecast files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
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


def test_the_learner_recovers_a_tanks_heating_rate_and_idle_decay():
    rates = tf.learn_thermal_rates(_tank(), on_threshold_kw=0.5)
    assert rates.is_learned
    assert rates.idle_decay_c_per_hour == pytest.approx(0.8, rel=0.02)
    assert rates.heating_rate_c_per_kwh == pytest.approx(HEAT_C_PER_KWH, rel=0.15)
    assert rates.heating_sample_count == 3  # every run has idle on both sides
    assert rates.idle_sample_count == 4
    assert rates.loss_coeff_per_h is None


def test_with_ambient_history_it_learns_the_loss_against_the_gap():
    history = _tank(loss=0.02)
    ambient = [(T0 + timedelta(hours=h), 15.0) for h in range(0, 73, 6)]
    rates = tf.learn_thermal_rates(
        history, on_threshold_kw=0.5, ambient_history=ambient
    )
    assert rates.loss_coeff_per_h == pytest.approx(0.02, rel=0.1)
    assert rates.loss_coeff_sample_count == rates.idle_sample_count


def test_too_little_history_falls_back_to_defaults_and_says_so():
    empty = tf.learn_thermal_rates([], on_threshold_kw=0.5)
    assert not empty.is_learned
    assert empty.heating_rate_c_per_kwh == tf.DEFAULT_HEATING_RATE_C_PER_KWH
    assert empty.idle_decay_c_per_hour == tf.DEFAULT_IDLE_DECAY_C_PER_HOUR
    # Twenty idle minutes teach the decay but no heating rate.
    idle = tf.learn_thermal_rates(_tank(days=1)[:5], on_threshold_kw=0.5)
    assert not idle.is_learned
    assert idle.heating_rate_c_per_kwh == tf.DEFAULT_HEATING_RATE_C_PER_KWH
    assert idle.idle_decay_c_per_hour == pytest.approx(0.8, rel=0.01)
    assert (idle.heating_sample_count, idle.idle_sample_count) == (0, 1)


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


def test_with_an_ambient_forecast_the_loss_scales_with_the_gap():
    plan = _plan([0.0, 0.0])
    ambient = [
        {"time": T0.isoformat(), "value": 10.0},
        {"time": (T0 + timedelta(hours=1)).isoformat(), "value": 30.0},
    ]
    out = tf.project_temperature_forecast(
        plan,
        start_temperature=50.0,
        heating_rate_c_per_kwh=2.0,
        idle_decay_c_per_hour=9.0,  # ignored once the loss model applies
        on_threshold_kw=0.5,
        loss_coeff_per_h=0.1,
        ambient_forecast=ambient,
    )
    first = 50.0 - 0.1 * (50.0 - 10.0) * 0.5
    second = first - 0.1 * (first - 20.0) * 0.5
    assert [e["value"] for e in out] == [round(first, 2), round(second, 2)]


def test_the_projection_reproduces_the_simulated_tank():
    history = _tank(days=1)
    rates = tf.learn_thermal_rates(_tank(), on_threshold_kw=0.5)
    plan = [{"time": t, "value": p} for t, _temp, p in history]
    out = tf.project_temperature_forecast(
        plan,
        start_temperature=history[0][1],
        heating_rate_c_per_kwh=rates.heating_rate_c_per_kwh,
        idle_decay_c_per_hour=rates.idle_decay_c_per_hour,
        on_threshold_kw=0.5,
    )
    # out[i] is the temperature at the END of period i: history[i + 1].
    errors = [
        abs(float(o["value"]) - h[1]) for o, h in zip(out, history[1:], strict=False)
    ]
    assert max(errors) < 1.5


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
