"""The generated Control Panel card finds the dispatch arm switch and mode
select from Nimbus's own settings (household, 21 Nov 2026).

Since 3 Nov the Control Panel tab has been the generated one, which carries
no `armed_entity` or `mode_select_entity`. The card then read DISARMED / OFF
and "Dispatch is off" on an install whose dispatch was armed and running,
and its toggle and mode chips did nothing. The household cannot edit a
generated card, so the two helpers are Solver settings, published on
`sensor.nimbus_solver_config`, and a card left blank adopts them as it does
the battery and solar sensors (#1574).
"""

from __future__ import annotations

import importlib
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CARD = (
    ROOT
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-dispatch-card-v4.js"
)

sys.path.append(str(ROOT / "tests"))
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(ROOT))


def _src() -> str:
    return CARD.read_text(encoding="utf-8")


def test_the_two_helpers_are_solver_settings_the_config_sensor_exposes():
    const = importlib.import_module("custom_components.nimbus_load.const")
    hub = importlib.import_module("custom_components.nimbus_load.flows.hub_options")
    sm = importlib.import_module("custom_components.nimbus_load.sensor")
    keys = (
        const.CONF_SOLVER_DISPATCH_ARM_SWITCH,
        const.CONF_SOLVER_DISPATCH_MODE_SELECT,
    )
    assert keys == ("solver_dispatch_arm_switch", "solver_dispatch_mode_select")
    shown = hub._effective_schema_keys(hub._solver_battery_schema({}))
    for key in keys:
        assert key in shown
        assert key in hub._SOLVER_WIZARD_SCHEMA_KEYS
        assert key in sm._SOLVER_ALL_KEYS


def _method() -> str:
    m = re.search(r"  _adoptSolverSensors\(\) \{.*?\n  \}\n", _src(), re.DOTALL)
    assert m
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_blank_arm_mode_and_grid_fields_follow_nimbus_settings():
    solver = {
        "solver_dispatch_arm_switch": "input_boolean.armed",
        "solver_dispatch_mode_select": "input_select.mode",
        "grid_sensor": "sensor.grid",
    }
    script = (
        "class C {\n" + _method() + "}\n"
        "const hass = {states: {'sensor.nimbus_solver_config': {attributes: "
        + json.dumps(solver)
        + "}}};\n"
        "const out = [];\n"
        "for (const config of [{}, {armed_entity: 'input_boolean.mine'}]) {\n"
        "  const c = new C(); c.config = config; c._hass = hass;\n"
        "  c._battEntity = ''; c._historyFetchedAt = 0; c._actualHistory = [];\n"
        "  c._adoptSolverSensors();\n"
        "  out.push([c._armedEntity, c._modeEntity, c._gridEntity]);\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    blank, explicit = json.loads(res.stdout)
    assert blank == ["input_boolean.armed", "input_select.mode", "sensor.grid"]
    assert explicit[0] == "input_boolean.mine"
