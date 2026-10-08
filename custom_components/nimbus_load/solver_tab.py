"""The standard Solver tab of the Nimbus dashboard (nimbus #1594).

Agreed contents (household, 6 and 8 Oct 2026): a headline, the plan against
the measured battery, the money, how well it did, the risk sliders, and every
Solver setting, grouped into **Basic settings**, **P2P (optional)** and
**Advanced settings**, each clearly labelled. Only Nimbus's own entities are
used, plus the battery sensor the Solver itself is configured with; nothing
HAEO (standing rule) and nothing a household has to fill in.

Built from ordinary Home Assistant cards (headings, tiles, entities, history
graph), so a household can move, resize or remove any of them. Home Assistant
has no native collapsible card and the standard tabs must not depend on a
third-party one, so "P2P (optional)" is its own clearly titled section.

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
    "nimbus_solver_lp_status",
    "nimbus_solver_solve_seconds",
    "nimbus_solver_current_dispatch_direction",
    "nimbus_solver_current_battery_kw",
    "nimbus_solver_current_soc_pct",
]

_MONEY = [
    "nimbus_solver_total_cost",
    "nimbus_solver_total_cost_with_fixed_costs",
    "nimbus_solver_cost_band_lower",
    "nimbus_solver_cost_band_upper",
    "nimbus_solver_cost_breakdown_grid_net",
    "nimbus_solver_cost_breakdown_degradation",
    "nimbus_solver_cost_breakdown_charge_fee",
    "nimbus_solver_cost_breakdown_discharge_fee",
    "nimbus_solver_cost_breakdown_terminal_value_credit",
    "nimbus_solver_current_import_price",
    "nimbus_solver_current_export_price",
]

_QUALITY = [
    "nimbus_quality_epr",
    "nimbus_quality_epr_reliable",
    "nimbus_quality_epr_reason",
    "nimbus_quality_regret_dollars",
    "nimbus_counterfactual_soc",
    "nimbus_solver_equivalent_full_cycles",
]

_RISK = [
    ("solver_import_price_risk_aversion", "nimbus_solver_import_price_risk_effect_now"),
    ("solver_export_price_risk_aversion", "nimbus_solver_export_price_risk_effect_now"),
    ("solver_risk_aversion", "nimbus_solver_solar_risk_effect_now_kw"),
]

_BASIC = [
    (
        "Battery",
        [
            "solver_battery_capacity_kwh",
            "solver_battery_soh_percent",
            "solver_battery_min_soc_percent",
            "solver_battery_max_soc_percent",
            "solver_max_charge_kw",
            "solver_max_discharge_kw",
            "solver_efficiency_percent",
        ],
    ),
    (
        "Grid",
        [
            "solver_grid_max_import_kw",
            "solver_grid_max_export_kw",
        ],
    ),
    (
        "Fees",
        [
            "solver_network_fee_default_rate",
            "solver_network_fee_1_start_hour",
            "solver_network_fee_1_end_hour",
            "solver_network_fee_1_rate",
            "solver_network_fee_2_start_hour",
            "solver_network_fee_2_end_hour",
            "solver_network_fee_2_rate",
            "solver_network_fee_3_start_hour",
            "solver_network_fee_3_end_hour",
            "solver_network_fee_3_rate",
            "solver_flat_fee_rate",
            "solver_fixed_daily_charge",
        ],
    ),
]

_P2P = [
    "solver_p2p_block_1_rate_kw",
    "solver_p2p_block_1_start_hour",
    "solver_p2p_block_1_end_hour",
    "solver_p2p_block_2_rate_kw",
    "solver_p2p_block_2_start_hour",
    "solver_p2p_block_2_end_hour",
    "solver_p2p_block_3_rate_kw",
    "solver_p2p_block_3_start_hour",
    "solver_p2p_block_3_end_hour",
    "solver_p2p_block_lead_time_minutes",
    "solver_p2p_bonus_price",
    "solver_p2p_bonus_volume_kwh",
    "nimbus_solver_current_bonus_price",
    "nimbus_solver_p2p_match_fraction",
    "nimbus_solver_p2p_recent_avg_volume_kwh",
]

_ADVANCED = [
    (
        "Costs",
        [
            "solver_charge_cost",
            "solver_discharge_cost",
            "solver_degradation_cost_per_kwh",
            "solver_salvage_value",
            "solver_inverter_self_consumption_kw",
        ],
    ),
    (
        "Stability",
        [
            "solver_intraplan_smoothness_weight_kw",
            "solver_proximal_weight_kw",
            "solver_battery_charge_earliness_budget_kw",
            "solver_post_window_self_consume_hours",
        ],
    ),
    (
        "Price events",
        [
            "solver_price_spike_threshold",
            "solver_price_spike_discharge_kw",
            "solver_price_spike_override_armed",
            "solver_price_event_enabled",
            "solver_aemo_p5min_disagreement_threshold_dollars",
        ],
    ),
    (
        "Features",
        [
            "solver_load_forecast_source_policy",
            "solver_auto_include_known_solar",
            "solver_calibrated_objective_enabled",
            "solver_nowcast_measurement_enabled",
            "solver_offer_curve_enabled",
            "solver_flex_signals_enabled",
            "solver_score_controllable_loads_enabled",
        ],
    ),
    (
        "Scoring thresholds",
        [
            "solver_soc_discrepancy_mean_threshold_pct",
            "solver_soc_discrepancy_max_threshold_pct",
        ],
    ),
]

_RISK_TEXT = (
    "Each slider makes the plan more cautious about one kind of forecast "
    "error. **Import**: buy less where the import price could rise. "
    "**Export**: sell less where the export price could fall. **Overall**: "
    "lean less on the solar forecast. The value beside each shows what the "
    "slider is doing to the plan right now."
)

_P2P_TEXT = (
    "Only for households with a peer-to-peer export commitment. Leave the "
    "block rates at 0 if you have none -- nothing here is needed otherwise."
)


def _section(title: str, cards: list[dict[str, Any]], span: int = 2) -> dict[str, Any]:
    return {
        "type": "grid",
        "column_span": span,
        "cards": [
            {"type": "heading", "heading": title, "heading_style": "title"},
            *cards,
        ],
    }


def _entities(keys: list[str], title: str | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {"type": "entities", "entities": [f"@{k}" for k in keys]}
    if title:
        card["title"] = title
    return card


def template() -> list[dict[str, Any]]:
    """The tab's sections with `@key` placeholders, before resolution. Also
    what the tab's design fingerprint is taken from, so it is the same on
    every install."""
    headline = [
        {"type": "tile", "entity": f"@{k}", "grid_options": {"columns": 6}}
        for k in _HEADLINE
    ] + [
        {
            "type": "tile",
            "entity": "@solver_dispatch_dry_run",
            "name": "Dry run (off = dispatching live)",
            "grid_options": {"columns": 6},
        }
    ]
    plan = {
        "type": "history-graph",
        "title": "Battery: plan against measured",
        "hours_to_show": 24,
        "entities": [
            {"entity": "@nimbus_solver_battery_forecast", "name": "Nimbus plan (now)"},
            {"entity": "@option:battery_sensor", "name": "Measured"},
        ],
    }
    risk = [
        {"type": "markdown", "content": _RISK_TEXT},
        _entities([k for pair in _RISK for k in pair]),
    ]
    return [
        _section("Solver", headline, span=4),
        _section("Plan against actual", [plan], span=4),
        _section("Money", [_entities(_MONEY)]),
        _section("How well it did", [_entities(_QUALITY)]),
        _section("Risk sliders", risk, span=4),
        _section("Basic settings", [_entities(k, t) for t, k in _BASIC], span=4),
        _section(
            "P2P (optional)",
            [{"type": "markdown", "content": _P2P_TEXT}, _entities(_P2P)],
            span=4,
        ),
        _section("Advanced settings", [_entities(k, t) for t, k in _ADVANCED], span=4),
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
        if isinstance(node.get("entities"), list):
            node["entities"] = [
                e for e in (one(x) for x in node["entities"]) if e is not None
            ]
            if not node["entities"]:
                return None
        return node

    return walk(value)
