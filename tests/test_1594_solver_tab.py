"""nimbus #1594: the standard Solver tab.

Contents agreed by the household (6 and 8 Oct 2026): headline, plan against
actual, money, how well it did, risk sliders, and every setting grouped into
Basic, P2P (optional) and Advanced. It installs itself: second on a new
install's dashboard, and on an existing one beside a household-built tab of
the same title, which is never touched.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from tests._ha_stubs import install_ha_stubs

install_ha_stubs()

_PKG = Path(__file__).resolve().parents[1] / "custom_components" / "nimbus_load"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _PKG / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


solver_tab = _load("solver_tab", "solver_tab.py")
dash = _load("_nimbus_fdash_1594", "forecaster_dashboard.py")

ENTRY = "01TESTENTRY"


def _keys(node) -> set[str]:
    text = json.dumps(node)
    out, i = set(), 0
    while (i := text.find('"@', i)) != -1:
        j = text.index('"', i + 1)
        out.add(text[i + 2 : j])
        i = j
    return out


def _lookup_all(key: str) -> str | None:
    """Every Nimbus entity exists; entity_ids carry a `_2` to prove the
    template never relies on the entity_id itself."""
    if key.startswith("option:"):
        return "sensor.my_battery_power" if key == "option:battery_sensor" else None
    domain = "number" if key.startswith("solver_") else "sensor"
    if key.endswith("_enabled") or key in (
        "solver_dispatch_dry_run",
        "solver_auto_include_known_solar",
        "solver_price_spike_override_armed",
    ):
        domain = "switch"
    if key == "solver_load_forecast_source_policy":
        domain = "select"
    object_id = key if key.startswith("nimbus_") else f"nimbus_{key}"
    return f"{domain}.{object_id}_2"


def _resolve(view):
    return solver_tab.resolve(view, _lookup_all)


def _block(card) -> str | None:
    """A block's opening band: a markdown card holding a coloured ha-alert
    with the block's name as a heading (household, 11 Oct 2026)."""
    content = card.get("content", "")
    # A band is the alert alone; a price-safety card (16 Oct 2026) reads live
    # state first and carries text under its alert.
    if card.get("type") != "markdown" or not content.startswith("<ha-alert"):
        return None
    assert "<h2>" not in content  # does not survive HA's markdown
    return content.split('title="', 1)[1].split('"', 1)[0]


def _what(card):
    if card["type"] == "entities":
        return card["title"]
    if card["type"] == "custom:apexcharts-card":
        return card["header"]["title"]
    if card["type"] == "tile":
        features = [f["type"] for f in card.get("features", ())]
        kind = (
            "toggle"
            if features == ["toggle"]
            else "select"
            if features == ["select-options"]
            else "slider"
            if features
            else "tile"
        )
        return f"{kind}: {card['name']}"
    if card["type"] == "markdown" and card.get("title"):
        return card["title"]
    if card["type"] == "markdown" and card.get("content"):
        text = card["content"]
        if "**" not in text and 'title="' in text:
            # A card opening with a coloured ha-alert band (the price-safety
            # cards) is named by the band's title.
            return "markdown: " + text.split('title="', 1)[1].split('"', 1)[0]
        return "markdown: " + text[text.index("**") + 2 :].split("**", 1)[0]
    if card["type"] == "button":
        return f"button: {card['name']}"
    return card["type"]


# Rendered heights in px, measured off the household's screenshots of devhub
# (11 Oct 2026): a number or time box 67, a toggle or a reading 44, a select
# 60, and a divider with a line of text 38 + 21 per wrapped line (about 62
# characters at a quarter of the screen). The first estimate counted rows
# only, and the cards visibly did not line up.
_NUMBER, _OTHER, _SELECT = 67, 44, 60


# Keys whose entity is not what its name suggests to the rule below: two
# selects, one number, and two readings whose key starts with solver_.
_SELECTS = {"@household_mode", "@pricing_source"}
_NUMBERS = {"@solve_on_price_change_debounce_s"}
_READINGS = {"@solver_price_response_latency", "@solver_config"}


def _height(card) -> int:
    total = 0
    for e in card["entities"]:
        if e.get("type") == "conditional":
            continue
        if e.get("type") == "section":
            total += 38 + 21 * -(-len(e["label"]) // 62)
        elif e["entity"].endswith("_policy") or e["entity"] in _SELECTS:
            total += _SELECT
        elif e["entity"] in _NUMBERS:
            total += _NUMBER
        elif e["entity"] in _READINGS:
            total += _OTHER
        elif e["entity"].startswith("@solver_") and not e["entity"].endswith(
            ("_enabled", "_dry_run", "_known_solar", "_armed")
        ):
            total += _NUMBER
        else:
            total += _OTHER
    return total


def test_the_sliders_are_sliders() -> None:
    """Household, 11 Oct 2026: "u have called it sliders but produced
    inputs". 12 Oct: the household's mushroom slider with a 100 px icon
    where mushroom is installed, HA's tile slider elsewhere."""

    def lookup(key):
        return {"card:mushroom": MUSH}.get(key, f"number.nimbus_{key}")

    for MUSH, kind in (("custom:mushroom-number-card", "mushroom"), (None, "tile")):
        cards = [
            c
            for s in solver_tab.resolve(solver_tab.template(), lookup)
            for c in s["cards"]
            if c.get("display_mode") == "slider"
            or any(f["type"] == "numeric-input" for f in c.get("features", ()))
        ]
        assert len(cards) == 3, kind
        for card in cards:
            if MUSH:
                assert card["type"] == MUSH
                assert "--mush-icon-size: 100px" in card["card_mod"]["style"]
                assert card["entity"] in card["card_mod"]["style"]
            else:
                assert card["type"] == "tile"
                assert card["features"] == [
                    {"type": "numeric-input", "style": "slider"}
                ]


def _row(title_or_header: str) -> list[dict]:
    """The sections from the one whose first card is `title_or_header` to
    the next full-width band."""
    sections = solver_tab.template()
    names = [
        s["cards"][0].get("title") or s["cards"][0].get("header", {}).get("title")
        for s in sections
    ]
    i = names.index(title_or_header)
    out = []
    for s in sections[i:]:
        if s["column_span"] == 4 and out:
            break
        out.append(s)
    return out


def test_an_entities_card_with_nothing_left_is_dropped() -> None:
    """With no entities at all only the Solve now button and the block bands
    are left: every card, chart and slider goes, and its column with it."""
    view = solver_tab.resolve(solver_tab.template(), lambda _k: None)
    assert [[_block(c) for c in s["cards"]] for s in view] == [
        [None],  # Solve now needs no entity
        ["Plan"],
        ["Money"],
        ["Performance"],
        ["Basic settings"],
        ["Advanced settings"],
        ["More settings"],
        ["Stochastic"],
    ]


def test_the_design_is_the_same_on_every_install() -> None:
    spec = next(s for s in dash.STANDARD_VIEWS if s["key"] == "solver")
    assert dash.design_of(spec) == dash.design_of(spec)
    assert '"@nimbus_solver_lp_status"' in json.dumps(dash._view_for(spec))


def test_forecast_card_soc_is_drawn_at_the_end_of_each_period() -> None:
    import pathlib

    js = (
        pathlib.Path(solver_tab.__file__).parent
        / "frontend"
        / "nimbus-forecast-card.js"
    ).read_text(encoding="utf-8")
    soc = js[js.index('name: "Solver SoC % (proposed)"') :][:400]
    assert "(p.hours || 0) * 3600000, p.soc_pct" in soc
