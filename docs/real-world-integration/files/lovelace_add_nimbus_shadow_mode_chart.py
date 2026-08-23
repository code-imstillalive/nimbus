#!/usr/bin/env python3
"""Adds the "Nimbus vs HAEO vs Real - Battery (Shadow Mode)" chart to
the live dashboard -- history AND forecast for each of Nimbus's own
plan, this household's real controller's plan (HAEO), and the real
measured battery power, all on one chart. This is what actually lets
you watch Nimbus's shadow-mode plan against reality over time, not
just read a single "current forecast" number.

*** NOT directly portable as-is -- read this before deploying. ***
Two of the three real entities this chart compares are household-
specific:
  - sensor.battery_active_power -- HAEO's own plan sensor. If you
    don't run HAEO (most installs won't), this series will just show
    "unavailable" -- swap it for YOUR real controller's own plan
    sensor, or delete the two "HAEO plan" series entirely for a clean
    2-way Nimbus-vs-Real comparison.
  - sensor.logger_battery_power -- this household's own real, measured
    battery power sensor. Swap for your own equivalent.
The one genuinely portable entity is sensor.nimbus_solver_battery_
forecast -- the same name on every install, this is what you're
actually validating Nimbus's own shadow-mode accuracy against.

Included anyway, same "show the real wiring, not a sanitized example"
philosophy as nimbus_solver_quality_writer.py (same folder, see its
own README section) -- real chart config with real quirks (correct
yaxis_id/extend_to/stroke_dash usage for a paired history+forecast
series pattern) is more useful as a working reference to copy from
than a stripped-down toy example would be.

Self-locating (same pattern as the sibling scripts in this folder):
finds whichever dashboard already shows sensor.nimbus_solver_battery_
forecast and adds this chart to that same section.

Deploy (Python-via-docker-exec, per this project's own standing
PowerShell-BOM-corruption rule):
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/lovelace_add_nimbus_shadow_mode_chart.py > /tmp/lovelace_add_nimbus_shadow_mode_chart.py
  docker cp /tmp/lovelace_add_nimbus_shadow_mode_chart.py opt_homeassistant_1:/tmp/
  docker exec opt_homeassistant_1 python3 /tmp/lovelace_add_nimbus_shadow_mode_chart.py
  docker restart opt_homeassistant_1
"""
import json

CANDIDATE_FILES = [
    "/config/.storage/lovelace.dashboard_nimbus",
    "/config/.storage/lovelace.dashboard_pv",
]
ANCHOR_ENTITY = "sensor.nimbus_solver_battery_forecast"

SHADOW_MODE_CHART_CARD = {'type': 'custom:apexcharts-card', 'header': {'title': 'Nimbus vs HAEO vs Real — Battery (Shadow Mode)', 'show': True, 'show_states': True, 'colorize_states': True}, 'graph_span': '30h', 'span': {'offset': '+24h'}, 'apex_config': {'chart': {'height': 300}, 'legend': {'show': True}, 'annotations': {'yaxis': [{'y': 0, 'strokeDashArray': 4, 'borderColor': '#666666', 'label': {'text': '0 kW', 'style': {'color': '#999999', 'background': 'transparent'}}}]}}, 'yaxis': [{'id': 'power', 'decimals': 1, 'apex_config': {'title': {'text': 'kW'}}}], 'series': [{'entity': 'sensor.logger_battery_power', 'name': 'Real (measured)', 'yaxis_id': 'power', 'color': '#ff9800', 'extend_to': False, 'group_by': {'func': 'last', 'duration': '1m'}, 'stroke_width': 2}, {'entity': 'sensor.nimbus_solver_battery_forecast', 'name': 'Nimbus plan', 'yaxis_id': 'power', 'color': '#00e5ff', 'curve': 'stepline', 'data_generator': 'const fc = entity.attributes.forecast || []; return fc.map(p => [new Date(p.time).getTime(), p.battery_kw]);', 'stroke_width': 4, 'stroke_dash': 8, 'show': {'in_header': False}}, {'entity': 'sensor.nimbus_solver_battery_forecast', 'name': 'Nimbus plan', 'yaxis_id': 'power', 'color': '#00e5ff', 'show': {'in_chart': False, 'in_header': True}}, {'entity': 'sensor.battery_active_power', 'name': 'HAEO plan', 'yaxis_id': 'power', 'color': '#e91e63', 'curve': 'stepline', 'stroke_width': 4, 'stroke_dash': 2, 'data_generator': 'const fc = entity.attributes.forecast || []; return fc.map(p => [new Date(p.time).getTime(), p.value]);', 'show': {'in_header': False}}, {'entity': 'sensor.battery_active_power', 'name': 'HAEO plan', 'yaxis_id': 'power', 'color': '#e91e63', 'show': {'in_chart': False, 'in_header': True}}, {'entity': 'sensor.nimbus_solver_battery_forecast', 'name': 'Nimbus plan (history)', 'yaxis_id': 'power', 'color': '#00e5ff', 'stroke_width': 2, 'extend_to': False, 'group_by': {'func': 'last', 'duration': '1m'}, 'show': {'in_header': False}}, {'entity': 'sensor.battery_active_power', 'name': 'HAEO plan (history)', 'yaxis_id': 'power', 'color': '#e91e63', 'stroke_width': 2, 'extend_to': False, 'group_by': {'func': 'last', 'duration': '1m'}, 'show': {'in_header': False}}], 'now': {'show': True, 'color': '#ffffff', 'label': 'Now'}, 'grid_options': {'columns': 'full'}}


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


def _card_title(c: dict):
    if c.get("type") == "custom:apexcharts-card":
        return c.get("header", {}).get("title")
    return c.get("title")


def _replace_or_append(cards: list, new_card: dict) -> str:
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
            target_cards = section.setdefault("cards", []) if section is not None else view.setdefault("cards", [])
            r = _replace_or_append(target_cards, SHADOW_MODE_CHART_CARD)
            print(f"  {r} shadow-mode chart ({len(target_cards)} cards total in target).")
            break

    if target_path is None:
        path = "/config/.storage/lovelace.dashboard_nimbus"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        views = data["data"]["config"]["views"]
        if not views:
            raise RuntimeError(f"{path} has no views at all -- cannot add a fallback section")
        view = views[0]
        if "sections" in view:
            view["sections"].append({"type": "grid", "cards": [SHADOW_MODE_CHART_CARD]})
            print(f"{ANCHOR_ENTITY} not found anywhere -- added a NEW section to {path}'s first view instead.")
        else:
            view.setdefault("cards", []).append(SHADOW_MODE_CHART_CARD)
            print(f"{ANCHOR_ENTITY} not found anywhere -- appended card to {path}'s first view's card list instead.")
        target_path = path

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"Saved {target_path}. Restart HA for this to take effect.")


if __name__ == "__main__":
    main()
