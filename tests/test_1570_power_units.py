"""nimbus issue #1570: one power-unit converter for the Forecaster, the Solver
and every card.

Before this, about seven places carried their own `unit == "W"` check (exact,
case-sensitive, W only), the Solver's live whole-house load had no conversion
at all, and the dispatch card read battery, grid and EV power raw. A tester's
W battery sensor stretched the Control Panel chart's y-axis to thousands and
flattened the plan to a line (2026-10-06).
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
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


def test_the_solver_and_forecaster_use_it():
    """No hand-rolled W check is left anywhere in the integration."""
    pattern = re.compile(r"""==\s*["']W["']|lower\(\)\s*==\s*["']w["']""")
    offenders = [
        f"{p.relative_to(CC)}:{i}"
        for p in CC.rglob("*.py")
        if p.name != "power_units.py"  # its docstring quotes the old check
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line) and not line.lstrip().startswith("#")
    ]
    assert offenders == []
    assert "PowerConverter" not in (CC / "coordinator.py").read_text(encoding="utf-8")
    assert "power_scale_to_kw(unit)" in (CC / "solver_shared.py").read_text(
        encoding="utf-8"
    )


def test_the_live_whole_house_load_is_converted():
    src = (CC / "solver_inputs" / "load.py").read_text(encoding="utf-8")
    assert "sw._kw_scale_factor(whole_house_cross_check_sensor)" in src


# --- every card carries the same table ------------------------------------------


def _tables(src: str) -> tuple[dict, dict]:
    t = re.search(r"const NIMBUS_POWER_TO_KW = (\{[^}]*\});", src)
    e = re.search(r"const NIMBUS_POWER_EXACT = (\{[^}]*\});", src)
    assert t and e
    return json.loads(t.group(1)), json.loads(e.group(1))


@pytest.mark.parametrize("card", CARDS)
def test_each_card_matches_power_units(card):
    table, exact = _tables((FE / card).read_text(encoding="utf-8"))
    assert table == pytest.approx(pu.POWER_TO_KW)
    assert exact == pytest.approx(pu._EXACT)


def test_the_dispatch_card_converts_every_power_entity_and_its_history():
    src = (FE / "nimbus-dispatch-card-v4.js").read_text(encoding="utf-8")
    for read in (
        "this._numAsKw(this._battEntity, 0)",
        "this._numAsKw(this._gridEntity, 0)",
        "this._numAsKw(this._evEntity, 0)",
        "this._numAsKw(this._gridEntity, NaN)",
        "this._numAsKw(this._battEntity, NaN)",
        "this._battSign() * this._kwScaleOf(this._battEntity)",
    ):
        assert read in src, read
    for raw in (
        "this._num(this._battEntity",
        "this._num(this._gridEntity",
        "this._num(this._evEntity",
    ):
        assert raw not in src, raw


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
