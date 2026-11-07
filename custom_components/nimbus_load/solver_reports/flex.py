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

import json
import urllib.error
from datetime import UTC, datetime, timedelta

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


# --- moved from solver_writer.py (#2289 item 6, step 7b part 3): the publisher ---


def compute_daily_flex_report(cfg: dict, now: datetime) -> dict | None:
    """nimbus issue #496 (Signals 7/7 of #489), the second half Mark
    Purcell authorized 2026-09-09 ("compute_daily_flex_report()'s own
    offered-vs-realised/envelope-curtailment arithmetic") -- the sensor
    half (switch.nimbus_solver_flex_signals_enabled, sensor.nimbus_flex_
    signals) shipped separately as v0.94.282/v0.94.283. Same "yesterday"
    thin-wrapper convention as compute_daily_quality_report() just above
    this file's own quality-report section -- see that function's own
    docstring for why "yesterday" (not "today so far") is the right
    window for a real day-level report.

    Three real, independently-available components, per this issue's
    own body:

    1. **Offered vs realised flex** (kWh, up and down). "Offered" is the
       real recorder history of sensor.nimbus_flex_signals's own
       flex_available_up_kw (native state) / flex_available_down_kw
       (attribute) -- genuinely only available on days the opt-in
       switch was on; `None` otherwise, never fabricated. "Realised" is
       defined here as the real measured battery response: actual
       charge kWh for "up" (absorbing more load right now IS charging
       more), actual discharge kWh for "down" (reducing net import /
       increasing export IS discharging) -- the same real battery-power
       history and sign-convention handling compute_daily_quality_
       report() already uses (solver_battery_power_sensor,
       solver_battery_power_positive_is_charge), not a second,
       independently-drifting read of the same sensor. This is a real,
       defensible, but not the only possible definition of "realised" --
       no generic commanded-vs-actual flex-event signal exists anywhere
       in this codebase (same honest limitation compute_daily_quality_
       report()'s own docstring already discloses for tracking_fidelity).

    2. **Price-response curve**: real (import price, net import kW)
       pairs for the day, binned into $0.05/kWh-wide price bands, mean
       net import kW per band -- an empirical demand-response curve
       from Nimbus's own measured data, matching #496's own "the
       nem-flex-telemetry price-response tab computed locally" ask.
       net_import_kw is derived from the SAME three sensors quality
       report already requires (load - solar - discharge + charge, the
       real energy-balance identity), not a new required config field.

    3. **Envelope curtailment kWh**: `max(0, solar - load - export_limit)`
       per interval, real solar/load history, real configured
       `solver_grid_max_export_kw` as the export limit -- NOT the 5 kW
       default this issue's own body explicitly warns against. #493's
       own live DNSP envelope entity (resolve_envelope_limit_kw()) is
       deliberately NOT used here: that function only ever returns the
       CURRENT live/forecast value held flat, with no real historical
       backing for a past envelope schedule (see its own docstring) --
       using it for a day already gone would silently report today's
       envelope as if it were yesterday's, which is worse than the
       honest, disclosed simplification of using the flat configured
       static limit (the same real per-install number every other
       fallback in this codebase already uses instead of a fabricated
       constant).

    Returns a dict with `latest_date` plus the three components above
    (offered/realised are `None`, not zero, on a day the switch was
    off), or `None` if genuinely not configured (same three required
    sensors as compute_daily_quality_report()) or no real history
    exists for yesterday at all -- callers must treat `None` as "skip
    this cycle, retry later," never an error, same convention as every
    other report function in this file.
    """
    sw = _solver_writer()
    yesterday = (now - timedelta(days=1)).date()
    day_start = datetime(
        yesterday.year, yesterday.month, yesterday.day, tzinfo=sw.LOCAL_TZ
    )
    day_end = day_start + timedelta(days=1)
    return _compute_flex_report_for_window(cfg, day_start, day_end)


FLEX_REPORT_ENTITY_ID = "sensor.nimbus_flex_report"


def publish_daily_flex_report(cfg: dict, now: datetime) -> None:
    """Publishes sensor.nimbus_flex_report -- same idempotency-first,
    re-push-on-fast-path pattern as publish_daily_quality_report() just
    above this file's own quality-report section (see that function's
    own docstring for the full "why re-push instead of no-op" incident
    history, #289/#292 -- applies identically here, same freshness-
    watchdog mechanism, same entity class).
    """
    sw = _solver_writer()
    yesterday_key = (now - timedelta(days=1)).date().isoformat()
    try:
        existing = sw.ha_get(
            solver_shared.resolve_real_entity_id(FLEX_REPORT_ENTITY_ID)
        )
        if existing.get("attributes", {}).get("latest_date") == yesterday_key:
            solver_shared._LOGGER.debug(
                "Nimbus flex report: fast-path hit, already scored %s -- "
                "re-pushing cached state to keep the freshness stamp alive",
                yesterday_key,
            )
            sw.ha_post_state(
                FLEX_REPORT_ENTITY_ID, existing["state"], existing["attributes"]
            )
            return
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        pass
    report = compute_daily_flex_report(cfg, now)
    if report is None:
        solver_shared._LOGGER.debug(
            "Nimbus flex report: no report for %s this cycle -- sensor "
            "left unchanged, will retry next cycle",
            yesterday_key,
        )
        return
    sw.ha_post_state(
        FLEX_REPORT_ENTITY_ID,
        report["realised_up_kwh"] + report["realised_down_kwh"],
        {
            "unit_of_measurement": "kWh",
            "friendly_name": "Nimbus Flex Report",
            **report,
            "generated_at": datetime.now(UTC).astimezone(sw.LOCAL_TZ).isoformat(),
            # nimbus issue #1256: stamped with the computation, not left to
            # the entity's running-install fallback -- this sensor restores
            # across a restart, so the two diverge on every deploy.
            **sw._version_stamp(),
        },
    )
