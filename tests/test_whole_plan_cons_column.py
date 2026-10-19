"""Household, 19 Oct 2026: with price consolidation driving the plan, the
Whole Plan table's Fees¢ went negative ("is this in solver view an error?
how could fees go negative"). Fees¢ was import_price minus LocalVolts' own
price, so it carried consolidation's move. Now Fees¢ is the fees alone and
Cons¢ (violet) is how far consolidation moved the price ("option b is
definitely better for transparency... in different colour please").

Rendered through Jinja with HA's two helpers stubbed, on synthetic rows."""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import jinja2

BRISBANE = ZoneInfo("Australia/Brisbane")
_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _solver_tab():
    sys.path.insert(0, str(_PKG))
    spec = importlib.util.spec_from_file_location(
        "solver_tab_cons", _PKG / "solver_tab.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(time, raw, used, source="primary", consolidated=None):
    return {
        "time": time,
        "import_price_raw": raw,
        "import_price": used,
        "import_price_source": source,
        "consolidated_import_price": consolidated,
        "export_price": 0.1,
        "bonus_price": 0.0,
        "export_bonus_kw": 0.0,
        "hours": 0.5,
        "load_kw": 1.0,
        "solar_kw": 0.0,
        "battery_kw": 0.0,
        "soc_pct": 50,
        "net_cost": 0.1,
    }


def _render(rows):
    tab = _solver_tab()
    content = tab._HOUSEHOLD_UNTIL_MIDNIGHT["content"].replace(
        "${@nimbus_solver_battery_forecast}", "sensor.plan"
    )
    env = jinja2.Environment()
    # HA renders in the install's timezone; pinned so CI (UTC) agrees.
    env.filters["timestamp_custom"] = lambda ts, fmt: datetime.fromtimestamp(
        ts, BRISBANE
    ).strftime(fmt)
    plan = SimpleNamespace(attributes={"forecast": rows})
    states = SimpleNamespace(sensor=SimpleNamespace(plan=plan))
    return env.from_string(content).render(
        states=states,
        as_timestamp=lambda t: datetime.fromisoformat(t).timestamp(),
    )


def _cells(markdown, time):
    line = next(x for x in markdown.splitlines() if x.startswith(f"| {time} |"))
    return [c.strip() for c in line.strip("|").split("|")]


def test_fees_are_the_fees_and_consolidation_has_its_own_column():
    rows = [
        # Settled period: consolidation never touches it.
        _row("2026-10-19T14:55:00+10:00", 0.112, 0.112),
        # Consolidated 2.3c above LocalVolts; no fees on this install.
        _row("2026-10-19T15:00:00+10:00", 0.163, 0.186, "consolidated", 0.186),
        # Consolidated 2.4c below LocalVolts, plus a 1.5c fee.
        _row("2026-10-19T20:00:00+10:00", 0.224, 0.215, "consolidated", 0.200),
    ]
    md = _render(rows)
    header = next(x for x in md.splitlines() if x.startswith("| Time |"))
    assert [c.strip() for c in header.strip("|").split("|")][:4] == [
        "Time",
        "Buy¢",
        "Fees¢",
        "Cons¢",
    ]
    settled = _cells(md, "14:55")
    assert settled[1:4] == ["11.2", "0.0", ""]
    above = _cells(md, "15:00")
    assert above[1:3] == ["16.3", "0.0"]
    assert re.sub(r"<[^>]+>", "", above[3]) == "+2.3"
    assert "violet" in above[3]
    below = _cells(md, "20:00")
    assert below[1:3] == ["22.4", "1.5"]
    assert re.sub(r"<[^>]+>", "", below[3]) == "-2.4"


def test_with_consolidation_off_the_column_is_blank_and_fees_unchanged():
    rows = [
        _row("2026-10-19T15:00:00+10:00", 0.163, 0.163, "primary", 0.186),
    ]
    cells = _cells(_render(rows), "15:00")
    # Shadow: consolidated_import_price is published, but the plan did not
    # use it, so Cons¢ is blank and Fees¢ is the plain difference.
    assert cells[1:4] == ["16.3", "0.0", ""]


def test_the_total_row_has_a_cell_for_the_new_column():
    md = _render([_row("2026-10-19T23:30:00+10:00", 0.2, 0.2)])
    header_cells = next(x for x in md.splitlines() if x.startswith("| Time |")).count(
        "|"
    )
    total = next(x for x in md.splitlines() if "TOTAL" in x)
    assert total.count("|") == header_cells
