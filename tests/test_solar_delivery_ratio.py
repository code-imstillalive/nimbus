"""Tests for the rolling actual-vs-forecast solar delivery ratio (2026-08-25,
nimbus issue #128, Mark Purcell): "switch.solar_curtailment doesn't detect
implicit inverter AC-clipping... solar_delivery_ratio = rolling_avg(actual_solar_kw
/ forecast_solar_kw) over the last N hours where forecast > 5 kW."

update_solar_delivery_ratio() (solver_writer.py) queues one new prediction per
call (~60 min ahead, from THIS solve's own forecast) and resolves any
previously-queued prediction whose target time has arrived by fetching the
real measured reading at that moment -- same "record now, grade later" shape
as coordinator.py's own _last_step_prediction residual tracking.
"""

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

BRISBANE = solver_writer.BRISBANE_TZ


def _cfg(**overrides):
    cfg = {"solver_solar_power_sensor": "sensor.real_solar"}
    cfg.update(overrides)
    return cfg


def _grid(start, n=48, step_minutes=15):
    return [start + timedelta(minutes=i * step_minutes) for i in range(n)]


class TestGating(unittest.TestCase):
    def test_missing_solar_sensor_returns_none_and_writes_nothing(self, *_):
        cfg = _cfg(solver_solar_power_sensor=None)
        now = datetime(2026, 8, 25, 12, 0, tzinfo=BRISBANE)
        with patch.object(
            solver_writer, "SOLAR_DELIVERY_RATIO_PATH", "/nonexistent/should-not-write"
        ):
            result = solver_writer.update_solar_delivery_ratio(
                cfg, now, _grid(now), [10.0] * 48
            )
        self.assertIsNone(result)


class TestQueueAndResolve(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile

        fd, self._tmpfile = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._patcher = patch.object(
            solver_writer, "SOLAR_DELIVERY_RATIO_PATH", self._tmpfile
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        import os

        try:
            os.remove(self._tmpfile)
        except OSError:
            pass

    def test_first_call_queues_a_prediction_with_no_ratio_yet(self):
        cfg = _cfg()
        now = datetime(2026, 8, 25, 12, 0, tzinfo=BRISBANE)
        grid_times = _grid(now)
        solar_kw = [10.0] * len(grid_times)

        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer.update_solar_delivery_ratio(
                cfg, now, grid_times, solar_kw
            )
            fetch.assert_not_called()

        self.assertEqual(result["solar_delivery_ratio"], None)
        self.assertEqual(result["solar_delivery_sample_count"], 0)
        self.assertFalse(result["solar_delivery_underperforming"])

        with open(self._tmpfile, encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(len(state["pending"]), 1)
        self.assertEqual(len(state["ratios"]), 0)

    def test_pending_prediction_resolves_once_its_target_time_arrives(self):
        cfg = _cfg()
        t0 = datetime(2026, 8, 25, 12, 0, tzinfo=BRISBANE)
        grid_times = _grid(t0)
        solar_kw = [10.0] * len(grid_times)

        with patch.object(solver_writer, "fetch_entity_history_range"):
            solver_writer.update_solar_delivery_ratio(cfg, t0, grid_times, solar_kw)

        # An hour later: the queued ~60min-ahead prediction's target time
        # has now arrived. A real history fetch resolves it.
        t1 = t0 + timedelta(minutes=65)
        grid_times_2 = _grid(t1)

        def fake_fetch(entity_id, start, end):
            self.assertEqual(entity_id, "sensor.real_solar")
            return [(start + timedelta(minutes=5), 7.5)]

        with patch.object(
            solver_writer, "fetch_entity_history_range", side_effect=fake_fetch
        ):
            result = solver_writer.update_solar_delivery_ratio(
                cfg, t1, grid_times_2, [10.0] * len(grid_times_2)
            )

        # actual 7.5 / forecast 10.0 = 0.75
        self.assertEqual(result["solar_delivery_sample_count"], 1)
        self.assertAlmostEqual(result["solar_delivery_ratio"], 0.75, places=3)
        self.assertTrue(result["solar_delivery_underperforming"])  # 0.75 < 0.80

    def test_forecast_below_threshold_is_dropped_not_resolved(self):
        # A near-dawn/dusk forecast under SOLAR_DELIVERY_MIN_FORECAST_KW
        # must never be fetched or turned into a ratio -- Mark's own
        # "avoid near-zero-solar noise" spec.
        cfg = _cfg()
        t0 = datetime(2026, 8, 25, 6, 0, tzinfo=BRISBANE)
        grid_times = _grid(t0)
        solar_kw = [1.0] * len(grid_times)  # well under the 5.0 kW floor

        with patch.object(solver_writer, "fetch_entity_history_range"):
            solver_writer.update_solar_delivery_ratio(cfg, t0, grid_times, solar_kw)

        t1 = t0 + timedelta(minutes=65)
        with patch.object(solver_writer, "fetch_entity_history_range") as fetch:
            result = solver_writer.update_solar_delivery_ratio(
                cfg, t1, _grid(t1), [1.0] * len(grid_times)
            )
            fetch.assert_not_called()

        self.assertEqual(result["solar_delivery_sample_count"], 0)

    def test_ratios_older_than_the_rolling_window_are_trimmed(self):
        cfg = _cfg()
        now = datetime(2026, 8, 25, 12, 0, tzinfo=BRISBANE)
        # Seed state directly with one stale ratio (well past the
        # rolling window) and one fresh one.
        stale_time = now - timedelta(
            hours=solver_writer.SOLAR_DELIVERY_ROLLING_WINDOW_HOURS + 1
        )
        fresh_time = now - timedelta(minutes=10)
        with open(self._tmpfile, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "pending": [],
                    "ratios": [
                        {"time": stale_time.isoformat(), "ratio": 0.5},
                        {"time": fresh_time.isoformat(), "ratio": 0.9},
                    ],
                },
                f,
            )

        grid_times = _grid(now)
        with patch.object(solver_writer, "fetch_entity_history_range"):
            result = solver_writer.update_solar_delivery_ratio(
                cfg, now, grid_times, [10.0] * len(grid_times)
            )

        self.assertEqual(result["solar_delivery_sample_count"], 1)
        self.assertAlmostEqual(result["solar_delivery_ratio"], 0.9, places=3)


if __name__ == "__main__":
    unittest.main()
