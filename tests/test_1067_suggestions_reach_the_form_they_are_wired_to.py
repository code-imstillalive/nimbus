"""nimbus #1067: a wizard suggestion must land on a step whose schema actually
contains the key it suggests.

## The bug this exists to prevent, which already happened

#1267 added `_energy_dashboard_solver_source_suggestions()` and wired it into
`async_step_solver_sources()`. The helper returns three keys — battery SoC,
import price, export price — and **the Sources schema contains none of them**:

    sources : 16 fields | suggestion targets present: NONE
    battery :  2 fields | ['battery_soc']
    grid    :  9 fields | ['import_price', 'export_price']

Home Assistant silently drops a `suggested_value` for a key that is not in the
schema, so the feature was completely inert while looking finished.

It shipped green with 17 tests. Every one of them tested the **helper** — what it
returns, its type-safety, its `{**suggestions, **existing}` word order — and not
one tested that the suggested keys exist in the schema of the step it was wired
to. The gap was not rigour, it was that every test looked at the same side of the
seam.

`TestEverySuggestedKeyReachesItsOwnForm` is that missing test. Its central
assertion derives the wiring **from the source** -- it finds which step function
actually calls the helper, then checks the helper's keys against *that* step's
schema. Verified to fail on the pre-fix code, where the only caller is
`async_step_solver_sources` and the overlap is empty.

A static table of "which step owns which key" would NOT have caught this: the
battery and grid schemas were always correct, so such a table passes happily
while the helper is wired to a third step entirely. That weaker version was
written first.

## Why this class of bug is invisible without it

Nothing errors. The form renders, the wizard submits, options save. The only
symptom is a field that is blank when it could have been pre-filled — which looks
exactly like "the Energy Dashboard had nothing to offer", the helper's own
documented and legitimate empty-handed case. There is no failure to observe.
"""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.nimbus_load.const import (
    CONF_SOLVER_BATTERY_SOC_SENSOR,
    CONF_SOLVER_EXPORT_PRICE_SENSOR,
    CONF_SOLVER_IMPORT_PRICE_SENSOR,
)
from custom_components.nimbus_load.flows import hub_options as ho


# --------------------------------------------------------------- fake hass
class _State:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class _States:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, entity_id):
        return self._m.get(entity_id)


class _Hass:
    def __init__(self, mapping):
        self.states = _States(mapping)


_ENERGY_PREFS = {
    "energy_sources": [
        {"type": "battery", "stat_soc": "sensor.my_soc"},
        {
            "type": "grid",
            "flow_from": [{"entity_energy_price": "sensor.my_import_price"}],
            "flow_to": [{"entity_energy_price": "sensor.my_export_price"}],
        },
    ]
}

_HASS = _Hass(
    {
        "sensor.my_soc": _State(
            "57", {"device_class": "battery", "unit_of_measurement": "%"}
        ),
        "sensor.my_import_price": _State("0.294"),
        "sensor.my_export_price": _State("0.065"),
    }
)


def _install_energy_stub(prefs):
    """The helper imports `async_get_manager` inside its own try/except, so the
    module has to exist in sys.modules at call time."""
    mod = types.ModuleType("homeassistant.components.energy.data")

    class _Manager:
        data = prefs

    async def async_get_manager(_hass):
        return _Manager()

    mod.async_get_manager = async_get_manager
    pkg = types.ModuleType("homeassistant.components.energy")
    pkg.data = mod
    sys.modules["homeassistant.components.energy"] = pkg
    sys.modules["homeassistant.components.energy.data"] = mod


def _suggestions(prefs=_ENERGY_PREFS):
    _install_energy_stub(prefs)
    return asyncio.run(ho._energy_dashboard_solver_source_suggestions(_HASS))


def _schema_keys(builder) -> set[str]:
    return {str(getattr(m, "schema", m)) for m in builder({}).schema}


_STEP_SCHEMAS = {
    "async_step_solver_battery": ho._solver_battery_schema,
    "async_step_solver_grid": ho._solver_grid_schema,
    "async_step_solver_sources": ho._solver_sources_schema,
}


def _steps_calling_the_helper():
    """Which wizard steps actually call the Energy-Dashboard helper, read off
    the source. This is what makes the central test a real guard rather than a
    restatement of a table someone maintains by hand."""
    import inspect

    out = []
    for fn_name, builder in _STEP_SCHEMAS.items():
        src = inspect.getsource(getattr(ho.NimbusHubOptionsFlow, fn_name))
        if "_energy_dashboard_solver_source_suggestions" in src:
            out.append((fn_name, builder))
    return out


# Which step owns which suggested key. This mapping is the claim under test:
# if a key moves to a different step, this table must move with it.
_STEP_OWNERS = {
    "solver_battery": (ho._solver_battery_schema, {CONF_SOLVER_BATTERY_SOC_SENSOR}),
    "solver_grid": (
        ho._solver_grid_schema,
        {CONF_SOLVER_IMPORT_PRICE_SENSOR, CONF_SOLVER_EXPORT_PRICE_SENSOR},
    ),
}


class TestTheHelperStillWorks(unittest.TestCase):
    def test_it_returns_all_three_keys_from_a_full_energy_dashboard(self):
        got = _suggestions()
        self.assertEqual(
            got,
            {
                CONF_SOLVER_BATTERY_SOC_SENSOR: "sensor.my_soc",
                CONF_SOLVER_IMPORT_PRICE_SENSOR: "sensor.my_import_price",
                CONF_SOLVER_EXPORT_PRICE_SENSOR: "sensor.my_export_price",
            },
        )

    def test_no_energy_dashboard_is_an_empty_dict_not_an_error(self):
        self.assertEqual(_suggestions({}), {})


class TestEverySuggestedKeyReachesItsOwnForm(unittest.TestCase):
    """The guard that was missing. Fails on the pre-fix code."""

    def test_the_keys_land_on_the_step_that_actually_calls_the_helper(self):
        """The real guard, and the only assertion here that fails on the
        pre-fix code.

        Deliberately derives the wiring from the SOURCE rather than from a
        hand-maintained table: it asks which step functions call the helper,
        and checks the helper's own keys against the union of just those
        steps' schemas. On the pre-fix code the sole caller is
        `async_step_solver_sources`, whose schema contains none of the three,
        so the overlap is empty and this fails.
        """
        wired = _steps_calling_the_helper()
        self.assertTrue(
            wired,
            "the Energy-Dashboard helper is not called from any wizard step, "
            "so nothing it returns can ever reach a form",
        )
        reachable: set[str] = set()
        for step_name, builder in wired:
            reachable |= _schema_keys(builder)
        for key in _suggestions():
            with self.subTest(key=key, wired_to=[n for n, _ in wired]):
                self.assertIn(
                    key,
                    reachable,
                    f"{key} is suggested but no step that CALLS the helper "
                    f"has it in its schema -- HA drops it silently (#1067)",
                )

    def test_each_key_is_in_the_schema_of_the_step_it_is_wired_to(self):
        """The specific failure: the keys existed *somewhere*, just not on the
        step the helper was called from."""
        for step, (builder, owned) in _STEP_OWNERS.items():
            keys = _schema_keys(builder)
            for key in owned:
                with self.subTest(step=step, key=key):
                    self.assertIn(key, keys)

    def test_the_sources_step_owns_NONE_of_them(self):
        """Pins the measurement that exposed the bug, so a future change that
        moves a price field into Sources has to update this deliberately."""
        keys = _schema_keys(ho._solver_sources_schema)
        for key in _suggestions():
            with self.subTest(key=key):
                self.assertNotIn(key, keys)


class TestItIsWiredToTheRightSteps(unittest.TestCase):
    def test_the_battery_step_calls_it(self):
        import inspect

        src = inspect.getsource(ho.NimbusHubOptionsFlow.async_step_solver_battery)
        self.assertIn("_energy_dashboard_solver_source_suggestions", src)

    def test_the_grid_step_calls_it(self):
        import inspect

        src = inspect.getsource(ho.NimbusHubOptionsFlow.async_step_solver_grid)
        self.assertIn("_energy_dashboard_solver_source_suggestions", src)

    def test_the_sources_step_does_NOT(self):
        import inspect

        src = inspect.getsource(ho.NimbusHubOptionsFlow.async_step_solver_sources)
        self.assertNotIn("_energy_dashboard_solver_source_suggestions", src)


class TestASavedValueStillWins(unittest.TestCase):
    """The wizard-wipe guard, carried over from #1267 and still load-bearing:
    reversing these two words would overwrite real configuration on every
    wizard open. This repo has had three bugs of that exact shape."""

    def test_the_battery_step_puts_existing_last(self):
        import inspect

        src = inspect.getsource(ho.NimbusHubOptionsFlow.async_step_solver_battery)
        self.assertIn("**energy_dashboard, **existing}", src)

    def test_the_grid_step_puts_saved_options_last(self):
        import inspect

        src = inspect.getsource(ho.NimbusHubOptionsFlow.async_step_solver_grid)
        i = src.index("_energy_dashboard_solver_source_suggestions")
        self.assertIn("**dict(self.config_entry.options)", src[i : i + 400])


if __name__ == "__main__":
    unittest.main(verbosity=2)
