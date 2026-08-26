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
HACS installs or HA runs for you, so it needs real shell + cron access
to deploy at all. IMPORTANT, easy to get wrong: that does NOT mean HA
itself has to run as Docker/Supervised. This script only ever talks to
HA over plain HTTP (see HA_BASE below -- GET/POST against the REST API,
nothing else), never touches HA's local filesystem or process, so it
can run from ANY always-on shell-capable device on the same network as
HA -- a Raspberry Pi, an old laptop, a NAS with Docker, a cheap VPS,
whatever's already sitting around. If HA itself IS Docker/Supervised,
the simplest option is just running this on that same box (HA_BASE =
localhost, as below). If HA itself is Home Assistant OS specifically
(genuinely no general shell/cron surface at all, confirmed no way
around that), this script needs a SEPARATE device -- HA_BASE then
points at HAOS's real LAN IP instead of localhost, and the required
sensor.nimbus_solver_config/nimbus_solver_config still updates the same
way regardless of where this script physically runs, since it's all
just HTTP either way.

UPDATE (2026-08-22) -- there is now a FOURTH, genuinely pure-integration
option, and it's the one to reach for first on any fresh install: this
exact file (byte-identical, see set_native_hass() below) also ships as
part of the nimbus_load custom_component itself
(custom_components/nimbus_load/solver_writer.py, same repo) and gets run
in-process by custom_components/nimbus_load/solver_runtime.py -- no
separate device, no cron, no Add-on Store git-clone-with-no-auth wall
(a real, live blocker hit installing the addon below against this
private repo), and no separate token file at all. This is now genuinely
the SIMPLEST path for anyone new: install the Nimbus integration via
HACS, run its "Solver settings" wizard, done -- the forecast starts
producing itself. The standalone-script path below (and the
nimbus_solver_app addon) still exist and are unchanged/still fully
supported -- this household's own live NUC deployment keeps using cron
exactly as documented below, deliberately not migrated in the same
change that added the native path, to avoid any risk to a real, already-
working production system for a change that exists to help OTHER
installs.

Only if genuinely nothing else works would a real HAOS Add-on (a proper
Docker-packaged Supervisor add-on, not this bare script) be the honest
fallback -- a bigger, separate build, still available (nimbus_solver_app,
same repo) but no longer the recommended first choice given the native
path above. The Solver's own config-flow "Solver settings" wizard
(Nimbus hub -> Configure) installs and works fine via HACS on ANY
platform including HAOS regardless of any of the above -- it's what
actually makes the native path possible now.

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

import functools
import io
import json
import logging
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
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
# file (import_fee_rate's TOU schedule, battery_discharge_cost_rate,
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

# PORTABILITY (2026-08-21, env-var-overridable -- was hardcoded, edit-the-
# file-yourself before this) -- every one of these three household-
# specific values now has a real default (this NUC's own exact current
# setup, so behavior here is UNCHANGED with zero env vars set) but can be
# overridden without touching a single line of actual solve logic. This
# is what makes it possible for nimbus_solver_app/ (a real Home Assistant
# Supervisor app -- see that folder, same repo) to run the EXACT same
# script unmodified inside a container, rather than needing a forked/
# drifted copy.
sys.path.insert(
    0,
    os.environ.get(
        "NIMBUS_SOLVER_PATH",
        "/opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load",
    ),
)
# ^ wherever your own clone of https://github.com/code-imstillalive/nimbus
# actually lives (NIMBUS_SOLVER_PATH env var, or this exact NUC path by
# default) -- doesn't need to be inside an HA config tree at all, this
# script never touches HA's filesystem, only imports the pure-Python
# solver/ package from wherever it's checked out.
import numpy as np
from ml.blend import blend_forecast_array, cross_source_spread
from numpy.typing import NDArray
from solver import elements, network
from solver.backtest import run_efficiency_sensitivity_sweep
from solver.quality_report import compute_quality_report
from solver.regret import evaluate_realized_cost

if os.environ.get("SUPERVISOR_TOKEN") and not os.environ.get("HA_BASE"):
    # Running inside a real HA Supervisor app/add-on container with
    # homeassistant_api: true set (see nimbus_solver_app/config.yaml) --
    # Supervisor auto-injects SUPERVISOR_TOKEN and proxies the real REST
    # API at this internal address. Best understanding from HA's own
    # published docs as of 2026-08-21, NOT yet live-verified against a
    # real Supervisor install -- if this base path is wrong, every
    # ha_get()/POST call below will fail loudly (a plain HTTPError), not
    # silently, so it'll be obvious on the very first real test run.
    HA_BASE = "http://supervisor/core"
else:
    HA_BASE = os.environ.get("HA_BASE", "http://localhost:8123")
# ^ "localhost" (the default) only works if THIS script runs on the same
# machine as HA itself (true here -- HA runs in Docker on this NUC, this
# script runs on that same NUC's host). If HA is Home Assistant OS, or
# otherwise runs somewhere this script can't reach via localhost, set the
# HA_BASE env var to HA's real LAN IP instead (e.g.
# "http://192.168.1.50:8123") -- nothing else in this script cares where
# it's physically running, every HA interaction below is a plain HTTP
# GET/POST against this base URL.
TOKEN_PATH = os.environ.get("HA_TOKEN_PATH", "/home/homehub/.ha_token")
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
#
# Env-var-overridable (2026-08-22, same "installable by anyone" reasoning
# as TOKEN_PATH/HA_BASE/NIMBUS_SOLVER_PATH above) -- /opt only makes
# sense on THIS household's own NUC host. Running natively inside the
# Nimbus integration itself (custom_components/nimbus_load/
# solver_runtime.py, same repo) points this at hass.config.path(...)
# instead -- HA's own real, persistent, always-writable storage
# directory, correct on any HA install regardless of platform.
PLAN_STATE_PATH = os.environ.get(
    "NIMBUS_SOLVER_PLAN_STATE_PATH", "/opt/nimbus_solver_last_plan.json"
)
# Real PID-file overlap guard (2026-08-17, see the deploy docstring's own
# "* * * * *" comment above) -- per the usual /opt-is-root-owned gotcha,
# this file needs the same one-time `sudo touch` + `chown` on first
# deploy as PLAN_STATE_PATH. Same env-var-overridable reasoning as above.
LOCK_PATH = os.environ.get(
    "NIMBUS_SOLVER_LOCK_PATH", "/opt/nimbus_solver_forecast_writer.lock"
)
# Real bug found live (nimbus repo issue #66, Mark Purcell, 2026-08-23):
# a load-forecast sensor with an unrecognized attribute shape degraded
# or crashed with zero operator-visible signal. The persistent
# notification this sentinel gates (see _notify_load_forecast_error_once())
# fires once per genuinely-new error message, not once ever and not
# every single cron cycle -- same env-var-overridable /opt-default
# convention as PLAN_STATE_PATH/LOCK_PATH above.
LOAD_FORECAST_ERROR_NOTIFIED_PATH = os.environ.get(
    "NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_PATH",
    "/opt/nimbus_solver_load_forecast_error.txt",
)

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
TIER0_MINUTES = 5.0  # ultra-fine tier: how far out 1-min resolution runs
TIER0_PERIOD_MINUTES = 1.0
TIER1_HOURS = (
    24.0  # fine tier: how far out 5-min resolution runs (from TIER0's own end)
)
TIER1_PERIOD_HOURS = 5.0 / 60.0
TIER2_HOURS = 72.0  # coarse tier: additional span beyond tier 1
TIER2_PERIOD_HOURS = (
    1.0  # -> 24h + 72h = 96h total (plus tier0's own 5 real minutes), ~360 periods
)

# Real, bill-confirmed TOU network rates and certificates rate (2026-08-16,
# real ask: "it needs ot be super accurate") -- reused directly from this
# project's own already bill-verified lv_costs.yaml rate table (Energex
# NTC 6900 Residential TOU Energy), not re-derived. Baked directly into
# import_price[t] below (not just reported after the fact) so the LP's
# own DISPATCH decision correctly avoids importing during real peak
# hours, not just the reported total_cost number.
# REMOVED (2026-08-22, direct household demand: "how do they configure
# fees column... I TOLD U NO HARDCODED INPUTS - this has to work as
# user setting"). The plain NETWORK_ENERGY_*/CERTIFICATES_RATE Python
# constants that used to live here (this household's own real Energex
# NTC 6900 rates) are GONE -- replaced by import_fee_rate(), below,
# which reads real, live, dashboard-editable number.nimbus_solver_
# network_fee_*/flat_fee_rate entities instead (same "up to 3
# configurable TOU blocks" shape already proven by the P2P blocks).
# Genuinely portable now: someone on AGL/Amber/Origin/any other
# retailer sets their OWN real tariff on their OWN dashboard, nothing
# to hand-edit in this file.

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

# Real per-load demand, OPTIONALLY summed from a household's own
# individually-forecasted circuit breakers instead of one whole-house
# forecast sensor (2026-08-17, direct ask on this project's own
# reference install: "i was hoping not to need whole house load if all
# 18 loads could be individually input and measured and added into a
# total"). Real, richer-than-a-single-entity signal WHEN a household
# fills this in via the wizard -- a genuine per-circuit health dot, and
# a real cross-check against a separate whole-house meter (see
# whole_house_cross_check_sensor below, reported but never used to
# price/dispatch anything -- a real divergence between "sum of
# configured circuits" and "one real whole-house meter" is itself
# useful, honest information worth surfacing, not hiding). Genuinely
# optional: a fresh install with neither field filled in falls back
# cleanly to the wizard's own single load-forecast-sensor field below.
#
# 2026-08-20: the raw SOURCE sensor is what a household configures, not
# its own forecast entity_id -- the forecast entity name is derived
# from it at read time (see below) specifically so a future reconfigure
# (task #99's own auto-rename mechanism) can never leave this cross-
# check silently pointing at a dead, renamed entity_id again. Real,
# live-confirmed incident that shaped this design (2026-08-20, this
# project's own reference install): its "Whole House" Power Signal was
# reconfigured from sensor.logger_load_power (the raw, noisy Modbus
# meter -- see this project's own CLAUDE.md, "real P2P-window grid
# spikes root-caused") to sensor.cb_total_combined_power_adjusted_kw.
# Task #99's auto-rename correctly renamed the live forecast entity to
# match -- but at the time, this writer's own hardcoded cross-check
# pointer was still the OLD literal forecast entity_id, confirmed 404
# the very next run.
#
# Real bug found live (nimbus repo issues #56/#60, reported by an
# independent installer, 2026-08-22): these two used to be hardcoded
# Python constants here -- one household's own 18 real circuit entity
# IDs and one household's own real whole-house cross-check sensor. That
# meant every OTHER install summed 18 nonexistent entities every solve
# cycle (18 real 404 warnings per cycle, 216/hour at the default 5-min
# cadence) and the config-flow's own single-sensor fallback field
# below was permanently unreachable dead code for anyone but the
# maintainer. Fixed 2026-08-23: both are now read live from cfg
# (Nimbus's own Solver settings wizard) inside main() -- see
# load_forecast_entities / whole_house_cross_check_sensor further down.
# Genuinely empty/None by default for a fresh install; a household that
# wants per-circuit summation and/or a whole-house cross-check fills
# these in explicitly through the wizard.

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
#
# Real PORTABILITY BUG found and fixed (2026-08-24, nimbus repo issue
# #100, Mark Purcell -- an independent installer's own live health-check
# found his own load total's confidence band stuck dead flat at exactly
# 0.215 for 362/363 points, with no household hardware of his own that
# would explain that specific number). This used to be a bare, hardcoded
# module-level constant -- meaning EVERY OTHER Nimbus install got this
# household's own specific 215W bias silently added to their own load
# total and band, whether or not their own hardware has any such bias at
# all. No longer a module constant -- now read live from cfg (see
# main()'s own sum_load_forecasts() call site below), same
# number.nimbus_solver_* config-flow pattern every other economic/
# hardware Solver setting already uses (const.py's own
# CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW comment has the full story).
# 0.0 is the real, portable default for anyone else; this project's own
# reference install needs number.nimbus_solver_inverter_self_consumption_kw
# set to 0.215 once, manually, after this change first deploys -- there
# is nothing in entry.options for a brand-new field to seed itself from.


# Same real, live config-flow keys as P2P_BLOCK_KEYS above, mirrored
# exactly for network TOU fees (2026-08-22 -- see import_fee_rate()'s
# own docstring for the full "no hardcoded tariff" story).
NETWORK_FEE_BLOCK_KEYS = (
    (
        "solver_network_fee_1_rate",
        "solver_network_fee_1_start_hour",
        "solver_network_fee_1_end_hour",
    ),
    (
        "solver_network_fee_2_rate",
        "solver_network_fee_2_start_hour",
        "solver_network_fee_2_end_hour",
    ),
    (
        "solver_network_fee_3_rate",
        "solver_network_fee_3_start_hour",
        "solver_network_fee_3_end_hour",
    ),
)


def _cfg_num(cfg: dict, key: str, default: float) -> float:
    """Return ``float(cfg[key])`` unless the value is missing/None, in
    which case return ``default``.

    REPLACES the pervasive ``float(cfg.get(key) or default)`` pattern
    that used to appear throughout this file. That pattern silently
    treats an intentional ``0.0`` as "unset" (0.0 is falsy in Python)
    and swaps in the hardcoded default -- a real, live footgun for any
    field where 0 is a legitimate user setting (min_soc_percent set to
    0% by an installer as a temporary bypass being the specific case
    that surfaced this: setting the dashboard number entity to 0.0 was
    byte-identical to leaving it unset, and the solver silently reverted
    to the 5.0% default and kept crashing).

    Using ``is None`` distinguishes "user set 0" from "never configured,"
    which is what a genuine numeric default should do. For fields where
    the hardcoded default IS ``0.0``, this helper is functionally
    identical to the old pattern -- swapped anyway for consistency and
    defensiveness against a future default change.
    """
    val = cfg.get(key)
    if val is None:
        return default
    return float(val)


def _cfg_int(cfg: dict, key: str, default: int) -> int:
    """Same as ``_cfg_num`` but returns ``int``. Used for hour-of-day
    block boundaries where ``0`` is a legitimate value (midnight).
    """
    val = cfg.get(key)
    if val is None:
        return default
    return int(val)


def resolve_max_discharge_kw(cfg: dict) -> float:
    """PREFER this household's own real, live hardware setpoint entity's
    own `max` attribute if one is CONFIGURED (2026-08-16 real finding
    for why this mechanism exists at all -- protects against the LP
    planning beyond a real, live hardware ceiling even if the static
    config value is ever stale or wrong). Falls back to the portable,
    static solver_max_discharge_kw config value whenever this field is
    left unset -- the correct default for almost every install.

    2026-08-24, nimbus #125 (Mark Purcell's own real repro): this used
    to be a bare HARDCODED entity_id
    ("number.logger_charging_discharging_power_kw", this repo's own
    reference household's real Sungrow Logger entity) -- on Mark's own
    Sigen-based system, SOME unrelated entity apparently exists at that
    exact name/slug, so entity_exists() returned True and its own,
    completely unrelated `max` attribute (1.93) silently replaced his
    real configured 24kW with zero warning, capping the LP's real
    discharge capability for the entire 96h horizon. The charge side
    (max_charge_kw, read directly from cfg with no such override) never
    had this bug at all -- confirmed by Mark's own evidence (charge
    correctly bounded at his configured 21.0kW). Now a genuine,
    optional, per-household config field
    (solver_max_discharge_live_entity) -- unset (the correct default
    for a portable install) skips this mechanism entirely, matching
    every other optional entity-pointer field in this file's own
    fallback discipline.

    Extracted as its own standalone, directly-testable function
    (2026-08-24) rather than left inline inside main() -- same
    precedent as _cfg_num/_cfg_int above -- specifically so this exact
    bug class (a real entity read silently overriding a real configured
    value) has real unit-test coverage, not just source-inspection.
    """
    live_entity = cfg.get("solver_max_discharge_live_entity")
    if live_entity and entity_exists(live_entity):
        try:
            return float(ha_get(live_entity)["attributes"]["max"])
        except (KeyError, TypeError, ValueError):
            print(
                f"WARN: solver_max_discharge_live_entity '{live_entity}' "
                f"exists but has no usable numeric 'max' attribute -- "
                f"falling back to solver_max_discharge_kw.",
                file=sys.stderr,
            )
    return float(cfg["solver_max_discharge_kw"])


_MIN_SOC_FLOOR_FRACTION = 0.0005  # 0.05% of capacity -- see docstring below.


def resolve_min_soc_kwh(
    min_pct: float, capacity_kwh: float, max_soc_kwh: float
) -> float:
    """Convert the configured Min SoC percent into kWh, with a strictly
    positive floor.

    elements.BatteryConfig.__post_init__ requires 0 < min_soc_kwh <=
    max_soc_kwh <= capacity_kwh -- a deliberate LP-level degeneracy/
    safety floor, not negotiable at that layer. But the dashboard's own
    "Battery Min SoC" number entity allows dragging all the way down to
    0% (number.py's own native_min_value=0, deliberately -- _cfg_num's
    own docstring, and this file's test_solver_writer_cfg_defaults.py,
    both already name "min_soc_percent set to 0% by an installer as a
    temporary bypass" as a real, legitimate use case, not user error).

    Left unguarded, that combination hard-crashes main() -- and
    therefore the whole native solver_runtime.py loop -- every single
    solve cycle. Confirmed live, 2026-08-24: 20 consecutive crashes over
    24 minutes on a real independent install with Min SoC genuinely set
    to 0%.

    Same "absorb real reality rather than propagate a ValueError every
    minute" philosophy as the initial_soc_kwh clamp in main() (2026-08-
    23) -- a tiny relative floor (0.05% of capacity, negligible for any
    real battery) keeps a 0% intent honoured as "effectively no
    reserve" while staying strictly positive and therefore solvable.
    min() against max_soc_kwh is a defensive guard against the
    pathological case where Max SoC is ALSO at or near 0 -- not
    observed in the wild, but keeps the invariant min <= max intact
    regardless.

    Extracted as its own standalone, directly-testable function -- same
    precedent as resolve_max_discharge_kw() above -- rather than left
    inline inside main(), which needs a live HA fetch and can't be unit-
    tested standalone.
    """
    min_soc_kwh = capacity_kwh * min_pct / 100.0
    if min_soc_kwh <= 0.0:
        floor_kwh = min(capacity_kwh * _MIN_SOC_FLOOR_FRACTION, max_soc_kwh)
        print(
            f"WARN: configured Min SoC ({min_pct:.2f}%) resolves to "
            f"{min_soc_kwh:.4f} kWh, at or below zero -- the solver "
            f"requires a strictly positive floor to stay solvable. "
            f"Clamped to {floor_kwh:.4f} kWh for this solve "
            f"(effectively no reserve, not a literal 0%). If you "
            f"genuinely want a small real reserve instead, raise Min "
            f"SoC above 0% on the dashboard.",
            file=sys.stderr,
        )
        return floor_kwh
    return min_soc_kwh


def safe_num(entity_id: str, fallback: float = 0.0) -> float:
    """Read entity_id's current state as a float, degrading gracefully
    (WARN + fallback) instead of crashing the whole solve cycle when the
    entity's real state can't be parsed as a number.

    Real, live crash this fixes (Mark Purcell, 2026-08-24, direct
    follow-up to #58's own "it should catch errors and manage them"
    complaint -- see resolve_min_soc_kwh() above for the other half of
    that same conversation): a configured solver_export_price_sensor
    entity's real state came back as '2026-08-24T13:00:00+10:00' (a
    timestamp, not a price) -- the bare, unprotected
    ``float(ha_get(entity_id)["state"])`` this replaces (previously a
    small closure named ``num()``, local to main() and therefore
    untestable in isolation -- extracted here for the same reason as
    resolve_max_discharge_kw()/resolve_min_soc_kwh() above) had no
    defence at all against that shape, and crashed every single solve
    cycle it was reached on. Same class of external-read that "might
    not be shaped as expected" already handled this way elsewhere in
    this file (resolve_max_discharge_kw()'s own malformed-'max'-
    attribute handling).

    0.0 (the default fallback) matches this file's own established "no
    better default exists" convention for a portable/generic install
    with genuinely missing data (the P2P bonus fields, flat fee rate,
    etc. all default the same way). Used for the three real, required
    scalar-entity reads in main(): solver_import_price_sensor's and
    solver_export_price_sensor's own scalar-fallback branch (only
    reached when no forecast array exists at all), and
    solver_battery_soc_sensor's live SoC read -- the latter is doubly
    protected even on a 0.0 fallback: the existing initial_soc_kwh clamp
    immediately below always has a strictly-positive floor to clamp
    into now, thanks to resolve_min_soc_kwh() above.
    """
    try:
        return float(ha_get(entity_id)["state"])
    except (KeyError, TypeError, ValueError) as e:
        print(
            f"WARN: entity '{entity_id}' has a non-numeric state -- "
            f"could not parse it as a price/SoC value ({e}). Falling "
            f"back to {fallback} for this solve. Check that this "
            f"entity is genuinely configured correctly (a real price/"
            f"SoC sensor, not something else that happens to share the "
            f"name).",
            file=sys.stderr,
        )
        return fallback


def compute_binding_constraint_label(
    plan: network.Plan,
    export_limit_kw: float,
    import_limit_kw: float,
    max_charge_kw: float,
    max_discharge_kw: float,
) -> tuple[str, float | None]:
    """ "What's binding RIGHT NOW (period 0)" -- Mark Purcell's audit item
    #3 (2026-08-18), deliberately a SMALL summary rather than the raw
    plan.duals/reduced_costs dicts (those can hold thousands of entries
    at real 365-period production scale, real risk of blowing past HA's
    16384-byte recorder attribute limit -- a repeatedly-hit constraint
    elsewhere in this project's own history).

    Extracted as its own standalone, directly-testable function
    (2026-08-24) -- same precedent as resolve_max_discharge_kw() above --
    specifically because of the real bug this exact refactor was built
    to fix and now has real unit-test coverage for, not just source-
    inspection:

    2026-08-24 fix (Mark Purcell, nimbus #125/#133, real repro): a
    nonzero reduced cost on e.g. battery_discharge_0 used to be labelled
    "Battery max discharge power" UNCONDITIONALLY -- but a real, nonzero
    LP reduced cost fires whenever a variable is pinned at EITHER of its
    own bounds, not only its upper/capacity bound (a core LP optimality
    property: a non-basic variable's reduced cost is only ever nonzero
    when it's sitting exactly at a bound -- lower OR upper). Mark's own
    plan showed the battery CHARGING at period 0 (not discharging at
    all) while this label still reported "Battery max discharge power"
    -- the true story was battery_discharge_0 pinned at its LOWER bound
    (0, a genuine "not economical to discharge right now" decision), not
    the 24kW ceiling his own config actually set (confirmed separately,
    by direct source read, that max_discharge_kw is applied UNSCALED as
    the LP variable's own upper bound -- `ub=battery.max_discharge_kw`
    at network.py's battery_discharge_{t} construction, no efficiency/
    SoC derating on the bound itself -- ruling out both of Mark's own
    suggested "second override path" hypotheses: a second hardcoded
    entity slug, confirmed absent via a repo-wide grep for "logger_";
    and SoC/efficiency scaling of the bound, confirmed absent by reading
    network.py's own variable construction directly).

    Genuinely ambiguous from the OLD label alone which of these two,
    very different real stories was true. Now disambiguated by checking
    the variable's own real SOLVED value (plan.battery_discharge_kw[0],
    etc.) against its two real bounds: only the genuine "pinned at the
    real ceiling" case keeps the original 4 label strings (byte-
    identical, no compatibility break for anyone already reading this
    field for THAT case); the "pinned at zero" case gets its own new,
    distinct, honest label instead of silently reusing the ceiling
    wording it was never actually describing.

    Returns (label, shadow_price_per_kwh) -- shadow_price is None only
    when nothing is currently binding (label == "Nothing currently
    binding"), matching this function's one and only caller's own
    existing external contract (the pushed sensor attribute shape).
    """
    _BINDING_FAMILIES = {
        # key: (exact original ceiling label, short name for the "at
        # zero" case, real solved-value array, real configured limit)
        "grid_export_0": (
            "Grid export limit",
            "Grid export",
            plan.grid_export_kw,
            export_limit_kw,
        ),
        "grid_import_0": (
            "Grid import limit",
            "Grid import",
            plan.grid_import_kw,
            import_limit_kw,
        ),
        "battery_charge_0": (
            "Battery max charge power",
            "Battery charge",
            plan.battery_charge_kw,
            max_charge_kw,
        ),
        "battery_discharge_0": (
            "Battery max discharge power",
            "Battery discharge",
            plan.battery_discharge_kw,
            max_discharge_kw,
        ),
    }
    binding_now = None
    binding_now_value_per_kwh = None
    for var_key, (
        ceiling_label,
        short_name,
        values,
        limit_kw,
    ) in _BINDING_FAMILIES.items():
        val = plan.reduced_costs.get(var_key, 0.0)
        if abs(val) > 1e-6 and (
            binding_now_value_per_kwh is None
            or abs(val) > abs(binding_now_value_per_kwh)
        ):
            solved_value = float(values[0])
            if limit_kw > 1e-9 and solved_value >= limit_kw - 1e-6:
                # Genuinely at the real ceiling -- exact original wording,
                # byte-identical, no compatibility break for anyone
                # already reading this field for this specific case.
                binding_now = ceiling_label
            elif solved_value <= 1e-6:
                # Pinned at zero -- a real "not worth it right now"
                # economic decision, NOT a capacity constraint. Distinct
                # from the ceiling case on purpose (see docstring above).
                binding_now = f"{short_name} at zero (not economical right now)"
            else:
                # Shouldn't happen for a variable with a genuinely
                # nonzero reduced cost (LP optimality: only ever nonzero
                # exactly at a bound) -- represented honestly rather
                # than assumed, matching this module's own "never paper
                # over an unexpected state" convention.
                binding_now = (
                    f"{short_name} at {solved_value:.2f} kW "
                    f"(unexpected -- neither its 0 nor {limit_kw:.2f} kW bound)"
                )
            binding_now_value_per_kwh = round(val, 4)
    if binding_now is None:
        binding_now = "Nothing currently binding"
    return binding_now, binding_now_value_per_kwh


def resolve_load_forecast_source_label(
    load_forecast_entities: list[str], single_sensor_entity_id: str
) -> str:
    """Named, plain-English record of which of the two mutually-exclusive
    load-forecast paths actually fed the LP this cycle, and with what
    (nimbus issues #148/#116, 2026-08-25).

    Mark Purcell found live that `solver_load_forecast_sensor` can be
    configured, correct, and completely silently ignored the instant
    `solver_load_forecast_entities` has even one entry -- exactly as this
    project's own README already documents in prose ("wins outright over
    the field above the instant it has even one entry, regardless of what's
    configured there"), but never surfaced anywhere as an actual diagnostic
    field an operator (or a bug report) could read.

    Extracted as its own standalone, directly-testable function (2026-08-25)
    -- same precedent as compute_binding_constraint_label()/
    compute_cost_breakdown() above. Takes the exact same two config values
    the real branch decision below is made from, so it can never drift from
    what actually ran.
    """
    if load_forecast_entities:
        return (
            f"summed {len(load_forecast_entities)} circuit(s): "
            f"{', '.join(load_forecast_entities)}"
        )
    return f"single sensor: {single_sensor_entity_id}"


def compute_cost_breakdown(
    net_costs: list[float],
    total_cost: float | None,
    degradation_cost_per_kwh: float,
    total_throughput_kwh: float,
    charge_cost: float,
    total_charge_kwh: float,
    discharge_cost_arr: NDArray[np.float64],
    battery_discharge_kw: NDArray[np.float64],
    period_hours: NDArray[np.float64],
) -> dict[str, float]:
    """Named cost-component breakdown for the solver diagnostics dump
    (2026-08-25, nimbus issue #149 -- Mark Purcell's own executable
    reconciliation tests, run against both the v0.80 and v0.81.0 dumps:
    `total_cost` could not be reconstructed from anything else in the
    dump, off by exactly degradation+charge_fee+discharge_fee minus the
    #144 terminal-value credit).

    Extracted as its own standalone, directly-testable function (2026-08-25)
    -- same precedent as compute_binding_constraint_label() above.

    `grid_net` sums the caller's own already-computed per-period net_cost
    values (grid-only cash flow -- see that field's own comment at its
    construction site) rather than re-deriving effective/risk-adjusted
    prices a second time here -- guarantees this figure matches what the
    LP actually saw on the grid side, not an approximation of it.

    `degradation`/`charge_fee`/`discharge_fee` mirror network.py's own LP
    cost coefficients exactly: degradation_cost_per_kwh is applied
    additively to BOTH the charge and discharge legs (see build_plan's own
    "(charge_cost_arr[t] + battery.degradation_cost_per_kwh)" comment), so
    its total here is degradation_cost_per_kwh * total_throughput_kwh;
    discharge_fee sums discharge_cost_arr[t] per period rather than
    multiplying by a flat scalar, since a real household's own LocalVolts
    schedule (battery_discharge_cost_rate) makes it hour-varying, not a
    constant -- charge_cost IS a flat scalar in every branch that builds
    it, so charge_fee is the cheaper flat multiplication.

    `terminal_value_credit` is deliberately the RESIDUAL (total_cost minus
    the four terms above), not a re-implementation of
    terminal_value_breakpoints_for()'s own piecewise segment math in a
    second place -- residual-by-construction means this always reconciles
    exactly (Mark's own test #2: grid_net + degradation + charge_fee +
    discharge_fee + terminal_value_credit == total_cost), and its value
    already IS what an operator wants to see (the real terminal-value/
    salvage credit's total economic effect, whatever combination of
    checkpoints produced it), without a second implementation that could
    silently drift from the LP's own real one over time.
    """
    grid_net_cost = sum(net_costs)
    degradation_cost = degradation_cost_per_kwh * total_throughput_kwh
    charge_fee_cost = charge_cost * total_charge_kwh
    discharge_fee_cost = sum(
        float(discharge_cost_arr[i])
        * float(battery_discharge_kw[i])
        * float(period_hours[i])
        for i in range(len(battery_discharge_kw))
    )
    terminal_value_credit = (total_cost or 0.0) - (
        grid_net_cost + degradation_cost + charge_fee_cost + discharge_fee_cost
    )
    return {
        "grid_net": round(grid_net_cost, 4),
        "degradation": round(degradation_cost, 4),
        "charge_fee": round(charge_fee_cost, 4),
        "discharge_fee": round(discharge_fee_cost, 4),
        "terminal_value_credit": round(terminal_value_credit, 4),
    }


def compute_cost_band(
    *,
    period_hours: NDArray[np.float64],
    load_lower_kw: NDArray[np.float64],
    load_upper_kw: NDArray[np.float64],
    solar_kw: NDArray[np.float64],
    import_price: NDArray[np.float64],
    export_price: NDArray[np.float64],
    charge_committed_kw: NDArray[np.float64],
    discharge_committed_kw: NDArray[np.float64],
    charge_cost: float,
    discharge_cost_arr: NDArray[np.float64],
    final_soc_kwh: float,
    salvage_value: float,
    import_limit_kw: float,
    export_limit_kw: float,
) -> dict[str, float] | None:
    """Cost-band diagnostic (2026-08-25, nimbus issue #147: "the load
    forecast's own uncertainty band is up to 8x the total cost being
    optimised, and the LP never sees it") -- re-costs the COMMITTED
    dispatch (this solve's own real charge/discharge decisions, held
    fixed) against the load forecast's own stated lower/upper
    confidence bounds instead of its point value, via
    evaluate_realized_cost() (regret.py) -- the same "hold dispatch
    fixed, recompute the real balance" technique compute_quality_
    report()'s own J_ach already uses, just swapping which load series
    it's evaluated against. Needs no LP change -- the LP already ran;
    this is read-only post-hoc analysis on its output.

    Deliberately prices export at the plain base export_price only, no
    P2P bonus term -- the SAME residual-only convention compute_
    quality_report()'s own J_ref/J_ach split already uses (see that
    module's docstring for why): evaluate_realized_cost() has no
    concept of GridConfig's own two-tier bonus mechanic, and a bonus-
    aware band would need real settled bonus $, not something
    available for a still-open future plan. This makes the returned
    band a real, honest LOWER BOUND on the true swing, not the full
    picture -- callers should surface that caveat alongside the field,
    not treat it as the complete answer.

    Returns {"lower", "upper", "width"} (all $, "width" = upper -
    lower), or None if the re-costing itself fails for any reason --
    this is a read-only diagnostic and must never break the real solve
    it's reporting on.
    """
    try:
        lower_cost = evaluate_realized_cost(
            hours=period_hours,
            load_real_kw=np.asarray(load_lower_kw),
            solar_real_kw=np.asarray(solar_kw),
            import_price_real=np.asarray(import_price),
            export_price_real=np.asarray(export_price),
            charge_committed_kw=charge_committed_kw,
            discharge_committed_kw=discharge_committed_kw,
            charge_cost=charge_cost,
            discharge_cost=discharge_cost_arr,
            final_soc_kwh=final_soc_kwh,
            salvage_value=salvage_value,
            grid_import_limit_kw=import_limit_kw,
            grid_export_limit_kw=export_limit_kw,
        ).total_cost
        upper_cost = evaluate_realized_cost(
            hours=period_hours,
            load_real_kw=np.asarray(load_upper_kw),
            solar_real_kw=np.asarray(solar_kw),
            import_price_real=np.asarray(import_price),
            export_price_real=np.asarray(export_price),
            charge_committed_kw=charge_committed_kw,
            discharge_committed_kw=discharge_committed_kw,
            charge_cost=charge_cost,
            discharge_cost=discharge_cost_arr,
            final_soc_kwh=final_soc_kwh,
            salvage_value=salvage_value,
            grid_import_limit_kw=import_limit_kw,
            grid_export_limit_kw=export_limit_kw,
        ).total_cost
    except Exception:  # noqa: BLE001 -- cosmetic diagnostic, must never break the real solve
        return None
    return {
        "lower": round(lower_cost, 4),
        "upper": round(upper_cost, 4),
        "width": round(upper_cost - lower_cost, 4),
    }


def import_fee_rate(cfg: dict, hour: int) -> float:
    """Real, live, dashboard-configurable network TOU fee for a given
    hour -- REPLACES the old hardcoded network_energy_rate()
    (2026-08-22, direct household demand after the Buy¢/Fees¢ split:
    "how do they configure fees column... I TOLD U NO HARDCODED INPUTS
    - this has to work as user setting"). Every household's own real
    tariff is completely different (retailer, network, region, TOU
    structure) -- there is no universal default, so this reads live
    number.nimbus_solver_network_fee_* entities instead of a Python
    constant.

    Same shape as fetch_p2p_fixed_export_kw()'s own P2P blocks: a
    DEFAULT rate (the baseline/"shoulder" rate applied to any hour not
    covered by an override block) plus up to 3 optional override
    blocks (rate<=0 = "not configured", same convention as the P2P
    blocks). A flat single-rate tariff sets only the default and
    leaves all 3 blocks off; a 2-tier peak/offpeak retailer sets
    default=offpeak + one block=peak; a 3-tier retailer (this
    household's own real Energex NTC 6900 structure) sets
    default=shoulder + block1=peak + block2=offpeak. Leaving
    everything at its 0.0 default (a fresh install, or anyone who
    hasn't configured this) correctly makes this a complete no-op --
    Fees¢ shows 0, same honest "no data assumed" default as every
    other optional Solver field.
    """
    default_rate = _cfg_num(cfg, "solver_network_fee_default_rate", 0.0)
    for rate_key, start_key, end_key in NETWORK_FEE_BLOCK_KEYS:
        rate = _cfg_num(cfg, rate_key, 0.0)
        start_hour = _cfg_int(cfg, start_key, 0)
        end_hour = _cfg_int(cfg, end_key, 0)
        if rate <= 0 or end_hour <= start_hour:
            continue
        if start_hour <= hour < end_hour:
            return rate
    return default_rate


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
BATTERY_DISCHARGE_COST_DAY = 0.09  # 7am-5pm
BATTERY_SALVAGE_VALUE_NIGHT = 0.3  # 5pm-midnight (P2P window only)
BATTERY_SALVAGE_VALUE_OTHER = 0.15  # midnight-5pm


def battery_discharge_cost_rate(hour: int) -> float:
    return (
        BATTERY_DISCHARGE_COST_NIGHT
        if (hour >= 17 or hour < 7)
        else BATTERY_DISCHARGE_COST_DAY
    )


def battery_salvage_value_rate(hour: int) -> float:
    """Salvage value only applies ONCE, to the horizon's own FINAL
    period, so this doesn't need a full per-period array -- just needs
    to reflect what the real schedule would set at whatever real hour
    the horizon happens to end at, not whatever's live right now."""
    return BATTERY_SALVAGE_VALUE_NIGHT if hour >= 17 else BATTERY_SALVAGE_VALUE_OTHER


def midnight_boundary_period_indices(grid_times: list[datetime]) -> list[int]:
    """Real, direct fix for the 2026-08-22 finding (shadow-mode chart
    evidence): the Solver's own plan kept discharging for ~1hr PAST the
    real P2P window's close, at essentially unchanged export price.
    Root cause: terminal_value_breakpoints only ever protected soc at
    the horizon's own true FINAL period (see terminal_value_period_
    indices' own docstring, nimbus repo elements.py) -- every OTHER day
    boundary in this multi-day horizon had nothing telling the LP
    tomorrow has its own P2P opportunity too, so with discharge_cost
    held at a real, deliberately tiny $0.01/kWh, any export price above
    that stayed "profitable" forever and it just kept selling toward
    the floor.

    Returns the period index immediately BEFORE each real local
    midnight -- i.e. soc[idx] represents the battery's state at the
    exact moment a real day (and this household's own real P2P window)
    closes, the correct anchor for "how much should be held back going
    into tomorrow". grid_times[t].hour is already real local AEST (see
    build_tiered_grid -- 'now' is built from BRISBANE_TZ, not UTC), so
    no timezone conversion is needed here. Works correctly regardless of
    which tier a given midnight falls in -- the 5-min Tier1 region and
    the 1-hour Tier2 region both break exactly on real hour boundaries,
    so "the period right before an hour-0 period" is always well-
    defined either way.
    """
    indices = []
    for t in range(len(grid_times) - 1):
        if grid_times[t].hour != 0 and grid_times[t + 1].hour == 0:
            indices.append(t)
    return indices


def terminal_value_breakpoints_for(
    base_rate: float, min_soc_kwh: float, max_soc_kwh: float
) -> list:
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


# Same 2026-08-21 portability pass -- checked in order: SUPERVISOR_TOKEN
# (auto-injected by HA's own Supervisor into a real app/add-on container,
# no manual token setup at all) beats a raw HA_TOKEN env var (any other
# non-Supervisor container/host) beats the original TOKEN_PATH file (this
# NUC's own unchanged default).
#
# Real bug caught before it ever shipped (2026-08-22, building the PURE
# INTEGRATION native path below): this whole block runs unconditionally
# at MODULE IMPORT TIME, before set_native_hass() (further down this
# file) even exists to be called -- so importing this file natively,
# in-process, inside custom_components/nimbus_load/ would previously
# have crashed immediately with a bare FileNotFoundError on TOKEN_PATH,
# on any system that isn't this exact household's own NUC (no
# /home/homehub/.ha_token, and no SUPERVISOR_TOKEN either, since native
# mode doesn't go through Supervisor at all) -- long before the native
# seam ever got a chance to make TOKEN completely irrelevant. Genuinely
# needed for REST/standalone mode (cron, the nimbus_solver_app addon) --
# unchanged, still fails loudly there, which is correct, existing,
# already-accepted behaviour for that deployment path. Wrapped in
# try/except purely so a MISSING token can never crash native mode,
# which never reads TOKEN at all (see ha_get()/ha_post_state()/
# fetch_price_history() above -- every REST branch that would actually
# USE this value is skipped entirely once _NATIVE_HASS is set).
TOKEN = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN")
if not TOKEN:
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as f:
            TOKEN = f.read().strip()
    except OSError:
        TOKEN = None


# PURE INTEGRATION seam (2026-08-22, direct real-world push: Mark Purcell
# hit a private-repo git-clone-with-no-auth wall trying to install the
# nimbus_solver_app addon, and separately flagged the deeper, correct
# architectural point -- "EMHASS had the addon, which was always a
# complication for access logs and sending commands... HAEO runs as a
# pure integration." This is the fix: a real, additive extension point
# that lets the EXACT SAME ~2400 lines below run natively inside HA
# Core's own process (via custom_components/nimbus_load/
# solver_runtime.py, same repo), with ZERO behaviour change to the
# existing standalone deployment (cron on this household's own NUC, or
# the nimbus_solver_app addon) -- _NATIVE_HASS defaults to None, and
# every one of the ~2400 lines below still just calls ha_get(...)/
# ha_post_state(...)/fetch_price_history(...) by name, exactly as it
# always has. Only what THOSE THREE functions do internally branches on
# whether a real hass instance has been injected.
_NATIVE_HASS = None  # None = standalone/REST mode (default, unchanged behaviour).

# stdlib logging.Logger, NOT this file's own print() convention -- deliberately
# so the #85 trace below (which used to be print()-only, and per issue #85's
# own thread was "cannot be surfaced via HA log API (print -> stdout, not
# _LOGGER); ignore") actually lands somewhere ha_get_logs()/HA's error_log can
# see it in native mode. logging.getLogger() is plain stdlib, not an HA
# import, so this doesn't compromise the standalone/cron/addon path's own
# "zero HA imports" requirement -- in that mode nothing configures a handler
# for this logger, so it's silent by default exactly as before. In native
# mode this logger is a child of HA's own logging tree (module name matches
# this file's dotted path), so `logger.set_level` /
# `custom_components.nimbus_load.solver_writer: debug` in configuration.yaml
# both work on it exactly like any other HA component logger.
_LOGGER = logging.getLogger(__name__)

# PURE INTEGRATION dispatch seam (2026-08-23, issue #55) -- lets
# sensor.py's real SensorEntity classes register themselves as the
# native-mode handler for a specific entity_id, so ha_post_state() below
# routes state updates for that entity_id through the entity's own
# async_write_ha_state() path (proper unique_id, device_info, device_
# class, unrecorded_attributes, etc.) instead of writing a raw dict
# straight into the state machine via states.async_set(). Empty by
# default -- an unregistered entity_id still falls through to the
# original states.async_set() fallback (preserving behaviour for anyone
# still on a version that hasn't migrated its SensorEntities yet), and
# the REST path below is completely unaffected.
_ENTITY_UPDATE_HANDLERS: dict[str, object] = {}


def set_native_hass(hass) -> None:
    """Called once by the Nimbus integration itself, before running a
    solve in-process. See this module's own "PURE INTEGRATION seam"
    comment immediately above for the full story."""
    global _NATIVE_HASS
    _NATIVE_HASS = hass


def register_entity_handler(entity_id: str, handler) -> None:
    """Called once per migrated entity from sensor.py's async_setup_entry.

    `handler(state, attributes)` will be scheduled on the event loop
    (via hass.add_job) whenever ha_post_state() below is asked to update
    this entity_id in native mode. In practice the handler is the
    entity's own update_from_solver() method, which stores the values
    and calls async_write_ha_state() so HA sees a real entity update
    with all the SensorEntity class metadata attached.

    Registration is idempotent (a re-registration cleanly replaces the
    previous handler) so a config-entry reload doesn't leave a stale
    handler pointing at a torn-down entity -- see issue #55's own
    conversation about the module-level import-caching gotcha with
    _ensure_ready() in solver_runtime.py.
    """
    _ENTITY_UPDATE_HANDLERS[entity_id] = handler


def unregister_entity_handler(entity_id: str) -> None:
    """Symmetric partner to register_entity_handler(); safe to call for
    an entity_id that was never registered. Called on config-entry
    unload so a torn-down entity's handler doesn't linger."""
    _ENTITY_UPDATE_HANDLERS.pop(entity_id, None)


def _native_http_error(entity_id: str, code: int, msg: str) -> urllib.error.HTTPError:
    # A real, well-formed HTTPError -- not a synthetic ad-hoc exception --
    # specifically so every one of this file's own existing
    # `except urllib.error.HTTPError as e: if e.code == 404` sites
    # (entity_exists, fetch_load_forecast_safe, fetch_solar_source_safe,
    # and several more scattered through main()) keeps working completely
    # unchanged in native mode too. fp=a real BytesIO (not None) matters:
    # the top-level __main__ guard's own `except ... e.read()` would
    # crash on a bare HTTPError(fp=None) if this were ever reached that
    # way instead -- confirmed by reading urllib.error.HTTPError's own
    # real implementation (it's a thin wrapper over its own `fp`).
    return urllib.error.HTTPError(
        url=f"native://{entity_id}",
        code=code,
        msg=msg,
        hdrs=None,
        fp=io.BytesIO(msg.encode("utf-8")),
    )


def ha_get(entity_id: str) -> dict:
    if _NATIVE_HASS is not None:
        # hass.states.get() is a plain, synchronous, in-memory dict
        # lookup (HA's own state machine) -- real, established practice
        # to call it from a worker thread (see solver_runtime.py's own
        # module docstring for the full "why this is safe" reasoning),
        # not textbook-perfect event-loop-only HA threading but the
        # pragmatic, low-risk choice given the alternative is restructuring
        # ~2400 lines of already-correct, already-live-tested logic.
        state = _NATIVE_HASS.states.get(entity_id)
        if state is None:
            raise _native_http_error(entity_id, 404, f"Entity {entity_id} not found")
        return {
            "entity_id": state.entity_id,
            "state": state.state,
            "attributes": dict(state.attributes),
        }
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
            '"Configure" button in Home Assistant, choose "Solver settings", '
            "and fill in every required field (battery capacity/SoC sensor, "
            "max charge/discharge power, grid import/export limits, live "
            "import/export price sensors, solar/load forecast sensors) "
            "before running this writer."
        )
        raise RuntimeError(msg)
    return state["attributes"]


def ha_post_state(entity_id: str, state, attributes: dict) -> None:
    if _NATIVE_HASS is not None:
        # Dispatch-table shortcut (2026-08-23, issue #55): if a real
        # SensorEntity has registered itself as the handler for this
        # entity_id, route through its own update_from_solver() so HA
        # sees a proper entity update (unique_id, device_info, device_
        # class, unrecorded_attributes) instead of a raw state-machine
        # write. Unregistered entity_ids still fall through to
        # states.async_set() below -- same behaviour as before this
        # seam existed, so any entity that hasn't been migrated yet
        # keeps working exactly as it always has.
        handler = _ENTITY_UPDATE_HANDLERS.get(entity_id)
        # nimbus issue #85 diagnostic (2026-08-23, not yet root-caused):
        # proves/disproves two real candidates in one line -- whether
        # ha_post_state() is being called MORE THAN ONCE per entity per
        # solve cycle at all (which this file's own two known call
        # sites, one each for these entity_ids, shouldn't produce), and
        # whether a call ever falls through to the raw states.async_set
        # fallback for an entity_id that SHOULD have a registered
        # handler (which would mean the handler was unregistered
        # between two calls -- a real, different bug class from #83).
        # print() kept for the standalone/cron/addon deployment (still
        # zero HA imports there, and this trace is genuinely useful when
        # tailing that process's own stdout). _LOGGER.debug() added
        # alongside it (2026-08-27, live devhub recurrence of #85's own
        # symptom on these same two entities, ~3x the expected write
        # frequency per the recorder's own 16 KB-attrs WARNING counts) --
        # purcell-lab's own suggested next capture on #85 was exactly
        # this: "_LOGGER (not print) for ha_post_state, so it lands in
        # error_log". Native-mode only (this whole branch already is),
        # so the standalone/addon "zero HA imports" contract is untouched.
        _trace_msg = (
            f"#85 trace: ha_post_state entity_id={entity_id} state={state!r} "
            f"attrs_keys={sorted(attributes.keys()) if attributes else attributes} "
            f"via_handler={handler is not None}"
        )
        print(f"[{datetime.now(UTC).isoformat()}] {_trace_msg}")
        _LOGGER.debug(_trace_msg)
        if handler is not None:
            _NATIVE_HASS.add_job(functools.partial(handler, state, attributes))
            return
        # states.async_set() mutates HA's own state machine and fires a
        # real event -- unlike the plain dict-read in ha_get() above,
        # this genuinely must happen ON the event loop, never directly
        # from a worker thread. hass.add_job() is HA's own documented
        # thread-safe scheduling primitive for exactly this: safe to call
        # from any thread, correctly hops onto the event loop itself.
        # Also #85 diagnostic: purcell-lab's own follow-up ask on that
        # issue was "a single trace line at every entry to the
        # states.async_set() fallback ... whether or not the dispatch
        # table routes elsewhere" -- via_handler=False above already
        # covers "did we reach the fallback", this WARNING-level line
        # additionally covers "did the fallback actually get scheduled",
        # visible without opting into DEBUG logging first.
        _LOGGER.warning(
            "Nimbus #85 trace: raw states.async_set fallback used for %s "
            "(no registered SensorEntity handler) state=%r attrs_keys=%s",
            entity_id,
            state,
            sorted(attributes.keys()) if attributes else attributes,
        )
        _NATIVE_HASS.add_job(
            functools.partial(
                _NATIVE_HASS.states.async_set, entity_id, state, attributes
            )
        )
        return
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


def ha_call_service(domain: str, service: str, data: dict) -> None:
    """Fire-and-forget HA service call -- same native/REST dual-mode
    split as ha_get()/ha_post_state() above. Currently used only for
    the one-time load-forecast-shape persistent notification (see
    _notify_load_forecast_error_once()) -- any failure here is
    deliberately swallowed by the caller, since a failed notification
    must never be allowed to break the actual solve."""
    if _NATIVE_HASS is not None:
        _NATIVE_HASS.add_job(
            functools.partial(_NATIVE_HASS.services.async_call, domain, service, data)
        )
        return
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"{HA_BASE}/api/services/{domain}/{service}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def ha_call_service_with_response(domain: str, service: str, data: dict) -> dict | None:
    """Same REST call as ha_call_service() above, but for a service that
    returns response data (return_response) -- e.g. weather.get_forecasts,
    whose whole forecast array is ONLY available via this response
    payload on any modern (2023+) HA weather entity, never a plain state
    attribute the way every other forecast source in this file publishes.
    Used by publish_weather_forecast_mirrors() (2026-08-25, nimbus repo
    dashboard follow-up) -- see that function's own docstring.

    Returns the service's own service_response dict (keyed by the
    entity_id(s) targeted), or None on any failure. Deliberately
    swallows every failure rather than raising -- every caller here
    already treats a missing/unavailable forecast source as a graceful
    no-op (see every other optional external source in this file), not
    a reason to break the actual solve.

    Native mode (2026-08-25, real bug found live on devhub -- this
    function's own first version returned None unconditionally here,
    silently no-op'ing publish_weather_forecast_mirrors() on any native
    install without ever surfacing why): same
    asyncio.run_coroutine_threadsafe() bridge fetch_price_history()'s
    own native branch already uses to call async, event-loop-owned HA
    APIs from this file's own sync executor-thread context -- a plain
    hass.services.async_call(..., blocking=True, return_response=True)
    is itself a coroutine, and there's no public sync-callable variant,
    same reasoning as that function's own comment.
    """
    if _NATIVE_HASS is not None:
        try:
            import asyncio

            async def _call() -> dict:
                return await _NATIVE_HASS.services.async_call(
                    domain,
                    service,
                    data,
                    blocking=True,
                    return_response=True,
                )

            future = asyncio.run_coroutine_threadsafe(_call(), _NATIVE_HASS.loop)
            return future.result(timeout=15)
        except Exception:  # noqa: BLE001 -- see this function's own "deliberately swallows every failure" docstring line
            return None
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"{HA_BASE}/api/services/{domain}/{service}?return_response",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ):
        return None
    return payload.get("service_response")


def publish_weather_forecast_mirrors(cfg: dict) -> None:
    """Real forward temperature/humidity forecast for the devhub
    dashboard's Forecaster chart (2026-08-25 follow-up to that chart's
    own extend_to fix) -- sourced from whatever real weather forecaster
    the household points CONF_SOLVER_WEATHER_FORECAST_SENSOR
    ("solver_weather_forecast_sensor") at (see that field's own comment
    in const.py). Never hardcodes a specific integration's entity_id --
    an earlier version of this function DID hardcode a weather.
    pirateweather/weather.home preference order, caught and corrected
    the same day this shipped (standing project rule: every entity
    reference is a wizard field, never a literal in code).

    Same two accepted shapes as CONF_TEMPERATURE_FORECAST_SENSOR
    (coordinator.py's own _async_fetch_temperature_forecast()): a
    weather.* entity (Nimbus calls weather.get_forecasts for you --
    modern HA weather entities publish forecast data ONLY via that
    service response, see ha_call_service_with_response()'s own
    docstring) or a sensor.* whose own 'forecast' attribute already
    carries datetime/temperature[/humidity] entries directly. Bridged
    into the same {time, value} shape ha_post_state() already uses for
    every other _forecast sensor in this file, so the dashboard's
    existing data_generator convention needs no special-casing.
    Pushed at the source's own native resolution -- no need to
    resample onto this file's own solver grid_times, since nothing
    here feeds the LP.

    Humidity is only published when the source's own forecast entries
    actually carry a 'humidity' field -- a fabricated humidity forecast
    would be strictly worse than none. Whether that's true depends
    entirely on which real forecaster the household has configured
    (e.g. Pirate Weather's hourly forecast carries one, Open-Meteo's
    doesn't) -- never assumed or guessed here.

    Purely cosmetic/dashboard -- never referenced by the actual LP
    solve. Graceful no-op if CONF_SOLVER_WEATHER_FORECAST_SENSOR isn't
    configured, or if the fetch fails, same as every other optional
    external source in this file.
    """
    entity_id = cfg.get("solver_weather_forecast_sensor")
    if not entity_id:
        return
    domain = entity_id.split(".", 1)[0]
    if domain == "weather":
        response = ha_call_service_with_response(
            "weather", "get_forecasts", {"entity_id": entity_id, "type": "hourly"}
        )
        hourly = (response or {}).get(entity_id, {}).get("forecast")
    else:
        try:
            state = ha_get(entity_id)
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            return
        hourly = (state.get("attributes", {}) if isinstance(state, dict) else {}).get(
            "forecast"
        )
    if not isinstance(hourly, list) or not hourly:
        return
    temp_points = [
        {"time": p["datetime"], "value": round(float(p["temperature"]), 1)}
        for p in hourly
        if isinstance(p, dict)
        and p.get("datetime") is not None
        and p.get("temperature") is not None
    ]
    humidity_points = [
        {"time": p["datetime"], "value": round(float(p["humidity"]), 1)}
        for p in hourly
        if isinstance(p, dict)
        and p.get("datetime") is not None
        and p.get("humidity") is not None
    ]
    generated_at = datetime.now(UTC).astimezone(BRISBANE_TZ).isoformat()
    if temp_points:
        ha_post_state(
            "sensor.nimbus_mirror_temperature_forecast",
            temp_points[0]["value"],
            {
                "unit_of_measurement": "°C",
                "friendly_name": "Nimbus Mirror Temperature Forecast",
                "forecast": temp_points,
                "source": entity_id,
                "generated_at": generated_at,
            },
        )
    if humidity_points:
        ha_post_state(
            "sensor.nimbus_mirror_humidity_forecast",
            humidity_points[0]["value"],
            {
                "unit_of_measurement": "%",
                "friendly_name": "Nimbus Mirror Humidity Forecast",
                "forecast": humidity_points,
                "source": entity_id,
                "generated_at": generated_at,
            },
        )


def parse_iso(s) -> datetime:
    # Real bug, confirmed live 2026-08-22 (first-ever native-mode run):
    # every call site here was written and only ever tested against
    # REST-sourced data, where a timestamp is ALWAYS a plain string --
    # HA's own JSON serialization stringifies every datetime on the way
    # out over HTTP, so REST mode's ha_get() never sees anything else.
    # Native mode's ha_get() (solver_runtime.py / set_native_hass())
    # reads state.attributes directly, the RAW unserialized Python
    # object -- and at least one real integration (Solcast, confirmed
    # via the live traceback) stores its own forecast[]'s own "time"
    # field as a genuine datetime object internally, not a string.
    # `s.replace("Z", "+00:00")` on a real datetime silently resolves to
    # datetime.replace() instead of str.replace() -- a completely
    # different method (year/month/day/... as integers), so Python
    # raises the confusing "'str' object cannot be interpreted as an
    # integer" rather than any hint this was ever a type mismatch.
    if isinstance(s, datetime):
        # Already a real datetime -- nothing to parse. HA's own internal
        # convention is that stored datetimes are timezone-aware
        # (almost certainly true here), but fall back to explicit UTC on
        # the off chance it's naive, matching what a bare "...Z"-suffixed
        # string would have meant on the REST-mode path above.
        return s if s.tzinfo is not None else s.replace(tzinfo=UTC)
    return datetime.fromisoformat(s)


def fetch_load_forecast_safe(entity_id: str) -> tuple[list[dict] | None, str | None]:
    """Real, per-entity-guarded, VALIDATED fetch for ONE of a household's
    own individually-forecasted circuits (2026-08-17, direct ask: "and
    individually wrapped into float 0"). Returns (None, error) (never
    raises) on ANY failure -- entity missing/renamed, HTTP error,
    malformed JSON, no usable 'forecast' attribute, wrong per-point
    shape, wrong unit -- so sum_load_forecasts() below can treat a
    genuinely unavailable OR malformed circuit as a safe, honest 0.0
    contribution rather than let it corrupt or crash the whole sum.

    Real bug found live (nimbus repo issue #105, Mark Purcell, a real
    independent installer's own live health-check, 2026-08-24 -- direct
    follow-up to #66): this function used to be a bare, unvalidated
    `ha_get(entity_id)["attributes"]["forecast"]` -- zero shape check,
    zero unit-hint scaling, zero per-point validation, the EXACT class
    of bug #66 already fixed on the single-sensor path
    (read_load_forecast_sensor()) but never applied here. A source
    entity publishing the wrong shape either got silently summed as
    raw, un-validated points (risking a garbage contribution to the
    whole sum, not just a dropped one) or, if the bare `["forecast"]`
    lookup itself raised, got silently zeroed with only a generic
    "unavailable" message -- no way to tell "this circuit's sensor is
    down" apart from "this circuit's sensor is UP but publishing
    something Nimbus can't parse," which is exactly the diagnostic gap
    that made #105's own real-world root cause (a genuinely wrong-shape
    or wrong-unit third-party forecast entity) hard to see from the
    outside. Now shares the SAME real validation as the single-sensor
    path -- see _validate_and_parse_load_forecast_attrs()'s own
    docstring for the full mechanism.

    Same real lesson already learned and documented once this project
    (sibling 116KAT-HA-AI repo's own CLAUDE.md, 2026-08-16 session,
    sensor.cb_total_combined_power_adjusted_kw): a household's own
    circuit sensors collectively have a meaningfully HIGHER chance that
    "at least one is briefly offline" than a single Modbus connection
    does -- guarding only the OUTER sum, not each individual term,
    doesn't help (a raw string-concatenation or KeyError from one bad
    entity happens before any outer guard gets a chance to catch it).
    Every entity sum_load_forecasts() fetches is wrapped exactly this
    way, individually, not just the total.
    """
    try:
        state = ha_get(entity_id)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        KeyError,
        json.JSONDecodeError,
    ) as e:
        print(
            f"WARN: {entity_id} unavailable ({e}) -- treating as 0.0 kW for this solve",
            file=sys.stderr,
        )
        return None, f"{entity_id} unavailable ({e})"

    attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
    fc_dicts, _has_bands, error = _validate_and_parse_load_forecast_attrs(
        entity_id, attrs
    )
    if error is not None:
        print(f"WARN: {error} -- treating as 0.0 kW for this solve", file=sys.stderr)
        return None, error
    return fc_dicts, None


def sum_load_forecasts(
    entity_ids: list[str],
    grid_times: list[datetime],
    inverter_self_consumption_kw: float = 0.0,
    now: datetime | None = None,
) -> tuple[
    list[float], list[float], list[float], list[str], dict[str, str], float | None
]:
    """Real household demand, summed from a household's own individually-
    forecasted circuits -- see load_forecast_entities in main() (the
    comment right above the module-level "Real per-load demand" block
    near the top of this file) for the full "why sum individual circuits
    instead of one whole-house entity" reasoning. Each entity is fetched
    via fetch_load_forecast_safe() (individually guarded AND validated,
    never crashes or silently corrupts the whole sum) and resampled with
    the SAME resample_forecast() every other forecast entity in this
    file already uses -- no new resampling logic needed.

    inverter_self_consumption_kw: a real, permanent, per-household bias
    (own comment above CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW in
    const.py has the full story) -- 0.0 is a genuine no-op, the caller's
    own responsibility to pass a real value if their install has one.

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
    without needing to poll each of the 18 entities itself. This list's
    own shape (a bare list[str] of entity_ids) is UNCHANGED from before
    -- kept backward compatible with that existing external contract.

    NEW (2026-08-24, issue #105): a fifth return value, `warnings` --
    entity_id -> the exact human-readable reason it was excluded (not
    just "unavailable", the real shape/unit-mismatch diagnostic
    fetch_load_forecast_safe() now surfaces). Genuinely additive --
    every existing caller destructuring the first 4 values still works.

    NEW (2026-08-25, issue #112): a sixth return value,
    `coverage_hours` -- the REAL forecast coverage of this sum, in
    hours ahead of `now` (defaults to grid_times[0], which
    build_tiered_grid() always sets to real "now" -- see that
    function's own docstring). Computed as the MINIMUM per-entity
    coverage across every entity that fetched successfully: the sum is
    only as trustworthy as its shortest-covered circuit, since every
    period beyond that circuit's real coverage is resample_forecast()'s
    own flat-hold padding, not a real forecast for the whole household.
    None if no entity fetched successfully (nothing real to report).
    """
    total_kw = [0.0] * len(grid_times)
    total_lower_kw = [0.0] * len(grid_times)
    total_upper_kw = [0.0] * len(grid_times)
    failed_entities: list[str] = []
    warnings: dict[str, str] = {}
    coverage_hours_per_entity: list[float] = []
    anchor = now if now is not None else (grid_times[0] if grid_times else None)
    for entity_id in entity_ids:
        fc, error = fetch_load_forecast_safe(entity_id)
        if fc is None:
            failed_entities.append(entity_id)
            if error is not None:
                warnings[entity_id] = error
            continue  # this load contributes 0.0 for every period
        if anchor is not None:
            entity_coverage = compute_forecast_coverage_hours(fc, anchor)
            if entity_coverage is not None:
                coverage_hours_per_entity.append(entity_coverage)
        pt_kw = resample_forecast(fc, "value", grid_times)
        pt_lower = resample_forecast(fc, "lower", grid_times)
        pt_upper = resample_forecast(fc, "upper", grid_times)
        for i in range(len(grid_times)):
            total_kw[i] += max(0.0, pt_kw[i])
            total_lower_kw[i] += max(0.0, pt_lower[i])
            total_upper_kw[i] += max(0.0, pt_upper[i])
    # Real, known, permanent inverter self-consumption bias (see this
    # function's own inverter_self_consumption_kw parameter docstring
    # above) -- added flat to every period, point AND band alike (a
    # known constant carries no real uncertainty of its own to widen the
    # band with). 0.0 by default -- a genuine no-op for any install that
    # hasn't configured a real value.
    total_kw = [v + inverter_self_consumption_kw for v in total_kw]
    total_lower_kw = [v + inverter_self_consumption_kw for v in total_lower_kw]
    total_upper_kw = [v + inverter_self_consumption_kw for v in total_upper_kw]
    # Defensive bracket, same reasoning as solar/load's own clamp
    # elsewhere in this file: guarantee lower <= point <= upper even if
    # one per-load band was individually inconsistent (elements.py's own
    # _validate_confidence_band() requires this exactly, at every period).
    total_lower_kw = [
        min(total_lower_kw[i], total_kw[i]) for i in range(len(grid_times))
    ]
    total_upper_kw = [
        max(total_upper_kw[i], total_kw[i]) for i in range(len(grid_times))
    ]
    coverage_hours = (
        min(coverage_hours_per_entity) if coverage_hours_per_entity else None
    )
    return (
        total_kw,
        total_lower_kw,
        total_upper_kw,
        failed_entities,
        warnings,
        coverage_hours,
    )


def compute_forecast_coverage_hours(
    fc_dicts: list[dict], anchor: datetime
) -> float | None:
    """Real coverage of a RAW (pre-resample) forecast list, in hours
    ahead of `anchor` -- nimbus repo issue #112 ("solver horizon 96.3h
    exceeds subentry forecast horizon 48h").

    resample_forecast()'s own nearest-at-or-before lookup has no upper
    bound: once grid_times runs past a source's real last timestamp,
    every remaining grid point silently reuses that same last real
    value forever. That's a genuine, load-bearing behavior (a flat
    hold beats a crash or a 0.0 cliff), but it also destroys the one
    piece of information that would let anyone SEE the gap -- by the
    time main() has resampled arrays in hand, "real point" and
    "padded-flat point" look identical. This function is called on the
    raw fc_dicts, before resampling, specifically to capture that real
    coverage span while it still exists.

    Returns None for an empty/unparseable list -- nothing to report.
    """
    times = [parse_iso(p["time"]) for p in fc_dicts if p.get("time") is not None]
    if not times:
        return None
    return max(0.0, (max(times) - anchor).total_seconds() / 3600.0)


def resample_forecast(
    forecast: list[dict], value_key: str, grid_times: list[datetime]
) -> list[float]:
    """Nearest-at-or-before lookup against the source's own native
    resolution -- must resample against the RAW forecast array, never an
    already-quantized grid (real bug found and fixed earlier in this
    build when this exact mistake flattened 5 of 6 test values)."""
    pts = sorted(
        (
            (parse_iso(p["time"]), p[value_key])
            for p in forecast
            if p.get(value_key) is not None
        ),
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


def _validate_and_parse_load_forecast_attrs(
    entity_id: str, attrs: dict
) -> tuple[list[dict] | None, bool, str | None]:
    """Shared shape/unit validation for ANY load-forecast source entity
    -- extracted (2026-08-24, nimbus repo issue #105, real follow-up to
    #66) from what used to be read_load_forecast_sensor()'s own inline
    logic, so BOTH the single-sensor path (solver_load_forecast_sensor)
    AND the multi-circuit summing path (fetch_load_forecast_safe(),
    used by sum_load_forecasts()) get the same real protection --
    previously only the single-sensor path did, meaning a malformed or
    wrong-unit source configured in solver_load_forecast_entities could
    silently corrupt or 0.0-drop one circuit's own contribution to an
    18-circuit sum with zero diagnostic signal, the exact same class of
    problem #66 already fixed on the OTHER path.

    Real bug found live (nimbus repo issue #66, Mark Purcell, 2026-08-23):
    a bare `attrs["forecast"]` read, zero validation, either crashed
    (an uncaught KeyError) or degraded with a genuinely useless flat
    plan and no operator-visible signal at all on any sensor publishing
    a different shape.

    Genuinely common in practice, not a hypothetical: EMHASS's own
    load-forecast sensors publish under `scheduled_forecast` (not
    `forecast`), with per-point keys `date`/`<the sensor's own
    object_id>` (not `time`/`value`), as STRING values, frequently in W
    not kW. Auto-detected and handled here as a known, safe alternate
    shape -- not because every possible shape can be guessed, but
    because this one is common enough, and unambiguous enough (the
    sensor's own object_id names its value column), to convert safely
    rather than just report a clearer error.

    Returns (fc_dicts, has_bands, error) -- fc_dicts is None together
    with a non-None error on failure. Callers must check error, not
    just truthiness of fc_dicts (an entity that resolves to zero points
    after real filtering is itself the error case, not a valid empty
    success).
    """
    raw_fc = attrs.get("forecast")
    time_key, value_key, has_bands = "time", "value", True

    # Real bug found live (devhub, 2026-08-24, first-ever solve on a freshly
    # configured install): `not raw_fc` is True for BOTH "missing/wrong-shape
    # attribute" and "attribute present, correct type, genuinely empty list"
    # -- a brand-new load subentry's ML forecaster hasn't trained yet and
    # legitimately publishes `forecast: []` for its first several days. The
    # old single combined branch below reported that as "has no usable
    # 'forecast' attribute (list-valued attributes present: ['forecast'])"
    # -- naming the very attribute it just rejected, which reads as a
    # malformed-sensor error when the real, expected cause is "not trained
    # yet." These are genuinely different operator actions (fix your sensor
    # vs. just wait) and need genuinely different messages.
    if isinstance(raw_fc, list) and not raw_fc:
        return (
            None,
            False,
            (
                f"{entity_id}'s 'forecast' attribute is present but empty "
                f"(0 points) -- this is expected for the first few days after "
                f"a load subentry is created, before its ML forecaster has "
                f"trained on enough real recorder history. Not a malformed "
                f"sensor; check back once training history has accumulated."
            ),
        )

    if not isinstance(raw_fc, list) or not raw_fc:
        # Not the canonical shape -- try the one known common alternate
        # (EMHASS's own scheduled_forecast/date/<object_id> pattern).
        alt_fc = attrs.get("scheduled_forecast")
        object_id = entity_id.split(".", 1)[-1]
        if (
            isinstance(alt_fc, list)
            and alt_fc
            and isinstance(alt_fc[0], dict)
            and object_id in alt_fc[0]
        ):
            raw_fc, time_key, value_key, has_bands = alt_fc, "date", object_id, False
        else:
            available = sorted(k for k, v in attrs.items() if isinstance(v, list))
            return (
                None,
                False,
                (
                    f"{entity_id} has no usable 'forecast' attribute (list-valued "
                    f"attributes present: {available or 'none'}). Expected a list "
                    f"of dicts with a 'time' and 'value' key -- see the canonical "
                    f"shape any sensor.nimbus_<load>_forecast entity publishes."
                ),
            )

    parsed_points = []
    for point in raw_fc:
        if not isinstance(point, dict):
            continue
        t_raw, v_raw = point.get(time_key), point.get(value_key)
        if t_raw is None or v_raw is None:
            continue
        try:
            parse_iso(t_raw)
            float(v_raw)
        except (ValueError, TypeError):
            continue
        parsed_points.append(point)

    if not parsed_points:
        return (
            None,
            False,
            (
                f"{entity_id}'s forecast has {len(raw_fc)} point(s) but none "
                f"parsed cleanly under keys '{time_key}'/'{value_key}'."
            ),
        )

    # Unit hint: W -> kW, using the sensor's own unit_of_measurement --
    # real, live, not guessed (EMHASS's own repro published exactly this
    # combination: unit_of_measurement 'W', raw values in the hundreds).
    # Nimbus's own canonical forecast entities are always already kW, so
    # this only ever fires on the alternate-shape path in practice, but
    # checked unconditionally rather than assumed.
    scale = (
        1.0 / 1000.0
        if str(attrs.get("unit_of_measurement", "")).strip().lower() == "w"
        else 1.0
    )

    fc_dicts = []
    for point in parsed_points:
        entry = {"time": point[time_key], "value": float(point[value_key]) * scale}
        if has_bands:
            if point.get("lower") is not None:
                entry["lower"] = point["lower"]
            if point.get("upper") is not None:
                entry["upper"] = point["upper"]
        fc_dicts.append(entry)

    return fc_dicts, has_bands, None


def read_load_forecast_sensor(
    entity_id: str, grid_times: list[datetime], now: datetime | None = None
) -> tuple[
    list[float] | None,
    list[float] | None,
    list[float] | None,
    str | None,
    float | None,
]:
    """Validated read of the single-sensor load-forecast fallback (the
    solver_load_forecast_sensor wizard field -- used when a household
    hasn't configured individual Nimbus Load subentries). Shape/unit
    validation itself lives in _validate_and_parse_load_forecast_attrs()
    (see that function's own docstring for the full #66 history) --
    this function's own job is just the fetch and the resample/clamp
    into a grid-aligned (value, lower, upper) triple.

    Returns (load_kw, load_lower_kw, load_upper_kw, error, coverage_hours)
    -- error is None on success. The three arrays are None together with
    error on failure -- callers must check error, not just truthiness. A
    STRUCTURALLY valid but near-all-zero series (real timestamps, real
    parseable values, just <10% of points meaningfully nonzero) is
    treated as a failure, not "a valid, if unusual, success" -- see
    the real #118 incident this specific check exists for, right
    before the final return below.

    coverage_hours (NEW, 2026-08-25, issue #112): the source's own real
    forecast coverage, in hours ahead of `now` (defaults to
    grid_times[0], real "now" per build_tiered_grid()'s own docstring)
    -- computed on the RAW fc_dicts, before resample_forecast() pads
    flat past it. None on failure or an unparseable list.
    """
    try:
        state = ha_get(entity_id)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        return None, None, None, f"{entity_id} could not be read ({e})", None

    attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
    fc_dicts, has_bands, error = _validate_and_parse_load_forecast_attrs(
        entity_id, attrs
    )
    if error is not None:
        return None, None, None, error, None

    anchor = now if now is not None else (grid_times[0] if grid_times else None)
    coverage_hours = (
        compute_forecast_coverage_hours(fc_dicts, anchor)
        if anchor is not None
        else None
    )

    load_kw = [max(0.0, v) for v in resample_forecast(fc_dicts, "value", grid_times)]
    if has_bands:
        # Exactly the original pre-#66 behavior for the canonical shape:
        # resample_forecast() returns 0.0 for every point when a source
        # genuinely has no lower/upper keys at all, and the clamp below
        # then widens that to [0, load_kw] rather than a zero-width band
        # -- preserved as-is for backward compatibility with any
        # existing install already relying on this exact shape.
        load_lower_kw = [
            max(0.0, v) for v in resample_forecast(fc_dicts, "lower", grid_times)
        ]
        load_upper_kw = [
            max(0.0, v) for v in resample_forecast(fc_dicts, "upper", grid_times)
        ]
        load_lower_kw = [
            min(load_lower_kw[i], load_kw[i]) for i in range(len(grid_times))
        ]
        load_upper_kw = [
            max(load_upper_kw[i], load_kw[i]) for i in range(len(grid_times))
        ]
    else:
        # EMHASS's own shape carries no confidence band at all -- zero-
        # width around the point estimate, same convention already used
        # elsewhere in this file for a genuinely bandless input.
        load_lower_kw, load_upper_kw = list(load_kw), list(load_kw)

    # Real bug found live (nimbus repo issue #118, Mark Purcell, a real
    # independent installer's own live health-check, 2026-08-24, direct
    # follow-up to #111): a genuinely common configuration mistake --
    # pointing solver_load_forecast_sensor at Nimbus's OWN household-
    # total aggregator (sensor.nimbus_household_load_total_forecast)
    # instead of a real per-signal forecast entity -- creates a
    # circular reference. With no individual circuits configured, the
    # aggregator's own upstream is empty, so it publishes a real,
    # structurally-valid {time, value} shape (passing every check
    # above) that's near-all-zero except the live "now" anchor point.
    # This function's own docstring used to call that "a valid, if
    # unusual, success" -- true in the abstract, but in practice a real
    # household's consumption essentially never sits at true zero for
    # the vast majority of a multi-day forecast (unlike solar, which
    # legitimately does every night -- see the SEPARATE, deliberately
    # different fallback for that in main()'s solar-fetching block).
    # Left unguarded, this produced a confident-looking "optimal" solve
    # with load_forecast_source_error still None (nothing flagged it) --
    # the household was told the battery could safely export ~$46/day
    # more than it actually could, because the solver believed nobody
    # was consuming anything. Threshold and reasoning are Mark's own
    # proposed fix direction from #118, applied here.
    nonzero_points = sum(1 for v in load_kw if v > 0.01)
    if len(load_kw) > 0 and (nonzero_points / len(load_kw)) < 0.1:
        return (
            None,
            None,
            None,
            (
                f"{entity_id}'s forecast has only {nonzero_points}/"
                f"{len(load_kw)} non-trivial (>0.01 kW) points -- a real "
                f"household load essentially never sits at true zero for "
                f"90%+ of a multi-day forecast. This usually means "
                f"solver_load_forecast_sensor is pointed at Nimbus's own "
                f"household-total aggregator (sensor.nimbus_household_"
                f"load_total_forecast) with no individual circuits "
                f"configured -- a circular reference, since the "
                f"aggregator has nothing to sum. Point this field at a "
                f"real per-signal forecast entity instead (e.g. "
                f"sensor.nimbus_<your_load_signal>_forecast)."
            ),
            None,
        )

    return load_kw, load_lower_kw, load_upper_kw, None, coverage_hours


def _notify_load_forecast_error_once(error: str) -> None:
    """Fires a real HA persistent_notification, but only once per
    genuinely NEW error message -- an unchanging misconfiguration
    shouldn't re-notify every single cron cycle, but a DIFFERENT new
    error (e.g. the sensor started, then broke a different way) should
    still surface. Tracked via a plain sentinel file (see
    LOAD_FORECAST_ERROR_NOTIFIED_PATH's own comment) holding the exact
    last-notified message. Any failure here (can't write the sentinel,
    can't reach HA's service-call endpoint) is deliberately swallowed --
    a notification is a courtesy, never allowed to break the real solve.
    """
    try:
        already = ""
        if os.path.exists(LOAD_FORECAST_ERROR_NOTIFIED_PATH):
            with open(LOAD_FORECAST_ERROR_NOTIFIED_PATH, "r", encoding="utf-8") as f:
                already = f.read()
        if already == error:
            return
        ha_call_service(
            "persistent_notification",
            "create",
            {
                "title": "Nimbus Solver: load forecast misconfigured",
                "message": (
                    f"{error}\n\nThe Solver is using a flat 0.0 kW load "
                    "placeholder until this is fixed -- the plan it "
                    "publishes is not usable while this stands. Open the "
                    'Nimbus hub\'s "Solver settings" and check the load '
                    "forecast source, or configure individual Load "
                    "subentries instead."
                ),
                "notification_id": "nimbus_solver_load_forecast_error",
            },
        )
        with open(LOAD_FORECAST_ERROR_NOTIFIED_PATH, "w", encoding="utf-8") as f:
            f.write(error)
    except Exception:  # noqa: BLE001, S110 -- a failed notification/sentinel-file write must never break the solve; nothing to log or react to beyond that
        pass


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
    except Exception:  # noqa: BLE001 -- a missing/malformed P2P forecast source degrades to a flat 0.0 array, never crashes the solve
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
    fallback_rate = (
        statistics.median(real_positive_rates) if real_positive_rates else 0.0
    )

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
    (
        "solver_p2p_block_1_rate_kw",
        "solver_p2p_block_1_start_hour",
        "solver_p2p_block_1_end_hour",
    ),
    (
        "solver_p2p_block_2_rate_kw",
        "solver_p2p_block_2_start_hour",
        "solver_p2p_block_2_end_hour",
    ),
    (
        "solver_p2p_block_3_rate_kw",
        "solver_p2p_block_3_start_hour",
        "solver_p2p_block_3_end_hour",
    ),
)

# Real, live household automation design (config/automations.yaml):
# p2p_battery_sell_end_midnight switches the battery to Self-Consume the
# INSTANT the P2P window closes (00:00:00 sharp, no ramp), and
# p2p_haeo_resume_at_4am hands control back at 04:00:00. This is a
# deterministic SWITCH, not something reasoning about marginal profit --
# real, direct household finding (2026-08-22): Nimbus's own shadow plan
# kept discharging for ~30-40 minutes PAST that real cutoff, because the
# existing terminal_value_period_indices mechanism (solver/network.py,
# nimbus repo) is a SOFT economic nudge, and a soft nudge can always be
# outbid if the real export price is still positive enough to look
# marginally profitable. A hard, deterministic real-world rule needs a
# hard LP constraint to match it, not a stronger nudge.
SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE = 4


def fetch_p2p_fixed_export_kw(
    cfg: dict, grid_times: list[datetime]
) -> list[float] | None:
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

    Real, direct fix (2026-08-22, see SELF_CONSUME_HOURS_AFTER_MIDNIGHT_
    CLOSE's own comment above for the full "why a soft nudge alone
    wasn't enough" story): for any block that runs THROUGH midnight
    (end_hour==24), export is ALSO hard-pinned to exactly 0.0kW for the
    following SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE hours -- matching
    the real automation's own deterministic self-consume window, every
    real calendar day this multi-day horizon spans, not just the
    nearest one. 0.0kW export, not "no constraint" -- the real automation
    genuinely stops exporting to the grid at that boundary (self-consume
    still lets the battery cover house load, which is a completely
    separate, still-LP-free decision; only the GRID-EXPORT variable
    itself is pinned here).
    """
    blocks: list[tuple[float, int, int]] = []
    for rate_key, start_key, end_key in P2P_BLOCK_KEYS:
        try:
            rate_kw = _cfg_num(cfg, rate_key, 0.0)
            start_hour = _cfg_int(cfg, start_key, 0)
            end_hour = _cfg_int(cfg, end_key, 0)
        except (TypeError, ValueError):
            continue
        if rate_kw <= 0 or end_hour <= start_hour:
            continue
        blocks.append((rate_kw, start_hour, end_hour))

    if not blocks:
        return None

    runs_through_midnight = any(
        end_hour == 24 for _rate_kw, _start_hour, end_hour in blocks
    )

    result: list[float] = []
    for gt in grid_times:
        matched_rate = float("nan")
        for rate_kw, start_hour, end_hour in blocks:
            if start_hour <= gt.hour < end_hour:
                matched_rate = rate_kw
                break
        if runs_through_midnight and gt.hour < SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE:
            matched_rate = 0.0
        result.append(matched_rate)
    return result


def fetch_price_history(entity_id: str, days: int = 5) -> list[tuple[datetime, float]]:
    """Real recorded history for a single sensor's numeric state, as
    (local time, value) points -- the shared building block for
    compute_5min_offset() below. Returns [] (callers fall back further)
    if history is genuinely unavailable -- must never crash the writer.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    if _NATIVE_HASS is not None:
        # Real recorder history, in-process -- no HTTP round-trip at all.
        # state_changes_during_period(hass, start, end, entity_id,
        # no_attributes=..., ...) -> {entity_id: [State, ...]}, verified
        # against HA core's own current source.
        #
        # Real bug caught and fixed live (2026-08-22, first native-mode
        # test): calling this DIRECTLY from here -- a plain function
        # running inside solver_runtime.py's own hass.async_add_executor_
        # job() worker thread -- tripped HA's own recorder safety check:
        # "accesses the database without the database executor." The
        # recorder keeps its OWN dedicated executor, separate from HA's
        # generic one this whole solve already runs inside, specifically
        # because its underlying DB session isn't meant to be touched
        # from just any worker thread. Recorder.async_add_executor_job()
        # is itself event-loop-only (a @callback, returns an
        # asyncio.Future) -- no public sync-callable variant exists to
        # call it directly from here. asyncio.run_coroutine_threadsafe()
        # is the standard, genuinely correct stdlib bridge for exactly
        # this: schedule a coroutine onto hass's OWN event loop from this
        # worker thread, block for the result via .result(). Import kept
        # INSIDE the try so a wrong path/signature degrades to the same
        # honest [] fallback every other failure mode here already uses,
        # never a crash -- this is a real, but non-critical, price-band
        # enrichment.
        try:
            import asyncio

            from homeassistant.components.recorder import (
                get_instance as _recorder_get_instance,
            )
            from homeassistant.components.recorder import history as _recorder_history

            async def _fetch() -> dict:
                return await _recorder_get_instance(
                    _NATIVE_HASS
                ).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    _NATIVE_HASS,
                    start,
                    end,
                    entity_id,
                    True,  # no_attributes
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), _NATIVE_HASS.loop)
            changes = future.result(timeout=30)
            states = changes.get(entity_id, [])
        except Exception:  # noqa: BLE001 -- a recorder read failure degrades to no price-band enrichment, never crashes the solve
            return []
        out: list[tuple[datetime, float]] = []
        for s in states:
            try:
                v = float(s.state)
            except (TypeError, ValueError):
                continue
            out.append((s.last_changed.astimezone(BRISBANE_TZ), v))
        return sorted(out, key=lambda x: x[0])
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


def resample_generic_price_forecast(
    entity_id: str, grid_times: list[datetime]
) -> list[float] | None:
    """Generic {time, value}-shaped forecast resampler for the portable
    FALLBACK price path below (2026-08-22, real ask from Mark Purcell's
    own install: his configured price sensors ARE real, live, genuinely
    dynamic Amber Electric sensors -- not a hardcoded placeholder -- but
    the BASE amberelectric price sensor carries no forecast attribute at
    all; Amber only exposes forward-looking prices via a separate
    service call (amberelectric.get_forecasts), which needs its own
    small helper sensor to expose as a real attribute -- see this
    project's docs/real-world-integration/files/
    amber_forecast_for_solver.yaml for a real, working example any
    Amber-using installer can drop in).

    Works for ANY price sensor whose own `forecast` attribute is a list
    of {"time": <iso datetime string>, "value": <float>} dicts -- the
    same simple convention this project's own LocalVolts sensors already
    use (see resample_price_with_extrapolation()'s own real-world
    equivalent above), so this isn't Amber-specific despite the
    motivating case -- any future portable price source that produces
    this same shape gets picked up automatically, no config-flow change
    needed. This household's own has_localvolts branch never reaches
    this function -- it exists purely for the portable fallback path,
    kept in sync with the sibling standalone script.

    Step ("hold the most recent point") lookup, deliberately NOT linear
    interpolation between two forecast points -- smearing a genuine
    price step into a fake ramp is a real, already-documented bug class
    in this project's own history (HAEO's own forecast_fuser, see
    CLAUDE.md's session-41-era investigation) and Amber's own forecasts
    are themselves already step-shaped (each interval duplicated at its
    own start_time and end_time, both carrying the same value -- see the
    real amber_forecast_for_solver.yaml companion file).

    Returns None (caller falls back to the flat current-value repeat) if
    the entity has no `forecast` attribute, it's empty, or nothing in it
    parses -- never raises. A period at or before the earliest available
    forecast point uses that point's own value (best available), rather
    than leaving a real gap.

    Also accepts `calibrated` as a fallback key when `value` is absent
    (2026-08-25, direct household ask to blend in Mark Purcell's own
    NEM PD7 sensor, sensor.nem_pd7day_qld1_nem_spot_price_forecast --
    see fetch_aemo_forecast()'s own docstring for the full "why
    calibrated, not raw_value" story: NEM PD7 runs a real isotonic-
    regression spike-calibration algorithm against historical AEMO
    forecast accuracy, exactly the kind of "commercial grade algorithm"
    a plain hold-flat/linear-extrapolation fallback isn't). Before this,
    pointing a new solver_import/export_price_sensor_2/_3 field directly
    at NEM PD7 silently produced zero usable points -- every one of its
    forecast entries carries `calibrated`/`raw_value`/`spike_credible`,
    never a bare `value` key, so the strict-`value` parse below dropped
    every single point without error. `value` is still tried FIRST and
    preferred where present -- this only widens what counts as usable,
    it never changes what an existing Amber-shaped sensor resolves to.
    """
    r = resample_generic_price_forecast_with_coverage(entity_id, grid_times)
    return r[0] if r is not None else None


def resample_generic_price_forecast_with_coverage(
    entity_id: str, grid_times: list[datetime]
) -> tuple[list[float], list[bool]] | None:
    """Same resampling as resample_generic_price_forecast() (see that
    function's own docstring for the full "why a generic {time,value}
    resampler" story), plus a per-period REAL-coverage mask alongside
    the values.

    `real_mask[i]` is True only when grid_times[i] falls within this
    source's own [first real point, last real point] span -- False for
    any period where the "hold the most recent point" step-lookup above
    had nothing real to hold yet (before the source's first point) or
    is repeating its last point flat because the source's own forecast
    doesn't reach that far. Extracted as its own function (2026-08-27,
    nimbus repo issue #216, Mark Purcell) specifically so
    blend_price_with_secondary_sources() can tell a genuine second
    opinion apart from a source silently repeating its own boundary
    value -- see that function's own docstring for why the distinction
    matters. resample_generic_price_forecast() itself just discards the
    mask, unchanged for every other existing caller.
    """
    try:
        state = ha_get(entity_id)
    except Exception:  # noqa: BLE001 -- see docstring: "never raises", caller falls back to the flat current-value repeat
        return None
    forecast = state.get("attributes", {}).get("forecast")
    if not forecast:
        return None
    points: list[tuple[datetime, float]] = []
    for f in forecast:
        try:
            t = parse_iso(f["time"])
            raw = f["value"] if "value" in f else f["calibrated"]
            v = float(raw)
        except (KeyError, TypeError, ValueError):
            continue
        points.append((t, v))
    if not points:
        return None
    points.sort(key=lambda p: p[0])
    first_t, last_t = points[0][0], points[-1][0]
    result: list[float] = []
    real_mask: list[bool] = []
    for gt in grid_times:
        candidates = [v for t, v in points if t <= gt]
        result.append(candidates[-1] if candidates else points[0][1])
        real_mask.append(first_t <= gt <= last_t)
    return result, real_mask


def blend_price_with_secondary_sources(
    primary: list[float],
    cfg: dict,
    secondary_keys: tuple[str, str],
    grid_times: list[datetime],
    primary_real_mask: list[bool] | None = None,
) -> tuple[list[float], NDArray[np.float64] | None]:
    """Optional second/third price source blending (2026-08-25, direct
    household ask: "u also are missing my blended price forecasts...
    in case we can feed it more than one... e.g. aemo... and amber").

    `primary` is whatever spot_import_raw/spot_export either the
    has_localvolts or generic branch above already produced -- this
    function never re-derives it, only optionally blends more sources
    in on top. `secondary_keys` is a (key_2, key_3) pair of cfg keys
    (e.g. ("solver_import_price_sensor_2", "solver_import_price_sensor_3"))
    so the same function serves both import and export without
    duplicating the fetch/blend loop.

    Fetches each configured secondary source via
    resample_generic_price_forecast_with_coverage() (the same generic
    resampler the portable fallback branch already uses -- also accepts
    NEM PD7's own `calibrated` field, see that function's own
    docstring), and returns cross_source_spread() as a real, earned
    disagreement-based uncertainty signal (not an arbitrary knob) for
    the caller to feed into price_risk_aversion's own band, exactly as
    solar's own multi-source spread widens its confidence band.

    COVERAGE-AWARE (2026-08-27, nimbus repo issue #216, Mark Purcell:
    "Solver export_price is a ~0.5x linear compression of the Amber
    Express feed-in sensor"). Previously blended every configured
    source at a flat, unconditional equal weight across the WHOLE grid,
    including periods where a source's own real forecast doesn't reach
    yet -- resample_generic_price_forecast() silently holds a source's
    own first/last real point flat for any grid time outside its real
    coverage, and the old blend treated that placeholder exactly like a
    genuine second opinion, diluting a fully real primary value 50/50
    with a source that was really just repeating its own boundary
    number. Root-caused live: Mark's `_sensor_2` (a "day 2-7" AEMO
    forecast with no real data for today at all) was blended 50/50
    against a fully-real Amber Express primary for the ENTIRE captured
    window, producing exactly the reported ~0.5x + constant-offset
    compression -- confirmed by his own follow-up test clearing
    `_sensor_2` and getting an exact 1:1 pass-through (R²=1.0000).

    Now each configured source (primary included, via
    `primary_real_mask` -- callers that don't have one default to
    "always real", i.e. today's behaviour) contributes to a period ONLY
    when that period is within that source's own real coverage span.
    Periods where every configured source genuinely covers still blend
    exactly as before; a period where only one source has real data
    passes that source through unchanged (matches Mark's own
    clear-`_sensor_2` finding); the rare period where NOTHING has real
    coverage falls back to the old equal-weight blend across every
    source (the honest "everyone's guessing" case) rather than an
    arbitrary pick.

    Returns `(primary, None)` completely unchanged whenever no
    secondary source is configured OR configured but currently
    unavailable -- a single-source install (the overwhelming majority
    today) is byte-identical to before this function existed.
    """
    sources = [np.array(primary, dtype=float)]
    real_masks: list[list[bool]] = [
        list(primary_real_mask)
        if primary_real_mask is not None
        else [True] * len(primary)
    ]
    for key in secondary_keys:
        entity_id = cfg.get(key)
        if entity_id:
            r = resample_generic_price_forecast_with_coverage(entity_id, grid_times)
            if r is not None:
                fc, mask = r
                sources.append(np.array(fc, dtype=float))
                real_masks.append(mask)
    if len(sources) == 1:
        return primary, None
    n = len(primary)
    blended = np.empty(n, dtype=float)
    for i in range(n):
        real_idx = [j for j in range(len(sources)) if real_masks[j][i]]
        idx = real_idx if real_idx else list(range(len(sources)))
        blended[i] = float(np.mean([sources[j][i] for j in idx]))
    return list(blended), cross_source_spread(sources)


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
        fc = ha_get("sensor.nem_pd7day_qld1_nem_spot_price_forecast")["attributes"][
            "forecast"
        ]
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError):
        return []
    return sorted(
        (
            (parse_iso(p["time"]), p["calibrated"])
            for p in fc
            if p.get("calibrated") is not None
        ),
        key=lambda x: x[0],
    )


def compute_5min_offset(
    real_history: list[tuple[datetime, float]], days: int = 5
) -> dict[int, float]:
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
    aemo_history = fetch_price_history(
        "sensor.aemo_nem_qld1_current_5min_period_price", days=days
    )
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


def compute_price_percentile_band(
    price_history: list[tuple[datetime, float]], percentile: float
) -> dict[int, float]:
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


def apply_price_band(
    point_price: list[float], grid_times: list[datetime], band_by_5min: dict[int, float]
) -> list[float] | None:
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
) -> tuple[list[float], list[bool]]:
    """Same nearest-at-or-before lookup as resample_forecast() for any
    grid_time within the real forecast's own coverage -- but for periods
    BEYOND the last real data point, uses real AEMO forward spot data
    for that future period plus the real, 5-min-of-day retail offset
    (offset_by_5min, see compute_5min_offset()) instead of freezing the
    last real point flat. Falls back to the last real value if AEMO
    itself has no coverage for a given period (should not happen given
    AEMO's real 7.6-day coverage vs a 96h horizon, but defensive) --
    must never crash the writer.

    Also returns a per-period real-coverage mask alongside the values
    (2026-08-27, nimbus repo issue #216 -- same coverage-aware blending
    story as resample_generic_price_forecast_with_coverage(), see that
    function's own docstring). True only for a grid time within
    [this source's first real point, its last real point] -- False both
    before the first real point (the `pts[0][1]` placeholder below) and
    at/after the last one, since AEMO-extrapolated periods are a
    synthetic fill-in for THIS source, not a second genuine opinion.
    """
    pts = sorted(
        (
            (parse_iso(p["time"]), p[value_key])
            for p in forecast
            if p.get(value_key) is not None
        ),
        key=lambda x: x[0],
    )
    if not pts:
        return [0.0 for _ in grid_times], [False for _ in grid_times]
    first_real_time = pts[0][0]
    last_real_time = pts[-1][0]
    last_real_value = pts[-1][1]

    def nearest_before(
        source_pts: list[tuple[datetime, float]], gt: datetime
    ) -> float | None:
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
    real_mask = []
    for gt in grid_times:
        if gt <= last_real_time:
            val = pts[0][1]
            for t, v in pts:
                if t <= gt:
                    val = v
                else:
                    break
            out.append(float(val))
            real_mask.append(first_real_time <= gt)
            continue
        aemo_v = nearest_before(aemo_pts, gt)
        if aemo_v is not None:
            bucket = gt.hour * 12 + gt.minute // 5
            out.append(float(aemo_v + offset_by_5min.get(bucket, 0.0)))
        else:
            out.append(float(last_real_value))
        real_mask.append(False)
    return out, real_mask


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
    every coarse period's own import_fee_rate(cfg, hour) TOU lookup
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
        periods = elements.PeriodGrid(
            hours=hours_arr, start=parse_iso(data["period_start"])
        )
        return network.Plan(
            status="optimal",
            periods=periods,
            battery_charge_kw=np.array(data["battery_charge_kw"]),
            battery_discharge_kw=np.array(data["battery_discharge_kw"]),
            battery_soc_kwh=np.zeros(
                n
            ),  # not read by the stability mechanisms, zero-fill is fine
            grid_import_kw=np.array(data["grid_import_kw"]),
            grid_export_kw=np.array(data["grid_export_kw"]),
            export_bonus_kw=np.zeros(
                n
            ),  # not read by the stability mechanisms, zero-fill is fine
            solar_used_kw=np.zeros(n),
            solar_curtailed_kw=np.zeros(n),
            sheddable_loads=[],
            adequacy_loads=[],
            total_cost=None,
            iterations=0,
        )
    except (KeyError, ValueError):
        return None


def save_plan_state(
    plan: network.Plan, period_hours_arr: list[float], period_start: datetime
) -> None:
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
        print(
            f"WARN: could not save plan state ({e}) -- next run will solve without stability continuity",
            file=sys.stderr,
        )


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
    volumes = [
        hist[d].get("export_volume", 0.0)
        for d in dates
        if hist[d].get("export_volume", 0.0) > 0
    ]
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


QUALITY_ENTITY_ID = "sensor.nimbus_solver_quality_report"


def fetch_entity_history_range(
    entity_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Real recorded numeric history for one entity over an EXPLICIT
    [start, end) window -- generalizes fetch_price_history()'s own
    "last N days up to right now" shape (this file's other history
    fetch) into an explicit day-window fetch, needed by
    compute_daily_quality_report() to score one specific, already-
    elapsed calendar day rather than a rolling recent window. Same
    REST/native dual-mode split, same "degrade to [] on any failure,
    never crash" discipline as every other real-data fetch in this
    file.
    """
    if _NATIVE_HASS is not None:
        try:
            import asyncio

            from homeassistant.components.recorder import (
                get_instance as _recorder_get_instance,
            )
            from homeassistant.components.recorder import history as _recorder_history

            async def _fetch() -> dict:
                return await _recorder_get_instance(
                    _NATIVE_HASS
                ).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    _NATIVE_HASS,
                    start,
                    end,
                    entity_id,
                    True,  # no_attributes
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), _NATIVE_HASS.loop)
            changes = future.result(timeout=30)
            states = changes.get(entity_id, [])
        except Exception:  # noqa: BLE001 -- a recorder read failure degrades to no history for this entity, never crashes the scoring cycle
            return []
        out: list[tuple[datetime, float]] = []
        for s in states:
            try:
                v = float(s.state)
            except (TypeError, ValueError):
                continue
            out.append((s.last_changed.astimezone(BRISBANE_TZ), v))
        return sorted(out, key=lambda x: x[0])
    url = (
        f"{HA_BASE}/api/history/period/{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    if not data or not data[0]:
        return []
    out = []
    for p in data[0]:
        state = p.get("state")
        if state in (None, "unknown", "unavailable"):
            continue
        try:
            v = float(state)
        except (TypeError, ValueError):
            continue
        out.append((parse_iso(p["last_changed"]).astimezone(BRISBANE_TZ), v))
    return sorted(out, key=lambda x: x[0])


def resample_history_nearest(
    pts: list[tuple[datetime, float]], grid_times: list[datetime], default: float = 0.0
) -> list[float]:
    """Nearest-at-or-before lookup against real recorded history points
    -- same convention as resample_forecast() elsewhere in this file,
    just against (datetime, value) tuples from fetch_entity_history_
    range() instead of a forecast's own {time, value} dicts."""
    out = []
    for gt in grid_times:
        val = pts[0][1] if pts else default
        for t, v in pts:
            if t <= gt:
                val = v
            else:
                break
        out.append(float(val))
    return out


def compute_daily_quality_report(cfg: dict, now: datetime) -> dict | None:
    """Generic, retailer-agnostic built-in EPR/regret/tracking quality
    score (2026-08-25, direct ask: "it should be a part of the suite to
    monitor epr and trend and regret... nimbus should have it built
    in") -- a from-scratch generalization of the real-world reference
    script (docs/real-world-integration/files/nimbus_solver_quality_
    writer.py), which hardcodes one household's own LocalVolts/Sungrow/
    Modbus stack throughout. This version uses ONLY genuinely portable
    inputs: real recorder history for solar/battery (the two new
    CONF_SOLVER_SOLAR_POWER_SENSOR/CONF_SOLVER_BATTERY_POWER_SENSOR
    fields -- see their own comments in const.py) and load (reusing the
    EXISTING CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR field, which is
    already a real measured sensor whenever it's configured), plus the
    SAME flat price/battery-economics config values main() already
    reads for the forward-looking plan.

    Scores "yesterday" (the most recently fully-elapsed real calendar
    day), same reasoning as the reference script: a real settlement
    source (see below) only becomes trustworthy ground truth overnight,
    and even without one, "today so far" would be scored against a day
    that genuinely isn't over yet.

    Two real, honest simplifications versus the reference script, both
    disclosed here rather than silently assumed:
    - No commanded-vs-actual TRACKING signal exists generically -- every
      Nimbus writer is observation-only (nothing in this project ever
      actually dispatches a battery), so there is no generic "what was
      commanded" entity to compare against the real measured trajectory
      the way 116KAT's own household-specific automation layer can be.
      commanded is set equal to actual here, which makes
      tracking_fidelity=1.0/tracking_cost=0.0 by construction -- an
      honest reflection of "Nimbus itself never commands anything," not
      a claim that real-world execution is always perfect.
    - Without CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR configured,
      export is priced at the plain configured export rate only (no
      bonus/P2P revenue) for J_ach and J_star alike -- a real, honest
      EPR for any install with no such program, a less precise one for
      a household that has one but hasn't wired the field in.

    Returns a dict shaped like the reference script's own day_entry
    (epr, theoretical_maximum_yield, value_captured, uplift_available,
    j_ref, j_ach, j_star, regret_dollars, tracking_fidelity,
    tracking_cost, real_p2p_dollars, real_p2p_volume_kwh), or None if
    genuinely not configured (either power sensor missing) or real
    history for yesterday isn't available yet -- callers must treat
    None as "skip this cycle, retry later," never an error.
    """
    solar_sensor = cfg.get("solver_solar_power_sensor")
    battery_sensor = cfg.get("solver_battery_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    if not solar_sensor or not battery_sensor or not load_sensor:
        return None

    yesterday = (now - timedelta(days=1)).date()
    day_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=BRISBANE_TZ
    )
    day_end = day_start + timedelta(days=1)

    period_hours = 0.25
    n_periods = 96
    grid_times = [
        day_start + timedelta(hours=i * period_hours) for i in range(n_periods)
    ]
    period_hours_arr = np.full(n_periods, period_hours)

    solar_hist = fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = fetch_entity_history_range(load_sensor, day_start, day_end)
    battery_hist = fetch_entity_history_range(battery_sensor, day_start, day_end)
    if not solar_hist or not load_hist or not battery_hist:
        return None

    import_price_hist = fetch_entity_history_range(
        cfg["solver_import_price_sensor"], day_start, day_end
    )
    export_price_hist = fetch_entity_history_range(
        cfg["solver_export_price_sensor"], day_start, day_end
    )
    soc_sensor = cfg.get("solver_battery_soc_sensor")
    soc_hist = (
        fetch_entity_history_range(soc_sensor, day_start - timedelta(hours=6), day_end)
        if soc_sensor
        else []
    )

    solar_kw = np.array(
        [max(0.0, v) for v in resample_history_nearest(solar_hist, grid_times)]
    )
    load_kw = np.array(
        [max(0.0, v) for v in resample_history_nearest(load_hist, grid_times)]
    )
    actual_net_kw = np.array(resample_history_nearest(battery_hist, grid_times))
    actual_charge_kw = np.array([max(0.0, -v) for v in actual_net_kw])
    actual_discharge_kw = np.array([max(0.0, v) for v in actual_net_kw])
    # No generic commanded-dispatch signal exists -- see this function's
    # own docstring. Setting commanded = actual makes tracking_fidelity/
    # tracking_cost trivially perfect by construction, an honest
    # reflection of "nothing here ever actually dispatched," not a
    # claim about real-world execution quality.
    commanded_charge_kw = actual_charge_kw
    commanded_discharge_kw = actual_discharge_kw

    import_price = np.array(
        [
            v + import_fee_rate(cfg, grid_times[i].hour)
            for i, v in enumerate(
                resample_history_nearest(import_price_hist, grid_times, default=0.20)
            )
        ]
    )
    export_price = np.array(
        resample_history_nearest(export_price_hist, grid_times, default=0.05)
    )

    capacity_kwh = _cfg_num(cfg, "solver_battery_capacity_kwh", 0.0)
    min_pct = _cfg_num(cfg, "solver_battery_min_soc_percent", 5.0)
    max_pct = _cfg_num(cfg, "solver_battery_max_soc_percent", 100.0)
    initial_pct = (
        resample_history_nearest(soc_hist, [day_start], default=50.0)[0]
        if soc_hist
        else 50.0
    )
    final_pct = (
        resample_history_nearest(
            soc_hist, [day_end - timedelta(seconds=1)], default=initial_pct
        )[0]
        if soc_hist
        else initial_pct
    )
    initial_soc_kwh = capacity_kwh * initial_pct / 100.0
    final_soc_kwh_actual = capacity_kwh * final_pct / 100.0

    # Flat economics only -- deliberately NOT the household-specific
    # day/night discharge-cost/salvage-value schedule main() applies
    # for a LocalVolts-configured install (see that branch's own
    # comment, "tuned specifically around this household's own P2P
    # window, no portable equivalent yet"). This scorer always uses the
    # same flat config-flow values every OTHER install's forward plan
    # already falls back to.
    battery_cfg = elements.BatteryConfig(
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=capacity_kwh * min_pct / 100.0,
        max_soc_kwh=capacity_kwh * max_pct / 100.0,
        max_charge_kw=_cfg_num(cfg, "solver_max_charge_kw", 5.0),
        max_discharge_kw=_cfg_num(cfg, "solver_max_discharge_kw", 5.0),
        # solver_efficiency_percent is a single ROUND-TRIP figure, split
        # geometrically into per-direction charge/discharge efficiency
        # via sqrt() -- see main()'s own real BatteryConfig construction
        # for the canonical comment on why. Nimbus issue #168 (Mark
        # Purcell, 2026-08-25): this used to apply the round-trip value
        # directly to both directions instead of sqrt()-splitting it,
        # a real efficiency-convention mismatch against the live plan's
        # own oracle solve -- both branches must model the SAME battery
        # physics for EPR/regret to mean what it claims to mean.
        charge_efficiency=(_cfg_num(cfg, "solver_efficiency_percent", 90.0) / 100.0)
        ** 0.5,
        discharge_efficiency=(_cfg_num(cfg, "solver_efficiency_percent", 90.0) / 100.0)
        ** 0.5,
        charge_cost=_cfg_num(cfg, "solver_charge_cost", 0.01),
        discharge_cost=np.full(n_periods, _cfg_num(cfg, "solver_discharge_cost", 0.01)),
        salvage_value=_cfg_num(cfg, "solver_salvage_value", 0.15),
    )
    grid_residual = elements.GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=_cfg_num(cfg, "solver_grid_max_import_kw", 20.0),
        export_limit_kw=_cfg_num(cfg, "solver_grid_max_export_kw", 20.0),
    )

    # Optional real settlement hook -- see CONF_SOLVER_P2P_SETTLEMENT_
    # HISTORY_SENSOR's own comment in const.py. Retailer-agnostic in
    # SHAPE: any entity whose 'history' attribute holds real
    # {date: {export_cost, export_volume}} entries works.
    real_p2p_dollars = 0.0
    real_p2p_volume_kwh = 0.0
    grid_oracle = grid_residual
    settlement_sensor = cfg.get("solver_p2p_settlement_history_sensor")
    if settlement_sensor:
        try:
            day_data = (ha_get(settlement_sensor)["attributes"]["history"]).get(
                yesterday.isoformat()
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            KeyError,
            json.JSONDecodeError,
        ):
            day_data = None
        if day_data:
            real_p2p_dollars = float(day_data.get("export_cost", 0.0))
            real_p2p_volume_kwh = float(day_data.get("export_volume", 0.0))
            if real_p2p_volume_kwh > 0.01:
                # A flat bonus rate matching this project's own existing
                # bonus-mechanic convention (elements.GridConfig's own
                # export_bonus_price/export_bonus_volume_kwh, network.py's
                # two-tier bonus term) -- the real settled $/kWh this
                # specific day, not a forward-looking forecast rate.
                bonus_rate = real_p2p_dollars / real_p2p_volume_kwh
                grid_oracle = elements.GridConfig(
                    import_price=import_price,
                    export_price=export_price,
                    import_limit_kw=grid_residual.import_limit_kw,
                    export_limit_kw=grid_residual.export_limit_kw,
                    export_bonus_price=np.full(n_periods, bonus_rate),
                    export_bonus_volume_kwh=real_p2p_volume_kwh,
                )

    solar_cfg = elements.SolarConfig(forecast_kw=solar_kw)
    load_cfg = elements.LoadConfig(name="whole_house", forecast_kw=load_kw)
    periods = elements.PeriodGrid(hours=period_hours_arr, start=grid_times[0])

    try:
        report = compute_quality_report(
            periods=periods,
            grid_residual=grid_residual,
            grid_oracle=grid_oracle,
            battery=battery_cfg,
            solar=solar_cfg,
            load=load_cfg,
            timestamps=grid_times,
            real_p2p_dollars_earned=real_p2p_dollars,
            commanded_charge_kw=commanded_charge_kw,
            commanded_discharge_kw=commanded_discharge_kw,
            actual_charge_kw=actual_charge_kw,
            actual_discharge_kw=actual_discharge_kw,
            final_soc_kwh_actual=final_soc_kwh_actual,
        )
    except RuntimeError:
        # Oracle solve genuinely infeasible for this day's real data --
        # skip, same "retry next cycle" convention as every other
        # genuine failure mode here, never a crash.
        return None

    regret_dollars = report.j_ach - report.j_star
    return {
        "epr": round(report.epr.epr, 4),
        "theoretical_maximum_yield": round(report.epr.theoretical_maximum_yield, 4),
        "value_captured": round(report.epr.value_captured, 4),
        "uplift_available": round(report.epr.uplift_available, 4),
        "j_ref": round(report.j_ref, 4),
        "j_ach": round(report.j_ach, 4),
        "j_star": round(report.j_star, 4),
        "regret_dollars": round(regret_dollars, 4),
        "tracking_fidelity": round(report.tracking.tracking_fidelity, 4),
        "tracking_cost": round(report.tracking_cost, 4),
        "real_p2p_dollars": round(real_p2p_dollars, 4),
        "real_p2p_volume_kwh": round(real_p2p_volume_kwh, 3),
    }


def publish_daily_quality_report(cfg: dict, now: datetime) -> None:
    """Publishes sensor.nimbus_solver_quality_report -- the exact
    entity_id the devhub dashboard's own "Nimbus Solver Quality" card
    already reads (see nimbus-devhub's Forecaster view), so this needs
    zero dashboard changes to start working the instant it's deployed
    and configured.

    Cheap idempotency check FIRST, matching the reference script's own
    "already scored" fast path: reads back this sensor's own currently-
    published latest_date attribute (the same warm-start-from-own-prior-
    output technique main() already uses for previous_plan) before ever
    attempting the real oracle LP-solve -- so a solver cycle that runs
    every minute doesn't re-solve an already-scored day 1440 times.
    Unlike the reference script, there is no local on-disk history file
    to fall back to (native/devhub installs have no persistent local
    storage) -- if this sensor is ever wiped (e.g. by a restart, since a
    plain REST-pushed sensor has no persistent HA backing of its own),
    the day's score is recomputed once, cheaply, rather than lost.
    """
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    try:
        existing = ha_get(QUALITY_ENTITY_ID)
        if existing.get("attributes", {}).get("latest_date") == yesterday_key:
            return
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        pass  # never seen before, or transiently unreachable -- fall through and try to compute
    day_entry = compute_daily_quality_report(cfg, now)
    if day_entry is None:
        return
    ha_post_state(
        QUALITY_ENTITY_ID,
        day_entry["epr"],
        {
            "unit_of_measurement": None,
            "friendly_name": "Nimbus Solver Quality Report (EPR)",
            "latest_date": yesterday_key,
            "generated_at": now.isoformat(),
            **day_entry,
        },
    )


BACKTEST_ENTITY_ID = "sensor.nimbus_efficiency_backtest"


def compute_efficiency_backtest_report(cfg: dict, now: datetime) -> dict | None:
    """The retrospective backtesting engine's first real check (2026-08-25,
    direct household ask for a genuine "outstanding, unique" idea -- an
    offline engine that proves Nimbus's own decisions against reality
    rather than a bigger LP or a fancier model): "if your real round-trip
    efficiency were actually different, would yesterday's real day have
    scored meaningfully differently?"

    See solver/backtest.py's own module docstring for the full, honest
    "what this can and cannot test" reasoning -- efficiency is the FIRST
    candidate because it directly changes the LP's own economic tradeoff
    even under perfect knowledge of what actually happened; risk_aversion
    is deliberately NOT here (it would silently produce identical scores
    for every candidate -- see that module's own docstring for why).

    Reconstructs the SAME real "yesterday" (solar/load/battery/price
    history, BatteryConfig/GridConfig) compute_daily_quality_report()
    already builds -- deliberately a separate, self-contained
    reconstruction rather than a shared refactor, so this new, more
    speculative feature can never risk regressing the already-shipped,
    already-relied-on EPR/regret report by sharing code paths with it.

    Returns None (skip this cycle, retry later) under the exact same
    conditions compute_daily_quality_report() does: required sensors not
    configured, or real history for yesterday not yet available.
    """
    solar_sensor = cfg.get("solver_solar_power_sensor")
    battery_sensor = cfg.get("solver_battery_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    if not solar_sensor or not battery_sensor or not load_sensor:
        return None

    yesterday = (now - timedelta(days=1)).date()
    day_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=BRISBANE_TZ
    )
    day_end = day_start + timedelta(days=1)

    period_hours = 0.25
    n_periods = 96
    grid_times = [
        day_start + timedelta(hours=i * period_hours) for i in range(n_periods)
    ]
    period_hours_arr = np.full(n_periods, period_hours)

    solar_hist = fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = fetch_entity_history_range(load_sensor, day_start, day_end)
    if not solar_hist or not load_hist:
        return None

    import_price_hist = fetch_entity_history_range(
        cfg["solver_import_price_sensor"], day_start, day_end
    )
    export_price_hist = fetch_entity_history_range(
        cfg["solver_export_price_sensor"], day_start, day_end
    )

    solar_kw = np.array(
        [max(0.0, v) for v in resample_history_nearest(solar_hist, grid_times)]
    )
    load_kw = np.array(
        [max(0.0, v) for v in resample_history_nearest(load_hist, grid_times)]
    )
    import_price = np.array(
        [
            v + import_fee_rate(cfg, grid_times[i].hour)
            for i, v in enumerate(
                resample_history_nearest(import_price_hist, grid_times, default=0.20)
            )
        ]
    )
    export_price = np.array(
        resample_history_nearest(export_price_hist, grid_times, default=0.05)
    )

    capacity_kwh = _cfg_num(cfg, "solver_battery_capacity_kwh", 0.0)
    min_pct = _cfg_num(cfg, "solver_battery_min_soc_percent", 5.0)
    max_pct = _cfg_num(cfg, "solver_battery_max_soc_percent", 100.0)
    # Initial/final SoC don't need real history here the way the EPR
    # report's own tracking comparison does -- the oracle re-solve is
    # free to choose its own trajectory from a reasonable starting point
    # regardless, and this feature's whole question is "how did the
    # SHAPE of the optimal plan change with efficiency," not a tracking
    # comparison against one specific real starting SoC.
    initial_soc_kwh = capacity_kwh * 0.5

    base_battery = elements.BatteryConfig(
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=capacity_kwh * min_pct / 100.0,
        max_soc_kwh=capacity_kwh * max_pct / 100.0,
        max_charge_kw=_cfg_num(cfg, "solver_max_charge_kw", 5.0),
        max_discharge_kw=_cfg_num(cfg, "solver_max_discharge_kw", 5.0),
        # Overwritten per-candidate by run_efficiency_sensitivity_sweep()
        # -- these two values are never actually read, kept only because
        # BatteryConfig requires something valid at construction time.
        charge_efficiency=0.90,
        discharge_efficiency=0.90,
        charge_cost=_cfg_num(cfg, "solver_charge_cost", 0.01),
        discharge_cost=np.full(n_periods, _cfg_num(cfg, "solver_discharge_cost", 0.01)),
        salvage_value=_cfg_num(cfg, "solver_salvage_value", 0.15),
    )
    grid_cfg = elements.GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=_cfg_num(cfg, "solver_grid_max_import_kw", 20.0),
        export_limit_kw=_cfg_num(cfg, "solver_grid_max_export_kw", 20.0),
    )
    solar_cfg = elements.SolarConfig(forecast_kw=solar_kw)
    load_cfg = elements.LoadConfig(name="whole_house", forecast_kw=load_kw)
    periods = elements.PeriodGrid(hours=period_hours_arr, start=grid_times[0])

    results = run_efficiency_sensitivity_sweep(
        periods=periods,
        grid=grid_cfg,
        base_battery=base_battery,
        solar=solar_cfg,
        load=load_cfg,
    )
    if not results:
        # Every candidate was genuinely infeasible for this real day --
        # a real, if unusual, outcome (see run_efficiency_sensitivity_
        # sweep()'s own per-candidate defensive skip) -- report nothing
        # rather than a misleadingly empty-but-successful entry.
        return None

    configured_pct = _cfg_num(cfg, "solver_efficiency_percent", 90.0)
    best = min(results, key=lambda r: r.total_cost)
    worst = max(results, key=lambda r: r.total_cost)
    return {
        "candidates": [
            {"efficiency_percent": r.label, "total_cost": round(r.total_cost, 4)}
            for r in results
        ],
        "configured_efficiency_percent": round(configured_pct, 1),
        "best_candidate": best.label,
        "best_candidate_cost": round(best.total_cost, 4),
        "worst_candidate": worst.label,
        "worst_candidate_cost": round(worst.total_cost, 4),
        # How much cheaper the BEST tested efficiency would have scored
        # vs the WORST, on this one real day -- a direct, human-readable
        # "does efficiency actually matter here" answer. Always >= 0 by
        # construction (best <= worst).
        "spread_dollars": round(worst.total_cost - best.total_cost, 4),
    }


def publish_efficiency_backtest_report(cfg: dict, now: datetime) -> None:
    """Publishes sensor.nimbus_efficiency_backtest. Same cheap
    idempotency-first pattern as publish_daily_quality_report() -- see
    that function's own docstring for why (a solver cycle running every
    minute must not re-solve an already-scored day's whole sweep 1440
    times).
    """
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    try:
        existing = ha_get(BACKTEST_ENTITY_ID)
        if existing.get("attributes", {}).get("latest_date") == yesterday_key:
            return
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        pass
    report = compute_efficiency_backtest_report(cfg, now)
    if report is None:
        return
    ha_post_state(
        BACKTEST_ENTITY_ID,
        report["spread_dollars"],
        {
            "unit_of_measurement": "$",
            "friendly_name": "Nimbus Efficiency Backtest",
            "latest_date": yesterday_key,
            "generated_at": now.isoformat(),
            **report,
        },
    )


COUNTERFACTUAL_ENTITY_ID = "sensor.nimbus_counterfactual_soc"


def compute_nimbus_only_soc_counterfactual(cfg: dict, day: datetime) -> dict | None:
    """Generic, wizard-config-driven port of the reference household's own
    NUC1 script (docs/real-world-integration/files/nimbus_counterfactual_
    writer.py, "Stage 1 of the household's own staged path toward
    eventually letting Nimbus drive real dispatch") -- direct ask
    (2026-08-25): "nuc one nimbus solver view has counterfactual
    table.... i want u to build that into devbox package."

    Answers a different question than compute_daily_quality_report()'s
    EPR/regret score: not "was the plan economically right," but "if
    Nimbus's OWN reasoning had been driving the battery all day --
    starting from the SAME real midnight SoC, but from that instant
    onward using ONLY its own simulated trajectory as the next tick's
    starting point, never the real (possibly HAEO- or other-automation-
    influenced) SoC -- would the battery have stayed in a sane state?"
    Mechanism: a real receding-horizon replay, re-solving the REST of
    the real calendar day from scratch every 15 minutes (same
    network.build_plan() the live writer uses), committing only each
    tick's own first-period dispatch and feeding the resulting simulated
    SoC into the next tick -- exactly rolling.py's own real production
    pattern, just walked across an already-elapsed day's real recorder
    history instead of a live forecast.

    Explicit correction applied here, direct household instruction
    (2026-08-25): "nimbus is written for localvolts and people without
    localvolts... so p2p is a feature but also something people can
    ignore.. needs to be wrapped that way." Unlike the reference script
    (which hardcodes this ONE household's own P2P target/window/viability
    threshold as module constants), every P2P-related input here is the
    SAME optional, wizard-configured field the live writer already reads
    (solver_p2p_block_*/solver_p2p_bonus_price/solver_p2p_bonus_volume_
    kwh) -- a household with none of them set gets a complete no-op
    (export left fully LP-optimized against real spot prices, no
    checkpoint/viability verdict computed, exactly as if this concept
    didn't exist), never a crash or a household-specific default leaking
    through. Also, deliberately, ALWAYS uses the flat/generic economics
    (solver_discharge_cost, solver_salvage_value) rather than the
    LocalVolts-specific day/night schedule main() applies for a
    has_localvolts install -- see that branch's own comment for why that
    schedule has no portable equivalent yet.

    Returns None if the required generic sensors aren't configured
    (solar/whole-house-load/battery-SoC power sensors) or real recorder
    history for the day is empty -- callers must treat None as "skip,
    retry later," never an error.
    """
    solar_sensor = cfg.get("solver_solar_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    soc_sensor = cfg.get("solver_battery_soc_sensor")
    if not solar_sensor or not load_sensor or not soc_sensor:
        return None

    capacity_kwh = _cfg_num(cfg, "solver_battery_capacity_kwh", 0.0)
    if capacity_kwh <= 0:
        return None

    day_start = datetime(day.year, day.month, day.day, tzinfo=BRISBANE_TZ)
    day_end = day_start + timedelta(days=1)
    step = timedelta(minutes=15)

    solar_hist = fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = fetch_entity_history_range(load_sensor, day_start, day_end)
    soc_hist = fetch_entity_history_range(
        soc_sensor, day_start - timedelta(hours=6), day_end
    )
    if not solar_hist or not load_hist or not soc_hist:
        return None

    import_price_hist = fetch_entity_history_range(
        cfg["solver_import_price_sensor"], day_start, day_end
    )
    export_price_hist = fetch_entity_history_range(
        cfg["solver_export_price_sensor"], day_start, day_end
    )

    min_pct = _cfg_num(cfg, "solver_battery_min_soc_percent", 5.0)
    max_pct = _cfg_num(cfg, "solver_battery_max_soc_percent", 100.0)
    max_soc_kwh = capacity_kwh * max_pct / 100.0
    min_soc_kwh = resolve_min_soc_kwh(min_pct, capacity_kwh, max_soc_kwh)
    max_charge_kw = _cfg_num(cfg, "solver_max_charge_kw", 5.0)
    max_discharge_kw = resolve_max_discharge_kw(cfg)
    # solver_efficiency_percent is a single ROUND-TRIP figure, split
    # geometrically via sqrt() into the per-direction value the LP (and
    # this replay's own post-solve SoC bookkeeping) actually needs --
    # same convention as main()'s own real BatteryConfig and issue #168's
    # own fix in compute_daily_quality_report(). Using the round-trip
    # value directly here would model a battery physically different
    # from the one the real live plan solves against.
    efficiency = (_cfg_num(cfg, "solver_efficiency_percent", 90.0) / 100.0) ** 0.5
    charge_cost = _cfg_num(cfg, "solver_charge_cost", 0.01)
    discharge_cost_flat = _cfg_num(cfg, "solver_discharge_cost", 0.01)
    salvage_value_flat = _cfg_num(cfg, "solver_salvage_value", 0.15)
    import_limit_kw = _cfg_num(cfg, "solver_grid_max_import_kw", 20.0)
    export_limit_kw = _cfg_num(cfg, "solver_grid_max_export_kw", 20.0)
    flat_fee_rate = _cfg_num(cfg, "solver_flat_fee_rate", 0.0)

    # Fully optional -- see this function's own docstring. A household
    # with no P2P/community-trading scheme configured gets
    # p2p_bonus_volume_cap=0.0 (bonus price is then a genuine no-op) and
    # checkpoint_hour=-1 (no window-open moment to check), while export
    # still gets fully LP-optimized against real spot prices exactly as
    # the live writer's own generic fallback path already does.
    p2p_bonus_price_flat = _cfg_num(cfg, "solver_p2p_bonus_price", 0.0)
    p2p_bonus_volume_cap = _cfg_num(cfg, "solver_p2p_bonus_volume_kwh", 0.0)
    p2p_block_1_rate = _cfg_num(cfg, "solver_p2p_block_1_rate_kw", 0.0)
    checkpoint_hour = (
        _cfg_int(cfg, "solver_p2p_block_1_start_hour", -1)
        if p2p_block_1_rate > 0
        else -1
    )
    viable_threshold_pct = (
        min(100.0, (p2p_bonus_volume_cap / capacity_kwh * 100.0) * 1.1)
        if p2p_bonus_volume_cap > 0
        else None
    )

    initial_pct = resample_history_nearest(soc_hist, [day_start], default=50.0)[0]
    real_soc_close_pct = resample_history_nearest(
        soc_hist, [day_end - timedelta(seconds=1)], default=initial_pct
    )[0]
    real_soc_checkpoint_pct = (
        resample_history_nearest(
            soc_hist, [day_start.replace(hour=checkpoint_hour)], default=initial_pct
        )[0]
        if checkpoint_hour >= 0
        else None
    )

    sim_soc_kwh = min(max(capacity_kwh * initial_pct / 100.0, min_soc_kwh), max_soc_kwh)
    bonus_used_kwh_today = 0.0
    sim_soc_checkpoint_pct: float | None = None

    t = day_start
    while t < day_end:
        grid_times = []
        tt = t
        while tt < day_end:
            grid_times.append(tt)
            tt += step
        n = len(grid_times)
        hours_arr = np.full(n, step.total_seconds() / 3600.0)

        solar_kw = np.array(
            [max(0.0, v) for v in resample_history_nearest(solar_hist, grid_times)]
        )
        load_kw = np.array(
            [max(0.1, v) for v in resample_history_nearest(load_hist, grid_times)]
        )
        import_price = np.array(
            [
                v + import_fee_rate(cfg, gt.hour) + flat_fee_rate
                for v, gt in zip(
                    resample_history_nearest(
                        import_price_hist, grid_times, default=0.20
                    ),
                    grid_times,
                )
            ]
        )
        export_price = np.array(
            resample_history_nearest(export_price_hist, grid_times, default=0.05)
        )

        fixed_export_kw = fetch_p2p_fixed_export_kw(cfg, grid_times)
        remaining_bonus_kwh = max(0.0, p2p_bonus_volume_cap - bonus_used_kwh_today)

        # Same universal concave terminal-value mechanism main() always
        # applies (Solver PR #35, portable) -- zeroed once a tick starts
        # inside the configured P2P window, same reasoning as the
        # reference script's own fix (a real automation blindly
        # following a fixed export rate has zero regard for what happens
        # after it closes; a nonzero terminal reward there just biases
        # the LP to import/charge purely to bank it). A no-op for any
        # household with no P2P window configured -- t.hour is never
        # "inside" a window that doesn't exist.
        in_p2p_window = checkpoint_hour >= 0 and t.hour >= checkpoint_hour
        salvage_value = 0.0 if in_p2p_window else salvage_value_flat

        periods = elements.PeriodGrid(hours=hours_arr, start=t)
        grid = elements.GridConfig(
            import_price=import_price,
            export_price=export_price,
            import_limit_kw=import_limit_kw,
            export_limit_kw=export_limit_kw,
            export_bonus_price=np.full(n, p2p_bonus_price_flat),
            export_bonus_volume_kwh=remaining_bonus_kwh,
            fixed_export_kw=np.array(fixed_export_kw)
            if fixed_export_kw is not None
            else None,
        )
        battery = elements.BatteryConfig(
            capacity_kwh=capacity_kwh,
            initial_soc_kwh=min(max(sim_soc_kwh, min_soc_kwh), max_soc_kwh),
            min_soc_kwh=min_soc_kwh,
            max_soc_kwh=max_soc_kwh,
            max_charge_kw=max_charge_kw,
            max_discharge_kw=max_discharge_kw,
            charge_efficiency=efficiency,
            discharge_efficiency=efficiency,
            charge_cost=charge_cost,
            discharge_cost=np.full(n, discharge_cost_flat),
            salvage_value=salvage_value,
            terminal_value_breakpoints=terminal_value_breakpoints_for(
                salvage_value, min_soc_kwh, max_soc_kwh
            )
            if salvage_value > 0
            else None,
        )
        solar = elements.SolarConfig(forecast_kw=solar_kw)
        loads = [elements.LoadConfig(name="whole_house", forecast_kw=load_kw)]

        try:
            plan = network.build_plan(
                periods=periods,
                grid=grid,
                battery=battery,
                solar=solar,
                loads=loads,
                smoothness_weight=network.DEFAULT_SMOOTHNESS_WEIGHT_KW,
            )
        except Exception:  # noqa: BLE001 -- one bad/infeasible tick must not sink the whole day's replay; hold SoC unchanged and keep going, same convention as the reference script
            plan = None

        if plan is not None and plan.status == "optimal":
            net0 = float(plan.battery_discharge_kw[0] - plan.battery_charge_kw[0])
            if net0 >= 0:
                sim_soc_kwh -= net0 * (step.total_seconds() / 3600.0) / efficiency
            else:
                sim_soc_kwh += (-net0) * efficiency * (step.total_seconds() / 3600.0)
            sim_soc_kwh = min(max(sim_soc_kwh, min_soc_kwh), max_soc_kwh)
            if plan.export_bonus_kw is not None:
                bonus_used_kwh_today += float(plan.export_bonus_kw[0]) * (
                    step.total_seconds() / 3600.0
                )

        if (
            checkpoint_hour >= 0
            and t.hour == checkpoint_hour
            and t.minute < step.total_seconds() / 60.0
            and sim_soc_checkpoint_pct is None
        ):
            sim_soc_checkpoint_pct = sim_soc_kwh / capacity_kwh * 100.0
        t += step

    sim_soc_close_pct = sim_soc_kwh / capacity_kwh * 100.0
    viable = (
        sim_soc_checkpoint_pct is not None
        and viable_threshold_pct is not None
        and sim_soc_checkpoint_pct >= viable_threshold_pct
    )

    return {
        "date": day_start.date().isoformat(),
        "real_soc_anchor_pct": round(initial_pct, 1),
        "nimbus_only_soc_checkpoint_pct": round(sim_soc_checkpoint_pct, 1)
        if sim_soc_checkpoint_pct is not None
        else None,
        "real_soc_checkpoint_pct": round(real_soc_checkpoint_pct, 1)
        if real_soc_checkpoint_pct is not None
        else None,
        "checkpoint_hour": checkpoint_hour if checkpoint_hour >= 0 else None,
        "nimbus_only_soc_close_pct": round(sim_soc_close_pct, 1),
        "real_soc_close_pct": round(real_soc_close_pct, 1),
        "viable": viable,
        "viable_threshold_pct": round(viable_threshold_pct, 1)
        if viable_threshold_pct is not None
        else None,
        "p2p_configured": checkpoint_hour >= 0,
    }


def publish_nimbus_only_soc_counterfactual(cfg: dict, now: datetime) -> None:
    """Publishes sensor.nimbus_counterfactual_soc -- the generic,
    built-in equivalent of the reference household's own NUC1
    "Nimbus-only Counterfactual SoC" card (see
    compute_nimbus_only_soc_counterfactual()'s own docstring). Same
    idempotency-check-first pattern as publish_daily_quality_report():
    reads back this sensor's own currently-published latest_date before
    ever attempting a real day-long rolling replay (96 LP solves), so a
    solver cycle that runs every minute doesn't redo that work 1440
    times for an already-scored day.
    """
    yesterday = (now - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yesterday_key = yesterday.date().isoformat()
    try:
        existing = ha_get(COUNTERFACTUAL_ENTITY_ID)
        if existing.get("attributes", {}).get("latest_date") == yesterday_key:
            return
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        pass  # never seen before, or transiently unreachable -- fall through and try to compute
    day_entry = compute_nimbus_only_soc_counterfactual(cfg, yesterday)
    if day_entry is None:
        return
    ha_post_state(
        COUNTERFACTUAL_ENTITY_ID,
        day_entry["nimbus_only_soc_close_pct"],
        {
            "unit_of_measurement": "%",
            "friendly_name": "Nimbus-only Counterfactual SoC",
            "latest_date": yesterday_key,
            "generated_at": now.isoformat(),
            **day_entry,
        },
    )


# Nimbus issue #128 (Mark Purcell, 2026-08-25): "switch.solar_curtailment
# doesn't detect implicit inverter AC-clipping" (#114's own curtailment
# switch only sees EXPLICIT curtailment control -- an inverter silently
# capping AC output because real DC generation exceeds its own AC rating
# is invisible to it). His own proposed fix, agreed to and built here
# exactly as specced: "solar_delivery_ratio = rolling_avg(actual_solar_kw
# / forecast_solar_kw) over the last N hours where forecast > 5 kW."
#
# Genuinely optional and fully generic: reuses the SAME
# solver_solar_power_sensor wizard field already added for the EPR
# quality report (#162) -- no new config surface needed. A household
# with it unset gets a complete no-op, matching every other optional
# diagnostic in this file.
#
# Env-var-overridable /opt-default persisted state, same convention as
# PLAN_STATE_PATH/LOCK_PATH/LOAD_FORECAST_ERROR_NOTIFIED_PATH above --
# solver_runtime.py's own set_default_env_vars() points this at HA's
# real storage directory for the native in-process path.
SOLAR_DELIVERY_RATIO_PATH = os.environ.get(
    "NIMBUS_SOLVER_SOLAR_DELIVERY_RATIO_PATH",
    "/opt/nimbus_solver_solar_delivery_ratio.json",
)
# Mark's own spec, verbatim -- avoids near-zero-solar noise (a forecast
# of 0.3kW vs an actual of 0.1kW is a real 3x "miss" that means nothing
# at dawn/dusk).
SOLAR_DELIVERY_MIN_FORECAST_KW = 5.0
# How far ahead each solve's own forecast[] value is queued for later
# resolution against reality -- long enough that solar genuinely could
# have moved (cloud cover, curtailment), short enough that the forecast
# being tested is still today's, not a much-later horizon point.
SOLAR_DELIVERY_LOOKAHEAD_MINUTES = 60
SOLAR_DELIVERY_ROLLING_WINDOW_HOURS = 6.0
# Mark's own suggested reading: "if this ratio persistently sits below
# (e.g.) 0.80 during high-PV hours, that's implicit AC-side clipping."
SOLAR_DELIVERY_UNDERPERFORMING_THRESHOLD = 0.80


def _load_solar_delivery_state() -> dict:
    try:
        with open(SOLAR_DELIVERY_RATIO_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "pending" in data and "ratios" in data:
            return data
    except (OSError, ValueError):
        pass
    return {"pending": [], "ratios": []}


def _save_solar_delivery_state(state: dict) -> None:
    try:
        with open(SOLAR_DELIVERY_RATIO_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def update_solar_delivery_ratio(
    cfg: dict,
    now: datetime,
    grid_times: list[datetime],
    solar_kw: list[float],
) -> dict | None:
    """Real forecast-vs-actual solar tracking (nimbus issue #128): each
    call queues one NEW prediction (~SOLAR_DELIVERY_LOOKAHEAD_MINUTES
    ahead, from THIS solve's own forecast -- the exact value the LP is
    using right now, before any live-measurement override touches only
    index 0), and resolves any previously-queued predictions whose
    target time has now arrived by fetching the real measured reading at
    that moment. Same "record a prediction now, grade it once reality
    catches up" shape already proven in coordinator.py's own
    `_last_step_prediction` residual tracking -- this is the Solver's
    own equivalent, since solver_writer.py has no persistent Python
    object across solves to hold that state in memory (a fresh process
    on the standalone-script path; a stateless helper function on the
    native path) -- hence the small on-disk JSON buffer, same pattern as
    PLAN_STATE_PATH.

    Returns None (a complete no-op) if solver_solar_power_sensor isn't
    configured. Never raises -- every failure mode (missing history,
    corrupt state file, disk write failure) degrades to "this cycle
    contributes nothing new," never crashes the real solve.
    """
    solar_sensor = cfg.get("solver_solar_power_sensor")
    if not solar_sensor:
        return None

    state = _load_solar_delivery_state()
    pending = state.get("pending", [])
    ratios = state.get("ratios", [])

    still_pending = []
    for entry in pending:
        try:
            target_time = datetime.fromisoformat(entry["target_time"])
            forecast_kw = float(entry["forecast_kw"])
        except (KeyError, TypeError, ValueError):
            continue
        if target_time > now:
            still_pending.append(entry)
            continue
        if forecast_kw >= SOLAR_DELIVERY_MIN_FORECAST_KW:
            hist = fetch_entity_history_range(
                solar_sensor,
                target_time - timedelta(minutes=10),
                target_time + timedelta(minutes=10),
            )
            if hist:
                actual_kw = resample_history_nearest(hist, [target_time])[0]
                ratios.append(
                    {"time": now.isoformat(), "ratio": actual_kw / forecast_kw}
                )
        # Resolved (or genuinely unresolvable -- no history, or the
        # forecast was too small to be meaningful) either way -- never
        # re-queued.

    cutoff = now - timedelta(hours=SOLAR_DELIVERY_ROLLING_WINDOW_HOURS)
    ratios = [
        r
        for r in ratios
        if "time" in r
        and _safe_fromisoformat(r["time"]) is not None
        and _safe_fromisoformat(r["time"]) >= cutoff
    ]

    if grid_times:
        target = now + timedelta(minutes=SOLAR_DELIVERY_LOOKAHEAD_MINUTES)
        closest_idx = min(
            range(len(grid_times)),
            key=lambda i: abs((grid_times[i] - target).total_seconds()),
        )
        still_pending.append(
            {
                "target_time": grid_times[closest_idx].isoformat(),
                "forecast_kw": float(solar_kw[closest_idx]),
            }
        )

    _save_solar_delivery_state({"pending": still_pending, "ratios": ratios})

    if not ratios:
        return {
            "solar_delivery_ratio": None,
            "solar_delivery_sample_count": 0,
            "solar_delivery_underperforming": False,
        }
    avg_ratio = sum(r["ratio"] for r in ratios) / len(ratios)
    return {
        "solar_delivery_ratio": round(avg_ratio, 3),
        "solar_delivery_sample_count": len(ratios),
        "solar_delivery_underperforming": avg_ratio
        < SOLAR_DELIVERY_UNDERPERFORMING_THRESHOLD,
    }


def _safe_fromisoformat(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def main() -> None:
    # Fail fast, with a real, actionable message, if the Solver hasn't
    # been configured yet -- see fetch_solver_config()'s own docstring
    # for the full "installable by anyone" context this closes.
    cfg = fetch_solver_config()

    now = datetime.now(UTC).astimezone(BRISBANE_TZ).replace(second=0, microsecond=0)
    grid_times, period_hours_arr = build_tiered_grid(now)
    n_periods = len(grid_times)

    # Solar forecast sources -- REAL raw sensors, read and blended
    # directly in Python, no intermediate HA template sensor (2026-08-22,
    # direct household ask: "why not input proper raw sensors they
    # offer... i would much rather input proper raw sensors they offer
    # and blend in solver" -- the earlier "combined" adapter template
    # sensor approach genuinely worked but added an extra layer of
    # indirection between the real integration and the solve; this
    # reads each real integration's own native entities straight from
    # the API and reshapes them here instead).
    #
    # Real household finding driving this, checked live: with only 2
    # sources, BOTH were overpredicting real measured solar by 36-57%
    # at the SAME moment [real 4.225kW vs source 1 6.639kW vs source 2
    # 5.73kW] -- proof two sources sharing the same directional bias
    # don't cancel out when averaged, they just average the bias. A
    # third, genuinely differently-modeled source (Solcast --
    # satellite-imagery-anchored, architecturally distinct from both a
    # self-trained ML model and a different NWP provider) gives the
    # blend a real chance at partially-uncorrelated error.
    #
    # Source 1 (CONF_SOLVER_SOLAR_FORECAST_SENSOR) stays a generic,
    # config-flow-pointed entity -- this is what keeps the Solver
    # genuinely installable by anyone, not just this household (any
    # source with a standard forecast:[{time,value,lower,upper}] shape
    # works, including Nimbus's own self-trained model). Open-Meteo and
    # Solcast are auto-detected directly by their own well-known real
    # entity names (entity_exists() gated, a complete no-op on any
    # install without them) -- no separate config field needed for
    # these two specific, already-known integrations.
    #
    # EACH source is fetched independently and safely -- unlike the 18
    # load forecasts (fetch_load_forecast_safe(), below, where
    # "unavailable" safely defaults to 0.0 kW, a genuinely plausible
    # real value for an idle circuit), a failed SOLAR source must NEVER
    # be treated as 0kW: solar is essentially never legitimately zero
    # during daylight hours, and silently blending in a false zero
    # would drag the whole average WAY down -- actively corrupting an
    # otherwise-healthy blend rather than degrading gracefully. On
    # failure, that source is DROPPED from the average entirely; the
    # blend runs across however many sources actually returned real
    # data this cycle, never a phantom zero standing in for a missing
    # one. (Direct household correction, 2026-08-22: "make sure if one
    # fails it returns 0 -- being wrapped?" -- the WRAPPING/never-crash
    # instinct is right and applied here; the specific fallback VALUE
    # had to be "drop", not "0", since 0kW solar mid-morning is never a
    # safe assumption the way 0kW on one idle circuit out of 18
    # genuinely can be.)
    #
    # EQUAL weight across whichever sources succeed, deliberately --
    # ml/blend.py's own weights_from_mae() already supports real
    # accuracy-derived weighting, but there is no matured per-source
    # accuracy data yet (Solver audit item #9's own capture-and-compare
    # mechanism has only a handful of real snapshots so far). Averaging
    # a known-bad source against a known-good one would make things
    # WORSE -- but with genuinely UNKNOWN relative accuracy (the honest
    # state right now), equal weight is the correct, defensible
    # default, not a shortcut. Same, uniform treatment for EVERY
    # period, including "now" -- no special-cased override anywhere.
    def fetch_solar_source_safe(
        entity_id: str,
    ) -> tuple[list[float], list[float], list[float]] | None:
        """(value, lower, upper) kW arrays for ONE solar source that
        already publishes a standard forecast:[{time,value,lower,upper}]
        array, or None on any failure -- see this section's own comment
        above for why a missing source is DROPPED, never zero-filled."""
        try:
            fc = ha_get(entity_id)["attributes"]["forecast"]
            # Real, honest clamp: a ML forecaster can produce a tiny
            # negative excursion near zero (physically impossible for
            # solar) -- found live on this script's very first real run.
            value = [max(0.0, v) for v in resample_forecast(fc, "value", grid_times)]
            has_bounds = any(p.get("lower") is not None for p in fc)
            if has_bounds:
                lower = [
                    max(0.0, v) for v in resample_forecast(fc, "lower", grid_times)
                ]
                upper = [
                    max(0.0, v) for v in resample_forecast(fc, "upper", grid_times)
                ]
                lower = [min(lower[i], value[i]) for i in range(n_periods)]
                upper = [max(upper[i], value[i]) for i in range(n_periods)]
            else:
                lower = list(value)
                upper = list(value)
            return value, lower, upper
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            KeyError,
            json.JSONDecodeError,
        ) as e:
            print(
                f"WARN: solar source {entity_id} unavailable ({e}) -- dropped from this solve's blend",
                file=sys.stderr,
            )
            return None

    def fetch_open_meteo_solar_raw() -> (
        tuple[list[float], list[float], list[float]] | None
    ):
        """Real, DIRECT read of Open-Meteo Solar Forecast's own 8 native
        entities (today/tomorrow/d2..d7) -- reshaped from their native
        {timestamp: watts} dict shape (15-min resolution, Watts) into
        the standard {time, value} shape right here, no intermediate HA
        template sensor. Auto-detected via entity_exists() on the
        anchor entity -- a complete no-op, not an error, on any install
        without Open-Meteo Solar Forecast. No real per-point uncertainty
        data exists from this source -- lower/upper mirror value (a
        zero-width band), same honest default as every other
        no-uncertainty source."""
        anchor = "sensor.home_energy_production_today"
        if not entity_exists(anchor):
            return None
        entity_ids = [
            "sensor.home_energy_production_today",
            "sensor.home_energy_production_tomorrow",
            "sensor.home_energy_production_d2",
            "sensor.home_energy_production_d3",
            "sensor.home_energy_production_d4",
            "sensor.home_energy_production_d5",
            "sensor.home_energy_production_d6",
            "sensor.home_energy_production_d7",
        ]
        entries: list[dict] = []
        for eid in entity_ids:
            if not entity_exists(eid):
                continue
            watts = ha_get(eid)["attributes"].get("watts")
            if not watts:
                continue
            for ts, w in watts.items():
                entries.append({"time": ts, "value": float(w) / 1000.0})
        if not entries:
            return None
        entries.sort(key=lambda e: e["time"])
        value = [max(0.0, v) for v in resample_forecast(entries, "value", grid_times)]
        return value, list(value), list(value)

    def fetch_solcast_solar_raw() -> (
        tuple[list[float], list[float], list[float]] | None
    ):
        """Real, DIRECT read of Solcast's own 2 native entities
        (today/tomorrow) -- reshaped from their native detailedForecast
        list shape (30-min resolution, period_start/pv_estimate/
        pv_estimate10/pv_estimate90) right here, no intermediate HA
        template sensor. Auto-detected, a complete no-op on any install
        without Solcast. Carries Solcast's own REAL p10/p90 as genuine
        lower/upper confidence bounds -- a real bonus over Open-Meteo,
        which has none. pv_estimate is already kW average power for its
        30-min period, NOT the parent entity's own "kWh" unit tag
        (confirmed live, 2026-08-22: a real midday pv_estimate landed
        squarely between real measured solar and Open-Meteo's own kW
        value, not double that -- no unit conversion applied here."""
        anchor = "sensor.solcast_pv_forecast_forecast_today"
        if not entity_exists(anchor):
            return None
        entity_ids = [
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.solcast_pv_forecast_forecast_tomorrow",
        ]
        entries: list[dict] = []
        for eid in entity_ids:
            if not entity_exists(eid):
                continue
            detailed = ha_get(eid)["attributes"].get("detailedForecast")
            if not detailed:
                continue
            for p in detailed:
                entries.append(
                    {
                        "time": p["period_start"],
                        "value": float(p.get("pv_estimate", 0.0) or 0.0),
                        "lower": float(p.get("pv_estimate10", 0.0) or 0.0),
                        "upper": float(p.get("pv_estimate90", 0.0) or 0.0),
                    }
                )
        if not entries:
            return None
        entries.sort(key=lambda e: e["time"])
        value = [max(0.0, v) for v in resample_forecast(entries, "value", grid_times)]
        lower = [max(0.0, v) for v in resample_forecast(entries, "lower", grid_times)]
        upper = [max(0.0, v) for v in resample_forecast(entries, "upper", grid_times)]
        lower = [min(lower[i], value[i]) for i in range(n_periods)]
        upper = [max(upper[i], value[i]) for i in range(n_periods)]
        return value, lower, upper

    solar_values, solar_lowers, solar_uppers = [], [], []

    # Source 1: whatever's configured via the Solver settings wizard
    # (this household: Nimbus's own self-trained model).
    configured_entity = cfg.get("solver_solar_forecast_sensor")
    if configured_entity:
        result = fetch_solar_source_safe(configured_entity)
        if result is not None:
            v, lo, up = result
            solar_values.append(np.array(v))
            solar_lowers.append(np.array(lo))
            solar_uppers.append(np.array(up))

    # Known real integrations (Open-Meteo, Solcast) -- ONLY included if
    # switch.nimbus_solver_auto_include_known_solar is explicitly ON
    # (2026-08-22, direct household correction: this used to run
    # unconditionally, dressed up as "auto-detect", entirely outside the
    # 3 solar_forecast_sensor_1/2/3 config fields -- "then what is the
    # purposed of having 3 inputs since it forces user ot autodetect...
    # that feels wrong". Default is False (see Nimbus's own const.py
    # comment on CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR) -- a fresh
    # install gets exactly what's configured in sources 1/2/3, nothing
    # more, unless this switch is explicitly turned on.
    if cfg.get("solver_auto_include_known_solar"):
        for fetcher in (fetch_open_meteo_solar_raw, fetch_solcast_solar_raw):
            result = fetcher()
            if result is not None:
                v, lo, up = result
                solar_values.append(np.array(v))
                solar_lowers.append(np.array(lo))
                solar_uppers.append(np.array(up))

    # Optional generic ADDITIONAL sources (solver_solar_forecast_
    # sensor_2/_3) -- for any OTHER solar forecast integration this
    # writer doesn't already know how to auto-detect (portability for
    # a different household's own install), or a second pointer at the
    # same known integration if wanted. Blank (the default) contributes
    # nothing, same guarantee as every other optional field.
    for entity_id in (
        cfg.get("solver_solar_forecast_sensor_2"),
        cfg.get("solver_solar_forecast_sensor_3"),
    ):
        if not entity_id:
            continue
        result = fetch_solar_source_safe(entity_id)
        if result is not None:
            v, lo, up = result
            solar_values.append(np.array(v))
            solar_lowers.append(np.array(lo))
            solar_uppers.append(np.array(up))

    if not solar_values:
        # Real bug found live (nimbus repo issue #115, Mark Purcell, a
        # real independent installer's own live health-check,
        # 2026-08-24): this used to `raise RuntimeError`, refusing to
        # solve AT ALL, ~470 times over an 8-hour overnight window on a
        # real install -- every single one of his configured solar
        # sources genuinely producing no data during the exact hours
        # solar is expected to be zero anyway (sunset to sunrise). This
        # is the WRONG failure mode for a condition that recurs every
        # single night on every solar install: the solver going
        # completely blind for hours (no re-optimisation against
        # changing overnight prices, no recovery from an unrelated
        # entity going unavailable until the next daylight cycle) is a
        # much worse outcome than solving with a real, honest 0.0 kW
        # solar placeholder -- exactly matching the flat-0.0-on-failure
        # convention already established for load
        # (read_load_forecast_sensor()'s own error path) and every
        # other genuinely-optional input in this file. A loud WARNING
        # (not a silent fallback) still fires so this is visible in the
        # log, same as the load-forecast equivalent.
        print(
            "WARN: no solar forecast source produced any real data this "
            "cycle (all configured sources unavailable, or none "
            "configured) -- solving with a flat 0.0 kW solar placeholder "
            "instead of refusing to solve. This is expected and harmless "
            "overnight (0.0 kW solar overnight is the correct real value "
            "regardless); if this fires during genuine daylight hours, "
            "check that at least one solver_solar_forecast_sensor_*/"
            "auto-include-known-solar source is configured and reachable.",
            file=sys.stderr,
        )
        solar_values = [np.zeros(n_periods)]
        solar_lowers = [np.zeros(n_periods)]
        solar_uppers = [np.zeros(n_periods)]

    if len(solar_values) == 1:
        solar_kw = [float(v) for v in solar_values[0]]
        # elements.py's own _validate_confidence_band() requires
        # lower_kw <= forecast_kw <= upper_kw exactly -- see the same
        # defensive clamp already applied per-source above.
        solar_lower_kw = [float(v) for v in solar_lowers[0]]
        solar_upper_kw = [float(v) for v in solar_uppers[0]]
    else:
        blended = blend_forecast_array(solar_values)
        # cross_source_spread() widens the confidence band by the real
        # DISAGREEMENT between the sources that actually succeeded this
        # cycle -- sources that agree closely add little; sources that
        # disagree sharply (exactly what was found live) genuinely
        # should make the Solver less confident at that specific
        # period, feeding the already-proven risk_aversion mechanism a
        # real, earned signal instead of just one source's own
        # (possibly overconfident) band.
        spread = cross_source_spread(solar_values)
        solar_kw = [max(0.0, v) for v in blended]
        combined_lower = np.min(np.stack(solar_lowers, axis=0), axis=0)
        combined_upper = np.max(np.stack(solar_uppers, axis=0), axis=0)
        solar_lower_kw = [
            max(0.0, min(combined_lower[i], solar_kw[i]) - spread[i] / 2)
            for i in range(n_periods)
        ]
        solar_upper_kw = [
            max(combined_upper[i], solar_kw[i]) + spread[i] / 2
            for i in range(n_periods)
        ]

    # Real, live anchor for the CURRENT period ONLY (2026-08-22, direct
    # household decision, after Mark Purcell's own question: "Why
    # doesn't NIMBUS use actuals for the current solar interval?").
    # Earlier the SAME day, an equivalent mechanism was explicitly
    # declined ("i do not want tricks") when it looked like it would
    # just make the forecast LOOK more accurate without the underlying
    # model improving. Reconsidered and reversed, deliberately, on a
    # different justification: the current interval is the one period
    # where the real answer is already known via direct measurement,
    # not something that needs predicting at all -- using it isn't a
    # trick, it's just not discarding information already in hand. HAEO
    # does exactly this already (confirmed earlier the same day: this
    # is why its own solar figure looks "spot on" for right now, not
    # because its underlying forecasting is better).
    #
    # sensor.combined_total_dc_power is the real, physical Sungrow
    # DC-power measurement (W) -- confirmed live, same day: 3.80kW
    # measured vs 5.57kW forecast at the same instant, a real,
    # meaningful divergence this closes. Deliberately scoped to index 0
    # ONLY -- every other period stays a genuine forecast, nothing
    # propagates beyond "right now". Zero-width confidence band at this
    # point -- a known, measured value has no forecast uncertainty to
    # represent. Any read failure (missing/unavailable entity, bad
    # value) leaves solar_kw[0] as the forecast value, same graceful-
    # degradation convention as every other optional source in this file.
    if entity_exists("sensor.combined_total_dc_power"):
        try:
            live_solar_kw = (
                float(ha_get("sensor.combined_total_dc_power")["state"]) / 1000.0
            )
            solar_kw[0] = max(0.0, live_solar_kw)
            solar_lower_kw[0] = solar_kw[0]
            solar_upper_kw[0] = solar_kw[0]
        except (ValueError, KeyError, TypeError):
            pass

    # Real household demand. OPTIONAL, richer path: sum a household's own
    # individually-forecasted circuits, read live from cfg (the Solver
    # settings wizard's own solver_load_forecast_entities field, 2026-08-23
    # fix for nimbus repo issues #56/#60) instead of one opaque whole-
    # house entity. When filled in, this is genuinely richer than a
    # single-entity config field could express (a real, live health dot
    # per circuit, a real cross-check against the whole-house meter
    # below). Genuinely empty by default -- a fresh install falls
    # straight to the single-sensor fallback below, same simple single-
    # entity pattern already used for solar above.
    load_forecast_entities = cfg.get("solver_load_forecast_entities") or []
    load_forecast_error = None
    # Computed BEFORE the branch below runs so it always reflects which
    # branch is ABOUT to execute, not an inference from its result -- see
    # resolve_load_forecast_source_label()'s own docstring.
    load_forecast_source_used = resolve_load_forecast_source_label(
        load_forecast_entities, cfg["solver_load_forecast_sensor"]
    )
    if load_forecast_entities:
        (
            load_kw,
            load_lower_kw,
            load_upper_kw,
            failed_load_entities,
            load_forecast_warnings,
            load_forecast_coverage_hours,
        ) = sum_load_forecasts(
            load_forecast_entities,
            grid_times,
            _cfg_num(cfg, "solver_inverter_self_consumption_kw", 0.0),
            now,
        )
    else:
        # Validated read (2026-08-23, real fix for nimbus repo issue
        # #66) -- the old bare ha_get(...)["attributes"]["forecast"]
        # either crashed this whole script or degraded silently on any
        # sensor shape other than the canonical {time, value}, with no
        # signal to the operator either way. On failure: a flat, honest
        # 0.0 kW placeholder (never a crash -- price/battery/grid parts
        # of the plan are still real and worth publishing even with load
        # wrong) plus a loud stderr WARN and a one-time persistent
        # notification, both naming the exact real reason.
        (
            load_kw,
            load_lower_kw,
            load_upper_kw,
            load_forecast_error,
            load_forecast_coverage_hours,
        ) = read_load_forecast_sensor(
            cfg["solver_load_forecast_sensor"], grid_times, now
        )
        if load_forecast_error is not None:
            print(f"WARN: {load_forecast_error}", file=sys.stderr)
            load_kw = [0.0] * n_periods
            load_lower_kw = [0.0] * n_periods
            load_upper_kw = [0.0] * n_periods
            _notify_load_forecast_error_once(load_forecast_error)
        failed_load_entities = []
        load_forecast_warnings = {}

    # Real, honest cross-check (reported only, never used to price or
    # dispatch anything): how far does "sum of 18 real circuits" diverge
    # from "one real whole-house meter's own forecast" right now? A
    # real, meaningful gap here is itself useful information (a missed
    # or newly-added circuit, sensor drift) worth surfacing on the
    # dashboard, not hiding silently.
    # Optional, read live from cfg (the wizard's own
    # solver_whole_house_cross_check_sensor field, 2026-08-23 fix for
    # nimbus repo issues #56/#60) -- None on a fresh install, a real
    # no-op below rather than a crash on an empty entity_id.
    whole_house_cross_check_sensor = (
        cfg.get("solver_whole_house_cross_check_sensor") or None
    )
    whole_house_now_kw = None
    if whole_house_cross_check_sensor:
        try:
            # Derived at read time from the real SOURCE sensor, not
            # hardcoded as a forecast entity_id directly -- matches
            # Nimbus's own real object_id_from_source() transform
            # (nimbus repo, sensor.py) so a future reconfigure of this
            # signal's source can never again leave this cross-check
            # silently pointing at a dead, renamed entity_id (exactly
            # what happened on this project's own reference install,
            # 2026-08-20 -- see this field's own comment above).
            object_id = whole_house_cross_check_sensor.split(".", 1)[-1]
            whole_house_cross_check_entity = f"sensor.nimbus_{object_id}_forecast"
            whole_house_fc = ha_get(whole_house_cross_check_entity)["attributes"][
                "forecast"
            ]
            whole_house_now_kw = max(
                0.0, resample_forecast(whole_house_fc, "value", grid_times[:1])[0]
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            KeyError,
            json.JSONDecodeError,
        ) as e:
            print(f"WARN: whole-house cross-check unavailable ({e})", file=sys.stderr)
            whole_house_now_kw = None
    summed_18_now_kw = load_kw[0]

    # Real, live anchor for the CURRENT period ONLY -- same mechanism
    # and reasoning as solar's own live anchor above (2026-08-22, direct
    # continuation of Mark Purcell's own request: "If you can fix
    # actuals for load and solar, becuase they are measured, then you
    # get better calculates for battery and grid outcomes"). Reads the
    # cross-check sensor's own RAW state directly -- NOT either forecast
    # (not the configured-circuits sum, not the whole-house meter's own
    # forecast-of-itself, both already captured above, UNCHANGED, for the
    # real cross-check diagnostic) -- this is deliberately inserted AFTER
    # summed_18_now_kw/whole_house_now_kw are captured so that diagnostic
    # keeps comparing two genuine forecasts against each other, not a
    # forecast against itself. Deliberately scoped to index 0 only, same
    # as solar; every other period stays a genuine forecast. Zero-width
    # band at this point -- no forecast uncertainty in something already
    # measured. Graceful no-op if unconfigured or on any read failure.
    if whole_house_cross_check_sensor and entity_exists(whole_house_cross_check_sensor):
        try:
            live_load_kw = float(ha_get(whole_house_cross_check_sensor)["state"])
            load_kw[0] = max(0.0, live_load_kw)
            load_lower_kw[0] = load_kw[0]
            load_upper_kw[0] = load_kw[0]
        except (ValueError, KeyError, TypeError):
            pass

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
        # Real bug found via a real-install health check (nimbus repo
        # #100, Mark Purcell): this sensor's own `state` was using
        # summed_18_now_kw -- a snapshot taken BEFORE the live cross-
        # check anchor above (2607-2614) can overwrite load_kw[0] --
        # while `forecast[0].value` below uses load_kw[0] AFTER that
        # same overwrite. Whenever a household configures the whole-
        # house cross-check sensor, this sensor's own headline `state`
        # and its own `forecast[0].value` would silently disagree --
        # two numbers a reasonable reader assumes are the same thing.
        # `sensor.nimbus_solver_config`'s own load_summed_18_now_kw
        # diagnostic (below) is DELIBERATELY left reading the pre-
        # anchor summed_18_now_kw -- its whole documented purpose is
        # comparing two genuinely independent forecasts, not a
        # forecast against an already-live-corrected value (see that
        # field's own comment). This fix is scoped to just this one
        # sensor's own internal state/forecast consistency.
        round(load_kw[0], 3),
        {
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
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
            "source_entities": load_forecast_entities,
            "load_forecast_source_used": load_forecast_source_used,
            # 2026-08-25, nimbus issue #187 (Mark Purcell, real-install
            # IV&V): v0.89.1's source_sensor/signal_role attributes only
            # ever reached NimbusForecastSensor (subentry-backed load/
            # power-signal sensors) -- this sensor is a genuinely
            # different class (_NimbusSolverPushSensor, a pure REST-
            # attribute mirror), so it never got either key at all, a
            # real missed code path, not an intentional scope boundary.
            # "other", matching every Load subentry's own signal_role
            # (there is no distinct SIGNAL_ROLE_LOAD in this project --
            # see const.py's own convention) -- this sensor's role is
            # definitionally a load aggregate. source_sensor is the
            # single real entity_id only when the single-sensor path is
            # actually active; when the richer multi-circuit summing
            # path is active there genuinely isn't one source to name,
            # so it's honestly None here -- source_entities above is
            # already the correct, richer answer for that case.
            "signal_role": "other",
            "source_sensor": cfg["solver_load_forecast_sensor"]
            if not load_forecast_entities
            else None,
            # NEW (2026-08-25, issue #112: "solver horizon 96.3h exceeds
            # subentry forecast horizon 48h") -- the REAL forecast
            # coverage this run's load_kw is backed by, in hours ahead
            # of `now`. None if it couldn't be determined. Compare
            # against horizon_hours (sensor.nimbus_solver_battery_
            # forecast, and the print() line below): whenever this is
            # smaller, every period beyond it is resample_forecast()'s
            # own flat-hold padding, not a real forecast -- see
            # compute_forecast_coverage_hours()'s own docstring.
            "load_forecast_coverage_hours": round(load_forecast_coverage_hours, 1)
            if load_forecast_coverage_hours is not None
            else None,
            "failed_load_entities": failed_load_entities,
            # NEW (2026-08-24, issue #105): entity_id -> the exact real
            # reason each failed_load_entities member was excluded --
            # was previously invisible on the multi-circuit summing
            # path (a bare "unavailable" in the log, nothing on this
            # sensor at all). See sum_load_forecasts()'s own docstring
            # for the full "why this exists" story.
            "load_forecast_warnings": load_forecast_warnings,
            "whole_house_cross_check_now_kw": round(whole_house_now_kw, 3)
            if whole_house_now_kw is not None
            else None,
            "inverter_self_consumption_kw": _cfg_num(
                cfg, "solver_inverter_self_consumption_kw", 0.0
            ),
            # None on success -- the exact human-readable reason on
            # failure, real proposal #2 from nimbus repo issue #66.
            "load_forecast_source_error": load_forecast_error,
            "generated_at": now.isoformat(),
        },
    )

    # Purely cosmetic devhub dashboard sensor (2026-08-25) -- never
    # referenced below, never feeds the LP. Wrapped exactly like
    # _notify_load_forecast_error_once() above: a failed weather-mirror
    # publish must never be allowed to break the actual solve.
    try:
        publish_weather_forecast_mirrors(cfg)
    except Exception:  # noqa: BLE001, S110 -- cosmetic-only, see comment above
        pass

    # Built-in EPR/regret/tracking quality score (2026-08-25) -- own
    # cheap idempotency check means this is safe to call every cycle;
    # wrapped the same way as every other non-essential publish in this
    # file, since a failure here must never take down the real solve.
    try:
        publish_daily_quality_report(cfg, now)
    except Exception:  # noqa: BLE001, S110 -- see comment above
        pass

    # Same "never break the real solve" wrapping as the two publishes
    # above -- see publish_nimbus_only_soc_counterfactual()'s own
    # docstring (2026-08-25, "i want u to build that into devbox
    # package").
    try:
        publish_nimbus_only_soc_counterfactual(cfg, now)
    except Exception:  # noqa: BLE001, S110 -- see comment above
        pass

    # Same "never break the real solve" wrapping -- see
    # publish_efficiency_backtest_report()'s own docstring (2026-08-25,
    # the "outstanding, unique" backtesting-engine ask).
    try:
        publish_efficiency_backtest_report(cfg, now)
    except Exception:  # noqa: BLE001, S110 -- see comment above
        pass

    # Same "never break the real solve" wrapping -- see
    # update_solar_delivery_ratio()'s own docstring (nimbus issue #128).
    try:
        solar_delivery = update_solar_delivery_ratio(cfg, now, grid_times, solar_kw)
    except Exception:  # noqa: BLE001 -- see comment above
        solar_delivery = None

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
        lv_price_fc = ha_get("sensor.localvolts_price_forecast")["attributes"][
            "forecast"
        ]
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
        spot_import_raw, import_real_mask = resample_price_with_extrapolation(
            lv_price_fc, "costsflexup", grid_times, aemo_forecast, import_offset_by_5min
        )
        spot_export, export_real_mask = resample_price_with_extrapolation(
            lv_price_fc,
            "earningsflexup",
            grid_times,
            aemo_forecast,
            export_offset_by_5min,
        )
        p2p_export = resample_real_p2p_rate(grid_times)

        # Real, live-CONFIGURABLE TOU network + flat fees baked directly
        # into import_price[t] (2026-08-16, real ask: "it needs ot be super
        # accurate") -- was previously just costsflexup (the spot commodity
        # price alone), missing real cost this household actually pays.
        # Baking it into the LP's own price input (not just reporting it
        # after the fact) means the dispatch DECISION also correctly
        # avoids real peak-hour import, not just the reported total.
        #
        # import_fee_rate()/solver_flat_fee_rate replace the old hardcoded
        # NETWORK_ENERGY_PEAK/OFFPEAK/SHOULDER_RATE/CERTIFICATES_RATE
        # Python constants entirely (2026-08-22, direct household demand:
        # "I TOLD U NO HARDCODED INPUTS - this has to work as user
        # setting") -- see import_fee_rate()'s own docstring for the real
        # "default + up to 3 override blocks" mechanism. Reads live from
        # cfg, same as every other Solver setting; a fresh install with
        # nothing configured correctly contributes 0 fees, same honest
        # no-op default as the fallback branch below already has.
        #
        # Real bug found live (devhub, 2026-08-24, nimbus repo issue
        # #152): this fee application used to live ONLY inside this
        # has_localvolts branch, even though network_fee_*/
        # solver_flat_fee_rate are genuinely generic, portable
        # config-flow fields with no LocalVolts dependency at all --
        # any real installer without LocalVolts who filled them in via
        # the dashboard (exactly as the wizard invites them to) had
        # them silently ignored, zero fee ever applied, zero warning.
        # Moved below the has_localvolts/else split entirely (see
        # "generic + real" fee application, after this if/else) so it
        # applies uniformly to whichever spot_import_raw either branch
        # produced -- LocalVolts-specific behaviour (AEMO extrapolation,
        # live P2P-window detection) stays exactly as before, only the
        # genuinely-portable fee step moved.

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
        export_bonus_price = [
            max(0.0, p2p_export[i] - spot_export[i]) for i in range(n_periods)
        ]
    else:
        # FALLBACK (2026-08-20, for anyone else): PREFERS a real, live
        # forecast if the configured sensor exposes one (2026-08-22, real
        # finding from Mark Purcell's own install -- see
        # resample_generic_price_forecast()'s own docstring for the full
        # story). Falls back to the sensor's CURRENT value held flat
        # across the whole horizon only when no usable forecast is found
        # -- the only thing genuinely possible with truly no forward-
        # looking data at all. No AEMO extrapolation, no network TOU
        # tables, no live P2P-window detection -- all of those are this-
        # household/Australian-NEM-specific and have no portable
        # equivalent yet (a real, honest, separately-tracked gap, not
        # pretended away).
        _import_fc = resample_generic_price_forecast_with_coverage(
            cfg["solver_import_price_sensor"], grid_times
        )
        # Real fee breakdown DOES apply to a generic install too, same
        # as LocalVolts -- see the "generic + real" fee application
        # below, after this if/else (nimbus repo issue #152). This is
        # just the raw spot/live price; fees get added uniformly for
        # both branches after this block.
        if _import_fc is not None:
            spot_import_raw, import_real_mask = _import_fc
        else:
            spot_import_raw = [safe_num(cfg["solver_import_price_sensor"])] * n_periods
            import_real_mask = [True] * n_periods
        _export_fc = resample_generic_price_forecast_with_coverage(
            cfg["solver_export_price_sensor"], grid_times
        )
        if _export_fc is not None:
            spot_export, export_real_mask = _export_fc
        else:
            spot_export = [safe_num(cfg["solver_export_price_sensor"])] * n_periods
            export_real_mask = [True] * n_periods
        match_fraction = 0.0
        # Manual, static P2P bonus from the config-flow's own optional
        # block (both default to 0.0 -- a full no-op -- if the household
        # doesn't have any P2P/community-trading scheme at all).
        p2p_recent_volume_kwh = _cfg_num(cfg, "solver_p2p_bonus_volume_kwh", 0.0)
        bonus_price_flat = _cfg_num(cfg, "solver_p2p_bonus_price", 0.0)
        export_bonus_price = [bonus_price_flat] * n_periods
        # No real multi-day recorded history to build an empirical band
        # from for a generic install -- price_risk_aversion (if a household
        # sets it > 0 anyway) is then a genuine no-op, same as every other
        # this-household-specific enhancement in the fallback branch above.
        import_price_upper_band = {}
        export_price_lower_band = {}

    # True pre-blend source pass-through (2026-08-27, nimbus repo issue
    # #216, Mark Purcell's refined asks #1/#2: publish an
    # export_price_raw alongside import_price_raw, and make `_raw`
    # actually mean "what the configured source sensor itself said,
    # before any transform" rather than the post-blend value). Captured
    # here, BEFORE blend_price_with_secondary_sources() below can
    # overwrite spot_import_raw/spot_export with a blended value -- on a
    # single-source install (no secondary configured) these are
    # byte-identical to the post-blend values, so nothing changes for
    # the overwhelming majority of installs.
    spot_import_source = list(spot_import_raw)
    spot_export_source = list(spot_export)

    # Optional second/third price sources to BLEND (2026-08-25, direct
    # household ask: "u also are missing my blended price forecasts...
    # in case we can feed it more than one... e.g. aemo... and amber").
    # Applies uniformly to whichever spot_import_raw/spot_export either
    # branch above produced -- genuinely optional, and every secondary
    # field defaults to unset, so a single-source install (the
    # overwhelming majority today) sees these two variables completely
    # unchanged. See blend_price_with_secondary_sources()'s own
    # docstring for the full mechanism -- extracted into its own
    # function specifically so it's directly unit-testable without
    # needing to drive the whole of main().
    spot_import_raw, import_price_cross_spread = blend_price_with_secondary_sources(
        spot_import_raw,
        cfg,
        ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
        grid_times,
        primary_real_mask=import_real_mask,
    )
    spot_export, export_price_cross_spread = blend_price_with_secondary_sources(
        spot_export,
        cfg,
        ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
        grid_times,
        primary_real_mask=export_real_mask,
    )

    # Generic + real: TOU network fees and the flat fee rate apply to
    # EVERY install, LocalVolts or not (nimbus repo issue #152, fixed
    # 2026-08-24) -- these are genuinely portable, no-NEM-specific-data-
    # required config-flow fields (number.nimbus_solver_network_fee_*,
    # number.nimbus_solver_flat_fee_rate), unlike the LocalVolts branch's
    # own AEMO extrapolation / live P2P-window detection which genuinely
    # do need Australian-NEM-specific data and stay LocalVolts-only.
    # Uses whichever spot_import_raw either branch above produced.
    # A fresh/generic install with every fee field left at its 0.0
    # default still contributes exactly 0 -- same honest no-op as
    # before, just no longer silently dropped just because LocalVolts
    # isn't configured.
    flat_fee_rate = _cfg_num(cfg, "solver_flat_fee_rate", 0.0)
    import_price = [
        spot_import_raw[i] + import_fee_rate(cfg, grid_times[i].hour) + flat_fee_rate
        for i in range(n_periods)
    ]

    # 2026-08-20: reads Nimbus's own real Solver settings config-flow
    # (see fetch_solver_config()'s own docstring for the full "close this
    # gap... need its own installer and inputs period" context) --
    # REPLACES the old ad-hoc input_number.nimbus_solver_* helpers, which
    # had to be hand-created via a separate, undocumented YAML package
    # file. A fresh install now needs nothing more than filling in
    # Nimbus's own hub "Configure" -> "Solver settings" form.
    capacity_kwh = float(cfg["solver_battery_capacity_kwh"])
    min_pct = _cfg_num(cfg, "solver_battery_min_soc_percent", 5.0)
    max_pct = _cfg_num(cfg, "solver_battery_max_soc_percent", 100.0)
    # The config-flow's own solver_battery_soc_sensor field replaces the
    # old hardcoded sensor.logger_battery_level_soc -- any household's
    # own real, live-measured SoC sensor now works, not just this one's.
    initial_pct = safe_num(cfg["solver_battery_soc_sensor"])
    max_charge_kw = float(cfg["solver_max_charge_kw"])
    # See resolve_max_discharge_kw()'s own docstring (near _cfg_num/
    # _cfg_int, top of file) for the full nimbus #125 story.
    max_discharge_kw = resolve_max_discharge_kw(cfg)
    charge_cost = _cfg_num(
        cfg, "solver_charge_cost", 0.01
    )  # not scheduled -- real automations never touch this, manual control

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
        discharge_cost_arr = np.array(
            [battery_discharge_cost_rate(t.hour) for t in grid_times]
        )
        salvage_value = battery_salvage_value_rate(grid_times[-1].hour)
    else:
        # FALLBACK (2026-08-20, for anyone else): flat values straight
        # from the config-flow's own Economic Policy step -- no day/night
        # schedule (that's tuned specifically around this household's own
        # P2P window, no portable equivalent yet).
        discharge_cost_arr = np.full(
            n_periods, _cfg_num(cfg, "solver_discharge_cost", 0.01)
        )
        salvage_value = _cfg_num(cfg, "solver_salvage_value", 0.15)

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
    n_clamped = (
        0  # kept in the pushed sensor's own attributes for continuity; always 0 now
    )

    # Real empirical price bands, mapped onto this solve's own real
    # grid_times (2026-08-21, task #128) -- None (a complete no-op) for
    # any household without the multi-day history to build one from (the
    # fallback branch above already sets both to {}).
    import_price_upper = apply_price_band(
        import_price, grid_times, import_price_upper_band
    )
    export_price_lower = apply_price_band(
        export_price, grid_times, export_price_lower_band
    )

    # Widen the risk_aversion band with the real cross-source disagreement
    # computed above, on top of (not instead of) any empirical percentile
    # band a has_localvolts install already built. A generic install with
    # no percentile band at all (import_price_upper/export_price_lower
    # still None here) but a genuine second/third price source configured
    # still gets a real, earned band from the disagreement alone --
    # exactly the household most likely to want blending in the first
    # place, and otherwise price_risk_aversion would stay a silent no-op
    # for them even after configuring a second source.
    if import_price_cross_spread is not None:
        base_upper = (
            import_price_upper if import_price_upper is not None else import_price
        )
        import_price_upper = [
            base_upper[i] + import_price_cross_spread[i] / 2 for i in range(n_periods)
        ]
    if export_price_cross_spread is not None:
        base_lower = (
            export_price_lower if export_price_lower is not None else export_price
        )
        export_price_lower = [
            base_lower[i] - export_price_cross_spread[i] / 2 for i in range(n_periods)
        ]

    max_soc_kwh_val = capacity_kwh * max_pct / 100.0
    min_soc_kwh_val = resolve_min_soc_kwh(min_pct, capacity_kwh, max_soc_kwh_val)
    initial_soc_kwh_raw = capacity_kwh * initial_pct / 100.0

    # Clamp initial_soc_kwh into [min_soc_kwh, max_soc_kwh] before the
    # BatteryConfig invariant check fires. Real, live cause this exists
    # (2026-08-23): the SoC sensor genuinely can (and does) read below
    # min_soc_percent for legitimate reasons -- the inverter runs the
    # pack below its own configured Solver floor during a fault, a fresh
    # install starts empty, a battery-cold event drops usable capacity
    # below the static floor. Every one of those is a real state the
    # world can be in, not a bad config; a live-sensor reading should
    # not crash the entire solve. The invariant in
    # elements.BatteryConfig.__post_init__ correctly protects USER-
    # PROVIDED configs (someone typing initial_soc=200% into a static
    # config file), but the writer's own initial_soc comes from a live
    # sensor -- it needs to gracefully absorb real, transient reality
    # rather than propagate a ValueError up through async_track_time_
    # interval every minute (which is exactly what a household reported:
    # 27+ crashes in a single window while SoC read 0.0% against a 5%
    # min). Clamp, log the violation, keep solving.
    initial_soc_kwh = min(max(initial_soc_kwh_raw, min_soc_kwh_val), max_soc_kwh_val)
    if initial_soc_kwh != initial_soc_kwh_raw:
        _initial_pct_raw = (
            initial_soc_kwh_raw / capacity_kwh * 100.0 if capacity_kwh > 0 else 0.0
        )
        _initial_pct_clamped = (
            initial_soc_kwh / capacity_kwh * 100.0 if capacity_kwh > 0 else 0.0
        )
        print(
            f"WARN: live battery SoC {_initial_pct_raw:.2f}% is outside the "
            f"configured Solver floor/ceiling [{min_pct:.2f}%, {max_pct:.2f}%] "
            f"-- clamped initial_soc to {_initial_pct_clamped:.2f}% for this "
            f"solve. If this repeats every period the real battery is stuck "
            f"outside its own configured range (fault, cold pack, sensor drift) "
            f"-- investigate rather than lower the floor.",
            file=sys.stderr,
        )
    charge_discharge_efficiency = (
        min(_cfg_num(cfg, "solver_efficiency_percent", 95.0) / 100.0, 0.999) ** 0.5
    )
    battery = elements.BatteryConfig(
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=initial_soc_kwh,
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
        #
        # Named here (not just inlined) so the SAME value can be surfaced
        # on the pushed diagnostic entity below -- nimbus issue #168 (Mark
        # Purcell, 2026-08-25): "a user reading solver_efficiency_percent
        # = 95 would reasonably interpret it as one-way... and get the
        # arithmetic wrong" without this documented on the entity itself.
        charge_efficiency=charge_discharge_efficiency,
        discharge_efficiency=charge_discharge_efficiency,
        charge_cost=charge_cost,
        discharge_cost=discharge_cost_arr,
        salvage_value=salvage_value,  # required field, but overridden by terminal_value_breakpoints below when set
        terminal_value_breakpoints=terminal_value_breakpoints_for(
            salvage_value, min_soc_kwh_val, max_soc_kwh_val
        ),
        # Every real day boundary in the horizon, plus the true final
        # period -- see midnight_boundary_period_indices()'s own
        # docstring above for the real 2026-08-22 finding this fixes.
        terminal_value_period_indices=sorted(
            set(midnight_boundary_period_indices(grid_times) + [len(grid_times) - 1])
        ),
        # Real economic cycle-wear cost (Track B2, 2026-08-22). 0.0
        # (unconfigured, the default) is a genuine no-op -- see
        # BatteryConfig's own degradation_cost_per_kwh docstring.
        degradation_cost_per_kwh=_cfg_num(cfg, "solver_degradation_cost_per_kwh", 0.0),
    )
    fixed_export_kw = fetch_p2p_fixed_export_kw(cfg, grid_times)
    grid = elements.GridConfig(
        import_price=np.array(import_price),
        export_price=np.array(export_price),
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
        export_bonus_price=np.array(export_bonus_price),
        export_bonus_volume_kwh=p2p_recent_volume_kwh,
        fixed_export_kw=np.array(fixed_export_kw)
        if fixed_export_kw is not None
        else None,
        import_price_upper=np.array(import_price_upper)
        if import_price_upper is not None
        else None,
        export_price_lower=np.array(export_price_lower)
        if export_price_lower is not None
        else None,
    )
    solar = elements.SolarConfig(
        forecast_kw=np.array(solar_kw),
        lower_kw=np.array(solar_lower_kw),
        upper_kw=np.array(solar_upper_kw),
    )
    loads = [
        elements.LoadConfig(
            name="household_load_summed_18",
            forecast_kw=np.array(load_kw),
            lower_kw=np.array(load_lower_kw),
            upper_kw=np.array(load_upper_kw),
        )
    ]
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
    risk_aversion = float(
        cfg.get("solver_risk_aversion")
        if cfg.get("solver_risk_aversion") is not None
        else RISK_AVERSION
    )
    import_price_risk_aversion = _cfg_num(cfg, "solver_import_price_risk_aversion", 0.0)
    export_price_risk_aversion = _cfg_num(cfg, "solver_export_price_risk_aversion", 0.0)
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
        periods=periods,
        grid=grid,
        battery=battery,
        solar=solar,
        loads=loads,
        previous_plan=previous_plan,
        risk_aversion=risk_aversion,
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
    total_cost_with_fixed_costs = (
        plan.total_cost or 0.0
    ) + horizon_days * FIXED_DAILY_CHARGES

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
    equivalent_full_cycles = (
        total_throughput_kwh / (2.0 * capacity_kwh) if capacity_kwh > 0 else 0.0
    )

    net_battery = plan.battery_discharge_kw - plan.battery_charge_kw
    corrected_grid_import = plan.grid_import_kw

    # DEFENSIVE SAFETY NET (2026-08-22) -- this file's own real, found
    # root cause. THIS module IS the native in-process solve path
    # (solver_runtime.py imports it exactly once, at container startup,
    # via a lazy module-level singleton that's never re-imported for the
    # life of the process) -- see the sibling standalone script's own
    # matching comment (116KAT-HA-AI repo, scripts/
    # nimbus_solver_forecast_writer.py) for the full incident writeup.
    # Short version: this container's most recent restart (2026-08-22
    # ~15:25 AEST, to deploy the native runtime itself) happened nearly 2
    # hours BEFORE the real network.py fix landed (commit 3f90c1f,
    # 17:20:03) -- so THIS path, specifically, has been the one silently
    # running the old, unfixed battery_charge bound every minute since,
    # racing the standalone cron writer's own always-current code. This
    # clamp stays in place permanently regardless of cause, as a genuine
    # backstop -- see the sibling file's own comment for exactly what it
    # corrects and why. A future container restart flushes this module's
    # own stale in-memory code (there's no other way to force a re-import
    # here); until then, disabling the Nimbus integration was used as an
    # immediate same-night mitigation, since that correctly cancels this
    # module's own timer (entry.async_on_unload, __init__.py) without a
    # restart.
    if grid.fixed_export_kw is not None:
        _fixed_mask = ~np.isnan(grid.fixed_export_kw)
        _violation_mask = _fixed_mask & (plan.battery_charge_kw > 0.05)
        _n_violations = int(np.sum(_violation_mask))
        if _n_violations > 0:
            print(
                f"[{now.isoformat()}] *** WARNING: solver returned {_n_violations} "
                f"period(s) with battery_charge_kw>0 during a committed "
                f"fixed_export_kw period -- mathematically should be impossible, "
                f"applying defensive clamp before push. ***",
                file=sys.stderr,
            )
            net_battery = np.where(
                _violation_mask, plan.battery_discharge_kw, net_battery
            )
            corrected_grid_import = np.where(
                _violation_mask,
                np.maximum(0.0, plan.grid_import_kw - plan.battery_charge_kw),
                plan.grid_import_kw,
            )

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
            # import side uses corrected_grid_import (see the defensive
            # clamp above) -- keeps this consistent with battery_kw
            # rather than silently reflecting the RAW, uncorrected import
            # on any period the clamp touched.
            "grid_import_kw": round(float(corrected_grid_import[i]), 3),
            "grid_export_kw": round(float(plan.grid_export_kw[i]), 3),
            # How much of grid_export_kw[i] earned the real, undiluted
            # P2P premium (vs the base/spot rate) -- exposed directly so
            # a real dashboard can show WHERE the real committed volume
            # landed, not just infer it (see nimbus's own network.py
            # Plan.export_bonus_kw docstring).
            "export_bonus_kw": round(float(plan.export_bonus_kw[i]), 3),
            "import_price": round(import_price[i], 4),
            # The raw commodity/spot price ALONE, before network TOU +
            # certificates are added on (2026-08-22, direct household
            # ask, after the real 8.4 vs 7.1c investigation: "normal
            # dumb folk user would look for buy price ot be what
            # localvolts_cost_flexup is... they would not get why you
            # added up costs to it... so maybe the table needs fees
            # column next ot cost?"). import_price above is UNCHANGED --
            # still the full landed cost, still what net_cost/the LP
            # itself actually uses -- this is purely an additional,
            # honest field so a dashboard can show Buy¢ = this (matches
            # what LocalVolts' own app shows) and Fees¢ = import_price
            # minus this, instead of one opaque combined number nobody
            # outside this codebase could verify against anything real.
            # True pre-blend source pass-through (2026-08-27, nimbus repo
            # issue #216, Mark Purcell) -- what the configured
            # solver_import_price_sensor itself said, before
            # blend_price_with_secondary_sources() folds in any
            # configured _sensor_2/_sensor_3. Previously this read
            # spot_import_raw AFTER blending, so on any install with a
            # secondary source configured, import_price_raw silently
            # stopped being a real "before any transformation" probe --
            # exactly Mark's own found-live gap ("on Config B... import_
            # price and import_price_raw are byte-identical... isn't
            # currently serving as a before-any-transformation
            # diagnostic"). On a single-source install (no _sensor_2/_3
            # configured) this is unchanged, byte-identical to before.
            "import_price_raw": round(spot_import_source[i], 4),
            "export_price": round(export_price[i], 4),
            # Same true pre-blend pass-through as import_price_raw above,
            # for the export side -- new field (2026-08-27, nimbus repo
            # issue #216, Mark Purcell's refined ask #1: "Publish
            # export_price_raw (same shape as the existing
            # import_price_raw attribute)").
            "export_price_raw": round(spot_export_source[i], 4),
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
                import_price[i] * float(corrected_grid_import[i]) * period_hours_arr[i]
                - export_price[i] * float(plan.grid_export_kw[i]) * period_hours_arr[i]
                - export_bonus_price[i]
                * float(plan.export_bonus_kw[i])
                * period_hours_arr[i],
                4,
            ),
        }
        for i in range(n_periods)
    ]

    # Named cost-component breakdown (2026-08-25, nimbus issue #149) --
    # see compute_cost_breakdown()'s own docstring for the full reasoning.
    cost_breakdown = compute_cost_breakdown(
        net_costs=[period["net_cost"] for period in forecast],
        total_cost=plan.total_cost,
        degradation_cost_per_kwh=_cfg_num(cfg, "solver_degradation_cost_per_kwh", 0.0),
        total_throughput_kwh=total_throughput_kwh,
        charge_cost=charge_cost,
        total_charge_kwh=total_charge_kwh,
        discharge_cost_arr=discharge_cost_arr,
        battery_discharge_kw=plan.battery_discharge_kw,
        period_hours=period_hours_arr,
    )

    # Cost-band diagnostic (2026-08-25, nimbus issue #147) -- see
    # compute_cost_band()'s own docstring for the full reasoning.
    cost_band = compute_cost_band(
        period_hours=period_hours_arr,
        load_lower_kw=np.array(load_lower_kw),
        load_upper_kw=np.array(load_upper_kw),
        solar_kw=np.array(solar_kw),
        import_price=np.array(import_price),
        export_price=np.array(export_price),
        charge_committed_kw=plan.battery_charge_kw,
        discharge_committed_kw=plan.battery_discharge_kw,
        charge_cost=charge_cost,
        discharge_cost_arr=discharge_cost_arr,
        final_soc_kwh=float(plan.battery_soc_kwh[-1]),
        salvage_value=salvage_value,
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
    )

    # Binding-constraint diagnostics (2026-08-18, Mark Purcell's audit
    # item #3; relabelled 2026-08-24, see compute_binding_constraint_
    # label()'s own docstring near resolve_max_discharge_kw for the
    # full "pinned at zero vs pinned at the real ceiling" story).
    binding_now, binding_now_value_per_kwh = compute_binding_constraint_label(
        plan, export_limit_kw, import_limit_kw, max_charge_kw, max_discharge_kw
    )
    # Earliest export_bonus_cap_<date> entry (ISO date strings sort
    # correctly as plain strings) is always tonight's/the current cap --
    # None when the two-tier export bonus mechanism isn't active at all.
    _p2p_cap_keys = sorted(
        k
        for k in plan.duals
        if k.startswith("export_bonus_cap_") and k != "export_bonus_cap_global"
    )
    p2p_volume_cap_shadow_price = (
        round(plan.duals[_p2p_cap_keys[0]], 4) if _p2p_cap_keys else None
    )

    ha_post_state(
        ENTITY_ID,
        round(float(net_battery[0]), 3),
        {
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
            "friendly_name": "Nimbus Solver Battery Forecast",
            # 2026-08-25, nimbus issue #189 (Mark Purcell, real-install
            # reproducer -- the follow-up to #187): this is the
            # "flagship diagnostic sensor" a Nimbus dashboard is most
            # likely to be built against, and it's a genuinely different
            # class again from both NimbusForecastSensor (fixed in
            # v0.89.1) and _NimbusSolverPushSensor's OTHER instance
            # (household load total, fixed in v0.92.1) -- this is the
            # Solver's own LP-derived dispatch plan, not a mirror or a
            # sum of upstream forecasts. "battery" (SIGNAL_ROLE_BATTERY's
            # own string value) is definitionally this entity's role.
            # source_sensor is the one real, measured entity this whole
            # plan is actually built around -- the live SoC reading the
            # LP solves forward from -- since there's no single upstream
            # "battery forecast" sensor the way load has
            # solver_load_forecast_sensor.
            "signal_role": "battery",
            "source_sensor": cfg["solver_battery_soc_sensor"],
            "forecast": forecast,
            "status": plan.status,
            "total_cost": plan.total_cost,
            "total_cost_with_fixed_costs": round(total_cost_with_fixed_costs, 4),
            "cost_breakdown": cost_breakdown,
            "cost_band": cost_band,
            "p2p_match_fraction": round(match_fraction, 4),
            "risk_aversion": risk_aversion,
            "import_price_risk_aversion": import_price_risk_aversion,
            "export_price_risk_aversion": export_price_risk_aversion,
            # Nimbus issue #205 (Mark Purcell, 2026-08-26): asked for an
            # entity exposing the terminal-value stack so overnight
            # reserve size can be regressed against price data without
            # pulling the full diagnostic each session. salvage_value is
            # the configured $/kWh rate terminal_value_breakpoints_for()
            # derives its curve from (see solver/elements.py's own
            # BatteryConfig docstring); degradation_cost_per_kwh is the
            # other half of the marginal-discharge economics driving the
            # same reserve decision. Both are the flat, currently-active
            # per-solve values, not a historical series.
            "salvage_value": salvage_value,
            "degradation_cost_per_kwh": _cfg_num(
                cfg, "solver_degradation_cost_per_kwh", 0.0
            ),
            "total_charge_kwh": round(total_charge_kwh, 2),
            "total_discharge_kwh": round(total_discharge_kwh, 2),
            "total_throughput_kwh": round(total_throughput_kwh, 2),
            "equivalent_full_cycles": round(equivalent_full_cycles, 3),
            # Nimbus issue #168 (Mark Purcell, 2026-08-25): "a user reading
            # solver_efficiency_percent = 95 would reasonably interpret it
            # as one-way... and get the arithmetic wrong." These four
            # fields pin down the exact convention this entity's own
            # forecast[].battery_kw and totals above already use, verified
            # against real live data (residual < 0.5 kWh over 100+ kWh
            # throughput on Mark's own atomic snapshot):
            # battery_kw is AC-side (grid-side of the inverter), and
            # solver_efficiency_percent is a ROUND-TRIP figure applied as
            # sqrt(round_trip) to each direction independently -- see
            # charge_discharge_efficiency's own comment above.
            "battery_kw_side": "AC",
            # Nimbus issue #197 (Mark Purcell, 2026-08-26): battery_kw's own
            # sign wasn't documented anywhere, and its convention here is
            # the opposite of what a reader would naturally assume --
            # net_battery = discharge_kw - charge_kw above, so POSITIVE
            # means discharging and NEGATIVE means charging. Mark had to
            # reverse-derive this from his own SoC data before he could
            # trust an external analysis built on this field.
            "battery_kw_sign_convention": "positive_discharge_negative_charge",
            "efficiency_convention": "round_trip_symmetric_sqrt",
            "charge_efficiency": round(charge_discharge_efficiency, 4),
            "discharge_efficiency": round(charge_discharge_efficiency, 4),
            # $ value of the AC-bus losses these efficiencies imply over
            # this horizon's own total_charge_kwh/total_discharge_kwh --
            # matches Mark's own Kirchhoff reconciliation formula
            # (tc*(1-eff) + td*(1/eff-1)), the expected gap between
            # AC-side source/sink sums once real inverter losses are
            # accounted for.
            "ac_bus_losses_kwh": round(
                total_charge_kwh * (1 - charge_discharge_efficiency)
                + total_discharge_kwh * (1 / charge_discharge_efficiency - 1),
                3,
            ),
            "p2p_recent_avg_volume_kwh": round(p2p_recent_volume_kwh, 2),
            # Nimbus issue #128 (Mark Purcell): rolling actual-vs-forecast
            # solar ratio, catches implicit inverter AC-side clipping
            # #114's own curtailment switch can't see. None when
            # solver_solar_power_sensor isn't configured, or when no
            # prediction has resolved yet -- both honest no-ops, never
            # a fabricated 1.0.
            "solar_delivery_ratio": (solar_delivery or {}).get("solar_delivery_ratio"),
            "solar_delivery_sample_count": (solar_delivery or {}).get(
                "solar_delivery_sample_count", 0
            ),
            "solar_delivery_underperforming": (solar_delivery or {}).get(
                "solar_delivery_underperforming", False
            ),
            "load_summed_18_now_kw": round(summed_18_now_kw, 3),
            "load_whole_house_cross_check_now_kw": round(whole_house_now_kw, 3)
            if whole_house_now_kw is not None
            else None,
            "failed_load_entities": failed_load_entities,
            # NEW (2026-08-24, issue #105) -- same dual-publication
            # convention as failed_load_entities/load_forecast_source_
            # error immediately below: present here AND on sensor.
            # nimbus_household_load_total_forecast.
            "load_forecast_warnings": load_forecast_warnings,
            # None on success -- real fix for nimbus repo issue #66
            # ("no attribute on sensor.nimbus_solver_battery_forecast
            # telling the operator the sensor shape they wired in was
            # rejected"). Present here (this entity) AND on sensor.
            # nimbus_household_load_total_forecast above -- the issue
            # named both.
            "load_forecast_source_error": load_forecast_error,
            # NEW (2026-08-25, nimbus issues #148/#116) -- present here AND
            # on sensor.nimbus_household_load_total_forecast above, same
            # dual-publication convention as the two fields immediately
            # above. See this field's own construction site (near
            # solver_load_forecast_entities, above) for the full reasoning.
            "load_forecast_source_used": load_forecast_source_used,
            # NEW (2026-08-25, issue #112) -- present here AND on sensor.
            # nimbus_household_load_total_forecast above (see that
            # field's own comment for the full reasoning). Directly
            # comparable to horizon_hours below: a smaller coverage
            # means part of this plan's own load input is
            # resample_forecast()'s flat-hold padding, not real.
            "load_forecast_coverage_hours": round(load_forecast_coverage_hours, 1)
            if load_forecast_coverage_hours is not None
            else None,
            "n_clamped_periods": n_clamped,
            "n_periods": n_periods,
            "horizon_hours": round(horizon_days * 24, 1),
            "solve_seconds": round(solve_seconds, 2),
            "generated_at": now.isoformat(),
            "binding_constraint_now": binding_now,
            "binding_constraint_shadow_price": binding_now_value_per_kwh,
            "energy_shadow_price_now": round(
                plan.duals.get("power_balance_t0", 0.0), 4
            ),
            "p2p_volume_cap_shadow_price": p2p_volume_cap_shadow_price,
        },
    )
    cross_check_str = (
        f"{whole_house_now_kw:.2f}kW"
        if whole_house_now_kw is not None
        else "unavailable"
    )
    coverage_str = (
        f"{load_forecast_coverage_hours:.1f}h"
        if load_forecast_coverage_hours is not None
        else "unknown"
    )
    print(
        f"[{now.isoformat()}] pushed {ENTITY_ID}: status={plan.status} "
        f"n_periods={n_periods} horizon={horizon_days * 24:.1f}h "
        f"load_forecast_coverage={coverage_str} solve_time={solve_seconds:.2f}s "
        f"total_cost={plan.total_cost:.2f} total_cost_with_fixed={total_cost_with_fixed_costs:.2f} "
        f"p2p_match_fraction={match_fraction:.3f} net_battery_now={net_battery[0]:.2f}kW "
        f"summed_18_loads_now={summed_18_now_kw:.2f}kW whole_house_cross_check={cross_check_str} "
        f"previous_plan_found={previous_plan is not None} "
        f"binding_now={binding_now!r} energy_shadow_price_now={plan.duals.get('power_balance_t0', 0.0):.4f} "
        f"p2p_volume_cap_shadow_price={p2p_volume_cap_shadow_price}"
    )


if __name__ == "__main__":
    if not acquire_lock():
        print(
            f"[{datetime.now(UTC).astimezone(BRISBANE_TZ).isoformat()}] previous run still in progress -- skipping this tick",
            flush=True,
        )
        sys.exit(0)
    try:
        main()
    except urllib.error.HTTPError as e:
        print(
            f"HTTP error: {e.code} {e.read().decode('utf-8', errors='replace')}",
            file=sys.stderr,
        )
        raise
    finally:
        release_lock()
