"""The Nimbus Solver's solve cycle: reads the household's live inputs, runs
the LP over a tiered horizon (build_tiered_grid()) and publishes the plan to
sensor.nimbus_solver_battery_forecast and its sibling sensors.

It runs only inside Home Assistant: solver_runtime.py imports it, calls
set_native_hass(hass), and runs main() in a worker thread every cycle. The
standalone cron deployment it started as, which talked to HA over its REST
API, is retired (nimbus #2289 item 5, B.2a, 7 Nov 2026), and so is the
Supervisor add-on (#357).

Every read and write goes through the injected hass (solver_shared.NATIVE);
the REST transport is gone (#2289 item 5, B.2b). The stub test suite runs
the same native code against tests/native_transport.py.

Observation and dispatch both go through Home Assistant: the plan is
published as sensors, and controllable loads are commanded through HA
services by solver_dispatch/guard.py. Settings come from
sensor.nimbus_solver_config, the Solver settings wizard's bridge sensor.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import sys
import urllib.error
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
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
    from . import p5min_guard
except ImportError:  # pragma: no cover - standalone/cron path
    import p5min_guard  # type: ignore[no-redef]
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
    from .solver_inputs import price_blend, price_freshness
    from .solver_inputs import prices as price_inputs
except ImportError:
    from solver_inputs import price_blend, price_freshness  # type: ignore[no-redef]
    from solver_inputs import prices as price_inputs  # type: ignore[no-redef]
try:
    from .solver_inputs import solar_quantiles
except ImportError:
    from solver_inputs import solar_quantiles  # type: ignore[no-redef]
try:
    from .solver_inputs import price_error_history
except ImportError:
    from solver_inputs import price_error_history  # type: ignore[no-redef]
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
    from . import solver_shared
    from .solver_shared import (
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
        finite_float,
        ha_get,
        ha_post_state,
        import_fee_rate,
        last_posted,
        p2p_blocks_daily_energy_kwh,
        p2p_bonus_price_by_period,
        parse_iso,
        power_scale_or_none,
        resample_history_mean,
        resample_history_nearest,
        resolve_effective_capacity_kwh,
        safe_num,
    )
except ImportError:
    import solver_shared  # type: ignore[no-redef]
    from solver_shared import (  # type: ignore[no-redef]
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
        TIER1_PERIOD_HOURS,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        TIER2_PERIOD_HOURS,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        _cfg_int,
        _cfg_num,
        _kw_scale_factor,
        _local,
        _native_http_error,  # noqa: F401 -- re-export, see comment below
        _nimbus_version,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        _version_stamp,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        _warn_if_attrs_exceed_recorder_cap,  # noqa: F401 -- re-export, see comment below
        fetch_entity_attribute_history_range,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        fetch_entity_history_range,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ and tests (#1301)
        fetch_p2p_fixed_export_kw,  # noqa: F401 -- re-export: main()'s own call
        finite_float,
        # moved to solver_plan.py (#1303); 8 test files still call it via THIS module
        ha_get,
        ha_post_state,
        import_fee_rate,
        last_posted,  # noqa: F401 -- re-export for solver_runtime (#1658)
        p2p_blocks_daily_energy_kwh,  # noqa: F401 -- re-export: reached as sw.<name> from solver_inputs/prices.py (#1537)
        p2p_bonus_price_by_period,  # noqa: F401 -- re-export: reached as sw.<name> from solver_reports/ (#1301)
        parse_iso,
        power_scale_or_none,
        resample_history_mean,  # noqa: F401 -- re-export: reached as sw.<name> from solver_inputs/ and solver_reports/
        resample_history_nearest,  # noqa: F401 -- re-export: reached as sw.<name> from solver_inputs/, solver_reports/ and solver_dispatch/
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
# in a test) reaching for the OLD
# location must keep working. Ruff's F401 cannot see a caller in a
# DIFFERENT file, so it reads these as dead imports; `# X as X` (the
# usual explicit-re-export idiom) was tried and rejected, since ruff's
# own PLC0414 (useless-import-alias) flags that same pattern outside
# `__init__.py` -- the two rules disagree here, and marking `__all__` for
# a file this size, used this many other ways, was judged a bigger and
# riskier change than this migration's own "pure relocation" scope. A
# per-line noqa, this repo's own established convention for exactly this
# kind of individually-justified exception (see this file's own BLE001


# Every local-time value uses LOCAL_TZ, an explicitly resolved zone, never a
# bare `.astimezone()`: a host's own zone can resolve to UTC (2026-08-17).
# LOCAL_TZ is the NIMBUS_SOLVER_TIMEZONE env var if set, else this default,
# and native mode re-resolves it from hass.config.time_zone in
# set_native_hass() (nimbus #347). Known gap (#347): timedelta arithmetic on
# a LOCAL_TZ-aware datetime moves in wall-clock time across a DST change.


import numpy as np

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
    # __init__.py. (2) tests/v2/orchestration/test_import_and_token_
    # laziness.py asserts on its __module__ as the probe for nimbus
    # issue #349's "the relative import must not produce a second,
    # distinct top-level `ml` module" guarantee. Removing it would
    # break both.
    from ml.blend import blend_forecast_array, cross_source_spread  # noqa: F401
    from solver import (  # noqa: F401 -- `lp` is a re-export: main()'s own
        # lp.CalibratedOptions() call moved to solver_plan.py (#1303), but
        # tests/v2/orchestration/test_smoothness_and_proximal_weights.py asserts
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

# Plan-to-plan stability state, read by load_previous_plan() and written by
# save_plan_state(): two solves minutes apart must not flip strategy on a thin
# margin (2026-08-16). Native mode points it at hass.config.path().
PLAN_STATE_PATH = os.environ.get(
    "NIMBUS_SOLVER_PLAN_STATE_PATH", "/opt/nimbus_solver_last_plan.json"
)
# PID-file overlap guard: a tick that fires while a solve is still running
# exits cleanly instead of running a second solve alongside it.
LOCK_PATH = os.environ.get(
    "NIMBUS_SOLVER_LOCK_PATH", "/opt/nimbus_solver_forecast_writer.lock"
)
# Gates _notify_load_forecast_error_once(): one notification per new
# load-forecast error message, not one per cycle (nimbus #66).
LOAD_FORECAST_ERROR_NOTIFIED_PATH = os.environ.get(
    "NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_PATH",
    "/opt/nimbus_solver_load_forecast_error.txt",
)


# Network and certificate fees come from the household's own fee numbers
# through import_fee_rate() below, never from constants here (household,
# 2026-08-22: no hardcoded inputs).

# nimbus #348: one household-specific choice is not yet generalised: the P2P
# matched-rate sensor's fixed 17:00-24:00 window, independent of the
# configured P2P blocks. _log_active_household_specific_overrides_once()
# names it in the log at startup. The other #348 findings are fields now
# (fixed daily charge, post-midnight self-consume window, discharge-cost
# schedule, forecast-array keys).
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
#   _REAFFIRM_CAP_WARNED          a test (since retired, #1837) did
#                                 solver_writer._REAFFIRM_CAP_WARNED.clear().
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
    # The P2P matched-rate window used to be listed here (#348): a fixed
    # 17:00-24:00. It is no longer hardcoded -- configured blocks bound it
    # (#1551) and without blocks the rate is flexible (#1559) -- so there is
    # nothing household-specific left to report for it.
    if active:
        _LOGGER.warning(
            "Nimbus Solver: %d household-specific override(s) active for this "
            "install (see nimbus issue #348 for the full context) -- %s",
            len(active),
            "; ".join(active),
        )


# Load demand can be the sum of individually forecast circuits
# (load_forecast_entities) instead of one whole-house forecast, with an
# optional whole-house cross-check that is reported, never priced. Both are
# read from the Solver settings (cfg) in main(), never constants (nimbus
# #56/#60). The cross-check is configured as its source sensor and its
# forecast id derived at read time, so a renamed forecast can't leave it
# pointing at a dead entity (2026-08-20).

# Inverter self-consumption (number.nimbus_solver_inverter_self_consumption_kw,
# default 0): a constant draw no circuit meter sees, added to a summed-circuit
# load total. Read from cfg, never a module constant (nimbus #100).


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
    own docstring, and its tests (tests/v2/orchestration/test_writer_inputs.py),
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


# The discharge-cost / salvage-value schedule: optional blocks keyed in the
# Solver settings (nimbus #348). Since 13 Oct 2026 the defaults are the
# install's own Discharge Cost and Salvage Value at every hour, with every
# block off: the earlier fixed defaults overrode the visible setting every
# day from 07:00, and the plan and its own scorer disagreed. charge_cost is
# never scheduled.

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
# Per-block schema-default fallbacks: every block defaults OFF (13 Oct 2026,
# see above); a block applies only when its rate key is set above 0.
# Keyed by the same tuples above so the lookup functions below can stay
# a single, non-repetitive loop instead of one hardcoded branch per slot.
_DISCHARGE_COST_BLOCK_DEFAULTS = (
    (0.0, 17, 7),  # wraps past midnight
    (0.0, 0, 0),
    (0.0, 0, 0),
)
_SALVAGE_VALUE_BLOCK_DEFAULTS = (
    (0.0, 17, 24),
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
        cfg,
        "solver_discharge_cost_schedule_default_rate",
        _cfg_num(cfg, "solver_discharge_cost", 0.01),
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
        cfg,
        "solver_salvage_value_schedule_default_rate",
        _cfg_num(cfg, "solver_salvage_value", 0.15),
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


# The native seam: NATIVE.hass is None until set_native_hass() runs, and the
# injected hass after. Only ha_get(),
# ha_post_state() and fetch_price_history() branch on it; everything else
# calls them by name. NATIVE's identity never changes (nimbus #1437; see
# solver_shared.NATIVE).

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
            # solver_shared keeps its own LOCAL_TZ, read by _local() and the
            # history readers -- the local hour behind TOU fees, the EV
            # departure hour, day boundaries and P2P checkpoints. Left at its
            # import-time default (Australia/Brisbane), every install outside
            # that zone evaluated those in Brisbane time. One zone for both.
            sys.modules[_local.__module__].LOCAL_TZ = LOCAL_TZ
        except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
            _LOGGER.exception(
                "Nimbus: could not resolve hass.config.time_zone (%r) -- "
                "keeping the existing LOCAL_TZ (%s)",
                raw_time_zone,
                LOCAL_TZ,
            )


def entity_exists(entity_id: str) -> bool:
    """Real existence check, not just "did the last read happen to
    succeed" -- used to gate the optional LocalVolts/AEMO-specific price-
    forecasting enhancement below (2026-08-20, see solver_shared.fetch_solver_config()'s
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


# nimbus issue #944: the recorder's own per-state attribute cap
# (homeassistant/components/recorder/db_schema.py::MAX_STATE_ATTRS_BYTES).
# Mirrored here rather than imported because this module must keep
# working on the standalone/cron deployment, which has no homeassistant
# package at all.

# Warn once per entity_id per process. The condition is structural -- if
# a payload is over the cap this cycle it will be over it every cycle --
# so repeating it once a minute would be exactly the noise v0.94.297 had
# to clean up for #757.


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
        response = solver_shared.ha_call_service_with_response(
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
    service response, see solver_shared.ha_call_service_with_response()'s own
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


def reset_flex_ranging_cadence() -> None:
    """nimbus issue #489: forget the in-process flex cadence state -- which
    5-minute interval last ranged, and the two held payloads -- so the next
    `main()` ranges and nothing is held over. For tests, which drive `main()`
    many times inside one interval; production never needs it."""
    solver_plan.reset_flex_ranging_state()
    # nimbus #1797: the stochastic shadow solve's cadence is in-process state
    # of the same kind, and tests drive main() many times inside one interval.
    solver_plan.reset_stochastic_state()
    solver_publish._LAST_FLEX_SIGNALS_POST.clear()
    solver_publish._LAST_FLEX_TELEMETRY_POST.clear()


FLEX_TELEMETRY_ENTITY_ID = "sensor.nimbus_flex_telemetry"

# Why a record can be absent, in the two expected cases. Held as module
# constants so the reasons are one grep away from the entity that goes
# `unknown` because of them, and so build_flex_telemetry_record() stays
# inside the #1301 size gate (tests/gates/size_ratchet.py).
_FLEX_TELEMETRY_NO_MEASUREMENT = (
    "this solve had no house load for period 0, so there is no site figure to publish"
)


def _measured_house_load_kw(load_kw, load_forecast_error, live_load_kw) -> float | None:
    """Period 0's load for the telemetry record, or None when it is only the
    load forecast's zero-fallback (nimbus #1665, Mark Purcell). A broken
    load forecast leaves `load_kw` at 0.0 by design (#370/#416); unless a live
    whole-house reading replaced period 0, that zero is not a measurement and
    the record must say `no_measurement`, not publish 0.0."""
    if load_forecast_error is not None and live_load_kw is None:
        return None
    return float(load_kw[0])


def _grid_meter_sign(meter_sensor: str) -> float | None:
    """+1.0 when `meter_sensor` reads positive = import, -1.0 when positive =
    export, None when not established (nimbus #1634).

    Nimbus has no setting for a grid meter's sign. The daily quality
    report's meter reconciliation (#1465) fits it from a whole day, and only
    a day that AGREED on this same sensor is used: one reading near zero
    cannot tell the two signs apart."""
    try:
        attrs = ha_get(solver_shared.resolve_real_entity_id(QUALITY_ENTITY_ID)).get(
            "attributes"
        )
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        _LOGGER.debug("Nimbus flex telemetry: meter sign unreadable", exc_info=True)
        return None
    rec = (attrs or {}).get("meter_reconciliation") or {}
    if rec.get("status") != "agrees" or rec.get("meter_sensor") != meter_sensor:
        return None
    return {"positive_is_import": 1.0, "positive_is_export": -1.0}.get(
        rec.get("meter_sign")
    )


def _live_grid_meter_kw(cfg: dict) -> float | None:
    """The grid meter's live net import (kW, + = import): ONE state read, no
    history (nimbus #1634, Mark Purcell). None when no meter is configured,
    its sign is not established (`_grid_meter_sign`), or its state or unit
    is unusable."""
    meter = cfg.get("switchboard_grid_meter_sensor") or cfg.get("grid_sensor")
    sign = _grid_meter_sign(meter) if meter else None
    if sign is None:
        return None
    try:
        state = ha_get(meter)
        unit = (state.get("attributes") or {}).get("unit_of_measurement")
        scale = power_scale_or_none(unit)  # nimbus #1643: amps etc. are no reading
        value = float(state["state"]) * scale if scale is not None else math.nan
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        _LOGGER.debug("Nimbus flex telemetry: %s unreadable", meter, exc_info=True)
        return None
    return value * sign if math.isfinite(value) else None


def _flex_telemetry_measured(
    plan, house_load_kw: float | None, cfg: dict | None = None
) -> dict | None:
    """The record's four site figures, from what this solve already holds
    (nimbus #1634, Mark Purcell: the record publishes data Nimbus already
    generates and needs no history reads). `None` when the solve had no
    house load.

    - `house_load_kw`: the solve's period-0 load, which is the live
      whole-house reading when that sensor is configured
      (`solver_inputs/load.py` overwrites period 0 with it);
    - `solar_kw`: the plan's period-0 solar, used plus curtailed, which is
      the live solar reading the same way (`solver_inputs/solar.py`);
    - `net_import_kw`: the grid meter's live reading when its sign is
      established (`_live_grid_meter_kw`), else the plan's period-0 grid
      import minus export; `net_import_source` says which;
    - `naive_baseline_kw`: house load minus solar, flex_telemetry.py's own
      decision 2.
    """
    if house_load_kw is None:
        return None
    load_kw = max(0.0, float(house_load_kw))
    solar_kw = max(
        0.0, float(plan.solar_used_kw[0]) + float(plan.solar_curtailed_kw[0])
    )
    metered = _live_grid_meter_kw(cfg or {})
    planned = float(plan.grid_import_kw[0]) - float(plan.grid_export_kw[0])
    return {
        "solar_kw": solar_kw,
        "house_load_kw": load_kw,
        "net_import_kw": planned if metered is None else metered,
        "net_import_source": "plan" if metered is None else "grid_meter",
        "naive_baseline_kw": load_kw - solar_kw,
    }


def _flex_telemetry_assets(
    plan, batteries, household_lambda, clamped, cfg
) -> list[dict]:
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
    # #2140: the participants the config sensor names as vehicles.
    vehicles = frozenset(cfg.get("flex_vehicle_participants") or ())
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
                vehicle=bp.name in vehicles,
            )
        )
    return assets


def _flex_available_physical(
    plan, import_limit_kw: float, export_limit_kw: float
) -> dict[str, float]:
    """Household `flex_available_up_kw`/`_down_kw` as PHYSICAL availability
    (nimbus #1634 step 1, option (b); #1639): the sum of every battery's own
    hardware headroom in period 0, capped by what the grid envelope still
    allows at the planned exchange. Up = more consumption (charge more /
    discharge less, so more import); down = the reverse. Pure arithmetic --
    no HiGHS ranging, so the record no longer depends on the 9-11x flex
    signals switch. The LP's ranging headroom stays on
    `sensor.nimbus_flex_signals`, where it means what it says."""
    signals = list(plan.battery_signals or [])
    up = sum(float(b.available_up_kw[0]) for b in signals)
    down = sum(float(b.available_down_kw[0]) for b in signals)
    net_import = float(plan.grid_import_kw[0]) - float(plan.grid_export_kw[0])
    return {
        "flex_available_up_kw": max(0.0, min(up, import_limit_kw - net_import)),
        "flex_available_down_kw": max(0.0, min(down, export_limit_kw + net_import)),
    }


def _no_flex_record(code: str, reason: str) -> flex_telemetry.RecordBuild:
    """No record this cycle, with its #1634 reason code (kept out of
    build_flex_telemetry_record so it stays inside the #1301 size gate)."""
    return flex_telemetry.RecordBuild(reason=reason, reason_code=code)


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
    house_load_kw: float | None = None,
):
    """One schema-v2.0 `RecordBuild` for the last COMPLETE 5-minute interval
    (#495), `.record` None with a `.reason` when no valid record is possible.
    Never raises. Contract decisions: `flex_telemetry.py`'s docstring."""
    bad = flex_telemetry.location_problem(cfg.get("region"), cfg.get("postcode_prefix"))
    if bad is not None:  # nimbus #1634: no work for a record that cannot exist
        return bad
    interval_start = flex_telemetry.last_complete_interval_start(now)
    measured = _flex_telemetry_measured(plan, house_load_kw, cfg)
    if measured is None:
        return _no_flex_record("no_measurement", _FLEX_TELEMETRY_NO_MEASUREMENT)
    gs = plan.grid_signals
    hours = period_hours if period_hours > 0 else 1.0
    household_lambda = plan.duals.get("power_balance_t0", 0.0) / hours
    # `solar_used_0`'s own upper bound IS period 0's solar forecast, so its
    # reduced cost is #495's "solar-used bound". None, never a plausible 0.
    reduced = plan.reduced_costs
    solar_shadow = _note_flex_shadow_raw(plan, reduced, hours)
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
        **_flex_available_physical(plan, import_limit_kw, export_limit_kw),
        shadow_energy_price=household_lambda,
        shadow_solar_forecast_price=solar_shadow,
        # GridSignals' own docstring: these two ARE #493's envelope shadows.
        shadow_envelope_import_price=(
            float(gs.forced_import_cost[0]) if gs is not None else None
        ),
        shadow_envelope_export_price=(
            float(gs.forced_export_cost[0]) if gs is not None else None
        ),
        assets=_flex_telemetry_assets(plan, batteries, household_lambda, clamped, cfg),
        clamped_in=clamped,
        net_import_source=measured["net_import_source"],
    )


#: nimbus #2180: the last flex record's raw shadow-price inputs, before
#: `/hours` and the schema's clamp; named in the clamp WARNING.
FLEX_SHADOW_RAW: dict = {}


def _note_flex_shadow_raw(plan, reduced, hours: float) -> float | None:
    """The solar shadow price, `solar_used_0`'s reduced cost per hour (None
    without reduced costs), keeping the raw figures behind it and the energy
    shadow price for the clamp WARNING (nimbus #2180, Mark Purcell)."""
    # The figure logged is the one divided below, defaulted the same way.
    solar_reduced = reduced.get("solar_used_0", 0.0) if reduced else None
    FLEX_SHADOW_RAW.clear()
    FLEX_SHADOW_RAW.update(
        {
            "period_hours": round(hours, 6),
            "solar_used_0_reduced_cost": solar_reduced,
            "power_balance_t0_dual": plan.duals.get("power_balance_t0"),
        }
    )
    return None if solar_reduced is None else solar_reduced / hours


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


# One reshape for every solar-source reader (nimbus #542): a configured
# Solcast (detailedForecast) or Open-Meteo (watts) entity is read the same
# way as the auto-included ones, instead of being dropped when it isn't
# already in the generic forecast=[{time,value,...}] shape.
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
        # nimbus #2004: each value is the power at its key + 15 min, an
        # instant; read with resample_instants(), never held from its key.
        return [
            {
                "time": parse_iso(ts) + OPEN_METEO_WATTS_LEAD,
                "value": float(w) / 1000.0,
                "instant": True,
            }
            for ts, w in watts.items()
        ]
    return None


# nimbus #2004 (checked in the library's source):
# Open-Meteo Solar Forecast's `watts` is the power from the instantaneous
# irradiance at each 15-minute step, stored under the step's START
# (`time_start = time - timedelta(minutes=15)`), so `watts[t]` is the power
# at t + 15 min. Held from its key it made the plan's 5-minute periods read
# the power 5-15 minutes ahead. If the library ever re-keys `watts`, this
# offset must come out (tests/v2/forecasting/test_solar_inputs.py pins it).
OPEN_METEO_WATTS_LEAD = timedelta(minutes=15)


def resample_instants(
    forecast: list[dict], value_key: str, grid_times: list[datetime]
) -> list[float]:
    """nimbus #2004: a series of instantaneous readings on the plan's grid.
    Each period's value is the reading at its midpoint, linear between the
    readings either side, which for a smoothly varying power is the
    period's mean; before the first reading or after the last, that
    reading is held, as resample_forecast() holds. The last period is taken
    as long as the one before it. On a 30-minute period starting on the
    half hour this is exactly the `watts` value keyed at its start, the
    reading before #2004."""
    pts = sorted(
        (
            (parse_iso(p["time"]), float(p[value_key]))
            for p in forecast
            if p.get(value_key) is not None
        ),
        key=lambda x: x[0],
    )
    if not pts:
        return [0.0] * len(grid_times)
    out = []
    for i, start in enumerate(grid_times):
        if i + 1 < len(grid_times):
            mid = start + (grid_times[i + 1] - start) / 2
        elif i > 0:
            mid = start + (start - grid_times[i - 1]) / 2
        else:
            mid = start
        if mid <= pts[0][0]:
            out.append(pts[0][1])
            continue
        if mid >= pts[-1][0]:
            out.append(pts[-1][1])
            continue
        for (t0, v0), (t1, v1) in itertools.pairwise(pts):
            if t0 <= mid <= t1:
                span = (t1 - t0).total_seconds()
                frac = (mid - t0).total_seconds() / span if span else 0.0
                out.append(v0 + (v1 - v0) * frac)
                break
    return out


# Fixed-rate P2P delivery: up to three optional blocks from the Solver
# config (cfg), set on the dashboard. P2P rewards consistent delivery, so a
# block holds one flat export rate rather than chasing each interval's rate
# (household, 2026-08-20). See solver.elements.GridConfig.fixed_export_kw.

# After a P2P window closes, the battery self-consumes for
# post_window_self_consume_hours (default 4; nimbus #348). A hard LP
# constraint, because a soft terminal-value nudge can be outbid by an export
# price that is still positive (2026-08-22).


def provider_forecast_until(
    real_mask: object, grid_times: list[datetime]
) -> str | None:
    """nimbus #1753: the start of the last plan period the price provider's
    own forecast covers (ISO), or None when it covers none. Periods after it
    are priced by this module's wholesale + learned-markup extension (or
    held flat), so a dashboard can tell the provider's prices from Nimbus's
    estimate. `real_mask` is the per-period coverage mask the price inputs
    already build (resample_price_with_extrapolation(),
    price_blend.resample_generic_price_forecast_with_coverage())."""
    try:
        flags = [bool(f) for f in real_mask]  # type: ignore[attr-defined]
    except TypeError:
        return None
    last = max((i for i, f in enumerate(flags[: len(grid_times)]) if f), default=None)
    return None if last is None else grid_times[last].isoformat()


# nimbus #1698: Amber Express's per-interval `spot_per_kwh` is in
# `detailedForecast`, which the recorder does not keep, so its history is
# collected here, one settled value per 5-minute interval. Same env-var
# convention as SOLAR_DELIVERY_RATIO_PATH; solver_runtime points it at HA's
# config directory for the native path.
AMBER_SPOT_PATH = os.environ.get(
    "NIMBUS_SOLVER_AMBER_SPOT_PATH", "/opt/nimbus_solver_amber_spot.json"
)


# nimbus #1786 (Mark Purcell): a consolidated price forecast and each source's
# running error by lead. Same env-var convention as AMBER_SPOT_PATH;
# solver_runtime points it at HA's config directory for the native path.
PRICE_CONSOLIDATION_PATH = os.environ.get(
    "NIMBUS_SOLVER_PRICE_CONSOLIDATION_PATH",
    "/opt/nimbus_solver_price_consolidation.json",
)

# nimbus issue #1807: the solar forecast's actual/p50 history by lead, from
# which the calibrated p10/p90 are built (solver_inputs/solar_quantiles.py).
# Same env-var convention; solver_runtime points it at HA's config directory.
SOLAR_QUANTILES_PATH = os.environ.get(
    "NIMBUS_SOLVER_SOLAR_QUANTILES_PATH",
    "/opt/nimbus_solver_solar_quantiles.json",
)
_SOLAR_QUANTILES_CACHE: solar_quantiles.SolarErrorHistory | None = None
_SOLAR_QUANTILES_SAVED = False

# nimbus #1797: signed price-forecast errors by source, lead and regime, the
# history price scenarios will be calibrated from
# (solver_inputs/price_error_history.py). Same env-var convention;
# solver_runtime points it at HA's config directory.
PRICE_ERROR_HISTORY_PATH = os.environ.get(
    "NIMBUS_SOLVER_PRICE_ERROR_HISTORY_PATH",
    "/opt/nimbus_solver_price_error_history.json",
)
_PRICE_ERROR_HISTORY_CACHE: price_error_history.PriceErrorHistory | None = None
_PRICE_ERROR_HISTORY_SAVED_AT: datetime | None = None


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
    by_series = quality_reports._history_coverage_by_series(
        {str(i): hist for i, hist in enumerate(histories)}, start, end
    )
    return min(cov.hours for cov in by_series.values())


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
        live_value = finite_float(state["state"]) * scale
    except (KeyError, TypeError, ValueError):
        _warn_envelope_limit_dropped_once(entity_id, "reporting a non-numeric state")
        return [static_limit_kw] * n
    _note_envelope_limit_recovered(entity_id)
    return [live_value] * n


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
# tests/test_1172_the_regret_card_drops_the_measured_capacity.py pins
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
    hard way -- the achieved-energy suite (since retired, #1837) mocked
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
    # The raw LP objective, search penalties included -- NOT the oracle EPR
    # divides by. EPR uses j_star_evaluator (#1081); a row has no room for it
    # under the attribute budget (test_quality_report_attribute_size_budget),
    # so recompute EPR from quality_history_store's records, which keep both.
    # From a row, (j_ref - j_ach) / (j_ref - j_star) does not reproduce EPR,
    # and on 17 Sep, 2 Oct and 7-8 Oct it looked as if the real result beat
    # perfect foresight (#1797 review, 16 Oct 2026).
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


# nimbus #1200: "p" marks a row whose figures were computed WITHOUT the day's
# P2P settlement, which lands hours after the day is first scored. Until then
# j_ref, j_ach and j_star are all P2P-blind and the EPR reads worst on the
# best-earning days. Recorded in the row, the repair has no deadline: the
# hourly re-score of the published day (#1082) expires at midnight, before
# the settlement often lands. Written only when provisional, to save bytes
# (the payload sits near the recorder's 16 KB cap, #944); absence means
# "settled, or written before this existed", and costs at most a missed
# repair, never a wrong one.
_QUALITY_HISTORY_PROVISIONAL_FIELD = "p"

# nimbus #937: "f" is the day's day-ahead nimbus_value_add_dollars
# (j_persistence - j_forecast; negative means naive persistence beat the ML
# load forecast). It's kept per row because "is the forecaster worth using"
# is only answerable over many days, and the headline attribute holds one.
# One number per row, about 13 bytes, not the old forecast_regret sub-dict.
# The attribution terms stay headline-only: they explain one day. Written
# only when the decomposition computed; a fabricated 0.0 would read as "the
# forecaster exactly matched persistence".
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

    **Read through `solver_shared.resolve_real_entity_id()`, never the literal string.** This
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
            ha_get(solver_shared.resolve_real_entity_id(QUALITY_ENTITY_ID)).get(
                "attributes"
            )
            or {}
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
    return status in quality_reports._PROVISIONAL_SETTLEMENT_STATUSES


# nimbus issue #1120, part 3: the ceiling on a single rescore call.
#
# Each day costs a full oracle MILP -- the same solve the daily scorer
# runs once a night -- so a rescore is explicit, bounded, and never
# automatic on upgrade. Thirty is the practical cap rather than
# _QUALITY_HISTORY_MAX_DAYS (60): a caller who genuinely wants the whole
# table can call twice, and the smaller number makes an accidental
# "rescore everything" cost minutes rather than an hour.
_RESCORE_MAX_DAYS = 30


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
            ha_get(solver_shared.resolve_real_entity_id(QUALITY_ENTITY_ID)).get(
                "attributes"
            )
            or {}
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


# nimbus #128: solar_delivery_ratio, the rolling mean of actual / forecast
# solar over recent hours where the forecast is above 5 kW, catches inverter
# AC clipping that the curtailment switch can't see. Optional: it reuses
# solver_solar_power_sensor, and does nothing without it. Native mode points
# this path at HA's storage directory.
SOLAR_DELIVERY_RATIO_PATH = os.environ.get(
    "NIMBUS_SOLVER_SOLAR_DELIVERY_RATIO_PATH",
    "/opt/nimbus_solver_solar_delivery_ratio.json",
)


def _safe_fromisoformat(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


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


# nimbus #768: a thermal controllable load with no power sensor set finds one
# through the DEVICE REGISTRY (the entities on the same physical device),
# never by name. Without it, thermal learning fell back to generic
# constants. Zero or several matches return None and log once: the
# explicit power-sensor setting covers those cases, and picking one of
# several would be confidently wrong (#118).
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
    module's own `solver_shared.ha_call_service_with_response()` -- so it works in BOTH
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

    response = solver_shared.ha_call_service_with_response(
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
        # tests/v2/orchestration/test_writer_inputs.py pins it.
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
        # Logged, not a bare `pass`: a silent swallow here hid an
        # entity-id collision for days (2026-08-31). Still caught, so a
        # failure never takes down the solve.
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
        flex_reports.publish_daily_flex_report(cfg_unmoded, now)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        _LOGGER.warning("Nimbus: daily flex report publish failed: %s", e)

    # Same "never break the real solve" wrapping; see
    # publish_nimbus_only_soc_counterfactual()'s docstring.
    try:
        counterfactual_reports.publish_nimbus_only_soc_counterfactual(cfg_unmoded, now)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        # Logged, not swallowed (2026-08-31), as above.
        _LOGGER.warning("Nimbus: counterfactual SoC publish failed: %s", e)

    # Same "never break the real solve" wrapping; see
    # publish_efficiency_backtest_report()'s docstring.
    try:
        backtest_reports.publish_efficiency_backtest_report(cfg_unmoded, now)
    except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
        # Logged, not swallowed (2026-08-31), as above.
        _LOGGER.warning("Nimbus: efficiency backtest publish failed: %s", e)

    # Same "never break the real solve" wrapping -- see
    # update_solar_delivery_ratio()'s own docstring (nimbus issue #128).
    try:
        solar_delivery = solar_inputs_solar.update_solar_delivery_ratio(
            cfg, now, grid_times, solar_kw
        )
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


@dataclass(frozen=True)
class _Settings:
    """What `_settings_phase()` hands the rest of `main()` (#2289 item 6 step 9)."""

    cfg: dict
    cfg_unmoded: dict
    now: datetime
    grid_times: list[datetime]
    period_hours_arr: list[float]
    n_periods: int


def _settings_phase() -> _Settings:
    """The solve's settings, household mode, start time and time grid: the first
    phase of `main()`, moved verbatim (#2289 item 6 step 9)."""
    # Fail fast, with a real, actionable message, if the Solver hasn't
    # been configured yet -- see solver_shared.fetch_solver_config()'s own docstring
    # for the full "installable by anyone" context this closes.
    cfg = solver_shared.fetch_solver_config()
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
    grid_times, period_hours_arr = solver_shared.build_tiered_grid(now)
    n_periods = len(grid_times)
    return _Settings(
        cfg=cfg,
        cfg_unmoded=cfg_unmoded,
        now=now,
        grid_times=grid_times,
        period_hours_arr=period_hours_arr,
        n_periods=n_periods,
    )


@dataclass(frozen=True)
class _Inputs:
    """What `_input_phase()` hands the rest of `main()` (#2289 item 6 step 9)."""

    failed_load_entities: Any
    live_load_kw: Any
    load_forecast_coverage_hours: Any
    load_forecast_error: Any
    load_forecast_source_decision: Any
    load_forecast_source_used: Any
    load_forecast_warnings: Any
    load_kw: Any
    load_lower_kw: Any
    load_upper_kw: Any
    solar_delivery: Any
    solar_kw: Any
    solar_lower_kw: Any
    solar_quantile_result: Any
    solar_upper_kw: Any
    summed_18_now_kw: Any
    whole_house_now_kw: Any


def _input_phase(
    cfg, cfg_unmoded, grid_times, n_periods, now, period_hours_arr
) -> _Inputs:
    """The solar and load inputs, the household load-total publish and the side reports: phase 2 of `main()`, moved verbatim (#2289 item 6 step 9)."""
    # Solar forecast sources -- every configured/auto-detected source
    # fetched, blended, and live-anchored for period 0. Moved out of
    # this function wholesale by nimbus issue #735 stage 1 (pure code
    # organization, no behaviour change): the real household findings,
    # the reversed "i do not want tricks" decision, and the specific
    # regressions each guard exists to prevent all live in
    # build_solar_arrays()'s own docstring rather than being restated
    # here in two places.
    _solcast_band: list = []
    solar_kw, solar_lower_kw, solar_upper_kw = solar_inputs_solar.build_solar_arrays(
        cfg, grid_times, n_periods, now, solcast_band_out=_solcast_band
    )
    # nimbus issue #1807: calibrated p10/p90 beside the envelope above.
    solar_quantile_result = solar_quantiles.update_and_build(
        sys.modules[__name__],
        cfg,
        now,
        grid_times,
        list(period_hours_arr),
        solar_kw,
        _solcast_band[0] if _solcast_band else None,
    )

    # Household demand: the sum of individually forecast circuits
    # (solver_load_forecast_entities, read from cfg; nimbus #56/#60) when
    # set, else the single load-forecast sensor. Extracted to
    # solver_inputs/load.py (#735). summed_18_now_kw and load_kw[0] are both
    # carried: they differ when the whole-house cross-check is set (#100).
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

    # sensor.nimbus_household_load_total_forecast: the summed circuit total,
    # published before the solve so it reflects this run's inputs even if the
    # LP later fails. failed_load_entities lists the circuits that were
    # unavailable and counted as 0.0 this run.
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

    # The non-essential publishes (nimbus #735 stage 6). See
    # _publish_side_reports(): none of them may break the real solve.
    solar_delivery = _publish_side_reports(cfg, cfg_unmoded, now, grid_times, solar_kw)
    return _Inputs(
        failed_load_entities=failed_load_entities,
        live_load_kw=live_load_kw,
        load_forecast_coverage_hours=load_forecast_coverage_hours,
        load_forecast_error=load_forecast_error,
        load_forecast_source_decision=load_forecast_source_decision,
        load_forecast_source_used=load_forecast_source_used,
        load_forecast_warnings=load_forecast_warnings,
        load_kw=load_kw,
        load_lower_kw=load_lower_kw,
        load_upper_kw=load_upper_kw,
        solar_delivery=solar_delivery,
        solar_kw=solar_kw,
        solar_lower_kw=solar_lower_kw,
        solar_quantile_result=solar_quantile_result,
        solar_upper_kw=solar_upper_kw,
        summed_18_now_kw=summed_18_now_kw,
        whole_house_now_kw=whole_house_now_kw,
    )


@dataclass(frozen=True)
class _PriceSources:
    """What `_price_sources_phase()` hands the rest of `main()` (#2289 item 6 step 9)."""

    _prices: Any
    export_bonus_price: Any
    export_price_lower_band: Any
    export_real_mask: Any
    has_price_forecast_array: Any
    import_price_upper_band: Any
    import_real_mask: Any
    match_fraction: Any
    p2p_recent_volume_kwh: Any
    price_source_freshness: Any
    price_source_on_aemo: Any
    spot_export: Any
    spot_import_raw: Any


def _price_sources_phase(cfg, grid_times, n_periods, now) -> _PriceSources:
    """The price sources: the price forecast array check, the price arrays, and whether the source is AEMO. Phase 3 of `main()`, moved verbatim (#2289 item 6 step 9)."""
    # Two price paths, gated on whether a forecast-array price sensor is
    # configured (CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR), never on a
    # retailer's literal entity existing (2026-09-02). An install without
    # one runs the plain path.
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
    price_source_freshness = _prices.price_source_freshness
    price_source_on_aemo = bool(
        price_source_freshness and price_source_freshness.get("fallback") == "aemo"
    )
    return _PriceSources(
        _prices=_prices,
        export_bonus_price=export_bonus_price,
        export_price_lower_band=export_price_lower_band,
        export_real_mask=export_real_mask,
        has_price_forecast_array=has_price_forecast_array,
        import_price_upper_band=import_price_upper_band,
        import_real_mask=import_real_mask,
        match_fraction=match_fraction,
        p2p_recent_volume_kwh=p2p_recent_volume_kwh,
        price_source_freshness=price_source_freshness,
        price_source_on_aemo=price_source_on_aemo,
        spot_export=spot_export,
        spot_import_raw=spot_import_raw,
    )


@dataclass(frozen=True)
class _PriceBlend:
    """What `_price_blend_phase()` hands the rest of `main()` (#2289 item 6 step 9)."""

    export_price_cross_spread: Any
    export_price_source: Any
    import_price_cross_spread: Any
    import_price_source: Any
    price_consolidation_result: Any
    rrp_crosscheck_result: Any
    spot_export: Any
    spot_export_source: Any
    spot_import_raw: Any
    spot_import_source: Any


def _price_blend_phase(
    _prices,
    cfg,
    export_real_mask,
    grid_times,
    import_real_mask,
    now,
    period_hours_arr,
    price_source_on_aemo,
    spot_export,
    spot_import_raw,
) -> _PriceBlend:
    """The settled-period override, the secondary-source blend, consolidation, the RRP cross-check and the p5min guard. Phase 4 of `main()`, moved verbatim (#2289 item 6 step 9)."""
    # Every period inside the current settlement block takes the source's
    # live settled `state`, not a forecast or blended value (nimbus #220).
    # The settled price is a contractual fact, and a forecast-array lookup
    # can differ from it. That's period 0 plus the 1-minute tier-0 rows still
    # inside the same 5-minute block, counted from grid_times. If the state
    # can't be read, the resampled value stands.
    _block_start = grid_times[0].replace(
        minute=(_local(grid_times[0]).minute // 5) * 5, second=0, microsecond=0
    )
    _block_end = _block_start + timedelta(minutes=5)
    n_settled_periods = sum(1 for t in grid_times if _block_start <= t < _block_end)
    # The settled import and export prices are always the configured
    # sensors (2026-09-02: no retailer-specific literal branch).
    settled_import_sensor = cfg["solver_import_price_sensor"]
    settled_export_sensor = cfg["solver_export_price_sensor"]
    # nimbus #1793: a stale source's live state is the stale row too; on the
    # AEMO fallback the current interval keeps the live AEMO price already
    # set by build_price_arrays().
    if price_source_on_aemo:
        _settled_import_value = spot_import_raw[0]
        _settled_export_value = spot_export[0]
    else:
        _settled_import_value = price_blend.price_now(
            settled_import_sensor, fallback=spot_import_raw[0]
        )
        _settled_export_value = price_blend.price_now(
            settled_export_sensor, fallback=spot_export[0]
        )
    for _i in range(n_settled_periods):
        spot_import_raw[_i] = _settled_import_value
        spot_export[_i] = _settled_export_value

    # The configured source's own values, captured before any blending, so
    # import_price_raw / export_price_raw mean what the source said (nimbus
    # #216). Identical to the blended values on a single-source install.
    spot_import_source = list(spot_import_raw)
    spot_export_source = list(spot_export)

    # Optional second and third price sources, blended
    # (price_blend.blend_price_with_secondary_sources()). Every secondary defaults to
    # unset, so a single-source install is unchanged.
    spot_import_raw, import_price_cross_spread, import_price_source = (
        price_blend.blend_price_with_secondary_sources(
            spot_import_raw,
            cfg,
            ("solver_import_price_sensor_2", "solver_import_price_sensor_3"),
            grid_times,
            primary_real_mask=import_real_mask,
        )
    )
    spot_export, export_price_cross_spread, export_price_source = (
        price_blend.blend_price_with_secondary_sources(
            spot_export,
            cfg,
            ("solver_export_price_sensor_2", "solver_export_price_sensor_3"),
            grid_times,
            primary_real_mask=export_real_mask,
        )
    )
    # Re-assert the settled current-block value (nimbus repo issue #220):
    # price_blend.blend_price_with_secondary_sources() has no notion of "index 0 is
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
    if price_source_on_aemo:
        # nimbus #1793: say on every row that the primary was not used.
        for _i, _label in enumerate(import_price_source):
            if _label == "primary":
                import_price_source[_i] = price_freshness.FALLBACK_LABEL
        for _i, _label in enumerate(export_price_source):
            if _label == "primary":
                export_price_source[_i] = price_freshness.FALLBACK_LABEL

    # nimbus #1786 (Mark Purcell): the consolidated price and each source's
    # running error. Shadow (published only) unless
    # switch.nimbus_solver_price_consolidation_live is on, which replaces the
    # future periods' scheduling prices; the settled block is never touched,
    # and the P5MIN guard below still runs after it.
    price_consolidation_result = price_inputs.apply_price_consolidation(
        cfg,
        now,
        grid_times,
        period_hours_arr,
        n_settled_periods,
        _prices,
        spot_import_source,
        spot_export_source,
        # A stale source's settled price is AEMO's (#1793): nothing to score.
        None if price_source_on_aemo else _settled_import_value,
        None if price_source_on_aemo else _settled_export_value,
        spot_import_raw,
        spot_export,
        import_price_source,
        export_price_source,
    )

    rrp_crosscheck_result = price_inputs.rrp_crosscheck_report(cfg, now)

    # nimbus #1778 (Mark Purcell): while the P5MIN false-spike flag holds,
    # forward spikes in the SCHEDULING prices -- whatever the source, after
    # blending -- are replaced; the current interval keeps its real price.
    spot_import_raw, spot_export = p5min_guard.guard_schedule_prices(
        spot_import_raw,
        spot_export,
        grid_times,
        _cfg_num(
            cfg,
            "solver_p5min_false_spike_threshold_mwh",
            p5min_guard.DEFAULT_THRESHOLD_MWH,
        ),
        ha_get,
    )
    return _PriceBlend(
        export_price_cross_spread=export_price_cross_spread,
        export_price_source=export_price_source,
        import_price_cross_spread=import_price_cross_spread,
        import_price_source=import_price_source,
        price_consolidation_result=price_consolidation_result,
        rrp_crosscheck_result=rrp_crosscheck_result,
        spot_export=spot_export,
        spot_export_source=spot_export_source,
        spot_import_raw=spot_import_raw,
        spot_import_source=spot_import_source,
    )


@dataclass(frozen=True)
class _Limits:
    """What `_limits_phase()` hands the rest of `main()` (#2289 item 6 step 9)."""

    capacity_kwh: Any
    charge_cost: Any
    discharge_cost_arr: Any
    export_limit_kw: Any
    import_limit_kw: Any
    import_price: Any
    initial_pct: Any
    max_charge_kw: Any
    max_discharge_kw: Any
    max_pct: Any
    min_pct: Any
    salvage_value: Any
    static_export_limit_kw: Any
    static_import_limit_kw: Any


def _limits_phase(
    cfg, grid_times, has_price_forecast_array, n_periods, spot_import_raw
) -> _Limits:
    """The import price and the battery and grid limits: capacity, SoC bounds, charge and discharge rates and costs, salvage value and the grid envelope. Phase 5 of `main()`, moved verbatim (#2289 item 6 step 9)."""
    # TOU network fees and the flat fee rate apply to every install
    # (nimbus #152), and are zero until the household sets them.
    flat_fee_rate = _cfg_num(cfg, "solver_flat_fee_rate", 0.0)
    import_price = [
        spot_import_raw[i]
        + import_fee_rate(cfg, _local(grid_times[i]).hour)
        + flat_fee_rate
        for i in range(n_periods)
    ]

    # Battery settings come from the Solver settings (solver_shared.fetch_solver_config()).
    # Capacity is SoH-derated on this live dispatch path (nimbus #1013); see
    # resolve_effective_capacity_kwh().
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

    # The discharge-cost / salvage-value schedule (nimbus #348) applies only
    # with a forecast-array price sensor; the `else` branch uses the flat
    # settings. Its defaults are the flat settings at every hour since
    # 13 Oct 2026; see scheduled_discharge_cost_rate().
    if has_price_forecast_array:
        discharge_cost_arr = np.array(
            [scheduled_discharge_cost_rate(cfg, _local(t).hour) for t in grid_times]
        )
        salvage_value = scheduled_salvage_value_rate(cfg, _local(grid_times[-1]).hour)
    else:
        # Flat Discharge Cost and Salvage Value from the settings, no
        # schedule.
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
    return _Limits(
        capacity_kwh=capacity_kwh,
        charge_cost=charge_cost,
        discharge_cost_arr=discharge_cost_arr,
        export_limit_kw=export_limit_kw,
        import_limit_kw=import_limit_kw,
        import_price=import_price,
        initial_pct=initial_pct,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        max_pct=max_pct,
        min_pct=min_pct,
        salvage_value=salvage_value,
        static_export_limit_kw=static_export_limit_kw,
        static_import_limit_kw=static_import_limit_kw,
    )


@dataclass(frozen=True)
class _PriceArrays:
    """What `_price_arrays_phase()` hands the rest of `main()` (#2289 item 6 step 9)."""

    export_price: Any
    export_price_lower: Any
    import_price: Any
    import_price_upper: Any
    n_clamped: Any


def _price_arrays_phase(
    cfg,
    export_price_cross_spread,
    export_price_lower_band,
    grid_times,
    import_price,
    import_price_cross_spread,
    import_price_upper_band,
    n_periods,
    spot_export,
) -> _PriceArrays:
    """The export price, the price event, the bounds and bands, and the cross-source spread. Phase 6 of `main()`, moved verbatim (#2289 item 6 step 9)."""
    # The unclamped spot rate is the base export_price, and the P2P premium
    # goes through export_bonus_price. Nothing is clamped: network.py's
    # same-period wash-trade constraints make the free-money exploit
    # infeasible at any price (2026-08-16).
    export_price = list(spot_export)
    # nimbus issue #1213 (Mark Purcell): the price-event simulation, applied
    # here -- at the very END of price resolution, after the fallback
    # branches, after price_blend.blend_price_with_secondary_sources(), and after the
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
    # every line of this. See price_inputs.resolve_price_event_delta().
    _price_event_delta = price_inputs.resolve_price_event_delta(cfg, grid_times)
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
    import_price_upper = price_blend.apply_price_band(
        import_price, grid_times, import_price_upper_band
    )
    export_price_lower = price_blend.apply_price_band(
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
    return _PriceArrays(
        export_price=export_price,
        export_price_lower=export_price_lower,
        import_price=import_price,
        import_price_upper=import_price_upper,
        n_clamped=n_clamped,
    )


def main() -> None:
    _settings = _settings_phase()
    cfg = _settings.cfg
    cfg_unmoded = _settings.cfg_unmoded
    now = _settings.now
    grid_times = _settings.grid_times
    period_hours_arr = _settings.period_hours_arr
    n_periods = _settings.n_periods

    _input = _input_phase(
        cfg=cfg,
        cfg_unmoded=cfg_unmoded,
        grid_times=grid_times,
        n_periods=n_periods,
        now=now,
        period_hours_arr=period_hours_arr,
    )
    failed_load_entities = _input.failed_load_entities
    live_load_kw = _input.live_load_kw
    load_forecast_coverage_hours = _input.load_forecast_coverage_hours
    load_forecast_error = _input.load_forecast_error
    load_forecast_source_decision = _input.load_forecast_source_decision
    load_forecast_source_used = _input.load_forecast_source_used
    load_forecast_warnings = _input.load_forecast_warnings
    load_kw = _input.load_kw
    load_lower_kw = _input.load_lower_kw
    load_upper_kw = _input.load_upper_kw
    solar_delivery = _input.solar_delivery
    solar_kw = _input.solar_kw
    solar_lower_kw = _input.solar_lower_kw
    solar_quantile_result = _input.solar_quantile_result
    solar_upper_kw = _input.solar_upper_kw
    summed_18_now_kw = _input.summed_18_now_kw
    whole_house_now_kw = _input.whole_house_now_kw

    _price_sources = _price_sources_phase(
        cfg=cfg, grid_times=grid_times, n_periods=n_periods, now=now
    )
    _prices = _price_sources._prices
    export_bonus_price = _price_sources.export_bonus_price
    export_price_lower_band = _price_sources.export_price_lower_band
    export_real_mask = _price_sources.export_real_mask
    has_price_forecast_array = _price_sources.has_price_forecast_array
    import_price_upper_band = _price_sources.import_price_upper_band
    import_real_mask = _price_sources.import_real_mask
    match_fraction = _price_sources.match_fraction
    p2p_recent_volume_kwh = _price_sources.p2p_recent_volume_kwh
    price_source_freshness = _price_sources.price_source_freshness
    price_source_on_aemo = _price_sources.price_source_on_aemo
    spot_export = _price_sources.spot_export
    spot_import_raw = _price_sources.spot_import_raw

    _price_blend = _price_blend_phase(
        _prices=_prices,
        cfg=cfg,
        export_real_mask=export_real_mask,
        grid_times=grid_times,
        import_real_mask=import_real_mask,
        now=now,
        period_hours_arr=period_hours_arr,
        price_source_on_aemo=price_source_on_aemo,
        spot_export=spot_export,
        spot_import_raw=spot_import_raw,
    )
    export_price_cross_spread = _price_blend.export_price_cross_spread
    export_price_source = _price_blend.export_price_source
    import_price_cross_spread = _price_blend.import_price_cross_spread
    import_price_source = _price_blend.import_price_source
    price_consolidation_result = _price_blend.price_consolidation_result
    rrp_crosscheck_result = _price_blend.rrp_crosscheck_result
    spot_export = _price_blend.spot_export
    spot_export_source = _price_blend.spot_export_source
    spot_import_raw = _price_blend.spot_import_raw
    spot_import_source = _price_blend.spot_import_source

    _limits = _limits_phase(
        cfg=cfg,
        grid_times=grid_times,
        has_price_forecast_array=has_price_forecast_array,
        n_periods=n_periods,
        spot_import_raw=spot_import_raw,
    )
    capacity_kwh = _limits.capacity_kwh
    charge_cost = _limits.charge_cost
    discharge_cost_arr = _limits.discharge_cost_arr
    export_limit_kw = _limits.export_limit_kw
    import_limit_kw = _limits.import_limit_kw
    import_price = _limits.import_price
    initial_pct = _limits.initial_pct
    max_charge_kw = _limits.max_charge_kw
    max_discharge_kw = _limits.max_discharge_kw
    max_pct = _limits.max_pct
    min_pct = _limits.min_pct
    salvage_value = _limits.salvage_value
    static_export_limit_kw = _limits.static_export_limit_kw
    static_import_limit_kw = _limits.static_import_limit_kw

    _price_arrays = _price_arrays_phase(
        cfg=cfg,
        export_price_cross_spread=export_price_cross_spread,
        export_price_lower_band=export_price_lower_band,
        grid_times=grid_times,
        import_price=import_price,
        import_price_cross_spread=import_price_cross_spread,
        import_price_upper_band=import_price_upper_band,
        n_periods=n_periods,
        spot_export=spot_export,
    )
    export_price = _price_arrays.export_price
    export_price_lower = _price_arrays.export_price_lower
    import_price = _price_arrays.import_price
    import_price_upper = _price_arrays.import_price_upper
    n_clamped = _price_arrays.n_clamped

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
    # price_inputs.resolve_price_spike_override()'s own docstring): real-time "sell
    # into a price spike, right now" override. spike_detected is
    # published below regardless of whether spike_override_kw ends up
    # armed/active, so the dashboard's own "$$$" visibility signal fires
    # purely on detection.
    spike_override_kw, price_spike_active = price_inputs.resolve_price_spike_override(
        cfg, import_price[0]
    )
    spike_override_kw = price_inputs.clamp_spike_override_to_live_ceiling(
        cfg, spike_override_kw, max_discharge_kw
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
        solar_p10_kw=solar_quantile_result["p10"],
        solar_p90_kw=solar_quantile_result["p90"],
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
    tariff_attributed_cost_by_subentry = price_inputs.compute_tariff_attributed_cost(
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
        import_price_provider_until=provider_forecast_until(
            import_real_mask, grid_times
        ),
        export_price_provider_until=provider_forecast_until(
            export_real_mask, grid_times
        ),
        price_source_freshness=price_source_freshness,
        price_consolidation=price_consolidation_result,
        rrp_crosscheck=rrp_crosscheck_result,
        stochastic=_assembly.stochastic,
        stochastic_paths=_assembly.stochastic_paths,
        solar_quantiles=solar_quantile_result,
    )
    # nimbus issue #494 (Signals 5/7 of #489): no-op unless offer_curve_
    # enabled was true above (plan.offer_curve_import stays None
    # otherwise) -- see publish_offer_curve()'s own docstring. #2121: the
    # curve is swept here, after the plan and its dispatch are published.
    plan = solver_publish.sweep_offer_curve(plan, monotonic())
    solver_publish.publish_offer_curve(plan)
    # nimbus issue #496 (Signals 7/7 of #489): no-op unless flex_signals_
    # enabled was true above (plan.grid_signals stays None otherwise) --
    # see publish_flex_signals()'s own docstring.
    solver_publish.publish_flex_signals(plan, hold_last=_assembly.flex_ranging_deferred)
    # nimbus issue #495 (Signals 6/7 of #489): the nem-flex-telemetry
    # schema-v2.0 record, one per completed 5-minute interval. Not gated on
    # the flex-signals switch since #1640: the flexibility fields come from
    # physical availability, so no ranging is needed. When no record can be
    # built, the sensor carries the reason instead (#1635).
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
        house_load_kw=_measured_house_load_kw(
            load_kw, load_forecast_error, live_load_kw
        ),
    )
