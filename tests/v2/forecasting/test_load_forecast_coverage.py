"""A load forecast's real coverage is measured before resampling pads it flat
(#112) (#1837).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import solver_writer


class TestComputeForecastCoverageHours(unittest.TestCase):
    def test_all_unparseable_points_returns_none(self):
        anchor = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
        fc_dicts = [{"value": 1.0}, {"time": None, "value": 2.0}]
        self.assertIsNone(
            solver_writer.compute_forecast_coverage_hours(fc_dicts, anchor)
        )
