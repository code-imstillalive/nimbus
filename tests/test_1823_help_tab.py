"""nimbus #1823: the Help tab.

Household, 17 Oct 2026: install steps and every wizard screen, each view
explained, a glossary, every setting with its formula, where to find each
thing, a link to report issues, what's new, and "very very clear that BASIC
INSTALL WILL LEAVE SOME FEATURES BLANK". These tests keep it from drifting:
a Solver-tab row without a Help entry, a wizard screen without its section, a
release without its What's new entry, or a lost GitHub link fails here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

REPO = Path(__file__).resolve().parent.parent
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


def test_every_wizard_screen_with_fields_has_its_section() -> None:
    strings = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))
    covered = {path for _title, path in help_tab.WIZARD_STEPS}
    expected = set()
    for top in ("config", "options"):
        for step, body in strings[top].get("step", {}).items():
            if body.get("data"):
                expected.add((top, "step", step))
    for sub, body in strings["config_subentries"].items():
        for step, step_body in body.get("step", {}).items():
            if step_body.get("data"):
                expected.add(("config_subentries", sub, "step", step))
    assert sorted(expected - covered) == []


def test_a_wizard_screen_keeps_the_wizards_own_words_and_red_markers() -> None:
    strings = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))
    step = strings["options"]["step"]["solver_battery"]
    md = help_tab.wizard_step(
        "Configure → Solver settings → screen 1 of 3",
        ("options", "step", "solver_battery"),
    )
    assert step["title"] in md
    for label in step["data"].values():
        assert help_tab._cell(label) in md
    assert "🔴" in md  # the required SoC sensor keeps its marker


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


def test_whats_new_lists_this_release() -> None:
    """A release that does not add its entry fails here."""
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    versions = [v for v, _day, _items in help_tab.WHATS_NEW]
    assert manifest["version"] in versions
    assert versions == sorted(
        versions, key=lambda v: tuple(map(int, v.split("."))), reverse=True
    )
    assert help_tab.RELEASES_URL in help_tab.whats_new_markdown()


def test_the_glossary_carries_formulas() -> None:
    terms = {t for t, _m, _f in help_tab.GLOSSARY}
    for term in ("EPR", "Regret", "Grid money", "Risk aversion", "Salvage value"):
        assert term in terms
    epr = next(f for t, _m, f in help_tab.GLOSSARY if t == "EPR")
    assert "J_ref" in epr and "43.4%" in epr


def test_no_jinja_text_comes_in_from_the_wizard_strings() -> None:
    """The wizard text is pasted into markdown the template engine renders."""
    raw = (PACKAGE / "strings.json").read_text(encoding="utf-8")
    assert "{{" not in raw and "{%" not in raw


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


def test_strings_json_is_read_once_and_off_the_event_loop() -> None:
    """v0.94.462 on devhub: HA logged a blocking read_text on the event loop
    from the Help tab. The file is read once, by the executor, before the
    dashboard sync builds the tab."""
    import inspect
    from unittest.mock import patch

    help_tab._strings.cache_clear()
    help_tab._strings()
    with patch.object(Path, "read_text", side_effect=AssertionError("re-read")):
        help_tab.template()  # served from the cache, no file read
    src = inspect.getsource(dash.async_ensure_nimbus_dashboard)
    warm = src.index("await hass.async_add_executor_job(help_tab._strings)")
    assert warm < src.index("_sync_views(")
