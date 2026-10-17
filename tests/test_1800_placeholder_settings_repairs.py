"""nimbus #1800: placeholder and contradictory battery / grid settings as Repairs.

A fresh install starts its battery capacity, max charge and discharge and grid
limits at number.py's 0.1 floor, and until now said so only in a one-off
notification. A Solver running on a 0.1 kWh battery raises no error at all.
These Repairs name each setting and the Solver-tab card it is on, and clear
themselves once the values are real.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import setup_health as sh

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "nimbus_load"

REAL = {
    "solver_battery_capacity_kwh": 119.8,
    "solver_max_charge_kw": 45.0,
    "solver_max_discharge_kw": 45.0,
    "solver_grid_max_import_kw": 44.0,
    "solver_battery_min_soc_percent": 5.0,
    "solver_battery_max_soc_percent": 100.0,
    "solver_charge_by_percent": 0.0,
}


def _issues(settings):
    return {
        i.kind: i
        for i in sh.evaluate_health(
            options={}, forecasts=(), hub_up_for=sh.TRAIN_GRACE, settings=settings
        )
        if i.kind in (sh.KIND_PLACEHOLDER_SETTINGS, sh.KIND_CONTRADICTORY_SETTINGS)
    }


def test_a_configured_install_has_neither_repair():
    assert _issues(REAL) == {}


def test_a_fresh_install_names_each_placeholder_and_its_card():
    fresh = {**REAL, **{key: 0.1 for key, _l, _w in sh._PLACEHOLDER_SETTINGS}}
    text = _issues(fresh)[sh.KIND_PLACEHOLDER_SETTINGS].placeholders["settings"]
    assert "Battery capacity (Battery card) is 0.1" in text
    assert "Max charge power (Battery card)" in text
    assert "Max discharge power (Battery card)" in text
    assert "Grid max import (Grid and fees card)" in text


def test_zero_export_is_not_a_placeholder():
    """A zero-export site can genuinely sit at the floor."""
    issues = _issues({**REAL, "solver_grid_max_export_kw": 0.1})
    assert sh.KIND_PLACEHOLDER_SETTINGS not in issues
    assert "solver_grid_max_export_kw" not in sh.SETTING_KEYS


def test_the_floor_is_number_pys_own_minimum():
    """If number.py's floor moves, a fresh install would stop being caught."""
    from custom_components.nimbus_load import number

    floors = {d.key: d.min_value for d in number._DESCRIPTIONS}
    for key, _label, _where in sh._PLACEHOLDER_SETTINGS:
        assert floors[key] == sh.PLACEHOLDER_FLOOR, key


def test_min_soc_not_below_max_soc_is_contradictory():
    text = _issues(
        {
            **REAL,
            "solver_battery_min_soc_percent": 90.0,
            "solver_battery_max_soc_percent": 90.0,
        }
    )[sh.KIND_CONTRADICTORY_SETTINGS].placeholders["settings"]
    assert "Min SoC (90%) is not below Max SoC (90%)" in text


def test_a_charge_by_target_above_max_soc_is_contradictory():
    issues = _issues(
        {
            **REAL,
            "solver_battery_max_soc_percent": 90.0,
            "solver_charge_by_percent": 95.0,
        }
    )
    assert (
        "above Max SoC"
        in (issues[sh.KIND_CONTRADICTORY_SETTINGS].placeholders["settings"])
    )
    # Charge By off (0) is not a contradiction.
    assert _issues({**REAL, "solver_charge_by_percent": 0.0}) == {}


def test_an_unread_setting_is_not_checked():
    assert _issues({}) == {}


def test_both_kinds_are_translated_in_both_files():
    for rel in ("strings.json", "translations/en.json"):
        issues = json.loads((ROOT / rel).read_text(encoding="utf-8"))["issues"]
        for kind in (sh.KIND_PLACEHOLDER_SETTINGS, sh.KIND_CONTRADICTORY_SETTINGS):
            assert "{settings}" in issues[kind]["description"], (rel, kind)
            assert "Solver tab" in issues[kind]["description"], (rel, kind)


# --- the live read -------------------------------------------------------------


class _Registry:
    def __init__(self, ids):
        self.ids = ids

    def async_get_entity_id(self, domain, platform, unique_id):
        assert (domain, platform) == ("number", sh.DOMAIN)
        return self.ids.get(unique_id)


def test_settings_are_read_by_unique_id_not_entity_id():
    """On a mirrored install the plain entity_id can be someone else's; the
    registry's unique_id lookup finds this hub's own number."""
    ids = {
        "e1_solver_battery_capacity_kwh": "number.nimbus_solver_battery_capacity_kwh_2"
    }
    states = {
        "number.nimbus_solver_battery_capacity_kwh_2": SimpleNamespace(state="0.1"),
        "number.nimbus_solver_battery_capacity_kwh": SimpleNamespace(state="119.8"),
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    er = SimpleNamespace(async_get=lambda _h: _Registry(ids))
    helpers = sys.modules["homeassistant.helpers"]
    with (
        patch.dict(sys.modules, {"homeassistant.helpers.entity_registry": er}),
        patch.object(helpers, "entity_registry", er, create=True),
    ):
        got = sh._live_settings(hass, SimpleNamespace(entry_id="e1"))
    assert got == {"solver_battery_capacity_kwh": 0.1}


def test_an_unreadable_registry_skips_only_these_checks():
    hass = SimpleNamespace()  # no states, no registry
    assert sh._live_settings(hass, SimpleNamespace(entry_id="e1")) == {}
