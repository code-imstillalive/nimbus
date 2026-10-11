"""The standard Solver tab of the Nimbus dashboard (nimbus #1594).

Agreed contents (household, 6 and 8 Oct 2026): what the Solver is doing, the
plan against the measured battery, the money, how well it did, the risk
sliders, and every Solver setting.

Layout (household, 11 Oct 2026): four blocks, top to bottom --

  Performance        the last scored day, the money ahead, the battery
                     now, P2P matching
  Plan               the battery chart (past and plan on one axis, a "now"
                     line), where the cost comes from, the risk sliders
  Basic settings     battery and grid, fees, P2P, battery reserve
  Advanced settings  costs, stability, price events and scoring, features

Each block is its name across the full width, then its columns -- one
section each, so four sit side by side on a PC, two on a tablet, and on a
phone they stack in order. Every card carries its title inside it and a
one-line note under it saying what it shows or what its settings do. A fee or
P2P block shows its hours only once its rate is set.

Plain Home Assistant cards (entities, markdown), plus ApexCharts Card for the
chart -- the same card the Forecaster tab already needs. A household can
move, resize or remove any of it.

Entities are written into the template by **unique-id key**, as `@<key>`,
and resolved per install by `resolve()` when the tab is written. An entity_id
is not stable across installs -- Home Assistant suffixes one that collides,
which is exactly what devhub's `_2` copies are -- but the unique id is
`<config entry id>_<key>`. A card whose entity does not exist on this install
is dropped rather than written blank. `@option:<name>` reads the hub's own
option of that name (the Solver's configured battery sensor).

HA-import-free on purpose, like `ml/` and `solver/`: the caller supplies the
lookup.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

# A card is (title, rows, note). A row is (key, name), or
# ("block", rate_key, start_key, end_key, label) for a fee or P2P block,
# whose hours appear only while its rate is set.
_Card = tuple[str, list[tuple[str, ...]], str]

_SCORED: _Card = (
    "Last day, scored",
    [
        ("nimbus_quality_epr", "EPR"),
        ("nimbus_quality_regret_dollars", "Regret"),
        ("nimbus_quality_epr_reliable", "Score reliable"),
        ("nimbus_counterfactual_soc", "Nimbus-only SoC at 5 pm"),
    ],
    (
        "How the last settled day went. EPR is the share of the best possible "
        "result captured; regret is the money left on the table."
    ),
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
    (
        "What the plan ahead is expected to cost; negative means it earns. "
        "The range is where forecast error could take it."
    ),
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
    "How the battery is being run this minute, and how hard the plan ahead works it.",
)

_P2P_NOW: _Card = (
    "P2P matching",
    [
        ("nimbus_solver_p2p_match_fraction", "Matched share"),
        ("nimbus_solver_p2p_recent_avg_volume_kwh", "Matched volume, recent"),
        ("nimbus_solver_current_bonus_price", "Bonus price now"),
    ],
    (
        "How much peer-to-peer export has recently found a buyer. Empty "
        "without a P2P commitment."
    ),
)

_BREAKDOWN: _Card = (
    "Where the cost comes from",
    [
        ("nimbus_solver_cost_breakdown_grid_net", "Grid, net"),
        ("nimbus_solver_cost_breakdown_charge_fee", "Charging cost"),
        ("nimbus_solver_cost_breakdown_discharge_fee", "Discharging cost"),
        ("nimbus_solver_cost_breakdown_degradation", "Battery wear"),
        (
            "nimbus_solver_cost_breakdown_terminal_value_credit",
            "Value left at plan end",
        ),
    ],
    (
        "The plan cost split into its parts. Value left at plan end is credit "
        "for energy still in the battery."
    ),
)

_BATTERY: _Card = (
    "Battery and grid",
    [
        ("solver_battery_capacity_kwh", "Capacity"),
        ("solver_battery_soh_percent", "State of health"),
        ("solver_battery_min_soc_percent", "Minimum SoC"),
        ("solver_battery_max_soc_percent", "Maximum SoC"),
        ("solver_max_charge_kw", "Max charge power"),
        ("solver_max_discharge_kw", "Max discharge power"),
        ("solver_efficiency_percent", "Round-trip efficiency"),
        ("solver_grid_max_import_kw", "Max grid import"),
        ("solver_grid_max_export_kw", "Max grid export"),
    ],
    "The physical limits every plan must respect.",
)

_RESERVE: _Card = (
    "Battery reserve",
    [
        ("solver_battery_reserve_percent", "Reserve"),
        ("solver_battery_reserve_release_price", "Release margin"),
        ("solver_battery_reserve_start_hour", "Hold from (hour)"),
        ("solver_battery_reserve_end_hour", "Release at (hour)"),
    ],
    (
        "Holds the bottom of the battery back as insurance against an "
        "unforecast price spike, between these hours. 0 turns it off."
    ),
)

_COSTS: _Card = (
    "Costs",
    [
        ("solver_charge_cost", "Charge cost"),
        ("solver_discharge_cost", "Discharge cost"),
        ("solver_degradation_cost_per_kwh", "Battery wear cost"),
        ("solver_salvage_value", "Value left at plan end"),
        ("solver_inverter_self_consumption_kw", "Inverter self-use"),
    ],
    "Small per-kWh costs that stop the battery cycling for no real gain.",
)

_STABILITY: _Card = (
    "Stability",
    [
        ("solver_intraplan_smoothness_weight_kw", "Plan smoothness"),
        ("solver_proximal_weight_kw", "Stick to the last plan"),
        ("solver_battery_charge_earliness_budget_kw", "Charge earliness"),
        ("solver_post_window_self_consume_hours", "Self-consume after a block"),
    ],
    "Keep the battery command steady when several plans cost about the same.",
)

_FEES: _Card = (
    "Fees",
    [
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
    (
        "Added to the import price before planning. A fee block sets a "
        "different network fee for set hours; its hours appear once its rate "
        "is set."
    ),
)

_P2P: _Card = (
    "P2P",
    [
        (
            "block",
            "solver_p2p_block_1_rate_kw",
            "solver_p2p_block_1_start_hour",
            "solver_p2p_block_1_end_hour",
            "Block 1",
        ),
        (
            "block",
            "solver_p2p_block_2_rate_kw",
            "solver_p2p_block_2_start_hour",
            "solver_p2p_block_2_end_hour",
            "Block 2",
        ),
        (
            "block",
            "solver_p2p_block_3_rate_kw",
            "solver_p2p_block_3_start_hour",
            "solver_p2p_block_3_end_hour",
            "Block 3",
        ),
        ("solver_p2p_block_lead_time_minutes", "Start lead time"),
        ("solver_p2p_bonus_price", "Bonus price"),
        ("solver_p2p_bonus_volume_kwh", "Bonus volume"),
    ],
    (
        "Only for a peer-to-peer export commitment: export at the block's rate "
        "between its hours. Leave the rates at 0 if you have none."
    ),
)

_EVENTS: _Card = (
    "Price events",
    [
        ("solver_price_event_enabled", "Price events"),
        ("solver_price_spike_threshold", "Spike threshold"),
        ("solver_price_spike_discharge_kw", "Spike discharge power"),
        ("solver_price_spike_override_armed", "Spike override armed"),
        ("solver_aemo_p5min_disagreement_threshold_dollars", "AEMO disagreement alert"),
    ],
    (
        "How the plan treats a sudden high price: above the threshold it may "
        "sell at the spike power."
    ),
)

_RISK: _Card = (
    "Risk sliders",
    [
        ("solver_import_price_risk_aversion", "Import price"),
        ("nimbus_solver_import_price_risk_effect_now", "Import price: effect now"),
        ("solver_export_price_risk_aversion", "Export price"),
        ("nimbus_solver_export_price_risk_effect_now", "Export price: effect now"),
        ("solver_risk_aversion", "Solar forecast"),
        ("nimbus_solver_solar_risk_effect_now_kw", "Solar forecast: effect now"),
    ],
    (
        "0 trusts the forecast; 1 plans for its worst case. Import: buy less "
        "where the price could rise. Export: sell less where it could fall. "
        "Solar: lean less on the solar forecast."
    ),
)

_FEATURES: _Card = (
    "Features",
    [
        ("solver_load_forecast_source_policy", "Load forecast source"),
        ("solver_auto_include_known_solar", "Auto-include solar integrations"),
        ("solver_calibrated_objective_enabled", "Calibrated objective"),
        ("solver_nowcast_measurement_enabled", "Nowcast measurement"),
        ("solver_offer_curve_enabled", "Offer curve"),
        ("solver_flex_signals_enabled", "Flex signals (slower solves)"),
        ("solver_score_controllable_loads_enabled", "Score controllable loads"),
        ("solver_dispatch_dry_run", "Built-in dispatcher: dry run"),
    ],
    (
        "Optional behaviours; the defaults suit most installs. Dry run on means "
        "Nimbus's own dispatcher sends nothing to the inverter."
    ),
)

_SCORING: _Card = (
    "Scoring thresholds",
    [
        ("solver_soc_discrepancy_mean_threshold_pct", "SoC gap, average"),
        ("solver_soc_discrepancy_max_threshold_pct", "SoC gap, largest"),
    ],
    "A day whose battery readings disagree by more than this is not scored.",
)

# The four blocks (household, 11 Oct 2026). A block is its name, then
# columns; a column is one section holding one or more cards. Four columns
# take one section width each, two take two, so a block fills the row on a
# PC and stacks in order on a phone.
_BLOCKS: tuple[tuple[str, tuple[tuple[_Card, ...], ...]], ...] = (
    (
        "Performance",
        ((_SCORED,), (_MONEY,), (_BATTERY_NOW,), (_P2P_NOW,)),
    ),
    ("Plan", ((_BREAKDOWN,), (_RISK,))),
    ("Basic settings", ((_BATTERY,), (_FEES,), (_P2P,), (_RESERVE,))),
    (
        "Advanced settings",
        ((_COSTS,), (_STABILITY,), (_EVENTS, _SCORING), (_FEATURES,)),
    ),
)

_CHART_NOTE = (
    "Battery power, positive = discharging. Solid: measured. Blue: the plan "
    "as it was published, then the plan ahead (dashed) from now."
)

# A block's rate reads "0.0" (or "0") while unused.
_UNUSED = ("0", "0.0")


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
        for key, part in ((start, "from (hour)"), (end, "until (hour)")):
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


def _note(text: str) -> dict[str, Any]:
    return {"type": "markdown", "text_only": True, "content": text}


def _card(card: _Card) -> list[dict[str, Any]]:
    """A titled entities card with its one-line note under it."""
    title, rows, note = card
    return [
        {"type": "entities", "title": title, "entities": _rows(rows)},
        _note(note),
    ]


def _chart() -> dict[str, Any]:
    plan = "@nimbus_solver_battery_forecast"
    step = {
        "curve": "stepline",
        "extend_to": False,
        "group_by": {"func": "last", "duration": "1min"},
    }
    return {
        "type": "custom:apexcharts-card",
        "header": {
            "show": True,
            "title": "Battery: plan against measured",
            "show_states": True,
            "colorize_states": True,
        },
        "update_interval": "1min",
        "graph_span": "36h",
        "span": {"start": "hour", "offset": "-12h"},
        "now": {"show": True, "label": "Now", "color": "#ff4d8d"},
        "yaxis": [{"decimals": 0}],
        "apex_config": {"chart": {"height": 280}, "legend": {"show": True}},
        "series": [
            {
                "entity": "@option:battery_sensor",
                "name": "Measured",
                "color": "#ffb340",
                "stroke_width": 2,
                **step,
            },
            {
                "entity": plan,
                "name": "Plan, as published",
                "color": "#4fa3ff",
                "stroke_width": 1.5,
                "show": {"in_header": False},
                **step,
            },
            {
                "entity": plan,
                "name": "Plan ahead",
                "color": "#4fa3ff",
                "stroke_width": 2.5,
                "stroke_dash": 5,
                "curve": "stepline",
                "extend_to": False,
                "data_generator": (
                    "return (entity.attributes.forecast || []).map("
                    "p => [new Date(p.time).getTime(), p.battery_kw]);"
                ),
            },
        ],
    }


def template() -> list[dict[str, Any]]:
    """The tab's sections with `@key` placeholders, before resolution. Also
    what the tab's design fingerprint is taken from, so it is the same on
    every install. Per block: a full-width section with its name (and, for
    Plan, the battery chart), then one section per column."""
    sections: list[dict[str, Any]] = []
    for name, columns in _BLOCKS:
        top: list[dict[str, Any]] = [
            {"type": "heading", "heading": name, "heading_style": "title"}
        ]
        if name == "Plan":
            top += [
                {**_chart(), "grid_options": {"columns": "full"}},
                {**_note(_CHART_NOTE), "grid_options": {"columns": "full"}},
            ]
        sections.append({"type": "grid", "column_span": 4, "cards": top})
        span = 4 // len(columns)
        for column in columns:
            sections.append(
                {
                    "type": "grid",
                    "column_span": span,
                    "cards": [
                        {**c, "grid_options": {"columns": "full"}}
                        for card in column
                        for c in _card(card)
                    ],
                }
            )
    return sections


def resolve(value: Any, lookup: Callable[[str], str | None]) -> Any:
    """`value` with every `@key` replaced by this install's entity_id.

    `lookup("nimbus_solver_lp_status")` returns the entity_id or None;
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
            dropped = False
            for item in node:
                # A card's note (a text-only markdown right after it) goes
                # with the card.
                if dropped and isinstance(item, dict) and item.get("text_only"):
                    continue
                item = walk(item)
                dropped = item is None
                if item is not None:
                    out.append(item)
            return out
        if not isinstance(node, dict):
            return node
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
