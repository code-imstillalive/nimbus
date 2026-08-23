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
sys.path.insert(0, os.environ.get(
    "NIMBUS_SOLVER_PATH",
    "/opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load",
))
# ^ wherever your own clone of https://github.com/code-imstillalive/nimbus
# actually lives (NIMBUS_SOLVER_PATH env var, or this exact NUC path by
# default) -- doesn't need to be inside an HA config tree at all, this
# script never touches HA's filesystem, only imports the pure-Python
# solver/ package from wherever it's checked out.
from solver import elements, network  # noqa: E402
from ml.blend import blend_forecast_array, cross_source_spread  # noqa: E402
import numpy as np  # noqa: E402

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
PLAN_STATE_PATH = os.environ.get("NIMBUS_SOLVER_PLAN_STATE_PATH", "/opt/nimbus_solver_last_plan.json")
# Real PID-file overlap guard (2026-08-17, see the deploy docstring's own
# "* * * * *" comment above) -- per the usual /opt-is-root-owned gotcha,
# this file needs the same one-time `sudo touch` + `chown` on first
# deploy as PLAN_STATE_PATH. Same env-var-overridable reasoning as above.
LOCK_PATH = os.environ.get("NIMBUS_SOLVER_LOCK_PATH", "/opt/nimbus_solver_forecast_writer.lock")
# Real bug found live (nimbus repo issue #66, Mark Purcell, 2026-08-23):
# a load-forecast sensor with an unrecognized attribute shape degraded
# or crashed with zero operator-visible signal. The persistent
# notification this sentinel gates (see _notify_load_forecast_error_once())
# fires once per genuinely-new error message, not once ever and not
# every single cron cycle -- same env-var-overridable /opt-default
# convention as PLAN_STATE_PATH/LOCK_PATH above.
LOAD_FORECAST_ERROR_NOTIFIED_PATH = os.environ.get(
    "NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_PATH", "/opt/nimbus_solver_load_forecast_error.txt"
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
INVERTER_SELF_CONSUMPTION_KW = 0.215


# Same real, live config-flow keys as P2P_BLOCK_KEYS above, mirrored
# exactly for network TOU fees (2026-08-22 -- see import_fee_rate()'s
# own docstring for the full "no hardcoded tariff" story).
NETWORK_FEE_BLOCK_KEYS = (
    ("solver_network_fee_1_rate", "solver_network_fee_1_start_hour", "solver_network_fee_1_end_hour"),
    ("solver_network_fee_2_rate", "solver_network_fee_2_start_hour", "solver_network_fee_2_end_hour"),
    ("solver_network_fee_3_rate", "solver_network_fee_3_start_hour", "solver_network_fee_3_end_hour"),
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
TOKEN = (
    os.environ.get("SUPERVISOR_TOKEN")
    or os.environ.get("HA_TOKEN")
)
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


def set_native_hass(hass) -> None:
    """Called once by the Nimbus integration itself, before running a
    solve in-process. See this module's own "PURE INTEGRATION seam"
    comment immediately above for the full story."""
    global _NATIVE_HASS
    _NATIVE_HASS = hass


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
        url=f"native://{entity_id}", code=code, msg=msg, hdrs=None,
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
        return {"entity_id": state.entity_id, "state": state.state, "attributes": dict(state.attributes)}
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
    if _NATIVE_HASS is not None:
        # states.async_set() mutates HA's own state machine and fires a
        # real event -- unlike the plain dict-read in ha_get() above,
        # this genuinely must happen ON the event loop, never directly
        # from a worker thread. hass.add_job() is HA's own documented
        # thread-safe scheduling primitive for exactly this: safe to call
        # from any thread, correctly hops onto the event loop itself.
        _NATIVE_HASS.add_job(
            functools.partial(_NATIVE_HASS.states.async_set, entity_id, state, attributes)
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
        return s if s.tzinfo is not None else s.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(s)


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
    """Real household demand, summed from a household's own individually-
    forecasted circuits -- see load_forecast_entities in main() (the
    comment right above the module-level "Real per-load demand" block
    near the top of this file) for the full "why sum individual circuits
    instead of one whole-house entity" reasoning. Each entity is fetched
    via fetch_load_forecast_safe() (individually guarded, never crashes
    the whole sum) and resampled with the SAME resample_forecast() every
    other forecast entity in this file already uses -- no new resampling
    logic needed.

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


def read_load_forecast_sensor(
    entity_id: str, grid_times: list[datetime]
) -> tuple[list[float] | None, list[float] | None, list[float] | None, str | None]:
    """Validated read of the single-sensor load-forecast fallback (the
    solver_load_forecast_sensor wizard field -- used when a household
    hasn't configured individual Nimbus Load subentries).

    Real bug found live (nimbus repo issue #66, Mark Purcell, 2026-08-23):
    the original version of this read was a bare
    `ha_get(entity_id)["attributes"]["forecast"]`, zero validation.
    Any sensor publishing its forecast under a different attribute name,
    or with different per-point keys, either crashed this whole script
    (an uncaught KeyError propagating out of main() -- the dashboard
    just freezes at whatever the LAST successful plan was, no crash
    visible anywhere except this script's own log) or degraded with a
    genuinely useless flat plan and no operator-visible signal at all.

    Genuinely common in practice, not a hypothetical: EMHASS's own
    load-forecast sensors publish under `scheduled_forecast` (not
    `forecast`), with per-point keys `date`/`<the sensor's own
    object_id>` (not `time`/`value`), as STRING values, frequently in W
    not kW. Auto-detected and handled here as a known, safe alternate
    shape -- not because every possible shape can be guessed, but
    because this one is common enough, and unambiguous enough (the
    sensor's own object_id names its value column), to convert safely
    rather than just report a clearer error.

    Returns (load_kw, load_lower_kw, load_upper_kw, error) -- error is
    None on success. The three arrays are None together with error on
    failure -- callers must check error, not just truthiness (an
    all-zero real forecast is a valid, if unusual, success).
    """
    try:
        state = ha_get(entity_id)
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        return None, None, None, f"{entity_id} could not be read ({e})"

    attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
    raw_fc = attrs.get("forecast")
    time_key, value_key, has_bands = "time", "value", True

    if not isinstance(raw_fc, list) or not raw_fc:
        # Not the canonical shape -- try the one known common alternate
        # (EMHASS's own scheduled_forecast/date/<object_id> pattern).
        alt_fc = attrs.get("scheduled_forecast")
        object_id = entity_id.split(".", 1)[-1]
        if isinstance(alt_fc, list) and alt_fc and isinstance(alt_fc[0], dict) and object_id in alt_fc[0]:
            raw_fc, time_key, value_key, has_bands = alt_fc, "date", object_id, False
        else:
            available = sorted(k for k, v in attrs.items() if isinstance(v, list))
            return None, None, None, (
                f"{entity_id} has no usable 'forecast' attribute (list-valued "
                f"attributes present: {available or 'none'}). Expected a list "
                f"of dicts with a 'time' and 'value' key -- see the canonical "
                f"shape any sensor.nimbus_<load>_forecast entity publishes."
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
        return None, None, None, (
            f"{entity_id}'s forecast has {len(raw_fc)} point(s) but none "
            f"parsed cleanly under keys '{time_key}'/'{value_key}'."
        )

    # Unit hint: W -> kW, using the sensor's own unit_of_measurement --
    # real, live, not guessed (EMHASS's own repro published exactly this
    # combination: unit_of_measurement 'W', raw values in the hundreds).
    # Nimbus's own canonical forecast entities are always already kW, so
    # this only ever fires on the alternate-shape path in practice, but
    # checked unconditionally rather than assumed.
    scale = 1.0 / 1000.0 if str(attrs.get("unit_of_measurement", "")).strip().lower() == "w" else 1.0

    fc_dicts = []
    for point in parsed_points:
        entry = {"time": point[time_key], "value": float(point[value_key]) * scale}
        if has_bands:
            if point.get("lower") is not None:
                entry["lower"] = point["lower"]
            if point.get("upper") is not None:
                entry["upper"] = point["upper"]
        fc_dicts.append(entry)

    load_kw = [max(0.0, v) for v in resample_forecast(fc_dicts, "value", grid_times)]
    if has_bands:
        # Exactly the original pre-#66 behavior for the canonical shape:
        # resample_forecast() returns 0.0 for every point when a source
        # genuinely has no lower/upper keys at all, and the clamp below
        # then widens that to [0, load_kw] rather than a zero-width band
        # -- preserved as-is for backward compatibility with any
        # existing install already relying on this exact shape.
        load_lower_kw = [max(0.0, v) for v in resample_forecast(fc_dicts, "lower", grid_times)]
        load_upper_kw = [max(0.0, v) for v in resample_forecast(fc_dicts, "upper", grid_times)]
        load_lower_kw = [min(load_lower_kw[i], load_kw[i]) for i in range(len(grid_times))]
        load_upper_kw = [max(load_upper_kw[i], load_kw[i]) for i in range(len(grid_times))]
    else:
        # EMHASS's own shape carries no confidence band at all -- zero-
        # width around the point estimate, same convention already used
        # elsewhere in this file for a genuinely bandless input.
        load_lower_kw, load_upper_kw = list(load_kw), list(load_kw)

    return load_kw, load_lower_kw, load_upper_kw, None


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
                    "Nimbus hub's \"Solver settings\" and check the load "
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

    runs_through_midnight = any(end_hour == 24 for _rate_kw, _start_hour, end_hour in blocks)

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
    end = datetime.now(timezone.utc)
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
            from homeassistant.components.recorder import get_instance as _recorder_get_instance
            from homeassistant.components.recorder import history as _recorder_history

            async def _fetch() -> dict:
                return await _recorder_get_instance(_NATIVE_HASS).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    _NATIVE_HASS, start, end, entity_id, True,  # no_attributes
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


def resample_generic_price_forecast(entity_id: str, grid_times: list[datetime]) -> list[float] | None:
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
            v = float(f["value"])
        except (KeyError, TypeError, ValueError):
            continue
        points.append((t, v))
    if not points:
        return None
    points.sort(key=lambda p: p[0])
    result: list[float] = []
    for gt in grid_times:
        candidates = [v for t, v in points if t <= gt]
        result.append(candidates[-1] if candidates else points[0][1])
    return result


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
    def fetch_solar_source_safe(entity_id: str) -> tuple[list[float], list[float], list[float]] | None:
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
                lower = [max(0.0, v) for v in resample_forecast(fc, "lower", grid_times)]
                upper = [max(0.0, v) for v in resample_forecast(fc, "upper", grid_times)]
                lower = [min(lower[i], value[i]) for i in range(n_periods)]
                upper = [max(upper[i], value[i]) for i in range(n_periods)]
            else:
                lower = list(value)
                upper = list(value)
            return value, lower, upper
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
            print(
                f"WARN: solar source {entity_id} unavailable ({e}) -- dropped from this solve's blend",
                file=sys.stderr,
            )
            return None

    def fetch_open_meteo_solar_raw() -> tuple[list[float], list[float], list[float]] | None:
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

    def fetch_solcast_solar_raw() -> tuple[list[float], list[float], list[float]] | None:
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
    for entity_id in (cfg.get("solver_solar_forecast_sensor_2"), cfg.get("solver_solar_forecast_sensor_3")):
        if not entity_id:
            continue
        result = fetch_solar_source_safe(entity_id)
        if result is not None:
            v, lo, up = result
            solar_values.append(np.array(v))
            solar_lowers.append(np.array(lo))
            solar_uppers.append(np.array(up))

    if not solar_values:
        msg = "No solar forecast source produced any real data this cycle -- cannot solve without at least one."
        raise RuntimeError(msg)

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
            max(0.0, min(combined_lower[i], solar_kw[i]) - spread[i] / 2) for i in range(n_periods)
        ]
        solar_upper_kw = [max(combined_upper[i], solar_kw[i]) + spread[i] / 2 for i in range(n_periods)]

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
            live_solar_kw = float(ha_get("sensor.combined_total_dc_power")["state"]) / 1000.0
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
    if load_forecast_entities:
        load_kw, load_lower_kw, load_upper_kw, failed_load_entities = sum_load_forecasts(load_forecast_entities, grid_times)
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
        load_kw, load_lower_kw, load_upper_kw, load_forecast_error = read_load_forecast_sensor(
            cfg["solver_load_forecast_sensor"], grid_times
        )
        if load_forecast_error is not None:
            print(f"WARN: {load_forecast_error}", file=sys.stderr)
            load_kw = [0.0] * n_periods
            load_lower_kw = [0.0] * n_periods
            load_upper_kw = [0.0] * n_periods
            _notify_load_forecast_error_once(load_forecast_error)
        failed_load_entities = []

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
    whole_house_cross_check_sensor = cfg.get("solver_whole_house_cross_check_sensor") or None
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
            whole_house_fc = ha_get(whole_house_cross_check_entity)["attributes"]["forecast"]
            whole_house_now_kw = max(0.0, resample_forecast(whole_house_fc, "value", grid_times[:1])[0])
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
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
            "source_entities": load_forecast_entities,
            "failed_load_entities": failed_load_entities,
            "whole_house_cross_check_now_kw": round(whole_house_now_kw, 3) if whole_house_now_kw is not None else None,
            "inverter_self_consumption_kw": INVERTER_SELF_CONSUMPTION_KW,
            # None on success -- the exact human-readable reason on
            # failure, real proposal #2 from nimbus repo issue #66.
            "load_forecast_source_error": load_forecast_error,
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
        flat_fee_rate = _cfg_num(cfg, "solver_flat_fee_rate", 0.0)
        import_price = [
            spot_import_raw[i] + import_fee_rate(cfg, grid_times[i].hour) + flat_fee_rate
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
        _import_fc = resample_generic_price_forecast(cfg["solver_import_price_sensor"], grid_times)
        import_price = _import_fc if _import_fc is not None else [num(cfg["solver_import_price_sensor"])] * n_periods
        # No real fee breakdown exists for a generic install -- the whole
        # configured value IS the raw price, no separate network/
        # certificates add-on to split out (see import_price_raw's own
        # comment where it's pushed, below).
        spot_import_raw = list(import_price)
        _export_fc = resample_generic_price_forecast(cfg["solver_export_price_sensor"], grid_times)
        spot_export = _export_fc if _export_fc is not None else [num(cfg["solver_export_price_sensor"])] * n_periods
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
    charge_cost = _cfg_num(cfg, "solver_charge_cost", 0.01)  # not scheduled -- real automations never touch this, manual control

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
        discharge_cost_arr = np.full(n_periods, _cfg_num(cfg, "solver_discharge_cost", 0.01))
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
    n_clamped = 0  # kept in the pushed sensor's own attributes for continuity; always 0 now

    # Real empirical price bands, mapped onto this solve's own real
    # grid_times (2026-08-21, task #128) -- None (a complete no-op) for
    # any household without the multi-day history to build one from (the
    # fallback branch above already sets both to {}).
    import_price_upper = apply_price_band(import_price, grid_times, import_price_upper_band)
    export_price_lower = apply_price_band(export_price, grid_times, export_price_lower_band)

    min_soc_kwh_val = capacity_kwh * min_pct / 100.0
    max_soc_kwh_val = capacity_kwh * max_pct / 100.0
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
        charge_efficiency=min(_cfg_num(cfg, "solver_efficiency_percent", 95.0) / 100.0, 0.999) ** 0.5,
        discharge_efficiency=min(_cfg_num(cfg, "solver_efficiency_percent", 95.0) / 100.0, 0.999) ** 0.5,
        charge_cost=charge_cost,
        discharge_cost=discharge_cost_arr,
        salvage_value=salvage_value,  # required field, but overridden by terminal_value_breakpoints below when set
        terminal_value_breakpoints=terminal_value_breakpoints_for(salvage_value, min_soc_kwh_val, max_soc_kwh_val),
        # Every real day boundary in the horizon, plus the true final
        # period -- see midnight_boundary_period_indices()'s own
        # docstring above for the real 2026-08-22 finding this fixes.
        terminal_value_period_indices=sorted(set(
            midnight_boundary_period_indices(grid_times) + [len(grid_times) - 1]
        )),
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
            net_battery = np.where(_violation_mask, plan.battery_discharge_kw, net_battery)
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
            "import_price_raw": round(spot_import_raw[i], 4),
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
                import_price[i] * float(corrected_grid_import[i]) * period_hours_arr[i]
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
            # None on success -- real fix for nimbus repo issue #66
            # ("no attribute on sensor.nimbus_solver_battery_forecast
            # telling the operator the sensor shape they wired in was
            # rejected"). Present here (this entity) AND on sensor.
            # nimbus_household_load_total_forecast above -- the issue
            # named both.
            "load_forecast_source_error": load_forecast_error,
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
