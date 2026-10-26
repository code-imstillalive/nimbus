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
        .replace("${@option:solver_import_price_sensor_2}", "sensor.amber_buy")
        # An option the install has not set resolves to "" (resolve()).
        .replace("${@option:solver_import_price_sensor_3}", "")
    )
    env = jinja2.Environment()
    # HA renders in the install's timezone; pinned so CI (UTC) agrees.
    env.filters["timestamp_custom"] = lambda ts, fmt: datetime.fromtimestamp(
        ts, BRISBANE
    ).strftime(fmt)
    attrs = {
        "sensor.plan": {"price_consolidation": {"import": imp}},
        "sensor.lv_buy": {"friendly_name": "LocalVolts Buy"},
        "sensor.amber_buy": {"friendly_name": "Amber General Price"},
    }
    return env.from_string(content).render(
        state_attr=lambda e, a: attrs.get(e, {}).get(a),
        as_timestamp=lambda t: datetime.fromisoformat(t).timestamp(),
    )


def _imp(names, with_next=True, with_now=False):
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
    if with_now:
        imp["now"] = {
            "time": "2026-10-19T15:55:00+10:00",
            "settled": 0.2101,
            "sources": {
                n: {"primary": 0.2101, "aemo_markup": 0.2950}.get(n) for n in names
            },
            "consolidated": 0.2160,
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


# --- Household, 21 Oct 2026: the interval running now ------------------------


def test_a_source_with_no_price_now_shows_a_dash():
    imp = _imp(["aemo_markup", "primary", "secondary_2"], with_now=True)
    assert _cells(_render(imp), "Amber General Price")[6] == "—"
