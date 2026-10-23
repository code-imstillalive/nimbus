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
