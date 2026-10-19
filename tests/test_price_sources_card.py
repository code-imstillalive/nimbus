"""Household with Mark Purcell, 19 Oct 2026: the Solver tab's Price sources
card shows every import price source the consolidation reads, its accuracy
and weight, and its own price for the next slots beside the blend; it grows
and shrinks with the sources ("flexible so when sources shrink or grow it
has room"). Rendered through Jinja with HA's helpers stubbed."""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2

BRISBANE = ZoneInfo("Australia/Brisbane")
_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _card():
    sys.path.insert(0, str(_PKG))
    spec = importlib.util.spec_from_file_location(
        "solver_tab_src", _PKG / "solver_tab.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._CONSOLIDATION_SOURCES["content"]


def _render(imp, live="off"):
    content = (
        _card()
        .replace("${@nimbus_solver_battery_forecast}", "sensor.plan")
        .replace("${@solver_price_consolidation_live}", "switch.live")
        .replace("${@option:solver_import_price_sensor}", "sensor.lv_buy")
    )
    env = jinja2.Environment(extensions=["jinja2.ext.loopcontrols"])
    env.filters["timestamp_custom"] = lambda ts, fmt: datetime.fromtimestamp(
        ts, BRISBANE
    ).strftime(fmt)
    attrs = {
        "sensor.plan": {"price_consolidation": {"import": imp}},
        "sensor.lv_buy": {"friendly_name": "LocalVolts Buy"},
    }
    return env.from_string(content).render(
        state_attr=lambda e, a: attrs.get(e, {}).get(a),
        states=lambda e: live,
        as_timestamp=lambda t: datetime.fromisoformat(t).timestamp(),
    )


def _imp(names):
    times = ["2026-10-19T16:00:00+10:00", "2026-10-19T16:30:00+10:00"]
    base = {"primary": [0.2142, 0.2252], "aemo_markup": [0.30, 0.31]}
    return {
        "sources": names,
        "spread_now": 0.082,
        "mae_by_source_and_lead": {
            n: {"0-5 min": 0.015, "5-60 min": 0.02, "1-4 h": 0.037, "4-24 h": 0.092}
            for n in names
        },
        "weights": {"5-60 min": {n: 1 / len(names) for n in names}},
        "next": {
            "times": times,
            "sources": {n: base.get(n, [0.25, 0.26]) for n in names},
            "consolidated": [0.2155, 0.2232],
        },
    }


def _table_rows(md, first_cell):
    return [x for x in md.splitlines() if x.startswith(f"| {first_cell}")]


def test_one_source_says_there_is_nothing_to_blend():
    md = _render(_imp(["primary"]))
    assert "One price source: nothing to blend." in md
    assert "| Average miss" not in md


def test_two_sources_give_two_rows_and_two_price_columns():
    md = _render(_imp(["aemo_markup", "primary"]))
    assert "Sources differ now by 8.2¢" in md
    assert "Off: the plan uses LocalVolts Buy" in md
    header = next(x for x in md.splitlines() if x.startswith("| Next |"))
    assert [c.strip() for c in header.strip("|").split("|")] == [
        "Next",
        "AEMO + markup",
        "LocalVolts Buy",
        "Blend",
        "Gap",
    ]
    assert len(_table_rows(md, "AEMO + markup")) == 1
    assert len(_table_rows(md, "LocalVolts Buy")) == 1
    row = next(x for x in md.splitlines() if x.startswith("| 16:00 |"))
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in row.strip("|").split("|")]
    assert cells == ["16:00", "30.0", "21.4", "21.6", "+0.1"]


def test_four_sources_grow_the_card_without_an_empty_slot():
    names = ["aemo_markup", "primary", "secondary_2", "secondary_3"]
    md = _render(_imp(names))
    header = next(x for x in md.splitlines() if x.startswith("| Next |"))
    assert header.count("|") == len(names) + 4
    for label in ("Second source", "Third source"):
        assert len(_table_rows(md, label)) == 1


def test_no_published_data_renders_without_error():
    md = _render({})
    assert "One price source: nothing to blend." in md
