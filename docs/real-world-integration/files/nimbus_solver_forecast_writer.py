#!/usr/bin/env python3
"""Runs the Nimbus Solver over a real 96h tiered horizon (24h fine-
grained @ 15-min + 72h coarse @ 1-hour, see TIER1_*/TIER2_* constants
and build_tiered_grid() for why) using real live sensor data, and pushes
the resulting proposed battery plan to
sensor.nimbus_solver_battery_forecast so it can be plotted on a real
dashboard -- same REST-push pattern as lv_p2p_forecast_writer.py,
haeo_forecast_to_influxdb.py, and every other forecast writer already
running in this project.

OBSERVATION ONLY -- this script only ever calls GET then one POST to a
plain sensor. It never calls number.set_value, never calls a script,
never touches Modbus. The Solver package itself
(custom_components/nimbus_load/solver/) has zero Home Assistant imports
and is not wired to anything live -- this script is the very first (and
so far only) thing that ever calls it against real data, and even this
only produces a number to look at, not an action.

PLATFORM REQUIREMENT -- this is a plain HOST cron script, not something
HACS installs or HA runs for you. It needs real host-level shell + cron
access to deploy at all, which means Docker or Supervised installs
only -- Home Assistant OS has no general shell/cron surface for this to
run on (same class of gap this project already solved once for a
different writer via a pure rest:/template: HA-native rewrite -- see
docs/localvolts-p2p-integration/files/*_haos.yaml in the sibling
116KAT-HA-AI repo if that pattern is ever worth porting here). The
Solver's own config-flow "Solver settings" wizard (Nimbus hub ->
Configure) installs and works fine via HACS on ANY platform including
HAOS -- it's specifically producing a LIVE forecast that needs this
separate script running somewhere with shell access.

Deliberately reads LocalVolts' own native price sensors
(sensor.localvolts_price_forecast, sensor.localvolts_p2p_price_forecast)
rather than HAEO's already-blended number.grid_import_price/
grid_export_price -- per this project's own standing rule that Nimbus
(and anything built on it, including this Solver test) must never
reference a HAEO entity, since Nimbus exists to be a genuine HAEO
alternative, not something that depends on it.

PREREQUISITE (2026-08-20, replaces the old input_number-package-file
step below): this script now reads its battery/grid/price config from
sensor.nimbus_solver_config -- a real bridge sensor that mirrors
whatever the household filled in through Nimbus's own HA-native
"Configure" -> "Solver settings" wizard (see fetch_solver_config()'s own
docstring for the full "installable by anyone, not just this household"
reasoning). This closes the gap a fresh install (Mark Purcell, or
anyone) used to hit: no more hand-created YAML package file, no more
Python constants to edit -- just a form in the HA UI. Before deploying
THIS script, on the NUC that will run it:
  cd /opt/homeassistant/config/nimbus_repo && git pull origin main
  # nimbus_repo's own custom_components/nimbus_load/ is a Python
  # custom_component -- a config/module change here ALWAYS needs a full
  # restart to load (a reload_all cannot reload changed Python modules,
  # per this project's own documented rule). Needs nimbus >= 0.32.0 (the
  # version that added sensor.nimbus_solver_config itself) -- check
  # custom_components/nimbus_load/manifest.json's own "version" field.
  docker restart opt_homeassistant_1
  # Then, in the HA UI: Settings -> Devices & services -> Nimbus ->
  # Configure -> "Solver settings" -- fill in every field across all 6
  # steps (Battery / Power / Grid / Price & Forecast Sources / Economic
  # Policy / P2P, the last one optional -- leave blank if this household
  # has no community-trading/P2P scheme at all). Confirm
  # sensor.nimbus_solver_config reads state "configured" (Developer
  # Tools -> States) before running this script -- fetch_solver_config()
  # raises a clear RuntimeError naming exactly what's still missing if
  # this is skipped, rather than a confusing crash deep inside network.py.

Deploy (run via cron on whichever NUC currently holds the VIP -- this
script runs on the NUC HOST, not inside the HA container, same as every
other writer script in this project):
  cd /opt/homeassistant && git pull origin main
  git show origin/main:scripts/nimbus_solver_forecast_writer.py > /opt/nimbus_solver_forecast_writer.py
  python3 -c "import numpy" || pip3 install --user numpy
  python3 -c "import highspy" || pip3 install --break-system-packages highspy
  # highspy is the real, compiled LP solver lp.py imports at module load
  # time (import highspy, near the top of solver/lp.py) -- without it,
  # the very first run crashes immediately with ModuleNotFoundError,
  # before this script's own config/entity checks ever get a chance to
  # run. --break-system-packages is needed on Debian-family hosts (PEP
  # 668) since this isn't going in a venv -- confirmed live 2026-08-18
  # installing a real matching manylinux wheel with zero build-from-
  # source needed, on this project's own Debian-based NUC. Genuinely
  # untested on any OS/architecture outside this household -- if the
  # wheel doesn't exist for your platform, that's real, new information
  # worth reporting back, not something to assume will "just work."
  # /opt is root-owned -- homehub cannot create a brand-new file directly
  # inside it (documented project-wide convention, CLAUDE.md "NUC Script
  # Deployment" section). Pre-create the log file with the right
  # ownership BEFORE the first cron tick, or cron's own `>>` redirect
  # fails silently every single run and python3 never even starts --
  # confirmed live 2026-08-17, this exact gap: cron was correctly
  # scheduled, but "Entity not found" persisted indefinitely because the
  # log file (and therefore the script itself) had never once actually
  # run.
  sudo touch /opt/nimbus_solver_forecast_writer.log && sudo chown homehub:homehub /opt/nimbus_solver_forecast_writer.log
  python3 /opt/nimbus_solver_forecast_writer.py   # one-off test run first
  (crontab -l 2>/dev/null; echo "* * * * * python3 /opt/nimbus_solver_forecast_writer.py >> /opt/nimbus_solver_forecast_writer.log 2>&1") | crontab -
  # 2026-08-17, direct real ask: "we want to be better not behind" (vs
  # HAEO's own faster cadence) -- was */5 (before that, */15). Genuinely
  # the fastest a 1-tick-per-run cron CAN safely go: the real LP solve
  # measured live this session at 44.95s-52.31s, leaving real but not
  # huge margin under a 60s tick. A bare `* * * * *` alone would risk a
  # slow run still executing when the next tick fires (two solves
  # competing for CPU, writing conflicting plan-state files) -- see
  # acquire_lock()/release_lock() below, a real PID-file overlap guard
  # that makes this safe: a tick that fires while a previous run is
  # still genuinely in progress exits cleanly instead of ever running
  # concurrently, rather than needing a slower, more conservative
  # cadence "just in case."

Token: /home/homehub/.ha_token (same file every other writer script uses)
Solver source: /opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load/solver/
(the real git clone of code-imstillalive/nimbus -- see CLAUDE.md's own
"What is and isn't tracked in git" section for this layout)
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Real, confirmed-live bug (2026-08-17): this script's own docstrings
# used to assert "this NUC runs Australia/Brisbane" and relied on plain
# `.astimezone()` (no argument -- converts to whatever the SYSTEM's own
# local timezone resolves to) throughout, on that assumption. Confirmed
# live this was WRONG: a real deploy run's own generated_at (compared
# directly against the real wall-clock AEST time at that exact moment,
# from the deploy conversation's own timeline) showed a UTC offset
# (+00:00), not +10:00 -- meaning `.astimezone()` on the NUC's own cron/
# shell environment silently resolves to UTC, not AEST, most likely
# because the environment this script actually runs in (cron, or the
# interactive shell used to test it) doesn't carry a correctly-resolved
# TZ, even though the box's own /etc/timezone may well be set correctly
# for everything else. Consequence: EVERY hour-of-day decision in this
# file (network_energy_rate's TOU schedule, battery_discharge_cost_rate,
# battery_salvage_value_rate, and resample_p2p_forecast's own P2P-window
# check) was evaluating against UTC hour, a consistent 10-hour offset
# from the real intended AEST hour -- e.g. the real 17:00-24:00 P2P
# window was actually being checked as UTC 17:00-24:00, which is AEST
# 03:00-10:00 the NEXT day.
#
# Fix: never trust the system's own local-timezone resolution. Every
# real-local-time value in this file is built from BRISBANE_TZ
# explicitly (zoneinfo, Python 3.9+ stdlib -- Brisbane has no DST, so
# this is also simpler and more robust than any UTC-offset arithmetic).
BRISBANE_TZ = ZoneInfo("Australia/Brisbane")

sys.path.insert(0, "/opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load")
from solver import elements, network  # noqa: E402
import numpy as np  # noqa: E402

HA_BASE = "http://localhost:8123"
TOKEN_PATH = "/home/homehub/.ha_token"
ENTITY_ID = "sensor.nimbus_solver_battery_forecast"
# Real state file for plan-to-plan stability (2026-08-16, real finding:
# two solves 4 minutes apart, same code, produced total_cost -$31 vs
# -$15 -- a thin, marginal arbitrage opportunity (buy overnight at
# ~$0.22, sell tomorrow's P2P window at ~$0.336) flipped the LP's whole
# strategy). network.py's own module docstring already documents real,
# tested machinery for exactly this ("the user's own explicit concern:
# 'i do not want a dumb algorithm... clever and responsive but not
# chaotic'") -- this writer just never wired it up. See save_plan_state()/
# load_previous_plan() below. Per the usual /opt-is-root-owned gotcha,
# this file needs a one-time `sudo touch` + `chown` on first deploy.
PLAN_STATE_PATH = "/opt/nimbus_solver_last_plan.json"
# Real PID-file overlap guard (2026-08-17, see the deploy docstring's own
# "* * * * *" comment above) -- per the usual /opt-is-root-owned gotcha,
# this file needs the same one-time `sudo touch` + `chown` on first
# deploy as PLAN_STATE_PATH.
LOCK_PATH = "/opt/nimbus_solver_forecast_writer.lock"

# Tiered horizon (2026-08-16, real ask: "how about 5 days forecast?" /
# "how about 96hrs?"), same real architecture HAEO's own horizon already
# uses ("minute-resolution tiers for the first 5 minutes, then 5-min
# tiers, then hourly" -- this project's own documented HAEO design).
#
# Real, precisely measured finding that drove this: this solver's own
# dense-tableau simplex (see lp.py) scales roughly CUBICALLY in period
# count (measured: 24 periods=0.09s, 48=0.59s, 96=4.41s, 144=14.35s,
# 192=36.84s -- iteration count grows near-linearly, but time-per-
# iteration grows near-quadratically, since the dense tableau's own
# per-pivot cost is O(rows x cols) and both dimensions scale with period
# count). A flat, uniform 15-min grid across a real 96h horizon needs 384
# periods -- measured at 197s, too close to a 15-min cron cadence to
# trust. Tiering the SAME 96h into fine-near/coarse-far periods instead
# needs only ~168 periods (~23s estimated from the measured curve) for
# the identical real-world coverage.
#
# Also matches where the real underlying data itself runs out of
# precision, so coarsening far-out periods doesn't discard any real
# signal it never had: solar/load forecasts genuinely cover the full 96h
# (Nimbus's own Forecaster), but the LocalVolts price forecasts only
# cover ~12h (import) / ~36h (P2P export) -- resample_forecast() already
# just holds the last known value flat past that point regardless of how
# many periods represent it, so fewer, coarser periods there is more
# honest, not less accurate.
# 2026-08-17, direct real ask: "why is there 15min spans, not 1min...
# for first 5min and then every 5min" -- three tiers now, not two.
# TIER0: the first 5 real minutes at 1-min resolution (the genuinely
# imminent, most decision-relevant window). TIER1: 5-min resolution
# (was 15-min) for the SAME real 24h span tier1 has always covered.
# Real, accepted tradeoff, not free: this roughly DOUBLES the total
# period count (tier1 alone goes 96 -> 288 periods), and the real
# measured LP solve time at the OLD ~169-period size was already
# 44.95s-52.31s against a 60s cron tick (see acquire_lock()'s own
# comment). A slower solve here doesn't break anything -- the same
# overlap lock that already exists for exactly this reason just skips
# a tick cleanly if the previous run is still solving, degrading from
# "every minute" to "every ~1.5-2 minutes" in the worst case rather
# than ever running two solves concurrently or crashing.
TIER0_MINUTES = 5.0        # ultra-fine tier: how far out 1-min resolution runs
TIER0_PERIOD_MINUTES = 1.0
TIER1_HOURS = 24.0        # fine tier: how far out 5-min resolution runs (from TIER0's own end)
TIER1_PERIOD_HOURS = 5.0 / 60.0
TIER2_HOURS = 72.0        # coarse tier: additional span beyond tier 1
TIER2_PERIOD_HOURS = 1.0  # -> 24h + 72h = 96h total (plus tier0's own 5 real minutes), ~360 periods

# Real, bill-confirmed TOU network rates and certificates rate (2026-08-16,
# real ask: "it needs ot be super accurate") -- reused directly from this
# project's own already bill-verified lv_costs.yaml rate table (Energex
# NTC 6900 Residential TOU Energy), not re-derived. Baked directly into
# import_price[t] below (not just reported after the fact) so the LP's
# own DISPATCH decision correctly avoids importing during real peak
# hours, not just the reported total_cost number.
NETWORK_ENERGY_PEAK_RATE = 0.214863    # $/kWh, 16:00-21:00
NETWORK_ENERGY_OFFPEAK_RATE = 0.00476  # $/kWh, 11:00-16:00
NETWORK_ENERGY_SHOULDER_RATE = 0.066759  # $/kWh, all other hours
CERTIFICATES_RATE = 0.008246  # $/kWh, flat, real bill-confirmed

# Real fixed daily charges (Network Access $0.85 + LV Fee $1.10),
# independent of dispatch -- reported honestly alongside total_cost, not
# fed into the LP (a flat cost can't change an optimal LP decision, only
# shift the objective by a constant).
FIXED_DAILY_CHARGES = 1.95  # $/day, real bill-confirmed

# Real empirical fallback if the live confirmed-history sensor is
# unavailable for some reason -- roughly the 13-day all-time average
# (0.686) as of 2026-08-16, NOT the more-accurate live-computed recent
# average this writer normally uses (see p2p_match_fraction() below).
# Still computed/reported for informational context (pushed as
# p2p_match_fraction in the sensor's own attributes) -- no longer used
# to PRICE the LP, see export_bonus_price/export_bonus_volume_kwh below.
P2P_MATCH_FRACTION_FALLBACK = 0.65

# Confidence-aware dispatch (2026-08-17, real "keep building the Solver's
# own real inputs" ask). Nimbus's own Forecaster sensors already carry a
# real, genuine lower/upper confidence band per forecast point
# (confirmed live: sensor.nimbus_combined_total_dc_power_forecast's own
# forecast array has {time, value, lower, upper} keys) -- built and
# validated as part of the Forecaster's own GBRT-quantile / calibrated-
# residual machinery, not invented for this. network.py's own
# build_plan() has had a fully-built, tested risk_aversion mechanism for
# exactly this since before this writer ever ran (see its own
# "CONFIDENCE-AWARE DISPATCH" docstring section) -- it was simply never
# wired up: this writer used to call resample_forecast(..., "value", ...)
# only, silently discarding the real lower/upper fields sitting right
# there in the same response.
#
# 0.25 is a real, deliberately MODEST choice, not the mechanism's own
# default-safe 0.0 (would waste real, already-computed data) or its
# extreme 1.0 (would plan for the full pessimistic bound every period,
# regardless of how tight/confident that period's own real forecast is
# -- this project's own many prior sessions of P2P dispatch tuning were
# all done against risk_aversion=0.0 behaviour; jumping straight to an
# aggressive setting risks visibly changing already-tuned dispatch
# timing on the very first deploy of a brand-new, never-live-tested
# mechanism). A real, tunable lever going forward -- raise it if the
# Solver is later found under-provisioning against real forecast misses,
# lower it (back to 0.0) if 0.25 is ever found overly conservative.
#
# 2026-08-21 (task #128): this is now only the FALLBACK default, read
# once at deploy time -- the LIVE value comes from
# number.nimbus_solver_risk_aversion (dashboard-editable, per the direct
# household ask for "a flexible sliding charging urgency control"),
# fetched fresh from cfg on every solve. See main()'s own risk_aversion=
# read for the exact fallback logic (falls back to this constant only if
# the live entity is somehow unavailable, not on every run).
RISK_AVERSION = 0.25

# Real empirical fallback for the two-tier export bonus's own volume cap
# (see p2p_recent_avg_volume_kwh() below) -- roughly matches this
# household's own long-documented ~60-65kWh/night real P2P delivery
# (session history, sibling 116KAT-HA-AI repo's own CLAUDE.md).
P2P_RECENT_AVG_VOLUME_FALLBACK_KWH = 60.0

# Real per-load demand, summed from Nimbus's own 18 individually-
# forecasted circuit breakers (2026-08-17, direct ask: "i was hoping not
# to need whole house load if all 18 loads could be individually input
# and measured and added into a total"). Confirmed live this is the
# COMPLETE real load list -- every genuinely metered circuit on this
# system's own Zigbee CB network, 18 entities, matching this project's
# own long-documented "18 Nimbus-forecast Loads" count exactly (sibling
# 116KAT-HA-AI repo's own CLAUDE.md, repeated across many sessions).
# The single whole-house aggregate this writer used to read instead is
# kept ONLY as a real, independent cross-check (see
# WHOLE_HOUSE_CROSS_CHECK_SOURCE_SENSOR below, reported but never used
# to price/dispatch anything) -- not removed outright, since a real
# divergence between "sum of 18 real circuits" and "one real whole-house
# meter" is itself useful, honest information (a missed/newly-added
# circuit, a sensor drift) worth surfacing, not hiding.
#
# 2026-08-20: this constant is the RAW SENSOR that whole-house signal is
# derived from, not its own forecast entity_id -- the forecast entity
# name is derived from it at read time (see below) specifically so a
# future reconfigure (task #99's own auto-rename mechanism) can never
# leave this cross-check silently pointing at a dead, renamed entity_id
# again. Real, live-confirmed incident this replaces (2026-08-20): the
# household's own "Whole House" Power Signal was reconfigured from
# sensor.logger_load_power (the raw, noisy Modbus meter -- see this
# project's own CLAUDE.md, "real P2P-window grid spikes root-caused")
# to sensor.cb_total_combined_power_adjusted_kw. Task #99's auto-rename
# correctly renamed the live forecast entity to match -- but this
# writer's own hardcoded WHOLE_HOUSE_CROSS_CHECK_ENTITY was still the
# OLD literal forecast entity_id, confirmed 404 the very next run.
LOAD_FORECAST_ENTITIES = [
    "sensor.nimbus_cb_pw_hws_l1_power_forecast",
    "sensor.nimbus_cb_pw_hws_l3_power_forecast",
    "sensor.nimbus_cb_pw_pool1_power_forecast",
    "sensor.nimbus_cb_pw_pool_2_power_forecast",
    "sensor.nimbus_cb_pw_comms_power_forecast",
    "sensor.nimbus_cb_pw_ldry_power_forecast",
    "sensor.nimbus_cb_pw_b1_power_forecast",
    "sensor.nimbus_cb_pw_l1_power_forecast",
    "sensor.nimbus_cb_lt_l1_power_forecast",
    "sensor.nimbus_cb_lt_l2_power_forecast",
    "sensor.nimbus_cb_pw_l2_power_forecast",
    "sensor.nimbus_cb_pw_ac_l1_power_forecast",
    "sensor.nimbus_cb_pw_ac_l2_power_forecast",
    "sensor.nimbus_cb_pw_ac_b1_power_forecast",
    "sensor.nimbus_cb_pw_ctp_power_forecast",
    "sensor.nimbus_cb_pw_oven_power_forecast",
    "sensor.nimbus_cb_pw_lounge_power_forecast",
    "sensor.nimbus_cb_pw_heater_power_forecast",
]
WHOLE_HOUSE_CROSS_CHECK_SOURCE_SENSOR = "sensor.cb_total_combined_power_adjusted_kw"

# Real, known, permanent inverter self-consumption bias (2026-08-17,
# direct household confirmation: "the only thing which differs is
# adjustment sensor of 0.215kw") -- the Sungrow logger's own internal
# accounting draws a constant real ~215W nothing on the Zigbee CB
# network can ever see (it's wired into the inverter itself, not a
# monitored circuit), 24/7. Already a real, established correction in
# this project (sensor.cb_total_combined_power_adjusted_kw, sibling
# 116KAT-HA-AI repo, adds the identical constant for the same reason).
# Confirmed live this same session: raw summed-18-circuits (2.18kW) +
# this constant (0.215kW) = 2.395kW, matching the real whole-house
# meter's own live reading (2.4kW) almost exactly -- without this, the
# 18-load sum would silently under-serve real demand by ~215W every
# single period (≈20.6kWh of real, invisible demand across a 96h
# horizon), biasing the LP toward under-provisioning battery/import.
INVERTER_SELF_CONSUMPTION_KW = 0.215


def network_energy_rate(hour: int) -> float:
    """Real Energex NTC 6900 TOU schedule, same window every day
    (Australia/Brisbane, no DST) -- see lv_costs.yaml's own docstring."""
    if 16 <= hour < 21:
        return NETWORK_ENERGY_PEAK_RATE
    if 11 <= hour < 16:
        return NETWORK_ENERGY_OFFPEAK_RATE
    return NETWORK_ENERGY_SHOULDER_RATE


# Real, git-tracked battery cost schedule (config/automations.yaml, "HAEO
# Battery Cost Schedule - 5pm/Midnight/7am") -- reused directly, not
# re-derived. 2026-08-16, direct real finding: this writer used to read
# number.battery_discharge_cost's LIVE value ONCE and apply it flat
# across the whole multi-day horizon -- at whatever time the writer
# happens to run, that's the WRONG value for most of the horizon (e.g.
# captured 0.09, the daytime rate, applied to the real overnight window
# where the deployed automation actually uses 0.01). Confirmed this was
# the real, direct cause of a household reporting the Solver's own plan
# going idle overnight instead of discharging to serve load -- at the
# wrong flat 0.09, discharging looked far less obviously favourable than
# the real 0.01 makes it. charge_cost is deliberately NOT scheduled here
# -- the real automations explicitly never touch it (see automations.
# yaml's own description: "under manual real-time control"), so this
# writer still reads its current live value as a flat scalar, same as
# before. Requires the Solver's own BatteryConfig.discharge_cost to
# accept a real per-period array, not just a scalar (see the separate
# nimbus repo commit "BatteryConfig.charge_cost/discharge_cost: allow a
# real per-period array").
BATTERY_DISCHARGE_COST_NIGHT = 0.01  # 5pm-7am (P2P window + midnight-7am)
BATTERY_DISCHARGE_COST_DAY = 0.09    # 7am-5pm
BATTERY_SALVAGE_VALUE_NIGHT = 0.3    # 5pm-midnight (P2P window only)
BATTERY_SALVAGE_VALUE_OTHER = 0.15   # midnight-5pm


def battery_discharge_cost_rate(hour: int) -> float:
    return BATTERY_DISCHARGE_COST_NIGHT if (hour >= 17 or hour < 7) else BATTERY_DISCHARGE_COST_DAY


def battery_salvage_value_rate(hour: int) -> float:
    """Salvage value only applies ONCE, to the horizon's own FINAL
    period, so this doesn't need a full per-period array -- just needs
    to reflect what the real schedule would set at whatever real hour
    the horizon happens to end at, not whatever's live right now."""
    return BATTERY_SALVAGE_VALUE_NIGHT if hour >= 17 else BATTERY_SALVAGE_VALUE_OTHER


def terminal_value_breakpoints_for(base_rate: float, min_soc_kwh: float, max_soc_kwh: float) -> list:
    """Concave piecewise-linear terminal value (Solver audit item #7,
    Nimbus PR #35) -- switched on live 2026-08-19, replacing the flat
    salvage_value mechanism above. Proven on 2 real household nights
    (2026-08-16/17) to be ~$3.12-3.15/day MORE profitable AND to avoid
    the flat mechanism's own confirmed real pathology: driving straight
    to a hard SoC corner every night (100% or the floor) with zero
    smooth transition -- the exact same class of behaviour HAEO itself
    was caught live doing on 2026-08-19 (different root cause -- a
    drifted 100% efficiency setting -- but the identical symptom).

    Same 3-segment shape already validated this session in
    scripts/research/forward_value_comparison.py's own real-data
    comparison, calibrated here to the SAME average $/kWh as whatever
    flat salvage_value rate it replaces (base_rate), so switching this
    on reflects a change in CURVATURE, not a change in how much total
    terminal value is being modeled -- the household isn't being handed
    a different valuation, just a smoother one.
    """
    above_floor = max_soc_kwh - min_soc_kwh
    return [
        (above_floor * 0.15, base_rate * 2.2),
        (above_floor * 0.55, base_rate * 1.0),
        (above_floor * 0.30, base_rate * 0.35),
    ]

with open(TOKEN_PATH, "r", encoding="utf-8") as f:
    TOKEN = f.read().strip()


def ha_get(entity_id: str) -> dict:
    req = urllib.request.Request(
        f"{HA_BASE}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def entity_exists(entity_id: str) -> bool:
    """Real existence check, not just "did the last read happen to
    succeed" -- used to gate the optional LocalVolts/AEMO-specific price-
    forecasting enhancement below (2026-08-20, see fetch_solver_config()'s
    own docstring for the full "installable by anyone" context). A caller
    without LocalVolts configured shouldn't get an HTTPError crash just
    because this ONE household happens to have it -- this is the genuine
    portability boundary, checked live, not assumed from config alone.
    """
    try:
        ha_get(entity_id)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def fetch_solver_config() -> dict:
    """The real Solver settings a household fills in through Nimbus's own
    Configure -> "Solver settings" wizard (nimbus repo,
    flows/hub_options.py), bridged out via sensor.nimbus_solver_config
    (nimbus repo, sensor.py's own NimbusSolverConfigSensor) since
    config_entries.options isn't exposed over HA's plain REST API
    (confirmed live 2026-08-20 -- /api/config/config_entries/entry only
    returns entry metadata, never the entry's own options dict).

    2026-08-20, direct household ask, following a genuinely honest self-
    assessment of how installable this Solver actually is for someone
    else (Mark Purcell, or anyone): "close this gap... or get rid of it
    totally - need its own installer and inputs period." Before this
    function existed, main() read battery/grid config from a set of ad-
    hoc input_number.nimbus_solver_* helpers that had to be hand-created
    via a separate YAML package file -- undocumented, NUC-specific,
    genuinely not something a fresh installer could discover on their
    own. This function (and the config-flow/bridge-sensor behind it) is
    what actually closes that gap: a real install now needs nothing more
    than filling in Nimbus's own hub "Configure" form.

    Raises a clear, actionable RuntimeError -- not a confusing KeyError
    deep inside network.py -- if the Solver hasn't been configured yet.
    """
    state = ha_get("sensor.nimbus_solver_config")
    if state["state"] != "configured":
        msg = (
            "Nimbus Solver is not configured yet. Open the Nimbus hub's own "
            "\"Configure\" button in Home Assistant, choose \"Solver settings\", "
            "and fill in every required field (battery capacity/SoC sensor, "
            "max charge/discharge power, grid import/export limits, live "
            "import/export price sensors, solar/load forecast sensors) "
            "before running this writer."
        )
        raise RuntimeError(msg)
    return state["attributes"]


def ha_post_state(entity_id: str, state, attributes: dict) -> None:
    body = json.dumps({"state": state, "attributes": attributes}).encode("utf-8")
    req = urllib.request.Request(
        f"{HA_BASE}/api/states/{entity_id}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fetch_load_forecast_safe(entity_id: str) -> list[dict]:
    """Real, per-entity-guarded fetch for ONE of the 18 real load
    forecasts (2026-08-17, direct ask: "and individually wrapped into
    float 0"). Returns [] (never raises) on ANY failure -- entity
    missing/renamed, HTTP error, malformed JSON, no "forecast" attribute
    at all -- so sum_load_forecasts() below can treat a genuinely
    unavailable circuit as a safe, honest 0.0 contribution rather than
    let it corrupt or crash the whole 18-load sum.

    Same real lesson already learned and documented once this project
    (sibling 116KAT-HA-AI repo's own CLAUDE.md, 2026-08-16 session,
    sensor.cb_total_combined_power_adjusted_kw): 18 independent
    Zigbee-mesh circuit sensors collectively have a meaningfully HIGHER
    chance that "at least one is briefly offline" than a single Modbus
    connection does -- guarding only the OUTER sum, not each individual
    term, doesn't help (a raw string-concatenation or KeyError from one
    bad entity happens before any outer guard gets a chance to catch
    it). Every one of the 18 fetches below is wrapped exactly this way,
    individually, not just the total.
    """
    try:
        return ha_get(entity_id)["attributes"]["forecast"]
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        print(f"WARN: {entity_id} unavailable ({e}) -- treating as 0.0 kW for this solve", file=sys.stderr)
        return []


def sum_load_forecasts(
    entity_ids: list[str], grid_times: list[datetime]
) -> tuple[list[float], list[float], list[float], list[str]]:
    """Real household demand, summed from Nimbus's own 18 individually-
    forecasted circuit breakers -- see LOAD_FORECAST_ENTITIES's own
    comment for the full "why sum 18 instead of one whole-house entity"
    reasoning. Each of the 18 is fetched via fetch_load_forecast_safe()
    (individually guarded, never crashes the whole sum) and resampled
    with the SAME resample_forecast() every other forecast entity in
    this file already uses -- no new resampling logic needed.

    lower_kw/upper_kw are summed the same way (sum of each load's own
    real per-load lower bound, sum of each load's own real per-load
    upper bound) -- a real, honest, deliberately CONSERVATIVE choice:
    this assumes every load's own worst case lands simultaneously,
    which is more pessimistic than a genuine independent-uncertainty
    combination (e.g. sqrt of summed variances) would be for 18 mostly-
    unrelated loads. Chosen anyway because it's simple, transparent, and
    matches exactly what RISK_AVERSION's own "plan for the pessimistic
    bound" mechanism is FOR -- never understates real risk, at the cost
    of being somewhat more conservative than a true independence
    assumption would justify. A real, stated limitation, not hidden.

    Also returns the real list of entity_ids that failed and were
    silently defaulted to 0.0 this run (2026-08-17, direct ask: "the
    warning would appear in the topology card... green/red dot?") --
    pushed as its own sensor attribute (see main()'s own
    ENTITY_ID_LOAD_TOTAL push) so a future topology-card change can
    cross-reference this list against its own already-built per-load
    health dots (see the sibling repo's own topology-card.js, PR #611)
    without needing to poll each of the 18 entities itself.
    """
    total_kw = [0.0] * len(grid_times)
    total_lower_kw = [0.0] * len(grid_times)
    total_upper_kw = [0.0] * len(grid_times)
    failed_entities: list[str] = []
    for entity_id in entity_ids:
        fc = fetch_load_forecast_safe(entity_id)
        if not fc:
            failed_entities.append(entity_id)  # already warned inside fetch_load_forecast_safe(); this load contributes 0.0 for every period
            continue
        pt_kw = resample_forecast(fc, "value", grid_times)
        pt_lower = resample_forecast(fc, "lower", grid_times)
        pt_upper = resample_forecast(fc, "upper", grid_times)
        for i in range(len(grid_times)):
            total_kw[i] += max(0.0, pt_kw[i])
            total_lower_kw[i] += max(0.0, pt_lower[i])
            total_upper_kw[i] += max(0.0, pt_upper[i])
    # Real, known, permanent inverter self-consumption bias (see
    # INVERTER_SELF_CONSUMPTION_KW's own comment) -- added flat to every
    # period, point AND band alike (a known constant carries no real
    # uncertainty of its own to widen the band with).
    total_kw = [v + INVERTER_SELF_CONSUMPTION_KW for v in total_kw]
    total_lower_kw = [v + INVERTER_SELF_CONSUMPTION_KW for v in total_lower_kw]
    total_upper_kw = [v + INVERTER_SELF_CONSUMPTION_KW for v in total_upper_kw]
    # Defensive bracket, same reasoning as solar/load's own clamp
    # elsewhere in this file: guarantee lower <= point <= upper even if
    # one per-load band was individually inconsistent (elements.py's own
    # _validate_confidence_band() requires this exactly, at every period).
    total_lower_kw = [min(total_lower_kw[i], total_kw[i]) for i in range(len(grid_times))]
    total_upper_kw = [max(total_upper_kw[i], total_kw[i]) for i in range(len(grid_times))]
    return total_kw, total_lower_kw, total_upper_kw, failed_entities


def resample_forecast(forecast: list[dict], value_key: str, grid_times: list[datetime]) -> list[float]:
    """Nearest-at-or-before lookup against the source's own native
    resolution -- must resample against the RAW forecast array, never an
    already-quantized grid (real bug found and fixed earlier in this
    build when this exact mistake flattened 5 of 6 test values)."""
    pts = sorted(
        ((parse_iso(p["time"]), p[value_key]) for p in forecast if p.get(value_key) is not None),
        key=lambda x: x[0],
    )
    out = []
    for gt in grid_times:
        val = pts[0][1] if pts else 0.0
        for t, v in pts:
            if t <= gt:
                val = v
            else:
                break
        out.append(float(val))
    return out


def resample_real_p2p_rate(grid_times: list[datetime]) -> list[float]:
    """Real, per-interval P2P export rate ($/kWh) -- REPLACES the old
    resample_p2p_forecast()/sensor.localvolts_p2p_price_forecast flat-
    $0.50-placeholder approach entirely (2026-08-20, direct household
    finding: "50c is an arbitrary unit we used to make HAEO believe...
    IT IS NOT THE PRICE IT ACTUALLY IS... actual price can vary from
    0-70c"). That placeholder was purpose-built as a workaround for
    HAEO's own specific LP-degeneracy problems with live per-period P2P
    data (see the sibling 116KAT-HA-AI repo's CLAUDE.md, session 41's PR
    #348) -- HAEO still genuinely needs it, and sensor.localvolts_p2p_
    price_forecast / lv_p2p_forecast_writer.py are UNCHANGED and
    deliberately left alone. Nimbus is a different system with its own
    stability mechanisms (proximal_weight, smoothness_weight) and has no
    reason to inherit a workaround built for a different LP's problems.

    Rate formula -- EXACTLY matches this project's own already-correct
    "P2P Trades Tonight" card (116KAT-HA-AI repo, scripts/lovelace_p2p_
    rate_none_not_zero.py / the live card's own Jinja):
        rate = matchedCost / (volume * proportionP2P)
    (that card displays cents; this returns dollars -- matched_vol is in
    kWh, matchedCost in $, so cost/matched_vol is already $/kWh directly,
    no *100/100 round-trip needed). Sourced from sensor.localvolts_p2p_
    forecast (a genuinely different sensor from the flat placeholder
    above -- this one carries real per-5-min matchedCost/volume/
    proportionP2P from LocalVolts' own live matching, the same real data
    the household's own reference card already uses).

    Verified live 2026-08-20, before building this, not assumed safe:
    (1) real economic plausibility -- forecast-quality (not yet settled)
    rates hours ahead showed genuine variation (43-65c), not a frozen
    template; (2) poll-to-poll stability -- the same future intervals,
    checked via two live polls 5 minutes apart, came back byte-identical
    (no drift/noise). That two-part check is what justifies feeding this
    directly into an LP as a live price signal, where the OLD flat-
    placeholder hack existed specifically because raw per-period P2P data
    was NOT trustworthy enough for HAEO's own architecture.

    Real points are keyed by INTERVAL START (time - 5min) -- LocalVolts
    labels every interval by its END, the same end-vs-start convention
    this project has hit and fixed more than once before for this exact
    sensor; matches the household's own reference card's own `ts = end_ts
    - timedelta(minutes=5)`. A period with ~0 real matched volume (most
    of the day, outside the real P2P window) contributes rate=0.0, same
    shape as the old placeholder's own zero-outside-window behaviour.

    Real coverage confirmed live 2026-08-20 to run out ~14h ahead (much
    shorter than the old placeholder's ~36h, since this is genuine live
    matching data, not a repeating template) -- beyond it, falls back to
    the MEDIAN of every real, meaningfully-matched rate seen within
    coverage, applied only during the real 17:00-24:00 local P2P window
    (median, not mean/max, specifically so one real outlier interval --
    e.g. a partial-minute settlement blip -- can't skew every future
    day's whole-window assumption). Same explicit 17<=hour<24 gate
    applied uniformly to BOTH branches (not just the extrapolated one) --
    this project already found and fixed a real bug once before
    (2026-08-17) where a stray nonzero in-coverage reading leaked outside
    the real window because only the extrapolation branch had the gate;
    not repeating that mistake here.
    """
    try:
        raw = ha_get("sensor.localvolts_p2p_forecast")["attributes"]["forecast"]
    except Exception:
        return [0.0 for _ in grid_times]

    pts = []
    for p in raw:
        try:
            end_t = parse_iso(p["time"])
            start_t = end_t - timedelta(minutes=5)
            vol = float(p.get("volume") or 0.0)
            prop = float(p.get("proportionP2P") or 0.0)
            cost = float(p.get("matchedCost") or 0.0)
            matched_vol = vol * prop
            rate = (cost / matched_vol) if matched_vol > 0.01 else 0.0
            pts.append((start_t, rate))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort(key=lambda x: x[0])
    if not pts:
        return [0.0 for _ in grid_times]

    last_real_time = pts[-1][0]
    real_positive_rates = [r for _, r in pts if r > 0.0]
    fallback_rate = statistics.median(real_positive_rates) if real_positive_rates else 0.0

    out = []
    for gt in grid_times:
        if not (17 <= gt.hour < 24):
            out.append(0.0)
        elif gt <= last_real_time:
            val = pts[0][1]
            for t, v in pts:
                if t <= gt:
                    val = v
                else:
                    break
            out.append(float(val))
        else:
            out.append(float(fallback_rate))
    return out


# 2026-08-20, direct household finding, live chart evidence: the Solver's
# own proposed P2P-window dispatch was swinging between near-40kW and
# near-zero, chasing whichever 5-min period showed the highest real per-
# interval P2P rate (resample_real_p2p_rate() above, itself a genuine fix
# earlier the same night) -- while the household's own real, live
# automation holds one flat, pre-committed rate for the whole window.
# Direct household explanation: P2P is a matching arrangement where
# CONSISTENCY of delivery is itself part of what earns the rate, not a
# plain price-taking market where each period can be independently re-
# decided. See solver.elements.GridConfig.fixed_export_kw's own docstring
# (nimbus repo) for the full LP-level mechanism this feeds.
#
# Deliberately a bare Python constant, same honest-portability pattern
# 2026-08-21: up to 3 independent, optional fixed-rate P2P delivery
# blocks, read from Nimbus's own sensor.nimbus_solver_config (already
# fetched into `cfg` by fetch_solver_config() -- no separate HTTP call
# needed here at all). Replaces the old single-window,
# household-specific input_number.p2p_grid_export_target_kw +
# hardcoded 17-24h check: that coupled Nimbus's own shadow plan directly
# to this household's real automation's own setpoint entity, which was
# never going to be portable to anyone else's install (a different rate,
# a different window, or MULTIPLE windows needed source editing). The
# real, deterministic p2p_battery_sell_5pm_midnight automation (config/
# automations.yaml, this same repo) is completely untouched by this --
# it's the actual, live, real-money dispatch mechanism, still reading
# its own input_number directly; Nimbus remains purely observational
# either way. This household's own real values (11.5kW, 17-24h) need
# setting once, manually, as Block 1 on the dashboard
# (number.nimbus_solver_p2p_block_1_rate_kw/start_hour/end_hour) --
# nothing carries over automatically from the old entity.
P2P_BLOCK_KEYS = (
    ("solver_p2p_block_1_rate_kw", "solver_p2p_block_1_start_hour", "solver_p2p_block_1_end_hour"),
    ("solver_p2p_block_2_rate_kw", "solver_p2p_block_2_start_hour", "solver_p2p_block_2_end_hour"),
    ("solver_p2p_block_3_rate_kw", "solver_p2p_block_3_start_hour", "solver_p2p_block_3_end_hour"),
)


def fetch_p2p_fixed_export_kw(cfg: dict, grid_times: list[datetime]) -> list[float] | None:
    """Builds the per-period fixed-export-rate array from however many of
    the 3 P2P blocks are actually configured (rate_kw > 0 -- see
    const.py's own comment on CONF_SOLVER_P2P_BLOCK_1_RATE_KW for why 0
    is the "not configured" signal, no separate enable flag needed).
    Returns None (a complete no-op, identical to no P2P scheme at all)
    if every block is unconfigured -- a fresh install with nothing set
    up sees grid_export stay fully LP-optimized against real spot
    prices, exactly as it already does with zero blocks filled in.

    Each grid_time is checked against every configured block's own
    [start_hour, end_hour) range; the first match wins (blocks are
    assumed non-overlapping -- a real household wouldn't configure two
    blocks covering the same hour, not worth defensive-coding against
    on day one, same call already made for the original single-window
    version). end_hour=24 correctly reaches through midnight, since
    Python's own datetime.hour never returns 24 -- any real hour (0-23)
    satisfies `< 24`.
    """
    blocks: list[tuple[float, int, int]] = []
    for rate_key, start_key, end_key in P2P_BLOCK_KEYS:
        try:
            rate_kw = float(cfg.get(rate_key) or 0.0)
            start_hour = int(cfg.get(start_key) or 0)
            end_hour = int(cfg.get(end_key) or 0)
        except (TypeError, ValueError):
            continue
        if rate_kw <= 0 or end_hour <= start_hour:
            continue
        blocks.append((rate_kw, start_hour, end_hour))

    if not blocks:
        return None

    result: list[float] = []
    for gt in grid_times:
        matched_rate = float("nan")
        for rate_kw, start_hour, end_hour in blocks:
            if start_hour <= gt.hour < end_hour:
                matched_rate = rate_kw
                break
        result.append(matched_rate)
    return result


def fetch_price_history(entity_id: str, days: int = 5) -> list[tuple[datetime, float]]:
    """Real recorded history for a single sensor's numeric state, as
    (local time, value) points -- the shared building block for
    compute_5min_offset() below. Returns [] (callers fall back further)
    if history is genuinely unavailable -- must never crash the writer.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    url = (
        f"{HA_BASE}/api/history/period/{start.strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}&end_time={end.strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    if not data or not data[0]:
        return []
    out: list[tuple[datetime, float]] = []
    for p in data[0]:
        try:
            v = float(p.get("state"))
        except (TypeError, ValueError):
            continue
        # Explicit BRISBANE_TZ conversion (2026-08-17 fix, see this
        # module's own top-of-file comment) -- matches grid_times' own
        # real local-hour convention (see main()'s own `now`
        # construction), regardless of what the running environment's
        # own system timezone happens to resolve to.
        out.append((parse_iso(p["last_changed"]).astimezone(BRISBANE_TZ), v))
    return sorted(out, key=lambda x: x[0])


def fetch_aemo_forecast() -> list[tuple[datetime, float]]:
    """Real, FORWARD-looking AEMO NEM QLD1 spot price forecast -- covers
    the FULL 96h horizon (confirmed live 2026-08-16: 367 real 30-min
    points, now -> +7.6 days), unlike LocalVolts (~24h real) or Amber
    (~23.7h real, confirmed via
    sensor.amber_express_116kathouse_forecast_horizon itself, not
    guessed). This is the only real price source on this system with
    genuine coverage all the way to the end of a 96h horizon, so it's
    the anchor for anything beyond LV's own real coverage.

    Uses the 'calibrated' field, NOT 'raw_value' -- real, direct finding
    (2026-08-16, user: "that is why mark written nem pd7 to take out
    these false aemo predictions ... 95% never happen"). Each forecast
    point carries BOTH: raw_value is AEMO's own uncalibrated spot
    forecast, which can predict extreme, low-probability spike events
    that mostly don't materialize (confirmed live: raw_value=8.999 for
    2026-08-19T19:00, 78h out, while the SAME point's own
    calibrated=0.105355 and spike_credible=False -- an explicit flag
    this integration computes specifically to say "don't trust the raw
    spike"). 'calibrated' (isotonic-regression corrected against real
    n_obs=41 historical observations at that horizon) is the field this
    integration exists to provide; using raw_value here would have fed
    the Solver a false, uncredible price signal.

    Only 30-min resolution (AEMO's own forecast granularity) -- the
    real 5-min-of-day structure comes from compute_5min_offset() below,
    layered on top of this coarser forward anchor. Returns [] (caller
    falls back further) if unavailable -- must never crash the writer.
    """
    try:
        fc = ha_get("sensor.nem_pd7day_qld1_nem_spot_price_forecast")["attributes"]["forecast"]
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return []
    return sorted(
        ((parse_iso(p["time"]), p["calibrated"]) for p in fc if p.get("calibrated") is not None),
        key=lambda x: x[0],
    )


def compute_5min_offset(real_history: list[tuple[datetime, float]], days: int = 5) -> dict[int, float]:
    """Real, empirical (LV retail price - AEMO wholesale spot) offset,
    binned by 5-MINUTE-of-day (288 buckets: hour*12 + minute//5) from
    real, multi-day RECORDED history for both sides -- not a single
    forecast snapshot.

    Real finding chain (2026-08-16, direct ask: "amber and lv are both
    5min measured intervals" / "the pricing should be per 5min intervals
    not per hour"): a single flat offset (computed from one day's
    forecast overlap) ranged +$0.0055 to +$0.24 -- useless. Binning that
    SAME single-day data by HOUR narrowed it (e.g. hour 18:
    0.2362-0.2403) but hour 16 still spanned 0.017-0.248 -- a real
    artifact of using one forecast snapshot, not a genuine repeating
    intra-hour pattern. Re-checked against 5 real days of actual
    RECORDED history (sensor.aemo_nem_qld1_current_5min_period_price vs
    sensor.costsflexup/earningsflexup, both genuinely 5-min resolution)
    binned at 5-min-of-day: every one of the 12 buckets inside that same
    16:00-17:00 hour is now tight across all 5 real days (e.g. 16:00:
    0.2267-0.2710) -- confirms the earlier hourly smear was a
    single-snapshot artifact, and that LV's real retail markup pattern
    genuinely has fine, repeatable 5-min-level structure worth capturing
    rather than averaging away.

    Falls back to an empty dict (caller then falls back further) if
    either side's real history is unavailable -- must never crash the
    writer.
    """
    aemo_history = fetch_price_history("sensor.aemo_nem_qld1_current_5min_period_price", days=days)
    if not real_history or not aemo_history:
        return {}

    def nearest_before(pts: list[tuple[datetime, float]], gt: datetime) -> float | None:
        val = pts[0][1]
        for t, v in pts:
            if t <= gt:
                val = v
            else:
                break
        return val

    by_bucket: dict[int, list[float]] = {}
    for t, real_v in real_history:
        aemo_v = nearest_before(aemo_history, t)
        if aemo_v is None:
            continue
        bucket = t.hour * 12 + t.minute // 5
        by_bucket.setdefault(bucket, []).append(real_v - aemo_v)
    return {b: sum(vals) / len(vals) for b, vals in by_bucket.items()}


def compute_price_percentile_band(price_history: list[tuple[datetime, float]], percentile: float) -> dict[int, float]:
    """Real, empirical price band by 5-MINUTE-of-day (288 buckets), from
    real multi-day recorded history -- same bucketing technique as
    compute_5min_offset() above, reused rather than re-derived. Builds
    GridConfig.import_price_upper/export_price_lower for price_risk_
    aversion (2026-08-21 -- see this project's own network.py docstring,
    "PRICE-FORECAST-ERROR HEDGING" section, and the direct household ask
    that drove it: "the forecasts are always wrong but they tend to be
    more expensive in the afternoons, so waiting is not a good idea...
    a flexible sliding charging urgency control").

    `percentile` in [0, 100] -- pass a HIGH percentile (e.g. 90) to build
    a pessimistic UPPER bound for import price (real historical evidence
    of how bad this time-of-day's price can genuinely get), a LOW
    percentile (e.g. 10) to build a pessimistic LOWER bound for export
    price. No pre-clamping against the point forecast needed here --
    network.py's own _risk_adjusted_one_sided() already guarantees a
    bound worse than the point forecast is a no-op (np.maximum(0.0, ...)),
    never inverts the adjustment -- confirmed by its own committed test,
    test_bound_worse_than_forecast_is_ignored_not_inverted.

    Falls back to an empty dict (caller then leaves price_risk_aversion
    as a complete no-op for whichever periods have no bucket -- see
    resample_price_with_extrapolation()'s own "hold flat" precedent) if
    history is unavailable -- must never crash the writer.
    """
    if not price_history:
        return {}
    by_bucket: dict[int, list[float]] = {}
    for t, v in price_history:
        bucket = t.hour * 12 + t.minute // 5
        by_bucket.setdefault(bucket, []).append(v)
    return {b: float(np.percentile(vals, percentile)) for b, vals in by_bucket.items()}


def apply_price_band(point_price: list[float], grid_times: list[datetime], band_by_5min: dict[int, float]) -> list[float] | None:
    """Maps a 5-min-of-day percentile band (compute_price_percentile_band())
    onto this solve's own real grid_times. Returns None (a complete no-op,
    matching GridConfig.import_price_upper/export_price_lower's own
    documented None-means-off default) if the band is empty (no history)
    -- a period with no matching bucket falls back to that period's own
    point price (network.py's own max(point, bound)/min(point, bound)
    logic then correctly treats this as "no adjustment," not a crash).
    """
    if not band_by_5min:
        return None
    out = []
    for i, gt in enumerate(grid_times):
        bucket = gt.hour * 12 + gt.minute // 5
        out.append(band_by_5min.get(bucket, point_price[i]))
    return out


def resample_price_with_extrapolation(
    forecast: list[dict],
    value_key: str,
    grid_times: list[datetime],
    aemo_pts: list[tuple[datetime, float]],
    offset_by_5min: dict[int, float],
) -> list[float]:
    """Same nearest-at-or-before lookup as resample_forecast() for any
    grid_time within the real forecast's own coverage -- but for periods
    BEYOND the last real data point, uses real AEMO forward spot data
    for that future period plus the real, 5-min-of-day retail offset
    (offset_by_5min, see compute_5min_offset()) instead of freezing the
    last real point flat. Falls back to the last real value if AEMO
    itself has no coverage for a given period (should not happen given
    AEMO's real 7.6-day coverage vs a 96h horizon, but defensive) --
    must never crash the writer.
    """
    pts = sorted(
        ((parse_iso(p["time"]), p[value_key]) for p in forecast if p.get(value_key) is not None),
        key=lambda x: x[0],
    )
    if not pts:
        return [0.0 for _ in grid_times]
    last_real_time = pts[-1][0]
    last_real_value = pts[-1][1]

    def nearest_before(source_pts: list[tuple[datetime, float]], gt: datetime) -> float | None:
        if not source_pts:
            return None
        val = source_pts[0][1]
        for t, v in source_pts:
            if t <= gt:
                val = v
            else:
                break
        return val

    out = []
    for gt in grid_times:
        if gt <= last_real_time:
            val = pts[0][1]
            for t, v in pts:
                if t <= gt:
                    val = v
                else:
                    break
            out.append(float(val))
            continue
        aemo_v = nearest_before(aemo_pts, gt)
        if aemo_v is not None:
            bucket = gt.hour * 12 + gt.minute // 5
            out.append(float(aemo_v + offset_by_5min.get(bucket, 0.0)))
        else:
            out.append(float(last_real_value))
    return out


def build_tiered_grid(now: datetime) -> tuple[list[datetime], list[float]]:
    """Real wall-clock period boundaries for the tiered horizon (see the
    TIER1_*/TIER2_* constants' own comment for why this exists).

    Tier 0 (1-min, first 5 real minutes) needs no bridge into tier 1
    (5-min, same 24h span tier1 has always covered): both are far finer
    than any real TOU rate boundary (which only ever changes on the
    hour), so there's no alignment concern the way tier1->tier2 has --
    5-min periods stack cleanly against 5-min periods regardless of
    exactly where tier0's own 5 minutes happened to end.

    Tier 2's own periods are snapped to real HOUR boundaries (not just
    "60 minutes after wherever tier 1 happened to end") -- `now` can be
    any real minute, so tier 1's own 24h-later end time is essentially
    never exactly on the hour. Snapping tier 2 to real clock hours keeps
    every coarse period's own network_energy_rate(hour) TOU lookup
    aligned to the REAL rate boundary (Peak/Off-peak/Shoulder switch
    exactly on the hour) -- using an un-snapped grid would let a single
    coarse period silently straddle a real rate change and get priced
    at only one side of it. The one bridging period (tier1_end -> the
    next whole hour) is genuinely shorter than a full hour and is given
    its own real, honest duration rather than rounded away.

    REAL BUG FOUND AND FIXED (2026-08-17, live report with an annotated
    screenshot: "why start on odd number that is weird!!! ... why not
    start with 00, 01, 02, 03, 04, 05, 10, 15, 20"). This function used to
    start tier 0 from raw `now` -- the real wall-clock moment the writer
    happened to run, seconds/microseconds included, no rounding at all.
    Since tier 1 then continued directly from wherever tier 0's own 5
    real minutes happened to land, EVERY period in the whole table
    inherited that same arbitrary offset -- confirmed live via the
    deployed forecast's own raw timestamps landing on :13/:28/:43/:58
    instead of any clean clock mark. (Separately, confirmed live the SAME
    session that this fix's own PREVIOUS version -- 5-min tier1 periods
    at all -- had never actually made it onto the live NUC despite an
    earlier turn's deploy claiming success: the live sensor's own
    `hours` field read a flat 0.25 for every period, the OLD 15-min-only
    build. Both a real code bug and a real deploy failure, found and
    fixed together.)

    Fix: round `now` UP to the next whole real MINUTE (tier 0's own
    start) -- then run 1-min tier-0 periods only as far as the next clean
    5-MINUTE mark (0-4 periods, whatever it actually takes to reach one;
    zero if `now` already rounds onto a clean 5-min mark), so tier 1
    always starts on a genuine :00/:05/:10/... boundary. This is the same
    real "snap to clock, don't drift with whatever `now` happens to be"
    principle already applied to tier 1->tier 2 below -- tier 0->tier 1
    just never had it.
    """
    times: list[datetime] = []
    hours: list[float] = []
    minute_start = now.replace(second=0, microsecond=0)
    if minute_start < now:
        minute_start += timedelta(minutes=1)
    tier1_start = minute_start
    while tier1_start.minute % 5 != 0:
        tier1_start += timedelta(minutes=1)
    t = minute_start
    while t < tier1_start:
        times.append(t)
        hours.append(TIER0_PERIOD_MINUTES / 60.0)
        t += timedelta(minutes=TIER0_PERIOD_MINUTES)
    tier1_end = tier1_start + timedelta(hours=TIER1_HOURS)
    t = tier1_start
    while t < tier1_end:
        times.append(t)
        hours.append(TIER1_PERIOD_HOURS)
        t += timedelta(hours=TIER1_PERIOD_HOURS)
    # t is now >= tier1_end (the loop's own last step may overshoot
    # tier1_end by less than one tier-1 period -- fine, tier 2 starts
    # from the real next-hour boundary regardless, not from `t` itself).
    tier2_start = tier1_end.replace(minute=0, second=0, microsecond=0)
    if tier2_start <= tier1_end:
        tier2_start += timedelta(hours=1)
    bridge_hours = (tier2_start - tier1_end).total_seconds() / 3600.0
    if bridge_hours > 1e-6:
        times.append(tier1_end)
        hours.append(bridge_hours)
    t = tier2_start
    horizon_end = tier1_start + timedelta(hours=TIER1_HOURS + TIER2_HOURS)
    while t < horizon_end:
        times.append(t)
        hours.append(TIER2_PERIOD_HOURS)
        t += timedelta(hours=TIER2_PERIOD_HOURS)
    return times, hours


def load_previous_plan() -> network.Plan | None:
    """Reconstruct a real network.Plan from the last successful solve's
    own persisted dispatch arrays, for passing as build_plan()'s own
    previous_plan= argument. Returns None (not an error) if the state
    file is missing, unreadable, or the last solve wasn't optimal -- this
    is a stability NICETY, not something that should ever crash the
    writer or block a solve.

    Deliberately does NOT check the file's own age/staleness here --
    build_plan()'s own _align_previous_periods() already handles that
    correctly: it only aligns periods that share a REAL matching wall-
    clock start time (within 1 second), so a genuinely stale previous
    plan (e.g. after a real cron gap) simply produces an empty alignment
    dict and the stability mechanisms become silent no-ops, exactly as
    if no previous_plan had been given at all. No extra logic needed.
    """
    try:
        with open(PLAN_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("status") != "optimal":
        return None
    try:
        hours_arr = np.array(data["period_hours"])
        n = len(hours_arr)
        periods = elements.PeriodGrid(hours=hours_arr, start=parse_iso(data["period_start"]))
        return network.Plan(
            status="optimal",
            periods=periods,
            battery_charge_kw=np.array(data["battery_charge_kw"]),
            battery_discharge_kw=np.array(data["battery_discharge_kw"]),
            battery_soc_kwh=np.zeros(n),  # not read by the stability mechanisms, zero-fill is fine
            grid_import_kw=np.array(data["grid_import_kw"]),
            grid_export_kw=np.array(data["grid_export_kw"]),
            export_bonus_kw=np.zeros(n),  # not read by the stability mechanisms, zero-fill is fine
            solar_used_kw=np.zeros(n),
            solar_curtailed_kw=np.zeros(n),
            sheddable_loads=[],
            adequacy_loads=[],
            total_cost=None,
            iterations=0,
        )
    except (KeyError, ValueError):
        return None


def save_plan_state(plan: network.Plan, period_hours_arr: list[float], period_start: datetime) -> None:
    """Persist this solve's own dispatch arrays for the NEXT run's
    load_previous_plan() to pick up. Best-effort -- a failure here
    should never take down an otherwise-successful solve."""
    try:
        with open(PLAN_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "status": plan.status,
                    "period_start": period_start.isoformat(),
                    "period_hours": list(period_hours_arr),
                    "battery_charge_kw": plan.battery_charge_kw.tolist(),
                    "battery_discharge_kw": plan.battery_discharge_kw.tolist(),
                    "grid_import_kw": plan.grid_import_kw.tolist(),
                    "grid_export_kw": plan.grid_export_kw.tolist(),
                },
                f,
            )
    except OSError as e:
        print(f"WARN: could not save plan state ({e}) -- next run will solve without stability continuity", file=sys.stderr)


def p2p_match_fraction(recent_days: int = 5) -> float:
    """Real, empirical fraction of exported energy during the P2P window
    that actually gets matched at the P2P rate (vs reverting to the much
    lower spot rate) -- averaged over the most recent `recent_days` REAL
    SETTLED days from sensor.lv_v2_p2p_confirmed_history (the same safe,
    REST-pushed, zero-recorder-risk mechanism lv_p2p_daily_recalibrate.py
    already proved out for this exact class of data).

    Direct, real finding (2026-08-16): assuming 100% match (this
    project's old flat-$0.50 placeholder, PR #308) overstated a real
    day's P2P revenue by ~$10 against LocalVolts' own settled Cashflow
    Breakdown -- confirmed 78% match that specific day. The recent-5-day
    average (not all-time) is used deliberately: the real match rate has
    been genuinely DECLINING (13-day avg 0.686 vs last-5-day avg 0.599,
    computed live 2026-08-16), so the more recent window is the more
    accurate estimate of what's likely to happen tonight, not a stale
    long-run average.

    Falls back to P2P_MATCH_FRACTION_FALLBACK if the sensor or its
    history is unavailable for any reason -- this writer must never
    crash outright over a secondary accuracy refinement.
    """
    try:
        hist = ha_get("sensor.lv_v2_p2p_confirmed_history")["attributes"]["history"]
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return P2P_MATCH_FRACTION_FALLBACK
    dates = sorted(hist.keys())[-recent_days:]
    fracs = []
    for d in dates:
        p2p_kwh = hist[d].get("export_volume", 0.0)
        spot_kwh = hist[d].get("spot_export_volume", 0.0)
        total = p2p_kwh + spot_kwh
        if total > 0:
            fracs.append(p2p_kwh / total)
    if not fracs:
        return P2P_MATCH_FRACTION_FALLBACK
    return sum(fracs) / len(fracs)


def p2p_recent_avg_volume_kwh(recent_days: int = 5) -> float:
    """Real, empirical AVERAGE ABSOLUTE kWh of export that gets P2P-matched
    per night -- averaged over the most recent `recent_days` REAL SETTLED
    days, same source (sensor.lv_v2_p2p_confirmed_history) and same
    recency reasoning as p2p_match_fraction() above.

    Real, direct fix (2026-08-17, household-confirmed live: "if the
    solver was good it would have kept selling rather than landing
    prematurely"): p2p_match_fraction()'s own FRACTION was being used to
    apply a flat percentage DISCOUNT to every exported kWh's price
    (match_fraction * p2p_rate + (1-match_fraction) * spot_rate,
    uniformly) -- confirmed this systematically understates real P2P
    revenue, since the real mechanism isn't a per-kWh lottery, it's a
    fixed ABSOLUTE nightly volume matched against the household's own
    known historical pattern (documented extensively in this project's
    own CLAUDE.md). This function returns that real absolute volume
    directly, feeding the Solver's own GridConfig.export_bonus_volume_kwh
    (see the nimbus repo's own network.py docstring, "TWO-TIER EXPORT
    BONUS") instead -- the LP now sees the real, UNDILUTED P2P rate for
    up to this many real kWh PER REAL CALENDAR DAY (the constraint resets
    every night, not once across the whole multi-day horizon -- see that
    same docstring for a real bug this exact distinction fixed), falling
    back to spot only beyond that, rather than a diluted average applied
    to everything.

    Falls back to P2P_RECENT_AVG_VOLUME_FALLBACK_KWH if the sensor or its
    history is unavailable for any reason -- this writer must never
    crash outright over a secondary accuracy refinement.
    """
    try:
        hist = ha_get("sensor.lv_v2_p2p_confirmed_history")["attributes"]["history"]
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return P2P_RECENT_AVG_VOLUME_FALLBACK_KWH
    dates = sorted(hist.keys())[-recent_days:]
    volumes = [hist[d].get("export_volume", 0.0) for d in dates if hist[d].get("export_volume", 0.0) > 0]
    if not volumes:
        return P2P_RECENT_AVG_VOLUME_FALLBACK_KWH
    return sum(volumes) / len(volumes)


def acquire_lock() -> bool:
    """Real PID-file overlap guard (2026-08-17, see LOCK_PATH's own
    comment) -- makes a genuine 1-minute cron cadence safe against the
    real, measured 45-52s solve time without needing a slower, more
    conservative interval "just in case". Returns True (caller should
    proceed) if no other run is genuinely still active; False (caller
    should exit cleanly, no error) if one is.

    Stale-lock safe: if LOCK_PATH exists but the PID inside it is no
    longer a real running process (a previous run crashed hard enough to
    skip its own cleanup, e.g. a killed container), os.kill(pid, 0)
    raises -- on real POSIX deploy targets specifically ProcessLookupError
    ("No such process"), confirmed via Python's own os.kill() docs; a
    real, live discrepancy found testing this same check on Windows
    (where a nonexistent PID instead raises a plain OSError, not that
    specific subclass) is exactly why this catches OSError broadly, not
    just the one POSIX-specific subclass -- ProcessLookupError/
    PermissionError are both already OSError subclasses, so this loses
    no real specificity, and stays correct regardless of which platform
    it happens to run on. ANY failure to positively confirm the old PID
    is a real, currently-running process is treated as "not actually
    locked" -- the stale file is overwritten with this run's own PID
    rather than ever permanently wedging every future run.
    """
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # raises if that PID isn't real; sends no actual signal
            return False  # a genuine previous run is still alive
        except (ValueError, OSError):
            pass  # empty/corrupt/stale lock file, or a PID that's since exited -- safe to reclaim
    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_lock() -> None:
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass  # already gone, or never created -- either way, nothing left to clean up


def main() -> None:
    # Fail fast, with a real, actionable message, if the Solver hasn't
    # been configured yet -- see fetch_solver_config()'s own docstring
    # for the full "installable by anyone" context this closes.
    cfg = fetch_solver_config()

    def num(entity_id: str) -> float:
        return float(ha_get(entity_id)["state"])

    now = datetime.now(timezone.utc).astimezone(BRISBANE_TZ).replace(second=0, microsecond=0)
    grid_times, period_hours_arr = build_tiered_grid(now)
    n_periods = len(grid_times)

    # Solar forecast source: whatever the household configured via
    # Nimbus's own "Solver settings" -> solar/load sources step -- was
    # hardcoded to this ONE household's own power-signal entity name
    # before 2026-08-20's config-flow wiring.
    solar_fc = ha_get(cfg["solver_solar_forecast_sensor"])["attributes"]["forecast"]
    # Real, honest clamp: a ML forecaster can produce a tiny negative
    # excursion near zero (physically impossible for solar/load) -- found
    # live on this script's very first real run. Clamped at the solver's
    # own boundary rather than inside elements.py, since elements.py's
    # own strict >= 0 validation is correct and should stay strict; it's
    # this writer's job to hand it physically sane numbers.
    solar_kw = [max(0.0, v) for v in resample_forecast(solar_fc, "value", grid_times)]
    # Real confidence bands, same nearest-at-or-before resample_forecast()
    # already used for the point value -- carries "lower"/"upper"
    # alongside "value" on every real forecast point (see RISK_AVERSION's
    # own comment above). Defensively clamped to bracket the (already
    # physically-clamped) point forecast at every period -- elements.py's
    # own _validate_confidence_band() requires lower_kw <= forecast_kw <=
    # upper_kw exactly, and while the Forecaster's bounds SHOULD already
    # satisfy this by construction, this guarantees it regardless of any
    # minor float/ordering edge case rather than let a rare violation
    # crash the whole solve.
    solar_lower_kw = [max(0.0, v) for v in resample_forecast(solar_fc, "lower", grid_times)]
    solar_upper_kw = [max(0.0, v) for v in resample_forecast(solar_fc, "upper", grid_times)]
    solar_lower_kw = [min(solar_lower_kw[i], solar_kw[i]) for i in range(n_periods)]
    solar_upper_kw = [max(solar_upper_kw[i], solar_kw[i]) for i in range(n_periods)]

    # Real household demand. PRIMARY path (this household's own real
    # setup): summed from the 18 real individually-forecasted circuits,
    # not one opaque whole-house entity -- see LOAD_FORECAST_ENTITIES's
    # own comment for the full reasoning. This is genuinely richer than
    # anything a single-entity config field could express (a real, live
    # health dot per circuit, a real cross-check against the whole-house
    # meter below) -- kept as-is rather than flattened to match the
    # generic case.
    #
    # FALLBACK (2026-08-20, for anyone else): LOAD_FORECAST_ENTITIES is
    # still a hardcoded Python list -- a genuinely different household's
    # own 18 circuit names would need hand-editing this constant, which
    # is real, honest, NOT closed by this pass. What IS closed: leaving
    # this list EMPTY (the realistic default for a fresh install nobody's
    # customized yet) no longer crashes -- it falls back cleanly to the
    # single sensor.solver_load_forecast_sensor entity every install
    # already configures via the Solver settings wizard, same simple
    # single-entity pattern already used for solar above.
    if LOAD_FORECAST_ENTITIES:
        load_kw, load_lower_kw, load_upper_kw, failed_load_entities = sum_load_forecasts(LOAD_FORECAST_ENTITIES, grid_times)
    else:
        load_fc = ha_get(cfg["solver_load_forecast_sensor"])["attributes"]["forecast"]
        load_kw = [max(0.0, v) for v in resample_forecast(load_fc, "value", grid_times)]
        load_lower_kw = [max(0.0, v) for v in resample_forecast(load_fc, "lower", grid_times)]
        load_upper_kw = [max(0.0, v) for v in resample_forecast(load_fc, "upper", grid_times)]
        load_lower_kw = [min(load_lower_kw[i], load_kw[i]) for i in range(n_periods)]
        load_upper_kw = [max(load_upper_kw[i], load_kw[i]) for i in range(n_periods)]
        failed_load_entities = []

    # Real, honest cross-check (reported only, never used to price or
    # dispatch anything): how far does "sum of 18 real circuits" diverge
    # from "one real whole-house meter's own forecast" right now? A
    # real, meaningful gap here is itself useful information (a missed
    # or newly-added circuit, sensor drift) worth surfacing on the
    # dashboard, not hiding silently.
    try:
        # Derived at read time from the real SOURCE sensor, not hardcoded
        # as a forecast entity_id directly -- matches Nimbus's own real
        # object_id_from_source() transform (nimbus repo, sensor.py) so a
        # future reconfigure of this signal's source can never again
        # leave this cross-check silently pointing at a dead, renamed
        # entity_id (exactly what happened here 2026-08-20, see this
        # constant's own comment above for the full incident).
        object_id = WHOLE_HOUSE_CROSS_CHECK_SOURCE_SENSOR.split(".", 1)[-1]
        whole_house_cross_check_entity = f"sensor.nimbus_{object_id}_forecast"
        whole_house_fc = ha_get(whole_house_cross_check_entity)["attributes"]["forecast"]
        whole_house_now_kw = max(0.0, resample_forecast(whole_house_fc, "value", grid_times[:1])[0])
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        print(f"WARN: whole-house cross-check unavailable ({e})", file=sys.stderr)
        whole_house_now_kw = None
    summed_18_now_kw = load_kw[0]

    # Real, standalone Nimbus entity for the summed 18-load total
    # (2026-08-17, direct ask: "like haeo concept nimbus should sum up
    # all sub sensors into one nimbus entity") -- published the SAME
    # REST-push way every other computed Solver sensor in this file
    # already is (sensor.nimbus_solver_battery_forecast, sensor.
    # nimbus_solver_quality_report), not a new custom_component entity:
    # zero added risk to the live, deployed Nimbus integration itself,
    # reusing a pattern already proven dozens of times this project.
    # Pushed BEFORE the real solve below so it reflects this run's own
    # real inputs even if the LP itself later fails for an unrelated
    # reason (price data, battery config, etc.) -- this sensor's own
    # correctness never depends on the solve succeeding.
    #
    # failed_load_entities: real, honest list of which (if any) of the
    # 18 circuits were unavailable and defaulted to 0.0 this run (direct
    # ask: "the warning would appear in the topology card... green/red
    # dot?") -- exposed here so a future topology-card change can cross-
    # reference this list against its own already-built per-load health
    # dots (sibling repo's own topology-card.js, PR #611) without
    # separately polling all 18 entities itself. Not yet wired into the
    # topology card's own JS -- that's a real, separate follow-up.
    ha_post_state(
        "sensor.nimbus_household_load_total_forecast",
        round(summed_18_now_kw, 3),
        {
            "unit_of_measurement": "kW",
            "friendly_name": "Nimbus Household Load Total (Summed)",
            "forecast": [
                {
                    "time": grid_times[i].isoformat(),
                    "value": round(load_kw[i], 3),
                    "lower": round(load_lower_kw[i], 3),
                    "upper": round(load_upper_kw[i], 3),
                }
                for i in range(n_periods)
            ],
            "source_entities": LOAD_FORECAST_ENTITIES,
            "failed_load_entities": failed_load_entities,
            "whole_house_cross_check_now_kw": round(whole_house_now_kw, 3) if whole_house_now_kw is not None else None,
            "inverter_self_consumption_kw": INVERTER_SELF_CONSUMPTION_KW,
            "generated_at": now.isoformat(),
        },
    )

    # Two paths, gated on whether THIS system actually has LocalVolts
    # configured (checked live, not assumed from config alone -- see
    # entity_exists()'s own docstring). Real households outside this
    # project's own setup won't have sensor.localvolts_price_forecast at
    # all, and that's fine: the whole point of 2026-08-20's config-flow
    # work is that the Solver still runs correctly for them, just without
    # this household's own extra sophistication.
    has_localvolts = entity_exists("sensor.localvolts_price_forecast")
    if has_localvolts:
        # PRIMARY path -- this household's own real, live setup,
        # unchanged from before 2026-08-20's config-flow wiring.
        lv_price_fc = ha_get("sensor.localvolts_price_forecast")["attributes"]["forecast"]
        # Real AEMO-anchored, 5-min-of-day price extrapolation (2026-08-16,
        # see compute_5min_offset()'s own docstring for the full real
        # finding) -- replaces the old flat-hold-last-value / hourly-average
        # behaviour for periods beyond LV's real forecast coverage (~24h
        # after the lv_forecast_writer.py truncation fix, was ~12h).
        aemo_forecast = fetch_aemo_forecast()
        # 2026-08-20: migrated off guerrier's sensor.costsflexup/earningsflexup
        # onto our own project-owned equivalents (same shape, same source --
        # lv_forecast_writer.py's push_flex_sensor(), built session 41
        # specifically to mirror guerrier's own sensor attributes; already
        # recorder-tracked with real, gap-free history -- confirmed live
        # 2026-08-20). Real goal: this project no longer needs the guerrier
        # HACS integration at all once every consumer is migrated (see
        # CLAUDE.md's Aug 20 session log for the full investigation).
        import_history = fetch_price_history("sensor.localvolts_costs_flex_up")
        export_history = fetch_price_history("sensor.localvolts_earnings_flex_up")
        import_offset_by_5min = compute_5min_offset(import_history)
        export_offset_by_5min = compute_5min_offset(export_history)
        # Real empirical price bands for price_risk_aversion (2026-08-21,
        # task #128 -- see compute_price_percentile_band()'s own docstring).
        # A SEPARATE, longer (14-day, vs the 5-day history already fetched
        # above for compute_5min_offset's own mean-offset use) fetch --
        # percentile estimation genuinely benefits from more real samples
        # per bucket than a mean does, and this data has been live and
        # recorder-tracked since well before 14 days ago.
        import_price_upper_band = compute_price_percentile_band(
            fetch_price_history("sensor.localvolts_costs_flex_up", days=14), 90.0
        )
        export_price_lower_band = compute_price_percentile_band(
            fetch_price_history("sensor.localvolts_earnings_flex_up", days=14), 10.0
        )
        spot_import_raw = resample_price_with_extrapolation(
            lv_price_fc, "costsflexup", grid_times, aemo_forecast, import_offset_by_5min
        )
        spot_export = resample_price_with_extrapolation(
            lv_price_fc, "earningsflexup", grid_times, aemo_forecast, export_offset_by_5min
        )
        p2p_export = resample_real_p2p_rate(grid_times)

        # Real, bill-verified TOU network + certificates cost baked directly
        # into import_price[t] (2026-08-16, real ask: "it needs ot be super
        # accurate") -- was previously just costsflexup (the spot commodity
        # price alone), missing ~$3.48/day of real cost this household
        # actually pays. Baking it into the LP's own price input (not just
        # reporting it after the fact) means the dispatch DECISION also
        # correctly avoids real peak-hour import, not just the reported total.
        # NETWORK_ENERGY_PEAK/OFFPEAK/SHOULDER_RATE and CERTIFICATES_RATE
        # remain hardcoded Python constants (2026-08-20) -- genuinely
        # this household's own real, bill-verified tariff, not yet part of
        # the config-flow. A real, smaller, separately-flagged gap, not
        # silently dropped: the fallback branch below correctly has none
        # of this baked in, since a different household's own tariff would
        # be different numbers entirely.
        import_price = [
            spot_import_raw[i] + network_energy_rate(grid_times[i].hour) + CERTIFICATES_RATE
            for i in range(n_periods)
        ]

        # Two-tier export pricing (2026-08-17, real fix -- see
        # p2p_recent_avg_volume_kwh()'s own docstring for the full finding).
        # REPLACES the old flat match_fraction price-dilution blend: instead
        # of averaging every kWh down to a diluted rate, export_price is now
        # just the real base/spot rate (always available, uncapped), and the
        # real INCREMENTAL P2P premium (only positive during a genuine real
        # P2P-window period, since p2p_export[i] is 0 elsewhere by the
        # placeholder sensor's own design) is passed to the Solver separately
        # via GridConfig.export_bonus_price, capped in volume PER REAL DAY by
        # GridConfig.export_bonus_volume_kwh (see nimbus's own network.py
        # docstring, "TWO-TIER EXPORT BONUS"). match_fraction is still
        # computed and reported (pushed sensor attribute) for informational
        # context -- no longer used to price the LP.
        match_fraction = p2p_match_fraction()
        p2p_recent_volume_kwh = p2p_recent_avg_volume_kwh()
        export_bonus_price = [max(0.0, p2p_export[i] - spot_export[i]) for i in range(n_periods)]
    else:
        # FALLBACK (2026-08-20, for anyone else): the household's own
        # configured import/export price sensor's CURRENT value, held
        # flat across the whole horizon -- the only thing genuinely
        # possible without knowing anything about the installer's own
        # retailer/region/tariff structure. No AEMO extrapolation, no
        # network TOU tables, no live P2P-window detection -- all of
        # those are this-household/Australian-NEM-specific and have no
        # portable equivalent yet (a real, honest, separately-tracked
        # gap, not pretended away).
        import_price = [num(cfg["solver_import_price_sensor"])] * n_periods
        spot_export = [num(cfg["solver_export_price_sensor"])] * n_periods
        match_fraction = 0.0
        # Manual, static P2P bonus from the config-flow's own optional
        # block (both default to 0.0 -- a full no-op -- if the household
        # doesn't have any P2P/community-trading scheme at all).
        p2p_recent_volume_kwh = float(cfg.get("solver_p2p_bonus_volume_kwh") or 0.0)
        bonus_price_flat = float(cfg.get("solver_p2p_bonus_price") or 0.0)
        export_bonus_price = [bonus_price_flat] * n_periods
        # No real multi-day recorded history to build an empirical band
        # from for a generic install -- price_risk_aversion (if a household
        # sets it > 0 anyway) is then a genuine no-op, same as every other
        # this-household-specific enhancement in the fallback branch above.
        import_price_upper_band = {}
        export_price_lower_band = {}

    # 2026-08-20: reads Nimbus's own real Solver settings config-flow
    # (see fetch_solver_config()'s own docstring for the full "close this
    # gap... need its own installer and inputs period" context) --
    # REPLACES the old ad-hoc input_number.nimbus_solver_* helpers, which
    # had to be hand-created via a separate, undocumented YAML package
    # file. A fresh install now needs nothing more than filling in
    # Nimbus's own hub "Configure" -> "Solver settings" form.
    capacity_kwh = float(cfg["solver_battery_capacity_kwh"])
    min_pct = float(cfg.get("solver_battery_min_soc_percent") or 5.0)
    max_pct = float(cfg.get("solver_battery_max_soc_percent") or 100.0)
    # The config-flow's own solver_battery_soc_sensor field replaces the
    # old hardcoded sensor.logger_battery_level_soc -- any household's
    # own real, live-measured SoC sensor now works, not just this one's.
    initial_pct = num(cfg["solver_battery_soc_sensor"])
    max_charge_kw = float(cfg["solver_max_charge_kw"])
    # max_discharge_kw: PREFER this household's own real, live Modbus
    # setpoint entity's own `max` attribute if it exists (2026-08-16 real
    # finding, kept unchanged below for exactly this reason -- protects
    # against the LP planning beyond a real, live hardware ceiling even
    # if the static config value is ever stale or wrong). Falls back to
    # the portable, static solver_max_discharge_kw config value for
    # anyone without this exact entity (a different household's own
    # inverter setpoint entity would have a different name entirely).
    _max_discharge_entity = "number.logger_charging_discharging_power_kw"
    if entity_exists(_max_discharge_entity):
        max_discharge_kw = ha_get(_max_discharge_entity)["attributes"]["max"]
    else:
        max_discharge_kw = float(cfg["solver_max_discharge_kw"])
    charge_cost = float(cfg.get("solver_charge_cost") or 0.01)  # not scheduled -- real automations never touch this, manual control

    if has_localvolts:
        # This household's own real, tuned day/night discharge-cost
        # schedule (built around the SAME 5pm/midnight/7am P2P-window
        # boundaries as the pricing block above) -- deliberately KEPT
        # exactly as before 2026-08-20's config-flow wiring. This is
        # real, currently-live, revenue-affecting tuning for tonight's
        # actual P2P dispatch; silently replacing it with a flat config
        # value just to look more "generic" would be a real regression
        # to money this household actually earns, not a genuine
        # improvement for anyone.
        discharge_cost_arr = np.array([battery_discharge_cost_rate(t.hour) for t in grid_times])
        salvage_value = battery_salvage_value_rate(grid_times[-1].hour)
    else:
        # FALLBACK (2026-08-20, for anyone else): flat values straight
        # from the config-flow's own Economic Policy step -- no day/night
        # schedule (that's tuned specifically around this household's own
        # P2P window, no portable equivalent yet).
        discharge_cost_arr = np.full(n_periods, float(cfg.get("solver_discharge_cost") or 0.01))
        salvage_value = float(cfg.get("solver_salvage_value") or 0.15)

    import_limit_kw = float(cfg["solver_grid_max_import_kw"])
    export_limit_kw = float(cfg["solver_grid_max_export_kw"])

    # No clamp needed as of 2026-08-16 -- the solver's grid degeneracy
    # guard that used to require import_price - export_price >= 0.001 at
    # every period (and which this writer used to satisfy by clamping the
    # real P2P price down, suppressing the real signal from ever reaching
    # the LP) has been REMOVED and replaced with two real structural
    # constraints in network.py itself (see its own "SAME-PERIOD
    # WASH-TRADE PREVENTION" docstring section) that make the underlying
    # free-money exploit physically infeasible regardless of price. The
    # real, unclamped spot rate now goes straight to the LP as the base
    # export_price; the real P2P premium goes through export_bonus_price
    # instead (see above) -- neither is diluted or clamped.
    export_price = list(spot_export)
    n_clamped = 0  # kept in the pushed sensor's own attributes for continuity; always 0 now

    # Real empirical price bands, mapped onto this solve's own real
    # grid_times (2026-08-21, task #128) -- None (a complete no-op) for
    # any household without the multi-day history to build one from (the
    # fallback branch above already sets both to {}).
    import_price_upper = apply_price_band(import_price, grid_times, import_price_upper_band)
    export_price_lower = apply_price_band(export_price, grid_times, export_price_lower_band)

    min_soc_kwh_val = capacity_kwh * min_pct / 100.0
    max_soc_kwh_val = capacity_kwh * max_pct / 100.0
    battery = elements.BatteryConfig(
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=capacity_kwh * initial_pct / 100.0,
        min_soc_kwh=min_soc_kwh_val,
        max_soc_kwh=max_soc_kwh_val,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        # solver_efficiency_percent is a single ROUND-TRIP figure (see
        # hub_options.py's own field help text: "combined battery-
        # chemistry + inverter conversion losses"), but BatteryConfig
        # wants separate charge_efficiency/discharge_efficiency -- split
        # geometrically (charge_eff = discharge_eff = sqrt(round_trip)),
        # the standard, defensible simplification when only one combined
        # number is known. min(..., 0.999) is a real, defensive clamp --
        # the config-flow's own help text already warns against ever
        # entering 100%, but a stale/mistaken 100% entry would otherwise
        # crash the solver's own structural degeneracy guard rather than
        # just quietly degrade to "solver treats this as effectively
        # lossless," so this floor is deliberately kept even though a
        # correctly-filled-in form should never actually need it.
        charge_efficiency=min(float(cfg.get("solver_efficiency_percent") or 95.0) / 100.0, 0.999) ** 0.5,
        discharge_efficiency=min(float(cfg.get("solver_efficiency_percent") or 95.0) / 100.0, 0.999) ** 0.5,
        charge_cost=charge_cost,
        discharge_cost=discharge_cost_arr,
        salvage_value=salvage_value,  # required field, but overridden by terminal_value_breakpoints below when set
        terminal_value_breakpoints=terminal_value_breakpoints_for(salvage_value, min_soc_kwh_val, max_soc_kwh_val),
    )
    fixed_export_kw = fetch_p2p_fixed_export_kw(cfg, grid_times)
    grid = elements.GridConfig(
        import_price=np.array(import_price),
        export_price=np.array(export_price),
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
        export_bonus_price=np.array(export_bonus_price),
        export_bonus_volume_kwh=p2p_recent_volume_kwh,
        fixed_export_kw=np.array(fixed_export_kw) if fixed_export_kw is not None else None,
        import_price_upper=np.array(import_price_upper) if import_price_upper is not None else None,
        export_price_lower=np.array(export_price_lower) if export_price_lower is not None else None,
    )
    solar = elements.SolarConfig(
        forecast_kw=np.array(solar_kw),
        lower_kw=np.array(solar_lower_kw), upper_kw=np.array(solar_upper_kw),
    )
    loads = [elements.LoadConfig(
        name="household_load_summed_18", forecast_kw=np.array(load_kw),
        lower_kw=np.array(load_lower_kw), upper_kw=np.array(load_upper_kw),
    )]
    periods = elements.PeriodGrid(hours=np.array(period_hours_arr), start=grid_times[0])

    # Plan-to-plan stability (2026-08-16, see PLAN_STATE_PATH's own
    # comment) -- proximal_weight uses the Solver's own documented
    # default (DEFAULT_PROXIMAL_WEIGHT_KW), deliberately not overridden:
    # small enough to never override a genuine economic signal, just
    # enough to break a near-tie toward continuity instead of an
    # arbitrary vertex. max_rate_kw deliberately NOT used here -- a hard
    # cap risks suppressing the legitimate, large, real swing at the
    # actual 5pm P2P transition, and this Solver still only observes, it
    # doesn't control anything, so there's no real inverter to protect
    # from a rate-of-change perspective the way max_rate_kw exists for.
    previous_plan = load_previous_plan()
    solve_started = time.monotonic()
    # risk_aversion / import+export price_risk_aversion (2026-08-21, task
    # #128) -- now read live from cfg (number.nimbus_solver_risk_aversion
    # / _import_price_risk_aversion / _export_price_risk_aversion,
    # dashboard-editable), replacing the old hardcoded RISK_AVERSION=0.25
    # module constant. Falls back to that same 0.25 default for
    # risk_aversion (matches the constant's own original value exactly --
    # a no-op change for an already-configured household on first deploy)
    # and 0.0 (a complete no-op) for both price dials, which never
    # existed as a constant before. Split into two independent cfg reads
    # the same day this was first wired up (see nimbus's own network.py
    # docstring / number.py comment for the full "one shared scalar
    # forces charge/discharge hedging to move together" reasoning) --
    # this writer only ever had the single-scalar version live for a
    # brief window before the split, never a real production concern.
    risk_aversion = float(cfg.get("solver_risk_aversion") if cfg.get("solver_risk_aversion") is not None else RISK_AVERSION)
    import_price_risk_aversion = float(cfg.get("solver_import_price_risk_aversion") or 0.0)
    export_price_risk_aversion = float(cfg.get("solver_export_price_risk_aversion") or 0.0)
    # smoothness_weight (2026-08-20, real household finding: "why nimbus
    # decided to make such decisions and charge in bursts not
    # continuously") -- mechanism 4, network.py's own DEFAULT_SMOOTHNESS_
    # WEIGHT_KW, same value/reasoning as proximal_weight (mechanism 1),
    # just applied within this solve's own timeline instead of across
    # solves. Locally validated (both repo's own scratchpad and nimbus's
    # own committed tests): eliminates a real, reconstructed degenerate
    # burst at byte-identical total_cost, and does NOT smear a genuine,
    # large, real transition (an 80kW price-step scenario, on or off,
    # within $0.07 either way).
    plan = network.build_plan(
        periods=periods, grid=grid, battery=battery, solar=solar, loads=loads,
        previous_plan=previous_plan, risk_aversion=risk_aversion,
        import_price_risk_aversion=import_price_risk_aversion,
        export_price_risk_aversion=export_price_risk_aversion,
        smoothness_weight=network.DEFAULT_SMOOTHNESS_WEIGHT_KW,
    )
    solve_seconds = time.monotonic() - solve_started
    if plan.status == "optimal":
        save_plan_state(plan, period_hours_arr, grid_times[0])

    # Real fixed daily charges (Network Access + LV Fee), reported
    # honestly alongside the LP's own total_cost -- NOT fed into the LP
    # itself (a flat, dispatch-independent cost can't change an optimal
    # LP decision, only shift the objective by a constant, so there's
    # nothing for the solver to do with it). Prorated to this horizon's
    # own real span (sum of period_hours_arr, NOT n_periods*a-fixed-width
    # -- the tiered grid has two different period widths) / 24, not just
    # added flat.
    horizon_days = sum(period_hours_arr) / 24.0
    total_cost_with_fixed_costs = (plan.total_cost or 0.0) + horizon_days * FIXED_DAILY_CHARGES

    # Real battery throughput/cycling exposure (2026-08-21, direct Mark
    # Purcell finding, relayed via the household: "degradation isn't in
    # the objective, so 1.0 will look free when it isn't" -- risk_aversion/
    # price_risk_aversion both tend to bias toward MORE defensive
    # charge/discharge activity, and the LP's own $ total_cost has no way
    # to reflect the real wear that causes, since BatteryConfig has no
    # genuine degradation cost term (charge_cost/discharge_cost are small,
    # flat $/kWh throughput costs, not a cycle-depth-aware wear model).
    # Reported honestly alongside the dollar figures rather than hidden --
    # a household turning either risk dial up should be able to SEE the
    # real cycling cost of that choice, not just the (incomplete) $ total.
    hours_arr_np = np.array(period_hours_arr)
    total_charge_kwh = float(np.sum(plan.battery_charge_kw * hours_arr_np))
    total_discharge_kwh = float(np.sum(plan.battery_discharge_kw * hours_arr_np))
    total_throughput_kwh = total_charge_kwh + total_discharge_kwh
    # A "full cycle" = one full charge + one full discharge = 2x capacity
    # of throughput -- the standard, real-world battery-degradation unit
    # (manufacturer cycle-life ratings are quoted in full-equivalent-
    # cycles, not raw kWh moved), so this is directly comparable to a
    # real spec sheet, not an invented metric.
    equivalent_full_cycles = total_throughput_kwh / (2.0 * capacity_kwh) if capacity_kwh > 0 else 0.0

    net_battery = plan.battery_discharge_kw - plan.battery_charge_kw
    # Real per-period price/load/solar/net-cost fields added (2026-08-17,
    # direct ask: "still waiting for haeo like markdown table where I
    # can see forecasted costs fit load solar and soc% and period net")
    # -- previously ONLY battery/SoC/grid kW were pushed; a real forecast
    # TABLE needs the same real inputs the LP itself actually solved
    # against, not just its output. import_price/export_price/
    # export_bonus_price are all already computed above (this writer's
    # own real inputs, not re-derived); load_kw/solar_kw are the same
    # real per-period arrays already fed to LoadConfig/SolarConfig.
    # net_cost is the real grid-side cash flow for that period (import
    # cost minus base export revenue minus P2P bonus revenue) --
    # deliberately NOT including battery charge/discharge wear cost,
    # matching this project's own established "Net $" convention from
    # the HAEO forecast table this mirrors.
    forecast = [
        {
            "time": grid_times[i].isoformat(),
            "battery_kw": round(float(net_battery[i]), 3),
            "soc_pct": round(float(plan.battery_soc_kwh[i] / capacity_kwh * 100), 2),
            "grid_import_kw": round(float(plan.grid_import_kw[i]), 3),
            "grid_export_kw": round(float(plan.grid_export_kw[i]), 3),
            # How much of grid_export_kw[i] earned the real, undiluted
            # P2P premium (vs the base/spot rate) -- exposed directly so
            # a real dashboard can show WHERE the real committed volume
            # landed, not just infer it (see nimbus's own network.py
            # Plan.export_bonus_kw docstring).
            "export_bonus_kw": round(float(plan.export_bonus_kw[i]), 3),
            "import_price": round(import_price[i], 4),
            "export_price": round(export_price[i], 4),
            "bonus_price": round(export_bonus_price[i], 4),
            "load_kw": round(load_kw[i], 3),
            "solar_kw": round(solar_kw[i], 3),
            # Real per-period duration (2026-08-17, found while fixing a
            # real bug this same session: the daily-summary dashboard
            # card was hardcoding a flat 0.25h multiplier for every
            # period's own kWh contribution -- correct for the first 24h
            # (TIER1_PERIOD_HOURS=0.25) but WRONG for anything beyond it
            # (TIER2_PERIOD_HOURS=1.0), silently under-counting a coarse-
            # tier period's real kWh by 4x. "Today" is entirely inside
            # the fine tier so was unaffected, but "Tomorrow" spans BOTH
            # tiers -- exposing this field lets any consumer compute real
            # kWh sums correctly regardless of which tier a period falls
            # in, instead of assuming a fixed width.
            "hours": round(period_hours_arr[i], 4),
            "net_cost": round(
                import_price[i] * float(plan.grid_import_kw[i]) * period_hours_arr[i]
                - export_price[i] * float(plan.grid_export_kw[i]) * period_hours_arr[i]
                - export_bonus_price[i] * float(plan.export_bonus_kw[i]) * period_hours_arr[i],
                4,
            ),
        }
        for i in range(n_periods)
    ]

    # Binding-constraint diagnostics (2026-08-18, Mark Purcell's audit
    # item #3), deliberately a SMALL summary rather than the raw
    # plan.duals/reduced_costs dicts -- those can hold thousands of
    # entries at real 365-period production scale, real risk of blowing
    # past HA's 16384-byte recorder attribute limit (a repeatedly-hit
    # constraint elsewhere in this project's own history). Only ever
    # reports what's binding RIGHT NOW (period 0) and tonight's own P2P
    # volume-cap shadow price -- the two answers this feature actually
    # exists to give, not a full dump.
    _BINDING_FAMILIES = {
        "grid_export_0": "Grid export limit",
        "grid_import_0": "Grid import limit",
        "battery_charge_0": "Battery max charge power",
        "battery_discharge_0": "Battery max discharge power",
    }
    binding_now = None
    binding_now_value_per_kwh = None
    for var_key, label in _BINDING_FAMILIES.items():
        val = plan.reduced_costs.get(var_key, 0.0)
        if abs(val) > 1e-6 and (binding_now_value_per_kwh is None or abs(val) > abs(binding_now_value_per_kwh)):
            binding_now = label
            binding_now_value_per_kwh = round(val, 4)
    if binding_now is None:
        binding_now = "Nothing currently binding"
    # Earliest export_bonus_cap_<date> entry (ISO date strings sort
    # correctly as plain strings) is always tonight's/the current cap --
    # None when the two-tier export bonus mechanism isn't active at all.
    _p2p_cap_keys = sorted(k for k in plan.duals if k.startswith("export_bonus_cap_") and k != "export_bonus_cap_global")
    p2p_volume_cap_shadow_price = round(plan.duals[_p2p_cap_keys[0]], 4) if _p2p_cap_keys else None

    ha_post_state(
        ENTITY_ID,
        round(float(net_battery[0]), 3),
        {
            "unit_of_measurement": "kW",
            "friendly_name": "Nimbus Solver Battery Forecast",
            "forecast": forecast,
            "status": plan.status,
            "total_cost": plan.total_cost,
            "total_cost_with_fixed_costs": round(total_cost_with_fixed_costs, 4),
            "p2p_match_fraction": round(match_fraction, 4),
            "risk_aversion": risk_aversion,
            "import_price_risk_aversion": import_price_risk_aversion,
            "export_price_risk_aversion": export_price_risk_aversion,
            "total_charge_kwh": round(total_charge_kwh, 2),
            "total_discharge_kwh": round(total_discharge_kwh, 2),
            "total_throughput_kwh": round(total_throughput_kwh, 2),
            "equivalent_full_cycles": round(equivalent_full_cycles, 3),
            "p2p_recent_avg_volume_kwh": round(p2p_recent_volume_kwh, 2),
            "load_summed_18_now_kw": round(summed_18_now_kw, 3),
            "load_whole_house_cross_check_now_kw": round(whole_house_now_kw, 3) if whole_house_now_kw is not None else None,
            "failed_load_entities": failed_load_entities,
            "n_clamped_periods": n_clamped,
            "n_periods": n_periods,
            "horizon_hours": round(horizon_days * 24, 1),
            "solve_seconds": round(solve_seconds, 2),
            "generated_at": now.isoformat(),
            "binding_constraint_now": binding_now,
            "binding_constraint_shadow_price": binding_now_value_per_kwh,
            "energy_shadow_price_now": round(plan.duals.get("power_balance_t0", 0.0), 4),
            "p2p_volume_cap_shadow_price": p2p_volume_cap_shadow_price,
        },
    )
    cross_check_str = f"{whole_house_now_kw:.2f}kW" if whole_house_now_kw is not None else "unavailable"
    print(
        f"[{now.isoformat()}] pushed {ENTITY_ID}: status={plan.status} "
        f"n_periods={n_periods} horizon={horizon_days * 24:.1f}h solve_time={solve_seconds:.2f}s "
        f"total_cost={plan.total_cost:.2f} total_cost_with_fixed={total_cost_with_fixed_costs:.2f} "
        f"p2p_match_fraction={match_fraction:.3f} net_battery_now={net_battery[0]:.2f}kW "
        f"summed_18_loads_now={summed_18_now_kw:.2f}kW whole_house_cross_check={cross_check_str} "
        f"previous_plan_found={previous_plan is not None} "
        f"binding_now={binding_now!r} energy_shadow_price_now={plan.duals.get('power_balance_t0', 0.0):.4f} "
        f"p2p_volume_cap_shadow_price={p2p_volume_cap_shadow_price}"
    )


if __name__ == "__main__":
    if not acquire_lock():
        print(f"[{datetime.now(timezone.utc).astimezone(BRISBANE_TZ).isoformat()}] previous run still in progress -- skipping this tick", flush=True)
        sys.exit(0)
    try:
        main()
    except urllib.error.HTTPError as e:
        print(f"HTTP error: {e.code} {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        raise
    finally:
        release_lock()
