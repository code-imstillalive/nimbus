"""Real regression test for nimbus repo issue #118 (Mark Purcell, a
real independent installer's own live health-check, 2026-08-24, direct
follow-up to #111): read_load_forecast_sensor() used to treat a
STRUCTURALLY valid but near-all-zero forecast series as a genuine
success ("an all-zero real forecast is a valid, if unusual, success"),
because nothing about that shape fails the real #66 validation checks
(_validate_and_parse_load_forecast_attrs()) -- real timestamps, real
parseable float values, just almost all of them happen to be 0.0.

Mark's own exact real repro: solver_load_forecast_sensor pointed at
Nimbus's OWN household-total aggregator
(sensor.nimbus_household_load_total_forecast) with no individual
circuits configured -- a circular reference. The aggregator's own
upstream is empty, so it publishes 361/362 zero points (only the live
"now" anchor is real), which read_load_forecast_sensor() then
faithfully re-ingests as if it were a genuine forecast. This produced
a confident-looking "optimal" solve with load_forecast_source_error
still None (nothing flagged it) -- the solver believed the household
consumed ~0.03 kW for 96h and planned to export the battery
accordingly.
"""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
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


class TestMarksExactRealRepro(unittest.TestCase):
    """Mark's own reported shape: 362 points, only index 0 (the "now"
    anchor) is nonzero, 361 are exactly 0.0 -- 1/362 ~= 0.28% non-
    trivial, well under any reasonable threshold."""

    def test_362_points_1_nonzero_is_rejected_not_silently_accepted(self):
        # Only index 0 (the "now" anchor) is real -- 1/362 ~= 0.28%
        # non-trivial, exactly matching Mark's own real report.
        points = {0: 6.076}
        state = _state_with_n_points(points, total=362)
        with patch.object(solver_writer, "ha_get", return_value=state):
            load_kw, lower, upper, error = solver_writer.read_load_forecast_sensor(
                "sensor.nimbus_household_load_total_forecast",
                _grid_times(362),
            )
        self.assertIsNone(load_kw)
        self.assertIsNone(lower)
        self.assertIsNone(upper)
        self.assertIsNotNone(error)
        # A real, specific, actionable message -- not just "invalid".
        self.assertIn("circular reference", error)
        self.assertIn("aggregator", error)


class TestGenuineHealthySeriesIsUnaffected(unittest.TestCase):
    """A real, healthy household load forecast -- mostly nonzero, as
    any real occupied home's consumption actually looks -- must NOT be
    flagged by this new check. No false positives on real data."""

    def test_mostly_nonzero_series_still_succeeds(self):
        points = {i: 1.5 + 0.3 * (i % 5) for i in range(20)}
        state = _state_with_n_points(points)
        with patch.object(solver_writer, "ha_get", return_value=state):
            load_kw, _lower, _upper, error = solver_writer.read_load_forecast_sensor(
                "sensor.nimbus_a_real_load_forecast", _grid_times(20)
            )
        self.assertIsNone(error)
        self.assertIsNotNone(load_kw)
        self.assertEqual(len(load_kw), 20)

    def test_a_genuinely_low_but_real_overnight_load_still_succeeds(self):
        """A real household's overnight trickle (small but consistently
        nonzero -- fridge, standby power) must not be mistaken for the
        degenerate case, since every point is still meaningfully above
        the 0.01 kW noise floor."""
        points = {i: 0.15 for i in range(20)}
        state = _state_with_n_points(points)
        with patch.object(solver_writer, "ha_get", return_value=state):
            load_kw, _lower, _upper, error = solver_writer.read_load_forecast_sensor(
                "sensor.nimbus_a_real_load_forecast", _grid_times(20)
            )
        self.assertIsNone(error)
        self.assertIsNotNone(load_kw)


class TestBoundaryAtTenPercent(unittest.TestCase):
    """Precise threshold check, per Mark's own proposed
    nz_points/total_points < 0.1 direction."""

    def test_exactly_10_percent_nonzero_still_succeeds(self):
        # 10/100 nonzero -> ratio == 0.1, NOT < 0.1 -> should succeed.
        points = {i: 2.0 for i in range(10)}
        state = _state_with_n_points(points, total=100)
        with patch.object(solver_writer, "ha_get", return_value=state):
            _load_kw, _lower, _upper, error = solver_writer.read_load_forecast_sensor(
                "sensor.nimbus_a_real_load_forecast", _grid_times(100)
            )
        self.assertIsNone(error)

    def test_just_under_10_percent_nonzero_is_rejected(self):
        # 9/100 nonzero -> ratio 0.09 < 0.1 -> should fail.
        points = {i: 2.0 for i in range(9)}
        state = _state_with_n_points(points, total=100)
        with patch.object(solver_writer, "ha_get", return_value=state):
            _load_kw, _lower, _upper, error = solver_writer.read_load_forecast_sensor(
                "sensor.nimbus_a_real_load_forecast", _grid_times(100)
            )
        self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main()
