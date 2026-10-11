"""nimbus #1703 (household, 11 Oct 2026): a burst load's "running" hold.

The household's own YAML forecasts for its ACs, laundry and EV charger said:
at or above a threshold (600 W, or 1000 W for the EV), forecast the current
draw for the next hour, then 0, re-evaluated on every reading. Without that,
the recursive lag chain carried a running AC's 0.9 kW across the whole
horizon. A Load can now carry the same rule: `running_threshold_kw` and
`run_hold_hours`.
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


class TestTheFieldsReachThePredictCall(unittest.TestCase):
    def test_the_coordinator_passes_both_fields(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "nimbus_load"
            / "coordinator.py"
        ).read_text(encoding="utf-8")
        call = src[src.index("result: PredictionResult = await") :][:900]
        self.assertIn("self._running_threshold_kw", call)
        self.assertIn("self._run_hold_hours", call)


if __name__ == "__main__":
    unittest.main()
