"""The quality-report cluster: EPR scoring, its published sensor, the
rolling history it carries forward, and the rescore path -- moved verbatim
out of `solver_writer.py`.

nimbus issue #1301, Phase 2c of #1298's decomposition, and the last and
largest piece of #1301: **2,380 lines** across five functions.

    _compute_report_for_window       1,280   the scoring orchestration
    publish_daily_quality_report       393   the sensor publish
    _soc_discrepancy_stats             274   SoC-vs-reconstruction check
    rescore_quality_history            234   the rescore_history service path
    _carry_forward_quality_history     199   the rolling history attribute

These five moved together because they are one cluster, not five
independent functions: `rescore_quality_history()` calls both
`_compute_report_for_window()` and `_carry_forward_quality_history()`,
`publish_daily_quality_report()` calls `_carry_forward_quality_history()`,
and `_compute_report_for_window()` calls `_soc_discrepancy_stats()`. The
#1298 dependency analysis put their module-level blocker counts at
15/12/11/4/1 with constants shared between them, so splitting them across
PRs would have meant a module that could not import itself.

A pure relocation, verified mechanically rather than asserted: every
inserted `sw.` was stripped back off and each of the five segments
compared byte-for-byte against the original lines -- all five IDENTICAL.
After `ruff format` re-wrapped lines that the added prefix pushed past the
limit, equivalence was re-established at the AST level.

**Every name that resolved in `solver_writer`'s namespace is reached as
`sw.<name>` -- including the other four cluster members.** That last part
is deliberate and is the one judgement call worth explaining. Sibling
calls *could* have been left as direct local calls now that all five live
here, and that would have been faster. But a direct call resolves inside
THIS module's namespace and so escapes `patch.object(solver_writer,
"_compute_report_for_window", ...)`. Going through `sw.` keeps resolution
exactly where it was before the move, for every one of the ~43 names
involved, under one rule with no exceptions to audit. The facade's
re-export aliases make `sw._compute_report_for_window` the identical
object, so this costs an attribute lookup and nothing else.

The private helpers these functions depend on (`_epr_reliability`,
`_achieved_feasibility_stats`, `_history_coverage_by_series`,
`battery_energy_balance`, `_regret_path_delta_share`, and the rest)
deliberately do NOT move. Several have other callers still in
`solver_writer`, and moving a helper also moves its internal callers,
which is exactly the seam hazard `solver_inputs/__init__.py` documents.
They stay put and are reached via `sw.`.

See `solver_reports/__init__.py` for the import-direction reasoning and
the measured patch counts.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import numpy as np

try:
    from ..solver import elements
except ImportError:  # pragma: no cover - standalone/cron path
    from solver import elements  # type: ignore[no-redef]


# nimbus issue #1248: how the currently-published quality report read back.
# An explicit status rather than a boolean "degraded", because the call site
# knows which of four things happened and flattening that to true/false is what
# conflated "the entity exists but I could not read its rows" (a real risk to
# real data) with "I could not reach HA" (routine, and knows nothing about
# whether rows existed).
PRIOR_READ_OK = "ok"
PRIOR_READ_ABSENT = "absent"  # 404 -- a genuine first publish
PRIOR_READ_UNAVAILABLE = "unavailable"  # read succeeded, attributes dropped
PRIOR_READ_UNREACHABLE = "unreachable"  # HA could not be reached at all
_PRIOR_READ_DEGRADED = (PRIOR_READ_UNAVAILABLE, PRIOR_READ_UNREACHABLE)


try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see this package's `__init__.py` for the full reasoning."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


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
    sw = _solver_writer()
    if day_end <= day_start:
        solver_shared._LOGGER.debug(
            "Nimbus quality: skip. Window end (%s) not after start (%s)",
            day_end.isoformat(),
            day_start.isoformat(),
        )
        return None

    solar_sensor = cfg.get("solver_solar_power_sensor")
    battery_sensor = cfg.get("solver_battery_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    if not solar_sensor or not battery_sensor or not load_sensor:
        solver_shared._LOGGER.debug(
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
        solver_shared._LOGGER.debug(
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
        sw.TIER1_PERIOD_HOURS
        if window_hours <= sw.MAX_TIER1_HOURS
        else sw.TIER2_PERIOD_HOURS
    )
    n_periods = round(window_hours / period_hours)
    if n_periods < 1:
        solver_shared._LOGGER.debug(
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
    solar_hist = sw.fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = sw.fetch_entity_history_range(load_sensor, day_start, day_end)
    battery_hist = sw.fetch_entity_history_range(battery_sensor, day_start, day_end)
    if not solar_hist or not load_hist or not battery_hist:
        solver_shared._LOGGER.info(
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
        coverage = sw._history_coverage_by_series(
            {
                str(solar_sensor): solar_hist,
                str(load_sensor): load_hist,
                str(battery_sensor): battery_hist,
            },
            day_start,
            day_end,
        )
        # nimbus issue #1477: a series that stops early is not necessarily
        # short. HA's recorder writes a row only on a CHANGE, so a sensor
        # holding a steady value -- PV at 0.0 from dusk to midnight -- writes
        # nothing after its last change, and "last row - first row" reads a
        # quiet evening as a 6-hour gap. On a real install that refused a
        # fully-closed day on every cycle, 200+ times. Only when the
        # threshold would fail, check each short series for a real outage
        # after its last row; with none, the value was held, so the span
        # runs to the window end (never past now). Unreadable -> keep
        # refusing: an unknown is not evidence the sensor was fine.
        if min(cov.hours for cov in coverage.values()) < (
            window_hours * sw._MIN_DAILY_COVERAGE_FRACTION
        ):
            held_until = min(day_end, datetime.now(day_end.tzinfo))
            needed = window_hours * sw._MIN_DAILY_COVERAGE_FRACTION
            for name, cov in list(coverage.items()):
                # Only the series that are themselves short -- the others
                # already pass, and each probe is a recorder read.
                if (
                    cov.hours >= needed
                    or cov.first is None
                    or cov.last is None
                    or cov.last >= held_until
                ):
                    continue
                if (
                    solver_shared.fetch_entity_went_unavailable(
                        name, cov.last, held_until
                    )
                    is False
                ):
                    coverage[name] = type(cov)(
                        (held_until - cov.first).total_seconds() / 3600.0,
                        cov.first,
                        held_until,
                    )
        covered = min(cov.hours for cov in coverage.values())
        if covered < window_hours * sw._MIN_DAILY_COVERAGE_FRACTION:
            # Worst first -- the one that actually failed the gate leads.
            # The bracketed span is the covered region's own edges in
            # local time; for a truncated window that is the gap
            # boundary #1054 went looking for by hand.
            breakdown = ", ".join(
                f"{name} {cov.hours:.2f} h"
                + (
                    " [no history]"
                    if cov.first is None or cov.last is None
                    else f" [{cov.first.astimezone(sw.LOCAL_TZ):%H:%M}-"
                    f"{cov.last.astimezone(sw.LOCAL_TZ):%H:%M}]"
                )
                for name, cov in sorted(coverage.items(), key=lambda kv: kv[1].hours)
            )
            day_key = day_start.date().isoformat()
            seen = sw._COVERAGE_SKIP_COUNTS.get(day_key, 0) + 1
            sw._COVERAGE_SKIP_COUNTS[day_key] = seen
            # Routine on the first retries, a real WARNING once it is
            # clearly not the recorder catching up -- see
            # _COVERAGE_SKIP_WARN_AFTER for why this is not one level.
            log = (
                solver_shared._LOGGER.info
                if seen < sw._COVERAGE_SKIP_WARN_AFTER
                else solver_shared._LOGGER.warning
            )
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
                sw._MIN_DAILY_COVERAGE_FRACTION * 100.0,
                breakdown,
            )
            return None
        # Scored cleanly -- let a day that recovers stop warning.
        sw._COVERAGE_SKIP_COUNTS.pop(day_start.date().isoformat(), None)

    import_price_hist = sw.fetch_entity_history_range(
        cfg["solver_import_price_sensor"], day_start, day_end
    )
    export_price_hist = sw.fetch_entity_history_range(
        cfg["solver_export_price_sensor"], day_start, day_end
    )
    soc_sensor = cfg.get("solver_battery_soc_sensor")
    soc_hist = (
        sw.fetch_entity_history_range(
            soc_sensor, day_start - timedelta(hours=6), day_end
        )
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
        seen = sw._SOC_HISTORY_SKIP_COUNTS.get(day_key, 0) + 1
        sw._SOC_HISTORY_SKIP_COUNTS[day_key] = seen
        log = (
            solver_shared._LOGGER.info
            if seen < sw._COVERAGE_SKIP_WARN_AFTER
            else solver_shared._LOGGER.warning
        )
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
            sw._ASSUMED_INITIAL_SOC_PCT,
        )
        return None

    # Real, confirmed-live bug (2026-08-28) -- see _kw_scale_factor()'s
    # own docstring: these three configured sensors are never guaranteed
    # to already report kW (solar in particular is commonly a native
    # Watts sensor), and the rest of this function has always assumed
    # they are without checking.
    solar_scale = sw._kw_scale_factor(solar_sensor)
    load_scale = sw._kw_scale_factor(load_sensor)
    battery_scale = sw._kw_scale_factor(battery_sensor)
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
            for v in sw.resample_history_mean(solar_hist, grid_times, period_hours)
        ]
    )
    load_kw = np.array(
        [
            max(0.0, v * load_scale)
            for v in sw.resample_history_mean(load_hist, grid_times, period_hours)
        ]
    )
    actual_net_kw = np.array(
        [
            v * battery_scale * battery_sign
            for v in sw.resample_history_mean(battery_hist, grid_times, period_hours)
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
    home_power_coverage = sw._power_history_coverage(
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
            v + sw.import_fee_rate(cfg, sw._local(grid_times[i]).hour)
            for i, v in enumerate(
                sw.resample_history_nearest(
                    import_price_hist, grid_times, default=0.20, backfill_first=True
                )
            )
        ]
    )
    export_price = np.array(
        sw.resample_history_nearest(
            export_price_hist, grid_times, default=0.05, backfill_first=True
        )
    )

    # nimbus issue #1013: SoH-derated, same as every other BatteryConfig
    # construction -- see resolve_effective_capacity_kwh()'s docstring.
    capacity_kwh = sw.resolve_effective_capacity_kwh(cfg)
    min_pct = sw._cfg_num(cfg, "solver_battery_min_soc_percent", 5.0)
    max_pct = sw._cfg_num(cfg, "solver_battery_max_soc_percent", 100.0)
    initial_pct = (
        sw.resample_history_nearest(
            soc_hist,
            [day_start],
            default=sw._ASSUMED_INITIAL_SOC_PCT,
            backfill_first=True,
        )[0]
        if soc_hist
        else sw._ASSUMED_INITIAL_SOC_PCT
    )
    final_pct = (
        sw.resample_history_nearest(
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
        solver_shared._LOGGER.warning(
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
        solver_shared._LOGGER.warning(
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
        solver_shared._LOGGER.warning(
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
        max_charge_kw=sw._cfg_num(cfg, "solver_max_charge_kw", 5.0),
        max_discharge_kw=sw._cfg_num(cfg, "solver_max_discharge_kw", 5.0),
        # solver_efficiency_percent is a single ROUND-TRIP figure, split
        # geometrically into per-direction charge/discharge efficiency
        # via sqrt() -- see main()'s own real BatteryConfig construction
        # for the canonical comment on why. Nimbus issue #168 (Mark
        # Purcell, 2026-08-25): this used to apply the round-trip value
        # directly to both directions instead of sqrt()-splitting it,
        # a real efficiency-convention mismatch against the live plan's
        # own oracle solve -- both branches must model the SAME battery
        # physics for EPR/regret to mean what it claims to mean.
        charge_efficiency=(sw._cfg_num(cfg, "solver_efficiency_percent", 90.0) / 100.0)
        ** 0.5,
        discharge_efficiency=(
            sw._cfg_num(cfg, "solver_efficiency_percent", 90.0) / 100.0
        )
        ** 0.5,
        charge_cost=sw._cfg_num(cfg, "solver_charge_cost", 0.01),
        discharge_cost=np.full(
            n_periods, sw._cfg_num(cfg, "solver_discharge_cost", 0.01)
        ),
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
        degradation_cost_per_kwh=sw._cfg_num(
            cfg, "solver_degradation_cost_per_kwh", 0.0
        ),
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
    participant_batteries = (
        sw.battery_participants_inputs._resolve_battery_participant_history(
            day_start=day_start,
            day_end=day_end,
            grid_times=grid_times,
            period_hours=period_hours,
            n_periods=n_periods,
        )
    )
    # nimbus issue #768, the OTHER half of the same ask: what each
    # Controllable Load really delivered over this window, per period, from
    # its own power sensor's recorder history. This is the capability that
    # issue named as its own prerequisite on 2026-09-13 and that nothing
    # provided until now.
    #
    # It is a MEASUREMENT here and nothing more. The oracle's own
    # `build_plan()` call below is deliberately NOT given `adequacy_loads=`
    # / `sheddable_loads=`, so every EPR, j_star and regret figure this
    # function returns is identical to before -- Mark Purcell's own
    # sequencing, 2026-09-27: "Build that first, land it as its own change,
    # then wire the LP plumbing ... as a second, smaller step." Publishing
    # the reconstruction first is what lets it be checked against a real
    # install BEFORE anything is scored against it, which is the order
    # #1242 used for soc_discrepancy_power_coverage.
    #
    # Zero controllable loads (the reference household, devhub, and any
    # standalone/cron deployment) returns [] and publishes an empty list.
    try:
        controllable_load_delivery = (
            sw.controllable_load_history.resolve_controllable_load_delivery_history(
                day_start=day_start,
                day_end=day_end,
                grid_times=grid_times,
                period_hours=period_hours,
                n_periods=n_periods,
            )
        )
    except Exception:  # noqa: BLE001 -- exc_info is logged; ruff cannot trace _LOGGER through this module's dual-mode solver_shared import (#1301)  # a diagnostic must never take the whole day's report down; same posture as every other optional reconstruction here
        solver_shared._LOGGER.debug(
            "Nimbus quality: controllable-load delivery reconstruction failed "
            "for [%s, %s] -- the rest of the report is unaffected",
            day_start.isoformat(),
            day_end.isoformat(),
            exc_info=True,
        )
        controllable_load_delivery = []
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
        import_limit_kw=sw._cfg_num(cfg, "solver_grid_max_import_kw", 20.0),
        export_limit_kw=sw._cfg_num(cfg, "solver_grid_max_export_kw", 20.0),
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
    fixed_export_kw = sw.fetch_p2p_fixed_export_kw(cfg, grid_times)

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
        solver_shared._LOGGER.warning(
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
        and day_start.astimezone(sw.LOCAL_TZ).time().hour == 0
        and day_start.astimezone(sw.LOCAL_TZ).time().minute == 0
    )
    if settlement_sensor and not is_calendar_day:
        real_p2p_settlement_status = "window_is_not_one_local_calendar_day"
    if settlement_sensor and is_calendar_day:
        settled_date = day_start.astimezone(sw.LOCAL_TZ).date()
        try:
            day_data = (sw.ha_get(settlement_sensor)["attributes"]["history"]).get(
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
                    export_bonus_price=sw.p2p_bonus_price_by_period(
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

    # nimbus issue #1357, step 2: hand the reconstruction above to the oracle
    # as re-timeable load, behind a switch that is OFF by default.
    #
    # `None` keeps compute_quality_report() on its pre-#1357 path exactly, so
    # an install that has not turned the switch on -- and every install with no
    # Controllable Loads, where this is structurally inert -- scores byte-for-
    # byte as before. That is the property `test_768_delivery_is_published_
    # without_moving_the_score.py` pins, and it stays true with the switch off.
    #
    # Off by default is the design rather than caution: this MOVES published
    # EPR, and #768's own thread records that the direction is not determinable
    # from the code (an AdequacyLoadConfig adds the freedom to re-time AND the
    # obligation to deliver, which push opposite ways). A household has to be
    # able to run the same days both ways and compare, which a release that
    # simply changed the number would make impossible.
    oracle_controllable_loads = None
    if controllable_load_delivery and bool(
        cfg.get("solver_score_controllable_loads_enabled", False)
    ):
        try:
            oracle_controllable_loads = (
                sw.controllable_load_history.build_oracle_controllable_loads(
                    deliveries=controllable_load_delivery,
                    grid_times=grid_times,
                    period_hours=period_hours,
                    n_periods=n_periods,
                    window_start=day_start,
                )
            )
        except Exception:  # noqa: BLE001 -- exc_info is logged; same posture as the reconstruction above, and a scoring EXTRA must never take the day's whole report down
            solver_shared._LOGGER.warning(
                "Nimbus quality: building the oracle's controllable-load inputs "
                "failed for [%s, %s] -- scoring this window WITHOUT them, which "
                "is the pre-#1357 behaviour rather than a wrong number",
                day_start.isoformat(),
                day_end.isoformat(),
                exc_info=True,
            )
            oracle_controllable_loads = None

    try:
        report = sw.compute_quality_report(
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
            controllable_loads=oracle_controllable_loads,
        )
    except RuntimeError as e:
        # Oracle solve genuinely infeasible for this day's real data --
        # skip, same "retry next cycle" convention as every other
        # genuine failure mode here, never a crash. issue #314 (Mark
        # Purcell): this exact path was diagnosed once, by hand, on
        # 2026-08-30 as initial_soc_kwh < min_soc_kwh after a reload --
        # logging the same three values here makes that diagnosis a
        # one-line log read instead of a repeat investigation.
        solver_shared._LOGGER.warning(
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
    # sw.compute_quality_report()'s own comment at the compute_epr() call
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
    soc_discrepancy_max_threshold_pct = sw._cfg_num(
        cfg, "solver_soc_discrepancy_max_threshold_pct", 15.0
    )
    soc_discrepancy_mean_threshold_pct = sw._cfg_num(
        cfg, "solver_soc_discrepancy_mean_threshold_pct", 8.0
    )
    soc_discrepancy = sw._soc_discrepancy_stats(
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
    achieved_feasibility = sw._achieved_feasibility_stats(
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
        achieved_feasibility["epr_reason"] = sw._epr_soc_reason(
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
    nowcast_skill_attrs = sw._load_nowcast_skill_attributes(
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
    # nimbus issue #937: the DAY-AHEAD decomposition, beside #919's
    # one-step-ahead nowcast skill above. That issue is explicit the two are
    # not comparable, so they are separate key sets rather than one.
    #
    # Same fast path protects it: two extra LP solves, paid once per day
    # behind this function's own latest_date check, and only when a snapshot
    # for the scored day actually exists.
    forecast_regret_attrs = sw._day_ahead_forecast_regret_attributes(
        solar_sensor=solar_sensor,
        solar_scale=solar_scale,
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
        **forecast_regret_attrs,
        # nimbus issue #1496: which shape of report this is. The "already
        # scored" fast path re-pushes a day's published attributes verbatim, so
        # without this a day first scored by an older release never gains the
        # fields a newer one adds. See QUALITY_REPORT_SCHEMA.
        "report_schema": QUALITY_REPORT_SCHEMA,
        # Fractional EPR (0..1). Canonical downstream contract: the OpEd
        # hero chart, the sw.compute_quality_report service payload, and the
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
        # nimbus issue #768, the controllable-load half. One entry per
        # CONFIGURED Controllable Load, scorable or not -- a household must
        # be able to see the loads this day's reconstruction could not read,
        # which is the same distinction `scored_participants` above makes on
        # the battery side.
        #
        # `delivered_kwh` is the REAL delivered energy (Mark's own decided
        # convention, 2026-09-27), never the configured target. `null` with a
        # `reason` means not reconstructable for this day -- honest absence,
        # not 0.0, the same posture `offered_up_kwh` already takes.
        #
        # Whether this is CONSUMED for scoring now depends on the #1357
        # switch, which is off by default: with it off the oracle call above
        # is unchanged and this cannot move EPR or regret, exactly as when it
        # was measurement-only. The three fields below say which it was.
        "controllable_load_delivery": [
            d.as_attribute() for d in controllable_load_delivery
        ],
        # nimbus issue #1357. Read these three together -- they are what
        # makes a partially-scored day unable to read as a wholly-scored one.
        #
        # `scored` empty on an install WITH configured Controllable Loads
        # means the switch is off (or every load was skipped), not that there
        # are none: `controllable_load_delivery` above is the list of what
        # exists, and this is the subset the oracle was allowed to re-time.
        "oracle_controllable_loads_scored": list(
            report.oracle_controllable_loads_scored
        ),
        # (name, reason) per configured load left out -- no power sensor, a
        # day before that sensor existed, or kind=thermal per #768.
        "oracle_controllable_loads_skipped": [
            {"name": n, "reason": r}
            for n, r in report.oracle_controllable_loads_skipped
        ],
        # kWh the reconstruction claimed that could NOT be removed from the
        # oracle's base load without driving it negative. Nonzero means the
        # two meters disagree (#1231 records a 5-8% offset on the reference
        # install), and the oracle was therefore asked to serve MORE than the
        # real house did -- which DEFLATES this day's EPR through a
        # measurement artifact rather than through dispatch quality. A reason
        # to distrust the number, not to adjust it; same posture as
        # `soc_discrepancy_power_coverage`.
        "oracle_controllable_loads_unsubtracted_kwh": (
            report.oracle_controllable_loads_unsubtracted_kwh
        ),
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
        "j_star_path_delta_explained": report.j_star_path_delta_explained,
        "j_star_path_delta_unexplained": report.j_star_path_delta_unexplained,
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
        "regret_path_delta_share": sw._regret_path_delta_share(
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
        # cost dict sw.compute_quality_report already built via hourly_
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
        # handed to sw.compute_quality_report(), not the whole fleet. That
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
            sw.battery_energy_balance(
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
        "epr_reliable": sw._epr_reliability(
            soc_discrepancy["soc_discrepancy_reliable"],
            achieved_feasibility["regret_reliable"],
            epr_denominator_reason,
        ),
        **achieved_feasibility,
    }


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
    sw = _solver_writer()
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
            sw.resample_history_nearest(hist, [hour_dt], backfill_first=True)[0]
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
            near_zero = (
                ach_pct < 0.0 and real_pct <= sw._SOC_BOUNDARY_EDGE_TOLERANCE_PCT
            )
            near_hundred = (
                ach_pct > 100.0
                and real_pct >= 100.0 - sw._SOC_BOUNDARY_EDGE_TOLERANCE_PCT
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


def _carry_forward_quality_history(
    prior_attrs: dict,
    day_key: str,
    day_entry: dict,
    *,
    freshly_computed: bool = True,
    prior_read: str = PRIOR_READ_OK,
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
    sw = _solver_writer()
    # nimbus issue #1248: declared up here because Python requires the
    # declaration to precede the first READ of the name, and the degraded-read
    # check below reads it.
    # nimbus issue #1301 Phase 2c -- the ONE genuinely semantic edit in this
    # module, re-applied after regenerating this function from origin/main to
    # pick up #937's own changes (that regeneration reinstated main's
    # `global` statement, which is why this appears twice in the branch's
    # history rather than once).
    #
    # A `global` statement binds in the namespace of the module the function
    # is DEFINED in, so relocating the function relocates the variable it
    # writes -- the cache would live here while every reader
    # (solver_writer.set_native_hass(), tests/golden/harness.py,
    # test_1248_history_never_shrinks.py) still looks at solver_writer's.
    #
    # A facade alias cannot repair it: an alias captures the current OBJECT
    # and this name is REBOUND, so the alias would keep pointing at the
    # previous dict forever. The variable therefore stays in solver_writer --
    # it has a real rebinding caller there -- and this function reaches it as
    # a module attribute. `sw.X = ...` rebinds solver_writer's own global,
    # which is exactly what `global X; X = ...` did before the move.

    prior = prior_attrs.get("history")
    # nimbus issue #1248: a read that came back with no `history` is
    # ambiguous on its own -- it is either a genuine first-ever publish or a
    # degraded read. The caller's own comment treated them as the same thing
    # ("a first-ever publish, or an unreachable read"), which is what made the
    # loss permanent.
    #
    # `prior_read` is the caller's POSITIVE classification, not a guess from an
    # empty prior: `ha_get()` distinguishes the cases exactly, and this
    # function used to have no way to tell them apart. Only the two degraded
    # statuses recover, so a genuine first publish (404) never consults this
    # cache -- which is what keeps the behaviour independent of whatever this
    # process happened to publish earlier.
    if (
        prior_read in _PRIOR_READ_DEGRADED
        and sw._quality_history_cache_active()
        and (not isinstance(prior, dict) or not prior)
    ):
        if sw._LAST_KNOWN_QUALITY_HISTORY:
            prior = dict(sw._LAST_KNOWN_QUALITY_HISTORY)
            # Only claim a rescue if something was actually rescued. A cache
            # holding nothing but the day being written right now recovers
            # nothing -- this function would produce that single row anyway --
            # and warning about it would fire on every cycle of a normal
            # single-day install. Measured against
            # test_quality_report_achieved_energy_and_reliability.py's
            # TestPublishLogsOncePerScoredDay, which publishes the same day
            # twice through an unreachable read and asserts the second is
            # silent: an unconditional warning here broke it, correctly.
            rescued = sorted(k for k in prior if k != day_key)
            if rescued:
                solver_shared._LOGGER.warning(
                    "Nimbus #1248: the quality report read back %s and with no "
                    "history, so recovering the %d earlier row(s) this process "
                    "last published (%s) rather than writing a one-row table "
                    "the next cycle would inherit. HA itself no longer has "
                    "those rows, so this cache is the only place they still "
                    "exist. Signature of a recorder/executor stall -- see "
                    "#1217's 06:00 window.",
                    prior_read,
                    len(rescued),
                    ", ".join(rescued),
                )
        elif prior_read == PRIOR_READ_UNAVAILABLE:
            # The read SUCCEEDED, so the entity exists -- which means it has
            # published before and very likely had rows. Nothing to recover
            # from, so this publish genuinely truncates, and that is worth
            # saying out loud.
            solver_shared._LOGGER.warning(
                "Nimbus #1248: the quality report exists but read back "
                "`unavailable`, so its history could not be read, and this "
                "process has published none yet to recover from. Publishing "
                "today's row alone -- any older rows are not retrievable from "
                "here. Rebuild them with the rescore_history service."
            )
        else:
            # Unreachable HA says nothing about whether rows existed, and on
            # the standalone/cron path it is a routine transient. INFO, not
            # WARNING: warning every cycle while HA is down would be noise,
            # and two existing tests assert this path stays quiet.
            solver_shared._LOGGER.info(
                "Nimbus #1248: could not reach HA to read the quality "
                "report's history, and this process has published none yet. "
                "Publishing today's row alone."
            )
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
        for field in sw._QUALITY_HISTORY_FIELDS
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
        version = sw._nimbus_version()
        if version is not None:
            history[day_key][sw._QUALITY_HISTORY_VERSION_FIELD] = version
    elif sw._QUALITY_HISTORY_VERSION_FIELD in prior_row:
        history[day_key][sw._QUALITY_HISTORY_VERSION_FIELD] = prior_row[
            sw._QUALITY_HISTORY_VERSION_FIELD
        ]
    # nimbus issue #1162 ask 2: and the verdict that qualifies those five
    # numbers, so a card can caveat any row rather than only the latest.
    code = sw._epr_reliability_code(day_entry)
    if code is not None:
        history[day_key][sw._QUALITY_HISTORY_RELIABILITY_FIELD] = code
    # nimbus issue #1200: and whether those five numbers are still
    # waiting on this day's real P2P settlement, so the repair sweep can
    # still find this row tomorrow rather than only within the hour.
    # Written only when provisional -- see the field's own note on why
    # that convention is the opposite of `"r"` above.
    if sw._settlement_is_provisional(day_entry) is True:
        history[day_key][sw._QUALITY_HISTORY_PROVISIONAL_FIELD] = 1
    # nimbus issue #937: and what the DAY-AHEAD forecast was worth on this day,
    # so the issue's own "more days" is a series rather than one overwritten
    # headline. See the field's own note above for the byte measurement and for
    # why only the value-add is retained, not the whole decomposition.
    #
    # On a seed/re-push (`freshly_computed` False) `day_entry` is the previously
    # published attribute set, which carries `forecast_regret_reason` and the
    # value verbatim -- so this re-derives the same number rather than dropping
    # the key, matching how `"r"` behaves on that path and unlike `"v"`, which
    # must NOT advance there (#1219).
    value_add = sw._day_ahead_value_add_for_history(day_entry)
    if value_add is not None:
        history[day_key][sw._QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD] = value_add
    elif sw._QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD in prior_row:
        # A day scored once WITH a snapshot and re-pushed later on a cycle that
        # could not recompute it must not silently lose its verdict -- the row
        # is the only place it survives at all.
        history[day_key][sw._QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD] = prior_row[
            sw._QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD
        ]
    if len(history) > sw._QUALITY_HISTORY_MAX_DAYS:
        # ISO dates sort lexicographically, so this is a real
        # most-recent-N without parsing anything.
        for stale in sorted(history)[: len(history) - sw._QUALITY_HISTORY_MAX_DAYS]:
            del history[stale]
    # nimbus issue #1248: remember what is about to be published, so a
    # later degraded read can be recovered from rather than truncated.
    # Native-only for the reason `_quality_history_cache_active()` documents --
    # a cron invocation exits before anything could read this back.
    if sw._quality_history_cache_active():
        sw._LAST_KNOWN_QUALITY_HISTORY = dict(history)
    return history


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
    sw = _solver_writer()
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    if days > sw._RESCORE_MAX_DAYS:
        raise ValueError(
            f"days must be <= {sw._RESCORE_MAX_DAYS} (each day is a full oracle "
            f"MILP solve), got {days}"
        )

    existing = sw.ha_get(sw.QUALITY_ENTITY_ID)
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
        day_start = datetime(target.year, target.month, target.day, tzinfo=sw.LOCAL_TZ)
        day_end = day_start + timedelta(days=1)
        try:
            entry = sw._compute_report_for_window(
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
            solver_shared._LOGGER.warning(
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
        attrs["history"] = sw._carry_forward_quality_history(attrs, key, entry)
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
            #
            # nimbus issue #1292 (Mark Purcell, IV&V #1289): via
            # `_version_stamp()`, not `_nimbus_version()` directly. #1256
            # built that helper precisely because `_nimbus_version()` can
            # return None (a missing or malformed manifest.json), and an
            # explicit `"nimbus_version": None` is strictly worse than an
            # absent key -- the entity layer's own #972 fallback declines to
            # overwrite a key that is already PRESENT, so a published None
            # permanently suppresses the fallback that would have supplied
            # the real running version. This call site was simply missed by
            # that migration.
            attrs.update(sw._version_stamp())
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
        sw.ha_post_state(sw.QUALITY_ENTITY_ID, state, attrs)
        solver_shared._LOGGER.info(
            "Nimbus quality (#1120): rescored %d day(s) %s, skipped %d",
            len(rescored),
            ", ".join(r["date"] for r in rescored),
            len(skipped),
        )
    else:
        solver_shared._LOGGER.warning(
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


#: nimbus issue #1463: days already warned about being unscoreable while the
#: previous report is held on the sensor -- once per day, not once a minute.
_QUALITY_HOLD_WARNED: set[str] = set()

# nimbus issue #1496: the shape of the published quality report. BUMP THIS
# whenever a field is added to (or removed from) the dict
# `_compute_report_for_window()` returns -- tests/test_1496_* pins the dict's
# literal key set to this number, so forgetting fails CI rather than silently
# freezing old days at their old shape.
#
#   1  every report published before the stamp existed (absent = 1)
#   2  #1480's j_star_path_delta_explained / _unexplained, plus the stamp
#
# Why it is needed: the "already scored" fast path below re-pushes a day's
# published attributes verbatim on every later cycle. Measured on a real
# install (Mark Purcell, #1496): 30 Sep was scored before v0.94.433 reached it,
# and after the upgrade its sensor still read j_star_path_delta_explained=None
# while a fresh compute_quality_report for the identical window returned 1.2357.
QUALITY_REPORT_SCHEMA = 2

# When each day was last re-scored for an older schema, as time.monotonic().
# A failed re-score is RETRIED, but no more often than
# _SCHEMA_RESCORE_RETRY_SECONDS, so a recompute that keeps failing (and is then
# held by #1463) cannot turn into an LP solve on every ~17 s cycle.
#
# It used to be "once per process", and that failed on its first real deploy:
# the reference household restarted on v0.94.435 at 15:49 AEST 1 Oct, the
# re-score fired during startup at 15:49:57, the recorder returned 0 load and
# battery rows that early, the day was held -- and "once" meant it was never
# tried again until the next restart. A success needs no memo at all: the
# re-published report carries the current stamp, so the check passes.
_SCHEMA_RESCORE_RETRY_SECONDS = 30 * 60
_SCHEMA_RESCORE_TRIED: dict[str, float] = {}


def _published_schema_is_current(attrs: dict, day_key: str) -> bool:
    """False when the published report predates QUALITY_REPORT_SCHEMA and
    this day has not been re-scored in the last _SCHEMA_RESCORE_RETRY_SECONDS
    -- see QUALITY_REPORT_SCHEMA."""
    try:
        published = int(attrs.get("report_schema") or 1)
    except (TypeError, ValueError):
        published = 1
    if published >= QUALITY_REPORT_SCHEMA:
        return True
    now = time.monotonic()
    last = _SCHEMA_RESCORE_TRIED.get(day_key)
    if last is not None and now - last < _SCHEMA_RESCORE_RETRY_SECONDS:
        return True
    _SCHEMA_RESCORE_TRIED[day_key] = now
    return False


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
    sw = _solver_writer()
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    # nimbus issue #994: read once, used twice -- the fast path's own
    # idempotency check below, and the `history` carry-forward at publish
    # time. Defaults to empty so a first-ever publish, or an unreachable
    # read, still produces a valid one-entry table rather than crashing.
    #
    # nimbus issue #1248: that default is also, on its own, a data-loss bug,
    # and the sentence above says why without noticing -- "a first-ever
    # publish, OR an unreachable read" are treated as the same thing, and
    # they are not. An unreachable read is not an empty history; it is an
    # UNKNOWN history. Publishing a one-row table for it is not graceful
    # degradation, because `ha_post_state` replaces attributes wholesale and
    # the next cycle then carries forward from the truncated result -- a
    # one-way ratchet with nothing to restore from.
    #
    # Measured on production 2026-09-26: 10 rows to 1, no restart, the nine
    # lost days (16-24 Sep) unrecoverable. It happened at 06:06 inside a live
    # #1217 window where the recorder was returning 0 rows and every
    # flattened quality child read `unavailable` for ~2 minutes.
    #
    # Deliberately NOT fixed by skipping the publish on a failed read: a
    # genuine first-ever publish on a fresh install ALSO fails this read
    # (404 on an entity that does not exist yet), so skipping would mean a
    # new install could never write its first row.
    #
    # They are distinguishable, though -- `ha_get()` gives three outcomes and
    # this function was discarding the difference between them:
    #
    #   * HTTPError 404              -> the entity does not exist -> a GENUINE
    #                                   first publish. Write the one row.
    #   * a SUCCESSFUL read whose
    #     state is unavailable/unknown -> the entity exists but HA has replaced
    #                                   its attributes with the unavailable
    #                                   minimum -> DEGRADED. This is the
    #                                   measured case: the quality report is
    #                                   kept non-stale by the per-cycle
    #                                   fast-path re-push below (#289/#292),
    #                                   ten consecutive skipped cycles stopped
    #                                   that, `available` went False, and the
    #                                   pushed attributes went with it.
    #   * URLError / non-404 HTTPError -> HA or the network is unreachable ->
    #                                   DEGRADED.
    #
    # `prior_read` carries that verdict to
    # _carry_forward_quality_history(), which is the only place the dropped
    # rows still exist to be recovered from. An explicit status rather than a
    # boolean, because `unavailable` (the entity exists, so rows probably did
    # too) and `unreachable` (knows nothing, routine on the cron path) warrant
    # different things being said when there is nothing to recover.
    existing_attrs: dict = {}
    existing: dict | None = None
    prior_read = PRIOR_READ_OK
    try:
        # resolve_real_entity_id() (2026-08-31): read back THIS entity's
        # own real state, not whatever the literal QUALITY_ENTITY_ID
        # string happens to resolve to if something else (e.g. a
        # remote_homeassistant mirror of another install) has claimed it.
        # See that function's own docstring for the full incident.
        existing = sw.ha_get(sw.resolve_real_entity_id(sw.QUALITY_ENTITY_ID))
        existing_attrs = existing.get("attributes", {}) or {}
        # nimbus issue #1248: the read SUCCEEDED, so the entity exists -- but
        # an `unavailable`/`unknown` state means HA has dropped the pushed
        # attributes, so `existing_attrs` describes nothing. Not a first
        # publish, and not a reason to truncate.
        if str(existing.get("state")) in ("unavailable", "unknown"):
            prior_read = PRIOR_READ_UNAVAILABLE
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
            if not sw._keep_published_quality_score(existing_attrs, now):
                solver_shared._LOGGER.info(
                    "Nimbus quality: %s was scored before its settlement "
                    "was available (%s) -- re-scoring it now that the "
                    "real figures may have landed",
                    yesterday_key,
                    existing_attrs.get("real_p2p_settlement_status"),
                )
            elif not _published_schema_is_current(existing_attrs, yesterday_key):
                # nimbus issue #1496: scored by an older release, so it lacks
                # fields this one publishes. Re-score once (per process) so the
                # day carries the current report shape.
                solver_shared._LOGGER.info(
                    "Nimbus quality: %s was published with report schema %s; "
                    "this release publishes schema %s -- re-scoring it once so "
                    "it carries the current fields",
                    yesterday_key,
                    existing_attrs.get("report_schema", 1),
                    QUALITY_REPORT_SCHEMA,
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
                existing["attributes"]["history"] = sw._carry_forward_quality_history(
                    existing_attrs,
                    yesterday_key,
                    existing_attrs,
                    # nimbus issue #1219: this path recomputes nothing --
                    # `day_entry` here IS the previously published
                    # attributes -- so it must not restamp the row with
                    # the running release.
                    freshly_computed=False,
                )
                solver_shared._LOGGER.debug(
                    "Nimbus quality: fast-path hit, already scored %s -- re-"
                    "pushing cached state to keep the freshness stamp alive",
                    yesterday_key,
                )
                sw.ha_post_state(
                    sw.QUALITY_ENTITY_ID, existing["state"], existing["attributes"]
                )
                return
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        json.JSONDecodeError,
    ) as e:
        # nimbus issue #1248: a 404 is the ONE outcome here that genuinely
        # means "no history exists yet" -- the entity does not exist, so a
        # fresh install must be allowed to write its first row. Every other
        # failure means the history could not be READ, which is a different
        # thing entirely and must not be published as emptiness. HTTPError is
        # a subclass of URLError, so it is caught first on purpose.
        prior_read = (
            PRIOR_READ_ABSENT
            if isinstance(e, urllib.error.HTTPError) and e.code == 404
            else PRIOR_READ_UNREACHABLE
        )
        # never seen before, or transiently unreachable -- fall through and try to compute
    day_entry = sw.compute_daily_quality_report(cfg, now)
    if day_entry is None:
        # nimbus issue #1463: keep the LAST GOOD report alive instead of
        # publishing nothing. This branch used to just return, and this
        # sensor goes `unavailable` after _STALE_AFTER_SECONDS (5 min)
        # without a push -- so from local midnight, when the fast path stops
        # matching and the new day cannot be scored yet, the scorecard
        # vanished for as long as scoring kept failing (6+ hours on a real
        # install). Worse than cosmetic: an `unavailable` read is exactly
        # what arms #1248's history-truncation ratchet on the next publish.
        #
        # Re-pushed verbatim, so `latest_date` still names the day the score
        # belongs to -- nothing claims to be a score for a day it is not.
        # Only when the previous report was genuinely read and is a real
        # score (not unknown/unavailable, not an unreachable read).
        if (
            existing is not None
            and prior_read == PRIOR_READ_OK
            and existing_attrs.get("latest_date")
        ):
            if yesterday_key not in _QUALITY_HOLD_WARNED:
                _QUALITY_HOLD_WARNED.add(yesterday_key)
                solver_shared._LOGGER.warning(
                    "Nimbus quality: %s could not be scored yet (reason "
                    "logged just above, if any) -- holding the %s report on "
                    "the sensor rather than letting it go unavailable. Will "
                    "keep retrying every cycle; logged once per day (nimbus "
                    "issue #1463)",
                    yesterday_key,
                    existing_attrs.get("latest_date"),
                )
            sw.ha_post_state(
                sw.QUALITY_ENTITY_ID, existing["state"], existing["attributes"]
            )
        # issue #313: compute_daily_quality_report()/_compute_report_for_
        # window() already logs the SPECIFIC reason for a None return
        # (missing config, missing history, infeasible oracle) at its own
        # call site -- this one line is what ties that reason back to
        # "and therefore the sensor was not updated this cycle," so a log
        # search for this entity's own name always surfaces the full story.
        solver_shared._LOGGER.debug(
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
        and yesterday_key not in sw._QUALITY_REPORT_NEGATIVE_REGRET_WARNED
    ):
        sw._QUALITY_REPORT_NEGATIVE_REGRET_WARNED.add(yesterday_key)
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
        solver_shared._LOGGER.warning(
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
        and yesterday_key not in sw._QUALITY_REPORT_EPR_DENOMINATOR_WARNED
    ):
        sw._QUALITY_REPORT_EPR_DENOMINATOR_WARNED.add(yesterday_key)
        solver_shared._LOGGER.warning(
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
        and yesterday_key not in sw._QUALITY_REPORT_UNRELIABLE_WARNED
    ):
        sw._QUALITY_REPORT_UNRELIABLE_WARNED.add(yesterday_key)
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
        solver_shared._LOGGER.warning(
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
    sw.ha_post_state(
        sw.QUALITY_ENTITY_ID,
        # State channel gets the percent-scaled value (0..100) so it
        # renders correctly against unit_of_measurement="%" below.
        # The canonical 0..1 fraction stays available as the "epr"
        # attribute via the **day_entry expansion for consumers that
        # want the raw ratio (sw.compute_quality_report service payload,
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
            **sw._version_stamp(),
            # nimbus issue #994: placed BEFORE **day_entry, so a future
            # day_entry key named "history" would win rather than being
            # silently shadowed -- the same ordering rule the keys above
            # already rely on.
            "history": sw._carry_forward_quality_history(
                existing_attrs,
                yesterday_key,
                day_entry,
                # nimbus issue #1248: see the `existing_attrs` note above --
                # this is the difference between "no history yet" and "the
                # history could not be read".
                prior_read=prior_read,
            ),
            **day_entry,
        },
    )
