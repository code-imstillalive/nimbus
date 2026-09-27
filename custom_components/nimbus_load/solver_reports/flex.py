"""`_compute_flex_report_for_window()` -- the daily flex-signals report,
moved verbatim out of `solver_writer.py`.

nimbus issue #1301, Phase 2b of #1298's decomposition. A pure relocation,
verified mechanically: strip every inserted `sw.` and the result is
byte-for-byte the original 152 lines.

`FLEX_SIGNALS_ENTITY_ID` and `_PRICE_BAND_WIDTH` travel with the function
rather than staying behind, because this was the function's only caller --
checked per constant with grep before moving, not assumed, and neither is
patched by any test. Both keep a re-export alias in `solver_writer` for the
three test files that read `FLEX_SIGNALS_ENTITY_ID` from there.

A note on cost, since it governs whether this report runs at all: flex
signals make the solver request HiGHS ranging, which was measured on the
reference household at **~9x solve time** (median 1.16 s -> 10.3 s). The
report is therefore gated behind the flex-signals switch and off by
default. Moving it here changes none of that.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

# nimbus issue #489/#495: the flex-signals sensor these windows are read
# back from, and the price-band width the report buckets by. Both moved
# here with their single caller -- see the module docstring.
FLEX_SIGNALS_ENTITY_ID = "sensor.nimbus_flex_signals"
_PRICE_BAND_WIDTH = (
    0.05  # $/kWh -- matches this file's own price-rounding convention elsewhere
)


try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]


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


def _compute_flex_report_for_window(
    cfg: dict, day_start: datetime, day_end: datetime
) -> dict | None:
    """Real body behind compute_daily_flex_report() -- see that
    function's own docstring for the full reasoning behind each
    component. Split out the same way _compute_report_for_window() is
    split from compute_daily_quality_report(), for the same reason
    (keeps the door open for a future arbitrary-window service call,
    same shape as nimbus_load.compute_quality_report, without
    duplicating this body -- not added in this pass since #496's own
    text never asked for one, only flagging the seam exists)."""
    sw = _solver_writer()
    if day_end <= day_start:
        return None
    solar_sensor = cfg.get("solver_solar_power_sensor")
    battery_sensor = cfg.get("solver_battery_power_sensor")
    load_sensor = cfg.get("solver_whole_house_cross_check_sensor")
    if not solar_sensor or not battery_sensor or not load_sensor:
        solver_shared._LOGGER.debug(
            "Nimbus flex report: skip. Missing sensor config (solar=%s "
            "battery=%s load=%s) -- same three sensors compute_daily_"
            "quality_report() requires, under Solver settings",
            solar_sensor,
            battery_sensor,
            load_sensor,
        )
        return None
    window_hours = (day_end - day_start).total_seconds() / 3600.0
    if window_hours < 24.0:
        solver_shared._LOGGER.debug(
            "Nimbus flex report: skip. Window is %.2f h, shorter than the "
            "24 h a full-day report requires",
            window_hours,
        )
        return None
    period_hours = sw.TIER2_PERIOD_HOURS
    n_periods = round(window_hours / period_hours)
    if n_periods < 1:
        return None
    grid_times = [
        day_start + timedelta(hours=i * period_hours) for i in range(n_periods)
    ]

    solar_hist = sw.fetch_entity_history_range(solar_sensor, day_start, day_end)
    load_hist = sw.fetch_entity_history_range(load_sensor, day_start, day_end)
    battery_hist = sw.fetch_entity_history_range(battery_sensor, day_start, day_end)
    if not solar_hist or not load_hist or not battery_hist:
        solver_shared._LOGGER.info(
            "Nimbus flex report: skip. Real history missing for window "
            "[%s, %s] (solar=%d, load=%d, battery=%d rows)",
            day_start.isoformat(),
            day_end.isoformat(),
            len(solar_hist),
            len(load_hist),
            len(battery_hist),
        )
        return None
    import_price_hist = sw.fetch_entity_history_range(
        cfg["solver_import_price_sensor"], day_start, day_end
    )

    solar_scale = sw._kw_scale_factor(solar_sensor)
    load_scale = sw._kw_scale_factor(load_sensor)
    battery_scale = sw._kw_scale_factor(battery_sensor)
    battery_sign = -1.0 if cfg.get("solver_battery_power_positive_is_charge") else 1.0

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
    actual_charge_kw = np.array([max(0.0, -v) for v in actual_net_kw])
    actual_discharge_kw = np.array([max(0.0, v) for v in actual_net_kw])
    import_price = np.array(
        sw.resample_history_nearest(
            import_price_hist, grid_times, default=0.20, backfill_first=True
        )
    )

    # Component 1: offered vs realised flex -- offered needs real
    # sensor.nimbus_flex_signals history, genuinely absent on a day the
    # opt-in switch was off. Never fabricated: None, not 0.0, when it's
    # simply not there.
    offered_up_hist = sw.fetch_entity_history_range(
        FLEX_SIGNALS_ENTITY_ID, day_start, day_end
    )
    offered_down_hist = sw.fetch_entity_attribute_history_range(
        FLEX_SIGNALS_ENTITY_ID, "flex_available_down_kw", day_start, day_end
    )
    offered_up_kwh = None
    offered_down_kwh = None
    if offered_up_hist:
        offered_up_kw = sw.resample_history_mean(
            offered_up_hist, grid_times, period_hours
        )
        offered_up_kwh = round(float(sum(offered_up_kw) * period_hours), 3)
    if offered_down_hist:
        offered_down_kw = sw.resample_history_mean(
            offered_down_hist, grid_times, period_hours
        )
        offered_down_kwh = round(float(sum(offered_down_kw) * period_hours), 3)
    realised_up_kwh = round(float(np.sum(actual_charge_kw) * period_hours), 3)
    realised_down_kwh = round(float(np.sum(actual_discharge_kw) * period_hours), 3)

    # Component 2: price-response curve -- real net import derived from
    # the same energy-balance identity the rest of this file already
    # relies on, binned by real import price seen.
    net_import_kw = load_kw - solar_kw - actual_discharge_kw + actual_charge_kw
    price_bands: dict[int, list[float]] = {}
    for price, kw in zip(import_price.tolist(), net_import_kw.tolist(), strict=True):
        band_index = int(price // _PRICE_BAND_WIDTH)
        price_bands.setdefault(band_index, []).append(kw)
    price_response_curve = [
        {
            "price_band_low": round(band_index * _PRICE_BAND_WIDTH, 2),
            "price_band_high": round((band_index + 1) * _PRICE_BAND_WIDTH, 2),
            "mean_net_import_kw": round(float(np.mean(kws)), 3),
            "n_samples": len(kws),
        }
        for band_index, kws in sorted(price_bands.items())
    ]

    # Component 3: envelope curtailment -- see this function's own
    # caller docstring for why the static configured limit, not the
    # live #493 envelope entity, is the honest choice for a past day.
    static_export_limit_kw = sw._cfg_num(cfg, "solver_grid_max_export_kw", 0.0)
    envelope_curtailment_kw = np.maximum(
        0.0, solar_kw - load_kw - static_export_limit_kw
    )
    envelope_curtailment_kwh = round(
        float(np.sum(envelope_curtailment_kw) * period_hours), 3
    )

    return {
        "latest_date": day_start.date().isoformat(),
        "offered_up_kwh": offered_up_kwh,
        "offered_down_kwh": offered_down_kwh,
        "realised_up_kwh": realised_up_kwh,
        "realised_down_kwh": realised_down_kwh,
        "envelope_curtailment_kwh": envelope_curtailment_kwh,
        "price_response_curve": price_response_curve,
    }
