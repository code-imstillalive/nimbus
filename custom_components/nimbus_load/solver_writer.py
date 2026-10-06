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
import itertools
import json
import math
import os
import re
import statistics
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
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

    # nimbus #1302 (spec 003): the eight bodies that USED this alias moved to
    # solver_inputs/controllable_loads.py, so nothing in this file reads it any
    # more -- but spec 001's identity invariant says every name in scope before a
    # relocation still resolves after it, and tests/test_solver_writer_
    # controllable_loads.py::TestParseDoneWhen reads solver_writer._parse_done_when.
    # Kept as a re-export rather than retargeting those six tests.
    from .done_condition import parse_done_when as _parse_done_when
except ImportError:
    from done_condition import (
        ATTRIBUTE_DONE_DOMAINS as _done_condition_attribute_domains,
    )
    from done_condition import parse_done_when as _parse_done_when  # noqa: F401

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
# nimbus issue #1259: the nowcast-disagreement measurement lives in
# solver_inputs/solar.py (see the comment at its definition for why it was
# placed there rather than added here). Re-exported so this file's own call
# site and the tests that reach solver_writer.update_solar_nowcast_
# disagreement() resolve the identical object.
update_solar_nowcast_disagreement = solar_inputs_solar.update_solar_nowcast_disagreement
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
# nimbus issue #1303 (Phase 4 of #1298): the `extra_batteries as
# extra_batteries_inputs` import that used to sit here is gone. main()'s
# plan-assembly span was its only reader in this module, and that span now lives
# in solver_plan.py, which imports it directly. Checked before removing: nothing
# reaches it as `sw.extra_batteries_inputs`, and the tests that use it import it
# straight from `solver_inputs`. `battery_participants` below is NOT the same
# case -- it IS reached as `sw.<name>` from solver_reports/, so it keeps its
# re-export.
try:
    from .solver_inputs import battery_participants as battery_participants_inputs
except ImportError:
    from solver_inputs import (  # type: ignore[no-redef]
        battery_participants as battery_participants_inputs,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
    )
# nimbus issue #768: the controllable-load counterpart of the module above --
# what each Controllable Load really delivered on an already-elapsed day,
# reconstructed from its own power sensor's recorder history. Measurement
# only; nothing in it reaches the oracle yet. See its own docstring for
# Mark Purcell's own sequencing decision (2026-09-27) and why.
try:
    from .solver_inputs import controllable_load_history
except ImportError:
    from solver_inputs import (  # type: ignore[no-redef]
        controllable_load_history,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
    )
# nimbus issue #1301, Phase 2b of #1298's decomposition: the three
# independent reporting subsystems (efficiency backtest, Nimbus-only SoC
# counterfactual, flex report) moved verbatim into solver_reports/. Same
# dual-mode, imported-as-a-MODULE pattern as every sibling above -- see
# solver_reports/__init__.py for why the import direction is what it is.
#
# The aliases below are what keep this a pure relocation: every existing
# call site in this file, and every `patch.object(solver_writer, "<name>",
# ...)` in the suite, keeps resolving the identical object it always did.
try:
    from .solver_reports import backtest as backtest_reports
except ImportError:
    from solver_reports import backtest as backtest_reports  # type: ignore[no-redef]
try:
    from .solver_reports import counterfactual as counterfactual_reports
except ImportError:
    from solver_reports import (  # type: ignore[no-redef]
        counterfactual as counterfactual_reports,
    )
try:
    from .solver_reports import flex as flex_reports
except ImportError:
    from solver_reports import flex as flex_reports  # type: ignore[no-redef]

try:
    from .solver_reports import quality as quality_reports
except ImportError:
    from solver_reports import quality as quality_reports  # type: ignore[no-redef]

# nimbus issue #1301 Phase 2c: the prior-read status constants moved into
# solver_reports/quality.py with their only callers. PRIOR_READ_OK in
# particular HAD to move: it is a DEFAULT ARGUMENT of
# _carry_forward_quality_history(), so it is evaluated at function-definition
# time, when the deferred `sw` accessor is not yet bound. Aliased back here
# because they are public names; immutable strings, and no test patches them.
PRIOR_READ_OK = quality_reports.PRIOR_READ_OK
PRIOR_READ_ABSENT = quality_reports.PRIOR_READ_ABSENT
PRIOR_READ_UNAVAILABLE = quality_reports.PRIOR_READ_UNAVAILABLE
PRIOR_READ_UNREACHABLE = quality_reports.PRIOR_READ_UNREACHABLE
_PRIOR_READ_DEGRADED = quality_reports._PRIOR_READ_DEGRADED
_compute_report_for_window = quality_reports._compute_report_for_window
_soc_discrepancy_stats = quality_reports._soc_discrepancy_stats
_carry_forward_quality_history = quality_reports._carry_forward_quality_history
rescore_quality_history = quality_reports.rescore_quality_history
publish_daily_quality_report = quality_reports.publish_daily_quality_report
compute_efficiency_backtest_report = backtest_reports.compute_efficiency_backtest_report
compute_nimbus_only_soc_counterfactual = (
    counterfactual_reports.compute_nimbus_only_soc_counterfactual
)
_compute_flex_report_for_window = flex_reports._compute_flex_report_for_window
FLEX_SIGNALS_ENTITY_ID = flex_reports.FLEX_SIGNALS_ENTITY_ID
_PRICE_BAND_WIDTH = flex_reports._PRICE_BAND_WIDTH
# nimbus issue #485: household-mode presets. Pure, HA-free table +
# two apply functions; same dual-mode import as every other
# project-internal module here.
try:
    from . import household_modes
except ImportError:
    import household_modes  # type: ignore[no-redef]

# nimbus issue #495 (Signals 6/7 of #489): the nem-flex-telemetry
# schema-v2.0 record shape. Pure stdlib, no HA and no other Nimbus
# module -- see that module's own docstring for the three measurement
# decisions it owns. Same dual-mode import as household_modes above.
try:
    from . import flex_telemetry
except ImportError:
    import flex_telemetry  # type: ignore[no-redef]

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
# nimbus issue #495: the flex-telemetry publish lives in solver_publish.py
# -- see the comment at its definition for why it was placed there rather
# than added to this file. Re-exported so existing call sites and the three
# tests that call solver_writer.publish_flex_telemetry_record() resolve the
# identical object.
publish_flex_telemetry_record = solver_publish.publish_flex_telemetry_record
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

# nimbus issue #1301 (Phase 2, spec 001, #1347): the HA-I/O, config-
# resolution and small-utility core this file used to define directly now
# lives in solver_shared.py -- see that module's own docstring for the
# full "why" and the façade decision (48 ha_get / 29+ ha_post_state
# monkeypatch sites make retargeting every call site in the same PR a
# large, error-prone diff for zero behavioural gain). Every name below
# resolves to the IDENTICAL object it always has -- this is a pure
# relocation, not a redesign, and every existing call site in this file
# (and every existing `patch.object(solver_writer, "<name>", ...)`-style
# test) continues to resolve exactly as before.
#
# `_LOGGER` is included as a genuine alias, not a separately-created
# logger -- `solver_writer._LOGGER is solver_shared._LOGGER`, confirmed
# by a real identity test. An earlier draft of this had `_LOGGER` create
# its own independent `logging.getLogger(__name__)` here on the theory
# that some test asserts on a logger's own name; peer review checked that
# rather than assuming it and found the opposite is true (zero such
# tests) while four existing suites capture log lines via `assertLogs(
# solver_writer._LOGGER, ...)` on modules that log through solver_shared's
# logger after this same spec repoints them -- aliasing is what keeps
# those suites passing, not what breaks them.
try:
    from .solver_shared import (
        _ENTITY_UPDATE_HANDLERS,
        _LOGGER,
        _MAX_STATE_ATTRS_BYTES,
        _NATIVE_MANAGED_ENTITY_IDS,
        _OVERSIZE_ATTRS_WARNED,
        _SOH_RANGE_WARNED,
        LOCAL_TZ,
        MAX_TIER1_HOURS,
        NATIVE,
        NETWORK_FEE_BLOCK_KEYS,
        P2P_BLOCK_KEYS,
        TIER1_PERIOD_HOURS,
        TIER2_PERIOD_HOURS,
        _cfg_int,
        _cfg_num,
        _kw_scale_factor,
        _local,
        _native_http_error,
        _nimbus_version,
        _version_stamp,
        _warn_if_attrs_exceed_recorder_cap,
        fetch_entity_attribute_history_range,
        fetch_entity_history_range,
        fetch_p2p_fixed_export_kw,
        ha_get,
        ha_post_state,
        import_fee_rate,
        p2p_blocks_daily_energy_kwh,
        p2p_bonus_price_by_period,
        parse_iso,
        resample_history_mean,
        resample_history_nearest,
        resolve_effective_capacity_kwh,
        safe_num,
    )
except ImportError:
    from solver_shared import (  # type: ignore[no-redef]
        _ENTITY_UPDATE_HANDLERS,
        _LOGGER,
        _MAX_STATE_ATTRS_BYTES,  # noqa: F401 -- re-export, see comment below
        _NATIVE_MANAGED_ENTITY_IDS,  # noqa: F401 -- re-export, see comment below
        _OVERSIZE_ATTRS_WARNED,  # noqa: F401 -- re-export, see comment below
        _SOH_RANGE_WARNED,  # noqa: F401 -- re-export, see comment below
        LOCAL_TZ,
        MAX_TIER1_HOURS,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        NATIVE,
        NETWORK_FEE_BLOCK_KEYS,  # noqa: F401 -- re-export, see comment below
        P2P_BLOCK_KEYS,  # noqa: F401 -- re-export, see comment below
        TIER1_PERIOD_HOURS,
        TIER2_PERIOD_HOURS,
        _cfg_int,
        _cfg_num,
        _kw_scale_factor,
        _local,
        _native_http_error,  # noqa: F401 -- re-export, see comment below
        _nimbus_version,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        _version_stamp,
        _warn_if_attrs_exceed_recorder_cap,  # noqa: F401 -- re-export, see comment below
        fetch_entity_attribute_history_range,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        fetch_entity_history_range,
        fetch_p2p_fixed_export_kw,  # noqa: F401 -- re-export: main()'s own call
        # moved to solver_plan.py (#1303); 8 test files still call it via THIS module
        ha_get,
        ha_post_state,
        import_fee_rate,
        p2p_blocks_daily_energy_kwh,  # noqa: F401 -- re-export: reached as sw.<name> from solver_inputs/prices.py (#1537)
        p2p_bonus_price_by_period,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        parse_iso,
        resample_history_mean,
        resample_history_nearest,
        resolve_effective_capacity_kwh,
        safe_num,
    )

# The F401 exceptions marked above (NETWORK_FEE_BLOCK_KEYS, P2P_BLOCK_KEYS,
# _MAX_STATE_ATTRS_BYTES, _native_http_error, _NATIVE_MANAGED_ENTITY_IDS,
# _OVERSIZE_ATTRS_WARNED, _SOH_RANGE_WARNED, _warn_if_attrs_exceed_
# recorder_cap): these 8 names have zero remaining bare-name callers in
# THIS file after the move -- every real use now lives in solver_shared.py
# alongside the function that reads them. They stay re-exported here
# anyway, same as every other name above, because the façade decision
# (this file's own docstring reference above) is "every name keeps a
# re-export," not "every name a caller still uses" -- an external
# consumer (this repo's own tests, e.g. `solver_writer._SOH_RANGE_WARNED`
# in test_effective_capacity_soh_derating.py) reaching for the OLD
# location must keep working. Ruff's F401 cannot see a caller in a
# DIFFERENT file, so it reads these as dead imports; `# X as X` (the
# usual explicit-re-export idiom) was tried and rejected, since ruff's
# own PLC0414 (useless-import-alias) flags that same pattern outside
# `__init__.py` -- the two rules disagree here, and marking `__all__` for
# a file this size, used this many other ways, was judged a bigger and
# riskier change than this migration's own "pure relocation" scope. A
# per-line noqa, this repo's own established convention for exactly this
# kind of individually-justified exception (see this file's own BLE001


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
    from .solver import cycle_lock, elements, lp, network, nowcast_skill
    from .solver.backtest import (
        EFFICIENCY_CANDIDATES_PERCENT,
        efficiency_label,
        run_efficiency_sensitivity_sweep,
    )
    from .solver.quality_report import compute_quality_report
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
    from solver import (  # noqa: F401 -- `lp` is a re-export: main()'s own
        # lp.CalibratedOptions() call moved to solver_plan.py (#1303), but
        # test_solver_writer_smoothness_and_proximal_weight_wiring.py asserts
        # against solver_writer.lp.CalibratedOptions at :215 and :225.
        cycle_lock,
        elements,
        lp,
        network,
        nowcast_skill,
    )
    from solver.backtest import (
        EFFICIENCY_CANDIDATES_PERCENT,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        efficiency_label,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        run_efficiency_sensitivity_sweep,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
    )
    from solver.quality_report import (
        compute_quality_report,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
    )

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


# nimbus issue #1302 (spec 003, Phase 3 of #1298): these eight moved to
# solver_inputs/controllable_loads.py. Re-exported here for all eight rather
# than only the three the caller-count criterion strictly requires, because
# main() calls build_controllable_loads as a bare name and two test files
# monkeypatch resolve_controllable_load_power_sensor and NATIVE.hass (formerly _NATIVE_HASS) on THIS
# module -- leaving those resolving through one import is cheaper than
# rewriting the call sites, and one import block beats two.
# nimbus issue #1303 (spec 004, Phase 4 of #1298): plan assembly moved to
# solver_plan.py. Imported as a MODULE for the call in main(), and
# terminal_value_breakpoints_for re-exported by NAME because
# solver_reports/counterfactual.py reaches it as sw.terminal_value_
# breakpoints_for through the deferred seam -- that facade is load-bearing.
# midnight_boundary_period_indices and RISK_AVERSION moved too and need no
# facade: zero code references either one outside solver_plan.py.
try:
    from . import solver_plan
    from .solver_plan import terminal_value_breakpoints_for
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_plan  # type: ignore[no-redef]
    from solver_plan import (  # type: ignore[no-redef]  # noqa: F401 -- re-export (#1303)
        terminal_value_breakpoints_for,
    )


try:
    from .solver_inputs.controllable_loads import (
        _build_daily_adequacy_windows,
        _earliest_period_for_same_day_window,
        _evaluate_done_condition,
        _resolve_controllable_load_tuning,
        _resolve_hour_to_period_index,
        _sample_load_run_state,
        build_controllable_loads,
        resolve_controllable_load_power_sensor,
    )
except ImportError:  # pragma: no cover - standalone/cron path
    from solver_inputs.controllable_loads import (  # type: ignore[no-redef]  # noqa: F401 -- re-export (#1302)
        _build_daily_adequacy_windows,
        _earliest_period_for_same_day_window,
        _evaluate_done_condition,
        _resolve_controllable_load_tuning,
        _resolve_hour_to_period_index,
        _sample_load_run_state,
        build_controllable_loads,
        resolve_controllable_load_power_sensor,
    )


# nimbus issue #1305 (spec 006, Phase 6 of #1298): the controllable-load dispatch
# guard moved to solver_dispatch/guard.py. Two names are re-exported -- measured
# per name rather than following spec 006's blanket "no facade, retarget the test
# files":
#
#   apply_commanded_state_guard   main() below calls it as a bare name, and three
#                                 test files reach it as solver_writer.apply_
#                                 commanded_state_guard -- one via
#                                 inspect.getsource() on it.
#   _REAFFIRM_CAP_WARNED          test_reaffirm_cap_exhaustion_is_silent.py:187
#                                 does solver_writer._REAFFIRM_CAP_WARNED.clear().
#                                 An alias is exactly right: the set is mutated in
#                                 place, never rebound, so identity holds.
#
# _FLOOR_CROSSING_WARNED, dispatch_commanded_state and _resolve_reaffirm_after_
# seconds are NOT re-exported -- zero references to any of them outside the moved
# code, confirmed by grep over both the package and tests/.
try:
    from .solver_dispatch.guard import (
        _REAFFIRM_CAP_WARNED,
        apply_commanded_state_guard,
    )
except ImportError:  # pragma: no cover - standalone/cron path
    from solver_dispatch.guard import (  # type: ignore[no-redef]
        _REAFFIRM_CAP_WARNED,  # noqa: F401 -- re-export (#1305)
        apply_commanded_state_guard,
    )


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


# How far below-or-at its own committed rate grid_export[0] may sit and
# still be called "pinned by the P2P commitment" (nimbus issue #921).
# Not a float-equality epsilon: this is one variable out of a ~12,000-
# column two-phase MIP, and the residual on a real instance is nothing
# like the exact bound a small synthetic LP returns. 10 W is far below
# anything a household could act on and far above any plausible solver
# residual on a 12 kW commitment.


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
    fetch_price_history()) -- native mode's own NATIVE.hass seam skips
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
# NATIVE.hass defaults to None (#1437; formerly _NATIVE_HASS), and
# every one of the ~2400 lines below still just calls ha_get(...)/
# ha_post_state(...)/fetch_price_history(...) by name, exactly as it
# always has. Only what THOSE THREE functions do internally branches on
# whether a real hass instance has been injected.
# nimbus issue #1437: the injected `hass` now lives on `solver_shared.NATIVE`,
# a holder whose IDENTITY never changes -- see that class's own docstring.
# `set_native_hass()` below mutates `NATIVE.hass`; nothing rebinds a module
# name any more. `NATIVE.hass is None` = standalone/REST mode (the default).

# stdlib logging.Logger, NOT this file's own print() convention -- deliberately
# so the #85 trace below (which used to be print()-only, and per issue #85's
# own thread was "cannot be surfaced via HA log API (print -> stdout, not
# _LOGGER); ignore") actually lands somewhere ha_get_logs()/HA's error_log can
# see it in native mode. logging.getLogger() is plain stdlib, not an HA
# import, so this doesn't compromise the standalone/cron/addon path's own
# "zero HA imports" requirement -- in that mode nothing configures a handler
# for this logger, so it's silent by default exactly as before. In native
# mode this logger is a child of HA's own logging tree, so `logger.set_level`
# works on it exactly like any other HA component logger.
#
# nimbus issue #1301 (Phase 2, spec 001): `_LOGGER` itself is now created in
# `solver_shared.py` and imported here as a genuine alias (see this file's
# own `from .solver_shared import (...)` block above) -- `solver_writer.
# _LOGGER is solver_shared._LOGGER`, the SAME object, not two independently-
# created loggers. Its real dotted name is therefore `custom_components.
# nimbus_load.solver_shared`, not `...solver_writer` -- set THAT name (or
# the shared `custom_components.nimbus_load` parent) in configuration.yaml
# to control it.

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
    global LOCAL_TZ, _LAST_KNOWN_QUALITY_HISTORY
    NATIVE.hass = hass
    # nimbus issue #1248: a new `hass` is a different instance, so whatever
    # #1248's recovery cache holds describes a report that is no longer the one
    # being published. Carrying it across would let a reconfigure or a reload
    # inject another instance's rows -- the opposite of what the cache is for,
    # which is recovering THIS report's own rows.
    #
    # It also closes the cache's last cross-contamination channel inside a
    # single long-lived process: CI failed on a suite-wide ordering effect that
    # could not be reproduced by running the affected files directly, and a
    # cache that resets whenever the seam is re-pointed cannot carry state
    # between unrelated publishes however the tests are ordered.
    _LAST_KNOWN_QUALITY_HISTORY = {}
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
        except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
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

# Warn once per entity_id per process. The condition is structural -- if
# a payload is over the cap this cycle it will be over it every cycle --
# so repeating it once a minute would be exactly the noise v0.94.297 had
# to clean up for #757.


def ha_call_service(domain: str, service: str, data: dict) -> None:
    """Fire-and-forget HA service call -- same native/REST dual-mode
    split as ha_get()/ha_post_state() above. Currently used only for
    the one-time load-forecast-shape persistent notification (see
    _notify_load_forecast_error_once()) -- any failure here is
    deliberately swallowed by the caller, since a failed notification
    must never be allowed to break the actual solve."""
    if NATIVE.hass is not None:
        NATIVE.hass.add_job(
            functools.partial(NATIVE.hass.services.async_call, domain, service, data)
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
    if NATIVE.hass is not None:
        try:
            import asyncio

            async def _call() -> dict:
                return await NATIVE.hass.services.async_call(
                    domain,
                    service,
                    data,
                    blocking=True,
                    return_response=True,
                )

            future = asyncio.run_coroutine_threadsafe(_call(), NATIVE.hass.loop)
            return future.result(timeout=15)
        except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
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
                # nimbus issue #1293: found by the codebase-wide grep that
                # #1253's "zero hardcoded currency strings" claim should have
                # run, and fixed the OPPOSITE way to every other site.
                #
                # These two numbers are AEMO's own Market Floor Price and
                # Market Price Cap -- Australian market constants, not this
                # household's money. Resolving them against
                # `hass.config.currency` would relabel a genuinely-AUD figure
                # as EUR on a European install, which is a false statement
                # about the value rather than a localisation. So it is pinned
                # to an explicit ISO code instead: honest, unambiguous, and
                # deliberately NOT household-dependent. The bare symbol it
                # replaced was ambiguous across a dozen currencies, which is
                # the same objection #1253 raised everywhere else.
                "unit": "AUD/kWh",
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


# nimbus issue #489: the last payload publish_flex_signals() actually posted,
# re-posted verbatim on a cycle where ranging was deferred to the next 5-minute
# interval (see solver_plan.FLEX_RANGING_INTERVAL_SECONDS). Without it the push
# sensor would go `unavailable` after _STALE_AFTER_SECONDS (300 s) between two
# ranging solves that are ~300 s apart. The payload keeps its own
# `generated_at`, so a held value never claims to be newer than it is.
_LAST_FLEX_SIGNALS_POST: dict = {}


def reset_flex_ranging_cadence() -> None:
    """nimbus issue #489: forget the in-process flex cadence state -- which
    5-minute interval last ranged, and the two held payloads -- so the next
    `main()` ranges and nothing is held over. For tests, which drive `main()`
    many times inside one interval; production never needs it."""
    solver_plan.reset_flex_ranging_state()
    _LAST_FLEX_SIGNALS_POST.clear()
    solver_publish._LAST_FLEX_TELEMETRY_POST.clear()


def publish_flex_signals(plan, *, hold_last: bool = False) -> None:
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
        # nimbus issue #489: ranging deferred to the next 5-minute interval --
        # keep the last real payload alive. NOT when the switch is off
        # (`hold_last` is False then), so switching it off still lets the
        # sensors go stale exactly as before.
        if hold_last and _LAST_FLEX_SIGNALS_POST:
            ha_post_state(
                "sensor.nimbus_flex_signals",
                _LAST_FLEX_SIGNALS_POST["state"],
                dict(_LAST_FLEX_SIGNALS_POST["attributes"]),
            )
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
    state = round(float(gs.flex_available_up_kw[0]), 3)
    attributes = {
        "unit_of_measurement": "kW",
        "friendly_name": "Nimbus Flex Signals",
        "grid_import_headroom_kw": round(float(gs.grid_import_headroom_kw[0]), 3),
        "grid_import_headroom_kwh": round(float(gs.grid_import_headroom_kwh[0]), 3),
        "grid_export_headroom_kw": round(float(gs.grid_export_headroom_kw[0]), 3),
        "grid_export_headroom_kwh": round(float(gs.grid_export_headroom_kwh[0]), 3),
        # nimbus issue #496: whether each headroom figure above is a real
        # band or a zero-width tie. Without these a published 0.0 conflates
        # "genuinely no headroom" with "the ranging could not say", and the
        # two mean opposite things to anyone acting on the number.
        #
        # Measured on the reference household 2026-09-27: export headroom
        # reached 19.069 kW while import never left 0.0. That asymmetry is
        # consistent with import being pinned at a bound -- i.e. a CORRECT
        # 0.0 -- but nothing published could distinguish that from a
        # non-answer, which is what these two settle.
        #
        # `flex_available_up_kw`/`_down_kw` below are aliases of the two
        # headroom arrays (#493 unbuilt), so their degeneracy is these same
        # flags rather than separate ones -- deliberately not duplicated
        # under a second name that could drift.
        "grid_import_headroom_unranged": bool(gs.grid_import_headroom_unranged[0]),
        "grid_export_headroom_unranged": bool(gs.grid_export_headroom_unranged[0]),
        "forced_import_cost": round(float(gs.forced_import_cost[0]), 4),
        "forced_export_cost": round(float(gs.forced_export_cost[0]), 4),
        "flex_available_up_kw": round(float(gs.flex_available_up_kw[0]), 3),
        "flex_available_down_kw": round(float(gs.flex_available_down_kw[0]), 3),
        "load_headroom_up_kwh": round(float(gs.load_headroom_up_kwh[0]), 3),
        "load_headroom_down_kwh": round(float(gs.load_headroom_down_kwh[0]), 3),
        "battery_signals": battery_signals,
        "load_signals": load_signals,
        "generated_at": datetime.now(UTC).astimezone(LOCAL_TZ).isoformat(),
    }
    ha_post_state("sensor.nimbus_flex_signals", state, attributes)
    # nimbus issue #489: remembered so a deferred-ranging cycle can hold it.
    _LAST_FLEX_SIGNALS_POST.clear()
    _LAST_FLEX_SIGNALS_POST.update(state=state, attributes=dict(attributes))


FLEX_TELEMETRY_ENTITY_ID = "sensor.nimbus_flex_telemetry"

# Why a record can be absent, in the two expected cases. Held as module
# constants so the reasons are one grep away from the entity that goes
# `unknown` because of them, and so build_flex_telemetry_record() stays
# inside the #1301 size gate (tests/gates/size_ratchet.py).
_FLEX_TELEMETRY_RANGING_OFF = (
    "no ranging signals this cycle -- switch.nimbus_solver_flex_signals_enabled "
    "is off, which is the default, and it costs ~9x solve time when on (measured "
    "on a real install: median 1.16 s -> 10.3 s). The schema's own flex_available_"
    "up_kw/_down_kw are required, non-nullable numbers and come from ranging, so "
    "there is no valid partial record to emit instead."
)
_FLEX_TELEMETRY_NO_HISTORY = (
    "no real recorder history for [{start}, {end}) on the solar/battery/"
    "whole-house sensors, or one of them is unconfigured under Solver settings "
    "-- the same three compute_daily_quality_report() already requires"
)


def _flex_telemetry_measured(
    cfg: dict, interval_start: datetime, interval_end: datetime
) -> dict | None:
    """The four measured kW figures for ONE complete NEM 5-minute
    interval, as genuine period means -- `None` when the three sensors
    `compute_daily_quality_report()` already requires are unconfigured, or
    when the recorder holds no sample for any of them in the window.

    Deliberately the same three sensors, the same `_kw_scale_factor()`
    scaling, the same `solver_battery_power_positive_is_charge` sign
    resolution and the same energy-balance identity as
    `_compute_flex_report_for_window()` -- a second, independently-derived
    net-import formula is exactly how two Nimbus surfaces end up
    disagreeing about the same interval (#116's class).
    """
    solar_sensor = cfg.get("solver_solar_power_sensor")
    battery_sensor = cfg.get("solver_battery_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    if not solar_sensor or not battery_sensor or not load_sensor:
        return None
    solar_hist = fetch_entity_history_range(solar_sensor, interval_start, interval_end)
    load_hist = fetch_entity_history_range(load_sensor, interval_start, interval_end)
    battery_hist = fetch_entity_history_range(
        battery_sensor, interval_start, interval_end
    )
    if not solar_hist or not load_hist or not battery_hist:
        return None
    grid_times = [interval_start]
    period_hours = flex_telemetry.INTERVAL_SECONDS / 3600.0

    def _mean(hist, entity_id: str) -> float:
        return resample_history_mean(hist, grid_times, period_hours)[
            0
        ] * _kw_scale_factor(entity_id)

    battery_sign = -1.0 if cfg.get("solver_battery_power_positive_is_charge") else 1.0
    solar_kw = max(0.0, _mean(solar_hist, solar_sensor))
    load_kw = max(0.0, _mean(load_hist, load_sensor))
    net_battery_kw = _mean(battery_hist, battery_sensor) * battery_sign
    charge_kw = max(0.0, -net_battery_kw)
    discharge_kw = max(0.0, net_battery_kw)
    return {
        "solar_kw": solar_kw,
        "house_load_kw": load_kw,
        "net_import_kw": load_kw - solar_kw - discharge_kw + charge_kw,
        # `subtraction`: the same identity with the battery terms removed.
        # See flex_telemetry.py's own decision 2.
        "naive_baseline_kw": load_kw - solar_kw,
    }


def _flex_telemetry_assets(plan, batteries, household_lambda, clamped) -> list[dict]:
    """One `assets[]` entry per battery the LP actually planned for.

    Three sources joined by `name`, which is the key `BatteryPlan`/
    `BatterySignals` already use for exactly this: the plan for SoC and
    the period-0 setpoint, `plan.battery_signals` for the PHYSICAL
    available up/down (never the ranging pair -- the schema's own
    `available_up_kw` is the hardware figure, and `BatterySignals`'
    docstring says so), and the `BatteryConfig` list for capacity and the
    discharge ceiling `bidirectional_capable` is derived from.

    `shadow_power_balance_price` is the household λ for every asset, per
    #495's own field table ("per-battery once #467 lands, else the
    household λ") -- the LP has one `power_balance_t` row, so there is no
    per-battery dual to read yet.
    """
    signals_by_name = {b.name: b for b in plan.battery_signals}
    configs_by_name = {b.name: b for b in batteries}
    assets: list[dict] = []
    for bp in plan.batteries:
        cfg_entry = configs_by_name.get(bp.name)
        signal = signals_by_name.get(bp.name)
        assets.append(
            flex_telemetry.build_asset(
                name=bp.name,
                capacity_kwh=float(getattr(cfg_entry, "capacity_kwh", 0.0) or 0.0),
                soc_kwh=float(bp.soc_kwh[0]),
                charge_kw=float(bp.charge_kw[0]),
                discharge_kw=float(bp.discharge_kw[0]),
                max_discharge_kw=float(
                    getattr(cfg_entry, "max_discharge_kw", 0.0) or 0.0
                ),
                available_up_kw=(
                    float(signal.available_up_kw[0]) if signal is not None else 0.0
                ),
                available_down_kw=(
                    float(signal.available_down_kw[0]) if signal is not None else 0.0
                ),
                shadow_power_balance_price=household_lambda,
                clamped=clamped,
            )
        )
    return assets


def build_flex_telemetry_record(
    cfg: dict,
    plan,
    now: datetime,
    *,
    batteries,
    import_price: float,
    export_price: float,
    import_limit_kw: float,
    export_limit_kw: float,
    period_hours: float,
):
    """One schema-v2.0 `RecordBuild` for the last COMPLETE 5-minute
    interval (#495) -- `.record` None with a `.reason` when no VALID
    record is possible. Never raises. Every measurement and contract
    decision lives in `flex_telemetry.py`'s own docstring."""
    if plan.grid_signals is None:
        return flex_telemetry.RecordBuild(reason=_FLEX_TELEMETRY_RANGING_OFF)
    interval_start = flex_telemetry.last_complete_interval_start(now)
    interval_end = interval_start + timedelta(seconds=flex_telemetry.INTERVAL_SECONDS)
    measured = _flex_telemetry_measured(cfg, interval_start, interval_end)
    if measured is None:
        return flex_telemetry.RecordBuild(
            reason=_FLEX_TELEMETRY_NO_HISTORY.format(
                start=interval_start.isoformat(), end=interval_end.isoformat()
            )
        )
    gs = plan.grid_signals
    hours = period_hours if period_hours > 0 else 1.0
    household_lambda = plan.duals.get("power_balance_t0", 0.0) / hours
    # `solar_used_0`'s own upper bound IS period 0's solar forecast, so its
    # reduced cost is #495's "solar-used bound". None, never a plausible 0.
    reduced = plan.reduced_costs
    solar_shadow = reduced.get("solar_used_0", 0.0) / hours if reduced else None
    clamped: list[str] = []
    return flex_telemetry.build_record(
        interval_start=interval_start,
        region=cfg.get("region"),
        postcode_prefix=cfg.get("postcode_prefix"),
        net_import_kw=measured["net_import_kw"],
        solar_kw=measured["solar_kw"],
        house_load_kw=measured["house_load_kw"],
        deferrable_load_kw=0.0,  # #476 gates a real figure; slot reserved
        naive_baseline_kw=measured["naive_baseline_kw"],
        # "seen by the optimiser": the price the LP was BUILT with, not a
        # fresh read that could have moved since.
        price_signal_seen=import_price,
        price_export_seen=export_price,
        envelope_import_limit_kw=import_limit_kw,
        envelope_export_limit_kw=export_limit_kw,
        flex_available_up_kw=float(gs.flex_available_up_kw[0]),
        flex_available_down_kw=float(gs.flex_available_down_kw[0]),
        shadow_energy_price=household_lambda,
        shadow_solar_forecast_price=solar_shadow,
        # GridSignals' own docstring: these two ARE #493's envelope shadows.
        shadow_envelope_import_price=float(gs.forced_import_cost[0]),
        shadow_envelope_export_price=float(gs.forced_export_cost[0]),
        assets=_flex_telemetry_assets(plan, batteries, household_lambda, clamped),
        clamped_in=clamped,
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
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
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
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        _LOGGER.debug(
            "Nimbus Solver: _clear_load_forecast_error_notification_if_needed failed",
            exc_info=True,
        )


def resample_real_p2p_rate(
    grid_times: list[datetime],
    sensor_id: str | None = None,
    p2p_window_kw: list[float] | None = None,
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

    The window (nimbus #1537 item 5): `p2p_window_kw` is the household's
    own configured blocks, from `fetch_p2p_fixed_export_kw()`, and a period
    is inside the window when its value there is > 0. Without it, or with no
    block configured, the 17:00-24:00 gate above still applies, which is the
    reference household's own window. Before this a household whose blocks
    sat anywhere else had every P2P rate zeroed outside 17:00-24:00 and saw
    none inside its own blocks, with nothing in the log to say why.

    Returns a flat 0.0 array (never crashes) if `sensor_id` is blank --
    the same graceful no-op every household with no P2P/community-
    trading program at all gets.
    """
    if not sensor_id:
        return [0.0 for _ in grid_times]
    try:
        raw = ha_get(sensor_id)["attributes"]["forecast"]
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
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
            # nimbus #1537: purcell-lab/localvolts_v2 (>= 2.7.0) publishes the
            # same Sell rows with `matchedCost`, but keys the interval end as
            # `intervalEnd` rather than the reference household's renamed
            # `time`. Both are the interval END, so the arithmetic below is
            # unchanged; `time` wins when a row carries both.
            end_t = parse_iso(p["time"] if "time" in p else p["intervalEnd"])
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

    use_blocks = p2p_window_kw is not None and len(p2p_window_kw) == len(grid_times)
    out = []
    for i, gt in enumerate(grid_times):
        if use_blocks:
            in_window = p2p_window_kw[i] > 0  # NaN (no block) compares False
        else:
            in_window = 17 <= _local(gt).hour < 24
        if not in_window:
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
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
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


# nimbus #1306 (spec 007, Phase 7a): both overlap-guard mechanisms, and every
# comment justifying them, moved verbatim to solver/cycle_lock.py.
#
# LOCK_PATH itself deliberately stayed above, with the other three persisted
# paths. Four test sites rebind it on THIS module and none is visible to a
# literal-name scan (two `patch.object` calls driven by a tuple of strings, two
# plain attribute assignments) -- so moving it would have left any site missed
# in the sweep writing to the real /opt PID file. That module's docstring has
# the table and the full reasoning.
#
# The alias below is the same Lock object, not a copy: nothing anywhere rebinds
# _IN_PROCESS_LOCK, it is only acquired and released, so identity holds and
# `solver_writer._IN_PROCESS_LOCK is cycle_lock._IN_PROCESS_LOCK`.
_IN_PROCESS_LOCK = cycle_lock._IN_PROCESS_LOCK


def acquire_lock() -> bool:
    """See `solver.cycle_lock.acquire_lock`, which holds the real logic and the
    #757/#346 reasoning. This passes this module's own LOCK_PATH, so every
    existing caller and every test that rebinds LOCK_PATH here keeps working."""
    return cycle_lock.acquire_lock(LOCK_PATH)


def release_lock() -> None:
    """See `solver.cycle_lock.release_lock`. Passes this module's own LOCK_PATH."""
    cycle_lock.release_lock(LOCK_PATH)


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
    if NATIVE.hass is not None:
        try:
            import asyncio

            from homeassistant.components.recorder import (
                get_instance as _recorder_get_instance,
            )
            from homeassistant.components.recorder import history as _recorder_history

            async def _fetch() -> dict:
                return await _recorder_get_instance(NATIVE.hass).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    NATIVE.hass,
                    start,
                    end,
                    entity_id,
                    False,  # no_attributes=False -- unit_of_measurement lives there
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), NATIVE.hass.loop)
            changes = future.result(timeout=30)
            states = changes.get(entity_id, [])
        except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
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
    if NATIVE.hass is not None:
        try:
            import asyncio

            from homeassistant.components.recorder import (
                get_instance as _recorder_get_instance,
            )
            from homeassistant.components.recorder import history as _recorder_history

            async def _fetch() -> dict:
                return await _recorder_get_instance(NATIVE.hass).async_add_executor_job(
                    _recorder_history.state_changes_during_period,
                    NATIVE.hass,
                    start,
                    end,
                    entity_id,
                    True,  # no_attributes
                )

            future = asyncio.run_coroutine_threadsafe(_fetch(), NATIVE.hass.loop)
            changes = future.result(timeout=30)
            states = changes.get(entity_id, [])
        except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
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


# nimbus issue #937. The DAY-AHEAD decomposition, as distinct from #919's
# one-step-ahead nowcast skill above -- that issue is explicit that the two
# are not comparable ("a nowcast is a much easier problem").
#
# Stable key set for the same reason the nowcast one is: a consumer must never
# see a key appear and vanish between windows (#589).
_FORECAST_REGRET_KEYS = (
    "forecast_regret_j_star",
    "forecast_regret_j_forecast",
    "forecast_regret_j_persistence",
    "forecast_regret_dollars",
    "forecast_regret_persistence_dollars",
    "forecast_regret_nimbus_value_add_dollars",
    # The three-way ATTRIBUTION. #937's own text assumes the load
    # forecaster is the dominant error term; forecast_regret.py's own
    # docstring answers that assumption directly -- "Published
    # deliberately, because #937 assumes the load forecaster is the
    # dominant term and nobody has checked. If this dominates, the issue
    # is chasing the wrong forecaster." Publishing the headline dollars
    # without these would ship the metric and leave the question it exists
    # to settle unanswered.
    #
    # All three are None together when the level split is undefined (a
    # forecast summing to under 1% of the real day has no usable shape to
    # rescale -- #1072). None, not 0.0: a fabricated zero reads as "no
    # level error", which is the confident-wrong-number failure that
    # function's own comments say it exists to prevent.
    "forecast_regret_load_level_error_dollars",
    "forecast_regret_load_shape_error_dollars",
    "forecast_regret_solar_error_dollars",
    "forecast_regret_snapshot_captured_at",
    "forecast_regret_reason",
)


def _day_ahead_forecast_regret_attributes(
    *,
    solar_sensor: str,
    solar_scale: float,
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
    """Score what the DAY-AHEAD forecast was worth (nimbus issue #937).

    Three scenarios over one identical LP: perfect foresight (`j_star`), a
    plan built from what the forecast actually SAID the night before
    (`j_forecast`), and one built from naive persistence (`j_persistence`).
    `nimbus_value_add_dollars = j_persistence - j_forecast`, so a positive
    number means the forecast produced a genuinely cheaper real outcome than
    assuming today looks like yesterday.

    #937 measured **-$0.71/day** over 14 days on the reference household --
    persistence winning on 11 of 14 -- and then the series stopped, because
    the decomposition is assembled only in the standalone cron writer. This
    is the native equivalent, and the first time any HACS install can
    produce the number at all.

    **A missing snapshot returns None values and a reason, never zeros.** A
    zero-filled forecast scores as one that confidently predicted darkness
    and no load -- the worst forecast possible -- when the truth is that
    there is nothing to judge. Same contract `resample_snapshot_to_grid()`
    and the standalone `load_forecast_snapshot()` both state, and the reason
    this function has six distinct `reason` strings rather than one.

    Persistence is the PREVIOUS calendar day's real solar and load, read on
    that day's own grid and shifted forward 24 h -- identical in shape to
    `_load_nowcast_skill_attributes()`'s own baseline, so the two numbers
    differ only in the forecast they judge, not in what they judge it
    against.

    Takes the sensors and their scale factors already resolved by the
    caller rather than re-reading `cfg`: which entity counts as "the real
    load" is a decision `_compute_report_for_window()` has already made,
    and making it twice is how the two halves of one report drift apart.
    Uses `fetch_entity_history_range()` + `_kw_scale_factor`, not
    `fetch_entity_power_history_kw()`, whose own docstring states it is
    deliberately scoped to a single caller because preserving per-row
    attributes makes a full-day recorder read materially heavier.
    """
    blank = dict.fromkeys(_FORECAST_REGRET_KEYS)

    try:
        from .forecast_snapshot_store import get_cached_snapshot
        from .solver_inputs.forecast_snapshot import (
            day_key_for,
            resample_snapshot_to_grid,
        )
    except ImportError:
        # Standalone/cron path: no Store and no cache to prime one. Not an
        # error -- that copy assembles its own decomposition from its own
        # on-disk snapshot, which is the very asymmetry #937 is about.
        return {**blank, "forecast_regret_reason": "native_only"}

    snapshot = get_cached_snapshot(day_key_for(day_start))
    if not snapshot:
        # The ordinary state of every install for its first day, and of any
        # install that restarted before its first solve of a day. Named
        # distinctly so it is not read as a failure.
        return {**blank, "forecast_regret_reason": "no_forecast_snapshot_for_this_day"}

    resampled = resample_snapshot_to_grid(snapshot, grid_times)
    if resampled is None:
        return {**blank, "forecast_regret_reason": "snapshot_unusable_for_this_grid"}
    solar_forecast_kw, load_forecast_kw = resampled

    shift = timedelta(hours=24)
    solar_prev_hist = fetch_entity_history_range(
        solar_sensor, day_start - shift, day_end - shift
    )
    load_prev_hist = fetch_entity_history_range(
        load_sensor, day_start - shift, day_end - shift
    )
    if not solar_prev_hist or not load_prev_hist:
        return {**blank, "forecast_regret_reason": "previous_day_history_unavailable"}

    solar_persistence_kw = resample_history_mean(
        [(t + shift, v * solar_scale) for t, v in solar_prev_hist],
        grid_times,
        period_hours,
    )
    load_persistence_kw = resample_history_mean(
        [(t + shift, v * load_scale) for t, v in load_prev_hist],
        grid_times,
        period_hours,
    )
    if not any(load_persistence_kw):
        # An all-zero persistence baseline is not a baseline. It would make
        # the forecast look arbitrarily good by comparison, which is the
        # exact direction of error this metric exists to detect -- so it is
        # refused rather than published as a flattering number.
        return {**blank, "forecast_regret_reason": "no_persistence_baseline"}

    try:
        from .solver.forecast_regret import compute_forecast_regret

        fr = compute_forecast_regret(
            periods=periods,
            grid=grid,
            battery=battery,
            solar_real_kw=np.array(solar_real_kw),
            load_real_kw=np.array(load_real_kw),
            solar_forecast_kw=np.array(solar_forecast_kw),
            load_forecast_kw=np.array(load_forecast_kw),
            solar_persistence_kw=np.array(solar_persistence_kw),
            load_persistence_kw=np.array(load_persistence_kw),
        )
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        # Four extra LP solves. A diagnostic must never take the EPR path
        # down with it -- #366/#373's "degrade, never wedge", the same
        # posture the nowcast helper above takes.
        _LOGGER.debug(
            "Nimbus quality: day-ahead forecast-regret decomposition failed",
            exc_info=True,
        )
        return {**blank, "forecast_regret_reason": "decomposition_solve_failed"}

    def _maybe(value: float | None) -> float | None:
        """Round a dollar figure, or keep None as None.

        `round(None, 4)` raises, and an attribution term is legitimately
        absent whenever the level split is undefined -- so this is the
        difference between publishing "we could not attribute this" and
        crashing the whole quality report on an install whose forecast
        read as near-zero for a day.
        """
        return None if value is None else round(value, 4)

    return {
        "forecast_regret_j_star": round(fr.j_star, 4),
        "forecast_regret_j_forecast": round(fr.j_forecast, 4),
        "forecast_regret_j_persistence": round(fr.j_persistence, 4),
        "forecast_regret_dollars": round(fr.forecast_regret_dollars, 4),
        "forecast_regret_persistence_dollars": round(fr.persistence_regret_dollars, 4),
        "forecast_regret_nimbus_value_add_dollars": round(
            fr.nimbus_value_add_dollars, 4
        ),
        # Path-dependent by construction, and worth stating rather than
        # hiding: corrections apply level -> shape -> solar, so where two
        # errors interact the interaction lands in the later term. A
        # symmetric Shapley attribution would need 2^3 solves for a
        # diagnostic. The ordering matches the question -- level first,
        # because a biased forecaster is biased at every horizon and is the
        # cheaper thing to rule out.
        "forecast_regret_load_level_error_dollars": _maybe(fr.load_level_error_dollars),
        "forecast_regret_load_shape_error_dollars": _maybe(fr.load_shape_error_dollars),
        "forecast_regret_solar_error_dollars": _maybe(fr.solar_error_dollars),
        # Published so a consumer can tell a true post-midnight capture from
        # a post-restart one that had already seen part of the day it
        # forecasts, and so flatters itself. Without this the number looks
        # equally trustworthy either way.
        "forecast_regret_snapshot_captured_at": snapshot.get("captured_at"),
        "forecast_regret_reason": None,
    }


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
# nimbus issue #1248: the last quality history this process successfully read
# or published, used ONLY to survive a degraded read.
#
# The attribute is the system of record. This is a safety net for the one
# failure that cannot be recovered from the attribute itself: a read that comes
# back with no `history` at all while the sensor is `unavailable`. Measured on
# production 2026-09-26 -- 10 rows to 1, no restart, inside a #1217 window
# where every flattened quality child read `unavailable` for ~2 minutes.
#
# Process-lifetime on purpose. The measured incident had no restart, so this
# closes it exactly; and across a real restart HA's own state restoration
# repopulates the attribute, so starting empty on a fresh process is correct
# rather than lossy. A durable Store would add a second system of record for
# no gain against the failure actually observed.
#
# NATIVE-ONLY, and not as a convenience -- see `_quality_history_cache_active()`
# directly below for why the standalone/cron deployment cannot use this at all.
_LAST_KNOWN_QUALITY_HISTORY: dict[str, dict[str, float | str]] = {}


def _quality_history_cache_active() -> bool:
    """Whether #1248's recovery cache is in play at all.

    **The cron deployment cannot use it, and that is not a limitation to work
    around.** Those writers run once per cron invocation and exit, so a
    process-lifetime cache is empty on every single run and could never rescue
    anything. The native integration is the only long-lived process -- and the
    only place the measured incident could happen, which it did: 10 rows to 1
    with no restart, same process throughout.

    Gating on the native seam therefore costs production nothing and the cron
    path nothing it ever had. It also removes a real cross-contamination
    channel by construction: a caller that has not injected a `hass` gets no
    cache, so state cannot leak between unrelated publishes. CI found that the
    hard way -- `test_quality_report_achieved_energy_and_reliability.py` mocks
    an unreachable read, which is correctly classified as degraded, and in a
    single pytest process it was inheriting days another test had published.
    A conftest fixture resetting the global would have hidden that; this
    removes the channel.
    """
    return NATIVE.hass is not None


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

# nimbus issue #937: this day's DAY-AHEAD `nimbus_value_add_dollars` --
# `j_persistence - j_forecast`, so NEGATIVE means naive persistence produced a
# cheaper real outcome than the ML load forecaster did.
#
# **Why a row needs this, and why stage 3 was not enough.** #1307 made the
# native path publish the day-ahead decomposition, which removed the reason
# there were no days. It did not make them accumulate. The decomposition is a
# headline attribute describing `latest_date` alone, so tomorrow's 06:00 scoring
# overwrites today's number and nothing keeps it -- which is the same
# retention-not-publication defect `"r"` above exists to fix, one field over.
#
# That matters more here than anywhere else in this row, because #937's own
# top-priority action is literally "More days":
#
#     days scored 14 / naive persistence beat Nimbus's forecast 11 /
#     mean nimbus_value_add_dollars -$0.71/day / worst -$3.42
#
# Those fourteen days were readable only because the standalone cron writer of
# the time put a `forecast_regret` sub-dict into `day_entry`, and the series
# stopped the day the reference household moved to the native path. A window
# like it cannot be rebuilt from headline attributes at all: they hold one day.
#
# **One character and one number, not the sub-dict.** The cron-era sub-dict is
# what the five-key allow-list above was written to exclude, and correctly --
# rows are kept small enough that a year of them fits in one attribute payload.
# Measured on the reference household 2026-09-27: the quality report's whole
# attribute payload is **22,750 bytes** and `history` is **1,548** of it across
# 11 rows, ~140 bytes/row. `"f":-0.7123` costs ~13, so at the 60-day cap this
# adds ~780 bytes to a 8.4 KB table -- against the eight separate figures a
# full `forecast_regret` sub-dict would have cost per row.
#
# The attribution terms (level/shape/solar) deliberately stay headline-only.
# They answer "why was this day bad", which is a question about one day; the
# value-add answers "is the forecaster worth using", which is only answerable
# over a window, and is the one the selection policy consumes.
#
# **Written ONLY when the decomposition actually computed**, the same
# convention as `"p"` and for the same byte reason: a day with no forecast
# snapshot, or with unusable previous-day history, has no verdict, and a
# fabricated 0.0 there would read as "the forecaster exactly matched
# persistence" -- the confident-wrong-number failure
# `_day_ahead_forecast_regret_attributes()`'s own six reason codes exist to
# prevent. Absence means "not computed, or written before this existed".
_QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD = "f"


def _day_ahead_value_add_for_history(day_entry: dict) -> float | None:
    """This day's day-ahead `nimbus_value_add_dollars`, or None when there is
    no verdict to record (nimbus issue #937).

    Gated on `forecast_regret_reason` being None rather than on the value being
    present, because those are different statements.
    `_day_ahead_forecast_regret_attributes()` returns the full stable key set on
    every path (#589: a consumer must never see a key appear and vanish), so the
    value key exists and holds None on all six failure paths. Reading the value
    alone would work today and break silently the moment any of those paths
    learns to report a partial figure.
    """
    if day_entry.get("forecast_regret_reason") is not None:
        return None
    value = day_entry.get("forecast_regret_nimbus_value_add_dollars")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, 4)


def read_day_ahead_value_add_history() -> dict[str, float]:
    """The trailing day-ahead value-add record, read off this install's own
    quality report (nimbus issue #937 item 4).

    `{ISO local date: nimbus_value_add_dollars}`, negative meaning naive
    persistence was cheaper. Empty on any read failure, on an install that has
    not scored a day with a snapshot yet, and on every install running a
    release older than the one that started writing `"f"` -- all of which are
    the same thing to the caller: no evidence, so
    `select_forecast_source()`'s own `no_trailing_record` gate keeps using the
    ML forecast.

    **Read through `resolve_real_entity_id()`, never the literal string.** This
    is a read-back of this install's own prior output, which is exactly the case
    that function's docstring reserves it for: on an instance where a
    `remote_homeassistant` mirror of ANOTHER Nimbus install has claimed the
    plain `sensor.nimbus_solver_quality_report`, the literal read returns the
    other household's record. Deciding which load forecast to dispatch on from a
    different house's forecast scores is a materially worse version of the
    flicker defect that function was written for.
    """
    try:
        attrs = (
            ha_get(resolve_real_entity_id(QUALITY_ENTITY_ID)).get("attributes") or {}
        )
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        json.JSONDecodeError,
        TimeoutError,
        OSError,
    ):
        return {}
    history = attrs.get("history")
    if not isinstance(history, dict):
        return {}
    out: dict[str, float] = {}
    for key, row in history.items():
        if not isinstance(key, str) or not isinstance(row, dict):
            continue
        value = row.get(_QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        out[key] = float(value)
    return out


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


# nimbus issue #1120, part 3: the ceiling on a single rescore call.
#
# Each day costs a full oracle MILP -- the same solve the daily scorer
# runs once a night -- so a rescore is explicit, bounded, and never
# automatic on upgrade. Thirty is the practical cap rather than
# _QUALITY_HISTORY_MAX_DAYS (60): a caller who genuinely wants the whole
# table can call twice, and the smaller number makes an accidental
# "rescore everything" cost minutes rather than an hour.
_RESCORE_MAX_DAYS = 30


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


BACKTEST_ENTITY_ID = "sensor.nimbus_efficiency_backtest"


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

# nimbus issue #1259. How far apart solar_kw[0] (the live-measured
# anchor) and solar_kw[1] (the first genuine forecast period) have to sit
# before this cycle counts as a real DISAGREEMENT worth grading against
# reality. 3.0 kW is derived from the one event on record, not picked: it
# stepped 1.78 -> 15.89 kW, so any threshold below ~14 kW captures it,
# and the reason to sit well below that is to capture the ORDINARY cases
# too -- the open question is how often a sharp disagreement happens at
# all, which a threshold tuned to the single known event could never
# answer. Above the measurement noise of a real PV sensor, below any
# step a household would notice on a chart.
SOLAR_NOWCAST_DISAGREEMENT_KW = 3.0
# Rolling window the published counts cover. Wider than the delivery
# ratio's own 6 h because these events are RARE by construction (only
# disagreements above the threshold are recorded), so a 6 h window would
# usually publish zero and answer nothing. A full day also spans both a
# morning and an afternoon cloud regime.
SOLAR_NOWCAST_ROLLING_WINDOW_HOURS = 24.0


def _load_solar_delivery_state() -> dict:
    # nimbus issue #1259: two independent measurements now share this one
    # state file (the #128 delivery ratio and the index-0-vs-index-1
    # nowcast disagreement check). The nowcast keys are defaulted rather
    # than required, so a state file written by any earlier version loads
    # cleanly and simply starts with an empty nowcast buffer -- and both
    # writers preserve keys they do not own (see each function's own save
    # call), so neither can silently wipe the other's buffer.
    state: dict = {
        "pending": [],
        "ratios": [],
        "nowcast_pending": [],
        "nowcast_events": [],
        "nowcast_considered": [],
        "nowcast_disagreements": [],
    }
    try:
        with open(SOLAR_DELIVERY_RATIO_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "pending" in data and "ratios" in data:
            state.update(data)
            return state
    except (OSError, ValueError):
        pass
    return state


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

    # nimbus issue #1259: write back the state dict this function LOADED,
    # with only the two keys it owns replaced -- not a freshly-built
    # two-key dict, which would silently drop the nowcast buffer the
    # sibling measurement keeps in this same file.
    state["pending"] = still_pending
    state["ratios"] = ratios
    _save_solar_delivery_state(state)

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


#: nimbus issue #937 item 4. Stable key set, same reason `_FORECAST_REGRET_KEYS`
#: and `_NOWCAST_SKILL_KEYS` are stable: a consumer must never see a key appear
#: and vanish between cycles (#589).


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


# nimbus issue #535: log the W->kW scaling hint once per power_sensor
# entity_id, not every solve tick -- same #313/#314 discipline as every
# other log-once dedup this session (_DONE_CONDITION_WARNED,
# _QUALITY_REPORT_UNRELIABLE_WARNED). Module-level, lives for the process.
_LOAD_POWER_SENSOR_UNIT_HINT_LOGGED: set[str] = set()


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


def _discover_power_sensor_for_device(device_entity: str) -> str | None:
    """The single `device_class: power` sensor on the same physical
    device as `device_entity`, or None when there isn't exactly one.

    Native-mode only -- a standalone/cron run has no entity or device
    registry to consult, and Controllable Loads have no standalone
    existence anyway (build_controllable_loads() returns ([], []) there).
    """
    if NATIVE.hass is None or not device_entity:
        return None
    try:
        from homeassistant.helpers import entity_registry as er
    except ImportError:  # pragma: no cover - standalone/cron path
        return None

    registry = er.async_get(NATIVE.hass)
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


def _is_date_only(value: str) -> bool:
    """Is this calendar timestamp an all-day (iCalendar VALUE=DATE) string?

    nimbus issue #1290. `calendar.get_events` returns a bare `"2026-09-27"`
    for an all-day entry and a full `"2026-09-27T08:00:00"` (or an
    offset-bearing string) for a timed one. Both naive shapes parse to a
    naive datetime, so **the parsed value cannot tell them apart** -- the
    distinction survives only in the raw string, which is why this takes a
    str and not a datetime.

    Deliberately strict: exactly `YYYY-MM-DD`, nothing else. A looser test
    (e.g. "no 'T' in it") would catch a space-separated `"2026-09-27 08:00"`
    and anchor a real 08:00 departure to midnight, which is a worse error
    than the one being fixed.
    """
    text = value.strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return False
    return text[:4].isdigit() and text[5:7].isdigit() and text[8:].isdigit()


def fetch_calendar_trips(
    entity_id: str,
    start: datetime,
    end: datetime,
) -> list:
    """Upcoming events on one HA `calendar.*` entity, as `TripEvent`s
    (nimbus issue #467 item 4).

    Uses `calendar.get_events`, a response-returning service, through this
    module's own `ha_call_service_with_response()` -- so it works in BOTH
    deployment shapes with no new mechanism: that helper already bridges the
    native in-process path and the standalone/cron REST path, exactly as
    `publish_weather_forecast_mirrors()` does for `weather.get_forecasts`.

    Returns [] on ANY failure or unexpected shape, deliberately and without
    raising. A calendar that is missing, unavailable, renamed, or returning
    something this function does not recognise must degrade to "no trip
    planned this solve" -- which then falls back to the fixed departure pair
    if one is configured. The alternative, letting a calendar read break the
    solve, would make an optional convenience a single point of failure for
    real battery dispatch.

    Times come back as ISO strings; an event whose start or end will not parse
    is skipped individually rather than discarding the whole response.
    """
    from .solver_inputs.calendar_trips import TripEvent

    response = ha_call_service_with_response(
        "calendar",
        "get_events",
        {
            "entity_id": entity_id,
            "start_date_time": start.isoformat(),
            "end_date_time": end.isoformat(),
        },
    )
    if not isinstance(response, dict):
        return []
    # Keyed by entity_id, the same shape weather.get_forecasts returns.
    payload = response.get(entity_id)
    if not isinstance(payload, dict):
        return []
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        return []
    trips: list = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        started = _safe_fromisoformat(str(raw.get("start") or ""))
        ended = _safe_fromisoformat(str(raw.get("end") or ""))
        if started is None or ended is None:
            # One malformed event must not discard the rest -- an all-day
            # entry, a provider quirk, a half-written event.
            continue
        # nimbus issue #363: `_safe_fromisoformat()` can return a NAIVE
        # datetime, and a calendar provider genuinely can emit an
        # offset-less string. Every consumer downstream compares these
        # against tz-aware `grid_times`, and that comparison raises
        # TypeError deep inside the resolution rather than anywhere near
        # here -- the exact shape #363 documents for solar sources. So a
        # naive value must be given a zone here, unconditionally.
        #
        # nimbus issue #1290 (Mark Purcell, IV&V #1289): WHICH zone depends
        # on the raw string, and the original code used UTC for both shapes.
        #
        #   "2026-09-27"           all-day  -> LOCAL midnight
        #   "2026-09-27T08:00:00"  naive    -> UTC (#363's own rule)
        #
        # An all-day event is an iCalendar `VALUE=DATE`, which is a
        # local-calendar fact by definition -- "the 27th" for the household
        # reading it. Reading it as UTC midnight shifted it by the whole UTC
        # offset, so on this household (Brisbane, UTC+10) a trip meant to
        # start at local midnight resolved to 10:00 local the same day. For
        # an EV departure that is a ten-hour error in the direction that
        # matters: the car is assumed still plugged in for the entire
        # morning it is actually away.
        #
        # #363's rule is kept exactly as-is for the timed shape -- an
        # external API handing back a naive-but-genuinely-UTC instant is a
        # real thing, and
        # test_467_calendar_fetch.py::TestNaiveTimestampsCannotReachTheSolver
        # pins it.
        if started.tzinfo is None:
            started = started.replace(
                tzinfo=LOCAL_TZ if _is_date_only(str(raw.get("start") or "")) else UTC
            )
        if ended.tzinfo is None:
            ended = ended.replace(
                tzinfo=LOCAL_TZ if _is_date_only(str(raw.get("end") or "")) else UTC
            )
        trips.append(
            TripEvent(
                start=started,
                end=ended,
                summary=str(raw.get("summary") or ""),
                description=str(raw.get("description") or ""),
            )
        )
    return trips


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

    # nimbus issue #1259: the index-0-vs-index-1 solar disagreement
    # measurement, off unless
    # switch.nimbus_solver_nowcast_measurement_enabled is on. Same "never break the real solve" wrapping as every publish
    # above, and carried back on the same dict so the caller's own
    # published-attributes block has one thing to read rather than two.
    # Wrapped SEPARATELY from update_solar_delivery_ratio() on purpose:
    # an opt-in measurement failing must not cost the household the #128
    # ratio, which is on by default and has a real diagnostic use.
    try:
        nowcast = update_solar_nowcast_disagreement(cfg, now, grid_times, solar_kw)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        _LOGGER.warning("Nimbus: solar nowcast disagreement update failed: %s", e)
        nowcast = None
    if nowcast is not None:
        solar_delivery = dict(solar_delivery or {})
        solar_delivery["solar_nowcast_check"] = nowcast
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
    # nimbus issue #937 item 4: which load forecast this cycle's LP actually
    # consumed, and the trailing evidence behind that. Published on the plan
    # sensor beside `load_forecast_source_used` (which says which SENSORS the
    # forecast was read from -- a different question, and the two together are
    # the whole provenance of the load array).
    load_forecast_source_decision = _load.load_forecast_source_decision

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
    # nimbus issue #1303 (spec 004, Phase 4 of #1298): the 244-line plan-
    # assembly span that used to sit inline here now lives in solver_plan.py.
    #
    # load_previous_plan() is called HERE rather than inside the moved span
    # because it stays in this module (layer 4) while solver_plan.py is layer
    # 3 -- calling it from there would be an upward dependency needing an
    # import-linter exception. Hoisting it is observationally identical:
    # everything it now crosses is pure computation (elements.*Config
    # construction, terminal_value_breakpoints_for,
    # midnight_boundary_period_indices, fetch_p2p_fixed_export_kw -- all
    # verified to do no I/O), and the only write to PLAN_STATE_PATH is
    # save_plan_state(), which runs later inside publish_plan().
    previous_plan = load_previous_plan()
    _assembly = solver_plan.assemble_and_solve_plan(
        cfg,
        soc_envelope=_soc_envelope,
        now=now,
        grid_times=grid_times,
        period_hours_arr=period_hours_arr,
        n_periods=n_periods,
        capacity_kwh=capacity_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        charge_cost=charge_cost,
        discharge_cost_arr=discharge_cost_arr,
        salvage_value=salvage_value,
        spike_override_kw=spike_override_kw,
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=import_limit_kw,
        export_limit_kw=export_limit_kw,
        export_bonus_price=export_bonus_price,
        p2p_recent_volume_kwh=p2p_recent_volume_kwh,
        import_price_upper=import_price_upper,
        export_price_lower=export_price_lower,
        solar_kw=solar_kw,
        solar_lower_kw=solar_lower_kw,
        solar_upper_kw=solar_upper_kw,
        load_kw=load_kw,
        load_lower_kw=load_lower_kw,
        load_upper_kw=load_upper_kw,
        previous_plan=previous_plan,
    )
    # Unpacked into the same names the inline span bound, so every line below
    # is byte-identical to before this phase.
    plan = _assembly.plan
    grid = _assembly.grid
    all_batteries = _assembly.all_batteries
    fleet_capacity_kwh = _assembly.fleet_capacity_kwh
    solve_started = _assembly.solve_started
    risk_aversion = _assembly.risk_aversion
    import_price_risk_aversion = _assembly.import_price_risk_aversion
    export_price_risk_aversion = _assembly.export_price_risk_aversion
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
        solver_publish._flow_decomposition(
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
    solver_publish.publish_plan(
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
        load_forecast_source_decision=load_forecast_source_decision,
    )
    # nimbus issue #494 (Signals 5/7 of #489): no-op unless offer_curve_
    # enabled was true above (plan.offer_curve_import stays None
    # otherwise) -- see publish_offer_curve()'s own docstring.
    publish_offer_curve(plan)
    # nimbus issue #496 (Signals 7/7 of #489): no-op unless flex_signals_
    # enabled was true above (plan.grid_signals stays None otherwise) --
    # see publish_flex_signals()'s own docstring.
    publish_flex_signals(plan, hold_last=_assembly.flex_ranging_deferred)
    # nimbus issue #495 (Signals 6/7 of #489): the nem-flex-telemetry
    # schema-v2.0 record. Gated on the SAME switch as the flex signals
    # above, and not by choice -- the schema's own `flex_available_up_kw`/
    # `_down_kw` are required, non-nullable numbers, and they come from
    # `Plan.grid_signals`, which is None unless ranging ran. So "default
    # off" and "costs ~9x solve time when on" are inherited facts here,
    # not a second decision.
    solver_publish.publish_flex_telemetry_record(
        cfg,
        plan,
        now,
        batteries=all_batteries,
        import_price=float(import_price[0]),
        export_price=float(export_price[0]),
        import_limit_kw=float(np.asarray(import_limit_kw).ravel()[0]),
        export_limit_kw=float(np.asarray(export_limit_kw).ravel()[0]),
        period_hours=float(period_hours_arr[0]),
        hold_last=_assembly.flex_ranging_deferred,
    )


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
