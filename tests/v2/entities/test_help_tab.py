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
from custom_components.nimbus_load import help_tab

PACKAGE = REPO / "custom_components" / "nimbus_load"


def _text() -> str:
    return json.dumps(help_tab.template(), ensure_ascii=False)


def _solver_rows() -> list[str]:
    return [k for _block, _card, keys in help_tab._settings_by_card() for k in keys]


def test_the_start_here_steps_cover_every_tab_with_a_live_tick():
    # test_the_start_here_steps_cover_every_tab_with_a_live_tick
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

    # test_the_glossary_carries_formulas
    terms = {t for t, _m, _f in help_tab.GLOSSARY}
    for term in ("EPR", "Regret", "Grid money", "Risk aversion", "Salvage value"):
        assert term in terms
    epr = next(f for t, _m, f in help_tab.GLOSSARY if t == "EPR")
    assert "J_ref" in epr and "43.4%" in epr

    # --- the wizard's own screens and words -----------------------------------------------------
    # test_every_wizard_screen_with_fields_has_its_section
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

    # test_no_jinja_text_comes_in_from_the_wizard_strings: The wizard text is pasted into markdown the template engine renders.
    raw = (PACKAGE / "strings.json").read_text(encoding="utf-8")
    assert "{{" not in raw and "{%" not in raw

    # test_whats_new_is_read_live_so_the_tab_stays_current: household, 4 Nov
    # 2026, "panel should always be current". Written into the tab, every
    # release changed Help, and the Tabs card offered a new Help each time.
    # The list is read off sensor.nimbus_status, so the tab itself carries no
    # release line and a kept Help tab shows each new release as it lands.
    import jinja2

    md = help_tab.whats_new_markdown()
    assert help_tab.WHATS_NEW[0][0] not in md and help_tab.WHATS_NEW[-1][0] not in md
    assert "state_attr('${@status}', 'whats_new')" in md
    attr = [
        {"version": "9.9.9", "date": "1 Jan 2030", "items": ["**A** thing.", "B."]},
        {"version": "9.9.8", "date": "31 Dec 2029", "items": ["C."]},
    ]
    out = (
        jinja2.Environment()
        .from_string(md)
        .render(state_attr=lambda e, a: attr if a == "whats_new" else "9.9.9")
    )
    assert "**9.9.9** · 1 Jan 2030\n- **A** thing.\n- B." in out
    assert "**9.9.8** · 31 Dec 2029\n- C." in out
