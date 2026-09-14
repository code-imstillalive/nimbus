"""nimbus issue #849: the flattened fan-out gained string-valued
children (`sensor.nimbus_solver_lp_status`,
`sensor.nimbus_solver_binding_constraint_now`), and a string state can
grow past Home Assistant's own 255-character state limit in a way a
number never could.

HA rejects an over-long state outright and logs an error, which leaves
the entity stuck at its previous value with no visible reason -- the
"silently stops working" shape this project keeps getting bitten by
(#538/#692/#837 are all the same class). `_clamp_state_length()`
truncates instead, so the sensor keeps updating.

Why this matters concretely rather than theoretically: `load_forecast_
source_used` measured **762 characters** on the reference household
(it names every summed circuit). It is deliberately NOT published as a
child for exactly that reason -- but `binding_constraint_now` is a real,
already-published string whose text is generated from the LP's own
binding constraint, and nothing bounds it as the model grows.
"""

from __future__ import annotations

import unittest

import _solver_path  # noqa: F401  -- side-effect: puts solver/ + ml/ on sys.path
from _ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.nimbus_load import sensor_flattened


class TestStateLengthConstantDoesNotDrift(unittest.TestCase):
    def test_local_constant_matches_home_assistants_own(self):
        """`_MAX_STATE_LENGTH` is defined locally rather than imported,
        because this project's test harness stubs `homeassistant.const`
        with only the names it needs and importing a new one from it
        breaks collection for every test touching sensor_flattened.

        That's a real constraint, but a hand-copied constant can drift
        silently if HA ever changes theirs. When a real homeassistant is
        importable, assert the two agree; skip cleanly when it isn't, so
        this never becomes the reason the suite can't run somewhere.
        """
        try:
            from homeassistant.const import MAX_LENGTH_STATE_STATE
        except ImportError:  # pragma: no cover - stubbed/absent HA
            self.skipTest("real homeassistant.const not importable here")
        self.assertEqual(
            sensor_flattened._MAX_STATE_LENGTH,
            MAX_LENGTH_STATE_STATE,
            "sensor_flattened._MAX_STATE_LENGTH has drifted from Home "
            "Assistant's own MAX_LENGTH_STATE_STATE -- update it (and the "
            "comment above it) to match.",
        )


class TestClampStateLength(unittest.TestCase):
    def setUp(self):
        sensor_flattened._LOGGED_TRUNCATION.clear()

    def test_non_strings_pass_through_untouched(self):
        """Every pre-#849 flattened child carries a number. The guard runs
        on all of them, so it must be a genuine no-op for non-strings --
        including the falsy ones, which a careless truthiness check would
        mangle."""
        for value in (0, 0.0, -1.5, 12345.6789, None, True, False):
            with self.subTest(value=value):
                self.assertIs(
                    sensor_flattened._clamp_state_length(value, "sensor.x"), value
                )

    def test_a_short_string_is_returned_unchanged(self):
        for value in ("", "optimal", "Grid export at zero (not economical right now)"):
            with self.subTest(value=value):
                self.assertEqual(
                    sensor_flattened._clamp_state_length(value, "sensor.x"), value
                )

    def test_a_string_exactly_at_the_limit_is_not_truncated(self):
        """Boundary: HA's limit is inclusive, so a state of exactly
        _MAX_STATE_LENGTH characters is legal and must survive intact."""
        value = "x" * sensor_flattened._MAX_STATE_LENGTH
        out = sensor_flattened._clamp_state_length(value, "sensor.x")
        self.assertEqual(out, value)
        self.assertEqual(len(out), sensor_flattened._MAX_STATE_LENGTH)

    def test_one_character_over_is_truncated_to_the_limit(self):
        value = "x" * (sensor_flattened._MAX_STATE_LENGTH + 1)
        out = sensor_flattened._clamp_state_length(value, "sensor.x")
        self.assertEqual(len(out), sensor_flattened._MAX_STATE_LENGTH)
        self.assertTrue(out.endswith(sensor_flattened._STATE_TRUNCATION_SUFFIX))

    def test_the_real_762_character_case_is_brought_under_the_limit(self):
        """`load_forecast_source_used`'s real measured length on the
        reference household. It isn't published as a child today, but this
        is the magnitude the guard exists for."""
        value = "sensor.nimbus_cb_pw_hws_l1_power_forecast, " * 19
        self.assertGreater(len(value), 700)
        out = sensor_flattened._clamp_state_length(value, "sensor.x")
        self.assertLessEqual(len(out), sensor_flattened._MAX_STATE_LENGTH)

    def test_truncation_preserves_the_leading_text(self):
        """A truncated diagnostic is only useful if the front survives --
        the first characters are what a human actually reads."""
        value = "Grid export at zero (not economical right now) " + "y" * 400
        out = sensor_flattened._clamp_state_length(value, "sensor.x")
        self.assertTrue(
            out.startswith("Grid export at zero (not economical right now)")
        )

    def test_warns_once_per_entity_not_every_solve(self):
        """The solve loop runs every minute. Warning per-solve would
        reproduce the exact log-flooding problem #849 was found while
        investigating (recorder warnings were ~45% of devhub log volume)."""
        value = "z" * 500
        with self.assertLogs(sensor_flattened._LOGGER, level="WARNING") as caught:
            for _ in range(5):
                sensor_flattened._clamp_state_length(value, "sensor.same_entity")
        self.assertEqual(len(caught.output), 1, caught.output)

    def test_a_different_entity_gets_its_own_warning(self):
        value = "z" * 500
        with self.assertLogs(sensor_flattened._LOGGER, level="WARNING") as caught:
            sensor_flattened._clamp_state_length(value, "sensor.first")
            sensor_flattened._clamp_state_length(value, "sensor.second")
        self.assertEqual(len(caught.output), 2, caught.output)


class TestNewNumericChildrenExist(unittest.TestCase):
    """#849's second correction: the issue claimed the #465 fan-out
    already covered everything numeric. Diffing the parent's live
    attributes against every declared source_key showed six numeric,
    genuinely variable attributes with no child at all -- so they had no
    history anywhere, contrary to the issue's own text."""

    _EXPECTED = (
        "envelope_import_limit_kw",
        "envelope_export_limit_kw",
        "solar_risk_effect_now_kw",
        "import_price_risk_effect_now",
        "export_price_risk_effect_now",
        "load_whole_house_live_now_kw",
    )

    def _all_source_keys(self):
        keys = set()
        for name in dir(sensor_flattened):
            if not name.startswith("FLATTENED_ATTRS"):
                continue
            for spec in getattr(sensor_flattened, name):
                keys.add(spec.source_key)
        return keys

    def test_each_previously_unflattened_numeric_attribute_now_has_a_child(self):
        keys = self._all_source_keys()
        for source_key in self._EXPECTED:
            with self.subTest(source_key=source_key):
                self.assertIn(source_key, keys)

    def test_near_constant_and_oversized_attributes_are_deliberately_absent(self):
        """Guards the judgement, not just the additions. These were
        excluded on measured grounds -- constants gain nothing from
        history, dicts aren't scalar states, and load_forecast_source_used
        is 762 characters. If a future pass adds them it should have to
        delete this assertion and say why."""
        keys = self._all_source_keys()
        for source_key in (
            "battery_kw_side",
            "battery_kw_sign_convention",
            "efficiency_convention",
            "load_forecast_source_used",
            "solve_diagnostics",
        ):
            with self.subTest(source_key=source_key):
                self.assertNotIn(source_key, keys)


if __name__ == "__main__":
    unittest.main()
