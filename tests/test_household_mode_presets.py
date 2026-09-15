"""nimbus issue #485: household-mode presets over the existing levers.

Mark Purcell's steer is the design constraint, and it reversed two
earlier proposals of mine (~125 disabled-by-default sibling entities,
then a wizard field): *"These modes should have preset values for the
existing levers, not generate new levers."*

The properties worth pinning are the ones a plausible re-implementation
gets wrong, and every one of them has already cost this project
something in a different form:

- **`home` must be byte-identical to today.** This issue's own second
  acceptance criterion. A preset table that happened to list `home` with
  factors of 1.0 would satisfy it numerically and still be wrong the
  first time someone added a lever and forgot the 1.0 — so `home` is
  absent from the tables entirely, and that absence is tested.
- **Relative, never absolute.** An absolute would discard real tuning the
  moment a mode was switched, silently, and be wrong by a different
  amount on every install.
- **Unknown/absent modes fail open.** A select entity that has not come
  up yet reads `None`; a household that renames a mode gets a string
  nothing recognises. Neither may reshape a real dispatch.
- **No mutation of the caller's dict.** `_resolve_controllable_load_
  tuning()` hands in a dict derived from subentry data; mutating it in
  place would leak a moded value into the next solve even after the mode
  changed back.

Pure table + two functions, no HA imports, so these are direct calls
with no harness at all.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import household_modes as hm


class TestHomeIsTheIdentityTransform(unittest.TestCase):
    def test_home_is_absent_from_both_tables(self):
        """Structural, not numeric. A `home` entry full of 1.0 factors
        would pass a value check and still rot the first time a lever was
        added without one."""
        self.assertNotIn("home", hm.SOLVER_PRESETS)
        self.assertNotIn("home", hm.LOAD_PRESETS)

    def test_home_returns_the_very_same_object(self):
        """Not merely an equal dict -- the same object, so the identity
        path is provably free and provably a no-op."""
        cfg = {"solver_degradation_cost_per_kwh": 0.02}
        out, applied = hm.apply_to_solver_config(cfg, "home")
        self.assertIs(out, cfg)
        self.assertEqual(applied, {})

    def test_an_unset_mode_is_also_identity(self):
        """`None` is what an install whose select entity has not come up
        yet reports. It must not default to a mode."""
        data = {"deferrable_target_kwh": 4.0}
        out, applied = hm.apply_to_load_config(data, None)
        self.assertIs(out, data)
        self.assertEqual(applied, {})

    def test_an_unrecognised_mode_is_identity_not_an_error(self):
        """A household renaming its modes must never reshape dispatch."""
        data = {"deferrable_target_kwh": 4.0}
        out, applied = hm.apply_to_load_config(data, "holiday-house")
        self.assertIs(out, data)
        self.assertEqual(applied, {})


class TestPresetsAreRelativeToTheHouseholdsOwnValue(unittest.TestCase):
    def test_guests_scales_the_configured_target_not_a_fixed_number(self):
        """Two installs with different baselines must get different
        results -- that is what 'relative' means, and an absolute would
        make these equal."""
        a, _ = hm.apply_to_load_config({"deferrable_target_kwh": 4.0}, "guests")
        b, _ = hm.apply_to_load_config({"deferrable_target_kwh": 10.0}, "guests")
        self.assertNotEqual(a["deferrable_target_kwh"], b["deferrable_target_kwh"])
        self.assertAlmostEqual(a["deferrable_target_kwh"], 5.2)
        self.assertAlmostEqual(b["deferrable_target_kwh"], 13.0)

    def test_away_reduces_the_target_and_the_urgency_together(self):
        out, applied = hm.apply_to_load_config(
            {"deferrable_target_kwh": 4.0, "deferrable_shortfall_price": 2.0}, "away"
        )
        self.assertAlmostEqual(out["deferrable_target_kwh"], 2.0)
        self.assertAlmostEqual(out["deferrable_shortfall_price"], 0.5)
        self.assertEqual(
            set(applied), {"deferrable_target_kwh", "deferrable_shortfall_price"}
        )

    def test_a_delta_preset_adds_rather_than_scales(self):
        out, _ = hm.apply_to_load_config(
            {"controllable_load_max_activations_per_day": 3.0}, "away"
        )
        self.assertAlmostEqual(out["controllable_load_max_activations_per_day"], 2.0)

    def test_every_table_entry_uses_a_known_operation(self):
        """Guards a typo'd op string, which would otherwise silently fall
        through to the delta branch and add where it meant to multiply."""
        for table in (hm.SOLVER_PRESETS, hm.LOAD_PRESETS):
            for mode, levers in table.items():
                for key, (op, _operand) in levers.items():
                    with self.subTest(mode=mode, key=key):
                        self.assertIn(op, (hm.MULTIPLY, hm.DELTA))


class TestItRefusesToInventABaseline(unittest.TestCase):
    def test_a_lever_this_install_does_not_configure_is_skipped(self):
        """Scaling a value that is not there would mean inventing one --
        the absolute this whole design exists to avoid."""
        out, applied = hm.apply_to_load_config({"something_else": 1.0}, "away")
        self.assertEqual(out, {"something_else": 1.0})
        self.assertEqual(applied, {})

    def test_a_non_numeric_value_is_left_alone(self):
        """Several config keys hold entity ids rather than numbers. A
        preset must not crash a real solve over one."""
        out, applied = hm.apply_to_load_config(
            {"deferrable_target_kwh": "sensor.something"}, "away"
        )
        self.assertEqual(out["deferrable_target_kwh"], "sensor.something")
        self.assertEqual(applied, {})

    def test_a_preset_never_drives_a_lever_below_zero(self):
        """Every key in these tables is a price, an energy target or a
        count. A negative would be meaningless, and a delta preset on a
        small baseline is the way to produce one by accident."""
        out, _ = hm.apply_to_load_config(
            {"controllable_load_max_activations_per_day": 0.0}, "away"
        )
        self.assertGreaterEqual(out["controllable_load_max_activations_per_day"], 0.0)


class TestTheCallersDictIsNeverMutated(unittest.TestCase):
    def test_the_input_is_unchanged_after_a_real_transform(self):
        """`_resolve_controllable_load_tuning()` builds its dict from
        subentry data. Mutating in place would leak a moded value into
        the next solve after the mode changed back."""
        data = {"deferrable_target_kwh": 4.0}
        out, _ = hm.apply_to_load_config(data, "guests")
        self.assertEqual(data["deferrable_target_kwh"], 4.0)
        self.assertIsNot(out, data)


class TestTheTablesOnlyTouchRealLevers(unittest.TestCase):
    """A preset naming a key nothing reads is dead config that looks
    live -- the failure mode is that someone switches to `away` and
    nothing happens, with no error to explain why."""

    def test_every_load_lever_is_a_real_config_key(self):
        """Widened from "is a live-tunable `number.*` key" on 2026-09-15,
        when the pool-heater price gate landed.

        `deferrable_value_per_kwh` is a real, wizard-saved config key and
        is deliberately NOT in `_CONTROLLABLE_LOAD_LIVE_NUMBER_KEYS` — it
        has no live `number.*` entity. The preset still reaches it,
        because `_resolve_controllable_load_tuning()` starts from the
        subentry's own data and the live overlay only adds to that.

        So the right invariant is "names a real config key", not "names a
        live-tunable one" — the narrower version would have blocked a
        legitimate lever. Kept rather than dropped: a preset naming a key
        nothing reads is dead config that looks live, and a household
        would see a mode switch do nothing with no error to explain it.
        """
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

    def test_the_live_tunable_levers_are_still_live_tunable(self):
        """The half of the old assertion worth keeping: a preset key that
        DOES have a live `number.*` entity must stay in that list, so a
        lever cannot silently lose its live overlay."""
        from custom_components.nimbus_load import solver_writer as sw

        live = set(sw._CONTROLLABLE_LOAD_LIVE_NUMBER_KEYS)
        # The price gate is deliberately wizard-only; see the test above.
        wizard_only = {"deferrable_value_per_kwh"}
        for mode, levers in hm.LOAD_PRESETS.items():
            for key in levers:
                if key in wizard_only:
                    continue
                with self.subTest(mode=mode, key=key):
                    self.assertIn(key, live)

    def test_every_solver_lever_is_a_real_configured_key(self):
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

    def test_every_mode_named_in_a_table_is_a_real_selectable_mode(self):
        from custom_components.nimbus_load import const

        for table in (hm.SOLVER_PRESETS, hm.LOAD_PRESETS):
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

        import solver_writer
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
            patch.object(
                solver_writer, "PLAN_STATE_PATH", "/tmp/nonexistent_mode_test.json"
            ),
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

    def test_the_preset_really_would_have_changed_it(self):
        """Without this, the test above passes even if the preset silently
        stopped working -- both values would be the baseline."""
        moded, applied = hm.apply_to_solver_config(
            {"solver_degradation_cost_per_kwh": 0.02}, "away"
        )
        self.assertNotAlmostEqual(moded["solver_degradation_cost_per_kwh"], 0.02)
        self.assertIn("solver_degradation_cost_per_kwh", applied)
