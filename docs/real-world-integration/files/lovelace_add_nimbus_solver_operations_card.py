#!/usr/bin/env python3
"""Adds the "Nimbus Solver — Operations" + "Nimbus-Only Counterfactual
(Stage 1)" markdown card pair to the live dashboard.

Both are fully portable -- every entity referenced is a standard
Nimbus-published sensor with the same name on any install
(sensor.nimbus_solver_battery_forecast, sensor.nimbus_solver_quality_
report, sensor.nimbus_counterfactual_soc_5pm), plus one genuinely
optional, gracefully-degrading reference (sensor.p2p_nightly_volume_
threshold_kwh -- a household-specific P2P-volume predictor this
household runs; the card's own Jinja already shows "Not yet available"
rather than erroring if it doesn't exist on your install).

Operations card: live solve performance (status/solve time/periods),
tonight's plan economics, the latest EPR quality score, and (if you
run it) the P2P volume threshold.

Counterfactual card: Stage 1 of this project's own "Nimbus -> HAEO
replacement" readiness checklist -- "if Nimbus alone had been deciding
since midnight, would the battery still have been ready for tonight's
delivery window", scored daily against sensor.nimbus_counterfactual_
soc_5pm (see nimbus_counterfactual_writer.py, same folder).

Self-locating (same pattern as lovelace_add_nimbus_solver_quality_
card.py, same folder): finds whichever dashboard already shows
sensor.nimbus_solver_battery_forecast and adds these cards to that
same section, falling back to a new section on dashboard_nimbus's
first view if nothing is found yet.

Deploy (Python-via-docker-exec, per this project's own standing
PowerShell-BOM-corruption rule):
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/lovelace_add_nimbus_solver_operations_card.py > /tmp/lovelace_add_nimbus_solver_operations_card.py
  docker cp /tmp/lovelace_add_nimbus_solver_operations_card.py opt_homeassistant_1:/tmp/
  docker exec opt_homeassistant_1 python3 /tmp/lovelace_add_nimbus_solver_operations_card.py
  docker restart opt_homeassistant_1
"""

import json

CANDIDATE_FILES = [
    "/config/.storage/lovelace.dashboard_nimbus",
    "/config/.storage/lovelace.dashboard_pv",
]
ANCHOR_ENTITY = "sensor.nimbus_solver_battery_forecast"

OPERATIONS_CARD = {
    "type": "markdown",
    "title": "Nimbus Solver — Operations",
    "content": "{% set fc = states.sensor.nimbus_solver_battery_forecast %}\n{% set qr = states.sensor.nimbus_solver_quality_report %}\n{% set th = states.sensor.p2p_nightly_volume_threshold_kwh %}\n\n### ⚡ Solve Performance\n{% if fc is not none and fc.state not in ('unavailable','unknown') %}\n| | |\n|---|---:|\n| Status | **{{ fc.attributes.get('status','?') }}** |\n| Solve time | **{{ fc.attributes.get('solve_seconds','?') }}s** |\n| Periods / Horizon | {{ fc.attributes.get('n_periods','?') }} / {{ fc.attributes.get('horizon_hours','?') }}h |\n| Clamped periods | {{ fc.attributes.get('n_clamped_periods','?') }} |\n| Last solved | {{ as_timestamp(fc.attributes.get('generated_at')) | timestamp_custom('%-I:%M:%S %p') if fc.attributes.get('generated_at') else '?' }} |\n{% else %}\n_Not yet available._\n{% endif %}\n\n### 💰 Tonight's Plan Economics\n{% if fc is not none and fc.state not in ('unavailable','unknown') %}\n| | |\n|---|---:|\n| Total cost (plan) | ${{ '%.2f'|format(fc.attributes.get('total_cost',0)|float) }} |\n| Total cost (+ fixed) | ${{ '%.2f'|format(fc.attributes.get('total_cost_with_fixed_costs',0)|float) }} |\n| P2P match fraction | {{ '%.1f'|format(fc.attributes.get('p2p_match_fraction',0)|float * 100) }}% |\n| Recent avg P2P volume | {{ fc.attributes.get('p2p_recent_avg_volume_kwh','?') }} kWh |\n{% else %}\n_Not yet available._\n{% endif %}\n\n### 🎯 Quality Score (EPR) — {{ qr.attributes.get('latest_date','?') if qr is not none else '?' }}\n{% if qr is not none and qr.state not in ('unavailable','unknown') %}\n| | |\n|---|---:|\n| EPR | **{{ '%.1f'|format(qr.state|float * 100) }}%** |\n| Theoretical max yield | ${{ qr.attributes.get('theoretical_maximum_yield','?') }} |\n| Value captured | ${{ qr.attributes.get('value_captured','?') }} |\n| Uplift available (regret) | ${{ qr.attributes.get('uplift_available','?') }} |\n| Real P2P earned | ${{ qr.attributes.get('real_p2p_dollars','?') }} / {{ qr.attributes.get('real_p2p_volume_kwh','?') }}kWh |\n| Tracking fidelity | {{ '%.0f'|format(qr.attributes.get('tracking_fidelity',0)|float * 100) }}% |\n| Tracking cost | ${{ '%.2f'|format(qr.attributes.get('tracking_cost',0)|float) }} |\n| Worst gap | {{ '%.1f'|format(qr.attributes.get('worst_gap_kw',0)|float) }}kW at {{ as_timestamp(qr.attributes.get('worst_gap_at_local')) | timestamp_custom('%-I:%M %p') if qr.attributes.get('worst_gap_at_local') else '?' }} |\n{% else %}\n_Not yet available._\n{% endif %}\n\n### 🛡️ Tonight's P2P Volume Threshold\n{% if th is not none and th.state not in ('unavailable','unknown') %}\n| | |\n|---|---:|\n| Threshold | **{{ th.state }} kWh** |\n| Model prediction | {{ th.attributes.get('model_prediction_kwh','?') }} kWh |\n| Historical floor | {{ th.attributes.get('floor_kwh','?') }} kWh |\n| Driving factor | {{ th.attributes.get('driver','?') }} |\n| Training days | {{ th.attributes.get('training_days','?') }} |\n{% else %}\n_Not yet available._\n{% endif %}\n",
}

COUNTERFACTUAL_CARD = {
    "type": "markdown",
    "title": "Nimbus-Only Counterfactual (Stage 1)",
    "content": "{% set cf = states.sensor.nimbus_counterfactual_soc_5pm %}\n{% if cf is not none and cf.state not in ('unavailable', 'unknown') %}\n**\"If Nimbus alone had been deciding since midnight -- would the battery still have been ready for tonight's P2P window?\"**\n\n| | |\n|---|---:|\n| Latest analyzed | {{ cf.attributes.get('latest_date', '?') }} |\n| Nimbus-only SoC at 5pm | **{{ cf.state }}%** |\n| Real SoC at 5pm (same day) | {{ cf.attributes.get('real_soc_5pm_pct', '?') }}% |\n| Viable (>= {{ cf.attributes.get('viable_threshold_pct', '?') }}%)? | {{ '<font color=\"lightgreen\">YES</font>' if cf.attributes.get('viable') else '<font color=\"orangered\">NO</font>' }} |\n| Nimbus-only SoC at midnight close | {{ cf.attributes.get('nimbus_only_soc_close_pct', '?') }}% |\n| Real SoC at midnight close | {{ cf.attributes.get('real_soc_close_pct', '?') }}% |\n\n**Trend so far:**\n\n| Date | Nimbus-only 5pm | Real 5pm | Viable |\n|---|---:|---:|:---:|\n{% set hist = cf.attributes.get('history', {}) %}\n{% for d in hist.keys() | sort(reverse=true) %}\n{% set day = hist[d] %}\n| {{ d }} | {{ day.nimbus_only_soc_5pm_pct }}% | {{ day.real_soc_5pm_pct }}% | {{ '<font color=\"lightgreen\">Y</font>' if day.viable else '<font color=\"orangered\">N</font>' }} |\n{% endfor %}\n{% else %}\n_Not yet available -- first run pending._\n{% endif %}\n",
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


def _replace_or_append(cards: list, new_card: dict) -> str:
    target_title = new_card.get("title")
    for i, c in enumerate(cards):
        if c.get("type") == new_card.get("type") and c.get("title") == target_title:
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
        view, section, _card = find_view_with_entity(views, ANCHOR_ENTITY)
        if view is not None:
            target_path = path
            data = candidate
            print(
                f"Found {ANCHOR_ENTITY} in {path}, view '{view.get('title', view.get('path'))}'"
            )
            if section is not None:
                section_cards = section.setdefault("cards", [])
                r1 = _replace_or_append(section_cards, OPERATIONS_CARD)
                r2 = _replace_or_append(section_cards, COUNTERFACTUAL_CARD)
                print(
                    f"  {r1} operations card, {r2} counterfactual card ({len(section_cards)} cards total)."
                )
            else:
                view_cards = view.setdefault("cards", [])
                r1 = _replace_or_append(view_cards, OPERATIONS_CARD)
                r2 = _replace_or_append(view_cards, COUNTERFACTUAL_CARD)
                print(
                    f"  {r1} operations card, {r2} counterfactual card ({len(view_cards)} cards total)."
                )
            break

    if target_path is None:
        path = "/config/.storage/lovelace.dashboard_nimbus"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        views = data["data"]["config"]["views"]
        if not views:
            raise RuntimeError(
                f"{path} has no views at all -- cannot add a fallback section"
            )
        view = views[0]
        if "sections" in view:
            view["sections"].append(
                {"type": "grid", "cards": [OPERATIONS_CARD, COUNTERFACTUAL_CARD]}
            )
            print(
                f"{ANCHOR_ENTITY} not found anywhere -- added a NEW section to {path}'s first view instead."
            )
        else:
            view.setdefault("cards", []).extend([OPERATIONS_CARD, COUNTERFACTUAL_CARD])
            print(
                f"{ANCHOR_ENTITY} not found anywhere -- appended cards to {path}'s first view's card list instead."
            )
        target_path = path

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"Saved {target_path}. Restart HA for this to take effect.")


if __name__ == "__main__":
    main()
