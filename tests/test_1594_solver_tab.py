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

import pytest

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
    """A block's opening card: a coloured title band (an ha-alert in a
    text-only markdown card) naming the block (household, 11 Oct 2026)."""
    content = card.get("content", "")
    if card.get("type") != "markdown" or "<ha-alert" not in content:
        return None
    return content.split('title="', 1)[1].split('"', 1)[0]


def _what(card):
    if card["type"] == "entities":
        return card["title"]
    if card["type"] == "custom:apexcharts-card":
        return card["header"]["title"]
    if card["type"] == "tile":
        return f"slider: {card['name']}"
    return card["type"]


def test_the_sections_are_the_agreed_ones_in_order() -> None:
    """The household's review on devhub, 11 Oct 2026: five blocks, each
    opened by a full-width coloured band; cards side by side in rows."""
    layout = []
    for section in solver_tab.template():
        cards = section["cards"]
        if _block(cards[0]):
            assert section["column_span"] == 4
            assert len(cards) == 1
            layout.append([_block(cards[0])])
            continue
        assert len(cards) == 1, cards
        layout[-1].append((section["column_span"], _what(cards[0])))
    assert layout == [
        [
            "Performance",
            (2, "At a glance"),
            (2, "Score by day"),
            (2, "Plan cost, last 7 days"),
            (2, "Where the cost comes from, last 24 h"),
        ],
        [
            "Money",
            (1, "Last day, scored"),
            (1, "Money, plan ahead"),
            (1, "Where the cost comes from"),
            (1, "Battery, now"),
        ],
        [
            "Plan",
            (3, "Battery: plan against measured"),
            (1, "Battery energy in the plan"),
            (1, "slider: Import price risk"),
            (1, "slider: Export price risk"),
            (1, "slider: Solar forecast risk"),
            (1, "markdown"),
        ],
        [
            "Basic settings",
            (1, "Battery"),
            (1, "Grid and fees"),
            (1, "P2P"),
            (1, "Reserve and price spikes"),
        ],
        [
            "Advanced settings",
            (1, "Costs and stability"),
            (1, "Features"),
            (1, "Scoring"),
            (1, "P2P hours and matching"),
        ],
    ]


def test_cards_side_by_side_have_the_same_number_of_rows() -> None:
    """Bottoms line up by content, not padding: every card in a row of four
    has the same number of rows, its note inside under a divider."""
    sections = solver_tab.template()
    rows: list[list[int]] = []
    for section in sections:
        card = section["cards"][0]
        if _block(card):
            rows.append([])
        elif card["type"] == "entities":
            entries = card["entities"]
            assert entries[-1]["type"] == "section", card["title"]  # the note
            assert len(entries[-1]["label"]) <= 42, entries[-1]["label"]
            plain = [
                e for e in entries if e.get("type") not in ("section", "conditional")
            ]
            rows[-1].append(len(plain))
    assert [r for r in rows if r] == [[6, 6, 6, 6], [8, 8, 8, 8], [8, 8, 8, 8]]


def test_the_sliders_are_sliders() -> None:
    """Household, 11 Oct 2026: "u have called it sliders but produced
    inputs". Each risk setting is a tile with a slider, all four columns of
    that row one height."""
    plan_row = [
        s["cards"][0]
        for s in solver_tab.template()
        if s["cards"][0]["type"] == "tile"
        or "What the sliders do" in s["cards"][0].get("content", "")
    ]
    assert len(plan_row) == 4
    for card in plan_row[:3]:
        assert card["features"] == [{"type": "numeric-input", "style": "slider"}]
    assert len({json.dumps(c["grid_options"]) for c in plan_row}) == 1


def test_live_values_in_text_are_resolved() -> None:
    view = _resolve(solver_tab.template())
    text = json.dumps(view)
    assert "${@" not in text
    assert "sensor.nimbus_solver_import_price_risk_effect_now_2" in text


def test_a_chart_with_two_axes_names_the_axis_on_every_series() -> None:
    """apexcharts-card 2.2.3 drops the whole card once a second y-axis exists
    and any series lacks `yaxis_id` (116KAT CLAUDE.md gotcha)."""
    for section in solver_tab.template():
        for card in section["cards"]:
            if len(card.get("yaxis", [])) > 1:
                assert all("yaxis_id" in s for s in card["series"]), card


def test_the_plan_chart_shows_past_and_future_with_a_now_line() -> None:
    """Household rule 5: one axis, the past and the plan ahead, a "now"
    marker, headline numbers, step lines. Never HAEO."""
    chart = next(
        s["cards"][0]
        for s in solver_tab.template()
        if s["cards"][0].get("header", {}).get("title")
        == "Battery: plan against measured"
    )
    assert chart["now"]["show"] is True
    assert chart["header"]["show_states"] is True
    assert chart["span"]["offset"].startswith("-")
    names = {s["name"]: s for s in chart["series"]}
    assert names["Measured"]["entity"] == "@option:battery_sensor"
    for line in ("Measured", "Plan", "Plan ahead", "Buy price", "Buy price ahead"):
        assert names[line]["curve"] == "stepline", line
    # Price behind Now is 5-minute detail from the buy price's own history.
    assert names["Buy price"]["group_by"]["duration"] == "5min"
    for planned in ("Plan ahead", "SoC ahead", "Buy price ahead"):
        assert names[planned]["entity"] == "@nimbus_solver_battery_forecast"
        assert "data_generator" in names[planned]
    assert "haeo" not in json.dumps(chart).lower()


def test_every_setting_is_on_the_tab_exactly_once() -> None:
    """All settings, grouped (household, 8 Oct 2026): every Solver number,
    switch and select appears once. A block's rate is also read by its own
    hour rows' conditions, so count the rows, not the references."""
    rows = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "conditional":
                walk(node["row"])
                return
            if isinstance(node.get("entity"), str):
                rows.append(node["entity"][1:])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(solver_tab.template())
    settings = [k for k in rows if k.startswith("solver_")]
    assert len(settings) == len(set(settings)), sorted(settings)
    assert {
        "solver_battery_capacity_kwh",
        "solver_network_fee_default_rate",
        "solver_p2p_block_1_rate_kw",
        "solver_proximal_weight_kw",
        "solver_flex_signals_enabled",
        "solver_import_price_risk_aversion",
        "solver_battery_reserve_percent",
    } <= set(settings)


def test_every_row_has_a_short_name() -> None:
    """No raw entity names (they read "Nimbus P2P P2P Block 1 Start..." and
    were cut off), so every entity row and tile carries its own name."""

    def walk(node, out):
        if isinstance(node, dict):
            if (
                isinstance(node.get("entity"), str)
                and node["entity"].startswith("@")
                and "conditions" not in node
            ):
                out.append(node)
            for v in node.values():
                walk(v, out)
        elif isinstance(node, list):
            for v in node:
                walk(v, out)
        return out

    named = walk(solver_tab.template(), [])
    conditions = [
        c for c in walk(solver_tab.template(), []) if set(c) <= {"entity", "state_not"}
    ]
    for item in named:
        if item in conditions:
            continue
        assert item.get("name"), item
        assert len(item["name"]) <= 32, item["name"]
        # The noise was the "Nimbus " prefix on every row; the chart legend's
        # "Nimbus plan (now)" names the plan line against "Measured" on purpose.
        if item["name"] != "Nimbus plan (now)":
            assert not item["name"].startswith("Nimbus "), item["name"]


def test_an_unused_block_shows_only_its_rate() -> None:
    """A fee block's hours appear only once its rate is set. P2P hours
    have their own card in Advanced (household, 11 Oct)."""
    text = json.dumps(solver_tab.template())
    assert text.count('"type": "conditional"') == 6  # 3 fee blocks x 2


def test_a_conditional_row_whose_entity_is_missing_is_dropped() -> None:
    view = solver_tab.resolve(
        solver_tab.template(),
        lambda k: None if k == "solver_network_fee_2_start_hour" else _lookup_all(k),
    )
    text = json.dumps(view)
    assert "solver_network_fee_2_start_hour" not in text
    assert '"row": null' not in text


def test_resolution_leaves_no_placeholder_and_uses_the_registry_ids() -> None:
    view = _resolve(
        dash._view_for(next(s for s in dash.STANDARD_VIEWS if s["key"] == "solver"))
    )
    text = json.dumps(view)
    assert '"@' not in text
    assert "sensor.nimbus_solver_lp_status_2" in text
    assert "sensor.my_battery_power" in text


def test_a_missing_entity_is_dropped_not_written_blank() -> None:
    def partial(key: str) -> str | None:
        return (
            None
            if key in ("nimbus_quality_epr", "option:battery_sensor")
            else _lookup_all(key)
        )

    view = solver_tab.resolve(solver_tab.template(), partial)
    text = json.dumps(view)
    assert "nimbus_quality_epr_2" not in text
    assert '"@' not in text
    chart = next(
        c
        for s in view
        for c in s["cards"]
        if c.get("header", {}).get("title") == "Battery: plan against measured"
    )
    assert "Measured" not in [x["name"] for x in chart["series"]]


def test_an_entities_card_with_nothing_left_is_dropped() -> None:
    """With no entities at all only the block bands are left: every card,
    chart and slider goes, and its column with it."""
    view = solver_tab.resolve(solver_tab.template(), lambda _k: None)
    assert [[_block(c) for c in s["cards"]] for s in view] == [
        ["Performance"],
        ["Money"],
        ["Plan"],
        ["Basic settings"],
        ["Advanced settings"],
    ]


def test_the_design_is_the_same_on_every_install() -> None:
    spec = next(s for s in dash.STANDARD_VIEWS if s["key"] == "solver")
    assert dash.design_of(spec) == dash.design_of(spec)
    assert '"@nimbus_solver_lp_status"' in json.dumps(dash._view_for(spec))


def test_a_new_install_gets_solver_second() -> None:
    views, done = dash._sync_views([], {}, [], created=True, resolve=_resolve)
    assert [v["title"] for v in views] == [
        "Forecaster",
        "Solver",
        "Topology",
        "Control Panel",
        "Regret",
    ]
    assert "added:solver" in done


def test_an_existing_dashboard_gets_it_beside_the_households_own_solver_tab() -> None:
    """Household, 8 Oct 2026: "install itself at the end or next to it"."""
    theirs = {"title": "Solver", "path": "solver", "type": "sections", "sections": []}
    before = [
        {
            "title": "Forecaster",
            "path": "forecaster",
            "cards": [{"type": "custom:nimbus-forecast-card"}],
        },
        theirs,
        {"title": "SUNSYNK", "path": "sunsynk", "cards": []},
    ]
    views, done = dash._sync_views([dict(v) for v in before], {}, [], resolve=_resolve)
    titles = [v["title"] for v in views]
    assert titles[:4] == ["Forecaster", "Solver", "Solver", "SUNSYNK"]
    assert views[1] == theirs  # untouched
    assert views[2]["path"] == "solver-2"
    assert "added:solver" in done


def test_without_a_matching_tab_it_goes_at_the_end() -> None:
    views, _ = dash._sync_views(
        [{"title": "Home", "path": "home", "cards": []}], {}, [], resolve=_resolve
    )
    assert [v["title"] for v in views][1] == "Solver" or views[-1]["title"] in (
        "Solver",
        "Regret",
    )
    assert any(v["title"] == "Solver" for v in views)


def test_a_restart_changes_nothing() -> None:
    mem: dict = {}
    views, _ = dash._sync_views([], mem, [], created=True, resolve=_resolve)
    again, done = dash._sync_views(
        json.loads(json.dumps(views)), mem, [], resolve=_resolve
    )
    assert done == []
    assert again == views


def test_without_a_resolver_the_tab_is_never_written() -> None:
    views, done = dash._sync_views([], {}, [], created=True)
    assert "Solver" not in [v["title"] for v in views]
    assert all("solver" not in d for d in done)


@pytest.mark.parametrize("view", [solver_tab.template()])
def test_no_haeo_and_no_icon(view) -> None:
    text = json.dumps(view).lower()
    assert "haeo" not in text
    spec = next(s for s in dash.STANDARD_VIEWS if s["key"] == "solver")
    assert "icon" not in dash._view_for(spec)
