"""Real regression test, found live on devhub (2026-08-25) while
diagnosing nimbus issues #148/#116: `solver_load_forecast_sensor` was
configured (`sensor.nimbus_mirror_whole_house_load_forecast`) but
completely, silently ignored because `solver_load_forecast_entities` had
4 entries -- exactly the precedence rule this project's own README already
documents in prose, but nothing in the diagnostics dump named which path
actually ran. `resolve_load_forecast_source_label()` (solver_writer.py)
fixes this by naming it plainly.
"""

import unittest

import _solver_path  # noqa: F401
import solver_writer


class TestLoadForecastSourceUsedLabel(unittest.TestCase):
    def test_empty_entities_list_uses_single_sensor_label(self):
        label = solver_writer.resolve_load_forecast_source_label(
            [], "sensor.nimbus_mirror_whole_house_load_forecast"
        )
        self.assertEqual(
            label, "single sensor: sensor.nimbus_mirror_whole_house_load_forecast"
        )

    def test_nonempty_entities_list_wins_even_with_single_sensor_configured(self):
        # The exact real devhub repro: solver_load_forecast_sensor is
        # configured and valid, but solver_load_forecast_entities has 4
        # entries -- the multi-circuit path must win, and the label must
        # say so, not silently report the single-sensor field instead.
        entities = [
            "sensor.nimbus_mirror_hws_l1_forecast",
            "sensor.nimbus_mirror_hws_l3_forecast",
            "sensor.nimbus_mirror_pool_1_forecast",
            "sensor.nimbus_mirror_pool_2_forecast",
        ]
        label = solver_writer.resolve_load_forecast_source_label(
            entities, "sensor.nimbus_mirror_whole_house_load_forecast"
        )
        self.assertIn("summed 4 circuit(s)", label)
        for entity in entities:
            self.assertIn(entity, label)
        self.assertNotIn("nimbus_mirror_whole_house_load_forecast", label)

    def test_single_entry_list_still_uses_summed_path_not_single_sensor(self):
        # Even ONE entry must win outright -- matches the README's own
        # documented "the instant it has even one entry" wording exactly.
        label = solver_writer.resolve_load_forecast_source_label(
            ["sensor.nimbus_mirror_pool_1_forecast"],
            "sensor.nimbus_mirror_whole_house_load_forecast",
        )
        self.assertIn("summed 1 circuit(s)", label)
        self.assertIn("sensor.nimbus_mirror_pool_1_forecast", label)


if __name__ == "__main__":
    unittest.main()
