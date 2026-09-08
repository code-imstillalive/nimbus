"""nimbus issue #592: real tests for thermal_forecast.py -- pure Python,
no HA imports, same "test the module directly" convention as
test_done_condition.py."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load")
)
import thermal_forecast as tf

_TZ = timezone(timedelta(hours=10))


class TestLearnThermalRates(unittest.TestCase):
    def test_empty_history_falls_back_to_the_534_cited_defaults(self):
        rates = tf.learn_thermal_rates([], on_threshold_kw=0.05)
        self.assertEqual(
            rates.heating_rate_c_per_kwh, tf.DEFAULT_HEATING_RATE_C_PER_KWH
        )
        self.assertEqual(rates.idle_decay_c_per_hour, tf.DEFAULT_IDLE_DECAY_C_PER_HOUR)
        self.assertFalse(rates.is_learned)

    def test_a_real_heating_and_idle_segment_are_both_learned(self):
        # nimbus issue #592's own cited 6 Sep figures: 48->61 degC in
        # 2.9h drawing a steady 0.6kW (1.74kWh -- close enough to the
        # issue's own rounded "1.6kWh" for this test's own synthetic
        # shape), then an idle segment losing temperature overnight.
        t0 = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        history = [
            (t0, 48.0, 0.6),
            (t0 + timedelta(hours=1.45), 54.5, 0.6),
            (t0 + timedelta(hours=2.9), 61.0, 0.6),
            (t0 + timedelta(hours=8), 60.0, 0.0),
            (t0 + timedelta(hours=9), 59.5, 0.0),
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        self.assertTrue(rates.is_learned)
        # 13 degC gained over ~1.74kWh -> real rate in the ballpark of
        # the issue's own cited ~8 degC/kWh, not the raw default.
        self.assertGreater(rates.heating_rate_c_per_kwh, 5.0)
        self.assertLess(rates.heating_rate_c_per_kwh, 10.0)
        self.assertAlmostEqual(rates.idle_decay_c_per_hour, 0.5)

    def test_a_heating_segment_shorter_than_the_minimum_is_dropped(self):
        # nimbus issue #592's own caveat: "when the pump does not reach
        # its heating threshold the rate should not be learned from
        # that run." A 2-minute blip is well under the 6-minute floor.
        t0 = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        history = [
            (t0, 50.0, 0.6),
            (t0 + timedelta(minutes=2), 50.5, 0.6),
            (t0 + timedelta(hours=5), 49.0, 0.0),
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        self.assertEqual(
            rates.heating_rate_c_per_kwh, tf.DEFAULT_HEATING_RATE_C_PER_KWH
        )

    def test_a_heating_segment_that_never_exceeds_the_threshold_is_ignored(self):
        # A compressor that never actually draws above on_threshold_kw
        # (a bridge reporting "on" with only standby draw) must never be
        # treated as a real heating segment at all.
        t0 = datetime(2026, 9, 6, 6, 0, tzinfo=_TZ)
        history = [
            (t0, 50.0, 0.02),
            (t0 + timedelta(hours=1), 50.2, 0.02),
            (t0 + timedelta(hours=6), 49.0, 0.0),
        ]
        rates = tf.learn_thermal_rates(history, on_threshold_kw=0.05)
        self.assertEqual(
            rates.heating_rate_c_per_kwh, tf.DEFAULT_HEATING_RATE_C_PER_KWH
        )

    def test_single_sample_history_is_a_safe_default_no_op(self):
        rates = tf.learn_thermal_rates(
            [(datetime(2026, 9, 6, 6, 0, tzinfo=_TZ), 50.0, 0.0)],
            on_threshold_kw=0.05,
        )
        self.assertFalse(rates.is_learned)


class TestProjectTemperatureForecast(unittest.TestCase):
    def setUp(self):
        self.t0 = datetime(2026, 9, 9, 6, 0, tzinfo=_TZ)

    def _plan(self, values, hours=1.0):
        return [
            {"time": (self.t0 + timedelta(hours=hours * i)).isoformat(), "value": v}
            for i, v in enumerate(values)
        ]

    def test_empty_plan_returns_empty_forecast(self):
        self.assertEqual(
            tf.project_temperature_forecast(
                [],
                start_temperature=50.0,
                heating_rate_c_per_kwh=8.0,
                idle_decay_c_per_hour=0.5,
                on_threshold_kw=0.05,
            ),
            [],
        )

    def test_idle_period_decays_by_the_real_period_duration(self):
        plan = self._plan([0.0, 0.0], hours=2.0)
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=53.8,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
        )
        # 2h at 0.5 degC/h decay -> -1.0 degC.
        self.assertAlmostEqual(forecast[0]["value"], 52.8)

    def test_heating_period_gains_rate_times_real_delivered_kwh(self):
        plan = self._plan([0.65, 0.65], hours=1.0)
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=53.8,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
        )
        # 0.65kW * 1h = 0.65kWh * 8 degC/kWh = 5.2 degC gained.
        self.assertAlmostEqual(forecast[0]["value"], 59.0)

    def test_scenario_matches_592s_own_worked_example_shape(self):
        # nimbus issue #592's own worked example: idle from 06:00 to
        # 11:00 (53.8 -> ~52 degC), then heating from 11:00 -- crossing
        # 60 degC partway through, well before the run's full target.
        plan = (
            self._plan([0.0] * 5, hours=1.0)  # 06:00-11:00, idle
            + [
                {
                    "time": (self.t0 + timedelta(hours=5 + i)).isoformat(),
                    "value": 0.65,
                }
                for i in range(4)  # 11:00 onward, heating
            ]
        )
        forecast = tf.project_temperature_forecast(
            plan,
            start_temperature=53.8,
            heating_rate_c_per_kwh=8.0,
            idle_decay_c_per_hour=0.5,
            on_threshold_kw=0.05,
        )
        # 5h idle at 0.5 degC/h -> 53.8 - 2.5 = 51.3, matching the
        # issue's own "~52 degC by 11:00" (its own figure is rounded).
        self.assertAlmostEqual(forecast[4]["value"], 51.3)
        # Crosses 60 degC partway through the heating run, not at its
        # very end -- the whole point of #592's own worked example.
        crossing_idx = next(i for i, p in enumerate(forecast) if p["value"] >= 60.0)
        self.assertLess(crossing_idx, len(forecast) - 1)


if __name__ == "__main__":
    unittest.main()
