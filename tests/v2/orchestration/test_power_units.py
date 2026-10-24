"""One power-unit converter for the Forecaster, the Solver and every card
(#1570) (#1837).
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[3]
CC = ROOT / "custom_components" / "nimbus_load"
FE = CC / "frontend"
sys.path.append(str(CC))

import power_units as pu

CARDS = (
    "nimbus-dispatch-card-v4.js",
    "nimbus-forecast-card.js",
    "nimbus-topology-card.js",
)


# --- the Python converter -------------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "scale"),
    [
        ("W", 1e-3),
        ("w", 1e-3),
        (" W ", 1e-3),
        ("kW", 1.0),
        ("KW", 1.0),
        ("kw", 1.0),
        ("MW", 1e3),
        ("mw", 1e3),
        ("mW", 1e-6),
        ("GW", 1e6),
        ("TW", 1e9),
        ("BTU/h", 0.00029307107),
    ],
)
def test_power_units(unit, scale):
    assert pu.power_scale_to_kw(unit) == pytest.approx(scale)
    assert pu.is_power_unit(unit)


@pytest.mark.parametrize("unit", [None, "", "Wh", "kWh", "%", "°C", 5])
def test_no_or_non_power_unit_keeps_the_kw_assumption(unit):
    assert pu.power_scale_to_kw(unit) == 1.0
    assert not pu.is_power_unit(unit)


def test_power_to_kw():
    assert pu.power_to_kw(5000.0, "W") == pytest.approx(5.0)
    assert pu.power_to_kw(2.5, "MW") == pytest.approx(2500.0)


# --- every card carries the same table ------------------------------------------


def _tables(src: str) -> tuple[dict, dict]:
    t = re.search(r"const NIMBUS_POWER_TO_KW = (\{[^}]*\});", src)
    e = re.search(r"const NIMBUS_POWER_EXACT = (\{[^}]*\});", src)
    assert t and e
    return json.loads(t.group(1)), json.loads(e.group(1))


# --- and the JS actually computes the same numbers (when node is available) ------


def _js_scale_fn(card: str) -> str:
    src = (FE / card).read_text(encoding="utf-8")
    if card == "nimbus-topology-card.js":
        m = re.search(
            r"function topologyPowerScaleToKw\(unit\) \{.*?\n\}\n", src, re.DOTALL
        )
        return m.group(0) + "const f = topologyPowerScaleToKw;"
    m = re.search(r"  static powerScaleToKw\(unit\) \{.*?\n  \}\n", src, re.DOTALL)
    return "function f(unit) {" + m.group(0).split("{", 1)[1].rsplit("}", 1)[0] + "}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("card", CARDS)
def test_card_js_matches_python(card):
    units = [
        "W",
        "w",
        " W ",
        "kW",
        "KW",
        "MW",
        "mw",
        "mW",
        "GW",
        "TW",
        "BTU/h",
        "",
        "Wh",
        "%",
        None,
    ]
    script = (
        _js_scale_fn(card)
        + "\nconsole.log(JSON.stringify("
        + json.dumps(units)
        + ".map(f)));"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    got = json.loads(out.stdout)
    want = [pu.power_scale_to_kw(u) for u in units]
    assert got == pytest.approx(want)
