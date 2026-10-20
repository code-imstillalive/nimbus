"""The standard Solver tab of the Nimbus dashboard (nimbus #1594).

Agreed contents (household, 6 and 8 Oct 2026): what the Solver is doing, the
plan against the measured battery, the money, how well it did, the risk
sliders, and every Solver setting.

Layout, from the household's review on devhub (11 Oct 2026) -- five blocks,
each opened by a full-width coloured title band:

  Performance        four charts: at a glance (rings), score by day, plan
                     cost and its range, where the cost comes from
  Money              four cards of six rows: last day scored, money ahead,
                     where the cost comes from, the battery now
  Plan               the battery chart (three columns) beside the plan's
                     battery energy donut (one); then three slider tiles
                     beside the explanation of what they do
  Basic settings     four cards of eight rows: battery, grid and fees, P2P,
                     reserve and price spikes
  Advanced settings  four cards of eight rows: costs and stability,
                     features, scoring, P2P hours and matching

The rules it follows: cards side by side have the same number of rows of
real content, so their bottoms line up without padding; every card has its
title inside it and its one-line note inside it, under a divider (an
entities card's `section` row), or as the chart's subtitle; sliders are real
sliders. A fee block's hours appear in its card only once its rate is set.

Plain Home Assistant cards (markdown, entities, tile), plus ApexCharts Card
for the charts -- the same card the Forecaster tab already needs. A
household can move, resize or remove any of it.

Entities are written into the template by **unique-id key**, as `@<key>`,
and resolved per install by `resolve()` when the tab is written. An entity_id
is not stable across installs -- Home Assistant suffixes one that collides,
which is exactly what devhub's `_2` copies are -- but the unique id is
`<config entry id>_<key>`. A card whose entity does not exist on this install
is dropped rather than written blank. `@option:<name>` reads the hub's own
option of that name (the Solver's configured battery sensor). Inside text,
`${@key}` is replaced the same way (the risk explanation's live values).

HA-import-free on purpose, like `ml/` and `solver/`: the caller supplies the
lookup.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from typing import Any

try:
    from .power_units import POWER_TO_KW, power_scale_to_kw
except ImportError:  # loaded standalone (tests)
    from power_units import POWER_TO_KW, power_scale_to_kw  # type: ignore[no-redef]

# A card is (title, rows, note). A row is (key, name), or
# ("block", rate_key, start_key, end_key, label): the rate, then its hours
# once the rate is set.
_Card = tuple[str, list[tuple[str, ...]], str]

# --- Money: four cards, six rows each --------------------------------------

_SCORED: _Card = (
    "Last day, scored",
    [
        ("nimbus_quality_epr", "EPR"),
        ("nimbus_quality_regret_dollars", "Regret"),
        ("nimbus_quality_value_captured", "Value captured"),
        ("nimbus_quality_theoretical_maximum_yield", "Best possible"),
        ("nimbus_quality_achieved_energy_in_kwh", "Energy into battery"),
        ("nimbus_quality_achieved_energy_out_kwh", "Energy out of battery"),
    ],
    "EPR = value captured ÷ best possible.",
)

_MONEY: _Card = (
    "Money, plan ahead",
    [
        ("nimbus_solver_total_cost", "Plan cost"),
        ("nimbus_solver_total_cost_with_fixed_costs", "With fixed charges"),
        ("nimbus_solver_cost_band_lower", "Likely best case"),
        ("nimbus_solver_cost_band_upper", "Likely worst case"),
        ("nimbus_solver_current_import_price", "Buy price now"),
        ("nimbus_solver_current_export_price", "Sell price now"),
    ],
    "Negative cost means the plan earns.",
)

_BREAKDOWN: _Card = (
    "Where the cost comes from",
    [
        ("nimbus_solver_cost_breakdown_grid_net", "Grid, net"),
        ("nimbus_solver_cost_breakdown_charge_fee", "Charging"),
        ("nimbus_solver_cost_breakdown_discharge_fee", "Discharging"),
        ("nimbus_solver_cost_breakdown_degradation", "Battery wear"),
        ("nimbus_solver_cost_breakdown_terminal_value_credit", "Value left at end"),
        ("nimbus_solver_total_cost", "Plan cost, total"),
    ],
    "The five parts add up to the total.",
)

_BATTERY_NOW: _Card = (
    "Battery, now",
    [
        ("nimbus_solver_current_dispatch_direction", "Doing now"),
        ("nimbus_solver_current_battery_kw", "Battery power"),
        ("nimbus_solver_current_soc_pct", "State of charge"),
        ("nimbus_solver_equivalent_full_cycles", "Cycles in plan"),
        ("nimbus_solver_lp_status", "Plan status"),
        ("nimbus_solver_solve_seconds", "Solve time"),
    ],
    "What the latest plan does this minute.",
)

# --- Basic settings: four cards, eight rows each ---------------------------

_BATTERY: _Card = (
    "Battery",
    [
        ("solver_battery_capacity_kwh", "Capacity"),
        ("solver_battery_soh_percent", "State of health"),
        ("solver_battery_min_soc_percent", "Minimum SoC"),
        ("solver_battery_max_soc_percent", "Maximum SoC"),
        # Household, 14 Oct 2026: beside Maximum SoC, the two together.
        ("solver_charge_by_hour", "Charge by"),
        ("solver_charge_by_percent", "Charge to"),
        ("solver_max_charge_kw", "Max charge"),
        ("solver_max_discharge_kw", "Max discharge"),
        ("solver_efficiency_percent", "Round-trip efficiency"),
        ("solver_inverter_self_consumption_kw", "Inverter self-use"),
    ],
    "Charge to: SoC to hold by Charge by each day; 0 = off.",
)

_GRID: _Card = (
    "Grid and fees",
    [
        ("solver_grid_max_import_kw", "Max grid import"),
        ("solver_grid_max_export_kw", "Max grid export"),
        ("solver_network_fee_default_rate", "Network fee"),
        (
            "block",
            "solver_network_fee_1_rate",
            "solver_network_fee_1_start_hour",
            "solver_network_fee_1_end_hour",
            "Fee block 1",
        ),
        (
            "block",
            "solver_network_fee_2_rate",
            "solver_network_fee_2_start_hour",
            "solver_network_fee_2_end_hour",
            "Fee block 2",
        ),
        (
            "block",
            "solver_network_fee_3_rate",
            "solver_network_fee_3_start_hour",
            "solver_network_fee_3_end_hour",
            "Fee block 3",
        ),
        ("solver_flat_fee_rate", "Flat fee"),
        ("solver_fixed_daily_charge", "Fixed daily charge"),
        ("section", "Max import and export: your connection's limits."),
        ("section", "Fee blocks: extra per kWh within set hours; 0 = unused."),
    ],
    "Fees are added to the buy price.",
)

_P2P: _Card = (
    "P2P",
    [
        ("solver_p2p_block_1_rate_kw", "Block 1 amount"),
        ("solver_p2p_block_2_rate_kw", "Block 2 amount"),
        ("solver_p2p_block_3_rate_kw", "Block 3 amount"),
        ("solver_p2p_block_lead_time_minutes", "Start lead time"),
        ("solver_p2p_bonus_price", "Bonus price"),
        ("solver_p2p_bonus_volume_kwh", "Bonus volume"),
        ("nimbus_solver_current_bonus_price", "Bonus price now"),
        ("solver_post_window_self_consume_hours", "Self-consume after midnight"),
        ("section", "Amount: the kW sold through each block; 0 = unused."),
        ("section", "Bonus: paid above the sell price on P2P volume."),
    ],
    (
        "Self-consume after midnight: after a block ending at midnight, "
        "hours with no selling. 0 = sell straight away."
    ),
)

_P2P_HOURS: _Card = (
    "P2P hours and matching",
    [
        ("solver_p2p_block_1_start_hour", "Block 1 start"),
        ("solver_p2p_block_1_end_hour", "Block 1 stop"),
        ("solver_p2p_block_2_start_hour", "Block 2 start"),
        ("solver_p2p_block_2_end_hour", "Block 2 stop"),
        ("solver_p2p_block_3_start_hour", "Block 3 start"),
        ("solver_p2p_block_3_end_hour", "Block 3 stop"),
        ("section", "A block ending at midnight shows as 00:00."),
        ("nimbus_solver_p2p_match_fraction", "Matched share (0–1)"),
        ("nimbus_solver_p2p_recent_avg_volume_kwh", "Matched volume"),
        ("section", "Start lead time (P2P card) begins each block early."),
        ("section", "Matched volume: average kWh sold to peers lately."),
    ],
    "Matched: export recently sold to a peer.",
)

_COSTS: _Card = (
    "Costs and stability",
    [
        ("solver_charge_cost", "Charge cost"),
        ("solver_discharge_cost", "Discharge cost"),
        ("solver_degradation_cost_per_kwh", "Battery wear cost"),
        ("solver_salvage_value", "Value left at end"),
        (
            "section",
            "Charge, discharge, wear: cents per kWh; raise to cycle less.",
        ),
        (
            "section",
            (
                "Value left at end: worth of a kWh still stored at the plan's end. "
                "Battery wear: replacement cost per kWh cycled."
            ),
        ),
        ("solver_intraplan_smoothness_weight_kw", "Plan smoothness"),
        ("solver_proximal_weight_kw", "Stick to last plan"),
        ("solver_battery_charge_earliness_budget_kw", "Charge earliness"),
        ("solver_period0_hysteresis", "Direction change margin"),
        ("solver_aemo_p5min_disagreement_threshold_dollars", "AEMO disagreement"),
        ("solver_p5min_false_spike_threshold_mwh", "P5MIN false spike"),
        ("section", "Direction change margin: what a switch must save; 0 = off."),
    ],
    "Ties: 0.005 suits most. AEMO: warn past gap. P5MIN: AUD/MWh.",
)

_FEATURES: _Card = (
    "Features",
    [
        ("solver_load_forecast_source_policy", "Load forecast source"),
        ("solver_auto_include_known_solar", "Auto-include solar"),
        ("solver_calibrated_objective_enabled", "Calibrated objective"),
        ("solver_nowcast_measurement_enabled", "Nowcast measurement"),
        ("solver_offer_curve_enabled", "Offer curve"),
        ("solver_flex_signals_enabled", "Flex signals (slower)"),
        ("solver_score_controllable_loads_enabled", "Score controllable loads"),
        ("solver_dispatch_dry_run", "Own dispatcher: dry run"),
        # One line per setting above, in the same order (household, 11 Oct).
        ("section", "Load forecast source: off = Nimbus's own forecast."),
        ("section", "Auto-include solar: finds solar forecast integrations."),
        ("section", "Calibrated objective: equal plans go to the steadier one."),
        ("section", "Nowcast: records how forecasts track reality."),
        ("section", "Offer curve: publishes how the plan responds to price."),
        (
            "section",
            "Flex signals: publishes flexibility ranges, but each solve is about 9× slower.",
        ),
        ("section", "Score controllable loads: the score may re-time them."),
        (
            "section",
            "Dry run: Nimbus's own dispatcher sends nothing. The Help tab says what each switch costs.",
        ),
    ],
    "Defaults suit most installs.",
)

_SCORING: _Card = (
    "Scoring",
    [
        ("nimbus_quality_soc_discrepancy_mean_pct", "SoC gap, average"),
        ("solver_soc_discrepancy_mean_threshold_pct", "Average limit"),
        ("nimbus_quality_soc_discrepancy_max_pct", "SoC gap, largest"),
        ("solver_soc_discrepancy_max_threshold_pct", "Largest limit"),
        ("nimbus_quality_soc_discrepancy_reliable", "SoC readings reliable"),
        ("nimbus_quality_epr_reliable", "Score reliable"),
        ("nimbus_counterfactual_nimbus_only_soc_close_pct", "Nimbus-only SoC, 5 pm"),
        ("nimbus_counterfactual_real_soc_close_pct", "Real SoC, 5 pm"),
        (
            "section",
            "SoC gap: how far the battery's reported charge and the energy it measured drifted apart yesterday.",
        ),
        ("section", "Limits: raise them only if good days keep going unscored."),
        ("section", "Gaps and limits are in percentage points of charge."),
        ("section", "Readings reliable: the battery's own SoC reports were usable."),
        ("section", "Score reliable: the day had enough data to be scored."),
        (
            "section",
            "Nimbus-only SoC: where the battery would have been at 5 pm had Nimbus alone decided; real SoC is where it was.",
        ),
        (
            "section",
            "The day's score (EPR) is the share of the possible saving the plan captured.",
        ),
    ],
    "A day over either limit is not scored.",
)

_RESERVE: _Card = (
    "Reserve and price spikes",
    [
        ("solver_battery_reserve_percent", "Reserve"),
        ("solver_battery_reserve_release_price", "Release margin"),
        ("solver_battery_reserve_start_hour", "Hold from"),
        ("solver_battery_reserve_end_hour", "Release at"),
        ("solver_export_floor_price", "Don't sell below"),
        ("section", "Reserve: battery share held back between these times; 0 = off."),
        (
            "section",
            "Release margin: edge to sell the reserve. Don't sell below: under it, spare energy fills the battery.",
        ),
        ("section", "Price spikes"),
        ("solver_price_event_enabled", "Price events"),
        ("solver_price_spike_threshold", "Spike threshold"),
        ("solver_price_spike_discharge_kw", "Spike discharge"),
        ("solver_price_spike_override_armed", "Spike override armed"),
        ("section", "Price events: plan around your own price-event sensor."),
        (
            "section",
            "Spike: a buy price above the threshold (0 = off). Armed, the battery sells at the spike discharge power.",
        ),
    ],
    "Insurance against a sudden high price.",
)

# --- Plan: risk sliders ----------------------------------------------------

_SLIDERS = (
    ("solver_import_price_risk_aversion", "Import price risk"),
    ("solver_export_price_risk_aversion", "Export price risk"),
    ("solver_risk_aversion", "Solar forecast risk"),
)

_RISK_TEXT = (
    "**What the sliders do** — 0 trusts the forecast, 1 plans for its worst case.\n\n"
    "| | Leans toward | Effect now |\n"
    "|:---|:---|---:|\n"
    "| Import | buying before prices rise | "
    "{{ states('${@nimbus_solver_import_price_risk_effect_now}') }} |\n"
    "| Export | selling before prices fall | "
    "{{ states('${@nimbus_solver_export_price_risk_effect_now}') }} |\n"
    "| Solar | keeping energy in case solar falls short | "
    "{{ states('${@nimbus_solver_solar_risk_effect_now_kw}') }} kW |\n\n"
    '<font color="#3ddc84">●</font> toward 1: act now  '
    '<font color="#ff5a5a">●</font> toward 0: wait for the forecast'
)

# --- Blocks ----------------------------------------------------------------

# --- Plan: price safety (household, 16 Oct 2026: "why release features which
# are not visible") -- four equal cards under the price chart, one per guard:
# the P5MIN false-spike guard (#1778), the price source's freshness (#1793),
# the dispatch hold signal (#1654) and the consolidated forecast (#1786). Each
# opens with a coloured band: green all well, red a guard acting now, orange
# hold or live, grey not running.
_SAFETY_GUARD = {
    "type": "markdown",
    "content": (
        "{% set g = state_attr('${@p5min_forecast}', 'false_spike_guard') %}"
        "<ha-alert alert-type=\"{% if g is mapping and g.get('active') %}error{% elif g is mapping %}success{% else %}info{% endif %}\" title=\"False-spike guard\">{% if g is mapping and g.get('active') %}Holding a spike out{% elif g is mapping %}Watching{% else %}Not running{% endif %}</ha-alert>\n\n"
        "{% if g is mapping and g.get('active') %}{{ g.get('reason') }}{% if g.get('detail', {}).get('constraint_id') %} · {{ g.detail.constraint_id }}{% endif %}{% if g.get('expires_at') %} · until {{ as_timestamp(g.expires_at) | timestamp_custom('%H:%M') }}{% endif %}{% elif g is mapping %}Forecast spikes over ${{ g.get('threshold') }}/kWh are checked against AEMO.{% else %}Needs an AEMO region.{% endif %}\n\n"
        "*Keeps a forecast spike AEMO cannot deliver out of the plan.*"
    ),
}

_SAFETY_SOURCE = {
    "type": "markdown",
    "content": (
        "{% set f = state_attr('${@nimbus_solver_battery_forecast}', 'price_source_freshness') %}"
        "<ha-alert alert-type=\"{% if f is mapping and f.get('stale') %}error{% elif f is mapping %}success{% else %}info{% endif %}\" title=\"Price source\">{% if f is mapping and f.get('stale') %}Stale, using AEMO{% elif f is mapping %}Fresh{% else %}No refresh time published{% endif %}</ha-alert>\n\n"
        "{% if f is mapping %}Rebuilt {{ [f.get('age_minutes', 0), 0] | max | round(0) | int }} min ago; stale after {{ f.get('stale_after_minutes', 60) | int }}.{% else %}This provider does not say when it last updated.{% endif %}\n\n"
        "*A frozen forecast is replaced by AEMO plus your markup.*"
    ),
}

_SAFETY_HOLD = {
    "type": "markdown",
    "content": (
        "{% set h = states('${@nimbus_solver_current_dispatch_hold}') %}"
        "<ha-alert alert-type=\"{% if h in ['True', 'on'] %}warning{% elif h in ['False', 'off'] %}success{% else %}info{% endif %}\" title=\"Battery hold\">{% if h in ['True', 'on'] %}Hold{% elif h in ['False', 'off'] %}No hold{% else %}Unknown{% endif %}</ha-alert>\n\n"
        "{% if h in ['True', 'on'] %}The plan keeps the battery idle while the grid supplies the house.{% else %}The plan is charging or discharging, or the grid is idle.{% endif %}\n\n"
        "*Hold, not Self-Consume, when the plan leaves the battery idle.*"
    ),
}

_SAFETY_CONSOLIDATION = {
    "type": "markdown",
    "content": (
        "{% set c = states('${@solver_price_consolidation_live}') %}"
        "<ha-alert alert-type=\"{% if c == 'on' %}warning{% else %}info{% endif %}\" title=\"Consolidated prices\">{% if c == 'on' %}Driving the plan{% elif c == 'off' %}Shadow{% else %}Unavailable{% endif %}</ha-alert>\n\n"
        "{% if c == 'on' %}Every price source combined drives the plan.{% else %}Every price source combined and scored; the plan still uses your provider.{% endif %}\n\n"
        "*Each source's running error decides its weight.*"
    ),
}

_PRICE_SAFETY = (_SAFETY_GUARD, _SAFETY_SOURCE, _SAFETY_HOLD, _SAFETY_CONSOLIDATION)

# Household, 19 Oct 2026, with Mark Purcell ("Ask it to show the
# differences between sources"; "it needs to be flexible so when sources
# shrink or grow it has room... and fits snug"): every import price source
# the consolidation reads, how accurate each has been, its weight, and its
# own price for the next slots beside the blend. One row and one column per
# source the install has; a single source says there is nothing to blend.
# No fixed height: the section is as tall as its content.
_CONSOLIDATION_SOURCES = {
    "type": "markdown",
    "content": (
        "{% set pc = state_attr('${@nimbus_solver_battery_forecast}', 'price_consolidation') or {} %}"
        "{% set imp = pc.get('import') or {} %}"
        "{% set names = imp.get('sources') or [] %}"
        "{% set prov = '${@option:solver_import_price_sensor}' %}"
        "{% set prov_name = state_attr(prov, 'friendly_name') or 'Your provider' %}"
        "{% set label = {'primary': prov_name, 'aemo_markup': 'AEMO + markup', 'retail_from_rrp': 'Retail from AEMO RRP', 'secondary_2': 'Second source', 'secondary_3': 'Third source'} %}"
        "{% set spread = imp.get('spread_now') %}"
        "{% if names | length < 2 %}**Price sources**: one source, nothing to blend."
        "{% else %}"
        "**Price sources**{% if spread is not none %}: they differ now by {{ (spread * 100) | round(1) }}¢{% endif %}\n\n"
        "{% set mae = imp.get('mae_by_source_and_lead') or {} %}"
        "{% set w = (imp.get('weights') or {}).get('5-60 min') or {} %}"
        "{% set nx = imp.get('next') or {} %}{% set times = nx.get('times') or [] %}"
        "{% set srcs = nx.get('sources') or {} %}{% set blend = nx.get('consolidated') or [] %}"
        "{% set prim = srcs.get('primary') or [] %}"
        "{% set buckets = ['0-5 min', '5-60 min', '1-4 h', '4-24 h'] %}"
        "| Source | Share | Off 5 min | Off 1 h | Off 4 h | Off 24 h "
        "{% for t in times %}| {{ as_timestamp(t) | timestamp_custom('%H:%M') }} {% endfor %}|\n"
        "|---|---|---|---|---|---{% for t in times %}|---{% endfor %}|\n"
        "{% for n in names %}{% set m = mae.get(n) or {} %}{% set v = srcs.get(n) or [] %}"
        "| {{ label.get(n, n) }} | {{ ((w.get(n) * 100) | round(0) | int ~ '%') if w.get(n) is not none else '—' }} "
        "{% for b in buckets %}| {{ ((m.get(b) * 100) | round(1) ~ '¢') if m.get(b) is not none else '—' }} {% endfor %}"
        "{% for i in range(times | length) %}| {{ ((v[i] * 100) | round(1)) if i < v | length and v[i] is not none else '—' }} {% endfor %}|\n"
        "{% endfor %}"
        "{% if times %}"
        "| **Blend** | | | | | "
        "{% for i in range(times | length) %}| **{{ ((blend[i] * 100) | round(1)) if i < blend | length and blend[i] is not none else '—' }}** {% endfor %}|\n"
        "| <font color='violet'>Gap</font> | | | | | "
        "{% for i in range(times | length) %}{% set b = blend[i] if i < blend | length else none %}{% set p0 = prim[i] if i < prim | length else none %}"
        "| {% if b is not none and p0 is not none %}<font color='violet'>{{ '+' if b > p0 else '' }}{{ ((b - p0) * 100) | round(1) }}</font>{% endif %} {% endfor %}|\n"
        "{% endif %}"
        "\n*Off: how far each source's forecast has been from the price that settled, by how far ahead. Share: how much each counts in the blend. Prices in ¢/kWh.*"
        "{% endif %}"
    ),
}


# Household, 19 Oct 2026: "where will these be toggled on or off from?" --
# the two controls 0.94.467 asks the household to turn on, under the
# price-safety cards: consolidation's live switch, and the device setup's
# mode (DC-004) with what it would do.
_CONSOLIDATION_TOGGLE = {
    "type": "tile",
    "entity": "@solver_price_consolidation_live",
    "name": "Consolidation drives the plan",
    "icon": "mdi:call-merge",
    "features": [{"type": "toggle"}],
    "features_position": "bottom",
}

_DEVICE_SETUP = {
    "type": "markdown",
    "content": (
        "{% set e = '${@device_setup_mode}' %}{% set m = states(e) %}"
        "<ha-alert alert-type=\"{% if m == 'active' %}warning{% elif m == 'shadow' %}info{% else %}success{% endif %}\" title=\"Device setup\">"
        "{% if m == 'active' %}Active: the device setup is in charge"
        "{% elif m == 'shadow' %}Shadow: stored and compared"
        "{% elif m == 'legacy' %}Legacy: your settings, as before"
        "{% else %}Unavailable{% endif %}</ha-alert>\n\n"
        "{% set c = state_attr(e, 'dry_run_clean') %}"
        "Revision {{ state_attr(e, 'revision') or 0 }} · "
        "{{ state_attr(e, 'devices') or 0 }} devices · dry run "
        "{% if c %}clean{% else %}{{ state_attr(e, 'dry_run_changes') or 0 }} change(s), "
        "{{ state_attr(e, 'dry_run_conflicts') or 0 }} conflict(s){% endif %}"
        "{% if (state_attr(e, 'drift_repairs') or 0) > 0 %} · "
        "<b>{{ state_attr(e, 'drift_repairs') }} drift Repair(s) open</b>{% endif %}\n\n"
        "*Shadow changes nothing; active puts the device setup in charge. "
        "Active only from shadow, on a clean dry run.*"
    ),
}

_DEVICE_SETUP_MODE = {
    "type": "tile",
    "entity": "@device_setup_mode",
    "name": "Device setup mode",
    "icon": "mdi:file-tree",
    "features": [{"type": "select-options"}],
    "features_position": "bottom",
}

# --- Stochastic: the two-stage stochastic solve (nimbus #1797) -------------
# Its own block, last on the tab, one row: Now (headline and what each solar
# day costs), the battery by how much sun, and its settings. Every
# card reads sensor.nimbus_solver_stochastic, which this install's own solve
# publishes (sensor_stochastic.py), so a mirrored plan sensor never stands in.
_STOCHASTIC_NOW = {
    "type": "markdown",
    "content": (
        "{% set e = '${@nimbus_solver_stochastic}' %}"
        "{% set s = state_attr(e, 'stochastic') %}"
        "{% set h = states(e) %}"
        "{% macro usd(v) %}{% if v is none %}—{% else %}"
        "${{ '%.2f' | format(v) }}{% endif %}{% endmacro %}"
        # Grid money in words: negative is money earned (exports and P2P beat
        # imports), so "$-96.93" reads "earns $96.93".
        "{% macro net(v) %}{% if v is none %}—{% elif v < 0 %}"
        "earns ${{ '%.2f' | format(-v) }}{% else %}"
        "pays ${{ '%.2f' | format(v) }}{% endif %}{% endmacro %}"
        '<ha-alert alert-type="'
        "{% if s is not mapping %}info"
        "{% elif s.get('drives_dispatch') %}warning"
        "{% elif s.get('status') == 'optimal' %}success"
        "{% else %}info{% endif %}"
        '" title="Now">{{ h }}</ha-alert>\n\n'
        "{% if s is mapping %}"
        '<font color="#9aa0ac">'
        "{{ 'Driving' if s.get('drives_dispatch') else ('Drive on' if s.get('mode') == 'drive' else 'Shadow') }}"
        " · {{ s.get('solar_band') or 'no' }} band"
        "{% if s.get('stochastic_solve_seconds') is not none %}"
        " · {{ '%.1f' | format(s.stochastic_solve_seconds) }} s{% endif %}"
        "{% if s.get('computed_at') %}"
        " · {{ as_timestamp(s.computed_at) | timestamp_custom('%H:%M') }}{% endif %}"
        "</font>\n\n"
        "{% endif %}"
        "{% if s is mapping and s.get('status') == 'optimal' %}"
        # Grid money at the real prices (import cost minus export and P2P
        # income), not the LP objective: that carries penalty and tie-break
        # terms that are not money (household question, 17 Oct 2026).
        "{% set c = s.get('scenario_grid_cost', {}) %}"
        "{% set end = s.get('scenario_terminal_soc_kwh', {}) %}"
        "{% set vals = [c.get('p10'), c.get('p50'), c.get('p90')]"
        " | reject('none') | list %}"
        "{% set hi = vals | max %}{% set lo = vals | min %}"
        # The decision being made now, main plan against stochastic: the
        # headline's numbers. kW, + discharging, - charging.
        "{% set m = s.get('deterministic_period0_battery_kw') %}"
        "{% set k = s.get('stage1_battery_kw') %}"
        "| Battery now | kW |\n|:---|---:|\n"
        "| Main plan | {{ '%+.1f' | format(m) if m is not none else '—' }} |\n"
        "| Stochastic | <b>{{ '%+.1f' | format(k) if k is not none else '—' }}</b> |\n\n"
        "| Solar day | Grid, 2 days | Battery at end |\n|:---|---:|---:|\n"
        "{% for k, label in [('p10', 'Low'), ('p50', 'Middle'), ('p90', 'High')] %}"
        "{% set v = c.get(k) %}"
        "| {{ label }} | <font color=\"{{ '#ff5a5a' if v == hi else"
        " ('#3ddc84' if v == lo else '#e8eaf0') }}\">{{ net(v) }}</font>"
        " | {% if end.get(k) is not none %}{{ '%.0f' | format(end[k]) }} kWh"
        "{% else %}—{% endif %} |\n"
        "{% endfor %}"
        "| <b>Expected</b> | <b>{{ net(s.get('expected_grid_cost')) }}</b> |"
        " spread {{ usd(hi - lo) }} |\n"
        "| Main plan | {{ net(s.get('deterministic_grid_cost')) }} | |\n\n"
        '<font color="#9aa0ac">Grid: imports paid minus exports and P2P earned,'
        " at your prices, over the plan.</font>"
        "{% endif %}"
    ),
}


# The Now card's tables span the card. They sit in ha-markdown's own shadow
# root, so they are styled there (as the until-midnight card's are).
def _stochastic_now_style() -> dict[str, str]:
    return {
        ".": _FILL_NO_SCROLL,
        "ha-markdown$": (
            "table { width: 100%; table-layout: fixed; } th, td { padding: 4px 8px; }"
        ),
    }


def _stochastic_chart() -> dict[str, Any]:
    """The battery on each solar day the stochastic solve planned for, with
    the main plan's own line: where they part is the hedge. Without a
    stochastic plan the rows carry the main plan alone."""
    entity = "@nimbus_solver_stochastic"
    ahead = {"extend_to": False, "show": {"in_header": False}}
    series = [
        {
            "entity": entity,
            "name": name,
            "type": "line",
            "color": colour,
            "stroke_width": width,
            "unit": "%",
            "data_generator": _forecast_at_period_end(field),
            **({"stroke_dash": 5} if field == "main" else {}),
            **ahead,
        }
        for name, field, colour, width in (
            ("Low solar day", "p10", "#ff7043", 2),
            ("Middle", "p50", "#3ddc84", 3),
            ("High solar day", "p90", "#4FC3C7", 2),
            ("Main plan", "main", "#e8eaf0", 2),
        )
    ]
    return _apex(
        "Battery by how much sun",
        "% of battery. Dashed: the main plan.",
        series,
        height=440,
        graph_span="2d",
        span={"start": "hour"},
        now={"show": True, "label": "Now", "color": "#ffffff"},
        yaxis=[
            {
                "min": 0,
                "max": 100,
                "decimals": 0,
                "apex_config": {"tickAmount": 4},
            }
        ],
    )


# The switch, then the four numbers (const.py says what each does), and the
# one-line note every settings card ends with (household, 11 Oct 2026).
# Household, 17 Oct 2026 (#1822): what each setting does, in two notes so the
# card fits its 9-row cell (a note per setting overflowed it).
_DRIVE_NOTE = (
    "Drive the battery: off, it only plans beside the main plan; "
    "on, it runs the battery unless it can't be trusted."
)
_DIALS_NOTE = (
    "Commit window: how far ahead a choice must suit all 3 days. "
    "Day weight w: cloudy, sunny w each; middle 1-2w. Time limit: "
    "past it, the main plan runs. Check every: re-plan gap while off."
)


def _stochastic_settings() -> dict[str, Any]:
    return {
        "type": "entities",
        "title": "Settings",
        "show_header_toggle": False,
        "entities": _rows(
            [
                ("solver_stochastic_dispatch", "Drive the battery"),
                ("solver_stochastic_stage1_minutes", "Commit window"),
                ("solver_stochastic_tail_weight", "Low/high day weight"),
                ("solver_stochastic_drive_budget_seconds", "Drive time limit"),
                ("solver_stochastic_shadow_interval_minutes", "Check every"),
                ("section", _DRIVE_NOTE),
                ("section", _DIALS_NOTE),
            ]
        ),
    }


_STOCHASTIC_ROW_SIZE = {"rows": 9}

# (name, ha-alert type, one line). The alert type gives each band its colour.
_BANNERS = (
    ("Performance", "info", "How it did, and what the plan ahead should earn."),
    ("Money", "success", "The figures behind the charts."),
    ("Plan", "warning", "What the Solver intends to do with the battery."),
    ("Basic settings", "info", "Your battery, grid, fees and P2P. Set these first."),
    ("Advanced settings", "error", "Fine-tuning. The defaults suit most installs."),
    (
        "More settings",
        "info",
        "Household mode, re-planning, charge taper, solar check.",
    ),
    (
        "Stochastic",
        "success",
        "Plans for a low, middle and high solar day at once (nimbus #1797).",
    ),
)

# --- More settings (household, 18 Oct 2026): every Nimbus control and reading
# the reference household's own dashboard used that this tab did not show, so
# every install has them, not one household.

_HOUSEHOLD: _Card = (
    "Household and re-planning",
    [
        ("household_mode", "Household mode"),
        ("pricing_source", "Pricing source"),
        ("solve_on_price_change", "Re-plan on price change"),
        ("solve_on_price_change_debounce_s", "Re-plan delay"),
        ("solver_price_response_latency", "Price to plan time"),
        ("section", "Household mode: home, away, guests or economy presets."),
        ("section", "Pricing source: switches both prices to one provider."),
    ],
    "Re-plan on price change: solve when a price sensor updates.",
)

_TAPER: _Card = (
    "Charging near full",
    [
        ("solver_charge_taper_start_percent", "Taper starts at"),
        ("solver_charge_power_at_full_percent", "Power at full"),
        ("section", "From Taper starts at, the charge limit falls in a line."),
        ("section", "Power at full: % of max charge left at 100% SoC."),
        ("section", "Taper starts at 100 = off: full power to Max SoC."),
        (
            "section",
            "Measured on a 40 kW battery: about 24 kW at 92-96% SoC, 6 kW at 99.5%.",
        ),
    ],
    "A battery cannot take full charge power near full.",
)

_SOLAR_LOW: _Card = (
    "Solar running low",
    [
        ("solver_solar_running_low_min_p50_kw", "Judge above"),
        ("solver_solar_running_low_on_percent", "On below"),
        ("solver_solar_running_low_off_percent", "Off above"),
        ("solver_solar_running_low_confirm_minutes", "Confirm for"),
        ("section", "Measured solar under On below % of forecast p50: plan on p10."),
        ("section", "Confirm for: minutes it must stay low first; 0 = at once."),
    ],
    "Judge above: kW of p50 needed to judge at all; 0 = off.",
)

_DIAGNOSTICS: _Card = (
    "Diagnostics",
    [
        ("solver_config", "Solver config"),
        ("nimbus_solver_discharge_efficiency", "Discharge efficiency"),
        ("nimbus_solver_dispatch_dry_run", "Own dispatcher would set"),
        ("nimbus_counterfactual_soc", "Counterfactual SoC"),
        ("section", "Solver config: configured once the wizard is complete."),
        ("section", "Own dispatcher: what it would command, in dry run."),
        ("section", "Counterfactual SoC: yesterday's real closing SoC."),
        ("section", "Discharge efficiency: one leg of the round trip."),
    ],
    "Readings only, for checking the plan.",
)

_MONEY_ROW = (_SCORED, _MONEY, _BREAKDOWN, _BATTERY_NOW)
_MORE_ROW = (_HOUSEHOLD, _TAPER, _SOLAR_LOW, _DIAGNOSTICS)
_BASIC_ROW = (_BATTERY, _GRID, _P2P, _P2P_HOURS)
_ADVANCED_ROW = (_COSTS, _FEATURES, _SCORING, _RESERVE)

# A block's rate reads "0.0" (or "0") while unused.
_UNUSED = ("0", "0.0")

_MUTED = "#9aa0ac"


def _row(key: str, name: str) -> dict[str, Any]:
    return {"entity": f"@{key}", "name": name}


def _rows(rows: list[tuple[str, ...]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if row[0] == "section":
            # A divider with a line of explanation, inside the card.
            out.append({"type": "section", "label": row[1]})
            continue
        if row[0] != "block":
            out.append(_row(*row))
            continue
        _, rate, start, end, label = row
        out.append(_row(rate, f"{label} rate"))
        for key, part in ((start, "from"), (end, "until")):
            out.append(
                {
                    "type": "conditional",
                    "conditions": [
                        {"entity": f"@{rate}", "state_not": s} for s in _UNUSED
                    ],
                    "row": _row(key, f"{label} {part}"),
                }
            )
    return out


def _section(
    cards: list[dict[str, Any]], span: int, size: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One column of the layout. `size` locks the cards' height, so charts
    side by side end on the same line."""
    grid = {"columns": "full", **(size or {})}
    return {
        "type": "grid",
        "column_span": span,
        "cards": [{**c, "grid_options": grid} for c in cards],
    }


def _banner(name: str, kind: str, line: str) -> dict[str, Any]:
    """A full-width band opening a block: a card (so it has the cards'
    outline) holding a coloured alert titled with the block's name. A
    heading inside the alert does not survive HA's markdown: the band came
    out empty with the name below it (household screenshot, 11 Oct 2026)."""
    return _section(
        [
            {
                "type": "markdown",
                "content": (
                    f'<ha-alert alert-type="{kind}" title="{name}">{line}</ha-alert>'
                ),
            }
        ],
        4,
    )


def _card(card: _Card) -> dict[str, Any]:
    """A titled entities card; its note sits inside, under a divider."""
    title, rows, note = card
    return {
        "type": "entities",
        "title": title,
        # HA adds a header toggle to a card with several switches; one tap
        # would flip them all (household screenshot, 11 Oct 2026).
        "show_header_toggle": False,
        "entities": [*_rows(rows), {"type": "section", "label": note}],
    }


def _apex(
    title: str, note: str, series: list[dict[str, Any]], height: int = 280, **extra: Any
) -> dict[str, Any]:
    """An ApexCharts Card with its note as the chart's subtitle."""
    apex_config = {
        "chart": {"height": height},
        "legend": {"show": True, "position": "bottom"},
        "subtitle": {
            "text": note,
            "align": "left",
            "style": {"color": _MUTED, "fontSize": "12px", "fontWeight": 400},
        },
        **extra.pop("apex_config", {}),
    }
    return {
        "type": "custom:apexcharts-card",
        "header": {
            "show": True,
            "title": title,
            "show_states": True,
            "colorize_states": True,
        },
        "update_interval": "1min",
        "apex_config": apex_config,
        "series": series,
        **extra,
    }


# A series on a data_generator takes its unit from the plan sensor (kW)
# unless given its own, so "SoC ahead" read "7 kW" (11 Oct 2026).
#
# The measured battery series is brought to kW with power_units.py's table
# (nimbus #1570), read against the sensor's live unit: history rows may carry
# no attributes. Case matters only for mW against MW.
_TO_KW = (
    "const s = hass.states['${@option:battery_sensor}']; "
    "const u = String((s && s.attributes && s.attributes.unit_of_measurement) || '').trim(); "
    f"const exact = {json.dumps({u: power_scale_to_kw(u) for u in ('mW', 'MW')})}; "
    f"const t = {json.dumps(POWER_TO_KW)}; "
    "const k = u in exact ? exact[u] : (u.toLowerCase() in t ? t[u.toLowerCase()] : 1); "
    "return x * k;"
)


def _forecast(field: str, scale: float = 1.0) -> str:
    """A data_generator reading one field of the published plan."""
    return (
        "return (entity.attributes.forecast || []).map(p => "
        f"[new Date(p.time).getTime(), p.{field} * {scale}]);"
    )


def _forecast_at_period_end(field: str) -> str:
    """Same, drawn at each row's END: `soc_pct` is the SoC after the period,
    so at the row's start a sale's SoC drop shows a period before its bar
    (household, 12 Oct 2026)."""
    return (
        "return (entity.attributes.forecast || []).map(p => "
        "[new Date(p.time).getTime() + (p.hours || 0) * 3600000, "
        f"p.{field}]);"
    )


# --- Performance charts ----------------------------------------------------


def _glance_chart() -> dict[str, Any]:
    ring = {"min": 0, "max": 100}
    return _apex(
        "At a glance",
        "Each ring is out of 100.",
        [
            {
                "entity": "@nimbus_quality_epr",
                "name": "EPR, last day",
                "color": "#4FC3C7",
                **ring,
            },
            {
                "entity": "@nimbus_solver_current_soc_pct",
                "name": "Battery now",
                "color": "#3ddc84",
                **ring,
            },
            {
                "entity": "@nimbus_solver_p2p_match_fraction",
                "name": "P2P matched",
                "color": "#ff4d8d",
                "transform": "return x * 100;",
                **ring,
            },
        ],
        chart_type="radialBar",
        apex_config={
            "legend": {"show": True, "position": "right"},
            "plotOptions": {
                "radialBar": {
                    "hollow": {"size": "22%"},
                    "track": {"margin": 4},
                    "dataLabels": {
                        "name": {"fontSize": "14px"},
                        "value": {"fontSize": "20px"},
                    },
                }
            },
        },
    )


def _history(field: str, scale: float = 1.0) -> str:
    """A data_generator reading the quality report's dated history: each
    scored day at its own date. The EPR/regret sensors change the morning
    AFTER the day they score, so charting their history put every day a day
    late (audit, 11 Oct 2026)."""
    return (
        "const r = hass.states['${@nimbus_solver_quality_report}']; "
        "const h = (r && r.attributes.history) || {}; "
        "return Object.keys(h).sort().map(d => "
        f"[new Date(d + 'T12:00:00').getTime(), h[d].{field} * {scale}]);"
    )


def _zero_line(axis_index: int = 0) -> dict[str, Any]:
    """A solid line at 0 on axis `axis_index`: every chart has its zero as a
    horizon (household, 12 Oct 2026)."""
    return {
        "annotations": {
            "yaxis": [
                {
                    "y": 0,
                    "yAxisIndex": axis_index,
                    "borderColor": "#c8ccd4",
                    "borderWidth": 1.5,
                    "strokeDashArray": 0,
                }
            ]
        }
    }


# Hide an axis's labels below zero: the SoC axis runs -100..100 so its 0
# sits on the kW axis's zero, but a negative SoC means nothing.
_NO_NEGATIVE_LABELS = "EVAL:function (v) { return v < 0 ? '' : Math.round(v); }"


def _mirror(field: str, scale: float = 1.0) -> str:
    """The plan's `field` and its negation at every point: an invisible
    series that makes its axis symmetric about 0, so that axis's zero lines
    up with the kW axis's (household, 12 Oct 2026)."""
    return (
        "return (entity.attributes.forecast || []).flatMap(p => { "
        "const t = new Date(p.time).getTime(); "
        f"const v = Math.abs((p.{field} || 0) * {scale}); "
        "return [[t, v], [t, -v]]; });"
    )


def _score_chart() -> dict[str, Any]:
    return _apex(
        "Score by day",
        "Each settled day: EPR (line) and regret in dollars (bars).",
        [
            {
                "entity": "@nimbus_quality_regret_dollars",
                "name": "Regret",
                "type": "column",
                "color": "#E8615F",
                "yaxis_id": "aud",
                "data_generator": _history("regret_dollars"),
            },
            {
                "entity": "@nimbus_quality_epr",
                "name": "EPR",
                "type": "line",
                "color": "#4FC3C7",
                "stroke_width": 3,
                "yaxis_id": "pct",
                "data_generator": _history("epr", 100.0),
            },
        ],
        graph_span="14d",
        span={"end": "day"},
        yaxis=[
            {"id": "pct", "min": 0, "max": 100, "decimals": 0, "opposite": True},
            {"id": "aud", "min": 0, "decimals": 0},
        ],
        apex_config=_zero_line(1),
    )


# Household, 20 Oct 2026: two estimates of -$1,466 flattened a week of -$30 to
# -$55 into one line along the top. Each point is clipped to the current plan
# cost plus or minus three times its size (at least $50), and the note says
# so; the live figures in the header stay unclipped.
_COST_CLIP = (
    "const c = parseFloat((hass.states['${@nimbus_solver_total_cost}'] || {}).state); "
    "if (isNaN(c)) return x; "
    "const r = Math.max(50, 3 * Math.abs(c)); "
    "return Math.min(Math.max(x, c - r), c + r);"
)


def _cost_chart() -> dict[str, Any]:
    # Household, 20 Oct 2026: "say plan ahead but it ends on NOW on the right
    # hand side... so it is a recorder of the past". It is: each point is a
    # forward estimate, plotted when it was made.
    hour = {
        "group_by": {"func": "last", "duration": "1h"},
        "curve": "stepline",
        "transform": _COST_CLIP,
    }
    return _apex(
        "How the plan's 4-day estimate has moved",
        "Each point is what the plan then expected the next four days to cost, "
        "plotted when it was made: the right-hand end is now, the rest are past "
        "estimates, not money spent. Below zero earns. Estimates beyond three "
        "times today's figure (at least $50) are drawn at the edge, so a rare "
        "spike does not flatten the week.",
        [
            {
                "entity": "@nimbus_solver_cost_band_upper",
                "name": "Worst case",
                "type": "area",
                "color": "#E8615F",
                "stroke_width": 0,
                **hour,
            },
            {
                "entity": "@nimbus_solver_cost_band_lower",
                "name": "Best case",
                "type": "area",
                "color": "#3ddc84",
                "stroke_width": 0,
                **hour,
            },
            {
                "entity": "@nimbus_solver_total_cost",
                "name": "Plan cost",
                "type": "line",
                "color": "#e8eaf0",
                "stroke_width": 2.5,
                **hour,
            },
        ],
        graph_span="7d",
        yaxis=[{"decimals": 0}],
        apex_config={"fill": {"opacity": [0.25, 0.25, 1]}, **_zero_line()},
    )


def _breakdown_chart() -> dict[str, Any]:
    """The CURRENT plan's cost, split into its parts, one labelled column
    each, then their total. The breakdown sensors are whole-plan figures
    (about four days ahead), re-estimated every solve -- NOT costs per hour.
    An earlier version charted their snapshots over 24 hours as if they were,
    and every figure on it misled (household, 11 Oct 2026). So: one moment,
    the latest, and the parts add up to the plan cost beside them."""
    # One column per part from its current value, placed mid-window. A
    # group_by over the current clock hour left the window empty (household
    # screenshot, 11 Oct 2026).
    now = {
        "type": "column",
        "data_generator": (
            # No current plan (a failed or stale solve leaves these
            # unavailable): no column, so the card reads N/A like the others
            # rather than NaN (a tester's install, 11 Oct 2026).
            "const v = parseFloat(entity.state); "
            "return isNaN(v) ? [] : [[Date.now() - 30 * 60 * 1000, v]];"
        ),
    }
    parts = (
        ("nimbus_solver_cost_breakdown_grid_net", "Grid, net", "#4fa3ff"),
        ("nimbus_solver_cost_breakdown_charge_fee", "Charging", "#ffb340"),
        ("nimbus_solver_cost_breakdown_discharge_fee", "Discharging", "#ff4d8d"),
        ("nimbus_solver_cost_breakdown_degradation", "Battery wear", "#E8615F"),
        ("nimbus_solver_cost_breakdown_terminal_value_credit", "Value left", "#3ddc84"),
        ("nimbus_solver_total_cost", "Plan cost", "#e8eaf0"),
    )
    return _apex(
        "Where the plan's cost comes from",
        "The whole plan ahead, about four days. The five parts add up to "
        "the plan cost. Above zero costs, below zero earns.",
        [{"entity": f"@{k}", "name": n, "color": c, **now} for k, n, c in parts],
        graph_span="1h",
        yaxis=[{"decimals": 0}],
        apex_config={
            **_zero_line(),
            "xaxis": {"labels": {"show": False}},
            "plotOptions": {"bar": {"columnWidth": "85%"}},
            "dataLabels": {
                "enabled": True,
                "style": {"fontSize": "12px"},
                "formatter": "EVAL:function(v){return v === null ? '' : '$' + v.toFixed(2);}",
            },
        },
    )


# --- Plan charts -----------------------------------------------------------


# Household, 16 Oct 2026: on a site with more than one battery the fleet SoC
# (every battery's kWh over all their capacity) matched no meter on the site,
# so there each battery gets its own line instead. Slot 1 exists only there
# (sensor_battery_soc.py), which is what selects one picture or the other.
_MULTI_BATTERY = "@nimbus_solver_battery_1_soc_pct"
_BATTERY_SOC_COLOURS = ("#3ddc84", "#ffd54f", "#ba68c8", "#4dd0e1")


def _battery_soc_series(
    past: dict[str, Any], ahead: dict[str, Any]
) -> list[dict[str, Any]]:
    """Measured-side and ahead SoC per battery slot. Unresolved on a
    single-battery install, so resolve() drops every one of them there."""
    series: list[dict[str, Any]] = []
    for slot, colour in enumerate(_BATTERY_SOC_COLOURS):
        key = f"nimbus_solver_battery_{slot}_soc_pct"
        entity = f"@{key}"
        # The battery's own name ("SoC Our EV"), from this install.
        name = "${@name:" + key + "}"
        series.append(
            {
                "entity": entity,
                "name": name,
                "type": "line",
                "color": colour,
                "stroke_width": 2,
                "yaxis_id": "soc",
                **past,
            }
        )
        series.append(
            {
                "entity": entity,
                "name": name,
                "type": "line",
                "color": colour,
                "stroke_width": 2,
                "stroke_dash": 4,
                "yaxis_id": "soc",
                "unit": "%",
                "data_generator": _forecast_at_period_end("soc_pct"),
                **ahead,
                "show": {**ahead.get("show", {}), "in_legend": False},
            }
        )
    return series


def _chart() -> dict[str, Any]:
    """Past and plan on one axis, a Now line, step lines like the household's
    own "Nimbus vs Real" chart. Price is 5-minute detail behind Now (the buy
    price's own history) and the plan's own steps ahead of it."""
    plan = "@nimbus_solver_battery_forecast"
    past = {"extend_to": False, "group_by": {"func": "last", "duration": "5min"}}
    ahead = {"extend_to": False, "show": {"in_header": False}}
    return _apex(
        "Battery: plan against measured",
        "kW, up = discharging. White: measured. Teal: what each plan said "
        "to do at that moment (its first minute only); dashed: the plan "
        "ahead. Orange: the charge each plan held for later, before the next "
        "P2P block (% of battery); high and never worked off means the plan "
        "keeps putting it off. Green: state of charge; with more than one "
        "battery, one line each (the whole fleet's figure stays on Solver "
        "Current SoC). Red: buy price.",
        [
            {
                "entity": "@option:battery_sensor",
                "name": "Measured",
                "curve": "stepline",
                "color": "#e8eaf0",
                "stroke_width": 2,
                "yaxis_id": "kw",
                # A battery sensor in W (a tester's, 11 Oct 2026) was plotted
                # as kW and flattened the plan to nothing.
                "unit": "kW",
                "transform": _TO_KW,
                **past,
            },
            {
                "entity": plan,
                "name": "Plan now",
                "curve": "stepline",
                "color": "#4FC3C7",
                "stroke_width": 1.5,
                "yaxis_id": "kw",
                **past,
            },
            {
                "entity": plan,
                "attribute": "charge_before_block_pct",
                "name": "Charge held for later",
                "curve": "stepline",
                "color": "#ffa726",
                "stroke_width": 2,
                "yaxis_id": "soc",
                "unit": "%",
                **past,
            },
            {
                "entity": plan,
                "name": "Plan ahead",
                "curve": "stepline",
                "color": "#4FC3C7",
                "stroke_width": 4.5,
                "stroke_dash": 5,
                "yaxis_id": "kw",
                "data_generator": _forecast("battery_kw"),
                **ahead,
            },
            {
                "entity": "@nimbus_solver_current_soc_pct",
                "name": "SoC",
                "type": "area",
                "color": "#3ddc84",
                "stroke_width": 1.5,
                "yaxis_id": "soc",
                "nimbus_unless": _MULTI_BATTERY,
                **past,
            },
            {
                "entity": plan,
                "name": "SoC ahead",
                "type": "area",
                "color": "#3ddc84",
                "stroke_width": 1.5,
                "stroke_dash": 4,
                "yaxis_id": "soc",
                "unit": "%",
                "data_generator": _forecast_at_period_end("soc_pct"),
                "nimbus_unless": _MULTI_BATTERY,
                **ahead,
            },
            *_battery_soc_series(past, ahead),
            {
                "entity": "@nimbus_solver_current_import_price",
                "name": "Buy price",
                "curve": "stepline",
                "color": "#ff3b5c",
                "stroke_width": 3,
                "yaxis_id": "price",
                "unit": "c/kWh",
                "transform": "return x * 100;",
                **past,
            },
            {
                "entity": plan,
                "name": "Buy price ahead",
                "curve": "stepline",
                "color": "#ff3b5c",
                "stroke_width": 3,
                "stroke_dash": 3,
                "yaxis_id": "price",
                "unit": "c/kWh",
                "data_generator": _forecast("import_price", 100.0),
                **ahead,
            },
            # Invisible: they make the kW and price axes symmetric about 0,
            # so all three axes share one zero line.
            *(
                {
                    "entity": plan,
                    "name": name,
                    "curve": "stepline",
                    "stroke_width": 0,
                    "opacity": 0,
                    "yaxis_id": axis,
                    "data_generator": _mirror(field, scale),
                    "show": {"in_legend": False, "in_header": False},
                    "extend_to": False,
                }
                for name, axis, field, scale in (
                    ("kW scale", "kw", "battery_kw", 1.0),
                    ("Price scale", "price", "import_price", 100.0),
                )
            ),
        ],
        height=360,
        graph_span="36h",
        span={"start": "hour", "offset": "-12h"},
        now={"show": True, "label": "Now", "color": "#ffffff"},
        yaxis=[
            {"id": "kw", "decimals": 0, "apex_config": {"title": {"text": "kW"}}},
            # Shown and labelled: hidden, a full battery sat on the kW axis's
            # top line and read as 40 kW (household, 12 Oct 2026).
            # -100..100 puts 0% on the kW axis's zero; negatives unlabelled.
            {
                "id": "soc",
                "min": -100,
                "max": 100,
                "opposite": True,
                "decimals": 0,
                "apex_config": {
                    "title": {"text": "SoC %"},
                    "labels": {"formatter": _NO_NEGATIVE_LABELS},
                },
            },
            {
                "id": "price",
                "opposite": True,
                "decimals": 0,
                "apex_config": {"title": {"text": "c/kWh"}},
            },
        ],
        apex_config={
            "fill": {"opacity": [1, 1, 1, 0.15, 0.08, 1, 1, 0, 0]},
            **_zero_line(0),
        },
    )


# --- Prices ahead (nimbus #1753) ------------------------------------------
#
# The plan prices its whole horizon (four days here), but the provider's own
# forecast covers only part of it (LocalVolts: 24 h); past that the price is
# Nimbus's estimate, wholesale forecast plus the markup learned from the
# provider's history. The plan publishes where each side's provider forecast
# ends (`*_price_provider_until`), so the chart draws the provider's part
# solid and the estimate dashed (household, 13 Oct 2026: "the user is
# blindsided").


def _price_part(field: str, until: str, part: str) -> str:
    """A data_generator for one price, either the periods the provider's
    forecast covers ("provider") or the rest ("estimate"). The provider part
    carries one extra point so its last step is drawn solid to its end."""
    pick = (
        "i < 0 ? [] : rows.slice(0, i + 2)"
        if part == "provider"
        else "rows.slice(i + 1)"
    )
    return (
        f"const u = entity.attributes.{until}; "
        "const cut = u ? new Date(u).getTime() : -Infinity; "
        "const rows = (entity.attributes.forecast || []).map(p => "
        f"[new Date(p.time).getTime(), p.{field} * 100]); "
        "let i = -1; rows.forEach((r, k) => { if (r[0] <= cut) i = k; }); "
        f"return {pick};"
    )


def _prices_chart() -> dict[str, Any]:
    plan = "@nimbus_solver_battery_forecast"
    hidden = {"extend_to": False, "show": {"in_header": False}}
    series = []
    for name, field, until, color in (
        ("Buy", "import_price", "import_price_provider_until", "#ff3b5c"),
        ("Sell", "export_price", "export_price_provider_until", "#3d9df3"),
    ):
        for part, label, dash in (
            ("provider", "forecast", 0),
            ("estimate", "Nimbus estimate", 4),
        ):
            series.append(
                {
                    "entity": plan,
                    "name": f"{name}, {label}",
                    "curve": "stepline",
                    "color": color,
                    "stroke_width": 2.5,
                    "stroke_dash": dash,
                    "unit": "c/kWh",
                    "data_generator": _price_part(field, until, part),
                    **hidden,
                }
            )
    return _apex(
        "Prices ahead",
        "c/kWh, what the plan pays and earns, fees included. Solid: your price "
        "provider's own forecast. Dashed: Nimbus's estimate past it, the "
        "wholesale forecast plus the markup learned from your price history.",
        series,
        height=300,
        graph_span="4d",
        span={"start": "hour"},
        now={"show": True, "label": "Now", "color": "#ffffff"},
        yaxis=[{"decimals": 0, "apex_config": {"title": {"text": "c/kWh"}}}],
        apex_config={**_zero_line(0)},
    )


# One card per day of the plan; time-weighted by each period's hours. The
# day's heading is inside the card, as the household's design rules ask.
_PRICE_DAY = """{% set plan = '${@nimbus_solver_battery_forecast}' %}
{% set fc = state_attr(plan, 'forecast') or [] %}
{% set d = (now() + timedelta(days=__K__)).strftime('%Y-%m-%d') %}
{% set ns = namespace(h=0, b=0, s=0, bmin=none, bmax=none, smin=none, smax=none, eh=0, eb=0, es=0, bt='', st='', first='', last='') %}
{% for p in fc %}{% set ts = as_timestamp(p.time) %}{% if ts | timestamp_custom('%Y-%m-%d') == d %}
{% set h = p.hours | float(0) %}{% set b = p.import_price * 100 %}{% set s = p.export_price * 100 %}
{% if ns.first == '' %}{% set ns.first = ts | timestamp_custom('%H:%M') %}{% endif %}
{% set ns.last = (ts + h * 3600) | timestamp_custom('%H:%M') %}
{% set ns.h = ns.h + h %}{% set ns.b = ns.b + b * h %}{% set ns.s = ns.s + s * h %}
{% if ns.bmin is none or b < ns.bmin %}{% set ns.bmin = b %}{% set ns.bt = ts | timestamp_custom('%H:%M') %}{% endif %}
{% if ns.bmax is none or b > ns.bmax %}{% set ns.bmax = b %}{% endif %}
{% if ns.smin is none or s < ns.smin %}{% set ns.smin = s %}{% endif %}
{% if ns.smax is none or s > ns.smax %}{% set ns.smax = s %}{% set ns.st = ts | timestamp_custom('%H:%M') %}{% endif %}
{% set hr = ts | timestamp_custom('%H') | int %}{% if 17 <= hr < 21 %}{% set ns.eh = ns.eh + h %}{% set ns.eb = ns.eb + b * h %}{% set ns.es = ns.es + s * h %}{% endif %}
{% endif %}{% endfor %}
{% set day = as_timestamp(d ~ 'T12:00:00') %}
#### {{ ['Today', 'Tomorrow'][__K__] if __K__ < 2 else day | timestamp_custom('%A') }} · {{ day | timestamp_custom('%a %d %b') }}
{% if ns.h > 0 %}
| | <font color='#ff3b5c'>Buy ¢</font> | <font color='#3d9df3'>Sell ¢</font> |
|:---|---:|---:|
| Lowest | {{ '%.1f' % ns.bmin }} | {{ '%.1f' % ns.smin }} |
| Average | {{ '%.1f' % (ns.b / ns.h) }} | {{ '%.1f' % (ns.s / ns.h) }} |
| Highest | {{ '%.1f' % ns.bmax }} | {{ '%.1f' % ns.smax }} |
| Evening 17–21 | {{ ('%.1f' % (ns.eb / ns.eh)) if ns.eh > 0 else '–' }} | {{ ('%.1f' % (ns.es / ns.eh)) if ns.eh > 0 else '–' }} |

Cheapest buy **{{ ns.bt }}** · best sell **{{ ns.st }}**{{ (' · plan ' ~ ns.first ~ '–' ~ ns.last) if ns.first != '00:00' or ns.last != '00:00' else '' }}
{% set u = state_attr(plan, 'import_price_provider_until') %}{% set ud = (as_timestamp(u) | timestamp_custom('%Y-%m-%d')) if u else '' %}
<font color='#9aa0ac'>{% if ud > d %}From your price provider's forecast.{% elif ud == d %}Provider's forecast until {{ as_timestamp(u) | timestamp_custom('%H:%M') }}, Nimbus's estimate after.{% else %}Nimbus's estimate: wholesale forecast plus your learned markup.{% endif %}</font>
{% else %}
*Not in the plan yet.*
{% endif %}"""


def _price_day(k: int) -> dict[str, Any]:
    return {"type": "markdown", "content": _PRICE_DAY.replace("__K__", str(k))}


# Yesterday (measured) and tomorrow (planned), midnight to midnight, side by
# side (household, 11 Oct 2026). Measured: the daily meters set in Nimbus ->
# Configure -> Topology, each meter's `last_period` (yesterday's total, kept by
# a utility meter); a meter that keeps no yesterday shows a dash rather than a
# wrong figure. Planned: tomorrow's 00:00-24:00 slice of the plan. An earlier
# donut summed the whole four-day plan and its labels read like solar and
# house totals -- 483 kWh "grid in" -- and was withdrawn.
_ENERGY_ROWS = (
    ("Solar", "switchboard_solar_energy_daily_sensor", "solar"),
    ("House", "switchboard_house_load_energy_daily_sensor", "house"),
    ("Grid in", "switchboard_import_energy_daily_sensor", "grid_in"),
    ("Grid out", "switchboard_export_energy_daily_sensor", "grid_out"),
    ("Battery in", "switchboard_battery_charge_daily_sensor", "batt_in"),
    ("Battery out", "switchboard_battery_discharge_daily_sensor", "batt_out"),
)

_ENERGY_PLAN_SUMS = (
    "{% set f = state_attr('${@nimbus_solver_battery_forecast}', 'forecast') or [] %}"
    "{% set t0 = today_at('00:00') + timedelta(days=1) %}"
    "{% set t1 = t0 + timedelta(days=1) %}"
    "{% set ns = namespace(solar=0, house=0, grid_in=0, grid_out=0, batt_in=0, batt_out=0) %}"
    "{% for p in f %}{% set t = as_datetime(p.time) %}"
    "{% if t is not none and t0 <= t < t1 %}{% set h = p.hours | float(0) %}"
    "{% set b = p.battery_kw | float(0) %}"
    "{% set ns.solar = ns.solar + (p.solar_kw | float(0)) * h %}"
    "{% set ns.house = ns.house + (p.load_kw | float(0)) * h %}"
    "{% set ns.grid_in = ns.grid_in + (p.grid_import_kw | float(0)) * h %}"
    "{% set ns.grid_out = ns.grid_out + (p.grid_export_kw | float(0)) * h %}"
    "{% set ns.batt_in = ns.batt_in + ([0, -b] | max) * h %}"
    "{% set ns.batt_out = ns.batt_out + ([0, b] | max) * h %}"
    "{% endif %}{% endfor %}"
)


def _energy_table() -> dict[str, Any]:
    rows = []
    for label, option, key in _ENERGY_ROWS:
        measured = (
            f"{{% set y = state_attr('${{@option:{option}}}', 'last_period') %}}"
            "{{ '%.1f' | format(y | float) if y is not none and y | is_number else '—' }}"
        )
        planned = f"{{{{ '%.1f' | format(ns.{key}) if f else '—' }}}}"
        rows.append(f"| {label} | {measured} | {planned} |")
    content = (
        _ENERGY_PLAN_SUMS
        + "**Yesterday and tomorrow, kWh**\n\n"
        + "| | Yesterday, measured | Tomorrow, planned |\n"
        + "|:---|---:|---:|\n"
        + "\n".join(rows)
        + "\n\nMidnight to midnight. Measured: the daily meters in Nimbus → "
        "Configure → Topology; a dash means that meter keeps no yesterday."
    )
    return {"type": "markdown", "content": content}


def _energy_pie() -> dict[str, Any]:
    """Tomorrow's battery energy, 00:00-24:00, as a donut -- the same day as
    the table's Tomorrow column beside it (household, 11 Oct 2026). Checked
    against production's plan: in 23.5 + 103.2 = 126.7 kWh = the plan's
    charging; out 14.4 + 92.8 + 1.3 = 108.5 = its discharging. An earlier
    version summed the whole four-day plan (483 kWh "grid in")."""
    plan = "@nimbus_solver_battery_forecast"
    flows = (
        ("pv_to_battery", "Solar → battery", "#ffb340"),
        ("grid_to_battery", "Grid → battery", "#4fa3ff"),
        ("battery_to_load", "Battery → house", "#3ddc84"),
        ("battery_to_grid", "Battery → grid", "#ff4d8d"),
        ("battery_to_losses", "Battery losses", "#6b7280"),
    )
    return _apex(
        "Battery energy, tomorrow",
        "The battery's own energy only, kWh, 00:00-24:00 tomorrow, as planned: "
        "what charges it (solar, grid) and where its discharge goes (house, grid, "
        "conversion losses). Solar that goes straight to the house or the grid is "
        "not counted, so Solar → battery is not the day's solar.",
        [
            {
                "entity": plan,
                "name": name,
                "color": color,
                "unit": "kWh",
                "float_precision": 1,
                "data_generator": (
                    "const d = new Date(); d.setHours(24, 0, 0, 0); "
                    "const t0 = d.getTime(), t1 = t0 + 24 * 3600 * 1000; "
                    "const f = (entity.attributes.forecast || []).filter(p => "
                    "{ const t = new Date(p.time).getTime(); return t >= t0 && t < t1; }); "
                    f"const kwh = f.reduce((a, p) => a + (p.flow_{key}_kw || 0) "
                    "* (p.hours || 0), 0); return [[Date.now(), kwh]];"
                ),
            }
            for key, name, color in flows
        ],
        chart_type="donut",
        apex_config={
            "legend": {"show": False},
            "plotOptions": {"pie": {"offsetY": 18, "customScale": 0.92}},
        },
    )


# --- #1704: score detail, plan tables (household, 11 Oct 2026) -------------
# Every figure is read from Nimbus's own published plan and quality report,
# and each table's total is the sum of its own rows.

_PLAN = "${@nimbus_solver_battery_forecast}"


def _per_day_money() -> dict[str, Any]:
    """The plan's cost, split by day (cost_breakdown_by_day, published by the
    Solver); the total is the plan cost."""
    content = (
        f"{{% set by = state_attr('{_PLAN}', 'cost_breakdown_by_day') or {{}} %}}"
        "{% set ns = namespace(rows=[], g=0, fe=0, w=0, e=0, c=0) %}"
        "{% for d in by | sort %}{% set r = by[d] %}"
        "{% set fee = (r.charge_fee | float(0)) + (r.discharge_fee | float(0)) %}"
        "{% set ns.g = ns.g + r.grid_net | float(0) %}{% set ns.fe = ns.fe + fee %}"
        "{% set ns.w = ns.w + r.degradation | float(0) %}"
        "{% set ns.e = ns.e + r.end_of_plan | float(0) %}"
        "{% set ns.c = ns.c + r.plan_cost | float(0) %}"
        "{% set ns.rows = ns.rows + ['| %s | %.2f | %.2f | %.2f | %s | %.2f |' "
        "| format(as_datetime(d ~ 'T00:00:00').strftime('%a %d %b'), r.grid_net | float(0), "
        "fee, r.degradation | float(0), ('%.2f' | format(r.end_of_plan | float(0))) "
        "if (r.end_of_plan | float(0)) != 0 else '—', r.plan_cost | float(0))] %}"
        "{% endfor %}"
        "**The plan's cost, by day, $**\n\n"
        "{% if ns.rows %}"
        "| Day | Grid | Fees | Battery wear | End of plan | Plan cost |\n"
        "|:---|---:|---:|---:|---:|---:|\n"
        "{{ ns.rows | join('\\n') }}\n"
        "| **Total** | **{{ '%.2f' | format(ns.g) }}** | **{{ '%.2f' | format(ns.fe) }}** "
        "| **{{ '%.2f' | format(ns.w) }}** | **{{ '%.2f' | format(ns.e) }}** "
        "| **{{ '%.2f' | format(ns.c) }}** |\n\n"
        "{% else %}Not published by the Solver yet: it is, from the first solve "
        "after updating to this version.\n\n{% endif %}"
        "Midnight to midnight. End of plan: the value left in the battery, on the "
        "plan's last day only. Below zero earns."
    )
    return {"type": "markdown", "content": content}


# The household's own cards, copied from their Forecaster tab as they are
# (household, 12 Oct 2026: "move things as they are"), entity ids resolved
# per install. Edits: the EPR card's `{% if %}` no longer leaves a
# blank line that ended its table, and the daily summary no longer says the
# Solver does not drive dispatch, which stopped being true in September;
# and (nimbus #1753, household, 13 Oct 2026) the forecast table runs the
# plan's whole horizon with its TOTAL row at each midnight, where it used
# to stop at the first one.
_HOUSEHOLD_QUALITY = {
    "type": "markdown",
    "title": "Nimbus Solver Quality (EPR / Regret)",
    "content": "{% set s = states.${@nimbus_solver_quality_report} %}{% if s and s.attributes.get('latest_date') %}{% set epr = s.attributes.get('epr', 0) %}{% set epr_color = 'lightgreen' if epr >= 0.7 else ('orange' if epr >= 0.4 else 'red') %}**Latest scored day: {{ s.attributes.latest_date }}**\n\n| Metric | Value |\n|---|---|\n{% set _h = s.attributes.get('history') or {} %}{% set _row = _h.get(s.attributes.get('latest_date')) or {} %}{% set _prov = _row.get('p') or s.attributes.get('real_p2p_settlement_status') in ['no_settlement_entry_for_this_date','settlement_sensor_unreadable'] %}| EPR (Economic Performance Ratio) | <font color='{{ epr_color }}'>**{{ (epr * 100) | round(1) }}%**</font>{% if _prov %} <font color='orange'>(provisional)</font>{% endif %} |\n{%- if _prov %}\n> <font color='orange'>**Waiting on P2P settlement.**</font> Scored before this day's settlement existed -- export priced at plain spot, so the EPR above reads far lower than reality and can be negative. Self-repairs once settlement lands. Not a dispatch fault.\n{%- endif %}\n| Value captured | <font color='lightgreen'>${{ s.attributes.get('value_captured', 0) | round(2) }}</font> |\n| Uplift available (regret) | <font color='orange'>${{ s.attributes.get('uplift_available', 0) | round(2) }}</font> |\n| Theoretical maximum yield | ${{ s.attributes.get('theoretical_maximum_yield', 0) | round(2) }} |\n| Real P2P settled | ${{ s.attributes.get('real_p2p_dollars', 0) | round(2) }} / {{ s.attributes.get('real_p2p_volume_kwh', 0) | round(1) }}kWh |\n| Tracking fidelity | {{ (s.attributes.get('tracking_fidelity', 0) * 100) | round(1) }}% |\n| Tracking cost | ${{ s.attributes.get('tracking_cost', 0) | round(2) }} |\n| J_ref (idle) / J_ach (real) / J_star (perfect foresight, real costs) | ${{ s.attributes.get('j_ref', 0) | round(2) }} / ${{ s.attributes.get('j_ach', 0) | round(2) }} / ${{ s.attributes.get('j_star_evaluator', s.attributes.get('j_star', 0)) | round(2) }} |\n\n*EPR = share of the real available economic uplift the Solver's own plan actually captured (100% = matched a real perfect-foresight oracle; 0% = captured nothing beyond doing nothing at all). Scored once daily against yesterday's real, fully-settled data.*{% else %}*No scored day yet -- runs once daily at 06:17 AEST against yesterday's real settled data.*{% endif %}",
}
_HOUSEHOLD_UNTIL_MIDNIGHT = {
    "type": "markdown",
    "title": "Solver Forecast — Whole Plan",
    "content": '{% set s = states.${@nimbus_solver_battery_forecast} %}{% if s and s.attributes.get(\'forecast\') %}{% set fc = s.attributes.forecast %}| Time | Buy¢ | Fees¢ | Cons¢ | Sell¢ | P2P¢ | Load | Solar | Batt | Grid | SoC% | Net$ |\n|---|---|---|---|---|---|---|---|---|---|---|---|\n{% set day_ns = namespace(date=none, net=0, p2p=0) %}{% for p in fc %}{% set p_date = as_timestamp(p.time) | timestamp_custom(\'%Y-%m-%d\') %}{% if day_ns.date is not none and p_date != day_ns.date %}| **— {{ day_ns.date }} TOTAL (shown rows) —** |  |  |  |  | <font color=\'gold\'>**+${{ day_ns.p2p | round(2) }}**</font> |  |  |  |  |  | <font color=\'{{ "lightgreen" if day_ns.net < 0 else "orange" }}\'>**${{ day_ns.net | round(2) }}**</font> |\n{% set day_ns.net = 0 %}{% set day_ns.p2p = 0 %}{% endif %}{% set day_ns.date = p_date %}{% set day_ns.net = day_ns.net + p.net_cost %}{% set day_ns.p2p = day_ns.p2p + p.bonus_price * p.export_bonus_kw * p.hours %}{% set cons = p.import_price_source == \'consolidated\' and p.consolidated_import_price is not none %}{% set base = p.consolidated_import_price if cons else p.import_price_raw %}{% set shift = ((base - p.import_price_raw) * 100) | round(1) %}| {{ as_timestamp(p.time) | timestamp_custom(\'%H:%M\') }} | {{ (p.import_price_raw * 100) | round(1) }} | {{ ((p.import_price - base) * 100) | round(1) }} | {% if cons %}<font color=\'violet\'>{{ \'+\' if shift > 0 else \'\' }}{{ shift }}</font>{% endif %} | {{ (p.export_price * 100) | round(1) }} | <font color=\'{{ "gold" if p.bonus_price > 0.01 else "gray" }}\'>{{ (((p.export_price + p.bonus_price) * 100) | round(1)) if p.bonus_price > 0.01 else 0 }}</font> | {{ p.load_kw | round(1) }} | {{ p.solar_kw | round(1) }} | <font color=\'{{ "lightgreen" if p.battery_kw > 0.05 else ("orange" if p.battery_kw < -0.05 else "gray") }}\'>{{ p.battery_kw | round(1) }}{{ " ⚡" if p.export_bonus_kw is defined and p.export_bonus_kw > 0 else "" }}</font> | {% set g = p.load_kw - p.solar_kw - p.battery_kw %}<font color=\'{{ "orange" if g > 0.05 else ("lightgreen" if g < -0.05 else "gray") }}\'>{{ (g | round(1)) if (g | abs) >= 0.05 else 0.0 }}</font> | {{ p.soc_pct | round(0) | int }}% | <font color=\'{{ "lightgreen" if p.net_cost < 0 else "orange" }}\'>{{ p.net_cost | round(2) }}</font> |\n{% endfor %}{% if day_ns.date is not none %}| **— {{ day_ns.date }} TOTAL (shown rows) —** |  |  |  |  | <font color=\'gold\'>**+${{ day_ns.p2p | round(2) }}**</font> |  |  |  |  |  | <font color=\'{{ "lightgreen" if day_ns.net < 0 else "orange" }}\'>**${{ day_ns.net | round(2) }}**</font> |\n{% endif %}\n{% endif %}',
}
_HOUSEHOLD_DAILY = {
    "type": "markdown",
    "title": "Solver Daily Summary (Forecast Only)",
    "content": "{% macro day_table(day_label, day_str, periods) %}\n{% set ns = namespace(import_cost=0, import_kwh=0, spot_earn=0, spot_kwh=0, p2p_earn=0, p2p_kwh=0, net=0, socs=[], batt=[], found=false, end_min=0) %}\n{% for p in periods %}\n{% if p.time[:10] == day_str %}\n{% set ns.found = true %}\n{% set ns.import_cost = ns.import_cost + p.grid_import_kw * p.hours * p.import_price %}\n{% set ns.import_kwh = ns.import_kwh + p.grid_import_kw * p.hours %}\n{% set ns.spot_earn = ns.spot_earn + p.grid_export_kw * p.hours * p.export_price %}\n{% set ns.spot_kwh = ns.spot_kwh + p.grid_export_kw * p.hours %}\n{% set ns.p2p_earn = ns.p2p_earn + p.export_bonus_kw * p.hours * p.bonus_price %}\n{% set ns.p2p_kwh = ns.p2p_kwh + p.export_bonus_kw * p.hours %}\n{% set ns.net = ns.net + p.net_cost %}\n{% set ns.socs = ns.socs + [p.soc_pct] %}\n{% set ns.batt = ns.batt + [p.battery_kw] %}\n{% set ns.end_min = (p.time[11:13] | int) * 60 + (p.time[14:16] | int) + p.hours * 60 %}\n{% endif %}\n{% endfor %}\n**{{ day_label }} ({{ day_str }})**{% if ns.found and ns.end_min < 1439 %} -- plan ends {{ '%02d:%02d' % ((ns.end_min // 60) | int, (ns.end_min % 60) | int) }}{% endif %}\n\n{% if ns.found %}\n| Metric | Value |\n|---|---:|\n| Import Cost | -${{ ns.import_cost | round(2) }} ({{ ns.import_kwh | round(1) }} kWh) |\n| Spot Export Earnings | +${{ ns.spot_earn | round(2) }} ({{ ns.spot_kwh | round(1) }} kWh) |\n| P2P Bonus Earnings | +${{ ns.p2p_earn | round(2) }} ({{ ns.p2p_kwh | round(1) }} kWh) |\n| Total Export Earnings | +${{ (ns.spot_earn + ns.p2p_earn) | round(2) }} |\n| Net $ (forecast) | <font color='{{ \"lightgreen\" if ns.net < 0 else \"orange\" }}'>${{ ns.net | round(2) }}</font> |\n| SoC Range | {{ (ns.socs | min) | round(0) | int }}% - {{ (ns.socs | max) | round(0) | int }}% |\n| Peak Charge / Discharge | {{ (ns.batt | min) | round(1) }} / {{ (ns.batt | max) | round(1) }} kW |\n{% else %}\n*No forecast periods for this day.*\n{% endif %}\n\n{% endmacro %}\n{% set fc = states.${@nimbus_solver_battery_forecast} %}\n{% if fc and fc.attributes.get('forecast') %}\n{% set periods = fc.attributes.forecast %}\n{{ day_table('Today', now().strftime('%Y-%m-%d'), periods) }}\n{{ day_table('Tomorrow', (now() + timedelta(days=1)).strftime('%Y-%m-%d'), periods) }}\n{% else %}\n*No Solver forecast yet.*\n{% endif %}",
}

# Cards in one row share one height and never scroll (household, 12 Oct
# 2026). Only the until-midnight table scrolls, as asked.
_FILL_NO_SCROLL = "ha-card { height: 100%; box-sizing: border-box; overflow: hidden; } ha-card td, ha-card th { white-space: normal; overflow-wrap: anywhere; }"
_UNTIL_MIDNIGHT_STYLE: Any = {
    ".": "ha-card { height: 100%; overflow-x: hidden; overflow-y: auto; }",
    # The table is inside ha-markdown's own shadow root, so it is styled
    # there; styled from ha-card it kept its own width (12 Oct 2026).
    "ha-markdown$": (
        "table { width: 100%; table-layout: fixed; font-size: 0.88em; } "
        "th, td { padding: 3px 6px; white-space: nowrap; overflow: hidden; }"
    ),
}
# Chart cards fill their cell, title on top, figures at the bottom.
_CHART_FIGURES_BELOW = "ha-card { height: 100%; box-sizing: border-box; display: flex; flex-direction: column; } #header { display: contents; } #header__title { order: 0; } #graph-wrapper { order: 1; flex: 1 1 auto; } #header__states { order: 2; margin-top: auto; }"
_FILL = "ha-card { height: 100%; box-sizing: border-box; }"
# The quality card in Performance (household, 17 Oct 2026): it overflowed its
# 8-row cell by a few pixels and scrolled; at 10 rows it left a gap. Tighter
# table rows fit it in 8. The table is in ha-markdown's own shadow root, so it
# is styled there (as _UNTIL_MIDNIGHT_STYLE does).
_QUALITY_STYLE: Any = {
    ".": _FILL_NO_SCROLL,
    "ha-markdown$": "th, td { padding-top: 2px; padding-bottom: 2px; }",
}


_DAILY_CALLS = (
    "{{ day_table('Today', now().strftime('%Y-%m-%d'), periods) }}\n",
    (
        "{{ day_table('Tomorrow', (now() + timedelta(days=1)).strftime"
        "('%Y-%m-%d'), periods) }}\n"
    ),
)


def _daily(offset: int) -> dict[str, Any]:
    """The household's daily summary for one day of the plan, `offset` days
    from today: their card with one day's table, title without "(Forecast
    Only)" (household, 12 Oct 2026). Days 0 and 1 are "Today" and
    "Tomorrow"; later days are named by weekday (household, 17 Oct 2026:
    the plan runs about four days, so days 3 and 4 fill the Plan row)."""
    content = _HOUSEHOLD_DAILY["content"]
    assert all(call in content for call in _DAILY_CALLS)
    when = f"(now() + timedelta(days={offset}))" if offset else "now()"
    label = (
        ("'Today'", "'Tomorrow'")[offset]
        if offset < len(_DAILY_CALLS)
        else f"{when}.strftime('%A')"
    )
    call = f"{{{{ day_table({label}, {when}.strftime('%Y-%m-%d'), periods) }}}}\n"
    first, second = _DAILY_CALLS
    return {
        **_HOUSEHOLD_DAILY,
        "title": "Solver Daily Summary",
        "content": content.replace(first + second, call),
    }


def _styled(card: dict[str, Any], style: Any) -> dict[str, Any]:
    return {**card, "card_mod": {"style": style}}


def _risk_row() -> list[dict[str, Any]]:
    """The three risk sliders and their explanation, one column each, one
    height (household, 12 Oct 2026). `nimbus:risk-slider` is resolved per
    install: the household's mushroom slider (large icon, red-to-green with
    the value) where mushroom is installed, HA's tile slider elsewhere."""
    height = {"columns": "full", "rows": 4}
    sliders = [
        {
            "type": "grid",
            "column_span": 1,
            "cards": [
                {
                    "type": "nimbus:risk-slider",
                    "entity": f"@{key}",
                    "name": name,
                    "grid_options": height,
                }
            ],
        }
        for key, name in _SLIDERS
    ]
    explain = {
        "type": "grid",
        "column_span": 1,
        "cards": [
            {
                "type": "markdown",
                "content": _RISK_TEXT,
                "card_mod": {"style": _FILL_NO_SCROLL},
                "grid_options": height,
            }
        ],
    }
    return [*sliders, explain]


# The household's own mushroom slider (their first Solver tab), icon at
# 100 px (12 Oct 2026): red at 0, green at 1.
_MUSHROOM_STYLE = (
    "ha-card {\n"
    "  {% set v = states('ENTITY') | float(0) %}\n"
    "  {% set r = (244 - (244 - 76) * v) | round | int %}\n"
    "  {% set g = (67 + (175 - 67) * v) | round | int %}\n"
    "  {% set b = (54 + (80 - 54) * v) | round | int %}\n"
    "  {% set c = 'rgb(' ~ r ~ ',' ~ g ~ ',' ~ b ~ ')' %}\n"
    "  --icon-color: {{ c }};\n"
    "  --main-color: {{ c }};\n"
    "  --slider-color: {{ c }};\n"
    "  --rgb-state-active: {{ r }},{{ g }},{{ b }};\n"
    "  --card-mod-icon-color: {{ c }};\n"
    "  --mush-icon-size: 100px;\n"
    "  --mush-icon-symbol-size: 0.6em;\n"
    "  height: 100%;\n"
    "}"
)


def _risk_slider(node: dict[str, Any], mushroom: str | None) -> dict[str, Any]:
    """One `nimbus:risk-slider` (entity already resolved) as a real card."""
    if mushroom:
        return {
            "type": mushroom,
            "entity": node["entity"],
            "name": node["name"],
            "secondary_info": "state",
            "icon": "mdi:speedometer",
            "layout": "vertical",
            "display_mode": "slider",
            "fill_container": True,
            "grid_options": node.get("grid_options"),
            "card_mod": {"style": _MUSHROOM_STYLE.replace("ENTITY", node["entity"])},
        }
    return {
        "type": "tile",
        "entity": node["entity"],
        "name": node["name"],
        "icon": "mdi:speedometer",
        "features": [{"type": "numeric-input", "style": "slider"}],
        "features_position": "bottom",
        "grid_options": node.get("grid_options"),
    }


def _top_row() -> list[dict[str, Any]]:
    """Solve now, and what it changes: the plan's status and when it last
    solved, how long that took, and what is limiting the plan right now."""
    height = {"columns": "full", "rows": 2}
    button: dict[str, Any] = {
        "type": "button",
        "name": "Solve now",
        "icon": "mdi:play-circle",
        "icon_height": "48px",
        "show_state": False,
        "tap_action": {
            "action": "perform-action",
            "perform_action": "nimbus_load.solve_now",
        },
    }
    tiles: list[dict[str, Any]] = [
        {
            "type": "tile",
            "entity": "@nimbus_solver_lp_status",
            "name": "Plan",
            "state_content": ["state", "last_updated"],
        },
        {
            "type": "tile",
            "entity": "@nimbus_solver_solve_seconds",
            "name": "Solve time",
        },
        {
            "type": "tile",
            "entity": "@nimbus_solver_binding_constraint_now",
            "name": "Limiting the plan now",
        },
    ]
    return [
        {"type": "grid", "column_span": 1, "cards": [{**c, "grid_options": height}]}
        for c in (button, *tiles)
    ]


def template() -> list[dict[str, Any]]:
    """The tab's sections with `@key` placeholders, before resolution. Also
    what the tab's design fingerprint is taken from, so it is the same on
    every install."""
    banners = {name: _banner(name, kind, line) for name, kind, line in _BANNERS}
    # Household, 17 Oct 2026: the quality card scrolled at 8 and left a gap
    # at 10, so it stays 8 with a tighter table (_QUALITY_STYLE).
    row = {"rows": 8}
    half = {"rows": 7}
    plan = {"rows": 12}
    return [
        *_top_row(),
        banners["Performance"],
        _section([_cost_chart()], 4, {"rows": 6}),
        _section([_styled(_HOUSEHOLD_QUALITY, _QUALITY_STYLE)], 1, row),
        _section([_styled(_glance_chart(), _CHART_FIGURES_BELOW)], 1, row),
        _section([_styled(_score_chart(), _CHART_FIGURES_BELOW)], 1, row),
        _section([_styled(_breakdown_chart(), _CHART_FIGURES_BELOW)], 1, row),
        banners["Money"],
        *(_section([_card(c)], 1) for c in _MONEY_ROW),
        _section([_styled(_energy_table(), _FILL_NO_SCROLL)], 1, half),
        _section([_styled(_per_day_money(), _FILL_NO_SCROLL)], 1, half),
        _section([_styled(_energy_pie(), _FILL)], 2, half),
        banners["Plan"],
        _section([_chart()], 4),
        # Household, 20 Oct 2026: on a first visit this chart drew a third of
        # the width until a reload. It was the only full-width chart in a
        # fixed-row cell with a height style on top; the battery chart above,
        # with neither, draws full width, so this one now matches it.
        _section([_prices_chart()], 4),
        *(
            _section([_styled(_price_day(k), _FILL_NO_SCROLL)], 1, {"rows": 5})
            for k in range(4)
        ),
        *(
            _section([_styled(c, _FILL_NO_SCROLL)], 1, {"rows": 3})
            for c in _PRICE_SAFETY
        ),
        _section([_CONSOLIDATION_TOGGLE], 1, {"rows": 3}),
        # Household, 19 Oct 2026: "why have whole empty row for a tiny
        # table... make the middle table fit in 1 column and make the
        # consolidation fit next to it". Four one-column sections, so later
        # sections keep their columns; Device setup and Price sources take
        # the height their text needs (no fixed rows), so nothing is clipped.
        _section([_styled(_DEVICE_SETUP, _FILL_NO_SCROLL)], 2, {"rows": 3}),
        _section([_DEVICE_SETUP_MODE], 1, {"rows": 3}),
        # Household, 19 Oct 2026 ("the gaps are horrible"; the status was
        # said twice): the controls row stays level, and the sources get a
        # full-width row of their own, sources down the side and the next
        # slots across, so the width carries real numbers. No fixed height.
        _section([_CONSOLIDATION_SOURCES], 4),
        _section([_styled(_HOUSEHOLD_UNTIL_MIDNIGHT, _UNTIL_MIDNIGHT_STYLE)], 2, plan),
        # Household, 17 Oct 2026: the quality card was here a second time.
        # The plan's four days instead, two to a column.
        *(
            _section(
                [_styled(_daily(d), _FILL_NO_SCROLL) for d in days],
                1,
                {"rows": 6},
            )
            for days in ((0, 1), (2, 3))
        ),
        *_risk_row(),
        banners["Basic settings"],
        *(_section([_card(c)], 1) for c in _BASIC_ROW),
        banners["Advanced settings"],
        *(_section([_card(c)], 1) for c in _ADVANCED_ROW),
        banners["More settings"],
        *(_section([_card(c)], 1) for c in _MORE_ROW),
        banners["Stochastic"],
        _section(
            [_styled(_STOCHASTIC_NOW, _stochastic_now_style())], 1, _STOCHASTIC_ROW_SIZE
        ),
        # The chart fills its cell (the Performance charts' flex style).
        _section(
            [_styled(_stochastic_chart(), _CHART_FIGURES_BELOW)],
            2,
            _STOCHASTIC_ROW_SIZE,
        ),
        _section([_styled(_stochastic_settings(), _FILL)], 1, _STOCHASTIC_ROW_SIZE),
    ]


_TEXT_REF = re.compile(r"\$\{@([a-z0-9_:]+)\}")


def resolve(value: Any, lookup: Callable[[str], str | None]) -> Any:
    """`value` with every `@key` replaced by this install's entity_id.

    `lookup("nimbus_solver_lp_status")` returns the entity_id or None;
    `${@key}` inside a string is replaced the same way;
    `lookup("option:battery_sensor")` the hub option. Anything unresolved is
    dropped: a card whose `entity` is missing, or one entry of an
    `entities` list. An `entities` card left empty is dropped with it.
    """
    value = copy.deepcopy(value)

    def one(ref: Any) -> Any:
        if isinstance(ref, str) and ref.startswith("@"):
            return lookup(ref[1:])
        if isinstance(ref, dict) and isinstance(ref.get("entity"), str):
            target = one(ref["entity"])
            return None if target is None else {**ref, "entity": target}
        return ref

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            out = []
            for item in node:
                item = walk(item)
                if item is not None:
                    out.append(item)
            return out
        if isinstance(node, str):
            # `${@key}` inside text (a markdown card's live values).
            return _TEXT_REF.sub(lambda m: lookup(m.group(1)) or "", node)
        if not isinstance(node, dict):
            return node
        refs = _TEXT_REF.findall(str(node.get("content", "")))
        if refs and not any(lookup(r) for r in refs):
            return None  # text about entities this install does not have
        # `nimbus_unless: "@key"`: the node is for installs WITHOUT that
        # entity (the fleet SoC lines, where per-battery ones replace them).
        unless = node.get("nimbus_unless")
        if isinstance(unless, str):
            if lookup(unless.removeprefix("@")) is not None:
                return None
            node = {k: v for k, v in node.items() if k != "nimbus_unless"}
        node = {k: walk(v) for k, v in node.items()}
        if "entity" in node:
            target = one(node["entity"])
            if target is None:
                return None
            node["entity"] = target
        if node.get("type") == "nimbus:risk-slider":
            return _risk_slider(node, lookup("card:mushroom"))
        if node.get("type") == "conditional" and (
            not node.get("row") or not node.get("conditions")
        ):
            return None
        if node.get("type") == "custom:apexcharts-card" and not node.get("series"):
            return None
        if isinstance(node.get("entities"), list):
            node["entities"] = [
                e for e in (one(x) for x in node["entities"]) if e is not None
            ]
            if not any(
                not (isinstance(e, dict) and e.get("type") == "section")
                for e in node["entities"]
            ):
                return None
        return node

    out = walk(value)
    # A column whose cards all dropped is left empty: drop it whole.
    if isinstance(out, list):
        out = [
            sec for sec in out if not (isinstance(sec, dict) and sec.get("cards") == [])
        ]
    return out
