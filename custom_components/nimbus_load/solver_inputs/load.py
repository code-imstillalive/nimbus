"""Load forecast input gathering for the Solver cycle.

nimbus issue #735 stage 2. Stage 1 extracted the solar slice; this is the
load slice, and it is deliberately **not** the clean analogue of it.

Measured before it was moved, because every blocker found on this issue so
far (#860, #861, and this stage's own three traps) was invisible from
reading the code being moved:

- **The seam is narrow on inputs and wide on outputs.** Four in, twelve
  out -- against solar's three out. That is why this returns a frozen
  dataclass rather than a tuple: a twelve-tuple at the call site would be
  unreadable and silently order-dependent.
- **An earlier count of this seam said eleven outputs.** It missed
  `load_forecast_entities`. Anyone building an eleven-field result from
  that count would have found a name unresolvable in `main()` afterwards.
- **A grep of this block's dependencies finds five module-level names. An
  AST walk finds fourteen.** The nine a grep misses -- three stdlib
  imports and six helpers -- are each a runtime `NameError`. The list was
  computed, not read.

**The ordering below is load-bearing and must not be tidied.**
`summed_18_now_kw` is snapshotted from `load_kw[0]` *before* the optional
live whole-house cross-check anchor overwrites `load_kw[0]` in place.
Recomputing it from the returned array afterwards reintroduces nimbus
issue **#100**, where `sensor.nimbus_household_load_total_forecast`'s own
`state` and `forecast[0].value` silently disagreed on any install with the
cross-check configured. Both values are returned precisely so the caller
never has to reconstruct either one.

The publish that used to sit inside this block was hoisted out first, into
`solver_publish.py` -- moving this block while it still contained an
`ha_post_state()` would have put a publish inside an inputs module and
contradicted the very split this issue is building.

See `solver_inputs/__init__.py` for why the `solver_writer` import is
deferred and by-module; the reasoning is identical and load-bearing.
"""

from __future__ import annotations

import json
import re
import urllib.error
from dataclasses import dataclass


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see this package's `__init__.py` for the full reasoning, and
    #861 for the concrete failure that made it load-bearing."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


@dataclass(frozen=True)
class LoadArrays:
    """Everything the load slice produces, in one value.

    `frozen=True` prevents rebinding a field; it deliberately does NOT
    make the arrays immutable, and they are genuinely mutated during
    construction (the anchor overwrite). The freeze is about the result
    object being a stable record of that construction, not about
    pretending the arrays are read-only.

    `summed_18_now_kw` and `load_kw[0]` will differ on any install with
    the whole-house cross-check configured -- that is not redundancy, it
    is the #100 distinction, and both are carried so no caller has to
    guess which one it wanted.
    """

    load_kw: object
    load_lower_kw: object
    load_upper_kw: object
    summed_18_now_kw: object
    whole_house_now_kw: object
    live_load_kw: object
    load_forecast_entities: object
    load_forecast_error: object
    load_forecast_warnings: object
    load_forecast_source_used: object
    load_forecast_coverage_hours: object
    failed_load_entities: object


def build_load_arrays(cfg, grid_times, n_periods, now) -> LoadArrays:
    """Fetch, resample and blend the household load forecast.

    Moved verbatim out of `main()`: the body below is the identical code,
    with only module-level names rebound onto the deferred module handle.
    No value is recomputed and no ordering is changed -- in particular the
    snapshot-before-overwrite sequence is preserved exactly (see this
    module's docstring for why tidying it is nimbus issue #100).
    """
    sw = _solver_writer()

    load_forecast_entities = cfg.get("solver_load_forecast_entities") or []
    load_forecast_error = None
    # Computed BEFORE the branch below runs so it always reflects which
    # branch is ABOUT to execute, not an inference from its result -- see
    # sw.resolve_load_forecast_source_label()'s own docstring.
    load_forecast_source_used = sw.resolve_load_forecast_source_label(
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
        ) = sw.sum_load_forecasts(
            load_forecast_entities,
            grid_times,
            sw._cfg_num(cfg, "solver_inverter_self_consumption_kw", 0.0),
            now,
        )
        # nimbus issue #933: #118's near-zero guard, for the summed
        # path. sum_load_forecasts() stays a pure summer -- its own
        # docstring makes the case for guarding each TERM rather than
        # the outer sum, and that is right -- so the decision to refuse
        # lives here, where load_forecast_error is already plumbed.
        #
        # Raising (rather than the zero-fallback below) is deliberate
        # and follows #370's own precedent for the transient case: the
        # failure modes this fires on are transient by nature (an HA
        # restart, a recorder stall, an integration reload, a template
        # chain briefly unavailable), so publishing nothing this cycle
        # and retrying on the next tick is the self-healing shape. The
        # alternative -- planning against a total that is missing data
        # -- is exactly what #118 established costs real money.
        summed_error = sw.near_zero_summed_load_error(
            load_kw,
            failed_load_entities,
            sw._cfg_num(cfg, "solver_inverter_self_consumption_kw", 0.0),
        )
        if summed_error is not None:
            sw._LOGGER.warning("Nimbus Solver: %s", summed_error)
            raise RuntimeError(f"Load forecast not usable: {summed_error}")
    else:
        # Validated read (2026-08-23, real fix for nimbus repo issue
        # #66) -- the old bare sw.ha_get(...)["attributes"]["forecast"]
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
        ) = sw.read_load_forecast_sensor(
            cfg["solver_load_forecast_sensor"], grid_times, now
        )
        if load_forecast_error is not None:
            # nimbus issue #370 (Mark Purcell, codebase review): a
            # transient startup-race error (the entity exists but hasn't
            # published real data yet) must never be silently treated as
            # "confirmed zero load" -- raising here instead means this
            # cycle publishes nothing (the sensor's own staleness
            # watchdog handles the rest, same self-healing shape as the
            # #365 config-sensor 404 fix), and the startup-retry loop
            # (or just the next periodic tick) simply tries again once
            # the source has real data. See _is_transient_startup_load_
            # forecast_error()'s own docstring for exactly which error
            # shapes this does/doesn't cover -- a genuine misconfiguration
            # still falls through to the zero-fallback + notification
            # below, unchanged.
            if sw._is_transient_startup_load_forecast_error(load_forecast_error):
                raise RuntimeError(
                    f"Load forecast not ready yet: {load_forecast_error}"
                )
            sw._LOGGER.warning("Nimbus Solver: %s", load_forecast_error)
            load_kw = [0.0] * n_periods
            load_lower_kw = [0.0] * n_periods
            load_upper_kw = [0.0] * n_periods
            # nimbus issue #416 (Mark Purcell): the 90%-zeros circular-
            # reference message (and any other error shape that embeds a
            # live count) bakes in numbers that change cycle to cycle --
            # normalizing them out here before de-dupe means the SAME
            # underlying condition, seen again on a later cycle with
            # different counts, is correctly recognized as unchanged
            # rather than re-notified as if it were new.
            sw._notify_load_forecast_error_once(
                load_forecast_error,
                error_key=re.sub(r"\d+/\d+", "N/N", load_forecast_error),
            )
        else:
            # nimbus issue #416: healthy this cycle -- if a notification
            # is still outstanding from an earlier cycle (including one
            # that was itself a transient startup-timing false positive,
            # not a real misconfiguration), clear it now rather than
            # leaving a stale, wrong, scary notification sitting there
            # indefinitely once the real forecast has recovered.
            sw._clear_load_forecast_error_notification_if_needed()
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
            whole_house_fc = sw.ha_get(whole_house_cross_check_entity)["attributes"][
                "forecast"
            ]
            whole_house_now_kw = max(
                0.0, sw.resample_forecast(whole_house_fc, "value", grid_times[:1])[0]
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            KeyError,
            json.JSONDecodeError,
        ) as e:
            sw._LOGGER.warning(
                "Nimbus Solver: whole-house cross-check unavailable (%s)", e
            )
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
    # nimbus issue #429 (Mark Purcell): live_load_kw itself (the one real,
    # actually-measured value this whole block reads) used to be computed
    # here and immediately discarded -- only ever used to overwrite
    # load_kw[0] for the solve, never published anywhere on its own. Both
    # load_summed_18_now_kw and load_whole_house_cross_check_now_kw below
    # are DELIBERATELY forecast-vs-forecast (see the comment above and at
    # each publish site) -- genuinely useful for catching a missing/
    # misconfigured circuit, but neither is what its own name/a
    # reasonable reader (confirmed live: Mark's own report read
    # load_whole_house_cross_check_now_kw AS "the real whole-house meter
    # cross-check", which is a real, understandable misreading given the
    # name) would assume: an actual live meter reading. There was no way
    # to do a genuine forecast-vs-reality check at all without this fix --
    # published below as load_whole_house_live_now_kw, additive only,
    # doesn't change either existing field's own value or meaning.
    live_load_kw = None
    if whole_house_cross_check_sensor and sw.entity_exists(
        whole_house_cross_check_sensor
    ):
        try:
            live_load_kw = float(sw.ha_get(whole_house_cross_check_sensor)["state"])
            load_kw[0] = max(0.0, live_load_kw)
            load_lower_kw[0] = load_kw[0]
            load_upper_kw[0] = load_kw[0]
        except (ValueError, KeyError, TypeError):
            live_load_kw = None
    return LoadArrays(
        load_kw=load_kw,
        load_lower_kw=load_lower_kw,
        load_upper_kw=load_upper_kw,
        summed_18_now_kw=summed_18_now_kw,
        whole_house_now_kw=whole_house_now_kw,
        live_load_kw=live_load_kw,
        load_forecast_entities=load_forecast_entities,
        load_forecast_error=load_forecast_error,
        load_forecast_warnings=load_forecast_warnings,
        load_forecast_source_used=load_forecast_source_used,
        load_forecast_coverage_hours=load_forecast_coverage_hours,
        failed_load_entities=failed_load_entities,
    )
