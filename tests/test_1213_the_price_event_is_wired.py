"""nimbus #1213: the price-event simulation is wired in after the blend and
the settled block, to import and export alike, and its arm, field and help
text exist. Checks that read source text, kept apart until the decision on
source-reading tests (#1837); the delta itself is in
tests/v2/orchestration/test_writer_inputs.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import solver_writer


class TestItIsAppliedSymmetricallyAndLast(unittest.TestCase):
    """A real wholesale price event moves the whole market, not one side,
    and #1213 asks for it at the very end of price resolution."""

    def test_main_adds_the_delta_to_both_import_and_export(self):
        import ast
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(solver_writer.main))
        self.assertIn("resolve_price_event_delta(cfg, grid_times)", src)
        # Both series, same delta.
        self.assertIn("import_price = [p + d for p, d in zip(", src)
        self.assertIn("export_price = [p + d for p, d in zip(", src)
        # And it really is a call, not only a mention in a comment.
        called = {
            node.func.id
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("resolve_price_event_delta", called)

    def test_it_is_applied_after_the_blend_and_the_settled_block(self):
        """Ordering is the whole reason it can stay ignorant of the
        blending and settlement logic that produced the series."""
        import inspect

        src = inspect.getsource(solver_writer.main)
        blend_at = src.rindex("blend_price_with_secondary_sources(")
        event_at = src.index("resolve_price_event_delta(cfg, grid_times)")
        self.assertGreater(
            event_at,
            blend_at,
            "the price event must be layered on top of the blended series, "
            "not before it",
        )


class TestTheConfigSurfaceExists(unittest.TestCase):
    """Two fields, a switch, and the wizard merge -- an option the wizard
    does not carry through would be silently dropped by the next step."""

    def test_the_const_keys_exist_and_default_off(self):
        from custom_components.nimbus_load import const

        self.assertEqual(
            const.CONF_SOLVER_PRICE_EVENT_SENSOR, "solver_price_event_sensor"
        )
        self.assertEqual(
            const.CONF_SOLVER_PRICE_EVENT_ENABLED, "solver_price_event_enabled"
        )
        self.assertFalse(const.DEFAULT_SOLVER_PRICE_EVENT_ENABLED)

    def test_the_sensor_is_carried_through_the_wizard_merge(self):
        from custom_components.nimbus_load.flows import hub_options

        self.assertIn(
            "solver_price_event_sensor",
            hub_options._SOLVER_WIZARD_SCHEMA_KEYS,
        )

    def test_a_switch_entity_is_declared_for_the_arm(self):
        src = Path(
            Path(__file__).resolve().parent.parent
            / "custom_components"
            / "nimbus_load"
            / "switch.py"
        ).read_text(encoding="utf-8")
        self.assertIn("CONF_SOLVER_PRICE_EVENT_ENABLED", src)
        self.assertIn("Price Event Simulation Armed", src)

    def test_both_string_files_describe_the_new_field(self):
        """CI checks strings/translations parity; this says plainly what
        the field is for, including the close-your-window requirement."""
        import json

        base = (
            Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"
        )
        for name in ("strings.json", "translations/en.json"):
            with self.subTest(file=name):
                blob = json.loads((base / name).read_text(encoding="utf-8"))
                dumped = json.dumps(blob)
                self.assertIn("solver_price_event_sensor", dumped)
                # The close-your-window requirement must be stated, since
                # it is the one mistake that turns a test into a permanent
                # distortion. Asserted on the two load-bearing phrases
                # rather than an exact sentence -- and NOT on a literal
                # brace pair, because HA's own hassfest reads {...} in a
                # translation as a placeholder and rejects the file.
                self.assertIn("value of 0", dumped)
                self.assertIn("holds forward", dumped)
                self.assertNotIn(
                    "{",
                    blob["options"]["step"]["solver_grid"]["data_description"][
                        "solver_price_event_sensor"
                    ],
                )


if __name__ == "__main__":
    unittest.main()
