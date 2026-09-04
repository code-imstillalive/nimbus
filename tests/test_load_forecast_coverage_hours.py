"""Real regression test for nimbus issue #112 ("solver horizon 96.3h
exceeds subentry forecast horizon 48h"). resample_forecast()'s own
nearest-at-or-before lookup silently pads flat past a source's real
last timestamp -- by the time main() has resampled load_kw arrays in
hand, there is no way left to tell a real forecast point from padded-
flat filler. compute_forecast_coverage_hours() captures the real
coverage span on the RAW forecast list, before that information is
lost, so it can be exposed as its own diagnostic field.
"""

import unittest
from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
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

    def test_last_real_point_before_anchor_clamps_to_zero_not_negative(self):
        # A stale source whose real coverage has already fully lapsed --
        # a real, honest 0.0h coverage, not a nonsensical negative one.
        anchor = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        fc_dicts = [{"time": "2026-08-25T06:00:00+00:00", "value": 1.0}]
        coverage = solver_writer.compute_forecast_coverage_hours(fc_dicts, anchor)
        self.assertEqual(coverage, 0.0)


class TestSumLoadForecastsCoverage(unittest.TestCase):
    def test_coverage_is_the_minimum_across_successfully_fetched_entities(self):
        # The real #112 scenario: household demand is a sum of several
        # circuits, each with its OWN real coverage. The sum's own
        # coverage must be the weakest link -- every period beyond the
        # shortest-covered circuit's real data is resample_forecast()'s
        # own flat-hold padding for THAT circuit, corrupting the whole
        # household total's own claim to being "real" beyond that point.
        anchor = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
        grid_times = [anchor + timedelta(hours=h) for h in range(0, 96, 4)]

        def fake_fetch(entity_id):
            if entity_id == "sensor.short_coverage":
                fc = [
                    {"time": (anchor + timedelta(hours=h)).isoformat(), "value": 1.0}
                    for h in (0, 24, 48)
                ]
            else:
                fc = [
                    {"time": (anchor + timedelta(hours=h)).isoformat(), "value": 1.0}
                    for h in (0, 24, 48, 72, 96)
                ]
            return fc, None

        original = solver_writer.fetch_load_forecast_safe
        solver_writer.fetch_load_forecast_safe = fake_fetch
        try:
            _, _, _, _, _, coverage_hours = solver_writer.sum_load_forecasts(
                ["sensor.short_coverage", "sensor.long_coverage"],
                grid_times,
                0.0,
                anchor,
            )
        finally:
            solver_writer.fetch_load_forecast_safe = original
        self.assertAlmostEqual(coverage_hours, 48.0, places=6)

    def test_coverage_is_none_when_every_entity_fails(self):
        grid_times = [datetime(2026, 8, 25, tzinfo=UTC)]

        original = solver_writer.fetch_load_forecast_safe
        solver_writer.fetch_load_forecast_safe = lambda entity_id: (None, "down")
        try:
            _, _, _, failed, _, coverage_hours = solver_writer.sum_load_forecasts(
                ["sensor.dead"], grid_times, 0.0, grid_times[0]
            )
        finally:
            solver_writer.fetch_load_forecast_safe = original
        self.assertEqual(failed, ["sensor.dead"])
        self.assertIsNone(coverage_hours)
