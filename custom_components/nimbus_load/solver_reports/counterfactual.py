"""`compute_nimbus_only_soc_counterfactual()` -- the "if Nimbus alone had
been deciding since midnight" tracker, moved verbatim out of
`solver_writer.py`.

nimbus issue #1301, Phase 2b of #1298's decomposition. A pure relocation,
verified mechanically: strip every inserted `sw.` and the result is
byte-for-byte the original 341 lines.

Its three function-valued dependencies -- `resolve_max_discharge_kw()`,
`resolve_min_soc_kwh()` and `terminal_value_breakpoints_for()` -- stay in
`solver_writer` and are reached via `sw.`, because each has other real
callers there (9, 5 and 5 references respectively, and `resolve_min_soc_kwh`
is also used by `solver_inputs/battery_soc.py`). Moving them would be a
different phase's decision, not this one's.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

try:
    from ..solver import elements, lp, network
except ImportError:  # pragma: no cover - standalone/cron path
    from solver import elements, lp, network  # type: ignore[no-redef]


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
    sw = _solver_writer()
    solar_sensor = cfg.get("solver_solar_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    soc_sensor = cfg.get("solver_battery_soc_sensor")
    if not solar_sensor or not load_sensor or not soc_sensor:
        return None

    # nimbus issue #1013: SoH-derated, same as every other BatteryConfig
    # construction -- see resolve_effective_capacity_kwh()'s docstring.
    capacity_kwh = sw.resolve_effective_capacity_kwh(cfg)
    if capacity_kwh <= 0:
        return None

    day_start = datetime(day.year, day.month, day.day, tzinfo=sw.LOCAL_TZ)
    day_end = day_start + timedelta(days=1)
    step = timedelta(minutes=15)

    solar_hist = sw.fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = sw.fetch_entity_history_range(load_sensor, day_start, day_end)
    soc_hist = sw.fetch_entity_history_range(
        soc_sensor, day_start - timedelta(hours=6), day_end
    )
    if not solar_hist or not load_hist or not soc_hist:
        return None

    import_price_hist = sw.fetch_entity_history_range(
        cfg["solver_import_price_sensor"], day_start, day_end
    )
    export_price_hist = sw.fetch_entity_history_range(
        cfg["solver_export_price_sensor"], day_start, day_end
    )

    min_pct = sw._cfg_num(cfg, "solver_battery_min_soc_percent", 5.0)
    max_pct = sw._cfg_num(cfg, "solver_battery_max_soc_percent", 100.0)
    max_soc_kwh = capacity_kwh * max_pct / 100.0
    min_soc_kwh = sw.resolve_min_soc_kwh(min_pct, capacity_kwh, max_soc_kwh)
    max_charge_kw = sw._cfg_num(cfg, "solver_max_charge_kw", 5.0)
    max_discharge_kw = sw.resolve_max_discharge_kw(cfg)
    # solver_efficiency_percent is a single ROUND-TRIP figure, split
    # geometrically via sqrt() into the per-direction value the LP (and
    # this replay's own post-solve SoC bookkeeping) actually needs --
    # same convention as main()'s own real BatteryConfig and issue #168's
    # own fix in compute_daily_quality_report(). Using the round-trip
    # value directly here would model a battery physically different
    # from the one the real live plan solves against.
    efficiency = (sw._cfg_num(cfg, "solver_efficiency_percent", 90.0) / 100.0) ** 0.5
    charge_cost = sw._cfg_num(cfg, "solver_charge_cost", 0.01)
    discharge_cost_flat = sw._cfg_num(cfg, "solver_discharge_cost", 0.01)
    salvage_value_flat = sw._cfg_num(cfg, "solver_salvage_value", 0.15)
    import_limit_kw = sw._cfg_num(cfg, "solver_grid_max_import_kw", 20.0)
    export_limit_kw = sw._cfg_num(cfg, "solver_grid_max_export_kw", 20.0)
    flat_fee_rate = sw._cfg_num(cfg, "solver_flat_fee_rate", 0.0)

    # Fully optional -- see this function's own docstring. A household
    # with no P2P/community-trading scheme configured gets
    # p2p_bonus_volume_cap=0.0 (bonus price is then a genuine no-op) and
    # checkpoint_hour=-1 (no window-open moment to check), while export
    # still gets fully LP-optimized against real spot prices exactly as
    # the live writer's own generic fallback path already does.
    p2p_bonus_price_flat = sw._cfg_num(cfg, "solver_p2p_bonus_price", 0.0)
    p2p_bonus_volume_cap = sw._cfg_num(cfg, "solver_p2p_bonus_volume_kwh", 0.0)
    p2p_block_1_rate = sw._cfg_num(cfg, "solver_p2p_block_1_rate_kw", 0.0)
    checkpoint_hour = (
        sw._cfg_int(cfg, "solver_p2p_block_1_start_hour", -1)
        if p2p_block_1_rate > 0
        else -1
    )
    viable_threshold_pct = (
        min(100.0, (p2p_bonus_volume_cap / capacity_kwh * 100.0) * 1.1)
        if p2p_bonus_volume_cap > 0
        else None
    )

    initial_pct = sw.resample_history_nearest(
        soc_hist, [day_start], default=50.0, backfill_first=True
    )[0]
    real_soc_close_pct = sw.resample_history_nearest(
        soc_hist,
        [day_end - timedelta(seconds=1)],
        default=initial_pct,
        backfill_first=True,
    )[0]
    real_soc_checkpoint_pct = (
        sw.resample_history_nearest(
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
            [max(0.0, v) for v in sw.resample_history_nearest(solar_hist, grid_times)]
        )
        load_kw = np.array(
            [max(0.1, v) for v in sw.resample_history_nearest(load_hist, grid_times)]
        )
        import_price = np.array(
            [
                v + sw.import_fee_rate(cfg, sw._local(gt).hour) + flat_fee_rate
                for v, gt in zip(
                    sw.resample_history_nearest(
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
            sw.resample_history_nearest(
                export_price_hist, grid_times, default=0.05, backfill_first=True
            )
        )

        fixed_export_kw = sw.fetch_p2p_fixed_export_kw(cfg, grid_times)
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
        in_p2p_window = checkpoint_hour >= 0 and sw._local(t).hour >= checkpoint_hour
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
            export_bonus_price=sw.p2p_bonus_price_by_period(
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
            terminal_value_breakpoints=sw.terminal_value_breakpoints_for(
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
                smoothness_weight=sw._cfg_num(
                    cfg,
                    "solver_intraplan_smoothness_weight_kw",
                    network.DEFAULT_SMOOTHNESS_WEIGHT_KW,
                ),
                # nimbus issue #692: same reasoning as smoothness_weight
                # just above -- reads the SAME live-tunable value the real
                # dispatch solve uses, not a second, silently-divergent
                # hardcoded copy.
                battery_charge_earliness_budget_kw=sw._cfg_num(
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
        except Exception:  # exc_info is logged below. The `blind-except suppression it
        # carried in solver_writer.py is deliberately NOT carried across:
        # ruff's `logger-objects` setting names solver_shared._LOGGER, and
        # reaching it as `sw._LOGGER` here stops BLE001 firing at all, so the
        # directive becomes RUF100 'unused noqa'. Dropping it is the only
        # non-verbatim edit in this module (nimbus issue #1301, Phase 2b).
            # nimbus issue #363 (Mark Purcell, codebase review): the
            # freeze-and-continue behaviour stays, breadcrumb added.
            sw._LOGGER.debug(
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
            and sw._local(t).hour == checkpoint_hour
            and sw._local(t).minute < step.total_seconds() / 60.0
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
