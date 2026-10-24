"""A burst load's running hold: at or above its threshold, the current
draw is forecast for the hold, then the load's usual pattern; a short dip
stays held, a longer one is off, and an unset threshold is the old
behaviour (#1703) (#1837).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import _ml_path  # noqa: F401
from nimbus_load.ml import model as ml_model

START = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001
DAYS = 21


def _burst_load_events() -> list[tuple[datetime, float]]:
    """An AC that runs 0.9 kW for one hour at 13:00 each day, idle 0.02 kW
    otherwise -- short runs, like the real circuit (6% of time above
    0.5 kW)."""
    events = []
    t = START
    end = START + timedelta(days=DAYS)
    while t < end:
        events.append((t, 0.9 if t.hour == 13 else 0.02))
        t += timedelta(minutes=15)
    return events


class TestRunningHold(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trained = ml_model.train_model(
            load_events=_burst_load_events(),
            temp_events=[],
            humidity_events=[],
            curtailment_events=[],
            start=START,
            end=START + timedelta(days=DAYS),
            resample_minutes=15,
            min_training_points=50,
        )
        assert cls.trained is not None

    def _predict(self, now: datetime, live_kw: float, **kw):
        timestamps = [now + timedelta(minutes=15 * i) for i in range(48)]
        recent = [(now - timedelta(minutes=15 * i), live_kw) for i in range(8)]
        return ml_model.predict(
            trained=self.trained,
            timestamps=timestamps,
            temps=[20.0] * len(timestamps),
            humidities=[50.0] * len(timestamps),
            recent_load_values=recent,
            resample_minutes=15,
            **kw,
        )

    def test_a_running_load_holds_its_draw_for_the_hold_then_drops(self):
        # 15:00, outside the usual 13:00 run, the AC still running at 0.9 kW.
        now = START + timedelta(days=DAYS, hours=15)
        result = self._predict(now, 0.9, running_threshold_kw=0.6, run_hold_hours=1.0)
        self.assertEqual(result.values[:4], [0.9] * 4)  # 15:00-15:45
        # After the hour: nothing (the household's "reprogram them to nil").
        self.assertEqual(result.values[4:], [0.0] * 44)

    def test_a_longer_hold_holds_longer(self):
        now = START + timedelta(days=DAYS, hours=15)
        result = self._predict(now, 0.9, running_threshold_kw=0.6, run_hold_hours=2.0)
        self.assertEqual(result.values[:8], [0.9] * 8)

    def test_switched_off_forecasts_nothing(self):
        # Below the threshold -- switched off, or idling -- the rule forecasts
        # nil, at every hour, including the 13:00 slot the model has learned.
        now = START + timedelta(days=DAYS, hours=12)
        result = self._predict(now, 0.3, running_threshold_kw=0.6)
        self.assertEqual(result.values, [0.0] * 48)
        learned = self._predict(now, 0.3)
        self.assertGreater(max(learned.values), 0.3)  # the model alone would not

    def _predict_series(self, now, series, **kw):
        """`series`: (minutes before now, kW) readings."""
        timestamps = [now + timedelta(minutes=15 * i) for i in range(48)]
        recent = [(now - timedelta(minutes=m), v) for m, v in series]
        return ml_model.predict(
            trained=self.trained,
            timestamps=timestamps,
            temps=[20.0] * len(timestamps),
            humidities=[50.0] * len(timestamps),
            recent_load_values=recent,
            resample_minutes=15,
            **kw,
        )

    def test_a_short_dip_stays_held(self):
        # A compressor dipping to 0.2 kW for 4 minutes is still running.
        now = START + timedelta(days=DAYS, hours=15)
        result = self._predict_series(
            now, [(30, 0.9), (4, 0.9), (0, 0.2)], running_threshold_kw=0.6
        )
        self.assertEqual(result.values[:3], [0.9] * 3)

    def test_a_dip_longer_than_the_grace_is_switched_off(self):
        now = START + timedelta(days=DAYS, hours=15)
        grace = ml_model.RUN_OFF_GRACE_MINUTES
        result = self._predict_series(
            now,
            [(grace + 5, 0.9), (grace + 1, 0.0), (0, 0.0)],
            running_threshold_kw=0.6,
        )
        self.assertEqual(result.values, [0.0] * 48)

    def test_unset_threshold_is_the_old_behaviour(self):
        now = START + timedelta(days=DAYS, hours=15)
        self.assertEqual(
            self._predict(now, 0.9, running_threshold_kw=None).values,
            self._predict(now, 0.9).values,
        )

    def test_the_hold_counts_from_the_latest_reading(self):
        # Re-evaluated on every reading, so a run that keeps going keeps
        # extending: 30 minutes later the hold still covers the next hour.
        now = START + timedelta(days=DAYS, hours=15, minutes=30)
        result = self._predict(now, 0.9, running_threshold_kw=0.6, run_hold_hours=1.0)
        self.assertEqual(result.values[:4], [0.9] * 4)


if __name__ == "__main__":
    unittest.main()


class TestLearnedWhenOff(unittest.TestCase):
    """For a load with a regular pattern (an AC on hot afternoons): while
    off, and after a hold, its usual runs are forecast instead of nothing."""

    @classmethod
    def setUpClass(cls):
        TestRunningHold.setUpClass()
        cls.trained = TestRunningHold.trained

    def _predict(self, now, live, **kw):
        timestamps = [now + timedelta(minutes=15 * i) for i in range(48)]
        recent = [(now - timedelta(minutes=15 * i), live) for i in range(8)]
        return ml_model.predict(
            trained=self.trained,
            timestamps=timestamps,
            temps=[20.0] * len(timestamps),
            humidities=[50.0] * len(timestamps),
            recent_load_values=recent,
            resample_minutes=15,
            **kw,
        )

    def test_off_forecasts_the_usual_13_00_run(self):
        now = START + timedelta(days=DAYS, hours=11)
        nil = self._predict(now, 0.02, running_threshold_kw=0.6)
        learned = self._predict(
            now, 0.02, running_threshold_kw=0.6, learned_when_off=True
        )
        self.assertEqual(max(nil.values), 0.0)
        # 13:00-14:00 is steps 8-11 from 11:00.
        self.assertGreater(max(learned.values[8:12]), 0.5)

    def test_running_holds_then_returns_to_the_usual_pattern(self):
        now = START + timedelta(days=DAYS, hours=15)
        result = self._predict(
            now, 0.9, running_threshold_kw=0.6, learned_when_off=True
        )
        self.assertEqual(result.values[:4], [0.9] * 4)
        # After the hold: the usual pattern for 16:00 onwards is idle, not the
        # held 0.9 carried on.
        self.assertLess(max(result.values[4:40]), 0.3)
