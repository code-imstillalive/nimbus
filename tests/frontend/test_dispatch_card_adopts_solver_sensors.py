"""nimbus #1574 stage 1: a blank dispatch-card field follows the Solver's own
sensor on every render, not only when the card is first added.

A tester's card was created before his Solver had a battery power sensor, so
`battery_power_entity` stayed blank: no "Actual" history line and no live
battery reading, silently.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

CARD = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-dispatch-card-v4.js"
)


def _src() -> str:
    return CARD.read_text(encoding="utf-8")


def test_set_hass_adopts_before_fetching_history():
    src = _src()
    m = re.search(r"set hass\(hass\) \{(.*?)\n  \}", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert body.index("_adoptSolverSensors()") < body.index("_maybeFetchHistory()")


def _method() -> str:
    m = re.search(r"  _adoptSolverSensors\(\) \{.*?\n  \}\n", _src(), re.DOTALL)
    assert m
    return m.group(0)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_blank_fields_follow_the_solver_and_yaml_wins():
    solver = {
        "solver_battery_power_sensor": "sensor.batt",
        "solver_battery_soc_sensor": "sensor.soc",
        "solver_solar_power_sensor": "sensor.pv",
    }
    script = (
        "class C {\n" + _method() + "}\n"
        "const hass = {states: {'sensor.nimbus_solver_config': {attributes: "
        + json.dumps(solver)
        + "}}};\n"
        "const out = [];\n"
        "for (const config of [{}, {battery_power_entity: 'sensor.mine'}]) {\n"
        "  const c = new C(); c.config = config; c._hass = hass;\n"
        "  c._battEntity = ''; c._historyFetchedAt = 123; c._actualHistory = [[1, 2]];\n"
        "  c._adoptSolverSensors();\n"
        "  out.push([c._battEntity, c._socEntity, c._solarEntity, c._historyFetchedAt, c._actualHistory.length]);\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    blank, explicit = json.loads(res.stdout)
    # blank YAML: adopts all three, and resets history for the new sensor
    assert blank == ["sensor.batt", "sensor.soc", "sensor.pv", 0, 0]
    # explicit YAML battery wins; the others still adopt
    assert explicit[:3] == ["sensor.mine", "sensor.soc", "sensor.pv"]
