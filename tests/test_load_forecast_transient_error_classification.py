"""Regression test for nimbus issue #370 (Mark Purcell, codebase review):
a HA restart left a third-party load-forecast sensor briefly `unavailable`
with no attributes at all; main() substituted a flat 0.0 kW load and
published a confidently "optimal" plan (idle battery, exactly zero-width
cost band) for the few minutes until the sensor caught up.

_is_transient_startup_load_forecast_error() is the new decision function
that separates "this entity genuinely hasn't published real data yet" (a
startup race -- must not be treated as confirmed zero load) from a real,
persistent misconfiguration (must keep the existing zero-fallback +
notification behaviour, so a genuinely broken sensor still gets a real
diagnostic).
"""

from __future__ import annotations

import _solver_path  # noqa: F401
import solver_writer


class TestTransientShapesReturnTrue:
    def test_a_raw_fetch_failure_is_transient(self):
        error = "sensor.nimbus_sigen_plant_total_load_power_forecast could not be read (HTTP Error 404: Not Found)"
        assert solver_writer._is_transient_startup_load_forecast_error(error) is True

    def test_no_list_valued_attributes_at_all_is_transient(self):
        # The exact live repro: an entity restored into the state
        # machine as unavailable, attributes wiped entirely.
        error = (
            "sensor.nimbus_sigen_plant_total_load_power_forecast has no usable "
            "'forecast' attribute (list-valued attributes present: none). "
            "Expected a list of dicts with a 'time' and 'value' key -- see "
            "the canonical shape any sensor.nimbus_<load>_forecast entity "
            "publishes."
        )
        assert solver_writer._is_transient_startup_load_forecast_error(error) is True


class TestPersistentMisconfigurationShapesReturnFalse:
    def test_present_but_empty_forecast_is_not_transient(self):
        # Explicitly a different, multi-day "hasn't trained yet" case --
        # a bounded startup retry wouldn't help, and the existing
        # zero-fallback + notification is the right degraded behaviour.
        error = (
            "sensor.nimbus_pool_forecast's 'forecast' attribute is present "
            "but empty (0 points) -- this is expected for the first few "
            "days after a load subentry is created, before its ML "
            "forecaster has trained on enough real recorder history."
        )
        assert solver_writer._is_transient_startup_load_forecast_error(error) is False

    def test_a_real_shape_mismatch_with_other_list_attributes_is_not_transient(self):
        # A genuinely wrong-shape/wrong-key sensor -- the entity DOES
        # have list-valued attributes, just not the expected one. Real,
        # persistent misconfiguration, not a startup race.
        error = (
            "sensor.some_other_integration has no usable 'forecast' "
            "attribute (list-valued attributes present: ['options']). "
            "Expected a list of dicts with a 'time' and 'value' key."
        )
        assert solver_writer._is_transient_startup_load_forecast_error(error) is False

    def test_unparseable_points_is_not_transient(self):
        error = (
            "sensor.nimbus_pool_forecast's forecast has 5 point(s) but none "
            "parsed cleanly under keys 'time'/'value'."
        )
        assert solver_writer._is_transient_startup_load_forecast_error(error) is False

    def test_the_90_percent_zeros_circular_reference_message_is_not_transient(self):
        # nimbus issue #118 -- explicitly, already, a real misconfiguration
        # diagnosis (pointing solver_load_forecast_sensor at Nimbus's own
        # household-total aggregator with no circuits configured).
        error = (
            "sensor.nimbus_household_load_total_forecast's forecast has only "
            "3/385 non-trivial (>0.01 kW) points -- a real household load "
            "essentially never sits at true zero for 90%+ of a multi-day "
            "forecast. This usually means solver_load_forecast_sensor is "
            "pointed at Nimbus's own household-total aggregator "
            "(sensor.nimbus_household_load_total_forecast) with no "
            "individual circuits configured."
        )
        assert solver_writer._is_transient_startup_load_forecast_error(error) is False
