"""Real regression test for nimbus repo issue #100 findings 1 & 2 (Mark
Purcell, an independent installer's own live health-check, 2026-08-24).

Root cause, precisely traced (not guessed) via his own reported
`source_entities: ['sensor.sigen_plant_consumed_power_forecast']` --
a ONE-ITEM LIST, which only sum_load_forecasts() ever produces (the
single-sensor fallback path, read_load_forecast_sensor(), never
populates `load_forecast_entities` at all): sum_load_forecasts() used
to unconditionally add a bare, hardcoded, THIS-PROJECT'S-OWN-HOUSEHOLD-
SPECIFIC module constant (INVERTER_SELF_CONSUMPTION_KW = 0.215) to
every point/lower/upper value it computed, on EVERY Nimbus install,
regardless of whether that household's own hardware has any such bias
at all. This is what precisely explains finding 2 (his own load total's
`lower` band sitting dead flat at exactly 0.215 for 362/363 points,
while `value`/`upper` genuinely varied) -- a source contributing ~0 of
its own real lower-band signal, plus the unconditional +0.215, with the
defensive upper-clamp (`max(total_upper_kw[i], total_kw[i])`) hiding
the identical addition on the upper side once the real point value grew
past it.

Now a real, optional, per-household parameter (default 0.0, a genuine
no-op) instead of a hardcoded constant -- see const.py's own
CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW comment for the full
"why this was a portability bug" story.
"""

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer


def _grid_times(n=4):
    base = datetime(2026, 8, 24, 11, 0, tzinfo=UTC)
    return [base + timedelta(minutes=30 * i) for i in range(n)]


def _state_with_no_band(point_values):
    """A source entity's own real forecast, canonical shape, but with
    genuinely no lower/upper keys published at all -- exactly the shape
    that, combined with the old hardcoded +0.215, reproduced Mark's own
    reported {value: v, lower: 0.215, upper: v} pattern."""
    times = _grid_times(len(point_values))
    return {
        "state": str(point_values[0]),
        "attributes": {
            "unit_of_measurement": "kW",
            "forecast": [
                {"time": t.isoformat(), "value": v} for t, v in zip(times, point_values)
            ],
        },
    }


class TestDefaultIsANoOp(unittest.TestCase):
    """0.0 (the default, and every install's own real behavior until
    this field is explicitly configured) must produce byte-identical
    output to summing the raw source values alone -- this is the
    portability guarantee the whole fix exists for."""

    def test_zero_bias_leaves_the_sum_unchanged(self):
        state = _state_with_no_band([1.0, 2.0, 3.0, 4.0])
        with patch.object(solver_writer, "ha_get", return_value=state):
            total_kw, lower_kw, upper_kw, failed, warnings = (
                solver_writer.sum_load_forecasts(["sensor.one_circuit"], _grid_times(4))
            )
        self.assertEqual(failed, [])
        self.assertEqual(warnings, {})
        self.assertEqual(total_kw, [1.0, 2.0, 3.0, 4.0])
        # No real band info -> resample_forecast() returns 0.0 for lower
        # AND upper. The defensive clamp is NOT symmetric: lower is
        # min(lower, value) (stays 0.0 -- already <= any real value),
        # upper is max(upper, value) (forced up to match value).
        self.assertEqual(lower_kw, [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(upper_kw, total_kw)

    def test_omitting_the_parameter_entirely_matches_explicit_zero(self):
        state = _state_with_no_band([5.5])
        with patch.object(solver_writer, "ha_get", return_value=state):
            no_arg = solver_writer.sum_load_forecasts(
                ["sensor.one_circuit"], _grid_times(1)
            )
            explicit_zero = solver_writer.sum_load_forecasts(
                ["sensor.one_circuit"], _grid_times(1), 0.0
            )
        self.assertEqual(no_arg, explicit_zero)


class TestConfiguredBiasReproducesFinding2Exactly(unittest.TestCase):
    """A real, non-zero household bias must reproduce the EXACT shape
    Mark reported -- this is the regression guard for the actual bug,
    not just the mechanism."""

    def test_lower_band_sticks_at_the_bias_while_upper_tracks_value(self):
        # A source with genuinely no lower/upper keys, real varying
        # point values, real household bias = 0.215 (this project's own
        # reference value -- see const.py's own comment for why).
        state = _state_with_no_band([1.0, 16.49, 18.91, 2.0])
        with patch.object(solver_writer, "ha_get", return_value=state):
            total_kw, lower_kw, upper_kw, failed, warnings = (
                solver_writer.sum_load_forecasts(
                    ["sensor.one_circuit"], _grid_times(4), 0.215
                )
            )
        self.assertEqual(failed, [])
        self.assertEqual(warnings, {})
        self.assertEqual(total_kw, [1.215, 16.705, 19.125, 2.215])
        # Every point: lower is exactly the bias (0 real signal + 0.215),
        # matching Mark's own reported {lower: 0.215} on 362/363 points.
        for v in lower_kw:
            self.assertAlmostEqual(v, 0.215, places=6)
        # Every point: upper is clamped to the real (biased) value, not
        # left at 0.215 -- matching his own reported {upper: value}.
        self.assertEqual(upper_kw, total_kw)

    def test_bias_is_summed_once_per_period_not_once_per_entity(self):
        """A real, easy-to-get-wrong edge case: with TWO configured
        circuits, the bias must still be added exactly once per period
        (it models one shared inverter-level loss, not a per-circuit
        one) -- confirms the fix's own placement (after the per-entity
        accumulation loop, not inside it)."""
        state_a = _state_with_no_band([1.0, 1.0])
        state_b = _state_with_no_band([2.0, 2.0])

        def _fake_ha_get(entity_id):
            return state_a if entity_id == "sensor.a" else state_b

        with patch.object(solver_writer, "ha_get", side_effect=_fake_ha_get):
            total_kw, _, _, failed, warnings = solver_writer.sum_load_forecasts(
                ["sensor.a", "sensor.b"], _grid_times(2), 0.215
            )
        self.assertEqual(failed, [])
        self.assertEqual(warnings, {})
        # 1.0 + 2.0 + 0.215 (once, not 0.215 + 0.215) = 3.215.
        self.assertAlmostEqual(total_kw[0], 3.215, places=6)
        self.assertAlmostEqual(total_kw[1], 3.215, places=6)


class TestMalformedCircuitGetsARealDiagnosticNotJustDropped(unittest.TestCase):
    """The actual point of issue #105: a genuinely malformed/wrong-shape
    source entity on the multi-circuit summing path used to be either
    silently corrupting the sum (if fetch_load_forecast_safe() happened
    to return SOMETHING that superficially looked like a list) or
    dropped to a bare "unavailable" with zero real diagnostic -- the
    exact class of problem #66 already fixed on the single-sensor path.
    A healthy circuit summed alongside a malformed one must still
    produce a correct sum, not be corrupted by its neighbour."""

    def test_malformed_shape_is_excluded_with_a_real_reason_not_silently_summed(self):
        healthy = _state_with_no_band([2.0, 2.0])
        malformed = {
            "state": "unknown",
            "attributes": {"unit_of_measurement": "kW", "not_forecast_at_all": []},
        }

        def _fake_ha_get(entity_id):
            return healthy if entity_id == "sensor.healthy" else malformed

        with patch.object(solver_writer, "ha_get", side_effect=_fake_ha_get):
            total_kw, _, _, failed, warnings = solver_writer.sum_load_forecasts(
                ["sensor.healthy", "sensor.malformed"], _grid_times(2)
            )
        # The healthy circuit's own real contribution must be unaffected
        # by its malformed neighbour -- not corrupted, not zeroed.
        self.assertEqual(total_kw, [2.0, 2.0])
        self.assertEqual(failed, ["sensor.malformed"])
        self.assertIn("sensor.malformed", warnings)
        # A REAL, specific reason -- not just "unavailable" -- naming
        # the actual attribute shape problem, matching #66's own bar.
        self.assertIn("no usable 'forecast' attribute", warnings["sensor.malformed"])

    def test_wrong_unit_is_auto_scaled_the_same_way_the_single_sensor_path_already_is(
        self,
    ):
        """A real, concrete test of the class of bug #105 itself
        speculated about ('is unit scaling wrong for one of the
        sources?') -- a source publishing genuine watts under
        unit_of_measurement 'W' must be scaled to kW on the multi-
        circuit path exactly like it already is on the single-sensor
        path (read_load_forecast_sensor()), not silently misread as
        already-kW."""
        times = _grid_times(2)
        state_in_watts = {
            "state": "1500",
            "attributes": {
                "unit_of_measurement": "W",
                "forecast": [
                    {"time": times[0].isoformat(), "value": 1500.0},
                    {"time": times[1].isoformat(), "value": 3000.0},
                ],
            },
        }
        with patch.object(solver_writer, "ha_get", return_value=state_in_watts):
            total_kw, _, _, failed, warnings = solver_writer.sum_load_forecasts(
                ["sensor.watts_source"], times
            )
        self.assertEqual(failed, [])
        self.assertEqual(warnings, {})
        self.assertAlmostEqual(total_kw[0], 1.5, places=6)
        self.assertAlmostEqual(total_kw[1], 3.0, places=6)


if __name__ == "__main__":
    unittest.main()
