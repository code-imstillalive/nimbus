"""Household, 23 Oct 2026 (devhub clean install): the Topology card showed
no whole-house headline and none of its daily solar / battery / load
figures, although the Switchboard daily sensors were configured.

The headline drew only when the card YAML carried a `whole_house` block.
The dashboard Nimbus generates never writes one, so every clean install
lost it; the reference household's hand-made dashboard sets it, so it was
never seen there. The card now takes the headline from the Solver config
(the whole-house meter and its forecast) when the YAML has no block, and a
YAML block still wins.

CI has no Node (same posture as every test in this directory): this pins
the structure in the real source. Checked by eye on devhub's generated
Topology tab before merge.
"""

from __future__ import annotations

import pathlib
import re

CARD_JS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "nimbus_load"
    / "frontend"
    / "nimbus-topology-card.js"
)
SRC = CARD_JS.read_text(encoding="utf-8")


def _method(name: str) -> str:
    start = SRC.index(f"  {name}(")
    end = SRC.index("\n  }\n", start)
    return SRC[start:end]


def test_the_headline_is_taken_from_the_solver_config() -> None:
    body = _method("_discoverWholeHouseFromSolverConfig")
    assert 'states["sensor.nimbus_solver_config"]' in body
    assert "solver_whole_house_cross_check_sensor" in body
    assert "solver_load_forecast_sensor" in body
    # Daily figures alone are enough to show the header.
    for key in (
        "house_load_energy_daily",
        "solar_energy_daily",
        "battery_charge_daily",
        "battery_discharge_daily",
    ):
        assert key in body


def test_it_applies_only_when_the_card_yaml_has_no_block() -> None:
    applied = re.search(
        r"if \(!cfg\.whole_house\) \{\s*const derivedWholeHouse = "
        r"this\._discoverWholeHouseFromSolverConfig\(cfg\.switchboard\);",
        SRC,
    )
    assert applied, "the derived headline must not override a YAML whole_house"
    # And it is in place before the header is drawn from cfg.whole_house.
    assert applied.start() < SRC.index("const wh = cfg.whole_house;")
