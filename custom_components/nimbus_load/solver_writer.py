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
(a real, live blocker hit installing the HAOS add-on this repo used to
ship against this private repo), and no separate token file at all. This
is now genuinely the SIMPLEST path for anyone new: install the Nimbus
integration via HACS, run its "Solver settings" wizard, done -- the
forecast starts producing itself. The standalone-script path below still
exists and is unchanged/still fully supported -- this household's own
live NUC deployment keeps using cron exactly as documented below,
deliberately not migrated in the same change that added the native path,
to avoid any risk to a real, already-working production system for a
change that exists to help OTHER installs.

nimbus issue #357 (2026-09-04): the `nimbus_solver_app` HAOS Supervisor
add-on this section used to describe as "a bigger, separate build, still
available" has been removed from this repo entirely -- it had silently
drifted out of sync with this file's own solver code (missing several
fixes) with no real path to staying maintained as a third copy, and the
native in-process path above already covers every architecture it did.
The Solver's own config-flow "Solver settings" wizard (Nimbus hub ->
Configure) installs and works fine via HACS on ANY platform including
HAOS -- it's what actually makes the native path possible now.

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
import itertools
import json
import logging
import math
import os
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

# nimbus issue #590: same dual-mode try/except shape as every other
# project-internal import elsewhere in this file (e.g. _sample_load_run_
# state's own load_run_state import) -- this file is loaded BOTH as part
# of the real package (`from . import ...` resolves) and as a bare
# top-level module by this project's own test harness and the
# standalone/cron deployment (plain `import ...` resolves instead, see
# tests/_solver_path.py). Only the domain tuple is needed at module
# scope here (_evaluate_done_condition's own membership check below);
# aliased at import time so the rest of this file never has to know
# which of the two import paths actually resolved.
try:
    from .done_condition import (
        ATTRIBUTE_DONE_DOMAINS as _done_condition_attribute_domains,
    )
    from .done_condition import parse_done_when as _parse_done_when
except ImportError:
    from done_condition import (
        ATTRIBUTE_DONE_DOMAINS as _done_condition_attribute_domains,
    )
    from done_condition import parse_done_when as _parse_done_when

# nimbus issue #735 stage 1: the input-gathering third of main(), moved
# out one source at a time. Same dual-mode try/except as every other
# project-internal import in this file. Imported as a MODULE, not by
# name, so this file's own patched-in-tests helpers stay resolvable at
# call time from the other side (see solver_inputs/__init__.py for the
# full note on that import direction).
try:
    from .solver_inputs import solar as solar_inputs_solar
except ImportError:
    from solver_inputs import solar as solar_inputs_solar  # type: ignore[no-redef]
try:
    from .solver_inputs import load as load_inputs
except ImportError:
    from solver_inputs import load as load_inputs  # type: ignore[no-redef]
try:
    from .solver_inputs import battery_soc as battery_soc_inputs
except ImportError:
    from solver_inputs import (  # type: ignore[no-redef]
        battery_soc as battery_soc_inputs,
    )
try:
    from .solver_inputs import prices as price_inputs
except ImportError:
    from solver_inputs import prices as price_inputs  # type: ignore[no-redef]
# nimbus issue #485: household-mode presets. Pure, HA-free table +
# two apply functions; same dual-mode import as every other
# project-internal module here.
try:
    from . import household_modes
except ImportError:
    import household_modes  # type: ignore[no-redef]

# nimbus issue #735 stage 3, pulled forward: the load-input block stage 2
# wants to extract has an ha_post_state() call sitting INSIDE it, so that
# block could not move without putting a publish into an inputs module.
# Hoisting this publish out is therefore a prerequisite for stage 2, not a
# detour. Same dual-mode, by-MODULE import as above and for the same
# load-bearing reason (#861).
try:
    from . import solver_publish
except ImportError:
    import solver_publish  # type: ignore[no-redef]
# aemo_crosscheck is noqa'd, not deleted: since #735 stage 4 moved the
# price/P2P branch out, its only consumer reaches it as an ATTRIBUTE of
# this module (sw.aemo_crosscheck) from solver_inputs/prices.py, which
# ruff's static analysis cannot see. Same arrangement, and same reason, as
# blend_forecast_array below -- this file stays the single binding point
# for every shared helper, so a monkeypatch here is honoured everywhere.
# The noqa sits on the standalone branch only, because that is the binding
# ruff sees as the surviving one -- putting it on both trips RUF100.
try:
    from . import aemo_crosscheck
except ImportError:
    import aemo_crosscheck  # type: ignore[no-redef]  # noqa: F401

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
# real-local-time value in this file is built from an explicitly-resolved
# zone (zoneinfo, Python 3.9+ stdlib), never a bare `.astimezone()`.
#
# nimbus issue #347 (Mark Purcell, 2026-09-03): this was hardcoded to
# Australia/Brisbane -- correct, and harmlessly DST-free, for THIS
# household's own install, but every hour-of-day decision in this file
# (TOU fee lookup, the P2P window, midnight SoC anchors, fixed-export
# blocks, quality-report day boundaries) ran in AEST for every other
# install too. A Sydney/Melbourne user during AEDT got all of these one
# hour late; a non-AU install was off by many hours -- the same bug
# class as the 2026-08-17 incident above, just moved from "the system's
# own tz" to "one specific household's tz" instead of resolving the
# REAL configured one. LOCAL_TZ now resolves in priority order: the
# NIMBUS_SOLVER_TIMEZONE env var (explicit override, works identically
# in standalone/addon/native mode) if set; otherwise this default
# (unchanged behaviour for this household and anyone else who never
# sets the env var) until set_native_hass() below runs, at which point
# native mode re-resolves it from hass.config.time_zone -- the real,
# already-configured value every HA install has, needing zero new user
# configuration. REST/standalone/addon mode has no equivalent live
# `hass` to ask, so it keeps whatever LOCAL_TZ already resolved to here
# (the env var, or this default) for the whole run.
#
# Deliberately NOT addressed here (nimbus issue #347's own second,
# separate finding, scoped out of this fix): wall-clock timedelta
# arithmetic on a tz-aware LOCAL_TZ datetime is genuinely unsafe across
# a real DST transition (a `timedelta` added to a ZoneInfo-aware
# datetime moves in WALL-CLOCK terms, not real elapsed time) -- the ML
# grid, solver period_starts, and per-day P2P cap grouping all build
# their own time grids this way. Harmless for Brisbane (no DST) and
# therefore never live-verified against a real transition by this
# household's own install; a real fix needs the grid built in UTC and
# converted to local only for feature/day-keying, per the issue's own
# suggested fix -- a materially larger, riskier change to the core ML/
# solver time-grid construction, deliberately left for a dedicated pass
# rather than rushed alongside this narrower, lower-risk timezone-
# resolution fix.
LOCAL_TZ = ZoneInfo(os.environ.get("NIMBUS_SOLVER_TIMEZONE", "Australia/Brisbane"))


def _local(ts):
    """The same instant, expressed in the household's OWN timezone.

    Every hour-of-day decision in this file -- the P2P window, network
    fee tiers, scheduled discharge cost, salvage value, the self-consume
    block, EV departure -- is a statement about LOCAL wall-clock time. A
    household's 17:00 P2P block is 17:00 where they live, not 17:00 UTC.

    Reading `.hour` straight off a datetime silently makes that decision
    depend on whatever timezone the CALLER attached, which is not a
    property this file controls: the same functions are reached from the
    native integration, from the cron writer, and from a service call
    whose `start` a user types. On the daily path `grid_times` carry
    UTC -- visible in the report's own j_ref_hourly keys, which are
    stamped +00:00 -- so a bare `gt.hour` gated the reference
    household's real 17:00-24:00 P2P block against 17:00-24:00 UTC,
    i.e. 03:00-10:00 Brisbane. That is their solar CHARGING window,
    where grid export is genuinely ~0, so the scorer recorded a 12 kW x
    7 h commitment as entirely undelivered: p2p_commitment_shortfall_kwh
    76.9 against a real value near zero, which then lands straight on
    j_ach and depresses EPR.

    This file's own line 249 records the same fault being found and
    fixed at ONE site; the pattern that allowed it was left everywhere
    else.

    So: convert explicitly, never trust the input. On a datetime already
    in LOCAL_TZ this is an exact no-op.

    A NAIVE datetime is returned unchanged rather than guessed at.
    `.astimezone()` on a naive value silently assumes the machine's own
    timezone -- a second, different wrong answer. Unchanged is exactly
    the previous behaviour, so this can never make a naive caller worse.
    """
    return ts.astimezone(LOCAL_TZ) if getattr(ts, "tzinfo", None) is not None else ts


import numpy as np
from numpy.typing import NDArray

# nimbus issue #349 (Mark Purcell, codebase review): this used to run an
# UNCONDITIONAL sys.path.insert(0, <package dir>) here, every single
# time this module was imported -- including natively, as
# custom_components.nimbus_load.solver_writer, where it's not needed at
# all (a plain relative import already resolves the real .ml/.solver
# subpackages correctly with zero sys.path mutation). That unconditional
# insert made `ml`/`solver`/`sensor`/`const`/etc. importable as TOP-
# LEVEL names for every other integration and library in the same HA
# process -- a real, process-wide namespace-pollution risk (a same-named
# module imported later by anything else would get shadowed), and
# created two genuinely distinct module objects for the same file
# (`ml.blend` here vs. `custom_components.nimbus_load.ml.blend` via
# coordinator.py) -- harmless for pure functions today, a real
# isinstance/identity trap the moment either package holds a dataclass/
# enum/singleton that crosses that boundary.
#
# Now tries the real relative import FIRST (native mode, and this
# project's own test suite via tests/_solver_path.py's sys.path shim
# both hit this successfully) -- the sys.path shim below is only ever
# reached via the ImportError this raises when there's genuinely no
# parent package to resolve `.ml`/`.solver` against (a bare
# `python3 solver_writer.py` cron/standalone run, __package__ == ""),
# not unconditionally on every import.
try:
    from .ml.blend import blend_forecast_array, cross_source_spread
    from .solver import elements, lp, network, nowcast_skill
    from .solver.backtest import (
        EFFICIENCY_CANDIDATES_PERCENT,
        efficiency_label,
        run_efficiency_sensitivity_sweep,
    )
    from .solver.quality_report import compute_quality_report
    from .solver.regret import evaluate_realized_cost
except ImportError:
    # Standalone/cron mode (2026-08-21, env-var-overridable -- was
    # hardcoded, edit-the-file-yourself before this): every one of these
    # three household-specific values now has a real default (this
    # NUC's own exact current setup, so behavior here is UNCHANGED with
    # zero env vars set) but can be overridden without touching a single
    # line of actual solve logic.
    sys.path.insert(
        0,
        os.environ.get(
            "NIMBUS_SOLVER_PATH",
            "/opt/homeassistant/config/nimbus_repo/custom_components/nimbus_load",
        ),
    )
    # ^ wherever your own clone of https://github.com/code-imstillalive/nimbus
    # actually lives (NIMBUS_SOLVER_PATH env var, or this exact NUC path
    # by default) -- doesn't need to be inside an HA config tree at all,
    # this script never touches HA's filesystem, only imports the
    # pure-Python solver/ package from wherever it's checked out.
    # blend_forecast_array is noqa'd, not deleted: it has two real
    # consumers ruff's static analysis cannot see. (1) solver_inputs/
    # solar.py reaches it as an ATTRIBUTE of this module
    # (sw.blend_forecast_array) so that this file stays the single
    # binding point for every shared helper -- see solver_inputs/
    # __init__.py. (2) tests/test_solver_writer_import_and_token_
    # laziness.py asserts on its __module__ as the probe for nimbus
    # issue #349's "the relative import must not produce a second,
    # distinct top-level `ml` module" guarantee. Removing it would
    # break both.
    from ml.blend import blend_forecast_array, cross_source_spread  # noqa: F401
    from solver import elements, lp, network, nowcast_skill
    from solver.backtest import (
        EFFICIENCY_CANDIDATES_PERCENT,
        efficiency_label,
        run_efficiency_sensitivity_sweep,
    )
    from solver.quality_report import compute_quality_report
    from solver.regret import evaluate_realized_cost

if os.environ.get("SUPERVISOR_TOKEN") and not os.environ.get("HA_BASE"):
    # Running inside a real HA Supervisor app/add-on container with
    # homeassistant_api: true set in that add-on's own config.yaml --
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
# nimbus issue #451 (Mark Purcell): TIER1_HOURS used to be a fixed 24.0 --
# tier1's own 5-min resolution ran for a full real day regardless of what
# the upstream price data could actually support at that resolution.
# Real finding: Nimbus does not have a genuine 5-minute-resolution
# forward price for 24h. LocalVolts costsFlexUp/earningsFlexUp is a
# genuine 5-min number, but only for the CURRENT settlement interval;
# AEMO's own PD7DAY predispatch report (the longer-horizon price source)
# is natively 30-min. Every "5-min" period beyond the current+next real
# NEM trading interval was really just one of those 30-min numbers
# stamped six times with compute_5min_offset()'s own historical bump on
# top -- fake precision, paid for in real solver load (365 periods at
# the old shape vs ~200 now). Tier1 is now boundary-snapped to the real
# current + next NEM trading interval (:00/:30) instead of a fixed
# duration -- see build_tiered_grid()'s own docstring for the exact
# mechanism. TRADING_INTERVAL_MINUTES names the real NEM settlement
# cadence tier1's own snapping (and tier2's own resolution below) both
# key off.
TRADING_INTERVAL_MINUTES = 30
TIER1_PERIOD_HOURS = 5.0 / 60.0
TIER2_PERIOD_HOURS = 0.5  # was 1.0 -- matches AEMO PD7DAY's own real 30-min cadence
TOTAL_HORIZON_HOURS = 96.0  # tier0 + tier1 + tier2 combined span from tier1_start
# The real, ceiling-not-typical max span tier1 can ever be now that it's
# boundary-snapped rather than fixed-duration -- "current + next 30-min
# trading interval" tops out at 60 real minutes (tier1_start landing
# exactly on a :00/:30 mark), never more. Used by the two report-scoring
# functions below (compute_daily_quality_report/compute_efficiency_
# backtest_report) to decide whether a scored window fits entirely
# inside tier1's own real span -- see nimbus issues #438/#441 for why
# this matching matters (a report scored at the wrong resolution
# silently disagrees with what the live dispatch it's grading actually
# did).
MAX_TIER1_HOURS = 1.0

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

# nimbus issue #348 (Mark Purcell, 2026-09-03 codebase review): several
# real, deliberate household-specific tuning choices in this file
# silently override or ignore an install's own wizard-configured values
# with zero visibility into that happening.
#
# Three of the four findings are now real, wizard-configurable fields,
# removed from the list this function warns about below as each was
# fixed: the fixed daily charge and the post-midnight self-consume
# window (see number.py's own CONF_SOLVER_FIXED_DAILY_CHARGE/
# CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS comments, fixed 2026-09-04);
# the battery_discharge_cost_rate()/battery_salvage_value_rate() day/
# night schedule (see scheduled_discharge_cost_rate()'s own docstring,
# near DISCHARGE_COST_SCHEDULE_BLOCK_KEYS, fixed 2026-09-04); and the
# "generic" price-forecast-array field's LocalVolts-specific
# costsflexup/earningsflexup key parsing (now solver_price_forecast_
# array_import_key/export_key, fixed 2026-09-04). None of these are
# "silent" anymore -- each is a real, visible, wizard-editable field,
# even though the discharge-cost/salvage-value schedule and the
# costsflexup/earningsflexup keys both still only APPLY on the
# has_price_forecast_array branch (a deliberate, unchanged scope, not a
# new gap -- a generic install without that sensor was never affected by
# either).
#
# The one remaining finding (the P2P matched-rate sensor's own fixed
# 17:00-24:00 window, independent of the household's own configured P2P
# block hours) is still a genuine, considered tradeoff not yet
# generalised -- this module-level flag + _log_active_household_
# specific_overrides_once() (called once near the top of main(), see
# that function's own call site) keeps it visible in the log at
# startup, by name, instead of only discoverable by reading this file's
# source.
_household_specific_overrides_logged = False


def _log_active_household_specific_overrides_once(cfg: dict) -> None:
    """Logs, once per process (not once per solve cycle -- main() runs
    every ~1-5 minutes), which of the known household-specific overrides
    in this file are currently active for THIS install's own config.
    Never raises: a lookup miss on any single condition just skips that
    one line rather than blocking the real solve this function is
    otherwise unrelated to.
    """
    global _household_specific_overrides_logged
    if _household_specific_overrides_logged:
        return
    _household_specific_overrides_logged = True
    active: list[str] = []
    if cfg.get("solver_p2p_matched_rate_forecast_sensor"):
        active.append(
            "solver_p2p_matched_rate_forecast_sensor is configured -- its real "
            "matched rate is forced to 0 outside the fixed 17:00-24:00 window "
            "regardless of your own configured P2P block hours"
        )
    if active:
        _LOGGER.warning(
            "Nimbus Solver: %d household-specific override(s) active for this "
            "install (see nimbus issue #348 for the full context) -- %s",
            len(active),
            "; ".join(active),
        )


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
            _LOGGER.warning(
                "Nimbus Solver: solver_max_discharge_live_entity '%s' exists "
                "but has no usable numeric 'max' attribute -- falling back "
                "to solver_max_discharge_kw.",
                live_entity,
            )
    return float(cfg["solver_max_discharge_kw"])


_SOH_RANGE_WARNED: set[float] = set()


def resolve_effective_capacity_kwh(cfg: dict) -> float:
    """Nameplate battery capacity derated by configured State of Health.

    nimbus issue #1013. `number.nimbus_solver_battery_soh_percent` has
    existed as a dashboard dial, been mirrored onto
    `sensor.nimbus_solver_config`, and been read by **nothing** -- no
    solve path, native or cron. A household setting State of Health
    changed nothing about dispatch or scoring, silently. The reference
    household had it at 98%, in the reasonable belief it derated a 122.2
    kWh pack to ~119.8, while every solve planned against the full
    122.2.

    The semantics implemented here are not invented: const.py's own
    comment beside CONF_SOLVER_BATTERY_SOH_PERCENT has documented
    `effective_capacity = capacity_kwh * soh_percent / 100` since the
    field was added. Only the code was missing.

    **Why this derates both rails rather than only the ceiling** -- the
    one real modelling choice here, and it was measured rather than
    argued. Fourteen consecutive days of the reference household's own
    daily statistics, two sensors describing one pack:

        sensor.combined_battery_charge   max 119.72 kWh  min 2.39 kWh
        sensor.logger_battery_level_soc  max 100.0 %     min 2.0 %
        sensor.combined_battery_capacity     122.16 kWh, constant

    The BMS reports 100% at **119.72 kWh, not at the 122.16 nameplate**
    -- so SoC is a percentage of the pack's CURRENT usable capacity, and
    the nameplate is not the scale the household's own SoC sensor speaks
    in. The floor agrees independently: Min SoC is configured at 2.0%
    and the daily minimum lands at 2.39 kWh, which is 2.0% of 119.72
    (2.394) rather than of 122.16 (2.443).

    So both bounds must come off the derated number, or the solver's kWh
    and the SoC sensor are describing different batteries. Derating only
    the ceiling would silently reserve MORE real energy at the floor
    than the household asked for.

    The configured value is also vindicated by the same reading: 122.2 x
    0.98 = 119.756 against a measured 119.72, a difference of 0.03%. The
    dial was right the whole time. Nothing read it. See
    tests/test_effective_capacity_soh_derating.py for the full data.

    **Deliberately still one static number, not a live sensor.** The
    inverters do publish per-pack SoH and it drifts over years, and
    CONF_BATTERY_TOWER_SOH_SENSOR already exists in the topology
    subentry -- but const.py's own comment is explicit that this field
    is "one number the owner updates occasionally... NOT an automated
    fade-tracking model." Wiring the live sensor is a real follow-up,
    not something to smuggle in under a bug fix.

    No-op by default: DEFAULT_SOLVER_SOH_PERCENT is 100.0, so any
    install that has never touched the dial gets a byte-identical
    number back.

    A reading outside (0, 100] returns nameplate UNCHANGED rather than
    scaling by it. number.py clamps the entity to [1, 100], so this is
    only reachable from hand-edited config or the cron copy's own YAML
    -- and a nonsense value must not silently shrink a real pack, nor
    inflate one beyond its nameplate.
    """
    nominal = _cfg_num(cfg, "solver_battery_capacity_kwh", 0.0)
    soh_pct = _cfg_num(cfg, "solver_battery_soh_percent", 100.0)
    if not 0.0 < soh_pct <= 100.0:
        if soh_pct not in _SOH_RANGE_WARNED:
            _SOH_RANGE_WARNED.add(soh_pct)
            _LOGGER.warning(
                "Nimbus Solver: configured Battery State of Health is "
                "%.2f%%, outside the valid (0, 100] range -- ignoring it "
                "and planning against the full %.2f kWh nameplate "
                "capacity for this solve. Set it between 1 and 100 on the "
                "dashboard if you meant to derate an aged pack.",
                soh_pct,
                nominal,
            )
        return nominal
    return nominal * soh_pct / 100.0


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
        _LOGGER.warning(
            "Nimbus Solver: configured Min SoC (%.2f%%) resolves to %.4f "
            "kWh, at or below zero -- the solver requires a strictly "
            "positive floor to stay solvable. Clamped to %.4f kWh for this "
            "solve (effectively no reserve, not a literal 0%%). If you "
            "genuinely want a small real reserve instead, raise Min SoC "
            "above 0%% on the dashboard.",
            min_pct,
            min_soc_kwh,
            floor_kwh,
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
        _LOGGER.warning(
            "Nimbus Solver: entity '%s' has a non-numeric state -- could "
            "not parse it as a price/SoC value (%s). Falling back to %s "
            "for this solve. Check that this entity is genuinely "
            "configured correctly (a real price/SoC sensor, not "
            "something else that happens to share the name).",
            entity_id,
            e,
            fallback,
        )
        return fallback


def _risk_aversion_effect_now(
    plan: network.Plan,
    solar_kw: list[float],
    import_price: list[float],
    export_price: list[float],
) -> dict[str, float | None]:
    """Real, live proof the three risk-aversion sliders (Load/Solar,
    Import Price, Export Price -- number.py's own _DESCRIPTIONS) are
    actually reaching the LP, not a UI-only control. 2026-09-07, direct
    household finding: "moved slider, nothing happened" is genuinely
    ambiguous between "the slider/write path is broken" and "the
    mechanism is a correct no-op right now because the underlying
    forecast band has ~zero width this period" -- nothing on any
    dashboard could tell those apart, so the household had no way to
    confirm the mechanism was even wired up correctly.

    Reads plan.effective_solar_kw/effective_import_price/
    effective_export_price (network.py's own _risk_adjusted()/
    _risk_adjusted_one_sided() output, exposed on Plan specifically for
    this) against the raw forecast/price arrays this SAME solve was fed
    -- period 0 only, "right now" being the only period a household
    looking at a live dashboard actually cares about. A nonzero
    raw-vs-effective gap is direct, physical proof the slider is having
    an effect this cycle; a zero gap at a nonzero slider value is
    equally real proof the band is currently zero-width, not that
    anything is broken.

    Returns every value None (not 0.0 -- a real gap of zero and "no
    data to compare" must stay distinguishable) whenever plan wasn't
    optimal or is a bare Plan() built without these fields (every
    existing test that constructs Plan directly, pre-2026-09-07).
    """
    if not plan.is_optimal or plan.effective_solar_kw.size == 0:
        return {
            "solar_risk_effect_now_kw": None,
            "import_price_risk_effect_now": None,
            "export_price_risk_effect_now": None,
        }
    return {
        "solar_risk_effect_now_kw": round(
            float(solar_kw[0]) - float(plan.effective_solar_kw[0]), 3
        ),
        "import_price_risk_effect_now": round(
            float(plan.effective_import_price[0]) - float(import_price[0]), 4
        ),
        "export_price_risk_effect_now": round(
            float(export_price[0]) - float(plan.effective_export_price[0]), 4
        ),
    }


# How far below-or-at its own committed rate grid_export[0] may sit and
# still be called "pinned by the P2P commitment" (nimbus issue #921).
# Not a float-equality epsilon: this is one variable out of a ~12,000-
# column two-phase MIP, and the residual on a real instance is nothing
# like the exact bound a small synthetic LP returns. 10 W is far below
# anything a household could act on and far above any plausible solver
# residual on a 12 kW commitment.
_PIN_MATCH_TOLERANCE_KW = 0.01


def resolve_fixed_export_charge_clamp(
    fixed_export_kw: NDArray[np.float64] | None,
    *,
    gated_charge_kw: NDArray[np.float64],
    aggregate_charge_kw: NDArray[np.float64],
    discharge_kw: NDArray[np.float64],
    grid_import_kw: NDArray[np.float64],
) -> tuple[
    NDArray[np.float64], NDArray[np.float64], NDArray[np.float64], NDArray[np.bool_]
]:
    """The P2P fixed-export charge backstop, as a pure function.

    Returns `(net_battery, charge_kw, grid_import_kw, violation_mask)`.

    Extracted 2026-09-15 for nimbus issue #923 -- same precedent as
    compute_binding_constraint_label() above, and for the same reason:
    this code had no test of its own, and it does not merely warn. It
    rewrites three published quantities, so a wrong premise here
    silently publishes wrong numbers rather than failing loudly.

    **`gated_charge_kw` must be the charge array of the battery the
    LP gate was actually applied to, not the fleet total.** network.py
    bounds charging during a commitment with

        charging_ub_during_fixed_window(t, grid, b.max_charge_kw)
        if b_idx == 0 else b.max_charge_kw

    -- a hard `ub=0` for battery 0 only. Every battery participant
    (#467: a second inverter, an EV) keeps its ordinary ceiling and may
    legitimately charge inside a committed window; `grid_export` is
    pinned there, so such a charge draws from solar or import rather
    than from committed export.

    `Plan.battery_charge_kw` is the documented SUMMED AGGREGATE across
    all of them. Comparing that aggregate against a per-battery bound is
    what #923 was: on a multi-battery install a participant's legal
    charge read as an impossible solve, and the clamp then erased it
    from the published plan and understated grid import by the same
    amount, every period of every committed window. The check was right
    when written (2026-08-22, one battery, aggregate == battery 0);
    #467 changed what the aggregate means and this was not revisited.

    Only the gated battery's own charge is removed. A participant's
    charge in the same period is real and survives into every published
    figure.
    """
    net_battery: NDArray[np.float64] = (discharge_kw - aggregate_charge_kw).astype(
        np.float64
    )
    no_violation: NDArray[np.bool_] = np.zeros(net_battery.shape, dtype=np.bool_)
    if fixed_export_kw is None:
        return net_battery, aggregate_charge_kw, grid_import_kw, no_violation

    violation = (~np.isnan(fixed_export_kw)) & (gated_charge_kw > 0.05)
    if not violation.any():
        return net_battery, aggregate_charge_kw, grid_import_kw, no_violation

    kept_charge_kw = np.maximum(0.0, aggregate_charge_kw - gated_charge_kw)
    return (
        np.where(violation, discharge_kw - kept_charge_kw, net_battery),
        np.where(violation, kept_charge_kw, aggregate_charge_kw),
        np.where(
            violation,
            np.maximum(0.0, grid_import_kw - gated_charge_kw),
            grid_import_kw,
        ),
        violation,
    )


def compute_binding_constraint_label(
    plan: network.Plan,
    export_limit_kw: float,
    import_limit_kw: float,
    max_charge_kw: float,
    max_discharge_kw: float,
    period_0_hours: float,
    fixed_export_kw_now: float | None = None,
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

    nimbus issue #662 (Mark Purcell): `period_0_hours` -- these 4
    variables' own LP objective coefficients are all scaled by period 0's
    own duration (`p.set_cost(grid_import[0], effective_import_price[0]
    * hours[0])`, same construction #662 itself diagnosed for the
    power_balance_t0 ROW dual), so each one's own raw reduced cost comes
    out in "$ per kW of bound," carrying the identical implicit
    `x hours[0]` factor -- dividing by it recovers the true $/kWh
    marginal value, the EXACT SAME correction `forced_import_cost`
    already applies (`reduced_costs[...] / hours[t]`, network.py). Found
    via the same #662 audit, same file, same mechanism -- not previously
    reported, but the identical bug class Mark's own issue names.
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
            elif (
                var_key == "grid_export_0"
                and fixed_export_kw_now is not None
                and not math.isnan(fixed_export_kw_now)
                and abs(solved_value - float(fixed_export_kw_now))
                <= _PIN_MATCH_TOLERANCE_KW
            ):
                # nimbus issue #921: a THIRD bound this variable really
                # has, and the only one not in _BINDING_FAMILIES above.
                # p2p_export.grid_export_bounds() pins grid_export[t] to
                # lb == ub == the committed rate for every period under a
                # real P2P export commitment -- so the variable is at a
                # bound (nonzero reduced cost, exactly as LP optimality
                # says) at a value that is neither 0 nor export_limit_kw.
                # Before this branch existed that landed in the "shouldn't
                # happen" case below and told a correctly-configured P2P
                # household its solver was confused, every period of every
                # evening block -- the hours where the most money moves
                # and where someone is most likely to be reading this
                # field to understand the plan.
                #
                # The tolerance is the whole point of this second pass.
                # v0.94.324 shipped this branch testing `abs(solved -
                # pin) <= 1e-6` and, on the very install it was written
                # for, it never fired: the attribute still published
                # "12.00 kW (unexpected ...)" with a 12.0 kW commitment
                # genuinely in force that period. A small synthetic LP
                # returns a pinned variable at exactly its bound (checked
                # directly: delta 0.000e+00), which is what made 1e-6
                # look safe -- and that does not generalise to one
                # variable out of a ~12,000-column two-phase MIP.
                #
                # Still two-sided, deliberately. A value well BELOW the
                # commitment is as impossible as one well above it, since
                # grid_export_bounds() returns (pin, pin); and #694's
                # price-spike override, which relaxes the bounds to (pin,
                # export_limit_kw) at t=0, is exactly a case where export
                # rises above the commitment and must NOT be reported as
                # pinned. Both are real states worth surfacing, so the
                # test is "within a real tolerance of the pin", not "at
                # or below it".
                #
                # Reports the COMMITMENT, not the solved value: the
                # commitment is the exact configured number the household
                # recognises, and the solved value is the same quantity
                # plus whatever the solver's own residual is.
                binding_now = (
                    f"{short_name} pinned at {float(fixed_export_kw_now):.2f} kW "
                    "by P2P export commitment"
                )
            elif solved_value <= 1e-6:
                # Pinned at zero -- a real "not worth it right now"
                # economic decision, NOT a capacity constraint. Distinct
                # from the ceiling case on purpose (see docstring above).
                #
                # nimbus issue #951 (Mark Purcell, 48-hour IV&V #950):
                # this branch used to sit ABOVE the P2P-pin branch, which
                # left a residual of the exact bug class #921 was filed to
                # fix. `fetch_p2p_fixed_export_kw()` deliberately pins
                # export to 0.0 for `solver_post_window_self_consume_hours`
                # after midnight on any block configured with end_hour=24,
                # matching the real automation's own self-consume window --
                # so 0.0 is a genuine, reachable COMMITMENT, not an absence
                # of one. Evaluated first, it intercepted that case and
                # told a correctly-configured P2P household its solver saw
                # no economic reason to export, during hours when export
                # was in fact deterministically forbidden.
                #
                # Safe to demote below the pin branch, and this is the part
                # worth not re-deriving: a period with NO commitment never
                # reaches here carrying 0.0. `fetch_p2p_fixed_export_kw()`
                # returns None when no block is configured at all, and
                # defaults an unmatched period to float("nan") -- both of
                # which the pin branch's own guards reject. 0.0 appears
                # only for the real post-midnight pin, so hoisting the pin
                # check cannot relabel a genuine "not economical" period.
                binding_now = f"{short_name} at zero (not economical right now)"
            else:
                # Shouldn't happen for a variable with a genuinely
                # nonzero reduced cost (LP optimality: only ever nonzero
                # exactly at a bound) -- represented honestly rather
                # than assumed, matching this module's own "never paper
                # over an unexpected state" convention.
                #
                # The LP-optimality reasoning above is sound; what makes
                # this branch reachable is the unstated assumption that
                # 0 and limit_kw are a variable's ONLY bounds. #921 found
                # one that isn't (the P2P export pin, handled directly
                # above). Anything still landing here is a bound nothing
                # in this function models -- which is worth saying loudly
                # rather than smoothing over, so keep this branch.
                #
                # It now names this period's own P2P commitment when there
                # is one. v0.94.324's branch above failed silently on the
                # exact install it was written for, and the message it
                # fell through to gave no way to tell "no commitment this
                # period" from "a commitment the comparison rejected".
                # Carrying the number makes the next misfire diagnosable
                # from the published attribute alone.
                # Only on grid export -- the commitment bounds that one
                # variable, and naming it beside a battery's own binding
                # constraint would be a non-sequitur.
                _pin_note = (
                    f", P2P commitment {float(fixed_export_kw_now):.2f} kW"
                    if var_key == "grid_export_0"
                    and fixed_export_kw_now is not None
                    and not math.isnan(fixed_export_kw_now)
                    else ""
                )
                binding_now = (
                    f"{short_name} at {solved_value:.2f} kW "
                    f"(unexpected -- neither its 0 nor {limit_kw:.2f} kW bound"
                    f"{_pin_note})"
                )
            binding_now_value_per_kwh = round(val / period_0_hours, 4)
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
    soc_penalty_cost: float = 0.0,
    grid_import_excess_penalty_cost: float = 0.0,
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

    `soc_penalty` (nimbus issue #781) is Plan.soc_penalty_cost passed
    straight through -- the real dollar total of every battery's own soft
    min/max-SoC violation penalty, computed directly from the same solved
    underfill/overfill variables and rate network.py's own build_plan()
    already used to cost them. Broken out as its OWN explicit term
    (not left inside the residual below) because it is deliberately
    LARGE ("dominant by construction," a bare $/kWh on the state
    violation applied every period, not scaled by hours[t]) and, before
    this field existed, had no explicit line item anywhere: a household's
    own real 3-battery solve showed a $1678 `terminal_value_credit` that
    was actually almost entirely this penalty (real batteries genuinely
    sitting below their configured floor), misrepresented as if it were
    salvage/terminal value earned rather than a "your battery is below
    its safety floor" warning sign.

    `grid_import_excess_penalty` (nimbus issue #788) is Plan.
    grid_import_excess_penalty_cost passed straight through -- the real
    dollar markup of network.py's own `import_excess_penalty_rate` on
    whatever grid_import_excess volume this cycle's release valve
    (nimbus issue #390) actually used. Broken out as its OWN explicit
    term for the identical reason soc_penalty was (#781): `grid_net`
    above only ever prices the COMBINED grid_import_kw (which already
    folds grid_import_excess_kw in) at the plain effective_import_price
    rate -- the penalty markup itself had no explicit line item anywhere
    before this field existed, so it fell entirely into the residual
    below, misrepresenting a real "the LP had to blow the configured
    import cap to stay feasible" warning sign as if it were terminal
    value/salvage credit. 0.0 (a genuine no-op) whenever the release
    valve was never needed this cycle, the common case.

    `terminal_value_credit` is deliberately the RESIDUAL (total_cost minus
    the six terms above, `soc_penalty` and `grid_import_excess_penalty`
    now included), not a re-implementation of
    terminal_value_breakpoints_for()'s own piecewise segment math in a
    second place -- residual-by-construction means this always reconciles
    exactly (Mark's own test #2: grid_net + degradation + charge_fee +
    discharge_fee + soc_penalty + grid_import_excess_penalty +
    terminal_value_credit == total_cost), and its value already IS what
    an operator wants to see (the real terminal-value/salvage credit's
    total economic effect, whatever combination of checkpoints produced
    it), without a second implementation that could silently drift from
    the LP's own real one over time.
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
        grid_net_cost
        + degradation_cost
        + charge_fee_cost
        + discharge_fee_cost
        + soc_penalty_cost
        + grid_import_excess_penalty_cost
    )
    return {
        "grid_net": round(grid_net_cost, 4),
        "degradation": round(degradation_cost, 4),
        "charge_fee": round(charge_fee_cost, 4),
        "discharge_fee": round(discharge_fee_cost, 4),
        "soc_penalty": round(soc_penalty_cost, 4),
        "grid_import_excess_penalty": round(grid_import_excess_penalty_cost, 4),
        "terminal_value_credit": round(terminal_value_credit, 4),
    }


def periods_within_hours(period_hours: NDArray[np.float64], hours: float) -> int:
    """nimbus issue #630: the number of leading periods (from "now")
    whose cumulative duration is at most `hours` -- e.g. how many of a
    tiered grid's own periods fall inside the next 24 real hours, used
    to slice compute_cost_band()'s own inputs down to a shorter-horizon
    band. Always at least 1, so a grid whose very first period alone
    already exceeds `hours` (a coarse, late-horizon-only grid, or a
    pathological single-period plan) still gets a real, non-empty
    slice rather than an empty array."""
    cum = np.cumsum(period_hours)
    return max(1, int(np.searchsorted(cum, hours, side="right")))


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
    except Exception:
        # nimbus issue #363 (Mark Purcell, codebase review): swallow stays
        # (this is a read-only, best-effort diagnostic re-costing, never
        # worth breaking the real solve over), but now with a breadcrumb.
        _LOGGER.debug("Nimbus Solver: compute_cost_band failed", exc_info=True)
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
#
# nimbus issue #348 (Mark Purcell, codebase review, fixed 2026-09-04):
# these used to be hardcoded Python constants -- a real, tuned economic
# schedule with zero way for THIS household (let alone anyone else) to
# retune it short of editing source. Now the SCHEMA DEFAULTS for a real,
# optional, wizard-configurable schedule (mirroring the already-
# established solver_network_fee_1/2/3_rate/start_hour/end_hour and
# solver_p2p_block_1/2/3 pattern in this same file) -- every existing
# install (including this one) gets BYTE-IDENTICAL behaviour, since none
# of the new config keys below have ever been set, and `_cfg_num`/
# `_cfg_int` fall back to exactly these same literals whenever a key is
# absent. Kept as named constants (not inlined) so the schema-default
# call sites below read as "the historical schedule," not magic numbers.
BATTERY_DISCHARGE_COST_NIGHT = 0.01  # 5pm-7am (P2P window + midnight-7am)
BATTERY_DISCHARGE_COST_DAY = 0.09  # 7am-5pm
BATTERY_SALVAGE_VALUE_NIGHT = 0.3  # 5pm-midnight (P2P window only)
BATTERY_SALVAGE_VALUE_OTHER = 0.15  # midnight-5pm

# Block 1 alone reproduces the real historical schedule (a single
# overnight window); blocks 2/3 default OFF (rate=0.0, the same "rate<=0
# = not configured" convention NETWORK_FEE_BLOCK_KEYS already uses) --
# provided for the same reason that pattern offers 3 slots everywhere
# else in this file: a more complex real-world tariff (e.g. a genuine
# 3-tier day/shoulder/night schedule) can express itself without a code
# change, not because this household's own schedule needs more than one.
DISCHARGE_COST_SCHEDULE_BLOCK_KEYS = (
    (
        "solver_discharge_cost_schedule_block_1_rate",
        "solver_discharge_cost_schedule_block_1_start_hour",
        "solver_discharge_cost_schedule_block_1_end_hour",
    ),
    (
        "solver_discharge_cost_schedule_block_2_rate",
        "solver_discharge_cost_schedule_block_2_start_hour",
        "solver_discharge_cost_schedule_block_2_end_hour",
    ),
    (
        "solver_discharge_cost_schedule_block_3_rate",
        "solver_discharge_cost_schedule_block_3_start_hour",
        "solver_discharge_cost_schedule_block_3_end_hour",
    ),
)
SALVAGE_VALUE_SCHEDULE_BLOCK_KEYS = (
    (
        "solver_salvage_value_schedule_block_1_rate",
        "solver_salvage_value_schedule_block_1_start_hour",
        "solver_salvage_value_schedule_block_1_end_hour",
    ),
    (
        "solver_salvage_value_schedule_block_2_rate",
        "solver_salvage_value_schedule_block_2_start_hour",
        "solver_salvage_value_schedule_block_2_end_hour",
    ),
    (
        "solver_salvage_value_schedule_block_3_rate",
        "solver_salvage_value_schedule_block_3_start_hour",
        "solver_salvage_value_schedule_block_3_end_hour",
    ),
)
# Per-block schema-default fallbacks: block 1 defaults to the real
# historical schedule (ON by construction); blocks 2/3 default OFF.
# Keyed by the same tuples above so the lookup functions below can stay
# a single, non-repetitive loop instead of one hardcoded branch per slot.
_DISCHARGE_COST_BLOCK_DEFAULTS = (
    (BATTERY_DISCHARGE_COST_NIGHT, 17, 7),  # wraps past midnight
    (0.0, 0, 0),
    (0.0, 0, 0),
)
_SALVAGE_VALUE_BLOCK_DEFAULTS = (
    (BATTERY_SALVAGE_VALUE_NIGHT, 17, 24),
    (0.0, 0, 0),
    (0.0, 0, 0),
)


def _hour_in_schedule_block(hour: int, start_hour: int, end_hour: int) -> bool:
    """True if `hour` (0-23) falls in [start_hour, end_hour). Handles a
    block that wraps past midnight (end_hour <= start_hour, e.g. 17->7
    meaning 17,18,...,23,0,...,6) -- unlike NETWORK_FEE_BLOCK_KEYS'/
    P2P_BLOCK_KEYS' own plain `start <= hour < end` check, which has
    never needed to express an overnight-spanning block until this real
    schedule (5pm-7am) needed one. `start_hour == end_hour` is a
    zero-width block, never matches -- same "not configured" meaning as
    end_hour<=start_hour in the non-wrapping helpers elsewhere.
    """
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def scheduled_discharge_cost_rate(cfg: dict, hour: int) -> float:
    """Real, wizard-configurable replacement for the old hardcoded
    battery_discharge_cost_rate() -- see this section's own module-level
    comment for the full nimbus issue #348 story. Only used on the
    has_price_forecast_array branch (see that branch's own call site);
    a generic install without that sensor configured is completely
    unaffected, still reading the flat solver_discharge_cost field.
    """
    default_rate = _cfg_num(
        cfg, "solver_discharge_cost_schedule_default_rate", BATTERY_DISCHARGE_COST_DAY
    )
    for (rate_key, start_key, end_key), (
        rate_default,
        start_default,
        end_default,
    ) in zip(
        DISCHARGE_COST_SCHEDULE_BLOCK_KEYS, _DISCHARGE_COST_BLOCK_DEFAULTS, strict=True
    ):
        rate = _cfg_num(cfg, rate_key, rate_default)
        if rate <= 0:
            continue
        start_hour = _cfg_int(cfg, start_key, start_default)
        end_hour = _cfg_int(cfg, end_key, end_default)
        if _hour_in_schedule_block(hour, start_hour, end_hour):
            return rate
    return default_rate


def scheduled_salvage_value_rate(cfg: dict, hour: int) -> float:
    """Real, wizard-configurable replacement for the old hardcoded
    battery_salvage_value_rate(). Salvage value only applies ONCE, to
    the horizon's own FINAL period, so this doesn't need a full
    per-period array -- just needs to reflect what the real schedule
    would set at whatever real hour the horizon happens to end at, not
    whatever's live right now."""
    default_rate = _cfg_num(
        cfg, "solver_salvage_value_schedule_default_rate", BATTERY_SALVAGE_VALUE_OTHER
    )
    for (rate_key, start_key, end_key), (
        rate_default,
        start_default,
        end_default,
    ) in zip(
        SALVAGE_VALUE_SCHEDULE_BLOCK_KEYS, _SALVAGE_VALUE_BLOCK_DEFAULTS, strict=True
    ):
        rate = _cfg_num(cfg, rate_key, rate_default)
        if rate <= 0:
            continue
        start_hour = _cfg_int(cfg, start_key, start_default)
        end_hour = _cfg_int(cfg, end_key, end_default)
        if _hour_in_schedule_block(hour, start_hour, end_hour):
            return rate
    return default_rate


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
    build_tiered_grid -- 'now' is built from LOCAL_TZ, not UTC), so
    no timezone conversion is needed here. Works correctly regardless of
    which tier a given midnight falls in -- the 5-min Tier1 region and
    the 1-hour Tier2 region both break exactly on real hour boundaries,
    so "the period right before an hour-0 period" is always well-
    defined either way.
    """
    indices = []
    for t in range(len(grid_times) - 1):
        if _local(grid_times[t]).hour != 0 and _local(grid_times[t + 1]).hour == 0:
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
# needed for REST/standalone mode (cron, or the now-removed
# nimbus_solver_app addon while it existed) -- unchanged, still fails
# loudly there, which is correct, existing,
# already-accepted behaviour for that deployment path. Wrapped in
# try/except purely so a MISSING token can never crash native mode,
# which never reads TOKEN at all (see ha_get()/ha_post_state()/
# fetch_price_history() above -- every REST branch that would actually
# USE this value is skipped entirely once _NATIVE_HASS is set).
_TOKEN: str | None = None
_TOKEN_LOADED = False


def _load_token() -> str | None:
    """Lazily resolves the REST-mode bearer token (nimbus issue #349,
    Mark Purcell): the original module-level TOKEN resolution above ran
    UNCONDITIONALLY at import time -- a real blocking file read
    (TOKEN_PATH) on the event loop the moment this module is first
    imported natively (sensor.py's own async_setup_entry). Only ever
    called from the REST-mode branches below (ha_get()/ha_post_state()/
    fetch_price_history()) -- native mode's own _NATIVE_HASS seam skips
    every one of those entirely, so a native install now never touches
    this file/env lookup at all, not even once. Cached (resolved once
    per process, same as the original module-level TOKEN) since the
    token itself never changes mid-run.
    """
    global _TOKEN, _TOKEN_LOADED
    if _TOKEN_LOADED:
        return _TOKEN
    _TOKEN_LOADED = True
    _TOKEN = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN")
    if not _TOKEN:
        try:
            with open(TOKEN_PATH, "r", encoding="utf-8") as f:
                _TOKEN = f.read().strip()
        except OSError:
            _TOKEN = None
    return _TOKEN


# PURE INTEGRATION seam (2026-08-22, direct real-world push: Mark Purcell
# hit a private-repo git-clone-with-no-auth wall trying to install the
# nimbus_solver_app addon (since removed, #357), and separately flagged
# the deeper, correct architectural point -- "EMHASS had the addon, which
# was always a complication for access logs and sending commands... HAEO
# runs as a pure integration." This is the fix: a real, additive
# extension point that lets the EXACT SAME ~2400 lines below run natively
# inside HA Core's own process (via custom_components/nimbus_load/
# solver_runtime.py, same repo), with ZERO behaviour change to the
# existing standalone deployment (cron on this household's own NUC) --
# _NATIVE_HASS defaults to None, and
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

# Every entity_id sensor.py's own async_setup_entry() DOES eventually call
# register_entity_handler() for, in native mode (2026-09-01, real root
# cause of issue #312's residual -- see __init__.py's own async_unload_
# entry() comment for the full incident). A handler missing for one of
# THESE specific entity_ids means "the real SensorEntity hasn't (re-)
# registered yet" -- a genuinely transient condition during setup/unload,
# never "this entity_id will never have one" -- so ha_post_state() below
# skips its raw states.async_set() fallback for these rather than writing
# a non-restored ghost state that then collides with the real entity's
# own registration a moment later. Any OTHER entity_id (the standalone/
# cron/addon deployment, or a genuinely not-yet-migrated one) keeps
# today's exact fallback behaviour, unchanged.
_NATIVE_MANAGED_ENTITY_IDS: frozenset[str] = frozenset(
    {
        "sensor.nimbus_solver_battery_forecast",
        "sensor.nimbus_household_load_total_forecast",
        "sensor.nimbus_solver_dispatch_dry_run",
        "sensor.nimbus_mirror_temperature_forecast",
        "sensor.nimbus_mirror_humidity_forecast",
        "sensor.nimbus_solver_quality_report",
        "sensor.nimbus_efficiency_backtest",
        "sensor.nimbus_counterfactual_soc",
        # nimbus issue #1192: these three were MISSING, and the set being
        # incomplete is unambiguously a bug rather than a choice -- this
        # set's own contract, stated above, is "every entity_id this
        # integration calls register_entity_handler() for in native mode",
        # and sensor.py calls it for eleven.
        #
        # The consequence is exactly the ghost this guard exists to
        # prevent: during a reload window the real SensorEntity has not
        # re-registered, so ha_post_state() fell through to a raw
        # states.async_set() for these three, writing a NON-RESTORED state
        # that then reads as a live conflict when the real entity is added
        # a moment later. #1192's own reproduction is the flex family and
        # nothing else, which is the shape this predicts.
        #
        # `test_1192_native_managed_set_covers_every_handler.py` now
        # derives the required set from sensor.py's own
        # register_entity_handler() call sites and fails if this one falls
        # behind again, so the next entity to get a handler cannot be
        # forgotten here the way these three were.
        "sensor.nimbus_flex_signals",
        "sensor.nimbus_flex_report",
        "sensor.nimbus_offer_curve",
    }
)

# Real-entity-id side-mapping (2026-08-31, devhub flicker investigation):
# register_entity_handler() below is always called with a fixed, literal
# entity_id string (e.g. "sensor.nimbus_solver_quality_report") -- correct
# for dispatch, since ha_post_state() looks the handler up by that same
# literal string every time. But a handful of functions (publish_daily_
# quality_report / publish_efficiency_backtest_report / publish_nimbus_
# only_soc_counterfactual) ALSO read that same literal entity_id back via
# ha_get() as a cheap "did I already score this" idempotency check --
# silently wrong on any install where that literal name is already
# claimed by something else (confirmed live on devhub: a remote_
# homeassistant mirror of another Nimbus install's identically-named
# sensor wins the plain name first, so the local platform entity gets
# bumped to a "_2" suffix by HA's own dedup). ha_get() has no way to know
# about that bump -- it just reads whatever the literal string currently
# resolves to in the state machine, which is the UNRELATED mirror, not
# this install's own entity. Confirmed live: the mirror's own `latest_
# date` attribute matches "yesterday" almost every day (since it's a
# different, working install), so the idempotency check's fast path
# fires immediately every cycle, thinking it already scored today,
# without ever calling compute_daily_quality_report() again for THIS
# install -- and the local "_2" entity's own freshness stamp only gets
# refreshed on the rare cycle where the fast path happens to also
# succeed in writing something back through it, explaining the
# multi-hour stale/unavailable stretches actually observed.
#
# Fix: register_entity_handler() now optionally records the entity's own
# REAL, HA-resolved entity_id (self.entity_id, captured after async_add_
# entities -- see sensor.py's async_setup_entry) alongside the literal
# dispatch key. resolve_real_entity_id() below lets a self-read use the
# real id when one was recorded, falling back to the literal string
# unchanged for any caller/entity that never registered one (REST-only
# mode, or an entity that predates this fix) -- byte-identical behaviour
# there.
_ENTITY_REAL_IDS: dict[str, str] = {}


def set_native_hass(hass) -> None:
    """Called once by the Nimbus integration itself, before running a
    solve in-process. See this module's own "PURE INTEGRATION seam"
    comment immediately above for the full story.

    nimbus issue #347: also re-resolves LOCAL_TZ from hass.config.
    time_zone, the household's own real, already-configured timezone --
    unless NIMBUS_SOLVER_TIMEZONE was explicitly set, which always wins
    (an explicit override should never be silently replaced by a live
    re-resolution). Never raises: an unexpected shape for hass.config.
    time_zone (missing, empty, not a real IANA name) leaves LOCAL_TZ at
    whatever it already resolved to at module import time rather than
    blocking native setup over a timezone lookup.
    """
    global _NATIVE_HASS, LOCAL_TZ
    _NATIVE_HASS = hass
    if "NIMBUS_SOLVER_TIMEZONE" not in os.environ:
        # Real CI failure caught the first time this shipped: several
        # existing tests call set_native_hass() with a deliberately
        # narrow fake hass (a bare _FakeHass with no .config attribute
        # at all, or literally None) that never needed a .config before
        # this fix -- hass.config itself raises AttributeError there,
        # not just hass.config.time_zone. getattr()-based access below,
        # not a direct attribute chain, so a missing hass/.config/
        # .time_zone at ANY level degrades to None (-> the except branch)
        # instead of raising before the try/except can catch it.
        raw_time_zone = getattr(getattr(hass, "config", None), "time_zone", None)
        try:
            LOCAL_TZ = ZoneInfo(raw_time_zone)
        except Exception:
            _LOGGER.exception(
                "Nimbus: could not resolve hass.config.time_zone (%r) -- "
                "keeping the existing LOCAL_TZ (%s)",
                raw_time_zone,
                LOCAL_TZ,
            )


def register_entity_handler(
    entity_id: str, handler, real_entity_id: str | None = None
) -> None:
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

    `real_entity_id` (2026-08-31): the entity's OWN actual, HA-resolved
    entity_id (self.entity_id), captured by the caller after async_add_
    entities has run. `entity_id` above stays the literal dispatch key
    ha_post_state() below is always called with -- that part was never
    broken, since every call site uses the same literal string both to
    register and to look up. What real_entity_id fixes is the SEPARATE
    self-read some publish functions do (ha_get(entity_id), as a cheap
    "did I already publish this" idempotency check) -- see
    resolve_real_entity_id()'s own docstring for the full story. Optional
    and additive: omitting it (every pre-2026-08-31 call site, until
    sensor.py is updated) leaves resolve_real_entity_id() falling back to
    the literal string unchanged, i.e. today's exact behaviour.
    """
    _ENTITY_UPDATE_HANDLERS[entity_id] = handler
    if real_entity_id is not None:
        _ENTITY_REAL_IDS[entity_id] = real_entity_id


def unregister_entity_handler(entity_id: str) -> None:
    """Symmetric partner to register_entity_handler(); safe to call for
    an entity_id that was never registered. Called on config-entry
    unload so a torn-down entity's handler doesn't linger."""
    _ENTITY_UPDATE_HANDLERS.pop(entity_id, None)
    _ENTITY_REAL_IDS.pop(entity_id, None)


def resolve_real_entity_id(entity_id: str) -> str:
    """Resolves a literal dispatch-key entity_id (e.g.
    "sensor.nimbus_solver_quality_report") to the entity's own real,
    HA-assigned entity_id, when register_entity_handler() recorded one --
    otherwise returns entity_id unchanged (REST mode, or a caller that
    hasn't passed real_entity_id).

    Use this before any ha_get()/ha_post_state() call whose job is to
    read back THIS install's own prior output (an idempotency check) --
    never for the plain forward publish, which is correctly keyed by the
    literal dispatch string regardless of collisions (see
    register_entity_handler's own docstring).

    Why this matters (2026-08-31, devhub quality-report flicker): the
    literal string is only guaranteed to resolve to THIS entity when
    nothing else in the same HA instance has already claimed it. Confirmed
    live on devhub: a remote_homeassistant mirror of another Nimbus
    install's identically-named sensor.nimbus_solver_quality_report wins
    the plain entity_id first, so HA's own dedup bumps the local platform
    entity to sensor.nimbus_solver_quality_report_2. A self-read via the
    literal string then silently reads the UNRELATED mirror's state
    instead of this install's own -- compute_daily_quality_report()'s
    idempotency check believed "already scored today" every cycle
    (because the mirror, a different working install, genuinely had
    scored today), so this install's own quality-report entity only ever
    got a fresh publish on the rare cycle the fast path happened to
    re-push something valid through it, explaining the multi-hour stale/
    unavailable stretches observed.
    """
    return _ENTITY_REAL_IDS.get(entity_id, entity_id)


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
        headers={"Authorization": f"Bearer {_load_token()}"},
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

    nimbus issue #831: also raises that same clear RuntimeError (not a
    raw urllib.error.HTTPError leaking straight through) if the bridge
    entity itself doesn't exist yet -- confirmed live on devhub, a
    scheduled `nimbus_load.compute_quality_report` call landed during a
    window where `sensor.nimbus_solver_config` wasn't registered yet
    (the entity genuinely not existing yet during startup/reload is a
    different failure mode than "registered but not configured", and
    deserves the same actionable message, not "HTTP Error 404: Entity
    sensor.nimbus_solver_config not found" with no guidance at all).
    """
    try:
        state = ha_get("sensor.nimbus_solver_config")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        raise RuntimeError(
            "Nimbus Solver's own sensor.nimbus_solver_config entity does not "
            "exist yet (the hub may still be starting up, reloading, or "
            "hasn't been set up at all). Wait for Home Assistant to finish "
            "loading, or if this is a fresh install, add the Nimbus "
            "integration first (Settings -> Devices & Services -> Add "
            "Integration -> Nimbus)."
        ) from e
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


# nimbus issue #944: the recorder's own per-state attribute cap
# (homeassistant/components/recorder/db_schema.py::MAX_STATE_ATTRS_BYTES).
# Mirrored here rather than imported because this module must keep
# working on the standalone/cron deployment, which has no homeassistant
# package at all.
_MAX_STATE_ATTRS_BYTES = 16384

# Warn once per entity_id per process. The condition is structural -- if
# a payload is over the cap this cycle it will be over it every cycle --
# so repeating it once a minute would be exactly the noise v0.94.297 had
# to clean up for #757.
_OVERSIZE_ATTRS_WARNED: set[str] = set()


def _warn_if_attrs_exceed_recorder_cap(entity_id: str, attributes: dict) -> None:
    """Say plainly when a published payload will lose ALL of its
    attribute history (nimbus issue #944).

    `_unrecorded_attributes` is what normally keeps the big per-period
    series out of the recorder, and it CANNOT apply here. HA reads it
    from `state.state_info`, which only the entity path populates;
    `states.async_set()` and the REST API both leave it `None`. So on
    these two publish paths the whole payload is measured, and once it
    exceeds the cap the recorder drops **every attribute on the row**
    (`return b"{}"`), not merely the oversized one.

    The knock-on is what makes this worth a warning rather than a
    comment: `unit_of_measurement` goes with the rest, the statistics
    compiler then sees no unit where it previously compiled one, and
    long-term statistics for that entity are suppressed. The only thing
    in the log today is a recorder line that reads like a database
    performance note and names neither Nimbus nor the consequence.

    Best-effort throughout: a diagnostic must never be the reason a real
    publish fails.
    """
    try:
        if entity_id in _OVERSIZE_ATTRS_WARNED or not attributes:
            return
        encoded = json.dumps(attributes).encode("utf-8")
        if len(encoded) <= _MAX_STATE_ATTRS_BYTES:
            return
        _OVERSIZE_ATTRS_WARNED.add(entity_id)
        biggest = sorted(
            ((len(json.dumps(v).encode("utf-8")), k) for k, v in attributes.items()),
            reverse=True,
        )[:3]
        _LOGGER.warning(
            "Nimbus #944: %s is publishing %d bytes of attributes, over the "
            "recorder's %d byte cap, on a publish path that cannot apply "
            "_unrecorded_attributes (no state_info: REST, or the raw "
            "states.async_set fallback). The recorder will therefore drop "
            "ALL attributes for this entity -- including unit_of_measurement, "
            "which then suppresses its long-term statistics. Largest "
            "contributors: %s. The live state is unaffected; what is lost is "
            "history. Logged once per entity per run.",
            entity_id,
            len(encoded),
            _MAX_STATE_ATTRS_BYTES,
            ", ".join(f"{name}={size}B" for size, name in biggest),
        )
    except Exception:
        _LOGGER.debug(
            "Nimbus #944: could not measure the attribute payload for %s "
            "(unserialisable value). Skipping the size check for this "
            "publish -- the publish itself is unaffected.",
            entity_id,
            exc_info=True,
        )


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
        # nimbus issue #363 (Mark Purcell, codebase review): this used to
        # ALSO print() unconditionally here, on top of the _LOGGER.debug()
        # call below -- but this whole branch only ever executes when
        # _NATIVE_HASS is not None, i.e. ONLY in native (in-process HA
        # integration) mode, never the standalone/cron/addon deployment
        # the removed comment claimed to be keeping it for (that path
        # never sets _NATIVE_HASS at all). A native-mode user reads HA's
        # own logs, not this container's raw stdout, so the print() was
        # pure unconditional noise (8+ lines per cycle) with zero real
        # audience -- _LOGGER.debug() alone already gives the identical
        # trace, correctly gated by log level and visible via HA's own
        # error_log/`logger: default: debug` exactly as issue #85's own
        # original ask wanted ("_LOGGER (not print) for ha_post_state, so
        # it lands in error_log").
        _trace_msg = (
            f"#85 trace: ha_post_state entity_id={entity_id} state={state!r} "
            f"attrs_keys={sorted(attributes.keys()) if attributes else attributes} "
            f"via_handler={handler is not None}"
        )
        _LOGGER.debug(_trace_msg)
        if handler is not None:
            _NATIVE_HASS.add_job(functools.partial(handler, state, attributes))
            return
        # Real root cause of issue #312's residual (2026-09-01, see
        # _NATIVE_MANAGED_ENTITY_IDS' own comment and __init__.py's
        # async_unload_entry() for the full incident): for an entity_id
        # that DOES eventually get a native handler, a missing handler
        # right now means "not (re-)registered yet" -- during setup,
        # during a reload's unload window, or an old trigger that fired
        # in the narrow gap before its own cancellation ran -- never
        # "this will never exist." Writing a raw, non-restored state here
        # would occupy that entity_id in the state machine and make the
        # real entity's OWN registration a moment later collide with it
        # ("does not generate unique IDs... ignoring <entity_id>").
        # Skipping is safe: the real entity, once it registers, publishes
        # fresh data on its own very next solve cycle -- losing one push
        # during a setup/unload transition is a complete non-issue next
        # to a poisoned, permanently-colliding entity_id.
        if entity_id in _NATIVE_MANAGED_ENTITY_IDS:
            _LOGGER.debug(
                "Nimbus: skipping raw states.async_set fallback for %s -- "
                "no native handler registered yet (real entity is mid-"
                "setup/unload, not missing) -- this cycle's update for "
                "this entity is dropped, the next cycle will publish once "
                "the real entity has (re-)registered",
                entity_id,
            )
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
        _warn_if_attrs_exceed_recorder_cap(entity_id, attributes)
        _NATIVE_HASS.add_job(
            functools.partial(
                _NATIVE_HASS.states.async_set, entity_id, state, attributes
            )
        )
        return
    _warn_if_attrs_exceed_recorder_cap(entity_id, attributes)
    body = json.dumps({"state": state, "attributes": attributes}).encode("utf-8")
    req = urllib.request.Request(
        f"{HA_BASE}/api/states/{entity_id}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_load_token()}",
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
            "Authorization": f"Bearer {_load_token()}",
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
        except Exception:
            # nimbus issue #363 (Mark Purcell, codebase review): the swallow
            # itself is the correct, deliberate contract (see docstring) --
            # what was missing is any breadcrumb AT ALL when it fires.
            # DEBUG, not WARNING: a missing/unavailable weather-forecast
            # service call is already an accepted, routine no-op for every
            # caller here, not an operator-actionable condition on its own.
            _LOGGER.debug(
                "Nimbus Solver: ha_call_service_with_response(%s.%s) failed",
                domain,
                service,
                exc_info=True,
            )
            return None
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"{HA_BASE}/api/services/{domain}/{service}?return_response",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {_load_token()}",
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


def _fetch_weather_hourly_forecast(entity_id: str) -> list[dict] | None:
    """nimbus issue #481: the raw-fetch half of publish_weather_forecast_
    mirrors()'s own CONF_SOLVER_WEATHER_FORECAST_SENSOR reading, factored
    out so the new thermal ambient-covariate wiring (build_controllable_
    loads()'s own kind=thermal/deferrable branches, learn_thermal_rates()/
    project_temperature_forecast() callers) can source the SAME real
    forward-temperature-forecast entity the dashboard mirror already
    reads, rather than a second, potentially-diverging fetch or a
    hardcoded entity preference (the exact mistake this file's own
    publish_weather_forecast_mirrors() docstring already warns against).
    Same two accepted entity shapes (weather.* via weather.get_forecasts,
    sensor.* via its own 'forecast' attribute); returns None/empty on any
    missing config or fetch failure, same graceful no-op contract as
    every other optional external source in this file. Callers build
    their own {"time","value"} points from the raw entries -- this
    function only fetches, since different callers want different
    fields (temperature only, vs. publish_weather_forecast_mirrors()'s
    own temperature+humidity)."""
    if not entity_id:
        return None
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
            return None
        hourly = (state.get("attributes", {}) if isinstance(state, dict) else {}).get(
            "forecast"
        )
    return hourly if isinstance(hourly, list) and hourly else None


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
    hourly = _fetch_weather_hourly_forecast(entity_id)
    if not hourly:
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
    generated_at = datetime.now(UTC).astimezone(LOCAL_TZ).isoformat()
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


def publish_offer_curve(plan) -> None:
    """nimbus issue #494 (Signals 5/7 of #489): pushes sensor.nimbus_
    offer_curve when build_plan() actually computed one this cycle
    (switch.nimbus_solver_offer_curve_enabled on) -- no-op otherwise,
    same "None means not computed this cycle" convention Plan.grid_
    signals already uses for #491. Called from main() right after
    publish_plan() -- deliberately a small standalone push rather than
    threaded through publish_plan()'s own already-long parameter list,
    since this needs nothing beyond the plan itself.

    State is period 0's own real grid_import_kw -- a live, dashboard-
    visible instance of #494's own "curve at the current retail price
    equals the main plan's period-0 import" consistency check, not a
    separately-derived figure that could silently drift from it.

    `import_curve`/`export_curve` (nimbus issue #706, superseding #677's
    own dict shape): a list of self-contained band objects --
    `{"price_lower", "price_upper", "kw"}`, one per real breakpoint --
    not a `{price: kW}` dict with a SEPARATE parallel `_ranging` dict
    correlated only by a rounded 4dp price-string key. That key-based
    design is what let two distinct real sweep points that happened to
    round to the identical displayed price silently collide -- one
    kept, the other genuinely dropped, no warning strong enough to make
    that anything but real, live data loss (confirmed on this
    household's own real data, six collisions in one solve cycle, one
    of them ~0.8 kW apart -- not float noise). A list of bands has
    nothing to collide on: every real breakpoint keeps its own real
    (price range, kW) pair, always, with no dedup logic needed
    anywhere. Still price-sorted ascending (network.py's own walk
    output order, unchanged). `price_lower`/`price_upper` are `None`
    only for a band whose own ranging interval came back invalid at
    that step (see `LPResult.sweep_cost_with_ranging()`'s own
    docstring); `kw` is always the real solved value regardless.

    `price_limits` (nimbus issue #705): the real AEMO NEM domain the walk
    itself is bounded by (`network._OFFER_CURVE_DOMAIN_MIN`/`_MAX`), as
    a genuine JSON object rather than only ever living as a Python
    constant a dashboard/consumer has no way to read -- so a household
    (or a future correction, if AEMC's own multi-year MPC escalation
    moves the real cap again) can see exactly what domain this cycle's
    curve was walked against without reading this module's own source.
    """
    if plan.offer_curve_import is None or plan.offer_curve_export is None:
        return
    import_curve = _build_offer_curve_bands(
        plan.offer_curve_import, plan.offer_curve_import_ranging
    )
    export_curve = _build_offer_curve_bands(
        plan.offer_curve_export, plan.offer_curve_export_ranging
    )
    ha_post_state(
        "sensor.nimbus_offer_curve",
        round(float(plan.grid_import_kw[0]), 3),
        {
            "unit_of_measurement": "kW",
            "friendly_name": "Nimbus Offer Curve",
            "import_curve": import_curve,
            "export_curve": export_curve,
            "price_limits": {
                "market_floor_price": network._OFFER_CURVE_DOMAIN_MIN,
                "market_price_cap": network._OFFER_CURVE_DOMAIN_MAX,
                "unit": "$/kWh",
                "source": "AEMO Market Floor Price / Market Price Cap "
                "(nimbus issue #705, confirmed by Mark Purcell)",
            },
            "sweep_seconds": round(plan.offer_curve_sweep_seconds, 4)
            if plan.offer_curve_sweep_seconds is not None
            else None,
            "generated_at": datetime.now(UTC).astimezone(LOCAL_TZ).isoformat(),
        },
    )


def _build_offer_curve_bands(
    curve: list[tuple[float, float]],
    ranging: list[tuple[float, float] | None] | None,
) -> list[dict[str, float | None]]:
    """nimbus issue #706: one self-contained band object per real
    breakpoint -- see `publish_offer_curve()`'s own docstring for why
    this replaced #677's two-dicts-keyed-by-rounded-price shape.

    Adjacent EXACT duplicates (same price_lower/price_upper/kw, all
    already rounded for display) are collapsed to one row -- unlike the
    #677 collision this replaced, this loses no information: two
    already-identical-after-rounding rows are indistinguishable to any
    consumer by construction, so keeping both is pure noise, not a
    second real breakpoint. This differs from the walk's own internal
    near-duplicate raw solves (see network.py's own docstring on why a
    retail solve landing on an already-walked price is harmless) only
    in WHERE the comparison happens -- here, after rounding, on the
    values a consumer actually sees.
    """
    bands: list[dict[str, float | None]] = []
    for i, (_price, kw) in enumerate(curve):
        interval = ranging[i] if ranging is not None else None
        if interval is None:
            price_lower: float | None = None
            price_upper: float | None = None
        else:
            price_lower, price_upper = interval
            price_lower = round(price_lower, 4)
            price_upper = round(price_upper, 4)
        band: dict[str, float | None] = {
            "price_lower": price_lower,
            "price_upper": price_upper,
            "kw": round(kw, 3),
        }
        if bands and bands[-1] == band:
            continue
        # nimbus issue #730 (Mark Purcell, live finding): several
        # independent solves (a walk step, the always-separate retail
        # solve, sometimes the #705 backstop) can land in the SAME tied
        # optimal basis under a degenerate LP -- each one's own ranging
        # is real and independently correct ("how far THIS solve's own
        # basis extends"), but sorted-by-price adjacency alone doesn't
        # catch that a narrower neighbour's range is now fully swallowed
        # by a wider one, both reporting the same value. Confirmed live:
        # this exact shape self-healed on a re-fetch 22s later, so it's
        # a real, intermittent artifact of the walk, not a persistent
        # state. Generalizes the exact-duplicate collapse above to a
        # nested/subset one: same kw, one band's range entirely inside
        # the other's -- keep the wider band, drop the redundant one.
        # Never invents a boundary value neither solve actually reported
        # (unlike clipping against a neighbour would); a None bound (a
        # genuinely missing/invalid ranging interval, never a real
        # infinite one -- see this function's own docstring) is never
        # treated as unbounded here, so containment stays unknowable
        # rather than guessed whenever either side is None.
        if bands and bands[-1]["kw"] == band["kw"]:
            prev = bands[-1]
            if _offer_curve_band_range_contains(
                prev["price_lower"],
                prev["price_upper"],
                band["price_lower"],
                band["price_upper"],
            ):
                continue
            if _offer_curve_band_range_contains(
                band["price_lower"],
                band["price_upper"],
                prev["price_lower"],
                prev["price_upper"],
            ):
                bands[-1] = band
                continue
        bands.append(band)
    return bands


def publish_flex_signals(plan) -> None:
    """nimbus issue #496 (Signals 7/7 of #489): pushes sensor.nimbus_
    flex_signals when build_plan() actually computed ranging this cycle
    (switch.nimbus_solver_flex_signals_enabled on) -- no-op otherwise,
    same "None means not computed this cycle" convention publish_offer_
    curve() already uses for #494. Called from main() right after
    publish_offer_curve().

    Period-0 only, deliberately -- this is a live "what can the grid
    operator/aggregator/household call on RIGHT NOW" surface (#491's own
    framing), not the full per-period forecast array every other array-
    shaped Plan field already publishes on sensor.nimbus_solver_battery_
    forecast. `Plan.grid_signals`' own array fields are one-per-period;
    every value below is that array's own `[0]` entry.

    Per-battery (`Plan.battery_signals`) and per-load (`Plan.load_
    signals`) entries are published as plain JSON lists on this one
    sensor's own attributes, not as their own dynamically-generated
    per-battery/per-load entities -- the battery/load COUNT varies per
    install and can change at any time (a subentry added/removed), and
    generating/tearing-down real HA entities to match that live is a
    genuinely different, riskier class of change (new entity-registry
    lifecycle work) than this issue's own scope asked for. A household
    with the common 1-battery, few-load shape sees a small, well-under-
    16KB attribute list either way.
    """
    if plan.grid_signals is None:
        return
    gs = plan.grid_signals
    battery_signals = [
        {
            "name": b.name,
            "available_up_kw": round(float(b.available_up_kw[0]), 3),
            "available_down_kw": round(float(b.available_down_kw[0]), 3),
            "available_up_ranging_kw": (
                round(float(b.available_up_ranging_kw[0]), 3)
                if b.available_up_ranging_kw is not None
                else None
            ),
            "available_down_ranging_kw": (
                round(float(b.available_down_ranging_kw[0]), 3)
                if b.available_down_ranging_kw is not None
                else None
            ),
        }
        for b in plan.battery_signals
    ]
    load_signals = [
        {
            "name": ls.name,
            "intent": ls.intent[0],
            "band_min_kw": round(float(ls.band_min_kw[0]), 3),
            "band_max_kw": round(float(ls.band_max_kw[0]), 3),
            "reduced_cost_per_kwh": round(float(ls.reduced_cost_per_kwh[0]), 4),
            "degenerate": bool(ls.degenerate[0]),
        }
        for ls in plan.load_signals
    ]
    ha_post_state(
        "sensor.nimbus_flex_signals",
        round(float(gs.flex_available_up_kw[0]), 3),
        {
            "unit_of_measurement": "kW",
            "friendly_name": "Nimbus Flex Signals",
            "grid_import_headroom_kw": round(float(gs.grid_import_headroom_kw[0]), 3),
            "grid_import_headroom_kwh": round(float(gs.grid_import_headroom_kwh[0]), 3),
            "grid_export_headroom_kw": round(float(gs.grid_export_headroom_kw[0]), 3),
            "grid_export_headroom_kwh": round(float(gs.grid_export_headroom_kwh[0]), 3),
            "forced_import_cost": round(float(gs.forced_import_cost[0]), 4),
            "forced_export_cost": round(float(gs.forced_export_cost[0]), 4),
            "flex_available_up_kw": round(float(gs.flex_available_up_kw[0]), 3),
            "flex_available_down_kw": round(float(gs.flex_available_down_kw[0]), 3),
            "load_headroom_up_kwh": round(float(gs.load_headroom_up_kwh[0]), 3),
            "load_headroom_down_kwh": round(float(gs.load_headroom_down_kwh[0]), 3),
            "battery_signals": battery_signals,
            "load_signals": load_signals,
            "generated_at": datetime.now(UTC).astimezone(LOCAL_TZ).isoformat(),
        },
    )


def _offer_curve_band_range_contains(
    outer_lower: float | None,
    outer_upper: float | None,
    inner_lower: float | None,
    inner_upper: float | None,
) -> bool:
    """True when [inner_lower, inner_upper] sits entirely inside
    [outer_lower, outer_upper] -- nimbus issue #730's own nested-range
    check. A `None` bound is a genuinely missing/invalid ranging
    interval (never a real unbounded one; those already arrive as
    `float('-inf')`/`float('inf')`, which compare correctly on their
    own), so containment is never claimed when either range has one --
    unknown must never be treated as "fits inside."""
    if (
        outer_lower is None
        or outer_upper is None
        or inner_lower is None
        or inner_upper is None
    ):
        return False
    return inner_lower >= outer_lower and inner_upper <= outer_upper


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
    # nimbus issue #363 (Mark Purcell): a third-party source can genuinely
    # publish an offset-less ISO string (e.g. "2026-09-04T12:00:00", no
    # "Z"/"+00:00" suffix) -- datetime.fromisoformat() on that produces a
    # NAIVE datetime, which every real caller of this function (sorting
    # against other tz-aware timestamps in resample_forecast(),
    # .astimezone() calls, PeriodGrid arithmetic) assumes never happens.
    # Comparing a naive result against grid_times raises TypeError deep
    # inside resample_forecast() -- and fetch_solar_source_safe()'s own
    # except clause only catches HTTPError/URLError/KeyError/
    # JSONDecodeError, so this took down the ENTIRE solve cycle with a
    # traceback instead of the one source being safely dropped. Same
    # "assume UTC for a genuinely naive value" treatment the datetime
    # branch above already gets.
    parsed = datetime.fromisoformat(s)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


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
        _LOGGER.warning(
            "Nimbus Solver: %s unavailable (%s) -- treating as 0.0 kW for this solve",
            entity_id,
            e,
        )
        return None, f"{entity_id} unavailable ({e})"

    attrs = state.get("attributes", {}) if isinstance(state, dict) else {}
    fc_dicts, _has_bands, error = _validate_and_parse_load_forecast_attrs(
        entity_id, attrs
    )
    if error is not None:
        _LOGGER.warning("Nimbus Solver: %s -- treating as 0.0 kW for this solve", error)
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


# nimbus issue #1073: how much throughput the SMALLER direction needs,
# as a fraction of the larger, before a window counts as genuinely
# two-directional.
#
# Relative rather than absolute for the same reason #1072 needed the
# same change: a window with 100 kWh in and 0.01 kWh out is effectively
# one-directional, and calling it "mixed" would suppress the decisive
# reading the caller actually has. 1% is the same floor #1072 settled
# on, kept deliberately identical so there is one notion of
# "negligible throughput" in this file rather than two.
_MIXED_WINDOW_MIN_FRACTION = 0.01


# nimbus issue #1098: what fraction of a window a battery participant may
# be away for before its energy balance stops meaning anything.
#
# Mark Purcell suggested mirroring the 1% floor above and left the number
# open ("tuned to whatever fraction of away time genuinely makes the
# SoC-vs-power comparison meaningless"). 1% it is, but for a different
# reason than the sibling constant, and the difference is the point:
#
# For #1072/#1073, 1% is a NEGLIGIBILITY threshold -- a hundredth of the
# throughput genuinely cannot move the reading much. Away time has no
# such property. **Unaccounted energy is not proportional to time away.**
# A fifteen-minute DC fast-charge stop is well under 1% of a day and can
# put 30 kWh into a pack, none of it visible to this household's grid
# connection. So the floor here is not "below this it does not matter",
# it is "below this we are choosing not to cry wolf about a car that
# briefly left the driveway", and it is deliberately set as low as the
# sibling rather than at some comfortable-looking 5% or 10%.
_AWAY_WINDOW_MIN_FRACTION = 0.01


def _is_mixed_direction_window(in_kwh: float, out_kwh: float) -> bool:
    """True when BOTH directions carry real throughput, so neither
    implied efficiency can be trusted on its own (nimbus issue #1073).
    """
    smaller, larger = sorted((abs(in_kwh), abs(out_kwh)))
    if larger <= 1e-6:
        return False
    return smaller / larger >= _MIXED_WINDOW_MIN_FRACTION


# How close to its starting SoC a window must finish before energy in and
# energy out can be compared directly. 5% of throughput: on the four real
# days this was derived from, every one closed to within 1.5%, and a
# single-direction window misses it by two orders of magnitude (a
# pure-discharge day scores 107%), so the threshold is nowhere near
# anything real.
_CLOSED_LOOP_MAX_RESIDUAL_FRACTION = 0.05


def _is_closed_soc_loop(
    in_kwh: float, out_kwh: float, measured_delta_kwh: float
) -> bool:
    """True when the window began and ended at essentially the same state
    of charge, which is what makes energy in and energy out directly
    comparable (nimbus issue #1086).
    """
    throughput = abs(in_kwh) + abs(out_kwh)
    if throughput <= 1e-6:
        return False
    return abs(measured_delta_kwh) / throughput <= _CLOSED_LOOP_MAX_RESIDUAL_FRACTION


def battery_energy_balance(
    *,
    name: str,
    in_kwh: float,
    out_kwh: float,
    initial_soc_kwh: float,
    final_soc_kwh: float,
    capacity_kwh: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    away_period_count: int = 0,
    stale_period_count: int = 0,
    n_periods: int = 0,
) -> dict[str, float | str | None]:
    """Does this battery's measured energy actually reconcile with its
    measured SoC swing? (nimbus issue #1012.)

    Every kWh-based reconstruction in the scorer rests on one unstated
    assumption: that the configured efficiency, applied to the power
    sensor's readings, converts to the same energy the SoC sensor
    reports. Nothing has ever checked it. #1012 is what happens when it
    is wrong -- a ~37 point SoC-trajectory divergence that survived
    #1008's energy-total fix, and which traces to either a wrong
    `solver_efficiency_percent` or an AC/DC reference-plane mismatch in
    how the counters are interpreted.

    The check closes the loop per battery:

        modelled delta = in * charge_eff - out / discharge_eff
        measured delta = final_soc - initial_soc
        residual       = modelled - measured

    ## Why the implied efficiency is the number worth reporting

    A residual says "something is off" without saying what. The
    efficiency that WOULD close the balance says which:

    - implied ~= configured  -> the plane is consistent and the
      configured number is right; look elsewhere.
    - implied systematically HIGHER than configured -> the efficiency is
      being applied to readings that already have the loss baked in
      (a pack-side sensor read as if it were AC-side), so the loss is
      counted twice. This is #1012's leading hypothesis, and on the
      reference household the gap is 85.8% configured against ~95%
      implied.
    - implied > 1.0 -> not an efficiency at all. No real battery stores
      more than it is given; this is proof of a sign or plane error
      rather than a mis-tuned dial.

    So the implied value is a DIAGNOSIS, not just a discrepancy, and it
    is the specific figure #1012's thread has been arguing about from
    two directions without either side being able to measure it.

    ## Deliberately per-battery

    nimbus issue #949 established that fleet-blending produces a false
    signal from perfect data once more than one battery is scored. This
    is computed per battery and never blended, so it is immune to that
    by construction -- and on a fleet install it says WHICH battery
    fails to reconcile, which a blended figure structurally cannot.

    Returns None for the implied efficiency (never a fabricated number)
    when it is not computable -- no throughput to imply it from, or a
    battery that only discharged, where the charge efficiency genuinely
    does not appear in the balance.
    """
    modelled = in_kwh * charge_efficiency - out_kwh / discharge_efficiency
    measured = final_soc_kwh - initial_soc_kwh
    residual = modelled - measured
    # The single efficiency e that would satisfy
    #     in*e - out/e = measured
    # has no clean closed form when both terms are live, and inventing
    # one would hide the asymmetry rather than report it. The honest,
    # decisive case is a window with real charging: solve the charge
    # side alone, holding the measured discharge at its configured
    # value, which is exactly the "charge phase" comparison #1012 has
    # been making by hand.
    implied: float | None = None
    if in_kwh > 1e-6:
        implied = (measured + out_kwh / discharge_efficiency) / in_kwh
    away_fraction = (
        away_period_count / n_periods
        if n_periods > 0 and away_period_count > 0
        else 0.0
    )
    stale_fraction = (
        stale_period_count / n_periods
        if n_periods > 0 and stale_period_count > 0
        else 0.0
    )
    reason: str | None = None
    if away_fraction >= _AWAY_WINDOW_MIN_FRACTION:
        # nimbus issue #1098 (Mark Purcell), with real data from his own
        # install: `ev_my` away three times on 17 Sep (~4h56m total)
        # reported `implied_charge_efficiency: 1.2849` -- 128.5%,
        # physically impossible -- under the reason
        # `implied_efficiency_above_unity`, which is the label for a
        # genuine sign or reference-plane error.
        #
        # It is not one. `_resolve_battery_participant_history()`
        # deliberately ZEROES a participant's charge/discharge for every
        # period it is away (#768/#467), and correctly so: a car's
        # propulsion discharge, or a charge it took somewhere else, never
        # touches this household's grid connection and must not be priced
        # as though it did. But `initial_soc_kwh`/`final_soc_kwh` come
        # from the participant's own SoC telemetry, which reports EVERY
        # real state change, home or away.
        #
        # So the two sides of this balance are answering different
        # questions -- grid-relevant flow versus total real pack state --
        # and on an away-heavy day they were never going to reconcile.
        # Reporting that as an efficiency finding is the confident-wrong-
        # number failure this whole diagnostic exists to prevent.
        #
        # **This is checked FIRST, ahead of every other reason**, and
        # `soc_moved_without_throughput` is why that matters rather than
        # being a tidiness preference. A participant away for most of a
        # window has its throughput masked to ~zero while its SoC moves
        # freely, which is exactly that branch's trigger -- and that
        # branch asserts the participant's power sensor "failed to see
        # its dispatch", a reconstruction blind spot. Here the sensor saw
        # it fine and Nimbus masked it on purpose. Letting that fire
        # would accuse the household's hardware of a fault this code
        # introduced deliberately.
        #
        # Mark verified the sensor choice before filing, which closes off
        # the obvious alternative: `battery_participant_power_sensor` on
        # both his EVs is already the comprehensive pack-level reading
        # (driving + AC + DC charging), so no better sensor exists to
        # configure and this cannot be resolved by setup.
        reason = "participant_away_during_window"
    elif implied is None and in_kwh <= 1e-6 and out_kwh <= 1e-6 and abs(measured) > 0.5:
        # nimbus issue #1012, observed 2026-09-17 on a real participant:
        # SoC moved 6.1 kWh (10% of its capacity) across a window in
        # which its power sensor recorded NO throughput in either
        # direction. Energy cannot appear or leave without flowing, so
        # this is the participant's own power sensor failing to see its
        # dispatch -- a reconstruction blind spot, not a quiet battery.
        #
        # Named separately because "no_charge_throughput" is the label
        # for a perfectly ordinary discharge-only window, and letting
        # this share it means a real instrumentation gap reads as
        # "nothing to report". That is the same absence-is-the-only-
        # signal failure this whole diagnostic exists to catch.
        #
        # The 0.5 kWh floor keeps genuine idleness (sensor quantisation,
        # a few tenths of self-discharge) out of it.
        reason = "soc_moved_without_throughput"
    elif out_kwh > in_kwh and _is_closed_soc_loop(in_kwh, out_kwh, measured):
        # nimbus issue #1086, observed 2026-09-18 on a real scored day
        # (13 Sep): in 108.136 kWh, out 113.328 kWh, measured SoC delta
        # -0.240 kWh. The pack returned to within 0.11% of throughput of
        # where it started and delivered 5.2 kWh MORE than it received.
        #
        # That violates conservation of energy at any efficiency, so it
        # is not a statement about the battery -- it is proof that one of
        # the three inputs is incomplete. On that day it was the recorder:
        # `load_nowcast_skill_coverage` read 0.958, and the achieved
        # discharge came back 113.328 against inverter counters of ~119.3.
        #
        # Named separately because the existing reasons all describe
        # reduced CONFIDENCE in a number that is otherwise sound. This one
        # says the inputs do not add up. On 13 Sep the report published
        # `mixed_window_not_decisive_implied_above_unity` -- true, but it
        # reads as "this window cannot separate the two directions", not
        # "this day's data is missing periods", and a reader acting on the
        # former would go looking in entirely the wrong place.
        #
        # Deliberately gated on the loop closing. A window that legitimately
        # starts full and ends empty has out >> in by design and is not
        # remarkable; it is only impossible when the pack came back to
        # where it began.
        reason = "energy_out_exceeds_in_on_closed_loop"
    elif implied is None:
        reason = "no_charge_throughput"
    elif _is_mixed_direction_window(in_kwh, out_kwh):
        # nimbus issue #1073 (Mark Purcell, IV&V since #1058): on a
        # window with real throughput in BOTH directions, neither
        # implied value is decisive and the report must say so.
        #
        # Each one nets out the OTHER direction using that direction's
        # CONFIGURED efficiency -- which is exactly the unknown this
        # diagnostic exists to measure. When both configured values are
        # wrong, and #1012 measured precisely that on the reference
        # household (wrong in different directions at once), each implied
        # figure is contaminated by the other's error.
        #
        # This is the COMMON case, not an edge one: every ordinary daily
        # quality report scores a calendar day, and a real day charges
        # and discharges. Before this, those reports published two
        # confounded numbers with reason=None -- the same confidence
        # level as a genuinely one-directional window, which is the only
        # shape that can actually separate capacity from efficiency
        # (see e926a10's own commit message).
        #
        # The numbers are KEPT rather than nulled: they still bound the
        # answer and a reader who understands the caveat can use them.
        # What changes is that the caveat is now attached to them.
        reason = (
            "mixed_window_not_decisive_implied_above_unity"
            if implied > 1.0
            else "mixed_window_not_decisive"
        )
    elif implied > 1.0:
        # Physically impossible rather than merely surprising -- worth
        # naming separately so it is never read as "very efficient".
        reason = "implied_efficiency_above_unity"
    return {
        "name": name,
        "modelled_soc_delta_kwh": round(modelled, 3),
        "measured_soc_delta_kwh": round(measured, 3),
        "residual_kwh": round(residual, 3),
        # Scale-free, so one threshold reads the same on a 10 kWh home
        # pack and a 120 kWh fleet -- a 5 kWh residual means very
        # different things on those two.
        "residual_pct_of_capacity": (
            round(100.0 * residual / capacity_kwh, 2) if capacity_kwh > 0 else None
        ),
        "configured_charge_efficiency": round(charge_efficiency, 4),
        "configured_discharge_efficiency": round(discharge_efficiency, 4),
        "implied_charge_efficiency": None if implied is None else round(implied, 4),
        "implied_efficiency_reason": reason,
        # nimbus issue #1098: HOW MUCH of the window the participant was
        # away for, published rather than only used as a gate.
        #
        # The reason string says the comparison is void; this says by how
        # far, which is the difference between "the car popped out for
        # twenty minutes" and Mark's own measured day -- three trips,
        # ~4h56m, 20.6% of the window. Without it a reader who wants to
        # judge whether the number is merely caveated or entirely
        # meaningless has to go back to the availability sensor's own
        # history to find out.
        #
        # Always present (0.0 for a home battery, which is never away) so
        # a consumer never has to distinguish missing from zero.
        "participant_away_fraction": round(away_fraction, 4),
        # nimbus issue #1161: how much of the window had NO recorded
        # power behind it at all.
        #
        # Distinct from `participant_away_fraction` directly above and
        # the two must not be read as interchangeable: away means the
        # energy really did flow somewhere this household's grid never
        # saw, while stale means nobody knows whether it flowed. Away
        # explains a residual; stale says the residual is unexplainable
        # from this data.
        #
        # Always present (0.0 when every period had a real sample) so a
        # consumer never has to distinguish missing from zero.
        "unobserved_power_fraction": round(stale_fraction, 4),
        # nimbus issue #1012: the DISCHARGE-side mirror, and the reason
        # it earns its own field rather than being inferable.
        #
        # Measured on the reference household's own sensors 2026-09-17,
        # which is what motivated adding it. Let `k` be the ratio of the
        # capacity the scorer assumes to the pack's true usable
        # capacity, and `e` the true one-way efficiency. For an AC-side
        # power sensor:
        #
        #     charge:     measured / in    ==  e * k
        #     discharge:  |measured| / out ==  k / e
        #
        # A charge-only window yields the single product `e*k`, and
        # every (capacity, efficiency) pair on that hyperbola fits it
        # equally well. That degeneracy is exactly why #1012 went back
        # and forth between "the efficiency is wrong" and "the reference
        # plane is wrong" without either being settleable -- with one
        # direction they are the same measurement.
        #
        # Two one-directional windows separate them:
        #
        #     k = sqrt(rc * rd)        e = sqrt(rc / rd)
        #
        # On that install those solve to a usable capacity near 113 kWh
        # against the 119.76 assumed, and a one-way efficiency near 1.00
        # against the 0.9263 configured. Both wrong, not either.
        #
        # The joint solve deliberately is NOT done here: within one
        # scored window `measured` is a NET swing, and a window with
        # both directions cannot attribute it to either side. Computing
        # k and e from a net would produce a confident number out of a
        # quantity that does not contain the answer -- the exact failure
        # this diagnostic exists to catch. Pairing two windows is the
        # caller's job, and the issue records how.
        "implied_discharge_efficiency": (
            round(out_kwh / (in_kwh * charge_efficiency - measured), 4)
            if out_kwh > 1e-6 and (in_kwh * charge_efficiency - measured) > 1e-6
            else None
        ),
    }


def near_zero_summed_load_error(
    total_kw: list[float],
    failed_entities: list[str],
    inverter_self_consumption_kw: float = 0.0,
) -> str | None:
    """nimbus issue #933: #118's near-zero guard, for the SUMMED
    multi-circuit path. Returns the error to refuse on, or None.

    #118 established that a structurally-valid, near-all-zero load
    forecast produces a confident `optimal` solve telling a household
    the battery can export ~$46/day more than it really can, because the
    solver believes nobody is consuming anything. That guard lives in
    read_load_forecast_sensor() and protects only the SINGLE-sensor
    path. The summed path reaches the identical input state by a
    different route: each per-entity fetch failure contributes 0.0 for
    every period (correctly, individually), and enough simultaneous
    failures accumulate silently into a near-zero total.

    Not hypothetical. The reference household's own daily statistics
    show the summed sensor reaching **exactly 0.0** on five separate
    days -- and HA's statistics compiler skips non-numeric states, so
    `unavailable`/`unknown` are excluded rather than stored as zero. A
    recorded 0.0 means the sensor genuinely published 0.0, which on this
    path requires every contributing circuit to have contributed 0.0.
    An 18-circuit household cannot draw exactly nothing.

    ## Why BOTH conditions, not either alone

    The issue offered two variants and the thread converged on gating on
    their conjunction, which is strictly safer than either:

    - **`nonzero_fraction < 0.1` alone** has an onboarding trap. The
      more likely cause of those five real days was circuits not yet
      reporting during setup, and a circuit that has never produced a
      forecast fails the fetch exactly like one that is transiently
      down. A household adding circuits one at a time would see Nimbus
      refuse to plan, with no obvious reason.
    - **"every entity failed" alone** misses the partial case: 14 of 18
      down still yields a badly wrong total.

    Requiring `failed_entities` non-empty AND the total near-zero keeps
    the guard for mass failure while never firing on a household whose
    fetches all SUCCEEDED and whose load is genuinely low -- an empty
    holiday house is a real, correct near-zero, and nothing is missing
    from its data. The conjunction also sidesteps the question of
    whether failures cluster, which is what previously blocked this:
    the AND-gate is correct either way, so the clustering measurement
    became a "how often does this fire" follow-up rather than a
    precondition (Mark Purcell, 2026-09-17).

    ## The self-consumption subtlety

    `inverter_self_consumption_kw` is added to every period by
    sum_load_forecasts() AFTER the sum, so a fully-failed total does not
    arrive here as zeros -- it arrives as that constant, repeated. A
    naive `v > 0.01` test would see 100% non-zero periods on any install
    with a real self-consumption value configured and never fire at all.
    It is subtracted back out before the comparison, which is the whole
    reason this takes the parameter.
    """
    # No failure, nothing missing -- a genuinely low total is the
    # household's real data and must never be refused.
    if not failed_entities or not total_kw:
        return None
    nonzero_points = sum(
        1 for v in total_kw if (v - inverter_self_consumption_kw) > 0.01
    )
    if (nonzero_points / len(total_kw)) >= 0.1:
        return None
    return (
        f"the summed load forecast has only {nonzero_points}/"
        f"{len(total_kw)} non-trivial (>0.01 kW) points while "
        f"{len(failed_entities)} configured circuit(s) failed to fetch "
        f"({', '.join(failed_entities)}) -- a real household load "
        f"essentially never sits at true zero for 90%+ of a multi-day "
        f"forecast, so this total is missing data rather than measuring "
        f"a quiet house. Refusing to plan against it (nimbus issue "
        f"#933); this usually clears itself once those entities are "
        f"reporting again."
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


# nimbus issue #546 (Mark Purcell, real regression on his own v0.94.169
# install, found the SAME day #542/#543 shipped): the known-integration
# entity IDs fetch_open_meteo_solar_raw()/fetch_solcast_solar_raw()
# already read, hoisted to real module-level constants (previously
# duplicated as a local list inside each function) so the caller-site
# dedup logic below can check against the SAME single source of truth,
# never two lists that could silently drift apart.
_KNOWN_OPEN_METEO_SOLAR_ENTITY_IDS: frozenset[str] = frozenset(
    {
        "sensor.home_energy_production_today",
        "sensor.home_energy_production_tomorrow",
        "sensor.home_energy_production_d2",
        "sensor.home_energy_production_d3",
        "sensor.home_energy_production_d4",
        "sensor.home_energy_production_d5",
        "sensor.home_energy_production_d6",
        "sensor.home_energy_production_d7",
    }
)
_KNOWN_SOLCAST_SOLAR_ENTITY_IDS: frozenset[str] = frozenset(
    {
        "sensor.solcast_pv_forecast_forecast_today",
        "sensor.solcast_pv_forecast_forecast_tomorrow",
    }
)


def _is_known_solar_integration_entity(entity_id: str) -> bool:
    """True when `entity_id` is one of Open-Meteo Solar Forecast's or
    Solcast's own native entities -- i.e. one the auto-include path
    (fetch_open_meteo_solar_raw()/fetch_solcast_solar_raw()) already
    reads together with the REST of that integration's own entities.

    nimbus issue #546 (Mark Purcell, real regression, same day #542
    shipped): #542's own first fix taught a *configured*
    solver_solar_forecast_sensor_1/2/3 to read Solcast's/Open-Meteo's
    real shapes -- but its own dedup (an entity-level skip_entities set
    threaded into the auto-include fetchers) turned one coverage gap
    into three. Solcast's native detailedForecast entity only ever
    covers ONE day (today, or tomorrow) -- skipping just the ONE
    entity that's also configured left the auto-include fetch reading
    only the OTHER day, and resample_forecast() holds the nearest real
    point for every grid time outside a series' own native coverage
    (its own first point for times before it, its own last point for
    times after) -- so BOTH the standalone configured member (covering
    only its one native day) and the now-fragmented auto-include member
    (covering only the OTHER day) held a near-zero value (a series'
    own dawn/dusk edge point) across most of the 96h grid, and the
    unweighted blend mean dragged every day's own solar down by a
    third on Mark's real 8 Sep plan (252.8 kWh -> 172.4 kWh, same real
    forecasts). The real fix is dedup at the INTEGRATION level, not the
    entity level: when a configured source is one of a KNOWN
    integration's own entities and that integration's auto-include
    path is going to run anyway, skip the configured source as a
    standalone member entirely -- the auto-include fetch already reads
    ALL of that integration's own entities together (Solcast's real
    2-day coverage, Open-Meteo's real 8-day coverage), so it's the
    sole, correctly-covered representative for that integration,
    exactly restoring the same two-member blend structure v0.94.168
    already had (Solcast 2-day + Open-Meteo 8-day), just with Solcast's
    own shape now correctly read instead of silently dropped.
    """
    return (
        entity_id in _KNOWN_OPEN_METEO_SOLAR_ENTITY_IDS
        or entity_id in _KNOWN_SOLCAST_SOLAR_ENTITY_IDS
    )


# nimbus issue #542 item 1 (Mark Purcell, real household finding): a
# solar-forecast entity configured directly as solver_solar_forecast_
# sensor_1/2/3 was silently dropped from every solve whenever it wasn't
# already shaped as the generic forecast=[{time,value,lower,upper}]
# array every ML-produced Nimbus signal uses -- Solcast's own native
# entities publish detailedForecast=[{period_start,pv_estimate,...}]
# instead, and Open-Meteo Solar Forecast's own entities publish
# watts={timestamp: value}. This writer already knew how to read BOTH
# of those shapes (fetch_solcast_solar_raw()/fetch_open_meteo_solar_raw()
# below), just not when the household pointed a *configured* source at
# them directly rather than relying on the separate auto-include path --
# so the two readers could (and did) disagree about the exact same
# entity. One shared reshape function, used by every solar-source
# reader in this file, closes that gap for good.
def _solar_entries_from_attributes(attrs: dict) -> list[dict] | None:
    """Reshapes one solar-forecast entity's raw attributes dict into the
    standard forecast-entries list (each entry at least {"time",
    "value"}, optionally "lower"/"upper"), trying every shape this file
    already knows how to read, in priority order: the generic
    forecast=[...] array, Solcast's own detailedForecast=[...] array
    (period_start/pv_estimate/pv_estimate10/pv_estimate90 -- pv_estimate
    is already kW average power for its 30-min period, not the parent
    entity's own "kWh" unit tag, confirmed live 2026-08-22), and Open-
    Meteo Solar Forecast's own watts={timestamp: value} dict (native
    Watts, 15-min resolution -- scaled to kW here).

    Returns None when none of these attributes are present at all --a
    genuinely unrecognized shape, distinct from an HTTP/URL failure, so
    the caller can report the real reason instead of a generic
    "unavailable".
    """
    forecast = attrs.get("forecast")
    if forecast:
        return forecast
    detailed = attrs.get("detailedForecast")
    if detailed:
        return [
            {
                "time": p["period_start"],
                "value": float(p.get("pv_estimate", 0.0) or 0.0),
                "lower": float(p.get("pv_estimate10", 0.0) or 0.0),
                "upper": float(p.get("pv_estimate90", 0.0) or 0.0),
            }
            for p in detailed
        ]
    watts = attrs.get("watts")
    if watts:
        return [{"time": ts, "value": float(w) / 1000.0} for ts, w in watts.items()]
    return None


# nimbus issue #543 (Mark Purcell, real household finding): a solar
# source dropped from the blend used to warn on EVERY solve -- 205
# copies in 4 hours on one real install (three per 5-min cycle: the
# scheduled solve plus price-change solves), burying the once-per-day
# quality warning (#538) and every genuine transient in the same
# logger. Same #313/#314 "log once per condition" discipline as
# _DONE_CONDITION_WARNED/_LOAD_POWER_SENSOR_UNIT_HINT_LOGGED, keyed on
# (entity_id, reason) so a genuinely NEW failure reason for the same
# entity still gets its own one-time log. Unlike those two, THIS
# condition is worth reporting recovery from (#543's own explicit ask)
# -- a solar source coming back after being down for hours is a real,
# useful signal a household would want to see without grepping the
# log, unlike a misconfiguration that stays broken until a human fixes
# it -- so entries are cleared (not kept forever) the moment the same
# entity_id next succeeds, and that recovery is logged once at INFO.
_SOLAR_SOURCE_WARNED: set[tuple[str, str]] = set()


def _warn_solar_source_dropped_once(entity_id: str, reason: str, detail: str) -> None:
    key = (entity_id, reason)
    if key in _SOLAR_SOURCE_WARNED:
        _LOGGER.debug(
            "Nimbus: solar source %s still %s (%s) -- dropped from this "
            "solve's blend (logged once per condition, not every solve)",
            entity_id,
            reason,
            detail,
        )
        return
    _SOLAR_SOURCE_WARNED.add(key)
    _LOGGER.warning(
        "Nimbus: solar source %s %s (%s) -- dropped from this solve's "
        "blend (logged once per condition, not every solve)",
        entity_id,
        reason,
        detail,
    )


def _note_solar_source_recovered(entity_id: str) -> None:
    had_any = any(eid == entity_id for eid, _reason in _SOLAR_SOURCE_WARNED)
    if not had_any:
        return
    _SOLAR_SOURCE_WARNED.difference_update(
        {key for key in _SOLAR_SOURCE_WARNED if key[0] == entity_id}
    )
    _LOGGER.info(
        "Nimbus: solar source %s is contributing to the blend again "
        "(previously dropped)",
        entity_id,
    )


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
        # nimbus issue #374 (Mark Purcell, codebase review, #370 residual):
        # this branch used to return the identical message regardless of
        # WHY the forecast is empty -- confirmed live, that message never
        # matched _is_transient_startup_load_forecast_error()'s narrow
        # patterns, so main() treated it as a genuine, persistent
        # misconfiguration and substituted a flat 0.0kW load, publishing a
        # confidently "optimal" plan (idle battery, zero-width cost band)
        # for as long as the forecast stayed empty -- hours, for a
        # subentry whose model was discarded or never trained, not the
        # few-minutes startup race #370 fixed. Only a genuine nimbus
        # forecast sensor publishes `model_trained_at`/`training_points`
        # at all (checked via key presence, not just a falsy value, so a
        # third-party sensor that doesn't publish these keys keeps the
        # ORIGINAL "genuine misconfiguration" behaviour below unchanged --
        # this distinction only ever applies to nimbus's own sensors).
        if "model_trained_at" in attrs and (
            attrs.get("model_trained_at") is None or not attrs.get("training_points")
        ):
            return (
                None,
                False,
                (
                    f"{entity_id}'s 'forecast' attribute is present but empty "
                    f"(0 points) and it has never completed a training cycle "
                    f"yet (model_trained_at is unset) -- genuinely new, or its "
                    f"model was discarded/a retrain hasn't succeeded yet. Not "
                    f"a malformed sensor; check back once training completes."
                ),
            )
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


def _is_transient_startup_load_forecast_error(error: str) -> bool:
    """True for the specific shape of load_forecast_error that means
    "this entity genuinely exists but hasn't published real data yet"
    (a startup race -- RestoreEntity/coordinator not caught up, or a
    real fetch failure while HA itself is still starting), as opposed
    to a genuine, persistent misconfiguration.

    nimbus issue #370 (Mark Purcell, codebase review): confirmed live,
    a HA restart left sensor.nimbus_sigen_plant_total_load_power_forecast
    briefly `unavailable` with zero attributes -- read_load_forecast_
    sensor() correctly reported "no usable 'forecast' attribute (list-
    valued attributes present: none)", but main() substituted a flat
    0.0 kW load and published a confidently "optimal" plan (idle
    battery, exactly zero-width cost band) for the ~3 minutes until the
    sensor caught up. The zero-load fallback exists for a genuinely
    MISCONFIGURED sensor (see the 90%-zeros circular-reference check in
    read_load_forecast_sensor() above) -- a sensor that just hasn't
    published its first real point yet is a completely different case
    and should never be treated as "confirmed zero load."

    Deliberately narrow: only matches the shapes that specifically mean
    "no real data reached us at all" (a raw fetch failure, an entity
    with LITERALLY NO list-valued attributes -- the exact signature of a
    third-party entity restored into the state machine as unavailable
    with its attributes wiped -- or, nimbus issue #374, a nimbus forecast
    sensor whose model has genuinely never completed a training cycle).
    Does NOT match a present-but-empty forecast from an ALREADY-trained
    model (a real, ongoing misconfiguration -- e.g. a scheduling window
    excluding every current period -- worth surfacing via the
    persistent_notification below, not silently retrying forever), a
    shape/key mismatch, or the 90%-zeros circular-reference message
    (explicitly, already, a real misconfiguration diagnosis) -- all three
    keep the existing zero-fallback + notification behaviour unchanged.
    """
    return (
        "could not be read (" in error
        or "list-valued attributes present: none)" in error
        or "never completed a training cycle yet" in error
    )


def _notify_load_forecast_error_once(
    error: str, *, error_key: str | None = None
) -> None:
    """Fires a real HA persistent_notification, but only once per
    genuinely NEW error message -- an unchanging misconfiguration
    shouldn't re-notify every single cron cycle, but a DIFFERENT new
    error (e.g. the sensor started, then broke a different way) should
    still surface. Tracked via a plain sentinel file (see
    LOAD_FORECAST_ERROR_NOTIFIED_PATH's own comment) holding the exact
    last-notified message. Any failure here (can't write the sentinel,
    can't reach HA's service-call endpoint) is deliberately swallowed --
    a notification is a courtesy, never allowed to break the real solve.

    nimbus issue #416 (Mark Purcell): `error_key` (defaults to `error`
    itself for backward compatibility) is a STABLE de-dupe key, separate
    from the full message shown to the household. The 90%-zeros
    circular-reference message (read_load_forecast_sensor(), above)
    embeds the live `{nonzero_points}/{len(load_kw)}` counts -- comparing
    the FULL message meant a later cycle hitting the exact same
    underlying condition, but with a different point count, counted as a
    "new" error and re-notified, defeating the intended de-dupe. Callers
    with a message that varies cycle to cycle should pass a fixed
    `error_key` (e.g. just the entity_id + which check failed) instead.
    """
    try:
        already = ""
        if os.path.exists(LOAD_FORECAST_ERROR_NOTIFIED_PATH):
            with open(LOAD_FORECAST_ERROR_NOTIFIED_PATH, "r", encoding="utf-8") as f:
                already = f.read()
        key = error_key if error_key is not None else error
        if already == key:
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
            f.write(key)
    except Exception:
        # nimbus issue #363 (Mark Purcell, codebase review): the swallow
        # stays (a failed notification must never break the real solve),
        # but this used to have zero breadcrumb at all -- if this fires
        # repeatedly, the household never gets the intended persistent
        # notification about their own load-forecast misconfiguration,
        # which is worth being able to diagnose.
        _LOGGER.debug(
            "Nimbus Solver: _notify_load_forecast_error_once failed",
            exc_info=True,
        )


def _clear_load_forecast_error_notification_if_needed() -> None:
    """nimbus issue #416 (Mark Purcell): a notification fired by a
    genuinely transient condition (a startup-timing race producing a
    still-empty/placeholder forecast on the very first post-restart solve
    cycle -- see read_load_forecast_sensor()'s own 90%-zeros check) used
    to sit there indefinitely once the real forecast populated a few
    cycles later, since nothing ever cleared it -- a stale, wrong,
    scary-looking notification left behind with no way for the household
    to know it was already stale. Called once per cycle on the healthy
    path (load_forecast_error is None): if a notification is currently
    outstanding (the sentinel file exists), actively dismiss it and clear
    the sentinel so a genuinely NEW future error notifies again rather
    than being silently swallowed by a stale de-dupe key. Same "courtesy
    only, never breaks the real solve" swallow as
    _notify_load_forecast_error_once() above.
    """
    try:
        if not os.path.exists(LOAD_FORECAST_ERROR_NOTIFIED_PATH):
            return
        ha_call_service(
            "persistent_notification",
            "dismiss",
            {"notification_id": "nimbus_solver_load_forecast_error"},
        )
        os.remove(LOAD_FORECAST_ERROR_NOTIFIED_PATH)
    except Exception:
        _LOGGER.debug(
            "Nimbus Solver: _clear_load_forecast_error_notification_if_needed failed",
            exc_info=True,
        )


def resample_real_p2p_rate(
    grid_times: list[datetime], sensor_id: str | None = None
) -> list[float]:
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
    no *100/100 round-trip needed). Sourced from `sensor_id`
    (CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR, was hardcoded to
    sensor.localvolts_p2p_forecast until the 2026-09-02 audit -- a
    genuinely different sensor from the flat placeholder above -- this
    one carries real per-5-min matchedCost/volume/proportionP2P from
    LocalVolts' own live matching, the same real data the household's
    own reference card already uses).

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

    Returns a flat 0.0 array (never crashes) if `sensor_id` is blank --
    the same graceful no-op every household with no P2P/community-
    trading program at all gets.
    """
    if not sensor_id:
        return [0.0 for _ in grid_times]
    try:
        raw = ha_get(sensor_id)["attributes"]["forecast"]
    except Exception:
        # nimbus issue #363 (Mark Purcell, codebase review): the degrade-
        # to-flat-0.0 behaviour stays, but this used to have zero
        # breadcrumb -- a genuinely misconfigured/renamed P2P sensor would
        # otherwise silently price every period as if no P2P program
        # existed at all, with no way to tell that apart from "genuinely
        # no P2P configured."
        _LOGGER.debug(
            "Nimbus Solver: resample_real_p2p_rate(%s) failed", sensor_id, exc_info=True
        )
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
        if not (17 <= _local(gt).hour < 24):
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
#
# nimbus issue #348 (Mark Purcell, fixed 2026-09-04): the real hour
# count itself is now a wizard field (number.nimbus_solver_post_window_
# self_consume_hours, const.py's CONF_SOLVER_POST_WINDOW_SELF_CONSUME_
# HOURS) -- read via cfg inside fetch_p2p_fixed_export_kw() below, not a
# module constant, so another install's own real self-consume-switch
# timing can be genuinely different from this household's 4h. 4 stays
# here only as the literal default for an install that hasn't set the
# field, matching const.py's own DEFAULT_SOLVER_POST_WINDOW_SELF_
# CONSUME_HOURS -- byte-identical behaviour for every existing install.


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

    nimbus issue #565 (real live household observation, 8 Sep ~17:00
    P2P boundary): a P2P block's own rate is a fixed, pre-configured
    $/kWh value with no live-price dependency -- there's no real reason
    the block's own START needs to wait for the literal hour boundary
    the way a spot-price-dependent decision genuinely does. `number.
    nimbus_solver_p2p_block_lead_time_minutes` (shared across all 3
    blocks, 0 default = complete no-op) shifts each block's own
    effective START minute earlier by that many minutes, so the fixed-
    rate window can begin a minute or so before the configured hour,
    absorbing real dispatch-side lag structurally instead of relying on
    tight solve/automation timing. Only the START shifts -- the END
    stays exactly at end_hour*60, unchanged, matching the issue's own
    scope ("begin a minute or so before"). Clamped to not go below
    0 minutes into the calendar day (`max(0, ...)`) -- a block
    configured to start at hour 0 with a nonzero lead time stays a
    same-day-only comparison, same as every other block, rather than
    reaching back into the previous day's own final periods (a real but
    rare edge case, not worth the added wrap-around complexity for a
    "begin a minute early" feature).

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
    lead_time_minutes = _cfg_int(cfg, "solver_p2p_block_lead_time_minutes", 0)

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
        start_minute = max(0, start_hour * 60 - lead_time_minutes)
        blocks.append((rate_kw, start_minute, end_hour * 60))

    if not blocks:
        return None

    runs_through_midnight = any(
        end_minute == 24 * 60 for _rate_kw, _start_minute, end_minute in blocks
    )
    self_consume_hours = _cfg_int(cfg, "solver_post_window_self_consume_hours", 4)

    result: list[float] = []
    for gt in grid_times:
        gt_minute = _local(gt).hour * 60 + _local(gt).minute
        matched_rate = float("nan")
        for rate_kw, start_minute, end_minute in blocks:
            if start_minute <= gt_minute < end_minute:
                matched_rate = rate_kw
                break
        if runs_through_midnight and _local(gt).hour < self_consume_hours:
            matched_rate = 0.0
        result.append(matched_rate)
    return result


def p2p_bonus_price_by_period(
    bonus_rate: float, fixed_export_kw: list[float] | None, n_periods: int
) -> np.ndarray:
    """The settled P2P rate, offered ONLY in the periods where the
    household actually has a P2P commitment -- and zero everywhere else.

    A P2P premium is not a property of the day, it is a property of the
    BLOCK. The household's scheme settles inside their committed window
    (17:00-24:00 local here); an hour outside it earns plain spot and
    nothing more. Spreading the day's settled $/kWh flat across all 24
    hours hands the oracle money it could not have earned, and the
    oracle -- being an oracle -- spends the day arranging to collect it.

    Confirmed on real devhub data, 2026-09-17, scoring 15 Sep: with a
    flat bonus of $0.2183/kWh (the day's real $10.4032 / 47.668 kWh)
    `j_star` charged the battery at 01:00-02:00 local paying $0.215/kWh
    and dumped 20 kW to grid at 05:00 local into a spot price of
    $0.088/kWh. That is a 13c/kWh loss on its face and no optimiser
    takes it voluntarily -- it only clears because the phantom premium
    turns $0.088 into $0.306. Every dollar of that trade inflates
    `j_star`, and regret is measured against `j_star`.

    The same `grid_oracle` is reused for `j_ref` pricing (#1026), so the
    reference plane was being credited the premium too -- on 15 Sep for
    ~41 kWh of MIDDAY SOLAR export, none of it inside the window. That
    lifts `j_ref` toward `j_ach` and shrinks the numerator. Both ends of
    the EPR fraction moved, both in the direction that depresses it.

    `fixed_export_kw` is the honest window signal and it is already
    built from the household's own configured blocks by
    fetch_p2p_fixed_export_kw() -- non-zero exactly where a commitment
    exists, and explicitly 0.0 through the post-midnight self-consume
    hours where export is pinned off entirely. Reusing it means this
    gate cannot drift away from the window the LP is actually pinned to,
    which a second reading of the block hours here certainly would.

    `fixed_export_kw is None` means no block is configured at all. There
    is then no window information to gate on, so the flat rate is kept:
    that is the pre-existing behaviour and this must not make an install
    with no configured P2P block worse on the strength of a guess.
    """
    if fixed_export_kw is None:
        return np.full(n_periods, bonus_rate)
    return np.where(
        np.asarray(fixed_export_kw, dtype=np.float64) > 0.0, bonus_rate, 0.0
    )


def resolve_price_spike_override(
    cfg: dict, import_price_now: float
) -> tuple[float | None, bool]:
    """nimbus issue #567: a real-time "sell into a price spike,
    deliberately, right now" household decision. Returns
    `(effective_override_kw, spike_detected)`:

    - `spike_detected` is True whenever EITHER trigger condition holds
      (a price >= the configured threshold, OR the optional alert
      entity reads "on") -- this is the real-time visibility signal for
      the dashboard's own "$$$" indicator, published REGARDLESS of
      whether the household has armed the override. A household should
      be able to see a spike is happening before deciding to act on it.
    - `effective_override_kw` is the value that actually gets passed
      into BatteryConfig.spike_override_discharge_kw -- None unless
      EVERY one of these holds: the household has explicitly armed the
      override (switch.nimbus_solver_price_spike_override_armed), a
      spike is genuinely detected, and a real rate > 0 is configured
      (number.nimbus_solver_price_spike_discharge_kw). Human stays in
      the loop for the actual discharge decision -- arming is a
      deliberate, explicit action, not inferred from the threshold/
      alert alone.

    nimbus issue #694 (household, 10 Sep, reversing #567's own original
    design): this override now WINS over an active P2P fixed-export
    commitment rather than being exempted from it -- #567 originally
    skipped the override during P2P on the household's own "consistency
    of delivery is itself part of what earns the rate" reasoning, but
    the household's own later, explicit instruction reverses that: "i
    want it to win over p2p cos p2p will be lower... by default....
    otherwise it makes no sense" (a genuine spike is very likely to
    exceed whatever a P2P block pre-committed at, so exempting the
    override during exactly a P2P window meant it could never fire in
    the scenario it's most valuable for). P2P itself still "remains" --
    its committed rate becomes a FLOOR rather than being dropped, so it
    keeps being honestly delivered while export rises above it -- see
    network.py's own build_plan() docstring for that floor mechanism on
    grid_export[0]'s own bounds. This function no longer checks P2P
    state at all; the floor/ceiling interaction lives entirely in
    network.py now.

    threshold<=0 (the default) means "no threshold configured" -- never
    fires on its own, matching every other "0/blank means off" field in
    this project. The alert entity is optional and read defensively
    (unavailable/missing/fetch-failure all fail open to "no alert"),
    same posture as every other optional external entity read in this
    file -- a spike-alert integration having a bad moment must never
    crash a regular solve cycle.
    """
    threshold = _cfg_num(cfg, "solver_price_spike_threshold", 0.0)
    spike_by_threshold = threshold > 0 and import_price_now >= threshold

    spike_by_alert = False
    alert_entity = cfg.get("solver_price_spike_alert_entity")
    if alert_entity:
        try:
            state = ha_get(alert_entity)
            spike_by_alert = state.get("state") == "on"
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            spike_by_alert = False

    spike_detected = spike_by_threshold or spike_by_alert

    armed = bool(cfg.get("solver_price_spike_override_armed"))
    discharge_kw = _cfg_num(cfg, "solver_price_spike_discharge_kw", 0.0)
    if not (armed and spike_detected and discharge_kw > 0):
        return None, spike_detected

    return discharge_kw, spike_detected


def fetch_price_history(entity_id: str, days: int = 5) -> list[tuple[datetime, float]]:
    """Real recorded history for a single sensor's numeric state, as
    (local time, value) points -- the shared building block for
    compute_5min_offset() below. Returns [] (callers fall back further)
    if history is genuinely unavailable -- must never crash the writer.

    nimbus issue #363 (Mark Purcell, codebase review), finding 4: this
    used to duplicate ~90% of fetch_entity_history_range() below --
    same REST/native dual-mode recorder bridge, same point-building
    loop, differing only in "last N days from now" vs an explicit
    window and cosmetic details (a bare try/float() vs an explicit
    unknown/unavailable check beforehand, which skip the exact same
    values either way; start/end already being UTC-aware here makes
    fetch_entity_history_range()'s own .astimezone(UTC) call a no-op).
    Now a thin wrapper -- one real implementation, not two kept in sync
    by hand. See tests/test_fetch_price_history_and_entity_history_
    range.py for the differential coverage confirming this is
    behaviour-preserving.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return fetch_entity_history_range(entity_id, start, end)


def resolve_price_event_delta(
    cfg: dict, grid_times: list[datetime]
) -> list[float] | None:
    """The per-period $/kWh delta from an armed price-event test sensor
    (nimbus issue #1213, Mark Purcell), or None when the feature is off.

    Simulates a real NEM Market Price Cap (LOR2/LOR3, $23.20/kWh) or a
    Minimum System Load negative-price floor against the ACTUAL live
    forecast, so dispatch behaviour around such an event can be watched
    BEFORE one happens -- does the battery pre-charge ahead of the window,
    does it recognise the extreme period is coming and revise earlier
    commitments -- rather than only being explained afterwards.

    Returns None (not a list of zeros) when the feature contributes
    nothing, so the caller can skip the arithmetic entirely and a reader
    of the log can tell "off" from "armed but flat".

    Off means any of: the arm switch is off, no sensor configured, the
    sensor missing or carrying no usable `forecast`, or every resolved
    period landing at exactly 0.0.

    **Step semantics, and the one place this deliberately differs from
    every other price path here.** The `forecast` shape and the
    hold-the-most-recent-point lookup are exactly
    `resample_generic_price_forecast()`'s, which is the convention this
    repo already proved and which #1213 asks for by name. But that
    function resolves a PRICE, where holding the last value forward is
    correct. This resolves a DELTA, where holding forward means **an event
    that never ends**:

    * Before the first point -> **0.0**, never backfilled. A price series
      backfills because a price always exists; a delta before its own
      first step-point is simply absent.
    * After the last point -> held forward, same as any step function --
      which is why the household must emit an explicit `0` point at the
      end of each window to close it. That is the single real footgun in
      this feature, so a non-zero final point gets a **WARNING naming it**
      rather than being silently obeyed forever. Obeying it is still the
      correct reading of a step function; being quiet about it is not.

    Applied SYMMETRICALLY by the caller -- the same delta added to both
    import and export -- because a real wholesale price event moves the
    whole market, not one side of it.
    """
    if not bool(cfg.get("solver_price_event_enabled")):
        return None
    entity_id = cfg.get("solver_price_event_sensor")
    if not entity_id or not grid_times:
        return None

    state = ha_get(entity_id)
    if not isinstance(state, dict):
        _LOGGER.warning(
            "Nimbus #1213: price-event simulation is ARMED but its sensor "
            "%r could not be read -- no delta applied this cycle. Either "
            "point solver_price_event_sensor at a real sensor or turn "
            "switch.nimbus_solver_price_event_enabled off.",
            entity_id,
        )
        return None
    forecast = (state.get("attributes") or {}).get("forecast")
    if not isinstance(forecast, list) or not forecast:
        _LOGGER.warning(
            "Nimbus #1213: price-event simulation is ARMED but %s carries "
            "no usable `forecast` attribute (expected a list of "
            '{"time": <iso>, "value": <$/kWh>} step points) -- no delta '
            "applied this cycle.",
            entity_id,
        )
        return None

    points: list[tuple[datetime, float]] = []
    for row in forecast:
        if not isinstance(row, dict):
            continue
        raw_time = row.get("time")
        raw_value = row.get("value")
        if raw_time is None or raw_value is None:
            continue
        try:
            when = parse_iso(raw_time)
            value = float(raw_value)
        except (TypeError, ValueError, AttributeError):
            # One unparseable row must not discard a whole armed window.
            continue
        points.append((when, value))
    if not points:
        _LOGGER.warning(
            "Nimbus #1213: price-event simulation is ARMED but none of "
            "%s's %d forecast rows parsed into a (time, value) step point "
            "-- no delta applied this cycle.",
            entity_id,
            len(forecast),
        )
        return None
    points.sort(key=lambda p: p[0])

    # The footgun, named rather than obeyed silently. A step function's
    # last value holds forever, so a window that does not end with an
    # explicit 0 point distorts every future solve, not just the test.
    if abs(points[-1][1]) > 0.0:
        _LOGGER.warning(
            "Nimbus #1213: %s's LAST step point is %+.4f $/kWh at %s, not "
            "0. A step value holds forward indefinitely, so this event "
            "never closes and will keep shifting BOTH import and export "
            'prices on every future solve. Add a trailing {"time": '
            '<window end>, "value": 0} point to close the window.',
            entity_id,
            points[-1][1],
            points[-1][0].isoformat(),
        )

    deltas: list[float] = []
    idx = 0
    for when in grid_times:
        # Advance to the last point at or before this period.
        while idx + 1 < len(points) and points[idx + 1][0] <= when:
            idx += 1
        if points[idx][0] > when:
            # Before the first step point -- absent, not backfilled.
            deltas.append(0.0)
        else:
            deltas.append(points[idx][1])

    if not any(abs(d) > 0.0 for d in deltas):
        _LOGGER.debug(
            "Nimbus #1213: price-event simulation armed, but %s's own "
            "windows do not overlap this solve's horizon -- no delta "
            "applied (a clean no-op, not a fault).",
            entity_id,
        )
        return None

    affected = sum(1 for d in deltas if abs(d) > 0.0)
    _LOGGER.warning(
        "Nimbus #1213: price-event simulation ACTIVE from %s -- %+.4f to "
        "%+.4f $/kWh applied to BOTH import and export across %d of %d "
        "periods. This is a what-if test, not a real market price: the "
        "plan, the offer curve and every published cost this cycle reflect "
        "the simulated event. Turn "
        "switch.nimbus_solver_price_event_enabled off to return to real "
        "prices.",
        entity_id,
        min(deltas),
        max(deltas),
        affected,
        len(deltas),
    )
    return deltas


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
    needed. This household's own has_price_forecast_array branch never reaches
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
    except Exception:
        # nimbus issue #363 (Mark Purcell, codebase review): fallback
        # stays, breadcrumb added.
        _LOGGER.debug(
            "Nimbus Solver: resample_generic_price_forecast_with_coverage(%s) failed",
            entity_id,
            exc_info=True,
        )
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
) -> tuple[list[float], NDArray[np.float64] | None, list[str]]:
    """Optional second/third price source blending (2026-08-25, direct
    household ask: "u also are missing my blended price forecasts...
    in case we can feed it more than one... e.g. aemo... and amber").

    `primary` is whatever spot_import_raw/spot_export either the
    has_price_forecast_array or generic branch above already produced -- this
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

    PRIMARY-PREFERRING (2026-08-27, nimbus repo issue #239, following
    #236: "50/50 price-blend inflates export_price... producing
    simultaneous 22kW grid import + 30kW grid export"). Previously, once
    coverage-awareness landed for #216, a period where BOTH the primary
    and a secondary had real coverage still averaged them 50/50 -- fine
    when the two sources roughly agree, but a real Amber Express tick
    and a real QLD1 PD7DAY forecast can legitimately disagree by tens of
    cents on the same instant (they're not independent estimators of the
    same thing, see #239's own market-structure argument), and averaging
    them can fabricate a price no counterparty will ever settle at --
    confirmed live inverting the import/export relationship and causing
    an unphysical simultaneous-import-and-export LP plan.

    Now the primary (via `primary_real_mask` -- callers without one
    default to "always real", i.e. always wins) is used UNBLENDED
    whenever it has real coverage at a period, full stop -- never
    averaged with a secondary just because one happens to be live too.
    A secondary only ever contributes where the PRIMARY lacks real
    coverage (extending the horizon past Amber's own ~24h reach, its one
    genuine job): a period where only a secondary has real data passes
    that source through unchanged (still matches Mark's own
    clear-`_sensor_2` finding from #216); the rare period where NOTHING
    has real coverage anywhere still falls back to the old equal-weight
    blend across every source's own held-flat value (the honest
    "everyone's guessing" case) rather than an arbitrary pick.

    Returns `(primary, None, ["primary"] * len(primary))` completely
    unchanged whenever no secondary source is configured OR configured
    but currently unavailable -- a single-source install (the
    overwhelming majority today) is byte-identical to before this
    function existed (aside from the trivial, always-"primary" label
    list, new in #631).

    Note on period 0 (2026-08-27, nimbus repo issue #220, Mark Purcell:
    "Settled prices must not be blended"): grid_times[0] is always "now"
    by construction (build_tiered_grid()), and every configured price
    source publishes a SETTLED, contractual value for that block, not
    an estimate -- there is no valid blending weight for it other than
    1.0 on the primary. This function still runs its normal coverage
    logic on period 0 like any other (it's a general-purpose blend, not
    aware of which index is "now"); the caller (main()) is responsible
    for re-asserting the settled primary value at index 0 AFTER calling
    this, which is simpler and keeps this function's own contract
    (and its existing direct unit tests) unchanged. The caller must
    likewise force this function's own `source_labels[0]` back to
    "primary" after that re-assertion, for the same reason.

    nimbus issue #631 (Mark Purcell, live finding: a single 71.3 kWh
    charge block committed 20 hours ahead on a secondary source's own
    price, with nothing in the published plan distinguishing "known
    from the retailer's own near-term forecast" from "extrapolated from
    a weekly tariff table" -- `import_price`/`import_price_raw` were
    byte-identical in every row regardless of which source actually won
    that period). The third return value is a per-period label,
    `"primary"`/`"secondary"`/`"fallback"`, naming EXACTLY which branch
    of the same decision this function already makes for `blended[i]`
    produced that period's own value -- not a second, independently
    reasoned classification that could ever drift out of sync with the
    real blend decision above it.
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
        return primary, None, ["primary"] * len(primary)
    n = len(primary)
    blended = np.empty(n, dtype=float)
    source_labels: list[str] = []
    for i in range(n):
        # PRIMARY-PREFERRING (2026-08-27, nimbus repo issue #239, Mark
        # Purcell, following his own #236 report): whenever the primary
        # source has real coverage at this period, use it UNBLENDED --
        # never averaged against a secondary. Mark's own market-structure
        # argument (see #236's reopen-adjacent comment) is why this isn't
        # a preference call: on any real Australian NEM tariff, import
        # and export both derive from the SAME underlying AEMO spot price
        # for that (region, interval) -- they're not two independent
        # estimators that can legitimately disagree while both are live.
        # A genuine price-cap/spike event shows up as a real tick on the
        # PRIMARY sensor itself (e.g. Amber's own feed-in price hitting
        # $17.50/kWh) -- it doesn't need a secondary (a generic day-ahead
        # AEMO forecast) averaged in to "catch" it, and doing so only
        # ever dilutes/corrupts a real primary tick with a same-instant
        # forecast that isn't what any counterparty will actually settle
        # at. Confirmed exactly this way live: a real Amber value
        # (-$0.0037) averaged 50/50 against a real QLD1 PD7DAY forecast
        # (+$0.6701) inverted the import/export price relationship and
        # the LP planned simultaneous 22kW import + 30kW export as a
        # result.
        #
        # The secondary's own real, legitimate job -- extending the
        # horizon past the primary's own real coverage (Amber's ~24h vs
        # PD7DAY's 7 days) -- is completely unaffected: this only changes
        # behaviour for periods where BOTH the primary and a secondary
        # are real at the same instant (previously averaged, now primary
        # wins outright). Periods where the primary lacks real coverage
        # yet still fall through to blending whichever secondaries ARE
        # real there (unchanged), and periods where NOTHING has real
        # coverage anywhere still fall back to the old equal-weight mean
        # across every source's own held-flat value (unchanged) -- this
        # is the same "everyone's guessing" honest fallback the coverage-
        # aware rewrite for #216 already established, just no longer
        # reached when the primary alone would have been sufficient.
        if real_masks[0][i]:
            blended[i] = sources[0][i]
            source_labels.append("primary")
            continue
        secondary_real_idx = [j for j in range(1, len(sources)) if real_masks[j][i]]
        idx = secondary_real_idx if secondary_real_idx else list(range(len(sources)))
        blended[i] = float(np.mean([sources[j][i] for j in idx]))
        source_labels.append("secondary" if secondary_real_idx else "fallback")
    return list(blended), cross_source_spread(sources), source_labels


def fetch_aemo_forecast(
    sensor_id: str | None = None,
) -> list[tuple[datetime, float]]:
    """Real, FORWARD-looking AEMO NEM spot price forecast (a
    purcell-lab/nem_pd7day-shaped sensor) -- covers the FULL 96h horizon
    on a real QLD1 install (confirmed live 2026-08-16: 367 real 30-min
    points, now -> +7.6 days), unlike LocalVolts (~24h real) or Amber
    (~23.7h real, confirmed via
    sensor.amber_express_116kathouse_forecast_horizon itself, not
    guessed). This is the only real price source on this system with
    genuine coverage all the way to the end of a 96h horizon, so it's
    the anchor for anything beyond LV's own real coverage.

    `sensor_id` is CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR (const.py's
    own comment has the full real-bug-audit story, 2026-09-02): the
    same NEM-region-forecast CONCEPT is genuinely useful outside QLD1
    (any Australian state running the same nem_pd7day integration for
    their own region), but the entity name itself is per-region --
    hardcoding QLD1 made this permanently unreachable for anyone else.
    None/blank (the default, and the only behaviour for a market with
    no NEM-equivalent forecast at all) hits the exact same "unavailable"
    fallback below as it always has.

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
    if not sensor_id:
        return []
    try:
        fc = ha_get(sensor_id)["attributes"]["forecast"]
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
    real_history: list[tuple[datetime, float]],
    days: int = 5,
    regional_spot_sensor: str | None = None,
) -> dict[int, float]:
    """Real, empirical (retail price - regional wholesale spot) offset,
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
    RECORDED history (`regional_spot_sensor` -- CONF_SOLVER_REGIONAL_
    SPOT_CURRENT_PRICE_SENSOR, was hardcoded to this household's own
    sensor.aemo_nem_qld1_current_5min_period_price until the 2026-09-02
    audit -- vs sensor.costsflexup/earningsflexup, both genuinely 5-min
    resolution) binned at 5-min-of-day: every one of the 12 buckets
    inside that same 16:00-17:00 hour is now tight across all 5 real
    days (e.g. 16:00: 0.2267-0.2710) -- confirms the earlier hourly
    smear was a single-snapshot artifact, and that this household's
    real retail markup pattern genuinely has fine, repeatable 5-min-
    level structure worth capturing rather than averaging away.

    Falls back to an empty dict (caller then falls back further) if
    `regional_spot_sensor` is blank, or either side's real history is
    unavailable -- must never crash the writer.
    """
    if not regional_spot_sensor:
        return {}
    aemo_history = fetch_price_history(regional_spot_sensor, days=days)
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
        bucket = _local(t).hour * 12 + _local(t).minute // 5
        by_bucket.setdefault(bucket, []).append(real_v - aemo_v)
    return {b: sum(vals) / len(vals) for b, vals in by_bucket.items()}


def check_aemo_p5min_disagreement(
    regional_spot_sensor: str | None,
    retail_price_now: float,
    bucket_5min: int,
    offset_by_5min: dict[int, float],
    threshold_dollars: float,
) -> dict | None:
    """nimbus issue #452 (Mark Purcell): cross-check the current
    interval's real retail commodity price against AEMO's own live
    P5MIN wholesale price (`regional_spot_sensor` -- CONF_SOLVER_
    REGIONAL_SPOT_CURRENT_PRICE_SENSOR, already the exact entity this
    check needs -- confirmed live against a real household's own
    `sensor.aemo_nem_qld1_current_5min_period_price`, no new sensor
    field required).

    A raw retail-vs-wholesale difference is EXPECTED (network fees,
    retailer margin) -- comparing against zero would flag every single
    period. What's genuinely anomalous is a difference from the
    household's OWN typical same-time-of-day markup, which compute_
    5min_offset() above already computes as a real, bucketed empirical
    mean from recorded history. This reuses that same `offset_by_5min`
    dict (already computed by every caller of this function for a
    different purpose -- see main()'s own call site) rather than
    re-deriving a second notion of "expected offset".

    Deliberately scoped to the CURRENT interval only, not "current +
    next" as #452's own title suggests -- the only two real AEMO
    entities confirmed live for this (Mark Purcell, 2026-09-13) are a
    genuine 5-min CURRENT price and a 30-min-bucketed FORWARD forecast,
    neither of which is a true "next 5-min interval" P5MIN value; the
    current-interval check alone already covers this issue's own core
    motivation ("exactly the window where a dispatch decision is most
    consequential"). A next-interval check is real, honest follow-up
    work, not silently dropped -- see #452's own tracking comment.

    Returns None (no flag, no data to check) when `regional_spot_sensor`
    is blank, this bucket has no offset history yet (compute_5min_
    offset()'s own empty-dict/missing-bucket case), or the live AEMO
    read fails/is non-numeric (`unavailable`/`unknown` state, entity
    gone) -- never raises, matches this file's "must never break the
    real solve" convention throughout (see ha_get()'s own callers
    elsewhere in this file for the same defensive pattern).
    """
    if not regional_spot_sensor:
        return None
    expected_offset = offset_by_5min.get(bucket_5min)
    if expected_offset is None:
        return None
    try:
        aemo_now = float(ha_get(regional_spot_sensor)["state"])
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return None
    expected_retail = aemo_now + expected_offset
    disagreement = retail_price_now - expected_retail
    return {
        "aemo_p5min_now": round(aemo_now, 4),
        "retail_now": round(retail_price_now, 4),
        "expected_retail": round(expected_retail, 4),
        "disagreement_dollars": round(disagreement, 4),
        "threshold_dollars": round(threshold_dollars, 4),
        "flagged": abs(disagreement) > threshold_dollars,
    }


# nimbus issue #452: log-once-per-PERIOD discipline (not the open-ended
# log-once-per-condition-until-recovery pattern _ENVELOPE_LIMIT_WARNED
# uses above) -- a genuine price disagreement is a fresh, time-varying
# event every 5-min bucket, not a persistent degraded state, so it must
# re-fire each time "now" rolls into a new flagged period rather than
# staying silent forever after the first one. A single remembered
# timestamp (not a growing set) is enough: solves re-run every ~1 minute
# on the SAME period far more often than periods actually change.
_AEMO_P5MIN_LAST_WARNED_PERIOD: datetime | None = None


def _warn_aemo_p5min_disagreement_once(period_start: datetime, detail: dict) -> None:
    global _AEMO_P5MIN_LAST_WARNED_PERIOD
    if _AEMO_P5MIN_LAST_WARNED_PERIOD == period_start:
        return
    _AEMO_P5MIN_LAST_WARNED_PERIOD = period_start
    _LOGGER.warning(
        "Nimbus #452: current-interval retail price ($%.4f/kWh) disagrees "
        "with AEMO P5MIN ($%.4f/kWh, expected ~$%.4f/kWh given this time-"
        "of-day's normal retail markup) by $%.4f/kWh, beyond the "
        "configured $%.4f/kWh threshold (logged once per period)",
        detail["retail_now"],
        detail["aemo_p5min_now"],
        detail["expected_retail"],
        detail["disagreement_dollars"],
        detail["threshold_dollars"],
    )


# nimbus issue #452: same log-once-per-PERIOD discipline as the
# current-interval check above -- a forecast/actuals divergence is a
# fresh event each window, not a persistent degraded state, so it must
# re-fire when "now" rolls into a new period rather than going silent
# forever after the first one.
_AEMO_FC_LAST_WARNED_PERIOD: datetime | None = None


def _warn_aemo_forecast_vs_actuals_once(period_start: datetime, detail: dict) -> None:
    global _AEMO_FC_LAST_WARNED_PERIOD
    if _AEMO_FC_LAST_WARNED_PERIOD == period_start:
        return
    _AEMO_FC_LAST_WARNED_PERIOD = period_start
    _LOGGER.warning(
        "Nimbus #452: AEMO's own 30-min forecast ($%.4f/kWh) for %s-%s "
        "disagrees with its own realised 5-min prices so far ($%.4f/kWh "
        "from %d sample(s)) by $%.4f/kWh, beyond the configured "
        "$%.4f/kWh threshold (logged once per period)",
        detail["forecast_price"],
        detail["window_start"],
        detail["window_end"],
        detail["realised_mean_price"],
        detail["n_samples"],
        detail["disagreement_dollars"],
        detail["threshold_dollars"],
    )


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
        bucket = _local(t).hour * 12 + _local(t).minute // 5
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
        bucket = _local(gt).hour * 12 + _local(gt).minute // 5
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
            bucket = _local(gt).hour * 12 + _local(gt).minute // 5
            out.append(float(aemo_v + offset_by_5min.get(bucket, 0.0)))
        else:
            out.append(float(last_real_value))
        real_mask.append(False)
    return out, real_mask


def build_tiered_grid(now: datetime) -> tuple[list[datetime], list[float]]:
    """Real wall-clock period boundaries for the tiered horizon (see the
    TIER1_*/TIER2_* constants' own comment for why this exists).

    Tier 0 (1-min, first 5 real minutes) needs no bridge into tier 1
    (5-min): both are far finer than any real TOU rate boundary (which
    only ever changes on the hour) or NEM trading-interval boundary
    (:00/:30), so there's no alignment concern the way tier1->tier2 has
    -- 5-min periods stack cleanly against 5-min periods regardless of
    exactly where tier0's own 5 minutes happened to end.

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
    principle already applied to tier 1's own end boundary below.

    nimbus issue #451 (Mark Purcell): tier 1's own END used to be a
    fixed 24h after tier1_start, then tier 2 snapped to the next real
    HOUR boundary. Both replaced: tier1_end now snaps to the end of the
    real NEXT NEM trading interval (:00/:30) after the one containing
    tier1_start -- i.e. tier 1 covers exactly the CURRENT + NEXT real
    30-min trading interval, never more, since that's the genuine limit
    of what LocalVolts/AEMO's own 5-min-resolution price data actually
    covers (see the TRADING_INTERVAL_MINUTES/MAX_TIER1_HOURS constants'
    own comment). Because tier1_end is now itself always a clean :00/:30
    mark (TIER1_PERIOD_HOURS=5min divides evenly into it), tier 2 starts
    exactly on a real trading-interval boundary by construction -- no
    separate bridging period is needed the way the old hour-snapped
    tier1->tier2 boundary required one. TOU rate boundaries (real Peak/
    Off-peak/Shoulder switches, which only ever change on the hour) stay
    correctly aligned too, since every real hour mark is also a :00
    trading-interval mark.
    """
    times: list[datetime] = []
    hours: list[float] = []
    minute_start = now.replace(second=0, microsecond=0)
    if minute_start < now:
        minute_start += timedelta(minutes=1)
    tier1_start = minute_start
    while _local(tier1_start).minute % 5 != 0:
        tier1_start += timedelta(minutes=1)
    t = minute_start
    while t < tier1_start:
        times.append(t)
        hours.append(TIER0_PERIOD_MINUTES / 60.0)
        t += timedelta(minutes=TIER0_PERIOD_MINUTES)
    # End of the trading interval CONTAINING tier1_start, then extended
    # by one more full interval to cover "current + next" -- the `<=`
    # (not `<`) on the first loop guard means a tier1_start that already
    # sits exactly on a :00/:30 mark still advances to the FOLLOWING
    # boundary first (it's the start of its own interval, not the end),
    # before the same one-interval extension applies uniformly either way.
    tier1_end = tier1_start.replace(second=0, microsecond=0)
    while tier1_end <= tier1_start or (
        _local(tier1_end).minute % TRADING_INTERVAL_MINUTES != 0
    ):
        tier1_end += timedelta(minutes=1)
    tier1_end += timedelta(minutes=TRADING_INTERVAL_MINUTES)
    t = tier1_start
    while t < tier1_end:
        times.append(t)
        hours.append(TIER1_PERIOD_HOURS)
        t += timedelta(hours=TIER1_PERIOD_HOURS)
    t = tier1_end
    horizon_end = tier1_start + timedelta(hours=TOTAL_HORIZON_HOURS)
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
        battery_charge_kw = np.array(data["battery_charge_kw"])
        battery_discharge_kw = np.array(data["battery_discharge_kw"])
        # nimbus issue #467: reconstruct a single-entry Plan.batteries
        # too, so network.py's own per-participant cross-solve stability
        # (matched by name) keeps working across a restart -- this
        # writer only ever solves one real battery, so its aggregate
        # arrays above ARE that one battery's own arrays. A state file
        # saved before #467 (no "battery_name" key) falls back to
        # "home", matching the real BatteryConfig name this writer uses.
        battery_name = data.get("battery_name", "home")
        return network.Plan(
            status="optimal",
            periods=periods,
            battery_charge_kw=battery_charge_kw,
            battery_discharge_kw=battery_discharge_kw,
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
            # nimbus issue #785 (real regression from #781's own fix):
            # Plan.soc_penalty_cost is a required field -- this
            # reconstructed-from-cache Plan is only ever used for
            # build_plan()'s own previous_plan= stability mechanisms
            # (proximal/rate-limit matching), none of which read
            # soc_penalty_cost, so 0.0 is a genuine, safe no-op here,
            # same posture as the zero-filled arrays just above.
            soc_penalty_cost=0.0,
            iterations=0,
            batteries=[
                network.BatteryPlan(
                    name=battery_name,
                    charge_kw=battery_charge_kw,
                    discharge_kw=battery_discharge_kw,
                    soc_kwh=np.zeros(n),
                )
            ],
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
                    # nimbus issue #467: this writer only ever solves one
                    # real battery ("home", see battery_cfg's own
                    # comment above) -- persisting its own name alongside
                    # the (already single-battery) aggregate arrays above
                    # is enough for load_previous_plan() to reconstruct a
                    # real Plan.batteries entry that keeps per-participant
                    # cross-solve stability (network.py's own #467
                    # plumbing) working across a restart, not just the
                    # pre-#467 aggregate-only continuity.
                    "battery_name": plan.batteries[0].name
                    if plan.batteries
                    else "home",
                },
                f,
            )
    except OSError as e:
        _LOGGER.warning(
            "Nimbus Solver: could not save plan state (%s) -- next run will "
            "solve without stability continuity",
            e,
        )


def p2p_match_fraction(
    recent_days: int = 5, settlement_sensor: str | None = None
) -> float:
    """Real, empirical fraction of exported energy during the P2P window
    that actually gets matched at the P2P rate (vs reverting to the much
    lower spot rate) -- averaged over the most recent `recent_days` REAL
    SETTLED days from `settlement_sensor` (CONF_SOLVER_P2P_SETTLEMENT_
    HISTORY_SENSOR -- already documented in const.py as retailer-
    agnostic in shape; LocalVolts' own sensor.lv_v2_p2p_confirmed_history,
    the same safe REST-pushed mechanism lv_p2p_daily_recalibrate.py
    already proved out, just happens to be this household's real value
    for it. This function itself was hardcoded to that literal until the
    2026-09-02 audit -- the field already existed and was already wired
    into compute_daily_quality_report(), just never threaded through
    here too).

    Direct, real finding (2026-08-16): assuming 100% match (this
    project's old flat-$0.50 placeholder, PR #308) overstated a real
    day's P2P revenue by ~$10 against LocalVolts' own settled Cashflow
    Breakdown -- confirmed 78% match that specific day. The recent-5-day
    average (not all-time) is used deliberately: the real match rate has
    been genuinely DECLINING (13-day avg 0.686 vs last-5-day avg 0.599,
    computed live 2026-08-16), so the more recent window is the more
    accurate estimate of what's likely to happen tonight, not a stale
    long-run average.

    Falls back to P2P_MATCH_FRACTION_FALLBACK if `settlement_sensor` is
    blank, or the sensor/its history is unavailable for any reason --
    this writer must never crash outright over a secondary accuracy
    refinement.
    """
    if not settlement_sensor:
        return P2P_MATCH_FRACTION_FALLBACK
    try:
        hist = ha_get(settlement_sensor)["attributes"]["history"]
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


def p2p_recent_avg_volume_kwh(
    recent_days: int = 5, settlement_sensor: str | None = None
) -> float:
    """Real, empirical AVERAGE ABSOLUTE kWh of export that gets P2P-matched
    per night -- averaged over the most recent `recent_days` REAL SETTLED
    days, same source (`settlement_sensor`, see p2p_match_fraction()'s
    own docstring for the full field/audit story) and same recency
    reasoning as p2p_match_fraction() above.

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

    Falls back to P2P_RECENT_AVG_VOLUME_FALLBACK_KWH if `settlement_sensor`
    is blank, or the sensor/its history is unavailable for any reason --
    this writer must never crash outright over a secondary accuracy
    refinement.
    """
    if not settlement_sensor:
        return P2P_RECENT_AVG_VOLUME_FALLBACK_KWH
    try:
        hist = ha_get(settlement_sensor)["attributes"]["history"]
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


# nimbus issue #757: the process-local half of the overlap guard.
#
# LOCK_PATH below is a PID file, which is exactly right for the
# standalone/cron script -- two runs there genuinely are two
# processes. In native mode every solve runs in the SAME hass
# process, on different executor worker threads, so the PID in that
# file is always our own -- and acquire_lock()'s own #346 branch
# (`if old_pid == os.getpid()`) therefore reads a genuine concurrent
# solve as a stale file and hands out the lock. Measured directly
# against the real function before this existed:
#
#     thread A acquires: True
#     thread B acquires WHILE A HOLDS IT: True
#
# So in native mode the overlap guard had never refused anything,
# and solver_runtime.py's own #315 'previous cycle still in
# progress' WARNING was unreachable code. That is the concurrency
# half of #757: concurrent solves, each publishing over the last,
# which is why ten investigations of that issue disagreed with each
# other about whether a battery participant was being excluded.
#
# Both mechanisms are kept because they answer genuinely different
# questions and neither substitutes for the other. A PID file cannot
# see a sibling thread; a process-local lock cannot see a sibling
# process. #346's branch stays exactly as it was -- it is still the
# only thing that reclaims a lock file left behind by a worker
# thread killed mid-solve, which in a container frequently holds a
# PID identical to ours after a restart.
_IN_PROCESS_LOCK = threading.Lock()


def acquire_lock() -> bool:
    """Two-mechanism overlap guard. Returns True (caller should
    proceed) if no other run is genuinely still active; False (caller
    should exit cleanly, no error) if one is.

    A process-local threading.Lock covers a concurrent solve on
    another worker thread of THIS process (native mode -- see
    _IN_PROCESS_LOCK's own comment above for the #757 defect that
    exists to fix, and why a PID file structurally cannot see it).
    The PID file below covers a genuinely separate process (the
    standalone/cron script) -- 2026-08-17, see LOCK_PATH's own
    comment; makes a genuine 1-minute cron cadence safe against the
    real, measured 45-52s solve time without needing a slower, more
    conservative interval "just in case".

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
    if not _IN_PROCESS_LOCK.acquire(blocking=False):
        # nimbus issue #757: a genuine concurrent solve on another
        # worker thread of THIS process -- the one case the PID file
        # below structurally cannot see. Non-blocking deliberately:
        # parking an HA executor worker for the whole of another
        # solve would be worse than the bug, and is exactly the
        # executor starvation #773 documents. The caller's contract
        # is unchanged -- False still means 'skip this tick
        # cleanly', and solver_runtime.py already logs it.
        return False
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                old_pid = int(f.read().strip())
            # nimbus issue #346 (Mark Purcell): in native mode this file
            # holds HA's OWN pid, not a genuinely separate process's --
            # `solver_runtime.py`'s own driver calls this in-process, on a
            # worker thread of the same `hass` process, every cycle. A
            # worker thread mid-LP-solve when HA is stopped/killed is not
            # guaranteed to reach this function's own `release_lock()`
            # (called from solver_runtime.py's `finally:`), so the file
            # can be left behind holding this same process's own PID. In
            # a Docker/HAOS container that PID is frequently identical
            # across restarts (PID 1, or close to it) -- without this
            # check, `os.kill(old_pid, 0)` genuinely succeeds (it's us),
            # every single tick returns False forever, and nothing ever
            # deletes the stale file on its own. A PID that IS our own
            # can never indicate a real overlapping run (we are, by
            # definition, not currently blocked acquiring this lock).
            if old_pid == os.getpid():
                pass  # stale file from an unclean stop -- safe to reclaim
            else:
                os.kill(old_pid, 0)  # raises if that PID isn't real; sends no signal
                # Hand the process-local lock straight back: this run
                # is not proceeding, and holding it would refuse every
                # FUTURE tick on this install forever, long after the
                # other process is gone (nimbus issue #757).
                _IN_PROCESS_LOCK.release()
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
    finally:
        # nimbus issue #757: in a `finally:` so a failure to remove
        # the PID file can never strand the process-local lock and
        # wedge every subsequent solve. RuntimeError is the
        # already-unlocked case -- release_lock() is itself called
        # from a `finally:` and must never be the thing that raises.
        try:
            _IN_PROCESS_LOCK.release()
        except RuntimeError:
            pass


QUALITY_ENTITY_ID = "sensor.nimbus_solver_quality_report"


# nimbus issue #984: how much of a full-day window the recorder must
# actually have returned before that day may be scored.
#
# 0.9 rather than 1.0 on purpose. Real installs drop samples -- a sensor
# goes unavailable for a few minutes, an integration reloads -- and a
# day with a 20-minute gap is still a perfectly honest day to score.
# What this has to catch is the qualitatively different case: a window
# that came back half-empty because the recorder was mid-purge, which
# produced figures roughly HALF the real ones on a live install.
_MIN_DAILY_COVERAGE_FRACTION = 0.9

# nimbus issue #1054: how many times the SAME day may be skipped for
# thin coverage before the skip stops being logged as routine INFO and
# starts being a WARNING.
#
# The INFO level was right for what #984 expected to catch -- the
# recorder still catching up just after midnight, which resolves itself
# on the next cycle and should not page anyone. It was wrong for what
# actually happened on the production install: the same day skipped
# across 5+ consecutive solves, `sensor.nimbus_solver_quality_report`
# sitting at `unknown` for a day and a half, and NOTHING at the default
# log level saying why. The reason was only recoverable by raising the
# logger to INFO by hand and re-running `solve_now`.
#
# Three is deliberately past "the recorder is catching up" (that clears
# on the first or second retry) and well short of spamming a real
# outage, and the day key means a genuinely broken day warns once per
# cycle rather than every scored day re-arming it.
_COVERAGE_SKIP_WARN_AFTER = 3
_COVERAGE_SKIP_COUNTS: dict[str, int] = {}

# nimbus issue #1214: the same escalating-log idiom, for a configured SoC
# sensor whose recorder read came back empty.
#
# Separate counter from the coverage one directly above on purpose: a day
# can hit either, both, or neither, and one silencing the other is the
# #538 mistake. Same threshold, because the reasoning is identical --
# the first retries are the recorder catching up or an executor that is
# briefly busy, and only a persistent one is a real fault.
_SOC_HISTORY_SKIP_COUNTS: dict[str, int] = {}

# nimbus issue #1214: the opening SoC assumed when this install has no
# battery SoC sensor configured at all.
#
# It was previously reachable two ways -- no sensor configured, and a
# configured sensor whose read failed -- and the second is what made it
# dangerous, because an install that HAS a real SoC can silently be
# scored as though it were at half charge. The guard above now returns
# None for that case, so this value only ever applies where there is
# genuinely nothing better to know.
#
# 50% is not a defensible estimate of any particular battery; it is the
# midpoint, chosen so the error is bounded in both directions rather than
# biased. Named rather than repeated inline so the two remaining uses
# cannot drift apart, and so a search for it finds this comment.
_ASSUMED_INITIAL_SOC_PCT = 50.0


def _history_coverage_hours(
    histories: tuple[list[tuple[datetime, float]], ...],
    start: datetime,
    end: datetime,
) -> float:
    """Hours of `[start, end)` actually spanned by the WORST-covered of
    `histories` (nimbus issue #984).

    Measured first-sample-to-last-sample, clipped to the window, and
    minimised across the series -- the report is only as trustworthy as
    its thinnest input, so one truncated sensor has to fail the whole
    day rather than be averaged away by two healthy ones.

    Deliberately a span rather than a sample count: sample rates differ
    per sensor and per install, so "how many rows" has no fixed
    expectation to compare against, while "how much of the day do these
    rows reach across" does. The trade-off is that an interior gap does
    not reduce the span -- accepted, because the failure this exists to
    catch is a TRUNCATED window (the recorder returning only the tail of
    the day), not a perforated one.

    Returns 0.0 for an empty series, which fails any threshold.
    """
    if not histories:
        return 0.0
    by_series = _history_coverage_by_series(
        {str(i): hist for i, hist in enumerate(histories)}, start, end
    )
    return min(cov.hours for cov in by_series.values())


@dataclass(frozen=True)
class _SeriesCoverage:
    """One series' own contribution to the coverage gate (nimbus issue
    #1054).

    `hours` is the number the threshold acts on. `first`/`last` are the
    covered span's own edges, clipped to the window and None for a series
    with no history at all -- they are what turn "this sensor was short"
    into "this sensor's history runs 06:24 to 23:59", which for a
    TRUNCATED window (the only kind this gate can see -- see
    `_history_coverage_hours()` on why an interior gap does not reduce a
    span) is the gap boundary itself.
    """

    hours: float
    first: datetime | None
    last: datetime | None


def _history_coverage_by_series(
    histories: dict[str, list[tuple[datetime, float]]],
    start: datetime,
    end: datetime,
) -> dict[str, _SeriesCoverage]:
    """The same span measurement as `_history_coverage_hours()`, kept
    PER SERIES instead of minimised away (nimbus issue #1054).

    `_history_coverage_hours()` reports only the worst number, which is
    the right input for the threshold test but throws away the one fact
    a household needs once the gate actually fires: **which** sensor was
    short. Mark Purcell hit exactly that on the real production install
    -- the skip line said 17.37 h of 24.00 h "worst of solar/load/
    battery" and #1054's own next step had to be "pull all three
    sensors' raw history by hand and find out which one it was", a
    manual recorder pull to recover a number this function had already
    computed and discarded.

    So this is the real implementation and `_history_coverage_hours()`
    delegates to it -- one measurement, no chance of the headline number
    and the breakdown drifting apart, and the existing tuple signature
    (and its tests) unchanged.

    An empty series maps to 0.0 here rather than short-circuiting the
    whole dict, which is what makes an entirely-missing sensor
    distinguishable from a merely truncated one -- though note that
    end-to-end an entirely-absent sensor is caught UPSTREAM of this gate
    by #314's own row-count guard, which already names it, so the 0.0
    here is defensive rather than the path a household actually hits.
    The minimum over the result is identical either way, so the
    threshold behaviour is byte-for-byte what it was.

    The span's own edges come back too -- #1054's stated next step was
    finding the actual gap boundary, and for a truncated window that IS
    the boundary, so there is no reason to make anyone pull the recorder
    by hand for something already in scope here.
    """
    out: dict[str, _SeriesCoverage] = {}
    for name, hist in histories.items():
        if not hist:
            out[name] = _SeriesCoverage(0.0, None, None)
            continue
        first = max(hist[0][0], start)
        last = min(hist[-1][0], end)
        out[name] = _SeriesCoverage(
            max(0.0, (last - first).total_seconds() / 3600.0), first, last
        )
    return out


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
        except Exception:
            # nimbus issue #363 (Mark Purcell, codebase review): degrade
            # stays, breadcrumb added -- same reasoning as fetch_price_
            # history()'s own identical recorder-bridge except above.
            _LOGGER.debug(
                "Nimbus Solver: fetch_entity_history_range(%s) recorder read failed",
                entity_id,
                exc_info=True,
            )
            return []
        out: list[tuple[datetime, float]] = []
        for s in states:
            try:
                v = float(s.state)
            except (TypeError, ValueError):
                continue
            out.append((s.last_changed.astimezone(LOCAL_TZ), v))
        return sorted(out, key=lambda x: x[0])
    url = (
        f"{HA_BASE}/api/history/period/{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {_load_token()}"}
    )
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
        out.append((parse_iso(p["last_changed"]).astimezone(LOCAL_TZ), v))
    return sorted(out, key=lambda x: x[0])


def fetch_entity_power_history_kw(
    entity_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """Real recorded power history normalised to kW **per row**, using
    each sample's OWN recorded `unit_of_measurement`.

    nimbus issue #843 (option A of Mark Purcell's own A/B/C steer,
    2026-09-13). `fetch_entity_history_range()` above deliberately strips
    attributes on BOTH paths -- `no_attributes=True` natively and
    `&minimal_response` over REST -- so per-row units are not merely
    ignored there, they are never fetched. Every caller therefore has to
    lean on `_kw_scale_factor()`, which reads the sensor's CURRENT LIVE
    unit exactly once and applies that single scale across the whole
    window.

    That is structurally unable to see a sensor whose unit changes
    mid-window, which is a real, confirmed thing rather than a
    hypothetical: Mark's own EV pack sensor reports `W` for ~80 seconds
    as the car wakes from sleep, then switches to `kW` --

        08:51:04.410  state=1514.417   unit="W"     <- really 1.514417 kW
        08:51:30.004  state=-774.818   (still W)
        08:52:27.078  state=-0.774994  <- kW from here on

    -- so a live check returning `kW` scaled that 1514.417 by 1.0 and
    the quality report published ~1,500 kW of achieved battery power
    against a real ~80 kW fleet ceiling.

    Deliberately a SEPARATE function rather than a flag on
    fetch_entity_history_range(), and deliberately used by only ONE
    caller (`_resolve_battery_participant_history()`'s participant power
    fetch). Preserving attributes makes a recorder read materially
    heavier -- they are a separate join, and the quality report fetches a
    full day per sensor per participant -- so every other history read in
    this file keeps the cheap attribute-stripped path. That scoping is
    the whole reason option A was viable at all; a blanket change would
    have imposed the cost on every install to fix a wake transient on one
    sensor family.

    Per-row conversion mirrors `_async_fetch_thermal_history()`'s own
    existing precedent in this same file (`unit == "W"` -> divide by
    1000) rather than inventing a second convention. A row with no unit
    at all is taken as already-kW, matching `_kw_scale_factor()`'s own
    documented default for the same case.

    Same "degrade to [] on any failure, never crash" discipline as every
    other real-data fetch here.
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
                    False,  # no_attributes=False -- unit_of_measurement lives there
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), _NATIVE_HASS.loop)
            changes = future.result(timeout=30)
            states = changes.get(entity_id, [])
        except Exception:
            _LOGGER.debug(
                "Nimbus Solver: fetch_entity_power_history_kw(%s) recorder read failed",
                entity_id,
                exc_info=True,
            )
            return []
        out: list[tuple[datetime, float]] = []
        for s in states:
            try:
                v = float(s.state)
            except (TypeError, ValueError):
                continue
            if s.attributes.get("unit_of_measurement") == "W":
                v = v / 1000.0
            out.append((s.last_changed.astimezone(LOCAL_TZ), v))
        return sorted(out, key=lambda x: x[0])
    # REST fallback: no `&minimal_response`, so each point keeps its own
    # attributes dict (the whole point of this function).
    url = (
        f"{HA_BASE}/api/history/period/{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {_load_token()}"}
    )
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
        if (p.get("attributes") or {}).get("unit_of_measurement") == "W":
            v = v / 1000.0
        out.append((parse_iso(p["last_changed"]).astimezone(LOCAL_TZ), v))
    return sorted(out, key=lambda x: x[0])


def fetch_entity_state_history_range(
    entity_id: str, start: datetime, end: datetime
) -> list[tuple[datetime, str]]:
    """Real recorded STATE-STRING history for one entity over an
    explicit [start, end) window -- same shape and same dual native/
    REST mode as fetch_entity_history_range() just above, but keeps
    each state as its raw string instead of casting to float.

    nimbus issue #768 (Mark Purcell, 2026-09-13): needed for a
    genuinely non-numeric state -- a battery_participant's own
    `battery_participant_available_entity` (a `binary_sensor`,
    "on"/"off", not a number). fetch_entity_history_range() itself
    would silently drop EVERY point for an entity like this (its own
    `float(s.state)` cast fails and skips it), returning an empty
    history regardless of how much real data actually exists -- exactly
    the kind of silent, misleading "history missing" this project's own
    established discipline never wants (see that function's own several
    real-bug-history comments).

    Skips only a genuinely missing/unusable state (None, "unknown",
    "unavailable") -- every other real string state is kept as-is,
    unlike fetch_entity_history_range()'s own numeric-cast filter.
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
        except Exception:
            _LOGGER.debug(
                "Nimbus Solver: fetch_entity_state_history_range(%s) recorder read failed",
                entity_id,
                exc_info=True,
            )
            return []
        out: list[tuple[datetime, str]] = []
        for s in states:
            if s.state in (None, "unknown", "unavailable"):
                continue
            out.append((s.last_changed.astimezone(LOCAL_TZ), s.state))
        return sorted(out, key=lambda x: x[0])
    url = (
        f"{HA_BASE}/api/history/period/{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z&minimal_response"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {_load_token()}"}
    )
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
        out.append((parse_iso(p["last_changed"]).astimezone(LOCAL_TZ), state))
    return sorted(out, key=lambda x: x[0])


def fetch_entity_attribute_history_range(
    entity_id: str, attribute: str, start: datetime, end: datetime
) -> list[tuple[datetime, float]]:
    """nimbus issue #592: same shape as fetch_entity_history_range()
    just above, for the real case that function can't cover -- a
    water_heater/climate done_entity's own STATE is a mode string
    ("eco"/"performance"), never a number; the value that matters
    (current_temperature) lives on the ATTRIBUTE instead (see
    done_condition.py's own read_current_temperature() for the live-
    read equivalent of this same fact). Fetches WITH attributes (unlike
    fetch_entity_history_range()'s own no_attributes=True), reads
    `attribute` off each historical state instead of the state itself.
    Same dual native/REST mode, same degrade-to-[]-on-any-failure
    discipline as every other real-data fetch in this file.
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
                    False,  # no_attributes -- must be False, the whole point here
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), _NATIVE_HASS.loop)
            changes = future.result(timeout=30)
            states = changes.get(entity_id, [])
        except Exception:
            _LOGGER.debug(
                "Nimbus Solver: fetch_entity_attribute_history_range(%s, %s) "
                "recorder read failed",
                entity_id,
                attribute,
                exc_info=True,
            )
            return []
        out: list[tuple[datetime, float]] = []
        for s in states:
            raw = s.attributes.get(attribute)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            out.append((s.last_changed.astimezone(LOCAL_TZ), v))
        return sorted(out, key=lambda x: x[0])
    url = (
        f"{HA_BASE}/api/history/period/{start.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
        f"?filter_entity_id={entity_id}"
        f"&end_time={end.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')}Z"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {_load_token()}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return []
    if not data or not data[0]:
        return []
    out = []
    for p in data[0]:
        raw = (p.get("attributes") or {}).get(attribute)
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        out.append((parse_iso(p["last_changed"]).astimezone(LOCAL_TZ), v))
    return sorted(out, key=lambda x: x[0])


def resample_history_nearest(
    pts: list[tuple[datetime, float]],
    grid_times: list[datetime],
    default: float = 0.0,
    *,
    backfill_first: bool = False,
) -> list[float]:
    """Nearest-at-or-before lookup against real recorded history points
    -- same convention as resample_forecast() elsewhere in this file,
    just against (datetime, value) tuples from fetch_entity_history_
    range() instead of a forecast's own {time, value} dicts.

    What happens when NOTHING in `pts` precedes a given `gt` is an
    explicit, per-caller choice, because the physically correct answer
    genuinely differs by signal type (the same FLOW-vs-STATE split
    resample_history_mean() below already documents):

    - `backfill_first=False` (default): return `default`. Correct for
      power-FLOW signals (solar/load/battery/EV pack power) and for
      boolean availability masks -- absence of data does NOT mean
      "whatever this sensor read the next time it woke up."
    - `backfill_first=True`: return the earliest real sample instead
      (`pts[0][1]`), falling back to `default` only when `pts` is
      genuinely empty. Correct for STATE signals (SoC %, prices), where
      the first real reading of the window is a far better estimate of
      the unobserved earlier state than a hardcoded constant -- an EV
      parked overnight really did sit at its wake-up SoC the whole time.

    nimbus issue #843 (Mark Purcell, root-caused against real household
    recorder data): this function previously ALWAYS initialised `val` to
    `pts[0][1]`, i.e. it silently behaved as `backfill_first=True` for
    every caller, and could therefore return a value recorded AFTER `gt`
    from a function whose whole contract is "at or before" -- never once
    falling through to the `default` its callers explicitly passed.

    Confirmed live impact: an EV whose telemetry genuinely sleeps
    overnight (zero recorder rows 00:00 -> 08:51, real Tesla sleep
    behaviour) had every hour from 00:00-07:00 inherit its first
    post-wake reading. That reading also happened to be a ~1000x-wrong
    W-vs-kW boot transient (a separate, still-open half of #843), so the
    quality report published a ~1,500 kW achieved battery power -- about
    19x the household's real physical fleet ceiling -- for eight
    straight hours, making EPR/regret unusable for that day. The unit
    bug corrupted one sample; THIS bug is what smeared it across a third
    of the day.
    """
    out = []
    for gt in grid_times:
        val = pts[0][1] if (pts and backfill_first) else default
        for t, v in pts:
            if t <= gt:
                val = v
            else:
                break
        out.append(float(val))
    return out


def resample_history_mean(
    pts: list[tuple[datetime, float]],
    grid_times: list[datetime],
    period_hours: float,
    default: float = 0.0,
) -> list[float]:
    """Period-mean lookup against real recorded history points -- averages
    every real sample falling within [grid_time, grid_time + period_hours)
    for each grid point, instead of resample_history_nearest()'s single
    nearest-at-or-before instant.

    nimbus issue #428 (Mark Purcell): confirmed live that a brief, real,
    isolated telemetry spike right at a period boundary (a genuine
    +20.9kW battery sample, seconds wide, against that hour's real mean
    of -2.66kW) gets picked up WHOLE by resample_history_nearest() --
    treated as representative of the entire 15-minute period, flipping
    that period's reconstructed grid direction relative to what a real
    energy-weighted average of the period would show (traced end to end
    against Mark's own real recorder data: the spike alone reconstructs
    to a large positive/importing grid figure, while the real hourly
    mean reconstructs to negative/exporting -- matching the real meter).
    compute_daily_quality_report()'s achieved (J_ach) trajectory
    reconstructs real energy flow (solar/load/battery power) from
    history -- an energy-weighted mean over the period is the physically
    correct way to do that (matches how a real meter integrates power
    into energy over that quarter-hour), not an instantaneous point
    sample of whatever the sensor happened to read at the exact grid
    instant.

    Falls back to resample_history_nearest()'s own nearest-at-or-before
    pick when a period genuinely has zero real samples inside its own
    window (sparse/low-frequency history) -- never fabricates a gap.

    Deliberately scoped to the three power-FLOW signals (solar/load/
    battery) feeding compute_daily_quality_report()'s achieved
    reconstruction -- SoC and price are real STATE values (sample-and-
    hold is the physically correct model for those, not an average),
    and every other caller of resample_history_nearest() is unchanged.
    """
    out = []
    for gt in grid_times:
        window_end = gt + timedelta(hours=period_hours)
        rows = [(ts, v) for ts, v in pts if gt <= ts < window_end]
        if rows:
            # nimbus issue #1008: TIME-WEIGHTED, not a plain average of
            # samples.
            #
            # HA's recorder stores state CHANGES, so samples are
            # irregularly spaced -- dense while a value is moving, sparse
            # while it holds. `sum(vals) / len(vals)` therefore weights a
            # 2-second spike exactly as heavily as a 40-minute plateau,
            # and a battery that jumps to +-40 kW and then sits flat
            # writes a row for every step of the spike and almost none
            # for the plateau.
            #
            # Measured on the reference household, 2026-09-15:
            #
            #   scorer, sample mean   ->  99.675 kWh in / 107.428 out
            #                             = -7.75 kWh net (net DISCHARGE)
            #   inverter counters     -> 106.6   kWh in / 100.9   out
            #                             = +5.70 kWh net (net CHARGE)
            #   HA's own statistics   -> mean -0.2229 kW x 24 h
            #                             = -5.35 kWh  (net CHARGE)
            #
            # HA's statistics mean IS time-weighted, which is why it
            # agrees with the counters to 0.35 kWh while this function
            # disagreed by 13.5 kWh and inverted the day's direction --
            # the reconstructed SoC ran 16.07% -> 0.44% on a day the pack
            # really went 17.4% -> ~20%.
            #
            # Each sample is weighted by how long it HELD: until the next
            # sample, or the end of the window for the last one. The
            # first sample's weight starts at the window boundary rather
            # than at its own timestamp, because whatever value preceded
            # it is carried in by the `else` branch's semantics below --
            # a recorded state holds until the next one replaces it.
            total = 0.0
            weighted = 0.0
            for i, (ts, v) in enumerate(rows):
                next_ts = rows[i + 1][0] if i + 1 < len(rows) else window_end
                span = (next_ts - ts).total_seconds()
                if span <= 0:
                    continue
                weighted += v * span
                total += span
            # Time between the window start and the first recorded sample
            # is held by the last REAL value before the window -- the same
            # last-known-value model resample_history_nearest() applies.
            #
            # Only when such a sample genuinely exists. If nothing
            # precedes the window there is no observation to carry in,
            # and weighting the gap at `default` would INVENT data inside
            # a period that has real samples -- biasing every scored
            # day's first period, since the history fetch starts at the
            # window boundary. A period with no samples at all still
            # falls through to the `else` branch below, which is where
            # the flow-signal "absent power reads as 0" convention
            # belongs.
            prior_rows = [v for ts, v in pts if ts < gt]
            lead_span = (rows[0][0] - gt).total_seconds()
            if lead_span > 0 and prior_rows:
                weighted += prior_rows[-1] * lead_span
                total += lead_span
            out.append(weighted / total if total > 0 else rows[0][1])
        else:
            out.append(resample_history_nearest(pts, [gt], default=default)[0])
    return out


def _kw_scale_factor(entity_id: str) -> float:
    """Real bug found live on devhub 2026-08-28: compute_daily_quality_
    report() and compute_efficiency_backtest_report() both take a
    configured *_power_sensor entity_id and treat its raw historical
    values as already being kW, with no check against what the entity
    itself actually declares. A household pointing solver_solar_power_
    sensor at a native Watts sensor (very common for raw inverter/logger
    telemetry -- confirmed live: sensor.combined_total_dc_power's own
    unit_of_measurement is "W") silently fed solar values ~1000x too
    large into both reports, producing nonsense economics (confirmed
    live: theoretical_maximum_yield/regret_dollars around -$1280/-$1289
    for one real household-day, an impossible magnitude).

    Returns the multiplier to bring a history value into kW: 0.001 for a
    "W" sensor, 1.0 for "kW" or anything else (including no unit at all,
    or a lookup failure) -- 1.0 is the correct default since "kW" was
    always the original, undocumented assumption these two functions
    made; this only corrects the one real, confirmed-live mismatch
    (Watts), not a guess at every possible unit HA's power device class
    could report.
    """
    try:
        unit = ha_get(entity_id).get("attributes", {}).get("unit_of_measurement")
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return 1.0
    return 0.001 if unit == "W" else 1.0


# nimbus issue #493 (Signals 4/7 of #489, item 1 -- Mark Purcell's own
# authorized next step, real target: Open Dynamic Export's `opModExpLimW`/
# `opModImpLimW` MQTT publish, a SA-Power-Networks-certified CSIP-AUS/
# SEP2/IEEE-2030.5 client). Same log-once-per-condition discipline as
# _SOLAR_SOURCE_WARNED above -- an envelope entity that goes unavailable
# shouldn't spam a WARNING on every solve tick, but a genuinely new
# failure reason (or entity) still gets its own one-time log, and
# recovery is worth reporting the same way a solar source coming back
# online is.
_ENVELOPE_LIMIT_WARNED: set[tuple[str, str]] = set()


def _warn_envelope_limit_dropped_once(entity_id: str, reason: str) -> None:
    key = (entity_id, reason)
    if key in _ENVELOPE_LIMIT_WARNED:
        _LOGGER.debug(
            "Nimbus: envelope limit entity %s still %s -- falling back to "
            "the configured static grid limit (logged once per condition, "
            "not every solve)",
            entity_id,
            reason,
        )
        return
    _ENVELOPE_LIMIT_WARNED.add(key)
    _LOGGER.warning(
        "Nimbus: envelope limit entity %s %s -- falling back to the "
        "configured static grid limit (logged once per condition, not "
        "every solve)",
        entity_id,
        reason,
    )


def _note_envelope_limit_recovered(entity_id: str) -> None:
    had_any = any(eid == entity_id for eid, _reason in _ENVELOPE_LIMIT_WARNED)
    if not had_any:
        return
    _ENVELOPE_LIMIT_WARNED.difference_update(
        {key for key in _ENVELOPE_LIMIT_WARNED if key[0] == entity_id}
    )
    _LOGGER.info(
        "Nimbus: envelope limit entity %s is reporting again (previously dropped)",
        entity_id,
    )


FLEX_SIGNALS_ENTITY_ID = "sensor.nimbus_flex_signals"


def compute_daily_flex_report(cfg: dict, now: datetime) -> dict | None:
    """nimbus issue #496 (Signals 7/7 of #489), the second half Mark
    Purcell authorized 2026-09-09 ("compute_daily_flex_report()'s own
    offered-vs-realised/envelope-curtailment arithmetic") -- the sensor
    half (switch.nimbus_solver_flex_signals_enabled, sensor.nimbus_flex_
    signals) shipped separately as v0.94.282/v0.94.283. Same "yesterday"
    thin-wrapper convention as compute_daily_quality_report() just above
    this file's own quality-report section -- see that function's own
    docstring for why "yesterday" (not "today so far") is the right
    window for a real day-level report.

    Three real, independently-available components, per this issue's
    own body:

    1. **Offered vs realised flex** (kWh, up and down). "Offered" is the
       real recorder history of sensor.nimbus_flex_signals's own
       flex_available_up_kw (native state) / flex_available_down_kw
       (attribute) -- genuinely only available on days the opt-in
       switch was on; `None` otherwise, never fabricated. "Realised" is
       defined here as the real measured battery response: actual
       charge kWh for "up" (absorbing more load right now IS charging
       more), actual discharge kWh for "down" (reducing net import /
       increasing export IS discharging) -- the same real battery-power
       history and sign-convention handling compute_daily_quality_
       report() already uses (solver_battery_power_sensor,
       solver_battery_power_positive_is_charge), not a second,
       independently-drifting read of the same sensor. This is a real,
       defensible, but not the only possible definition of "realised" --
       no generic commanded-vs-actual flex-event signal exists anywhere
       in this codebase (same honest limitation compute_daily_quality_
       report()'s own docstring already discloses for tracking_fidelity).

    2. **Price-response curve**: real (import price, net import kW)
       pairs for the day, binned into $0.05/kWh-wide price bands, mean
       net import kW per band -- an empirical demand-response curve
       from Nimbus's own measured data, matching #496's own "the
       nem-flex-telemetry price-response tab computed locally" ask.
       net_import_kw is derived from the SAME three sensors quality
       report already requires (load - solar - discharge + charge, the
       real energy-balance identity), not a new required config field.

    3. **Envelope curtailment kWh**: `max(0, solar - load - export_limit)`
       per interval, real solar/load history, real configured
       `solver_grid_max_export_kw` as the export limit -- NOT the 5 kW
       default this issue's own body explicitly warns against. #493's
       own live DNSP envelope entity (resolve_envelope_limit_kw()) is
       deliberately NOT used here: that function only ever returns the
       CURRENT live/forecast value held flat, with no real historical
       backing for a past envelope schedule (see its own docstring) --
       using it for a day already gone would silently report today's
       envelope as if it were yesterday's, which is worse than the
       honest, disclosed simplification of using the flat configured
       static limit (the same real per-install number every other
       fallback in this codebase already uses instead of a fabricated
       constant).

    Returns a dict with `latest_date` plus the three components above
    (offered/realised are `None`, not zero, on a day the switch was
    off), or `None` if genuinely not configured (same three required
    sensors as compute_daily_quality_report()) or no real history
    exists for yesterday at all -- callers must treat `None` as "skip
    this cycle, retry later," never an error, same convention as every
    other report function in this file.
    """
    yesterday = (now - timedelta(days=1)).date()
    day_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=LOCAL_TZ
    )
    day_end = day_start + timedelta(days=1)
    return _compute_flex_report_for_window(cfg, day_start, day_end)


_PRICE_BAND_WIDTH = (
    0.05  # $/kWh -- matches this file's own price-rounding convention elsewhere
)


def _compute_flex_report_for_window(
    cfg: dict, day_start: datetime, day_end: datetime
) -> dict | None:
    """Real body behind compute_daily_flex_report() -- see that
    function's own docstring for the full reasoning behind each
    component. Split out the same way _compute_report_for_window() is
    split from compute_daily_quality_report(), for the same reason
    (keeps the door open for a future arbitrary-window service call,
    same shape as nimbus_load.compute_quality_report, without
    duplicating this body -- not added in this pass since #496's own
    text never asked for one, only flagging the seam exists)."""
    if day_end <= day_start:
        return None
    solar_sensor = cfg.get("solver_solar_power_sensor")
    battery_sensor = cfg.get("solver_battery_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    if not solar_sensor or not battery_sensor or not load_sensor:
        _LOGGER.debug(
            "Nimbus flex report: skip. Missing sensor config (solar=%s "
            "battery=%s load=%s) -- same three sensors compute_daily_"
            "quality_report() requires, under Solver settings",
            solar_sensor,
            battery_sensor,
            load_sensor,
        )
        return None
    window_hours = (day_end - day_start).total_seconds() / 3600.0
    if window_hours < 24.0:
        _LOGGER.debug(
            "Nimbus flex report: skip. Window is %.2f h, shorter than the "
            "24 h a full-day report requires",
            window_hours,
        )
        return None
    period_hours = TIER2_PERIOD_HOURS
    n_periods = round(window_hours / period_hours)
    if n_periods < 1:
        return None
    grid_times = [
        day_start + timedelta(hours=i * period_hours) for i in range(n_periods)
    ]

    solar_hist = fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = fetch_entity_history_range(load_sensor, day_start, day_end)
    battery_hist = fetch_entity_history_range(battery_sensor, day_start, day_end)
    if not solar_hist or not load_hist or not battery_hist:
        _LOGGER.info(
            "Nimbus flex report: skip. Real history missing for window "
            "[%s, %s] (solar=%d, load=%d, battery=%d rows)",
            day_start.isoformat(),
            day_end.isoformat(),
            len(solar_hist),
            len(load_hist),
            len(battery_hist),
        )
        return None
    import_price_hist = fetch_entity_history_range(
        cfg["solver_import_price_sensor"], day_start, day_end
    )

    solar_scale = _kw_scale_factor(solar_sensor)
    load_scale = _kw_scale_factor(load_sensor)
    battery_scale = _kw_scale_factor(battery_sensor)
    battery_sign = -1.0 if cfg.get("solver_battery_power_positive_is_charge") else 1.0

    solar_kw = np.array(
        [
            max(0.0, v * solar_scale)
            for v in resample_history_mean(solar_hist, grid_times, period_hours)
        ]
    )
    load_kw = np.array(
        [
            max(0.0, v * load_scale)
            for v in resample_history_mean(load_hist, grid_times, period_hours)
        ]
    )
    actual_net_kw = np.array(
        [
            v * battery_scale * battery_sign
            for v in resample_history_mean(battery_hist, grid_times, period_hours)
        ]
    )
    actual_charge_kw = np.array([max(0.0, -v) for v in actual_net_kw])
    actual_discharge_kw = np.array([max(0.0, v) for v in actual_net_kw])
    import_price = np.array(
        resample_history_nearest(
            import_price_hist, grid_times, default=0.20, backfill_first=True
        )
    )

    # Component 1: offered vs realised flex -- offered needs real
    # sensor.nimbus_flex_signals history, genuinely absent on a day the
    # opt-in switch was off. Never fabricated: None, not 0.0, when it's
    # simply not there.
    offered_up_hist = fetch_entity_history_range(
        FLEX_SIGNALS_ENTITY_ID, day_start, day_end
    )
    offered_down_hist = fetch_entity_attribute_history_range(
        FLEX_SIGNALS_ENTITY_ID, "flex_available_down_kw", day_start, day_end
    )
    offered_up_kwh = None
    offered_down_kwh = None
    if offered_up_hist:
        offered_up_kw = resample_history_mean(offered_up_hist, grid_times, period_hours)
        offered_up_kwh = round(float(sum(offered_up_kw) * period_hours), 3)
    if offered_down_hist:
        offered_down_kw = resample_history_mean(
            offered_down_hist, grid_times, period_hours
        )
        offered_down_kwh = round(float(sum(offered_down_kw) * period_hours), 3)
    realised_up_kwh = round(float(np.sum(actual_charge_kw) * period_hours), 3)
    realised_down_kwh = round(float(np.sum(actual_discharge_kw) * period_hours), 3)

    # Component 2: price-response curve -- real net import derived from
    # the same energy-balance identity the rest of this file already
    # relies on, binned by real import price seen.
    net_import_kw = load_kw - solar_kw - actual_discharge_kw + actual_charge_kw
    price_bands: dict[int, list[float]] = {}
    for price, kw in zip(import_price.tolist(), net_import_kw.tolist(), strict=True):
        band_index = int(price // _PRICE_BAND_WIDTH)
        price_bands.setdefault(band_index, []).append(kw)
    price_response_curve = [
        {
            "price_band_low": round(band_index * _PRICE_BAND_WIDTH, 2),
            "price_band_high": round((band_index + 1) * _PRICE_BAND_WIDTH, 2),
            "mean_net_import_kw": round(float(np.mean(kws)), 3),
            "n_samples": len(kws),
        }
        for band_index, kws in sorted(price_bands.items())
    ]

    # Component 3: envelope curtailment -- see this function's own
    # caller docstring for why the static configured limit, not the
    # live #493 envelope entity, is the honest choice for a past day.
    static_export_limit_kw = _cfg_num(cfg, "solver_grid_max_export_kw", 0.0)
    envelope_curtailment_kw = np.maximum(
        0.0, solar_kw - load_kw - static_export_limit_kw
    )
    envelope_curtailment_kwh = round(
        float(np.sum(envelope_curtailment_kw) * period_hours), 3
    )

    return {
        "latest_date": day_start.date().isoformat(),
        "offered_up_kwh": offered_up_kwh,
        "offered_down_kwh": offered_down_kwh,
        "realised_up_kwh": realised_up_kwh,
        "realised_down_kwh": realised_down_kwh,
        "envelope_curtailment_kwh": envelope_curtailment_kwh,
        "price_response_curve": price_response_curve,
    }


FLEX_REPORT_ENTITY_ID = "sensor.nimbus_flex_report"


def publish_daily_flex_report(cfg: dict, now: datetime) -> None:
    """Publishes sensor.nimbus_flex_report -- same idempotency-first,
    re-push-on-fast-path pattern as publish_daily_quality_report() just
    above this file's own quality-report section (see that function's
    own docstring for the full "why re-push instead of no-op" incident
    history, #289/#292 -- applies identically here, same freshness-
    watchdog mechanism, same entity class).
    """
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    try:
        existing = ha_get(resolve_real_entity_id(FLEX_REPORT_ENTITY_ID))
        if existing.get("attributes", {}).get("latest_date") == yesterday_key:
            _LOGGER.debug(
                "Nimbus flex report: fast-path hit, already scored %s -- "
                "re-pushing cached state to keep the freshness stamp alive",
                yesterday_key,
            )
            ha_post_state(
                FLEX_REPORT_ENTITY_ID, existing["state"], existing["attributes"]
            )
            return
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        pass
    report = compute_daily_flex_report(cfg, now)
    if report is None:
        _LOGGER.debug(
            "Nimbus flex report: no report for %s this cycle -- sensor "
            "left unchanged, will retry next cycle",
            yesterday_key,
        )
        return
    ha_post_state(
        FLEX_REPORT_ENTITY_ID,
        report["realised_up_kwh"] + report["realised_down_kwh"],
        {
            "unit_of_measurement": "kWh",
            "friendly_name": "Nimbus Flex Report",
            **report,
            "generated_at": datetime.now(UTC).astimezone(LOCAL_TZ).isoformat(),
            # nimbus issue #1256: stamped with the computation, not left to
            # the entity's running-install fallback -- this sensor restores
            # across a restart, so the two diverge on every deploy.
            **_version_stamp(),
        },
    )


def resolve_envelope_limit_kw(
    entity_id: str | None,
    static_limit_kw: float,
    grid_times: list[datetime],
) -> list[float]:
    """nimbus issue #493 (Signals 4/7 of #489, item 1): a live DNSP
    dynamic import/export envelope becomes a genuine per-period
    feasibility bound -- GridConfig.import_limit_kw/export_limit_kw
    already accept a real per-period array (v0.94.218, this issue's own
    item 0), so this is exposure/wiring, not new solver work.

    Two real shapes handled, matching this project's own established
    forecast-attribute-or-flat convention (CONF_SOLVER_IMPORT_PRICE_
    SENSOR/EXPORT_PRICE_SENSOR): a `forecast` attribute (list of
    {time, value}) -- a DNSP schedule published ahead of time, resampled
    via resample_forecast() same as every other forecast-shaped entity
    in this file -- or, with no `forecast` attribute, the entity's own
    plain state IS the current live limit (Open Dynamic Export's own
    real shape: a single numeric MQTT-sourced value updated live, no
    forward schedule), held FLAT across the whole solve horizon -- same
    "no forward-looking source exists for a real measured value"
    reasoning this project's own recursive-forecast bug chain
    (CLAUDE.md) already documents for battery_kw/grid_kw/solar_kw
    context features.

    `entity_id` None (not configured), missing/unavailable, or a
    non-numeric state -- falls back to `static_limit_kw` flat across the
    whole horizon, logged once per (entity_id, reason) at WARNING, never
    a fabricated envelope. Unit-aware (`_kw_scale_factor()`, the same
    W-vs-kW correction every other power-reading site in this file
    already applies) -- a household pointing this at a native Watts
    sensor (very common for raw MQTT telemetry) must not silently apply
    a 1000x-too-large bound.
    """
    n = len(grid_times)
    if not entity_id:
        return [static_limit_kw] * n
    try:
        state = ha_get(entity_id)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        _warn_envelope_limit_dropped_once(entity_id, "unavailable")
        return [static_limit_kw] * n
    if state.get("state") in (None, "unknown", "unavailable"):
        _warn_envelope_limit_dropped_once(entity_id, "unavailable")
        return [static_limit_kw] * n
    scale = _kw_scale_factor(entity_id)
    forecast = state.get("attributes", {}).get("forecast")
    if forecast:
        values = resample_forecast(forecast, "value", grid_times)
        _note_envelope_limit_recovered(entity_id)
        return [v * scale for v in values]
    try:
        live_value = float(state["state"]) * scale
    except (KeyError, TypeError, ValueError):
        _warn_envelope_limit_dropped_once(entity_id, "reporting a non-numeric state")
        return [static_limit_kw] * n
    _note_envelope_limit_recovered(entity_id)
    return [live_value] * n


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

    Thin wrapper over `_compute_report_for_window()` since v0.94.42 --
    the whole scoring body was extracted so the new nimbus_load.compute
    _quality_report service (issue #316) can score arbitrary windows
    without duplicating any of it. This wrapper preserves the exact
    "yesterday" calendar-day semantics every existing caller (main()'s
    own publish_daily_quality_report path) has always relied on.
    """
    yesterday = (now - timedelta(days=1)).date()
    day_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=LOCAL_TZ
    )
    day_end = day_start + timedelta(days=1)
    return _compute_report_for_window(cfg, day_start, day_end, allow_partial=False)


_LOAD_NOWCAST_TRAIL_ENTITY_ID = (
    "sensor.nimbus_solver_load_whole_house_cross_check_now_kw"
)
"""The recorded forecast trail #919 reads back -- deliberately NOT
`sensor.nimbus_household_load_total_forecast`.

That sensor's state is `round(load_kw[0], 3)`, and `solver_inputs/
load.py` overwrites `load_kw[0]` with the live cross-check reading right
before the publish (#429's anchor). The cross-check sensor is the same
one this function uses as its real-load ground truth, so that trail is
the ground truth echoed back -- confirmed live, agreeing to the cent on
every row of three hours of history. It would have published
near-perfect skill on every install, forever.

This entity carries `whole_house_now_kw`, snapshotted BEFORE the anchor
precisely so #429's cross-check compares two genuine forecasts. It is
the forecast OF the ground-truth sensor, which is exactly the pairing a
skill measurement wants. See solver/nowcast_skill.py's own docstring.
"""

_NOWCAST_SKILL_KEYS = (
    "load_nowcast_skill_j_star",
    "load_nowcast_skill_j_forecast",
    "load_nowcast_skill_j_persistence",
    "load_nowcast_skill_value_add_dollars",
    "load_nowcast_skill_coverage",
    "load_nowcast_skill_periods_measured",
    # nimbus issue #1176: WHY every other key is None, when they are.
    #
    # Same shape as #1162's `epr_reason`, and the same defect it fixes: a
    # diagnostic that goes silent without saying which gate closed. Every
    # one of the three blank paths below was DEBUG-only, and the third
    # line covered two genuinely different causes at once ("Only %d of %d
    # periods ... or a scenario solve failed"), so even with DEBUG on a
    # reader could not separate them.
    #
    # null when the skill computed.
    "load_nowcast_skill_reason",
)


def _load_nowcast_skill_attributes(
    *,
    load_sensor: str,
    load_scale: float,
    grid_times: list[datetime],
    period_hours: float,
    periods,
    grid,
    battery,
    solar_real_kw,
    load_real_kw,
    day_start: datetime,
    day_end: datetime,
) -> dict:
    """Recover the forecast trail from recorder history and score this
    window's one-step-ahead load-forecast skill (nimbus issue #919).

    Always returns every key in `_NOWCAST_SKILL_KEYS`, with None values
    when the skill could not be computed -- a stable attribute set, so a
    consumer never sees a key appear and vanish between windows (#589's
    own "empty attributes for one cycle" lesson).

    Persistence is same-time-yesterday from the REAL load sensor's own
    history, shifted forward 24 h onto this window's grid. That needs no
    new storage either, and it is the baseline forecast_regret.py's own
    docstring already recommends for a real writer.
    """
    blank = dict.fromkeys(_NOWCAST_SKILL_KEYS)

    trail_hist = fetch_entity_history_range(
        _LOAD_NOWCAST_TRAIL_ENTITY_ID, day_start, day_end
    )
    if not trail_hist:
        _LOGGER.debug(
            "Nimbus quality: no load-nowcast skill. No recorded history for "
            "%s over [%s, %s] -- expected on an install that has not run "
            "this version for a full window yet",
            _LOAD_NOWCAST_TRAIL_ENTITY_ID,
            day_start.isoformat(),
            day_end.isoformat(),
        )
        return {**blank, "load_nowcast_skill_reason": "no_forecast_trail"}

    # No _kw_scale_factor() on the trail: it is Nimbus's own published
    # sensor and always declares kW. The REAL load sensor is a household
    # entity that may report W, so its own scale is passed in and applied
    # to the persistence series below -- the same factor already applied
    # to load_real_kw by the caller.
    shift = timedelta(hours=24)
    prev_hist = fetch_entity_history_range(
        load_sensor, day_start - shift, day_end - shift
    )
    if not prev_hist:
        _LOGGER.debug(
            "Nimbus quality: no load-nowcast skill. No real load history for "
            "the preceding 24 h, so there is no persistence baseline to "
            "compare against"
        )
        return {**blank, "load_nowcast_skill_reason": "no_persistence_baseline"}

    measured, _total = nowcast_skill.period_sample_coverage(
        [t for t, _v in trail_hist], grid_times, period_hours
    )
    load_nowcast_kw = np.array(
        resample_history_mean(trail_hist, grid_times, period_hours)
    )
    load_persistence_kw = np.array(
        resample_history_mean(
            [(t + shift, v * load_scale) for t, v in prev_hist],
            grid_times,
            period_hours,
        )
    )

    result = nowcast_skill.compute_load_nowcast_skill(
        periods=periods,
        grid=grid,
        battery=battery,
        solar_real_kw=solar_real_kw,
        load_real_kw=load_real_kw,
        load_nowcast_kw=load_nowcast_kw,
        load_persistence_kw=load_persistence_kw,
        n_periods_measured=measured,
    )
    if result is None:
        # nimbus issue #1176: separate the two causes this branch used to
        # conflate, and publish the coverage that failed rather than
        # nulling a number already in hand.
        #
        # No solver-package change is needed for the split. `measured`
        # and `len(grid_times)` are the two figures the old debug line
        # already printed, and `DEFAULT_MIN_COVERAGE` is exported, so
        # "coverage below the bar" is decidable right here. Anything else
        # returning None is the scenario solve, which
        # compute_load_nowcast_skill() swallows on purpose (#366/#373's
        # "degrade, never wedge").
        n_periods = len(grid_times)
        coverage = (measured / n_periods) if n_periods else 0.0
        below = coverage < nowcast_skill.DEFAULT_MIN_COVERAGE

        # nimbus issue #1188 (Mark Purcell): the split above was binary --
        # coverage, or else the solve -- and compute_load_nowcast_skill()
        # has THREE None returns, not two. It refuses an input whose
        # arrays disagree in length with the period grid, BEFORE the
        # coverage gate and before any solve is attempted. Folding that
        # into "scenario_solve_failed" points a household at a solver
        # problem that was never reached.
        #
        # Decided here the same way coverage is, and in the function's own
        # order -- length, then coverage, then the solve -- so the label
        # names the gate that actually closed.
        #
        # Compared against `len(grid_times)` rather than
        # `len(periods.hours)`: the two are constructed together at the
        # real call site, and grid_times is already in hand here, so this
        # needs nothing from `periods` that the blank-path callers may not
        # supply.
        #
        # Dormant today, and worth saying so: the one production call site
        # builds all four arrays to the same length, so this branch is not
        # currently reachable. It stops being dormant the moment this
        # function gains a second caller with independently sized arrays,
        # which its own docstring does not forbid.
        mismatched = [
            name
            for name, arr in (
                ("solar_real_kw", solar_real_kw),
                ("load_real_kw", load_real_kw),
                ("load_nowcast_kw", load_nowcast_kw),
                ("load_persistence_kw", load_persistence_kw),
            )
            if arr is not None and len(arr) != n_periods
        ]
        if mismatched:
            reason = "input_length_mismatch"
        elif below:
            reason = "coverage_below_threshold"
        else:
            reason = "scenario_solve_failed"
        _LOGGER.debug(
            "Nimbus quality: no load-nowcast skill (%s). %d of %d periods "
            "carried a real recorded sample (coverage %.3f against a %.2f "
            "minimum)%s",
            {
                "input_length_mismatch": "input arrays disagree with the grid",
                "coverage_below_threshold": "coverage below threshold",
                "scenario_solve_failed": "a scenario solve failed",
            }[reason],
            measured,
            n_periods,
            coverage,
            nowcast_skill.DEFAULT_MIN_COVERAGE,
            f" -- mismatched: {', '.join(mismatched)}" if mismatched else "",
        )
        return {
            **blank,
            "load_nowcast_skill_reason": reason,
            # Known in this branch and previously discarded. Telling a
            # household the check did not run, without telling them it
            # missed the bar by 0.31, is the absence-as-the-only-signal
            # shape this repo keeps recording.
            "load_nowcast_skill_coverage": round(coverage, 3),
            "load_nowcast_skill_periods_measured": measured,
        }

    return {
        "load_nowcast_skill_j_star": round(result.j_star, 4),
        "load_nowcast_skill_j_forecast": round(result.j_forecast, 4),
        "load_nowcast_skill_j_persistence": round(result.j_persistence, 4),
        "load_nowcast_skill_value_add_dollars": round(result.value_add_dollars, 4),
        "load_nowcast_skill_coverage": round(result.coverage, 3),
        "load_nowcast_skill_periods_measured": result.n_periods_measured,
        # Explicitly None rather than omitted: this function's contract
        # is that every key in _NOWCAST_SKILL_KEYS is always present, so
        # a consumer never sees one appear and vanish between windows
        # (#589). Leaving it out here was caught by this change's own
        # test rather than in review.
        "load_nowcast_skill_reason": None,
    }


def _compute_report_for_window(
    cfg: dict,
    day_start: datetime,
    day_end: datetime,
    allow_partial: bool = False,
) -> dict | None:
    """Score an arbitrary [day_start, day_end] window and return the
    same dict shape compute_daily_quality_report() has always returned.

    Extracted from compute_daily_quality_report() in v0.94.42 (issue
    #316) so a service call can score any real historical window --
    diagnostics, backfill after a silent scoring freeze (issue #312),
    A/B comparing a fix candidate against a fixed reference day. Zero
    behaviour change for the "yesterday" wrapper: when called with the
    same calendar-day boundaries this function's own caller has always
    passed, it computes the exact same numbers via the same code path.

    Arguments:
    - cfg: fetch_solver_config() output (same as every other writer).
    - day_start, day_end: timezone-aware datetimes bounding the window
      to score. Must satisfy day_start < day_end.
    - allow_partial: if False (the default, matching the yesterday
      caller), returns None for windows shorter than 24 h. Real
      calendar-day scoring is what every existing caller has always
      relied on. When True, scores any real window with at least one
      full period of data (5-min resolution for a window of 24h or
      less, matching the live dispatch grid's own tier-1 resolution --
      see #438; 15-min for anything longer). Partial-window scores are honest
      (they score exactly what is in the window) but the EPR / regret
      numbers are NOT directly comparable to a full-day score, because
      the oracle's own optimisation horizon is shorter.

    P2P settlement history lookup is retained only when the window
    exactly matches a real calendar day. The settlement sensor's own
    history dict is keyed by ISO date, and a lookup on a non-calendar-
    aligned window is meaningless. Cross-midnight and partial-day
    windows publish with real_p2p_dollars=0 / real_p2p_volume_kwh=0,
    the same as an install with no settlement sensor configured.

    Returns None when either power sensor is missing, real history is
    not available for the requested window, the oracle solve is
    genuinely infeasible, or allow_partial is False and the window
    is shorter than 24 hours.
    """
    if day_end <= day_start:
        _LOGGER.debug(
            "Nimbus quality: skip. Window end (%s) not after start (%s)",
            day_end.isoformat(),
            day_start.isoformat(),
        )
        return None

    solar_sensor = cfg.get("solver_solar_power_sensor")
    battery_sensor = cfg.get("solver_battery_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    if not solar_sensor or not battery_sensor or not load_sensor:
        _LOGGER.debug(
            "Nimbus quality: skip. Missing sensor config (solar=%s battery=%s "
            "load=%s) -- configure all three under Solver settings to enable "
            "quality scoring",
            solar_sensor,
            battery_sensor,
            load_sensor,
        )
        return None

    window_hours = (day_end - day_start).total_seconds() / 3600.0
    if not allow_partial and window_hours < 24.0:
        _LOGGER.debug(
            "Nimbus quality: skip. Window is %.2f h, shorter than the 24 h a "
            "full-day score requires (allow_partial=False)",
            window_hours,
        )
        return None

    # nimbus issue #438 (Mark Purcell), updated for #451: this used to
    # hardcode 0.25h (15 min) regardless of window length -- coarser
    # than both the live dispatch grid it's grading and the real
    # settlement interval (NEM, 5 min since Oct 2021). A window that
    # fits entirely within tier1's own real span (MAX_TIER1_HOURS, now
    # at most 60 real minutes since #451's boundary-snapped reshape --
    # see build_tiered_grid()'s own docstring) matches tier1's own real
    # 5-min resolution. Every existing caller's window is far longer
    # than that (the daily "yesterday" wrapper is always exactly 24h),
    # so this now falls through to TIER2_PERIOD_HOURS (30 min, was a
    # separately-hardcoded 0.25/15min before #451) for the same reason
    # #438 originally cared about: matching the live dispatch's own
    # ACTUAL dominant resolution for the window being scored, which for
    # any window longer than an hour is now tier 2's 30-min cadence, not
    # tier 1's brief 5-min one. Reusing TIER2_PERIOD_HOURS directly
    # (rather than a second independent literal) is the same "can't
    # silently drift apart" fix already applied elsewhere in this
    # project (see e.g. the dispatch card's own --ftable-min-width).
    period_hours = (
        TIER1_PERIOD_HOURS if window_hours <= MAX_TIER1_HOURS else TIER2_PERIOD_HOURS
    )
    n_periods = round(window_hours / period_hours)
    if n_periods < 1:
        _LOGGER.debug(
            "Nimbus quality: skip. Window (%.4f h) rounds to fewer than one "
            "%.2f h period",
            window_hours,
            period_hours,
        )
        return None
    grid_times = [
        day_start + timedelta(hours=i * period_hours) for i in range(n_periods)
    ]
    period_hours_arr = np.full(n_periods, period_hours)
    solar_hist = fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = fetch_entity_history_range(load_sensor, day_start, day_end)
    battery_hist = fetch_entity_history_range(battery_sensor, day_start, day_end)
    if not solar_hist or not load_hist or not battery_hist:
        _LOGGER.info(
            "Nimbus quality: skip. Real history missing for window "
            "[%s, %s] (solar=%d, load=%d, battery=%d rows) -- either the "
            "window predates when these sensors started recording, or one "
            "of them went unavailable for the whole window",
            day_start.isoformat(),
            day_end.isoformat(),
            len(solar_hist),
            len(load_hist),
            len(battery_hist),
        )
        return None

    # nimbus issue #984: a NON-EMPTY history is not a COVERING one, and
    # until now the emptiness check above was the only gate.
    #
    # Observed on a real install, every morning at ~06:02 for at least
    # three consecutive days: the daily rescore published j_ref 2.34 /
    # j_ach -0.57 / j_star -9.91 (regret +9.34, EPR 23.77%), then ~5
    # minutes later the same day settled to j_ref 4.79 / j_ach -15.90 /
    # j_star -15.17 (regret -0.73, EPR 103.66%). Every figure in the
    # first set is a fraction of the second: a PARTIAL window scored as
    # if it were a full day.
    #
    # `window_hours` above cannot catch it -- it measures the window
    # REQUESTED (always exactly 24 h here), never what the recorder
    # actually returned. One row per sensor satisfied the emptiness
    # check and the report went out as a valid daily score.
    #
    # The cost is not a dashboard blip: the wrong value is WRITTEN TO
    # HISTORY and to long-term statistics, so any chart aggregating a day
    # by max/first/last keeps picking it up afterwards. A household
    # reading "regret $9.34" on three consecutive days was reading this,
    # not their dispatch.
    #
    # Refusing returns None, which the caller already treats as "leave
    # the sensor alone, retry next cycle" -- and that retry is what
    # produced the correct score at 06:07 on its own.
    if not allow_partial:
        # nimbus issue #1054 (Mark Purcell, real production install):
        # measure per sensor, not just the minimum. The threshold test
        # below is unchanged -- it still uses the worst number -- but
        # the skip line now names WHICH sensor was short and by how
        # much, alongside its entity_id. #1054's stated next step was a
        # manual, paginated recorder pull of all three sensors to find
        # that out, plus the gap boundary; this line answers both
        # without one. (An entirely-absent sensor never gets here --
        # #314's row-count guard above catches it first and already
        # names it per sensor.)
        coverage = _history_coverage_by_series(
            {
                str(solar_sensor): solar_hist,
                str(load_sensor): load_hist,
                str(battery_sensor): battery_hist,
            },
            day_start,
            day_end,
        )
        covered = min(cov.hours for cov in coverage.values())
        if covered < window_hours * _MIN_DAILY_COVERAGE_FRACTION:
            # Worst first -- the one that actually failed the gate leads.
            # The bracketed span is the covered region's own edges in
            # local time; for a truncated window that is the gap
            # boundary #1054 went looking for by hand.
            breakdown = ", ".join(
                f"{name} {cov.hours:.2f} h"
                + (
                    " [no history]"
                    if cov.first is None or cov.last is None
                    else f" [{cov.first.astimezone(LOCAL_TZ):%H:%M}-"
                    f"{cov.last.astimezone(LOCAL_TZ):%H:%M}]"
                )
                for name, cov in sorted(coverage.items(), key=lambda kv: kv[1].hours)
            )
            day_key = day_start.date().isoformat()
            seen = _COVERAGE_SKIP_COUNTS.get(day_key, 0) + 1
            _COVERAGE_SKIP_COUNTS[day_key] = seen
            # Routine on the first retries, a real WARNING once it is
            # clearly not the recorder catching up -- see
            # _COVERAGE_SKIP_WARN_AFTER for why this is not one level.
            log = _LOGGER.info if seen < _COVERAGE_SKIP_WARN_AFTER else _LOGGER.warning
            log(
                "Nimbus quality: skip #%d for %s. Real history covers only "
                "%.2f h of the %.2f h window, under the %.0f%% a full-day "
                "score requires. Per sensor, worst first: %s. Retrying next "
                "cycle rather than publishing a partial window as a full-day "
                "score (nimbus issue #984). The bracketed span is each "
                "sensor's own covered region -- for a truncated window that "
                "is the gap boundary; the one short of the rest is the one "
                "to chase in the recorder (nimbus issue #1054).",
                seen,
                day_key,
                covered,
                window_hours,
                _MIN_DAILY_COVERAGE_FRACTION * 100.0,
                breakdown,
            )
            return None
        # Scored cleanly -- let a day that recovers stop warning.
        _COVERAGE_SKIP_COUNTS.pop(day_start.date().isoformat(), None)

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

    # nimbus issue #1214: a CONFIGURED SoC sensor that returns no history
    # is a transient failure, and scoring the day anyway invents the
    # battery's opening state.
    #
    # **What it costs when it is allowed through.** `initial_pct` below
    # falls to its hardcoded 50.0 default. Measured on the reference
    # household for 2026-09-24, whose real midnight SoC was 16.6%: the
    # day published **EPR 21.11%** with `j_ach -3.05` and `regret
    # $21.03`, while a second install scoring the same day from the same
    # mirrored sensors -- but with the real SoC -- published **68.86%**
    # with `j_ach -14.62` and `regret $8.50`. The hourly `battery_kw`
    # series were identical to four decimals on both, so the entire
    # 47.75-point difference is this one number.
    #
    # Starting 33 points high also drives the achieved trajectory through
    # the top of the pack: that day reported `achieved_soc_max_pct
    # 125.48` and `achieved_above_ceiling_kwh 30.52`. A reconstruction
    # above 100% SoC is not a score with a caveat, it is arithmetic about
    # a battery that does not exist.
    #
    # **Why it is transient, and therefore worth retrying rather than
    # publishing.** `fetch_entity_history_range()` waits
    # `future.result(timeout=30)` on a recorder read and degrades to []
    # on any failure, logging only at DEBUG. On the reference install the
    # daily retrain runs at 06:00 and saturates the executor: the 06:00
    # rescore started at 06:00:03.517 and the first "previous cycle still
    # in progress" warning landed at 06:00:33.638 -- 30.1 s later, the
    # timeout expiring to the second. All 21 of that day's skip warnings
    # fell in hour 06 and none in the other 23, which is exactly why this
    # only ever corrupts the 06:00 rescore. The next cycle reads the same
    # history without trouble.
    #
    # So: refuse, and let the existing retry win it back. This is the
    # same posture the solar/load/battery emptiness check above already
    # takes, and the same one #984's coverage gate takes -- returning
    # None means "leave the sensor alone, retry next cycle", never a lost
    # day.
    #
    # **Deliberately scoped to a CONFIGURED sensor.** An install with no
    # `solver_battery_soc_sensor` at all has nothing to wait for and is
    # not experiencing a failure; it keeps the existing default and the
    # existing behaviour, byte-identical. Refusing there would silently
    # stop scoring every install that has never configured one.
    if soc_sensor and not soc_hist:
        day_key = day_start.date().isoformat()
        seen = _SOC_HISTORY_SKIP_COUNTS.get(day_key, 0) + 1
        _SOC_HISTORY_SKIP_COUNTS[day_key] = seen
        log = _LOGGER.info if seen < _COVERAGE_SKIP_WARN_AFTER else _LOGGER.warning
        log(
            "Nimbus quality: skip #%d for %s. The configured battery SoC "
            "sensor %r returned no history for [%s, %s], so this day's "
            "opening state of charge is unknown. Scoring anyway would "
            "silently assume %.1f%% and, if that is wrong, mis-price the "
            "whole day -- a real install published EPR 21.11%% instead of "
            "68.86%% from exactly this (nimbus issue #1214). Retrying next "
            "cycle rather than publishing a score built on an assumed "
            "battery. A recorder read that times out under load (the "
            "daily retrain is the known one) usually succeeds on the very "
            "next attempt.",
            seen,
            day_key,
            soc_sensor,
            (day_start - timedelta(hours=6)).isoformat(),
            day_end.isoformat(),
            _ASSUMED_INITIAL_SOC_PCT,
        )
        return None

    # Real, confirmed-live bug (2026-08-28) -- see _kw_scale_factor()'s
    # own docstring: these three configured sensors are never guaranteed
    # to already report kW (solar in particular is commonly a native
    # Watts sensor), and the rest of this function has always assumed
    # they are without checking.
    solar_scale = _kw_scale_factor(solar_sensor)
    load_scale = _kw_scale_factor(load_sensor)
    battery_scale = _kw_scale_factor(battery_sensor)
    # Real, confirmed-live bug found by Mark Purcell (issue #299,
    # 2026-08-31): this function always assumed the configured battery
    # sensor follows this project's own established convention
    # (positive = discharge, matching the reference household's real
    # sensor.logger_battery_power) with no way to say otherwise. A
    # SigEnergy plant's own sensor reports the OPPOSITE sign (positive =
    # charge) -- every charge event was silently booked as a discharge
    # and vice versa, producing a structurally impossible EPR (-137.47%;
    # EPR can never go negative when scored correctly, since a
    # perfect-foresight oracle can never be beaten). See
    # CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE's own comment in
    # const.py. False (the default) reproduces the exact original
    # behaviour -- this is a pure multiply-by-plus-or-minus-1, so it's a
    # complete no-op for every install that never sets the flag.
    battery_sign = -1.0 if cfg.get("solver_battery_power_positive_is_charge") else 1.0

    # nimbus issue #428 (Mark Purcell): period-mean, not point-sample --
    # see resample_history_mean()'s own docstring for the real spike this
    # was confirmed to fix.
    solar_kw = np.array(
        [
            max(0.0, v * solar_scale)
            for v in resample_history_mean(solar_hist, grid_times, period_hours)
        ]
    )
    load_kw = np.array(
        [
            max(0.0, v * load_scale)
            for v in resample_history_mean(load_hist, grid_times, period_hours)
        ]
    )
    actual_net_kw = np.array(
        [
            v * battery_scale * battery_sign
            for v in resample_history_mean(battery_hist, grid_times, period_hours)
        ]
    )
    # nimbus issue #1181: measure how much of this window the home
    # battery's power sensor actually observed, and publish it -- do NOT
    # adjust `actual_net_kw` above.
    #
    # The participant path zeroes its stale periods
    # (`_stale_power_period_indices()`'s one call site until now), and
    # doing the same here would be wrong rather than merely different:
    # zeroing is conservative for THROUGHPUT but the home reconstruction
    # already under-rises through charge, so zeroing would deepen that,
    # increase `soc_discrepancy`, and make a household's EPR read less
    # reliable because of a fix. See `_power_history_coverage()`.
    home_power_coverage = _power_history_coverage(
        battery_hist, grid_times, period_hours
    )
    actual_charge_kw = np.array([max(0.0, -v) for v in actual_net_kw])
    actual_discharge_kw = np.array([max(0.0, v) for v in actual_net_kw])
    # No generic commanded-dispatch signal exists -- see this function's
    # own docstring. commanded = actual (for "home" and for every
    # participant below) makes tracking_fidelity/tracking_cost trivially
    # perfect by construction, an honest reflection of "nothing here
    # ever actually dispatched," not a claim about real-world execution
    # quality.

    import_price = np.array(
        [
            v + import_fee_rate(cfg, _local(grid_times[i]).hour)
            for i, v in enumerate(
                resample_history_nearest(
                    import_price_hist, grid_times, default=0.20, backfill_first=True
                )
            )
        ]
    )
    export_price = np.array(
        resample_history_nearest(
            export_price_hist, grid_times, default=0.05, backfill_first=True
        )
    )

    # nimbus issue #1013: SoH-derated, same as every other BatteryConfig
    # construction -- see resolve_effective_capacity_kwh()'s docstring.
    capacity_kwh = resolve_effective_capacity_kwh(cfg)
    min_pct = _cfg_num(cfg, "solver_battery_min_soc_percent", 5.0)
    max_pct = _cfg_num(cfg, "solver_battery_max_soc_percent", 100.0)
    initial_pct = (
        resample_history_nearest(
            soc_hist,
            [day_start],
            default=_ASSUMED_INITIAL_SOC_PCT,
            backfill_first=True,
        )[0]
        if soc_hist
        else _ASSUMED_INITIAL_SOC_PCT
    )
    final_pct = (
        resample_history_nearest(
            soc_hist,
            [day_end - timedelta(seconds=1)],
            default=initial_pct,
            backfill_first=True,
        )[0]
        if soc_hist
        else initial_pct
    )
    initial_soc_kwh_raw = capacity_kwh * initial_pct / 100.0
    final_soc_kwh_raw = capacity_kwh * final_pct / 100.0

    # nimbus issue #328 (Mark Purcell) -- honest pass-through, no clamp,
    # same fix and same reasoning as main()'s own initial_soc_kwh site.
    # The #325/#327 clamp this replaced stopped a real crash (8.6h of
    # sensor.nimbus_solver_quality_report and all nine sensor.nimbus_
    # quality_* sensors sitting `unavailable` across 103+ failed
    # publishes, from elements.BatteryConfig's own invariant rejecting a
    # historical SoC reading below the configured floor -- a template-
    # averaged SoC sensor, a fault, a cold pack, a fresh install starting
    # empty, sensor drift, or a recorder gap can all legitimately produce
    # one), but it did so by feeding the LP-oracle path (J_ref) a
    # DIFFERENT, fictional starting state than the achieved-trajectory
    # path (J_ach) -- Mark's own issue #328 traces exactly how this makes
    # the resulting EPR ratio meaningless, comparing two trajectories
    # that started from different states. elements.BatteryConfig now
    # only requires a value to sit inside the PHYSICAL range [0,
    # capacity_kwh] (never raises for a below-floor/above-ceiling
    # historical reading on its own), and the LP's own soc[t]/
    # underfill[t]/overfill[t] construction treats min_soc/max_soc as a
    # soft preference -- so both J_ref and J_ach can now see the SAME
    # true historical state honestly.
    min_soc_kwh_bound = capacity_kwh * min_pct / 100.0
    max_soc_kwh_bound = capacity_kwh * max_pct / 100.0
    initial_soc_kwh = initial_soc_kwh_raw
    final_soc_kwh_actual = final_soc_kwh_raw
    if not (min_soc_kwh_bound <= initial_soc_kwh_raw <= max_soc_kwh_bound) or not (
        min_soc_kwh_bound <= final_soc_kwh_raw <= max_soc_kwh_bound
    ):
        _LOGGER.warning(
            "Nimbus Solver: historical SoC outside configured [%.2f%%, %.2f%%] "
            "envelope for this scorer window (start %.2f%%, end %.2f%%) -- "
            "scoring the real trajectory honestly, both J_ref and J_ach see "
            "this true state. Usual real causes: a template-averaged SoC "
            "sensor (an EV-charger channel reading 0%% when unplugged), a "
            "fault, a cold pack, or a recorder gap.",
            min_pct,
            max_pct,
            initial_pct,
            final_pct,
        )
    # A genuinely PHYSICAL clamp still has to stay here, unlike the
    # scheduling-envelope clamp removed above: elements.BatteryConfig
    # only relaxed the [min_soc, max_soc] SCHEDULING invariant (#328),
    # it still (correctly) rejects a value outside the true physical
    # range [0, capacity_kwh] -- more energy than the battery can
    # physically hold, or negative energy, isn't a "real historical
    # state" for the LP to score honestly, it's sensor nonsense (a
    # calibration artefact, template-averaging overshoot). Clamping
    # HERE, to the physical envelope only, preserves #325's own original
    # guarantee (this scorer must never raise on a bad reading) without
    # reintroducing #328's bug (silently rewriting a real, physically
    # valid but out-of-schedule state before the LP ever sees it).
    if not (0.0 <= initial_soc_kwh_raw <= capacity_kwh):
        initial_soc_kwh = min(max(initial_soc_kwh_raw, 0.0), capacity_kwh)
        _LOGGER.warning(
            "Nimbus Solver: historical starting SoC (%.2f%%) is outside the "
            "battery's own PHYSICAL range [0%%, 100%%] -- clamping to %.4f "
            "kWh to keep this scorer alive. This is sensor nonsense "
            "(calibration drift, a template-averaging overshoot), not a "
            "real state -- investigate the sensor if this recurs.",
            initial_pct,
            initial_soc_kwh,
        )
    if not (0.0 <= final_soc_kwh_raw <= capacity_kwh):
        final_soc_kwh_actual = min(max(final_soc_kwh_raw, 0.0), capacity_kwh)
        _LOGGER.warning(
            "Nimbus Solver: historical ending SoC (%.2f%%) is outside the "
            "battery's own PHYSICAL range [0%%, 100%%] -- clamping to %.4f "
            "kWh to keep this scorer alive. This is sensor nonsense "
            "(calibration drift, a template-averaging overshoot), not a "
            "real state -- investigate the sensor if this recurs.",
            final_pct,
            final_soc_kwh_actual,
        )

    # Flat economics only -- deliberately NOT the household-specific
    # day/night discharge-cost/salvage-value schedule main() applies
    # for a LocalVolts-configured install (see that branch's own
    # comment, "tuned specifically around this household's own P2P
    # window, no portable equivalent yet"). This scorer always uses the
    # same flat config-flow values every OTHER install's forward plan
    # already falls back to.
    min_soc_kwh = min_soc_kwh_bound
    max_soc_kwh = max_soc_kwh_bound
    battery_cfg = elements.BatteryConfig(
        # nimbus issue #467: this writer only ever configures the one
        # real household battery today -- "home" is a plain, stable
        # identifier, not yet a config-flow field (a genuine multi-
        # battery config surface is later #467 work, out of scope for
        # this stage).
        name="home",
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=min_soc_kwh,
        max_soc_kwh=max_soc_kwh,
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
        # nimbus issue #336 (Mark Purcell's live-dashboard finding,
        # 2026-09-04): this scorer's own battery config never populated
        # degradation_cost_per_kwh at all, defaulting it to 0.0 -- so
        # j_ref/j_ach (via evaluate_realized_cost(), regret.py) and
        # j_star/the oracle (via build_plan(), network.py) all scored
        # a battery that cycles for free, while main()'s own REAL live
        # dispatch battery config (see its matching comment) prices this
        # field for real. For an install with it configured nonzero
        # (e.g. 3c/kWh), the oracle in particular over-cycled for "free"
        # arbitrage the real household would never actually find
        # worthwhile net of degradation -- inflating regret_dollars.
        # Threading the same real value through here means j_ref/j_ach/
        # j_star all price the SAME battery physics, matching the
        # comment on charge_efficiency/discharge_efficiency above about
        # why that parity matters for EPR/regret to mean anything.
        degradation_cost_per_kwh=_cfg_num(cfg, "solver_degradation_cost_per_kwh", 0.0),
        # ZERO, not the configured forward-planning solver_salvage_value, and
        # deliberately no terminal_value_breakpoints either (issue: EPR>100%,
        # negative regret_dollars, reported live 2026-08-29/30). This scorer
        # evaluates exactly ONE already-elapsed calendar day in isolation --
        # crediting leftover end-of-day SoC (flat OR via a concave curve) is
        # a guess about tomorrow's value this function has no honest basis
        # for making. Verified against a real incident day: flat salvage
        # gave 145.0% EPR/-$18.15 regret (invalid), a concave curve gave
        # 127.7%/-$11.14 (still invalid -- ANY positive per-kWh credit for
        # leftover energy still over-rewards a trajectory that accidentally
        # under-delivered that day), salvage_value=0.0 gave 76.0%/+$8.94
        # (both valid). Tomorrow's own quality report, run independently
        # against tomorrow's real initial_soc_kwh, is what actually prices
        # whatever gets carried forward -- not this one.
        salvage_value=0.0,
    )

    # nimbus #768/#585 (Mark Purcell): extend the scored fleet with any
    # configured battery_participant subentries (EVs sharing this hub),
    # reusing each one's own already-configured power sensor as its
    # real historical dispatch baseline -- see _resolve_battery_
    # participant_history()'s own docstring for the full reasoning and
    # what's deliberately still out of scope (a shared-charger sensor
    # disambiguation). Zero participants (every install before this
    # change, and any standalone/cron deployment) returns [] -- `home`
    # stays the only scored battery, byte-identical to before.
    participant_batteries = _resolve_battery_participant_history(
        day_start=day_start,
        day_end=day_end,
        grid_times=grid_times,
        period_hours=period_hours,
        n_periods=n_periods,
    )
    batteries = [battery_cfg, *(p[0] for p in participant_batteries)]
    actual_charge_kw_list = [actual_charge_kw, *(p[1] for p in participant_batteries)]
    actual_discharge_kw_list = [
        actual_discharge_kw,
        *(p[2] for p in participant_batteries),
    ]
    final_soc_kwh_actual_list = [
        final_soc_kwh_actual,
        *(p[3] for p in participant_batteries),
    ]
    # nimbus issue #949 (Mark Purcell's own chosen fix): the real,
    # measured side of the SoC-discrepancy comparison, fleet-blended the
    # SAME way `j_ach_soc_pct`'s own reconstruction is -- one
    # `(soc_hist, capacity_kwh)` pair per battery actually scored today,
    # home first. Passed to `_soc_discrepancy_stats()` below instead of
    # the home sensor's `soc_hist` alone, so both sides of the comparison
    # measure the same quantity on a multi-battery install.
    battery_socs_for_discrepancy = [
        (soc_hist, capacity_kwh),
        *((p[4], p[0].capacity_kwh) for p in participant_batteries),
    ]
    # No generic commanded-dispatch signal exists for a participant
    # either -- same honest "commanded = actual" convention as "home"
    # above (this function's own docstring already covers why).
    commanded_charge_kw_list = actual_charge_kw_list
    commanded_discharge_kw_list = actual_discharge_kw_list

    grid_residual = elements.GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=_cfg_num(cfg, "solver_grid_max_import_kw", 20.0),
        export_limit_kw=_cfg_num(cfg, "solver_grid_max_export_kw", 20.0),
    )

    # Real bug found live (2026-09-01, direct household catch on a
    # reconstructed dispatch-regret chart): the oracle's own LP re-solve
    # (build_plan(), called on grid_oracle below) previously had no idea
    # this household's real P2P program is a FIXED, committed export
    # RATE during specific hours (e.g. 11.5kW, 17:00-24:00 -- see
    # fetch_p2p_fixed_export_kw()'s own docstring), not an open market it
    # could export up to solver_grid_max_export_kw (42kW) into whenever
    # spot-plus-bonus pricing looked attractive. The oracle was
    # accordingly "solving" a fictional market -- e.g. wanting to dump
    # 34-40kW in a single hour when the real, physically-committed rate
    # was 11.5kW -- systematically overstating both J_star's own achieved
    # value and therefore every regret/EPR number derived from it.
    # fetch_p2p_fixed_export_kw() already exists and is already the
    # correct, tested mechanism main()'s own forward-planning branch
    # uses for exactly this constraint -- reused verbatim here, not
    # reimplemented, so the retrospective scorer and the forward plan can
    # never model this household's real P2P commitment two different
    # ways. Applied to grid_oracle only: evaluate_realized_cost() (which
    # grid_residual feeds, for J_ref/J_ach) prices an already-fixed,
    # already-happened trajectory against real prices/limits -- it has
    # no LP constraints to pin in the first place. Only build_plan()'s
    # own genuine re-solve (grid_oracle, for J_star) can meaningfully be
    # constrained by a fixed rate at all.
    fixed_export_kw = fetch_p2p_fixed_export_kw(cfg, grid_times)

    # Optional real settlement hook -- see CONF_SOLVER_P2P_SETTLEMENT_
    # HISTORY_SENSOR's own comment in const.py. Retailer-agnostic in
    # SHAPE: any entity whose 'history' attribute holds real
    # {date: {export_cost, export_volume}} entries works.
    real_p2p_dollars = 0.0
    real_p2p_volume_kwh = 0.0
    # nimbus issue #1015: WHY those are zero. Without this the report
    # carries `real_p2p_dollars: 0` and prices export at plain spot,
    # which is indistinguishable from a household that earns no P2P at
    # all -- no error, no flag, an ordinary-looking number.
    #
    # Measured: scoring 2026-09-15 as a UTC-aligned 24 h window returned
    # 0 against $10.4032 for the same day as a local calendar day,
    # moving `j_ach` by $9.82. The gate itself is right -- settlement
    # history is keyed by ISO local date, so a window that is not one
    # real local day has no entry to look up -- but being silent about
    # it is not.
    real_p2p_settlement_status = "no_sensor_configured"
    grid_oracle = (
        elements.GridConfig(
            import_price=import_price,
            export_price=export_price,
            import_limit_kw=grid_residual.import_limit_kw,
            export_limit_kw=grid_residual.export_limit_kw,
            fixed_export_kw=np.array(fixed_export_kw),
        )
        if fixed_export_kw is not None
        else grid_residual
    )
    settlement_sensor = cfg.get("solver_p2p_settlement_history_sensor")
    # nimbus issue #1236: a real P2P commitment with nothing to reconcile
    # it against is a misconfiguration, and it used to be completely
    # silent.
    #
    # `no_sensor_configured` is PERMANENT -- it is deliberately excluded
    # from `_PROVISIONAL_SETTLEMENT_STATUSES`, so #1201's repair sweep
    # will never revisit such a day, and correctly so: there is nothing to
    # wait for. But the day is then priced with zero P2P export credit and
    # `real_p2p_dollars: 0.0`, which is indistinguishable from a household
    # that genuinely earns no P2P -- no error, no flag, an ordinary-looking
    # number, on every day, forever. Measured on the reference household,
    # the gap is not small: 24 Sep read 41.6% scored P2P-blind against
    # 68.85% with real settlement applied, and 21-23 Sep read 51.9/38.5/
    # 36.4% against 89.8/87.7/87.2%.
    #
    # Gated on `fixed_export_kw is not None`, which is exactly "at least
    # one P2P block has rate_kw > 0" (see fetch_p2p_fixed_export_kw()'s
    # own docstring -- it returns None when every block is unconfigured).
    # So an install with no P2P scheme at all stays completely silent,
    # which matters: an unconditional warning here would fire on every
    # scored day of every install that does not use the feature, and a
    # warning that is always present is a warning nobody reads.
    #
    # The sibling status already got this reasoning -- see the comment on
    # `window_is_not_one_local_calendar_day` a few lines above, which ends
    # "The gate itself is right ... but being silent about it is not."
    # This is that same argument applied to the case it skipped.
    if not settlement_sensor and fixed_export_kw is not None:
        _LOGGER.warning(
            "Nimbus: this install commits real P2P export (a P2P block is "
            "configured with rate_kw > 0) but no P2P settlement history "
            "sensor is set, so %s is being scored with ZERO P2P export "
            "credit and will never be re-scored -- "
            "real_p2p_settlement_status 'no_sensor_configured' is "
            "permanent, not provisional. Set Configure -> Solver settings "
            "-> P2P settlement history sensor to score the real settled "
            "revenue (nimbus issue #1236)",
            day_start.date().isoformat(),
        )
    # Real settlement history is keyed by ISO date, so it is only
    # meaningful when the window exactly matches one real calendar day
    # in the local timezone. Cross-midnight windows and partial-day
    # windows deliberately skip this branch and price export at the
    # plain configured rate for J_ach and J_star alike -- the same
    # honest fallback an install with no settlement sensor configured
    # already uses.
    is_calendar_day = (
        window_hours == 24.0
        and day_start.astimezone(LOCAL_TZ).time().hour == 0
        and day_start.astimezone(LOCAL_TZ).time().minute == 0
    )
    if settlement_sensor and not is_calendar_day:
        real_p2p_settlement_status = "window_is_not_one_local_calendar_day"
    if settlement_sensor and is_calendar_day:
        settled_date = day_start.astimezone(LOCAL_TZ).date()
        try:
            day_data = (ha_get(settlement_sensor)["attributes"]["history"]).get(
                settled_date.isoformat()
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            KeyError,
            json.JSONDecodeError,
        ):
            day_data = None
            real_p2p_settlement_status = "settlement_sensor_unreadable"
        if day_data:
            real_p2p_dollars = float(day_data.get("export_cost", 0.0))
            real_p2p_volume_kwh = float(day_data.get("export_volume", 0.0))
            real_p2p_settlement_status = "applied"
            # nimbus #1056 (Mark Purcell, IV&V pass #1055): this block
            # belongs HERE, under `if day_data:`, and nowhere else.
            #
            # #1016 re-indented it one level deeper, into the `elif`
            # below, while adding real_p2p_settlement_status. In that
            # branch day_data is falsy BY CONSTRUCTION, so real_p2p_
            # dollars/real_p2p_volume_kwh were never reassigned and
            # still held their initial 0.0 -- making `> 0.01` always
            # False wherever the code could be reached. The bonus-priced
            # rebuild therefore never ran on ANY install with a
            # settlement sensor, the fully-successful "applied" case
            # included, and j_star -- plus, via this same reused
            # grid_oracle variable, #1026's own j_ref bonus pricing --
            # silently fell back to plain spot export pricing.
            #
            # #1016's commit message said "diagnostic only -- no
            # economics change". It was an economics change, and the
            # tests #1016 added could not see it: they assert the three
            # status/dollars/volume values, all of which are set
            # correctly ABOVE this line. tests/test_p2p_bonus_pricing_
            # reaches_the_oracle.py now spies on GridConfig
            # construction instead, which is the thing that was broken.
            #
            # The general trap, recorded on #873 as well: hoisting or
            # re-indenting a block whose first statement is `if` into an
            # if/elif chain silently re-parents it. Nothing errors.
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
                    # nimbus #1079: gated to the committed periods, not
                    # spread flat over the day -- see
                    # p2p_bonus_price_by_period()'s own docstring for the
                    # real 15 Sep trade this was funding at 05:00 local.
                    export_bonus_price=p2p_bonus_price_by_period(
                        bonus_rate, fixed_export_kw, n_periods
                    ),
                    export_bonus_volume_kwh=real_p2p_volume_kwh,
                    # Preserves the fixed-rate constraint set above --
                    # this branch must never silently drop it just
                    # because a settlement sensor also happens to be
                    # configured. Both can be real at once: fixed_
                    # export_kw pins WHAT the oracle must physically
                    # deliver each committed hour, export_bonus_price/
                    # volume prices however much of that (or beyond it)
                    # earns the real settled P2P rate rather than plain
                    # spot.
                    fixed_export_kw=np.array(fixed_export_kw)
                    if fixed_export_kw is not None
                    else None,
                )
        elif real_p2p_settlement_status != "settlement_sensor_unreadable":
            # Read fine, but this specific date is not in the table --
            # a genuinely unsettled day, not a configuration problem.
            # Nothing to bonus-price here: there is no settled rate for
            # a day that was never settled, so grid_oracle keeps the
            # plain-spot export pricing it was built with above.
            real_p2p_settlement_status = "no_settlement_entry_for_this_date"

    solar_cfg = elements.SolarConfig(forecast_kw=solar_kw)
    load_cfg = elements.LoadConfig(name="whole_house", forecast_kw=load_kw)
    periods = elements.PeriodGrid(hours=period_hours_arr, start=grid_times[0])

    try:
        report = compute_quality_report(
            periods=periods,
            grid_residual=grid_residual,
            grid_oracle=grid_oracle,
            batteries=batteries,
            solar=solar_cfg,
            load=load_cfg,
            timestamps=grid_times,
            real_p2p_dollars_earned=real_p2p_dollars,
            commanded_charge_kw=commanded_charge_kw_list,
            commanded_discharge_kw=commanded_discharge_kw_list,
            actual_charge_kw=actual_charge_kw_list,
            actual_discharge_kw=actual_discharge_kw_list,
            final_soc_kwh_actual=final_soc_kwh_actual_list,
        )
    except RuntimeError as e:
        # Oracle solve genuinely infeasible for this day's real data --
        # skip, same "retry next cycle" convention as every other
        # genuine failure mode here, never a crash. issue #314 (Mark
        # Purcell): this exact path was diagnosed once, by hand, on
        # 2026-08-30 as initial_soc_kwh < min_soc_kwh after a reload --
        # logging the same three values here makes that diagnosis a
        # one-line log read instead of a repeat investigation.
        _LOGGER.warning(
            "Nimbus quality: skip. Oracle LP infeasible for window "
            "[%s, %s] (initial_soc=%.3f min_soc=%.3f max_soc=%.3f kWh): %s",
            day_start.isoformat(),
            day_end.isoformat(),
            initial_soc_kwh,
            min_soc_kwh,
            max_soc_kwh,
            e,
        )
        return None

    # nimbus issue #1081 (Mark Purcell's decision, 2026-09-18): regret is
    # measured against `j_star_evaluator`, not the raw LP objective. The
    # LP is unchanged and `j_star` is still published beside this -- see
    # compute_quality_report()'s own comment at the compute_epr() call
    # for the full reasoning. Both numbers were already on the report;
    # only which one the headline derives from has changed.
    regret_dollars = report.j_ach - report.j_star_evaluator
    # nimbus issue #538 (Mark Purcell, real household finding): these two
    # dashboard-editable thresholds are the "agreement" half of the
    # reliability test -- see _soc_discrepancy_stats()'s own docstring.
    # Same _cfg_num() convention as risk_aversion/etc above (real 0.0 is
    # a legitimate, if unusual, household setting -- never silently
    # swapped for the default).
    # Literal fallback defaults (not an imported const.py DEFAULT_ symbol)
    # -- matches this file's own established convention for a cfg.get()
    # fallback (see import_price_risk_aversion/export_price_risk_aversion
    # above); const.py's DEFAULT_SOLVER_SOC_DISCREPANCY_*_THRESHOLD_PCT
    # is the single source of truth for the NUMBER ENTITY's own seeded
    # default, these two literals are that same value mirrored for the
    # rare case a household's dashboard number hasn't restored yet.
    soc_discrepancy_max_threshold_pct = _cfg_num(
        cfg, "solver_soc_discrepancy_max_threshold_pct", 15.0
    )
    soc_discrepancy_mean_threshold_pct = _cfg_num(
        cfg, "solver_soc_discrepancy_mean_threshold_pct", 8.0
    )
    soc_discrepancy = _soc_discrepancy_stats(
        battery_socs_for_discrepancy,
        report.j_ach_hourly,
        max_threshold_pct=soc_discrepancy_max_threshold_pct,
        mean_threshold_pct=soc_discrepancy_mean_threshold_pct,
        # nimbus issue #1228: the like-for-like achieved side. Absent on a
        # report built before that field existed, which falls back to the
        # hourly mean and says so via soc_discrepancy_basis.
        ach_soc_pct_at_hour=getattr(report, "j_ach_soc_pct_at_hour", None) or None,
        # nimbus issue #1181: measured, not acted on. Lets a reader tell
        # "the reconstruction disagrees" from "the reconstruction had
        # nothing to work with" -- both of which read as
        # `soc_discrepancy_reason: disagreement` today.
        power_coverage=home_power_coverage,
    )
    # nimbus issue #956: the oracle is a bound by construction, so
    # regret < 0 is not a result -- it is proof the comparison was
    # invalid. Measured and published rather than silently passed on as
    # though 103.66% were a score a household could act on.
    achieved_feasibility = _achieved_feasibility_stats(
        report.j_ach_hourly,
        regret_dollars,
        min_pct=min_pct,
        max_pct=max_pct,
        capacity_kwh=capacity_kwh,
    )
    # The WARNING for a negative regret is raised at the publish site,
    # alongside the #538 SoC-discrepancy one, so it inherits the same
    # log-once-per-scored-day guard rather than firing every cycle.
    achieved_feasibility["lp_soc_envelope_pct"] = [
        round(min_pct, 4),
        round(max_pct, 4),
    ]

    # nimbus issue #1089: EPR's DENOMINATOR, checked for the first time.
    # Computed by compute_epr() itself rather than here, so the
    # standalone cron writer -- which publishes `epr` and
    # `theoretical_maximum_yield` and has never had ANY of the three
    # reliability signals -- gets the same check from the same code
    # instead of a second copy that can drift (#357).
    #
    # Folded into achieved_feasibility only to ride its existing spread
    # into the report dict. It is NOT a statement about the achieved
    # trajectory: j_ach does not enter it at all.
    epr_denominator_reason = report.epr.denominator_reason
    achieved_feasibility["epr_denominator_reason"] = epr_denominator_reason

    # nimbus issue #1162: `epr_reliable` could be False while every field
    # naming EPR read None, because the SoC half of `_epr_reliability()`
    # had no reason of its own. Filled here rather than inside
    # `_achieved_feasibility_stats()` because that function is scoped to
    # the achieved trajectory and never sees the SoC comparison.
    #
    # Fallback only -- a regret finding already in the field wins, since
    # it is the stronger statement and its labels have history.
    if achieved_feasibility.get("epr_reason") is None:
        achieved_feasibility["epr_reason"] = _epr_soc_reason(
            soc_discrepancy["soc_discrepancy_reliable"],
            soc_discrepancy.get("soc_discrepancy_reason"),
        )

    # nimbus issue #919: "is the ML load forecaster actually beating naive
    # persistence on this household's data?" -- a question no deployed
    # install could answer until now. Uses grid_oracle, not grid_residual:
    # all three scenarios must plan against the same unconstrained grid
    # the oracle itself uses, since a pinned P2P export commitment would
    # stop the battery responding to a load-forecast difference at all and
    # so mask the very thing being measured. Costs two extra LP solves,
    # paid once per day behind this function's own latest_date fast path.
    nowcast_skill_attrs = _load_nowcast_skill_attributes(
        load_sensor=load_sensor,
        load_scale=load_scale,
        grid_times=grid_times,
        period_hours=period_hours,
        periods=periods,
        grid=grid_oracle,
        battery=battery_cfg,
        solar_real_kw=solar_kw,
        load_real_kw=load_kw,
        day_start=day_start,
        day_end=day_end,
    )
    return {
        **nowcast_skill_attrs,
        # Fractional EPR (0..1). Canonical downstream contract: the OpEd
        # hero chart, the compute_quality_report service payload, and the
        # LinkedIn article all treat this attribute as a 0..1 ratio. Do
        # not scale here.
        "epr": round(report.epr.epr, 4),
        # Same value scaled to a real percent (0..100). Separate field so
        # the parent sensor state and the flattened Quality EPR child can
        # both publish with unit_of_measurement="%" without lying about
        # the number. Two decimals is enough resolution for a percent
        # (four on the fraction gives the same effective precision).
        "epr_pct": round(report.epr.epr * 100, 2),
        # nimbus issue #585 (Mark Purcell, real finding on his own three-
        # battery install: 8 Sep's 16-22 kW of evening export came from
        # the Model 3 via the shared Sigen DC charger, not the home pack
        # -- invisible to this scorer, which then attributed the full
        # export opportunity to the pack alone as missed value. EPR
        # 35.7% was a real statement about the pack against an oracle
        # that only knows the pack, not a statement about the
        # household's actual decision).
        #
        # nimbus issue #768 (Mark Purcell, 2026-09-12): the fix landed --
        # `batteries` now includes "home" plus every battery_participant
        # with complete real history for this day (see
        # _resolve_battery_participant_history()'s own docstring), and
        # the oracle above genuinely re-solves batteries=[home,
        # *participants] jointly. `scored_participants` now names
        # whoever was ACTUALLY included in this specific day's score --
        # never assume it's the full configured fleet, since a
        # participant with missing/incomplete history for this
        # particular day is honestly excluded (see that function's own
        # per-participant skip logging) rather than silently pretended
        # complete. Still real, still-open, named rather than assumed
        # solved (see regret.py's own oracle_dispatch() docstring):
        # disambiguating a sensor SHARED between two participants (this
        # household's own Sigen DC charger) is not attempted -- a
        # participant scored via a shared sensor is scored using that
        # reading as-is.
        "scored_participants": [b.name for b in batteries],
        "theoretical_maximum_yield": round(report.epr.theoretical_maximum_yield, 4),
        "value_captured": round(report.epr.value_captured, 4),
        "uplift_available": round(report.epr.uplift_available, 4),
        "j_ref": round(report.j_ref, 4),
        "j_ach": round(report.j_ach, 4),
        "j_star": round(report.j_star, 4),
        # nimbus issue #1001: the oracle no longer refuses to consider an
        # under-delivered committed hour -- it cannot, without making the
        # comparison impossible -- so the missed commitment is reported
        # here instead of being implicit in a regret that had gone
        # negative. 0.0 means nothing was owed or everything was met.
        "p2p_commitment_shortfall_kwh": report.p2p_commitment_shortfall_kwh,
        # nimbus issue #1081: j_star is the LP's own objective, j_ach an
        # independent arithmetic evaluator. "j_star <= j_ach by
        # construction" is an argument about TRAJECTORIES; it only
        # carries to the NUMBERS if both are priced the same way. These
        # two reprice the oracle's own plan through j_ach's path so the
        # disagreement is measured rather than assumed -- see
        # QualityReport's own field docs for the 15 Sep case.
        "j_star_evaluator": report.j_star_evaluator,
        "j_star_path_delta": report.j_star_path_delta,
        "regret_dollars": round(regret_dollars, 4),
        # nimbus issue #1162 (ask 3): how much of the published regret is
        # the two pricing paths disagreeing about the ORACLE'S OWN PLAN,
        # rather than the household having dispatched differently.
        #
        # `j_star_path_delta` is exactly that amount, and the identity is
        # worth stating because it is not obvious from the field names::
        #
        #     regret_evaluator - regret_raw
        #       = (j_ach - j_star_evaluator) - (j_ach - j_star)
        #       = j_star - j_star_evaluator
        #       = j_star_path_delta
        #
        # So the delta IS the amount the regret moved by repricing the
        # oracle's plan. Published as a share so one threshold reads the
        # same on a $3 day and a $30 one.
        #
        # Measured on the reference household, 19 Sep 2026: regret
        # $3.6485, of which $2.6259 -- **72%** -- was path delta. A
        # household reading "$3.65 of regret" would go looking for a
        # dispatch mistake that was mostly not there. That is the
        # confident-wrong-number failure this project keeps paying for,
        # and #1073 already fixed its twin on the energy-balance side by
        # attaching the caveat to the figure rather than nulling it.
        #
        # Always present (0.0 when the paths agree, which is the healthy
        # case and was true on 16 Sep) so a consumer never has to
        # distinguish missing from zero. The regret itself is KEPT, not
        # qualified away -- same choice #1073 made.
        "regret_path_delta_share": _regret_path_delta_share(
            regret_dollars, report.j_star_path_delta
        ),
        "tracking_fidelity": round(report.tracking.tracking_fidelity, 4),
        "tracking_cost": round(report.tracking_cost, 4),
        "real_p2p_dollars": round(real_p2p_dollars, 4),
        # nimbus issue #1015: says WHY real_p2p_dollars is what it is.
        # "applied" | "no_sensor_configured" |
        # "window_is_not_one_local_calendar_day" |
        # "no_settlement_entry_for_this_date" |
        # "settlement_sensor_unreadable"
        "real_p2p_settlement_status": real_p2p_settlement_status,
        "real_p2p_volume_kwh": round(real_p2p_volume_kwh, 3),
        # Hourly regret breakdown (2026-08-31, sibling addition to the
        # reconstruction dicts below): the per-hour actual-minus-oracle
        # cost dict compute_quality_report already built via hourly_
        # regret_breakdown() but never published. Fanned out to
        # sensor.nimbus_quality_regret_dollars via FLATTENED_ATTRS_QUALITY's
        # attrs_source_key = "hourly_regret". Same {str(hour): float, ...}
        # shape as the reconstruction dicts for a coherent card-side
        # aggregation contract; string keys because HA/JSON attribute
        # dicts round-trip better with strings than ints. The dict's
        # own sum does NOT necessarily equal (j_ach - j_star) --
        # deliberate, documented gap for salvage_value / two-tier bonus
        # trajectories (see hourly_regret_breakdown()'s own docstring).
        "hourly_regret": {
            str(k): round(float(v), 4) for k, v in report.hourly_regret.items()
        },
        # 24-hour reconstruction dicts, one per trajectory (2026-08-31,
        # direct ask: "expand the attributes of sensor.nimbus_quality_j_ach
        # to include the average for each of the 24 hours as a dict; the
        # full reconstruction; buy & sell prices, power levels; grid, PV,
        # load and battery. This should enable a full reconstruction of
        # the state. Then once we have completed for sensor.nimbus_quality_
        # j_ach. Lets do similar for the other quality_j entities.").
        # Fanned out to the flattened J_ref/J_ach/J_star sensors via
        # FLATTENED_ATTRS_QUALITY's hourly source keys (sensor_flattened
        # .py). See QualityReport.j_*_hourly docstring for the exact
        # dict shape and sign conventions.
        "j_ref_hourly": report.j_ref_hourly,
        "j_ach_hourly": report.j_ach_hourly,
        "j_star_hourly": report.j_star_hourly,
        **soc_discrepancy,
        # nimbus issue #1172: the capacity the solver and this
        # reconstruction actually plan against.
        #
        # Published ALONE. A `measured_usable_capacity_kwh` sat beside it
        # from v0.94.407 until it was retracted in v0.94.412 -- see the
        # block above `_regret_path_delta_share()` for why a power sensor
        # cannot measure capacity, and do not add one back.
        #
        # This is the EFFECTIVE capacity -- nameplate already derated by
        # `solver_battery_soh_percent` -- because that is what the solver
        # and the reconstruction both actually use. Publishing the
        # nameplate instead would send a household reaching for the wrong
        # number: on the reference household nameplate is 122.2 and
        # effective is 119.8, and it is 119.8 that prices the LP.
        #
        # Worth stating on its own (nimbus issue #1013): SoH was a
        # dashboard dial read by nothing until v0.94.3xx, so a household
        # could not otherwise tell what capacity the solver believed in.
        # Verified correct on the reference household 2026-09-20 -- the
        # BMS's own energy counter puts usable at 119.7 kWh against this
        # 119.72.
        "configured_usable_capacity_kwh": round(capacity_kwh, 1)
        if capacity_kwh > 0
        else None,
        # nimbus issue #532 (Mark Purcell, real household data, 7 Sep):
        # the real energy that moved through actual_charge_kw/
        # actual_discharge_kw over the whole scored window -- exposed
        # alongside soc_discrepancy_reliable so a household can tell
        # "history gap" from "this sensor covers more storage than
        # capacity_kwh describes" from the sensor's own attributes,
        # without a manual recorder pull (Mark's own case: a combined
        # battery-power sensor summing the home pack + a shared EV DC
        # charger, feeding a 100 kWh single-battery model -- achieved_
        # energy_in_kwh/achieved_energy_out_kwh alone made this
        # diagnosable by eye once he had them). Deliberately NOT an
        # automatic cause classifier (history-gap vs model-mismatch) --
        # that needs a real recorder-gap detector this pass doesn't
        # build; the two raw numbers are honest and sufficient on their
        # own for a human (or a future automated check) to draw the
        # same conclusion.
        #
        # nimbus issue #858 (2026-09-14): these two are deliberately
        # HOME-BATTERY-ONLY -- they read the bare actual_charge_kw/
        # actual_discharge_kw, which is element 0 of the actual_*_kw_list
        # handed to compute_quality_report(), not the whole fleet. That
        # is correct for the diagnostic described above (it compares the
        # configured solver_battery_power_sensor against solver_battery_
        # capacity_kwh, an inherently home-battery question), but it was
        # written in #532 when the scorer was effectively single-battery
        # and "home" and "fleet" were the same number. #563/#768's
        # battery_participant work made them different without revisiting
        # this, so on a fleet install these two silently omitted every
        # participant while EPR/regret/SoC on the SAME sensor were
        # fleet-wide. Kept home-scoped (renaming a field a household
        # already diagnoses with is worse than the ambiguity) and the
        # scope is now stated here, in docs/entities.md, and by the
        # fleet_* companions immediately below.
        # nimbus issue #1149: the "what did the controller do
        # differently" companion to `hourly_regret`'s "which hour cost
        # money". Four rows (reference / achieved / oracle /
        # achieved_minus_oracle), each carrying charge_kwh,
        # discharge_kwh, grid_import_kwh, grid_export_kwh.
        #
        # The achieved row's charge/discharge reconcile with the
        # fleet_achieved_energy_in_kwh/_out_kwh pair below BY
        # CONSTRUCTION -- same arrays, same hours -- so these are one
        # answer stated twice rather than two answers that could drift.
        # What is genuinely new is the ORACLE side and the grid figures,
        # neither of which existed anywhere on this sensor before, which
        # is why a day whose whole regret was one over-charge could not
        # be read off it without summing the hourly rows by hand.
        "energy_decomposition": report.energy_decomposition,
        "achieved_energy_in_kwh": round(
            float(np.sum(actual_charge_kw * period_hours_arr)), 3
        ),
        "achieved_energy_out_kwh": round(
            float(np.sum(actual_discharge_kw * period_hours_arr)), 3
        ),
        # nimbus issue #858: the same two figures at the scope every
        # OTHER number on this sensor already uses -- summed across
        # every scored battery. On a single-battery install (every
        # install before #563, and every standalone/cron deployment,
        # where build_extra_batteries() returns []) these are equal to
        # the home-only pair above by construction, so nothing changes
        # for anyone who has no participants configured.
        "fleet_achieved_energy_in_kwh": round(
            float(sum(np.sum(a * period_hours_arr) for a in actual_charge_kw_list)), 3
        ),
        "fleet_achieved_energy_out_kwh": round(
            float(sum(np.sum(a * period_hours_arr) for a in actual_discharge_kw_list)),
            3,
        ),
        # nimbus issue #858: per-battery breakdown, keyed by each
        # scored battery's own name. A fleet install can see which
        # participant contributed what without a manual recorder pull --
        # and this is the shape that would have made #843's ~1,500 kW
        # participant corruption attributable from the sensor alone,
        # rather than needing the raw history pulled by hand.
        "achieved_energy_by_battery": {
            b.name: {
                "in_kwh": round(float(np.sum(chg * period_hours_arr)), 3),
                "out_kwh": round(float(np.sum(dis * period_hours_arr)), 3),
            }
            for b, chg, dis in zip(
                batteries,
                actual_charge_kw_list,
                actual_discharge_kw_list,
                strict=True,
            )
        },
        # nimbus issue #1012: does each battery's measured energy
        # actually reconcile with its measured SoC swing? Every
        # kWh-based reconstruction in this scorer assumes the configured
        # efficiency, applied to the power sensor's readings, converts
        # to the same energy the SoC sensor reports -- and nothing has
        # ever checked that assumption. #1012 is what it looks like when
        # it is wrong.
        #
        # The published `implied_charge_efficiency` is the point: a bare
        # residual says "something is off", while the efficiency that
        # WOULD close the balance says which thing. It is also the exact
        # figure #1012's thread has been arguing about from two
        # directions (85.8% configured vs ~95% implied) without either
        # side being able to measure it directly.
        #
        # Per battery and never blended, so #949's fleet-blend artefact
        # cannot contaminate it -- and on a fleet install it says WHICH
        # battery fails to reconcile, which a blended figure cannot.
        #
        # Mark Purcell's #768 sequencing note is why this is worth
        # having before more reconstruction gets built: the efficiency /
        # reference-plane question is upstream of every kWh-based
        # reconstruction, so anything built on top of it inherits the
        # error until this is settled.
        "achieved_energy_balance_by_battery": [
            battery_energy_balance(
                name=b.name,
                in_kwh=float(np.sum(chg * period_hours_arr)),
                out_kwh=float(np.sum(dis * period_hours_arr)),
                initial_soc_kwh=b.initial_soc_kwh,
                final_soc_kwh=fin,
                capacity_kwh=b.capacity_kwh,
                charge_efficiency=b.charge_efficiency,
                discharge_efficiency=b.discharge_efficiency,
                # nimbus issue #1098: no new plumbing needed to get this
                # here. `_resolve_battery_participant_history()` already
                # threads the same away mask onto the participant's own
                # BatteryConfig as `unavailable_period_indices` (#467),
                # so the balance can read it off the config it is already
                # being handed. Mark's filing suggested threading the
                # away fraction down from the resolver; it turned out to
                # have arrived here on its own.
                #
                # `or ()` covers the home battery, which has no
                # availability entity and carries None.
                away_period_count=len(b.unavailable_period_indices or ()),
                stale_period_count=len(b.stale_history_period_indices or ()),
                n_periods=len(period_hours_arr),
            )
            for b, chg, dis, fin in zip(
                batteries,
                actual_charge_kw_list,
                actual_discharge_kw_list,
                final_soc_kwh_actual_list,
                strict=True,
            )
        ],
        # nimbus issue #533: the EPR headline's own reliability
        # qualifier, named for what it qualifies. #533 noted that a
        # "future second EPR-reliability signal has somewhere to fold in
        # without a rename" -- nimbus issue #956 is that second signal,
        # and nimbus issue #1089 the third, so this is no longer an
        # alias of soc_discrepancy_reliable.
        "epr_reliable": _epr_reliability(
            soc_discrepancy["soc_discrepancy_reliable"],
            achieved_feasibility["regret_reliable"],
            epr_denominator_reason,
        ),
        **achieved_feasibility,
    }


# nimbus issue #1172: there is deliberately NO capacity measurement here.
#
# v0.94.407 shipped `_measured_usable_capacity_kwh()`, which divided the
# energy seen at the battery POWER sensor by the real-SoC points gained
# and published the quotient as this pack's usable capacity. It was
# retracted in v0.94.412 because that quotient is not capacity, and
# cannot be made into capacity from these inputs.
#
# **Why it is unsound in principle.** `energy_at_the_power_sensor /
# SoC_points` equals capacity only if the power sensor integrates to
# exactly the energy the pack actually moved. Any scale error in that
# sensor, and any conversion loss between it and the cells, lands
# entirely in the answer -- so the function returned
# `capacity x sensor_error`, with no term able to separate the two.
#
# **What it did in practice**, measured on the reference household on
# 2026-09-20 against the BMS's own energy counter -- an independent
# instrument, rather than another view of the same sensor:
#
#     combined_battery_charge   109.11 -> 21.86 kWh  = -87.25 kWh
#     logger_battery_level_soc   91.10 -> 18.20 %    = -72.90 points
#     battery power, integrated                      = -82.59 kWh
#
#     usable capacity from the BMS counter   87.25 / 0.729 = 119.7 kWh
#     configured (122.16 nameplate x 0.98 SoH)         = 119.72 kWh
#
# The configured value was right to within 0.04 kWh. The power sensor
# accounted for 82.59 kWh across that same window -- a disagreement the
# retracted function booked entirely as "capacity", returning 113.3 kWh
# there and 109.4 kWh on a charge window, telling a household whose pack
# is configured correctly that it was ~9% too large.
#
# **The SIZE and DIRECTION of that sensor/counter disagreement are NOT
# established, and an earlier version of this comment claimed a "5.3%
# sensor under-read" that the evidence does not support.** The counter is
# not a clean reference: it re-estimates in discrete steps, dropping
# 9.13 kWh in 12 minutes near the pack floor on 2026-09-20 while both
# power sensors saw a 1.79 kW mean. Exclude those steps and the counter
# and the sensors agree to 1.4% on that window. See nimbus #1172's own
# thread for the measurement.
#
# That makes the case for removal STRONGER, not weaker: the disagreement
# is not even a stable fraction, so it could never have been calibrated
# out of the quotient.
#
# The four consecutive days that appeared to corroborate the retracted
# figure were four runs of the same method over the same sensor, which
# is not corroboration.
#
# **Do not reintroduce this from a power sensor.** Measuring capacity
# needs an instrument reporting pack ENERGY directly (a BMS charge
# counter). Nimbus takes no such sensor as config today, so the honest
# position is to publish no measurement rather than one that is wrong on
# a correctly configured install.
# `configured_usable_capacity_kwh` is still published, because what the
# solver plans against is a fact worth stating (nimbus issue #1013).
#
# tests/test_1172_no_capacity_measurement_from_a_power_sensor.py pins
# this absence.


def _regret_path_delta_share(
    regret_dollars: float, j_star_path_delta: float | None
) -> float:
    """What fraction of the published regret is a pricing-path
    disagreement rather than a dispatch difference (nimbus issue #1162).

    `j_star_path_delta` is precisely the amount the regret moved by
    repricing the oracle's own plan through the evaluator instead of
    reading the LP's objective (see the call site for the identity). So
    dividing it by the regret says how much of the headline is about the
    comparison rather than about the household.

    Returns 0.0 rather than None when the regret is ~zero: a share of
    nothing is not a finding, and a null here would make every consumer
    branch for a case that carries no information. Clamped to [0, 1]
    because a delta larger than the regret says the same thing as a
    delta equal to it -- the number is a share, not a ratio to be read
    past its own scale.

    Uses magnitudes on both sides deliberately. Regret can be negative
    (`regret_reliable` False, the #956 case), and a signed share there
    would flip meaning for a reason that has nothing to do with the
    pricing paths.
    """
    if not j_star_path_delta:
        return 0.0
    denom = abs(float(regret_dollars))
    if denom < 1e-9:
        return 0.0
    return round(min(1.0, abs(float(j_star_path_delta)) / denom), 4)


def _epr_soc_reason(
    soc_discrepancy_reliable: object,
    soc_discrepancy_reason: object = None,
) -> str | None:
    """The `epr_reason` value for the one reliability signal that had
    none (nimbus issue #1162).

    `_epr_reliability()` above combines three signals, and until this
    function existed only two of them could be traced back from the
    published report:

    ========================== ===================== ==========================
    signal                     makes epr_reliable    reason a reader can find
    ========================== ===================== ==========================
    `regret_reliable`          False                 `epr_reason`
    `epr_denominator_reason`   False                 its own field
    `soc_discrepancy_reliable` False / None          **nothing**
    ========================== ===================== ==========================

    Measured on the reference household, 2026-09-20, scoring 19 Sep::

        epr_reliable              false
        epr_reason                null
        epr_denominator_reason    null
        regret_reliable           true
        soc_discrepancy_reliable  false
        soc_discrepancy_reason    "disagreement"

    Three fields naming EPR all say nothing is wrong, and the flag says
    the EPR cannot be read as a measurement. The cause was real -- the
    achieved reconstruction had drifted 19.11 points from the real SoC
    sensor -- and `soc_discrepancy_reason` did record it, but nothing
    connects that field to the EPR flag, so a reader looking at the EPR
    has no thread to pull.

    **Only fills a gap; never overwrites.** `regret_reliable`'s own
    labels (`oracle_beaten`, `oracle_beaten_achieved_outside_lp_soc_bounds`)
    are left exactly as they are, so a history of them stays readable and
    the more serious finding keeps the field when both fire at once.

    **The denominator cause is deliberately NOT mirrored here.**
    `_epr_reliability()`'s own docstring records that decision -- the two
    describe different halves of the same ratio, and a reader needs to be
    able to see both on a day where both happen. Folding it in would undo
    that.

    Returns None when the SoC half is fine, so the caller can use it as a
    plain fallback.
    """
    if soc_discrepancy_reliable is None:
        # Genuinely unknown rather than wrong -- no real SoC history to
        # compare against. `_epr_reliability()` propagates this as None
        # (unknown) rather than False, and the reason has to make the
        # same distinction or a reader cannot tell "we checked and it
        # disagrees" from "we could not check".
        return "achieved_soc_unverifiable"
    if not soc_discrepancy_reliable:
        reason = str(soc_discrepancy_reason) if soc_discrepancy_reason else "unknown"
        # Carries the SoC half's own verdict rather than restating it, so
        # the two fields cannot drift into disagreeing about the same
        # finding, and so a reader lands on the right attribute.
        return f"achieved_soc_unreliable:{reason}"
    return None


def _epr_reliability(
    soc_discrepancy_reliable: object,
    regret_reliable: object,
    epr_denominator_reason: object = None,
) -> bool | None:
    """Whether the published EPR/regret pair can be read as a
    measurement (nimbus issues #533, #956, #1089).

    Three independent signals, combined so that a definite "no" always
    wins over an "unknown":

    - `soc_discrepancy_reliable` -- #533's original test. `None` means
      it could not be computed at all (no real SoC history), which is
      genuinely unknown rather than fine.
    - `regret_reliable` -- #956's. `regret_dollars < 0` means the
      achieved dispatch priced out cheaper than perfect foresight,
      which is not a result but proof the comparison was invalid.
    - `epr_denominator_reason` -- #1089's. A non-None reason means
      EPR's own denominator is not a positive quantity, so the ratio
      is not a percentage of anything. See
      `_epr_denominator_reason()`.

    #533 anticipated exactly this shape -- "a future second
    EPR-reliability signal has somewhere to fold in without a rename" --
    and #956 was that second signal. This is the third, and it is kept
    as its OWN published field rather than overwriting `epr_reason`
    because the two describe different halves of the same ratio: a
    reader needs to know that BOTH the oracle was beaten and the
    denominator inverted, on a day where both happen.

    A False from any of the three is a positive finding and returns
    False. `None` only survives when the SoC half is unknown and the
    other two found nothing wrong.

    Typed `object` rather than `bool | None` / `str | None` because
    these arrive out of heterogeneous report dicts whose declared value
    types are unions; narrowing here keeps the call site free of casts
    that would assert more than those dicts actually promise.
    """
    if not regret_reliable:
        return False
    if epr_denominator_reason is not None:
        return False
    if soc_discrepancy_reliable is None:
        return None
    return bool(soc_discrepancy_reliable)


def _achieved_feasibility_stats(
    j_ach_hourly: dict[str, dict[str, float]],
    regret_dollars: float,
    min_pct: float,
    max_pct: float,
    capacity_kwh: float,
) -> dict[str, object]:
    """Whether the achieved trajectory respects the same SoC envelope
    the oracle is bound by (nimbus issue #956).

    **Why this is not already covered by #571's `out_of_range`.** That
    flag tests the achieved trajectory against `[0, 100]` -- physical
    possibility. This tests it against `[min_pct, max_pct]` -- the LP's
    own feasible set. They are different questions, and the gap between
    them is exactly where #956 lives.

    Confirmed on the reference household's own 2026-09-15 report: with
    Min SoC configured at 2.0%, the oracle sat precisely on 2.0% for two
    hours while the achieved trajectory finished at **0.4429%** -- 1.90
    kWh of energy the oracle was structurally forbidden from selling.
    Every hour of that day reported `out_of_range` False, because 0.4429
    is comfortably inside [0, 100]. The published EPR was 103.66% and
    `regret_dollars` -0.7307.

    That is the whole mechanism: the achieved side is priced against a
    strictly larger feasible set than the oracle, so it can come out
    cheaper than optimal. `regret >= 0` is documented across this
    codebase as structural ("the oracle can never be beaten"), and it is
    -- for two trajectories drawn from the same feasible set.

    **The household settled that on 2026-09-16 -- "go with B".** The
    ORACLE is now widened to contain the achieved trajectory rather than
    the achieved integration being clamped to fit the oracle, so the two
    sides are drawn from the same feasible set again and `regret >= 0`
    holds by construction. See `solver/quality_report.py`'s own
    `_soc_envelope_containing_achieved()` and
    `_widen_export_pin_to_achieved()`.

    That does NOT retire this function, and the reason is worth being
    explicit about. `j_ach` is deliberately still priced against the
    CONFIGURED envelope -- option B's whole point -- so these numbers
    keep answering the question a household actually asked: how far
    outside its own configured limits did the battery run today. What
    changes is what a surviving `regret_reliable: False` now means. The
    one mechanism this file verified is gone, so a negative regret from
    here on is evidence of something else, and `epr_reason` says which
    of the two cases it is.
    """
    soc_pcts = [
        float(row["soc_pct"]) for row in j_ach_hourly.values() if "soc_pct" in row
    ]
    regret_reliable = regret_dollars >= 0.0

    if not soc_pcts or capacity_kwh <= 0.0:
        return {
            "regret_reliable": regret_reliable,
            "achieved_within_lp_soc_bounds": None,
            "achieved_soc_min_pct": None,
            "achieved_soc_max_pct": None,
            "achieved_below_floor_kwh": None,
            "achieved_above_ceiling_kwh": None,
            "epr_reason": None if regret_reliable else "oracle_beaten",
        }

    lo, hi = min(soc_pcts), max(soc_pcts)
    below_pct = max(0.0, min_pct - lo)
    above_pct = max(0.0, hi - max_pct)
    within = below_pct == 0.0 and above_pct == 0.0

    if regret_reliable:
        reason = None
    elif within:
        # Negative regret WITHOUT an envelope breach. Worth naming
        # separately rather than folding into one label: it means the
        # mechanism verified on the reference household does not explain
        # this install's own violation, and something else does.
        reason = "oracle_beaten"
    else:
        # Since the oracle is widened to contain the achieved trajectory
        # (#956 option B), this branch no longer describes a comparison
        # the widening failed to fix -- it describes one it could not:
        # the widening is clamped to `[0, capacity]`, so a trajectory
        # reconstructed as leaving THAT is left outside the oracle's
        # feasible set on purpose, because it is sensor or unit trouble
        # rather than a state the oracle should be asked to match. Kept
        # under its original label so a history of these stays readable.
        reason = "oracle_beaten_achieved_outside_lp_soc_bounds"

    return {
        "regret_reliable": regret_reliable,
        "achieved_within_lp_soc_bounds": within,
        "achieved_soc_min_pct": round(lo, 4),
        "achieved_soc_max_pct": round(hi, 4),
        "achieved_below_floor_kwh": round(below_pct * capacity_kwh / 100.0, 4),
        "achieved_above_ceiling_kwh": round(above_pct * capacity_kwh / 100.0, 4),
        "epr_reason": reason,
    }


# nimbus issue #571 (Mark Purcell): how close the REAL SoC sensor must
# sit to 0%/100% to excuse the achieved (integrated) trajectory's own
# overshoot past that same boundary as real-physical-edge noise rather
# than an out-of-range fault -- see _soc_discrepancy_stats()'s own
# docstring for the full real-data investigation. A separate, deliberately
# fixed concept from max_threshold_pct/mean_threshold_pct below (those
# measure how far apart two in-range trajectories are allowed to be; this
# measures how close to a physical edge counts as "at" that edge), so it
# is not folded into either household-tunable threshold.
_SOC_BOUNDARY_EDGE_TOLERANCE_PCT = 1.0


def _soc_discrepancy_stats(
    battery_socs: list[tuple[list[tuple[datetime, float]], float]],
    j_ach_hourly: dict[str, dict[str, float]],
    max_threshold_pct: float = 15.0,
    mean_threshold_pct: float = 8.0,
    ach_soc_pct_at_hour: dict[str, float] | None = None,
    power_coverage: dict[str, float | int] | None = None,
) -> dict[str, float | bool | str | list[dict[str, float | bool | str]] | None]:
    """nimbus issue #427 (Mark Purcell): the achieved trajectory's own
    SoC is *integrated* from real battery-power history through the
    efficiency model (see compute_quality_report()'s own j_ach_soc_kwh
    construction), not read directly from the real SoC sensor -- any
    sensor gap, sampling drop, or efficiency-model mismatch compounds
    over the scored window. Mark's own report measured this directly on
    a real day (max 22.5 points, mean 6.9 points) and suggested exposing
    it as a real diagnostic rather than something only visible via a
    manual report run.

    nimbus issue #949 (Mark Purcell, his own chosen fix among the three
    the issue named): `battery_socs` is one `(soc_hist, capacity_kwh)`
    pair PER BATTERY actually scored this window -- home first, then any
    `battery_participant` -- not the home sensor alone. The real, measured
    side is now blended the SAME capacity-weighted way `j_ach_soc_pct`
    already is (`quality_report.py`'s own `_soc_pct()`: total stored kWh
    across every battery / total capacity across every battery), so both
    sides of this comparison measure the identical quantity on a
    multi-battery install.

    Before this fix, `soc_hist` was the home battery's sensor ALONE,
    compared against a fleet-blended `ach_pct` -- structurally different
    quantities once any `battery_participant` entered scoring, which
    #949 measured as a real 21-29 point gap from PERFECT data (no sensor
    gap, no efficiency mismatch, nothing physically wrong) on a
    two/three-battery fleet, rising with the EVs' own share of total
    capacity. That mechanism is why `epr_reliable` read False on
    essentially every scored day once `ev_m3p`/`ev_my` joined the fleet.

    Each `soc_hist` here is resampled at the SAME hourly timestamps
    j_ach_hourly is already keyed by (report.j_ach_hourly's own keys,
    'day_start + h hours' ISO strings -- see quality_report.py's
    _hourly_means_by_key() docstring), so every comparison point is
    genuinely the same real hour on every side, not a separate
    resampling with its own chance to disagree on alignment.

    nimbus issue #1228: sharing a timestamp was NOT enough, and the
    paragraph above -- correct about alignment -- was read for a long time
    as though it settled comparability too. It does not. The real side is
    a POINT SAMPLE at `HH:00:00` (`resample_history_nearest()`), while
    `j_ach_hourly['soc_pct']` is that hour's MEAN
    (`_hourly_means_by_key()`). Differencing a mean against an instant
    injects roughly half the hour's ramp rate as pure artifact, largest
    exactly where SoC moves fastest -- measured on the reference
    household's 24 Sep: a published `max 21.17 / mean 9.88` whose max
    landed on hour 12, the steepest charge ramp of the day, against a
    like-for-like `max 11.82 / mean 6.46` that passes both thresholds.
    That single artifact was the sole cause of `epr_reliable: False` on an
    install whose sensors were in fact fine.

    `ach_soc_pct_at_hour` (`report.j_ach_soc_pct_at_hour`) supplies the
    achieved SoC AT each boundary instant, making the comparison
    like-for-like. Fixed on the achieved side rather than by averaging the
    real side because point-sampling is what `resample_history_mean()`'s
    own docstring already commits to for SoC -- a STATE, sampled and
    held, not a flow to be averaged -- and because it leaves the real side
    untouched.

    Falls back to the hourly mean when that mapping is absent or has no
    entry for an hour (an older persisted report, or a window shape that
    produced no period for that hour), and reports WHICH basis it used in
    `soc_discrepancy_basis` rather than silently reverting to the
    behaviour this issue is about.

    Returns None for both stats when `battery_socs` is empty or every
    battery's own capacity is non-positive (no SoC sensor configured
    anywhere, or no capacity to blend against) -- an honest absence, not
    a fabricated 0.0 that would misleadingly read as "perfect agreement".
    A battery reaching this function is guaranteed to carry real,
    non-empty history -- `_resolve_battery_participant_history()`'s own
    docstring covers why a participant missing it is excluded from the
    fleet entirely before it ever gets here, so there is no separate
    per-participant honest-absence case to handle at this layer.

    nimbus issue #445 (Mark Purcell), same day as #427 shipped: j_ach's
    own soc_pct is a pure, unclamped cumulative integration of real
    battery-power history (quality_report.py's j_ach_soc_kwh = initial +
    cumsum(delta)) -- correct and self-consistent when that history is
    genuinely complete for the whole scored window, but if a signal's
    real recorder history only covers part of the 24h (e.g. the sensor
    was only just configured, or an outage truncated it -- both real,
    observed, self-resolving-with-time conditions, not a bug in the
    integration itself), the missing hours' delta is effectively
    fabricated and the integrated soc_pct can drift far outside the
    physically real [0, 100] range. Two independently-bounded [0, 100]
    percentages can never legitimately disagree by more than 100 points
    -- a raw gap exceeding that is proof one side (usually j_ach, but
    checked on both sides below since a resampled real_pct could in
    principle carry the same kind of glitch) was out of range, not a
    real >100pp disagreement. Clamping each side to [0, 100] before
    differencing keeps the reported number itself physically meaningful
    (never removes the *signal* that a real, large discrepancy exists --
    it just stops that signal from reading as an impossible value), and
    the new soc_discrepancy_reliable flag names the out-of-range
    condition explicitly so a caller doesn't have to infer "this looks
    like a data-continuity gap, not a real dispatch problem" from the
    number's own magnitude.

    nimbus issue #538 (Mark Purcell, real household finding on this
    repo's own v0.94.166): the range test above catches one failure
    mode (the integration leaving [0, 100]) but is blind to the other
    -- a genuinely large, sustained disagreement that never leaves the
    range. Mark's own real case: raising the configured battery
    capacity kept the trajectory in-range while the gap against the
    real SoC sensor stayed at 40.7 points max / 10.85 mean, and the
    flag read "reliable" regardless. max_threshold_pct/mean_threshold_
    pct are the second, independent test -- a household-tunable
    dashboard number (see number.py), not a fixed constant, since what
    counts as "too far apart" genuinely depends on how well-matched
    that household's own power/SoC sensors are to its capacity model.
    soc_discrepancy_reason names WHICH test failed ("out_of_range" takes
    priority when both would fail, since it's the more fundamental
    problem -- a trajectory that left the physical range at all makes
    the disagreement numbers themselves suspect), so a consumer (or the
    once-per-day WARNING below) doesn't have to re-derive the cause from
    the raw numbers.
    """
    # A battery with no real SoC history at all (the home battery's own
    # entry when no `solver_battery_soc_sensor` is configured -- unlike a
    # participant, which never reaches here without one, see this
    # function's own docstring above) contributes nothing honest to the
    # blend. Dropped up front rather than left in to silently resample
    # against `resample_history_nearest()`'s own empty-history default
    # (0.0), which would read as a real, if extreme, measurement instead
    # of the absence it actually is.
    battery_socs = [(hist, capacity) for hist, capacity in battery_socs if hist]
    total_capacity_kwh = sum(capacity for _hist, capacity in battery_socs)
    if not battery_socs or not j_ach_hourly or total_capacity_kwh <= 0.0:
        return {
            "soc_discrepancy_max_pct": None,
            "soc_discrepancy_mean_pct": None,
            "soc_discrepancy_reliable": None,
            "soc_discrepancy_reason": None,
            "soc_discrepancy_hourly": None,
            "soc_discrepancy_basis": None,
            "soc_discrepancy_power_coverage": power_coverage,
        }
    gaps: list[float] = []
    any_out_of_range = False
    # nimbus issue #681 (Mark Purcell, real finding: his own independent
    # `score_day.py` reconstruction of this exact statistic from the same
    # `recorder.json`/`quality_report.json` inputs disagreed 3x with this
    # function's own number -- 10.4/3.4pt vs 32.13/6.41pt -- "I don't know
    # which side is right... flagging the disagreement itself"). Rather
    # than guess which resampling choice is correct without access to his
    # own script, this exposes the exact per-hour (real_pct, ach_pct, gap)
    # triples THIS function itself used, so any external reconstruction
    # (his own or a future one) can diff directly against Nimbus's own
    # real numbers hour by hour instead of independently re-deriving them
    # and hoping the two resampling methods happen to agree.
    hourly_rows: list[dict[str, float | bool | str]] = []
    used_boundary = 0
    used_mean = 0
    for key_str, row in j_ach_hourly.items():
        # nimbus issue #1228: the boundary sample is the like-for-like
        # counterpart to `real_pct` below; the hourly mean is a fallback
        # that keeps an older persisted report working, counted so the
        # published basis can say which one actually got used.
        ach_pct = (
            ach_soc_pct_at_hour.get(key_str)
            if ach_soc_pct_at_hour is not None
            else None
        )
        if ach_pct is None:
            ach_pct = row.get("soc_pct")
            if ach_pct is not None:
                used_mean += 1
        else:
            used_boundary += 1
        if ach_pct is None:
            continue
        hour_dt = datetime.fromisoformat(key_str)
        # nimbus issue #949: capacity-weighted fleet blend, one battery
        # at a time, mirroring quality_report.py's own `_soc_pct()`
        # exactly (`total stored kWh / total capacity kWh * 100`) so this
        # is the SAME quantity `ach_pct` already is, not a second,
        # differently-shaped approximation of it.
        stored_kwh = sum(
            resample_history_nearest(hist, [hour_dt], backfill_first=True)[0]
            / 100.0
            * capacity
            for hist, capacity in battery_socs
        )
        real_pct = stored_kwh / total_capacity_kwh * 100.0
        ach_out_of_range = not (0.0 <= ach_pct <= 100.0)
        real_out_of_range = not (0.0 <= real_pct <= 100.0)
        exempted = False
        if ach_out_of_range and not real_out_of_range:
            # nimbus issue #571 (Mark Purcell, confirmed against real
            # recorder data 8 Sep): a pack that genuinely runs down to
            # its own physical cut-off (real_pct == 0.0, confirmed via
            # the plant's own discharge_cut_off_soc reading, not a
            # sensor fault) can still leave j_ach's round-trip-efficiency
            # accounting a few points negative for the same hour -- the
            # achieved trajectory's own integration loss landing right at
            # a real physical edge, not evidence the two sensors describe
            # different storage. Only flag this hour when the real sensor
            # ISN'T itself already sitting within a small tolerance of the
            # same boundary the achieved trajectory crossed; a real
            # in-range sensor confirms the edge is genuine.
            near_zero = ach_pct < 0.0 and real_pct <= _SOC_BOUNDARY_EDGE_TOLERANCE_PCT
            near_hundred = (
                ach_pct > 100.0 and real_pct >= 100.0 - _SOC_BOUNDARY_EDGE_TOLERANCE_PCT
            )
            exempted = near_zero or near_hundred
            if not exempted:
                any_out_of_range = True
        elif ach_out_of_range or real_out_of_range:
            any_out_of_range = True
        clamped_ach = min(100.0, max(0.0, ach_pct))
        clamped_real = min(100.0, max(0.0, real_pct))
        gap = abs(clamped_real - clamped_ach)
        gaps.append(gap)
        hourly_rows.append(
            {
                "hour": key_str,
                "real_pct": round(real_pct, 2),
                "ach_pct": round(ach_pct, 2),
                "gap_pct": round(gap, 2),
                "out_of_range": ach_out_of_range or real_out_of_range,
                "boundary_exempted": exempted,
            }
        )
    if not gaps:
        return {
            "soc_discrepancy_max_pct": None,
            "soc_discrepancy_mean_pct": None,
            "soc_discrepancy_reliable": None,
            "soc_discrepancy_reason": None,
            "soc_discrepancy_hourly": None,
            "soc_discrepancy_basis": None,
            "soc_discrepancy_power_coverage": power_coverage,
        }
    max_gap = max(gaps)
    mean_gap = sum(gaps) / len(gaps)
    # nimbus issue #1228: name the basis rather than leave a consumer to
    # infer it from the magnitude. "hourly_mean" anywhere means some hour
    # was still compared the pre-fix way.
    if used_boundary and not used_mean:
        basis = "hour_boundary"
    elif used_mean and not used_boundary:
        basis = "hourly_mean"
    else:
        basis = "mixed"
    if any_out_of_range:
        reliable = False
        reason: str | None = "out_of_range"
    elif max_gap > max_threshold_pct or mean_gap > mean_threshold_pct:
        reliable = False
        reason = "disagreement"
    else:
        reliable = True
        reason = None
    return {
        "soc_discrepancy_max_pct": round(max_gap, 2),
        "soc_discrepancy_mean_pct": round(mean_gap, 2),
        "soc_discrepancy_reliable": reliable,
        "soc_discrepancy_reason": reason,
        "soc_discrepancy_hourly": hourly_rows,
        "soc_discrepancy_basis": basis,
        # nimbus issue #1181: the home battery's own power-history
        # coverage for this window, measured and published, never acted
        # on -- see _power_history_coverage() for why adjusting would
        # make this statistic worse rather than better.
        "soc_discrepancy_power_coverage": power_coverage,
    }


# nimbus issue #533 (Mark Purcell's own item 3): log once per SCORED
# DAY, not every cycle that happens to re-publish it -- same #313/#314
# "log once, with the number" discipline already applied elsewhere this
# session (#480's own done_when warning). Keyed by the scored date
# string (yesterday_key) so a genuinely NEW day's own unreliable score
# gets its own warning even if a PRIOR day's was already logged.
_QUALITY_REPORT_UNRELIABLE_WARNED: set[str] = set()

# nimbus issue #956: same discipline, its own set. Deliberately NOT
# folded into the one above -- a day can violate the regret bound while
# the SoC discrepancy reads fine, or the reverse, and sharing a set
# would let whichever condition was seen first silence the other for
# that day.
_QUALITY_REPORT_NEGATIVE_REGRET_WARNED: set[str] = set()

# nimbus issue #1089: a third set, for the same reason the second one
# exists. The denominator condition is independent of both others -- on
# the real 2026-09-17 day it fired while `regret_reliable` was True, so
# sharing the set above would have meant no warning at all on the one
# condition that makes the headline read BETTER than the truth.
_QUALITY_REPORT_EPR_DENOMINATOR_WARNED: set[str] = set()

# nimbus issue #994: how many scored days the quality report's own
# `history` table keeps. The Regret card's longest view is 30 days, so 60
# is real headroom without turning a table into an archive -- and the
# ceiling matters, because this dict rides in the same attribute payload
# #944 measures against the recorder's 16 KB cap. Five rounded floats and
# an ISO date key is roughly 110 bytes, so 60 days is ~6.6 KB.
_QUALITY_HISTORY_MAX_DAYS = 60

# The five numbers nimbus-regret-card.js reads out of each
# `history[dateKey]` entry -- deliberately NOT the whole day_entry, which
# carries three 24-row hourly reconstructions and would blow the cap
# within a week.
_QUALITY_HISTORY_FIELDS = (
    "epr",
    "j_ref",
    "j_ach",
    "j_star",
    "regret_dollars",
)

# nimbus issue #1149: headline attributes that describe the CURRENTLY
# PUBLISHED day but are deliberately NOT carried in each history row,
# because a row is meant to stay small enough that a year of them fits
# in one attribute payload.
#
# They still have to move when a rescore changes which figures the
# headline is describing, or the sensor ends up with a rescored state
# sitting next to a decomposition of the previous computation -- the
# same "headline and table disagreeing" defect #1120 exists to fix.
# Kept as its own tuple rather than appended to the one above so the
# distinction stays visible: that set goes into every row, this set
# goes only on top.

# nimbus issue #1120: which release scored this day.
#
# A day is scored ONCE, the morning after, and frozen into the table
# above -- nothing ever recomputes an entry. So every scoring-formula
# change splits the table in two, and the two halves sit next to each
# other on the same card with nothing saying they are not comparable.
# Measured on the reference household hours after v0.94.391 landed: the
# freshly-rescored day read EPR 94.8% / regret $1.67 while the row beside
# it read **106.4% / -$0.79** -- the `oracle_beaten` signature #1081
# fixed, still sitting in a row written before the fix existed.
#
# This does not make the stale row right; nothing here can, short of a
# rescore path that persists (#1120's own second half). It makes it
# VISIBLE, which is the difference between a household reading a wrong
# number and reading a qualified one.
#
# **The key is one character on purpose.** This dict rides in the same
# attribute payload #944 measures against the recorder's 16 KB cap, and
# that payload was measured at 20,738 bytes on a real install -- already
# 27% over. At 60 days a `"v":"0.94.392"` pair costs ~960 bytes; the
# same field spelled `scored_by_nimbus_version` would cost ~2.4 KB.
_QUALITY_HISTORY_VERSION_FIELD = "v"


# nimbus issue #1162 ask 2: whether the row's own EPR was a measurement.
#
# **The ask, and why the answer is not `unknown`.** #1162 asked what the
# sensor `state` should be when `epr_reliable` is false, offering
# `unknown` or "a companion flag the cards consult". Publishing `unknown`
# is the wrong half of that: the state feeds long-term statistics and the
# EPR trend chart, so blanking it puts a hole in the series on exactly
# the days most worth looking at, and makes the state flap between a
# number and nothing -- the appear/vanish shape #589 exists about.
#
# **What was actually broken is retention, not publication.** The
# reliability verdict lived ONLY in the headline attributes, which
# describe `latest_date` alone. That is why `nimbus-regret-card.js`'s own
# caveat is gated on `attrs.latest_date === dateKey`: for every other
# scored day the card had nothing to consult, so it rendered the EPR
# bare. A 60-day table where 59 rows cannot be qualified is the defect;
# the 60th being qualified is not a fix.
#
# So the verdict goes INTO the row, beside the five numbers it qualifies,
# and travels with them for as long as they are displayed.
#
# **One character, same budget reasoning as `"v"` above**: at 60 days
# `"r":"s"` costs ~480 bytes against a payload already measured 27% over
# the recorder's 16 KB cap.
#
# **Always written, never omitted as shorthand for "fine".** Absence has
# to keep meaning exactly one thing -- "written before this existed" --
# or a pre-#1162 row becomes indistinguishable from a reliable one, which
# is the same absence-as-the-only-signal failure this scorer keeps
# recording.
_QUALITY_HISTORY_RELIABILITY_FIELD = "r"

# The codes. Single characters for the byte budget above; a reader that
# does not recognise one must still say SOMETHING (the card's own
# `reason || "the report did not say why"` fallback), so an unknown code
# degrades to "flagged, cause not recorded" rather than to silence.
_RELIABILITY_OK = "y"  # epr_reliable True
_RELIABILITY_UNKNOWN = "u"  # epr_reliable None -- the SoC half could not be computed
_RELIABILITY_SOC = "s"  # the reconstructed SoC disagrees with the sensor
_RELIABILITY_ORACLE = "o"  # regret < 0: oracle "beaten", comparison void
_RELIABILITY_DENOMINATOR = "d"  # EPR's denominator is not a positive quantity
_RELIABILITY_UNSTATED = "?"  # flagged false, and no field said which


# nimbus issue #1200: whether this row's five figures were computed
# WITHOUT the day's real P2P settlement, and are therefore provisional.
#
# **Why a row needs this at all.** The settlement lands hours AFTER the
# day is first scored. Measured on the reference household: the
# settlement sensor gained 2026-09-23 at 09:00 local, where that day was
# scored at 00:00. Until it arrives `real_p2p_dollars` is 0, which also
# makes the `real_p2p_volume_kwh > 0.01` bonus gate false -- so `j_ref`,
# `j_ach` and `j_star` go P2P-blind TOGETHER and the day is priced as
# though the household had no P2P arrangement at all. Measured over four
# consecutive real days: EPR 51.9 / 38.5 / 36.4 / 41.6% against settled
# export revenue of $14.91 / $12.71 / $13.46 -- the scorer read WORST on
# the household's best-earning days, and read 88.6% on the one day that
# genuinely under-exported ($4.01). Anti-correlated with the money.
#
# `_keep_published_quality_score()` (#1082) already re-scores the
# CURRENTLY PUBLISHED day hourly while it is provisional. That is not
# enough, and the gap is structural rather than a tuning problem: its
# retry is reachable only while `latest_date == yesterday_key`, so it
# expires at local midnight. On three consecutive measured days the
# retry's last firing was 06:00 against a 09:00 settlement -- three
# hours early -- and the row was then frozen P2P-blind for good.
#
# Recording provisionality IN THE ROW removes the deadline: the row
# still says "these numbers are missing their settlement" tomorrow, next
# week, and after a restart, so the repair no longer has to win a race.
#
# **Written ONLY when provisional, the opposite convention to `"r"`
# above, and the reason is the byte budget**: this rides in the payload
# #944 measured at 20,738 bytes against the recorder's 16 KB cap. A key
# present only on days awaiting settlement costs nothing on the rows
# that are already correct, and self-clears when the day is re-scored
# with its real figures. Absence therefore means "not provisional, or
# written before this existed" -- safe here in a way it was not for
# `"r"`, because the repair sweep reads an absent key as "nothing to
# do": a wrongly-absent key costs a missed repair, never a wrong one.
_QUALITY_HISTORY_PROVISIONAL_FIELD = "p"


def _epr_reliability_code(day_entry: dict) -> str | None:
    """One character naming this day's EPR verdict, for the history row.

    Derived from the SAME three signals as `_epr_reliability()` and in
    the SAME precedence order, so the code and the boolean can never
    disagree about whether the day was reliable -- only about how much
    detail they carry. `TestTheCodeNeverContradictsTheBoolean` drives
    every combination of the three and pins that.

    Precedence matters for which cause gets named on a day that trips
    more than one: `_epr_reliability()` tests regret first, then the
    denominator, then SoC, and a household reading "oracle beaten" on a
    day that ALSO has a denominator problem is being pointed at the one
    that decided the verdict.

    Returns None when the entry carries no verdict at all, so the caller
    writes no key rather than inventing one -- see the field's own note
    above on why absence must keep meaning "written before this existed".
    """
    reliable = day_entry.get("epr_reliable")
    if reliable is None and "epr_reliable" not in day_entry:
        return None
    if reliable is True:
        return _RELIABILITY_OK
    if reliable is None:
        return _RELIABILITY_UNKNOWN
    # False. Name the signal that decided it, in _epr_reliability()'s
    # own order.
    if day_entry.get("regret_reliable") is False:
        return _RELIABILITY_ORACLE
    if day_entry.get("epr_denominator_reason") is not None:
        return _RELIABILITY_DENOMINATOR
    reason = day_entry.get("epr_reason")
    if isinstance(reason, str) and reason.startswith("achieved_soc"):
        return _RELIABILITY_SOC
    if day_entry.get("soc_discrepancy_reliable") is False:
        return _RELIABILITY_SOC
    return _RELIABILITY_UNSTATED


def _settlement_is_provisional(day_entry: dict) -> bool | None:
    """Whether this day's figures are still missing their real P2P
    settlement (nimbus issue #1200).

    Reads the same `real_p2p_settlement_status` field
    `_keep_published_quality_score()` reads, against the same
    `_PROVISIONAL_SETTLEMENT_STATUSES` set, so the row flag and the
    same-day retry can never disagree about what "provisional" means.
    Restating either the field name or the membership test here is
    exactly the drift this scorer keeps recording.

    Returns None when the entry carries no status at all -- a report
    computed before #1016 added the field, or a dict that is not a report
    -- so the caller writes no key rather than guessing. That matters
    more than usual here: defaulting to True would mark every such row
    for a repair that can never succeed, turning one missing field into a
    permanent MILP every cycle.
    """
    status = day_entry.get("real_p2p_settlement_status")
    if not isinstance(status, str):
        return None
    return status in _PROVISIONAL_SETTLEMENT_STATUSES


@functools.cache
def _nimbus_version() -> str | None:
    """This package's own `manifest.json` version, or None.

    Read from disk rather than taken from the config entry, because this
    module runs in BOTH deployment shapes: natively inside HA, where the
    entity layer supplies `sw_version`, and in the standalone/cron writer,
    where there is no entity layer and no `hass` at all (see this module's
    own header for the dual-import boundary). `manifest.json` sits beside
    this file in both, so it is the one source available to each.

    Returns None rather than raising on any failure -- a missing or
    malformed manifest must never cost a day's score. An unstamped row
    then reads exactly as every row written before this change did, which
    is the correct fallback: "scored by something that did not say".
    """
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")
        with open(path, encoding="utf-8") as handle:
            version = json.load(handle).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and version else None


def _version_stamp() -> dict[str, str]:
    """`{"nimbus_version": <this release>}`, or `{}` when unknown.

    **Why a dict to splat rather than a plain value** (nimbus issue #1256).
    `_nimbus_version()` can return None, and an explicit
    `"nimbus_version": None` is worse than an absent key: the entity layer's
    own #972 fallback declines to overwrite a key that is already present, so
    a None would publish through as None and suppress the fallback that would
    otherwise have said something true. An absent key reads exactly as every
    row written before #1120 did -- "produced by something that did not say" --
    which is this module's own established convention for an unknown version.

    **Why the publishers stamp at all, when #972 already adds one.** They are
    answering different questions with the same attribute name, and #1256 is
    what happens when the difference is not noticed:

    * #972's entity-level stamp describes the RUNNING install. That is right
      for its own purpose, which is mirror detection -- a `nimbus_version`
      disagreeing with the version just deployed proves the entity_id
      resolved to another install's sensor.
    * #1120/#1219's row-level stamp describes the release that COMPUTED the
      figures. That is what a reader of a once-a-day report needs.

    The four daily publishers restore their previous attributes across a
    restart (`_RESTORE_ACROSS_RESTART = True`), so for them the two answers
    diverge the moment a release is deployed: the figures are yesterday's, the
    running install is today's. Measured on production 2026-09-26 -- a deploy
    at 10:38 AEST left all four sensors reading `nimbus_version: 0.94.420`
    against `generated_at` values of 00:00, 00:00, 00:00:13 and 06:06, every
    one of them computed by v0.94.417. The same quality report had read
    `0.94.417` correctly a few hours earlier, so the stamp moved without the
    figures being recomputed.

    That is not cosmetic. It read as "the #1233 fix shipped in .420 and did
    not work" when the truth was "these are .417's numbers wearing .420's
    label" -- and this project's own standing lesson is that *a version
    difference is not an explanation for a wrong number until the diff is
    actually read*. A stamp that lies makes that check harder, not easier.

    Stamping here makes the claim travel with the computation, so a restore
    reproduces it unchanged alongside `generated_at`.
    """
    version = _nimbus_version()
    return {"nimbus_version": version} if version is not None else {}


def _carry_forward_quality_history(
    prior_attrs: dict,
    day_key: str,
    day_entry: dict,
    *,
    freshly_computed: bool = True,
) -> dict[str, dict[str, float | str]]:
    """The scored-day table, with today's entry added and the oldest
    trimmed (nimbus issue #994).

    **The defect this fixes.** `publish_daily_quality_report()` built a
    fresh attributes dict every time it scored a day, and `ha_post_state`
    replaces attributes wholesale -- so each new score silently DESTROYED
    the `history` table. Nothing wrote it back, because nothing in this
    integration ever wrote it in the first place: it came from the
    standalone retrospective writer, and `nimbus-regret-card.js` treats
    it as authoritative ("the bible", per the household's own 2026-09-05
    instruction).

    With the table gone, the card falls back per date to re-scoring that
    day live -- and says so in its own sub-header, which is the only
    reason this was ever visible at all. The two paths do not agree:
    measured on a real install, `j_star` **-$9.45 live against -$15.17 in
    the table** for the same day, so EPR read **117.8% on the card and
    103.66% on the sensor**. `j_ref` differed too (4.43 vs 4.79), and
    `j_ref` is the battery-idle baseline -- two scorers agreeing on their
    inputs agree on it whatever they do with the battery. They did not,
    so the two paths were never reading the same input history.

    That disagreement is not new and was never root-caused: the card's
    own header comment records EPR 71.5% vs the table's 89.9% on
    2026-09-04, "real root cause not yet found". This is it. The card was
    never comparing two scorers -- the table it was built to trust had
    been wiped, leaving only the fallback.

    **Why the integration now owns the table rather than merely
    preserving someone else's.** Carrying the prior dict forward fixes
    the wipe, but an install that never ran the standalone writer would
    still have no table and still fall back forever. Maintaining it here
    makes the card work as designed on every install, which is what this
    project's own portability rule asks for regardless.

    Prior entries are preserved exactly as found, including any written
    by the standalone writer -- this only ever adds one key and drops the
    oldest beyond the cap.
    """
    prior = prior_attrs.get("history")
    history: dict[str, dict[str, float | str]] = {}
    if isinstance(prior, dict):
        # Defensive about shape rather than trusting it: this dict may
        # have been written by another program entirely, and one bad
        # entry must not cost the whole table.
        for key, value in prior.items():
            if isinstance(key, str) and isinstance(value, dict):
                history[key] = value
    # nimbus issue #1219: keep the row as it was BEFORE rebuilding it, so
    # a re-push can carry its original stamp forward rather than claiming
    # the running release produced it.
    prior_row = dict(history.get(day_key) or {})
    history[day_key] = {
        field: day_entry[field]
        for field in _QUALITY_HISTORY_FIELDS
        if field in day_entry
    }
    # nimbus issue #1120: stamp the release that produced this row, so a
    # table mixing scoring formulas says so. Only the row being written
    # now -- prior entries are preserved exactly as found (including ones
    # written by the standalone writer, and ones written before this
    # change, which correctly stay unstamped rather than being back-dated
    # to a version that did not score them).
    # nimbus issue #1219: stamp the release that COMPUTED these figures,
    # which is not always the release doing the pushing.
    #
    # **The defect.** `publish_daily_quality_report()`'s idempotency fast
    # path re-pushes the already-published attributes to keep the
    # freshness stamp alive (#289/#292) and seeds the table while it is
    # there (#994) -- passing the published attributes back in as
    # `day_entry`. Nothing is recomputed on that path, but this stamp
    # fired anyway, so an upgrade-then-restart silently re-labelled the
    # published day with the new release.
    #
    # Measured on the reference household, 2026-09-25: the 2026-09-24 row
    # moved `v: 0.94.413` -> `v: 0.94.417` across a restart while
    # `generated_at` stayed `06:00:00` and every figure was byte-identical
    # (`epr 21.11`, `j_ach -3.0503`, `j_star -20.9741`). The row asserted
    # that v0.94.417 produced numbers v0.94.413 produced.
    #
    # That inverts the field's whole purpose. #1120 added it so a table
    # mixing scoring formulas says so -- a row carrying the buggy
    # release's output must not be able to present as the fixed
    # release's. A stamp that advances on restart rather than on scoring
    # is worse than no stamp, because it is trusted: the obvious reading
    # of a version change on a row is "it rescored", and here it had not.
    #
    # So the seed path preserves whatever the row already carried,
    # including carrying nothing -- an unstamped pre-#1120 row stays
    # unstamped, which is exactly what that issue wanted.
    if freshly_computed:
        version = _nimbus_version()
        if version is not None:
            history[day_key][_QUALITY_HISTORY_VERSION_FIELD] = version
    elif _QUALITY_HISTORY_VERSION_FIELD in prior_row:
        history[day_key][_QUALITY_HISTORY_VERSION_FIELD] = prior_row[
            _QUALITY_HISTORY_VERSION_FIELD
        ]
    # nimbus issue #1162 ask 2: and the verdict that qualifies those five
    # numbers, so a card can caveat any row rather than only the latest.
    code = _epr_reliability_code(day_entry)
    if code is not None:
        history[day_key][_QUALITY_HISTORY_RELIABILITY_FIELD] = code
    # nimbus issue #1200: and whether those five numbers are still
    # waiting on this day's real P2P settlement, so the repair sweep can
    # still find this row tomorrow rather than only within the hour.
    # Written only when provisional -- see the field's own note on why
    # that convention is the opposite of `"r"` above.
    if _settlement_is_provisional(day_entry) is True:
        history[day_key][_QUALITY_HISTORY_PROVISIONAL_FIELD] = 1
    if len(history) > _QUALITY_HISTORY_MAX_DAYS:
        # ISO dates sort lexicographically, so this is a real
        # most-recent-N without parsing anything.
        for stale in sorted(history)[: len(history) - _QUALITY_HISTORY_MAX_DAYS]:
            del history[stale]
    return history


# nimbus issue #1120, part 3: the ceiling on a single rescore call.
#
# Each day costs a full oracle MILP -- the same solve the daily scorer
# runs once a night -- so a rescore is explicit, bounded, and never
# automatic on upgrade. Thirty is the practical cap rather than
# _QUALITY_HISTORY_MAX_DAYS (60): a caller who genuinely wants the whole
# table can call twice, and the smaller number makes an accidental
# "rescore everything" cost minutes rather than an hour.
_RESCORE_MAX_DAYS = 30


def rescore_quality_history(
    cfg: dict, now: datetime, days: int, only_dates: set[str] | None = None
) -> dict:
    """Re-score the last `days` complete local days and write the results
    back into the quality report's `history` table (nimbus issue #1120).

    **The gap this closes.** A day is scored ONCE, the morning after, and
    frozen. Nothing ever recomputes an entry, so every scoring-formula
    change splits the table in two: days scored before the change keep
    whatever formula was live then, days after are right, and the two sit
    on the same card with nothing reconciling them. `compute_quality_
    report()` already scores an arbitrary window correctly -- it just
    RETURNS the answer and writes nothing back, so an operator who knows
    exactly which row is stale still has no way to fix it.

    **Why a separate service rather than a `persist: true` flag on
    `compute_quality_report`.** That service scores an ARBITRARY window;
    this table is keyed by calendar DAY. Persisting a 6-hour or 3-day
    window would mean either silently rounding it to a day key (wrong, and
    invisibly so) or rejecting most calls (confusing). Whole days are the
    only unit the table can actually hold, so the service that writes to
    it takes days.

    **Day boundaries are derived exactly as the daily scorer derives
    them** -- local midnight to local midnight, `allow_partial=False` --
    so a rescored row is directly comparable to one written the ordinary
    way rather than subtly different in its window.

    **A failed day is skipped, never fatal.** Real history thins out as
    it ages and any single day may be unscoreable; aborting the run would
    throw away every day already computed, each of which cost a MILP.
    Skips are returned with their reason so the caller sees what did not
    happen rather than inferring it from a short list.

    **Rows are written through `_carry_forward_quality_history()`**, the
    same merge the daily scorer uses, so a rescored row is stamped with
    the release that produced it exactly like a fresh one -- a table
    rescored halfway still says which half is which.

    **If the rescored day IS the currently-published day, the headline
    numbers move too.** The "Yesterday" card reads the sensor's top-level
    attributes while the trend card reads `history`; correcting one and
    not the other would leave them disagreeing, which is the very defect
    this issue is about rather than a fix for it.

    **`only_dates` narrows the run without narrowing the window.** The
    look-back still defines how far back to reach, but a caller that
    already knows exactly which rows are wrong can name them and pay for
    those solves alone. `repair_provisional_quality_history()` (nimbus
    issue #1200) is the caller that needs it: it repairs one specific
    settled day per cycle, and without this it would either buy 30 MILPs
    to fix one row, or need its own duplicate copy of the writeback and
    headline-sync logic below -- and duplicating that is the enumeration
    defect #1167 recorded four times in two days.

    Returns a summary dict (`rescored`, `skipped`, counts) rather than
    None, so the caller can see each day's before/after and judge whether
    the rescore actually changed anything.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    if days > _RESCORE_MAX_DAYS:
        raise ValueError(
            f"days must be <= {_RESCORE_MAX_DAYS} (each day is a full oracle "
            f"MILP solve), got {days}"
        )

    existing = ha_get(QUALITY_ENTITY_ID)
    attrs = dict(existing.get("attributes") or {})
    state = existing.get("state")
    latest_date = attrs.get("latest_date")

    rescored: list[dict] = []
    skipped: list[dict] = []
    latest_entry: dict | None = None

    for back in range(1, days + 1):
        target = (now - timedelta(days=back)).date()
        key = target.isoformat()
        if only_dates is not None and key not in only_dates:
            # Placed BEFORE _compute_report_for_window() so an unwanted
            # day never costs its oracle MILP, which is the whole point of
            # the parameter. Deliberately NOT appended to `skipped`
            # either: a caller that named the days it wants is not asking
            # about the ones it did not name, and reporting 29 "skips"
            # for a one-day repair would bury the one line that matters.
            continue
        day_start = datetime(target.year, target.month, target.day, tzinfo=LOCAL_TZ)
        day_end = day_start + timedelta(days=1)
        try:
            entry = _compute_report_for_window(
                cfg, day_start, day_end, allow_partial=False
            )
        except Exception as e:  # noqa: BLE001 -- one bad day must not
            # cost every day already scored in this run, each of which
            # already paid for its own oracle MILP. The reason is both
            # LOGGED and returned: returning it alone would satisfy the
            # caller but leave nothing in the log for anyone reading the
            # install afterwards, which is the silent-failure shape
            # tests/test_solver_writer_no_silent_failures.py exists to
            # stop (and did stop -- this handler shipped without the log
            # line and that guard caught it in CI).
            _LOGGER.warning(
                "Nimbus quality (#1120): rescoring %s failed (%s: %s) -- "
                "skipping this day and continuing with the rest",
                key,
                type(e).__name__,
                e,
            )
            skipped.append({"date": key, "reason": f"{type(e).__name__}: {e}"})
            continue
        if entry is None:
            skipped.append(
                {"date": key, "reason": "no usable real history for this day"}
            )
            continue
        before = dict((attrs.get("history") or {}).get(key) or {}) or None
        attrs["history"] = _carry_forward_quality_history(attrs, key, entry)
        rescored.append(
            {"date": key, "before": before, "after": dict(attrs["history"][key])}
        )
        if key == latest_date:
            latest_entry = entry

    if rescored:
        if latest_entry is not None:
            # nimbus issue #1167: publish what the DAILY scorer would
            # have published for this day, rather than copying a
            # hand-maintained list of field names onto the previous
            # computation's payload.
            #
            # This is the fourth instance of one defect class in two days
            # (#1149's energy_decomposition, #1164's reliability fields,
            # v0.94.402's version stamp, and then the whole hourly
            # payload). Each earlier fix added names to a tuple. The
            # names were never the problem -- enumerating was.
            #
            # Measured on the dev install under v0.94.402, after a
            # rescore that reported success and wrote a correctly
            # stamped row:
            #
            #     regret_dollars       2.0674   <- the rescored day
            #     sum(hourly_regret)   3.6485   <- the previous one
            #
            # Those are the same quantity computed two ways, disagreeing
            # by 76%, because one moved and the other did not.
            # `hourly_regret` is what nimbus-regret-card.js reads, so the
            # card rendered one day's hours under another day's total.
            #
            # `publish_daily_quality_report()` has always done this
            # correctly -- it spreads `**day_entry` wholesale. The
            # rescore was the only path that enumerated, and the only
            # one that drifted. Both now state the same rule: the
            # headline describes the day that was just scored, entirely.
            history = attrs.get("history")
            attrs.update(latest_entry)
            if history is not None:
                # Merged separately above, across every rescored day --
                # `latest_entry` is one day and must not replace it.
                # Mirrors #994's own ordering note on the daily path.
                attrs["history"] = history
            if "epr_pct" in latest_entry:
                state = latest_entry["epr_pct"]
            # The one field the recomputed report never carries, so an
            # update() cannot supply it: `_compute_report_for_window()`
            # does not set it and the sensor adds it as a fallback only
            # when the publish did not. Set from the running code,
            # because that is the truthful claim -- this rescore was
            # produced by THIS release.
            attrs["nimbus_version"] = _nimbus_version()
            # nimbus issue #1220: and the timestamp, for exactly the same
            # reason stated directly above -- `generated_at` is another
            # field the recomputed report never carries, so `update()`
            # cannot supply it either, and it was left describing the
            # publish these figures just replaced.
            #
            # Measured on the reference household, 2026-09-25: a rescore
            # moved `epr` 21.11 -> 68.85, `j_ach` -3.0503 -> -14.6231 and
            # `achieved_soc_max_pct` 125.4833 -> 92.1833 while
            # `generated_at` stayed at `2026-09-25T06:00:00+10:00`.
            #
            # This is not only cosmetic: `_keep_published_quality_score()`
            # (#1082) decides whether a provisional day is re-scored by
            # computing `age = now - parse_iso(generated_at)`, so a stale
            # value drives the retry cadence from a superseded
            # computation. It also misleads at the worst moment -- a
            # rescore is run precisely when someone is questioning a
            # figure, and `generated_at` is the field they check to see
            # whether it actually recomputed.
            #
            # Fifth instance of one class (#1149 energy_decomposition,
            # #1164 reliability fields, v0.94.402's version stamp, #1167's
            # hourly payload, this). #1167 shipped a guard that discovers
            # the REPORT's fields rather than listing them -- which
            # structurally cannot catch this one, because `generated_at`
            # is not a field of the report. It is added by the publisher.
            # The enumeration problem was solved for the report's fields
            # and left unsolved for the publisher's own.
            attrs["generated_at"] = now.isoformat()
        ha_post_state(QUALITY_ENTITY_ID, state, attrs)
        _LOGGER.info(
            "Nimbus quality (#1120): rescored %d day(s) %s, skipped %d",
            len(rescored),
            ", ".join(r["date"] for r in rescored),
            len(skipped),
        )
    else:
        _LOGGER.warning(
            "Nimbus quality (#1120): rescore over the last %d day(s) wrote "
            "nothing -- every day was unscoreable (%s)",
            days,
            "; ".join(f"{s['date']}: {s['reason']}" for s in skipped) or "no days",
        )

    return {
        "days_requested": days,
        "rescored_count": len(rescored),
        "skipped_count": len(skipped),
        "published": bool(rescored),
        "latest_date_rescored": latest_entry is not None,
        "rescored": rescored,
        "skipped": skipped,
    }


# nimbus issue #1082: the two settlement statuses that mean "not settled
# YET", as opposed to settled, or never going to be.
#
#   applied                              -> final, the real figures are in
#   no_sensor_configured                 -> permanent, this install has none
#   window_is_not_one_local_calendar_day -> permanent for that window shape
#
# Only the two below can change on their own with nothing but time passing,
# so only they are worth scoring again.
_PROVISIONAL_SETTLEMENT_STATUSES = frozenset(
    {"no_settlement_entry_for_this_date", "settlement_sensor_unreadable"}
)

# How long to leave a provisional score alone before scoring that day again.
#
# The publisher runs on every solve cycle -- about once a minute -- and a
# rescore costs a full oracle MIP. Retrying on every cycle would be a
# straight repeat of #773, where a multi-minute solve firing repeatedly
# starved HA's executor badly enough to fail backups. An hour bounds the
# worst case (a day whose settlement never arrives) at ~24 extra solves,
# and the retry window closes on its own at local midnight when
# `yesterday_key` rolls over.
_PROVISIONAL_RESCORE_INTERVAL = timedelta(hours=1)


def _keep_published_quality_score(attrs: dict, now: datetime) -> bool:
    """Whether an already-published score for this day should stand, or be
    computed again (nimbus issue #1082).

    **The defect.** `compute_daily_quality_report()` scores "yesterday",
    and the publisher is driven from the solve cycle, so the first attempt
    lands just after local midnight -- before that day's P2P settlement has
    populated. `real_p2p_dollars` is then 0, which additionally makes
    `real_p2p_volume_kwh > 0.01` false, so the bonus-priced `grid_oracle`
    rebuild never runs either: `j_ach`, `j_star` and `j_ref` all go
    P2P-blind together and the day is scored as though the household had no
    P2P arrangement at all.

    The idempotency fast path then re-pushes that score verbatim on every
    later cycle, so it stands permanently. Measured on a real install:
    v0.94.378 was still publishing 16 Sep at **EPR 90.6%** with
    `real_p2p_dollars: 0` twenty-one hours later, where scoring the
    identical day once the settlement had landed returned **95.71%** with
    the real $14.5364 applied. The whole 5-point gap is the P2P revenue.

    Very likely the mechanism behind a long-standing household report --
    *"I export, and your system tells me my EPR records zero P2P exports"*
    -- which had been read as a gating bug more than once. It is a timing
    bug. `real_p2p_settlement_status` has reported the truth since #1016;
    nothing was hiding it, nothing was reading it.

    **Why this is the whole fix.** The status field already distinguishes
    "not settled yet" from "settled" and from "never will be", so a
    provisional score can be recognised without any new published state, a
    new config field, or a notion of provisionality the sensor does not
    have. It is self-limiting in both directions: it stops as soon as a day
    settles, and `yesterday_key` rolls over at midnight regardless.

    An unparseable or missing `generated_at` keeps the score. Rescoring
    forever on a timestamp that cannot be read would be a worse failure
    than the one being fixed, and it is exactly the shape of thing that
    turns a diagnostic into an outage.
    """
    if attrs.get("real_p2p_settlement_status") not in _PROVISIONAL_SETTLEMENT_STATUSES:
        return True
    generated_at = attrs.get("generated_at")
    if not generated_at:
        return True
    try:
        age = now - parse_iso(generated_at)
    except (TypeError, ValueError, AttributeError):
        # `parse_iso()` anchors a genuinely naive value to UTC (#363), so
        # the subtraction should always compute today. Catching it anyway
        # is what keeps a future change there from turning this
        # diagnostic into an every-cycle exception -- but deliberately
        # around the SUBTRACTION rather than as a separate `tzinfo is
        # None` test, which parse_iso's own contract makes unreachable.
        # An unreachable guard is indistinguishable from one that works.
        return True
    return age < _PROVISIONAL_RESCORE_INTERVAL


def _settlement_entry_exists(cfg: dict, day_key: str) -> bool:
    """Whether the configured settlement sensor now carries an entry for
    `day_key` (an ISO local date).

    This is the gate that makes the repair sweep below self-limiting. A
    rescore costs a full oracle MILP, so the sweep must never pay one
    speculatively: a day whose settlement genuinely never arrives -- the
    sensor was misconfigured, the provider never published that date --
    would otherwise buy one solve per cycle forever, which is #773's
    executor-starvation failure re-created deliberately.

    **Checks for PRESENCE, not for a non-zero figure.** A genuinely
    settled day of zero export is a legitimate result, and reading it as
    "not arrived yet" would leave that row provisional for good -- the
    same absence-as-the-only-signal trap this scorer keeps recording.

    Every failure reads as False rather than raising. "Cannot repair
    anything this cycle" is the conservative direction, and is already
    exactly what the sweep does when there is nothing to repair.
    """
    settlement_sensor = cfg.get("solver_p2p_settlement_history_sensor")
    if not settlement_sensor:
        return False
    try:
        history = ha_get(settlement_sensor)["attributes"]["history"]
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
    ):
        return False
    return isinstance(history, dict) and day_key in history


# At most one day repaired per cycle. Each is a full oracle MILP -- the
# same solve the nightly scorer runs once -- and the cycle this runs
# inside ticks about once a minute. One per cycle drains a week's backlog
# in seven minutes while never putting two MILPs in one tick, which is
# the #773/#757 executor-starvation shape this repo has already paid for
# twice.
_PROVISIONAL_REPAIR_PER_CYCLE = 1


def repair_provisional_quality_history(cfg: dict, now: datetime) -> dict | None:
    """Re-score any PAST row whose figures were taken before its own P2P
    settlement existed, once that settlement has since arrived (nimbus
    issue #1200).

    **What this fixes that #1082 did not.** #1082 re-scores the currently
    published day hourly while it is provisional, and that retry is
    reachable only while `latest_date == yesterday_key` -- so it expires
    at local midnight. Settlement on the reference household lands at
    ~09:00 local for the previous day, comfortably inside that window,
    and it still failed on three consecutive measured days: the retry's
    last firing was 06:00 each time. Whatever stopped it, a repair that
    has to win a race against midnight will keep losing it -- to a
    restart, a failover, a slow provider, or the 06:00 retrain. This one
    has no deadline, because the row itself records that it is waiting.

    That distinction is why this is not another attempt at the same fix.
    #1082 made the repair possible; it left it time-boxed. This removes
    the box.

    **Bounded, and gated on evidence rather than hope.** A candidate is
    re-scored only once `_settlement_entry_exists()` confirms the real
    entry is there, and at most `_PROVISIONAL_REPAIR_PER_CYCLE` day is
    touched per call. A day whose settlement never arrives therefore
    costs zero solves, not one per cycle.

    **Oldest first**, so a backlog drains in the order the days happened
    -- the trend chart fills left to right rather than in scattered
    pieces, and a repeatedly-interrupted install still makes monotonic
    progress instead of re-picking the same row.

    **The currently-published day is deliberately left alone.** That is
    #1082's job, it already retries within the hour, and rescoring it
    here as well would mean two MILPs for one day in one cycle. It stops
    being the published day at midnight, at which point this sweep owns
    it like any other row.

    **Rows written before this change carry no flag and are not
    back-dated**, matching the `"v"` field's own rule. They are not
    invisible, they are simply not self-healing: `nimbus_load.
    rescore_history` remains the way to repair a row from before the flag
    existed, and running it once is what puts every subsequent day under
    this sweep.

    Returns a summary of what it repaired, or None when there was nothing
    to do -- so the caller logs a real event and stays silent otherwise.
    """
    try:
        attrs = (
            ha_get(resolve_real_entity_id(QUALITY_ENTITY_ID)).get("attributes") or {}
        )
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None
    history = attrs.get("history")
    if not isinstance(history, dict):
        return None
    latest_date = attrs.get("latest_date")
    candidates = sorted(
        key
        for key, row in history.items()
        if isinstance(key, str)
        and isinstance(row, dict)
        and row.get(_QUALITY_HISTORY_PROVISIONAL_FIELD)
        and key != latest_date
    )
    repaired: list[dict] = []
    for key in candidates:
        if len(repaired) >= _PROVISIONAL_REPAIR_PER_CYCLE:
            break
        target = _safe_fromisoformat(key)
        if target is None:
            continue
        back = (now.date() - target.date()).days
        if back < 1 or back > _RESCORE_MAX_DAYS:
            # Not actually in the past, or older than what a single
            # rescore call may reach. Left alone rather than clamped:
            # silently widening that ceiling here would hide the cost of
            # the solves it buys.
            continue
        if not _settlement_entry_exists(cfg, key):
            continue
        _LOGGER.info(
            "Nimbus quality (#1200): %s was scored before its settlement "
            "existed and the real entry has since landed -- re-scoring it now "
            "so the row stops under-reporting the day",
            key,
        )
        repaired.append(
            {
                "date": key,
                "summary": rescore_quality_history(cfg, now, back, only_dates={key}),
            }
        )
    return {"repaired": repaired} if repaired else None


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

    Real fix (2026-08-30, issues #289/#292): the "already scored" fast
    path used to just `return` with nothing published. That silently
    stopped refreshing this entity's own freshness stamp (update_from_
    solver()'s `_last_updated`, see sensor.py's `_NimbusSolverPushSensor`)
    the moment a day was first scored -- so after `_STALE_AFTER_SECONDS`
    (300s) with no NEW publish, the freshness watchdog correctly marked
    the entity unavailable. HA core's own `Entity.async_write_ha_state()`
    then writes an EMPTY attributes dict for an unavailable entity (real,
    long-standing HA core behaviour, not a bug in this integration) --
    so the VERY NEXT idempotency check here read back `attributes={}`,
    found no `latest_date` to match, and recomputed+republished from
    scratch. That one republish refreshed the stamp, the entity went
    available again, held for up to 300s, then repeated the whole cycle
    forever -- exactly the "fires every ~10 minutes, self-heals" pattern
    both issues independently, precisely documented. Fix: re-push the
    SAME already-read state/attributes on the fast path instead of doing
    nothing, so the freshness stamp keeps getting refreshed every cycle
    and the entity never goes stale (and therefore never has its
    attributes cleared) in the first place, matching the reference
    script's own "already scored... re-pushing sensor" behaviour that
    this native path had dropped.
    """
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    # nimbus issue #994: read once, used twice -- the fast path's own
    # idempotency check below, and the `history` carry-forward at publish
    # time. Defaults to empty so a first-ever publish, or an unreachable
    # read, still produces a valid one-entry table rather than crashing.
    existing_attrs: dict = {}
    try:
        # resolve_real_entity_id() (2026-08-31): read back THIS entity's
        # own real state, not whatever the literal QUALITY_ENTITY_ID
        # string happens to resolve to if something else (e.g. a
        # remote_homeassistant mirror of another install) has claimed it.
        # See that function's own docstring for the full incident.
        existing = ha_get(resolve_real_entity_id(QUALITY_ENTITY_ID))
        existing_attrs = existing.get("attributes", {}) or {}
        # Deliberately spelled out rather than reusing `existing_attrs`
        # above: test_solver_writer_family_a_freshness_repush.py matches
        # this exact expression as source text across every Family A
        # publisher, to prove none of them lost the idempotency check
        # that keeps a once-a-day score from re-solving 1440 times.
        # Tidying this into the local costs that guard its match.
        #
        # nimbus #1082: the date matching is necessary but no longer
        # sufficient. A score taken before the day's settlement landed is
        # provisional, and re-pushing it verbatim is what made it
        # permanent -- see _keep_published_quality_score() for the
        # measured 5-EPR-point case.
        if existing.get("attributes", {}).get("latest_date") == yesterday_key:
            # nimbus #1082: having scored this day is necessary but no
            # longer sufficient. A score taken before the day's settlement
            # landed is PROVISIONAL, and re-pushing it verbatim on every
            # later cycle is exactly what made it permanent. Falling
            # through here re-scores it with the settlement that has since
            # arrived -- see _keep_published_quality_score() for the
            # measured 5-EPR-point case, and for why this cannot loop.
            #
            # Deliberately nested rather than folded into the condition
            # above: test_solver_writer_family_a_freshness_repush.py
            # locates this fast path by the exact source text
            # `if<check>:`, so an `and` on that line silently defeats a
            # guard that exists to stop a once-a-day score re-solving
            # 1440 times. Correctness of the guard beats tidiness here.
            if not _keep_published_quality_score(existing_attrs, now):
                _LOGGER.info(
                    "Nimbus quality: %s was scored before its settlement "
                    "was available (%s) -- re-scoring it now that the "
                    "real figures may have landed",
                    yesterday_key,
                    existing_attrs.get("real_p2p_settlement_status"),
                )
            else:
                # issue #313 (Mark Purcell): this fast path used to be
                # externally indistinguishable from every silent-skip path
                # below it -- same "nothing changed, nothing logged" outcome.
                # DEBUG, not INFO: this is the expected, common case on every
                # cycle after the first of a given day, not a diagnostic event.
                # nimbus issue #994, second half: seed the table on the fast
                # path too. Without this, an install whose `history` was
                # already wiped -- which is every install that ever ran the
                # pre-v0.94.346 publisher -- keeps re-pushing the same
                # history-less attributes until a NEW day is scored, so the
                # Regret card goes on falling back to its second scorer for
                # another full day after the fix lands.
                #
                # Costs nothing: the five numbers for the already-scored day
                # are sitting in the attributes being re-pushed, so this
                # needs no recompute and no LP solve. `existing_attrs` is the
                # same dict object as `existing["attributes"]`, so seeding it
                # here is what the verbatim re-push below then publishes.
                existing["attributes"]["history"] = _carry_forward_quality_history(
                    existing_attrs,
                    yesterday_key,
                    existing_attrs,
                    # nimbus issue #1219: this path recomputes nothing --
                    # `day_entry` here IS the previously published
                    # attributes -- so it must not restamp the row with
                    # the running release.
                    freshly_computed=False,
                )
                _LOGGER.debug(
                    "Nimbus quality: fast-path hit, already scored %s -- re-"
                    "pushing cached state to keep the freshness stamp alive",
                    yesterday_key,
                )
                ha_post_state(
                    QUALITY_ENTITY_ID, existing["state"], existing["attributes"]
                )
                return
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        pass  # never seen before, or transiently unreachable -- fall through and try to compute
    day_entry = compute_daily_quality_report(cfg, now)
    if day_entry is None:
        # issue #313: compute_daily_quality_report()/_compute_report_for_
        # window() already logs the SPECIFIC reason for a None return
        # (missing config, missing history, infeasible oracle) at its own
        # call site -- this one line is what ties that reason back to
        # "and therefore the sensor was not updated this cycle," so a log
        # search for this entity's own name always surfaces the full story.
        _LOGGER.debug(
            "Nimbus quality: no report for %s this cycle -- sensor left "
            "unchanged, will retry next cycle (see the reason logged just "
            "above, if any)",
            yesterday_key,
        )
        return
    # nimbus issue #956: the oracle is a bound by construction, so a
    # negative regret is not a result -- it is proof the comparison was
    # invalid, and the EPR sitting above 100% beside it is not a score a
    # household can act on. Its own warned-set, because this and the
    # #538 condition are independent: a day can hit either, both, or
    # neither, and silencing one must not silence the other.
    if (
        day_entry.get("regret_reliable") is False
        and yesterday_key not in _QUALITY_REPORT_NEGATIVE_REGRET_WARNED
    ):
        _QUALITY_REPORT_NEGATIVE_REGRET_WARNED.add(yesterday_key)
        envelope = day_entry.get("lp_soc_envelope_pct") or [None, None]
        if day_entry.get("achieved_within_lp_soc_bounds") is False:
            diagnosis = (
                "the achieved SoC trajectory ranged {}-{}% against the LP's "
                "own configured envelope of {}-{}%, so it was priced against "
                "a strictly LARGER feasible set than the oracle -- it could "
                "sell energy the oracle is structurally forbidden to touch, "
                "which is exactly how 'optimal' gets beaten. Note this is NOT "
                "what soc_discrepancy's own out_of_range flag tests: that one "
                "asks about [0, 100], physical possibility, and reads fine "
                "here"
            ).format(
                day_entry.get("achieved_soc_min_pct"),
                day_entry.get("achieved_soc_max_pct"),
                envelope[0],
                envelope[1],
            )
        else:
            diagnosis = (
                "the achieved SoC trajectory stayed INSIDE the LP's own "
                "envelope, so the mechanism verified on the reference "
                "household (#956) does not explain this one -- please report "
                "this day's report on nimbus issue #956, it is a second cause"
            )
        _LOGGER.warning(
            "Nimbus quality: %s scored with regret_dollars=%s, which is "
            "NEGATIVE -- the achieved dispatch priced out cheaper than "
            "perfect foresight. EPR reads %s%% and neither figure is usable "
            "for this day. Diagnosis: %s.",
            yesterday_key,
            day_entry.get("regret_dollars"),
            day_entry.get("epr_pct"),
            diagnosis,
        )
    # nimbus issue #1089: EPR's own denominator went non-positive, so
    # the published percentage is not a fraction of anything available.
    # Warned separately and unconditionally on its own set because this
    # is the ONLY condition in this scorer whose failure mode makes the
    # headline look better than the truth -- the real 2026-09-17 day
    # published 242.7% on a day the household spent $3.50 more than
    # idling, with regret_dollars POSITIVE and every other flag clean.
    if (
        day_entry.get("epr_denominator_reason") is not None
        and yesterday_key not in _QUALITY_REPORT_EPR_DENOMINATOR_WARNED
    ):
        _QUALITY_REPORT_EPR_DENOMINATOR_WARNED.add(yesterday_key)
        _LOGGER.warning(
            "Nimbus quality: %s published EPR %s%% but its DENOMINATOR "
            "(theoretical_maximum_yield = j_ref - j_star) is %s, which is "
            "not positive -- reason %r. j_star=%s priced out WORSE than the "
            "do-nothing baseline j_ref=%s, so EPR measured value captured "
            "against a baseline the oracle was not free to choose. Two "
            "documented causes, and this day's own figures say which to "
            "look at: a binding loss-making P2P export commitment the "
            "oracle cannot decline (nimbus #1001 -- this day's "
            "p2p_commitment_shortfall_kwh=%s), and a pricing-path mismatch "
            "between the LP objective and the evaluator (nimbus #1081 -- "
            "this day's j_star_evaluator=%s, j_star_path_delta=%s). Treat "
            "the EPR for this day as uninterpretable REGARDLESS of its "
            "sign: when value_captured=%s is also negative the two signs "
            "cancel and a bad day publishes as a high score.",
            yesterday_key,
            day_entry.get("epr_pct"),
            day_entry.get("theoretical_maximum_yield"),
            day_entry.get("epr_denominator_reason"),
            day_entry.get("j_star"),
            day_entry.get("j_ref"),
            day_entry.get("p2p_commitment_shortfall_kwh"),
            day_entry.get("j_star_evaluator"),
            day_entry.get("j_star_path_delta"),
            day_entry.get("value_captured"),
        )
    if (
        day_entry.get("soc_discrepancy_reliable") is False
        and yesterday_key not in _QUALITY_REPORT_UNRELIABLE_WARNED
    ):
        _QUALITY_REPORT_UNRELIABLE_WARNED.add(yesterday_key)
        # nimbus issue #538 (Mark Purcell, item 2): two genuinely
        # different causes now share this flag -- name which one this
        # day actually hit, instead of a single message that always
        # reads as the out-of-range case even when it was a plain
        # threshold disagreement.
        reason = day_entry.get("soc_discrepancy_reason")
        if reason == "out_of_range":
            cause = (
                "the achieved SoC integration went outside the physically "
                "real [0, 100] range for at least one hour this day (a real "
                "recorder history gap, or the configured battery power/"
                "capacity sensors not matching what they physically "
                "describe -- compare this report's own achieved_energy_in_"
                "kwh/achieved_energy_out_kwh against solver_battery_"
                "capacity_kwh to tell the two apart)"
            )
        else:
            cause = (
                "the achieved SoC integration stayed inside [0, 100] but "
                "disagreed with the real SoC sensor by more than this "
                "install's configured threshold (number.nimbus_solver_"
                "soc_discrepancy_max_threshold_pct/_mean_threshold_pct) -- "
                "usually the SoC sensor covering different physical "
                "storage than the power sensor/capacity model (see nimbus "
                "issue #532)"
            )
        _LOGGER.warning(
            "Nimbus quality: %s scored with soc_discrepancy_reliable=False "
            "(reason=%s, max discrepancy %.1f pt, mean %.1f pt) -- %s. EPR "
            "and every other figure on this day's report are unreliable "
            "until this is understood.",
            yesterday_key,
            reason,
            day_entry.get("soc_discrepancy_max_pct") or 0.0,
            day_entry.get("soc_discrepancy_mean_pct") or 0.0,
            cause,
        )
    ha_post_state(
        QUALITY_ENTITY_ID,
        # State channel gets the percent-scaled value (0..100) so it
        # renders correctly against unit_of_measurement="%" below.
        # The canonical 0..1 fraction stays available as the "epr"
        # attribute via the **day_entry expansion for consumers that
        # want the raw ratio (compute_quality_report service payload,
        # OpEd hero chart, LinkedIn article calcs).
        day_entry["epr_pct"],
        {
            # Real bug found live (household-reported repeated Repairs
            # entries, 2026-08-31: "sensor.nimbus_solver_quality_report
            # no longer has a state class" -- on both devhub and the
            # reference household's NUC1, "pretty sure not the first
            # time"): this dict used to set unit_of_measurement to a bare
            # null, with no state_class key at all. In native mode,
            # with a registered SensorEntity handler present (the normal
            # case), that entity's own _attr_native_unit_of_measurement/
            # _attr_state_class correctly override this dict's stray
            # values by the time a live GET reads the state back -- but
            # ha_post_state()'s own RAW states.async_set() FALLBACK
            # (used whenever no handler is registered yet -- e.g. this
            # function racing sensor.py's own async_setup_entry() right
            # after a restart, or the fully standalone/cron/addon
            # deployment path, which never has an entity object at all)
            # writes these exact keys VERBATIM with no entity-level
            # correction available. Whichever path is used, correct,
            # real values here closes the gap outright rather than
            # relying on an override that only exists on one of the two
            # possible code paths.
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "friendly_name": "Nimbus Solver Quality Report (EPR)",
            "latest_date": yesterday_key,
            "generated_at": now.isoformat(),
            # nimbus issue #1256: stamped with the computation, not left to
            # the entity's running-install fallback -- this sensor restores
            # across a restart, so the two diverge on every deploy. The
            # rescore path already does its own equivalent a few hundred
            # lines up, for the same reason.
            **_version_stamp(),
            # nimbus issue #994: placed BEFORE **day_entry, so a future
            # day_entry key named "history" would win rather than being
            # silently shadowed -- the same ordering rule the keys above
            # already rely on.
            "history": _carry_forward_quality_history(
                existing_attrs, yesterday_key, day_entry
            ),
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
        yesterday.year, yesterday.month, yesterday.day, tzinfo=LOCAL_TZ
    )
    day_end = day_start + timedelta(days=1)

    # nimbus issue #441 (Mark Purcell), same fix/reasoning as #438,
    # updated for #451 -- see _compute_report_for_window()'s own
    # matching comment for the full explanation. This function always
    # scores a fixed 24h "yesterday" window, always far longer than
    # MAX_TIER1_HOURS (60 real minutes post-#451), so this resolves to
    # TIER2_PERIOD_HOURS (30 min) -- matching the live dispatch's own
    # real dominant resolution for a window this long, computed
    # explicitly rather than hardcoding 0.5 directly so this stays
    # correct if either constant, or the window this function scores,
    # ever changes later.
    window_hours = (day_end - day_start).total_seconds() / 3600.0
    period_hours = (
        TIER1_PERIOD_HOURS if window_hours <= MAX_TIER1_HOURS else TIER2_PERIOD_HOURS
    )
    n_periods = round(window_hours / period_hours)
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

    # Same fix as compute_daily_quality_report() -- see _kw_scale_
    # factor()'s own docstring for the real, confirmed-live bug this
    # corrects (a configured *_power_sensor reporting native Watts,
    # silently treated as kW).
    solar_scale = _kw_scale_factor(solar_sensor)
    load_scale = _kw_scale_factor(load_sensor)

    solar_kw = np.array(
        [
            max(0.0, v * solar_scale)
            for v in resample_history_nearest(solar_hist, grid_times)
        ]
    )
    load_kw = np.array(
        [
            max(0.0, v * load_scale)
            for v in resample_history_nearest(load_hist, grid_times)
        ]
    )
    import_price = np.array(
        [
            v + import_fee_rate(cfg, _local(grid_times[i]).hour)
            for i, v in enumerate(
                resample_history_nearest(
                    import_price_hist, grid_times, default=0.20, backfill_first=True
                )
            )
        ]
    )
    export_price = np.array(
        resample_history_nearest(
            export_price_hist, grid_times, default=0.05, backfill_first=True
        )
    )

    # nimbus issue #1013: SoH-derated, same as every other BatteryConfig
    # construction -- see resolve_effective_capacity_kwh()'s docstring.
    capacity_kwh = resolve_effective_capacity_kwh(cfg)
    min_pct = _cfg_num(cfg, "solver_battery_min_soc_percent", 5.0)
    max_pct = _cfg_num(cfg, "solver_battery_max_soc_percent", 100.0)
    # Initial/final SoC don't need real history here the way the EPR
    # report's own tracking comparison does -- the oracle re-solve is
    # free to choose its own trajectory from a reasonable starting point
    # regardless, and this feature's whole question is "how did the
    # SHAPE of the optimal plan change with efficiency," not a tracking
    # comparison against one specific real starting SoC.
    #
    # Clamped into the configured envelope anyway (2026-09-02, nimbus
    # issue #325's own "audit every BatteryConfig construction" ask --
    # this is the third path that issue predicted, found by that audit
    # rather than by a live crash). A bare 50% is NOT unconditionally
    # valid: it sits outside [min, max] for any household running a
    # backup-reserve floor above 50% (solver_battery_min_soc_percent =
    # 60 is a perfectly ordinary setting) or a max below it, and would
    # raise the identical ValueError out of __post_init__ -- taking the
    # whole efficiency-backtest report down the same way #325 took the
    # daily quality report down. No live report of this yet; the point
    # is that there doesn't need to be one.
    # nimbus issue #328 (Mark Purcell): no clamp needed any more, same
    # fix as the two sites above -- elements.BatteryConfig only requires
    # a value inside the physical range [0, capacity_kwh] now, and a
    # bare capacity_kwh*0.5 is trivially always inside that range
    # regardless of where min_soc/max_soc happen to sit. If 50% genuinely
    # falls outside this household's configured [min, max] envelope, the
    # LP's own soft-constraint machinery schedules honest recovery for
    # this synthetic starting assumption exactly the same way it would
    # for a real live/historical below-floor reading -- no separate
    # clamp-and-pretend needed here either.
    _min_soc_kwh = capacity_kwh * min_pct / 100.0
    _max_soc_kwh = capacity_kwh * max_pct / 100.0
    initial_soc_kwh = capacity_kwh * 0.5

    base_battery = elements.BatteryConfig(
        name="home",  # nimbus issue #467: single real household battery, see battery_cfg's own comment above
        capacity_kwh=capacity_kwh,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=_min_soc_kwh,
        max_soc_kwh=_max_soc_kwh,
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

    # nimbus issue #1232: include THIS install's own configured value in the
    # swept set. Before this, the candidates were a fixed (85, 90, 95, 99)
    # and the reference household's configured 85.8 was not among them --
    # so even a correctly-scored sweep could not answer the only question a
    # household actually has ("is my setting the best of these?"). Sorted
    # and de-duplicated so an install configured at exactly 90.0 does not
    # get a doubled candidate.
    configured_pct = _cfg_num(cfg, "solver_efficiency_percent", 90.0)
    swept = tuple(sorted({*EFFICIENCY_CANDIDATES_PERCENT, round(configured_pct, 2)}))
    results = run_efficiency_sensitivity_sweep(
        periods=periods,
        grid=grid_cfg,
        base_battery=base_battery,
        solar=solar_cfg,
        load=load_cfg,
        candidates_percent=swept,
    )
    if not results:
        # Every candidate was genuinely infeasible for this real day --
        # a real, if unusual, outcome (see run_efficiency_sensitivity_
        # sweep()'s own per-candidate defensive skip) -- report nothing
        # rather than a misleadingly empty-but-successful entry.
        return None

    best = min(results, key=lambda r: r.total_cost)
    worst = max(results, key=lambda r: r.total_cost)
    configured_label = efficiency_label(configured_pct)
    configured_result = next((r for r in results if r.label == configured_label), None)
    return {
        "candidates": [
            {
                "efficiency_percent": r.label,
                "total_cost": round(r.total_cost, 4),
                # nimbus issue #1232: how much of this candidate's own plan
                # the configured pack could not physically have delivered.
                # Non-zero means the candidate only looks as good as it does
                # by promising energy below the real floor -- read its cost
                # with that attached.
                "undeliverable_kwh": round(r.undeliverable_kwh, 3),
                "is_configured": r.label == configured_label,
            }
            for r in results
        ],
        "configured_efficiency_percent": round(configured_pct, 1),
        "best_candidate": best.label,
        "best_candidate_cost": round(best.total_cost, 4),
        "worst_candidate": worst.label,
        "worst_candidate_cost": round(worst.total_cost, 4),
        # nimbus issue #1232: the actionable number. Positive means some
        # tested setting would genuinely have served THIS pack better than
        # the configured one on this day; 0.0 means the configured value was
        # the best of those tested. None only if the configured value
        # somehow failed to solve while others did.
        "configured_is_best": (
            None if configured_result is None else best.label == configured_label
        ),
        "best_vs_configured_dollars": (
            None
            if configured_result is None
            else round(configured_result.total_cost - best.total_cost, 4)
        ),
        # nimbus issue #1232: names the physics every candidate was judged
        # by, so this figure is self-describing. Every candidate is scored
        # under the CONFIGURED battery -- varying only what the LP planned
        # with. Scoring each candidate under itself is what made this sweep
        # strictly monotonic and informationless.
        "scored_under": "configured",
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
    times), and for the real 2026-08-30 fix (issues #289/#292): the
    fast path re-pushes the same already-read state/attributes instead
    of returning with nothing published, so this entity's own freshness
    stamp keeps refreshing and it never goes stale/unavailable between
    real recomputes.
    """
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    try:
        # resolve_real_entity_id() -- see publish_daily_quality_report()'s
        # matching comment / that function's own docstring for the full
        # entity-id-collision incident this guards against.
        existing = ha_get(resolve_real_entity_id(BACKTEST_ENTITY_ID))
        if existing.get("attributes", {}).get("latest_date") == yesterday_key:
            ha_post_state(BACKTEST_ENTITY_ID, existing["state"], existing["attributes"])
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
            # nimbus issue #1253: `"$"` assumed a currency. This is the
            # cheapest of that issue's three sites -- a plain REST-posted
            # attribute with no entity-registry or long-term-statistics
            # relationship -- so it carries none of the ~20-repair migration
            # risk the flattened children do. The native path resolves this
            # from `hass.config.currency` on the entity; this standalone/cron
            # path has no `hass`, so it says nothing rather than guessing.
            # An absent unit is honest; a wrong one is not.
            "friendly_name": "Nimbus Efficiency Backtest",
            "latest_date": yesterday_key,
            "generated_at": now.isoformat(),
            # nimbus issue #1256: stamped with the computation -- see the
            # flex report above.
            **_version_stamp(),
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
    has_price_forecast_array install -- see that branch's own comment for why that
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

    # nimbus issue #1013: SoH-derated, same as every other BatteryConfig
    # construction -- see resolve_effective_capacity_kwh()'s docstring.
    capacity_kwh = resolve_effective_capacity_kwh(cfg)
    if capacity_kwh <= 0:
        return None

    day_start = datetime(day.year, day.month, day.day, tzinfo=LOCAL_TZ)
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

    initial_pct = resample_history_nearest(
        soc_hist, [day_start], default=50.0, backfill_first=True
    )[0]
    real_soc_close_pct = resample_history_nearest(
        soc_hist,
        [day_end - timedelta(seconds=1)],
        default=initial_pct,
        backfill_first=True,
    )[0]
    real_soc_checkpoint_pct = (
        resample_history_nearest(
            soc_hist,
            [day_start.replace(hour=checkpoint_hour)],
            default=initial_pct,
            backfill_first=True,
        )[0]
        if checkpoint_hour >= 0
        else None
    )

    # nimbus issue #328 (Mark Purcell): no clamp into [min_soc, max_soc]
    # here either -- this counterfactual tracker exists specifically to
    # honestly answer "what would Nimbus-only SoC actually have been,"
    # so silently pretending the real starting reading was inside the
    # envelope would corrupt the exact number this whole mechanism is
    # built to report. Only clamped to the genuine PHYSICAL range further
    # below, where sim_soc_kwh is updated after each simulated step.
    sim_soc_kwh = capacity_kwh * initial_pct / 100.0
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
                v + import_fee_rate(cfg, _local(gt).hour) + flat_fee_rate
                for v, gt in zip(
                    resample_history_nearest(
                        import_price_hist,
                        grid_times,
                        default=0.20,
                        backfill_first=True,
                    ),
                    grid_times,
                )
            ]
        )
        export_price = np.array(
            resample_history_nearest(
                export_price_hist, grid_times, default=0.05, backfill_first=True
            )
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
        in_p2p_window = checkpoint_hour >= 0 and _local(t).hour >= checkpoint_hour
        salvage_value = 0.0 if in_p2p_window else salvage_value_flat

        periods = elements.PeriodGrid(hours=hours_arr, start=t)
        grid = elements.GridConfig(
            import_price=import_price,
            export_price=export_price,
            import_limit_kw=import_limit_kw,
            export_limit_kw=export_limit_kw,
            # nimbus #1079: gated to the committed blocks. This replay is
            # the "what would Nimbus alone have done" counterfactual, so
            # an ungated premium here makes the counterfactual look good
            # for trades a real household could not have been paid for.
            export_bonus_price=p2p_bonus_price_by_period(
                p2p_bonus_price_flat, fixed_export_kw, n
            ),
            export_bonus_volume_kwh=remaining_bonus_kwh,
            fixed_export_kw=np.array(fixed_export_kw)
            if fixed_export_kw is not None
            else None,
        )
        battery = elements.BatteryConfig(
            name="home",  # nimbus issue #467: single real household battery, see battery_cfg's own comment above
            capacity_kwh=capacity_kwh,
            initial_soc_kwh=sim_soc_kwh,  # nimbus issue #328: honest, no envelope clamp -- see this loop's own seed comment above
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
            # 2026-09-08: reads the SAME live-tunable smoothness_weight as
            # the real dispatch solve (see build_plan()'s own call site
            # further below in this file) rather than a second, silently-
            # divergent hardcoded copy -- this counterfactual tracker
            # exists to honestly answer "what would Nimbus-only SoC have
            # been," which stops being true if it used a different
            # degeneracy-smoothing behaviour than the real solve did.
            plan = network.build_plan(
                periods=periods,
                grid=grid,
                batteries=[battery],
                solar=solar,
                loads=loads,
                smoothness_weight=_cfg_num(
                    cfg,
                    "solver_intraplan_smoothness_weight_kw",
                    network.DEFAULT_SMOOTHNESS_WEIGHT_KW,
                ),
                # nimbus issue #692: same reasoning as smoothness_weight
                # just above -- reads the SAME live-tunable value the real
                # dispatch solve uses, not a second, silently-divergent
                # hardcoded copy.
                battery_charge_earliness_budget_kw=_cfg_num(
                    cfg,
                    "solver_battery_charge_earliness_budget_kw",
                    network.DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
                ),
                # nimbus issue #696: same reasoning again -- reads the
                # SAME live switch.nimbus_solver_calibrated_objective_
                # enabled state the real dispatch solve uses below, not
                # a second, silently-divergent hardcoded choice.
                solve_options=(
                    lp.CalibratedOptions()
                    if bool(cfg.get("solver_calibrated_objective_enabled", True))
                    else None
                ),
            )
        except Exception:
            # nimbus issue #363 (Mark Purcell, codebase review): the
            # freeze-and-continue behaviour stays, breadcrumb added.
            _LOGGER.debug(
                "Nimbus Solver: compute_nimbus_only_soc_counterfactual "
                "tick solve failed",
                exc_info=True,
            )
            plan = None

        if plan is not None and plan.status == "optimal":
            net0 = float(plan.battery_discharge_kw[0] - plan.battery_charge_kw[0])
            if net0 >= 0:
                sim_soc_kwh -= net0 * (step.total_seconds() / 3600.0) / efficiency
            else:
                sim_soc_kwh += (-net0) * efficiency * (step.total_seconds() / 3600.0)
            # nimbus issue #328: clamp to the PHYSICAL range only, not
            # [min_soc, max_soc] -- unlike the envelope clamps removed
            # elsewhere in this fix, this one is load-bearing and stays:
            # sim_soc_kwh really cannot go below 0 or above capacity_kwh,
            # that's a genuine physical law, not a scheduling preference.
            # Sitting outside [min_soc, max_soc] is exactly the real
            # state this tracker needs to be free to report honestly.
            sim_soc_kwh = min(max(sim_soc_kwh, 0.0), capacity_kwh)
            if plan.export_bonus_kw is not None:
                bonus_used_kwh_today += float(plan.export_bonus_kw[0]) * (
                    step.total_seconds() / 3600.0
                )

        if (
            checkpoint_hour >= 0
            and _local(t).hour == checkpoint_hour
            and _local(t).minute < step.total_seconds() / 60.0
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
    times for an already-scored day -- and the same real 2026-08-30 fix
    (issues #289/#292): the fast path re-pushes the same already-read
    state/attributes instead of returning with nothing published, so
    this entity's own freshness stamp keeps refreshing and it never
    goes stale/unavailable between real recomputes.
    """
    yesterday = (now - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    yesterday_key = yesterday.date().isoformat()
    try:
        # resolve_real_entity_id() -- see publish_daily_quality_report()'s
        # matching comment / that function's own docstring for the full
        # entity-id-collision incident this guards against.
        existing = ha_get(resolve_real_entity_id(COUNTERFACTUAL_ENTITY_ID))
        if existing.get("attributes", {}).get("latest_date") == yesterday_key:
            ha_post_state(
                COUNTERFACTUAL_ENTITY_ID, existing["state"], existing["attributes"]
            )
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
            # nimbus issue #1256: stamped with the computation -- see the
            # flex report above.
            **_version_stamp(),
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
                # nimbus issue #843: deliberately keeps the pre-#843
                # backfill here. `hist` is a tight +/-10min window
                # fetched around this exact instant, so its first sample
                # is genuinely representative; falling through to 0.0
                # would record a misleading "solar delivered nothing"
                # ratio whenever the only rows land just after the mark.
                actual_kw = resample_history_nearest(
                    hist, [target_time], backfill_first=True
                )[0]
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


def _dispatch_source_breakdown(
    battery_kw: float,
    solar_kw_i: float,
    load_kw_i: float,
    *,
    grid_export_kw_i: float | None = None,
) -> tuple[str, str, float, str, float]:
    """Real per-period source/destination breakdown for the plan table
    (2026-08-28, direct ask: "the plan table should also say where it
    is coming from -- such as solar, grid, battery... not just
    charging... it should say direction, and then from/to what
    source"). The LP itself has no per-source flow variables to read
    back (BatteryConfig is a single aggregate on a single copper-plate
    bus -- see its own docstring), so this is an honest MERIT-ORDER
    decomposition of the same flow balance the LP already solved, not a
    dual/shadow-price attribution: solar serves load first, any surplus
    charges the battery, anything still short comes from grid import;
    symmetrically on a discharge period, the battery serves load before
    any of it is attributed to export. Matches how a household actually
    reasons about "why is it charging/discharging right now."

    nimbus issue #629 (Mark Purcell): on a discharge period, "Grid" %
    used to be a pure residual (discharge minus whatever served load),
    same bug as _flow_decomposition()'s own pre-#629 battery_to_grid --
    implying a real export/grid-fed-charge that never happened whenever
    a genuine AC-bus loss (or, on a multi-battery fleet, energy this
    function has no visibility into moving to another participant)
    left a residual. `grid_export_kw_i`, when given, caps "Grid" on a
    discharge period at the LP's own real grid_export_kw for THIS
    period -- the same honest-cap technique as the seven-flow fix.
    Optional and defaults to None (the exact pre-#629 uncapped
    behavior) so every existing caller/test not yet passing a real
    figure is completely unaffected; the real production caller
    (this file's own per-period forecast loop) always passes it.
    b_pct can legitimately read below what full residual attribution
    would have shown once capped -- a and b no longer have to sum to
    100% the moment a real, honestly-unattributable residual exists,
    which is the entire point: summing to 100% by construction was
    exactly what made the old uncapped version misleading.

    Returns (direction, source_a_label, source_a_pct, source_b_label,
    source_b_pct). direction is "charge"/"discharge"/"idle".
    """
    _CHARGE_EPS = 1e-3
    if battery_kw <= -_CHARGE_EPS:
        charge_kw = -battery_kw
        solar_surplus = max(0.0, solar_kw_i - load_kw_i)
        from_solar = min(solar_surplus, charge_kw)
        from_grid = charge_kw - from_solar
        return (
            "charge",
            "Solar",
            round(from_solar / charge_kw * 100, 1),
            "Grid",
            round(from_grid / charge_kw * 100, 1),
        )
    if battery_kw >= _CHARGE_EPS:
        discharge_kw = battery_kw
        remaining_load = max(0.0, load_kw_i - solar_kw_i)
        to_load = min(discharge_kw, remaining_load)
        to_grid_residual = discharge_kw - to_load
        to_grid = (
            to_grid_residual
            if grid_export_kw_i is None
            else min(to_grid_residual, max(0.0, grid_export_kw_i))
        )
        return (
            "discharge",
            "Load",
            round(to_load / discharge_kw * 100, 1),
            "Grid",
            round(to_grid / discharge_kw * 100, 1),
        )
    return ("idle", "Load", 0.0, "Grid", 0.0)


def _flow_decomposition(
    solar_kw_i: float,
    load_kw_i: float,
    charge_kw_i: float,
    discharge_kw_i: float,
    *,
    grid_export_kw_i: float,
) -> dict[str, float]:
    """Real per-period seven-flow merit-order decomposition (nimbus issue
    #264, Mark Purcell) -- extends _dispatch_source_breakdown()'s 2-way
    split (against the battery only) to all four real bus terminals
    (Solar/Battery/Grid/Load), so every kW of the period's balance
    belongs to exactly one of the seven physical flows: PV->Load,
    PV->Battery, PV->Grid, Battery->Load, Battery->Grid, Grid->Load,
    Grid->Battery -- plus an eighth bucket, battery_to_losses (nimbus
    issue #629, see below).

    Same merit-order convention as _dispatch_source_breakdown() (solar
    serves load first, then battery charge, then export; battery
    discharge serves load before being attributed to export; grid tops
    up whatever's left) -- this is an honest decomposition of the same
    flow balance the LP already solved, not a dual/shadow-price
    attribution (the LP has no per-source flow variables to read back).

    Deliberately takes charge_kw_i/discharge_kw_i as TWO SEPARATE
    pre-netted arguments, not the issue's own originally-sketched single
    net_battery_kw -- net_battery already collapses simultaneous
    charge+discharge into one signed scalar BEFORE this function would
    ever see it, which would silently defeat the one real diagnostic
    value the issue's own References section claims for this
    decomposition ("Battery->Grid inside a same-period wash trade will
    show a nonzero magnitude, which is a useful diagnostic in itself" --
    #245/#238). With the two pre-net arrays instead (already computed
    separately in main() as corrected_battery_charge_kw /
    corrected_battery_discharge_kw, for battery_kw_after_efficiency),
    a genuine same-period wash-trade period keeps both nonzero here too,
    so it actually shows up as a real, visible flow rather than being
    silently netted away first.

    nimbus issue #629 (Mark Purcell, real 3-battery-fleet report):
    battery_to_grid used to be a pure residual (discharge_kw_i minus
    whatever served load), with nowhere else for it to go -- every
    discharge period showed a nonzero Battery->Grid slice even when
    grid_export_kw was genuinely 0.0 the entire time, and
    dispatch_source_a_pct implied a real export that never happened.
    `grid_export_kw_i` -- the LP's own real, already-published
    grid_export_kw for this period -- now caps how much of that residual
    can honestly be called Battery->Grid; anything left over is a real,
    separately-labeled battery_to_losses bucket instead of a phantom
    export. This is deliberately NOT a claim about WHERE that leftover
    physically went (this project's own ac_bus_losses_kwh figure is one
    real candidate; energy genuinely absorbed by another battery
    participant behind the shared bus, invisible to this 4-terminal
    Solar/Battery/Grid/Load model, is another -- see #629's own
    discussion) -- only an honest "not really export" label, which is
    the entire ask of the issue regardless of the exact physical
    destination.

    nimbus issue #641 (Mark Purcell, live verification of #629's own
    fix): the identical bug, one level up -- pv_to_grid was STILL a pure
    residual (whatever solar wasn't used for load/charge), so a period
    where the LP curtails real solar surplus (grid_export_kw genuinely
    0.0 while PV clearly has more to give than load+battery absorb --
    real captured evidence: 0.33-1.5 kW of untouched PV surplus with
    export at 0.0 across 6 of the first 60 periods on a 3-battery
    fleet's own forecast) still showed a nonzero PV->Grid. Fixed the
    same shape as #629, one step later: battery_to_grid is computed
    FIRST exactly as #629 already does (still against the RAW,
    pre-cap pv_to_grid -- unchanged, since Mark's own verification
    confirmed the battery side already holds correctly), and pv_to_grid
    is THEN capped at whatever of grid_export_kw_i battery_to_grid
    didn't already claim -- so the two together can never exceed the
    real export, regardless of which one merit-order would naively
    credit first. The honest remainder becomes pv_to_curtailment: real
    generation genuinely not stored, not exported, and not consumed --
    a physically real category of its own (curtailment), not a loss in
    the #629 sense (nothing was generated and then wasted in transit;
    it was simply never drawn from the panels' own real headroom).

    Invariants (asserted in tests/test_flow_decomposition.py against
    both synthetic cases and the real regression fixtures under
    tests/regression/fixtures/):
      pv_to_load + pv_to_battery + pv_to_grid + pv_to_curtailment == solar_kw_i
      pv_to_load + battery_to_load + grid_to_load == load_kw_i
      pv_to_battery + grid_to_battery == charge_kw_i
      battery_to_load + battery_to_grid + battery_to_losses == discharge_kw_i
    These hold by construction, always, regardless of input. Two further
    invariants (grid_to_load + grid_to_battery == grid_import_kw,
    pv_to_grid + battery_to_grid == grid_export_kw) are NOT pure
    algebraic identities of this function alone the way the four above
    are -- grid_to_load/grid_to_battery still depend on the real LP's
    own grid_import_kw satisfying the same merit-order assumption this
    function encodes on the charge side (empirical, verified against
    real captured fixtures in the regression suite, not asserted here);
    pv_to_grid + battery_to_grid == grid_export_kw, however, now holds
    BY CONSTRUCTION on the export side too (both are capped against
    grid_export_kw_i, #629 for the battery's own share and #641 for
    PV's), which is the whole point of both issues' own fix.
    """
    pv_to_load = min(solar_kw_i, load_kw_i)
    solar_after_load = solar_kw_i - pv_to_load
    pv_to_battery = min(solar_after_load, charge_kw_i)
    solar_after_battery = solar_after_load - pv_to_battery
    pv_to_grid_residual = solar_after_battery

    load_after_solar = load_kw_i - pv_to_load
    battery_to_load = min(discharge_kw_i, load_after_solar)
    battery_residual = discharge_kw_i - battery_to_load
    # nimbus issue #629: bound the grid-bound share of the battery's own
    # residual output by what the LP itself actually exported this
    # period, net of PV's own already-computed (still-uncapped) share of
    # it -- the remainder is a real, honestly-labeled loss, not a
    # phantom export. Deliberately uses pv_to_grid_residual (not the
    # #641 capped value below) -- Mark's own live verification confirmed
    # this half already holds correctly, unchanged by #641.
    battery_to_grid = min(
        battery_residual, max(0.0, grid_export_kw_i - pv_to_grid_residual)
    )
    battery_to_losses = battery_residual - battery_to_grid

    # nimbus issue #641: PV's own share of the real export is whatever
    # battery_to_grid (just computed) didn't already claim -- together
    # the two can never exceed grid_export_kw_i. The honest remainder is
    # curtailment: real generation genuinely not stored, exported, or
    # consumed this period.
    pv_to_grid = min(pv_to_grid_residual, max(0.0, grid_export_kw_i - battery_to_grid))
    pv_to_curtailment = pv_to_grid_residual - pv_to_grid

    load_after_battery = load_after_solar - battery_to_load
    grid_to_load = load_after_battery
    grid_to_battery = charge_kw_i - pv_to_battery

    return {
        "pv_to_load": pv_to_load,
        "pv_to_battery": pv_to_battery,
        "pv_to_grid": pv_to_grid,
        "battery_to_load": battery_to_load,
        "battery_to_grid": battery_to_grid,
        "grid_to_load": grid_to_load,
        "grid_to_battery": grid_to_battery,
        "battery_to_losses": battery_to_losses,
        "pv_to_curtailment": pv_to_curtailment,
    }


def _compute_flow_economics(
    flows: list[dict[str, float]],
    import_price: np.ndarray,
    export_price: np.ndarray,
    period_hours: np.ndarray,
    round_trip_efficiency: float,
    initial_soc_kwh: float,
) -> list[dict[str, float]]:
    """Per-period shadow prices on each of the seven flows from
    _flow_decomposition(), plus the PV / Battery / Combined / Interaction
    savings model (nimbus issue #264, Mark Purcell).

    Shadow price table (all $/kWh, from the issue):
      PV -> Load      = import_price                    (retail avoided)
      PV -> Battery   = import_price - rt_loss_cost      (deferred credit)
      PV -> Grid      = export_price                     (settlement)
      Battery -> Load = import_price - rt_loss_cost      (retail avoided, less loss already paid)
      Battery -> Grid = export_price - rt_loss_cost - charge_price_at_source  (arbitrage margin)
      Grid -> Load    = -import_price                    (pure cost)
      Grid -> Battery = -import_price                     (cost, deferred against a later discharge)
    where rt_loss_cost = import_price * (1 - round_trip_efficiency).

    charge_price_at_source -- the $/kWh cost basis attributed to energy
    LEAVING the battery on a discharge period -- is deliberately NOT a
    same-period lookup. A battery's SoC persists across periods (it is
    charged in one period and very often discharged many periods later);
    same-period charge+discharge is itself the anomalous wash-trade
    condition #245 targets, not the normal case the pricing table needs
    to be right for. This tracks a real weighted-average cost of goods
    (WACOG) basis for whatever energy currently sits in the battery,
    updated every period: PV-sourced charge blends in at $0/kWh,
    grid-sourced charge blends in at that period's own import_price, and
    every period's discharge draws the existing average down without
    changing it -- exactly like an inventory cost basis, which moves on
    a purchase, never on a sale.

    Tracks PRE-efficiency kWh throughout (the same convention
    _flow_decomposition()'s own charge_kw/discharge_kw already use) --
    round-trip loss is charged exactly once, via rt_loss_cost in the
    price table above; folding efficiency into the cost basis too would
    double-count the same loss twice.

    initial_soc_kwh's own real cost basis is genuinely unknowable (it
    was charged at some real historical price before this forecast
    horizon began) -- seeded at this horizon's own opening import_price,
    the same "replacement cost" convention already used elsewhere in
    this file for salvage/terminal value.

    Combined savings and the interaction term are built from this same
    flow decomposition's own reconstructed load_kw / grid_import_kw /
    grid_export_kw (invariants 2/5/6 on _flow_decomposition()'s own
    docstring) rather than a second, independently-passed copy of the
    real LP arrays -- so PV + Battery + Interaction == Combined holds
    exactly, by construction, every period, rather than only
    approximately whenever the two sources happen to agree.
    """
    n = len(flows)
    results: list[dict[str, float]] = []

    running_energy_kwh = max(float(initial_soc_kwh), 1e-9)
    running_cost_basis = float(import_price[0]) if n else 0.0

    for i in range(n):
        f = flows[i]
        hrs = float(period_hours[i])
        ip = float(import_price[i])
        ep = float(export_price[i])
        loss = ip * (1.0 - round_trip_efficiency)

        charge_kwh_pv = f["pv_to_battery"] * hrs
        charge_kwh_grid = f["grid_to_battery"] * hrs
        charge_kwh = charge_kwh_pv + charge_kwh_grid
        # nimbus issue #629: battery_to_losses is real energy that left
        # the battery's own SoC (discharge_kw_i, by construction, equals
        # battery_to_load + battery_to_grid + battery_to_losses) even
        # though it never reached load or grid -- must count toward the
        # SoC/WACOG drawdown below or running_energy_kwh silently drifts
        # from the real battery state on every period this bucket is
        # nonzero. `.get(..., 0.0)` keeps this function tolerant of an
        # older-shaped flow dict (e.g. a caller/test predating #629).
        discharge_kwh = (
            f["battery_to_load"]
            + f["battery_to_grid"]
            + f.get("battery_to_losses", 0.0)
        ) * hrs

        if charge_kwh > 1e-9:
            charge_price_this_period = (
                charge_kwh_pv * 0.0 + charge_kwh_grid * ip
            ) / charge_kwh
            new_energy = running_energy_kwh + charge_kwh
            running_cost_basis = (
                running_cost_basis * running_energy_kwh
                + charge_price_this_period * charge_kwh
            ) / new_energy
            running_energy_kwh = new_energy

        charge_price_at_source = running_cost_basis

        if discharge_kwh > 1e-9:
            running_energy_kwh = max(0.0, running_energy_kwh - discharge_kwh)

        price_pv_to_load = ip
        price_pv_to_battery = ip - loss
        price_pv_to_grid = ep
        price_battery_to_load = ip - loss
        price_battery_to_grid = ep - loss - charge_price_at_source
        price_grid_to_load = -ip
        price_grid_to_battery = -ip

        pv_savings = (
            f["pv_to_load"] * price_pv_to_load
            + f["pv_to_grid"] * price_pv_to_grid
            + f["pv_to_battery"] * price_pv_to_battery
        ) * hrs
        battery_savings = (
            f["battery_to_load"] * price_battery_to_load
            + f["battery_to_grid"] * price_battery_to_grid
            - f["grid_to_battery"] * loss
        ) * hrs

        load_kw_recon = f["pv_to_load"] + f["battery_to_load"] + f["grid_to_load"]
        grid_import_recon = f["grid_to_load"] + f["grid_to_battery"]
        grid_export_recon = f["pv_to_grid"] + f["battery_to_grid"]
        combined_savings = (
            load_kw_recon * ip - (grid_import_recon * ip - grid_export_recon * ep)
        ) * hrs
        interaction_savings = combined_savings - pv_savings - battery_savings

        results.append(
            {
                "flow_price_pv_to_load": round(price_pv_to_load, 4),
                "flow_price_pv_to_battery": round(price_pv_to_battery, 4),
                "flow_price_pv_to_grid": round(price_pv_to_grid, 4),
                "flow_price_battery_to_load": round(price_battery_to_load, 4),
                "flow_price_battery_to_grid": round(price_battery_to_grid, 4),
                "flow_price_grid_to_load": round(price_grid_to_load, 4),
                "flow_price_grid_to_battery": round(price_grid_to_battery, 4),
                "flow_battery_cost_basis": round(charge_price_at_source, 4),
                "savings_pv": round(pv_savings, 4),
                "savings_battery": round(battery_savings, 4),
                "savings_combined": round(combined_savings, 4),
                "savings_interaction": round(interaction_savings, 4),
            }
        )

    return results


def compute_tariff_attributed_cost(
    adequacy_loads: list,
    grid_to_load_kw: NDArray[np.float64],
    whole_house_load_kw: NDArray[np.float64] | list[float],
    import_price: NDArray[np.float64] | list[float],
    period_hours: NDArray[np.float64] | list[float],
) -> dict[str, float]:
    """Nimbus issue #483 (sub-issue 7 of #476), item 2 -- "the device's
    share of the plan's actual grid flow, pro-rata by the device's power
    in each period." Item 1 (marginal_cost, the LP's own shadow-price-
    based figure) shipped in #483's first pass (v0.94.271) inside
    network.py itself, since it's a genuine LP-native quantity (reads
    the solve's own duals). This one is deliberately NOT computed
    there -- it's a pure post-processing ATTRIBUTION over the already-
    solved flow decomposition (_flow_decomposition(), a solver_writer.py-
    level function with no LP/dual concept at all), not a property the
    LP solve itself produces.

    Same "Grid -> Load = -import_price (pure cost)" pricing convention
    _compute_flow_economics()'s own shadow-price table already
    documents -- this attributes exactly that flow's real dollar cost,
    split pro-rata across every configured adequacy (deferrable) load
    by its own share of the whole house's real per-period load. A
    period with zero whole-house load has nothing to attribute (0.0
    contribution, not a division-by-zero crash).

    Honest scope, stated directly rather than left implicit: this
    covers every ADEQUACY (deferrable) load, matching marginal_cost's
    own existing scope -- sheddable/thermal loads don't get this
    figure in this pass, same "kept consistent with what's already
    shipped, not silently expanded" reasoning #774 itself used for
    kind=thermal's own reset of plan_marginal_cost/plan_profit_horizon
    to None.

    Real-world reconciliation caveat, spelled out per #483's own
    acceptance criterion ("sums to the plan's grid cost within
    rounding"): this holds EXACTLY only when grid_to_load accounts for
    the entire grid_net cost -- i.e. no grid-sourced battery charging,
    no export, and every real watt of whole-house load belongs to a
    configured adequacy load (the clean synthetic-day shape the issue's
    own Acceptance section describes). On a real household with
    uncontrolled background circuits (lighting, fridge, etc. -- not
    configured as a Controllable Load at all) or any grid export/
    charging in the same window, the sum of every device's own
    attributed cost is a real, honest PARTIAL answer (exactly what the
    tracked devices cost), not a claim that it reconciles to the full
    grid_net figure -- the untracked remainder is real cost this
    function has no device to attribute it to.
    """
    result: dict[str, float] = {}
    n = min(
        len(grid_to_load_kw),
        len(whole_house_load_kw),
        len(import_price),
        len(period_hours),
    )
    for al in adequacy_loads:
        if al.subentry_id is None:
            continue
        power = np.asarray(al.power_kw, dtype=np.float64)
        cost = 0.0
        for t in range(min(n, len(power))):
            house_load = float(whole_house_load_kw[t])
            if house_load <= 1e-9:
                continue
            share = float(power[t]) / house_load
            device_grid_to_load_kw = share * float(grid_to_load_kw[t])
            cost += (
                device_grid_to_load_kw * float(import_price[t]) * float(period_hours[t])
            )
        result[al.subentry_id] = cost
    return result


def build_per_battery_forecast(
    plan, grid_times, n_periods: int, battery_capacity_by_name: dict[str, float]
) -> list[dict]:
    """nimbus issue #563 item 5: Plan.batteries[] (network.py's own
    BatteryPlan, #467 stage 1) already carries each real battery
    participant's own charge/discharge/SoC series -- this is exposure,
    not new solver work, the same posture #613's own item 1
    (shadow_price) already established for this file. Same per-period
    {"time": ...} shape as the aggregate "forecast" array
    publish_plan() itself already builds, one entry per participant, so
    a dashboard/quality-report reader can show the home pack and each
    EV/second battery separately instead of only the fleet-summed
    battery_kw/soc_pct every "forecast" row already has.

    `soc_pct` is derived from THIS participant's own real capacity
    (`battery_capacity_by_name[bp.name]`), never the fleet total --
    dividing by the wrong capacity is exactly the real #569 bug already
    fixed once for the aggregate soc_pct field, not something to
    reintroduce here. 0.0 (not a crash or a fabricated 100%) for a
    participant genuinely missing from `battery_capacity_by_name` or
    configured with a non-positive capacity.

    Deliberately NOT the "one flattened sub-device per battery" half of
    #563 item 5's own ask -- that's a real, separate new-entity-
    lifecycle piece, left open.
    """
    return [
        {
            "name": bp.name,
            "forecast": [
                {
                    "time": grid_times[i].isoformat(),
                    "charge_kw": round(float(bp.charge_kw[i]), 3),
                    "discharge_kw": round(float(bp.discharge_kw[i]), 3),
                    "soc_kwh": round(float(bp.soc_kwh[i]), 3),
                    "soc_pct": round(
                        float(bp.soc_kwh[i] / battery_capacity_by_name[bp.name] * 100),
                        2,
                    )
                    if battery_capacity_by_name.get(bp.name, 0.0) > 0
                    else 0.0,
                }
                for i in range(n_periods)
            ],
        }
        for bp in plan.batteries
    ]


def publish_plan(
    *,
    cfg,
    now,
    plan,
    previous_plan,
    solve_started,
    period_hours_arr,
    grid_times,
    n_periods,
    capacity_kwh,
    # nimbus issue #569: the real fleet total across every battery
    # participant (home + any battery_participant subentries) -- see
    # main()'s own comment where this is computed for why this had to
    # become its own parameter rather than overloading capacity_kwh
    # (which stays the home battery's own capacity for existing callers
    # that legitimately still need just that).
    fleet_capacity_kwh,
    # nimbus issue #563 item 5: real per-participant capacity, keyed by
    # BatteryConfig.name -- Plan.batteries[] itself carries kW/SoC but
    # not capacity (network.py stays grid-agnostic about the config
    # that produced a Plan), needed here to derive each participant's
    # own soc_pct rather than only the fleet-summed one every existing
    # "forecast" row already publishes. See main()'s own call site.
    battery_capacity_by_name,
    charge_discharge_efficiency,
    grid,
    import_limit_kw,
    export_limit_kw,
    # nimbus issue #493 (Signals 4/7 of #489, item 1): the plain
    # configured static limit, kept SEPARATE from import_limit_kw/
    # export_limit_kw above (which are now the real per-period envelope-
    # resolved arrays GridConfig itself uses for dispatch) -- compute_
    # cost_band()'s own read-only #630 diagnostic takes a single flat
    # limit for its whole window by design, out of this issue's own
    # scope to change.
    static_import_limit_kw,
    static_export_limit_kw,
    max_charge_kw,
    max_discharge_kw,
    charge_cost,
    discharge_cost_arr,
    salvage_value,
    risk_aversion,
    import_price_risk_aversion,
    export_price_risk_aversion,
    import_price,
    export_price,
    spot_import_source,
    spot_export_source,
    # nimbus issue #631: per-period "primary"/"secondary"/"fallback"
    # label, straight from blend_price_with_secondary_sources()'s own
    # real decision -- see that function's own docstring.
    import_price_source,
    export_price_source,
    export_bonus_price,
    load_kw,
    solar_kw,
    load_lower_kw,
    load_upper_kw,
    initial_soc_kwh,
    match_fraction,
    summed_18_now_kw,
    whole_house_now_kw,
    live_load_kw,
    load_forecast_coverage_hours,
    load_forecast_error,
    load_forecast_source_used,
    load_forecast_warnings,
    failed_load_entities,
    n_clamped,
    solar_delivery,
    p2p_recent_volume_kwh,
    price_spike_active,
) -> None:
    """Extracted from main() (nimbus issue #363 step 2, Mark Purcell's
    own approved staged-extraction plan -- "please go ahead with step 2,
    publish_plan() extraction, per your own outermost-first ordering").
    Pure move, zero behavior change -- guarded by the #363 step-1
    golden-output test (tests/test_main_golden_output_guardrail.py),
    which asserts byte-identical published output for a real solve.

    Takes the already-solved `plan` (network.build_plan()'s own return
    value) plus every real input the LP itself was fed, computes every
    derived/diagnostic value (cost breakdown, cost band, binding
    constraint, per-period flow decomposition, etc.), and publishes
    both sensor.nimbus_household_load_total_forecast and this
    project's own flagship sensor.nimbus_solver_battery_forecast.
    """
    solve_seconds = time.monotonic() - solve_started
    if plan.status == "optimal":
        save_plan_state(plan, period_hours_arr, grid_times[0])
    elif plan.solver_failed:
        # nimbus issue #356 (Mark Purcell): this is genuinely NOT the same
        # thing as a real infeasible model -- HiGHS gave up/hit a limit
        # without ever determining feasibility either way (see network.py's
        # own Plan.raw_status docstring). Named explicitly here so an
        # operator sees "the solver failed, here's HiGHS's own reason"
        # rather than being sent hunting for a modeling/config problem that
        # doesn't exist.
        #
        # nimbus issue #757: and do not publish it. _infeasible_plan()
        # builds a well-formed but ENTIRELY ZERO-FILLED Plan for any
        # non-optimal solve -- no batteries, no loads, every array
        # zeros(n). Publishing that overwrites a perfectly good live plan
        # with a plausible-looking blank one: sensor.nimbus_solver_battery_
        # forecast reads "0 kW everywhere, batteries=[]", which is
        # indistinguishable on a dashboard from a real solve that genuinely
        # decided to do nothing.
        #
        # That is not hypothetical. #757 ("battery participant silently
        # excluded from the solve") sat open through TEN separate
        # investigations that disagreed with each other, because a live
        # trace showed build_plan() returning batteries=['home','Test EV']
        # with status='optimal' on every cycle while 7 of 10 actual
        # publishes carried batteries=[] with status='error'. The
        # "exclusion" was never an exclusion -- it was failed solves
        # overwriting good ones. Whoever looked next saw whichever write
        # landed last.
        #
        # Skipping the publish is the honest outcome, not a silent one:
        # _NimbusSolverPushSensor.available goes False once
        # _STALE_AFTER_SECONDS (5 min) passes with no fresh push, so a
        # persistently failing solver surfaces as "unavailable" -- which is
        # exactly what it is -- rather than as a confident plan of zeros.
        # A transient single failure keeps the last good plan for under
        # five minutes, which is strictly better than replacing it with
        # nothing.
        #
        # Deliberately NOT extended to "infeasible". That one is a real
        # modelling ANSWER (HiGHS proved no feasible dispatch exists for
        # the constraints given), the household needs to see it, and #773's
        # own fallback path depends on it being published. "error" is the
        # only status where the solver never determined anything at all.
        # nimbus issue #773: the elapsed time is logged HERE because
        # this function returns before ever publishing it. #757's own
        # guard above is right to skip the publish, but solve_seconds
        # reaches sensor.nimbus_solver_solve_seconds ~750 lines below
        # this point -- so from v0.94.301 onward that sensor only ever
        # reported SUCCESSFUL solves, and the duration of a failing one
        # became invisible. That is exactly the number #773 needs: on a
        # real install these failures take ~60s (the per-call limit)
        # against a 0.6s healthy cycle, and a guard that hides the
        # symptom it protects against is a bad trade. Logged, not
        # published -- no plan is written, so nothing about the guard
        # itself changes.
        # nimbus issue #1179: name the calibration fallback IN THIS LINE.
        #
        # The 2026-09-20 episode produced 153 failed cycles in 2.7h and 44
        # "no blend weight preserves primary cost ... using minimum weight
        # 1.00e-12" warnings, 37 of which shared a timestamp-SECOND with a
        # failure. That is a correlation by clock, not by cycle, and it is
        # precisely why the blend-collapse hypothesis could be neither
        # confirmed nor dropped: blend warnings accompanied only 44 of the
        # 153, which is equally consistent with "one of several routes to
        # the same failure" and with "the warning is logged on a subset of
        # the cycles that take it".
        #
        # `Plan.calibration_min_weight_fallback` is threaded from
        # LPResult (see both dataclasses' own fields) and is set on
        # SUCCESSFUL solves too, so the next episode answers the question
        # with a base rate rather than with failures alone.
        # nimbus issue #1179, second half: name WHY the fallback fired, not
        # just that it did. The two reasons point in opposite causal
        # directions, and #1229's bool alone cannot separate them:
        #
        #   probe_not_optimal  -- the calibrator's own bracket probes did not
        #                         solve, so the model was already failing
        #                         BEFORE any weight was chosen. The blend
        #                         collapse is a symptom of this failure, not
        #                         its cause, and this line and the blend
        #                         warning are two views of one event.
        #   cost_not_preserved -- both probes solved and no weight in the
        #                         bracket held the primary cost. The only case
        #                         in which #1179's original hypothesis (that
        #                         handing HiGHS 1e-12 is itself what breaks the
        #                         solve) is even available.
        #
        # That distinction is what the 2026-09-20 episode could not answer:
        # blend warnings accompanied only 44 of 153 failures, which is equally
        # consistent with "one of several routes to the same failure" and with
        # "the warning is logged on a subset of the cycles that take it".
        # `probe_not_optimal` would explain the other 109 directly -- they
        # failed at a phase that never reaches calibration at all.
        if plan.calibration_min_weight_fallback:
            calibration_note = "FELL BACK to the minimum weight 1e-12, reason=%s" % (
                plan.calibration_fallback_reason or "unrecorded"
            )
            if plan.calibration_fallback_reason == "probe_not_optimal":
                calibration_note += (
                    " (so the model was ALREADY failing before a weight was "
                    "chosen -- this fallback is a symptom of this failure, not "
                    "its cause)"
                )
            elif plan.calibration_fallback_reason == "cost_not_preserved":
                calibration_note += (
                    " (both probes solved, so the collapsed weight is a real "
                    "calibration verdict and is a candidate CAUSE here)"
                )
        else:
            calibration_note = "found a usable weight (no minimum-weight fallback)"
        _LOGGER.warning(
            "Nimbus: solve did not complete after %.1fs -- HiGHS solver "
            "failure (%s), not a genuinely infeasible model; keeping the "
            "previous published plan rather than overwriting it with an "
            "empty one (nimbus issue #757). This cycle's blend calibration "
            "%s (nimbus issue #1179)",
            solve_seconds,
            plan.raw_status or "unknown reason",
            calibration_note,
        )
        return

    # Real fixed daily charges (Network Access + LV Fee), reported
    # honestly alongside the LP's own total_cost -- NOT fed into the LP
    # itself (a flat, dispatch-independent cost can't change an optimal
    # LP decision, only shift the objective by a constant, so there's
    # nothing for the solver to do with it). Prorated to this horizon's
    # own real span (sum of period_hours_arr, NOT n_periods*a-fixed-width
    # -- the tiered grid has two different period widths) / 24, not just
    # added flat.
    #
    # nimbus issue #348 (Mark Purcell, fixed 2026-09-04): a real wizard
    # field now (number.nimbus_solver_fixed_daily_charge), not a module
    # constant applied to every install regardless of their own real
    # retailer's actual daily supply charge. 1.95 as the literal default
    # matches const.py's own DEFAULT_SOLVER_FIXED_DAILY_CHARGE -- byte-
    # identical behaviour for every existing install until this field is
    # explicitly changed.
    horizon_days = sum(period_hours_arr) / 24.0
    fixed_daily_charge = _cfg_num(cfg, "solver_fixed_daily_charge", 1.95)
    total_cost_with_fixed_costs = (
        plan.total_cost or 0.0
    ) + horizon_days * fixed_daily_charge

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
    # nimbus issue #569: fleet_capacity_kwh (sum across every battery
    # participant), not capacity_kwh (the home battery alone) -- a 60 kWh
    # car and a 40 kWh pack do not share a cycle-life spec, and this is
    # the whole-fleet throughput being related to the whole fleet's own
    # capacity, same reasoning as soc_pct below.
    equivalent_full_cycles = (
        total_throughput_kwh / (2.0 * fleet_capacity_kwh)
        if fleet_capacity_kwh > 0
        else 0.0
    )

    # Pre-collapse per-direction arrays for battery_kw_after_efficiency
    # below (2026-08-27, nimbus issue #229) -- kept separate from net_battery
    # rather than reconstructed from its sign, because a period can have LP
    # degeneracy noise on BOTH plan.battery_charge_kw[i] and
    # plan.battery_discharge_kw[i] simultaneously; collapsing to net first
    # and branching on its sign silently drops whichever direction's real
    # efficiency loss the sign discarded. Applying each direction's own
    # efficiency BEFORE summing (same approach Mark Purcell verified in
    # PR #231 against the sibling nimbus_solver_app writer -- 0.013%
    # residual vs 3.65% reconstructing from the post-collapse net value)
    # is what actually closes tightly against Δ(soc_pct·capacity).
    corrected_battery_discharge_kw = plan.battery_discharge_kw

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
    # nimbus issue #923: `plan.batteries[0]` is the battery the LP gate was
    # applied to; the fleet aggregate is not. See the function's docstring.
    (
        net_battery,
        corrected_battery_charge_kw,
        corrected_grid_import,
        _violation_mask,
    ) = resolve_fixed_export_charge_clamp(
        grid.fixed_export_kw,
        gated_charge_kw=(
            plan.batteries[0].charge_kw if plan.batteries else plan.battery_charge_kw
        ),
        aggregate_charge_kw=plan.battery_charge_kw,
        discharge_kw=plan.battery_discharge_kw,
        grid_import_kw=plan.grid_import_kw,
    )
    if _violation_mask.any():
        _LOGGER.warning(
            "Nimbus Solver: solver returned %d period(s) with "
            "battery_charge_kw>0 on the home battery during a committed "
            "fixed_export_kw period -- mathematically should be impossible, "
            "applying defensive clamp before push. (Participant batteries "
            "are not gated here and are not counted, see nimbus issue #923.)",
            int(np.sum(_violation_mask)),
        )

    # Physical, post-efficiency energy rate at the battery terminals
    # (2026-08-27, nimbus issue #229, Mark Purcell) -- battery_kw above is
    # the LP's own pre-efficiency decision variable, which can't be
    # reconciled against soc_pct without knowing the applied efficiency
    # curve. Built from the (possibly defensively-corrected, see above)
    # per-direction arrays rather than net_battery's sign -- see that
    # variable's own comment for why.
    battery_kw_after_efficiency = (
        corrected_battery_discharge_kw / charge_discharge_efficiency
        - corrected_battery_charge_kw * charge_discharge_efficiency
    )

    # Real per-period source/destination breakdown (2026-08-28) -- see
    # _dispatch_source_breakdown()'s own module-level docstring for the
    # full rationale.
    dispatch_breakdown = [
        _dispatch_source_breakdown(
            net_battery[i],
            solar_kw[i],
            load_kw[i],
            grid_export_kw_i=float(plan.grid_export_kw[i]),
        )
        for i in range(n_periods)
    ]

    # Real per-period seven-flow decomposition + shadow prices + PV/
    # Battery/Combined savings model (2026-08-28, nimbus issue #264, Mark
    # Purcell) -- see _flow_decomposition()'s and _compute_flow_economics()'s
    # own module-level docstrings for the full rationale, including why
    # this deliberately extends the issue's own sketch (separate pre-net
    # charge/discharge arrays instead of a single net_battery_kw, and a
    # real cross-period WACOG cost basis instead of a same-period lookup).
    # Byte-identical, additive to the existing dispatch_source_a/b fields
    # above -- nothing already published changes shape or value.
    flow_decomp = [
        _flow_decomposition(
            solar_kw[i],
            load_kw[i],
            float(corrected_battery_charge_kw[i]),
            float(corrected_battery_discharge_kw[i]),
            grid_export_kw_i=float(plan.grid_export_kw[i]),
        )
        for i in range(n_periods)
    ]
    flow_econ = _compute_flow_economics(
        flow_decomp,
        import_price,
        export_price,
        period_hours_arr,
        round_trip_efficiency=charge_discharge_efficiency**2,
        initial_soc_kwh=initial_soc_kwh,
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
            # See battery_kw_after_efficiency's own definition above (nimbus
            # issue #229) for why this is built from the per-direction
            # arrays rather than net_battery[i] directly.
            "battery_kw_after_efficiency": round(
                float(battery_kw_after_efficiency[i]), 3
            ),
            # nimbus issue #569: fleet_capacity_kwh, not capacity_kwh --
            # plan.battery_soc_kwh[i] is the summed aggregate across every
            # battery participant (per #467 stage 1), so the denominator
            # has to be the fleet total too, or this reads well over 100%
            # the moment a second battery participant is configured.
            "soc_pct": round(
                float(plan.battery_soc_kwh[i] / fleet_capacity_kwh * 100), 2
            )
            if fleet_capacity_kwh > 0
            else 0.0,
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
            # nimbus issue #493 (Signals 4/7 of #489, item 1): the real
            # per-period bound the plan was actually solved against this
            # period -- the static configured limit at every period on
            # any install with no envelope entity configured (byte-
            # identical to before this issue), or the live/forecast-
            # resolved DNSP envelope value otherwise. See
            # resolve_envelope_limit_kw()'s own docstring.
            "envelope_import_limit_kw": round(float(import_limit_kw[i]), 3),
            "envelope_export_limit_kw": round(float(export_limit_kw[i]), 3),
            # nimbus issue #613 (Mark Purcell, item 1 of 3 -- explicitly
            # scoped to just this exposure piece, NOT the earliness-
            # timing behavior change items 2/3 describe, which need
            # their own separate design/verification pass): the whole-
            # system marginal cost of a kWh in THIS period, per the LP's
            # own power_balance_t{i} dual -- "already extracts duals...
            # this is exposure, not new solver work." Same 0.0 default-
            # on-missing convention energy_shadow_price_now (period 0's
            # own copy of this same number, kept for backward
            # compatibility) already uses.
            #
            # nimbus issue #662 (Mark Purcell): power_balance_t{i} is
            # built with every term at a plain +-1.0 coefficient (kW),
            # while the objective's own price terms are scaled by
            # period_hours_arr[i] (a $/kWh price x hours -> a real $
            # cost) -- so the row's own raw dual comes out in "$ per kW
            # of RHS," already carrying an implicit x hours[i] relative
            # to the true $/kWh marginal price. Dividing by period_
            # hours_arr[i] recovers it -- the EXACT SAME correction
            # forced_import_cost already applies to reduced_costs a few
            # hundred lines below (`reduced_costs[...] / hours[t]`),
            # just never extended to this sibling field. Confirmed live
            # against real numbers: 0.0186 (raw, pre-fix) / (5/60) =
            # 0.223, against a real contemporaneous import price of
            # 0.2134 -- a genuine marginal-price reading, not the
            # ~11.5x-too-small raw value.
            "shadow_price": round(
                plan.duals.get(f"power_balance_t{i}", 0.0) / period_hours_arr[i], 4
            ),
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
            # nimbus issue #631 (Mark Purcell, live finding: a single
            # 71.3 kWh charge block committed 20 hours ahead purely on a
            # secondary source's own price, with nothing published that
            # distinguished "known from the retailer's own near-term
            # forecast" from "extrapolated from a weekly tariff table" --
            # import_price and import_price_raw were byte-identical in
            # every row regardless of which source actually won that
            # period). "primary"/"secondary"/"fallback", one label per
            # period, straight from blend_price_with_secondary_sources()'s
            # own real per-period decision -- see that function's own
            # docstring. "primary" on every row of a single-source
            # install (the overwhelming majority today).
            "import_price_source": import_price_source[i],
            "export_price": round(export_price[i], 4),
            # Same true pre-blend pass-through as import_price_raw above,
            # for the export side -- new field (2026-08-27, nimbus repo
            # issue #216, Mark Purcell's refined ask #1: "Publish
            # export_price_raw (same shape as the existing
            # import_price_raw attribute)").
            "export_price_raw": round(spot_export_source[i], 4),
            # Same #631 per-period source label as import_price_source
            # above, for the export side.
            "export_price_source": export_price_source[i],
            "bonus_price": round(export_bonus_price[i], 4),
            "load_kw": round(load_kw[i], 3),
            "solar_kw": round(solar_kw[i], 3),
            "dispatch_direction": dispatch_breakdown[i][0],
            "dispatch_source_a_label": dispatch_breakdown[i][1],
            "dispatch_source_a_pct": dispatch_breakdown[i][2],
            "dispatch_source_b_label": dispatch_breakdown[i][3],
            "dispatch_source_b_pct": dispatch_breakdown[i][4],
            # Seven-flow decomposition + shadow prices + savings (nimbus
            # issue #264) -- see flow_decomp/flow_econ's own construction
            # above for the full rationale.
            "flow_pv_to_load_kw": round(flow_decomp[i]["pv_to_load"], 3),
            "flow_pv_to_battery_kw": round(flow_decomp[i]["pv_to_battery"], 3),
            "flow_pv_to_grid_kw": round(flow_decomp[i]["pv_to_grid"], 3),
            "flow_battery_to_load_kw": round(flow_decomp[i]["battery_to_load"], 3),
            "flow_battery_to_grid_kw": round(flow_decomp[i]["battery_to_grid"], 3),
            "flow_grid_to_load_kw": round(flow_decomp[i]["grid_to_load"], 3),
            "flow_grid_to_battery_kw": round(flow_decomp[i]["grid_to_battery"], 3),
            # nimbus issue #629 (Mark Purcell): the honest remainder of
            # the battery's own residual discharge once flow_battery_to_
            # grid_kw is capped at what the LP actually exported this
            # period -- never a phantom export again. See
            # _flow_decomposition()'s own docstring for what this
            # genuinely represents (and doesn't claim to represent).
            "flow_battery_to_losses_kw": round(flow_decomp[i]["battery_to_losses"], 3),
            # nimbus issue #641 (Mark Purcell, live verification of
            # #629): the identical bug one level up -- real PV surplus
            # the plan neither stores, exports, nor consumes this
            # period (the LP is curtailing, or the solar forecast
            # exceeds what the plan absorbs). See _flow_decomposition()'s
            # own docstring for the full reasoning.
            "flow_pv_to_curtailment_kw": round(flow_decomp[i]["pv_to_curtailment"], 3),
            **flow_econ[i],
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
        soc_penalty_cost=plan.soc_penalty_cost,
        grid_import_excess_penalty_cost=plan.grid_import_excess_penalty_cost,
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
        # nimbus issue #493: this read-only #630 diagnostic takes a
        # single flat limit for its whole window by design -- see
        # main()'s own comment where static_import_limit_kw/
        # static_export_limit_kw are captured, kept deliberately
        # separate from the real per-period envelope-resolved arrays
        # GridConfig itself now uses for the actual dispatch decision.
        import_limit_kw=static_import_limit_kw,
        export_limit_kw=static_export_limit_kw,
    )
    # nimbus issue #630 (Mark Purcell: "a band 75 times wider than the
    # day's bill tells a household nothing" -- the full 96h band is real
    # (its own width is earned from the load forecast's own confidence
    # interval widening the further out a period sits), but a household
    # reading it next to "the next 24 hours cost $4.67" has no way to
    # tell that the $349 width is mostly coming from periods 2-4 days
    # out. Same compute_cost_band(), same inputs, just sliced to
    # whichever periods fall inside the first 24 real hours -- an
    # honest, cheap re-use of the exact same re-costing machinery, not a
    # new band formula. final_soc_kwh is the plan's own SoC AT the 24h
    # mark (not the 96h terminal SoC) so the salvage-value term prices
    # what the battery is actually worth at THIS band's own horizon end.
    n_24h = periods_within_hours(period_hours_arr, 24.0)
    cost_band_24h = compute_cost_band(
        period_hours=period_hours_arr[:n_24h],
        load_lower_kw=np.array(load_lower_kw)[:n_24h],
        load_upper_kw=np.array(load_upper_kw)[:n_24h],
        solar_kw=np.array(solar_kw)[:n_24h],
        import_price=np.array(import_price)[:n_24h],
        export_price=np.array(export_price)[:n_24h],
        charge_committed_kw=plan.battery_charge_kw[:n_24h],
        discharge_committed_kw=plan.battery_discharge_kw[:n_24h],
        charge_cost=charge_cost,
        discharge_cost_arr=discharge_cost_arr[:n_24h],
        final_soc_kwh=float(plan.battery_soc_kwh[n_24h - 1]),
        salvage_value=salvage_value,
        import_limit_kw=static_import_limit_kw,
        export_limit_kw=static_export_limit_kw,
    )
    # nimbus issue #630's second ask ("say on the sensor whether any
    # risk-aversion term is active"): a plain, honest boolean -- true
    # only when at least one of the three configured weights is genuinely
    # nonzero, so a reader doesn't have to cross-reference three separate
    # published numbers to answer "is anything actually being hedged
    # against right now."
    risk_aversion_active = bool(
        risk_aversion > 0.0
        or import_price_risk_aversion > 0.0
        or export_price_risk_aversion > 0.0
    )

    # Binding-constraint diagnostics (2026-08-18, Mark Purcell's audit
    # item #3; relabelled 2026-08-24, see compute_binding_constraint_
    # label()'s own docstring near resolve_max_discharge_kw for the
    # full "pinned at zero vs pinned at the real ceiling" story).
    # nimbus issue #493: export_limit_kw/import_limit_kw are now the real
    # per-period envelope-resolved arrays -- period 0's own value is the
    # correct "what's binding RIGHT NOW" bound to compare against
    # (matches this function's own existing period_hours_arr[0] usage
    # right below).
    # nimbus issue #921: period 0's own P2P commitment, when there is
    # one, so the label can name the pin instead of reporting its own
    # bound table's blind spot as an unexpected solver state.
    _fixed_export_now = (
        float(grid.fixed_export_kw[0])
        if grid.fixed_export_kw is not None and len(grid.fixed_export_kw) > 0
        else None
    )
    binding_now, binding_now_value_per_kwh = compute_binding_constraint_label(
        plan,
        export_limit_kw[0],
        import_limit_kw[0],
        max_charge_kw,
        max_discharge_kw,
        period_hours_arr[0],
        _fixed_export_now,
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

    _diag_757_battery_forecast = build_per_battery_forecast(
        plan, grid_times, n_periods, battery_capacity_by_name
    )
    _LOGGER.debug(
        "Nimbus #757 diag: about to ha_post_state(%s) with batteries=%s (plan id=%x)",
        ENTITY_ID,
        [b["name"] for b in _diag_757_battery_forecast],
        id(plan),
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
            # nimbus issue #563 item 5 -- see build_per_battery_
            # forecast()'s own docstring for the full reasoning.
            # nimbus issue #757 diag: reusing the exact same list computed
            # and logged just above, instead of a second, separate call --
            # guarantees the diagnostic log and the real published value
            # are provably the same object, not two independent
            # computations that could theoretically diverge.
            "batteries": _diag_757_battery_forecast,
            "status": plan.status,
            # nimbus issue #756-golden-CI-flake: rounded to match every
            # sibling KPI's own precision (total_cost_with_fixed_costs,
            # cost_breakdown, cost_band all round to 4dp) -- HiGHS's LP
            # solve is not bit-for-bit deterministic run to run (observed
            # directly: two CI runs of the identical frozen-time fixture
            # differed at ~1e-13, e.g. 26.987417226270225 vs
            # 26.98741722627023), which previously leaked straight through
            # this one unrounded field into the golden-output guardrail
            # test and any real dashboard/history consumer. Internal
            # residual math (compute_cost_breakdown()'s own
            # terminal_value_credit) still uses the raw, unrounded
            # plan.total_cost above -- only the published value changes.
            "total_cost": round(plan.total_cost, 4)
            if plan.total_cost is not None
            else None,
            "total_cost_with_fixed_costs": round(total_cost_with_fixed_costs, 4),
            "cost_breakdown": cost_breakdown,
            "cost_band": cost_band,
            # nimbus issue #630: the same band, real-costed against only
            # the periods inside the next 24 real hours -- see this
            # sensor's own construction above for why the 96h band alone
            # is uninformative next to a household's own "what will
            # today cost" question. None whenever the 96h band's own
            # re-costing failed (same honest-diagnostic contract as
            # cost_band itself).
            "cost_band_24h": cost_band_24h,
            "p2p_match_fraction": round(match_fraction, 4),
            "risk_aversion": risk_aversion,
            "import_price_risk_aversion": import_price_risk_aversion,
            "export_price_risk_aversion": export_price_risk_aversion,
            # nimbus issue #630's second ask: whether any of the three
            # risk-aversion weights immediately above is actually
            # nonzero right now -- so a reader can tell "these numbers
            # are shown but inactive" from "these numbers are shaping
            # the plan" without doing that comparison themselves.
            "risk_aversion_active": risk_aversion_active,
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
            # Nimbus issue #237 (Mark Purcell, 2026-08-27): "confirm and
            # expose the price-blend algorithm". Originally an unweighted
            # np.mean() across every source with real coverage at a
            # period, primary included -- confirmed live against Mark's
            # #236 repro (a genuinely-real Amber Express value averaged
            # 50/50 against a genuinely-real but far larger QLD1 PD7DAY
            # wholesale forecast, inverting the import/export price
            # relationship and causing an unphysical simultaneous-
            # import-and-export LP plan). Fixed same-day in #239
            # (primary-preferring): the primary now wins UNBLENDED
            # whenever it has real coverage, full stop -- a secondary
            # only ever fills a period where the primary itself lacks
            # real coverage yet (its one legitimate job, extending the
            # horizon past Amber's own ~24h reach). See
            # blend_price_with_secondary_sources()'s own docstring for
            # the full market-structure argument (import/export both
            # derive from the SAME underlying AEMO spot price for a
            # given region+interval -- they're not independent
            # estimators that can legitimately disagree while both are
            # live).
            "price_blend_algorithm": "primary_preferring_fallback_to_secondary_mean",
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
            # nimbus issue #567: real-time visibility for the dashboard's
            # own "$$$" spike indicator -- True whenever a spike is
            # DETECTED (price threshold or alert entity), independent of
            # whether the household has armed the discharge override.
            "price_spike_active": price_spike_active,
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
            # nimbus issue #429 (Mark Purcell): the two fields above are
            # DELIBERATELY forecast-vs-forecast (sum of 18 circuit models
            # vs the whole-house meter's own separate forecast model) --
            # genuinely useful for catching a missing/misconfigured
            # circuit, but neither is a live meter reading despite what
            # "cross_check" suggests (confirmed live: Mark's own report
            # read load_whole_house_cross_check_now_kw as "the real
            # whole-house meter", a real, understandable misreading given
            # the name). This is the genuine live reading, so a real
            # forecast-vs-reality check is finally possible; additive
            # only, doesn't change either existing field's own value.
            "load_whole_house_live_now_kw": round(live_load_kw, 3)
            if live_load_kw is not None
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
            # nimbus issue #652 (Mark Purcell): sensor.nimbus_solver_
            # solve_seconds published duration alone, with no way to
            # tell "solve got slower because the problem got bigger" (a
            # real, legitimate reconfiguration -- Mark's own real case
            # was a 1-battery to 3-battery fleet change) from "solve got
            # slower because something regressed" without pulling raw
            # history and cross-referencing config-reload timestamps by
            # hand. Surfaced as extra_state_attributes on that flattened
            # sensor (see sensor_flattened.py's own attrs_source_key
            # mechanism) so a future jump can be attributed on sight.
            # Straight from the plan build_plan() itself just solved
            # against, so this can never drift from what actually ran.
            # nimbus issue #773 (2026-09-14): n_controllable_loads used to
            # sum only sheddable + adequacy loads, silently omitting
            # `thermal_loads` -- a first-class controllable-load kind
            # since #774/#800, and the kind the one real thermal load in
            # existence actually uses.
            #
            # Found live on devhub, not reasoned about: six controllable
            # loads were demonstrably in the plan (their own status
            # sensors reading "scheduled 09:00-15:30", "running", "done")
            # while this field reported 0. That is not a cosmetic
            # undercount -- #773's own triage reasoned directly from this
            # number ("whether the calibrated/secondary-cost lex phase has
            # an edge case that a large controllable-load + multi-battery-
            # participant combination can push into infeasibility"), so an
            # under-reporting field was actively misleading the
            # investigation into the problem shape.
            #
            # Broken out per kind rather than only corrected in total:
            # "which KIND of load grew" is the question a solve-time or
            # infeasibility regression actually needs answered, and a
            # single total cannot answer it. n_controllable_loads stays as
            # the honest sum so nothing consuming it breaks.
            "solve_diagnostics": {
                "n_batteries": len(plan.batteries),
                "n_periods": n_periods,
                "n_controllable_loads": len(plan.sheddable_loads)
                + len(plan.adequacy_loads)
                + len(plan.thermal_loads),
                "n_sheddable_loads": len(plan.sheddable_loads),
                "n_adequacy_loads": len(plan.adequacy_loads),
                "n_thermal_loads": len(plan.thermal_loads),
                # nimbus issue #485 acceptance criterion 3: "Diagnostics
                # show household_mode alongside the plan" -- so a plan can
                # be read knowing which mode produced it. Resolved live
                # from select.nimbus_household_mode via the solver-config
                # bridge; None on an install whose select entity has not
                # come up yet, which is honest rather than defaulting to
                # "home" and implying a mode was actually in force.
                "household_mode": cfg.get("household_mode"),
                # nimbus issue #1013. The dial derates capacity now, so
                # the number the solver actually planned against is no
                # longer the number on the dashboard -- and a household
                # that cannot see the difference cannot tell the fix
                # landed. Both are published, not just the result: a lone
                # "119.76" is indistinguishable from someone having
                # retyped the nameplate.
                "battery_soh_percent": _cfg_num(
                    cfg, "solver_battery_soh_percent", 100.0
                ),
                "battery_nameplate_capacity_kwh": round(
                    _cfg_num(cfg, "solver_battery_capacity_kwh", 0.0), 3
                ),
                "battery_effective_capacity_kwh": round(
                    resolve_effective_capacity_kwh(cfg), 3
                ),
            },
            "generated_at": now.isoformat(),
            "binding_constraint_now": binding_now,
            "binding_constraint_shadow_price": binding_now_value_per_kwh,
            # nimbus issue #662: same period_hours_arr[0] correction as
            # the per-period "shadow_price" field above -- see that
            # field's own comment for the full mechanism/verification.
            "energy_shadow_price_now": round(
                plan.duals.get("power_balance_t0", 0.0) / period_hours_arr[0], 4
            ),
            "p2p_volume_cap_shadow_price": p2p_volume_cap_shadow_price,
            # nimbus issue #493 (Signals 4/7 of #489, item 1): period 0's
            # own copy of the per-period envelope_import_limit_kw/
            # envelope_export_limit_kw fields above -- same "period 0
            # copy for backward-compatible one-glance reading" convention
            # energy_shadow_price_now already establishes.
            "envelope_import_limit_kw": round(float(import_limit_kw[0]), 3),
            "envelope_export_limit_kw": round(float(export_limit_kw[0]), 3),
            **_risk_aversion_effect_now(plan, solar_kw, import_price, export_price),
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
    # Issue #389 (Mark Purcell, live install, v0.94.125): _infeasible_plan()
    # returns total_cost=None, and this status line crashed every cycle for
    # 41 minutes formatting it with .2f (TypeError: unsupported format
    # string passed to NoneType.__format__) -- the plan itself had already
    # been correctly pushed with status="infeasible" by this point, so the
    # ONLY thing failing was this trailing log line, but it took down the
    # rest of main() (quality report / counterfactual / efficiency backtest
    # sensor updates) with it every single cycle. total_cost_with_fixed_costs
    # is already None-safe (built via `plan.total_cost or 0.0` above), it's
    # plan.total_cost itself on this line that wasn't guarded.
    total_cost_str = f"{plan.total_cost:.2f}" if plan.total_cost is not None else "n/a"
    print(
        f"[{now.isoformat()}] pushed {ENTITY_ID}: status={plan.status} "
        f"n_periods={n_periods} horizon={horizon_days * 24:.1f}h "
        f"load_forecast_coverage={coverage_str} solve_time={solve_seconds:.2f}s "
        f"total_cost={total_cost_str} total_cost_with_fixed={total_cost_with_fixed_costs:.2f} "
        f"p2p_match_fraction={match_fraction:.3f} net_battery_now={net_battery[0]:.2f}kW "
        f"summed_18_loads_now={summed_18_now_kw:.2f}kW whole_house_cross_check={cross_check_str} "
        f"previous_plan_found={previous_plan is not None} "
        f"binding_now={binding_now!r} energy_shadow_price_now={plan.duals.get('power_balance_t0', 0.0) / period_hours_arr[0]:.4f} "
        f"p2p_volume_cap_shadow_price={p2p_volume_cap_shadow_price}"
    )


def _period_index_for_instant(
    grid_times: list[datetime], target: datetime, *, is_deadline: bool
) -> int:
    """The real index-search core of _resolve_hour_to_period_index() --
    factored out (nimbus issue #612) so a caller that already has a
    concrete instant in hand (a specific calendar day's own earliest/
    deadline moment, not "the next occurrence from now") can reuse the
    exact same search/clamp semantics without going through that
    function's own "roll to the next occurrence from now" step first.
    See _resolve_hour_to_period_index's own docstring for what
    is_deadline=True/False each mean and why -- unchanged here."""
    n = len(grid_times)
    if is_deadline:
        idx = 0
        for i, t in enumerate(grid_times):
            if t <= target:
                idx = i
            else:
                break
        return idx
    for i, t in enumerate(grid_times):
        if t >= target:
            return i
    return n - 1


def _resolve_hour_to_period_index(
    grid_times: list[datetime], now: datetime, hour: float, *, is_deadline: bool
) -> int:
    """nimbus issue #486: a controllable-load wizard field is a plain
    24hr-decimal "hour of day" (e.g. 6.0 = 6am) -- AdequacyLoadConfig
    needs a real PERIOD INDEX into this cycle's own tiered grid instead.
    Resolves "the next real occurrence of this hour from `now`" against
    grid_times (build_tiered_grid()'s own real, boundary-snapped period
    start times) -- e.g. asked for 6.0 at 22:00 today resolves to 6am
    TOMORROW, not a nonsensical negative offset into the past.

    is_deadline=True (CONF_DEFERRABLE_DEADLINE_HOUR): returns the LAST
    period index whose own start time is still <= the target instant --
    the period containing the actual deadline moment, matching
    AdequacyLoadConfig's own "deadline_period is inclusive, cumulative
    energy through this period must reach target_kwh" contract.
    is_deadline=False (CONF_DEFERRABLE_EARLIEST_HOUR): returns the FIRST
    period index whose own start time is >= the target instant -- the
    first period this load is allowed to draw any power at all.

    Clamped to [0, len(grid_times)-1] -- a target more than 96h out (the
    grid's own real horizon) still resolves to a real, usable index
    rather than an out-of-range one build_plan() would reject.
    """
    target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        hours=hour
    )
    if target < now:
        target += timedelta(days=1)
    return _period_index_for_instant(grid_times, target, is_deadline=is_deadline)


def _earliest_period_for_same_day_window(
    *,
    now: datetime,
    earliest_hour: float | None,
    deadline_hour: float | None,
    earliest_period: int,
    deadline_period: int,
) -> int:
    """`earliest_period`, corrected for a same-day window that is already
    in progress (nimbus issue #582, Mark Purcell).

    `_resolve_hour_to_period_index()` resolves "the next occurrence from
    now" for each hour INDEPENDENTLY. Once `now` is past today's
    `earliest_hour`, earliest rolls forward to TOMORROW even though
    today's window is still open -- correct for a genuine overnight
    window (earliest=22, deadline=6, `now` between midnight and 6am),
    wrong for a same-day window once the day has started (earliest=6,
    deadline=16, `now`=06:01 -- Mark's real repro on the first live
    morning of #534). The load was then dropped for its ENTIRE active
    window, the opposite of intended.

    **Extracted 2026-09-18 from four identical copies** (nimbus #485).
    Two sat in `build_controllable_loads()` and two in
    `apply_commanded_state_guard()`, and the 09-09 worklog already
    recorded the risk as realised once:

        While rebasing onto #582, found and fixed a real inconsistency:
        apply_commanded_state_guard()'s own duplicated period-index
        resolution didn't inherit #582's same-day fix ... flagged the
        drift risk between the two call sites as a candidate for a
        future shared-helper refactor.

    That fix was itself applied by duplicating, and the copies grew to
    four. **Checked before extracting rather than assumed: all four were
    byte-identical in logic**, differing only in `ruff format` line
    wrapping at different indentation depths. So this consolidation is
    prophylactic -- it fixes no live divergence, it removes the room for
    the next one, which has already happened once and was caught by a
    rebase rather than by any test.

    It also makes the question #485 asks -- whether deadline/earliest
    hours should be moded per household mode -- a much smaller one:
    moding multiplies the distinct hour pairs flowing through this
    logic, and "is the one helper right" is answerable in a way "are the
    five copies still in agreement" is not.

    Returns `earliest_period` unchanged whenever either hour is unset, or
    the periods are already correctly ordered, or the window is a genuine
    overnight one.

    **A fifth site shares the predicate and is deliberately NOT folded in
    here.** `_build_daily_adequacy_windows()` evaluates the same
    `earliest_today <= now <= deadline_today` test, but inside a
    per-day loop and as one arm of a conditional that also handles
    `now > deadline_today` (skip today entirely), `today_done`, and an
    already-met target. It is a superset rather than a copy: its
    `earliest_period` comes from `_period_index_for_instant()` on a
    day-offset instant, not from a pre-resolved value, so calling this
    would mean reshaping it rather than substituting it. Noted here
    because "four copies became one" is only true of the four that were
    genuinely identical, and someone grepping the predicate will find a
    fifth.
    """
    if earliest_hour is None or deadline_hour is None:
        return earliest_period
    if deadline_period >= earliest_period:
        return earliest_period
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    earliest_today = midnight + timedelta(hours=float(earliest_hour))
    deadline_today = midnight + timedelta(hours=float(deadline_hour))
    if earliest_today <= now <= deadline_today:
        # The window opened earlier today and is still active, so the
        # load may draw RIGHT NOW -- period 0, not tomorrow.
        return 0
    return earliest_period


# nimbus issue #535: log the W->kW scaling hint once per power_sensor
# entity_id, not every solve tick -- same #313/#314 discipline as every
# other log-once dedup this session (_DONE_CONDITION_WARNED,
# _QUALITY_REPORT_UNRELIABLE_WARNED). Module-level, lives for the process.
_LOAD_POWER_SENSOR_UNIT_HINT_LOGGED: set[str] = set()


def _sample_load_run_state(
    hub_entry_id: str,
    subentry_id: str,
    power_sensor: str,
    now: datetime,
    day_key: str,
    import_price_now: float | None = None,
):
    """nimbus issue #479: reads one Controllable Load's real power sensor
    and folds a single solve-tick sample into its persisted LoadRunState
    (custom_components/nimbus_load/load_run_state.py). `import_price_now`
    (nimbus issue #591) is this cycle's own live blended import price,
    passed straight through to apply_power_sample() so it can accumulate
    the load's real actual-cost-today alongside delivered_today_kwh --
    optional/None is a genuine no-op, not an error. Best-effort and
    silent on any failure (sensor unavailable, Store I/O error, HA not
    fully started) -- this bookkeeping isn't consumed by build_plan() at
    all yet (see #479's own scope note), so it must never be able to take
    the actual solve cycle down. Native mode only, same reasoning as this
    function's own caller.

    nimbus issue #626: returns the freshly-persisted LoadRunState (or
    None on any failure/no-op path above) so build_controllable_loads()
    can read this cycle's own just-updated delivered_today_kwh without a
    second store round-trip -- this function already has the freshest
    possible sample for this solve tick, taken moments before its own
    caller resolves target_kwh.
    """
    if _NATIVE_HASS is None:
        # Not reachable from build_controllable_loads() (guarded at its
        # own entry), but this function has no other caller today either
        # -- a defensive, cheap-to-keep guard rather than an assumption.
        return None
    try:
        from homeassistant.helpers.storage import Store as _Store

        state_obj = _NATIVE_HASS.states.get(power_sensor)
        if state_obj is None or state_obj.state in (None, "unknown", "unavailable"):
            return None
        # nimbus issue #535 (Mark Purcell, real household finding): this
        # used to treat state_obj.state as already being kW, with no
        # check against what the sensor itself declares -- the same
        # real class of bug _kw_scale_factor() (this file, near
        # compute_daily_quality_report()) was already found and fixed
        # for the solar/load/battery quality-report sensors. A real
        # power/CT-clamp sensor reporting Watts (Mark's own case: a
        # 4.6W standby reading on a heat-pump HWS) was silently read as
        # 4.6 kW -- currently_on permanently true, delivered_today_kwh
        # ~1000x too large. Same fix, read directly off the already-
        # fetched state_obj's own attributes rather than a second
        # ha_get() round-trip (native mode only here, unlike
        # _kw_scale_factor()'s own REST-shaped caller).
        unit = state_obj.attributes.get("unit_of_measurement")
        scale = 0.001 if unit == "W" else 1.0
        if scale != 1.0 and power_sensor not in _LOAD_POWER_SENSOR_UNIT_HINT_LOGGED:
            _LOAD_POWER_SENSOR_UNIT_HINT_LOGGED.add(power_sensor)
            _LOGGER.info(
                "Nimbus: controllable load power sensor %s reports Watts "
                "(unit_of_measurement=%r) -- scaling by %.3f to kW for "
                "run-state sampling (logged once per entity)",
                power_sensor,
                unit,
                scale,
            )
        power_kw = float(state_obj.state) * scale

        try:
            from . import load_run_state
            from .const import DOMAIN
        except ImportError:
            import load_run_state
            from const import DOMAIN

        async def _update() -> load_run_state.LoadRunState:
            store = load_run_state.LoadRunStateStore(
                store=_Store(_NATIVE_HASS, 1, f"{DOMAIN}_{hub_entry_id}_load_run_state")
            )
            prev = await store.async_read(subentry_id)
            new = load_run_state.apply_power_sample(
                prev,
                now=now,
                day_key=day_key,
                power_kw=power_kw,
                import_price_now=import_price_now,
            )
            await store.async_write(subentry_id, new)
            return new

        import asyncio as _asyncio

        future = _asyncio.run_coroutine_threadsafe(_update(), _NATIVE_HASS.loop)
        return future.result(timeout=10)
    except Exception:
        _LOGGER.debug(
            "Nimbus: controllable load run-state sample failed for %s (%s)",
            subentry_id,
            power_sensor,
            exc_info=True,
        )
        return None


# nimbus issue #480: a small, FIXED comparison DSL for a deferrable
# load's own done_when field (e.g. ">= 60") -- deliberately not eval(),
# since a household-supplied config string must never run as code.
# nimbus issue #639: the actual operators/parser (_parse_done_when) now
# live in done_condition.py, imported above -- so sensor.py's schedule-
# view sensors can evaluate the identical done_when comparison without
# this module's own heavy numpy/highspy imports. See that module's own
# docstring.

# nimbus issue #480 (Mark Purcell's own live review): the malformed-
# done_when/non-numeric-state warning below used to fire on every solve
# tick (~5 min) for as long as the same bad condition persisted -- the
# #313/#314 "log once per condition" discipline this project already
# follows elsewhere (e.g. _log_active_household_specific_overrides_once
# above), not "every cycle". Keyed by the exact (entity, done_when,
# state) triple so a genuinely NEW bad reading (a different malformed
# done_when, or the entity settling on a different bad value) still
# gets its own one-time log -- only the identical, already-reported
# combination is suppressed. Module-level, same "lives for the process"
# scope as _household_specific_overrides_logged; never cleared, since a
# household fixing the misconfiguration changes the triple anyway.
_DONE_CONDITION_WARNED: set[tuple[str, str | None, str]] = set()

# nimbus issue #712/#713: log-once-per-(load, day) dedup for the
# hardware-floor-crossing warning below -- same #313/#314 discipline as
# every other recurring per-cycle warning in this module. Keyed by
# (subentry_id, day_key) -- the same identifier this function's own
# other warnings already log by, not a load's display name -- so a
# genuinely NEW day's own crossing still gets its own warning even if
# yesterday's was already reported.
_FLOOR_CROSSING_WARNED: set[tuple[str, str]] = set()

# nimbus issue #875, gap found by Mark Purcell's IV&V of PR #930. Same
# (subentry_id, day_key) shape and the same reasoning as the floor-crossing
# set just above: the condition it reports holds for every remaining solve
# of the day once it is true, and this codebase has already had to clean up
# per-cycle log spam twice (v0.94.301/302's overlap guard, #773's
# diagnostic quieted to two-tier DEBUG/WARNING). Keyed per load so one
# exhausted device cannot mute another's warning.
_REAFFIRM_CAP_WARNED: set[tuple[str, str]] = set()

# nimbus issue #534 (Mark Purcell, real SG-Ready heat-pump HWS install):
# a water_heater's/climate's own *state* is a mode string ("eco"), not a
# number -- done_when can never be evaluated against it. Both domains
# instead carry the live reading as the current_temperature attribute
# and the active setpoint as the temperature attribute, so a done
# condition on one of these entities reads current_temperature (not
# state), and an unset done_when defaults to ">= <temperature
# attribute>" (the household's own already-configured setpoint) rather
# than the binary_sensor "state == on" default used for every other
# domain.
# nimbus issue #590: the water_heater/climate domain list now lives in
# done_condition.py (see that module's own top docstring for why) so
# sensor.py's own schedule-view sensors can read the identical domain
# list/attribute without importing THIS module -- solver_writer.py's
# numpy/highspy imports make it unsafe to import from a plain event-loop
# context before the first real solve has already paid that cost once.
_ATTRIBUTE_DONE_DOMAINS = _done_condition_attribute_domains


def _evaluate_done_condition(done_entity: str, done_when: str | None) -> bool | None:
    """nimbus issue #480: reads done_entity's real live state and decides
    whether a deferrable load counts as DONE. Returns True/False, or
    None for "can't tell right now" (entity missing/unavailable/unknown,
    or a genuinely malformed done_when) -- the caller's own fail-open
    contract (#480's acceptance: "a done-sensor going unavailable is
    ignored... same discipline as #313/#314") treats None as "not done,
    keep the normal schedule", never as an error.

    done_when=None means done_entity is treated as a binary_sensor --
    its own "on" state alone is the done condition, the same convention
    a plain HA automation trigger would use. Any other domain (a numeric
    tank-temperature sensor, say) needs done_when to say what "done"
    means for that reading -- except water_heater/climate (#534), whose
    own state is a mode string: those read current_temperature instead,
    and an unset done_when falls back to the entity's own temperature
    (setpoint) attribute rather than the binary_sensor "on" convention.
    """
    if _NATIVE_HASS is None:
        return None
    state_obj = _NATIVE_HASS.states.get(done_entity)
    if state_obj is None or state_obj.state in (None, "unknown", "unavailable"):
        return None
    domain = done_entity.split(".", 1)[0]
    if domain in _ATTRIBUTE_DONE_DOMAINS:
        current = state_obj.attributes.get("current_temperature")
        if current is None:
            return None
        if done_when is None:
            target = state_obj.attributes.get("temperature")
            if target is None:
                return None
            try:
                return float(current) >= float(target)
            except (ValueError, TypeError):
                return None
        try:
            op_fn, threshold = _parse_done_when(done_when)
            return bool(op_fn(float(current), threshold))
        except (ValueError, TypeError):
            condition_key = (done_entity, done_when, str(current))
            if condition_key not in _DONE_CONDITION_WARNED:
                _DONE_CONDITION_WARNED.add(condition_key)
                _LOGGER.warning(
                    "Nimbus: controllable load done_entity %s / done_when %r "
                    "could not be evaluated (current_temperature %r) -- "
                    "treating as not done until this changes (logged once "
                    "per condition, not every solve)",
                    done_entity,
                    done_when,
                    current,
                )
            return None
    if done_when is None:
        return state_obj.state == "on"
    try:
        op_fn, threshold = _parse_done_when(done_when)
        return bool(op_fn(float(state_obj.state), threshold))
    except (ValueError, TypeError):
        condition_key = (done_entity, done_when, state_obj.state)
        if condition_key not in _DONE_CONDITION_WARNED:
            _DONE_CONDITION_WARNED.add(condition_key)
            _LOGGER.warning(
                "Nimbus: controllable load done_entity %s / done_when %r could "
                "not be evaluated (state %r) -- treating as not done until this "
                "changes (logged once per condition, not every solve)",
                done_entity,
                done_when,
                state_obj.state,
            )
        return None


def _build_daily_adequacy_windows(
    grid_times: list[datetime],
    now: datetime,
    earliest_hour: float,
    deadline_hour: float,
    target_kwh: float,
    *,
    today_delivered_kwh: float,
    today_done: bool,
) -> list:
    """nimbus issue #612: builds one elements.AdequacyWindow per real
    calendar-day occurrence of [earliest_hour, deadline_hour] that fits
    (even partially) within grid_times' own horizon -- so a deferrable
    load owes its own target_kwh FRESH every day, not just once wherever
    the single "next occurrence from now" window happens to land (the
    bug: `plan_forecast` reading 0.0 for every day past the first).

    Only ever called for the same-day window shape (deadline_hour >=
    earliest_hour) -- see this function's own caller for why a genuine
    overnight window falls through to the pre-#612 single-window path
    instead.

    Day 0 (today) gets the #582 same-day-in-progress treatment (earliest
    resolves to "right now" if the window already opened) plus the
    #626/#480 treatment (today_delivered_kwh reduces its own target;
    today_done, or today's window having already fully closed for the
    day, drops it from the list entirely). Day 1 onward always get the
    FULL, unreduced target_kwh -- "delivered today"/"done" only ever
    speak to today's own run, never a future day's.
    """
    try:
        from . import load_run_state
        from .solver import elements
    except ImportError:
        import load_run_state
        from solver import elements

    windows: list = []
    if not grid_times:
        return windows
    last_grid_time = grid_times[-1]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = 0
    while True:
        earliest_today = midnight + timedelta(days=day, hours=earliest_hour)
        if earliest_today > last_grid_time:
            break
        deadline_today = midnight + timedelta(days=day, hours=deadline_hour)
        if day == 0:
            day_target_kwh = load_run_state.remaining_kwh(
                target_kwh=target_kwh, delivered_today_kwh=today_delivered_kwh
            )
            if now > deadline_today or today_done or day_target_kwh <= 0.0:
                day += 1
                continue
            earliest_period = (
                0
                if earliest_today <= now <= deadline_today
                else _period_index_for_instant(
                    grid_times, earliest_today, is_deadline=False
                )
            )
        else:
            earliest_period = _period_index_for_instant(
                grid_times, earliest_today, is_deadline=False
            )
            day_target_kwh = target_kwh
        deadline_period = _period_index_for_instant(
            grid_times, deadline_today, is_deadline=True
        )
        if deadline_period >= earliest_period:
            windows.append(
                elements.AdequacyWindow(
                    earliest_period=earliest_period,
                    deadline_period=deadline_period,
                    target_kwh=day_target_kwh,
                )
            )
        day += 1
    return windows


# nimbus issue #645: real household ask, mirroring number.py's own
# hub-level "wizard for first-time setup, live entity for day-to-day
# tuning" pattern (2026-08-20) per Controllable Load. The 7 fields a
# household will genuinely want to retune as they learn a load's real
# behaviour, WITHOUT re-running the whole wizard step -- see number.py's
# own _CONTROLLABLE_LOAD_DESCRIPTIONS for the exact bounds/defaults and
# the full "why these 7, not the 3 sheddable-only fields too" scoping.
_CONTROLLABLE_LOAD_LIVE_NUMBER_KEYS = (
    "deferrable_target_kwh",
    "deferrable_max_power_kw",
    "deferrable_earliest_hour",
    "deferrable_deadline_hour",
    "deferrable_shortfall_price",
    "controllable_load_min_hold_minutes",
    "controllable_load_max_activations_per_day",
)


def _slug_for_controllable_load_entity_id(title: str) -> str:
    """Deliberate verbatim duplicate of sensor.py's own private
    `_slug_for_entity_id()` -- see number.py's own copy of this same
    function for the full "why duplicated, not imported" reasoning
    (this module's own dual native/standalone import boundary makes
    that doubly true here: solver_writer.py must stay importable with
    no `sensor`/`number` module in scope at all in standalone/cron
    mode). Keep in sync with both other copies if the slugging rule
    itself ever changes -- all three must agree on the SAME entity_id
    for the SAME title.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug or "load"


def _resolve_controllable_load_tuning(data: dict, subentry) -> dict:
    """nimbus issue #645: overlays each of the 7 real live-editable
    tuning fields (number.nimbus_<load>_<key>) on top of `data`'s own
    wizard-saved value, so every EXISTING read site in build_
    controllable_loads()/apply_commanded_state_guard() (a plain `data.
    get(CONF_DEFERRABLE_...)`) picks up the live value automatically,
    with zero further changes needed at each individual call site.

    A field whose live entity doesn't exist yet (a load created before
    this change, or a genuinely fresh install before the number platform
    has finished setup) or reads unknown/unavailable/non-numeric falls
    straight back to `data`'s own existing value -- the wizard value
    stays a REAL fallback, never silently dropped. Native mode only
    (returns `data` unchanged when `_NATIVE_HASS` is None), same
    reasoning as build_controllable_loads() itself: ConfigSubentries
    have no standalone/cron equivalent to read a live entity from
    either. Also returns `data` unchanged if `subentry` has no `title`
    (a real ConfigSubentry always does; a genuinely malformed/unusual
    object here fails open to the pre-#645 behaviour rather than
    crashing this whole load's own solve over a slug it can't compute).
    """
    if _NATIVE_HASS is None:
        return data
    title = getattr(subentry, "title", None)
    if not title:
        return data
    slug = _slug_for_controllable_load_entity_id(title)
    resolved = dict(data)
    for key in _CONTROLLABLE_LOAD_LIVE_NUMBER_KEYS:
        state = _NATIVE_HASS.states.get(f"number.nimbus_{slug}_{key}")
        if state is None or state.state in (None, "unknown", "unavailable"):
            continue
        try:
            resolved[key] = float(state.state)
        except (TypeError, ValueError):
            continue
    # nimbus issue #485: the household-mode preset lands LAST, on
    # top of the live number.* overlay above -- so it scales the
    # value actually in force, never a stale wizard entry the
    # household has already tuned past. `home` and an unset mode
    # are the identity transform.
    mode_state = _NATIVE_HASS.states.get("select.nimbus_household_mode")
    mode = None
    if mode_state is not None and mode_state.state not in (
        None,
        "unknown",
        "unavailable",
    ):
        mode = mode_state.state
    resolved, _mode_applied = household_modes.apply_to_load_config(resolved, mode)
    if _mode_applied:
        # DEBUG, not INFO, deliberately: this fires once per load per
        # solve, so a six-load install in `away` would emit six lines a
        # minute at INFO -- exactly the noise #757 and #773 each had to
        # clean up after shipping. The solver-lever half logs at INFO
        # because it fires once per solve, not once per load.
        #
        # Worth having at all because "which of my levers did `away`
        # actually move?" is the first question a household asks when a
        # mode does not do what they expected, and without this the
        # per-load half is invisible: the moded value lives only inside
        # the solve and is never published anywhere.
        _LOGGER.debug(
            "Nimbus #485: household mode %r moved %d lever(s) on %r: %s",
            mode,
            len(_mode_applied),
            title,
            _mode_applied,
        )
    return resolved


# nimbus issue #768 (Mark Purcell, 2026-09-14): "MQTT heat pump device
# has a power sensor, please use it, through auto discovery."
#
# Context: #809 removed `power_sensor` as a wizard field and defaulted
# done_entity/temperature_entity to the load's own device_entity -- but a
# water_heater/climate entity cannot report its own draw, so there was no
# equivalent default and the field stayed unset. Consequence, confirmed
# live on Mark's install: `_async_fetch_thermal_history()` got
# power_sensor=None, returned [], `learn_thermal_rates()` found no
# segments, and `thermal_rates_source` read "fallback" -- meaning the
# real HWS was being scheduled off DEFAULT_HEATING_RATE_C_PER_KWH /
# DEFAULT_IDLE_DECAY_C_PER_HOUR, generic constants rather than its own
# tank. #800's whole value proposition was silently not delivering on
# the one real thermal load that exists.
#
# Discovery is via the DEVICE REGISTRY, never the entity's name. A
# name-shaped guess ("sensor.<device slug>_power") would work on Mark's
# `water_heater.wwk302` -> `sensor.wwk302_power` and fail on anything
# else, and this project has already had that exact lesson: the Power
# Signal `signal_role` field exists precisely because naming could not
# reliably distinguish a battery/solar/grid sensor on real hardware
# ("Combined Total DC Power" has no "solar" in it). Same rule here --
# the device registry knows which entities belong to the same physical
# device, so ask it.
#
# Ambiguity is NOT resolved by guessing. Zero matches, or more than one
# (a device exposing per-phase power, say), returns None and logs once;
# the explicit CONF_CONTROLLABLE_LOAD_POWER_SENSOR override still exists
# via the set_controllable_load service for those cases. Silently
# picking one of several would be the same class of confidently-wrong
# behaviour #118 already cost this project a $46/day misplan over.
_POWER_SENSOR_DISCOVERY_LOGGED: set[str] = set()


# nimbus issue #1067: participants configured with no power limits at all.
# Log-once per subentry, same discipline as the two sets above -- this
# fires from build_extra_batteries(), which runs every solve, and a
# condition that is true once is true every cycle until reconfigured.
_IMMOBILE_PARTICIPANT_WARNED: set[str] = set()


def _discover_power_sensor_for_device(device_entity: str) -> str | None:
    """The single `device_class: power` sensor on the same physical
    device as `device_entity`, or None when there isn't exactly one.

    Native-mode only -- a standalone/cron run has no entity or device
    registry to consult, and Controllable Loads have no standalone
    existence anyway (build_controllable_loads() returns ([], []) there).
    """
    if _NATIVE_HASS is None or not device_entity:
        return None
    try:
        from homeassistant.helpers import entity_registry as er
    except ImportError:  # pragma: no cover - standalone/cron path
        return None

    registry = er.async_get(_NATIVE_HASS)
    entry = registry.async_get(device_entity)
    if entry is None or entry.device_id is None:
        return None

    candidates = [
        sibling.entity_id
        for sibling in er.async_entries_for_device(
            registry, entry.device_id, include_disabled_entities=False
        )
        if sibling.domain == "sensor"
        # A user override on the registry entry wins over the
        # integration's own original_device_class, same precedence HA
        # itself applies when rendering the entity.
        and (sibling.device_class or sibling.original_device_class) == "power"
    ]

    if len(candidates) == 1:
        if device_entity not in _POWER_SENSOR_DISCOVERY_LOGGED:
            _POWER_SENSOR_DISCOVERY_LOGGED.add(device_entity)
            _LOGGER.info(
                "Nimbus: auto-discovered power sensor %s for controllable load "
                "device %s (same device in the registry, device_class=power). "
                "Set controllable_load_power_sensor explicitly via the "
                "set_controllable_load service to override.",
                candidates[0],
                device_entity,
            )
        return candidates[0]

    if device_entity not in _POWER_SENSOR_DISCOVERY_LOGGED:
        _POWER_SENSOR_DISCOVERY_LOGGED.add(device_entity)
        if not candidates:
            _LOGGER.debug(
                "Nimbus: no device_class=power sensor found on the same device "
                "as %s -- thermal rate learning stays on fallback constants "
                "until controllable_load_power_sensor is set explicitly.",
                device_entity,
            )
        else:
            _LOGGER.warning(
                "Nimbus: %d power sensors found on the same device as %s (%s) "
                "-- refusing to guess which one measures this load. Set "
                "controllable_load_power_sensor explicitly via the "
                "set_controllable_load service.",
                len(candidates),
                device_entity,
                ", ".join(sorted(candidates)),
            )
    return None


def resolve_controllable_load_power_sensor(data: dict) -> str | None:
    """`controllable_load_power_sensor` if configured, otherwise the one
    auto-discovered from the load's own device (nimbus issue #768).

    An explicit setting always wins -- discovery only fills a gap, it
    never overrides a household's own stated answer.
    """
    # Same dual-mode deferred const import every other function in this
    # file uses -- solver_writer is loaded both as part of the real
    # package and as a bare top-level module (tests/_solver_path.py, the
    # standalone/cron deployment), and these names are not bound at
    # module scope here.
    try:
        from .const import (
            CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
            CONF_CONTROLLABLE_LOAD_POWER_SENSOR,
        )
    except ImportError:  # pragma: no cover - standalone/cron path
        from const import (  # type: ignore[no-redef]
            CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
            CONF_CONTROLLABLE_LOAD_POWER_SENSOR,
        )

    explicit = data.get(CONF_CONTROLLABLE_LOAD_POWER_SENSOR)
    if explicit:
        return explicit
    return _discover_power_sensor_for_device(
        data.get(CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY) or ""
    )


def build_controllable_loads(
    now: datetime,
    grid_times: list[datetime],
    n_periods: int,
    import_price_arr: list[float] | None = None,
) -> tuple[list, list, list]:
    """nimbus issue #486: builds SheddableLoadConfig/AdequacyLoadConfig
    lists from this hub's own `controllable_load` subentries, for
    build_plan()'s own sheddable_loads=/adequacy_loads= arguments --
    before this function existed, both were always empty (hardcoded),
    and neither LP class (despite existing since #229/#16) had ever
    actually run on a live install.

    nimbus issue #774: also builds ThermalLoadConfig entries for
    kind=thermal subentries, returned as a third list. `heating_rate_
    c_per_kwh`/`idle_decay_c_per_hour` come from (in order): an explicit
    CONF_THERMAL_HEATING_RATE_C_PER_KWH/CONF_THERMAL_IDLE_DECAY_C_PER_HOUR
    override, else this subentry's own ALREADY-PERSISTED LoadRunState
    (`run_state_sample.thermal_heating_rate_c_per_kwh`/`_idle_decay_c_
    per_hour`, keyed by subentry_id -- so a load migrated from
    kind=deferrable keeps whatever it already learned, since that
    learning lives in the run-state store, not the kind), else
    thermal_forecast's own module-level defaults. Real, honest scope
    limit (not silently hidden): this function is SYNCHRONOUS (native
    ConfigSubentry access only), and a fresh recorder-history relearn is
    genuinely async (see apply_commanded_state_guard()'s own deferrable-
    kind relearning block, which bridges into it via `_async_fetch_
    thermal_history()`) -- a thermal load that has NEVER been kind=
    deferrable has no persisted learned rate to read yet and stays on
    config-override/module-default until a future issue extends that
    same async relearning trigger to kind=thermal loads too.

    Native/in-process mode ONLY (returns ([], [], []) unconditionally when
    _NATIVE_HASS is None, i.e. the standalone/cron deployment) --
    ConfigSubentries are a real HA config_entries object, not something
    exposed over this module's own plain-REST ha_get()/ha_post_state()
    seam the standalone path uses, and Mark's own #486 spec doesn't ask
    for a standalone controllable-load config path either. Imported
    locally (not at module top) so this module's own standalone-mode
    import path (see this file's own top-of-file try/except) never has
    to resolve `.const` at all -- it's only ever needed here, and only
    ever reached once _NATIVE_HASS is already known to be set.
    """
    if _NATIVE_HASS is None:
        return [], [], []
    # Same relative-then-absolute fallback as this file's own top-of-file
    # import block -- solver_writer.py can be imported either as part of
    # the real `custom_components.nimbus_load` package (native mode,
    # relative import resolves) or as a bare top-level module (this
    # project's own stub-based test harness, and the standalone/cron
    # deployment -- no parent package, relative import raises
    # ImportError). _NATIVE_HASS being non-None only ever happens via
    # native mode's own set_native_hass() in real deployment, but a test
    # mocking that module-level global directly (as this function's own
    # tests do, to exercise this path without the full HA test harness)
    # hits the bare-module case, so both must actually work.
    try:
        from . import done_condition, load_run_state, thermal_forecast
        from .const import (
            CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
            CONF_CONTROLLABLE_LOAD_KIND,
            CONF_CONTROLLABLE_LOAD_NAME,
            CONF_DEFERRABLE_DEADLINE_HOUR,
            CONF_DEFERRABLE_DONE_ENTITY,
            CONF_DEFERRABLE_DONE_WHEN,
            CONF_DEFERRABLE_EARLIEST_HOUR,
            CONF_DEFERRABLE_MAX_KWH_PER_DAY,
            CONF_DEFERRABLE_MAX_POWER_KW,
            CONF_DEFERRABLE_MIN_DEFERRAL_SAVING_DOLLARS,
            CONF_DEFERRABLE_SHORTFALL_PRICE,
            CONF_DEFERRABLE_TARGET_KWH,
            CONF_DEFERRABLE_VALUE_PER_KWH,
            CONF_DEFERRABLE_VALUE_PER_KWH_ENTITY,
            CONF_SHEDDABLE_MIN_FRACTION,
            CONF_SHEDDABLE_NOMINAL_KW,
            CONF_SHEDDABLE_SHED_COST,
            CONF_THERMAL_COMFORT_FLOOR_C,
            CONF_THERMAL_COMFORT_FLOOR_COST,
            CONF_THERMAL_DEADLINE_HOUR,
            CONF_THERMAL_EARLIEST_HOUR,
            CONF_THERMAL_HEATING_RATE_C_PER_KWH,
            CONF_THERMAL_IDLE_DECAY_C_PER_HOUR,
            CONF_THERMAL_MAX_POWER_KW,
            CONF_THERMAL_TARGET_TEMPERATURE_C,
            CONF_THERMAL_TEMPERATURE_ENTITY,
            CONTROLLABLE_LOAD_KIND_DEFERRABLE,
            CONTROLLABLE_LOAD_KIND_SHEDDABLE,
            CONTROLLABLE_LOAD_KIND_THERMAL,
            DOMAIN,
            SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        )
    except ImportError:
        import done_condition
        import load_run_state
        import thermal_forecast
        from const import (
            CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
            CONF_CONTROLLABLE_LOAD_KIND,
            CONF_CONTROLLABLE_LOAD_NAME,
            CONF_DEFERRABLE_DEADLINE_HOUR,
            CONF_DEFERRABLE_DONE_ENTITY,
            CONF_DEFERRABLE_DONE_WHEN,
            CONF_DEFERRABLE_EARLIEST_HOUR,
            CONF_DEFERRABLE_MAX_KWH_PER_DAY,
            CONF_DEFERRABLE_MAX_POWER_KW,
            CONF_DEFERRABLE_MIN_DEFERRAL_SAVING_DOLLARS,
            CONF_DEFERRABLE_SHORTFALL_PRICE,
            CONF_DEFERRABLE_TARGET_KWH,
            CONF_DEFERRABLE_VALUE_PER_KWH,
            CONF_DEFERRABLE_VALUE_PER_KWH_ENTITY,
            CONF_SHEDDABLE_MIN_FRACTION,
            CONF_SHEDDABLE_NOMINAL_KW,
            CONF_SHEDDABLE_SHED_COST,
            CONF_THERMAL_COMFORT_FLOOR_C,
            CONF_THERMAL_COMFORT_FLOOR_COST,
            CONF_THERMAL_DEADLINE_HOUR,
            CONF_THERMAL_EARLIEST_HOUR,
            CONF_THERMAL_HEATING_RATE_C_PER_KWH,
            CONF_THERMAL_IDLE_DECAY_C_PER_HOUR,
            CONF_THERMAL_MAX_POWER_KW,
            CONF_THERMAL_TARGET_TEMPERATURE_C,
            CONF_THERMAL_TEMPERATURE_ENTITY,
            CONTROLLABLE_LOAD_KIND_DEFERRABLE,
            CONTROLLABLE_LOAD_KIND_SHEDDABLE,
            CONTROLLABLE_LOAD_KIND_THERMAL,
            DOMAIN,
            SUBENTRY_TYPE_CONTROLLABLE_LOAD,
        )

    sheddable_loads: list = []
    adequacy_loads: list = []
    thermal_loads: list = []
    entries = _NATIVE_HASS.config_entries.async_entries(DOMAIN)
    if not entries:
        return [], [], []
    # nimbus issue #757 (temporary diagnostic, remove once root-caused):
    # a live check found build_extra_batteries()'s own identical
    # `_NATIVE_HASS.config_entries.async_entries(DOMAIN)` call returning
    # a STALE `entries[0]` (34 subentries, missing every controllable_
    # load and battery_participant, real subentry_ids that don't match
    # a fresh `ha_get_integration` read of the SAME entry_id at the same
    # moment) -- while THIS function, using the exact same call, was
    # simultaneously dispatching real controllable loads correctly on
    # the same devhub install. Logging the same entry-count/entry_id/
    # title/subentry-count triple here too, so the next real occurrence
    # settles directly whether these two call sites are ever actually
    # seeing DIFFERENT `entries[0]` objects (which would mean something
    # environmental, not a bug in either function's own logic) or the
    # same one (which would mean build_extra_batteries()'s own filter
    # loop, not the entries lookup itself, is where subentries are
    # actually being lost).
    _LOGGER.debug(
        "Nimbus #757 diag: build_controllable_loads() async_entries(DOMAIN) "
        "returned %d entr%s: %s",
        len(entries),
        "y" if len(entries) == 1 else "ies",
        [
            (
                getattr(e, "entry_id", None),
                getattr(e, "title", None),
                getattr(getattr(e, "state", None), "value", None),
                len(e.subentries),
            )
            for e in entries
        ],
    )
    run_state_day_key = now.strftime("%Y-%m-%d")
    for subentry in entries[0].subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_CONTROLLABLE_LOAD:
            continue
        # nimbus issue #645: overlays the 7 live-editable tuning fields
        # on top of the wizard-saved data -- every read below is
        # unchanged, it now just sees the live value when one exists.
        data = _resolve_controllable_load_tuning(subentry.data, subentry)
        name = data.get(CONF_CONTROLLABLE_LOAD_NAME) or subentry.subentry_id
        kind = data.get(CONF_CONTROLLABLE_LOAD_KIND)
        # nimbus issue #768: falls back to the single device_class=power
        # sensor on this load's own device when the field is unset.
        power_sensor = resolve_controllable_load_power_sensor(data)
        run_state_sample = None
        if power_sensor:
            # nimbus issue #479: every configured load's own currently_on/
            # on_since/off_since/delivered_today_kwh gets sampled here,
            # regardless of kind -- #484's chatter-guard needs this for
            # any Controllable Load, not just a future quota kind. See
            # _sample_load_run_state's own docstring for why this never
            # raises into the rest of this function. `entries[0].entry_id`
            # is only ever read here (not unconditionally above), so a
            # fake/test hass object with no real entry_id -- like this
            # file's own tests use for the sheddable/deferrable cases,
            # neither of which sets power_sensor -- never has to carry
            # one just to exercise the rest of this function.
            #
            # nimbus issue #626: the return value (this cycle's own
            # freshest delivered_today_kwh) is kept for the deferrable
            # branch below, which needs it to stop scheduling the full
            # target_kwh on top of what's already been delivered today.
            run_state_sample = _sample_load_run_state(
                entries[0].entry_id,
                subentry.subentry_id,
                power_sensor,
                now,
                run_state_day_key,
                import_price_now=(
                    float(import_price_arr[0])
                    if import_price_arr is not None and len(import_price_arr) > 0
                    else None
                ),
            )
        if kind == CONTROLLABLE_LOAD_KIND_SHEDDABLE:
            nominal_kw = float(data.get(CONF_SHEDDABLE_NOMINAL_KW) or 0.0)
            if nominal_kw <= 0.0:
                # Real caller mistake (wizard submitted with the one
                # field this kind actually needs left blank) -- skip
                # rather than let elements.SheddableLoadConfig's own
                # >0 validation crash the whole solve cycle over one
                # misconfigured subentry.
                _LOGGER.warning(
                    "Nimbus: controllable load '%s' (sheddable) has no "
                    "nominal_kw configured -- skipping this cycle",
                    name,
                )
                continue
            sheddable_loads.append(
                elements.SheddableLoadConfig(
                    name=name,
                    forecast_kw=np.full(n_periods, nominal_kw),
                    shed_cost=float(
                        data.get(CONF_SHEDDABLE_SHED_COST) or elements.DEFAULT_SHED_COST
                    ),
                    min_fraction=float(data.get(CONF_SHEDDABLE_MIN_FRACTION) or 0.0),
                    subentry_id=subentry.subentry_id,
                )
            )
        elif kind == CONTROLLABLE_LOAD_KIND_DEFERRABLE:
            max_power_kw = float(data.get(CONF_DEFERRABLE_MAX_POWER_KW) or 0.0)
            target_kwh_config = float(data.get(CONF_DEFERRABLE_TARGET_KWH) or 0.0)
            if max_power_kw <= 0.0 or target_kwh_config <= 0.0:
                _LOGGER.warning(
                    "Nimbus: controllable load '%s' (deferrable) is missing "
                    "max_power_kw/target_kwh -- skipping this cycle",
                    name,
                )
                continue
            # nimbus issue #626 (Mark Purcell, real repro: 1.48 of 2.0 kWh
            # already delivered at 13:05, plan still scheduled 1.99 kWh
            # more): this used to pass the raw configured target_kwh
            # straight to AdequacyLoadConfig on every solve, with nothing
            # anywhere reducing it by what run_state_sample (just taken,
            # above) already shows was delivered today --
            # `load_run_state.remaining_kwh()` existed and was unit-
            # tested since #479 but was never actually called from here.
            # Only trust run_state_sample's own delivered_today_kwh when
            # its day_key matches THIS cycle's day -- apply_power_sample()
            # already rolls delivered_today_kwh back to 0.0 on a genuine
            # day change, so a mismatch here only means the sample call
            # above failed/no-op'd (see its own docstring), and 0.0
            # (today's config-target behaviour, unchanged) is the correct
            # fail-open default rather than guessing. Only ever speaks to
            # TODAY's own run -- see _build_daily_adequacy_windows() for
            # why a future day's window always gets the full,
            # config-configured target_kwh regardless of this value.
            delivered_today_kwh = (
                run_state_sample.delivered_today_kwh
                if run_state_sample is not None
                and run_state_sample.day_key == run_state_day_key
                else 0.0
            )
            earliest_hour = data.get(CONF_DEFERRABLE_EARLIEST_HOUR)
            deadline_hour = data.get(CONF_DEFERRABLE_DEADLINE_HOUR)
            # nimbus issue #480: a done_entity that currently reports DONE
            # means TODAY's real requirement is already satisfied -- fail-
            # open (None/malformed/unavailable) is treated as NOT done,
            # same as _evaluate_done_condition()'s own contract; only ever
            # speaks to today, same reasoning as delivered_today_kwh above.
            # nimbus issue #809: the wizard no longer asks for a separate
            # Done entity -- it defaults to this same load's own device_
            # entity (the common real case, and Mark's own actual
            # household config before #809: both fields pointed at the
            # identical water_heater.wwk302). An explicit override is
            # still honoured for a household whose done condition
            # genuinely lives on a different entity than the one being
            # commanded.
            done_entity = data.get(CONF_DEFERRABLE_DONE_ENTITY) or data.get(
                CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY
            )
            today_done = bool(
                done_entity
                and _evaluate_done_condition(
                    done_entity, data.get(CONF_DEFERRABLE_DONE_WHEN)
                )
            )
            value_per_kwh = data.get(CONF_DEFERRABLE_VALUE_PER_KWH)
            # nimbus issue #482: a configured entity's CURRENT numeric
            # state overrides the static field above for this solve --
            # same "live override, static field as the safe_num()
            # fallback" pattern as build_extra_batteries()'s own
            # charge_limit_entity handling for CONF_BATTERY_PARTICIPANT_
            # CHARGE_LIMIT_ENTITY. A household can point this at a
            # template sensor (hashprice x hashrate / power) or a plain
            # input_number ("willing to pay") and change it freely
            # between solves with no config reload.
            value_per_kwh_entity = data.get(CONF_DEFERRABLE_VALUE_PER_KWH_ENTITY)
            if value_per_kwh_entity:
                value_per_kwh = safe_num(
                    value_per_kwh_entity,
                    float(value_per_kwh) if value_per_kwh is not None else 0.0,
                )
            max_kwh_per_day = data.get(CONF_DEFERRABLE_MAX_KWH_PER_DAY)
            # nimbus issue #769 (household decision 2026-09-15):
            # "do not defer this load unless it saves more than $X",
            # per load. 0.0/unset is a complete no-op.
            min_deferral_saving = data.get(CONF_DEFERRABLE_MIN_DEFERRAL_SAVING_DOLLARS)
            shortfall_price = float(
                data.get(CONF_DEFERRABLE_SHORTFALL_PRICE)
                or elements.DEFAULT_ADEQUACY_SHORTFALL_PRICE
            )

            # nimbus issue #612 (Mark Purcell, real repro: plan_forecast
            # is 0.0 for 10-13 Sep because a load's target only ever
            # applied to whichever single day the "next occurrence from
            # now" window happened to land on): a load with BOTH
            # earliest/deadline hours set, in the ordinary same-day shape
            # (deadline_hour >= earliest_hour), owes its own fresh
            # target_kwh EVERY calendar day within the horizon, not just
            # once. A genuine overnight window (deadline_hour <
            # earliest_hour, e.g. earliest=22/deadline=6) is a different,
            # not-yet-validated recurring shape -- falls through to the
            # single-window path below unchanged, same as a load missing
            # either hour entirely.
            if (
                earliest_hour is not None
                and deadline_hour is not None
                and float(deadline_hour) >= float(earliest_hour)
            ):
                windows = _build_daily_adequacy_windows(
                    grid_times,
                    now,
                    float(earliest_hour),
                    float(deadline_hour),
                    target_kwh_config,
                    today_delivered_kwh=delivered_today_kwh,
                    today_done=today_done,
                )
                if not windows:
                    _LOGGER.info(
                        "Nimbus: controllable load '%s' (deferrable) has no "
                        "real window left in this cycle's own horizon -- "
                        "skipping (today's own target already met/done, and "
                        "no future day's window fits inside the horizon)",
                        name,
                    )
                    continue
                # nimbus issue #712/#713 (Mark Purcell, real live finding:
                # two consecutive nights of uncontrolled compressor cut-in
                # on the WWK302/#534 heat pump -- the deferrable model's
                # kWh-target/deadline framing has no representation of the
                # device's own physical thermal floor, so the LP is free
                # to wait for a cheaper period even when doing so lets the
                # tank fall past its floor and self-trigger, uncontrolled,
                # at whatever price happens to be live). #713's own text
                # proposed "pull the earliest allowed start forward" --
                # traced the actual LP constraint (network.py's adequacy
                # window sum) and that lever alone would not have changed
                # Mark's real repro: his window's earliest_period (06:00)
                # was already before the projected floor crossing (07:30),
                # the LP simply preferred the cheaper 08:00 WITHIN that
                # already-permissive window. The lever that actually forces
                # delivery before a real deadline is the window's own
                # DEADLINE, not its earliest bound -- tightening the
                # NEAREST window's deadline_period down to the projected
                # crossing period is what genuinely compels the LP to
                # schedule real heating before the tank breaches its floor,
                # rather than merely widening a bound the LP wasn't
                # constrained by in the first place.
                #
                # Deliberately only the NEAREST window (windows[0]) --
                # naive_floor_crossing_period() assumes ZERO further
                # heating from `now` onward, which is only a trustworthy
                # assumption up to whichever window real heating might
                # first occur in; a later window's own eventual deadline is
                # left untouched, same as PR #719's own floor_crossing_
                # forecast_* fields never claimed to predict past the first
                # crossing either.
                #
                # Known, honest simplification (not silently hidden): the
                # decay rate used here is the project's own documented
                # DEFAULT_IDLE_DECAY_C_PER_HOUR fallback, not this load's
                # own LEARNED rate (LoadRunState's thermal_idle_decay_c_
                # per_hour) -- the learned rate lives in the async run-state
                # store, and this function is synchronous (native
                # ConfigSubentry access only, see this function's own top
                # docstring), so threading the learned rate through here
                # would need a real async refactor of this function's own
                # call chain. Worth a follow-up once that's justified on
                # its own; the default fallback is the same constant this
                # project already trusts for a load with no learned rate
                # yet, not an invented number.
                if done_entity and done_entity.split(".", 1)[0] in (
                    done_condition.ATTRIBUTE_DONE_DOMAINS
                ):
                    live_temperature = done_condition.read_current_temperature(
                        _NATIVE_HASS, done_entity
                    )
                    done_state_obj = _NATIVE_HASS.states.get(done_entity)
                    min_temp = (
                        done_state_obj.attributes.get("min_temp")
                        if done_state_obj is not None
                        else None
                    )
                    floor_temperature = thermal_forecast.resolve_floor_temperature(
                        min_temp,
                        data.get(CONF_DEFERRABLE_DONE_WHEN),
                        done_condition.parse_done_when,
                    )
                    crossing_period = thermal_forecast.naive_floor_crossing_period(
                        grid_times,
                        now,
                        live_temperature,
                        thermal_forecast.DEFAULT_IDLE_DECAY_C_PER_HOUR,
                        floor_temperature,
                    )
                    first_window = windows[0]
                    if (
                        crossing_period is not None
                        and crossing_period < first_window.deadline_period
                    ):
                        new_deadline_period = max(
                            first_window.earliest_period, crossing_period
                        )
                        _LOGGER.warning(
                            "Nimbus: controllable load '%s' (deferrable) is "
                            "projected to cross its own physical floor (%.1f) "
                            "at period %d assuming no further heating -- "
                            "tightening this window's own deadline from "
                            "period %d to %d to force delivery before the "
                            "device self-triggers outside the solved plan "
                            "(nimbus issue #712/#713)",
                            name,
                            floor_temperature,
                            crossing_period,
                            first_window.deadline_period,
                            new_deadline_period,
                        )
                        windows = [
                            elements.AdequacyWindow(
                                earliest_period=first_window.earliest_period,
                                deadline_period=new_deadline_period,
                                target_kwh=first_window.target_kwh,
                            ),
                            *windows[1:],
                        ]
                first = windows[0]
                adequacy_loads.append(
                    elements.AdequacyLoadConfig(
                        name=name,
                        max_power_kw=max_power_kw,
                        # Required legacy fields, unused for LP construction
                        # once `windows` is set (see AdequacyLoadConfig's own
                        # docstring) -- populated from the first real window
                        # so they still describe something true rather than
                        # an arbitrary placeholder.
                        target_kwh=first.target_kwh,
                        deadline_period=first.deadline_period,
                        earliest_period=first.earliest_period,
                        shortfall_price=shortfall_price,
                        value_per_kwh=float(value_per_kwh)
                        if value_per_kwh is not None
                        else None,
                        max_kwh_per_day=float(max_kwh_per_day)
                        if max_kwh_per_day is not None
                        else None,
                        min_deferral_saving_dollars=float(min_deferral_saving)
                        if min_deferral_saving is not None
                        else 0.0,
                        subentry_id=subentry.subentry_id,
                        windows=tuple(windows),
                    )
                )
                continue

            # ---- Single-window path: a load missing one/both hours, or
            # a genuine overnight window -- unchanged from before #612.
            target_kwh = load_run_state.remaining_kwh(
                target_kwh=target_kwh_config, delivered_today_kwh=delivered_today_kwh
            )
            if target_kwh <= 0.0:
                _LOGGER.info(
                    "Nimbus: controllable load '%s' (deferrable) already "
                    "delivered %.3f kWh today, meeting its %.3f kWh target "
                    "-- releasing the remainder of this window's schedule",
                    name,
                    delivered_today_kwh,
                    target_kwh_config,
                )
                continue
            earliest_period = (
                _resolve_hour_to_period_index(
                    grid_times, now, float(earliest_hour), is_deadline=False
                )
                if earliest_hour is not None
                else 0
            )
            deadline_period = (
                _resolve_hour_to_period_index(
                    grid_times, now, float(deadline_hour), is_deadline=True
                )
                if deadline_hour is not None
                else n_periods - 1
            )
            # nimbus issue #582 (Mark Purcell, first live morning of #534):
            # _resolve_hour_to_period_index() always resolves "the next
            # occurrence from now" independently for each hour -- once
            # `now` is past today's earliest_hour, earliest rolls forward
            # to TOMORROW even though today's window (earliest through
            # deadline) is still open and in progress right now. Correct
            # for the genuine overnight case (earliest=22, deadline=6,
            # `now` between midnight and 6am) but wrong for a same-day
            # window once the day has started (earliest=6, deadline=16,
            # `now`=06:01 -- exactly Mark's real repro). Detect the
            # same-day-in-progress case directly, before the ordering
            # check below: today's own (unrolled) earliest and deadline
            # instants both fall on today, and `now` sits between them --
            # if so the window opened earlier today and is still active,
            # so earliest_period is simply "right now" (0), not tomorrow.
            # nimbus issue #582, extracted to one helper by #485 --
            # this logic previously existed as four identical copies.
            earliest_period = _earliest_period_for_same_day_window(
                now=now,
                earliest_hour=earliest_hour,
                deadline_hour=deadline_hour,
                earliest_period=earliest_period,
                deadline_period=deadline_period,
            )
            if deadline_period < earliest_period:
                # A real, live-possible edge: e.g. earliest=22.0 (10pm),
                # deadline=6.0 (6am) both resolve relative to `now` (see
                # _resolve_hour_to_period_index's own docstring), and if
                # `now` is already past today's 6am but before 10pm,
                # earliest resolves to tonight while deadline resolves to
                # tomorrow's 6am -- fine. But if `now` is itself between
                # midnight and 6am, both can resolve to the same day in
                # the wrong order. Rather than construct an
                # AdequacyLoadConfig that fails its own __post_init__
                # ordering check (a real crash), skip this cycle with a
                # clear reason -- the next cycle's own `now` will very
                # likely resolve this correctly on its own.
                _LOGGER.warning(
                    "Nimbus: controllable load '%s' (deferrable) resolved "
                    "deadline_period (%d) before earliest_period (%d) for "
                    "this cycle's own 'now' -- skipping until the window "
                    "resolves normally",
                    name,
                    deadline_period,
                    earliest_period,
                )
                continue
            # nimbus issue #480: a done_entity that currently reports DONE
            # means this window's real requirement is already satisfied --
            # skip this cycle entirely rather than let the LP keep buying
            # energy this load no longer needs (the issue's own worked
            # example: HWS scheduled for 3h, reaches setpoint after 2h,
            # "the third hour is still bought"). Fail-open on anything
            # else (no done_entity configured, entity unavailable, a
            # malformed done_when) -- _evaluate_done_condition() only
            # ever returns True when it's genuinely confident.
            if today_done:
                _LOGGER.info(
                    "Nimbus: controllable load '%s' (deferrable) reports "
                    "done via %s -- releasing the remainder of this "
                    "window's schedule",
                    name,
                    done_entity,
                )
                continue
            adequacy_loads.append(
                elements.AdequacyLoadConfig(
                    name=name,
                    max_power_kw=max_power_kw,
                    target_kwh=target_kwh,
                    deadline_period=deadline_period,
                    earliest_period=earliest_period,
                    shortfall_price=shortfall_price,
                    value_per_kwh=float(value_per_kwh)
                    if value_per_kwh is not None
                    else None,
                    max_kwh_per_day=float(max_kwh_per_day)
                    if max_kwh_per_day is not None
                    else None,
                    min_deferral_saving_dollars=float(min_deferral_saving)
                    if min_deferral_saving is not None
                    else 0.0,
                    subentry_id=subentry.subentry_id,
                )
            )
        elif kind == CONTROLLABLE_LOAD_KIND_THERMAL:
            max_power_kw = float(data.get(CONF_THERMAL_MAX_POWER_KW) or 0.0)
            target_temperature_c = data.get(CONF_THERMAL_TARGET_TEMPERATURE_C)
            # nimbus issue #809: the wizard no longer asks for a separate
            # temperature entity -- kind=thermal only ever has one real
            # device.py entity worth pointing at anyway (the same
            # water_heater/climate this load is commanded through), so
            # this defaults to the load's own device_entity. An explicit
            # override is still honoured for the rare household whose
            # temperature reading and commanded device are genuinely
            # different entities.
            temperature_entity = data.get(CONF_THERMAL_TEMPERATURE_ENTITY) or data.get(
                CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY
            )
            if (
                max_power_kw <= 0.0
                or target_temperature_c is None
                or not temperature_entity
            ):
                _LOGGER.warning(
                    "Nimbus: controllable load '%s' (thermal) is missing "
                    "max_power_kw/target_temperature_c/temperature_entity -- "
                    "skipping this cycle",
                    name,
                )
                continue
            live_temperature = done_condition.read_current_temperature(
                _NATIVE_HASS, temperature_entity
            )
            if live_temperature is None:
                _LOGGER.warning(
                    "Nimbus: controllable load '%s' (thermal) has no live "
                    "temperature reading from %s (unavailable, or not a "
                    "water_heater/climate entity) -- skipping this cycle",
                    name,
                    temperature_entity,
                )
                continue
            earliest_hour = data.get(CONF_THERMAL_EARLIEST_HOUR)
            deadline_hour = data.get(CONF_THERMAL_DEADLINE_HOUR)
            earliest_period = (
                _resolve_hour_to_period_index(
                    grid_times, now, float(earliest_hour), is_deadline=False
                )
                if earliest_hour is not None
                else 0
            )
            deadline_period = (
                _resolve_hour_to_period_index(
                    grid_times, now, float(deadline_hour), is_deadline=True
                )
                if deadline_hour is not None
                else n_periods - 1
            )
            # nimbus issue #582's own same-day-in-progress fix, duplicated
            # here rather than shared -- same accepted drift-risk tradeoff
            # already flagged for apply_commanded_state_guard()'s own
            # duplicate of this exact block (2026-09-09 worklog).
            # nimbus issue #582, extracted to one helper by #485 --
            # this logic previously existed as four identical copies.
            earliest_period = _earliest_period_for_same_day_window(
                now=now,
                earliest_hour=earliest_hour,
                deadline_hour=deadline_hour,
                earliest_period=earliest_period,
                deadline_period=deadline_period,
            )
            if deadline_period < earliest_period:
                _LOGGER.warning(
                    "Nimbus: controllable load '%s' (thermal) resolved "
                    "deadline_period (%d) before earliest_period (%d) for "
                    "this cycle's own 'now' -- skipping until the window "
                    "resolves normally",
                    name,
                    deadline_period,
                    earliest_period,
                )
                continue
            # nimbus issue #774: heating_rate_c_per_kwh/idle_decay_c_per_hour
            # -- explicit override, else this subentry's own already-
            # persisted LoadRunState (see this function's own docstring for
            # the full "why not a fresh relearn here" reasoning), else
            # thermal_forecast's own module-level defaults.
            heating_rate_override = data.get(CONF_THERMAL_HEATING_RATE_C_PER_KWH)
            idle_decay_override = data.get(CONF_THERMAL_IDLE_DECAY_C_PER_HOUR)
            # nimbus issue #940: the origin is recorded on the SAME
            # branch that picks the value, so it cannot describe a
            # different branch than the one taken. This is the only site
            # that resolves this precedence; everything downstream echoes
            # what is decided here.
            if heating_rate_override is not None:
                heating_rate_c_per_kwh = float(heating_rate_override)
                heating_rate_origin = "override"
            elif (
                run_state_sample is not None
                and run_state_sample.thermal_heating_rate_c_per_kwh is not None
            ):
                heating_rate_c_per_kwh = run_state_sample.thermal_heating_rate_c_per_kwh
                heating_rate_origin = "learned"
            else:
                heating_rate_c_per_kwh = thermal_forecast.DEFAULT_HEATING_RATE_C_PER_KWH
                heating_rate_origin = "fallback"
            if idle_decay_override is not None:
                idle_decay_c_per_hour = float(idle_decay_override)
                idle_decay_origin = "override"
            elif (
                run_state_sample is not None
                and run_state_sample.thermal_idle_decay_c_per_hour is not None
            ):
                idle_decay_c_per_hour = run_state_sample.thermal_idle_decay_c_per_hour
                idle_decay_origin = "learned"
            else:
                idle_decay_c_per_hour = thermal_forecast.DEFAULT_IDLE_DECAY_C_PER_HOUR
                idle_decay_origin = "fallback"
            comfort_floor_c = data.get(CONF_THERMAL_COMFORT_FLOOR_C)
            comfort_floor_cost = data.get(CONF_THERMAL_COMFORT_FLOOR_COST)
            thermal_loads.append(
                elements.ThermalLoadConfig(
                    name=name,
                    max_power_kw=max_power_kw,
                    initial_temperature_c=live_temperature,
                    target_temperature_c=float(target_temperature_c),
                    earliest_period=earliest_period,
                    deadline_period=deadline_period,
                    heating_rate_c_per_kwh=heating_rate_c_per_kwh,
                    idle_decay_c_per_hour=idle_decay_c_per_hour,
                    # nimbus issue #940
                    heating_rate_origin=heating_rate_origin,
                    idle_decay_origin=idle_decay_origin,
                    comfort_floor_c=(
                        float(comfort_floor_c) if comfort_floor_c is not None else None
                    ),
                    comfort_floor_cost=(
                        float(comfort_floor_cost)
                        if comfort_floor_cost is not None
                        else 0.0
                    ),
                    subentry_id=subentry.subentry_id,
                )
            )
    return sheddable_loads, adequacy_loads, thermal_loads


# nimbus issue #563: no wizard field exists yet for a battery
# participant's own charge/discharge $/kWh cost, but BatteryConfig.
# __post_init__ structurally requires the two to sum to at least
# elements.MIN_CHARGE_DISCHARGE_COST_SPREAD (0.01 $/kWh, the HAEO
# wash-trade-degeneracy guard). These are deliberately small (0.005 +
# 0.01 = 0.015, clearing the floor with a real margin) so they never
# meaningfully distort dispatch decisions -- a placeholder that clears
# a structural validation floor, not a real household-specific cost.
_DEFAULT_EXTRA_BATTERY_CHARGE_COST: float = 0.005
_DEFAULT_EXTRA_BATTERY_DISCHARGE_COST: float = 0.01

# nimbus issue #843 (Mark Purcell, option B of his own A/B/C steer,
# 2026-09-13): how far past a participant's OWN configured
# charge/discharge envelope a reconstructed sample may sit before it is
# treated as corrupt rather than real.
#
# Why 10x and not something tighter: real sensors legitimately overshoot
# a nameplate rating, and the configured figure is a household-entered
# number that may itself be conservative -- someone who types 5.0 for a
# 25 kW charger would have every genuine reading discarded by a tight
# bound, which is its own (silent, worse) bug. 10x leaves room for both.
#
# Why 10x is still tight enough to be useful: the real failure this
# guards against is a W-vs-kW mismatch, which is exactly 1000x. Mark's
# own confirmed incident was a 25 kW-configured EV reporting a single
# 1514.417 sample -- ~60x its envelope -- so a 10x bound catches it with
# two orders of magnitude to spare while never coming near plausible
# hardware overshoot. The gap between "conservative config" and "unit
# error" is three orders of magnitude wide; this sits in the middle of
# it deliberately.
_PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE: float = 10.0


def _drop_implausible_power_samples(
    power_hist: list[tuple[datetime, float]],
    *,
    power_scale: float,
    max_plausible_kw: float,
    participant_name: str,
    power_sensor: str,
) -> list[tuple[datetime, float]]:
    """nimbus issue #843 (option B): drop raw power samples whose real
    magnitude is physically impossible for this participant's own
    configured envelope, BEFORE they reach resample_history_mean().

    DISCARD, not clamp -- deliberately. Clamping a 1514 kW reading down
    to a 250 kW bound would assert a value that was never measured, and
    would still be ~10x what a 25 kW device can physically do: it turns
    an obviously-absurd number into a plausible-looking but still-wrong
    one, which is strictly worse for a figure that feeds a real cost
    calculation, because it no longer trips anyone's suspicion. Dropping
    the sample instead lets the period's mean be built from whatever
    REAL samples remain; a period left with none falls through to
    resample_history_mean()'s own `default=0.0`, which since #843's own
    v0.94.285 fix genuinely means "no measured flow" rather than
    "whatever this sensor read next". For a power FLOW signal that is
    the honest answer.

    Filtered on the RAW history rather than the resampled series, also
    deliberately: a bad sample that survives into resample_history_mean()
    has already been averaged into its period before any post-hoc check
    could see it, so a period holding one 1514 kW sample among five good
    ones emerges at ~252 kW -- contaminated, but no longer obviously
    corrupt enough for a magnitude bound to catch. Filtering upstream
    means the bad reading never enters any mean at all.

    `max_plausible_kw <= 0` means this participant has no configured
    envelope to judge against, so there is no bound and this is a
    genuine no-op -- never clamp to zero, never divide by anything.

    Logs once per call (not once per bad sample) with the count and the
    single worst offender: this is real corrupt data in a household's
    own recorder and they should know, but a sensor stuck in the wrong
    unit for an hour must not produce hundreds of WARNING lines. Same
    bounded-logging posture as _ENVELOPE_LIMIT_WARNED/_AEMO_P5MIN_LAST_
    WARNED_PERIOD elsewhere in this file, scoped to this function's own
    once-per-scored-day call pattern.
    """
    if max_plausible_kw <= 0.0 or not power_hist:
        return power_hist
    kept: list[tuple[datetime, float]] = []
    dropped: list[tuple[datetime, float]] = []
    for t, v in power_hist:
        if abs(v * power_scale) > max_plausible_kw:
            dropped.append((t, v))
        else:
            kept.append((t, v))
    if dropped:
        worst_t, worst_v = max(dropped, key=lambda p: abs(p[1] * power_scale))
        _LOGGER.warning(
            "Nimbus quality: discarded %d physically implausible reading(s) "
            "from battery participant '%s' power sensor %s -- worst was "
            "%.3f (scaled: %.3f kW) at %s, beyond this participant's own "
            "configured envelope of %.3f kW (%.0fx). Most often a sensor "
            "briefly reporting a different unit than it does now (nimbus "
            "issue #843); the affected periods are reconstructed from the "
            "remaining real samples instead, or read 0 kW if a period has "
            "none left",
            len(dropped),
            participant_name,
            power_sensor,
            worst_v,
            worst_v * power_scale,
            worst_t.isoformat(),
            max_plausible_kw,
            _PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE,
        )
    return kept


# nimbus issue #779 (Mark Purcell, confirmed decision): a battery
# participant reading "away" right now is only bounded evidence for the
# NEAR term -- see BatteryConfig.unavailable_until_period_index's own
# docstring for the full mechanism this backs. Explicitly a fixed
# constant, not a wizard field: Mark's own ask was "the next hour," and
# he confirmed it's the right value as-is, not a placeholder to tune
# against real commute patterns first (detailed availability -- knowing
# WHEN a car actually returns -- stays out of scope, waiting on real
# calendar/trip-window integration, #467 item 3).
_BATTERY_PARTICIPANT_AWAY_EXCLUSION_HOURS: float = 1.0

# nimbus issue #563 item 2: log-once-per-participant dedup for the
# "departure_hour/must_have_soc_by_departure_percent set alone" partial-
# config warning below -- same log-once-per-condition discipline this
# module already uses elsewhere (see _DONE_CONDITION_WARNED further up
# this file), kept as its own set rather than reusing that one since the
# two track genuinely different condition shapes.
_BATTERY_PARTICIPANT_WARNED: set[str] = set()

# nimbus issue #735 stage 5: _HOME_BATTERY_SOC_EXCURSION_WARNED moved to
# solver_inputs/battery_soc.py with the only block that ever read it.
# Its sibling _BATTERY_PARTICIPANT_WARNED above stays here, keyed by
# participant name, for build_extra_batteries()'s own equivalent warning.


def build_extra_batteries(periods: elements.PeriodGrid | None = None) -> list:
    """nimbus issue #563: the config surface for #467 stage 1's own
    `batteries: list[BatteryConfig]` solver support. Builds ADDITIONAL
    BatteryConfig entries from this hub's own `battery_participant`
    subentries -- the household's existing single hub-level battery
    (built separately in main()/compute_nimbus_only_soc_counterfactual(),
    always name="home") is never touched or replaced by this function;
    every caller does `batteries=[home_battery, *build_extra_batteries(periods)]`.

    Zero subentries (the default, and every install before #563) returns
    [] -- a real no-op, `batteries` stays exactly `[home_battery]`,
    byte-identical to v0.94.177's own single-battery behaviour. This is
    the explicit "upgrade is a no-op" requirement from #563's own issue
    body.

    Native/in-process mode ONLY -- same reasoning as build_controllable_
    loads() just above (ConfigSubentries aren't exposed over this
    module's own plain-REST seam, and the standalone/cron deployment
    doesn't have a wizard to configure these from anyway). Imported
    locally for the same reason build_controllable_loads() does.

    `periods` (2026-09-08, nimbus issue #563 items 2/3, following up on
    PR #566's own explicit deferral): THIS solve's own PeriodGrid, needed
    only to resolve a configured departure_hour into a real period index
    for the departure-deadline mechanism below (see BatteryConfig.must_
    have_soc_by_period_index's own docstring for why network.py itself
    stays grid-agnostic and only ever receives an already-resolved
    index). Optional and defaults to None so every existing test/caller
    that predates #563 items 2/3 keeps working unchanged; a real caller
    always passes the real periods (see main()'s own call site).

    Availability gating (item 2, the binary_sensor half): reads
    available_entity's CURRENT state once per solve -- 'on' (or no
    entity configured at all) means available, anything else ('off',
    missing, unavailable/unknown) means not available for this WHOLE
    solve. See BatteryConfig.available's own docstring for why this is
    a whole-horizon snapshot, not a mid-horizon prediction of when an
    away EV will return -- there is no real forecast for that, and
    fabricating one would be worse than the honest "re-evaluate fresh
    every 5-minute solve cycle" answer.

    Availability gating (item 2, the departure-deadline half): resolves
    departure_hour to the FIRST period in THIS horizon whose own real
    wall-clock start hour matches (via periods.period_starts) -- a
    departure hour with no matching period in this particular solve's
    horizon (a short manual solve, or the hour has already passed today
    with no later occurrence in range) is a real, expected no-op, not an
    error. Both departure_hour and must_have_soc_by_departure_percent
    must be configured together to do anything -- either one alone is
    treated as neither set (logged once, not every solve).

    **The deadline is ONE-SHOT, not daily** (nimbus issue #467, stated
    because the field name suggests otherwise). On the real 96-hour
    horizon a departure hour occurs four times and the loop below breaks
    on the first, so the constraint guarantees SoC for the NEXT
    departure and says nothing about the three after it.

    That is acceptable rather than a defect because Nimbus is
    receding-horizon (rolling.py): every cycle re-solves and re-resolves,
    so the near deadline is always the one being enforced, and the
    unconstrained far end of the plan is never committed -- the next
    solve replaces it. It stops being acceptable the moment a real
    calendar event is expressible ("next Tuesday 07:15", #467 stage 3),
    at which point recurring-vs-one-shot becomes a genuine modelling
    choice rather than an artefact of hour-of-day being the only
    vocabulary available. Pinned by test_the_deadline_is_ONE_SHOT_
    across_a_real_multi_day_horizon so that decision gets made rather
    than inherited.

    Shared-charger group (item 3): shared_charger_group/shared_charger_
    max_kw pass straight through to BatteryConfig -- the actual LP
    constraint (grouping participants by name, taking the minimum
    declared ceiling) lives entirely in network.py's own build_plan(),
    this function only carries the two raw config values across.
    """
    if _NATIVE_HASS is None:
        return []
    try:
        from .const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
            CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
            CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
            CONF_BATTERY_PARTICIPANT_NAME,
            CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            DOMAIN,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )
    except ImportError:
        from const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY,
            CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
            CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
            CONF_BATTERY_PARTICIPANT_NAME,
            CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            DOMAIN,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )

    batteries: list = []
    entries = _NATIVE_HASS.config_entries.async_entries(DOMAIN)
    if not entries:
        return []
    seen_names: set[str] = set()
    # nimbus issue #757 (temporary diagnostic, remove once root-caused):
    # a battery_participant subentry confirmed correctly stored (verified
    # directly via ha_get_integration's subentry introspection) was never
    # appearing in the solved battery list, with zero log output from any
    # of this loop's own existing warning branches -- meaning either this
    # loop never reaches the subentry, or it's being skipped by something
    # this unconditional trace hasn't been asked to explain yet. Logs
    # every subentry_type this loop actually sees, every cycle, until the
    # real mechanism is found.
    #
    # 2026-09-13 follow-up: a fresh devhub read found `entries[0].
    # subentries` reporting a stable set of 34 subentries (real
    # subentry_ids like "01KZYY...", no battery_participant present at
    # all) that does NOT match `ha_get_integration`'s own live read of
    # this exact entry_id moments later (38 subentries, real ids like
    # "01M14NJ...", battery_participant "Test EV" present) -- same
    # circuit TITLES appear under completely different subentry_ids in
    # the two reads. Two real, testable hypotheses this alone can't
    # distinguish between: (a) `async_entries(DOMAIN)` is genuinely
    # returning more than one config entry for this domain and
    # `entries[0]` is silently picking a stale/orphaned one instead of
    # the real, currently-configured entry the household actually sees;
    # or (b) something rarer (a stale Python object reference to the
    # "same" entry_id surviving past a point where HA replaced it).
    # Logging every entry this call actually returns -- not just
    # entries[0] -- to settle this directly on the next real occurrence,
    # rather than re-deriving it from static reading a third time.
    _LOGGER.debug(
        "Nimbus #757 diag: async_entries(DOMAIN) returned %d entr%s: %s",
        len(entries),
        "y" if len(entries) == 1 else "ies",
        [
            (
                getattr(e, "entry_id", None),
                getattr(e, "title", None),
                getattr(getattr(e, "state", None), "value", None),
                len(e.subentries),
            )
            for e in entries
        ],
    )
    _LOGGER.debug(
        "Nimbus #757 diag: build_extra_batteries scanning %d subentries: %s",
        len(entries[0].subentries),
        [
            (s.subentry_id, s.subentry_type, s.data.get(CONF_BATTERY_PARTICIPANT_NAME))
            for s in entries[0].subentries.values()
        ],
    )
    for subentry in entries[0].subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_BATTERY_PARTICIPANT:
            continue
        data = subentry.data
        name = data.get(CONF_BATTERY_PARTICIPANT_NAME) or subentry.subentry_id
        _LOGGER.debug(
            "Nimbus #757 diag: found battery_participant subentry name=%r data_keys=%s",
            name,
            sorted(data.keys()),
        )
        if name in seen_names or name == "home":
            # "home" is reserved for the hub's own single battery (see
            # this function's own docstring) -- a household typing it
            # in here by accident would otherwise silently collide with
            # it across LP variable naming and cross-solve stability.
            # A duplicate name between two subentries is the same real
            # risk. Both skipped with a loud warning rather than crashing
            # the whole solve cycle over one misconfigured subentry --
            # same "skip, don't crash" discipline build_controllable_
            # loads() above already uses.
            _LOGGER.warning(
                "Nimbus: battery participant subentry name '%s' is reserved "
                "or duplicated -- skipping this cycle. Every battery "
                "participant needs its own real name, and 'home' is "
                "reserved for the hub's own single battery.",
                name,
            )
            continue
        seen_names.add(name)
        capacity_kwh = float(data.get(CONF_BATTERY_PARTICIPANT_CAPACITY_KWH) or 0.0)
        max_charge_kw = float(data.get(CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW) or 0.0)
        max_discharge_kw = float(
            data.get(CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW) or 0.0
        )
        soc_sensor = data.get(CONF_BATTERY_PARTICIPANT_SOC_SENSOR)
        if capacity_kwh <= 0.0 or not soc_sensor:
            _LOGGER.warning(
                "Nimbus: battery participant '%s' is missing capacity_kwh "
                "or its SoC sensor -- skipping this cycle",
                name,
            )
            continue
        # nimbus issue #1067: a participant that can neither charge nor
        # discharge passes every check above -- capacity is positive,
        # the SoC sensor is present -- and joins the fleet as a battery
        # the LP can never move. Nothing said so.
        #
        # Reachable without touching the schema: the wizard requires
        # both fields, but `0` is a valid entry for a kW selector, and
        # both read sites fall back to `0.0` when absent.
        #
        # It is not inert, which is why it is worth a line. `battery_
        # oracle` still hands this participant its full SoC envelope, so
        # the scorer's oracle models a fleet member it can never
        # dispatch -- moving `j_star`, and therefore EPR and regret. Same
        # shape as the phantom recorded on #768, reached by a different
        # route.
        #
        # Deliberately a WARNING and NOT a `continue`. Excluding it is
        # probably the right end state, but that changes what a live
        # install solves, and the honest first step is to make the
        # silent case loud so a household can see it and decide. #1067
        # carries the argument for the stronger version.
        if (
            max_charge_kw <= 0.0
            and max_discharge_kw <= 0.0
            and subentry.subentry_id not in _IMMOBILE_PARTICIPANT_WARNED
        ):
            _IMMOBILE_PARTICIPANT_WARNED.add(subentry.subentry_id)
            _LOGGER.warning(
                "Nimbus #1067: battery participant %r has both "
                "max_charge_kw and max_discharge_kw at 0 -- it is in "
                "the fleet but the solver can never move it, and the "
                "scorer's oracle still receives its full SoC "
                "envelope, which shifts j_star/EPR/regret. Set real "
                "power limits, or remove the participant if it is "
                "not meant to be dispatched.",
                name,
            )
        min_soc_pct = float(data.get(CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT) or 0.0)
        max_soc_pct = float(data.get(CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT) or 100.0)
        # #563 item 4: a live number entity's CURRENT value, when
        # configured, overrides the wizard's own static max_soc_percent
        # for this solve -- see const.py's own comment on this field.
        charge_limit_entity = data.get(CONF_BATTERY_PARTICIPANT_CHARGE_LIMIT_ENTITY)
        if charge_limit_entity:
            max_soc_pct = safe_num(charge_limit_entity, max_soc_pct)
        min_soc_kwh = capacity_kwh * min_soc_pct / 100.0
        max_soc_kwh = capacity_kwh * max_soc_pct / 100.0
        # Same honest, no-crash-on-a-glitch-reading discipline as the
        # home battery's own live SoC read in main() -- see that call
        # site's own comment for the real "27+-crashes-per-window"
        # incident this pattern fixes. safe_num() itself already
        # degrades gracefully (WARN + fallback) on a non-numeric state.
        initial_soc_pct = safe_num(soc_sensor, min_soc_pct)
        initial_soc_kwh = capacity_kwh * initial_soc_pct / 100.0
        initial_soc_kwh = min(max(initial_soc_kwh, 0.0), capacity_kwh)
        # nimbus issue #563 item 2 (2026-09-08): availability gating.
        # No entity configured -- always available, byte-identical to
        # every scenario before this field existed. An entity that's
        # missing/unavailable/unknown is treated the same as 'off' (not
        # available) -- the conservative reading for a live safety-
        # relevant gate: if we can't confirm the car/resource is really
        # there, don't plan to dispatch it. Moved ahead of the SoC-
        # excursion warning below (nimbus issue #601) so that warning can
        # know whether this participant is even reachable this cycle.
        available_entity = data.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY)
        available = True
        if available_entity:
            state_obj = _NATIVE_HASS.states.get(available_entity)
            available = state_obj is not None and state_obj.state == "on"
        # nimbus issue #779: a currently-away participant is only gated
        # for the next _BATTERY_PARTICIPANT_AWAY_EXCLUSION_HOURS, not
        # this whole solve's horizon -- resolved to a real period index
        # the same way departure_hour is resolved further down (first
        # period whose own start is >= now + the exclusion window).
        # `periods` being unavailable (an older/manual caller with no
        # period context) falls back to the pre-#779 whole-horizon gate
        # -- same graceful-degradation posture #563 items 2/3 already
        # established for this same function.
        unavailable_until_period_index: int | None = None
        if not available and periods is not None:
            period_starts = periods.period_starts
            if period_starts:
                cutoff = period_starts[0] + timedelta(
                    hours=_BATTERY_PARTICIPANT_AWAY_EXCLUSION_HOURS
                )
                unavailable_until_period_index = sum(
                    1 for start in period_starts if start < cutoff
                )
        # Parity fix (2026-09-08, Mark Purcell's own live-tested #563
        # review): the home battery's own live SoC read in main() logs a
        # WARNING when it sits outside its configured [min, max] --
        # "if this repeats every period the real battery is stuck
        # outside its own configured range... investigate rather than
        # lower the floor." This path previously recovered silently (an
        # EV arriving above its own charge limit landing in the soft
        # `overfill` slack with nothing logged) -- same real signal,
        # same real reason to surface it, now the same warning.
        #
        # nimbus issue #601 (Mark Purcell, real finding: 35 WARNING lines
        # in 51 minutes for one parked EV recovering slowly on solar --
        # every solve, including the 2-3 extra price-change-triggered
        # solves per 5-minute slot, re-logged the identical line). Now
        # WARNING only on the FIRST cycle a participant is found outside
        # its own range, DEBUG on every cycle it stays outside (still
        # visible if you go looking, never spamming the real log), and
        # one INFO "recovered" the cycle it returns inside -- same
        # per-condition warn-once discipline _BATTERY_PARTICIPANT_WARNED
        # already uses for the partial departure-deadline config just
        # below. A participant that's `available=False` (gated off, e.g.
        # away from home) is skipped entirely: the LP cannot schedule its
        # recovery and the household cannot act on it either, so there is
        # nothing actionable to log.
        _soc_excursion_key = f"{name}:soc_excursion"
        if not (min_soc_kwh <= initial_soc_kwh <= max_soc_kwh):
            if not available:
                pass
            elif _soc_excursion_key not in _BATTERY_PARTICIPANT_WARNED:
                _BATTERY_PARTICIPANT_WARNED.add(_soc_excursion_key)
                _LOGGER.warning(
                    "Nimbus: battery participant '%s' live SoC %.2f%% is "
                    "outside its own configured floor/ceiling [%.2f%%, "
                    "%.2f%%] -- the LP is scheduling real recovery this "
                    "cycle rather than having this state clamped away. If "
                    "this repeats every period, investigate rather than "
                    "adjust the floor/ceiling. (Logged once per excursion; "
                    "further cycles are DEBUG until it recovers.)",
                    name,
                    initial_soc_pct,
                    min_soc_pct,
                    max_soc_pct,
                )
            else:
                _LOGGER.debug(
                    "Nimbus: battery participant '%s' live SoC %.2f%% "
                    "still outside its own configured floor/ceiling "
                    "[%.2f%%, %.2f%%] this cycle.",
                    name,
                    initial_soc_pct,
                    min_soc_pct,
                    max_soc_pct,
                )
        elif _soc_excursion_key in _BATTERY_PARTICIPANT_WARNED:
            _BATTERY_PARTICIPANT_WARNED.discard(_soc_excursion_key)
            _LOGGER.info(
                "Nimbus: battery participant '%s' live SoC %.2f%% has "
                "recovered back inside its own configured floor/ceiling "
                "[%.2f%%, %.2f%%].",
                name,
                initial_soc_pct,
                min_soc_pct,
                max_soc_pct,
            )
        # nimbus issue #563 item 2, the departure-deadline half. Both
        # fields must be set together to do anything -- either one alone
        # is treated as neither set (a real, expected partial config,
        # logged once so it's visible without being noisy every solve).
        departure_hour = data.get(CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR)
        must_have_soc_pct = data.get(
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT
        )
        must_have_soc_by_period_index: int | None = None
        must_have_soc_kwh: float | None = None
        if (departure_hour is not None) != (must_have_soc_pct is not None):
            _partial_key = f"{name}:departure_deadline_partial"
            if _partial_key not in _BATTERY_PARTICIPANT_WARNED:
                _BATTERY_PARTICIPANT_WARNED.add(_partial_key)
                _LOGGER.warning(
                    "Nimbus: battery participant '%s' has only one of "
                    "departure_hour/must_have_soc_by_departure_percent set -- "
                    "both are required together, treating as neither set "
                    "this cycle (logged once, not every solve).",
                    name,
                )
        elif departure_hour is not None and periods is not None:
            period_starts = periods.period_starts
            if period_starts is not None:
                for idx, start in enumerate(period_starts):
                    if _local(start).hour == int(departure_hour):
                        must_have_soc_by_period_index = idx
                        must_have_soc_kwh = (
                            capacity_kwh * float(must_have_soc_pct) / 100.0
                        )
                        break
                # No matching period in THIS horizon -- a real, expected
                # no-op (short manual solve, or the hour already passed
                # today with no later occurrence in range), not an error.
        # nimbus issue #563 item 3: passed straight through to
        # BatteryConfig -- the actual LP constraint lives in network.py.
        shared_charger_group = (
            data.get(CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP) or None
        )
        shared_charger_max_kw_raw = data.get(
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW
        )
        shared_charger_max_kw = (
            float(shared_charger_max_kw_raw)
            if shared_charger_max_kw_raw is not None
            else None
        )
        efficiency = (
            min(
                float(data.get(CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT) or 95.0)
                / 100.0,
                0.999,
            )
            ** 0.5
        )
        # power_positive_is_charge (data key CONF_BATTERY_PARTICIPANT_
        # POWER_POSITIVE_IS_CHARGE) is read live for future dashboard/
        # monitoring wiring, not needed by BatteryConfig itself -- the
        # LP's own charge[t]/discharge[t] are always two separate
        # nonnegative variables (see BatteryConfig's own class
        # docstring), never one signed reading.
        salvage_value = float(data.get(CONF_BATTERY_PARTICIPANT_SALVAGE_VALUE) or 0.0)
        degradation_cost_per_kwh = float(
            data.get(CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH) or 0.0
        )
        _LOGGER.debug(
            "Nimbus #757 diag: about to append BatteryConfig for %r "
            "(capacity_kwh=%s, max_charge_kw=%s, max_discharge_kw=%s, "
            "min_soc_kwh=%s, max_soc_kwh=%s, initial_soc_kwh=%s)",
            name,
            capacity_kwh,
            max_charge_kw,
            max_discharge_kw,
            min_soc_kwh,
            max_soc_kwh,
            initial_soc_kwh,
        )
        batteries.append(
            elements.BatteryConfig(
                name=name,
                capacity_kwh=capacity_kwh,
                initial_soc_kwh=initial_soc_kwh,
                min_soc_kwh=min_soc_kwh,
                max_soc_kwh=max_soc_kwh,
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw,
                charge_efficiency=efficiency,
                discharge_efficiency=efficiency,
                # No wizard field for a real charge/discharge $/kWh cost
                # per participant yet -- BatteryConfig.__post_init__
                # structurally requires charge_cost + discharge_cost to
                # clear elements.MIN_CHARGE_DISCHARGE_COST_SPREAD (the
                # HAEO wash-trade-degeneracy guard, see that constant's
                # own docstring), so a bare 0.0/0.0 is NOT a valid no-op
                # here the way it is for degradation_cost_per_kwh just
                # below. These two small reference constants clear that
                # floor with margin while staying a genuinely small,
                # non-distorting economic signal -- see their own
                # module-level comment.
                charge_cost=_DEFAULT_EXTRA_BATTERY_CHARGE_COST,
                discharge_cost=_DEFAULT_EXTRA_BATTERY_DISCHARGE_COST,
                salvage_value=salvage_value,
                degradation_cost_per_kwh=degradation_cost_per_kwh,
                available=available,
                unavailable_until_period_index=unavailable_until_period_index,
                must_have_soc_by_period_index=must_have_soc_by_period_index,
                must_have_soc_kwh=must_have_soc_kwh,
                shared_charger_group=shared_charger_group,
                shared_charger_max_kw=shared_charger_max_kw,
            )
        )
    _LOGGER.debug(
        "Nimbus #757 diag: build_extra_batteries returning %d battery config(s): %s",
        len(batteries),
        [b.name for b in batteries],
    )
    return batteries


def _power_history_coverage(
    pts: list[tuple[datetime, float]],
    grid_times: list[datetime],
    period_hours: float,
) -> dict[str, float | int] | None:
    """How well a power sensor's recorder history actually covers the
    scored window (nimbus issue #1181).

    `resample_history_mean()` is time-weighted and deliberately "never
    fabricates a gap", so a period with no sample of its own inherits the
    most recent earlier reading. For a STATE that is right. For POWER it
    means a stale instantaneous value is integrated across time nobody
    observed -- and on the reference household's own charge window,
    **5.93 of 11.0 hours (54%)** sat inside gaps longer than two minutes,
    with a largest gap of 30 minutes.

    Published rather than corrected, deliberately. The issue's own
    analysis rules out the obvious fix: zeroing stale periods is the
    conservative direction for THROUGHPUT (the participant path's choice,
    see `_stale_power_period_indices()`) but the wrong direction for a
    TRAJECTORY, because the home reconstruction already under-rises
    through charge and zeroing would deepen that -- making
    `soc_discrepancy` worse, and a household's EPR look less reliable,
    because of a fix.

    What this buys instead is the ability to tell two very different
    situations apart, which `soc_discrepancy` alone cannot:

        "the reconstruction disagrees with the real sensor"
        "the reconstruction had almost nothing to work with"

    Both currently surface as `soc_discrepancy_reason: disagreement`.

    Reported as raw, threshold-free quantities wherever possible --
    sample count, median gap, largest gap -- because the interesting
    threshold is not settled and baking one in would prejudge it. Two
    derived fractions are included because they are the ones a gate would
    plausibly use:

    `time_in_long_gaps_pct`  share of the window's SPAN sitting inside a
        gap longer than one grid period. Self-describing rather than
        arbitrary: a gap longer than one period guarantees at least one
        period had no sample of its own, so this is the fraction of the
        window whose power was held forward rather than observed.
    `stale_periods_pct`  share of periods `_stale_power_period_indices()`
        would call untrustworthy, i.e. under this repo's existing
        one-hour `MAX_SAMPLE_GAP_HOURS` guard. Deliberately the SAME
        measure the participant path acts on, so the two paths report a
        comparable number even though only one of them adjusts. Expect it
        to be far smaller than `time_in_long_gaps_pct` -- an hour is a
        very permissive guard, and the difference between the two
        fractions is precisely the coverage problem this issue is about.

    Returns None when there is nothing to measure (no window, or fewer
    than two samples, where "gap" is undefined) rather than a fabricated
    0.0 that would read as perfect coverage.
    """
    if not grid_times or not pts or len(pts) < 2:
        return None
    window_start = grid_times[0]
    window_end = grid_times[-1] + timedelta(hours=period_hours)
    span_s = (window_end - window_start).total_seconds()
    if span_s <= 0:
        return None

    # Only samples that bear on this window, in time order. A sample
    # before the window still matters -- it is what the first periods
    # hold forward FROM -- so the clamp is applied to the gap arithmetic
    # below rather than by discarding it here.
    times = sorted(t for t, _v in pts)
    period_s = period_hours * 3600.0
    gaps_s: list[float] = []
    long_gap_s = 0.0
    for earlier, later in itertools.pairwise(times):
        gap = (later - earlier).total_seconds()
        if gap <= 0:
            continue
        gaps_s.append(gap)
        if gap <= period_s:
            continue
        # Count only the part of the gap that lies inside the window.
        overlap_start = max(earlier, window_start)
        overlap_end = min(later, window_end)
        overlap = (overlap_end - overlap_start).total_seconds()
        if overlap > 0:
            long_gap_s += overlap
    if not gaps_s:
        return None
    ordered = sorted(gaps_s)
    mid = len(ordered) // 2
    median_s = (
        ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    )
    stale = _stale_power_period_indices(pts, grid_times, period_hours)
    return {
        "points": len(times),
        "median_gap_s": round(median_s, 1),
        "max_gap_s": round(max(gaps_s), 1),
        "time_in_long_gaps_pct": round(long_gap_s / span_s * 100.0, 2),
        "stale_periods_pct": round(len(stale) / len(grid_times) * 100.0, 2),
    }


def _stale_power_period_indices(
    pts: list[tuple[datetime, float]],
    grid_times: list[datetime],
    period_hours: float,
    max_gap_hours: float | None = None,
) -> frozenset[int]:
    """Which grid periods have no trustworthy power observation behind
    them (nimbus issue #1161).

    `resample_history_mean()` falls back to nearest-at-or-before for a
    period with no samples of its own, and documents that as deliberate:
    *"never fabricates a gap."* For a **state** (SoC, price) that is the
    physically correct model. For **power** it is not sample-and-hold at
    all -- it integrates a stale instantaneous reading across time nobody
    observed.

    Measured on this repo's own resample, 24 one-hour periods with
    samples present only 00:00-02:00 at 5.0 kW: every one of the
    remaining 21 periods came back 5.0 kW. A participant whose pack
    sensor stops writing rows after an early-morning discharge is
    credited 5 kW x 21 h = 105 kWh that never flowed -- 175% of a 60 kWh
    pack, every kWh of it then priced against real tariffs by
    `evaluate_realized_cost_multi()`.

    `load_run_state.py` already refuses exactly this for the live
    counter::

        if 0.0 < dt_hours <= MAX_SAMPLE_GAP_HOURS:
            delivered += power_kw * dt_hours

    so this reuses that constant rather than introducing a second
    number. One hour is the repo's existing answer to "how long may a
    power reading speak for", and having the retrospective scorer
    disagree with the live counter about it would be worse than either
    value.

    A period is trustworthy when EITHER it contains a real sample of its
    own, OR the most recent preceding sample is no older than
    `max_gap_hours` before the period starts -- a brief hold is what the
    resample is for and is not what this guards against.

    Returns the STALE indices (the complement), because that is what both
    callers want: one to zero, one to count.
    """
    if max_gap_hours is None:
        try:
            from . import load_run_state
        except ImportError:
            import load_run_state
        max_gap_hours = load_run_state.MAX_SAMPLE_GAP_HOURS

    if not grid_times:
        return frozenset()
    if not pts:
        # Every period unobserved. The participant path never reaches
        # here (an empty history excludes the participant outright), but
        # returning "all stale" rather than "none stale" keeps the
        # honest-absence posture if another caller ever does.
        return frozenset(range(len(grid_times)))

    ordered = sorted(pts, key=lambda row: row[0])
    times = [ts for ts, _ in ordered]
    stale: list[int] = []
    for i, gt in enumerate(grid_times):
        window_end = gt + timedelta(hours=period_hours)
        if any(gt <= ts < window_end for ts in times):
            continue
        preceding = [ts for ts in times if ts <= gt]
        if not preceding:
            # Before the first sample. Nothing was held forward here, so
            # the resample already returned its 0.0 default rather than
            # inventing energy -- but it is still unobserved, and a
            # reader judging the reconstruction deserves to see it.
            stale.append(i)
            continue
        if (gt - preceding[-1]).total_seconds() / 3600.0 > max_gap_hours:
            stale.append(i)
    return frozenset(stale)


def _resolve_battery_participant_history(
    *,
    day_start: datetime,
    day_end: datetime,
    grid_times: list[datetime],
    period_hours: float,
    n_periods: int,
) -> list[
    tuple[
        elements.BatteryConfig,
        np.ndarray,
        np.ndarray,
        float,
        list[tuple[datetime, float]],
    ]
]:
    """nimbus issue #768/#585 (Mark Purcell): the retrospective, HISTORY-
    based sibling of `build_extra_batteries()` just above -- same
    `battery_participant` subentries, but reconstructing each one's own
    REAL, already-elapsed [day_start, day_end) dispatch from recorder
    history, exactly the way `_compute_report_for_window()`'s own "home"
    battery reconstruction already does for the hub's single configured
    battery. This is what lets `compute_quality_report()` score the
    whole real storage fleet (home + EVs), not just the pack -- the gap
    solver_writer.py's own #585 comment already named explicitly:
    "integrating each battery_participant's own power sensor into its
    own SoC... is a substantially larger architectural piece,
    deliberately not attempted here." This function is that piece.

    Per Mark Purcell's own direct instruction (2026-09-12): reuses each
    participant's own ALREADY-CONFIGURED `battery_participant_power_
    sensor` (the same real sensor `build_extra_batteries()`'s own
    comment notes is "read live for future dashboard/monitoring wiring,
    not needed by BatteryConfig itself" for the forward solve) as the
    historic baseline -- no new sensor configuration needed, this is
    simply the first real consumer of a field that already existed.

    Returns one `(BatteryConfig, actual_charge_kw, actual_discharge_kw,
    final_soc_kwh_actual, soc_hist)` tuple per participant that has
    EVERYTHING this function needs to score it for real (capacity, SoC
    sensor, power sensor, and real non-empty history for both across the
    window) -- a participant missing any of these, or with a genuinely
    empty history for this specific day (e.g. an EV added to the wizard
    after this day already elapsed), is honestly SKIPPED for this one
    day's report, not treated as a hard failure of the whole report
    (mirrors this module's own "skip this cycle, retry later" discipline
    for the report as a whole, scoped down to one participant). Logged
    once (INFO) per (name, day) skip reason, not every solve cycle --

    `soc_hist` (nimbus issue #949, Mark Purcell's own chosen fix): the
    participant's raw, already-fetched real SoC recorder history
    (`[(datetime, pct), ...]`, same shape and 6h-lookback convention as
    the "home" battery's own `soc_hist` in `_compute_report_for_window()`
    just above), carried out rather than discarded once this function is
    done with it internally for `initial_pct`/`final_pct`. Every
    participant reaching `results.append()` below is guaranteed non-empty
    here -- the missing/empty-history skip above already excluded anyone
    who wouldn't be -- so a caller building a fleet-wide SoC comparison
    never has to re-derive an honest-absence case that was already
    resolved.
    this function only ever runs once per real calendar day being
    scored, so there is no log-spam risk to guard against the way the
    live per-solve gating above does.

    Bidirectional-flow gating (2026-09-13, Mark Purcell's own real,
    substantive catch): the pack-power sensor measures the PACK's own
    internal flow, which includes real propulsion discharge WHILE
    DRIVING -- energy that never touches the home's grid connection,
    solar, or the shared charger at all. Left ungated, that would get
    priced by evaluate_realized_cost_multi() as if it flowed into the
    household's own grid balance (a real trip's worth of discharge
    looking like a real grid export that never happened), corrupting
    j_ach/EPR the same way the #299 sign-convention bug and the #532
    shared-sensor double-count once did. Gated here using the SAME
    `battery_participant_available_entity` the live forward solve
    already uses for scheduling (#563/#779) -- its own HISTORY for this
    exact day (not live state) zeroes both `actual_charge_kw` and
    `actual_discharge_kw` for any period the car was away, so only
    genuine at-home flow is ever priced against the grid.

    RESOLVED 2026-09-14 (nimbus issue #467, Mark Purcell: "Build an
    internal gating gap please"). This function used to mask the
    RECONSTRUCTION side only -- the oracle re-solve had no equivalent
    per-period gate, because `BatteryConfig` could only express a single
    contiguous `unavailable_until_period_index` PREFIX, so a day with
    more than one separate trip (a morning school run AND a separate
    evening trip) let the oracle unrealistically assume the EV was home
    to charge/discharge during a real away window -- inflating the
    oracle's own achievable cost floor and therefore overstating that
    participant's regret.

    `BatteryConfig.unavailable_period_indices` (#467) is now a genuine
    per-period mask, and this function hands it exactly the same
    `is_home_mask` it uses to zero the reconstruction arrays. Both
    halves of the scorer now agree about where the car was, on any
    number of trips per day.

    Deliberately NOT handled here either (see regret.py's own
    `oracle_dispatch()` docstring, "Still deliberately NOT extended"): a
    participant whose ONLY power signal is a sensor SHARED with another
    participant (this household's own Sigen DC charger, serving both
    the Model 3 and Model Y) is scored using that shared reading as-is,
    honestly wrong on any day both EVs actually used it --
    disambiguating that is a real, separate, larger piece of work, not
    attempted in this pass.

    Native/in-process mode ONLY, same reasoning and same graceful `[]`
    fallback as `build_extra_batteries()` -- a standalone/cron
    deployment has no subentries at all, so `compute_quality_report()`
    always scores exactly the "home" battery there, unchanged from
    before this function existed.
    """
    if _NATIVE_HASS is None:
        return []
    try:
        from .const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
            CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
            CONF_BATTERY_PARTICIPANT_NAME,
            CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
            CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            DOMAIN,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )
    except ImportError:
        from const import (
            CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY,
            CONF_BATTERY_PARTICIPANT_CAPACITY_KWH,
            CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH,
            CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR,
            CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT,
            CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW,
            CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT,
            CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT,
            CONF_BATTERY_PARTICIPANT_NAME,
            CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE,
            CONF_BATTERY_PARTICIPANT_POWER_SENSOR,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP,
            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW,
            CONF_BATTERY_PARTICIPANT_SOC_SENSOR,
            DOMAIN,
            SUBENTRY_TYPE_BATTERY_PARTICIPANT,
        )

    entries = _NATIVE_HASS.config_entries.async_entries(DOMAIN)
    if not entries:
        return []

    results: list[tuple[elements.BatteryConfig, np.ndarray, np.ndarray, float]] = []
    for subentry in entries[0].subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_BATTERY_PARTICIPANT:
            continue
        data = subentry.data
        name = data.get(CONF_BATTERY_PARTICIPANT_NAME) or subentry.subentry_id
        capacity_kwh = float(data.get(CONF_BATTERY_PARTICIPANT_CAPACITY_KWH) or 0.0)
        power_sensor = data.get(CONF_BATTERY_PARTICIPANT_POWER_SENSOR)
        soc_sensor = data.get(CONF_BATTERY_PARTICIPANT_SOC_SENSOR)
        if capacity_kwh <= 0.0 or not soc_sensor or not power_sensor:
            _LOGGER.info(
                "Nimbus quality: battery participant '%s' has no power "
                "sensor/SoC sensor/capacity configured -- excluded from "
                "this day's multi-battery score (this participant simply "
                "isn't scored, the rest of the fleet is unaffected)",
                name,
            )
            continue

        # nimbus issue #843 (option A): per-row unit scaling, scoped to
        # exactly this one fetch. See fetch_entity_power_history_kw()'s
        # own docstring for why this is a separate function and why every
        # other history read in this file deliberately keeps the cheaper
        # attribute-stripped path.
        power_hist = fetch_entity_power_history_kw(power_sensor, day_start, day_end)
        soc_hist = fetch_entity_history_range(
            soc_sensor, day_start - timedelta(hours=6), day_end
        )
        if not power_hist or not soc_hist:
            _LOGGER.info(
                "Nimbus quality: battery participant '%s' has no real "
                "history for window [%s, %s] (power=%d, soc=%d rows) -- "
                "excluded from this day's multi-battery score",
                name,
                day_start.isoformat(),
                day_end.isoformat(),
                len(power_hist),
                len(soc_hist),
            )
            continue

        try:
            # nimbus issue #843 (option A): rows arrive already
            # normalised to kW by fetch_entity_power_history_kw(), using
            # each sample's OWN recorded unit -- so there is no whole-
            # window scale left to apply here. _kw_scale_factor() itself
            # is untouched and still correct for every other caller, none
            # of which fetches per-row units.
            power_scale = 1.0
            # Same sign convention as the home battery's own
            # solver_battery_power_positive_is_charge (nimbus #299):
            # internally, positive net_kw always means DISCHARGE. A
            # participant whose sensor reports positive=charge needs the
            # same -1.0 flip before this function's own charge/discharge
            # split below can treat every reading the same way.
            sign = (
                -1.0
                if data.get(CONF_BATTERY_PARTICIPANT_POWER_POSITIVE_IS_CHARGE)
                else 1.0
            )
            # nimbus issue #843 (option B, Mark Purcell's own steer): a
            # cheap always-on guard so no single corrupt reading can ever
            # publish a physically impossible figure again. Judged against
            # THIS participant's own configured envelope -- read here
            # rather than reusing the BatteryConfig construction further
            # down, because the filter has to run on the raw history
            # before it is resampled (see the helper's own docstring for
            # why upstream and why discard rather than clamp).
            max_plausible_kw = (
                max(
                    float(data.get(CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW) or 0.0),
                    float(data.get(CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW) or 0.0),
                )
                * _PARTICIPANT_POWER_IMPLAUSIBLE_MULTIPLE
            )
            power_hist = _drop_implausible_power_samples(
                power_hist,
                power_scale=power_scale,
                max_plausible_kw=max_plausible_kw,
                participant_name=str(name),
                power_sensor=str(power_sensor),
            )
            net_kw = np.array(
                [
                    v * power_scale * sign
                    for v in resample_history_mean(power_hist, grid_times, period_hours)
                ]
            )
            # nimbus issue #1161: refuse to credit throughput across a
            # recorder gap the resample merely held a stale reading over.
            #
            # `resample_history_mean()` carries the last sample forward
            # indefinitely -- correct for a STATE, and energy-fabricating
            # for POWER. A participant whose pack sensor stops writing
            # rows mid-window was otherwise credited its last reading
            # multiplied by every remaining hour of the day, then priced
            # against real tariffs. `load_run_state.py`'s live counter
            # has refused exactly this since it was written; the
            # retrospective scorer never inherited the guard.
            #
            # Zeroing (rather than holding) is the conservative
            # direction, and it is the one this repo already chose for
            # the live counter -- crediting nothing for time nobody
            # observed. The cost is that a real, unrecorded dispatch goes
            # uncounted, which is why the fraction is published rather
            # than adjusted silently (see `stale_history_period_indices`
            # on BatteryConfig).
            stale_period_indices = _stale_power_period_indices(
                power_hist, grid_times, period_hours
            )
            if stale_period_indices:
                stale_mask = np.zeros(len(net_kw), dtype=bool)
                stale_mask[list(stale_period_indices)] = True
                net_kw = np.where(stale_mask, 0.0, net_kw)
                _LOGGER.info(
                    "Nimbus quality: battery participant '%s' has no recorded "
                    "power for %d of %d periods (gaps longer than the "
                    "one-hour sample guard) -- those periods contribute no "
                    "throughput rather than holding the last reading",
                    name,
                    len(stale_period_indices),
                    len(grid_times),
                )
            actual_charge_kw = np.array([max(0.0, -v) for v in net_kw])
            actual_discharge_kw = np.array([max(0.0, v) for v in net_kw])

            # nimbus issue #768 (Mark Purcell, 2026-09-13, real catch):
            # the pack-power sensor also measures real propulsion
            # discharge WHILE DRIVING -- energy that never touches the
            # home's grid connection at all. Left in, a real trip would
            # get priced by evaluate_realized_cost_multi() as if it
            # exported into the household's own grid balance. Gated
            # using the SAME battery_participant_available_entity the
            # live forward solve already uses for scheduling (#563/
            # #779), read as HISTORY for this exact day rather than live
            # state -- zeroes both charge/discharge for any period the
            # car was away. See this function's own docstring for the
            # accepted multi-trip-day limitation this simple mask
            # carries (a real, separate, larger oracle-side fix, not
            # attempted here).
            # nimbus issue #467: the away-window mask computed just below
            # is now ALSO handed to the oracle via BatteryConfig, not only
            # used to zero the reconstruction arrays. None means "no
            # availability entity configured", which stays a real no-op.
            away_period_indices: frozenset[int] | None = None
            available_entity = data.get(CONF_BATTERY_PARTICIPANT_AVAILABLE_ENTITY)
            if available_entity:
                home_hist = fetch_entity_state_history_range(
                    available_entity, day_start, day_end
                )
                # Conservative default -- no real history at all for
                # this entity means "assume away" (0.0), same "if we
                # can't confirm the car/resource is really there, don't
                # count it" posture #563 item 2 already established for
                # the live solve's own availability gate.
                # nimbus issue #843, second real instance of the same
                # defect: this default=0.0 "assume away" intent was
                # silently defeated before that fix, because the helper
                # always initialised to pts[0][1]. A car whose FIRST
                # binary_sensor row of the day read "on" therefore had
                # every period before it masked as home -- the exact
                # opposite of the conservative posture stated above.
                # Now genuinely honoured (backfill_first stays False).
                home_numeric = [(t, 1.0 if v == "on" else 0.0) for t, v in home_hist]
                is_home = np.array(
                    resample_history_nearest(home_numeric, grid_times, default=0.0)
                )
                is_home_mask = is_home >= 0.5
                actual_charge_kw = np.where(is_home_mask, actual_charge_kw, 0.0)
                actual_discharge_kw = np.where(is_home_mask, actual_discharge_kw, 0.0)
                # nimbus issue #467: the SAME mask, in the shape the LP
                # takes. Previously this was computed, used to zero the
                # two reconstruction arrays above, and then thrown away --
                # so compute_quality_report()'s own oracle re-solve had no
                # idea the car had ever left, and was free to schedule
                # charge/discharge during a real away window. On a
                # multi-trip day that inflated the oracle's achievable
                # floor and therefore overstated this participant's regret.
                away_period_indices = frozenset(
                    int(i) for i in np.flatnonzero(~is_home_mask)
                )

            min_soc_pct = float(
                data.get(CONF_BATTERY_PARTICIPANT_MIN_SOC_PERCENT) or 0.0
            )
            max_soc_pct = float(
                data.get(CONF_BATTERY_PARTICIPANT_MAX_SOC_PERCENT) or 100.0
            )
            # nimbus issue #843: backfill_first=True is load-bearing for
            # exactly the EV this bug was found on. soc_hist is fetched
            # with a 6h lookback so a real preceding sample usually
            # exists -- but a car that sleeps through the whole buffer
            # (real Tesla behaviour) has none, and its SoC genuinely did
            # NOT change while parked. Backfilling its first real reading
            # is physically right; falling through to min_soc_pct would
            # newly corrupt this participant's own scored trajectory.
            initial_pct = resample_history_nearest(
                soc_hist, [day_start], default=min_soc_pct, backfill_first=True
            )[0]
            final_pct = resample_history_nearest(
                soc_hist,
                [day_end - timedelta(seconds=1)],
                default=initial_pct,
                backfill_first=True,
            )[0]
            # Physical-range clamp only (same reasoning as the home
            # battery's own #325/#327/#328 history -- a real installed
            # BatteryConfig must never crash this report on sensor
            # nonsense), not the home battery's own full scheduling-
            # envelope WARNING treatment -- a single participant's own
            # noisy sensor is honestly logged and skipped above already
            # if history is entirely missing; a physically-valid but
            # out-of-schedule reading (e.g. an EV parked below its own
            # configured floor) is scored as-is, same as the live solve
            # treats it as a soft preference, not a hard error.
            initial_soc_kwh = min(
                max(capacity_kwh * initial_pct / 100.0, 0.0), capacity_kwh
            )
            final_soc_kwh_actual = min(
                max(capacity_kwh * final_pct / 100.0, 0.0), capacity_kwh
            )
            min_soc_kwh = capacity_kwh * min_soc_pct / 100.0
            max_soc_kwh = capacity_kwh * max_soc_pct / 100.0
            efficiency = (
                min(
                    float(data.get(CONF_BATTERY_PARTICIPANT_EFFICIENCY_PERCENT) or 95.0)
                    / 100.0,
                    0.999,
                )
                ** 0.5
            )
            # nimbus issue #1111: computed here rather than inline in the
            # constructor because it needs the achieved trajectory (to
            # widen against) as well as the config, and the widening rule
            # is the substance rather than a detail.
            _must_have_idx, _must_have_kwh = _participant_departure_deadline(
                grid_times=grid_times,
                period_hours=period_hours,
                departure_hour=data.get(CONF_BATTERY_PARTICIPANT_DEPARTURE_HOUR),
                must_have_soc_pct=data.get(
                    CONF_BATTERY_PARTICIPANT_MUST_HAVE_SOC_BY_DEPARTURE_PERCENT
                ),
                capacity_kwh=capacity_kwh,
                initial_soc_kwh=initial_soc_kwh,
                actual_charge_kw=actual_charge_kw,
                actual_discharge_kw=actual_discharge_kw,
                efficiency=efficiency,
            )
            battery_cfg = elements.BatteryConfig(
                name=str(name),
                capacity_kwh=capacity_kwh,
                initial_soc_kwh=initial_soc_kwh,
                min_soc_kwh=min_soc_kwh,
                max_soc_kwh=max_soc_kwh,
                max_charge_kw=float(
                    data.get(CONF_BATTERY_PARTICIPANT_MAX_CHARGE_KW) or 0.0
                ),
                max_discharge_kw=float(
                    data.get(CONF_BATTERY_PARTICIPANT_MAX_DISCHARGE_KW) or 0.0
                ),
                charge_efficiency=efficiency,
                discharge_efficiency=efficiency,
                # nimbus issue #1111: the departure deadline, the second
                # field this function did not carry while its live-solve
                # sibling did. Widened to what the day actually reached --
                # see _participant_departure_deadline().
                must_have_soc_by_period_index=_must_have_idx,
                must_have_soc_kwh=_must_have_kwh,
                # nimbus issue #1109: the shared-charger cap, which this
                # function did NOT carry while its live-solve sibling
                # build_extra_batteries() did. #768 lists exactly this as
                # a prerequisite -- "an oracle free to charge both EVs on
                # one 25 kW charger simultaneously ... would overstate
                # achievable value" -- and the availability half (#467,
                # just below) made the trip while this half did not.
                #
                # Left at the CONFIGURED value here and widened after the
                # loop, once every participant's achieved charge array
                # exists: the widening is a property of the group, not of
                # any one member, so it cannot be computed yet.
                shared_charger_group=(
                    data.get(CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_GROUP) or None
                ),
                shared_charger_max_kw=(
                    float(shared_charger_max_kw_raw)
                    if (
                        shared_charger_max_kw_raw := data.get(
                            CONF_BATTERY_PARTICIPANT_SHARED_CHARGER_MAX_KW
                        )
                    )
                    is not None
                    else None
                ),
                # nimbus issue #467: the real per-period away-window mask
                # built above, so the oracle re-solve is gated by exactly
                # the same windows the reconstruction arrays were masked
                # by. `available` stays True -- this is a per-period gate
                # for an already-elapsed day, not a whole-horizon "this
                # car is away right now" statement.
                unavailable_period_indices=away_period_indices,
                # nimbus issue #1161: diagnostic only -- network.py
                # never reads this. Carried on the config for the same
                # reason #1098's away mask is: the quality report is
                # handed these configs already, so the fraction of the
                # window that was unobserved arrives without new
                # plumbing.
                stale_history_period_indices=(stale_period_indices or None),
                # Same reference constants build_extra_batteries() uses
                # for the live forward solve -- no wizard field for a
                # real per-participant $/kWh cost exists yet (see that
                # function's own comment for why 0.0/0.0 isn't a valid
                # no-op here).
                charge_cost=_DEFAULT_EXTRA_BATTERY_CHARGE_COST,
                discharge_cost=_DEFAULT_EXTRA_BATTERY_DISCHARGE_COST,
                # Stripped to 0.0/None regardless by compute_quality_
                # report()'s own battery_scoring pass (this is an
                # already-elapsed day, crediting leftover SoC is a guess
                # about tomorrow this scorer has no basis for) -- set
                # honestly here rather than left at a nonzero default
                # that would only ever be silently discarded downstream.
                salvage_value=0.0,
                degradation_cost_per_kwh=float(
                    data.get(CONF_BATTERY_PARTICIPANT_DEGRADATION_COST_PER_KWH) or 0.0
                ),
            )
        except (ValueError, TypeError) as e:
            # A bad/inconsistent config for ONE participant (e.g. a
            # physically-impossible min/max SoC pair) must never take
            # down the whole day's report -- skip just this participant,
            # same "the rest of the fleet is unaffected" posture as the
            # missing-sensor branch above.
            _LOGGER.warning(
                "Nimbus quality: battery participant '%s' could not be "
                "scored for window [%s, %s] (%s) -- excluded from this "
                "day's multi-battery score",
                name,
                day_start.isoformat(),
                day_end.isoformat(),
                e,
            )
            continue

        results.append(
            (
                battery_cfg,
                actual_charge_kw,
                actual_discharge_kw,
                final_soc_kwh_actual,
                soc_hist,
            )
        )
    return _widen_shared_charger_cap_to_achieved(results)


def _participant_departure_deadline(
    *,
    grid_times: list,
    period_hours: list,
    departure_hour: object,
    must_have_soc_pct: object,
    capacity_kwh: float,
    initial_soc_kwh: float,
    actual_charge_kw: np.ndarray,
    actual_discharge_kw: np.ndarray,
    efficiency: float,
) -> tuple[int | None, float | None]:
    """A participant's departure deadline for the SCORED window, widened
    to what the day actually reached (nimbus issue #1111).

    `build_extra_batteries()` sets `must_have_soc_by_period_index` /
    `must_have_soc_kwh` for the live solve; its history-based sibling
    never did. `elements.py` is explicit that this is a **hard**
    constraint -- *"a real EV genuinely needs a real SoC by a real time,
    not a priced preference"* -- so an oracle without it is free to leave
    the car empty at departure and arbitrage overnight instead, banking a
    trade the household could never have accepted. That makes `j_star`
    better than achievable, `regret_dollars` overstated and EPR
    understated, exactly as #1109 did through the shared-charger cap.

    **Widened, for the same reason and by the same rule as #1109.** A
    constraint narrows the oracle, and #956 is the record of what that
    costs when the achieved trajectory falls outside the narrowed set.
    Here the failure is ordinary rather than exotic: a household that
    simply did not plug the car in, or plugged it in late, has a day that
    misses its own departure target. Binding the oracle to a target the
    real day missed would put the achieved trajectory outside the
    oracle's feasible set on precisely those days.

    So the returned requirement is `min(configured, the SoC the day
    actually reached by that period)` -- as demanding as the day itself
    and no more. On a day that met its target this is the configured
    value and nothing changes.

    Returns `(None, None)` when the pair is not configured, when only one
    half is set (matching the live path's own "treat as neither"), or
    when the departure hour does not occur in this window -- all real,
    expected cases rather than errors.
    """
    if departure_hour is None or must_have_soc_pct is None:
        return None, None
    idx: int | None = None
    for i, start in enumerate(grid_times):
        if _local(start).hour == int(departure_hour):
            idx = i
            break
    if idx is None:
        # The hour does not occur in this scored window. Same posture the
        # live path takes for a short horizon: a no-op, not an error.
        return None, None

    configured_kwh = capacity_kwh * float(must_have_soc_pct) / 100.0

    # The achieved trajectory's own SoC at that period, integrated the
    # same way the reconstruction elsewhere does: charge credited at
    # efficiency, discharge debited by it.
    soc = float(initial_soc_kwh)
    for t in range(min(idx, len(actual_charge_kw))):
        hours = float(period_hours[t]) if t < len(period_hours) else 0.0
        soc += float(actual_charge_kw[t]) * efficiency * hours
        soc -= float(actual_discharge_kw[t]) / efficiency * hours

    return idx, min(configured_kwh, max(0.0, soc))


def _widen_shared_charger_cap_to_achieved(
    results: list[
        tuple[
            elements.BatteryConfig,
            np.ndarray,
            np.ndarray,
            float,
            list[tuple[datetime, float]],
        ]
    ],
) -> list[
    tuple[
        elements.BatteryConfig,
        np.ndarray,
        np.ndarray,
        float,
        list[tuple[datetime, float]],
    ]
]:
    """Each shared-charger group's cap, relaxed to contain what that group
    demonstrably drew (nimbus issue #1109).

    **Why the cap is not simply passed through.** Adding it at all is the
    fix -- the scorer's oracle could charge every member of a group at its
    own `max_charge_kw` simultaneously, which the hardware cannot do, so
    `j_star` was better than achievable and regret was overstated. But a
    constraint NARROWS the oracle's feasible set, and #956 is the record of
    what that costs when the achieved trajectory falls outside it: the
    achieved side prices out cheaper than optimal, regret goes negative,
    and the comparison is invalid rather than merely imprecise.

    The household settled that in #956 -- "go with B", widen the oracle to
    contain the achieved trajectory rather than clamp the achieved
    integration to fit the oracle -- and this applies the same decision to
    the same class of problem rather than making a new one. Same shape as
    `quality_report.py`'s own `_widen_export_pin_to_achieved()` does for
    the P2P export pin.

    **Exactly as wide as the day itself, and no wider.** The widened cap is
    `max(configured, the largest simultaneous draw the group actually
    made)` -- where "draw" is charge **plus** discharge, matching the LP
    constraint's own shape exactly (nimbus issue #1140; a shared
    connector's capacity is directional-agnostic). On a well-behaved day nothing changes: real draw stays under
    the cap, the max is the configured value, and the result is
    byte-identical to passing it straight through. It only moves when the
    real day already exceeded the configured number -- a cap set to
    nameplate while the charger briefly ran above it, a meter
    disagreement, or simply a misconfiguration -- which is precisely the
    case that would otherwise reintroduce #956.

    A group of one is left alone: `shared_charger_max_kw` on a single
    participant is just a second `max_charge_kw`, and widening it would
    quietly relax a real per-battery limit.
    """
    groups: dict[str, float] = {}
    for cfg, charge_kw, discharge_kw, _final, _soc_hist in results:
        group = cfg.shared_charger_group
        if not group or cfg.shared_charger_max_kw is None:
            continue
        # Charge AND discharge, because the LP constraint this widens
        # against sums both into one ceiling (nimbus issue #1140):
        # `shared_charger_{group}_t{t}` in network.py adds
        # charge_vars[m][t] + discharge_vars[m][t] for every member, which
        # is physically right -- one connector's throughput is bounded
        # regardless of direction, so a V2H participant discharging draws
        # from the same shared capacity as one charging. Summing charge
        # alone made a discharging member invisible to the widening, so
        # the cap could stay narrower than the real combined draw and
        # reintroduce #956's negative-regret failure for that case.
        arr = np.asarray(charge_kw, dtype=np.float64) + np.asarray(
            discharge_kw, dtype=np.float64
        )
        if group in groups:
            groups[group] = groups[group] + arr  # type: ignore[assignment]
        else:
            groups[group] = arr  # type: ignore[assignment]

    members: dict[str, int] = {}
    for cfg, _c, _d, _f, _sh in results:
        if cfg.shared_charger_group:
            members[cfg.shared_charger_group] = (
                members.get(cfg.shared_charger_group, 0) + 1
            )

    widened: list[
        tuple[
            elements.BatteryConfig,
            np.ndarray,
            np.ndarray,
            float,
            list[tuple[datetime, float]],
        ]
    ] = []
    for cfg, charge_kw, discharge_kw, final, soc_hist in results:
        group = cfg.shared_charger_group
        if (
            not group
            or cfg.shared_charger_max_kw is None
            or members.get(group, 0) < 2  # a group of one is a per-battery limit
        ):
            widened.append((cfg, charge_kw, discharge_kw, final, soc_hist))
            continue
        achieved_peak = float(np.max(np.asarray(groups[group], dtype=np.float64)))
        relaxed = max(float(cfg.shared_charger_max_kw), achieved_peak)
        if relaxed > float(cfg.shared_charger_max_kw):
            _LOGGER.debug(
                "Nimbus quality (#1109): shared charger group %r drew %.3f kW "
                "at peak against a configured cap of %.3f kW -- widening the "
                "oracle's cap to the achieved peak so the achieved trajectory "
                "stays inside the oracle's feasible set (#956)",
                group,
                achieved_peak,
                float(cfg.shared_charger_max_kw),
            )
        widened.append(
            (
                replace(cfg, shared_charger_max_kw=relaxed),
                charge_kw,
                discharge_kw,
                final,
                soc_hist,
            )
        )
    return widened


async def dispatch_commanded_state(
    hass,
    entity_id: str,
    commanded_state: bool,
    climate_on_hvac_mode: str | None = None,
) -> None:
    """nimbus issue #476/#534: the real output/actuation layer -- the
    piece #484's own docstring flagged as missing ("this bookkeeping has
    no consumer yet... no sensor exposes commanded_state today"). Calls
    the right HA service for `entity_id`'s own domain, decided by a plain
    string split on the entity_id itself (no config field needed for
    "which domain is this" -- HA entity_ids are already domain-prefixed).

    switch.*: turn_on/turn_off, unconditional -- no per-domain nuance.

    water_heater.*: set_operation_mode("performance"/"eco"), per #534
    item 3's own real device investigation (a DIY SG-Ready bridge whose
    two modes are exactly these two literal strings) -- deliberately NOT
    a per-load configurable mode-string pair in this pass; every real
    water_heater device #534 investigated uses this exact convention, and
    adding a speculative override for hardware nobody has yet would be
    guessing ahead of a real need.

    climate.* (nimbus issue #756, follow-up to #534): set_hvac_mode --
    "off" unconditionally when commanded_state is False (universally
    present in HA's own HVACMode enum, zero ambiguity), and
    `climate_on_hvac_mode` when True. Deliberately NOT climate.turn_on/
    turn_off: checked live against a real reference-household climate
    entity, its own `supported_features` did not advertise the
    TURN_ON/TURN_OFF capability bits, so those services are not a safe
    universal substitute. Deliberately NOT a guessed single "on" mode
    either -- the same real entity's own `hvac_modes` included both
    `heat` and `cool`, so there is no safe universal default; see
    CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE's own const.py comment.
    A climate device_entity with `climate_on_hvac_mode` left unset logs a
    WARNING and does not dispatch on an ON transition (never guesses) --
    OFF transitions are unaffected, since "off" needs no configured mode.

    An unrecognized domain (anything other than switch/water_heater/
    climate) logs a WARNING and is a safe no-op, never a crash -- a
    household who configures a device_entity in an unsupported domain
    finds out from the log, not from a silently-ignored command.

    Raises on a genuine service-call failure (a bad entity_id, the
    service unavailable) -- the caller (apply_commanded_state_guard())
    wraps each load's own dispatch in its own try/except so one load's
    failure never blocks another's, matching this file's established
    best-effort posture elsewhere.
    """
    domain = entity_id.split(".", 1)[0]
    if domain == "switch":
        service = "turn_on" if commanded_state else "turn_off"
        await hass.services.async_call(
            "switch", service, {"entity_id": entity_id}, blocking=False
        )
    elif domain == "water_heater":
        mode = "performance" if commanded_state else "eco"
        await hass.services.async_call(
            "water_heater",
            "set_operation_mode",
            {"entity_id": entity_id, "operation_mode": mode},
            blocking=False,
        )
    elif domain == "climate":
        if commanded_state:
            if not climate_on_hvac_mode:
                _LOGGER.warning(
                    "Nimbus: controllable load device entity '%s' is a "
                    "climate entity with no configured ON hvac_mode "
                    "(CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE) -- not "
                    "dispatched (#756). Configure one in the Controllable "
                    "Load wizard to enable ON commands for this device.",
                    entity_id,
                )
                return
            hvac_mode = climate_on_hvac_mode
        else:
            hvac_mode = "off"
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": hvac_mode},
            blocking=False,
        )
    else:
        _LOGGER.warning(
            "Nimbus: controllable load device entity '%s' has an unsupported "
            "domain '%s' for dispatch -- switch, water_heater, and climate "
            "are supported today",
            entity_id,
            domain,
        )


def _resolve_reaffirm_after_seconds(data: dict) -> float | None:
    """How long this load may be seen disagreeing with its own command
    before Nimbus re-sends it (nimbus issue #875).

    Per load, not global -- the household's own reasoning on #769, which
    applies identically here: "each load can have its own urgency."

    Returns None when nothing is configured, leaving the default to
    load_run_state.reaffirm_allowed() itself rather than resolving it
    here -- the default belongs beside the behaviour it governs, and
    this keeps the helper free of any cross-module import.

    **0 disables re-sending for this load**, restoring exactly the
    pre-#875 edge-triggered-only behaviour, which is the escape hatch
    for a device that must never be commanded twice. A negative or
    unparseable value is treated as unset rather than as 0, so a typo
    fails towards the working default instead of silently switching the
    feature off.
    """
    # Dual-mode, and NOT optional. A relative-only import looked safe
    # here -- this helper is reachable only from apply_commanded_state_
    # guard(), which is native-only -- and that reasoning is wrong: the
    # test harness imports solver_writer as a BARE module and calls that
    # guard directly. A relative import then raises ImportError, which
    # the guard's own whole-function handler swallows at DEBUG, aborting
    # every dispatch for that cycle silently. Caught by three existing
    # #741/#484 tests going red, which is exactly what they are for.
    try:
        from .const import CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES as _KEY
    except ImportError:  # bare-module import shape
        from const import CONF_CONTROLLABLE_LOAD_REAFFIRM_AFTER_MINUTES as _KEY

    raw = data.get(_KEY)
    if raw is None:
        return None
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        return None
    if minutes < 0:
        return None
    return minutes * 60.0


def apply_commanded_state_guard(
    plan: network.Plan,
    now: datetime,
    grid_times: list[datetime],
    period_hours_arr: NDArray[np.float64] | None = None,
    import_price_arr: NDArray[np.float64] | list[float] | None = None,
    tariff_attributed_cost_by_subentry: dict[str, float] | None = None,
    cfg: dict | None = None,
) -> None:
    """nimbus issue #484/#534: the relay-chatter guard, now with a real
    output stage. Reads each Controllable Load's own real, just-solved
    period-0 power off `plan` (its `subentry_id`, threaded through from
    build_controllable_loads()'s own config objects via elements.py/
    network.py), decides the raw new commanded state (on if period-0
    power exceeds the same on-threshold load_run_state.py's own power
    sampling uses, for a consistent on/off reading between the measured
    and the commanded side), persists the DEBOUNCED result via
    load_run_state.decide_commanded_state() -- see that function's own
    docstring for the actual guarantee -- and, when that subentry has a
    configured CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY AND commanded_state
    just genuinely CHANGED, actually dispatches it via
    dispatch_commanded_state() above. A load with no device entity
    configured is scored/persisted exactly as before this issue -- never
    physically commanded, a real no-op.

    Per-load min_hold_minutes (CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES)
    overrides the shared DEFAULT_MIN_HYSTERESIS_PERIODS-based debounce for
    just that load when set. A real ON dispatch is additionally gated by
    load_run_state.activation_allowed() against CONF_CONTROLLABLE_LOAD_
    MAX_ACTIVATIONS_PER_DAY -- when the cap is already reached, the
    solver's own desired commanded_state is still persisted (so a
    consumer honestly sees "wants ON, capped" rather than a silent lie),
    but no real service call is issued this cycle; record_activation()
    only increments on an actual, successful dispatch, never a blocked
    one. An OFF transition is never capped -- #534's own cap is
    specifically on "performance activations", not on releasing a load.

    nimbus issue #581 (Mark Purcell, real use the day after #578/#579
    shipped): also now persists each load's own FULL per-period plan
    (not just period 0) -- the household's own real ask was "chart the
    day-ahead plan for the heat pump," and the LP already computes this
    every cycle, it was simply being discarded. See LoadRunState's own
    plan_forecast/plan_delivered_kwh_forecast/plan_target_kwh/plan_
    shortfall_kwh/plan_earliest_period/plan_deadline_period/plan_
    nominal_kw fields (load_run_state.py) for exactly what's persisted
    per kind. This refreshes every solve cycle regardless of whether
    commanded_state itself changed (a fresh day-ahead schedule is the
    whole point), unlike the change-gated dispatch/write logic above --
    NimbusControllableLoadStateSensor (sensor.py) excludes these fields
    from long-term recorder history via _unrecorded_attributes (same
    #362 "churns every poll, not worth recording" reasoning already
    applied to NimbusHealthReportSensor's own generated_at/subentry_
    status), so this does not create #362's own class of recorder-churn
    bug.

    Best-effort and silent on any WHOLE-FUNCTION failure (a device_entity
    dispatch failure for one load is caught per-load below and does not
    escalate here) -- same posture as _sample_load_run_state(). Native
    mode only, same reasoning as build_controllable_loads() itself -- a
    no-op when _NATIVE_HASS is None (standalone/cron mode, or
    plan.sheddable_loads/adequacy_loads are always empty there anyway
    since build_controllable_loads() already returns ([], [])
    unconditionally in that mode).

    `cfg` (nimbus issue #481, optional -- omitted is a complete no-op,
    identical to every pre-#481 caller): the same solver config dict
    main() already threads into publish_plan()/publish_weather_forecast_
    mirrors(). Read here only for CONF_SOLVER_WEATHER_FORECAST_SENSOR --
    when configured, a thermal-forecast-eligible load also learns and
    applies the ambient-scaled loss coefficient (thermal_forecast.py's
    own learn_thermal_rates()/project_temperature_forecast() ambient
    parameters) instead of relying solely on the flat idle-decay rate;
    when not configured (or `cfg` itself is None), every thermal-forecast
    call site here behaves exactly as before this issue.
    """
    if _NATIVE_HASS is None or len(grid_times) < 2:
        return
    entries_with_ids = (
        [
            (
                sl.subentry_id,
                sl.served_kw[0] if len(sl.served_kw) else 0.0,
                "sheddable",
                sl,
            )
            for sl in plan.sheddable_loads
            if sl.subentry_id is not None
        ]
        + [
            (
                al.subentry_id,
                al.power_kw[0] if len(al.power_kw) else 0.0,
                "adequacy",
                al,
            )
            for al in plan.adequacy_loads
            if al.subentry_id is not None
        ]
        + [
            # nimbus issue #774: same shape as the adequacy entries above --
            # ThermalLoadPlan.power_kw is this load's own real driving
            # decision variable, the same role AdequacyLoadPlan.power_kw
            # plays for the debounce/dispatch logic below. getattr (not a
            # direct plan.thermal_loads read): this file's own bare-
            # SimpleNamespace test fakes predate this field, same
            # defensive posture as every other optional-attribute read on
            # `plan` in this function (see _raw_shadow_price_series()'s
            # own comment for the precedent).
            (tl.subentry_id, tl.power_kw[0] if len(tl.power_kw) else 0.0, "thermal", tl)
            for tl in getattr(plan, "thermal_loads", [])
            if tl.subentry_id is not None
        ]
    )
    if not entries_with_ids:
        return
    try:
        from homeassistant.helpers.storage import Store as _Store

        try:
            from . import done_condition, load_run_state, thermal_forecast
            from .const import (
                CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE,
                CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
                CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
                CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
                CONF_DEFERRABLE_DEADLINE_HOUR,
                CONF_DEFERRABLE_DONE_ENTITY,
                CONF_DEFERRABLE_DONE_WHEN,
                CONF_DEFERRABLE_EARLIEST_HOUR,
                CONF_DEFERRABLE_MAX_POWER_KW,
                CONF_DEFERRABLE_TARGET_KWH,
                CONF_SHEDDABLE_NOMINAL_KW,
                CONF_THERMAL_DEADLINE_HOUR,
                CONF_THERMAL_EARLIEST_HOUR,
                DOMAIN,
            )
        except ImportError:
            import done_condition
            import load_run_state
            import thermal_forecast
            from const import (
                CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE,
                CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY,
                CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY,
                CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES,
                CONF_DEFERRABLE_DEADLINE_HOUR,
                CONF_DEFERRABLE_DONE_ENTITY,
                CONF_DEFERRABLE_DONE_WHEN,
                CONF_DEFERRABLE_EARLIEST_HOUR,
                CONF_DEFERRABLE_MAX_POWER_KW,
                CONF_DEFERRABLE_TARGET_KWH,
                CONF_SHEDDABLE_NOMINAL_KW,
                CONF_THERMAL_DEADLINE_HOUR,
                CONF_THERMAL_EARLIEST_HOUR,
                DOMAIN,
            )

        entries = _NATIVE_HASS.config_entries.async_entries(DOMAIN)
        if not entries:
            return
        hub_entry_id = entries[0].entry_id
        # getattr, not direct access: existing callers/tests of this
        # function (predating #534) construct a minimal fake entry with
        # no .subentries attribute at all -- a real HA ConfigEntry always
        # has one, but this stays defensive rather than assume every
        # caller's fake matches the real shape.
        hub_subentries = getattr(entries[0], "subentries", {}) or {}
        period_seconds = (grid_times[1] - grid_times[0]).total_seconds()
        default_min_hysteresis_seconds = (
            period_seconds * load_run_state.DEFAULT_MIN_HYSTERESIS_PERIODS
        )
        day_key = now.strftime("%Y-%m-%d")
        n_periods = len(grid_times)

        def _plan_cost_forecast(
            power_series: NDArray[np.float64] | list[float],
        ) -> list[dict[str, object]] | None:
            # nimbus issue #591: per-period PLANNED cost (power_kw *
            # period_hours * that period's own blended import_price),
            # aligned one-to-one with plan_forecast -- None (not a
            # zero-filled series) whenever either input this cycle needs
            # isn't available, so derive_schedule_view() never mistakes
            # "not computed" for "genuinely free."
            if period_hours_arr is None or import_price_arr is None:
                return None
            n = min(len(power_series), len(period_hours_arr), len(import_price_arr))
            cost_values = [
                float(power_series[i])
                * float(period_hours_arr[i])
                * float(import_price_arr[i])
                for i in range(n)
            ]
            return load_run_state.build_time_value_series(grid_times[:n], cost_values)

        def _raw_shadow_price_series() -> list[float]:
            # nimbus issue #613 (item 1 of 3 -- exposure only, NOT the
            # earliness-timing behavior change items 2/3 describe): the
            # whole-system marginal cost of a kWh for every period this
            # load's own plan_forecast covers, straight off the LP's own
            # power_balance_t{i} dual -- "already extracts duals...
            # this is exposure, not new solver work." `getattr` (not a
            # direct `plan.duals` read) because `plan` here is
            # `network.Plan`, whose own dataclass default is `{}`, but
            # this file's own bare-SimpleNamespace test fakes predate
            # this field and don't set it -- same defensive posture as
            # every other optional-attribute read on `plan` in this
            # function. Shared, unrounded source for both
            # _plan_shadow_price_forecast() (the published series) and
            # item 3's own status-reason computation, which needs the
            # real, unrounded lambda(0) to compare against.
            #
            # nimbus issue #685 (Mark Purcell): this read power_balance_
            # t{i}'s own raw dual directly, with no hours[i] division --
            # the exact same #662 bug (the dual comes out in "$ per kW
            # of RHS," an implicit x hours[i] relative to the true
            # $/kWh marginal price) in a THIRD call site #662's own fix
            # never touched, since #613 (which added this function)
            # shipped before #662 was even found. Confirmed live against
            # Mark's own real numbers: every one of 7 checked periods
            # was off from sensor.nimbus_solver_battery_forecast's own
            # correctly-scaled shadow_price by exactly x12 (1/hours[i]
            # for these 5-minute periods). Same correction as the
            # power_balance_t{i} shadow_price field a few hundred lines
            # above (period_hours_arr[i] division) -- falls back to the
            # grid's own uniform period_seconds when period_hours_arr
            # isn't supplied (bare-SimpleNamespace tests predating this
            # parameter), since production always passes a real one
            # (see build_plan()'s own publish_plan() call).
            duals = getattr(plan, "duals", {}) or {}
            fallback_hours = period_seconds / 3600.0
            return [
                duals.get(f"power_balance_t{i}", 0.0)
                / (
                    float(period_hours_arr[i])
                    if period_hours_arr is not None and i < len(period_hours_arr)
                    else fallback_hours
                )
                for i in range(n_periods)
            ]

        def _plan_shadow_price_forecast() -> list[dict[str, object]]:
            return load_run_state.build_time_value_series(
                grid_times,
                _raw_shadow_price_series(),
                # build_time_value_series()'s own 3dp default would
                # collapse real, meaningful precision here -- a $/kWh
                # shadow price this small (Mark's own real numbers:
                # 0.13c/kWh vs 0.06c/kWh, i.e. 0.0013 vs 0.0006 $/kWh)
                # needs the same 4dp this file's own energy_shadow_
                # price_now field already uses, or two genuinely
                # different marginal costs round to the identical
                # published value.
                round_ndigits=4,
            )

        async def _async_fetch_thermal_history(
            done_entity: str, power_sensor: str, start: datetime, end: datetime
        ) -> list[tuple[datetime, float, float]]:
            # nimbus issue #592: the async-native sibling of
            # fetch_entity_attribute_history_range()/fetch_entity_
            # history_range() (this file, near resample_history_nearest())
            # -- those two are SYNC wrappers that internally dispatch onto
            # _NATIVE_HASS.loop via run_coroutine_threadsafe().result(),
            # correct for a sync caller running in an executor thread
            # (_sample_load_run_state()'s own caller context) but a real
            # deadlock risk called from HERE: _update_all() is itself a
            # coroutine already running ON that same loop (dispatched via
            # run_coroutine_threadsafe further down this function), so a
            # blocking .result() call from inside it would wait on the
            # loop it's blocking. Awaits the recorder's own executor job
            # directly instead -- safe from an already-async context.
            try:
                from homeassistant.components.recorder import (
                    get_instance as _recorder_get_instance,
                )
                from homeassistant.components.recorder import (
                    history as _recorder_history,
                )
            except ImportError:
                return []
            try:
                temp_changes = await _recorder_get_instance(
                    _NATIVE_HASS
                ).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    _NATIVE_HASS,
                    start,
                    end,
                    done_entity,
                    False,  # no_attributes=False -- current_temperature lives there
                )
                power_changes = await _recorder_get_instance(
                    _NATIVE_HASS
                ).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    _NATIVE_HASS,
                    start,
                    end,
                    power_sensor,
                    True,  # no_attributes -- plain numeric state is enough
                )
            except Exception:
                _LOGGER.debug(
                    "Nimbus: #592 thermal history fetch failed for %s/%s",
                    done_entity,
                    power_sensor,
                    exc_info=True,
                )
                return []
            temp_points: list[tuple[datetime, float]] = []
            for s in temp_changes.get(done_entity, []):
                raw = s.attributes.get("current_temperature")
                if raw is None:
                    continue
                try:
                    temp_points.append(
                        (s.last_changed.astimezone(LOCAL_TZ), float(raw))
                    )
                except (TypeError, ValueError):
                    continue
            temp_points.sort(key=lambda x: x[0])
            power_points: list[tuple[datetime, float]] = []
            for s in power_changes.get(power_sensor, []):
                try:
                    v = float(s.state)
                except (TypeError, ValueError):
                    continue
                unit = s.attributes.get("unit_of_measurement")
                if unit == "W":
                    v = v / 1000.0
                power_points.append((s.last_changed.astimezone(LOCAL_TZ), v))
            power_points.sort(key=lambda x: x[0])
            power_resampled = resample_history_nearest(
                power_points, [t for t, _ in temp_points]
            )
            return [(t, temp, p) for (t, temp), p in zip(temp_points, power_resampled)]

        async def _async_fetch_ambient_history(
            entity_id: str, start: datetime, end: datetime
        ) -> list[tuple[datetime, float]]:
            # nimbus issue #481: same async-native recorder-history
            # pattern as _async_fetch_thermal_history() just above (same
            # deadlock reasoning -- _update_all() is itself already
            # running on _NATIVE_HASS.loop), just reading a weather
            # entity's own real `temperature` attribute instead of a
            # load's own done_entity/power_sensor pair. Used only to
            # LEARN the ambient-scaled loss coefficient from real past
            # weather (thermal_forecast.learn_thermal_rates()'s own
            # ambient_history parameter) -- the forward FORECAST used for
            # projection is a completely separate fetch
            # (_fetch_weather_hourly_forecast(), a live weather.
            # get_forecasts call, not recorder history).
            try:
                from homeassistant.components.recorder import (
                    get_instance as _recorder_get_instance,
                )
                from homeassistant.components.recorder import (
                    history as _recorder_history,
                )
            except ImportError:
                return []
            try:
                changes = await _recorder_get_instance(
                    _NATIVE_HASS
                ).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    _NATIVE_HASS,
                    start,
                    end,
                    entity_id,
                    False,  # no_attributes=False -- temperature lives there
                )
            except Exception:
                _LOGGER.debug(
                    "Nimbus: #481 ambient history fetch failed for %s",
                    entity_id,
                    exc_info=True,
                )
                return []
            points: list[tuple[datetime, float]] = []
            for s in changes.get(entity_id, []):
                raw = s.attributes.get("temperature")
                if raw is None:
                    continue
                try:
                    points.append((s.last_changed.astimezone(LOCAL_TZ), float(raw)))
                except (TypeError, ValueError):
                    continue
            points.sort(key=lambda x: x[0])
            return points

        async def _update_all() -> None:
            store = load_run_state.LoadRunStateStore(
                store=_Store(_NATIVE_HASS, 1, f"{DOMAIN}_{hub_entry_id}_load_run_state")
            )
            for subentry_id, period0_kw, load_kind, load_plan in entries_with_ids:
                # nimbus issue #1019, the other half. Every controllable load
                # used to share one try/except -- the outermost one, far below
                # -- so a single raise abandoned the cycle for EVERY load not
                # yet processed. Loads already dispatched stayed dispatched;
                # the rest were simply never commanded. At a 5-minute cadence
                # the next cycle usually recovers, so it presented as
                # intermittent missed dispatch rather than an outage.
                #
                # v0.94.350 made that failure audible (DEBUG -> WARNING). This
                # makes it survivable: one bad load now costs itself and
                # nothing else.
                #
                # The body below is byte-identical to before, re-indented one
                # level and nothing more -- verified mechanically rather than
                # by eye, because #873 established that a mechanical move of a
                # block containing `if` can silently re-parent a following
                # `elif` while passing ruff, mypy and its own tests.
                try:
                    raw_new_state = (
                        float(period0_kw) > load_run_state.DEFAULT_ON_THRESHOLD_KW
                    )
                    subentry = hub_subentries.get(subentry_id)
                    # nimbus issue #645: same live-tuning overlay as build_
                    # controllable_loads() -- covers both reads below
                    # (min_hold_minutes here, max_activations_per_day
                    # further down this same loop iteration's own data).
                    data = (
                        _resolve_controllable_load_tuning(subentry.data, subentry)
                        if subentry is not None
                        else {}
                    )
                    min_hold_minutes = data.get(CONF_CONTROLLABLE_LOAD_MIN_HOLD_MINUTES)
                    min_hysteresis_seconds = (
                        float(min_hold_minutes) * 60.0
                        if min_hold_minutes is not None
                        else default_min_hysteresis_seconds
                    )
                    prev = await store.async_read(subentry_id)
                    # nimbus issue #595 (Mark Purcell, real finding: a 0.65kW
                    # heat pump cycled on/off every 15-35 min the first live
                    # morning, burning the daily activation cap before the
                    # cheap window even arrived). The LP's own adequacy-load
                    # plan is jagged at 5-minute resolution -- unlike the
                    # battery, it has no switching cost/smoothness term, so a
                    # single dip below threshold is common even while the
                    # load is genuinely still wanted "on" a few periods
                    # later. A dip that resumes within this load's own hold
                    # window should never register as a real OFF at all:
                    # committing to OFF and immediately re-arming ON a few
                    # minutes later defeats the entire point of the
                    # hysteresis guard below, and burns a real activation for
                    # nothing. Look ahead through the SAME plan already in
                    # hand this cycle (load_plan.power_kw/served_kw) for up
                    # to min_hysteresis_seconds -- if the load wants on again
                    # inside that window, treat this dip as noise (stay "raw
                    # on") rather than a genuine sustained off.
                    #
                    # Deliberately one-sided: only suppresses OFF when
                    # already commanded ON. Never accelerates an OFF->ON
                    # transition -- pre-empting "on" early would risk an
                    # activation the plan hasn't actually committed to yet.
                    if (
                        prev.commanded_state
                        and not raw_new_state
                        and period_hours_arr is not None
                    ):
                        period_series = (
                            load_plan.served_kw
                            if load_kind == "sheddable"
                            else load_plan.power_kw
                        )
                        lookahead_hours_needed = min_hysteresis_seconds / 3600.0
                        hours_elapsed = 0.0
                        for i in range(1, len(period_series)):
                            prior_hours = (
                                float(period_hours_arr[i - 1])
                                if i - 1 < len(period_hours_arr)
                                else 0.0
                            )
                            hours_elapsed += prior_hours
                            if hours_elapsed > lookahead_hours_needed:
                                break
                            if (
                                float(period_series[i])
                                > load_run_state.DEFAULT_ON_THRESHOLD_KW
                            ):
                                raw_new_state = True
                                break
                    new = load_run_state.decide_commanded_state(
                        prev,
                        raw_new_state=raw_new_state,
                        now=now,
                        min_hysteresis_seconds=min_hysteresis_seconds,
                    )
                    # nimbus issue #581: publish this cycle's own full plan
                    # series regardless of whether commanded_state itself
                    # changed -- see this function's own docstring.
                    # nimbus issue #873 (Mark Purcell, 2026-09-16: "Build it").
                    # The last_idle_temperature sampler and the heating-rate/idle-
                    # decay fitter used to sit INSIDE the `load_kind == "adequacy"`
                    # branch below, so a kind=thermal load never learned anything and
                    # ran forever on the generic fallback constants -- on the
                    # reference household, the one real hot-water system scheduled
                    # off 8.0 degC/kWh and 0.5 degC/h rather than its own measured
                    # tank. Invisible from outside until #940 (v0.94.335) began
                    # publishing thermal_heating_rate_origin, which reads `fallback`
                    # beside a null learned rate.
                    #
                    # Nothing here was ever adequacy-specific: done_entity,
                    # power_sensor, the temperature read, the settling check and the
                    # history fetch are all generic, and the weather entity comes off
                    # cfg. A parallel kind=thermal copy was ruled out on Mark's own
                    # reasoning -- the async context already exists here, and a
                    # second copy would join the exact drift class #357 already pays
                    # for.
                    #
                    # Deliberately a RELOCATION, not a loosening: every precondition
                    # is unchanged, including the temperature gate the fitter sits
                    # under without reading. Dropping that because it looks
                    # incidental would be a real behaviour change smuggled in as a
                    # refactor.
                    start_temperature = None
                    live_temperature = None
                    heating_rate = None
                    decay_rate = None
                    loss_coeff = None
                    weather_entity_id = None
                    done_entity = data.get(CONF_DEFERRABLE_DONE_ENTITY) or data.get(
                        CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY
                    )
                    # nimbus issue #768 (Mark Purcell): unset here meant thermal
                    # rate learning silently stayed on generic fallback
                    # constants -- auto-discover from the load's own device.
                    power_sensor = resolve_controllable_load_power_sensor(data)
                    if (
                        done_entity
                        and power_sensor
                        and done_entity.split(".", 1)[0]
                        in done_condition.ATTRIBUTE_DONE_DOMAINS
                    ):
                        live_temperature = done_condition.read_current_temperature(
                            _NATIVE_HASS, done_entity
                        )
                        # nimbus issue #609 (Mark Purcell, real finding:
                        # current_temperature reads ~10-11 degC LOW while
                        # the compressor is actively running on the #534
                        # SG Ready bridge -- a device-side reporting
                        # artifact, not a real physical drop, confirmed
                        # by every idle reading before/after a run
                        # agreeing with itself while every in-run reading
                        # is depressed). A live reading is only trusted
                        # once the load is confirmed idle AND has been
                        # off for at least thermal_forecast.SETTLING_
                        # MINUTES -- otherwise the last known-good idle
                        # reading anchors the projection instead.
                        settled = not new.currently_on and (
                            new.off_since is None
                            or (now.timestamp() - new.off_since)
                            >= thermal_forecast.SETTLING_MINUTES * 60.0
                        )
                        if settled and live_temperature is not None:
                            new = replace(new, last_idle_temperature=live_temperature)
                        start_temperature = (
                            live_temperature
                            if settled
                            else (
                                new.last_idle_temperature
                                if new.last_idle_temperature is not None
                                else live_temperature
                            )
                        )
                        if start_temperature is not None:
                            heating_rate = new.thermal_heating_rate_c_per_kwh
                            decay_rate = new.thermal_idle_decay_c_per_hour
                            loss_coeff = new.thermal_loss_coeff_per_h
                            # nimbus issue #481: resolved once per load
                            # per cycle, used by both the (day-key-gated)
                            # ambient-history learning fetch below and
                            # the (every-cycle) ambient-forecast
                            # projection fetch further down.
                            weather_entity_id = (cfg or {}).get(
                                "solver_weather_forecast_sensor"
                            )
                            # Recorder history is a real DB query -- only
                            # relearn once per calendar day (#592's own
                            # "on each retrain" ask), not every solve.
                            # nimbus issue #618 (Mark Purcell, real finding
                            # the day the #609/#610/#611 learner redesign
                            # shipped): thermal_rates_learned_day_key was
                            # already stamped today by the OLD (#592-era)
                            # learner before this cycle's code even landed,
                            # so the day-key gate alone silently skipped
                            # relearning under the NEW idle-to-idle logic
                            # until tomorrow -- thermal_rates_source stayed
                            # at its "never learned" "" default and the
                            # stale 8 C/kWh figure carried forward despite
                            # today's recorder already having everything
                            # the new learner needs. A never-yet-labeled
                            # thermal_rates_source ("" -- #610's own
                            # contract is only ever "learned"/"fallback"
                            # after a real run) also forces a relearn, so
                            # any future learner-logic change self-heals
                            # on its very next solve instead of waiting up
                            # to a full day.
                            if (
                                new.thermal_rates_learned_day_key != day_key
                                or not new.thermal_rates_source
                            ):
                                history_end = now
                                history_start = now - timedelta(days=3)
                                thermal_history = await _async_fetch_thermal_history(
                                    done_entity,
                                    power_sensor,
                                    history_start,
                                    history_end,
                                )
                                # nimbus issue #481: real outdoor-
                                # temperature history over the SAME
                                # window, only when the household has a
                                # weather source configured -- graceful
                                # no-op (ambient_history stays empty,
                                # learn_thermal_rates() returns loss_
                                # coeff_per_h=None, thermal_loss_coeff_
                                # per_h below stays None) for any install
                                # that hasn't configured one.
                                ambient_history: list[tuple[datetime, float]] = []
                                if weather_entity_id:
                                    ambient_history = (
                                        await _async_fetch_ambient_history(
                                            weather_entity_id,
                                            history_start,
                                            history_end,
                                        )
                                    )
                                learned = thermal_forecast.learn_thermal_rates(
                                    thermal_history,
                                    on_threshold_kw=load_run_state.DEFAULT_ON_THRESHOLD_KW,
                                    ambient_history=ambient_history,
                                )
                                heating_rate = learned.heating_rate_c_per_kwh
                                decay_rate = learned.idle_decay_c_per_hour
                                loss_coeff = learned.loss_coeff_per_h
                                new = replace(
                                    new,
                                    thermal_heating_rate_c_per_kwh=heating_rate,
                                    thermal_idle_decay_c_per_hour=decay_rate,
                                    thermal_loss_coeff_per_h=loss_coeff,
                                    thermal_rates_learned_day_key=day_key,
                                    # nimbus issue #610: "the published
                                    # attributes do not say which [a
                                    # learned rate from a default]."
                                    thermal_rates_source=(
                                        "learned" if learned.is_learned else "fallback"
                                    ),
                                )
                    if load_kind == "sheddable" and period_hours_arr is not None:
                        nominal_kw = data.get(CONF_SHEDDABLE_NOMINAL_KW)
                        new = replace(
                            new,
                            plan_forecast=load_run_state.build_time_value_series(
                                grid_times, load_plan.served_kw
                            ),
                            plan_cost_forecast=_plan_cost_forecast(load_plan.served_kw),
                            plan_shadow_price_forecast=_plan_shadow_price_forecast(),
                            plan_nominal_kw=(
                                float(nominal_kw) if nominal_kw is not None else None
                            ),
                        )
                    elif load_kind == "adequacy" and period_hours_arr is not None:
                        earliest_hour = data.get(CONF_DEFERRABLE_EARLIEST_HOUR)
                        deadline_hour = data.get(CONF_DEFERRABLE_DEADLINE_HOUR)
                        earliest_period = (
                            _resolve_hour_to_period_index(
                                grid_times, now, float(earliest_hour), is_deadline=False
                            )
                            if earliest_hour is not None
                            else 0
                        )
                        deadline_period = (
                            _resolve_hour_to_period_index(
                                grid_times, now, float(deadline_hour), is_deadline=True
                            )
                            if deadline_hour is not None
                            else n_periods - 1
                        )
                        # nimbus issue #582's own same-day-in-progress fix,
                        # duplicated here rather than shared -- this function
                        # resolves its OWN copy of earliest/deadline_period
                        # (for display only, not for the actual LP window,
                        # which build_controllable_loads() resolves
                        # separately) and would otherwise report the wrong
                        # (tomorrow) earliest_period for the exact same real
                        # case #582 fixed for the LP's own window: a same-day
                        # window already open right now. See that function's
                        # own comment for the full reasoning. KNOWN DRIFT
                        # RISK, flagged rather than silently left inconsistent:
                        # a future change to #582's own logic needs to be
                        # ported here too, or better, both call sites should
                        # be refactored onto one shared helper.
                        # nimbus issue #582, extracted to one helper by #485 --
                        # this logic previously existed as four identical copies.
                        earliest_period = _earliest_period_for_same_day_window(
                            now=now,
                            earliest_hour=earliest_hour,
                            deadline_hour=deadline_hour,
                            earliest_period=earliest_period,
                            deadline_period=deadline_period,
                        )
                        target_kwh = data.get(CONF_DEFERRABLE_TARGET_KWH)
                        delivered_kwh_cumulative = np.cumsum(
                            np.asarray(load_plan.power_kw, dtype=np.float64)
                            * np.asarray(period_hours_arr[: len(load_plan.power_kw)])
                        )
                        # nimbus issue #483: getattr, not direct attribute
                        # access -- this file's own bare-SimpleNamespace test
                        # fakes (_fake_load_plan(), same reasoning as the #774
                        # thermal_loads getattr elsewhere in this file) predate
                        # both of these fields.
                        marginal_cost = getattr(load_plan, "marginal_cost", 0.0)
                        profit_horizon = getattr(load_plan, "profit_horizon", None)
                        # nimbus issue #483, item 2: looked up by subentry_id
                        # from the dict main() computed once, up front, over
                        # every adequacy load in the same solve -- not a
                        # per-load LP-native value like marginal_cost/
                        # profit_horizon above (see compute_tariff_
                        # attributed_cost()'s own docstring for why it lives
                        # outside network.py). None when the caller didn't
                        # pass the dict at all (every existing test fixture
                        # predating this field) -- a real no-op, not a
                        # silent 0.0 masquerading as "genuinely computed and
                        # zero."
                        tariff_attributed_cost = (
                            tariff_attributed_cost_by_subentry.get(subentry_id)
                            if tariff_attributed_cost_by_subentry is not None
                            else None
                        )
                        new = replace(
                            new,
                            plan_forecast=load_run_state.build_time_value_series(
                                grid_times, load_plan.power_kw
                            ),
                            plan_cost_forecast=_plan_cost_forecast(load_plan.power_kw),
                            plan_shadow_price_forecast=_plan_shadow_price_forecast(),
                            plan_delivered_kwh_forecast=(
                                load_run_state.build_time_value_series(
                                    grid_times, delivered_kwh_cumulative
                                )
                            ),
                            plan_target_kwh=(
                                float(target_kwh) if target_kwh is not None else None
                            ),
                            plan_shortfall_kwh=float(load_plan.shortfall_kwh),
                            # nimbus issue #483: rounded to 4dp at this publish
                            # boundary, matching every other headline $ figure
                            # this project publishes (total_cost, cost_
                            # breakdown, cost_band -- see solver_writer.py's
                            # own total_cost fix, nimbus issue #756-golden-CI-
                            # flake) -- HiGHS's LP solve is not bit-for-bit
                            # deterministic run to run, and leaving this one
                            # unrounded would leak that same noise straight
                            # through. Internal math (network.py's own
                            # marginal_cost/profit_horizon computation) is
                            # untouched -- only the published value changes.
                            plan_marginal_cost=round(marginal_cost, 4),
                            plan_profit_horizon=(
                                round(profit_horizon, 4)
                                if profit_horizon is not None
                                else None
                            ),
                            plan_tariff_attributed_cost=(
                                round(tariff_attributed_cost, 4)
                                if tariff_attributed_cost is not None
                                else None
                            ),
                            plan_status_reason=load_run_state.compute_load_status_reason(
                                power_kw=load_plan.power_kw,
                                shadow_price=_raw_shadow_price_series(),
                                grid_times=grid_times,
                                earliest_period=earliest_period,
                                deadline_period=deadline_period,
                            ),
                            plan_earliest_period=earliest_period,
                            plan_deadline_period=deadline_period,
                        )
                        # nimbus issue #592 (Mark Purcell, part of #589 --
                        # "will the tank be at 60 by lunchtime?"): a
                        # water_heater/climate load with a real done_entity
                        # and power_sensor configured gets a projected
                        # temperature forecast, groundwork for the full #481
                        # thermal kind. See thermal_forecast.py's own module
                        # docstring for the full design and what's
                        # deliberately NOT part of this (model-based source
                        # marking, using the crossing to shorten the LP's
                        # own schedule ahead of time).
                        # nimbus issue #809: done_entity defaults to this same
                        # load's own device_entity -- see build_controllable_
                        # loads()'s own matching comment for the reasoning;
                        # duplicated here rather than shared, same accepted
                        # drift-risk tradeoff already flagged for this
                        # function's own duplicate of the #582 same-day fix.
                        # nimbus issue #873: the sampler and fitter moved above the
                        # load_kind branches; only the projection below is genuinely
                        # adequacy-specific, so it keeps its own gates.
                        if (
                            done_entity
                            and power_sensor
                            and done_entity.split(".", 1)[0]
                            in done_condition.ATTRIBUTE_DONE_DOMAINS
                            and start_temperature is not None
                        ):
                            # nimbus issue #611 (Mark Purcell: "the
                            # forecast is projected from plan_forecast,
                            # so it shows [cooling] ... while [real
                            # power] is actually going into the tank" --
                            # #595's own guard can hold commanded_state
                            # ON through a hold window a fresh solve's
                            # own plan_forecast[0] doesn't yet reflect).
                            # Only period 0 is overridden with the
                            # load's own configured max_power_kw when
                            # actually commanded on -- every later
                            # period still projects from the real plan.
                            max_power_kw = data.get(CONF_DEFERRABLE_MAX_POWER_KW)
                            override_power = (
                                float(max_power_kw)
                                if new.commanded_state and max_power_kw is not None
                                else None
                            )
                            # nimbus issue #640 (Mark Purcell, live
                            # verification of #610: with the heat pump
                            # idle in "eco" mode, `temperature` reads the
                            # 45 degC eco setpoint -- the floor the unit
                            # maintains BETWEEN runs, not the ceiling of
                            # a run -- which clamped a genuine 2 kWh/16
                            # degC reheat down to a flat line and made
                            # the tank look like it could never reach its
                            # own 60 degC done line). Clamp at the
                            # heater's real operating ceiling instead:
                            # "max_temp" first (the unit's own physical
                            # maximum, e.g. 65 here, still a real,
                            # never-invented attribute read off the
                            # entity, never hardcoded); "temperature" as
                            # a fallback for an entity that only
                            # publishes the current target and has no
                            # separate max_temp; and, when the entity
                            # exposes neither, the load's own configured
                            # done_when threshold (#610's own "at minimum
                            # the done_when threshold" fallback) so a
                            # done line the projection needs to actually
                            # reach is never clamped below itself.
                            ceiling_temperature = None
                            done_state_obj = _NATIVE_HASS.states.get(done_entity)
                            if done_state_obj is not None:
                                for _attr in ("max_temp", "temperature"):
                                    _raw = done_state_obj.attributes.get(_attr)
                                    if _raw is not None:
                                        try:
                                            ceiling_temperature = float(_raw)
                                        except (TypeError, ValueError):
                                            ceiling_temperature = None
                                        break
                            if ceiling_temperature is None:
                                done_when = data.get(CONF_DEFERRABLE_DONE_WHEN)
                                if done_when is not None:
                                    try:
                                        _, ceiling_temperature = (
                                            done_condition.parse_done_when(done_when)
                                        )
                                    except (ValueError, TypeError):
                                        ceiling_temperature = None
                            # nimbus issue #481: the forward ambient
                            # forecast for the PROJECTION step -- a
                            # separate, live weather.get_forecasts fetch
                            # from the recorder-history one just above
                            # (used only to LEARN loss_coeff, not to
                            # project it forward). Same weather_entity_id
                            # resolved above; graceful no-op (empty list)
                            # when unconfigured or the fetch fails, same
                            # posture as publish_weather_forecast_
                            # mirrors()'s own use of this helper.
                            ambient_forecast_points: list[dict[str, object]] = []
                            if weather_entity_id:
                                _hourly = _fetch_weather_hourly_forecast(
                                    weather_entity_id
                                )
                                if _hourly:
                                    ambient_forecast_points = [
                                        {
                                            "time": p["datetime"],
                                            "value": float(p["temperature"]),
                                        }
                                        for p in _hourly
                                        if isinstance(p, dict)
                                        and p.get("datetime") is not None
                                        and p.get("temperature") is not None
                                    ]
                            new = replace(
                                new,
                                temperature_forecast=thermal_forecast.project_temperature_forecast(
                                    new.plan_forecast or [],
                                    start_temperature=start_temperature,
                                    heating_rate_c_per_kwh=(
                                        heating_rate
                                        if heating_rate is not None
                                        else thermal_forecast.DEFAULT_HEATING_RATE_C_PER_KWH
                                    ),
                                    idle_decay_c_per_hour=(
                                        decay_rate
                                        if decay_rate is not None
                                        else thermal_forecast.DEFAULT_IDLE_DECAY_C_PER_HOUR
                                    ),
                                    on_threshold_kw=load_run_state.DEFAULT_ON_THRESHOLD_KW,
                                    ceiling_temperature=ceiling_temperature,
                                    override_first_period_power_kw=override_power,
                                    loss_coeff_per_h=loss_coeff,
                                    ambient_forecast=ambient_forecast_points,
                                ),
                            )
                            # nimbus issue #712/#713 (Mark Purcell, real
                            # live finding): the deferrable model's own
                            # kWh-target/deadline framing has no
                            # representation of the device's own
                            # PHYSICAL thermal floor at all -- confirmed
                            # live, two consecutive nights, the tank
                            # falling past its eco-mode floor and the
                            # compressor self-triggering hours before
                            # the next scheduled ON period. Not
                            # attempting a dispatch-changing fix here
                            # (#713's own text: worth checking whether
                            # pulling the earliest start forward is
                            # worth the price difference, a real,
                            # separate economic design question) --
                            # this is #712's own "at minimum" ask: a
                            # WARNING + a flag a household/future
                            # automation can act on. floor_temperature
                            # mirrors ceiling_temperature's own read
                            # order (min_temp first -- the unit's own
                            # real physical/eco floor, never invented;
                            # done_when's own threshold as a last
                            # resort for a device with no min_temp at
                            # all) so both bounds come from the exact
                            # same entity/config, never a second guess.
                            floor_temperature = None
                            if done_state_obj is not None:
                                _raw_floor = done_state_obj.attributes.get("min_temp")
                                if _raw_floor is not None:
                                    try:
                                        floor_temperature = float(_raw_floor)
                                    except (TypeError, ValueError):
                                        floor_temperature = None
                            if floor_temperature is None:
                                done_when = data.get(CONF_DEFERRABLE_DONE_WHEN)
                                if done_when is not None:
                                    try:
                                        _, floor_temperature = (
                                            done_condition.parse_done_when(done_when)
                                        )
                                    except (ValueError, TypeError):
                                        floor_temperature = None
                            floor_crossing = None
                            if (
                                floor_temperature is not None
                                and new.temperature_forecast
                            ):
                                floor_crossing = thermal_forecast.find_floor_crossing(
                                    new.temperature_forecast, floor_temperature
                                )
                            new = replace(
                                new,
                                floor_crossing_forecast_time=(
                                    str(floor_crossing["time"])
                                    if floor_crossing is not None
                                    else None
                                ),
                                floor_crossing_forecast_temperature=(
                                    float(floor_crossing["value"])  # type: ignore[arg-type]
                                    if floor_crossing is not None
                                    else None
                                ),
                            )
                            if floor_crossing is not None:
                                _floor_warn_key = (subentry_id, day_key)
                                if _floor_warn_key not in _FLOOR_CROSSING_WARNED:
                                    _FLOOR_CROSSING_WARNED.add(_floor_warn_key)
                                    _LOGGER.warning(
                                        "Nimbus: controllable load '%s' is "
                                        "forecast to cross its own hardware "
                                        "floor (%.1f) at %s, before its own "
                                        "planned schedule reaches it -- the "
                                        "device may self-trigger outside "
                                        "the solved plan (nimbus issue "
                                        "#712/#713)",
                                        subentry_id,
                                        floor_temperature,
                                        floor_crossing["time"],
                                    )
                    elif load_kind == "thermal" and period_hours_arr is not None:
                        # nimbus issue #774: same publish shape as the
                        # adequacy branch above, but simpler -- no windowed/
                        # done-entity/floor-crossing machinery (out of scope
                        # for v1, see ThermalLoadConfig's own docstring).
                        earliest_hour = data.get(CONF_THERMAL_EARLIEST_HOUR)
                        deadline_hour = data.get(CONF_THERMAL_DEADLINE_HOUR)
                        earliest_period = (
                            _resolve_hour_to_period_index(
                                grid_times, now, float(earliest_hour), is_deadline=False
                            )
                            if earliest_hour is not None
                            else 0
                        )
                        deadline_period = (
                            _resolve_hour_to_period_index(
                                grid_times, now, float(deadline_hour), is_deadline=True
                            )
                            if deadline_hour is not None
                            else n_periods - 1
                        )
                        # nimbus issue #582's own same-day-in-progress fix,
                        # duplicated here too -- same accepted drift-risk
                        # tradeoff already flagged on the adequacy branch
                        # above and in build_controllable_loads() itself.
                        # nimbus issue #582, extracted to one helper by #485 --
                        # this logic previously existed as four identical copies.
                        earliest_period = _earliest_period_for_same_day_window(
                            now=now,
                            earliest_hour=earliest_hour,
                            deadline_hour=deadline_hour,
                            earliest_period=earliest_period,
                            deadline_period=deadline_period,
                        )
                        new = replace(
                            new,
                            plan_forecast=load_run_state.build_time_value_series(
                                grid_times, load_plan.power_kw
                            ),
                            plan_cost_forecast=_plan_cost_forecast(load_plan.power_kw),
                            plan_shadow_price_forecast=_plan_shadow_price_forecast(),
                            # nimbus issue #774: the LP's own real, solved
                            # temperature trajectory -- for kind=thermal this
                            # REPLACES the display-only thermal_forecast.py
                            # projection a kind=deferrable load still uses
                            # above (re-deriving the same physics model the LP
                            # already solved with could only ever diverge from
                            # it, see ThermalLoadPlan's own docstring).
                            plan_temperature_forecast=load_run_state.build_time_value_series(
                                grid_times, load_plan.temperature_c
                            ),
                            # nimbus issue #940: the rates the LP was
                            # actually built with, echoed off the plan rather
                            # than re-resolved here. Until now a household
                            # could see only the LEARNED rates, which are
                            # None on every kind=thermal load (#873) while
                            # the LP scheduled real hot water on the 8.0/0.5
                            # module defaults.
                            thermal_effective_heating_rate_c_per_kwh=(
                                load_plan.heating_rate_c_per_kwh
                            ),
                            thermal_effective_idle_decay_c_per_hour=(
                                load_plan.idle_decay_c_per_hour
                            ),
                            thermal_heating_rate_origin=load_plan.heating_rate_origin,
                            thermal_idle_decay_origin=load_plan.idle_decay_origin,
                            # Not applicable to this kind -- explicitly reset
                            # rather than left stale, so a load migrated from
                            # kind=deferrable doesn't keep showing an old
                            # target/shortfall figure that no longer applies.
                            plan_target_kwh=None,
                            plan_shortfall_kwh=None,
                            # nimbus issue #483: AdequacyLoadPlan-only fields
                            # (a kind=thermal load has no equivalent computed
                            # yet -- ThermalLoadPlan doesn't carry a lambda-
                            # based cost today, a real future extension, not
                            # attempted in this pass), same explicit-reset
                            # reasoning as plan_target_kwh/plan_shortfall_kwh
                            # just above.
                            plan_marginal_cost=None,
                            plan_profit_horizon=None,
                            plan_tariff_attributed_cost=None,
                            plan_earliest_period=earliest_period,
                            plan_deadline_period=deadline_period,
                        )
                    device_entity = data.get(CONF_CONTROLLABLE_LOAD_DEVICE_ENTITY)
                    climate_on_hvac_mode = data.get(
                        CONF_CONTROLLABLE_LOAD_CLIMATE_ON_HVAC_MODE
                    )
                    if device_entity and new.commanded_state != prev.commanded_state:
                        if new.commanded_state:
                            max_activations_raw = data.get(
                                CONF_CONTROLLABLE_LOAD_MAX_ACTIVATIONS_PER_DAY
                            )
                            max_activations = (
                                int(max_activations_raw)
                                if max_activations_raw is not None
                                else None
                            )
                            if load_run_state.activation_allowed(
                                new,
                                max_activations_per_day=max_activations,
                                day_key=day_key,
                            ):
                                try:
                                    await dispatch_commanded_state(
                                        _NATIVE_HASS,
                                        device_entity,
                                        True,
                                        climate_on_hvac_mode=climate_on_hvac_mode,
                                    )
                                    new = load_run_state.record_activation(
                                        new, day_key=day_key
                                    )
                                    new = replace(new, last_dispatch_failed=False)
                                except Exception:
                                    _LOGGER.warning(
                                        "Nimbus: dispatch ON failed for "
                                        "controllable load '%s' (%s) -- will retry "
                                        "next cycle (nimbus issue #875)",
                                        subentry_id,
                                        device_entity,
                                        exc_info=True,
                                    )
                                    # nimbus issue #875: the command did not go
                                    # out. Recording that is what makes the retry
                                    # possible at all -- before this, the failed
                                    # attempt was persisted as commanded, and
                                    # edge-triggering meant the next cycle saw no
                                    # transition and never tried again. One
                                    # transient failure cost the load its window.
                                    new = replace(new, last_dispatch_failed=True)
                            else:
                                _LOGGER.warning(
                                    "Nimbus: controllable load '%s' wants ON but "
                                    "is capped at %s activations/day -- not "
                                    "dispatched this cycle",
                                    subentry_id,
                                    max_activations,
                                )
                        else:
                            try:
                                await dispatch_commanded_state(
                                    _NATIVE_HASS, device_entity, False
                                )
                                new = replace(new, last_dispatch_failed=False)
                            except Exception:
                                _LOGGER.warning(
                                    "Nimbus: dispatch OFF failed for "
                                    "controllable load '%s' (%s) -- will retry "
                                    "next cycle (nimbus issue #875)",
                                    subentry_id,
                                    device_entity,
                                    exc_info=True,
                                )
                                new = replace(new, last_dispatch_failed=True)
                    elif device_entity and load_run_state.reaffirm_allowed(
                        new,
                        now_ts=now.timestamp(),
                        reaffirm_after_seconds=_resolve_reaffirm_after_seconds(data),
                        day_key=day_key,
                    ):
                        # nimbus issue #875, household decision 2026-09-15: the
                        # device is not following a command already given (or the
                        # last send never went out). Re-send the SAME state --
                        # deliberately NOT via record_activation(), so this cannot
                        # consume one of #534's capped device-side activations. A
                        # reminder is not a new activation.
                        _why = (
                            "last dispatch failed"
                            if new.last_dispatch_failed
                            else "device has not followed the command"
                        )
                        try:
                            await dispatch_commanded_state(
                                _NATIVE_HASS,
                                device_entity,
                                new.commanded_state,
                                climate_on_hvac_mode=(
                                    climate_on_hvac_mode
                                    if new.commanded_state
                                    else None
                                ),
                            )
                            new = load_run_state.record_reaffirm(
                                new, now_ts=now.timestamp(), day_key=day_key
                            )
                            new = replace(new, last_dispatch_failed=False)
                            _LOGGER.info(
                                "Nimbus: re-sent %s to controllable load '%s' (%s) "
                                "-- %s. Re-send %d of %d today; this does NOT "
                                "count against the activations/day cap.",
                                "ON" if new.commanded_state else "OFF",
                                subentry_id,
                                device_entity,
                                _why,
                                new.reaffirms_today,
                                load_run_state.DEFAULT_MAX_REAFFIRMS_PER_DAY,
                            )
                        except Exception:
                            _LOGGER.warning(
                                "Nimbus: re-send failed for controllable load '%s' (%s)",
                                subentry_id,
                                device_entity,
                                exc_info=True,
                            )
                            new = replace(new, last_dispatch_failed=True)
                    elif device_entity and load_run_state.reaffirm_allowed(
                        new,
                        now_ts=now.timestamp(),
                        reaffirm_after_seconds=_resolve_reaffirm_after_seconds(data),
                        # The whole point of this branch: ask the SAME question
                        # again with the cap lifted. True here while the capped
                        # call above returned False means the daily cap is the
                        # only thing standing between this load and a re-send.
                        max_reaffirms_per_day=None,
                        day_key=day_key,
                    ):
                        # nimbus issue #875, gap found by Mark Purcell's IV&V of
                        # PR #930: reaffirm_allowed() correctly returns False
                        # once the cap is spent, and then NOTHING happened --
                        # no else, no log. The sibling activation-cap branch a
                        # few lines above logs every time it blocks a dispatch;
                        # a spent reaffirm cap produced no signal at all, and
                        # the only trace left was command_divergence_seconds()
                        # quietly growing on a sensor attribute nobody is
                        # prompted to check. A device in a genuine argument
                        # with something else -- the exact scenario this cap
                        # exists to bound -- went quiet after 20 tries.
                        #
                        # Deliberately derived by re-asking reaffirm_allowed()
                        # with max_reaffirms_per_day=None rather than
                        # re-deriving the counter comparison here: the day-key
                        # rollover semantics live in load_run_state.py and a
                        # second copy of them in this file is precisely the
                        # drift #357 exists to catch. It also makes the branch
                        # exact -- the other three reasons that function
                        # returns False (re-sends disabled with 0, divergence
                        # below threshold, interval not yet elapsed) are all
                        # ordinary every-cycle states and must stay silent.
                        _cap_warn_key = (subentry_id, day_key)
                        if _cap_warn_key not in _REAFFIRM_CAP_WARNED:
                            _REAFFIRM_CAP_WARNED.add(_cap_warn_key)
                            _LOGGER.warning(
                                "Nimbus: controllable load '%s' (%s) is still not "
                                "following its commanded state (%s), but the daily "
                                "re-send cap of %d is spent -- Nimbus will stop "
                                "re-sending to this load until tomorrow. Something "
                                "else may be writing to the device. Logged once "
                                "per load per day (nimbus issue #875).",
                                subentry_id,
                                device_entity,
                                "ON" if new.commanded_state else "OFF",
                                load_run_state.DEFAULT_MAX_REAFFIRMS_PER_DAY,
                            )
                    if new is not prev:
                        await store.async_write(subentry_id, new)
                except Exception:
                    # Per-load, so the loop continues. Deliberately WARNING and
                    # deliberately naming the load: this is the level at which
                    # a household can act on it, and the outer handler cannot
                    # say WHICH load failed because by then the frame is gone.
                    _LOGGER.warning(
                        "Nimbus: controllable load '%s' (%s) failed to be processed "
                        "this solve cycle and was NOT commanded -- every other load "
                        "is unaffected, and the next cycle retries this one from "
                        "scratch (nimbus issue #1019).",
                        subentry_id,
                        load_kind,
                        exc_info=True,
                    )
                    continue

        import asyncio as _asyncio

        future = _asyncio.run_coroutine_threadsafe(_update_all(), _NATIVE_HASS.loop)
        future.result(timeout=10)
    except Exception:
        # nimbus issue #1019. This handler must stay -- it is the last
        # thing between a controllable-load failure and the solve cycle
        # it runs inside, and dispatch must never take the solve down.
        #
        # But it was DEBUG, and that made it invisible. Every controllable
        # load is commanded through one coroutine with no per-load
        # isolation, so ANY raise -- an unavailable entity, a sensor
        # returning an unexpected type, a malformed subentry, a recorder
        # hiccup mid-fetch -- abandons the cycle for EVERY REMAINING
        # LOAD, silently. Loads already dispatched stay dispatched; the
        # rest are simply not commanded, with nothing said about it.
        #
        # At a 5-minute cadence the next cycle usually succeeds, so the
        # symptom is intermittent missed dispatch rather than an outage.
        # That is exactly the shape of #757 (ten investigations) and of
        # #315, where the ABSENCE of a warning was the evidence nobody
        # thought to check. A guard that cannot report its own failure is
        # indistinguishable from one that never runs.
        #
        # WARNING, not DEBUG, and it says what was lost. Per-load
        # isolation -- so one bad load costs one load rather than the
        # remainder of the cycle -- is the other half of #1019 and needs
        # a re-indent of the whole loop body, so it is deliberately not
        # bundled here.
        _LOGGER.warning(
            "Nimbus: commanded-state guard failed this solve cycle -- any "
            "controllable load not yet processed was NOT commanded (see "
            "nimbus issue #1019; loads already dispatched are unaffected, "
            "and the next cycle retries from scratch)",
            exc_info=True,
        )


def _publish_side_reports(
    cfg: dict,
    cfg_unmoded: dict,
    now: datetime,
    grid_times: list[datetime],
    solar_kw: list[float],
) -> dict | None:
    """Every non-essential publish `main()` makes after the real
    solve, and the one value it hands back.

    nimbus issue #735 stage 6. These six blocks share one contract,
    stated identically in each of their own comments: **a failure
    here must never take down the real solve.** That is why every one
    of them is a `try` around a single call with a WARNING in the
    `except` -- and why they were worth moving together rather than
    one at a time.

    Measured before moving, the same way stages 1-3 and 5 were, and
    at STATEMENT level rather than by line span (the error stage 4's
    own comments record twice): **5 inputs, 1 output, 47 lines.** For
    comparison with the seams already extracted -- solar had 3
    outputs, load 4 in / 12 out, the SoC envelope 5 in / 4 out -- this
    is the cleanest seam taken so far, and it was found by scanning
    every consecutive-statement window in `main()` rather than by
    reading for one.

    The `except Exception` in each block is deliberate and stays:
    these are best-effort reports, and #363 already established that
    the failure mode to avoid is the bare `pass` that hid a real bug
    for days -- hence the WARNING, not a narrower catch.
    """
    solar_delivery: dict | None = None
    try:
        publish_weather_forecast_mirrors(cfg)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        # nimbus issue #363 (Mark Purcell, codebase review): same "bare
        # pass hid a real, diagnosable bug for days" lesson as the four
        # sibling publishes below -- now logged instead of silently
        # swallowed.
        _LOGGER.warning("Nimbus: weather forecast mirror publish failed: %s", e)

    # Built-in EPR/regret/tracking quality score (2026-08-25) -- own
    # cheap idempotency check means this is safe to call every cycle;
    # wrapped the same way as every other non-essential publish in this
    # file, since a failure here must never take down the real solve.
    try:
        publish_daily_quality_report(cfg_unmoded, now)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        # 2026-08-31: previously a bare `pass` -- made the entity-id-
        # collision incident this file's own resolve_real_entity_id()
        # fixes completely invisible in the log for days (confirmed live
        # on devhub: 200+ recent log lines matching "nimbus", zero
        # exceptions, zero tracebacks, because every failure here was
        # silently swallowed). Logging costs nothing towards "must never
        # break the real solve" -- it's still caught and ignored either
        # way -- but now a future failure of this specific publish is
        # actually diagnosable instead of only visible as a stale sensor.
        _LOGGER.warning("Nimbus: daily quality report publish failed: %s", e)

    # nimbus issue #1200: and go back for any PAST row that was scored
    # before its own P2P settlement existed, now that the real figures
    # have landed. Separate from the publish above on purpose -- that
    # call owns "score yesterday", this one owns "and repair the days
    # whose settlement arrived after we scored them" -- and wrapped the
    # same way, since neither may ever break the real solve.
    try:
        _repaired_rows = repair_provisional_quality_history(cfg_unmoded, now)
        if _repaired_rows:
            _LOGGER.info(
                "Nimbus: repaired provisional quality rows: %s", _repaired_rows
            )
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        _LOGGER.warning("Nimbus: provisional quality history repair failed: %s", e)

    # Same "never break the real solve" wrapping -- nimbus issue #496
    # (Signals 7/7 of #489, the compute_daily_flex_report() half Mark
    # Purcell authorized 2026-09-09, shipped separately from the sensor
    # half already published above via publish_flex_signals()).
    try:
        publish_daily_flex_report(cfg_unmoded, now)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        _LOGGER.warning("Nimbus: daily flex report publish failed: %s", e)

    # Same "never break the real solve" wrapping as the two publishes
    # above -- see publish_nimbus_only_soc_counterfactual()'s own
    # docstring (2026-08-25, "i want u to build that into devbox
    # package").
    try:
        publish_nimbus_only_soc_counterfactual(cfg_unmoded, now)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        # 2026-08-31: see publish_daily_quality_report()'s own matching
        # comment -- same "bare pass hid a real, diagnosable bug for
        # days" lesson, now logged instead of silently swallowed.
        _LOGGER.warning("Nimbus: counterfactual SoC publish failed: %s", e)

    # Same "never break the real solve" wrapping -- see
    # publish_efficiency_backtest_report()'s own docstring (2026-08-25,
    # the "outstanding, unique" backtesting-engine ask).
    try:
        publish_efficiency_backtest_report(cfg_unmoded, now)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        # 2026-08-31: see publish_daily_quality_report()'s own matching
        # comment -- same "bare pass hid a real, diagnosable bug for
        # days" lesson, now logged instead of silently swallowed.
        _LOGGER.warning("Nimbus: efficiency backtest publish failed: %s", e)

    # Same "never break the real solve" wrapping -- see
    # update_solar_delivery_ratio()'s own docstring (nimbus issue #128).
    try:
        solar_delivery = update_solar_delivery_ratio(cfg, now, grid_times, solar_kw)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        # nimbus issue #363 (Mark Purcell, codebase review): same "bare
        # pass hid a real, diagnosable bug for days" lesson as the
        # publishes above -- now logged instead of silently swallowed.
        _LOGGER.warning("Nimbus: solar delivery ratio update failed: %s", e)
        solar_delivery = None
    return solar_delivery


def main() -> None:
    # Fail fast, with a real, actionable message, if the Solver hasn't
    # been configured yet -- see fetch_solver_config()'s own docstring
    # for the full "installable by anyone" context this closes.
    cfg = fetch_solver_config()
    # nimbus issue #485: household-mode presets, applied to `cfg`
    # ITSELF and here rather than at each consumer -- the battery and
    # solver levers are read at 3-4 independent `_cfg_num(cfg, ...)`
    # sites apiece with no chokepoint between them, so a preset applied
    # anywhere downstream would reach some of them and silently miss
    # the rest.
    #
    # Deliberately NOT claimed: this does not change what sensor.nimbus_
    # solver_config shows. `cfg` is a COPY fetched FROM that sensor's
    # attributes, so a moded value lives only in this solve. The INFO
    # log below is therefore the record of what a mode actually moved --
    # checked rather than assumed, after an earlier draft of this comment
    # asserted the opposite.
    # `home` (the default) and an unrecognised/absent mode are both the
    # identity transform, so this is provably a no-op until a household
    # deliberately switches.
    # `cfg_unmoded` is kept deliberately: the historical report
    # publishes below score a PAST day, and that day was not run under
    # today's mode. Scoring yesterday with an `away` degradation cost it
    # never actually paid would shift j_ach/j_star and therefore EPR and
    # regret, silently. Nimbus does not record which mode was in force on
    # a past day (that is a real new capability, not a lookup), so the
    # honest choice is to score against the household's own baseline
    # configuration rather than a mode that may have been switched on
    # this morning.
    cfg_unmoded = cfg
    cfg, _mode_applied_solver = household_modes.apply_to_solver_config(
        cfg, cfg.get("household_mode")
    )
    if _mode_applied_solver:
        _LOGGER.info(
            "Nimbus #485: household mode %r applied to %d solver lever(s): %s",
            cfg.get("household_mode"),
            len(_mode_applied_solver),
            _mode_applied_solver,
        )
    _log_active_household_specific_overrides_once(cfg)

    now = datetime.now(UTC).astimezone(LOCAL_TZ).replace(second=0, microsecond=0)
    grid_times, period_hours_arr = build_tiered_grid(now)
    n_periods = len(grid_times)

    # Solar forecast sources -- every configured/auto-detected source
    # fetched, blended, and live-anchored for period 0. Moved out of
    # this function wholesale by nimbus issue #735 stage 1 (pure code
    # organization, no behaviour change): the real household findings,
    # the reversed "i do not want tricks" decision, and the specific
    # regressions each guard exists to prevent all live in
    # build_solar_arrays()'s own docstring rather than being restated
    # here in two places.
    solar_kw, solar_lower_kw, solar_upper_kw = solar_inputs_solar.build_solar_arrays(
        cfg, grid_times, n_periods
    )

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
    # nimbus issue #735 stage 2: the load-forecast slice, extracted into
    # solver_inputs/load.py. Twelve outputs against solar's three, hence a
    # dataclass rather than a tuple. summed_18_now_kw and load_kw[0] are
    # BOTH carried deliberately -- they differ on any install with the
    # whole-house cross-check configured, and that difference is #100.
    _load = load_inputs.build_load_arrays(cfg, grid_times, n_periods, now)
    load_kw = _load.load_kw
    load_lower_kw = _load.load_lower_kw
    load_upper_kw = _load.load_upper_kw
    summed_18_now_kw = _load.summed_18_now_kw
    whole_house_now_kw = _load.whole_house_now_kw
    live_load_kw = _load.live_load_kw
    load_forecast_entities = _load.load_forecast_entities
    load_forecast_error = _load.load_forecast_error
    load_forecast_warnings = _load.load_forecast_warnings
    load_forecast_source_used = _load.load_forecast_source_used
    load_forecast_coverage_hours = _load.load_forecast_coverage_hours
    failed_load_entities = _load.failed_load_entities

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
    solver_publish.publish_household_load_total_forecast(
        cfg=cfg,
        grid_times=grid_times,
        n_periods=n_periods,
        now=now,
        load_kw=load_kw,
        load_lower_kw=load_lower_kw,
        load_upper_kw=load_upper_kw,
        live_load_kw=live_load_kw,
        whole_house_now_kw=whole_house_now_kw,
        load_forecast_entities=load_forecast_entities,
        load_forecast_error=load_forecast_error,
        load_forecast_warnings=load_forecast_warnings,
        load_forecast_source_used=load_forecast_source_used,
        load_forecast_coverage_hours=load_forecast_coverage_hours,
        failed_load_entities=failed_load_entities,
    )

    # Purely cosmetic devhub dashboard sensor (2026-08-25) -- never
    # referenced below, never feeds the LP. Wrapped exactly like
    # _notify_load_forecast_error_once() above: a failed weather-mirror
    # publish must never be allowed to break the actual solve.
    # nimbus issue #735 stage 6: the six non-essential publishes that
    # used to sit inline here. See _publish_side_reports() for the
    # measured seam and the shared 'must never break the real solve'
    # contract they all carry.
    solar_delivery = _publish_side_reports(cfg, cfg_unmoded, now, grid_times, solar_kw)

    # Two paths, gated on whether a rich, forecast-array-shaped price
    # sensor is actually CONFIGURED (CONF_SOLVER_PRICE_FORECAST_ARRAY_
    # SENSOR) -- NOT, as of the 2026-09-02 hardcoded-foreign-entity
    # audit, on whether a specific LITERAL entity (sensor.localvolts_
    # price_forecast) happens to exist. Real households outside this
    # project's own setup can leave this blank, and that's fine: the
    # whole point of 2026-08-20's config-flow work (and this 2026-09-02
    # follow-up) is that the Solver still runs correctly for them, just
    # without this household's own extra sophistication -- see each new
    # field's own const.py comment for the full real-bug-audit story
    # (this same block used to hardcode FIVE separate LocalVolts/AEMO-
    # QLD1 entity names, none of them reachable by any other install
    # regardless of whether they had a real equivalent).
    price_forecast_sensor = cfg.get("solver_price_forecast_array_sensor")
    has_price_forecast_array = bool(price_forecast_sensor) and entity_exists(
        price_forecast_sensor
    )
    # nimbus issue #735 stage 4. Nine outputs against solar's three and
    # load's twelve, hence a result object rather than a tuple -- same
    # reasoning as solver_inputs/load.py. `has_price_forecast_array` is
    # passed in rather than recomputed there because main() reads it again
    # below, and two sources of truth for one question is how the
    # percentile-band reuse at the second call site would drift.
    _prices = price_inputs.build_price_arrays(
        cfg, grid_times, n_periods, now, has_price_forecast_array, price_forecast_sensor
    )
    spot_import_raw = _prices.spot_import_raw
    spot_export = _prices.spot_export
    import_real_mask = _prices.import_real_mask
    export_real_mask = _prices.export_real_mask
    import_price_upper_band = _prices.import_price_upper_band
    export_price_lower_band = _prices.export_price_lower_band
    export_bonus_price = _prices.export_bonus_price
    match_fraction = _prices.match_fraction
    p2p_recent_volume_kwh = _prices.p2p_recent_volume_kwh

    # The current settlement block must never be a forecast/blend value
    # (2026-08-27, nimbus repo issue #220, Mark Purcell): Amber,
    # LocalVolts, and AEMO all publish the SETTLED price for the block
    # containing right-now -- a contractual fact, not an estimate, and
    # it cannot legitimately be diluted by any secondary source. Both
    # resample_price_with_extrapolation() and
    # resample_generic_price_forecast_with_coverage() above answer
    # period 0 via a "nearest-at-or-before" lookup against the source's
    # own FORECAST array, which is a genuinely different read than the
    # same source's own live `state` -- confirmed live on Mark's install
    # (2026-08-27 11:07 AEST): forecast-array lookup produced 4.07
    # c/kWh for the current block while the source sensor's own settled
    # `state` was 4.89 c/kWh. grid_times[0] is always "now" by
    # construction (see build_tiered_grid()), so period 0 is always the
    # current settlement block -- override it here with a direct state
    # read, same pattern safe_num() already uses for every other live
    # scalar read in this file. Falls back to whatever the resampler
    # already produced (never crashes, never worse than before this
    # fix) if the state itself is unavailable/unparseable.
    #
    # PARTIAL-REGRESSION FIX (2026-08-27, issue #220 reopened, Mark
    # Purcell): the first version of this fix only touched index 0.
    # build_tiered_grid()'s own tier-0 stretch runs 1-minute periods from
    # "now" up to the next clean 5-minute mark (0-4 extra periods,
    # depending on where "now" falls) -- EVERY one of those tier-0 rows
    # is still inside the SAME real NEM 5-minute settlement block as
    # period 0, not a future one, so they need the identical settled-
    # state override, not the forecast-array value. Confirmed live on
    # Mark's install (2026-08-27 17:33 AEST): rows at :32 (period 0) held
    # the correct identity, but :33/:34 (still inside the [17:30,17:35)
    # block) had already fallen back to the blended forecast value.
    # Computed directly from grid_times rather than threading a new
    # "how many tier-0 rows" value out of build_tiered_grid() -- tier-0
    # rows are 1-minute apart and never cross a 5-minute mark by
    # construction, so counting how many leading grid_times share
    # period 0's own 5-minute bucket is exactly equivalent and needs no
    # change to that function's own signature.
    _block_start = grid_times[0].replace(
        minute=(_local(grid_times[0]).minute // 5) * 5, second=0, microsecond=0
    )
    _block_end = _block_start + timedelta(minutes=5)
    n_settled_periods = sum(1 for t in grid_times if _block_start <= t < _block_end)
    # 2026-09-02 audit: this used to branch on has_localvolts (now
    # has_price_forecast_array) between a hardcoded LocalVolts literal
    # and the generic field -- collapsed to just the generic field
    # unconditionally, since CONF_SOLVER_IMPORT_PRICE_SENSOR/EXPORT_
    # PRICE_SENSOR are ALREADY the exact same real entities the
    # hardcoded literals pointed at on this household's own live
    # install (confirmed live before this change), and are the correct,
    # portable source for every other install too -- a genuine
    # simplification, not just a hardcode removal.
    settled_import_sensor = cfg["solver_import_price_sensor"]
    settled_export_sensor = cfg["solver_export_price_sensor"]
    _settled_import_value = safe_num(settled_import_sensor, fallback=spot_import_raw[0])
    _settled_export_value = safe_num(settled_export_sensor, fallback=spot_export[0])
    for _i in range(n_settled_periods):
        spot_import_raw[_i] = _settled_import_value
        spot_export[_i] = _settled_export_value

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
    spot_import_raw, import_price_cross_spread, import_price_source = (
        blend_price_with_secondary_sources(
            spot_import_raw,
            cfg,
            ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
            grid_times,
            primary_real_mask=import_real_mask,
        )
    )
    spot_export, export_price_cross_spread, export_price_source = (
        blend_price_with_secondary_sources(
            spot_export,
            cfg,
            ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
            grid_times,
            primary_real_mask=export_real_mask,
        )
    )
    # Re-assert the settled current-block value (nimbus repo issue #220):
    # blend_price_with_secondary_sources() has no notion of "index 0 is
    # now" (or, post-regression-fix, "indices 0..n_settled_periods-1 are
    # all still now") and may have blended any of them like any other
    # period if a secondary source happened to report real coverage
    # there too -- the settled value set above is never a valid blend
    # target, so re-apply it here as the final word regardless of what
    # the blend step did, across every row inside the current NEM
    # settlement block, not just index 0. Its own source label is
    # likewise forced back to "primary" (nimbus issue #631) -- a settled
    # value is by definition the primary source's own contractual
    # figure, never a blend, regardless of what the blend step above
    # happened to label it.
    for _i in range(n_settled_periods):
        spot_import_raw[_i] = spot_import_source[_i]
        spot_export[_i] = spot_export_source[_i]
        import_price_source[_i] = "primary"
        export_price_source[_i] = "primary"

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
        spot_import_raw[i]
        + import_fee_rate(cfg, _local(grid_times[i]).hour)
        + flat_fee_rate
        for i in range(n_periods)
    ]

    # 2026-08-20: reads Nimbus's own real Solver settings config-flow
    # (see fetch_solver_config()'s own docstring for the full "close this
    # gap... need its own installer and inputs period" context) --
    # REPLACES the old ad-hoc input_number.nimbus_solver_* helpers, which
    # had to be hand-created via a separate, undocumented YAML package
    # file. A fresh install now needs nothing more than filling in
    # Nimbus's own hub "Configure" -> "Solver settings" form.
    # nimbus issue #1013: SoH-derated. This is the LIVE dispatch path --
    # the one where a phantom ceiling actually costs something, since
    # #1012 established this pack reaches both rails every single day.
    # See resolve_effective_capacity_kwh()'s docstring.
    capacity_kwh = resolve_effective_capacity_kwh(cfg)
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

    # nimbus issue #348 (Mark Purcell, codebase review, fixed 2026-09-04):
    # this used to call battery_discharge_cost_rate()/battery_salvage_
    # value_rate(), hardcoded Python-constant functions with zero way to
    # retune the schedule short of editing source -- see
    # scheduled_discharge_cost_rate()'s/scheduled_salvage_value_rate()'s
    # own docstrings (near DISCHARGE_COST_SCHEDULE_BLOCK_KEYS, above) for
    # the full story. Byte-identical output for this household (and any
    # other has_price_forecast_array install that has never touched the
    # new fields) -- every new config key's own schema default reproduces
    # the exact historical 5pm/midnight/7am schedule. Still deliberately
    # gated on has_price_forecast_array, same as before this fix: a
    # generic install without that sensor configured is unaffected,
    # still reading the flat solver_discharge_cost/solver_salvage_value
    # fields in the `else` branch below -- silently applying ANY day/
    # night schedule (even a wizard-configurable one) to an install that
    # never asked for one would be a real, unrequested behaviour change.
    if has_price_forecast_array:
        discharge_cost_arr = np.array(
            [scheduled_discharge_cost_rate(cfg, _local(t).hour) for t in grid_times]
        )
        salvage_value = scheduled_salvage_value_rate(cfg, _local(grid_times[-1]).hour)
    else:
        # FALLBACK (2026-08-20, for anyone else): flat values straight
        # from the config-flow's own Economic Policy step -- no day/night
        # schedule (that's tuned specifically around this household's own
        # P2P window, no portable equivalent yet).
        discharge_cost_arr = np.full(
            n_periods, _cfg_num(cfg, "solver_discharge_cost", 0.01)
        )
        salvage_value = _cfg_num(cfg, "solver_salvage_value", 0.15)

    # nimbus issue #493 (Signals 4/7 of #489, item 1): a real per-period
    # array whenever a live DNSP envelope entity is configured (blank ==
    # the exact same flat static value as before this issue, at every
    # period -- byte-identical behaviour for any install that hasn't
    # touched the new field). See resolve_envelope_limit_kw()'s own
    # docstring for the fallback/unit-scaling/log-once behaviour. The
    # static scalars are kept alongside the resolved arrays -- compute_
    # cost_band()'s own read-only #630 diagnostic (below) takes a single
    # flat limit for its whole window by design (out of this issue's own
    # scope to change), so it keeps reading the plain configured value,
    # not the live envelope.
    static_import_limit_kw = float(cfg["solver_grid_max_import_kw"])
    static_export_limit_kw = float(cfg["solver_grid_max_export_kw"])
    import_limit_kw = resolve_envelope_limit_kw(
        cfg.get("solver_envelope_import_limit_entity"),
        static_import_limit_kw,
        grid_times,
    )
    export_limit_kw = resolve_envelope_limit_kw(
        cfg.get("solver_envelope_export_limit_entity"),
        static_export_limit_kw,
        grid_times,
    )

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
    # nimbus issue #1213 (Mark Purcell): the price-event simulation, applied
    # here -- at the very END of price resolution, after the fallback
    # branches, after blend_price_with_secondary_sources(), and after the
    # settled-current-block re-assertion above.
    #
    # Deliberately last, and deliberately additive. Last, so it never has
    # to know about or interact with any of the blending/settlement logic
    # that produced these two series. Additive and symmetric -- the same
    # delta on both sides -- because a real wholesale price event moves the
    # whole market rather than one side of it.
    #
    # Before the empirical/risk bands below, on purpose: a +$20/kWh cap
    # event should shift its own uncertainty band with it, not leave the
    # band sitting around the un-shifted price.
    #
    # None (the overwhelmingly common case: switch off, or no sensor) skips
    # every line of this. See resolve_price_event_delta().
    _price_event_delta = resolve_price_event_delta(cfg, grid_times)
    if _price_event_delta is not None:
        import_price = [p + d for p, d in zip(import_price, _price_event_delta)]
        export_price = [p + d for p, d in zip(export_price, _price_event_delta)]
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
    # band a has_price_forecast_array install already built. A generic install with
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

    # nimbus issue #735 stage 5: the SoC-envelope slice (configured
    # percents to kWh, the #328 honest pass-through, the #601 warn-once
    # excursion log, the separate PHYSICAL clamp, and the round-trip
    # efficiency) lives in solver_inputs/battery_soc.py. Five inputs,
    # four outputs -- measured narrower than either seam already
    # extracted. The element construction immediately below is
    # DELIBERATELY not extracted with it: the same measurement puts it
    # at 27 inputs, which would be a worse call site than the inline
    # code. See that module's own docstring for the numbers.
    _soc_envelope = battery_soc_inputs.resolve_soc_envelope(
        cfg,
        capacity_kwh=capacity_kwh,
        min_pct=min_pct,
        max_pct=max_pct,
        initial_pct=initial_pct,
    )
    initial_soc_kwh = _soc_envelope.initial_soc_kwh
    min_soc_kwh_val = _soc_envelope.min_soc_kwh
    max_soc_kwh_val = _soc_envelope.max_soc_kwh
    charge_discharge_efficiency = _soc_envelope.charge_discharge_efficiency
    # nimbus issue #567 (issue #694: now wins over P2P, see
    # resolve_price_spike_override()'s own docstring): real-time "sell
    # into a price spike, right now" override. spike_detected is
    # published below regardless of whether spike_override_kw ends up
    # armed/active, so the dashboard's own "$$$" visibility signal fires
    # purely on detection.
    spike_override_kw, price_spike_active = resolve_price_spike_override(
        cfg, import_price[0]
    )
    battery = elements.BatteryConfig(
        name="home",  # nimbus issue #467: single real household battery, see battery_cfg's own comment above
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
        spike_override_discharge_kw=spike_override_kw,
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
    # comment). proximal_weight now reads live from cfg (2026-09-08,
    # number.nimbus_solver_proximal_weight_kw, dashboard-editable) --
    # falls back to network.py's own DEFAULT_PROXIMAL_WEIGHT_KW, the
    # exact value this always silently used before, so an already-
    # configured household sees zero behaviour change until they
    # actually tune it. Same real gap, same fix, as smoothness_weight
    # just below. max_rate_kw deliberately NOT used here -- a hard
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
    proximal_weight = _cfg_num(
        cfg, "solver_proximal_weight_kw", network.DEFAULT_PROXIMAL_WEIGHT_KW
    )
    # smoothness_weight (2026-08-20, real household finding: "why nimbus
    # decided to make such decisions and charge in bursts not
    # continuously") -- mechanism 4, same value/reasoning as
    # proximal_weight (mechanism 1) just above, just applied within this
    # solve's own timeline instead of across solves. Locally validated
    # (both repo's own scratchpad and nimbus's own committed tests):
    # eliminates a real, reconstructed degenerate burst at byte-identical
    # total_cost, and does NOT smear a genuine, large, real transition
    # (an 80kW price-step scenario, on or off, within $0.07 either way).
    # 2026-09-08 (real household finding, NUC1's own first day of live
    # dispatch -- see network.py's own _add_intraplan_smoothness_penalty
    # docstring for the exact confirmed-live symptom): now reads live
    # from cfg (number.nimbus_solver_intraplan_smoothness_weight_kw,
    # dashboard-editable) instead of always silently passing network.py's
    # own DEFAULT_SMOOTHNESS_WEIGHT_KW constant -- falls back to that same
    # constant, so an already-configured household sees zero behaviour
    # change until they actually tune it up.
    smoothness_weight = _cfg_num(
        cfg,
        "solver_intraplan_smoothness_weight_kw",
        network.DEFAULT_SMOOTHNESS_WEIGHT_KW,
    )
    # nimbus issue #692 (household, real live plan mishaps: a critically-
    # low battery sitting idle through a perfectly good charging price,
    # only charging later at an equal or worse one): the battery's own
    # earliness tie-break, same live-dashboard-first pattern as
    # proximal_weight/smoothness_weight just above -- falls back to
    # network.py's own default, so an already-configured household sees
    # zero behaviour change until they actually tune it.
    battery_charge_earliness_budget_kw = _cfg_num(
        cfg,
        "solver_battery_charge_earliness_budget_kw",
        network.DEFAULT_BATTERY_CHARGE_EARLINESS_BUDGET_KW,
    )
    # nimbus issue #486: real controllable_load subentries (sheddable/
    # deferrable/thermal kinds -- see build_controllable_loads()'s own
    # docstring), replacing the hardcoded empty lists this call used
    # to pass. Native-mode-only, a real no-op ([], [], []) in standalone/
    # cron mode -- see that function's own docstring for why.
    sheddable_loads, adequacy_loads, thermal_loads = build_controllable_loads(
        now, grid_times, n_periods, import_price
    )
    # nimbus issue #563: real battery_participant subentries, in
    # addition to the household's own single "home" battery above --
    # see build_extra_batteries()'s own docstring for the full "upgrade
    # is a no-op with zero subentries" story. Availability gating and the
    # shared-charger power constraint (items 2/3, deferred out of #566)
    # now live in BatteryConfig/network.py -- periods is passed through
    # so a configured departure_hour can be resolved to a real period
    # index against THIS solve's own horizon. "home" (batteries[0]) is
    # the only participant this P2P fixed-export window logic below ever
    # targets -- see BatteryConfig's own docstring / build_plan()'s own
    # "batteries" docstring paragraph for that explicit #467 stage-1
    # decision.
    all_batteries = [battery, *build_extra_batteries(periods)]
    _LOGGER.debug(
        "Nimbus #757 diag: main() all_batteries after merge = %s (periods=%r)",
        [b.name for b in all_batteries],
        "set" if periods is not None else None,
    )
    # nimbus issue #569 (Mark Purcell, found live within hours of #563
    # landing): plan.battery_soc_kwh is the SUMMED aggregate across every
    # battery (per #467 stage 1's own contract), but every percentage
    # derived from it downstream (soc_pct, equivalent_full_cycles) was
    # still dividing by capacity_kwh -- the "home" battery ALONE, a
    # holdover from before #563 ever existed. Real live symptom: with a
    # 40.3 kWh home pack + two 60 kWh EVs (160.3 kWh fleet), soc_pct read
    # 296% instead of ~75%, and a real household automation
    # (automation.nimbus_battery_soc_control_ecoflow) started rejecting
    # every write to number.ecoflow_backup_reserve_level (outside its
    # valid 22-100 range) every single solve. Fixed at the source: a real
    # fleet-total capacity, computed once here from the same battery list
    # build_plan() itself just solved against, threaded through publish_
    # plan() as its own parameter rather than overloading capacity_kwh
    # (which stays the home battery's own capacity for whatever legitimately
    # still needs just that -- see publish_plan()'s own parameter list).
    fleet_capacity_kwh = sum(b.capacity_kwh for b in all_batteries)
    # nimbus issue #494 (Signals 5/7 of #489): opt-in, off by default --
    # see const.py's own comment on CONF_SOLVER_OFFER_CURVE_ENABLED for
    # why. Same live-switch-first read as auto_include_known_solar (which
    # moved to solver_inputs/solar.py's build_solar_arrays() in #735
    # stage 1, so it is no longer "above" in this function).
    offer_curve_enabled = bool(cfg.get("solver_offer_curve_enabled"))
    # nimbus issue #496 (Signals 7/7 of #489): opt-in, off by default --
    # see const.py's own comment on CONF_SOLVER_FLEX_SIGNALS_ENABLED for
    # why this one is deliberately NOT just "same reasoning as offer
    # curve" -- a real, live capacity concern (#773), not only convention.
    flex_signals_enabled = bool(cfg.get("solver_flex_signals_enabled"))
    # nimbus issue #696, Stage 2: default TRUE (unlike offer_curve_
    # enabled above) -- see const.py's own comment on CONF_SOLVER_
    # CALIBRATED_OBJECTIVE_ENABLED for the full "household's own
    # explicit, repeated ask to make this the real default now" story.
    # network.build_plan()'s own solve_options= docstring covers the
    # one real scope boundary this switch doesn't override: a solve
    # that ends up a MIP (adequacy loads present, semi-continuous
    # default on) silently keeps today's hand-tuned-magnitude behavior
    # regardless of this switch's state.
    calibrated_objective_enabled = bool(
        cfg.get("solver_calibrated_objective_enabled", True)
    )
    solve_options = lp.CalibratedOptions() if calibrated_objective_enabled else None
    plan = network.build_plan(
        periods=periods,
        grid=grid,
        batteries=all_batteries,
        solar=solar,
        loads=loads,
        sheddable_loads=sheddable_loads,
        adequacy_loads=adequacy_loads,
        thermal_loads=thermal_loads,
        previous_plan=previous_plan,
        risk_aversion=risk_aversion,
        import_price_risk_aversion=import_price_risk_aversion,
        export_price_risk_aversion=export_price_risk_aversion,
        proximal_weight=proximal_weight,
        smoothness_weight=smoothness_weight,
        battery_charge_earliness_budget_kw=battery_charge_earliness_budget_kw,
        compute_offer_curve=offer_curve_enabled,
        compute_signals=flex_signals_enabled,
        solve_options=solve_options,
    )
    _LOGGER.debug(
        "Nimbus #757 diag: plan.batteries immediately after build_plan() returns = %s (status=%r)",
        [b.name for b in plan.batteries],
        plan.status,
    )
    # nimbus issue #483, item 2: tariff-attributed cost needs the same
    # grid_to_load flow this function's own publish_plan() call below
    # independently recomputes for the published forecast table --
    # duplicated here (not threaded through publish_plan()'s own return,
    # which doesn't exist today and isn't worth adding) specifically
    # because apply_commanded_state_guard() below has to run BEFORE
    # publish_plan() (the comment on that call explains why: real
    # relay-dispatch timing, can't wait on the much larger diagnostic-
    # publishing pass). Uses plan.battery_charge_kw/discharge_kw
    # directly (not publish_plan()'s own corrected_battery_*, which
    # only ever differs from the raw plan values during a rare
    # historical-incident defensive clamp, see that variable's own
    # comment) -- KNOWN DRIFT RISK, same accepted tradeoff #582's own
    # duplicated same-day-window logic already carries in this file.
    _flow_decomp_for_tariff = [
        _flow_decomposition(
            float(solar_kw[i]),
            float(load_kw[i]),
            float(plan.battery_charge_kw[i]),
            float(plan.battery_discharge_kw[i]),
            grid_export_kw_i=float(plan.grid_export_kw[i]),
        )
        for i in range(n_periods)
    ]
    tariff_attributed_cost_by_subentry = compute_tariff_attributed_cost(
        plan.adequacy_loads,
        np.array([f["grid_to_load"] for f in _flow_decomp_for_tariff]),
        load_kw,
        import_price,
        period_hours_arr,
    )
    # nimbus issue #484: the relay-chatter guard, run once per solve
    # right after the plan exists -- needs the plan's own just-solved
    # period-0 power per load, so it can't run any earlier than this.
    apply_commanded_state_guard(
        plan,
        now,
        grid_times,
        period_hours_arr,
        import_price,
        tariff_attributed_cost_by_subentry=tariff_attributed_cost_by_subentry,
        cfg=cfg,
    )
    publish_plan(
        cfg=cfg,
        now=now,
        plan=plan,
        previous_plan=previous_plan,
        solve_started=solve_started,
        period_hours_arr=period_hours_arr,
        grid_times=grid_times,
        n_periods=n_periods,
        capacity_kwh=capacity_kwh,
        fleet_capacity_kwh=fleet_capacity_kwh,
        battery_capacity_by_name={b.name: b.capacity_kwh for b in all_batteries},
        charge_discharge_efficiency=charge_discharge_efficiency,
        grid=grid,
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
        static_import_limit_kw=static_import_limit_kw,
        static_export_limit_kw=static_export_limit_kw,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        charge_cost=charge_cost,
        discharge_cost_arr=discharge_cost_arr,
        salvage_value=salvage_value,
        risk_aversion=risk_aversion,
        import_price_risk_aversion=import_price_risk_aversion,
        export_price_risk_aversion=export_price_risk_aversion,
        import_price=import_price,
        export_price=export_price,
        spot_import_source=spot_import_source,
        spot_export_source=spot_export_source,
        import_price_source=import_price_source,
        export_price_source=export_price_source,
        export_bonus_price=export_bonus_price,
        load_kw=load_kw,
        solar_kw=solar_kw,
        load_lower_kw=load_lower_kw,
        load_upper_kw=load_upper_kw,
        initial_soc_kwh=initial_soc_kwh,
        match_fraction=match_fraction,
        summed_18_now_kw=summed_18_now_kw,
        whole_house_now_kw=whole_house_now_kw,
        live_load_kw=live_load_kw,
        load_forecast_coverage_hours=load_forecast_coverage_hours,
        load_forecast_error=load_forecast_error,
        load_forecast_source_used=load_forecast_source_used,
        load_forecast_warnings=load_forecast_warnings,
        failed_load_entities=failed_load_entities,
        n_clamped=n_clamped,
        solar_delivery=solar_delivery,
        p2p_recent_volume_kwh=p2p_recent_volume_kwh,
        price_spike_active=price_spike_active,
    )
    # nimbus issue #494 (Signals 5/7 of #489): no-op unless offer_curve_
    # enabled was true above (plan.offer_curve_import stays None
    # otherwise) -- see publish_offer_curve()'s own docstring.
    publish_offer_curve(plan)
    # nimbus issue #496 (Signals 7/7 of #489): no-op unless flex_signals_
    # enabled was true above (plan.grid_signals stays None otherwise) --
    # see publish_flex_signals()'s own docstring.
    publish_flex_signals(plan)


if __name__ == "__main__":
    if not acquire_lock():
        print(
            f"[{datetime.now(UTC).astimezone(LOCAL_TZ).isoformat()}] previous run still in progress -- skipping this tick",
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
