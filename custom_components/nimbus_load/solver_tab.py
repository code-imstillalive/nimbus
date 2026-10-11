"""The standard Solver tab of the Nimbus dashboard (nimbus #1594).

Agreed contents (household, 6 and 8 Oct 2026): a headline, the plan against
the measured battery, the money, how well it did, the risk sliders, and every
Solver setting, labelled Basic, P2P or Advanced.

Layout (household, 11 Oct 2026, after the first version shipped as raw entity
lists with truncated names and large empty areas): every row has a short
plain name; each functional group (Battery and grid, Fees, P2P blocks, Costs,
...) is ONE section with its own heading, so Home Assistant reflows whole
groups by screen width -- four across on a PC, two on a tablet, one on a
phone -- and a group is never split. Sliders are real sliders, each with its
live effect on the same row. A fee or P2P block shows its hours only once its
rate is set.

Built from ordinary Home Assistant cards (headings, tiles, entities, history
graph, markdown) and no third-party card, so it works on every install and a
household can move, resize or remove any of it.

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

_HEADLINE = [
    ("nimbus_solver_lp_status", "Plan"),
    ("nimbus_solver_solve_seconds", "Solve time"),
    ("nimbus_solver_current_dispatch_direction", "Doing now"),
    ("nimbus_solver_current_battery_kw", "Battery"),
    ("nimbus_solver_current_soc_pct", "State of charge"),
    ("solver_dispatch_dry_run", "Dry run"),
]

_MONEY = [
    ("nimbus_solver_total_cost", "Plan cost"),
    ("nimbus_solver_total_cost_with_fixed_costs", "Plan cost with fixed charges"),
    ("nimbus_solver_cost_band_lower", "Likely range, low"),
    ("nimbus_solver_cost_band_upper", "Likely range, high"),
    ("nimbus_solver_current_import_price", "Buy price now"),
    ("nimbus_solver_current_export_price", "Sell price now"),
]

_MONEY_BREAKDOWN = [
    ("nimbus_solver_cost_breakdown_grid_net", "Grid, net"),
    ("nimbus_solver_cost_breakdown_charge_fee", "Charging cost"),
    ("nimbus_solver_cost_breakdown_discharge_fee", "Discharging cost"),
    ("nimbus_solver_cost_breakdown_degradation", "Battery wear"),
    ("nimbus_solver_cost_breakdown_terminal_value_credit", "Value left at plan end"),
]

_QUALITY = [
    ("nimbus_quality_epr", "EPR, latest day"),
    ("nimbus_quality_regret_dollars", "Regret, latest day"),
    ("nimbus_quality_epr_reliable", "Score reliable"),
    ("nimbus_counterfactual_soc", "Nimbus-only SoC at 5 pm"),
    ("nimbus_solver_equivalent_full_cycles", "Battery cycles in plan"),
]

_RISK = [
    (
        "solver_import_price_risk_aversion",
        "Import price",
        "nimbus_solver_import_price_risk_effect_now",
    ),
    (
        "solver_export_price_risk_aversion",
        "Export price",
        "nimbus_solver_export_price_risk_effect_now",
    ),
    (
        "solver_risk_aversion",
        "Solar forecast",
        "nimbus_solver_solar_risk_effect_now_kw",
    ),
]

# Each settings card is a list of (heading or None, rows). A row is
# (key, name) or ("block", key_rate, key_start, key_end, label): the block's
# hours show only while its rate is set, so an unused block is one line.
_BASIC = [
    [
        (
            "Battery",
            [
                ("solver_battery_capacity_kwh", "Capacity"),
                ("solver_battery_soh_percent", "State of health"),
                ("solver_battery_min_soc_percent", "Minimum SoC"),
                ("solver_battery_max_soc_percent", "Maximum SoC"),
                ("solver_max_charge_kw", "Max charge power"),
                ("solver_max_discharge_kw", "Max discharge power"),
                ("solver_efficiency_percent", "Round-trip efficiency"),
            ],
        ),
        (
            "Grid",
            [
                ("solver_grid_max_import_kw", "Max import"),
                ("solver_grid_max_export_kw", "Max export"),
            ],
        ),
    ],
    [
        (
            "Fees",
            [
                ("solver_network_fee_default_rate", "Network fee, default"),
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
        ),
    ],
]

_P2P = [
    [
        (
            "Committed blocks",
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
            ],
        ),
    ],
    [
        (
            "Bonus and matching",
            [
                ("solver_p2p_bonus_price", "Bonus price"),
                ("solver_p2p_bonus_volume_kwh", "Bonus volume"),
                ("nimbus_solver_current_bonus_price", "Bonus price now"),
                ("nimbus_solver_p2p_match_fraction", "Matched share"),
                ("nimbus_solver_p2p_recent_avg_volume_kwh", "Recent matched volume"),
            ],
        ),
    ],
]

_ADVANCED = [
    [
        (
            "Costs",
            [
                ("solver_charge_cost", "Charge cost"),
                ("solver_discharge_cost", "Discharge cost"),
                ("solver_degradation_cost_per_kwh", "Battery wear cost"),
                ("solver_salvage_value", "Value left at plan end"),
                ("solver_inverter_self_consumption_kw", "Inverter self-use"),
            ],
        ),
        (
            "Stability",
            [
                ("solver_intraplan_smoothness_weight_kw", "Plan smoothness"),
                ("solver_proximal_weight_kw", "Stick to the last plan"),
                ("solver_battery_charge_earliness_budget_kw", "Charge earliness"),
                ("solver_post_window_self_consume_hours", "Self-consume after a block"),
            ],
        ),
        (
            "Battery reserve",
            [
                ("solver_battery_reserve_percent", "Reserve"),
                ("solver_battery_reserve_release_price", "Release margin"),
                ("solver_battery_reserve_start_hour", "Hold from"),
                ("solver_battery_reserve_end_hour", "Release at"),
            ],
        ),
    ],
    [
        (
            "Price events",
            [
                ("solver_price_event_enabled", "Price events"),
                ("solver_price_spike_threshold", "Spike threshold"),
                ("solver_price_spike_discharge_kw", "Spike discharge power"),
                ("solver_price_spike_override_armed", "Spike override armed"),
                (
                    "solver_aemo_p5min_disagreement_threshold_dollars",
                    "AEMO disagreement alert",
                ),
            ],
        ),
        (
            "Features",
            [
                ("solver_load_forecast_source_policy", "Load forecast source"),
                ("solver_auto_include_known_solar", "Auto-include solar integrations"),
                ("solver_calibrated_objective_enabled", "Calibrated objective"),
                ("solver_nowcast_measurement_enabled", "Nowcast measurement"),
                ("solver_offer_curve_enabled", "Offer curve"),
                ("solver_flex_signals_enabled", "Flex signals"),
                ("solver_score_controllable_loads_enabled", "Score controllable loads"),
            ],
        ),
        (
            "Scoring thresholds",
            [
                ("solver_soc_discrepancy_mean_threshold_pct", "SoC discrepancy, mean"),
                ("solver_soc_discrepancy_max_threshold_pct", "SoC discrepancy, max"),
            ],
        ),
    ],
]

_RISK_TEXT = (
    "Each slider makes the plan more cautious about one kind of forecast "
    "error -- **import**: buy less where the import price could rise; "
    "**export**: sell less where the export price could fall; **solar**: lean "
    "less on the solar forecast. Underneath each is what it is doing to the "
    "plan right now."
)

_P2P_TEXT = (
    "Only for a peer-to-peer export commitment. Leave the block rates at 0 if "
    "you have none; a block's hours appear once its rate is set."
)

# A block's rate reads "0.0" (or "0") while unused.
_UNUSED = ("0", "0.0")


def _section(title: str, cards: list[dict[str, Any]], span: int = 4) -> dict[str, Any]:
    return {
        "type": "grid",
        "column_span": span,
        "cards": [
            {"type": "heading", "heading": title, "heading_style": "title"},
            *cards,
        ],
    }


def _row(key: str, name: str) -> dict[str, Any]:
    return {"entity": f"@{key}", "name": name}


def _rows(groups: list[tuple[str | None, list[tuple]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for heading, rows in groups:
        if heading:
            out.append({"type": "section", "label": heading})
        for row in rows:
            if row[0] == "block":
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
            else:
                out.append(_row(*row))
    return out


def _group(title: str, groups: list[tuple], span: int = 1) -> dict[str, Any]:
    """One functional group as one section: its heading and its card travel
    together, so Home Assistant reflows whole groups by screen width (four
    across on a PC, two on a tablet, one on a phone) and never splits one."""
    return _section(
        title,
        [
            {
                "type": "entities",
                "entities": _rows(groups),
                "grid_options": {"columns": "full"},
            }
        ],
        span=span,
    )


def template() -> list[dict[str, Any]]:
    """The tab's sections with `@key` placeholders, before resolution. Also
    what the tab's design fingerprint is taken from, so it is the same on
    every install.

    Order is the reading order on a phone, where every section stacks:
    what it is doing now, the plan against reality, money and outcome, the
    risk sliders, then the settings group by group -- Basic, P2P, Advanced."""
    headline = [
        {
            "type": "tile",
            "entity": f"@{key}",
            "name": name,
            "grid_options": {"columns": 4},
        }
        for key, name in _HEADLINE
    ]
    plan = {
        "type": "history-graph",
        "title": "Battery: plan against measured",
        "hours_to_show": 24,
        "grid_options": {"columns": "full", "rows": 5},
        "entities": [
            {"entity": "@nimbus_solver_battery_forecast", "name": "Nimbus plan (now)"},
            {"entity": "@option:battery_sensor", "name": "Measured"},
        ],
    }
    money = _rows([(None, _MONEY), ("Where it comes from", _MONEY_BREAKDOWN)])
    risk: list[dict[str, Any]] = [
        {"type": "markdown", "content": _RISK_TEXT, "grid_options": {"columns": "full"}}
    ]
    for key, name, effect in _RISK:
        # Each slider and its effect share a row on every screen size.
        risk.append(
            {
                "type": "tile",
                "entity": f"@{key}",
                "name": name,
                "features": [{"type": "numeric-input", "style": "slider"}],
                "grid_options": {"columns": 8},
            }
        )
        risk.append(
            {
                "type": "tile",
                "entity": f"@{effect}",
                "name": "Effect now",
                "grid_options": {"columns": 4},
            }
        )
    battery, fees = _BASIC
    blocks, bonus = _P2P
    (costs, stability, reserve), (events, features, scoring) = _ADVANCED
    return [
        _section("Solver", headline),
        _section("Plan against actual", [plan]),
        _section(
            "Money",
            [
                {
                    "type": "entities",
                    "entities": money,
                    "grid_options": {"columns": "full"},
                }
            ],
            span=2,
        ),
        _section(
            "How well it did",
            [
                {
                    "type": "entities",
                    "entities": [_row(k, n) for k, n in _QUALITY],
                    "grid_options": {"columns": "full"},
                }
            ],
            span=2,
        ),
        _section("Risk sliders", risk),
        _group("Basic · Battery and grid", battery),
        _group("Basic · Fees", [(None, fees[0][1])]),
        _section(
            "P2P · Committed blocks",
            [
                {
                    "type": "markdown",
                    "content": _P2P_TEXT,
                    "grid_options": {"columns": "full"},
                },
                {
                    "type": "entities",
                    "entities": _rows([(None, blocks[0][1])]),
                    "grid_options": {"columns": "full"},
                },
            ],
            span=1,
        ),
        _group("P2P · Bonus and matching", [(None, bonus[0][1])]),
        _group("Advanced · Costs", [(None, costs[1])]),
        _group("Advanced · Stability", [(None, stability[1])]),
        _group("Advanced · Battery reserve", [(None, reserve[1])]),
        _group("Advanced · Price events", [(None, events[1])]),
        _group("Advanced · Features", [(None, features[1])]),
        _group("Advanced · Scoring thresholds", [(None, scoring[1])]),
    ]


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
            for item in node:
                item = walk(item)
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

    return walk(value)
