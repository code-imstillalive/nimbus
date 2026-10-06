"""nimbus #1067 / #448: the Solver's Sources step inherits what HA's Energy
Dashboard already knows.

## The direction this serves

From the household, 2026-09-26:

> we are trying to focus on making nimbus wizard simpler for anyone who has less
> skills than us… mark numerous times has already told you he doesnt want more
> fields and buttons… simple smarter menu, wizard less complex and quicker and
> more easily identifiable… perhaps auto inheriting some parts if available

And Mark's standing principle, from #449 and restated on #485:

> As a general principal we should be using less/ remove wizard configuration
> values and more sensible default which are surfaced as configuration entities
> on the respective devices.

So this adds **no field and no button**. It fills in what HA already knows,
which strictly *reduces* the decisions needed to get a working Solver.

## Why the Sources step specifically

#1067 names it: the form with the most fields (16) and *"the highest chance of
being pointed somewhere wrong (see #111 and #118 for what 'pointed somewhere
wrong' costs)"*. The switchboard form has had Energy-Dashboard suggestions since
#554. The Solver's own sources never got them.

## The two safeguards, inherited wholesale

They are the answer to the household's own question about the switchboard
version — *"how would we know its correctness?"*:

1. **Type safety** — a candidate is checked against what its own state actually
   looks like. `sensor.grid_active_power` LOOKS like the obvious grid sensor and
   is really a HAEO forecast on this very install (`topology_map.yaml`'s own
   note). That class of mistake is what this catches.
2. **Never silent** — suggestions are `suggested_value` only, on genuinely unset
   fields, and a human still submits the form. `{**suggestions, **existing}`, so
   a saved value always wins. `TestASavedValueAlwaysWins` is the load-bearing
   test here.

## The restraint is the interesting part

Two things are deliberately **not** suggested, and both would have been easy:

* **Power sensors.** The Energy Dashboard holds **energy** statistics (kWh,
  `total_increasing`); Nimbus wants **power** (kW). Different quantities, different
  entities. The tempting bridge — "find the power sensor on the same device" — is
  a guess wearing discovery's clothes, and putting an energy stat into a power
  field is exactly the wrong-kind error safeguard 1 exists to stop, arriving from
  the inside.
* **Forecast sensors.** The solar source names a forecast **config entry**, not a
  forecast entity, and that mapping differs per integration. A wrong forecast
  entity fails silently and expensively.

## One deliberate divergence from the switchboard sibling

Price entities are accepted on a **numeric state**, not a `device_class`. Real
tariff integrations — Amber, LocalVolts, most others — publish a bare numeric
sensor with no `device_class` at all, so requiring one would reject the very
entities this exists to find. A numeric state is the honest floor: it rules out
`unknown`/`unavailable`/text without pretending to a semantic check it cannot
make. Safeguard 2 covers the rest.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.const import (
    CONF_SOLVER_BATTERY_POWER_SENSOR,
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
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
    and is a HAEO forecast on this very install."""

    def test_a_soc_field_pointing_at_the_wrong_device_class_is_refused(self):
        out = _suggest(
            [{"type": "battery", "stat_soc": "sensor.not_soc"}],
            {
                "sensor.not_soc": _state(
                    state="55", device_class="power", unit_of_measurement="%"
                )
            },
        )
        self.assertNotIn(CONF_SOLVER_BATTERY_SOC_SENSOR, out)

    def test_a_soc_field_with_the_wrong_unit_is_refused(self):
        out = _suggest(
            [{"type": "battery", "stat_soc": "sensor.kwh_not_pct"}],
            {
                "sensor.kwh_not_pct": _state(
                    state="55", device_class="battery", unit_of_measurement="kWh"
                )
            },
        )
        self.assertNotIn(CONF_SOLVER_BATTERY_SOC_SENSOR, out)

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

    def test_no_power_sensor_is_ever_suggested(self):
        """The Energy Dashboard holds ENERGY (kWh); these fields want POWER (kW).
        Bridging them by guessing a sibling entity is the wrong-kind error
        safeguard 1 exists to stop, arriving from the inside."""
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

        grid = inspect.getsource(
            hub_options.NimbusHubOptionsFlow.async_step_solver_grid
        )
        i = grid.index("_energy_dashboard_solver_source_suggestions")
        self.assertIn(
            "**dict(self.config_entry.options)",
            grid[i : i + 400],
            "saved options must be LAST so they win the merge",
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

    def test_the_wizard_key_count_is_unchanged_by_this_feature(self):
        import re

        from custom_components.nimbus_load.flows import hub_options

        src = Path(hub_options.__file__).read_text(encoding="utf-8")
        m = re.search(
            r"_SOLVER_WIZARD_SCHEMA_KEYS[^=]*=\s*\((.*?)\)\s*\n", src, re.DOTALL
        )
        self.assertIsNotNone(m)
        count = len(re.findall(r"CONF_[A-Z0-9_]+", m.group(1)))
        self.assertEqual(
            count,
            27,
            "this feature must not add a wizard field; if the Solver wizard "
            "genuinely grew for another reason, update this number and say why",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
