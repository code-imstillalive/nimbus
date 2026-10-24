"""nimbus #1067: the Sources step reads the Energy Dashboard in its own
order, and the feature added no wizard key. Checks that read source text,
kept apart until the decision on source-reading tests (#1837); the
inheritance itself is in tests/v2/setup/test_energy_dashboard_sources.py."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestASavedValueAlwaysWins(unittest.TestCase):
    def test_the_call_site_uses_that_order(self):
        """Pinned on the source, because reversing these two words silently
        overwrites real configuration on every wizard open -- the exact class of
        wizard-wipe bug this repo has already had three of.

        **Corrected:** this used to inspect `async_step_solver_sources`, which
        is where #1267 originally wired the helper -- and that wiring was the
        bug. None of the three suggested keys is in the Sources schema, so
        every suggestion was silently dropped and the whole feature was inert.
        It is now called from the two steps that actually own those fields.
        See `test_1067_suggestions_reach_the_form_they_are_wired_to.py` for the
        guard that catches this class of mistake in general.
        """
        import inspect

        from custom_components.nimbus_load.flows import hub_options

        battery = inspect.getsource(
            hub_options.NimbusHubOptionsFlow.async_step_solver_battery
        )
        self.assertIn(
            "**energy_dashboard, **existing}",
            battery,
            "saved options must be LAST so they win the merge",
        )

        # nimbus #1686: the grid step no longer pre-fills prices at all; the
        # Energy Dashboard's price entities are proposed under Review pricing
        # suggestions, for empty fields only.
        grid = inspect.getsource(
            hub_options.NimbusHubOptionsFlow.async_step_solver_grid
        )
        self.assertNotIn("_energy_dashboard_solver_source_suggestions", grid)


class TestNoFieldWasAdded(unittest.TestCase):
    def test_the_wizard_key_count_is_unchanged_by_this_feature(self):
        import re

        from custom_components.nimbus_load.flows import hub_options

        src = Path(hub_options.__file__).read_text(encoding="utf-8")
        m = re.search(
            r"_SOLVER_WIZARD_SCHEMA_KEYS[^=]*=\s*\((.*?)\)\s*\n", src, re.DOTALL
        )
        self.assertIsNotNone(m)
        count = len(re.findall(r"CONF_[A-Z0-9_]+", m.group(1)))
        # 27 -> 28: nimbus #1537 item 3 (second P2P matched-rate source),
        # not this feature. 28 -> 29: nimbus #1786's comparison-only price
        # sources, not this feature either. 29 -> 31: nimbus #1786's two
        # loss-factor code sensors (see test_config_surface_budget.py).
        self.assertEqual(
            count,
            31,
            "this feature must not add a wizard field; if the Solver wizard "
            "genuinely grew for another reason, update this number and say why",
        )
