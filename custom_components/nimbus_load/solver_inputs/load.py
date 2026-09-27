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
import math
import re
import urllib.error
from dataclasses import dataclass
from datetime import timedelta

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]

try:
    from ..solver.forecast_source_selection import (
        POLICY_OFF,
        ForecastSourceDecision,
        blend_load_forecast,
        select_forecast_source,
    )
except ImportError:  # pragma: no cover - standalone/cron path
    from solver.forecast_source_selection import (  # type: ignore[no-redef]
        POLICY_OFF,
        ForecastSourceDecision,
        blend_load_forecast,
        select_forecast_source,
    )

#: One local day's worth of seconds, as the persistence lag. Seasonal-naive at
#: a 24 h period is what `forecast_regret.py`'s J_persistence scenario is built
#: from and what #937's own figures compare against -- "tomorrow looks like
#: today". Using anything else here would make the policy act on a baseline
#: different from the one the evidence was measured against.
_PERSISTENCE_LAG = timedelta(hours=24)

#: Raw recorder reads for the persistence baseline, keyed by
#: `(entity_id, hour bucket)`, so a 1-minute solve cadence does ONE 24-hour
#: recorder read per hour rather than sixty.
#:
#: A process-lifetime cache, so the cron path (one invocation, then exit) never
#: benefits and never needs to -- it runs once per cycle anyway. Bounded to a
#: handful of entries because the key's hour bucket rolls forward; stale buckets
#: are evicted rather than accumulating, since this module is imported for the
#: life of the HA process.
_PERSISTENCE_HISTORY_CACHE: dict[tuple[str, str], list[tuple[object, float]]] = {}
_PERSISTENCE_CACHE_MAX_ENTRIES = 4


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
    # nimbus issue #937 item 4. Carried rather than recomputed at the publish
    # site for the same reason `summed_18_now_kw` is: the decision is made
    # exactly once, inside the construction that acted on it, and a second
    # derivation is how two halves of one report come to disagree.
    #
    # Never None -- a decision is always made, including the `off` decision, so
    # the published attributes say which gate closed rather than going quiet.
    load_forecast_source_decision: object = None


def _seasonal_naive_load_kw(cfg, grid_times, period_hours, now):
    """A naive persistence load forecast over `grid_times`, or None.

    "Tomorrow looks like today" -- each grid period takes the REAL measured
    household load from the smallest whole number of 24 h periods back that
    lands inside the last day of recorder history. That is exactly the baseline
    `forecast_regret.py`'s `J_persistence` scenario is built from and exactly
    what #937's own -$0.71/day compares against, so the policy acts on the same
    baseline the evidence was measured against rather than a near-relative of
    it.

    **One 24-hour read serves any horizon.** A single shifted LOOKUP grid is
    built (`gt - k*24h`, `k = max(1, ceil((gt - now)/24h))`) and resampled once,
    rather than resampling per lag. Two resamples of a couple of thousand
    recorder rows against ~200 periods is real work to add to a 1-minute solve
    cycle, and the whole point of the `off` default is that no install pays it
    unless it asked to.

    The real load sensor is `solver_whole_house_cross_check_sensor`, which is
    this repo's own settled answer to "which entity counts as the real
    household load" -- `_compute_report_for_window()` resolves it the same way
    for the day-ahead scorer, and `build_load_arrays()` already reads it live
    for the period-0 anchor. Returns None when it is unconfigured: an install
    with no whole-house meter has no measured history to persist, and inventing
    one from the summed circuit forecasts would make the "persistence" baseline
    a function of the forecaster it is supposed to be an alternative to.
    """
    sensor = cfg.get("solver_whole_house_cross_check_sensor") or None
    if not sensor:
        return None
    bucket = now.replace(minute=0, second=0, microsecond=0).isoformat()
    cache_key = (sensor, bucket)
    hist = _PERSISTENCE_HISTORY_CACHE.get(cache_key)
    if hist is None:
        scale = solver_shared._kw_scale_factor(sensor)
        raw = solver_shared.fetch_entity_history_range(
            sensor, now - _PERSISTENCE_LAG, now
        )
        if not raw:
            return None
        hist = [(t, v * scale) for t, v in raw]
        if len(_PERSISTENCE_HISTORY_CACHE) >= _PERSISTENCE_CACHE_MAX_ENTRIES:
            # The key's hour bucket rolls forward, so old entries are dead
            # weight in a process that lives for months. Drop the oldest
            # bucket rather than letting the dict grow without bound.
            oldest = min(_PERSISTENCE_HISTORY_CACHE, key=lambda k: k[1])
            _PERSISTENCE_HISTORY_CACHE.pop(oldest, None)
        _PERSISTENCE_HISTORY_CACHE[cache_key] = hist
    lookup_times = []
    for gt in grid_times:
        ahead_days = (gt - now).total_seconds() / _PERSISTENCE_LAG.total_seconds()
        lag_periods = max(1, math.ceil(ahead_days))
        lookup_times.append(gt - lag_periods * _PERSISTENCE_LAG)
    values = solver_shared.resample_history_mean(hist, lookup_times, period_hours)
    if not any(v > 0.0 for v in values):
        # An all-zero (or negative-only) persistence baseline is not a baseline.
        # `_day_ahead_forecast_regret_attributes()` refuses one for exactly this
        # reason -- it would make the ML forecast look arbitrarily good by
        # comparison -- and here the direction of harm is worse: the LP would
        # plan a real battery against a household that draws nothing.
        return None
    # Never negative: the LP's load leg is a demand, and a momentary negative
    # meter read (the noisy-sensor class this project already fixed once for the
    # live P2P automation) must not become a negative demand in a plan.
    return [max(0.0, float(v)) for v in values]


def _resolve_load_forecast_source(cfg, grid_times, period_hours, now, load_kw):
    """Decide whether this cycle's LP consumes the ML load forecast,
    persistence, or a blend -- and build the persistence array only if the
    decision could possibly need it (nimbus issue #937 item 4).

    Returns `(load_kw_or_blended, decision, persistence_kw_or_None)`. `load_kw`
    comes back UNCHANGED, same object, whenever the decision is "ml" -- which is
    every install that has not set
    `select.nimbus_solver_load_forecast_source_policy` away from `off`, and
    which is what makes this additive to a live dispatch path rather than a
    change to it.

    The persistence array is returned rather than rebuilt by the caller for the
    uncertainty band: two resamples of the same recorder rows against the same
    grid is real work on a 1-minute cadence, and two derivations of one array is
    the drift shape this module's own docstring warns about for
    `summed_18_now_kw`.

    **The evidence is fetched only after the policy has been read**, and
    `POLICY_OFF` short-circuits before either the quality-report read or the
    recorder read. So the default costs one `cfg.get()` per cycle and nothing
    else -- no extra HTTP call, no extra recorder query, no arithmetic on the
    load array.
    """
    policy = cfg.get("solver_load_forecast_source_policy") or POLICY_OFF
    if policy == POLICY_OFF:
        return (
            load_kw,
            select_forecast_source(
                value_add_by_day={}, today=now.date(), policy=POLICY_OFF
            ),
            None,
        )
    sw = _solver_writer()
    decision = select_forecast_source(
        value_add_by_day=sw.read_day_ahead_value_add_history(),
        today=now.date(),
        policy=policy,
    )
    if decision.persistence_weight <= 0.0:
        return load_kw, decision, None
    persistence_kw = _seasonal_naive_load_kw(cfg, grid_times, period_hours, now)
    if persistence_kw is None or len(persistence_kw) != len(load_kw):
        # A policy that asked for persistence and cannot have it must say so and
        # keep the ML forecast, never silently half-apply. Reported as its own
        # reason so "I set the policy and nothing changed" is answerable from
        # the published attributes alone.
        solver_shared._LOGGER.warning(
            "Nimbus #937: load forecast source policy %r wanted persistence "
            "weight %.3f but no usable persistence baseline was available "
            "(configure solver_whole_house_cross_check_sensor, or check its "
            "recorder history) -- keeping the ML forecast this cycle",
            policy,
            decision.persistence_weight,
        )
        return (
            load_kw,
            ForecastSourceDecision(
                source="ml",
                persistence_weight=0.0,
                policy=policy,
                reason="persistence_baseline_unavailable",
                days_scored=decision.days_scored,
                days_persistence_won=decision.days_persistence_won,
                mean_value_add_dollars=decision.mean_value_add_dollars,
            ),
            None,
        )
    return (
        blend_load_forecast(load_kw, persistence_kw, decision.persistence_weight),
        decision,
        persistence_kw,
    )


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
            solver_shared._LOGGER.warning("Nimbus Solver: %s", summed_error)
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
            solver_shared._LOGGER.warning("Nimbus Solver: %s", load_forecast_error)
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
            solver_shared._LOGGER.warning(
                "Nimbus Solver: whole-house cross-check unavailable (%s)", e
            )
            whole_house_now_kw = None
    summed_18_now_kw = load_kw[0]

    # nimbus issue #937 item 4: the measured day-ahead verdict is allowed to
    # change what the LP consumes for load -- if the household has said so.
    # Default `off` returns `load_kw` unchanged, same object.
    #
    # **Placed here on purpose, and the position is load-bearing in both
    # directions.** AFTER `summed_18_now_kw` is snapshotted, so the
    # forecast-vs-forecast cross-check diagnostic keeps comparing the two
    # genuine forecasts it was built to compare (#100/#429) rather than
    # silently becoming a blend-vs-forecast comparison. BEFORE the live
    # period-0 anchor below, so a real MEASURED reading still wins period 0
    # whatever the policy says -- persistence has nothing useful to add about
    # the instant a meter is currently reporting.
    #
    # The band moves with the central array and by the same weight, treating
    # persistence as a point value: a naive baseline carries no uncertainty
    # quantification, so at weight 1.0 the band correctly collapses onto it.
    # Blending the centre alone would leave `lower <= central <= upper` intact
    # only by luck, and the stochastic LP reads all three.
    period_hours = (
        (grid_times[1] - grid_times[0]).total_seconds() / 3600.0
        if len(grid_times) > 1
        else 0.25
    )
    (
        load_kw,
        load_forecast_source_decision,
        _persistence_kw,
    ) = _resolve_load_forecast_source(cfg, grid_times, period_hours, now, load_kw)
    if _persistence_kw is not None:
        weight = load_forecast_source_decision.persistence_weight
        if len(_persistence_kw) == len(load_lower_kw):
            load_lower_kw = blend_load_forecast(load_lower_kw, _persistence_kw, weight)
        if len(_persistence_kw) == len(load_upper_kw):
            load_upper_kw = blend_load_forecast(load_upper_kw, _persistence_kw, weight)
        solver_shared._LOGGER.info(
            "Nimbus #937: load forecast source %s (policy=%s, weight=%.3f, "
            "reason=%s, %d/%d trailing days favoured persistence, mean "
            "value-add %s)",
            load_forecast_source_decision.source,
            load_forecast_source_decision.policy,
            load_forecast_source_decision.persistence_weight,
            load_forecast_source_decision.reason,
            load_forecast_source_decision.days_persistence_won,
            load_forecast_source_decision.days_scored,
            load_forecast_source_decision.mean_value_add_dollars,
        )

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
        load_forecast_source_decision=load_forecast_source_decision,
    )
