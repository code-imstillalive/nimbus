"""Native, in-process runtime for the Nimbus Solver -- the "pure
integration" path (2026-08-22).

Real motivation, not a nice-to-have: Mark Purcell hit a genuine wall
trying to install nimbus_solver_app (this repo's HAOS Supervisor add-on)
against this private repo -- Supervisor's own "Add repository" flow does
a raw, unauthenticated `git clone`, no PAT support at all, so it fails
outright (`fatal: could not read Username for 'https://github.com'`)
regardless of the HACS integration itself already working fine for him
(HACS CAN authenticate). Separately, and more importantly, he flagged
the deeper architectural point directly: "EMHASS had the addon, which
was always a complication for access logs and sending commands... HAEO
runs as a pure integration." Right on both counts -- Nimbus has zero
live control today so that complication hasn't bitten yet, but it's the
right foundation to have in place BEFORE it ever does.

This module is the fix: it runs solver_writer.py (this same package,
BYTE-IDENTICAL to the sibling 116KAT-HA-AI repo's own scripts/
nimbus_solver_forecast_writer.py -- see that file's own module docstring
for the full "why one script, kept in sync" story) natively, in-process,
on a timer -- no separate device, no cron, no addon, no Add-on Store
auth wall, no manual token file. Install via HACS, run the "Solver
settings" wizard, done.

HOW, without rewriting solver_writer.py's own ~2400 lines of already-
correct, already-live-tested business logic: that file already funnels
every single HA interaction through exactly three functions (ha_get,
ha_post_state, fetch_price_history) -- see its own "PURE INTEGRATION
seam" comment. set_native_hass(hass), called once below, switches all
three from REST calls to native hass.states/recorder calls. Every one
of the other ~2400 lines still just calls those three functions BY NAME,
completely unchanged, whether running standalone (cron/addon, REST
mode) or natively here.

THREADING MODEL, stated plainly (a real, deliberate tradeoff, not an
oversight): solver_writer.main() -- gathering every live sensor input
AND running the actual LP solve -- runs as ONE unit inside
hass.async_add_executor_job() below, on a worker thread, not the event
loop. Two real consequences, both accepted:
  1. The actual LP solve is genuinely blocking CPU work (real, measured
     ~0.4s at this project's own production scale) -- this is exactly
     what async_add_executor_job() exists for, no different from any
     other integration offloading real compute off the event loop.
  2. solver_writer.ha_get() therefore calls hass.states.get() FROM that
     worker thread, not the event loop -- a plain, synchronous, in-
     memory dict lookup under CPython's own GIL, which is safe in
     practice (no real data-race risk, worst case a very slightly stale
     read) even though HA's own documented convention is event-loop-only
     state access. This is the pragmatic, low-risk choice given the real
     alternative (restructuring ~2400 lines of interleaved fetch/compute
     logic into a strict "gather on the loop, then compute" two-phase
     shape) -- flagged here plainly rather than glossed over, per this
     project's own standing "don't overclaim, state the real tradeoff"
     discipline. solver_writer.ha_post_state()'s own native branch does
     NOT take this shortcut -- writing back to HA's state machine
     genuinely must happen on the event loop, so it hops back via
     hass.add_job() (HA's own thread-safe scheduling primitive),
     regardless of which thread called it from.

NOT YET LIVE-VERIFIED against a real HA instance -- built and reasoned
through carefully (see this project's own many "verify before shipping"
sessions for what that discipline normally looks like), including a live
web check of the real HA core recorder API this module's own
solver_writer.py native branch calls, but there is no live HA instance
in this dev environment to actually run it against. Every failure mode
here (missing entity, solve error, not-yet-configured) is caught and
logged rather than propagated, so a wrong assumption should surface as
a clear log line on the very first real test, not a crash that takes
down the rest of the integration.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Issues #294/#295 (Mark Purcell, 2026-08-31) -- a tiny, module-level
# registry, deliberately NOT in __init__.py or sensor.py: __init__.py's
# own price-watcher/cron code (the only real caller of record_solve_
# completed()/time_since_last_solve() below) already imports this module
# at top level, and sensor.py already imports it lazily inside async_
# setup_entry (see that file's own comment next to `from . import
# solver_runtime`) -- putting the registry here means neither of those
# two files needs a NEW import of the other, which would be circular
# (sensor.py -> __init__.py already can't happen; __init__.py already
# imports FROM sensor.py for object_id_from_source).
#
# _last_solve_completed_monotonic is monotonic, not wall-clock -- only
# elapsed time matters for issue #295's suppression window, and
# monotonic sidesteps any clock-skew/DST edge case, same reasoning
# _NimbusSolverPushSensor's own `_last_updated` field in sensor.py
# already uses. _price_latency_sensor is a single instance (only ever
# one hub, same "one per hub" assumption issue #294's own sensor class
# docstring makes) rather than a dict keyed by entry_id.
_last_solve_completed_monotonic: float | None = None
_price_latency_sensor = None


def register_price_latency_sensor(sensor) -> None:
    """Called once, from sensor.py's async_setup_entry, so the price-
    watcher/cron code in __init__.py has something to push observations
    into. `sensor` is a NimbusSolverPriceResponseLatencySensor -- typed
    loosely here (no import) so this module's own top-level scope stays
    free of homeassistant.components.sensor, which nothing else in this
    file needs."""
    global _price_latency_sensor
    _price_latency_sensor = sensor


def record_solve_completed(
    *,
    trigger_source: str,
    triggering_entity: str | None = None,
    price_change_at: datetime | None = None,
    debounce_s: float | None = None,
) -> None:
    """Record that a solve just completed -- the single call site both
    issue #295 (cron suppression, via time_since_last_solve() below) and
    issue #294 (the price-response-latency sensor) build on. Called from
    __init__.py's three real solve-trigger call sites (the phase-locked
    cron, the debounced event-driven price-change handler, and the
    startup-retry loop) immediately after `await async_run_solve(hass)`
    returns True -- deliberately NOT from inside async_run_solve() /
    _blocking() itself, since neither of those has any visibility into
    WHICH of the three triggered this particular cycle or what the
    triggering price event's own timestamp was; that context only exists
    at each call site in __init__.py.

    `trigger_source` is a plain string, not an enum, matching this
    project's own existing convention for this class of internal-only
    tag (e.g. solver_writer.py's own `dispatch_direction`) -- expected
    values are "cron", "price_change", and "startup", but nothing here
    validates that; an unrecognised value just means the sensor update
    below is skipped, exactly like "cron"/"startup" already are.

    Per issue #294's own explicit design ("For trigger_source=cron,
    last_price_change_at is null and latency is null... so the sensor
    sits at its last event-driven value"): only trigger_source==
    "price_change" ever updates sensor.nimbus_solver_price_response_
    latency. A cron or startup solve still updates _last_solve_completed_
    monotonic (issue #295 needs that unconditionally, regardless of
    which trigger produced the most recent solve) but leaves the
    latency sensor's own published state untouched.
    """
    global _last_solve_completed_monotonic
    _last_solve_completed_monotonic = time.monotonic()
    if trigger_source != "price_change" or _price_latency_sensor is None:
        return
    solve_at = dt_util.utcnow()
    latency_s = (
        (solve_at - price_change_at).total_seconds()
        if price_change_at is not None
        else None
    )
    _price_latency_sensor.record(
        latency_s=latency_s,
        trigger_source=trigger_source,
        triggering_entity=triggering_entity,
        price_change_at=price_change_at,
        solve_at=solve_at,
        debounce_s=debounce_s,
    )


def time_since_last_solve() -> float | None:
    """Seconds since the last successful solve completed, from ANY
    trigger source, or None if no solve has completed yet this process
    (a fresh install/restart -- the cron should never suppress its very
    first tick just because nothing has run yet).

    Issue #295: the phase-locked periodic cron in __init__.py calls this
    before running its own solve, and skips entirely if a solve already
    completed within _CRON_SUPPRESS_WINDOW_S seconds -- treating the
    cron as a timeout/watchdog ("guarantee at least one solve per 5-min
    block") rather than a heartbeat that always fires regardless of
    whether an event-driven solve already covered this block.
    """
    if _last_solve_completed_monotonic is None:
        return None
    return time.monotonic() - _last_solve_completed_monotonic


# Lazily imported (see _ensure_ready() below) -- this module's own
# env-var-overridable state/lock file paths (NIMBUS_SOLVER_PLAN_STATE_
# PATH / NIMBUS_SOLVER_LOCK_PATH / NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_
# PATH) are read ONCE, as plain module-level
# `os.environ.get(...)` assignments, at first import -- they MUST be set
# before that import happens, not after, or they'd silently fall back to
# this file's own /opt/... NUC-specific defaults (wrong, and very likely
# unwritable, inside a real HA container).
_solver_writer = None
# In-memory only (2026-08-23, Bronze test-before-setup): a real HA
# restart is the honest re-check point for "is highspy importable now" --
# a file-based sentinel (like the load-forecast-error one below) would
# keep suppressing the notification across restarts even after e.g. a
# genuine architecture/wheel fix, which is exactly backwards for a
# dependency problem. Reset to False on every fresh import of this
# module, i.e. every HA start/reload.
_import_error_notified = False


def set_default_env_vars(hass: HomeAssistant) -> None:
    """The env vars solver_writer.py's own module-level code needs
    ALREADY set, correctly, before it is EVER imported anywhere in this
    package -- pure os.environ.setdefault()/hass.config.path() calls,
    no disk I/O, no solver_writer import, safe to call from the event
    loop (see the two real call sites: sensor.py's async_setup_entry,
    which genuinely does run on the event loop, and _ensure_ready()
    below, which is deliberately called from a worker thread for
    OTHER reasons -- see that function's own docstring).

    Real bug found live (nimbus issue #89, Mark Purcell, 2026-08-23):
    this used to be inlined ONLY inside _ensure_ready() below, which
    is called ONLY from solver_runtime.async_run_solve()'s own worker
    thread. sensor.py's async_setup_entry does its OWN, separate,
    direct `from . import solver_writer` (to reach
    register_entity_handler() -- see that file's own comment) --
    which, on a fresh HA start, is genuinely the FIRST import of
    solver_writer.py in the whole process, happening well before
    async_run_solve() is ever scheduled. At that point none of these
    four env vars had been set yet, so solver_writer.py's own
    module-level `sys.path.insert(0, os.environ.get("NIMBUS_SOLVER_
    PATH", <this household's own hardcoded NUC path>))` fell straight
    through to that hardcoded default -- which doesn't exist on any
    install except this one -- and the subsequent bare `from ml.blend
    import ...` / `from solver import ...` crashed with
    ModuleNotFoundError on literally every restart, on Mark's own
    real HACS install. Since Python only executes a module's
    top-level code on its FIRST import (later `from . import
    solver_writer` calls, e.g. from _ensure_ready() below, just
    return the already-cached module object), a crash here isn't a
    "one bad cycle, try again next tick" failure -- it's permanent
    for the life of the process, and the same is true for the OTHER
    three defaults (LOCK_PATH/PLAN_STATE_PATH/LOAD_FORECAST_ERROR_
    NOTIFIED_PATH all baked in wrong too, even on an install where the
    import happens not to crash) unless this runs before that very
    first import, from EVERY call site that can trigger it, not just
    one.
    """
    import os

    os.environ.setdefault(
        "NIMBUS_SOLVER_PATH", os.path.dirname(os.path.abspath(__file__))
    )
    # HA's own real, persistent, always-writable storage location --
    # correct on Docker, Supervised, and HAOS alike, unlike the sibling
    # standalone script's own NUC-specific /opt/... defaults.
    os.environ.setdefault(
        "NIMBUS_SOLVER_PLAN_STATE_PATH",
        hass.config.path("nimbus_solver_last_plan.json"),
    )
    os.environ.setdefault(
        "NIMBUS_SOLVER_LOCK_PATH", hass.config.path("nimbus_solver_writer.lock")
    )
    os.environ.setdefault(
        "NIMBUS_SOLVER_LOAD_ERROR_NOTIFIED_PATH",
        hass.config.path("nimbus_solver_load_forecast_error.txt"),
    )
    os.environ.setdefault(
        "NIMBUS_SOLVER_SOLAR_DELIVERY_RATIO_PATH",
        hass.config.path("nimbus_solver_solar_delivery_ratio.json"),
    )


def _ensure_ready(hass: HomeAssistant):
    global _solver_writer
    if _solver_writer is not None:
        return _solver_writer

    set_default_env_vars(hass)
    from . import (
        solver_writer as _sw,
    )

    _sw.set_native_hass(hass)
    _solver_writer = _sw
    return _solver_writer


def _log_dispatch_dry_run(hass: HomeAssistant, sw) -> None:
    """Real-dispatch groundwork, phase 1 (2026-08-27, hardened 2026-08-28):
    observe-only. Nimbus has never written to an inverter -- this
    function doesn't change that. When `switch.nimbus_solver_dispatch_
    dry_run` is on, it records what the CURRENT period's plan says the
    battery should be doing -- both a log line (unchanged from the
    original 2026-08-27 version, still useful for live tailing when the
    logger level is turned up) AND a real, durable sensor update via
    `sensor.nimbus_solver_dispatch_dry_run` (see NimbusDispatchDryRun-
    Sensor in sensor.py) -- so a household can watch several real
    cycles' worth of "what would have been sent" via HA's own History
    graphs / long-term statistics, not just whatever happens to still
    be in the log buffer.

    2026-08-28 finding, not theoretical: on a real live install
    (devhub), the switch had genuinely been on and the Solver genuinely
    solving on schedule, but nimbus_load's own effective logger level
    (WARNING by default) sits above this function's original bare
    _LOGGER.info() call -- meaning zero dry-run observations had ever
    actually been recorded anywhere, despite the mechanism "working."
    A dry run nobody can review afterward isn't evidence of anything.
    The sensor push below doesn't depend on logger level at all.

    There is deliberately no hass.services.call() anywhere near this
    function -- phase 2 (a real write path, its own separate switch, and
    a hard safety-clamp independent of the LP's own constraints) is a
    later, separate change, not something this toggle can accidentally
    reach early.

    Called from _blocking() on the same worker thread as sw.main() and
    solver_writer.ha_get()'s own native branch -- see this module's own
    top-of-file docstring for why a plain hass.states.get() from that
    thread is the accepted, already-established pattern here, not a new
    risk this function introduces. `sw` (the already-imported
    solver_writer module) is passed in from that same caller rather
    than re-imported here, so this function has no import concerns of
    its own. sw.ha_post_state() is itself thread-safe in native mode
    (routes through hass.add_job() internally -- see its own docstring),
    so calling it from this worker thread is the same already-proven
    pattern every other native-mode entity push in this codebase uses.
    Wrapped so a bug here can never turn a successful solve into a
    failed one -- this is pure observation, never worth costing the
    real plan a publish.
    """
    try:
        dry_run = hass.states.get("switch.nimbus_solver_dispatch_dry_run")
        if dry_run is None or dry_run.state != "on":
            return
        forecast_state = hass.states.get("sensor.nimbus_solver_battery_forecast")
        if forecast_state is None:
            return
        periods = forecast_state.attributes.get("forecast") or []
        if not periods:
            return
        current = periods[0]
        battery_kw = current.get("battery_kw")
        if battery_kw is None:
            return
        _LOGGER.info(
            "Nimbus Dispatch (dry-run): current-period plan is %.2f kW "
            "(positive=discharge, negative=charge) -- no command sent, "
            "live dispatch is not implemented yet.",
            battery_kw,
        )
        sw.ha_post_state(
            "sensor.nimbus_solver_dispatch_dry_run",
            round(float(battery_kw), 3),
            {
                "soc_pct": current.get("soc_pct"),
                "grid_import_kw": current.get("grid_import_kw"),
                "grid_export_kw": current.get("grid_export_kw"),
                "import_price": current.get("import_price"),
                "export_price": current.get("export_price"),
                "period_time": current.get("time"),
                "dry_run_enabled": True,
                "dispatch_direction": current.get("dispatch_direction"),
                "dispatch_source_a_label": current.get("dispatch_source_a_label"),
                "dispatch_source_a_pct": current.get("dispatch_source_a_pct"),
                "dispatch_source_b_label": current.get("dispatch_source_b_label"),
                "dispatch_source_b_pct": current.get("dispatch_source_b_pct"),
            },
        )
    except Exception:  # observation-only, must never affect the real solve
        _LOGGER.exception("Nimbus Dispatch (dry-run): logging failed, ignoring")


async def async_run_solve(hass: HomeAssistant) -> bool:
    """Run one real Solver cycle in-process, right now. Returns True on a
    genuine, successful push to sensor.nimbus_solver_battery_forecast;
    False on any handled failure (Solver settings not configured yet, a
    previous cycle still genuinely in progress, a real solve error) --
    never raises. Called from a periodic timer (__init__.py), where one
    bad cycle must never take down the next one, and safe to call
    directly too (e.g. a future "solve now" button)."""

    def _blocking() -> bool:
        # _ensure_ready() deliberately called IN HERE, not before
        # hass.async_add_executor_job() below (2026-08-23, real bug found
        # live on the reference household's own first-ever restart with
        # this feature enabled -- HA's own blocking-call detector caught
        # solver_writer.py's module-level TOKEN_PATH file read happening
        # ON THE EVENT LOOP, because _ensure_ready()'s own lazy `from .
        # import solver_writer` -- which executes that module's full
        # top-level code, including the token read, the FIRST time it's
        # called -- was being invoked synchronously before this executor
        # job even started. Only ever bites the very first call (every
        # later call just returns the already-cached module, a trivial,
        # genuinely non-blocking check) -- matches exactly one warning
        # in the real log, not one per cycle. Moving the whole call in
        # here means even that first-ever import (and its own blocking
        # disk I/O) correctly happens on the worker thread.
        global _import_error_notified
        try:
            sw = _ensure_ready(hass)
        except (ImportError, ModuleNotFoundError) as e:
            # Bronze test-before-setup/test-before-configure (2026-08-23):
            # before this, an import failure here (most commonly no
            # highspy wheel for this host's architecture -- see the top-
            # level README's own 64-bit-host caveat) propagated straight
            # out of _blocking(), up through async_run_solve() -- directly
            # violating that function's own documented "never raises"
            # contract, and __init__.py's own periodic-solve callback has
            # zero try/except of its own, trusting that contract
            # completely. The real, user-visible effect was a generic
            # "Error in periodic task" from HA's own dispatcher every
            # _SOLVER_INTERVAL, with the actual cause (a missing
            # dependency) buried in a traceback nobody would think to
            # read as "go check your architecture." Fixed the same way
            # solver_writer.py's own _notify_load_forecast_error_once()
            # already handles a different class of setup failure -- a
            # real, clear persistent_notification, fired once (not every
            # cycle; see _import_error_notified's own comment above for
            # why in-memory, not a file sentinel, is the right choice
            # here specifically).
            _LOGGER.error(
                "Nimbus Solver: failed to import required dependencies (%s) -- "
                "the Solver cannot run until this is fixed. See the top-level "
                "README's 64-bit-host (amd64/aarch64) requirement.",
                e,
            )
            if not _import_error_notified:
                _import_error_notified = True
                hass.add_job(
                    hass.services.async_call,
                    "persistent_notification",
                    "create",
                    {
                        "title": "Nimbus Solver: missing dependency",
                        "message": (
                            f"The Solver failed to start: {e}\n\n"
                            "This usually means `highspy` has no compiled wheel "
                            "for this host's CPU architecture -- confirmed "
                            "available for amd64/aarch64 only. Check `uname -m` "
                            "and see the top-level README's Solver section. "
                            "The Nimbus Forecaster (load predictions) is "
                            "completely unaffected and continues working "
                            "normally regardless."
                        ),
                        "notification_id": "nimbus_solver_import_error",
                    },
                )
            return False
        if not sw.acquire_lock():
            _LOGGER.debug(
                "Nimbus Solver: previous cycle still in progress -- skipping this one"
            )
            return False
        try:
            sw.main()
            _log_dispatch_dry_run(hass, sw)
            return True
        except RuntimeError as e:
            # fetch_solver_config()'s own "Solver settings not configured
            # yet" message -- expected on a fresh install before the
            # wizard's been run, not a real error.
            _LOGGER.warning("Nimbus Solver: %s", e)
            return False
        except Exception:
            _LOGGER.exception("Nimbus Solver: solve cycle failed")
            return False
        finally:
            sw.release_lock()

    return await hass.async_add_executor_job(_blocking)
