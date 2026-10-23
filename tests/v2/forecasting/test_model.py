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


def test_the_forecaster_learns_the_evening_peak_and_forecasts_it_tomorrow(house):
    assert house is not None
    assert house.model_type in {"gbrt", "knn", "naive"}
    assert house.training_points > 1000
    stamps, result = _tomorrow(house, _house)
    values = np.array(result.values)
    truth = np.array([_house(t) for t in stamps])
    assert len(values) == 96 and (values >= 0).all()
    peak = values[[17 <= t.hour < 21 for t in stamps]].mean()
    quiet = values[[2 <= t.hour < 12 for t in stamps]].mean()
    assert peak > quiet + 1.5
    assert np.mean(np.abs(values - truth)) < 0.4


def test_bands_when_present_surround_the_point(house):
    _stamps, result = _tomorrow(house, _house)
    if result.model_lower is None:
        assert result.model_upper is None
        return
    lower, point, upper = (
        np.array(a) for a in (result.model_lower, result.values, result.model_upper)
    )
    assert (lower <= point + 1e-9).all() and (point <= upper + 1e-9).all()


def test_a_signed_signal_keeps_its_sign_only_when_allowed():
    trained = _train(_battery)
    assert trained is not None
    _stamps, clamped = _tomorrow(trained, _battery)
    stamps, signed = _tomorrow(trained, _battery, allow_negative=True)
    assert min(clamped.values) >= 0.0
    midday = np.array(signed.values)[[10 <= t.hour < 14 for t in stamps]]
    assert midday.mean() < -1.0


def test_no_history_or_too_little_is_no_model(caplog):
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


def test_an_idle_circuit_trains_on_its_idle_time_once_history_is_change_only():
    # A heater that ran 18:00-19:00 on each of the last 21 days and wrote
    # nothing while idle: two events a day.
    events = []
    for d in range(21):
        day = END - timedelta(days=21 - d)
        events += [(day.replace(hour=18), 2.0), (day.replace(hour=19), 0.0)]
    kwargs = {
        "load_events": events,
        "temp_events": _temps(),
        "humidity_events": [],
        "curtailment_events": [],
        "start": END - timedelta(days=21),
        "end": END,
        "resample_minutes": STEP,
        "min_training_points": 200,
    }
    assert model.train_model(**kwargs) is None  # capped: too few rows
    trained = model.train_model(**kwargs, change_only_from=END - timedelta(days=21))
    assert trained is not None and trained.training_points > 1000


def test_a_configured_schedule_overrides_the_model(house):
    stamps, result = _tomorrow(
        house,
        _house,
        expected_load_kw=3.7,
        schedule_start_hour=22.0,
        schedule_end_hour=2.0,
    )
    assert result.values == [
        3.7 if (t.hour >= 22 or t.hour < 2) else 0.0 for t in stamps
    ]
    assert result.model_lower is None


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


def test_a_load_running_now_is_held_at_its_draw_for_the_hold_then_nothing(house):
    # Drawing 3 kW for the last 30 minutes: hold 3 kW for an hour from the
    # latest reading at or above the threshold, then nil.
    values = _hold(house, [(30, 3.0), (15, 3.0), (0, 3.2)], run_hold_hours=1.0)
    assert values[:4] == [3.2] * 4 and values[4:] == [0.0] * 12


def test_a_short_dip_below_the_threshold_still_counts_as_running(house):
    # Below for 7 minutes: inside the 10-minute grace.
    values = _hold(house, [(15, 3.0), (8, 0.2)], run_hold_hours=1.0)
    assert values[0] == 3.0
    # Below for exactly the grace: it has stopped.
    assert _hold(house, [(15, 3.0), (5, 0.2)], run_hold_hours=1.0) == [0.0] * 16
    stopped = _hold(house, [(30, 3.0), (0, 0.2)], run_hold_hours=1.0)
    assert stopped == [0.0] * 16


def test_a_load_never_above_its_threshold_forecasts_nothing_unless_learned_when_off(
    house,
):
    assert _hold(house, [(15, 0.3), (0, 0.2)]) == [0.0] * 16
    learned = _hold(house, [(15, 0.3), (0, 0.2)], learned_when_off=True)
    assert any(v > 0.0 for v in learned)
    running = _hold(house, [(0, 3.0)], run_hold_hours=1.0, learned_when_off=True)
    assert running[:4] == [3.0] * 4 and any(v > 0.0 for v in running[4:])


# --- the seasonal anchor and the smoothing ---------------------------------------------------


def _as_knn(trained):
    """The same training data served by the learned k-NN model, not the
    seasonal-naive one the forecaster rightly picks for a perfectly
    periodic house."""
    return dataclasses.replace(trained, model_type="knn")


def test_the_seasonal_anchor_pulls_the_far_horizon_toward_the_usual_day(house):
    knn = _as_knn(house)
    stamps = [END + timedelta(minutes=STEP * i) for i in range(96)]
    recent = [(t, 0.4) for t, _v in _events(_house, days=1)]  # a quiet last day
    plain = model.predict(knn, stamps, [20.0] * 96, [0.0] * 96, recent, STEP).values
    anchored = model.predict(
        knn, stamps, [20.0] * 96, [0.0] * 96, recent, STEP, seasonal_anchor=True
    ).values
    peak = [i for i, t in enumerate(stamps) if 17 <= t.hour < 21]
    assert np.mean([anchored[i] for i in peak]) > np.mean([plain[i] for i in peak])


def test_the_learned_model_is_smoothed_from_step_to_step(house):
    stamps = [END + timedelta(minutes=STEP * i) for i in range(96)]
    recent = [(t, _house(t)) for t, _v in _events(_house, days=2)]
    naive = model.predict(house, stamps, [20.0] * 96, [0.0] * 96, recent, STEP).values
    knn = model.predict(
        _as_knn(house), stamps, [20.0] * 96, [0.0] * 96, recent, STEP
    ).values
    assert np.abs(np.diff(naive)).max() == pytest.approx(2.1)  # the usual day, exactly
    assert np.abs(np.diff(knn)).max() < 2.1  # no sharper than the real step
