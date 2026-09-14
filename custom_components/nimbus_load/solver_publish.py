"""Publishing for the Solver cycle -- stage 3 of nimbus issue #735.

#735 proposes splitting `solver_writer.py`'s ~1,650-line `main()` into a
real module structure, with `solver_inputs/` for input gathering and
`solver_publish.py` for the publishes. This module is the start of the
latter, created for a reason the issue itself records: the load-input
block that stage 2 wants to extract has an `ha_post_state()` call sitting
*inside* it, so moving that block wholesale would put a publish inside an
inputs module and contradict the very split being built.

Hoisting the publish out first is therefore a prerequisite for stage 2,
not a detour -- and it is stage 3 work that has to happen anyway.

**Why this function takes fifteen explicit parameters rather than a
context object.** They were computed by walking the call's own AST rather
than read off the screen, because every blocker found on #735 so far
(#860, #861, and stage 2's own three traps) was invisible from reading
the code being moved. The list is exactly what the call reads, no more:
passing a bag would hide which of them this publish actually depends on,
which is the one thing this exercise exists to make explicit.

One of them is worth naming. `summed_18_now_kw` is deliberately **absent**
-- the `state` published here is `load_kw[0]` *after* the live cross-check
anchor overwrites it, while `summed_18_now_kw` is a snapshot taken before.
Publishing the snapshot as `state` while `forecast[0].value` used the
overwritten array is nimbus issue **#100**, a real bug found on a real
install. Anything that "simplifies" this signature by reintroducing that
variable reintroduces the bug with it.

See `solver_inputs/__init__.py` for why the `solver_writer` import below
is deferred and by-module; the reasoning is identical and load-bearing.
"""

from __future__ import annotations


def _solver_writer():
    """The `solver_writer` module, imported late and by MODULE (never by
    name) -- see `solver_inputs/__init__.py` for the full reasoning on
    both counts, and #861 for the concrete failure that made it
    load-bearing: relocating a function also relocates who its internal
    callers resolve, escaping every `patch.object(solver_writer, ...)` in
    the suite and turning a mocked call into a live HTTP request.

    Dual-mode try/except is the same shape every other project-internal
    import in `solver_writer` already uses: this code is loaded both as
    part of the real package and as a bare top-level module by the test
    harness (tests/_solver_path.py) and the standalone/cron deployment.
    """
    try:
        from . import solver_writer
    except ImportError:  # pragma: no cover - standalone/cron path
        import solver_writer
    return solver_writer


def publish_household_load_total_forecast(
    *,
    cfg,
    grid_times,
    n_periods,
    now,
    load_kw,
    load_lower_kw,
    load_upper_kw,
    live_load_kw,
    whole_house_now_kw,
    load_forecast_entities,
    load_forecast_error,
    load_forecast_warnings,
    load_forecast_source_used,
    load_forecast_coverage_hours,
    failed_load_entities,
):
    """Publish `sensor.nimbus_household_load_total_forecast`.

    Moved verbatim out of `main()` -- the body below is the identical
    call, with only the two module-level names (`ha_post_state`,
    `_cfg_num`) rebound onto the deferred module handle. No value is
    recomputed, no ordering is changed, and in particular the
    `round(load_kw[0], 3)` state read is preserved exactly as it was
    (see this module's docstring for why that specific read matters).
    """
    sw = _solver_writer()
    sw.ha_post_state(
        "sensor.nimbus_household_load_total_forecast",
        # Real bug found via a real-install health check (nimbus repo
        # #100, Mark Purcell): this sensor's own `state` was using
        # summed_18_now_kw -- a snapshot taken BEFORE the live cross-
        # check anchor above (2607-2614) can overwrite load_kw[0] --
        # while `forecast[0].value` below uses load_kw[0] AFTER that
        # same overwrite. Whenever a household configures the whole-
        # house cross-check sensor, this sensor's own headline `state`
        # and its own `forecast[0].value` would silently disagree --
        # two numbers a reasonable reader assumes are the same thing.
        # `sensor.nimbus_solver_config`'s own load_summed_18_now_kw
        # diagnostic (below) is DELIBERATELY left reading the pre-
        # anchor summed_18_now_kw -- its whole documented purpose is
        # comparing two genuinely independent forecasts, not a
        # forecast against an already-live-corrected value (see that
        # field's own comment). This fix is scoped to just this one
        # sensor's own internal state/forecast consistency.
        round(load_kw[0], 3),
        {
            "unit_of_measurement": "kW",
            "device_class": "power",
            "state_class": "measurement",
            "friendly_name": "Nimbus Household Load Total (Summed)",
            "forecast": [
                {
                    "time": grid_times[i].isoformat(),
                    "value": round(load_kw[i], 3),
                    "lower": round(load_lower_kw[i], 3),
                    "upper": round(load_upper_kw[i], 3),
                }
                for i in range(n_periods)
            ],
            "source_entities": load_forecast_entities,
            "load_forecast_source_used": load_forecast_source_used,
            # 2026-08-25, nimbus issue #187 (Mark Purcell, real-install
            # IV&V): v0.89.1's source_sensor/signal_role attributes only
            # ever reached NimbusForecastSensor (subentry-backed load/
            # power-signal sensors) -- this sensor is a genuinely
            # different class (_NimbusSolverPushSensor, a pure REST-
            # attribute mirror), so it never got either key at all, a
            # real missed code path, not an intentional scope boundary.
            # "other", matching every Load subentry's own signal_role
            # (there is no distinct SIGNAL_ROLE_LOAD in this project --
            # see const.py's own convention) -- this sensor's role is
            # definitionally a load aggregate. source_sensor is the
            # single real entity_id only when the single-sensor path is
            # actually active; when the richer multi-circuit summing
            # path is active there genuinely isn't one source to name,
            # so it's honestly None here -- source_entities above is
            # already the correct, richer answer for that case.
            "signal_role": "other",
            "source_sensor": cfg["solver_load_forecast_sensor"]
            if not load_forecast_entities
            else None,
            # NEW (2026-08-25, issue #112: "solver horizon 96.3h exceeds
            # subentry forecast horizon 48h") -- the REAL forecast
            # coverage this run's load_kw is backed by, in hours ahead
            # of `now`. None if it couldn't be determined. Compare
            # against horizon_hours (sensor.nimbus_solver_battery_
            # forecast, and the print() line below): whenever this is
            # smaller, every period beyond it is resample_forecast()'s
            # own flat-hold padding, not a real forecast -- see
            # compute_forecast_coverage_hours()'s own docstring.
            "load_forecast_coverage_hours": round(load_forecast_coverage_hours, 1)
            if load_forecast_coverage_hours is not None
            else None,
            "failed_load_entities": failed_load_entities,
            # NEW (2026-08-24, issue #105): entity_id -> the exact real
            # reason each failed_load_entities member was excluded --
            # was previously invisible on the multi-circuit summing
            # path (a bare "unavailable" in the log, nothing on this
            # sensor at all). See sum_load_forecasts()'s own docstring
            # for the full "why this exists" story.
            "load_forecast_warnings": load_forecast_warnings,
            "whole_house_cross_check_now_kw": round(whole_house_now_kw, 3)
            if whole_house_now_kw is not None
            else None,
            # nimbus issue #429: the genuine live meter reading, distinct
            # from whole_house_cross_check_now_kw above (that field is
            # the meter's own FORECAST model, not a live reading -- see
            # this block's own comment further up). None when
            # unconfigured or the read failed, same honest-absence
            # convention as every other optional diagnostic here.
            "whole_house_live_now_kw": round(live_load_kw, 3)
            if live_load_kw is not None
            else None,
            "inverter_self_consumption_kw": sw._cfg_num(
                cfg, "solver_inverter_self_consumption_kw", 0.0
            ),
            # None on success -- the exact human-readable reason on
            # failure, real proposal #2 from nimbus repo issue #66.
            "load_forecast_source_error": load_forecast_error,
            "generated_at": now.isoformat(),
        },
    )
