"""Household modes as presets over the existing levers, never new levers
(#485) (#1837).

Each mode sets preset values for the levers a load already has; `home`
changes nothing and reports nothing, and an unknown mode is home.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import household_modes as hm


class TestHomeIsTheIdentityTransform(unittest.TestCase):
    def test_home_is_absent_from_both_tables(self):
        """Structural, not numeric. A `home` entry full of 1.0 factors
        would pass a value check and still rot the first time a lever was
        added without one."""
        self.assertNotIn("home", hm.SOLVER_PRESETS)
        self.assertNotIn("home", hm.LOAD_PRESETS)
        # nimbus issue #1314: the thermal BOUND table is held to the same
        # rule -- a `home` entry of +0.0 degrees would pass a value check
        # and rot the first moment a band lever was added without one.
        self.assertNotIn("home", hm.THERMAL_BAND_PRESETS)


class TestPresetsAreRelativeToTheHouseholdsOwnValue(unittest.TestCase):
    def test_every_table_entry_uses_a_known_operation(self):
        """Guards a typo'd op string, which would otherwise silently fall
        through to the delta branch and add where it meant to multiply."""
        for table in (hm.SOLVER_PRESETS, hm.LOAD_PRESETS, hm.THERMAL_BAND_PRESETS):
            for mode, levers in table.items():
                for key, (op, _operand) in levers.items():
                    with self.subTest(mode=mode, key=key):
                        self.assertIn(op, (hm.MULTIPLY, hm.DELTA))


class TestTheTablesOnlyTouchRealLevers(unittest.TestCase):
    """A preset naming a key nothing reads is dead config that looks
    live -- the failure mode is that someone switches to `away` and
    nothing happens, with no error to explain why."""

    def test_every_load_lever_is_a_real_config_key(self):
        # test_every_load_lever_is_a_real_config_key: Widened from "is a live-tunable `number.*` key" on 2026-09-15,
        from custom_components.nimbus_load import const

        known = {
            getattr(const, name)
            for name in dir(const)
            if name.startswith("CONF_") and isinstance(getattr(const, name), str)
        }
        for mode, levers in hm.LOAD_PRESETS.items():
            for key in levers:
                with self.subTest(mode=mode, key=key):
                    self.assertIn(key, known)

        # test_the_live_tunable_levers_are_still_live_tunable: The half of the old assertion worth keeping: a preset key that
        from custom_components.nimbus_load import solver_writer as sw

        live = set(sw._CONTROLLABLE_LOAD_LIVE_NUMBER_KEYS)
        # Deliberately wizard-only, no live `number.*` entity of their
        # own; see the test above for why "names a real config key" is
        # the right invariant and this one is the narrower half.
        wizard_only = {
            "deferrable_value_per_kwh",
            # nimbus issue #1314: the price of a mid-day reheat. A cost
            # lever, so it lives in LOAD_PRESETS rather than the band
            # table -- the band table holds BOUNDS only.
            "thermal_comfort_floor_cost",
        }
        for mode, levers in hm.LOAD_PRESETS.items():
            for key in levers:
                if key in wizard_only:
                    continue
                with self.subTest(mode=mode, key=key):
                    self.assertIn(key, live)

        # test_every_solver_lever_is_a_real_configured_key
        from custom_components.nimbus_load import const

        known = {
            getattr(const, name)
            for name in dir(const)
            if name.startswith("CONF_") and isinstance(getattr(const, name), str)
        }
        for mode, levers in hm.SOLVER_PRESETS.items():
            for key in levers:
                with self.subTest(mode=mode, key=key):
                    self.assertIn(key, known)

        # test_every_mode_named_in_a_table_is_a_real_selectable_mode
        from custom_components.nimbus_load import const

        for table in (hm.SOLVER_PRESETS, hm.LOAD_PRESETS, hm.THERMAL_BAND_PRESETS):
            for mode in table:
                with self.subTest(mode=mode):
                    self.assertIn(mode, const.HOUSEHOLD_MODES)


if __name__ == "__main__":
    unittest.main()


class TestTheHistoricalScorersKeepTheUnmodedBaseline(unittest.TestCase):
    """The second-order bug this feature can cause, caught before merge.

    `main()` applies the mode preset to `cfg` and then passes `cfg` to
    four publishes that score a **past** day. Yesterday was not run under
    today's mode, so pricing it with an `away` degradation cost it never
    actually paid would shift `j_ach`/`j_star`, and therefore EPR and
    regret, silently.

    Nimbus does not record which mode was in force on a past day -- that
    is a real new capability, not a lookup -- so the honest answer is to
    score against the household's own baseline. This test exists because
    `cfg_unmoded` looks redundant next to `cfg` and is exactly the kind of
    variable a later tidy-up deletes.
    """

    def _run_main_capturing(self, mode):
        import urllib.error
        from unittest.mock import patch

        import solver_shared
        import solver_writer
        from _isolated_state import isolated_state_paths
        from test_main_golden_output_guardrail import _SOLVER_CONFIG_ATTRS

        attrs = dict(_SOLVER_CONFIG_ATTRS)
        attrs["household_mode"] = mode
        attrs["solver_degradation_cost_per_kwh"] = 0.02
        known = {
            "sensor.nimbus_solver_config": {"state": "configured", "attributes": attrs},
            "sensor.fake_soc": {"state": "55.0", "attributes": {}},
            "sensor.fake_import_price": {"state": "0.30", "attributes": {}},
            "sensor.fake_export_price": {"state": "0.05", "attributes": {}},
        }
        from test_main_golden_output_guardrail import _HEALTHY_LOAD_STATE, _LOAD_SENSOR

        known[_LOAD_SENSOR] = _HEALTHY_LOAD_STATE

        def _ha_get(entity_id):
            if entity_id in known:
                return known[entity_id]
            raise urllib.error.HTTPError(entity_id, 404, "not found", {}, None)

        seen = {}

        def _capture_quality(cfg, now):
            seen["cfg"] = cfg

        with (
            patch.object(solver_writer, "ha_get", side_effect=_ha_get),
            # nimbus #1302 (spec 003 step 2): safe_num moved to
            # solver_shared.py and resolves ha_get from THAT namespace, so
            # patching only solver_writer.ha_get lets its call escape to a
            # real HA. main()'s own reads still go through solver_writer's
            # alias, so BOTH targets are needed -- not one instead of the
            # other.
            patch.object(solver_shared, "ha_get", side_effect=_ha_get),
            patch.object(solver_writer, "ha_post_state"),
            patch.object(solver_writer, "acquire_lock", return_value=True),
            patch.object(solver_writer, "release_lock"),
            patch.object(
                solver_writer,
                "publish_daily_quality_report",
                side_effect=_capture_quality,
            ),
            patch.object(solver_writer, "publish_daily_flex_report"),
            patch.object(solver_writer, "publish_nimbus_only_soc_counterfactual"),
            patch.object(solver_writer, "publish_efficiency_backtest_report"),
            # nimbus issue #1330: all four persisted paths, not just this one.
            isolated_state_paths(solver_writer),
        ):
            solver_writer.main()
        return seen.get("cfg")

    def test_the_scorer_sees_the_baseline_not_the_moded_value(self):
        cfg = self._run_main_capturing("away")
        self.assertIsNotNone(cfg, "publish_daily_quality_report was never called")
        self.assertAlmostEqual(
            float(cfg["solver_degradation_cost_per_kwh"]),
            0.02,
            msg="the historical scorer received a mode-adjusted degradation "
            "cost -- it would price a past day with a lever that day never "
            "actually ran under",
        )
