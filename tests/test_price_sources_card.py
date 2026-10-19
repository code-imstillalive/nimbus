"""Household with Mark Purcell, 19 Oct 2026: the Solver tab's Price sources
card. One full-width table: sources down the side (each with its share of
the blend and how far off it has been), the next slots across, then the
Blend and the Gap rows. It grows and shrinks with the sources ("flexible so
when sources shrink or grow it has room... and fits snug"). Rendered
through Jinja with HA's helpers stubbed."""

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


def _render(imp):
    content = (
        _card()
        .replace("${@nimbus_solver_battery_forecast}", "sensor.plan")
        .replace("${@option:solver_import_price_sensor}", "sensor.lv_buy")
    )
    env = jinja2.Environment()
    # HA renders in the install's timezone; pinned so CI (UTC) agrees.
    env.filters["timestamp_custom"] = lambda ts, fmt: datetime.fromtimestamp(
        ts, BRISBANE
    ).strftime(fmt)
    attrs = {
        "sensor.plan": {"price_consolidation": {"import": imp}},
        "sensor.lv_buy": {"friendly_name": "LocalVolts Buy"},
    }
    return env.from_string(content).render(
        state_attr=lambda e, a: attrs.get(e, {}).get(a),
        as_timestamp=lambda t: datetime.fromisoformat(t).timestamp(),
    )


def _imp(names, with_next=True):
    times = ["2026-10-19T16:00:00+10:00", "2026-10-19T16:30:00+10:00"]
    base = {"primary": [0.2142, 0.2252], "aemo_markup": [0.30, 0.31]}
    imp = {
        "sources": names,
        "spread_now": 0.082,
        "mae_by_source_and_lead": {
            n: {"0-5 min": 0.015, "5-60 min": 0.02, "1-4 h": 0.037, "4-24 h": 0.092}
            for n in names
        },
        "weights": {"5-60 min": {n: 1 / len(names) for n in names}},
    }
    if with_next:
        imp["next"] = {
            "times": times,
            "sources": {n: base.get(n, [0.25, 0.26]) for n in names},
            "consolidated": [0.2155, 0.2232],
        }
    return imp


def _plain(cell):
    return re.sub(r"<[^>]+>|\*\*", "", cell).strip()


def _cells(md, first):
    rows = [
        [_plain(c) for c in x.strip("|").split("|")]
        for x in md.splitlines()
        if x.startswith("|")
    ]
    return next(r for r in rows if r[0] == first)


def test_one_source_is_one_line_saying_there_is_nothing_to_blend():
    md = _render(_imp(["primary"]))
    assert md.strip() == "**Price sources**: one source, nothing to blend."


def test_two_sources_down_the_side_and_the_next_slots_across():
    md = _render(_imp(["aemo_markup", "primary"]))
    assert md.startswith("**Price sources**: they differ now by 8.2¢")
    assert _cells(md, "Source") == [
        "Source",
        "Share",
        "Off 5 min",
        "Off 1 h",
        "Off 4 h",
        "Off 24 h",
        "16:00",
        "16:30",
    ]
    assert _cells(md, "LocalVolts Buy") == [
        "LocalVolts Buy",
        "50%",
        "1.5¢",
        "2.0¢",
        "3.7¢",
        "9.2¢",
        "21.4",
        "22.5",
    ]
    assert _cells(md, "AEMO + markup")[6:] == ["30.0", "31.0"]
    assert _cells(md, "Blend")[6:] == ["21.6", "22.3"]
    assert _cells(md, "Gap")[6:] == ["+0.1", "-0.2"]
    assert "<font color='violet'>+0.1</font>" in md


def test_four_sources_add_rows_not_empty_slots():
    names = ["aemo_markup", "primary", "secondary_2", "secondary_3"]
    md = _render(_imp(names))
    for label in ("AEMO + markup", "LocalVolts Buy", "Second source", "Third source"):
        assert len([x for x in md.splitlines() if x.startswith(f"| {label} ")]) == 1
    widths = {x.count("|") for x in md.splitlines() if x.startswith("|")}
    assert widths == {9}, "every row has the same number of cells"


def test_without_published_next_slots_the_accuracy_still_shows():
    md = _render(_imp(["aemo_markup", "primary"], with_next=False))
    assert _cells(md, "Source") == [
        "Source",
        "Share",
        "Off 5 min",
        "Off 1 h",
        "Off 4 h",
        "Off 24 h",
    ]
    assert "Blend" not in md and "Gap" not in md


def test_no_published_data_renders_without_error():
    assert "nothing to blend" in _render({})
