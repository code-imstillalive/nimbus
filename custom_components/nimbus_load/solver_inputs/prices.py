"""The price and P2P input slice, extracted from `solver_writer.main()`
(nimbus issue #735 stage 4).

**Why this one took three attempts to cut.** Stage 4 was measured twice and
deferred twice -- once as "no clean seam", then corrected -- and stage 6's
own scan is what reversed that conclusion by finding the branch below is a
single self-contained `if`/`else` rather than the interleaved run the
earlier passes had assumed. Mark Purcell independently re-measured the
ledger afterwards (5 inputs, 10 outputs, ~263 lines) and confirmed it as
the largest remaining candidate.

**What made it safe to move.** Three properties, each checked mechanically
rather than assumed:

- the region is ONE statement, so there is no interleaving to preserve;
- every one of the nine returned names is assigned in **both** arms, so the
  unconditional `return` below cannot raise `NameError` on either path;
- the move is a pure dedent. The body is byte-identical to what stood in
  `main()` once re-indented, which is the one transformation that provably
  cannot re-parent anything -- the trap #873 hit, where hoisting a block
  beginning with `if` into an `if`/`elif` chain silently re-parented the
  following `elif` while passing ruff, mypy and its own tests.

The `sw.` prefixes were inserted at AST node positions and then verified by
mapping them back and comparing ASTs, so no occurrence inside a string or
comment was touched.

`has_price_forecast_array` is a PARAMETER rather than something computed
here, and deliberately so: `main()` reads it again after this call (to gate
the percentile-band reuse), so computing it in both places would be two
sources of truth for one question.
"""

from __future__ import annotations

import json
import math
import urllib.error
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

try:
    from .. import solver_shared
except ImportError:  # pragma: no cover - standalone/cron path
    import solver_shared  # type: ignore[no-redef]
try:
    from . import price_freshness
except ImportError:  # pragma: no cover - standalone/cron path
    from solver_inputs import price_freshness  # type: ignore[no-redef]

try:
    from .. import price_intervals
except ImportError:  # pragma: no cover - standalone/cron path
    import price_intervals  # type: ignore[no-redef]
try:
    from .. import device_bindings
except ImportError:  # pragma: no cover - standalone/cron path
    import device_bindings  # type: ignore[no-redef]

from . import (
    loss_factors,
    p2p,
    price_blend,
    price_consolidation,
    price_error_history,
    rrp_crosscheck,
)


def _bucket_5min(t):
    """5-minute-of-day bucket, local time, as compute_5min_offset() keys it."""
    lt = _solver_writer()._local(t)
    return lt.hour * 12 + lt.minute // 5


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
class PriceArrays:
    """Everything the price/P2P slice produces, in one value.

    Same `frozen=True` reasoning as `LoadArrays`: it stabilises the record,
    not the arrays inside it.

    `spot_import_raw` keeps its `_raw` suffix from `main()` unchanged. It is
    the pre-blend series, and the name is load-bearing at the call site --
    `blend_price_with_secondary_sources()` runs against it afterwards, so a
    caller reaching for "the import price" wants neither this nor the blend
    without knowing which.
    """

    spot_import_raw: object
    spot_export: object
    import_real_mask: object
    export_real_mask: object
    import_price_upper_band: object
    export_price_lower_band: object
    export_bonus_price: object
    match_fraction: object
    p2p_recent_volume_kwh: object
    # nimbus #1793: the price source's refresh stamp and age, or None when it
    # publishes none. `stale` True means every period above was priced from
    # AEMO plus the learned markup instead of the source's own rows.
    price_source_freshness: dict | None = None
    # nimbus #1786: the regional wholesale forecast plus the learned retail
    # markup, over the whole horizon, per side -- scored as a price source of
    # its own by the consolidation shadow. None on the generic branch.
    aemo_import_series: object = None
    aemo_export_series: object = None
    # nimbus #1786: "retail_from_rrp" when the site's loss factors are known
    # (the retail price built from AEMO's RRP: RRP x MLF x DLF plus the network
    # export tariff; on import, plus the network import tariff and retailer
    # usage fees, all x 1.1 GST), else "aemo_markup" (RRP plus one learned
    # offset, as before); and the factor, for `price_consolidation.loss_factor`.
    aemo_source: str = "aemo_markup"
    loss_factor: dict | None = None


def _contract_price_forecast(cfg, role, grid_times, now):
    """(values, real_mask) for `role` read through the device contract while
    the device setup is `active` (DC-006 slice 4, #1574), or None to read as
    today: not handed one, or the contract refuses it this cycle (no unit,
    overlapping points, unreadable).

    Read as the Solver's own reader, except where #2119 pins a difference:
    a period that straddles a price change takes the time-weighted mean,
    not the value at its start. Before the first point and past the last,
    where the contract has a gap, the nearest point is held with
    `real_mask` False, exactly as today, so the LP never lacks a price.
    A period is real when it starts inside a published price, today's rule;
    the last point's own interval (one step) is published, so it is real."""
    handed = (cfg.get("device_contract_price_forecasts") or {}).get(role)
    if not handed or not grid_times:
        return None
    document = {
        "schema_version": 1,
        "site": handed.get("site") or {},
        "devices": [
            {"id": "grid", "kind": "grid", "bindings": {role: handed.get("binding")}}
        ],
    }
    tail = (
        grid_times[-1] - grid_times[-2] if len(grid_times) > 1 else timedelta(minutes=5)
    )
    ends = [*grid_times[1:], grid_times[-1] + tail]
    sw = _solver_writer()

    def state(entity_id):
        try:
            return sw.ha_get(entity_id)
        except Exception:  # noqa: BLE001 -- an unreadable sensor is a refusal
            solver_shared._LOGGER.debug(
                "Nimbus #1574: %s unreadable for %s", entity_id, role, exc_info=True
            )
            return None

    try:
        series = device_bindings.resolve(
            document,
            "grid",
            role,
            device_bindings.Readers(state=state),
            now,
            window=(grid_times[0], ends[-1]),
        )
    except Exception:  # noqa: BLE001 -- the contract must never break a solve
        solver_shared._LOGGER.debug("Nimbus #1574: %s unreadable", role, exc_info=True)
        return None
    intervals = getattr(series, "intervals", ())
    # `stale` (no update for five minutes) is read as today's reader reads it,
    # which has no age rule: refusing it would switch readers every cycle on
    # a sensor that updates half-hourly.
    readable = (device_bindings.VALID, device_bindings.STALE)
    if getattr(series, "state", None) not in readable or not intervals:
        solver_shared._LOGGER.debug(
            "Nimbus #1574: %s refused (%s); read as today",
            role,
            getattr(series, "reason", None),
        )
        return None
    values: list[float] = []
    real: list[bool] = []
    held = intervals[0].value
    for start, end in zip(grid_times, ends, strict=True):
        value, _covered = device_bindings.value_over(series, start, end)
        if value is None:
            values.append(held)
            real.append(False)
            continue
        values.append(value)
        held = value
        # Real, as today, when the period starts inside a published price.
        real.append(any(i.start <= start < i.end for i in intervals))
    return values, real


def build_price_arrays(
    cfg, grid_times, n_periods, now, has_price_forecast_array, price_forecast_sensor
) -> PriceArrays:
    """Fetch, resample and blend the import/export price series and the
    P2P premium.

    Two paths, gated on `has_price_forecast_array` -- whether a rich,
    forecast-array-shaped price sensor is actually configured. The bodies
    are unchanged from `main()`; see their own inline comments for the real
    household findings behind each.
    """
    sw = _solver_writer()

    if has_price_forecast_array:
        # PRIMARY path -- richer than the generic fallback below whenever
        # this field is actually configured, unchanged in BEHAVIOUR from
        # before 2026-08-20's config-flow wiring; only WHERE each entity
        # name comes from changed in the 2026-09-02 audit.
        lv_price_fc = sw.ha_get(price_forecast_sensor)["attributes"]["forecast"]
        # Real AEMO-anchored, 5-min-of-day price extrapolation (2026-08-16,
        # see compute_5min_offset()'s own docstring for the full real
        # finding) -- replaces the old flat-hold-last-value / hourly-average
        # behaviour for periods beyond this sensor's own real forecast
        # coverage (~24h on this household's own LocalVolts install, after
        # the lv_forecast_writer.py truncation fix, was ~12h). Both new
        # entity params default to None (a clean no-op, same graceful
        # fallback each function already had) whenever the household
        # hasn't also configured the matching optional field.
        aemo_forecast = price_blend.fetch_aemo_forecast(
            cfg.get("solver_regional_spot_forecast_sensor")
        )
        # 2026-08-20: migrated off guerrier's sensor.costsflexup/earningsflexup
        # onto our own project-owned equivalents (same shape, same source --
        # lv_forecast_writer.py's push_flex_sensor(), built session 41
        # specifically to mirror guerrier's own sensor attributes; already
        # recorder-tracked with real, gap-free history -- confirmed live
        # 2026-08-20). Real goal: this project no longer needs the guerrier
        # HACS integration at all once every consumer is migrated (see
        # CLAUDE.md's Aug 20 session log for the full investigation). 2026-
        # 09-02: these are ALREADY the same real entities this household's
        # own CONF_SOLVER_IMPORT_PRICE_SENSOR/EXPORT_PRICE_SENSOR fields
        # point at (confirmed live) -- reusing those existing, already-
        # wizard-configured fields instead of two more hardcoded literals.
        import_history = price_blend.fetch_price_history(
            cfg["solver_import_price_sensor"]
        )
        export_history = price_blend.fetch_price_history(
            cfg["solver_export_price_sensor"]
        )
        # nimbus #1698 (Mark Purcell): an Amber Express price sensor carries the
        # interval's wholesale in the same response; collected each solve, it
        # is the reference for the intervals it covers. Empty otherwise.
        same_poll_wholesale = collect_amber_spot(
            (
                cfg.get("solver_import_price_sensor"),
                cfg.get("solver_export_price_sensor"),
            ),
            now,
        )
        import_offset_by_5min = price_blend.compute_5min_offset(
            import_history,
            regional_spot_sensor=cfg.get("solver_regional_spot_current_price_sensor"),
            same_poll_wholesale=same_poll_wholesale,
        )
        export_offset_by_5min = price_blend.compute_5min_offset(
            export_history,
            regional_spot_sensor=cfg.get("solver_regional_spot_current_price_sensor"),
            same_poll_wholesale=same_poll_wholesale,
        )
        # nimbus #1786: AEMO plus markup as a source of its own. Never allowed
        # to break the solve; None simply leaves it out of the consolidation.
        aemo_import_series: list[float | None] | None
        aemo_export_series: list[float | None] | None
        try:
            aemo_import_series = price_consolidation.aemo_markup_series(
                grid_times, aemo_forecast, import_offset_by_5min, _bucket_5min
            )
            aemo_export_series = price_consolidation.aemo_markup_series(
                grid_times, aemo_forecast, export_offset_by_5min, _bucket_5min
            )
        except Exception as e:  # noqa: BLE001 -- a shadow input must never break a solve
            solver_shared._LOGGER.debug("Nimbus #1786: AEMO series failed: %s", e)
            aemo_import_series = aemo_export_series = None
        # nimbus #1786 (Mark): with the site's loss-factor codes, the AEMO
        # source is the retail price built from the RRP by contract -- RRP x
        # MLF x DLF plus the learned network tariffs and retailer usage fees,
        # GST on import. Shadow only: the offsets
        # above, which also extend the live plan past its price source's own
        # coverage, are untouched.
        aemo_source = "aemo_markup"
        loss_factor = None
        # nimbus #2190 (Mark): AEMO NEM Data's own predispatch where it
        # covers, the regional forecast (PD7DAY) past its last row.
        rrp_forecast, aemo_until = price_consolidation.join_rrp_forecasts(
            price_blend.fetch_aemo_forecast(cfg.get("solver_aemo_forecast_sensor")),
            aemo_forecast,
        )
        try:
            factored = _retail_from_rrp(
                sw,
                cfg,
                now,
                grid_times,
                rrp_forecast,
                import_history,
                export_history,
                same_poll_wholesale,
            )
        except Exception as e:  # noqa: BLE001 -- a shadow input must never break a solve
            solver_shared._LOGGER.debug(
                "Nimbus #1786: loss-factor series failed: %s", e
            )
            factored = None
        if factored is not None:
            aemo_import_series, aemo_export_series, loss_factor = factored
            if isinstance(loss_factor, dict):
                loss_factor = {**loss_factor, "aemo_forecast_until": aemo_until}
            if aemo_import_series is not None:
                aemo_source = "retail_from_rrp"
        # Real empirical price bands for price_risk_aversion (2026-08-21,
        # task #128 -- see compute_price_percentile_band()'s own docstring).
        # A SEPARATE, longer (14-day, vs the 5-day history already fetched
        # above for compute_5min_offset's own mean-offset use) fetch --
        # percentile estimation genuinely benefits from more real samples
        # per bucket than a mean does, and this data has been live and
        # recorder-tracked since well before 14 days ago.
        import_price_upper_band = price_blend.compute_price_percentile_band(
            price_blend.fetch_price_history(cfg["solver_import_price_sensor"], days=14),
            90.0,
        )
        export_price_lower_band = price_blend.compute_price_percentile_band(
            price_blend.fetch_price_history(cfg["solver_export_price_sensor"], days=14),
            10.0,
        )
        # nimbus issue #348 (Mark Purcell, codebase review): the "generic"
        # solver_price_forecast_array_sensor field was parsed with these
        # two attribute-key literals hardcoded, even though
        # resample_price_with_extrapolation() itself already takes
        # `value_key` as a genuinely generic parameter -- the hardcoding
        # was purely at this call site. A non-LocalVolts array sensor with
        # a different attribute-key shape would silently find zero points
        # (see that function's own `if not pts: return zero-filled` guard)
        # rather than erroring, giving no hint the real problem was these
        # two literal names. Now two real, optional wizard fields, each
        # defaulting to the exact literal this call has always used --
        # byte-identical behaviour for this install (or any other genuine
        # LocalVolts install) until either is explicitly changed.
        import_key = cfg.get("solver_price_forecast_array_import_key") or "costsflexup"
        export_key = (
            cfg.get("solver_price_forecast_array_export_key") or "earningsflexup"
        )
        spot_import_raw, import_real_mask = (
            price_blend.resample_price_with_extrapolation(
                lv_price_fc,
                import_key,
                grid_times,
                aemo_forecast,
                import_offset_by_5min,
            )
        )
        spot_export, export_real_mask = price_blend.resample_price_with_extrapolation(
            lv_price_fc,
            export_key,
            grid_times,
            aemo_forecast,
            export_offset_by_5min,
        )
        # nimbus #1793 (Mark Purcell): a source whose rows LocalVolts has not
        # rebuilt for over an hour is not planned on. Every period, the
        # current interval included, comes from AEMO plus the learned
        # markup -- the rule already used beyond the source's horizon.
        price_source_freshness = price_freshness.check(
            sw.ha_get,
            (
                price_forecast_sensor,
                cfg.get("solver_import_price_sensor"),
                cfg.get("solver_export_price_sensor"),
            ),
            now,
            localvolts_v2=(cfg.get("known_sources") or {}).get("localvolts_v2"),
        )
        if price_source_freshness and price_source_freshness["stale"]:
            live_spot = None
            spot_sensor = cfg.get("solver_regional_spot_current_price_sensor")
            if spot_sensor:
                try:
                    live_spot = float(
                        price_blend.price_now(
                            spot_sensor, fallback=float("nan"), quiet=True
                        )
                    )
                except Exception as err:  # noqa: BLE001 -- no live spot is not an error
                    solver_shared._LOGGER.debug(
                        "Nimbus #1793: live AEMO spot unreadable: %s", err
                    )
                    live_spot = None
                if live_spot is not None and math.isnan(live_spot):
                    live_spot = None
            fallback_import = price_freshness.aemo_series(
                grid_times, aemo_forecast, import_offset_by_5min, sw._local, live_spot
            )
            fallback_export = price_freshness.aemo_series(
                grid_times, aemo_forecast, export_offset_by_5min, sw._local, live_spot
            )
            if fallback_import is not None and fallback_export is not None:
                spot_import_raw, spot_export = fallback_import, fallback_export
                import_real_mask = [False] * len(grid_times)
                export_real_mask = [False] * len(grid_times)
                price_source_freshness = {**price_source_freshness, "fallback": "aemo"}
                price_blend._warn_stale_price_source_once(price_source_freshness)
            else:
                price_source_freshness = {**price_source_freshness, "fallback": None}
        # nimbus #1537 item 5: the matched rate counts inside the household's
        # own configured blocks, not a fixed 17:00-24:00.
        # nimbus #1537 item 3: an optional second matched-rate source, used
        # per period where the first shows no match.
        p2p_export = p2p.resample_real_p2p_rate(
            grid_times,
            cfg.get("solver_p2p_matched_rate_forecast_sensor"),
            sw.fetch_p2p_fixed_export_kw(cfg, grid_times),
            fallback_sensor_id=cfg.get("solver_p2p_matched_rate_forecast_sensor_2"),
        )

        # nimbus issue #452: current-interval AEMO P5MIN cross-check.
        # spot_import_raw[0] is the raw retail COMMODITY price at "now"
        # (grid_times[0]) -- deliberately read here, before the TOU/flat-
        # fee baking immediately below, since comparing a fee-loaded
        # price against AEMO's own wholesale figure would make every
        # single period "disagree" by roughly the fee amount, defeating
        # the point of the check. Same "never break the real solve"
        # wrapping as update_solar_delivery_ratio() above -- this is a
        # diagnostic cross-check, never allowed to affect the real plan.
        try:
            aemo_p5min_check = price_blend.check_aemo_p5min_disagreement(
                cfg.get("solver_regional_spot_current_price_sensor"),
                float(spot_import_raw[0]),
                sw._local(grid_times[0]).hour * 12
                + sw._local(grid_times[0]).minute // 5,
                import_offset_by_5min,
                sw._cfg_num(
                    cfg, "solver_aemo_p5min_disagreement_threshold_dollars", 0.10
                ),
            )
        except Exception as e:  # noqa: BLE001 -- see comment above; must never break the real solve
            solver_shared._LOGGER.warning(
                "Nimbus #452: AEMO P5MIN disagreement check failed: %s", e
            )
            aemo_p5min_check = None
        if aemo_p5min_check is not None and aemo_p5min_check["flagged"]:
            price_blend._warn_aemo_p5min_disagreement_once(
                grid_times[0], aemo_p5min_check
            )

        # nimbus issue #452, the other half: AEMO's own 30-minute
        # forecast against AEMO's own realised 5-minute prices for the
        # SAME window. Wholesale against wholesale, so unlike the
        # retail check above there is no markup offset term at all --
        # this asks whether AEMO's predispatch is tracking AEMO's own
        # dispatch, which is a genuinely different question.
        #
        # Both entities are discovered, not configured: Mark Purcell
        # chose auto-discovery over a 27th wizard field ("proceed with
        # auto discovery"), consistent with #495/#768 and with #449
        # tracking that wizard as already too large. A no-op on any
        # install without exactly one of each -- same blank-is-off
        # convention as every other optional source here.
        try:
            forecast_entity = cfg.get("aemo_30min_forecast_sensor")
            actuals_entity = cfg.get("solver_regional_spot_current_price_sensor")
            aemo_fc_check = None
            if forecast_entity and actuals_entity:
                entries = sw.ha_get(forecast_entity)["attributes"].get("forecast")
                window_start = now - timedelta(minutes=30)
                samples = sw.fetch_entity_history_range(
                    actuals_entity, window_start, now
                )
                aemo_fc_check = sw.aemo_crosscheck.compare_forecast_to_actuals(
                    entries,
                    samples,
                    now,
                    sw._cfg_num(
                        cfg,
                        "solver_aemo_p5min_disagreement_threshold_dollars",
                        0.10,
                    ),
                )
        except Exception as e:  # noqa: BLE001 -- a diagnostic must never break a real solve
            solver_shared._LOGGER.warning(
                "Nimbus #452: AEMO forecast-vs-actuals check failed: %s", e
            )
            aemo_fc_check = None
        if aemo_fc_check is not None and aemo_fc_check["flagged"]:
            price_blend._warn_aemo_forecast_vs_actuals_once(
                grid_times[0], aemo_fc_check
            )

        # Real, live-CONFIGURABLE TOU network + flat fees baked directly
        # into import_price[t] (2026-08-16, real ask: "it needs ot be super
        # accurate") -- was previously just costsflexup (the spot commodity
        # price alone), missing real cost this household actually pays.
        # Baking it into the LP's own price input (not just reporting it
        # after the fact) means the dispatch DECISION also correctly
        # avoids real peak-hour import, not just the reported total.
        #
        # import_fee_rate()/solver_flat_fee_rate replace the old hardcoded
        # NETWORK_ENERGY_PEAK/OFFPEAK/SHOULDER_RATE/CERTIFICATES_RATE
        # Python constants entirely (2026-08-22, direct household demand:
        # "I TOLD U NO HARDCODED INPUTS - this has to work as user
        # setting") -- see import_fee_rate()'s own docstring for the real
        # "default + up to 3 override blocks" mechanism. Reads live from
        # cfg, same as every other Solver setting; a fresh install with
        # nothing configured correctly contributes 0 fees, same honest
        # no-op default as the fallback branch below already has.
        #
        # Real bug found live (devhub, 2026-08-24, nimbus repo issue
        # #152): this fee application used to live ONLY inside this
        # has_price_forecast_array branch, even though network_fee_*/
        # solver_flat_fee_rate are genuinely generic, portable
        # config-flow fields with no LocalVolts dependency at all --
        # any real installer without LocalVolts who filled them in via
        # the dashboard (exactly as the wizard invites them to) had
        # them silently ignored, zero fee ever applied, zero warning.
        # Moved below the has_price_forecast_array/else split entirely (see
        # "generic + real" fee application, after this if/else) so it
        # applies uniformly to whichever spot_import_raw either branch
        # produced -- LocalVolts-specific behaviour (AEMO extrapolation,
        # live P2P-window detection) stays exactly as before, only the
        # genuinely-portable fee step moved.

        # Two-tier export pricing (2026-08-17, real fix -- see
        # p2p_recent_avg_volume_kwh()'s own docstring for the full finding).
        # REPLACES the old flat match_fraction price-dilution blend: instead
        # of averaging every kWh down to a diluted rate, export_price is now
        # just the real base/spot rate (always available, uncapped), and the
        # real INCREMENTAL P2P premium (only positive during a genuine real
        # P2P-window period, since p2p_export[i] is 0 elsewhere by the
        # placeholder sensor's own design) is passed to the Solver separately
        # via GridConfig.export_bonus_price, capped in volume PER REAL DAY by
        # GridConfig.export_bonus_volume_kwh (see nimbus's own network.py
        # docstring, "TWO-TIER EXPORT BONUS"). match_fraction is still
        # computed and reported (pushed sensor attribute) for informational
        # context -- no longer used to price the LP.
        p2p_settlement_sensor = cfg.get("solver_p2p_settlement_history_sensor")
        match_fraction = p2p.p2p_match_fraction(settlement_sensor=p2p_settlement_sensor)
        p2p_recent_volume_kwh = p2p.p2p_recent_avg_volume_kwh(
            settlement_sensor=p2p_settlement_sensor
        )
        # nimbus #1537: with no settlement history to measure from, the
        # blocks' own committed energy, not the reference household's 60 kWh.
        # An install with a settlement sensor is unchanged.
        if not p2p_settlement_sensor:
            block_kwh = sw.p2p_blocks_daily_energy_kwh(cfg)
            if block_kwh > 0:
                p2p_recent_volume_kwh = block_kwh
        # Fix 2026-09-05 (mirrors the identical fix in the sibling
        # reference household's nimbus_solver_forecast_writer.py -- the
        # standalone cron writer's own copy of this exact logic). Direct
        # household catch, live, via a Solver Forecast table screenshot
        # showing gold "P2P active" coloring at 0.0 c/kWh for genuine
        # midday hours, well outside any configured P2P block. Confirmed
        # live: whenever real spot_export goes negative outside the P2P
        # window (a real midday high-solar effect) while p2p_export is
        # correctly 0 (no block active), max(0.0, 0 - negative) produces
        # a small POSITIVE bonus_price purely by construction, with zero
        # real P2P activity behind it -- feeding the LP a spurious, if
        # tiny, P2P-shaped incentive it should never see outside a real
        # block. Explicitly zeroed whenever p2p_export[i] itself is 0.
        export_bonus_price = [
            max(0.0, p2p_export[i] - spot_export[i]) if p2p_export[i] > 0 else 0.0
            for i in range(n_periods)
        ]
    else:
        # nimbus #1793: the generic arm reads no refresh stamp.
        price_source_freshness = None
        # FALLBACK (2026-08-20, for anyone else): PREFERS a real, live
        # forecast if the configured sensor exposes one (2026-08-22, real
        # finding from Mark Purcell's own install -- see
        # resample_generic_price_forecast()'s own docstring for the full
        # story). Falls back to the sensor's CURRENT value held flat
        # across the whole horizon only when no usable forecast is found
        # -- the only thing genuinely possible with truly no forward-
        # looking data at all. No AEMO extrapolation, no network TOU
        # tables, no live P2P-window detection -- all of those are this-
        # household/Australian-NEM-specific and have no portable
        # equivalent yet (a real, honest, separately-tracked gap, not
        # pretended away).
        # DC-006 slice 4 (#1574): in `active`, through the device contract.
        _import_fc = _contract_price_forecast(
            cfg, "import_price_forecast", grid_times, now
        ) or price_blend.resample_generic_price_forecast_with_coverage(
            cfg["solver_import_price_sensor"], grid_times
        )
        # Real fee breakdown DOES apply to a generic install too, same
        # as LocalVolts -- see the "generic + real" fee application
        # below, after this if/else (nimbus repo issue #152). This is
        # just the raw spot/live price; fees get added uniformly for
        # both branches after this block.
        if _import_fc is not None:
            spot_import_raw, import_real_mask = _import_fc
        else:
            spot_import_raw = [
                price_blend.price_now(cfg["solver_import_price_sensor"])
            ] * n_periods
            import_real_mask = [True] * n_periods
        _export_fc = _contract_price_forecast(
            cfg, "export_price_forecast", grid_times, now
        ) or price_blend.resample_generic_price_forecast_with_coverage(
            cfg["solver_export_price_sensor"], grid_times
        )
        if _export_fc is not None:
            spot_export, export_real_mask = _export_fc
        else:
            spot_export = [
                price_blend.price_now(cfg["solver_export_price_sensor"])
            ] * n_periods
            export_real_mask = [True] * n_periods
        # nimbus #1537 item 4: past the source's own last real point the
        # generic branch held the last price flat (on #1535's capture, 117 of
        # 133 opposing-flow periods sat in that tail). With a regional spot
        # forecast AND a regional current-price sensor configured, extend the
        # tail as the price-array branch does: wholesale forecast plus the
        # learned 5-minute-of-day retail markup. Without both, unchanged.
        if _import_fc is not None or _export_fc is not None:
            spot_import_raw, spot_export = price_blend.extend_generic_price_tails(
                cfg,
                grid_times,
                (spot_import_raw, import_real_mask, _import_fc is not None),
                (spot_export, export_real_mask, _export_fc is not None),
            )
        match_fraction = 0.0
        # Manual, static P2P bonus from the config-flow's own optional
        # block (both default to 0.0 -- a full no-op -- if the household
        # doesn't have any P2P/community-trading scheme at all).
        p2p_recent_volume_kwh = sw._cfg_num(cfg, "solver_p2p_bonus_volume_kwh", 0.0)
        # nimbus #1537: left at 0 with blocks configured, the LP could claim
        # no P2P volume at all, so the blocks' own committed energy instead.
        if p2p_recent_volume_kwh <= 0:
            p2p_recent_volume_kwh = sw.p2p_blocks_daily_energy_kwh(cfg)
        bonus_price_flat = sw._cfg_num(cfg, "solver_p2p_bonus_price", 0.0)
        # nimbus #1079: gated to the household's own committed blocks,
        # for the same reason the LocalVolts branch above zeroes it
        # outside a real block (2026-09-05). A static configured bonus
        # is no more earnable at 05:00 than a settled one is, and this
        # branch prices a LIVE dispatch LP -- an ungated premium here
        # buys real energy at a real cost to chase revenue that will
        # not arrive.
        export_bonus_price = list(
            sw.p2p_bonus_price_by_period(
                bonus_price_flat,
                sw.fetch_p2p_fixed_export_kw(cfg, grid_times),
                n_periods,
            )
        )
        # No real multi-day recorded history to build an empirical band
        # from for a generic install -- price_risk_aversion (if a household
        # sets it > 0 anyway) is then a genuine no-op, same as every other
        # this-household-specific enhancement in the fallback branch above.
        import_price_upper_band = {}
        export_price_lower_band = {}
        aemo_import_series = aemo_export_series = None
        aemo_source = "aemo_markup"
        loss_factor = None
    return PriceArrays(
        spot_import_raw=spot_import_raw,
        spot_export=spot_export,
        import_real_mask=import_real_mask,
        export_real_mask=export_real_mask,
        import_price_upper_band=import_price_upper_band,
        export_price_lower_band=export_price_lower_band,
        export_bonus_price=export_bonus_price,
        match_fraction=match_fraction,
        p2p_recent_volume_kwh=p2p_recent_volume_kwh,
        price_source_freshness=price_source_freshness,
        aemo_import_series=aemo_import_series,
        aemo_export_series=aemo_export_series,
        aemo_source=aemo_source,
        loss_factor=loss_factor,
    )


def _loss_factor_codes(sw, cfg) -> tuple[str | None, str | None]:
    """The site's TNI and DLF code, from the two configured sensors' states
    (nimbus #1786). None for a blank field or an unreadable state."""

    def state(key: str) -> str | None:
        entity_id = cfg.get(key)
        if not entity_id:
            return None
        try:
            value = sw.ha_get(entity_id).get("state")
        except Exception as e:  # noqa: BLE001 -- an unreadable code is no code
            solver_shared._LOGGER.debug("Nimbus #1786: %s unreadable: %s", entity_id, e)
            return None
        if value in (None, "", "unknown", "unavailable"):
            return None
        return str(value)

    return (
        state("solver_loss_factor_tni_sensor"),
        state("solver_loss_factor_dlf_code_sensor"),
    )


def _retail_from_rrp(
    sw,
    cfg,
    now,
    grid_times,
    aemo_forecast,
    import_history,
    export_history,
    same_poll_wholesale,
):
    """(import series, export series, loss-factor summary) priced by contract,
    or None when the site has no codes configured. With codes that can't be
    looked up, the series are None and the summary says why."""
    tni, dlf_code = _loss_factor_codes(sw, cfg)
    if tni is None and dlf_code is None:
        return None
    looked_up = loss_factors.site_factor(tni, dlf_code, now)
    summary = {
        "tni": tni,
        "dlf_code": dlf_code,
        "financial_year": loss_factors.financial_year(now),
        "value": None if looked_up.value is None else round(looked_up.value, 6),
        "reason": looked_up.reason,
    }
    if looked_up.value is None:
        return None, None, summary

    def export_scale(t):
        return loss_factors.site_factor(tni, dlf_code, t).value

    def import_scale(t):
        k = export_scale(t)
        return None if k is None else k * loss_factors.IMPORT_GST

    spot_sensor = cfg.get("solver_regional_spot_current_price_sensor")
    fees_import = price_blend.compute_5min_offset(
        import_history,
        regional_spot_sensor=spot_sensor,
        same_poll_wholesale=same_poll_wholesale,
        wholesale_scale=import_scale,
    )
    fees_export = price_blend.compute_5min_offset(
        export_history,
        regional_spot_sensor=spot_sensor,
        same_poll_wholesale=same_poll_wholesale,
        wholesale_scale=export_scale,
    )
    rrp_now = None
    if spot_sensor:
        value = price_blend.price_now(spot_sensor, fallback=float("nan"), quiet=True)
        rrp_now = None if math.isnan(value) else float(value)
    out = []
    for fees, scale in ((fees_import, import_scale), (fees_export, export_scale)):
        out.append(
            price_consolidation.retail_from_rrp_series(
                grid_times, aemo_forecast, fees, _bucket_5min, scale, rrp_now
            )
        )
    return out[0], out[1], summary


# --- moved from solver_writer.py (#2289 item 6, step 8 part 3): spike override, price event, Amber spot, consolidation, RRP cross-check and tariff attribution ---


def resolve_price_spike_override(
    cfg: dict, import_price_now: float
) -> tuple[float | None, bool]:
    """nimbus issue #567: a real-time "sell into a price spike,
    deliberately, right now" household decision. Returns
    `(effective_override_kw, spike_detected)`:

    - `spike_detected` is True whenever EITHER trigger condition holds
      (a price >= the configured threshold, OR the optional alert
      entity reads "on") -- this is the real-time visibility signal for
      the dashboard's own "$$$" indicator, published REGARDLESS of
      whether the household has armed the override. A household should
      be able to see a spike is happening before deciding to act on it.
    - `effective_override_kw` is the value that actually gets passed
      into BatteryConfig.spike_override_discharge_kw -- None unless
      EVERY one of these holds: the household has explicitly armed the
      override (switch.nimbus_solver_price_spike_override_armed), a
      spike is genuinely detected, and a real rate > 0 is configured
      (number.nimbus_solver_price_spike_discharge_kw). Human stays in
      the loop for the actual discharge decision -- arming is a
      deliberate, explicit action, not inferred from the threshold/
      alert alone.

    nimbus issue #694 (household, 10 Sep, reversing #567's own original
    design): this override now WINS over an active P2P fixed-export
    commitment rather than being exempted from it -- #567 originally
    skipped the override during P2P on the household's own "consistency
    of delivery is itself part of what earns the rate" reasoning, but
    the household's own later, explicit instruction reverses that: "i
    want it to win over p2p cos p2p will be lower... by default....
    otherwise it makes no sense" (a genuine spike is very likely to
    exceed whatever a P2P block pre-committed at, so exempting the
    override during exactly a P2P window meant it could never fire in
    the scenario it's most valuable for). P2P itself still "remains" --
    its committed rate becomes a FLOOR rather than being dropped, so it
    keeps being honestly delivered while export rises above it -- see
    network.py's own build_plan() docstring for that floor mechanism on
    grid_export[0]'s own bounds. This function no longer checks P2P
    state at all; the floor/ceiling interaction lives entirely in
    network.py now.

    threshold<=0 (the default) means "no threshold configured" -- never
    fires on its own, matching every other "0/blank means off" field in
    this project. The alert entity is optional and read defensively
    (unavailable/missing/fetch-failure all fail open to "no alert"),
    same posture as every other optional external entity read in this
    file -- a spike-alert integration having a bad moment must never
    crash a regular solve cycle.
    """
    threshold = _solver_writer()._cfg_num(cfg, "solver_price_spike_threshold", 0.0)
    spike_by_threshold = threshold > 0 and import_price_now >= threshold

    spike_by_alert = False
    alert_entity = cfg.get("solver_price_spike_alert_entity")
    if alert_entity:
        try:
            state = _solver_writer().ha_get(alert_entity)
            spike_by_alert = state.get("state") == "on"
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
            spike_by_alert = False

    spike_detected = spike_by_threshold or spike_by_alert

    armed = bool(cfg.get("solver_price_spike_override_armed"))
    discharge_kw = _solver_writer()._cfg_num(
        cfg, "solver_price_spike_discharge_kw", 0.0
    )
    if not (armed and spike_detected and discharge_kw > 0):
        return None, spike_detected

    return discharge_kw, spike_detected


# nimbus #2026: whether the clamp below has already warned during the current
# episode. Cleared as soon as the override fits again, so the next episode
# warns once more.
_SPIKE_OVERRIDE_CLAMP_WARNED = False


def clamp_spike_override_to_live_ceiling(
    cfg: dict, spike_override_kw: float | None, max_discharge_kw: float
) -> float | None:
    """nimbus #2026 (Mark Purcell's decision on the issue): an armed spike
    rate within the static `solver_max_discharge_kw` but above the live
    ceiling (`solver_max_discharge_live_entity`'s `max`, which can sit
    below the static setting through derating, no configuration error)
    is clamped to that live ceiling, with one warning per episode.
    Rejecting it made every solve fail during the very spike the
    override exists for.

    A rate above the static setting as well is a real misconfiguration,
    so it is passed through unchanged and BatteryConfig still rejects it
    loudly (#567).
    """
    global _SPIKE_OVERRIDE_CLAMP_WARNED
    static_kw = _solver_writer()._cfg_num(
        cfg, "solver_max_discharge_kw", max_discharge_kw
    )
    if (
        spike_override_kw is None
        or spike_override_kw <= max_discharge_kw
        or spike_override_kw > static_kw
    ):
        _SPIKE_OVERRIDE_CLAMP_WARNED = False
        return spike_override_kw
    if not _SPIKE_OVERRIDE_CLAMP_WARNED:
        _SPIKE_OVERRIDE_CLAMP_WARNED = True
        solver_shared._LOGGER.warning(
            "Nimbus Solver: the price spike discharge rate (%.2f kW) is above "
            "the live max-discharge ceiling (%.2f kW from %s) -- discharging "
            "at the live ceiling instead. Logged once until the rate fits "
            "again.",
            spike_override_kw,
            max_discharge_kw,
            cfg.get("solver_max_discharge_live_entity"),
        )
    return max_discharge_kw


def resolve_price_event_delta(
    cfg: dict, grid_times: list[datetime]
) -> list[float] | None:
    """The per-period $/kWh delta from an armed price-event test sensor
    (nimbus issue #1213, Mark Purcell), or None when the feature is off.

    Simulates a real NEM Market Price Cap (LOR2/LOR3, $23.20/kWh) or a
    Minimum System Load negative-price floor against the ACTUAL live
    forecast, so dispatch behaviour around such an event can be watched
    BEFORE one happens -- does the battery pre-charge ahead of the window,
    does it recognise the extreme period is coming and revise earlier
    commitments -- rather than only being explained afterwards.

    Returns None (not a list of zeros) when the feature contributes
    nothing, so the caller can skip the arithmetic entirely and a reader
    of the log can tell "off" from "armed but flat".

    Off means any of: the arm switch is off, no sensor configured, the
    sensor missing or carrying no usable `forecast`, or every resolved
    period landing at exactly 0.0.

    **Step semantics, and the one place this deliberately differs from
    every other price path here.** The `forecast` shape and the
    hold-the-most-recent-point lookup are exactly
    `resample_generic_price_forecast()`'s, which is the convention this
    repo already proved and which #1213 asks for by name. But that
    function resolves a PRICE, where holding the last value forward is
    correct. This resolves a DELTA, where holding forward means **an event
    that never ends**:

    * Before the first point -> **0.0**, never backfilled. A price series
      backfills because a price always exists; a delta before its own
      first step-point is simply absent.
    * After the last point -> held forward, same as any step function --
      which is why the household must emit an explicit `0` point at the
      end of each window to close it. That is the single real footgun in
      this feature, so a non-zero final point gets a **WARNING naming it**
      rather than being silently obeyed forever. Obeying it is still the
      correct reading of a step function; being quiet about it is not.

    Applied SYMMETRICALLY by the caller -- the same delta added to both
    import and export -- because a real wholesale price event moves the
    whole market, not one side of it.
    """
    if not bool(cfg.get("solver_price_event_enabled")):
        return None
    entity_id = cfg.get("solver_price_event_sensor")
    if not entity_id or not grid_times:
        return None

    state = _solver_writer().ha_get(entity_id)
    if not isinstance(state, dict):
        solver_shared._LOGGER.warning(
            "Nimbus #1213: price-event simulation is ARMED but its sensor "
            "%r could not be read -- no delta applied this cycle. Either "
            "point solver_price_event_sensor at a real sensor or turn "
            "switch.nimbus_solver_price_event_enabled off.",
            entity_id,
        )
        return None
    forecast = (state.get("attributes") or {}).get("forecast")
    if not isinstance(forecast, list) or not forecast:
        solver_shared._LOGGER.warning(
            "Nimbus #1213: price-event simulation is ARMED but %s carries "
            "no usable `forecast` attribute (expected a list of "
            '{"time": <iso>, "value": <$/kWh>} step points) -- no delta '
            "applied this cycle.",
            entity_id,
        )
        return None

    points: list[tuple[datetime, float]] = []
    for row in forecast:
        if not isinstance(row, dict):
            continue
        raw_time = row.get("time")
        raw_value = row.get("value")
        if raw_time is None or raw_value is None:
            continue
        try:
            when = _solver_writer().parse_iso(raw_time)
            value = float(raw_value)
        except (TypeError, ValueError, AttributeError):
            # One unparseable row must not discard a whole armed window.
            continue
        points.append((when, value))
    if not points:
        solver_shared._LOGGER.warning(
            "Nimbus #1213: price-event simulation is ARMED but none of "
            "%s's %d forecast rows parsed into a (time, value) step point "
            "-- no delta applied this cycle.",
            entity_id,
            len(forecast),
        )
        return None
    points.sort(key=lambda p: p[0])

    # The footgun, named rather than obeyed silently. A step function's
    # last value holds forever, so a window that does not end with an
    # explicit 0 point distorts every future solve, not just the test.
    if abs(points[-1][1]) > 0.0:
        solver_shared._LOGGER.warning(
            "Nimbus #1213: %s's LAST step point is %+.4f $/kWh at %s, not "
            "0. A step value holds forward indefinitely, so this event "
            "never closes and will keep shifting BOTH import and export "
            'prices on every future solve. Add a trailing {"time": '
            '<window end>, "value": 0} point to close the window.',
            entity_id,
            points[-1][1],
            points[-1][0].isoformat(),
        )

    deltas: list[float] = []
    idx = 0
    for when in grid_times:
        # Advance to the last point at or before this period.
        while idx + 1 < len(points) and points[idx + 1][0] <= when:
            idx += 1
        if points[idx][0] > when:
            # Before the first step point -- absent, not backfilled.
            deltas.append(0.0)
        else:
            deltas.append(points[idx][1])

    if not any(abs(d) > 0.0 for d in deltas):
        solver_shared._LOGGER.debug(
            "Nimbus #1213: price-event simulation armed, but %s's own "
            "windows do not overlap this solve's horizon -- no delta "
            "applied (a clean no-op, not a fault).",
            entity_id,
        )
        return None

    affected = sum(1 for d in deltas if abs(d) > 0.0)
    solver_shared._LOGGER.warning(
        "Nimbus #1213: price-event simulation ACTIVE from %s -- %+.4f to "
        "%+.4f $/kWh applied to BOTH import and export across %d of %d "
        "periods. This is a what-if test, not a real market price: the "
        "plan, the offer curve and every published cost this cycle reflect "
        "the simulated event. Turn "
        "switch.nimbus_solver_price_event_enabled off to return to real "
        "prices.",
        entity_id,
        min(deltas),
        max(deltas),
        affected,
        len(deltas),
    )
    return deltas


# compute_5min_offset() reads 5 days of retail history; keep as many.
_AMBER_SPOT_KEEP_INTERVALS = 5 * 288


_AMBER_SPOT_CACHE: dict[str, float] | None = None


def _load_amber_spot() -> dict[str, float]:
    global _AMBER_SPOT_CACHE
    if _AMBER_SPOT_CACHE is None:
        try:
            with open(_solver_writer().AMBER_SPOT_PATH, encoding="utf-8") as f:
                data = json.load(f)
            _AMBER_SPOT_CACHE = {
                str(k): float(v)
                for k, v in (data.items() if isinstance(data, dict) else [])
            }
        except (OSError, ValueError, TypeError):
            _AMBER_SPOT_CACHE = {}
    return _AMBER_SPOT_CACHE


def collect_amber_spot(entity_ids, now: datetime) -> dict[int, tuple[datetime, float]]:
    """nimbus #1698: record the in-progress interval's Amber Express wholesale
    (`spot_per_kwh` / 1.10) from the first of `entity_ids` that carries one,
    and return every interval collected, as compute_5min_offset()'s
    `same_poll_wholesale`. Empty for an install without Amber Express. Never
    raises: a failed read or write only means fewer pairs."""
    store = _load_amber_spot()
    for entity_id in entity_ids:
        if not entity_id:
            continue
        try:
            detailed = (_solver_writer().ha_get(entity_id).get("attributes") or {}).get(
                price_intervals.AMBER_EXPRESS_DETAILED_KEY
            )
        except Exception as e:  # noqa: BLE001 - a missing sensor is not an error here
            solver_shared._LOGGER.debug(
                "Nimbus #1698: no Amber spot from %s: %s", entity_id, e
            )
            continue
        found = price_intervals.amber_spot_in_progress(detailed, now)
        if found is None:
            continue
        start, spot = found
        key = str(int(start.timestamp()) // 300)
        if store.get(key) != spot:
            store[key] = spot
            newest = int(key)
            for old in [
                k for k in store if int(k) <= newest - _AMBER_SPOT_KEEP_INTERVALS
            ]:
                del store[old]
            try:
                with open(_solver_writer().AMBER_SPOT_PATH, "w", encoding="utf-8") as f:
                    json.dump(store, f)
            except OSError:
                pass
        break
    return {
        int(k): (datetime.fromtimestamp(int(k) * 300, UTC), v) for k, v in store.items()
    }


_PRICE_CONSOLIDATION_CACHE: dict[str, price_consolidation.SourceErrorTracker] | None = (
    None
)


_PRICE_CONSOLIDATION_FAILED_LOGGED = False


def _load_price_trackers() -> dict[str, price_consolidation.SourceErrorTracker]:
    global _PRICE_CONSOLIDATION_CACHE
    if _PRICE_CONSOLIDATION_CACHE is None:
        data: dict = {}
        try:
            with open(_solver_writer().PRICE_CONSOLIDATION_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError, TypeError):
            data = {}
        _PRICE_CONSOLIDATION_CACHE = {
            side: price_consolidation.SourceErrorTracker.from_dict(data.get(side))
            for side in ("import", "export")
        }
    return _PRICE_CONSOLIDATION_CACHE


def _save_price_trackers(
    trackers: dict[str, price_consolidation.SourceErrorTracker],
) -> None:
    try:
        with open(
            _solver_writer().PRICE_CONSOLIDATION_PATH, "w", encoding="utf-8"
        ) as f:
            json.dump({side: t.to_dict() for side, t in trackers.items()}, f)
    except OSError:
        pass


def _follow_price_sources(trackers: dict, cfg: dict) -> bool:
    """Each side's tracker forgets a slot whose sensor has changed, so the new
    sensor starts with no score or offset. True when any slot was forgotten."""
    forgotten = False
    for side, tracker in trackers.items():
        current = {
            "primary": cfg.get(f"solver_{side}_price_sensor"),
            "secondary_2": cfg.get(f"solver_{side}_price_sensor_2"),
            "secondary_3": cfg.get(f"solver_{side}_price_sensor_3"),
        }
        forgotten = bool(tracker.follow_sources(current)) or forgotten
    return forgotten


def _consolidation_sources(
    cfg: dict,
    grid_times: list[datetime],
    side: str,
    primary: list[float],
    prices: object,
) -> dict[str, list[float | None]]:
    """Each price source's own $/kWh series on the plan grid for `side`
    ("import"/"export"), None where that source has no real coverage. The
    primary is its pre-blend series."""
    primary_mask = getattr(prices, f"{side}_real_mask", None)
    secondary_keys = (f"solver_{side}_price_sensor_2", f"solver_{side}_price_sensor_3")
    aemo_series = getattr(prices, f"aemo_{side}_series", None)
    mask = primary_mask if primary_mask is not None else [True] * len(primary)
    out: dict[str, list[float | None]] = {
        "primary": [
            float(v) if (i < len(mask) and mask[i]) else None
            for i, v in enumerate(primary)
        ]
    }
    for n, key in enumerate(secondary_keys, start=2):
        entity_id = cfg.get(key)
        if not entity_id:
            continue
        r = price_blend.resample_generic_price_forecast_with_coverage(
            entity_id, grid_times
        )
        if r is not None:
            fc, m = r
            out[f"secondary_{n}"] = [
                float(v) if m[i] else None for i, v in enumerate(fc)
            ]
    if aemo_series is not None and any(v is not None for v in aemo_series):
        out[getattr(prices, "aemo_source", "aemo_markup")] = list(aemo_series)
    return out


def _loss_factor(prices: object) -> dict:
    """nimbus #1786: the site's loss factors for the plan's attribute, when its
    codes are configured; empty otherwise."""
    lf = getattr(prices, "loss_factor", None)
    return {"loss_factor": lf} if lf else {}


# nimbus #2190: kept across cycles, so a source that stays put while two
# others move is seen.
_RRP_STALE_WATCH = rrp_crosscheck.StaleWatch()


def rrp_crosscheck_report(cfg: dict, now: datetime) -> dict | None:
    """nimbus #2190 (Mark Purcell's design): every price source the
    household lists for comparison, plus the regional AEMO sensors, brought
    back to the RRP and compared per interval, current and future. A report
    only: no price, plan or dispatch reads it. None when fewer than two
    sources are configured."""
    entity_ids = [
        e
        for e in (
            cfg.get("solver_regional_spot_current_price_sensor"),
            cfg.get("solver_regional_spot_forecast_sensor"),
            *(cfg.get("solver_price_comparison_sensors") or []),
        )
        if e
    ]
    entity_ids = list(dict.fromkeys(entity_ids))
    if len(entity_ids) < 2:
        return None
    tni, dlf_code = _loss_factor_codes(_solver_writer(), cfg)
    site_factor = loss_factors.site_factor(tni, dlf_code, now).value
    reads = []
    ages: dict[str, str | None] = {}
    for entity_id in entity_ids:
        try:
            st = _solver_writer().ha_get(entity_id)
        except Exception as e:  # noqa: BLE001 - an unreadable source is reported, not fatal
            solver_shared._LOGGER.debug("Nimbus #2190: %s unreadable: %s", entity_id, e)
            st = {}
        ages[entity_id] = rrp_crosscheck.as_of(st)
        reads.append(
            rrp_crosscheck.read_source(
                entity_id, st.get("state"), st.get("attributes"), site_factor, now
            )
        )
    return rrp_crosscheck.report(
        reads, _RRP_STALE_WATCH.update(reads), site_factor, ages
    )


def apply_price_consolidation(
    cfg: dict,
    now: datetime,
    grid_times: list[datetime],
    period_hours: list[float],
    n_settled_periods: int,
    prices: object,
    spot_import_source: list[float],
    spot_export_source: list[float],
    settled_import: float | None,
    settled_export: float | None,
    spot_import_raw: list[float],
    spot_export: list[float],
    import_price_source: list[str],
    export_price_source: list[str],
) -> dict | None:
    """nimbus #1786: both sides' consolidated series and running error; live, the
    future scheduling prices are replaced (never the settled block). Never raises."""
    global _PRICE_CONSOLIDATION_FAILED_LOGGED
    try:
        pc = price_consolidation
        ctx = (now, grid_times, period_hours, n_settled_periods)
        live = bool(cfg.get("solver_price_consolidation_live", False))
        trackers = _load_price_trackers()
        spike = _solver_writer()._cfg_num(
            cfg, "solver_price_spike_threshold", 0.0
        )  # #1786: keep spikes
        result: dict = {"mode": "live" if live else "shadow", **_loss_factor(prices)}
        scored = _follow_price_sources(trackers, cfg)  # True: save below
        series_by_side: dict[str, dict[str, list[float | None]]] = {}
        for side, primary, settled in (
            ("import", spot_import_source, settled_import),
            ("export", spot_export_source, settled_export),
        ):
            series = _consolidation_sources(cfg, grid_times, side, primary, prices)
            series_by_side[side] = series
            out = pc.consolidate_side(trackers[side], series, *ctx, settled, spike)
            values, result[side], side_scored = out
            result[f"consolidated_{side}_price"] = values
            scored = scored or side_scored
        result["error_history"] = _record_price_errors(
            cfg, ctx, series_by_side, result, settled_import, settled_export
        )
        if live:
            for side, target, labels in (
                ("import", spot_import_raw, import_price_source),
                ("export", spot_export, export_price_source),
            ):
                pc.replace_future(
                    result[f"consolidated_{side}_price"],
                    target,
                    labels,
                    n_settled_periods,
                )
        if scored:
            _save_price_trackers(trackers)
        return result
    except Exception as e:  # noqa: BLE001 -- a shadow measurement must never break a solve
        if not _PRICE_CONSOLIDATION_FAILED_LOGGED:
            _PRICE_CONSOLIDATION_FAILED_LOGGED = True
            solver_shared._LOGGER.warning(
                "Nimbus #1786: price consolidation failed: %s", e
            )
        return None


def _with_consolidated(
    series_by_side: dict[str, dict[str, list[float | None]]], result: dict
) -> dict[str, dict[str, list[float | None]]]:
    """#1786: each side's sources plus the consolidated forecast, so the
    shadow history scores it beside them and answers "consolidated or the
    primary alone?" directly, by lead, before anyone turns it on."""
    return {
        side: {
            **series,
            **(
                {"consolidated": result[f"consolidated_{side}_price"]}
                if result.get(f"consolidated_{side}_price") is not None
                else {}
            ),
        }
        for side, series in series_by_side.items()
    }


def _record_price_errors(
    cfg: dict,
    ctx: tuple[datetime, Sequence[datetime], Sequence[float], int],
    series_by_side: dict[str, dict[str, list[float | None]]],
    result: dict,
    settled_import: float | None,
    settled_export: float | None,
) -> dict:
    """nimbus #1797: the consolidation's own series and settled prices,
    kept as signed pairs for price scenarios (price_error_history.py).
    Recorded only; nothing reads it yet. Never raises."""
    return price_error_history.update(
        _solver_writer(),
        cfg,
        *ctx,
        _with_consolidated(series_by_side, result),
        {"import": settled_import, "export": settled_export},
    )


def compute_tariff_attributed_cost(
    adequacy_loads: list,
    grid_to_load_kw: NDArray[np.float64],
    whole_house_load_kw: NDArray[np.float64] | list[float],
    import_price: NDArray[np.float64] | list[float],
    period_hours: NDArray[np.float64] | list[float],
) -> dict[str, float]:
    """Nimbus issue #483 (sub-issue 7 of #476), item 2 -- "the device's
    share of the plan's actual grid flow, pro-rata by the device's power
    in each period." Item 1 (marginal_cost, the LP's own shadow-price-
    based figure) shipped in #483's first pass (v0.94.271) inside
    network.py itself, since it's a genuine LP-native quantity (reads
    the solve's own duals). This one is deliberately NOT computed
    there -- it's a pure post-processing ATTRIBUTION over the already-
    solved flow decomposition (_flow_decomposition(), a solver_writer.py-
    level function with no LP/dual concept at all), not a property the
    LP solve itself produces.

    Same "Grid -> Load = -import_price (pure cost)" pricing convention
    _compute_flow_economics()'s own shadow-price table already
    documents -- this attributes exactly that flow's real dollar cost,
    split pro-rata across every configured adequacy (deferrable) load
    by its own share of the whole house's real per-period load. A
    period with zero whole-house load has nothing to attribute (0.0
    contribution, not a division-by-zero crash).

    Honest scope, stated directly rather than left implicit: this
    covers every ADEQUACY (deferrable) load, matching marginal_cost's
    own existing scope -- sheddable/thermal loads don't get this
    figure in this pass, same "kept consistent with what's already
    shipped, not silently expanded" reasoning #774 itself used for
    kind=thermal's own reset of plan_marginal_cost/plan_profit_horizon
    to None.

    Real-world reconciliation caveat, spelled out per #483's own
    acceptance criterion ("sums to the plan's grid cost within
    rounding"): this holds EXACTLY only when grid_to_load accounts for
    the entire grid_net cost -- i.e. no grid-sourced battery charging,
    no export, and every real watt of whole-house load belongs to a
    configured adequacy load (the clean synthetic-day shape the issue's
    own Acceptance section describes). On a real household with
    uncontrolled background circuits (lighting, fridge, etc. -- not
    configured as a Controllable Load at all) or any grid export/
    charging in the same window, the sum of every device's own
    attributed cost is a real, honest PARTIAL answer (exactly what the
    tracked devices cost), not a claim that it reconciles to the full
    grid_net figure -- the untracked remainder is real cost this
    function has no device to attribute it to.
    """
    result: dict[str, float] = {}
    n = min(
        len(grid_to_load_kw),
        len(whole_house_load_kw),
        len(import_price),
        len(period_hours),
    )
    for al in adequacy_loads:
        if al.subentry_id is None:
            continue
        power = np.asarray(al.power_kw, dtype=np.float64)
        cost = 0.0
        for t in range(min(n, len(power))):
            house_load = float(whole_house_load_kw[t])
            if house_load <= 1e-9:
                continue
            share = float(power[t]) / house_load
            device_grid_to_load_kw = share * float(grid_to_load_kw[t])
            cost += (
                device_grid_to_load_kw * float(import_price[t]) * float(period_hours[t])
            )
        result[al.subentry_id] = cost
    return result
