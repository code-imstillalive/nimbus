"""nimbus #1550: select.nimbus_pricing_source.

Lists the complete pricing profiles Nimbus detects plus Manual. Its current
option is derived from the Solver's saved pricing fields. Picking a provider
validates the profile, then writes both directions in one options update.
A failed check changes nothing. Manual changes nothing.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

from custom_components.nimbus_load import pricing_source as ps
from custom_components.nimbus_load import select as nimbus_select
from custom_components.nimbus_load.const import (
    CONF_SOLVER_EXPORT_PRICE_SENSOR as EXP,
)
from custom_components.nimbus_load.const import (
    CONF_SOLVER_IMPORT_PRICE_SENSOR as IMP,
)
from custom_components.nimbus_load.const import (
    CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE as FEE,
)
from custom_components.nimbus_load.const import (
    CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR as ARR,
)

LV = {
    IMP: "sensor.lv_buy",
    EXP: "sensor.lv_sell",
    ARR: "sensor.lv_forecast",
    "solver_p2p_matched_rate_forecast_sensor": "sensor.lv_p2p",
    "solver_p2p_settlement_history_sensor": "sensor.lv_hist",
}
AMBER = {IMP: "sensor.amber_general", EXP: "sensor.amber_feed_in", ARR: None}


def _hass(states: dict[str, str]):
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda eid: (
                SimpleNamespace(state=states[eid]) if eid in states else None
            )
        ),
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )


def _profiles(*_):
    return {"LocalVolts v2": dict(LV), "Amber Express": dict(AMBER)}


class TestPureHelpers(unittest.TestCase):
    def test_active_label_follows_the_saved_fields(self):
        profiles = _profiles()
        self.assertEqual(ps.active_label(dict(LV), profiles), "LocalVolts v2")
        amber_saved = {IMP: AMBER[IMP], EXP: AMBER[EXP]}
        self.assertEqual(ps.active_label(amber_saved, profiles), "Amber Express")
        self.assertEqual(
            ps.active_label({IMP: "sensor.my_tariff"}, profiles), ps.MANUAL
        )

    def test_applied_writes_only_the_profile_and_clears_the_array(self):
        options = {**LV, FEE: 0.07, "solver_p2p_block_1_rate_kw": 12}
        out = ps.applied(options, AMBER)
        self.assertEqual(out[IMP], AMBER[IMP])
        self.assertEqual(out[EXP], AMBER[EXP])
        self.assertNotIn(ARR, out)  # cleared, or it would keep overriding
        self.assertEqual(out[FEE], 0.07)  # fees untouched
        self.assertEqual(out["solver_p2p_block_1_rate_kw"], 12)  # P2P untouched
        self.assertEqual(
            out["solver_p2p_settlement_history_sensor"],
            LV["solver_p2p_settlement_history_sensor"],
        )

    def test_problems_name_missing_and_unreporting_entities(self):
        hass = _hass(
            {"sensor.amber_general": "0.21", "sensor.amber_feed_in": "unavailable"}
        )
        found = ps.problems(hass, AMBER)
        self.assertEqual(len(found), 1)
        self.assertIn("sensor.amber_feed_in is unavailable", found[0])
        found = ps.problems(_hass({}), AMBER)
        self.assertEqual(len(found), 2)
        self.assertTrue(all("does not exist" in p for p in found))


class TestTheSelect(unittest.TestCase):
    def _select(self, options, states):
        hass = _hass(states)
        entry = SimpleNamespace(entry_id="e1", options=dict(options))
        with patch.object(ps, "detected_profiles", _profiles):
            sel = nimbus_select.NimbusPricingSourceSelect(hass, entry, "0.94.450")
        sel.hass = hass
        sel.async_write_ha_state = MagicMock()
        return sel, hass, entry

    def test_options_and_current_option(self):
        sel, _, _ = self._select(LV, {})
        self.assertEqual(
            sel._attr_options, ["LocalVolts v2", "Amber Express", ps.MANUAL]
        )
        self.assertEqual(sel._attr_current_option, "LocalVolts v2")
        self.assertEqual(sel.entity_id, "select.nimbus_pricing_source")

    def test_switching_writes_both_directions_once(self):
        sel, hass, _entry = self._select(
            LV, {"sensor.amber_general": "0.21", "sensor.amber_feed_in": "0.05"}
        )
        with patch.object(ps, "detected_profiles", _profiles):
            asyncio.run(sel.async_select_option("Amber Express"))
        hass.config_entries.async_update_entry.assert_called_once()
        written = hass.config_entries.async_update_entry.call_args.kwargs["options"]
        self.assertEqual(written[IMP], AMBER[IMP])
        self.assertEqual(written[EXP], AMBER[EXP])
        self.assertNotIn(ARR, written)

    def test_a_failed_check_changes_nothing(self):
        sel, hass, _ = self._select(LV, {"sensor.amber_general": "0.21"})
        with (
            patch.object(ps, "detected_profiles", _profiles),
            self.assertRaises(Exception) as caught,
        ):
            asyncio.run(sel.async_select_option("Amber Express"))
        self.assertIn("nothing changed", str(caught.exception))
        hass.config_entries.async_update_entry.assert_not_called()

    def test_manual_changes_nothing(self):
        sel, hass, _ = self._select(LV, {})
        asyncio.run(sel.async_select_option(ps.MANUAL))
        hass.config_entries.async_update_entry.assert_not_called()
        self.assertEqual(sel._attr_current_option, ps.MANUAL)


if __name__ == "__main__":
    unittest.main()
