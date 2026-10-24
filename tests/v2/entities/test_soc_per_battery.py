"""On a site with more than one battery, the plan chart draws one SoC line per
battery, never the fleet's (#1837).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load import sensor_battery_soc as bs
from custom_components.nimbus_load import solver_tab


def _entry(participants: list[str]) -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "e16oct"
    entry.subentries = {
        f"s{i}": SimpleNamespace(
            subentry_id=f"s{i}",
            subentry_type="battery_participant",
            data={"battery_participant_name": name},
        )
        for i, name in enumerate(participants)
    }
    return entry


PLAN = {
    "forecast": [
        {"time": "2026-10-16T17:29:00+10:00", "hours": 1 / 60, "soc_pct": 86.4},
        {"time": "2026-10-16T17:30:00+10:00", "hours": 5 / 60, "soc_pct": 85.9},
    ],
    "batteries": [
        {
            "name": "home",
            "forecast": [
                {"time": "2026-10-16T17:29:00+10:00", "soc_pct": 92.99},
                {"time": "2026-10-16T17:30:00+10:00", "soc_pct": 91.96},
            ],
        },
        {
            "name": "Our EV",
            "forecast": [
                {"time": "2026-10-16T17:29:00+10:00", "soc_pct": 73.0},
                {"time": "2026-10-16T17:30:00+10:00", "soc_pct": 73.0},
            ],
        },
    ],
}


def test_a_single_battery_site_gets_no_per_battery_sensor() -> None:
    assert bs.create(_entry([]), "0.94.461") == []


def test_each_battery_gets_its_own_sensor_and_value() -> None:
    ents = bs.create(_entry(["Our EV"]), "0.94.461")
    assert [e.entity_id for e in ents] == [
        "sensor.nimbus_solver_battery_0_soc_pct",
        "sensor.nimbus_solver_battery_1_soc_pct",
    ]
    assert [e._attr_name for e in ents] == ["SoC Home battery", "SoC Our EV"]
    for e in ents:
        e.hass = None
        e.update_from_parent(PLAN["forecast"][0], parent_attributes=PLAN)
    assert [e.native_value for e in ents] == [92.99, 73.0]
    rows = ents[0].extra_state_attributes["forecast"]
    assert rows[1] == {
        "time": "2026-10-16T17:30:00+10:00",
        "hours": 5 / 60,
        "soc_pct": 91.96,
    }
    assert "forecast" in ents[0]._unrecorded_attributes


def test_a_battery_the_plan_skipped_keeps_its_last_value() -> None:
    (_home, ev) = bs.create(_entry(["Our EV"]), "0.94.461")
    ev.hass = None
    ev.update_from_parent(PLAN["forecast"][0], parent_attributes=PLAN)
    ev.update_from_parent({}, parent_attributes={"batteries": [PLAN["batteries"][0]]})
    assert ev.native_value == 73.0


_SLOT = re.compile(r"battery_\d_soc_pct")


def _soc_series(lookup) -> list[dict]:
    view = solver_tab.resolve(solver_tab.template(), lookup)
    text_series: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "custom:apexcharts-card":
                text_series.extend(
                    s for s in node["series"] if s.get("yaxis_id") == "soc"
                )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(view)
    return text_series


def _lookup(batteries: dict[int, str]):
    names = {
        f"nimbus_solver_battery_{slot}_soc_pct": name
        for slot, name in batteries.items()
    }

    def lookup(key: str) -> str | None:
        if key.startswith("name:"):
            return names.get(key.split(":", 1)[1])
        if key.startswith("nimbus_solver_battery_") and key.endswith("_soc_pct"):
            return f"sensor.{key}" if key in names else None
        if key.startswith("option:"):
            return "sensor.my_battery_power" if key == "option:battery_sensor" else None
        return f"sensor.{key}"

    return lookup


def test_a_single_battery_site_keeps_the_fleet_lines_only() -> None:
    series = _soc_series(_lookup({}))
    names = [s["name"] for s in series]
    assert "SoC" in names and "SoC ahead" in names
    assert not any(_SLOT.search(s["entity"]) for s in series)
    assert not any("nimbus_unless" in s for s in series)


def test_a_two_battery_site_draws_one_line_each_instead() -> None:
    series = _soc_series(_lookup({0: "SoC Home battery", 1: "SoC Our EV"}))
    names = [s["name"] for s in series]
    assert "SoC" not in names and "SoC ahead" not in names
    assert names.count("SoC Home battery") == 2  # measured + ahead
    assert names.count("SoC Our EV") == 2
    assert {s["entity"] for s in series if _SLOT.search(s["entity"])} == {
        "sensor.nimbus_solver_battery_0_soc_pct",
        "sensor.nimbus_solver_battery_1_soc_pct",
    }
    assert not any("nimbus_unless" in s for s in series)
