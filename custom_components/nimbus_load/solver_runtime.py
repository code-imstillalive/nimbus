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

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

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


def _ensure_ready(hass: HomeAssistant):
    global _solver_writer
    if _solver_writer is not None:
        return _solver_writer
    import os

    # Real bug caught before it ever shipped (2026-08-22): solver_writer.py
    # is BYTE-IDENTICAL to the sibling standalone script, which means its
    # own `sys.path.insert(0, os.environ.get("NIMBUS_SOLVER_PATH", ...))`
    # line is ALSO still in here -- defaulting to THIS HOUSEHOLD's own
    # hardcoded NUC path (/opt/homeassistant/config/nimbus_repo/
    # custom_components/nimbus_load). Left unset, that would make
    # solver_writer.py try to import its own `solver`/`ml` sibling
    # packages from a path that doesn't exist on anyone else's system --
    # wrong even though those exact packages are sitting right next to it
    # RIGHT NOW. Point it at THIS file's own real, actual directory
    # (wherever HACS/HA really installed nimbus_load -- Docker, Supervised,
    # HAOS, doesn't matter) before the very first import.
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
    from . import (
        solver_writer as _sw,
    )

    _sw.set_native_hass(hass)
    _solver_writer = _sw
    return _solver_writer


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
