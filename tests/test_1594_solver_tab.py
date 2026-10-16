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
        kind = "slider" if card.get("features") else "tile"
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


def test_the_sections_are_the_agreed_ones_in_order() -> None:
    """The household's reviews on devhub, 11 Oct 2026: Solve now on the very
    top row, then five blocks, each opened by a full-width band."""
    layout: list[list] = [["top"]]
    for section in solver_tab.template():
        cards = section["cards"]
        if _block(cards[0]):
            assert section["column_span"] == 4
            assert len(cards) == 1
            assert not cards[0].get("text_only")  # outlined like a card
            layout.append([_block(cards[0])])
            continue
        if len(cards) > 1:  # the daily summary: today and tomorrow, stacked
            assert [_what(c) for c in cards] == ["Solver Daily Summary"] * 2
        layout[-1].append((section["column_span"], _what(cards[0])))
    assert layout == [
        [
            "top",
            (1, "button: Solve now"),
            (1, "tile: Plan"),
            (1, "tile: Solve time"),
            (1, "tile: Limiting the plan now"),
        ],
        [
            "Performance",
            (4, "Plan-ahead cost, as estimated"),
            (1, "Nimbus Solver Quality (EPR / Regret)"),
            (1, "At a glance"),
            (1, "Score by day"),
            (1, "Where the plan's cost comes from"),
        ],
        [
            "Money",
            (1, "Last day, scored"),
            (1, "Money, plan ahead"),
            (1, "Where the cost comes from"),
            (1, "Battery, now"),
            (1, "markdown: Yesterday and tomorrow, kWh"),
            (1, "markdown: The plan's cost, by day, $"),
            (2, "Battery energy, tomorrow"),
        ],
        [
            "Plan",
            (4, "Battery: plan against measured"),
            # nimbus #1753: the plan's prices for its whole horizon, then one
            # card per day.
            (4, "Prices ahead"),
            # Household, 16 Oct 2026: "why release features which are not
            # visible" -- the price-safety card under the price chart.
            *[(1, "markdown: {{ ns.bt }}")] * 4,
            (1, "markdown: False-spike guard"),
            (1, "markdown: Price source"),
            (1, "markdown: Battery hold"),
            (1, "markdown: Consolidated prices"),
            (2, "Solver Forecast — Whole Plan"),
            (1, "Nimbus Solver Quality (EPR / Regret)"),
            (1, "Solver Daily Summary"),
            (1, "nimbus:risk-slider"),
            (1, "nimbus:risk-slider"),
            (1, "nimbus:risk-slider"),
            (1, "markdown: What the sliders do"),
            # nimbus #1797: the two-stage stochastic solve, shadow or drive.
            (2, "markdown: Stochastic dispatch"),
            (2, "markdown: Stochastic against the main plan"),
        ],
        [
            "Basic settings",
            (1, "Battery"),
            (1, "Grid and fees"),
            (1, "P2P"),
            (1, "P2P hours and matching"),
        ],
        [
            "Advanced settings",
            (1, "Costs and stability"),
            (1, "Features"),
            (1, "Scoring"),
            (1, "Reserve and price spikes"),
        ],
    ]


def test_solve_now_calls_the_solver() -> None:
    button = solver_tab.template()[0]["cards"][0]
    assert button["tap_action"] == {
        "action": "perform-action",
        "perform_action": "nimbus_load.solve_now",
    }


# Rendered heights in px, measured off the household's screenshots of devhub
# (11 Oct 2026): a number or time box 67, a toggle or a reading 44, a select
# 60, and a divider with a line of text 38 + 21 per wrapped line (about 62
# characters at a quarter of the screen). The first estimate counted rows
# only, and the cards visibly did not line up.
_NUMBER, _OTHER, _SELECT = 67, 44, 60


def _height(card) -> int:
    total = 0
    for e in card["entities"]:
        if e.get("type") == "conditional":
            continue
        if e.get("type") == "section":
            total += 38 + 21 * -(-len(e["label"]) // 62)
        elif e["entity"].endswith("_policy"):
            total += _SELECT
        elif e["entity"].startswith("@solver_") and not e["entity"].endswith(
            ("_enabled", "_dry_run", "_known_solar", "_armed")
        ):
            total += _NUMBER
        else:
            total += _OTHER
    return total


def test_cards_side_by_side_end_on_one_line() -> None:
    """Household, 11 Oct 2026: bottoms line up by content -- more settings,
    or more explanation where a card has fewer -- never by empty space."""
    rows: list[list[int]] = []
    for section in solver_tab.template():
        card = section["cards"][0]
        if _block(card):
            rows.append([])
        elif card["type"] == "entities" and rows:
            assert card["entities"][-1]["type"] == "section", card["title"]
            assert card["show_header_toggle"] is False, card["title"]
            rows[-1].append(_height(card))
    for heights in (r for r in rows if r):
        assert max(heights) - min(heights) <= 30, heights


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
            if c.get("display_mode") == "slider" or c.get("features")
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


def test_each_row_is_one_height_and_fills_it() -> None:
    """Household, 12 Oct 2026: cards side by side end on one line; chart
    cards fill their cell; nothing scrolls but the until-midnight table."""
    for first in (
        "Nimbus Solver Quality (EPR / Regret)",
        "Solver Forecast — Whole Plan",
    ):
        row = [s for s in _row(first) if len(s["cards"]) == 1][:2]
        heights = {
            json.dumps(s["cards"][0]["grid_options"])
            for s in row
            if not s["cards"][0].get("features")
        }
        assert len(heights) == 1, (first, heights)
        for s in row:
            style = s["cards"][0].get("card_mod", {}).get("style", "")
            if isinstance(style, dict):
                style = style["."]
            if s["cards"][0].get("features"):
                continue
            assert "height: 100%" in style
            if s["cards"][0].get("title") != "Solver Forecast — Whole Plan":
                assert "overflow-y: auto" not in style


def test_the_chart_figures_sit_below_the_chart() -> None:
    """Household, 12 Oct 2026: keep the titles, move the figures down."""
    for s in _row("Nimbus Solver Quality (EPR / Regret)")[1:]:
        assert "#header__states { order: 2" in s["cards"][0]["card_mod"]["style"]


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
    for line in ("Measured", "Plan now", "Plan ahead", "Buy price", "Buy price ahead"):
        assert names[line]["curve"] == "stepline", line
    # Household, 14 Oct 2026: period 0 alone drew 0 all morning while every
    # plan held a charge for later; this line shows the charge being held.
    held = names["Charge held for later"]
    assert held["entity"] == "@nimbus_solver_battery_forecast"
    assert held["attribute"] == "charge_before_block_pct"
    assert held["yaxis_id"] == "soc"
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
        if item["name"].startswith("${@name:"):
            # Resolved per install to the entity's own short name
            # ("SoC Our EV", sensor_battery_soc.py).
            continue
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
    """With no entities at all only the Solve now button and the block bands
    are left: every card, chart and slider goes, and its column with it."""
    view = solver_tab.resolve(solver_tab.template(), lambda _k: None)
    assert [[_block(c) for c in s["cards"]] for s in view] == [
        [None],  # Solve now needs no entity
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


def test_a_tab_that_only_mentions_the_plan_status_is_not_a_solver_tab() -> None:
    """11 Oct 2026: a tester's own Entities tab listed
    sensor.nimbus_solver_lp_status, and 0.94.448 took that for a Solver tab
    and added none. Only the tab Nimbus wrote counts."""
    entities_tab = {
        "title": "Entities",
        "path": "entities",
        "cards": [{"type": "entities", "entities": ["sensor.nimbus_solver_lp_status"]}],
    }
    views, done = dash._sync_views([dict(entities_tab)], {}, [], resolve=_resolve)
    assert "added:solver" in done
    assert views[0] == entities_tab  # untouched
    assert any(v.get("title") == "Solver" for v in views)


def test_an_install_wrongly_marked_by_that_guess_gets_the_tab_once() -> None:
    """The record 0.94.448 left on such an install -- the design, no path --
    adds the tab on the next restart, and only once."""
    entities_tab = {
        "title": "Entities",
        "path": "entities",
        "cards": [{"type": "entities", "entities": ["sensor.nimbus_solver_lp_status"]}],
    }
    spec = next(s for s in dash.STANDARD_VIEWS if s["key"] == "solver")
    mem = {"solver": {"design": dash.design_of(spec), "path": None, "written": None}}
    views, done = dash._sync_views([dict(entities_tab)], mem, [], resolve=_resolve)
    assert [d for d in done if "solver" in d], done
    assert sum(1 for v in views if v.get("title") == "Solver") == 1
    assert mem["solver"]["path"] is not None
    again, done2 = dash._sync_views(
        json.loads(json.dumps(views)), mem, [], resolve=_resolve
    )
    assert [d for d in done2 if "solver" in d] == []
    assert again == views


def test_a_deleted_solver_tab_stays_deleted() -> None:
    """The household's rule is unchanged: a tab Nimbus wrote and the household
    deleted is not put back for the same design."""
    mem: dict = {}
    views, _ = dash._sync_views([], mem, [], created=True, resolve=_resolve)
    without = [v for v in views if v.get("title") != "Solver"]
    again, done = dash._sync_views(
        json.loads(json.dumps(without)), mem, [], resolve=_resolve
    )
    assert [d for d in done if "solver" in d] == []
    assert not any(v.get("title") == "Solver" for v in again)


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


def test_the_energy_table_is_yesterday_measured_beside_tomorrow_planned() -> None:
    """Household, 11 Oct 2026: one table, side by side, midnight to midnight,
    plain labels. The donut it replaces summed the whole four-day plan."""
    content = next(
        s["cards"][0]["content"]
        for s in solver_tab.template()
        if "Yesterday and tomorrow" in s["cards"][0].get("content", "")
    )
    assert "| | Yesterday, measured | Tomorrow, planned |" in content
    for label in ("Solar", "House", "Grid in", "Grid out", "Battery in", "Battery out"):
        assert f"| {label} |" in content
    assert "timedelta(days=1)" in content  # tomorrow, not "the next 24 h"
    assert "'last_period'" in content  # yesterday's total, from Topology's meters
    assert "${@option:switchboard_house_load_energy_daily_sensor}" in content


def test_the_energy_pie_is_tomorrow_beside_the_table() -> None:
    """Household, 11 Oct 2026: keep the table AND the pie, one day each.
    12 Oct: the table and the cost by day one column each, the pie two."""
    sections = solver_tab.template()
    titles = [
        (s["column_span"], s["cards"][0].get("header", {}).get("title"))
        for s in sections
    ]
    i = titles.index((2, "Battery energy, tomorrow"))
    assert "Yesterday and tomorrow" in sections[i - 2]["cards"][0]["content"]
    assert "cost_breakdown_by_day" in sections[i - 1]["cards"][0]["content"]
    assert [sections[k]["column_span"] for k in (i - 2, i - 1, i)] == [1, 1, 2]
    assert (
        len(
            {
                json.dumps(sections[k]["cards"][0]["grid_options"])
                for k in (i - 2, i - 1, i)
            }
        )
        == 1
    )
    for series in sections[i]["cards"][0]["series"]:
        assert "setHours(24, 0, 0, 0)" in series["data_generator"]


def test_battery_chart_reads_watts_as_kw_and_labels_its_plan_series() -> None:
    """11 Oct 2026: a tester's battery sensor in W was drawn as kW, and the
    plan-ahead SoC and price series were labelled kW."""
    chart = solver_tab._chart()
    by_name = {s["name"]: s for s in chart["series"]}
    measured = by_name["Measured"]
    assert measured["unit"] == "kW"
    assert '"w": 0.001' in measured["transform"]
    assert '"MW": 1000.0' in measured["transform"]
    assert "${@option:battery_sensor}" in measured["transform"]
    resolved = solver_tab.resolve(
        measured, lambda k: "sensor.batt" if k == "option:battery_sensor" else None
    )
    assert "hass.states['sensor.batt']" in resolved["transform"]
    assert by_name["SoC ahead"]["unit"] == "%"
    assert by_name["Buy price ahead"]["unit"] == "c/kWh"


def test_soc_ahead_is_drawn_at_the_end_of_each_period() -> None:
    """A plan row's soc_pct is the SoC after its period (household, 12 Oct
    2026: a sale's SoC drop showed a period before its battery bar)."""
    gens = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("name") == "SoC ahead":
                gens.append(node["data_generator"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(solver_tab.template())
    assert gens
    assert all("p.hours" in g and "3600000" in g for g in gens)


def test_forecast_card_soc_is_drawn_at_the_end_of_each_period() -> None:
    import pathlib

    js = (
        pathlib.Path(solver_tab.__file__).parent
        / "frontend"
        / "nimbus-forecast-card.js"
    ).read_text(encoding="utf-8")
    soc = js[js.index('name: "Solver SoC % (proposed)"') :][:400]
    assert "(p.hours || 0) * 3600000, p.soc_pct" in soc
