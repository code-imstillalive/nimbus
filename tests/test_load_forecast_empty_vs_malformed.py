"""Real regression test, found live on devhub (2026-08-24, first-ever
solve on a freshly configured install): _validate_and_parse_load_forecast_attrs()
used a single `not raw_fc` check that was True for BOTH a missing/wrong-shape
'forecast' attribute AND a present-but-genuinely-empty one -- the latter is the
expected, benign state for a brand-new load subentry whose ML forecaster
hasn't trained on enough real recorder history yet, not a malformed sensor.

Before this fix, both cases produced the identical, confusing message:
"has no usable 'forecast' attribute (list-valued attributes present:
['forecast'])" -- naming the very attribute it just rejected, giving an
installer no way to tell "your sensor is broken" apart from "just wait, this
is normal for a new install."
"""

import unittest

import _solver_path  # noqa: F401
import solver_writer


class TestEmptyForecastGetsDistinctColdStartMessage(unittest.TestCase):
    def test_empty_list_is_not_conflated_with_missing_attribute(self):
        fc, has_bands, error = solver_writer._validate_and_parse_load_forecast_attrs(
            "sensor.nimbus_mirror_hws_l1_forecast", {"forecast": []}
        )
        self.assertIsNone(fc)
        self.assertIsNotNone(error)
        self.assertIn("empty", error)
        self.assertIn("trained", error)
        self.assertNotIn("has no usable", error)

    def test_missing_attribute_still_gets_the_original_message(self):
        fc, has_bands, error = solver_writer._validate_and_parse_load_forecast_attrs(
            "sensor.some_bad_sensor", {"other_attr": 1}
        )
        self.assertIsNone(fc)
        self.assertIsNotNone(error)
        self.assertIn("has no usable 'forecast' attribute", error)

    def test_wrong_type_forecast_still_gets_the_original_message(self):
        fc, has_bands, error = solver_writer._validate_and_parse_load_forecast_attrs(
            "sensor.some_bad_sensor", {"forecast": "not-a-list"}
        )
        self.assertIsNone(fc)
        self.assertIsNotNone(error)
        self.assertIn("has no usable 'forecast' attribute", error)

    def test_valid_forecast_still_parses_correctly_unaffected_by_this_change(self):
        fc, has_bands, error = solver_writer._validate_and_parse_load_forecast_attrs(
            "sensor.nimbus_mirror_hws_l1_forecast",
            {"forecast": [{"time": "2026-08-25T00:00:00+10:00", "value": 1.5}]},
        )
        self.assertIsNone(error)
        self.assertIsNotNone(fc)
        self.assertEqual(len(fc), 1)


if __name__ == "__main__":
    unittest.main()
