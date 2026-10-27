"""The load forecaster end to end: train on recorded history, then predict
(#1837).

A synthetic household with a daily evening peak, three weeks of 15-minute
readings: the forecaster must learn the peak, forecast it for tomorrow,
never forecast a negative load, keep a signed signal's sign when asked to,
refuse to train on too little, and follow a configured schedule exactly.
Covers what lived in #110, #351, #353, #1206, #1556, #1703 and the model
training and prediction files.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import _ml_path  # noqa: F401  (ml/ reaches up to const.py)
import numpy as np
import pytest
from nimbus_load.ml import model

BNE = ZoneInfo("Australia/Brisbane")
END = datetime(2026, 9, 20, 0, 0, tzinfo=BNE)
STEP = 15


def _house(t: datetime) -> float:
    """2.5 kW from 17:00 to 21:00, 0.4 kW otherwise."""
    return 2.5 if 17 <= t.hour < 21 else 0.4


def _battery(t: datetime) -> float:
    """Charging (negative) in the middle of the day, discharging at night."""
    return -3.0 if 9 <= t.hour < 15 else 1.5


def _events(fn, days=21, end=END):
    start = end - timedelta(days=days)
    n = days * 24 * 60 // STEP
    return [
        (start + timedelta(minutes=STEP * i), fn(start + timedelta(minutes=STEP * i)))
        for i in range(n)
    ]


def _temps(days=21, end=END):
    return _events(
        lambda t: 18.0 + 6.0 * np.sin((t.hour - 9) / 24 * 2 * np.pi), days, end
    )


def _train(fn, **kw):
    return model.train_model(
        load_events=_events(fn),
        temp_events=_temps(),
        humidity_events=[],
        curtailment_events=[],
        start=END - timedelta(days=21),
        end=END,
        resample_minutes=STEP,
        min_training_points=200,
        label="test",
        **kw,
    )


@pytest.fixture(scope="module")
def house():
    return _train(_house)


def _tomorrow(trained, fn, **kw):
    stamps = [END + timedelta(minutes=STEP * i) for i in range(96)]
    recent = [(t, fn(t)) for t, _v in _events(fn, days=2)]
    temps = [18.0 + 6.0 * np.sin((t.hour - 9) / 24 * 2 * np.pi) for t in stamps]
    result = model.predict(trained, stamps, temps, [0.0] * 96, recent, STEP, **kw)
    return stamps, result


def test_no_history_or_too_little_is_no_model(caplog, house):
    # test_no_history_or_too_little_is_no_model
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=model._LOGGER.name):
        none = model.train_model(
            load_events=[],
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=END - timedelta(days=1),
            end=END,
            resample_minutes=STEP,
            min_training_points=10,
            label="sensor.empty",
        )
        few = model.train_model(
            load_events=_events(_house, days=1),
            temp_events=_temps(days=1),
            humidity_events=[],
            curtailment_events=[],
            start=END - timedelta(days=1),
            end=END,
            resample_minutes=STEP,
            min_training_points=500,
            label="sensor.young",
        )
    assert none is None and few is None
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("sensor.empty: ") for m in messages)
    assert any(m.startswith("sensor.young: ") for m in messages)

    # test_a_short_dip_below_the_threshold_still_counts_as_running
    # Below for 7 minutes: inside the 10-minute grace.
    values = _hold(house, [(15, 3.0), (8, 0.2)], run_hold_hours=1.0)
    assert values[0] == 3.0
    # Below for exactly the grace: it has stopped.
    assert _hold(house, [(15, 3.0), (5, 0.2)], run_hold_hours=1.0) == [0.0] * 16
    stopped = _hold(house, [(30, 3.0), (0, 0.2)], run_hold_hours=1.0)
    assert stopped == [0.0] * 16

    # test_a_load_never_above_its_threshold_forecasts_nothing_unless_learned_when_off
    assert _hold(house, [(15, 0.3), (0, 0.2)]) == [0.0] * 16
    learned = _hold(house, [(15, 0.3), (0, 0.2)], learned_when_off=True)
    assert any(v > 0.0 for v in learned)
    running = _hold(house, [(0, 3.0)], run_hold_hours=1.0, learned_when_off=True)
    assert running[:4] == [3.0] * 4 and any(v > 0.0 for v in running[4:])

    # test_a_burst_load_is_held_for_as_long_as_its_runs_last
    # #2129 (household, 28 Oct 2026: an AC forecast to run until midnight,
    # "its not run that long in years"). An AC that sits at 0.02 kW and runs
    # at 1.2 kW for 3 to 6 hours learns those runs; past the halfway level.
    ac = [0.02] * (96 * 21)
    for day, hours in ((2, 4), (5, 4), (9, 3), (12, 4), (15, 3), (18, 6)):
        for k in range(hours * 4):
            ac[day * 96 + 36 + k] = 1.2
    threshold, runs = model.learn_run_profile(ac, STEP)
    assert threshold == pytest.approx(0.61)
    assert runs == (3.0, 3.0, 4.0, 4.0, 4.0, 6.0)
    assert model.learn_run_profile([1.0] * 2000, STEP) == (None, ())
    assert model.learn_run_profile([0.02] * 50, STEP) == (None, ())
    # A run lasts the median of the past runs longer than it already has; one
    # past them all runs a quarter-hour more; too little history, 2 hours.
    assert model.expected_run_hours(runs, 1.0) == 4.0
    assert model.expected_run_hours(runs, 4.5) == 6.0
    assert model.expected_run_hours(runs, 7.0) == 7.25
    assert model.expected_run_hours((4.0,), 0.5) == model.DEFAULT_RUN_HOURS
    # The run going on now started at its first reading; a 5-minute dip
    # doesn't break it, and 10 minutes off ends it.
    t0 = END - timedelta(hours=3)
    readings = [
        (t0 - timedelta(hours=1), 0.02),
        (t0, 1.2),
        (t0 + timedelta(hours=1), 0.3),
        (t0 + timedelta(hours=1, minutes=5), 1.2),
        (END, 1.2),
    ]
    assert model.run_started_at(readings, threshold) == t0
    off = [(END - timedelta(minutes=30), 1.2), (END - timedelta(minutes=20), 0.02)]
    assert model.run_started_at(off, threshold) is None
    # Running for an hour: the learned forecast to the usual 4 hours (3 more),
    # then its usual pattern, never chained on to midnight. Only the end
    # moves: inside the run the forecast is the model's own, so a fixed
    # daily schedule the rule also admits keeps it (Mark Purcell's review).
    bursty = dataclasses.replace(house, run_threshold_kw=threshold, run_hours=runs)
    stamps = [END + timedelta(minutes=STEP * i) for i in range(32)]
    recent = [(END - timedelta(hours=1), 1.2), (END, 1.2)]

    def forecast(times, **kw):
        n = len(times)
        return model.predict(
            bursty, times, [20.0] * n, [0.0] * n, recent, STEP, **kw
        ).values

    ended = forecast(stamps, run_started=END - timedelta(hours=1))
    assert ended[:12] == forecast(stamps[:12])
    assert ended[12:] == forecast(stamps[12:], seasonal_anchor=True)
    assert ended != forecast(stamps)

    # test_a_sensor_too_sparse_for_recursive_validation_is_named_as_such
    caplog.clear()
    trained, line = _selection_log(caplog, _sparse(5), max_staleness_minutes=600)
    assert line.startswith("probe: Recursive validation unavailable (")
    assert "no OBSERVED actual" in line and "which more history will not change" in line
    assert "origin(s) available" in line and "Too few origins" not in line
    assert line.endswith(
        f"falling back to one-step selection -> using {trained.model_type}"
    )

    # --- the horizon curve: selection at 4 h, a diagnostic out to a day (#937) --------------------
    # test_selection_validates_4_h_and_the_diagnostic_reaches_a_day_at_a_bounded_cost
    minutes = 15  # the forecaster's resample step
    assert model.RECURSIVE_VALIDATION_HORIZON_STEPS * minutes / 60 == 4.0
    horizons = model.RECURSIVE_VALIDATION_DIAGNOSTIC_HORIZON_STEPS
    assert max(horizons) * minutes / 60 >= 24.0
    assert all(h > model.RECURSIVE_VALIDATION_HORIZON_STEPS for h in horizons)
    assert model.RECURSIVE_VALIDATION_DIAGNOSTIC_MAX_ORIGINS * sum(horizons) <= 2000

    # test_the_model_is_chosen_by_the_selection_metric_never_the_diagnostic
    chosen = min(house.validation_recursive_mae, key=house.validation_recursive_mae.get)
    assert house.model_type == chosen
    assert house.validation_recursive_mae_by_horizon  # measured, and only measured


# --- a burst load's running hold (#1703) ------------------------------------------------


def _hold(house, readings, **kw):
    stamps = [END + timedelta(minutes=STEP * i) for i in range(16)]
    recent = [(END - timedelta(minutes=m), v) for m, v in readings]
    out = model.predict(
        house,
        stamps,
        [20.0] * 16,
        [0.0] * 16,
        recent,
        STEP,
        running_threshold_kw=1.0,
        **kw,
    )
    return out.values


# --- the seasonal anchor and the smoothing ---------------------------------------------------


def _as_knn(trained):
    """The same training data served by the learned k-NN model, not the
    seasonal-naive one the forecaster rightly picks for a perfectly
    periodic house."""
    return dataclasses.replace(trained, model_type="knn")


# --- which criterion chose the model, and what the other would have chosen (#937) ------------


def _sparse(gap_hours):
    start = END - timedelta(days=21)
    out, t = [], start
    while t < END:
        out.append((t, _house(t)))
        t += timedelta(hours=gap_hours)
    return out


def _selection_log(caplog, events, **kw):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=model._LOGGER.name):
        trained = model.train_model(
            load_events=events,
            temp_events=_temps(),
            humidity_events=[],
            curtailment_events=[],
            start=END - timedelta(days=21),
            end=END,
            resample_minutes=STEP,
            min_training_points=50,
            label="probe",
            **kw,
        )
    (line,) = [
        r.getMessage()
        for r in caplog.records
        if "Model validation (recursive" in r.getMessage()
        or "Recursive validation unavailable" in r.getMessage()
    ]
    return trained, line
