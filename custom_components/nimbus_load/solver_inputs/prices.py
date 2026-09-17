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

from dataclasses import dataclass
from datetime import timedelta


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
        aemo_forecast = sw.fetch_aemo_forecast(
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
        import_history = sw.fetch_price_history(cfg["solver_import_price_sensor"])
        export_history = sw.fetch_price_history(cfg["solver_export_price_sensor"])
        import_offset_by_5min = sw.compute_5min_offset(
            import_history,
            regional_spot_sensor=cfg.get("solver_regional_spot_current_price_sensor"),
        )
        export_offset_by_5min = sw.compute_5min_offset(
            export_history,
            regional_spot_sensor=cfg.get("solver_regional_spot_current_price_sensor"),
        )
        # Real empirical price bands for price_risk_aversion (2026-08-21,
        # task #128 -- see compute_price_percentile_band()'s own docstring).
        # A SEPARATE, longer (14-day, vs the 5-day history already fetched
        # above for compute_5min_offset's own mean-offset use) fetch --
        # percentile estimation genuinely benefits from more real samples
        # per bucket than a mean does, and this data has been live and
        # recorder-tracked since well before 14 days ago.
        import_price_upper_band = sw.compute_price_percentile_band(
            sw.fetch_price_history(cfg["solver_import_price_sensor"], days=14), 90.0
        )
        export_price_lower_band = sw.compute_price_percentile_band(
            sw.fetch_price_history(cfg["solver_export_price_sensor"], days=14), 10.0
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
        spot_import_raw, import_real_mask = sw.resample_price_with_extrapolation(
            lv_price_fc, import_key, grid_times, aemo_forecast, import_offset_by_5min
        )
        spot_export, export_real_mask = sw.resample_price_with_extrapolation(
            lv_price_fc,
            export_key,
            grid_times,
            aemo_forecast,
            export_offset_by_5min,
        )
        p2p_export = sw.resample_real_p2p_rate(
            grid_times, cfg.get("solver_p2p_matched_rate_forecast_sensor")
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
            aemo_p5min_check = sw.check_aemo_p5min_disagreement(
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
            sw._LOGGER.warning(
                "Nimbus #452: AEMO P5MIN disagreement check failed: %s", e
            )
            aemo_p5min_check = None
        if aemo_p5min_check is not None and aemo_p5min_check["flagged"]:
            sw._warn_aemo_p5min_disagreement_once(grid_times[0], aemo_p5min_check)

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
            sw._LOGGER.warning(
                "Nimbus #452: AEMO forecast-vs-actuals check failed: %s", e
            )
            aemo_fc_check = None
        if aemo_fc_check is not None and aemo_fc_check["flagged"]:
            sw._warn_aemo_forecast_vs_actuals_once(grid_times[0], aemo_fc_check)

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
        match_fraction = sw.p2p_match_fraction(settlement_sensor=p2p_settlement_sensor)
        p2p_recent_volume_kwh = sw.p2p_recent_avg_volume_kwh(
            settlement_sensor=p2p_settlement_sensor
        )
        # Fix 2026-09-05 (mirrors the identical fix in the sibling
        # 116KAT-HA-AI/scripts/nimbus_solver_forecast_writer.py -- the
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
        _import_fc = sw.resample_generic_price_forecast_with_coverage(
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
                sw.safe_num(cfg["solver_import_price_sensor"])
            ] * n_periods
            import_real_mask = [True] * n_periods
        _export_fc = sw.resample_generic_price_forecast_with_coverage(
            cfg["solver_export_price_sensor"], grid_times
        )
        if _export_fc is not None:
            spot_export, export_real_mask = _export_fc
        else:
            spot_export = [sw.safe_num(cfg["solver_export_price_sensor"])] * n_periods
            export_real_mask = [True] * n_periods
        match_fraction = 0.0
        # Manual, static P2P bonus from the config-flow's own optional
        # block (both default to 0.0 -- a full no-op -- if the household
        # doesn't have any P2P/community-trading scheme at all).
        p2p_recent_volume_kwh = sw._cfg_num(cfg, "solver_p2p_bonus_volume_kwh", 0.0)
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
    )
