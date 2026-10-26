"""Reading an external load forecast sensor: validation, the EMHASS shape, and
notifying once (#66) (#1837).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

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
            load_kw, lower, upper, error, _coverage = (
                solver_writer.read_load_forecast_sensor(
                    "sensor.p_load_forecast_custom_model", _grid_times_aest()
                )
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
