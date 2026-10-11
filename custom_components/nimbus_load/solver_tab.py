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
import re
from collections.abc import Callable
from typing import Any

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
        ("solver_max_charge_kw", "Max charge"),
        ("solver_max_discharge_kw", "Max discharge"),
        ("solver_efficiency_percent", "Round-trip efficiency"),
        ("solver_inverter_self_consumption_kw", "Inverter self-use"),
    ],
    "Limits every plan must respect.",
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
    ],
    "Fees are added to the buy price.",
)

_P2P: _Card = (
    "P2P",
    [
        ("solver_p2p_block_1_rate_kw", "Block 1 rate"),
        ("solver_p2p_block_2_rate_kw", "Block 2 rate"),
        ("solver_p2p_block_3_rate_kw", "Block 3 rate"),
        ("solver_p2p_block_lead_time_minutes", "Start lead time"),
        ("solver_p2p_bonus_price", "Bonus price"),
        ("solver_p2p_bonus_volume_kwh", "Bonus volume"),
        ("nimbus_solver_current_bonus_price", "Bonus price now"),
        ("solver_post_window_self_consume_hours", "Self-consume after"),
    ],
    "Rate 0 = block unused. Hours: Advanced.",
)

_RESERVE: _Card = (
    "Reserve and price spikes",
    [
        ("solver_battery_reserve_percent", "Reserve"),
        ("solver_battery_reserve_release_price", "Release margin"),
        ("solver_battery_reserve_start_hour", "Hold from"),
        ("solver_battery_reserve_end_hour", "Release at"),
        ("solver_price_event_enabled", "Price events"),
        ("solver_price_spike_threshold", "Spike threshold"),
        ("solver_price_spike_discharge_kw", "Spike discharge"),
        ("solver_price_spike_override_armed", "Spike override armed"),
    ],
    "Insurance against a sudden high price.",
)

# --- Advanced settings: four cards, eight rows each ------------------------

_COSTS: _Card = (
    "Costs and stability",
    [
        ("solver_charge_cost", "Charge cost"),
        ("solver_discharge_cost", "Discharge cost"),
        ("solver_degradation_cost_per_kwh", "Battery wear cost"),
        ("solver_salvage_value", "Value left at end"),
        ("solver_intraplan_smoothness_weight_kw", "Plan smoothness"),
        ("solver_proximal_weight_kw", "Stick to last plan"),
        ("solver_battery_charge_earliness_budget_kw", "Charge earliness"),
        ("solver_aemo_p5min_disagreement_threshold_dollars", "AEMO disagreement"),
    ],
    "Stop cycling and jitter for no gain.",
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
    ],
    "A day over the limits is not scored.",
)

_P2P_HOURS: _Card = (
    "P2P hours and matching",
    [
        ("solver_p2p_block_1_start_hour", "Block 1 from"),
        ("solver_p2p_block_1_end_hour", "Block 1 until"),
        ("solver_p2p_block_2_start_hour", "Block 2 from"),
        ("solver_p2p_block_2_end_hour", "Block 2 until"),
        ("solver_p2p_block_3_start_hour", "Block 3 from"),
        ("solver_p2p_block_3_end_hour", "Block 3 until"),
        ("nimbus_solver_p2p_match_fraction", "Matched share"),
        ("nimbus_solver_p2p_recent_avg_volume_kwh", "Matched volume"),
    ],
    "When each block exports; how much sold.",
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

# (name, ha-alert type, one line). The alert type gives each band its colour.
_BANNERS = (
    ("Performance", "info", "How it did, and what the plan ahead should earn."),
    ("Money", "success", "The figures behind the charts."),
    ("Plan", "warning", "What the Solver intends to do with the battery."),
    ("Basic settings", "info", "Your battery, grid, fees and P2P. Set these first."),
    ("Advanced settings", "error", "Fine-tuning. The defaults suit most installs."),
)

_MONEY_ROW = (_SCORED, _MONEY, _BREAKDOWN, _BATTERY_NOW)
_BASIC_ROW = (_BATTERY, _GRID, _P2P, _RESERVE)
_ADVANCED_ROW = (_COSTS, _FEATURES, _SCORING, _P2P_HOURS)

# A block's rate reads "0.0" (or "0") while unused.
_UNUSED = ("0", "0.0")

_MUTED = "#9aa0ac"


def _row(key: str, name: str) -> dict[str, Any]:
    return {"entity": f"@{key}", "name": name}


def _rows(rows: list[tuple[str, ...]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
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


def _section(cards: list[dict[str, Any]], span: int) -> dict[str, Any]:
    return {
        "type": "grid",
        "column_span": span,
        "cards": [{**c, "grid_options": {"columns": "full"}} for c in cards],
    }


def _banner(name: str, kind: str, line: str) -> dict[str, Any]:
    """A full-width coloured title band opening a block."""
    return _section(
        [
            {
                "type": "markdown",
                "text_only": True,
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


def _forecast(field: str, scale: float = 1.0) -> str:
    """A data_generator reading one field of the published plan."""
    return (
        "return (entity.attributes.forecast || []).map(p => "
        f"[new Date(p.time).getTime(), p.{field} * {scale}]);"
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
        apex_config={"plotOptions": {"radialBar": {"hollow": {"size": "30%"}}}},
    )


def _score_chart() -> dict[str, Any]:
    day = {"group_by": {"func": "last", "duration": "1d"}}
    return _apex(
        "Score by day",
        "EPR (line) and regret in dollars (bars), once settled.",
        [
            {
                "entity": "@nimbus_quality_regret_dollars",
                "name": "Regret",
                "type": "column",
                "color": "#E8615F",
                "yaxis_id": "aud",
                **day,
            },
            {
                "entity": "@nimbus_quality_epr",
                "name": "EPR",
                "type": "line",
                "color": "#4FC3C7",
                "stroke_width": 3,
                "yaxis_id": "pct",
                **day,
            },
        ],
        graph_span="14d",
        span={"end": "day"},
        yaxis=[
            {"id": "pct", "min": 0, "max": 100, "decimals": 0, "opposite": True},
            {"id": "aud", "min": 0, "decimals": 0},
        ],
    )


def _cost_chart() -> dict[str, Any]:
    hour = {"group_by": {"func": "last", "duration": "1h"}, "curve": "stepline"}
    return _apex(
        "Plan cost, last 7 days",
        "Plan cost (white) inside its best–worst range. Below zero it earns.",
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
        apex_config={"fill": {"opacity": [0.25, 0.25, 1]}},
    )


def _breakdown_chart() -> dict[str, Any]:
    """Stacked columns every 3 hours. Earnings stack below zero, costs above;
    the axis follows both, and every segment carries its dollar label so a
    few cents of wear still reads next to tens of dollars of grid."""
    step = {"group_by": {"func": "last", "duration": "3h"}, "type": "column"}
    parts = (
        ("nimbus_solver_cost_breakdown_grid_net", "Grid, net", "#4fa3ff"),
        ("nimbus_solver_cost_breakdown_charge_fee", "Charging", "#ffb340"),
        ("nimbus_solver_cost_breakdown_discharge_fee", "Discharging", "#ff4d8d"),
        ("nimbus_solver_cost_breakdown_degradation", "Battery wear", "#E8615F"),
        ("nimbus_solver_cost_breakdown_terminal_value_credit", "Value left", "#3ddc84"),
    )
    return _apex(
        "Where the cost comes from, last 24 h",
        "Above zero costs, below zero earns. Labels in dollars.",
        [{"entity": f"@{k}", "name": n, "color": c, **step} for k, n, c in parts],
        height=320,
        graph_span="24h",
        stacked=True,
        yaxis=[{"decimals": 0}],
        apex_config={
            "dataLabels": {
                "enabled": True,
                "style": {"fontSize": "10px"},
                "formatter": (
                    "EVAL:function(v){return (v === null || Math.abs(v) < 0.005)"
                    " ? '' : '$' + v.toFixed(2);}"
                ),
            }
        },
    )


# --- Plan charts -----------------------------------------------------------


def _chart() -> dict[str, Any]:
    """Past and plan on one axis, a Now line, step lines like the household's
    own "Nimbus vs Real" chart. Price is 5-minute detail behind Now (the buy
    price's own history) and the plan's own steps ahead of it."""
    plan = "@nimbus_solver_battery_forecast"
    past = {"extend_to": False, "group_by": {"func": "last", "duration": "5min"}}
    ahead = {"extend_to": False, "show": {"in_header": False}}
    return _apex(
        "Battery: plan against measured",
        "kW, up = discharging. Solid: measured; teal: plan, dashed ahead. "
        "Green: state of charge. Amber: buy price.",
        [
            {
                "entity": "@option:battery_sensor",
                "name": "Measured",
                "curve": "stepline",
                "color": "#e8eaf0",
                "stroke_width": 3,
                "yaxis_id": "kw",
                **past,
            },
            {
                "entity": plan,
                "name": "Plan",
                "curve": "stepline",
                "color": "#4FC3C7",
                "stroke_width": 1.5,
                "yaxis_id": "kw",
                **past,
            },
            {
                "entity": plan,
                "name": "Plan ahead",
                "curve": "stepline",
                "color": "#4FC3C7",
                "stroke_width": 2.5,
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
                "data_generator": _forecast("soc_pct"),
                **ahead,
            },
            {
                "entity": "@nimbus_solver_current_import_price",
                "name": "Buy price",
                "curve": "stepline",
                "color": "#ffb340",
                "stroke_width": 1.5,
                "yaxis_id": "price",
                "unit": "c/kWh",
                "transform": "return x * 100;",
                **past,
            },
            {
                "entity": plan,
                "name": "Buy price ahead",
                "curve": "stepline",
                "color": "#ffb340",
                "stroke_width": 1.5,
                "stroke_dash": 3,
                "yaxis_id": "price",
                "data_generator": _forecast("import_price", 100.0),
                **ahead,
            },
        ],
        height=360,
        graph_span="36h",
        span={"start": "hour", "offset": "-12h"},
        now={"show": True, "label": "Now", "color": "#ff4d8d"},
        yaxis=[
            {"id": "kw", "decimals": 0, "apex_config": {"title": {"text": "kW"}}},
            {"id": "soc", "min": 0, "max": 100, "show": False},
            {
                "id": "price",
                "opposite": True,
                "decimals": 0,
                "apex_config": {"title": {"text": "c/kWh"}},
            },
        ],
        apex_config={"fill": {"opacity": [1, 1, 1, 0.15, 0.08, 1, 1]}},
    )


def _energy_chart() -> dict[str, Any]:
    plan = "@nimbus_solver_battery_forecast"
    flows = (
        ("pv_to_battery", "Solar in", "#ffb340"),
        ("grid_to_battery", "Grid in", "#4fa3ff"),
        ("battery_to_load", "To house", "#3ddc84"),
        ("battery_to_grid", "To grid", "#ff4d8d"),
    )
    return _apex(
        "Battery energy in the plan",
        "kWh over the whole plan ahead.",
        [
            {
                "entity": plan,
                "name": name,
                "color": color,
                "unit": "kWh",
                "float_precision": 1,
                "data_generator": (
                    "const f = entity.attributes.forecast || []; "
                    f"const kwh = f.reduce((a, p) => a + (p.flow_{key}_kw || 0) "
                    "* (p.hours || 0), 0); return [[Date.now(), kwh]];"
                ),
            }
            for key, name, color in flows
        ],
        height=360,
        chart_type="donut",
    )


def _risk_row() -> list[dict[str, Any]]:
    """Three slider tiles and the explanation, one column each, one height."""
    height = {"rows": 4}
    tiles = [
        _section(
            [
                {
                    "type": "tile",
                    "entity": f"@{key}",
                    "name": name,
                    "vertical": True,
                    "features": [{"type": "numeric-input", "style": "slider"}],
                    "features_position": "bottom",
                }
            ],
            1,
        )
        for key, name in _SLIDERS
    ]
    explain = _section([{"type": "markdown", "content": _RISK_TEXT}], 1)
    for sec in (*tiles, explain):
        sec["cards"][0]["grid_options"] = {"columns": "full", **height}
    return [*tiles, explain]


def template() -> list[dict[str, Any]]:
    """The tab's sections with `@key` placeholders, before resolution. Also
    what the tab's design fingerprint is taken from, so it is the same on
    every install."""
    banners = {name: _banner(name, kind, line) for name, kind, line in _BANNERS}
    return [
        banners["Performance"],
        _section([_glance_chart()], 2),
        _section([_score_chart()], 2),
        _section([_cost_chart()], 2),
        _section([_breakdown_chart()], 2),
        banners["Money"],
        *(_section([_card(c)], 1) for c in _MONEY_ROW),
        banners["Plan"],
        _section([_chart()], 3),
        _section([_energy_chart()], 1),
        *_risk_row(),
        banners["Basic settings"],
        *(_section([_card(c)], 1) for c in _BASIC_ROW),
        banners["Advanced settings"],
        *(_section([_card(c)], 1) for c in _ADVANCED_ROW),
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
        node = {k: walk(v) for k, v in node.items()}
        if "entity" in node:
            target = one(node["entity"])
            if target is None:
                return None
            node["entity"] = target
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
