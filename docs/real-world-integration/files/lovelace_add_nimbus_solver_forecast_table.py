#!/usr/bin/env python3
"""Adds a real, HAEO-forecast-table-style markdown card pair to the
Solver's own dashboard -- direct ask: "still waiting for haeo like
markdown table where I can see forecasted costs fit load solar and
soc% and period net... I have built these and they are gold." /
"i know it is not plugged into real life yet... but one day it will
be and for me to get there i have to see its plan."

Card 1 (markdown, "Solver Forecast — Next 12h"): a real per-period
table -- Time / Buy¢ / Sell¢ / Load / Solar / Battery / SoC% / Net$ --
read directly from sensor.nimbus_solver_battery_forecast's own
`forecast` array (each period already carries all of these fields as
of PR #624). Limited to the first 48 periods (12h at this horizon's own
15-min fine-tier resolution, see nimbus_solver_forecast_writer.py's own
TIER1_PERIOD_HOURS) -- the genuinely actionable near-term window, not
all 169 periods of a 96h horizon crammed into one unreadable table.

Card 2 (markdown, "Solver Daily Summary"): real daily-level rollups
(today's forecasted net cost, total import/export kWh, peak discharge,
SoC range) computed by summing/reducing the SAME forecast array --
genuinely FORECAST-only (not a real "settled so far today" figure like
HAEO/LV's own daily summaries have, since the Solver doesn't drive real
dispatch yet -- there is no "actual" to reconcile against), labeled
honestly as such.

Self-locating, same technique as lovelace_add_nimbus_solver_quality_
card.py: searches for the Solver's own already-placed forecast card
rather than assuming a hardcoded dashboard/view/index.

Deploy (Python-via-docker-exec, per this project's own standing
PowerShell-BOM-corruption rule):
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/lovelace_add_nimbus_solver_forecast_table.py > /tmp/lovelace_add_nimbus_solver_forecast_table.py
  docker cp /tmp/lovelace_add_nimbus_solver_forecast_table.py opt_homeassistant_1:/tmp/
  docker exec opt_homeassistant_1 python3 /tmp/lovelace_add_nimbus_solver_forecast_table.py
  docker restart opt_homeassistant_1
"""
import json

CANDIDATE_FILES = [
    "/config/.storage/lovelace.dashboard_nimbus",
    "/config/.storage/lovelace.dashboard_pv",
]
ANCHOR_ENTITY = "sensor.nimbus_solver_battery_forecast"

FORECAST_TABLE_CARD = {
    "type": "markdown",
    "title": "Solver Forecast — Next 12h",
    # grid_options.rows + card_mod scroll (2026-08-17, direct ask: "could
    # we please set these tables to the correct row heights... scrolling
    # is ok on the big markdown"). This card's own real content (48+
    # rows) was forcing all 4 cards in this section to stretch to match
    # it (HA's `type: grid` section default: stretch every card to the
    # tallest sibling). Bumped 10 -> 22 per direct follow-up ("could
    # these cards just have rows: 22 or so or a number I could change
    # the way I want?") -- max-height kept in lockstep (rows * ~56px/row,
    # this project's own established estimate). card_mod's max-height +
    # overflow-y:auto is what actually makes THIS card's own excess
    # content scroll internally within that bounded height, rather than
    # being silently clipped by grid_options.rows alone (which only
    # constrains the grid CELL, not a card's own internal overflow
    # behavior). Once a card has explicit grid_options, HA's dashboard
    # editor shows a real drag-resize handle on it -- rows is now
    # genuinely user-adjustable live in the UI; if rows is ever changed
    # there without touching this script, this card_mod max-height
    # should be updated to match (rows * 56px) or the scroll cap and the
    # grid cell size will drift out of sync.
    "grid_options": {"rows": 22},
    "card_mod": {"style": "ha-card { max-height: 1230px; overflow-y: auto; }"},
    "content": (
        # Real, direct correction (2026-08-17, live feedback: "we spent
        # so long chasing costs, kw and $$$... and u give me a lousy
        # table... not even knowing about p2p?") -- the first version of
        # this table computed the real P2P bonus correctly internally
        # (baked into Net$) but never SHOWED bonus_price anywhere, so a
        # real period like Buy=50c/Sell=8c/Net=-$1.50 (a genuine, real
        # PROFIT, driven almost entirely by the ~50c P2P premium) looked
        # inexplicable -- nothing on screen showed WHY discharging
        # against an 8c sell price was ever a good idea. Added a real
        # "P2P¢" column (bonus_price, the exact same field the LP itself
        # solved against) so the mechanism this whole project has spent
        # so long chasing is actually visible, not hidden inside a
        # single opaque Net$ number.
        "{% set s = states.sensor.nimbus_solver_battery_forecast %}"
        "{% if s and s.attributes.get('forecast') %}"
        # 148, not 48 (2026-08-17): the writer's own horizon is now 3-tier
        # (1-min for the first 5 real minutes, then 5-min -- was a flat
        # 15-min throughout tier1). 48 periods was correct for "next 12h"
        # under the OLD 15-min-only tier1 (48 x 15min = 12h); under the
        # new resolution that same slice would only cover ~4h. 148 =
        # 5 (tier0, 1-min periods) + 143 (tier1, 5-min periods covering
        # the remaining 715 of 720 real minutes) -- a genuine 12h window
        # again. The card's own card_mod scroll (PR #635, same session)
        # already handles a longer table safely.
        "{% set fc = s.attributes.forecast[:148] %}"
        # Buy¢/Fees¢ split (2026-08-22, direct household ask -- see
        # import_price_raw's own comment in nimbus_solver_forecast_
        # writer.py for the full story): Buy¢ now shows the RAW
        # commodity price alone (import_price_raw) -- the exact same
        # number sensor.localvolts_costs_flex_up shows, independently
        # checkable against LocalVolts' own app -- and Fees¢ is the
        # real network TOU + certificates add-on shown SEPARATELY,
        # instead of one silently-combined Buy¢ nobody outside this
        # code could reconcile. Net$ still uses the FULL landed cost
        # (import_price = raw + fees) -- unchanged, still correct.
        "| Time | Buy¢ | Fees¢ | Sell¢ | P2P¢ | Load | Solar | Batt | SoC% | Net$ |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
        # Real day-boundary TOTAL row (2026-08-17, direct ask: "you are
        # missing the summary at 00.00=TOTAL") -- this table shows the
        # next 12h from "now", which routinely crosses real local
        # midnight (e.g. now=15:00 -> table runs to ~03:00 next day) with
        # nothing marking the boundary or showing a running total up to
        # that point. day_ns accumulates Net$/P2P$ for whichever real
        # calendar date is currently being rendered; the moment a row's
        # own date differs from the accumulator's, a bold TOTAL row is
        # emitted for the day that just finished (only the PORTION of
        # that day actually visible in this 12h-limited table, not a
        # full midnight-to-midnight total -- the separate Daily Summary
        # card below already has the real full-day figure), then the
        # accumulator resets and rendering continues into the new date.
        "{% set day_ns = namespace(date=none, net=0, p2p=0) %}"
        "{% for p in fc %}"
        "{% set p_date = as_timestamp(p.time) | timestamp_custom('%Y-%m-%d') %}"
        "{% if day_ns.date is not none and p_date != day_ns.date %}"
        "| **— {{ day_ns.date }} TOTAL (shown rows) —** |  |  |  | "
        "<font color='gold'>**+${{ day_ns.p2p | round(2) }}**</font> |  |  |  |  | "
        "<font color='{{ \"lightgreen\" if day_ns.net < 0 else \"orange\" }}'>**${{ day_ns.net | round(2) }}**</font> |\n"
        "{% set day_ns.net = 0 %}{% set day_ns.p2p = 0 %}"
        "{% endif %}"
        "{% set day_ns.date = p_date %}"
        "{% set day_ns.net = day_ns.net + p.net_cost %}"
        "{% set day_ns.p2p = day_ns.p2p + p.bonus_price * p.export_bonus_kw * p.hours %}"
        "| {{ as_timestamp(p.time) | timestamp_custom('%H:%M') }} "
        "| {{ (p.import_price_raw * 100) | round(1) }} "
        "| {{ ((p.import_price - p.import_price_raw) * 100) | round(1) }} "
        "| {{ (p.export_price * 100) | round(1) }} "
        # Threshold 0.01 (1c), not the raw >0 -- real, confirmed live
        # finding testing this exact fix: a period can show a tiny,
        # economically meaningless bonus_price (e.g. 0.0023) purely from
        # a slightly-negative base export_price outside the real P2P
        # window (max(0, p2p_export - spot_export) with spot_export < 0),
        # not a genuine P2P signal. 1c cleanly separates real P2P
        # premiums (confirmed live: ~40c during the actual window) from
        # this kind of rounding noise. NOTE (2026-08-17, live question:
        # "why is there one p2p at 16:32 2.8c?"): a value clearing this
        # 1c bar 20-30 min BEFORE the nominal 17:00 window open is a
        # separate, real, still-open question -- not yet root-caused,
        # under investigation. Not assumed to be noise just because it's
        # early; also not yet confirmed as a genuine signal.
        "| {{ '<b><font color=\"gold\">' + ((p.bonus_price * 100) | round(1) | string) + '</font></b>' if p.bonus_price > 0.01 else '—' }} "
        "| {{ p.load_kw | round(1) }} "
        "| {{ p.solar_kw | round(1) }} "
        "| <font color='{{ \"lightgreen\" if p.battery_kw > 0.05 else (\"orange\" if p.battery_kw < -0.05 else \"gray\") }}'>{{ p.battery_kw | round(1) }}{{ \"⚡\" if p.export_bonus_kw > 0.05 else \"\" }}</font> "
        "| {{ p.soc_pct | round(0) | int }}% "
        "| <font color='{{ \"lightgreen\" if p.net_cost < 0 else \"orange\" }}'>{{ p.net_cost | round(2) }}</font> |\n"
        "{% endfor %}\n"
        "*Buy¢ = raw commodity price (matches sensor.localvolts_costs_flex_up directly). "
        "Fees¢ = network TOU + certificates added on top — Buy¢ + Fees¢ = what's actually paid. "
        "Battery: green=discharging, orange=charging, ⚡=genuinely earning the P2P premium this period. "
        "P2P¢ = the real bonus rate ON TOP OF Sell¢ (gold when active). Net $: green=profit, orange=cost. "
        "TOTAL row = sum of only the rows shown above it for that date (this table is a 12h window, not a full day). "
        "Real Solver plan, OBSERVATION ONLY — not connected to live dispatch yet.*"
        "{% else %}"
        "*No Solver forecast yet — runs every 15 min once deployed.*"
        "{% endif %}"
    ),
}

DAILY_SUMMARY_CARD = {
    "type": "markdown",
    "title": "Solver Daily Summary (Forecast Only)",
    # Matches FORECAST_TABLE_CARD's own grid_options.rows + card_mod --
    # see its comment. This card is shorter than the forecast table but
    # still real (2 full day-summary tables), so it benefits from the
    # same scroll treatment rather than an even smaller fixed height.
    "grid_options": {"rows": 22},
    "card_mod": {"style": "ha-card { max-height: 1230px; overflow-y: auto; }"},
    "content": (
        "{% set s = states.sensor.nimbus_solver_battery_forecast %}"
        "{% if s and s.attributes.get('forecast') %}"
        "{% set ns = namespace(today=[], tomorrow=[]) %}"
        "{% set today_date = now().strftime('%Y-%m-%d') %}"
        "{% set tomorrow_date = (now() + timedelta(days=1)).strftime('%Y-%m-%d') %}"
        "{% for p in s.attributes.forecast %}"
        "{% set p_date = as_timestamp(p.time) | timestamp_custom('%Y-%m-%d') %}"
        "{% if p_date == today_date %}{% set ns.today = ns.today + [p] %}"
        "{% elif p_date == tomorrow_date %}{% set ns.tomorrow = ns.tomorrow + [p] %}"
        "{% endif %}"
        "{% endfor %}"
        "{% macro summary_rows(periods) %}"
        # Real bug fix (2026-08-17): was `| sum * 0.25`, a flat 15-min
        # multiplier -- correct for the fine tier (first 24h) but WRONG
        # for the coarse 1h tier beyond it, silently under-counting any
        # such period's real kWh by 4x. "Tomorrow" genuinely spans both
        # tiers. Now sums each period's own real power*hours individually
        # (p.hours, added to the pushed forecast this same session)
        # instead of assuming every period is the same width.
        "{% set net = periods | sum(attribute='net_cost') %}"
        "{% set imp_kwh = namespace(v=0) %}{% for p in periods %}{% set imp_kwh.v = imp_kwh.v + p.grid_import_kw * p.hours %}{% endfor %}"
        "{% set exp_kwh = namespace(v=0) %}{% for p in periods %}{% set exp_kwh.v = exp_kwh.v + p.grid_export_kw * p.hours %}{% endfor %}"
        "{% set bonus_kwh = namespace(v=0) %}{% for p in periods %}{% set bonus_kwh.v = bonus_kwh.v + p.export_bonus_kw * p.hours %}{% endfor %}"
        # Real $ breakdown by category (2026-08-17, direct ask: "also
        # daily summary for haeo is much more superior... not sure how to
        # tackle") -- HAEO/LV's own daily summaries earn that reputation
        # by showing WHERE the money comes from/goes (separate Import
        # Cost / Export Earnings / Fixed Cost rows), not just one flat
        # Net $. Each per-period $ term already exists in the pushed
        # forecast (import_price/export_price/bonus_price x the matching
        # kw x hours) -- this just sums them by category instead of only
        # ever summing the pre-combined net_cost field. import_cost -
        # spot_earnings - p2p_earnings reproduces net_cost exactly (same
        # formula nimbus_solver_forecast_writer.py itself uses to compute
        # it) -- shown here as a cross-check line, not a second
        # independent calculation.
        "{% set imp_cost = namespace(v=0) %}{% for p in periods %}{% set imp_cost.v = imp_cost.v + p.import_price * p.grid_import_kw * p.hours %}{% endfor %}"
        "{% set spot_earn = namespace(v=0) %}{% for p in periods %}{% set spot_earn.v = spot_earn.v + p.export_price * p.grid_export_kw * p.hours %}{% endfor %}"
        "{% set p2p_earn = namespace(v=0) %}{% for p in periods %}{% set p2p_earn.v = p2p_earn.v + p.bonus_price * p.export_bonus_kw * p.hours %}{% endfor %}"
        "{% set total_earn = spot_earn.v + p2p_earn.v %}"
        "{% set socs = periods | map(attribute='soc_pct') | list %}"
        "{% set batt_min = periods | map(attribute='battery_kw') | min %}"
        "{% set batt_max = periods | map(attribute='battery_kw') | max %}"
        "| Metric | Value |\n|---|---|\n"
        "| Import Cost | <font color='orange'>-${{ imp_cost.v | round(2) }}</font> ({{ imp_kwh.v | round(1) }} kWh) |\n"
        "| Spot Export Earnings | <font color='lightgreen'>+${{ spot_earn.v | round(2) }}</font> ({{ exp_kwh.v | round(1) }} kWh) |\n"
        "| P2P Bonus Earnings | <font color='gold'>+${{ p2p_earn.v | round(2) }}</font> ({{ bonus_kwh.v | round(1) }} kWh) |\n"
        "| **Total Export Earnings** | <font color='lightgreen'>**+${{ total_earn | round(2) }}**</font> |\n"
        "| **Net $ (forecast)** | <font color='{{ \"lightgreen\" if net < 0 else \"orange\" }}'>**${{ net | round(2) }}**</font> |\n"
        "| SoC Range | {{ socs | min | round(0) | int }}% – {{ socs | max | round(0) | int }}% |\n"
        "| Peak Charge / Discharge | {{ batt_min | round(1) }} / {{ batt_max | round(1) }} kW |\n"
        "{% endmacro %}"
        "**Today ({{ today_date }})**\n\n{{ summary_rows(ns.today) }}\n"
        "**Tomorrow ({{ tomorrow_date }})**\n\n{{ summary_rows(ns.tomorrow) }}\n"
        "*Forecast-only — the Solver doesn't drive real dispatch yet, so there is no "
        "\"actual so far today\" figure to reconcile against (unlike the HAEO/LV daily summaries).*"
        "{% else %}"
        "*No Solver forecast yet.*"
        "{% endif %}"
    ),
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
    """Real, confirmed bug fixed 2026-08-17: this used to unconditionally
    APPEND both cards every run, with no check for an already-deployed
    copy from a previous run. Since this script's own FORECAST_TABLE_CARD/
    DAILY_SUMMARY_CARD content had genuinely changed twice since the
    first deploy (P2P bonus column added, then the 4x kWh daily-summary
    tier bug fixed) but was never redeployed live, the household was
    looking at a STALE first-ever version for days -- direct, harsh, and
    correct live feedback: "it doesnt have correct sales, rates nor
    earnings, doesnt even know p2p exists." Matches the SAME safe
    replace-by-title pattern lovelace_build_power_signal_forecast_chart.py
    already used correctly from day one -- find an existing card with the
    SAME title, replace it in place; only append if genuinely new.
    """
    for i, c in enumerate(cards):
        if c.get("type") == "markdown" and c.get("title") == new_card["title"]:
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
            if section is not None:
                r1 = _replace_or_append(section.setdefault("cards", []), FORECAST_TABLE_CARD)
                r2 = _replace_or_append(section["cards"], DAILY_SUMMARY_CARD)
                print(f"  {r1} forecast table card, {r2} daily summary card in that view's section ({len(section['cards'])} cards total).")
            else:
                view_cards = view.setdefault("cards", [])
                r1 = _replace_or_append(view_cards, FORECAST_TABLE_CARD)
                r2 = _replace_or_append(view_cards, DAILY_SUMMARY_CARD)
                print(f"  {r1} forecast table card, {r2} daily summary card in that view's card list ({len(view_cards)} cards total).")
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
            view["sections"].append({"type": "grid", "cards": [FORECAST_TABLE_CARD, DAILY_SUMMARY_CARD]})
            print(f"{ANCHOR_ENTITY} not found anywhere -- added a NEW section to {path}'s first view instead.")
        else:
            view.setdefault("cards", []).extend([FORECAST_TABLE_CARD, DAILY_SUMMARY_CARD])
            print(f"{ANCHOR_ENTITY} not found anywhere -- appended cards to {path}'s first view's card list instead.")
        target_path = path

    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"Saved {target_path}. Restart HA for this to take effect.")


if __name__ == "__main__":
    main()
