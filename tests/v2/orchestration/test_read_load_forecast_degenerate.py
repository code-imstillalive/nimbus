"""A load forecast that is almost all zero is no forecast, not a success (#118)
(#1837).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import solver_writer


def _grid_times(n):
    base = datetime(2026, 8, 24, 9, 14, tzinfo=UTC)
    return [base + timedelta(minutes=5 * i) for i in range(n)]


def _state_with_n_points(points_dict, total=None, unit="kW"):
    """points_dict: {index: value} -- any index not present defaults to
    0.0, matching Mark's own exact reported shape (only index 0 real,
    everything else 0.0). `total` is the real, fixed series length --
    defaults to max(points_dict)+1 (just enough to fit the given
    points) if not given explicitly; MUST be given explicitly whenever
    a test cares about the exact nonzero/total ratio, since padding
    only up to the highest populated index would silently produce the
    wrong ratio for any dict whose keys don't already span the full
    intended series."""
    if total is None:
        total = max(points_dict) + 1 if points_dict else 1
    times = _grid_times(total)
    forecast = [
        {"time": t.isoformat(), "value": points_dict.get(i, 0.0)}
        for i, t in enumerate(times)
    ]
    return {
        "state": str(forecast[0]["value"]),
        "attributes": {"unit_of_measurement": unit, "forecast": forecast},
    }


class TestBoundaryAtTenPercent(unittest.TestCase):
    """Precise threshold check, per Mark's own proposed
    nz_points/total_points < 0.1 direction."""

    def test_just_under_10_percent_nonzero_is_rejected(self):
        # 9/100 nonzero -> ratio 0.09 < 0.1 -> should fail.
        points = {i: 2.0 for i in range(9)}
        state = _state_with_n_points(points, total=100)
        with patch.object(solver_writer, "ha_get", return_value=state):
            _load_kw, _lower, _upper, error, _coverage = (
                solver_writer.read_load_forecast_sensor(
                    "sensor.nimbus_a_real_load_forecast", _grid_times(100)
                )
            )
        self.assertIsNotNone(error)
