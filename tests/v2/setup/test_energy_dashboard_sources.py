"""The Solver's Sources step inherits what Home Assistant's Energy
Dashboard already knows: its solar, battery and grid sensors become
suggestions for empty fields, never overwrite a saved one, and add no new
field (#1067, #448) (#1837).
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.const import (
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
)
from custom_components.nimbus_load.flows.hub_options import (
    _energy_dashboard_solver_source_suggestions,
)


def _state(**attrs):
    st = MagicMock()
    st.state = attrs.pop("state", "0.0")
    st.attributes = attrs
    return st


def _hass(states: dict[str, object]):
    hass = MagicMock()
    hass.states.get.side_effect = lambda entity_id: states.get(entity_id)
    return hass


def _suggest(sources, states):
    manager = MagicMock(data={"energy_sources": sources})
    with patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=AsyncMock(return_value=manager),
    ):
        return asyncio.run(_energy_dashboard_solver_source_suggestions(_hass(states)))


_SOC = _state(state="55", device_class="battery", unit_of_measurement="%")
_PRICE = _state(state="0.31")


class TestTheThingsItCanHonestlyInherit(unittest.TestCase):
    def test_battery_soc(self):
        """`stat_soc` is the one Energy Dashboard field that is already the exact
        quantity Nimbus wants -- which is why #554's sibling could use it
        directly too."""
        out = _suggest(
            [{"type": "battery", "stat_soc": "sensor.pack_soc"}],
            {"sensor.pack_soc": _SOC},
        )
        self.assertEqual(out[CONF_SOLVER_BATTERY_SOC_SENSOR], "sensor.pack_soc")

    def test_import_and_export_price(self):
        out = _suggest(
            [
                {
                    "type": "grid",
                    "flow_from": [{"entity_energy_price": "sensor.buy_price"}],
                    "flow_to": [{"entity_energy_price": "sensor.sell_price"}],
                }
            ],
            {"sensor.buy_price": _PRICE, "sensor.sell_price": _PRICE},
        )
        self.assertEqual(out[CONF_SOLVER_IMPORT_PRICE_SENSOR], "sensor.buy_price")
        self.assertEqual(out[CONF_SOLVER_EXPORT_PRICE_SENSOR], "sensor.sell_price")

    def test_import_and_export_price_on_the_current_flat_grid_schema(self):
        """nimbus #1589: current Home Assistant (2026.7, read live from the
        reference household) stores a grid source flat, with no flow_from /
        flow_to lists. This found nothing before the fix."""
        out = _suggest(
            [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.import_kwh",
                    "stat_energy_to": "sensor.export_kwh",
                    "entity_energy_price": "sensor.buy_price",
                    "entity_energy_price_export": "sensor.sell_price",
                }
            ],
            {"sensor.buy_price": _PRICE, "sensor.sell_price": _PRICE},
        )
        self.assertEqual(out[CONF_SOLVER_IMPORT_PRICE_SENSOR], "sensor.buy_price")
        self.assertEqual(out[CONF_SOLVER_EXPORT_PRICE_SENSOR], "sensor.sell_price")

    def test_a_price_sensor_with_no_device_class_is_still_accepted(self):
        """The deliberate divergence from the switchboard sibling. Amber,
        LocalVolts and most tariff integrations publish a bare numeric sensor
        with no device_class -- requiring one would reject the very entities this
        exists to find."""
        out = _suggest(
            [
                {
                    "type": "grid",
                    "flow_from": [{"entity_energy_price": "sensor.amber_general"}],
                }
            ],
            {"sensor.amber_general": _state(state="0.42")},
        )
        self.assertEqual(out[CONF_SOLVER_IMPORT_PRICE_SENSOR], "sensor.amber_general")


class TestTypeSafety(unittest.TestCase):
    """Safeguard 1. `sensor.grid_active_power` looks like the obvious grid sensor
    and is another optimiser's forecast on this very install."""

    def test_a_non_numeric_price_is_refused(self):
        """`unavailable` at wizard time is the common real case."""
        out = _suggest(
            [
                {
                    "type": "grid",
                    "flow_from": [{"entity_energy_price": "sensor.broken"}],
                }
            ],
            {"sensor.broken": _state(state="unavailable")},
        )
        self.assertNotIn(CONF_SOLVER_IMPORT_PRICE_SENSOR, out)

    def test_an_entity_that_does_not_exist_is_refused(self):
        """A stale Energy Dashboard config outliving a renamed entity."""
        out = _suggest(
            [
                {
                    "type": "grid",
                    "flow_from": [{"entity_energy_price": "sensor.gone"}],
                }
            ],
            {},
        )
        self.assertEqual(out, {})


class TestTheRestraint(unittest.TestCase):
    """What it deliberately does NOT offer, pinned so a later change has to
    argue for it rather than drift into it."""

    def test_a_fixed_number_price_is_not_treated_as_a_sensor(self):
        """`number_energy_price` is a static figure. Nimbus wants a live sensor
        it can resample per period, so a fixed number is not a substitute."""
        out = _suggest(
            [{"type": "grid", "flow_from": [{"number_energy_price": 0.28}]}],
            {},
        )
        self.assertNotIn(CONF_SOLVER_IMPORT_PRICE_SENSOR, out)


class TestASavedValueAlwaysWins(unittest.TestCase):
    """Safeguard 2, and the load-bearing property: a suggestion must never
    overwrite something the household already configured. The call site merges
    `{**suggestions, **existing}` -- saved keys on the right win."""

    def test_the_merge_order_prefers_existing(self):
        suggestions = {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.suggested"}
        existing = {CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.configured_by_hand"}
        merged = {**suggestions, **existing}
        self.assertEqual(
            merged[CONF_SOLVER_IMPORT_PRICE_SENSOR], "sensor.configured_by_hand"
        )


class TestItNeverBreaksTheWizard(unittest.TestCase):
    """A household with no Energy Dashboard must see an unfilled form, never a
    broken step. Same posture as `_discover_nimbus_load_forecast_candidates()`."""

    def test_no_energy_sources_at_all(self):
        self.assertEqual(_suggest([], {}), {})

    def test_a_manager_that_raises(self):
        with patch(
            "homeassistant.components.energy.data.async_get_manager",
            new=AsyncMock(side_effect=RuntimeError("no energy component")),
        ):
            out = asyncio.run(_energy_dashboard_solver_source_suggestions(_hass({})))
        self.assertEqual(out, {})

    def test_a_manager_with_no_data(self):
        manager = MagicMock(data=None)
        with patch(
            "homeassistant.components.energy.data.async_get_manager",
            new=AsyncMock(return_value=manager),
        ):
            out = asyncio.run(_energy_dashboard_solver_source_suggestions(_hass({})))
        self.assertEqual(out, {})

    def test_a_malformed_source_entry(self):
        out = _suggest(["not a dict", {"type": "grid"}], {})
        self.assertEqual(out, {})

    def test_one_usable_source_survives_a_malformed_sibling(self):
        out = _suggest(
            ["junk", {"type": "battery", "stat_soc": "sensor.pack_soc"}],
            {"sensor.pack_soc": _SOC},
        )
        self.assertEqual(out[CONF_SOLVER_BATTERY_SOC_SENSOR], "sensor.pack_soc")


class TestNoFieldWasAdded(unittest.TestCase):
    """The household's steer, and Mark's, both say the same thing: no more
    fields. This change must reduce typing without growing the form."""


if __name__ == "__main__":
    unittest.main(verbosity=2)
