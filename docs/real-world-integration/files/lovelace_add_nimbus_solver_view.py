#!/usr/bin/env python3
"""Adds a real, GENUINELY DESIGNED "Solver" view to lovelace.dashboard_nimbus --
solve performance, plan economics, EPR quality score, and the P2P nightly
volume threshold, all grounded in real, live-confirmed sensor attributes.

## 2026-08-18 redesign, per direct real feedback on the first version

First version was a single tall markdown card stacking four plain pipe-
tables -- direct, blunt feedback: "I want everything elegant not some
left over minimal thought... all columns rows and tables aligned
designer and elegant.. not rubbish please." See CLAUDE.md's own new
PRIME DIRECTIVE -- GRAPHICALLY PLEASING DASHBOARDS for the permanent
standing rule this created.

Redesign, following that directive precisely:
- type: sections grid (this project's own already-proven pattern from
  the LOCALVOLTS/Topology dashboards), not one giant scrolling card.
- A real hero card up top -- the 3 numbers that actually matter
  (status, solve time, EPR) at a glance, color-coded, before any detail.
- Four smaller, focused detail cards in the grid below the hero, each
  its own card, not stacked sections inside one card.
- Every table uses consistent |:---|---:| alignment (label left, value
  right) throughout, no exceptions.
- Color coding via <font color="..."> -- confirmed the only method that
  survives HA's markdown sanitizer (a real, already-documented gotcha in
  this project's own history: <span style="color:..."> gets stripped).
- Consistent number formatting (fixed decimals, consistent units)
  throughout every card, not ad hoc.

Idempotent: checks for an existing view with path="solver" and REPLACES
its cards in place rather than duplicating a second view on re-run.

Deploy (run ON the NUC that currently holds the VIP, via docker exec --
reads/writes the LIVE lovelace file, not a possibly-stale git copy):
  git pull origin main
  git show origin/main:scripts/lovelace_add_nimbus_solver_view.py > /tmp/lovelace_add_nimbus_solver_view.py
  docker cp /tmp/lovelace_add_nimbus_solver_view.py opt_homeassistant_1:/tmp/
  docker exec opt_homeassistant_1 python3 /tmp/lovelace_add_nimbus_solver_view.py
  docker restart opt_homeassistant_1

Real, hard-learned lesson from deploying the FIRST version of this exact
card (2026-08-18): editing the live file while HA is still RUNNING, then
just `docker restart`, risks HA's own in-memory dashboard state (loaded
before the edit) being written back to disk during the restart's stop
phase, silently clobbering the file edit. If a `docker restart` alone
doesn't show the change, use the proven-safe sequence instead: `docker
stop opt_homeassistant_1`, apply the edit directly against the HOST path
(`/opt/homeassistant/config/.storage/...`, not `/config/...`, since the
container isn't running to resolve that mount) with `sudo` if a plain
write hits a permission error, then `docker start opt_homeassistant_1`.
"""
import json

LOVELACE_PATH = "/config/.storage/lovelace.dashboard_nimbus"

# Small, consistent palette -- reused across every card. <font color=...>
# only (never inline style=), per this file's own module docstring.
GREEN = "#66bb6a"
AMBER = "#ffb74d"
RED = "#ef5350"
BLUE = "#42a5f5"
MUTED = "#9e9e9e"

HERO_CARD = {
    "type": "markdown",
    "grid_options": {"columns": 12, "rows": 3},
    "content": """{% set fc = states.sensor.nimbus_solver_battery_forecast %}
{% set qr = states.sensor.nimbus_solver_quality_report %}
{% if fc is not none and fc.state not in ('unavailable','unknown') %}
{% set status = fc.attributes.get('status','?') %}
{% set status_color = '""" + GREEN + """' if status == 'optimal' else '""" + RED + """' %}
{% set solve_s = fc.attributes.get('solve_seconds', 0)|float %}
{% set epr = (qr.state|float * 100) if qr is not none and qr.state not in ('unavailable','unknown') else none %}
{% set epr_color = '""" + GREEN + """' if epr is not none and epr >= 80 else ('""" + AMBER + """' if epr is not none and epr >= 60 else '""" + RED + """') %}
## ⚡ Nimbus Solver

<font color="{{ status_color }}" size="5">**{{ status|upper }}**</font>&nbsp;&nbsp;&nbsp; solved in <font color="{{ '""" + GREEN + """' if solve_s < 5 else '""" + AMBER + """' }}" size="5">**{{ '%.2f'|format(solve_s) }}s**</font>&nbsp;&nbsp;&nbsp; EPR <font color="{{ epr_color }}" size="5">**{{ '%.1f'|format(epr) if epr is not none else '—' }}%**</font>

<font color="{{ '""" + MUTED + """' }}">{{ fc.attributes.get('n_periods','?') }} periods · {{ fc.attributes.get('horizon_hours','?') }}h horizon · last solved {{ as_timestamp(fc.attributes.get('generated_at')) | timestamp_custom('%-I:%M:%S %p') if fc.attributes.get('generated_at') else '?' }}</font>
{% else %}
## ⚡ Nimbus Solver
<font color="{{ '""" + MUTED + """' }}">_Not yet available._</font>
{% endif %}
""",
}

PERFORMANCE_CARD = {
    "type": "markdown",
    "title": "⚡ Solve Performance",
    "grid_options": {"columns": 6, "rows": 4},
    "content": """{% set fc = states.sensor.nimbus_solver_battery_forecast %}
{% if fc is not none and fc.state not in ('unavailable','unknown') %}
| | |
|:---|---:|
| Status | **{{ fc.attributes.get('status','?')|capitalize }}** |
| Solve time | **{{ '%.2f'|format(fc.attributes.get('solve_seconds',0)|float) }}s** |
| Periods | {{ fc.attributes.get('n_periods','?') }} |
| Horizon | {{ '%.1f'|format(fc.attributes.get('horizon_hours',0)|float) }}h |
| Clamped periods | {{ fc.attributes.get('n_clamped_periods','?') }} |
| Last solved | {{ as_timestamp(fc.attributes.get('generated_at')) | timestamp_custom('%-I:%M:%S %p') if fc.attributes.get('generated_at') else '?' }} |
{% else %}
<font color=\"""" + MUTED + """\">_Not yet available._</font>
{% endif %}
""",
}

ECONOMICS_CARD = {
    "type": "markdown",
    "title": "💰 Tonight's Plan Economics",
    "grid_options": {"columns": 6, "rows": 4},
    "content": """{% set fc = states.sensor.nimbus_solver_battery_forecast %}
{% if fc is not none and fc.state not in ('unavailable','unknown') %}
| | |
|:---|---:|
| Total cost (plan) | **${{ '%.2f'|format(fc.attributes.get('total_cost',0)|float) }}** |
| Total cost (+ fixed) | **${{ '%.2f'|format(fc.attributes.get('total_cost_with_fixed_costs',0)|float) }}** |
| P2P match fraction | {{ '%.1f'|format(fc.attributes.get('p2p_match_fraction',0)|float * 100) }}% |
| Recent avg P2P volume | {{ '%.2f'|format(fc.attributes.get('p2p_recent_avg_volume_kwh',0)|float) }} kWh |
{% else %}
<font color=\"""" + MUTED + """\">_Not yet available._</font>
{% endif %}
""",
}

QUALITY_CARD = {
    "type": "markdown",
    "title": "🎯 Quality Score (EPR)",
    "grid_options": {"columns": 6, "rows": 5},
    "content": """{% set qr = states.sensor.nimbus_solver_quality_report %}
{% if qr is not none and qr.state not in ('unavailable','unknown') %}
{% set epr = qr.state|float * 100 %}
{% set epr_color = '""" + GREEN + """' if epr >= 80 else ('""" + AMBER + """' if epr >= 60 else '""" + RED + """') %}
_{{ qr.attributes.get('latest_date','?') }}_

| | |
|:---|---:|
| EPR | <font color="{{ epr_color }}">**{{ '%.1f'|format(epr) }}%**</font> |
| Theoretical max yield | **${{ '%.2f'|format(qr.attributes.get('theoretical_maximum_yield',0)|float) }}** |
| Value captured | ${{ '%.2f'|format(qr.attributes.get('value_captured',0)|float) }} |
| Uplift available (regret) | <font color=\"""" + AMBER + """\">${{ '%.2f'|format(qr.attributes.get('uplift_available',0)|float) }}</font> |
| Real P2P earned | ${{ '%.2f'|format(qr.attributes.get('real_p2p_dollars',0)|float) }} / {{ '%.1f'|format(qr.attributes.get('real_p2p_volume_kwh',0)|float) }}kWh |
| Tracking fidelity | {{ '%.0f'|format(qr.attributes.get('tracking_fidelity',0)|float * 100) }}% |
{% else %}
<font color=\"""" + MUTED + """\">_Not yet available._</font>
{% endif %}
""",
}

THRESHOLD_CARD = {
    "type": "markdown",
    "title": "🛡️ Tonight's P2P Volume Threshold",
    "grid_options": {"columns": 6, "rows": 4},
    "content": """{% set th = states.sensor.p2p_nightly_volume_threshold_kwh %}
{% if th is not none and th.state not in ('unavailable','unknown') %}
{% set driver = th.attributes.get('driver','?') %}
{% set driver_color = '""" + BLUE + """' if driver == 'model' else '""" + MUTED + """' %}
| | |
|:---|---:|
| Threshold | **{{ '%.2f'|format(th.state|float) }} kWh** |
| Model prediction | {{ '%.2f'|format(th.attributes.get('model_prediction_kwh')|float) if th.attributes.get('model_prediction_kwh') is not none else '—' }} kWh |
| Historical floor | {{ '%.2f'|format(th.attributes.get('floor_kwh',0)|float) }} kWh |
| Driving factor | <font color="{{ driver_color }}">**{{ driver }}**</font> |
| Training days | {{ th.attributes.get('training_days','?') }} |
{% else %}
<font color=\"""" + MUTED + """\">_Not yet available._</font>
{% endif %}
""",
}

JOB_HEALTH_CARD = {
    "type": "entities",
    "title": "Solver Job Health (both NUCs)",
    "state_color": True,
    "grid_options": {"columns": 6, "rows": 5},
    "entities": [
        {"entity": "sensor.job_health_nuc1_nimbus_solver_forecast_writer", "name": "NUC1 Forecast Writer"},
        {"entity": "sensor.job_health_nuc1_nimbus_solver_quality_writer", "name": "NUC1 Quality Writer"},
        {"entity": "sensor.job_health_nuc1_p2p_nightly_volume_writer", "name": "NUC1 P2P Volume Writer"},
        {"type": "divider"},
        {"entity": "sensor.job_health_nuc2_nimbus_solver_forecast_writer", "name": "NUC2 Forecast Writer"},
        {"entity": "sensor.job_health_nuc2_nimbus_solver_quality_writer", "name": "NUC2 Quality Writer"},
        {"entity": "sensor.job_health_nuc2_p2p_nightly_volume_writer", "name": "NUC2 P2P Volume Writer"},
    ],
}

SOLVER_VIEW = {
    "path": "solver",
    "title": "Solver",
    "type": "sections",
    "max_columns": 12,
    "sections": [
        {"type": "grid", "cards": [HERO_CARD]},
        {
            "type": "grid",
            "cards": [PERFORMANCE_CARD, ECONOMICS_CARD, QUALITY_CARD, THRESHOLD_CARD, JOB_HEALTH_CARD],
        },
    ],
}


def main() -> None:
    with open(LOVELACE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    views = data["data"]["config"]["views"]
    existing_idx = next((i for i, v in enumerate(views) if v.get("path") == "solver"), None)
    if existing_idx is not None:
        views[existing_idx] = SOLVER_VIEW
        print("Replaced existing 'solver' view.")
    else:
        views.append(SOLVER_VIEW)
        print("Added new 'solver' view.")

    with open(LOVELACE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)

    print(f"Total views now: {len(views)}")


if __name__ == "__main__":
    main()
