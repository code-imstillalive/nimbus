#!/usr/bin/env python3
"""Adds the 3 risk-aversion "mushroom slider" cards + their legend to
the live dashboard -- import_price_risk_aversion, export_price_risk_
aversion, and risk_aversion, all real Nimbus dashboard-configurable
number.py entities (0.0-1.0), each with a red-to-green gradient
card_mod style so the slider's own color reflects its current value at
a glance.

Requires two HACS frontend cards this repo doesn't provide itself:
"mushroom" (custom:mushroom-number-card) and "card-mod" (the gradient
styling). Both are widely-used, actively-maintained HACS cards -- if
you don't have them installed yet: HACS -> Frontend -> search
"Mushroom Cards" / "card-mod".

What each dial means (see the legend card's own content below, or
this repo's solver/network.py -- build_plan()'s own docstring):
- risk_aversion: how much the Solver hedges against its OWN forecast
  uncertainty (wider confidence bands = more conservative dispatch).
- import_price_risk_aversion / export_price_risk_aversion: independent
  hedging against import and export price risk specifically -- kept
  separate on purpose (Mark Purcell's own finding: a single shared
  dial forces charge/discharge hedging to move together even though
  they're economically opposite decisions).

0.0 = no hedging (trust the point forecast exactly), 1.0 = maximum
hedging (always plan for the worst case within the confidence band).

Self-locating (same pattern as the sibling scripts in this folder):
finds whichever dashboard already shows sensor.nimbus_solver_battery_
forecast and adds these cards to that same section.

Deploy (Python-via-docker-exec, per this project's own standing
PowerShell-BOM-corruption rule):
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/lovelace_add_nimbus_risk_aversion_sliders.py > /tmp/lovelace_add_nimbus_risk_aversion_sliders.py
  docker cp /tmp/lovelace_add_nimbus_risk_aversion_sliders.py opt_homeassistant_1:/tmp/
  docker exec opt_homeassistant_1 python3 /tmp/lovelace_add_nimbus_risk_aversion_sliders.py
  docker restart opt_homeassistant_1
"""
import json

CANDIDATE_FILES = [
    "/config/.storage/lovelace.dashboard_nimbus",
    "/config/.storage/lovelace.dashboard_pv",
]
ANCHOR_ENTITY = "sensor.nimbus_solver_battery_forecast"

SLIDER_CARDS = [{'type': 'custom:mushroom-number-card', 'entity': 'number.nimbus_solver_import_price_risk_aversion', 'name': 'Import Price Risk Aversion', 'secondary_info': 'state', 'icon': 'mdi:speedometer', 'layout': 'vertical', 'display_mode': 'slider', 'fill_container': True, 'card_mod': {'style': "ha-card {\n  {% set v = states('number.nimbus_solver_import_price_risk_aversion') | float(0) %}\n  {% set r = (244 - (244 - 76) * v) | round | int %}\n  {% set g = (67 + (175 - 67) * v) | round | int %}\n  {% set b = (54 + (80 - 54) * v) | round | int %}\n  {% set c = 'rgb(' ~ r ~ ',' ~ g ~ ',' ~ b ~ ')' %}\n  --icon-color: {{ c }};\n  --main-color: {{ c }};\n  --slider-color: {{ c }};\n  --rgb-state-active: {{ r }},{{ g }},{{ b }};\n  --card-mod-icon-color: {{ c }};\n}"}}, {'type': 'custom:mushroom-number-card', 'entity': 'number.nimbus_solver_export_price_risk_aversion', 'name': 'Export Price Risk Aversion', 'secondary_info': 'state', 'icon': 'mdi:speedometer', 'layout': 'vertical', 'display_mode': 'slider', 'fill_container': True, 'card_mod': {'style': "ha-card {\n  {% set v = states('number.nimbus_solver_export_price_risk_aversion') | float(0) %}\n  {% set r = (244 - (244 - 76) * v) | round | int %}\n  {% set g = (67 + (175 - 67) * v) | round | int %}\n  {% set b = (54 + (80 - 54) * v) | round | int %}\n  {% set c = 'rgb(' ~ r ~ ',' ~ g ~ ',' ~ b ~ ')' %}\n  --icon-color: {{ c }};\n  --main-color: {{ c }};\n  --slider-color: {{ c }};\n  --rgb-state-active: {{ r }},{{ g }},{{ b }};\n  --card-mod-icon-color: {{ c }};\n}"}}, {'type': 'custom:mushroom-number-card', 'entity': 'number.nimbus_solver_risk_aversion', 'name': 'Risk Aversion', 'secondary_info': 'state', 'icon': 'mdi:speedometer', 'layout': 'vertical', 'display_mode': 'slider', 'fill_container': True, 'card_mod': {'style': "ha-card {\n  {% set v = states('number.nimbus_solver_risk_aversion') | float(0) %}\n  {% set r = (244 - (244 - 76) * v) | round | int %}\n  {% set g = (67 + (175 - 67) * v) | round | int %}\n  {% set b = (54 + (80 - 54) * v) | round | int %}\n  {% set c = 'rgb(' ~ r ~ ',' ~ g ~ ',' ~ b ~ ')' %}\n  --icon-color: {{ c }};\n  --main-color: {{ c }};\n  --slider-color: {{ c }};\n  --rgb-state-active: {{ r }},{{ g }},{{ b }};\n  --card-mod-icon-color: {{ c }};\n}"}}]

LEGEND_CARD = {'type': 'markdown', 'content': '### 🎚️ Risk Sliders -- what they mean\n| | |\n|---|---:|\n| **Risk Aversion** | Hedges *solar/load* forecast error. 0 = trust the forecast completely. 1 = plan for the worst case (less solar, more load than predicted) -- keeps more energy in reserve. |\n| **Import Price Risk Aversion** | Hedges the *import* side of the price forecast. 0 = trust the forecast (may wait for a predicted cheaper price later). 1 = assumes import could genuinely spike to the real historical worst-case for this time of day, and charges now rather than gambling on waiting. |\n| **Export Price Risk Aversion** | Hedges the *export* side of the price forecast, independently of import. 0 = trust the forecast (may hold charge for a predicted better export price later). 1 = assumes export could genuinely fall to the real historical worst-case for this time of day, and sells/discharges now rather than gambling on waiting. |\n\n🟢 Green (toward 1.0) = more urgent/defensive -- act now.\n🔴 Red (toward 0.0) = more patient -- trust the forecast.\n', 'title': 'Risk Sliders'}


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


def _card_key(c: dict):
    return (c.get("type"), c.get("entity") or c.get("title"))


def _replace_or_append(cards: list, new_card: dict) -> str:
    target = _card_key(new_card)
    for i, c in enumerate(cards):
        if _card_key(c) == target:
            cards[i] = new_card
            return "Replaced existing"
    cards.append(new_card)
    return "Appended new"


def main():
    all_new_cards = SLIDER_CARDS + [LEGEND_CARD]
    target_path = None
    data = None
    for path in CANDIDATE_FILES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                candidate = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        views = candidate["data"]["config"]["views"]
        view, section, _card = find_view_with_entity(views, ANCHOR_ENTITY)
        if view is not None:
            target_path = path
            data = candidate
            print(f"Found {ANCHOR_ENTITY} in {path}, view '{view.get('title', view.get('path'))}'")
            target_cards = section.setdefault("cards", []) if section is not None else view.setdefault("cards", [])
            results = [_replace_or_append(target_cards, c) for c in all_new_cards]
            print(f"  {results} ({len(target_cards)} cards total in target).")
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
            view["sections"].append({"type": "grid", "cards": all_new_cards})
            print(f"{ANCHOR_ENTITY} not found anywhere -- added a NEW section to {path}'s first view instead.")
        else:
            view.setdefault("cards", []).extend(all_new_cards)
            print(f"{ANCHOR_ENTITY} not found anywhere -- appended cards to {path}'s first view's card list instead.")
        target_path = path

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"Saved {target_path}. Restart HA for this to take effect.")


if __name__ == "__main__":
    main()
