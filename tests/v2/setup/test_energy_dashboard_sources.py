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
    CONF_SOLVER_BATTERY_POWER_SENSOR,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
    CONF_SOLVER_SOLAR_POWER_SENSOR,
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


class TestTypeSafety(unittest.TestCase):
    """Safeguard 1. `sensor.grid_active_power` looks like the obvious grid sensor
    and is another optimiser's forecast on this very install."""

    def test_a_soc_field_with_the_wrong_unit_is_refused(self):
        # test_a_soc_field_with_the_wrong_unit_is_refused
        out = _suggest(
            [{"type": "battery", "stat_soc": "sensor.kwh_not_pct"}],
            {
                "sensor.kwh_not_pct": _state(
                    state="55", device_class="battery", unit_of_measurement="kWh"
                )
            },
        )
        self.assertNotIn(CONF_SOLVER_BATTERY_SOC_SENSOR, out)

        # test_an_entity_that_does_not_exist_is_refused: A stale Energy Dashboard config outliving a renamed entity.
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

    def test_no_power_sensor_is_ever_suggested(self):
        # test_no_power_sensor_is_ever_suggested: The Energy Dashboard holds ENERGY (kWh); these fields want POWER (kW).
        out = _suggest(
            [
                {"type": "battery", "stat_energy_from": "sensor.batt_out_kwh"},
                {"type": "solar", "stat_energy_from": "sensor.pv_kwh"},
            ],
            {
                "sensor.batt_out_kwh": _state(
                    state="12", device_class="energy", state_class="total_increasing"
                ),
                "sensor.pv_kwh": _state(
                    state="30", device_class="energy", state_class="total_increasing"
                ),
            },
        )
        self.assertNotIn(CONF_SOLVER_BATTERY_POWER_SENSOR, out)
        self.assertNotIn(CONF_SOLVER_SOLAR_POWER_SENSOR, out)

        # test_a_fixed_number_price_is_not_treated_as_a_sensor: `number_energy_price` is a static figure. Nimbus wants a live sensor
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


class TestNoFieldWasAdded(unittest.TestCase):
    """The household's steer, and Mark's, both say the same thing: no more
    fields. This change must reduce typing without growing the form."""


if __name__ == "__main__":
    unittest.main(verbosity=2)
