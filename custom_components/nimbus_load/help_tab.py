"""The Help tab (nimbus #1823): set Nimbus up so every tab works, and what
every figure and setting means.

Household, 17 Oct 2026: "a new tab - install steps and glossary... where it
is all explained live view by view and all the wizard steps too", a link to
report issues on GitHub, every setting "short and concise but very
comprehensive" with its formula and an example, and "make very very clear
that BASIC INSTALL WILL LEAVE SOME FEATURES BLANK". A tester (#1526)
finished the basic setup and was left with an empty Topology and no
Forecaster chart: the guide described outcomes without the steps that reach
them. So the tab is organised by what each TAB needs, each with a live tick
for this install, not by wizard screen.

Built like the Solver tab (`solver_tab.template()`): sections with `@key`
placeholders that `solver_tab.resolve()` binds to this install's entities.
The wizard reference is generated from `strings.json`, the text the wizard
itself shows, so the two cannot drift; every Solver-tab setting has an
entry here, and `tests/test_1823_help_tab.py` fails if one is missing.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

try:
    from . import solver_tab
except ImportError:  # pragma: no cover - loaded as a top-level module in tests
    import solver_tab  # type: ignore[no-redef]

ISSUES_URL = "https://github.com/code-imstillalive/nimbus/issues"
NEW_ISSUE_URL = f"{ISSUES_URL}/new"

# Live values used below. Each card names at least one entity every install
# with a Solver has, so `resolve()` keeps the card; an unresolved `${@...}`
# renders empty and the tick reads "not set".
_PLAN = "${@nimbus_solver_battery_forecast}"
_QUALITY = "${@nimbus_solver_quality_report}"

# ---- start here -------------------------------------------------------------

_BASIC_WARNING = (
    '<ha-alert alert-type="error" title="A basic install leaves some tabs blank">'
    "The basic setup (the Solver wizard) makes Nimbus plan your battery. "
    "<b>The Forecaster, Topology, Regret and Stochastic views need the extra "
    "steps below</b>; until they are done those tabs show little or nothing. "
    "That is expected, not a fault. Work down the table until every row is "
    "ticked.</ha-alert>"
)

_WHERE = (
    "**Where things are**\n\n"
    "| Place | How to get there | What is there |\n|:---|:---|:---|\n"
    "| **Integration page** | Settings → Devices & services → **Nimbus** | "
    "the **+ Add** buttons: power signal, load, power source, PV string, "
    "battery tower, controllable load, battery participant |\n"
    "| **Configure menu** | Integration page → gear icon (Configure) on the "
    "Nimbus hub | **Forecaster settings** (1 screen) · **Solver settings** "
    "(3 screens: Battery, Grid Prices, Solar & Load Forecasts) · "
    "**Topology diagram settings** (1 screen) |\n"
    "| **Solver tab** | this dashboard | blocks: Performance · Money · Plan · "
    "Basic settings · Advanced settings · Stochastic |\n"
    "| **Repairs** | Settings → Repairs | setup gaps, each with a Fix button |\n"
    "| **Diagnostics** | Integration page → ⋮ → Download diagnostics | "
    "everything a bug report needs |"
)

_TICK = "{{ '✅' if %s else '⬜' }}"

_START_HERE = (
    _BASIC_WARNING
    + "\n\n"
    + f"{{% set plan = states.{_PLAN} %}}"
    + f"{{% set q = states.{_QUALITY} %}}"
    + "{% set stoch = (plan.attributes.get('stochastic') or {}) if plan else {} %}"
    + "{% set forecasts = integration_entities('nimbus_load') "
    "| select('search', '^sensor\\\\.nimbus_.*_forecast$') "
    "| reject('search', 'solver|household_load_total|p5min') | list %}"
    + "{% set real_sources = (state_attr('sensor.nimbus_topology_config', "
    "'power_sources') or []) | rejectattr('derived', 'defined') | list %}"
    + "\n| | Step | For | How |\n|:---:|:---|:---|:---|\n"
    + "| "
    + _TICK % "plan and plan.attributes.get('status') == 'optimal'"
    + " | **1. Solver settings** (basic) | Solver, Control Panel | "
    "Integration page → **Configure → Solver settings → screens 1-3** "
    "(Battery, Grid Prices, Solar & Load Forecasts); fill every 🔴 field. "
    "Each screen is under Setup wizard, below. |\n"
    + "| "
    + _TICK
    % "plan and (plan.attributes.get('solve_diagnostics') or {}).get("
    "'battery_effective_capacity_kwh', 0) > 1"
    + " | **2. Your battery and grid numbers** (basic) | Solver | "
    "**Solver tab → Basic settings → Battery card** (capacity, SoH, min / max "
    "SoC, max charge / discharge) and **→ Grid and fees card** (max import / "
    "export). The defaults are placeholders. |\n"
    + "| "
    + _TICK % "forecasts | count > 0"
    + " | **3. A whole-house Power Signal**, then Loads | Forecaster, Topology | "
    "Nimbus integration page → **+ Add power signal** (your measured "
    "whole-house load), then **+ Add load** per circuit you want learned. "
    "Needs **ApexCharts Card** (HACS → Frontend). |\n"
    + "| "
    + _TICK % "real_sources | count > 0"
    + " | **4. Topology entries** | Topology | Nimbus integration page → "
    "**+ Add power source** (one per inverter), then **+ Add PV string** and "
    "**+ Add battery tower** under it; Configure → **Topology diagram "
    "settings** for the daily kWh figures. Loads appear by themselves. |\n"
    + "| "
    + _TICK % "q and q.attributes.get('latest_date')"
    + " | **5. One full day scored** | Regret, Performance | Nothing to do: "
    "scored each morning at 06:17 from yesterday's real data. |\n"
    + "| "
    + _TICK % "stoch.get('solar_band') == 'calibrated'"
    + " | **6. Solar band calibrated** | Stochastic | Nothing to do: needs "
    "some days of solar history; until then it says why it is waiting. |\n"
    + "\n{% if plan %}Nimbus {{ plan.attributes.get('nimbus_version') }} · "
    "plan {{ plan.attributes.get('status') }}{% endif %}"
)

_TRAPS = (
    "**Common traps**\n\n"
    "- **Never point a Power Signal at a plan or forecast entity** (Nimbus's "
    "own, HAEO's, EMHASS's). Use measured sensors only, or the model learns "
    "from its own predictions.\n"
    "- **`sensor.nimbus_household_load_total_forecast` is an output.** Never "
    "select it as an input.\n"
    "- **Units:** a power sensor in W is converted; an energy sensor (kWh) in "
    "a power field is not. Pick the kW / W sensor.\n"
    "- **Battery numbers live on the Solver tab**, not in the wizard: the "
    "wizard only asks which SoC sensor to read.\n"
    "- **A Topology diagram with only the basic setup** shows a stand-in "
    '"Nimbus" inverter from the Solver\'s sensors. Add Power Sources (step 4) '
    "to draw your real system.\n"
    "- **Setup gaps are raised as Repairs** (Settings → Repairs) with a Fix "
    "button: check there first."
)

_REPORT = (
    '<ha-alert alert-type="info" title="Report a problem or ask a question">'
    f'<a href="{ISSUES_URL}" target="_blank">Search existing issues</a> · '
    f'<a href="{NEW_ISSUE_URL}" target="_blank">Open a new issue on GitHub</a>'
    "</ha-alert>\n\n"
    "Include:\n\n"
    "1. Your Nimbus version: "
    f"**{{{{ state_attr('{_PLAN}', 'nimbus_version') }}}}** "
    "and Home Assistant's (Settings → About).\n"
    "2. **Diagnostics:** Settings → Devices & services → Nimbus → ⋮ → "
    "**Download diagnostics**, attached to the issue.\n"
    "3. What you expected, what you saw, and when (a screenshot helps).\n"
    "4. Never paste tokens, passwords or your address."
)

# ---- what's new -------------------------------------------------------------

RELEASES_URL = "https://github.com/code-imstillalive/nimbus/releases"

#: (version, date, highlights), newest first. HACS installs only this folder,
#: not the repo's CHANGELOG, so the notes live here. Every release adds its
#: entry: tests/test_1823_help_tab.py fails when manifest.json's version is
#: not listed.
WHATS_NEW: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "0.94.463",
        "18 Oct 2026",
        (
            (
                "**Repairs for a battery still on its placeholder settings,** "
                "or with Min, Max or Charge By SoC that contradict each other."
            ),
            "**A load forecast's lower bound** no longer dips below zero.",
            (
                "**Comparison price sources:** list extra price sensors to record "
                "for scoring how prices should be combined; the plan never uses them."
            ),
        ),
    ),
    (
        "0.94.462",
        "17 Oct 2026",
        (
            (
                "**This Help tab:** what each tab needs, where every setting is "
                "and what it means."
            ),
            "**Stochastic settings explained** on their card.",
            (
                "**A stale price source no longer counts in the daily score:** "
                "its periods are re-priced from AEMO plus your learned markup."
            ),
        ),
    ),
    (
        "0.94.461",
        "17 Oct 2026",
        (
            (
                "**Stochastic dispatch, in shadow:** plans for a low, middle and high "
                "solar day at once; its own block at the end of the Solver tab. "
                "Switch on **Drive the battery** when you choose."
            ),
            "**Calibrated solar p10 / p90** from your own history.",
            "**An unreachable EV departure target** no longer stops the whole plan.",
            "**One SoC line per battery** on multi-battery sites.",
            (
                "**Solver tab:** the quality card once, no scrollbar; days 3 and 4 of "
                "the plan."
            ),
        ),
    ),
    (
        "0.94.460",
        "16 Oct 2026",
        (
            (
                "**Price safety row** on the Solver tab: the false-spike guard, the "
                "price source's freshness, the battery hold and the price mode."
            ),
            "**Charge By holds its target** for the whole final hour.",
        ),
    ),
    (
        "0.94.459",
        "16 Oct 2026",
        (
            (
                "**A stale price source is no longer planned on**; AEMO plus your "
                "learned markup stands in."
            ),
            (
                "**Consolidated price forecast** and each source's running error, in "
                "shadow."
            ),
        ),
    ),
    (
        "0.94.458",
        "16 Oct 2026",
        (
            (
                "**P5MIN false-spike guard:** a spike from an infeasible AEMO run is "
                "not planned on."
            ),
            "**The plan says when the battery should hold.**",
            (
                "**Solar running low:** when measured PV falls well below forecast, "
                "the rest of today is planned on the low case."
            ),
        ),
    ),
)


def whats_new_markdown() -> str:
    lines = [
        "**What's new**",
        "",
        (
            f"You are on **{{{{ state_attr('{_PLAN}', 'nimbus_version') }}}}**. "
            f'<a href="{RELEASES_URL}" target="_blank">Every release, in full</a>.'
        ),
        "",
    ]
    for version, day, items in WHATS_NEW:
        lines.append(f"**{version}** · {day}")
        lines += [f"- {item}" for item in items]
        lines.append("")
    return "\n".join(lines).rstrip()


# ---- each tab ---------------------------------------------------------------

_TABS = (
    "**Each tab, and how to read it**\n\n"
    "| Tab | What it shows | Blank until |\n|:---|:---|:---|\n"
    "| **Forecaster** | Each Power Signal's measured history, forecast and "
    "band, and each Load's forecast; the Solver's planned battery, grid and SoC "
    "on top. | Step 3, plus ApexCharts Card, plus the first nightly retrain. |\n"
    "| **Solver** | *Performance:* how yesterday scored and what the plan ahead "
    "should earn. *Money:* the figures behind it. *Plan:* the battery plan "
    "against measured, prices ahead, the price guards, the whole plan table and "
    "each day's totals. *Settings:* every dial. *Stochastic:* the plan for a "
    "low, middle and high solar day. | Steps 1 and 2 (Performance: step 5; "
    "Stochastic: step 6). |\n"
    "| **Topology** | Your switchboard: inverters, PV strings, battery towers, "
    "loads and the grid, with live flow arrows. | Step 4 (a stand-in until then). |\n"
    "| **Control Panel** | Mode and kill switch, SoC gauge with the reason, risk "
    "sliders, the 96 h plan against actual, and economics. | Step 1; the actual "
    "line needs the battery power sensor (Configure → Forecaster settings). |\n"
    "| **Regret** | A scored day replayed: doing nothing, what happened, and "
    "perfect hindsight, with regret by hour. | Step 5. |\n"
    "| **Help** | This page. | Never. |"
)

# ---- glossary ---------------------------------------------------------------

#: (term, meaning, formula or example). Kept short; the formula is the detail.
GLOSSARY: tuple[tuple[str, str, str], ...] = (
    (
        "Plan",
        "The Solver's battery, grid and load schedule for the next ~4 days, re-solved every 5 minutes.",
        "Only the first minutes are acted on; the rest is re-planned as data changes.",
    ),
    (
        "Period",
        "One step of the plan: 5 minutes for the first hour, then 30 minutes.",
        "96 h ≈ 12 × 5 min + 190 × 30 min.",
    ),
    (
        "SoC",
        "State of charge: how full the battery is.",
        "kWh = capacity × SoH × SoC %.",
    ),
    (
        "EPR",
        "Economic performance ratio: share of the possible saving the real day captured.",
        "(J_ref − J_ach) ÷ (J_ref − J*). 16 Oct: (2.21 + 19.96) ÷ (2.21 + 48.88) = 43.4%.",
    ),
    (
        "J_ref",
        "The day's cost if the battery had done nothing.",
        "Imports × buy price − exports × sell price, battery idle.",
    ),
    (
        "J_ach",
        "The day's real cost, from what the battery actually did.",
        "Same formula on measured flows.",
    ),
    (
        "J* (perfect foresight)",
        "The best possible cost for the day, planned knowing every real price, load and solar value.",
        "An oracle; no plan can beat it.",
    ),
    ("Regret", "Money the real day left on the table.", "J_ach − J*. $0 = perfect."),
    (
        "Value captured",
        "What the battery earned or saved against doing nothing.",
        "J_ref − J_ach.",
    ),
    (
        "Best possible",
        "What perfect foresight would have earned against doing nothing.",
        "J_ref − J*.",
    ),
    (
        "Grid money",
        "What the plan pays the grid, after exports and P2P.",
        "Σ hours × (import × buy − export × sell − P2P × bonus). Negative = earns.",
    ),
    (
        "Shadow price",
        "What one more kWh stored is worth to the plan at that moment.",
        "From the LP; high = the plan wants energy then.",
    ),
    (
        "p10 / p50 / p90",
        "Low, middle and high solar: the solar falls below p10 about one day in ten.",
        "Calibrated from your own solar history by lead time.",
    ),
    (
        "Risk aversion",
        "How far a forecast is leaned toward its bad case before planning.",
        "Solar used = p50 − r × (p50 − low). r = 0.25, p50 6 kW, low 2 kW → 5 kW.",
    ),
    (
        "Shadow mode",
        "A feature computes and shows its answer but does not act.",
        "Stochastic is in shadow until Drive the battery is on.",
    ),
    (
        "Drive",
        "The stochastic plan runs the battery.",
        "Falls back to the main plan on any cycle it cannot be trusted, and says why.",
    ),
    (
        "Commit window",
        "The stretch whose decisions must suit all three solar days.",
        "30 min = the first six 5-minute periods.",
    ),
    (
        "Expected (stochastic)",
        "The weighted average over the three solar days.",
        "0.3 × low + 0.4 × middle + 0.3 × high.",
    ),
    (
        "P2P block",
        "A window when you have agreed to sell a set kW to peers.",
        "Earns sell price + bonus price on the matched volume.",
    ),
    (
        "Charge By",
        "Be at least the target SoC by a time, and stay there for the final hour.",
        "80% by 07:00 → planned to reach 80% by 06:00 at the latest.",
    ),
    (
        "Reserve",
        "The bottom share of the battery, sold only above a release margin.",
        "10% reserve, margin $0.20 → that 10% exports only when the price beats the plan's value by $0.20/kWh.",
    ),
    (
        "Salvage value",
        "What each kWh left at the plan's end is worth.",
        "Credit = salvage × kWh left. $0.15 × 50 kWh = $7.50.",
    ),
    (
        "Effective inputs",
        "The solar and prices the plan counted on after the risk dials.",
        "Recorded on each plan row for replays.",
    ),
    (
        "Repair",
        "A Home Assistant notice (Settings → Repairs) for a setup gap, with a Fix button.",
        "",
    ),
)

# ---- every Solver-tab row ---------------------------------------------------

#: entity key -> (what it is, formula or example). Every entity row on the
#: Solver tab must have one (tests/test_1823_help_tab.py).
SETTINGS: dict[str, tuple[str, str]] = {
    # Last day, scored
    "nimbus_quality_epr": (
        "Yesterday's EPR (see Glossary).",
        "(J_ref − J_ach) ÷ (J_ref − J*).",
    ),
    "nimbus_quality_regret_dollars": (
        "Money yesterday left on the table.",
        "J_ach − J*.",
    ),
    "nimbus_quality_value_captured": (
        "What the battery earned or saved yesterday against doing nothing.",
        "J_ref − J_ach.",
    ),
    "nimbus_quality_theoretical_maximum_yield": (
        "What perfect foresight would have earned.",
        "J_ref − J*.",
    ),
    "nimbus_quality_achieved_energy_in_kwh": (
        "Energy that went into the battery yesterday, measured.",
        "kWh.",
    ),
    "nimbus_quality_achieved_energy_out_kwh": (
        "Energy that came out of the battery yesterday, measured.",
        "kWh; in ÷ out shows the real round trip.",
    ),
    # Money, plan ahead
    "nimbus_solver_total_cost": (
        "The whole plan ahead (~4 days), in dollars; below zero earns.",
        "Grid net + charging + discharging + battery wear + value left.",
    ),
    "nimbus_solver_total_cost_with_fixed_costs": (
        "Plan cost plus your fixed daily charges.",
        "Plan cost + days × fixed daily charge.",
    ),
    "nimbus_solver_cost_band_lower": (
        "Likely best case for the plan's cost.",
        "Re-priced at the favourable edge of the forecast bands.",
    ),
    "nimbus_solver_cost_band_upper": (
        "Likely worst case.",
        "Re-priced at the unfavourable edge.",
    ),
    "nimbus_solver_current_import_price": (
        "What a kWh bought now costs, fees included.",
        "$/kWh.",
    ),
    "nimbus_solver_current_export_price": ("What a kWh sold now earns.", "$/kWh."),
    # Where the cost comes from
    "nimbus_solver_cost_breakdown_grid_net": (
        "Grid imports paid minus exports and P2P earned.",
        "See Grid money in the Glossary.",
    ),
    "nimbus_solver_cost_breakdown_charge_fee": (
        "Charge cost × kWh charged.",
        "$0.01 × 100 kWh = $1.",
    ),
    "nimbus_solver_cost_breakdown_discharge_fee": (
        "Discharge cost × kWh discharged.",
        "",
    ),
    "nimbus_solver_cost_breakdown_degradation": (
        "Battery wear cost × kWh through the battery, both ways.",
        "",
    ),
    "nimbus_solver_cost_breakdown_terminal_value_credit": (
        "Credit for energy left at the plan's end (negative = a credit).",
        "−salvage × kWh left.",
    ),
    # Battery, now
    "nimbus_solver_current_dispatch_direction": (
        "What the plan has the battery doing this period.",
        "Charge, discharge, self-consume or hold.",
    ),
    "nimbus_solver_current_battery_kw": (
        "Planned battery power now.",
        "+ discharging, − charging, kW.",
    ),
    "nimbus_solver_current_soc_pct": (
        "Planned state of charge now (the fleet, on a multi-battery site).",
        "%.",
    ),
    "nimbus_solver_equivalent_full_cycles": (
        "How hard the plan works the battery.",
        "kWh discharged over the plan ÷ usable capacity.",
    ),
    "nimbus_solver_lp_status": (
        "Whether the last solve succeeded.",
        "optimal = good; anything else, see its reason.",
    ),
    "nimbus_solver_solve_seconds": (
        "How long the last solve took.",
        "Seconds; under ~10 s is normal.",
    ),
    # Battery (basic)
    "solver_battery_capacity_kwh": (
        "Nameplate usable capacity.",
        "kWh. Set your real value; the default is a placeholder.",
    ),
    "solver_battery_soh_percent": (
        "State of health: how much of the capacity is left.",
        "Usable = capacity × SoH. 122 kWh × 98% = 119.6 kWh.",
    ),
    "solver_battery_min_soc_percent": (
        "The plan never goes below this.",
        "% of usable capacity.",
    ),
    "solver_battery_max_soc_percent": (
        "The plan never goes above this.",
        "% of usable capacity.",
    ),
    "solver_charge_by_hour": (
        "Charge By: the time the target must be reached.",
        "Held for the whole final hour; blank = off.",
    ),
    "solver_charge_by_percent": (
        "Charge By: the target.",
        "80% by 07:00 → at least 80% from 06:00. 0 = off.",
    ),
    "solver_bank_solar_first": (
        "Bank solar first: solar the battery can take goes into it before any is sold.",
        (
            "Off, the plan may sell solar now and refill later when power is "
            "forecast cheaper. On: charge ≥ min(solar − load, max charge, room "
            "to Max SoC) this period. A price spike or a P2P block still sells."
        ),
    ),
    "solver_max_charge_kw": ("Your inverter's charging limit.", "kW, AC side."),
    "solver_max_discharge_kw": ("Your inverter's discharging limit.", "kW, AC side."),
    "solver_efficiency_percent": (
        "Round-trip efficiency.",
        "Each leg √η: 95% → 97.5% in, 97.5% out; 10 kWh in returns 9.5 kWh.",
    ),
    "solver_inverter_self_consumption_kw": (
        "Power the inverter uses itself, always on.",
        "Added to load every period. 0.05 kW = 1.2 kWh/day.",
    ),
    # Grid and fees
    "solver_grid_max_import_kw": ("Most the connection may import.", "kW."),
    "solver_grid_max_export_kw": (
        "Most the connection may export.",
        "kW; your export limit.",
    ),
    "solver_network_fee_default_rate": (
        "Network fee on every imported kWh, outside the blocks.",
        "$/kWh added to the buy price.",
    ),
    "solver_network_fee_1_rate": (
        "Fee block 1: replaces the default fee inside its hours.",
        "$/kWh; 0 = unused.",
    ),
    "solver_network_fee_1_start_hour": ("Fee block 1 starts.", "Local time."),
    "solver_network_fee_1_end_hour": ("Fee block 1 ends.", "Local time."),
    "solver_network_fee_2_rate": ("Fee block 2, as block 1.", "$/kWh; 0 = unused."),
    "solver_network_fee_2_start_hour": ("Fee block 2 starts.", ""),
    "solver_network_fee_2_end_hour": ("Fee block 2 ends.", ""),
    "solver_network_fee_3_rate": ("Fee block 3, as block 1.", "$/kWh; 0 = unused."),
    "solver_network_fee_3_start_hour": ("Fee block 3 starts.", ""),
    "solver_network_fee_3_end_hour": ("Fee block 3 ends.", ""),
    "solver_flat_fee_rate": (
        "A flat add-on to every imported kWh at any hour (e.g. certificates).",
        "$/kWh. Buy = price + network fee + flat fee.",
    ),
    "solver_fixed_daily_charge": (
        "Your daily supply charge.",
        "$/day; shown in With fixed charges, does not change the plan.",
    ),
    # P2P
    "solver_p2p_block_1_rate_kw": (
        "Block 1: the kW you sell to peers in its hours.",
        "0 = unused.",
    ),
    "solver_p2p_block_2_rate_kw": ("Block 2, as block 1.", ""),
    "solver_p2p_block_3_rate_kw": ("Block 3, as block 1.", ""),
    "solver_p2p_block_lead_time_minutes": (
        "Starts each block this many minutes early.",
        "Covers a ramp before the block opens.",
    ),
    "solver_p2p_bonus_price": (
        "Paid above the sell price on P2P volume.",
        "Earns (sell + bonus) × matched kWh.",
    ),
    "solver_p2p_bonus_volume_kwh": ("How much a day earns the bonus.", "kWh."),
    "nimbus_solver_current_bonus_price": ("The bonus in force now.", "$/kWh."),
    "solver_post_window_self_consume_hours": (
        "After a P2P block closes at midnight, the battery only self-consumes this long.",
        "Hours; a hard rule, no export.",
    ),
    # P2P hours and matching
    "solver_p2p_block_1_start_hour": ("Block 1 opens.", "Local time."),
    "solver_p2p_block_1_end_hour": (
        "Block 1 closes.",
        "A block ending at midnight shows 00:00.",
    ),
    "solver_p2p_block_2_start_hour": ("Block 2 opens.", ""),
    "solver_p2p_block_2_end_hour": ("Block 2 closes.", ""),
    "solver_p2p_block_3_start_hour": ("Block 3 opens.", ""),
    "solver_p2p_block_3_end_hour": ("Block 3 closes.", ""),
    "nimbus_solver_p2p_match_fraction": (
        "Share of what you offer that peers recently bought.",
        "0-1; 0.8 of 12 kW → 9.6 kW matched.",
    ),
    "nimbus_solver_p2p_recent_avg_volume_kwh": (
        "Average kWh sold to peers lately.",
        "kWh per block.",
    ),
    # Costs and stability
    "solver_charge_cost": (
        "A cost per kWh charged, so small trades are not worth it.",
        "$/kWh; $0.01 ignores spreads under 1c.",
    ),
    "solver_discharge_cost": ("A cost per kWh discharged.", "$/kWh."),
    "solver_degradation_cost_per_kwh": (
        "Battery wear, per kWh through it either way.",
        "$/kWh; battery cost ÷ lifetime kWh. $15,000 ÷ 300 MWh = $0.05.",
    ),
    "solver_salvage_value": (
        "What each kWh left at the plan's end is worth.",
        "Credit = salvage × kWh left. Higher keeps more in the battery.",
    ),
    "solver_intraplan_smoothness_weight_kw": (
        "Penalty on battery power changing between periods.",
        "$ per kW of change; higher = smoother plans.",
    ),
    "solver_proximal_weight_kw": (
        "Penalty on changing the plan from the last solve.",
        "$ per kW moved; higher = steadier plans.",
    ),
    "solver_battery_charge_earliness_budget_kw": (
        "Tie-break toward charging earlier when two plans cost the same.",
        "Tiny by design (0.005).",
    ),
    "solver_aemo_p5min_disagreement_threshold_dollars": (
        "Flags when your retail price departs from AEMO's live price by more than usual.",
        "$/kWh above your typical markup.",
    ),
    "solver_p5min_false_spike_threshold_mwh": (
        "A forecast price at or above this counts as a spike the guard checks.",
        "AUD/MWh; 5000 = $5/kWh.",
    ),
    # Features
    "solver_load_forecast_source_policy": (
        "Which load forecast the plan uses.",
        "off = Nimbus's own · measure = report only · blend · persistence.",
    ),
    "solver_auto_include_known_solar": (
        "Finds Solcast / Open-Meteo solar forecasts automatically.",
        "",
    ),
    "solver_calibrated_objective_enabled": (
        "Equal-cost plans go to the steadier one.",
        "On by default.",
    ),
    "solver_nowcast_measurement_enabled": (
        "Records how the first forecast period tracks reality.",
        "Measurement only.",
    ),
    "solver_offer_curve_enabled": (
        "Publishes how the plan's grid use responds to price.",
        "Slower solves.",
    ),
    "solver_flex_signals_enabled": (
        "Publishes how much the plan could flex up or down.",
        "Slower solves.",
    ),
    "solver_score_controllable_loads_enabled": (
        "Lets the daily score re-time controllable loads.",
        "",
    ),
    "solver_dispatch_dry_run": (
        "Logs what Nimbus's own dispatcher would command, without commanding.",
        "Log only.",
    ),
    # Scoring
    "nimbus_quality_soc_discrepancy_mean_pct": (
        "Average gap between the battery's reported SoC and the one rebuilt from measured power.",
        "Percentage points.",
    ),
    "solver_soc_discrepancy_mean_threshold_pct": (
        "Above this average gap the day's score is flagged.",
        "Default 8 points.",
    ),
    "nimbus_quality_soc_discrepancy_max_pct": (
        "Largest SoC gap in the day.",
        "Percentage points.",
    ),
    "solver_soc_discrepancy_max_threshold_pct": (
        "Above this largest gap the day is flagged.",
        "Default 15 points.",
    ),
    "nimbus_quality_soc_discrepancy_reliable": (
        "The SoC readings agreed well enough to trust.",
        "Both gaps under their limits.",
    ),
    "nimbus_quality_epr_reliable": ("The day had enough good data to score.", ""),
    "nimbus_counterfactual_nimbus_only_soc_close_pct": (
        "Where SoC would have been at 5 pm had only Nimbus acted.",
        "A counterfactual, %.",
    ),
    "nimbus_counterfactual_real_soc_close_pct": ("Where SoC really was at 5 pm.", "%."),
    # Reserve and price spikes
    "solver_battery_reserve_percent": (
        "The bottom share of the battery kept back between the hold times.",
        "% of capacity; 0 = off.",
    ),
    "solver_battery_reserve_release_price": (
        "The reserve is sold only when the price beats this margin.",
        "$/kWh.",
    ),
    "solver_battery_reserve_start_hour": (
        "The reserve is held from this time.",
        "Local time.",
    ),
    "solver_battery_reserve_end_hour": ("Released at this time.", "Local time."),
    "solver_price_event_enabled": (
        "Arms price events: a sensor you build signals an expected spike.",
        "Off = ignored.",
    ),
    "solver_price_spike_threshold": (
        "A price at or above this is a spike.",
        "$/kWh; 0 = off.",
    ),
    "solver_price_spike_discharge_kw": (
        "How hard to sell into a spike when armed.",
        "kW; a steady rate beats an instant dump on a long spike.",
    ),
    "solver_price_spike_override_armed": (
        "You allow Nimbus to sell into a spike now.",
        "Off = it only shows the spike.",
    ),
    # More settings (household, 18 Oct 2026)
    "household_mode": (
        "Household mode: home, away, guests or economy.",
        "Switches each load to its mode's targets, e.g. no hot-water deadline away.",
    ),
    "pricing_source": (
        "Which provider's prices the Solver uses.",
        "Picking one switches import and export together; Manual changes nothing.",
    ),
    "solve_on_price_change": (
        "Re-plan as soon as a configured price sensor updates.",
        "Off: the plan re-solves on its own cycle only.",
    ),
    "solve_on_price_change_debounce_s": (
        "Re-plan delay: price updates within this many seconds make one solve.",
        "Seconds; stops a burst of updates from solving several times.",
    ),
    "solver_price_response_latency": (
        "How long from a price update to the new plan.",
        "Seconds, last measured.",
    ),
    "solver_charge_taper_start_percent": (
        "SoC at which the charge limit starts to fall.",
        "% SoC. 100 = off, full power right up to Max SoC.",
    ),
    "solver_charge_power_at_full_percent": (
        "Charge power left at 100% SoC, as % of max charge.",
        "The limit falls in a straight line from Taper starts at to this.",
    ),
    "solver_solar_running_low_min_p50_kw": (
        "Solar running low: judged only while forecast p50 is at least this.",
        "kW; 0 = the check is off.",
    ),
    "solver_solar_running_low_on_percent": (
        "Measured solar below this % of p50: the rest of today plans on p10.",
        "% of the forecast p50.",
    ),
    "solver_solar_running_low_off_percent": (
        "Measured solar back above this % of p50: plans on p50 again.",
        "% of the forecast p50.",
    ),
    "solver_solar_running_low_confirm_minutes": (
        "Minutes solar must stay low before the check turns on.",
        "0 = at the first low reading.",
    ),
    "solver_config": (
        "Whether the Solver has the settings it needs.",
        "configured once the setup wizard is complete.",
    ),
    "nimbus_solver_discharge_efficiency": (
        "One leg of the round-trip efficiency.",
        "√ of round trip: 95% → 0.975 each way.",
    ),
    "nimbus_solver_dispatch_dry_run": (
        "What Nimbus's own dispatcher would command now, in dry run.",
        "kW; it commands nothing while dry run is on.",
    ),
    "nimbus_counterfactual_soc": (
        "Yesterday's real end-of-day battery SoC.",
        "%; its attributes add what Nimbus alone would have reached.",
    ),
    # Stochastic settings
    "solver_stochastic_dispatch": (
        "Drive the battery: off, the stochastic plan only runs beside the main plan.",
        "On, it runs the battery unless it cannot be trusted.",
    ),
    "solver_stochastic_stage1_minutes": (
        "Commit window: how far ahead a decision must suit all three solar days.",
        "30 min = first six periods; longer is more cautious.",
    ),
    "solver_stochastic_tail_weight": (
        "Day weight w for the low and high solar days.",
        "Weights w / 1−2w / w; 0.3 → 30 / 40 / 30%.",
    ),
    "solver_stochastic_drive_budget_seconds": (
        "Drive time limit: past it, the main plan runs that cycle.",
        "Seconds.",
    ),
    "solver_stochastic_shadow_interval_minutes": (
        "How often it re-plans while off (shadow).",
        "Minutes; the main plan solves every 5.",
    ),
}

# ---- the wizard, from strings.json ------------------------------------------

#: (section title, path into strings.json) in the order a household meets them.
WIZARD_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Configure → Solver settings → screen 1 of 3",
        ("options", "step", "solver_battery"),
    ),
    ("Configure → Solver settings → screen 2 of 3", ("options", "step", "solver_grid")),
    (
        "Configure → Solver settings → screen 3 of 3",
        ("options", "step", "solver_sources"),
    ),
    ("Configure → Forecaster settings (one screen)", ("options", "step", "forecaster")),
    (
        "Configure → Topology diagram settings (one screen)",
        ("options", "step", "switchboard"),
    ),
    (
        "Integration page → + Add power signal",
        ("config_subentries", "power_signal", "step", "user"),
    ),
    ("Integration page → + Add load", ("config_subentries", "load", "step", "user")),
    (
        "Integration page → + Add power source",
        ("config_subentries", "power_source", "step", "user"),
    ),
    (
        "Integration page → + Add PV string",
        ("config_subentries", "pv_string", "step", "user"),
    ),
    (
        "Integration page → + Add battery tower",
        ("config_subentries", "battery_tower", "step", "user"),
    ),
    (
        "Integration page → + Add controllable load",
        ("config_subentries", "controllable_load", "step", "user"),
    ),
    (
        "Integration page → + Add battery participant",
        ("config_subentries", "battery_participant", "step", "user"),
    ),
)


@functools.cache
def _strings() -> dict[str, Any]:
    """The wizard's own strings.json, read once. A file read, so it must not
    run first on the event loop: async_ensure_nimbus_dashboard() loads it in
    the executor before the Help tab is built (devhub, v0.94.462 validation:
    HA logged "Detected blocking call to read_text" from here)."""
    path = Path(__file__).with_name("strings.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _cell(text: Any) -> str:
    return str(text).replace("|", "/").replace("\n", " ").strip()


def wizard_step(title: str, path: tuple[str, ...]) -> str:
    """One wizard step as a collapsible block: its own title, description and
    every field with the wizard's own explanation (🔴 = required)."""
    node: Any = _strings()
    for part in path:
        node = node[part]
    data = node.get("data") or {}
    help_text = node.get("data_description") or {}
    lines = [
        f"<details><summary><b>{title}</b>: {_cell(node.get('title', ''))}</summary>\n",
        _cell(node.get("description", "")),
        "",
    ]
    if data:
        lines += ["| Field | What to put |", "|:---|:---|"]
        lines += [
            f"| {_cell(label)} | {_cell(help_text.get(key, ''))} |"
            for key, label in data.items()
        ]
    lines.append("\n</details>")
    return "\n".join(lines)


# ---- assembly ---------------------------------------------------------------


def _markdown(content: str, title: str | None = None) -> dict[str, Any]:
    card: dict[str, Any] = {"type": "markdown", "content": content}
    if title:
        card["title"] = title
    return card


def _full(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """A full-width section whose cards size to their content: no fixed
    height, so no scrollbar and no gap."""
    return {
        "type": "grid",
        "column_span": 4,
        "cards": [{**c, "grid_options": {"columns": "full"}} for c in cards],
    }


def _banner_name(section: Any) -> str | None:
    """A Solver-tab block's name, from its opening band (a lone ha-alert)."""
    cards = section.get("cards") if isinstance(section, dict) else None
    if not cards or len(cards) != 1:
        return None
    content = str(cards[0].get("content", ""))
    if not (content.startswith("<ha-alert") and content.endswith("</ha-alert>")):
        return None
    return content.split('title="', 1)[1].split('"', 1)[0]


def _settings_by_card() -> list[tuple[str, str, list[str]]]:
    """The Solver tab's entities cards in its own order: (block, card, keys)."""
    out: list[tuple[str, str, list[str]]] = []
    block = ""

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "entities":
                keys: list[str] = []
                for row in node.get("entities", []):
                    for ref in (
                        row.get("entity"),
                        (row.get("row") or {}).get("entity"),
                    ):
                        if isinstance(ref, str) and ref.startswith("@"):
                            keys.append(ref[1:])
                out.append((block, str(node.get("title", "")), keys))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for section in solver_tab.template():
        block = _banner_name(section) or block
        walk(section)
    return out


def settings_markdown() -> str:
    """Every Solver-tab row, grouped by the card it sits on."""
    parts = ["**Every figure and setting on the Solver tab, with its formula**\n"]
    for block, title, keys in _settings_by_card():
        parts.append(
            f"<details><summary><b>{title}</b> · Solver tab → {block} → "
            f"{title} card</summary>\n"
        )
        parts.append("| | What it is | Formula / example |\n|:---|:---|:---|")
        for key in keys:
            what, formula = SETTINGS[key]
            parts.append(f"| `{key}` | {what} | {formula} |")
        parts.append("\n</details>")
    return "\n".join(parts)


def glossary_markdown() -> str:
    rows = "\n".join(f"| **{t}** | {m} | {f} |" for t, m, f in GLOSSARY)
    return f"**Glossary**\n\n| Term | Meaning | Formula / example |\n|:---|:---|:---|\n{rows}"


def _banner(name: str, kind: str, line: str) -> dict[str, Any]:
    return solver_tab._banner(name, kind, line)


def template() -> list[dict[str, Any]]:
    """The tab's sections with `@key` placeholders, before resolution."""
    return [
        _banner("Start here", "error", "Set Nimbus up so every tab works."),
        _full([_markdown(_START_HERE)]),
        _full([_markdown(_WHERE)]),
        _full([_markdown(_TRAPS)]),
        _full([_markdown(_REPORT)]),
        _full([_markdown(whats_new_markdown())]),
        _banner("Each tab", "info", "What every view shows, and what it needs."),
        _full([_markdown(_TABS)]),
        _banner("Settings", "warning", "Every figure and dial, with its formula."),
        _full([_markdown(settings_markdown())]),
        _full([_markdown(glossary_markdown())]),
        _banner("Setup wizard", "success", "Every screen, in the wizard's own words."),
        _full(
            [
                _markdown(
                    "\n\n".join(wizard_step(t, p) for t, p in WIZARD_STEPS[i : i + 4])
                )
                for i in range(0, len(WIZARD_STEPS), 4)
            ]
        ),
    ]
