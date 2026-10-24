"""nimbus #1823: the wiring and the text this behaviour depends on. Checks that
read source text, kept apart until the decision on source-reading tests
(#1837); the behaviour is in tests/v2/entities/test_help_tab.py."""

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
from custom_components.nimbus_load import help_tab

PACKAGE = REPO / "custom_components" / "nimbus_load"


def _text() -> str:
    return json.dumps(help_tab.template(), ensure_ascii=False)


def _solver_rows() -> list[str]:
    return [k for _block, _card, keys in help_tab._settings_by_card() for k in keys]


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


def test_whats_new_lists_this_release() -> None:
    """A release that does not add its entry fails here."""
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    versions = [v for v, _day, _items in help_tab.WHATS_NEW]
    assert manifest["version"] in versions
    assert versions == sorted(
        versions, key=lambda v: tuple(map(int, v.split("."))), reverse=True
    )
    assert help_tab.RELEASES_URL in help_tab.whats_new_markdown()


def test_no_jinja_text_comes_in_from_the_wizard_strings() -> None:
    """The wizard text is pasted into markdown the template engine renders."""
    raw = (PACKAGE / "strings.json").read_text(encoding="utf-8")
    assert "{{" not in raw and "{%" not in raw


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
