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
import os
import re
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]

try:
    from .. import device_forecasts
except ImportError:  # pragma: no cover - standalone/cron path
    import device_forecasts  # type: ignore[no-redef]

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


def _load_total_sensor(cfg):
    """The whole-house load forecast sensor the Solver reads: in `active`,
    the device setup's `demand_forecast` binding (DC-006 slice 3, the load
    total), read by the same reader with the same failure rules; otherwise,
    or when the binding is not readable, `solver_load_forecast_sensor`."""
    binding = cfg.get("device_contract_load_forecast")
    if device_forecasts.readable(binding):
        return binding["source"]["entity_id"]
    return cfg["solver_load_forecast_sensor"]


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
        summed_error = near_zero_summed_load_error(
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
        ) = cast(
            # On error the three lists are None; the error branch below
            # replaces them with zeros before anything reads them.
            tuple[list[float], list[float], list[float], str | None, float | None],
            read_load_forecast_sensor(_load_total_sensor(cfg), grid_times, now),
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
            if _is_transient_startup_load_forecast_error(load_forecast_error):
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
            _notify_load_forecast_error_once(
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
            _clear_load_forecast_error_notification_if_needed()
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
            # nimbus #1570: this overwrote period 0 with the raw value, so a
            # W sensor made the plan's first period 1000x too large.
            live_load_kw = float(
                sw.ha_get(whole_house_cross_check_sensor)["state"]
            ) * sw._kw_scale_factor(whole_house_cross_check_sensor)
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


# --- moved from solver_writer.py (#2289 item 6, step 5): the load forecast's readers and notification ---


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
    `solver_shared.ha_get(entity_id)["attributes"]["forecast"]` -- zero shape check,
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
    (the reference household, 2026-08-16, its adjusted total
    sensor): a household's own
    circuit sensors collectively have a meaningfully HIGHER chance that
    "at least one is briefly offline" than a single Modbus connection
    does -- guarding only the OUTER sum, not each individual term,
    doesn't help (a raw string-concatenation or KeyError from one bad
    entity happens before any outer guard gets a chance to catch it).
    Every entity sum_load_forecasts() fetches is wrapped exactly this
    way, individually, not just the total.
    """
    try:
        state = solver_shared.ha_get(entity_id)
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        KeyError,
        json.JSONDecodeError,
    ) as e:
        solver_shared._LOGGER.warning(
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
        solver_shared._LOGGER.warning(
            "Nimbus Solver: %s -- treating as 0.0 kW for this solve", error
        )
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
    the SAME _solver_writer().resample_forecast() every other forecast entity in this
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
    period beyond that circuit's real coverage is _solver_writer().resample_forecast()'s
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
            entity_coverage = _solver_writer().compute_forecast_coverage_hours(
                fc, anchor
            )
            if entity_coverage is not None:
                coverage_hours_per_entity.append(entity_coverage)
        pt_kw = _solver_writer().resample_forecast(fc, "value", grid_times)
        pt_lower = _solver_writer().resample_forecast(fc, "lower", grid_times)
        pt_upper = _solver_writer().resample_forecast(fc, "upper", grid_times)
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

    # An empty `forecast: []` is a forecaster that hasn't trained yet, not a
    # malformed sensor, so it gets its own message: wait, rather than fix
    # (test box, 2026-08-24).
    if isinstance(raw_fc, list) and not raw_fc:
        # nimbus #374: a Nimbus forecast sensor (it publishes
        # `model_trained_at`) with an empty forecast says why, so main() can
        # tell a startup race from a model that was discarded or never
        # trained, instead of solving against a flat 0 kW load for hours. A
        # third-party sensor without these keys keeps the plain
        # misconfiguration message.
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
            solver_shared.parse_iso(t_raw)
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
    unit = attrs.get("unit_of_measurement")
    scale = solver_shared.power_scale_or_none(unit)  # nimbus #1570, #1643
    if scale is None:
        return (
            None,
            False,
            (
                f"{entity_id} reports '{unit}', which is not a power unit "
                "(W or kW), so it is not read as a load forecast (nimbus #1643)."
            ),
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
    -- computed on the RAW fc_dicts, before _solver_writer().resample_forecast() pads
    flat past it. None on failure or an unparseable list.
    """
    try:
        state = solver_shared.ha_get(entity_id)
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
        _solver_writer().compute_forecast_coverage_hours(fc_dicts, anchor)
        if anchor is not None
        else None
    )

    load_kw = [
        max(0.0, v)
        for v in _solver_writer().resample_forecast(fc_dicts, "value", grid_times)
    ]
    if has_bands:
        # Exactly the original pre-#66 behavior for the canonical shape:
        # _solver_writer().resample_forecast() returns 0.0 for every point when a source
        # genuinely has no lower/upper keys at all, and the clamp below
        # then widens that to [0, load_kw] rather than a zero-width band
        # -- preserved as-is for backward compatibility with any
        # existing install already relying on this exact shape.
        load_lower_kw = [
            max(0.0, v)
            for v in _solver_writer().resample_forecast(fc_dicts, "lower", grid_times)
        ]
        load_upper_kw = [
            max(0.0, v)
            for v in _solver_writer().resample_forecast(fc_dicts, "upper", grid_times)
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

    # nimbus #118: a load forecast near zero across nearly every period is
    # flagged, not solved against. A house never sits at zero for most of a
    # multi-day forecast (solar does, at night, and is handled separately in
    # main()). The usual cause is solver_load_forecast_sensor pointed at
    # Nimbus's own household-total aggregator, a circular reference.
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
    _solver_writer().LOAD_FORECAST_ERROR_NOTIFIED_PATH's own comment) holding the exact
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
        if os.path.exists(_solver_writer().LOAD_FORECAST_ERROR_NOTIFIED_PATH):
            with open(
                _solver_writer().LOAD_FORECAST_ERROR_NOTIFIED_PATH,
                "r",
                encoding="utf-8",
            ) as f:
                already = f.read()
        key = error_key if error_key is not None else error
        if already == key:
            return
        solver_shared.ha_call_service(
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
        with open(
            _solver_writer().LOAD_FORECAST_ERROR_NOTIFIED_PATH, "w", encoding="utf-8"
        ) as f:
            f.write(key)
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        # nimbus issue #363 (Mark Purcell, codebase review): the swallow
        # stays (a failed notification must never break the real solve),
        # but this used to have zero breadcrumb at all -- if this fires
        # repeatedly, the household never gets the intended persistent
        # notification about their own load-forecast misconfiguration,
        # which is worth being able to diagnose.
        solver_shared._LOGGER.debug(
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
        if not os.path.exists(_solver_writer().LOAD_FORECAST_ERROR_NOTIFIED_PATH):
            return
        solver_shared.ha_call_service(
            "persistent_notification",
            "dismiss",
            {"notification_id": "nimbus_solver_load_forecast_error"},
        )
        os.remove(_solver_writer().LOAD_FORECAST_ERROR_NOTIFIED_PATH)
    except Exception:  # noqa: BLE001 -- exc_info logged below; ruff's logger-objects can't trace _LOGGER through this file's dual-mode try/except import (nimbus issue #1301)
        solver_shared._LOGGER.debug(
            "Nimbus Solver: _clear_load_forecast_error_notification_if_needed failed",
            exc_info=True,
        )
