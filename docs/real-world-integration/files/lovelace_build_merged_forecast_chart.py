"""Regenerate the single "Nimbus Power Signals and Loads Forecasts"
apexcharts-card entirely from LIVE Nimbus state -- no hardcoded entity
list for either group. This is the ONE, canonical Nimbus Forecaster
chart on this dashboard as of 2026-08-22 -- see CLAUDE.md's own session
log for that date for the full step-by-step design history (why it was
merged from two separate cards, why loads live in the legend while
power signals live in the header, why 4 specific loads get bold/vivid
styling, and a real, confirmed apexcharts-card gotcha found the hard
way that night: `show.in_chart: false` combined with `show.in_legend:
true` silently drops a series from the legend entirely in this card
version, not just from the chart -- do NOT reintroduce that combination
anywhere in this file without re-testing it live first).

This file REPLACES two separate, now-retired scripts:
  - lovelace_build_power_signal_forecast_chart.py (deleted 2026-08-22)
  - lovelace_build_load_forecast_chart.py (deleted 2026-08-22, its own
    target card -- "Nimbus Load Forecasts" -- was deleted from the live
    dashboard by direct household instruction the same night: "now I
    deleted loads forecasts apex chart - we run with one only")
Do not recreate either of those files or their old cards. If a future
session finds itself wanting to "split this back into two charts",
that's a legitimate design change, but it needs a fresh, explicit
instruction -- not a stale script being re-run by accident. This exact
risk (an old generator script silently recreating a card the household
deliberately deleted) is why the two originals were removed outright
rather than just left around with a deprecation comment.

Two independent live-discovery loops feed ONE merged card:

1. **Power signals** (Battery/Solar/Grid, Nimbus's own "power_signal"
   subentry type) -- discovered via the same live `subentry_type`
   attribute check this project has used since 2026-08-15. Their real
   magnitude (tens of kW, signed) is why this whole chart still uses a
   dedicated "power" y-axis distinct from "load" -- crushing a signed
   +/-40kW swing and a few-kW individual circuit onto one shared scale
   was the original reason this project split into two charts in the
   first place (same "split by magnitude, don't force a shared axis"
   reasoning as the LV-GRAPH chart). Rendered as HEADER tiles (2026-08-22
   swap, direct ask: "power signals can be headers they are more
   important") -- Whole House Load is the one deliberate exception, kept
   in the LEGEND group below despite technically being a power_signal
   subentry, per the same night's explicit instruction.

2. **Individual loads** (Nimbus's own "load" subentry type, 18 real
   circuits on this household as of 2026-08-22) -- discovered the same
   way, no hardcoded entity list, so a load added/removed/renamed on the
   Nimbus hub is picked up automatically on the next run. Rendered as
   small LEGEND entries with a live number (`legend_value: true`) rather
   than chart lines by default -- direct ask, 2026-08-22: "loads can be
   legends... small". Four of these (Pool x2, HWS x2) get deliberately
   bold/vivid styling (thicker stroke, filled area, 50% opacity, a color
   picked to stand out from the whole muted CB-* palette) via a genuine
   keyword match on the entity_id ("pool"/"hws"), not a hardcoded list
   of this one household's own entity names -- matches this project's
   own repeated design principle (see the Nimbus Solver audit's own
   "topology" finding, 2026-08-20) that nothing here should be bound to
   this one installation only.

Per-load color: deterministic, derived from a hash of the FORECAST
entity_id itself (never array index/sort order) -- confirmed live
2026-08-22 that this EXACT hash function reproduces, byte-for-byte, all
13 of the currently-live hardcoded colors for this household's own
"plain" circuits (a real check, not an assumption) -- so a given load's
color is stable across future runs even as other loads are added,
removed, or renamed, without needing this file to hardcode anyone's
real entity names. Two categories of override on top of the hash
default, both real, both confirmed-necessary:
  - CB-PW HEATER -> #E67E22 (fixes a genuine color collision with
    Humidity's own fixed #26C6DA, found and fixed 2026-08-15/16 -- the
    hash function alone would put HEATER back on a colliding hue).
  - The 4 "stand out" loads (Pool x2, HWS x2) -> deliberately vivid,
    non-hash colors (gold/magenta/lime/deep-sky-blue), chosen 2026-08-22
    specifically to be visually distinct from the entire muted CB-*
    hash-derived palette, not just from each other.

Units, confirmed live 2026-08-16/22, not assumed: every real CB-*
circuit-breaker sensor reports in WATTS, while the Whole House
cross-reference sensor already reports in kW -- every load history
series therefore carries a `transform: x / 1000` except Whole House's
own, same convention this file's own power-signal history series
already use for Solar's own W-vs-kW mismatch.

Run via docker exec on whichever NUC currently holds the VIP:
  docker cp /home/homehub/.ha_token opt_homeassistant_1:/tmp/.ha_token
  docker cp lovelace_build_merged_forecast_chart.py opt_homeassistant_1:/tmp/
  docker exec opt_homeassistant_1 python3 /tmp/lovelace_build_merged_forecast_chart.py
  docker restart opt_homeassistant_1
"""
import colorsys
import json
import time
import urllib.request
import urllib.error

LOVELACE_PATH = "/config/.storage/lovelace.dashboard_nimbus"
TOKEN_PATH = "/tmp/.ha_token"
HA_BASE = "http://localhost:8123"
# The one, canonical card title on this dashboard as of 2026-08-22. No
# FORECASTER_VIEW_TITLE constant -- deliberately never depended on any
# view's own title (2026-08-21 finding: the "NIMBUS FORECASTER" view got
# renamed to just "Forecaster" mid-session and broke a view-title-based
# lookup outright; this file's own _find_card() only ever searches for
# its own card by TITLE, immune to any view rename).
CARD_TITLE = "Nimbus Power Signals and Loads Forecasts"
# Whole House Load cross-reference (2026-08-16/21/22) -- kept up here so
# the retry loop below (which needs to check for this entity's own
# presence) can reference it. See the "Whole House Load" series-building
# block further down for the full history/reasoning behind this one
# specific entity, including why it's deliberately NOT part of either
# generic discovery loop below.
WHOLE_HOUSE_FORECAST_ENTITY = "sensor.nimbus_cb_total_combined_power_adjusted_kw_forecast"

# Per-signal styling (2026-08-15, explicit ask), matched by keyword in
# the entity_id rather than an exact 3-entity hardcoded list -- stays
# correct if this household's real entity names ever change, and
# extends sensibly if another battery/solar/grid-like signal is ever
# added. Falls through to the DC/Solar style for anything that doesn't
# match "battery" or "meter"/"grid" -- solar is the only one of these
# three signals that's never negative, so a solid area fill would
# otherwise look identical to a load's own "deterministic mode" shading;
# dashed keeps it visually distinct at a glance.
_STYLE_BATTERY = {"color": "#8BC34A", "type": "area", "opacity": 0.2, "stroke_width": 2}  # light green, 20% fill
_STYLE_GRID = {"color": "#64B5F6", "type": "area", "opacity": 0.2, "stroke_width": 2}  # light blue, 20% fill
_STYLE_SOLAR = {"color": "#FF9800", "stroke_dash": 6, "stroke_width": 3}  # amber/orange, dashed, thicker


def _style_for(entity_id: str) -> dict:
    eid = entity_id.lower()
    if "battery" in eid:
        return dict(_STYLE_BATTERY)
    if "meter" in eid or "grid" in eid:
        return dict(_STYLE_GRID)
    return dict(_STYLE_SOLAR)


def _clean_name_for(entity_id: str) -> str:
    """Named series (2026-08-15 cleanup): unlike the 18 individual load
    circuits (where the full real device name carries real
    disambiguating meaning), there are only ever three conceptual
    power-signal categories here -- Battery, Grid, Solar -- regardless
    of what this household's own raw entity happens to be called
    ("Logger Meter total active power" is technically correct but reads
    as a mess on a chart legend/header badge). Reuses the exact same
    keyword classification as _style_for() so a series' name and its
    styling can never disagree about what it represents.

    Confirmed via ATTR_SUBENTRY_TYPE that this classification is scoped
    to genuinely power_signal-type entities only, so "solar" as the
    fallback category is safe -- not a generic catch-all for anything
    unexpected.
    """
    eid = entity_id.lower()
    if "battery" in eid:
        return "Nimbus Battery"
    if "meter" in eid or "grid" in eid:
        return "Nimbus Grid"
    return "Nimbus Solar"


_LOAD_SUFFIXES = (" Power Forecast", " Power")


def _short_load_name(friendly_name: str) -> str:
    """"CB-PW HWS L1 Power Forecast" -> "CB-PW HWS L1" -- strips only the
    trailing " Power"/" Power Forecast", which is genuinely redundant on
    a chart where every single series already is a power forecast.
    Nothing else gets touched.

    Confirmed live 2026-08-15, twice: "CB-", "PW", and "LT" all carry
    real meaning ("circuit breaker", "power circuit", "lighting
    circuit") -- an earlier version of this function stripped "CB-PW "/
    "CB-LT " down to nothing, which collapsed two genuinely different
    real loads down to the identical legend label. The full real device
    name is the correct, unambiguous label; this function's only job is
    trimming the one part that's truly just noise on this chart.
    """
    name = friendly_name
    for suffix in _LOAD_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def _hash_color_for(entity_id: str) -> str:
    """Deterministic hex color derived from a hash of `entity_id` itself,
    not array index/sort order -- a given load's color must stay stable
    run to run (even as loads are added/removed) and must be computable
    identically for both a load's history series and its forecast
    series. Confirmed live 2026-08-22 this reproduces, exactly, every
    one of this household's own already-live "plain" load colors --
    real proof this function IS what generated them originally, not a
    fresh, different-looking palette about to replace a known one.
    """
    h = 0
    for ch in entity_id:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    hue = (h % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.55, 0.55)
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


# Real, confirmed-necessary overrides on top of the hash default -- see
# this module's own docstring "Per-load color" section for why each one
# exists. Keyed by the FORECAST entity_id (matches _hash_color_for()'s
# own input), not by display name, so a future rename doesn't silently
# stop the override applying.
_LOAD_COLOR_OVERRIDES = {
    # Real color collision with Humidity's own fixed #26C6DA, found and
    # fixed 2026-08-15/16 -- the hash default alone lands HEATER back on
    # a colliding hue.
    "sensor.nimbus_cb_pw_heater_power_forecast": "#E67E22",
    # "Stand out" loads (2026-08-22, direct ask) -- deliberately vivid,
    # non-hash colors chosen to be visually distinct from the entire
    # muted hash-derived palette, not just from each other. See
    # _is_standout_load() below for how these 4 (and any future
    # pool/HWS-named load) get matched.
    "sensor.nimbus_cb_pw_pool1_power_forecast": "#FFD700",
    "sensor.nimbus_cb_pw_pool_2_power_forecast": "#FF00FF",
    "sensor.nimbus_cb_pw_hws_l1_power_forecast": "#CCFF00",
    "sensor.nimbus_cb_pw_hws_l3_power_forecast": "#00BFFF",
}


def _is_standout_load(entity_id: str) -> bool:
    """Real, portable keyword match (2026-08-22) -- NOT a hardcoded list
    of this one household's own entity names. "Pool" and "hot water
    system" are genuinely significant, schedule-driven loads on most
    real solar+battery households, which is the actual reason they were
    singled out for bold styling in the first place ("make sure these
    stand out"), not something specific to this installation. A future
    pool or HWS circuit, added or renamed, gets the same treatment
    automatically -- nothing here needs editing.
    """
    eid = entity_id.lower()
    return "pool" in eid or "hws" in eid


def _real_entity_for(forecast_entity_id: str) -> str:
    """sensor.nimbus_<real_slug>_forecast -> sensor.<real_slug> -- Nimbus's
    own forecast entity_id is always the real source entity's own
    object_id wrapped in "nimbus_..._forecast" (confirmed live against
    every real power-signal AND load entity on this system, zero
    misses), so this is a genuine derivation, not a guess specific to
    one household's naming.
    """
    slug = forecast_entity_id.removeprefix("sensor.nimbus_").removesuffix("_forecast")
    return f"sensor.{slug}"


with open(TOKEN_PATH, encoding="utf-8") as f:
    token = f.read().strip()


def _fetch_states() -> list[dict]:
    req = urllib.request.Request(
        f"{HA_BASE}/api/states", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


# 2026-08-21: real, repeated live failure -- WHOLE_HOUSE_FORECAST_ENTITY
# (a Power Signal, genuinely newer/less-established than Battery/Grid/
# Solar/the 18 Load subentries) intermittently doesn't exist yet at the
# exact moment this script queries /api/states, even though independent
# checks moments before AND after each failure confirmed it healthy
# every time. Real race in Nimbus's own sequential subentry setup, not
# something human timing can reliably dodge -- retry inside the script
# instead.
all_states = _fetch_states()
for _attempt in range(6):
    if any(s["entity_id"] == WHOLE_HOUSE_FORECAST_ENTITY for s in all_states):
        break
    print(f"WHOLE_HOUSE_FORECAST_ENTITY not found yet (attempt {_attempt + 1}/6) -- retrying in 3s...")
    time.sleep(3)
    all_states = _fetch_states()

# Two independent discovery loops -- both restricted to entities ending
# in "_power_forecast" (see the module docstring's own naming note:
# WHOLE_HOUSE_FORECAST_ENTITY's real slug ends in "_kw", not "_power",
# which is EXACTLY why it never matches either generic loop below and
# has to be pulled in as its own explicit cross-reference instead --
# confirmed live 2026-08-22, not a bug, a deliberate/necessary design).
signal_forecasts = [
    s for s in all_states
    if s["entity_id"].startswith("sensor.nimbus_")
    and s["entity_id"].endswith("_power_forecast")
    and s.get("attributes", {}).get("subentry_type") == "power_signal"
]
assert signal_forecasts, (
    "No power_signal-type sensor.nimbus_*_power_forecast entities found -- "
    "aborting, not saving. (Confirm Nimbus is deployed at v0.12.0+ with at "
    "least one 'power signal' subentry added.)"
)

load_forecasts = [
    s for s in all_states
    if s["entity_id"].startswith("sensor.nimbus_")
    and s["entity_id"].endswith("_power_forecast")
    and s.get("attributes", {}).get("subentry_type") == "load"
]
assert load_forecasts, (
    "No load-type sensor.nimbus_*_power_forecast entities found -- "
    "aborting, not saving."
)

data_gen = "return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), p.value]);\n"
lower_gen = "return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), p.lower]);\n"
upper_gen = "return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), p.upper]);\n"
series = []

# --- Loads first (2026-08-22: legend group at the bottom, styled/sorted
# for that role) -----------------------------------------------------
for s in sorted(load_forecasts, key=lambda s: s["entity_id"]):
    entity_id = s["entity_id"]
    real_entity = _real_entity_for(entity_id)
    name = _short_load_name(s.get("attributes", {}).get("friendly_name", entity_id))
    color = _LOAD_COLOR_OVERRIDES.get(entity_id, _hash_color_for(entity_id))
    standout = _is_standout_load(entity_id)

    history_entry = {
        "entity": real_entity,
        "name": f"{name} (history)",
        "extend_to": False,
        "color": color,
        "stroke_width": 3 if standout else 1.5,
        "yaxis_id": "load",
        "show": {"in_header": False, "in_legend": False},
        "transform": "return x / 1000;",
    }
    series.append(history_entry)

    entry = {
        "entity": entity_id,
        "name": name,
        "extend_to": False,
        "data_generator": data_gen,
        "color": color,
        "stroke_width": 3 if standout else 1.5,
        "yaxis_id": "load",
        # Small legend entry, not a chart line by default (2026-08-22,
        # direct ask: "loads can be legends... small"). legend_value
        # shows the live number right next to the label.
        #
        # DO NOT add "in_chart: false" here -- confirmed live 2026-08-22
        # this silently drops the series from the legend ENTIRELY in
        # this apexcharts-card version, not just from the chart. See
        # this module's own docstring for the full incident.
        "show": {"in_header": False, "in_legend": True, "legend_value": True},
    }
    if standout:
        entry["type"] = "area"
        entry["opacity"] = 0.5
    series.append(entry)

# --- Power signals (2026-08-22: header group at the top, "more
# important") -- same auto-discovery as before, just relocated to
# HEADER instead of LEGEND. ------------------------------------------
for s in sorted(signal_forecasts, key=lambda s: s["entity_id"]):
    name = _clean_name_for(s["entity_id"])
    style = _style_for(s["entity_id"])
    band_color = style["color"]
    real_entity = _real_entity_for(s["entity_id"])

    # Real recorded history (2026-08-15, explicit ask: "i wanna see the
    # past... recorded past") -- a plain entity series reading the REAL
    # underlying sensor's own recorded state, same color as the
    # forecast line so the two read as one continuous trace across
    # "now". Solar's real entity reports in WATTS, unlike Battery/
    # Grid's own kW -- transform only applied for that one category.
    history_entry = {
        "entity": real_entity,
        "name": f"{name} (history)",
        "extend_to": False,
        "color": band_color,
        "stroke_width": 2,
        "yaxis_id": "power",
        "show": {"in_header": False, "in_legend": False},
    }
    if band_color == _STYLE_SOLAR["color"]:
        history_entry["transform"] = "return x / 1000;"
    series.append(history_entry)

    entry = {
        "entity": s["entity_id"],
        "name": name,
        "extend_to": False,
        "data_generator": data_gen,
        "yaxis_id": "power",
        # Header value badge (2026-08-22, swapped from legend to header:
        # "power signals can be headers they are more important") --
        # restored to this card's own original, proven-working show
        # config from before the same night's brief experiments.
        "show": {"in_header": True},
    }
    entry.update(style)
    series.append(entry)
    # Bound lines -- shown in the legend (never the header, there are
    # already 3 header tiles per signal without these), real names so
    # what they represent is self-explanatory rather than needing to be
    # explained every time. See this project's own 2026-08-16 finding
    # for why "(lower bound)"/"(upper bound)" replaced an earlier,
    # genuinely misleading "(min)"/"(max)" label.
    for label, gen in (("lower bound", lower_gen), ("upper bound", upper_gen)):
        series.append({
            "entity": s["entity_id"],
            "name": f"{name} ({label})",
            "extend_to": False,
            "data_generator": gen,
            "color": band_color,
            "opacity": 0.85,
            "stroke_width": 2,
            "stroke_dash": 4,
            "yaxis_id": "power",
            "show": {"in_header": False, "in_legend": True},
        })

# Temp/Humidity (2026-08-15, explicit ask), on hidden overlay axes --
# same established pattern as this project's own LV-GRAPH chart. Real
# entities confirmed live matching this Nimbus hub's own configured
# Temp/Humidity sensors: sensor.archerfield_temp (degC),
# sensor.archerfield_humidity (%). Kept in the HEADER group
# (2026-08-22 swap) alongside the power signals.
series.append({
    "entity": "sensor.archerfield_temp",
    "name": "Temp",
    "extend_to": False,
    "color": "red",
    "stroke_width": 1.5,
    "yaxis_id": "temp",
    "show": {"in_header": True, "in_legend": True},
})
# Forecast extension (2026-08-15, explicit ask: "can it show forecasted
# as well?") -- sensor.pirateweather_hourly_forecast's own forecast
# array carries BOTH temperature and humidity, so one real forecast
# source covers both new series. Hidden from header/legend (the history
# series already carries that) to avoid a duplicate-looking entry.
temp_fc_gen = "return entity.attributes.forecast.map(p => [new Date(p.datetime).getTime(), p.temperature]);\n"
humidity_fc_gen = "return entity.attributes.forecast.map(p => [new Date(p.datetime).getTime(), p.humidity]);\n"
series.append({
    "entity": "sensor.pirateweather_hourly_forecast",
    "name": "Temp (forecast)",
    "extend_to": False,
    "data_generator": temp_fc_gen,
    "color": "red",
    "stroke_width": 1.5,
    "stroke_dash": 3,
    "yaxis_id": "temp",
    "show": {"in_header": False, "in_legend": False},
})
series.append({
    "entity": "sensor.archerfield_humidity",
    "name": "Humidity",
    "extend_to": False,
    "color": "#26C6DA",
    "stroke_width": 1.5,
    "yaxis_id": "humidity",
    "show": {"in_header": True, "in_legend": True},
})
series.append({
    "entity": "sensor.pirateweather_hourly_forecast",
    "name": "Humidity (forecast)",
    "extend_to": False,
    "data_generator": humidity_fc_gen,
    "color": "#26C6DA",
    "stroke_width": 1.5,
    "stroke_dash": 3,
    "yaxis_id": "humidity",
    "show": {"in_header": False, "in_legend": False},
})

# Whole House Load (2026-08-16, real ask: "during the day i cannot see
# load either") -- NOT part of either generic discovery loop above by
# design (see this module's own docstring "naming note"): it's pulled
# in explicitly as a cross-reference, same pattern as the Solver's own
# proposed plan below. Kept in the LEGEND group (2026-08-22, explicit
# exception: "whole house should be 2x width also... whole load remains
# a line") despite being a power_signal subentry -- the one deliberate
# override in this whole file where subentry_type does NOT decide
# header-vs-legend placement.
_load_fc_gen = "return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), p.value]);\n"
if any(s["entity_id"] == WHOLE_HOUSE_FORECAST_ENTITY for s in all_states):
    series.append({
        "entity": _real_entity_for(WHOLE_HOUSE_FORECAST_ENTITY),
        "name": "Whole House Load (history)",
        "extend_to": False,
        "color": "#E0E0E0",
        "stroke_width": 3,
        "yaxis_id": "load",
        "show": {"in_header": False, "in_legend": False},
    })
    series.append({
        "entity": WHOLE_HOUSE_FORECAST_ENTITY,
        "name": "Whole House Load",
        "extend_to": False,
        "data_generator": _load_fc_gen,
        "color": "#E0E0E0",
        "stroke_width": 3,
        "yaxis_id": "load",
        "show": {"in_header": False, "in_legend": True, "legend_value": True},
    })
else:
    print(f"WARNING: WHOLE_HOUSE_FORECAST_ENTITY ({WHOLE_HOUSE_FORECAST_ENTITY}) not found live -- "
          f"chart will have no 'Whole House Load' series this run.")

# Nimbus SOLVER's own proposed 24h plan (2026-08-16, direct ask). A real
# optimizer decision -- NOT a Forecaster prediction, hence "(proposed)"
# on every series name and colors deliberately unlike any Forecaster
# line's own palette so the two can never be mistaken for each other at
# a glance. Same yaxis_id:"power" as the real Battery/Grid lines above
# -- that's the entire point, direct visual comparison. Kept in the
# HEADER group (2026-08-22 swap) alongside the power signals it's meant
# to be compared against.
_solver_battery_gen = "return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), p.battery_kw]);\n"
_solver_soc_gen = "return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), p.soc_pct]);\n"
_solver_grid_import_gen = "return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), p.grid_import_kw]);\n"
_solver_grid_export_gen = "return entity.attributes.forecast.map(p => [new Date(p.time).getTime(), -p.grid_export_kw]);\n"
series.append({
    "entity": "sensor.nimbus_solver_battery_forecast",
    "name": "Solver Battery (proposed)",
    "extend_to": False,
    "data_generator": _solver_battery_gen,
    "yaxis_id": "power",
    "color": "#AB47BC",
    "type": "area",
    "opacity": 0.2,
    "stroke_width": 2.5,
    "stroke_dash": 2,
    "show": {"in_header": True, "in_legend": True},
})
series.append({
    "entity": "sensor.nimbus_solver_battery_forecast",
    "name": "Solver Grid import (proposed)",
    "extend_to": False,
    "data_generator": _solver_grid_import_gen,
    "yaxis_id": "power",
    "color": "#9575CD",
    "stroke_width": 1.5,
    "stroke_dash": 2,
    "show": {"in_header": False, "in_legend": True},
})
series.append({
    "entity": "sensor.nimbus_solver_battery_forecast",
    "name": "Solver Grid export (proposed)",
    "extend_to": False,
    "data_generator": _solver_grid_export_gen,
    "yaxis_id": "power",
    "color": "#4DB6AC",
    "stroke_width": 1.5,
    "stroke_dash": 2,
    "show": {"in_header": False, "in_legend": True},
})
series.append({
    "entity": "sensor.nimbus_solver_battery_forecast",
    "name": "Solver SoC % (proposed)",
    "extend_to": False,
    "data_generator": _solver_soc_gen,
    "yaxis_id": "soc",
    "color": "#C2185B",
    "stroke_width": 4,
    "stroke_dash": 8,
    "show": {"in_header": True, "in_legend": True},
})

card = {
    "type": "custom:apexcharts-card",
    "grid_options": {"columns": "full"},
    "header": {"title": CARD_TITLE, "show": True},
    # graph_span/span (2026-08-22, confirmed live via the household's
    # own pasted card YAML): window = [now-6h, now+48h]. Both this and
    # apex_config.chart.height are values the household has hand-tuned
    # live more than once before -- if either is ever caught drifted
    # again on a future deploy, re-confirm the CURRENT live value first
    # (the household's own pasted YAML, or a WebSocket dump) rather than
    # assume this comment is still current.
    "graph_span": "54h",
    "span": {"offset": "+48h"},
    "now": {"show": True, "label": "now", "color": "#E91E63"},
    "apex_config": {
        "chart": {"height": 750},
        "legend": {"show": True, "position": "bottom"},
        # Zero horizon line -- genuinely meaningful on a signed axis
        # (positive/negative means something different per series:
        # import vs export, discharge vs charge).
        "annotations": {
            "yaxis": [{
                "y": 0, "borderColor": "#FFFFFF", "strokeDashArray": 0,
                "borderWidth": 1.5, "opacity": 0.5,
            }]
        },
    },
    "yaxis": [
        {"id": "power", "min": -40, "max": 40, "decimals": 0, "apex_config": {"tickAmount": 16, "title": {"text": "kW"}}},
        {"id": "temp", "show": False, "min": -40, "max": 40},
        {"id": "humidity", "show": False, "min": 0, "max": 100},
        # "load" -- min/max matches "power" EXACTLY (2026-08-22 fix,
        # direct household catch via screenshot: a real 1.3kW value was
        # visually reading as ~2.6kW when this axis was scaled to half
        # the power axis's own range). Axis is hidden (show:False) --
        # a viewer has no way to tell it's on a different scale than
        # power's own visible gridlines, so it must match exactly, not
        # just share the same zero-alignment.
        {"id": "load", "show": False, "min": -40, "max": 40},
        {"id": "soc", "show": True, "opposite": True, "min": -100, "max": 100, "apex_config": {"title": {"text": "SoC %"}}},
    ],
    "series": series,
}

with open(LOVELACE_PATH, encoding="utf-8") as f:
    data = json.load(f)


def _find_card(title: str):
    """Searches every view/section for an apexcharts-card with this exact
    header title, returning (cards_list, index) or None. Deliberately
    does NOT go via any view's own title -- 2026-08-21, real live break:
    the "NIMBUS FORECASTER" view got renamed to just "Forecaster" during
    a same-day dashboard reorganisation, and an old view-title-based
    lookup broke outright. This pattern survives any future view rename
    with zero issue.
    """
    for view in data["data"]["config"]["views"]:
        for section in view.get("sections", []):
            cards = section.get("cards", [])
            for i, c in enumerate(cards):
                if c.get("type") == "custom:apexcharts-card" and c.get("header", {}).get("title") == title:
                    return cards, i
    return None


found = _find_card(CARD_TITLE)
if found is not None:
    cards, existing_idx = found
    cards[existing_idx] = card
    action = "Updated existing"
else:
    # Genuinely first-ever run, or the card was deleted and needs
    # recreating -- fall back to the first view's first section.
    cards = data["data"]["config"]["views"][0]["sections"][0]["cards"]
    cards.append(card)
    action = "Inserted new"

with open(LOVELACE_PATH, "w", encoding="utf-8") as f:
    json.dump(data, f)

print(f"{action} '{CARD_TITLE}' card with {len(series)} series ({len(load_forecasts)} loads, "
      f"{len(signal_forecasts)} power signals + Whole House + Solver + Temp/Humidity). Saved.")
for e in series:
    tag = " [standout]" if e.get("opacity") == 0.5 else ""
    print(f"  {e['name']}{tag}  <-  {e['entity']}")
