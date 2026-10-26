"""A load forecast's real coverage is measured before resampling pads it flat
(#112) (#1837).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import solver_writer


class TestComputeForecastCoverageHours(unittest.TestCase):
    def test_real_coverage_span_in_hours_ahead_of_anchor(self):
        anchor = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
        fc_dicts = [
            {"time": (anchor + timedelta(hours=h)).isoformat(), "value": 1.0}
            for h in (0, 12, 24, 48)
        ]
        coverage = solver_writer.compute_forecast_coverage_hours(fc_dicts, anchor)
        self.assertAlmostEqual(coverage, 48.0, places=6)

    def test_empty_list_returns_none(self):
        anchor = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
        self.assertIsNone(solver_writer.compute_forecast_coverage_hours([], anchor))

    def test_all_unparseable_points_returns_none(self):
        anchor = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
        fc_dicts = [{"value": 1.0}, {"time": None, "value": 2.0}]
        self.assertIsNone(
            solver_writer.compute_forecast_coverage_hours(fc_dicts, anchor)
        )
