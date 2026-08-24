"""Real regression test for nimbus repo issue #66 (Mark Purcell,
2026-08-23): read_load_forecast_sensor()'s validation, EMHASS-shape
auto-detection, and the one-time-notify wrapper.

Mark's real repro payload (reproduced exactly, see the issue body) --
an EMHASS load-forecast sensor publishing under `scheduled_forecast`,
per-point keys `date`/`<object_id>`, string values, unit_of_measurement
"W" -- is used verbatim as the primary test case, not a simplified
stand-in.
"""

import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


def _grid_times(n=4):
    base = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    return [base + timedelta(minutes=30 * i) for i in range(n)]


def _grid_times_aest(n=4):
    # Real Brisbane offset (+10, no DST) -- matches the EMHASS repro
    # payload's own timestamps exactly, so "nearest-at-or-before"
    # resampling picks the FIRST point for grid_times[0], not the last
    # (a UTC-vs-AEST mismatch here would silently pick the wrong point
    # and look like a real bug when it's a test-setup error).
    aest = timezone(timedelta(hours=10))
    base = datetime(2026, 8, 23, 9, 0, tzinfo=aest)
    return [base + timedelta(minutes=30 * i) for i in range(n)]


class TestCanonicalShape(unittest.TestCase):
    """The shape every sensor.nimbus_<load>_forecast entity already
    publishes -- must keep working exactly as before this fix."""

    def test_canonical_shape_with_bands_succeeds(self):
        state = {
            "state": "1.2",
            "attributes": {
                "unit_of_measurement": "kW",
                "forecast": [
                    {
                        "time": "2026-08-23T09:00:00+00:00",
                        "value": 1.2,
                        "lower": 1.0,
                        "upper": 1.5,
                    },
                    {
                        "time": "2026-08-23T09:30:00+00:00",
                        "value": 1.4,
                        "lower": 1.1,
                        "upper": 1.7,
                    },
                ],
            },
        }
        with patch.object(solver_writer, "ha_get", return_value=state):
            load_kw, lower, upper, error, _coverage = solver_writer.read_load_forecast_sensor(
                "sensor.nimbus_pool_forecast", _grid_times()
            )
        self.assertIsNone(error)
        self.assertAlmostEqual(load_kw[0], 1.2)
        self.assertAlmostEqual(load_kw[1], 1.4)
        # Real band values pass through, clamped around the point est.
        self.assertLessEqual(lower[0], load_kw[0])
        self.assertGreaterEqual(upper[0], load_kw[0])

    def test_canonical_shape_without_bands_gets_zero_width_fallback(self):
        # Original pre-#66 behavior: a source with no lower/upper keys
        # at all resamples to 0.0 for both, then the clamp widens that
        # to [0, load_kw] -- preserved exactly, not silently changed.
        state = {
            "state": "1.2",
            "attributes": {
                "unit_of_measurement": "kW",
                "forecast": [{"time": "2026-08-23T09:00:00+00:00", "value": 1.2}],
            },
        }
        with patch.object(solver_writer, "ha_get", return_value=state):
            load_kw, lower, upper, error, _coverage = solver_writer.read_load_forecast_sensor(
                "sensor.some_source", _grid_times()
            )
        self.assertIsNone(error)
        self.assertEqual(lower[0], 0.0)
        self.assertEqual(upper[0], load_kw[0])


class TestEmhassShape(unittest.TestCase):
    """Mark's exact real repro payload from the issue body."""

    def _emhass_state(self):
        return {
            "state": "691.67",
            "attributes": {
                "unit_of_measurement": "W",
                "scheduled_forecast": [
                    {
                        "date": "2026-08-23T09:00:00+10:00",
                        "p_load_forecast_custom_model": "691.67",
                    },
                    {
                        "date": "2026-08-23T09:30:00+10:00",
                        "p_load_forecast_custom_model": "576.36",
                    },
                    {
                        "date": "2026-08-23T10:00:00+10:00",
                        "p_load_forecast_custom_model": "840.69",
                    },
                ],
            },
        }

    def test_emhass_shape_auto_detected_and_converted(self):
        with patch.object(solver_writer, "ha_get", return_value=self._emhass_state()):
            load_kw, lower, upper, error, _coverage = solver_writer.read_load_forecast_sensor(
                "sensor.p_load_forecast_custom_model", _grid_times_aest()
            )
        self.assertIsNone(error, f"expected success, got error: {error}")
        # 691.67 W -> 0.69167 kW, NOT a flat 0.215 kW -- the value this
        # project's own reference install eventually confirmed as a
        # DIFFERENT, real, permanent inverter self-consumption bias
        # (now a real, optional, per-household number.py setting --
        # see const.py's own CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW
        # comment -- not a module constant read_load_forecast_sensor()
        # has ever touched at all). Kept as a bare literal here, not a
        # module attribute reference, specifically so this regression
        # guard survives that field being renamed/removed/reconfigured
        # in the future -- 0.215 is this test's own known historical
        # bad value, not a live dependency on any current constant.
        self.assertAlmostEqual(load_kw[0], 0.69167, places=4)
        self.assertNotAlmostEqual(load_kw[0], 0.215, places=2)
        # No real band info in this shape -- zero-width around the point.
        self.assertEqual(lower[0], load_kw[0])
        self.assertEqual(upper[0], load_kw[0])

    def test_emhass_shape_string_values_coerced_not_left_as_strings(self):
        with patch.object(solver_writer, "ha_get", return_value=self._emhass_state()):
            load_kw, _, _, error, _coverage = solver_writer.read_load_forecast_sensor(
                "sensor.p_load_forecast_custom_model", _grid_times_aest()
            )
        self.assertIsNone(error)
        for v in load_kw:
            self.assertIsInstance(v, float)


class TestGenuinelyUnrecognizedShape(unittest.TestCase):
    def test_unrecognized_shape_returns_clear_error_not_none_arrays_silently(self):
        state = {
            "state": "42",
            "attributes": {
                "unit_of_measurement": "W",
                "some_other_forecast_key": [{"whatever": 1}],
            },
        }
        with patch.object(solver_writer, "ha_get", return_value=state):
            load_kw, lower, upper, error, _coverage = solver_writer.read_load_forecast_sensor(
                "sensor.mystery_source", _grid_times()
            )
        self.assertIsNone(load_kw)
        self.assertIsNone(lower)
        self.assertIsNone(upper)
        self.assertIsNotNone(error)
        self.assertIn("sensor.mystery_source", error)

    def test_empty_forecast_list_returns_error(self):
        state = {"state": "0", "attributes": {"forecast": []}}
        with patch.object(solver_writer, "ha_get", return_value=state):
            load_kw, _, _, error, _coverage = solver_writer.read_load_forecast_sensor(
                "sensor.empty_source", _grid_times()
            )
        self.assertIsNone(load_kw)
        self.assertIsNotNone(error)

    def test_points_missing_time_or_value_are_skipped_not_crashed(self):
        state = {
            "state": "1.0",
            "attributes": {
                "forecast": [
                    {"time": "2026-08-23T09:00:00+00:00"},  # no value
                    {"value": 1.5},  # no time
                    {"time": "2026-08-23T09:30:00+00:00", "value": 1.1},  # real point
                ]
            },
        }
        with patch.object(solver_writer, "ha_get", return_value=state):
            load_kw, _, _, error, _coverage = solver_writer.read_load_forecast_sensor(
                "sensor.partial_source", _grid_times()
            )
        self.assertIsNone(error)
        self.assertTrue(any(abs(v - 1.1) < 1e-6 for v in load_kw))


class TestNotifyOnce(unittest.TestCase):
    def test_fires_once_for_a_new_error_not_again_for_the_same_one(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            sentinel = os.path.join(d, "notified.txt")
            with (
                patch.object(
                    solver_writer, "LOAD_FORECAST_ERROR_NOTIFIED_PATH", sentinel
                ),
                patch.object(solver_writer, "ha_call_service") as mock_call,
            ):
                solver_writer._notify_load_forecast_error_once("error A")
                solver_writer._notify_load_forecast_error_once("error A")
                self.assertEqual(
                    mock_call.call_count, 1, "same error must not re-notify"
                )
                solver_writer._notify_load_forecast_error_once("error B")
                self.assertEqual(
                    mock_call.call_count,
                    2,
                    "a genuinely different error must notify again",
                )

    def test_a_failed_notify_never_raises(self):
        # Deliberately swallowed -- a notification courtesy must never
        # be allowed to break the real solve.
        with patch.object(
            solver_writer, "ha_call_service", side_effect=RuntimeError("boom")
        ):
            try:
                solver_writer._notify_load_forecast_error_once("some error")
            except Exception as e:  # BLE001 ignored globally (see #72 Stage 2)
                self.fail(f"_notify_load_forecast_error_once raised: {e}")


if __name__ == "__main__":
    unittest.main()
