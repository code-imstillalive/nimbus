"""The Help tab (#1823) (#1837)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from custom_components.nimbus_load import forecaster_dashboard as dash
from custom_components.nimbus_load import help_tab, solver_tab

PACKAGE = REPO / "custom_components" / "nimbus_load"


def _text() -> str:
    return json.dumps(help_tab.template(), ensure_ascii=False)


def _solver_rows() -> list[str]:
    return [k for _block, _card, keys in help_tab._settings_by_card() for k in keys]


def test_every_solver_tab_row_has_a_help_entry_and_none_is_orphaned() -> None:
    rows = _solver_rows()
    assert rows, "found no Solver-tab rows: the walk is broken"
    assert sorted(set(rows) - set(help_tab.SETTINGS)) == []
    assert sorted(set(help_tab.SETTINGS) - set(rows)) == []
    for key, (what, _formula) in help_tab.SETTINGS.items():
        assert what.strip(), key


def test_each_setting_says_where_it_is() -> None:
    """Household, 17 Oct 2026: pointers to where to go."""
    md = help_tab.settings_markdown()
    for block, card, _keys in help_tab._settings_by_card():
        assert block, f"{card}: no Solver-tab block found before it"
        assert f"Solver tab → {block} → {card} card" in md


def test_the_basic_install_warning_comes_first_and_says_tabs_stay_blank() -> None:
    first = help_tab.template()[1]["cards"][0]["content"]
    assert first.startswith('<ha-alert alert-type="error"')
    assert "basic install leaves some tabs blank" in first.lower()
    for tab in ("Forecaster", "Topology", "Regret", "Stochastic"):
        assert tab in first


def test_the_start_here_steps_cover_every_tab_with_a_live_tick() -> None:
    start = help_tab._START_HERE
    for tab in (
        "Solver",
        "Control Panel",
        "Forecaster",
        "Topology",
        "Regret",
        "Stochastic",
    ):
        assert tab in start
    assert start.count("'✅' if") == 6
    # A derived stand-in inverter is not a real Power Source.
    assert "rejectattr('derived', 'defined')" in start


def test_a_problem_can_be_reported_on_github() -> None:
    text = _text()
    assert help_tab.ISSUES_URL in text
    assert help_tab.NEW_ISSUE_URL in text
    assert "Download diagnostics" in text


def test_the_glossary_carries_formulas() -> None:
    terms = {t for t, _m, _f in help_tab.GLOSSARY}
    for term in ("EPR", "Regret", "Grid money", "Risk aversion", "Salvage value"):
        assert term in terms
    epr = next(f for t, _m, f in help_tab.GLOSSARY if t == "EPR")
    assert "J_ref" in epr and "43.4%" in epr


def test_help_is_the_last_standard_tab_and_resolves_to_this_install() -> None:
    spec = dash.STANDARD_VIEWS[-1]
    assert (spec["key"], spec["title"], spec["path"]) == ("help", "Help", "help")
    view = dash._view_for(spec)
    assert view["sections"] == help_tab.template()

    def lookup(key: str) -> str | None:
        return (
            f"sensor.{key}"
            if not key.startswith(("option:", "name:", "card:"))
            else None
        )

    resolved = solver_tab.resolve(view, lookup)
    text = json.dumps(resolved, ensure_ascii=False)
    assert "${@" not in text
    # Every card survives: each one with a live reference names an entity
    # every Solver install has.
    assert len(resolved["sections"]) == len(view["sections"])
    assert sum(len(s["cards"]) for s in resolved["sections"]) == sum(
        len(s["cards"]) for s in view["sections"]
    )


def test_a_new_install_gets_help_last() -> None:
    views, done = dash._sync_views([], {}, [], created=True, resolve=lambda v: v)
    assert views[-1]["title"] == "Help"
    assert "added:help" in done
