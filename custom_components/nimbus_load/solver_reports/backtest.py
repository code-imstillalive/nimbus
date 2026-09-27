"""`compute_efficiency_backtest_report()` -- the round-trip-efficiency
backtest, moved verbatim out of `solver_writer.py`.

nimbus issue #1301, Phase 2b of #1298's decomposition. A pure relocation:
not one line of the computation changed. The only edit is that names which
used to resolve in `solver_writer`'s own module globals are now reached as
`sw.<name>`, which resolves the identical object at call time -- verified
mechanically, by stripping every inserted `sw.` and confirming the result
is byte-for-byte the original 242 lines.

This function was the lowest-risk target in Phase 2: the dependency
analysis (`tests/analyse_module_dependencies.py --phase 2`) reports **zero
module-level blockers** for it, the only one of the eight reporting
functions with none. It reads recorder history, sweeps candidate
efficiencies through the pure `solver/` math, and returns a dict -- no
publish, no dispatch, no module state.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

try:
    from ..solver import elements
except ImportError:  # pragma: no cover - standalone/cron path
    from solver import elements  # type: ignore[no-redef]


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see this package's `__init__.py` for the full reasoning. In
    short: a module-scope `from ..solver_writer import x` would both be
    circular and bind `x` at import time, silently defeating every
    `patch.object(solver_writer, "x", ...)` in the suite."""
    try:
        from .. import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer

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
    sw = _solver_writer()
    solar_sensor = cfg.get("solver_solar_power_sensor")
    battery_sensor = cfg.get("solver_battery_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    if not solar_sensor or not battery_sensor or not load_sensor:
        return None

    yesterday = (now - timedelta(days=1)).date()
    day_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=sw.LOCAL_TZ
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
        sw.TIER1_PERIOD_HOURS if window_hours <= sw.MAX_TIER1_HOURS else sw.TIER2_PERIOD_HOURS
    )
    n_periods = round(window_hours / period_hours)
    grid_times = [
        day_start + timedelta(hours=i * period_hours) for i in range(n_periods)
    ]
    period_hours_arr = np.full(n_periods, period_hours)

    solar_hist = sw.fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = sw.fetch_entity_history_range(load_sensor, day_start, day_end)
    if not solar_hist or not load_hist:
        return None

    import_price_hist = sw.fetch_entity_history_range(
        cfg["solver_import_price_sensor"], day_start, day_end
    )
    export_price_hist = sw.fetch_entity_history_range(
        cfg["solver_export_price_sensor"], day_start, day_end
    )

    # Same fix as compute_daily_quality_report() -- see _kw_scale_
    # factor()'s own docstring for the real, confirmed-live bug this
    # corrects (a configured *_power_sensor reporting native Watts,
    # silently treated as kW).
    solar_scale = sw._kw_scale_factor(solar_sensor)
    load_scale = sw._kw_scale_factor(load_sensor)

    solar_kw = np.array(
        [
            max(0.0, v * solar_scale)
            for v in sw.resample_history_nearest(solar_hist, grid_times)
        ]
    )
    load_kw = np.array(
        [
            max(0.0, v * load_scale)
            for v in sw.resample_history_nearest(load_hist, grid_times)
        ]
    )
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
        max_charge_kw=sw._cfg_num(cfg, "solver_max_charge_kw", 5.0),
        max_discharge_kw=sw._cfg_num(cfg, "solver_max_discharge_kw", 5.0),
        # Overwritten per-candidate by run_efficiency_sensitivity_sweep()
        # -- these two values are never actually read, kept only because
        # BatteryConfig requires something valid at construction time.
        charge_efficiency=0.90,
        discharge_efficiency=0.90,
        charge_cost=sw._cfg_num(cfg, "solver_charge_cost", 0.01),
        discharge_cost=np.full(n_periods, sw._cfg_num(cfg, "solver_discharge_cost", 0.01)),
        salvage_value=sw._cfg_num(cfg, "solver_salvage_value", 0.15),
    )
    grid_cfg = elements.GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=sw._cfg_num(cfg, "solver_grid_max_import_kw", 20.0),
        export_limit_kw=sw._cfg_num(cfg, "solver_grid_max_export_kw", 20.0),
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
    configured_pct = sw._cfg_num(cfg, "solver_efficiency_percent", 90.0)
    swept = tuple(sorted({*sw.EFFICIENCY_CANDIDATES_PERCENT, round(configured_pct, 2)}))
    results = sw.run_efficiency_sensitivity_sweep(
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
    configured_label = sw.efficiency_label(configured_pct)
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
