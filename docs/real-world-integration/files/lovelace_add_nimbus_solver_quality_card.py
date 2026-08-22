#!/usr/bin/env python3
"""Adds a real "Nimbus Solver Quality" card pair to the NUC's live
dashboard -- the actual "on the screen" delivery for the explicit ask:
"I think we should have a live tracker of the regret value and EPR
score on the screen as we keep going through the solver so we know if
it is doing better."

Card 1 (markdown): the LATEST scored day's real headline numbers --
EPR, real $ regret, real settled P2P $/kWh, tracking fidelity -- same
formatting convention as this project's already-established "LV Daily
Summary" markdown card (<font color=...>, not <span style=...>, which
HA's markdown sanitizer strips -- see CLAUDE.md's own PR #242 note).

Card 2 (apexcharts-card): a real, growing TREND over every day the
Solver has actually been scored -- EPR (0-100%, left axis, line) and
regret $ (right axis, column) -- read directly from sensor.nimbus_
solver_quality_report's own `history` dict attribute via data_generator,
same technique already proven for sensor.lv_v2_p2p_confirmed_history's
own gold "Grand Total (Confirmed)" line. This is the part that actually
answers "are we doing better", not just today's single snapshot.

Self-locating, not hardcoded to a specific dashboard/view index: this
script has never been run before, so it doesn't know in advance which
dashboard/view already shows sensor.nimbus_solver_battery_forecast (the
Solver's existing live plan sensor, added via the HA UI directly, not a
script). Searches dashboard_nimbus first (where this session's own
Solver/Topology work has lived), falls back to dashboard_pv, and if
neither already references it, adds a new section to dashboard_nimbus's
own first view rather than silently doing nothing.

Deploy (Python-via-docker-exec, per this project's own standing
PowerShell-BOM-corruption rule -- never write lovelace files from
Windows PowerShell Out-File):
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/lovelace_add_nimbus_solver_quality_card.py > /tmp/lovelace_add_nimbus_solver_quality_card.py
  docker cp /tmp/lovelace_add_nimbus_solver_quality_card.py opt_homeassistant_1:/tmp/
  docker exec opt_homeassistant_1 python3 /tmp/lovelace_add_nimbus_solver_quality_card.py
  docker restart opt_homeassistant_1
"""
import json

CANDIDATE_FILES = [
    "/config/.storage/lovelace.dashboard_nimbus",
    "/config/.storage/lovelace.dashboard_pv",
]
ANCHOR_ENTITY = "sensor.nimbus_solver_battery_forecast"

QUALITY_MD_CARD = {
    "type": "markdown",
    "title": "Nimbus Solver Quality (EPR / Regret)",
    # grid_options.rows (2026-08-17, direct ask: "could we please set
    # these tables to the correct row heights... scrolling is ok on the
    # big markdown") -- none of the 4 cards in this section had an
    # explicit height, so HA's own `type: grid` section default
    # (stretch every card to match the tallest sibling in the same row)
    # left this and the trend chart with a huge dead-space gap below
    # their real content, matched to the 48-row Forecast table's own
    # natural height. First deployed at rows:10, then bumped to rows:22
    # per direct follow-up ("could these cards just have rows: 22 or so
    # or a number I could change the way I want?") -- matched on all 4
    # cards in this section so none of them stretch to the others.
    # Once a card has ANY explicit grid_options set (unlike before this
    # fix), HA's own dashboard editor shows a real resize handle on it
    # (edit dashboard -> hover the card -> drag its bottom-right corner)
    # -- this number is now genuinely user-adjustable live in the UI,
    # not something that needs a script redeploy every time; this value
    # is just the starting point.
    "grid_options": {"rows": 22},
    "content": (
        "{% set s = states.sensor.nimbus_solver_quality_report %}"
        "{% if s and s.attributes.get('latest_date') %}"
        "{% set epr = s.attributes.get('epr', 0) %}"
        "{% set epr_color = 'lightgreen' if epr >= 0.7 else ('orange' if epr >= 0.4 else 'red') %}"
        "**Latest scored day: {{ s.attributes.latest_date }}**\n\n"
        "| Metric | Value |\n"
        "|---|---|\n"
        "| EPR (Economic Performance Ratio) | <font color='{{ epr_color }}'>**{{ (epr * 100) | round(1) }}%**</font> |\n"
        "| Value captured | <font color='lightgreen'>${{ s.attributes.get('value_captured', 0) | round(2) }}</font> |\n"
        "| Uplift available (regret) | <font color='orange'>${{ s.attributes.get('uplift_available', 0) | round(2) }}</font> |\n"
        "| Theoretical maximum yield | ${{ s.attributes.get('theoretical_maximum_yield', 0) | round(2) }} |\n"
        "| Real P2P settled | ${{ s.attributes.get('real_p2p_dollars', 0) | round(2) }} / "
        "{{ s.attributes.get('real_p2p_volume_kwh', 0) | round(1) }}kWh |\n"
        "| Tracking fidelity | {{ (s.attributes.get('tracking_fidelity', 0) * 100) | round(1) }}% |\n"
        "| Tracking cost | ${{ s.attributes.get('tracking_cost', 0) | round(2) }} |\n"
        "| J_ref (idle) / J_ach (real) / J_star (perfect foresight) | "
        "${{ s.attributes.get('j_ref', 0) | round(2) }} / "
        "${{ s.attributes.get('j_ach', 0) | round(2) }} / "
        "${{ s.attributes.get('j_star', 0) | round(2) }} |\n\n"
        "*EPR = share of the real available economic uplift the Solver's own plan actually captured "
        "(100% = matched a real perfect-foresight oracle; 0% = captured nothing beyond doing nothing at all). "
        "Scored once daily against yesterday's real, fully-settled data.*"
        "{% else %}"
        "*No scored day yet -- runs once daily at 06:17 AEST against yesterday's real settled data.*"
        "{% endif %}"
    ),
}

QUALITY_TREND_CHART_CARD = {
    "type": "custom:apexcharts-card",
    "header": {"show": True, "title": "Nimbus Solver Quality Trend", "show_states": False},
    # Matches QUALITY_MD_CARD's own grid_options.rows -- see its comment.
    "grid_options": {"rows": 22},
    "graph_span": "30d",
    "yaxis": [
        {"id": "epr", "min": 0, "max": 100, "decimals": 0, "apex_config": {"title": {"text": "EPR %"}}},
        {"id": "regret", "opposite": True, "decimals": 0, "apex_config": {"title": {"text": "Regret $"}}},
    ],
    "series": [
        {
            "entity": "sensor.nimbus_solver_quality_report",
            "name": "EPR",
            "yaxis_id": "epr",
            "type": "line",
            "color": "#4caf50",
            "stroke_width": 3,
            "data_generator": (
                "return Object.entries(entity.attributes.history || {})"
                ".sort((a, b) => new Date(a[0]) - new Date(b[0]))"
                ".map(([date, d]) => [new Date(date + 'T12:00:00').getTime(), Math.round((d.epr || 0) * 100)]);"
            ),
        },
        {
            "entity": "sensor.nimbus_solver_quality_report",
            "name": "Regret $",
            "yaxis_id": "regret",
            "type": "column",
            "color": "#ff9800",
            "data_generator": (
                "return Object.entries(entity.attributes.history || {})"
                ".sort((a, b) => new Date(a[0]) - new Date(b[0]))"
                ".map(([date, d]) => [new Date(date + 'T12:00:00').getTime(), Math.round((d.regret_dollars || 0) * 100) / 100]);"
            ),
        },
    ],
}


def find_view_with_entity(views, entity_id):
    for v in views:
        for section in v.get("sections", []):
            for card in section.get("cards", []):
                if entity_id in json.dumps(card):
                    return v, section, None
        for card in v.get("cards", []):
            if entity_id in json.dumps(card):
                return v, None, card
    return None, None, None


def _card_title(c: dict) -> str | None:
    """A card's own display title, regardless of card type -- markdown
    cards use a top-level `title`, apexcharts-card uses `header.title`.
    """
    if c.get("type") == "markdown":
        return c.get("title")
    if c.get("type") == "custom:apexcharts-card":
        return c.get("header", {}).get("title")
    return None


def _replace_or_append(cards: list, new_card: dict) -> str:
    """Real bug fixed 2026-08-17 (found while adding grid_options.rows to
    these same 2 cards): this used to unconditionally APPEND every run,
    with no check for an already-deployed copy -- re-running this script
    to apply a real content/styling change would have DUPLICATED both
    cards instead of updating them, the exact same class of bug already
    found and fixed in the sibling lovelace_add_nimbus_solver_forecast_
    table.py the same day. Matches by title (via _card_title(), which
    knows how to read either card type's own title field) -- replace an
    existing match in place, only append if genuinely new.
    """
    target_title = _card_title(new_card)
    for i, c in enumerate(cards):
        if c.get("type") == new_card.get("type") and _card_title(c) == target_title:
            cards[i] = new_card
            return "Replaced existing"
    cards.append(new_card)
    return "Appended new"


def main():
    target_path = None
    data = None
    for path in CANDIDATE_FILES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                candidate = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        views = candidate["data"]["config"]["views"]
        view, section, card = find_view_with_entity(views, ANCHOR_ENTITY)
        if view is not None:
            target_path = path
            data = candidate
            print(f"Found {ANCHOR_ENTITY} in {path}, view '{view.get('title', view.get('path'))}'")
            if section is not None:
                section_cards = section.setdefault("cards", [])
                r1 = _replace_or_append(section_cards, QUALITY_MD_CARD)
                r2 = _replace_or_append(section_cards, QUALITY_TREND_CHART_CARD)
                print(f"  {r1} quality card, {r2} trend chart card in that view's section ({len(section_cards)} cards total).")
            else:
                view_cards = view.setdefault("cards", [])
                r1 = _replace_or_append(view_cards, QUALITY_MD_CARD)
                r2 = _replace_or_append(view_cards, QUALITY_TREND_CHART_CARD)
                print(f"  {r1} quality card, {r2} trend chart card in that view's card list ({len(view_cards)} cards total).")
            break

    if target_path is None:
        # Fall back: add a new section to dashboard_nimbus's own first view.
        path = "/config/.storage/lovelace.dashboard_nimbus"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        views = data["data"]["config"]["views"]
        if not views:
            raise RuntimeError(f"{path} has no views at all -- cannot add a fallback section")
        view = views[0]
        if "sections" in view:
            view["sections"].append({"type": "grid", "cards": [QUALITY_MD_CARD, QUALITY_TREND_CHART_CARD]})
            print(f"{ANCHOR_ENTITY} not found anywhere -- added a NEW section to {path}'s first view instead.")
        else:
            view.setdefault("cards", []).extend([QUALITY_MD_CARD, QUALITY_TREND_CHART_CARD])
            print(f"{ANCHOR_ENTITY} not found anywhere -- appended cards to {path}'s first view's card list instead.")
        target_path = path

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"Saved {target_path}. Restart HA for this to take effect.")


if __name__ == "__main__":
    main()
