"""With a car or second battery as a Battery Participant the plan's battery_kw
and soc_pct are every battery together; the cards and the Solver tab show the
house battery by default, and the cards offer chips (House / each participant
/ All). Single-battery installs: no chips, the plan's own rows (household,
21 Nov 2026).
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

FRONTEND = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
)

PLAN = {
    "forecast": [
        {"time": "2026-11-21T13:00:00+10:00", "battery_kw": -38.0, "soc_pct": 50.0},
        {"time": "2026-11-21T13:30:00+10:00", "battery_kw": 0.0, "soc_pct": 60.0},
    ],
    "batteries": [
        {
            "name": "home",
            "forecast": [
                {"charge_kw": 31.0, "discharge_kw": 0.0, "soc_pct": 70.0},
                {"charge_kw": 0.0, "discharge_kw": 0.0, "soc_pct": 99.0},
            ],
        },
        {
            "name": "Our EV",
            "forecast": [
                {"charge_kw": 7.0, "discharge_kw": 0.0, "soc_pct": 41.0},
                {"charge_kw": 0.0, "discharge_kw": 0.0, "soc_pct": 45.0},
            ],
        },
    ],
}


def _methods(src: str, names: list[str]) -> str:
    out = []
    for n in names:
        m = re.search(r"  " + n + r"\([^)]*\) \{.*?\n  \}\n", src, re.DOTALL)
        assert m, n
        out.append(m.group(0))
    return "".join(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_dispatch_card_shows_the_house_battery_and_offers_chips():
    src = (FRONTEND / "nimbus-dispatch-card-v4.js").read_text(encoding="utf-8")
    body = _methods(
        src, ["_batteries", "_batteryViewKey", "_viewRows", "_batteryChips"]
    )
    script = (
        "global.window = {localStorage: {getItem: () => null, setItem: () => {}}};\n"
        "class C {\n" + body + "}\n"
        "const plan = {attributes: " + json.dumps(PLAN) + "};\n"
        "const single = {attributes: {forecast: plan.attributes.forecast, batteries: [plan.attributes.batteries[0]]}};\n"
        "const out = {};\n"
        "const cfgState = (veh) => ({states: {'sensor.nimbus_solver_config': {attributes: {flex_vehicle_participants: veh}}}});\n"
        "let c = new C(); c._hass = cfgState(['Our EV']);\n"
        "out.home = c._viewRows(plan).map(r => [r.battery_kw, r.soc_pct]);\n"
        "out.chips = c._batteryChips(plan);\n"
        "const two = {attributes: {forecast: plan.attributes.forecast, batteries: plan.attributes.batteries.concat([{name: 'Second car', forecast: []}, {name: 'Garage battery', forecast: []}])}};\n"
        "c._hass = cfgState(['Our EV', 'Second car']); out.twoCars = c._batteryChips(two);\n"
        "c._hass = {states: {}}; out.unknown = c._batteryChips(plan);\n"
        "c._batteryViewSel = 'Our EV'; out.ev = c._viewRows(plan).map(r => [r.battery_kw, r.soc_pct]);\n"
        "c._batteryViewSel = 'all'; out.all = c._viewRows(plan).map(r => [r.battery_kw, r.soc_pct]);\n"
        "c = new C(); out.single = c._viewRows(single).map(r => [r.battery_kw, r.soc_pct]);\n"
        "out.singleChips = c._batteryChips(single);\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    out = json.loads(res.stdout)
    assert out["home"] == [[-31.0, 70.0], [0.0, 99.0]]
    assert out["ev"] == [[-7.0, 41.0], [0.0, 45.0]]
    assert out["all"] == [[-38.0, 50.0], [0.0, 60.0]]
    assert out["single"] == [[-38.0, 50.0], [0.0, 60.0]]
    assert out["singleChips"] == ""
    # Household, 21 Nov 2026: "House. EV. All", and EV 1, EV 2 for more cars.
    for label in ("House", "EV", "All"):
        assert ">" + label + "<" in out["chips"]
    assert ">Our EV<" not in out["chips"]
    for label in ("House", "EV 1", "EV 2", "Garage battery", "All"):
        assert ">" + label + "<" in out["twoCars"], label
    # The vehicle list not readable yet: the participant's own name.
    assert ">Our EV<" in out["unknown"]


def test_every_card_and_the_solver_tab_use_the_house_battery():
    flow = (FRONTEND / "nimbus-flow-card.js").read_text(encoding="utf-8")
    assert 'b.name === "home"' in flow
    fc = (FRONTEND / "nimbus-forecast-card.js").read_text(encoding="utf-8")
    assert "_rowsCode()" in fc and "bview-chip" in fc
    tab = (FRONTEND.parent / "solver_tab.py").read_text(encoding="utf-8")
    # Whole Plan table, daily summary, energy sums and the battery chart line.
    assert tab.count("hb.forecast[loop.index0]") >= 3
    assert "b.name === 'home'" in tab
