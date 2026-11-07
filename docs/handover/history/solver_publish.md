# solver_publish.py: comment history

Moved out of `custom_components/nimbus_load/solver_publish.py` by #2289 item 7 (the method agreed with Mark Purcell, 7 Nov 2026), verbatim, keyed by the code line each block sat above. Line numbers are the file's at `main` before the move.

## Line 166: above `"signal_role": "other",`

```text
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
```

## Line 2010: above `weight_note = (`

```text
        # nimbus issue #356 (Mark Purcell): this is genuinely NOT the same
        # thing as a real infeasible model -- HiGHS gave up/hit a limit
        # without ever determining feasibility either way (see network.py's
        # own Plan.raw_status docstring). Named explicitly here so an
        # operator sees "the solver failed, here's HiGHS's own reason"
        # rather than being sent hunting for a modeling/config problem that
        # doesn't exist.
        #
        # nimbus issue #757: and do not publish it. _infeasible_plan()
        # builds a well-formed but ENTIRELY ZERO-FILLED Plan for any
        # non-optimal solve -- no batteries, no loads, every array
        # zeros(n). Publishing that overwrites a perfectly good live plan
        # with a plausible-looking blank one: sensor.nimbus_solver_battery_
        # forecast reads "0 kW everywhere, batteries=[]", which is
        # indistinguishable on a dashboard from a real solve that genuinely
        # decided to do nothing.
        #
        # That is not hypothetical. #757 ("battery participant silently
        # excluded from the solve") sat open through TEN separate
        # investigations that disagreed with each other, because a live
        # trace showed build_plan() returning batteries=['home','Test EV']
        # with status='optimal' on every cycle while 7 of 10 actual
        # publishes carried batteries=[] with status='error'. The
        # "exclusion" was never an exclusion -- it was failed solves
        # overwriting good ones. Whoever looked next saw whichever write
        # landed last.
        #
        # Skipping the publish is the honest outcome, not a silent one:
        # _NimbusSolverPushSensor.available goes False once
        # _STALE_AFTER_SECONDS (5 min) passes with no fresh push, so a
        # persistently failing solver surfaces as "unavailable" -- which is
        # exactly what it is -- rather than as a confident plan of zeros.
        # A transient single failure keeps the last good plan for under
        # five minutes, which is strictly better than replacing it with
        # nothing.
        #
        # Deliberately NOT extended to "infeasible". That one is a real
        # modelling ANSWER (HiGHS proved no feasible dispatch exists for
        # the constraints given), the household needs to see it, and #773's
        # own fallback path depends on it being published. "error" is the
        # only status where the solver never determined anything at all.
        # nimbus issue #773: the elapsed time is logged HERE because
        # this function returns before ever publishing it. #757's own
        # guard above is right to skip the publish, but solve_seconds
        # reaches sensor.nimbus_solver_solve_seconds ~750 lines below
        # this point -- so from v0.94.301 onward that sensor only ever
        # reported SUCCESSFUL solves, and the duration of a failing one
        # became invisible. That is exactly the number #773 needs: on a
        # real install these failures take ~60s (the per-call limit)
        # against a 0.6s healthy cycle, and a guard that hides the
        # symptom it protects against is a bad trade. Logged, not
        # published -- no plan is written, so nothing about the guard
        # itself changes.
        # nimbus issue #1179: name the calibration fallback IN THIS LINE.
        #
        # The 2026-09-20 episode produced 153 failed cycles in 2.7h and 44
        # "no blend weight preserves primary cost ... using minimum weight
        # 1.00e-12" warnings, 37 of which shared a timestamp-SECOND with a
        # failure. That is a correlation by clock, not by cycle, and it is
        # precisely why the blend-collapse hypothesis could be neither
        # confirmed nor dropped: blend warnings accompanied only 44 of the
        # 153, which is equally consistent with "one of several routes to
        # the same failure" and with "the warning is logged on a subset of
        # the cycles that take it".
        #
        # `Plan.calibration_min_weight_fallback` is threaded from
        # LPResult (see both dataclasses' own fields) and is set on
        # SUCCESSFUL solves too, so the next episode answers the question
        # with a base rate rather than with failures alone.
        # nimbus issue #1179, second half: name WHY the fallback fired, not
        # just that it did. The two reasons point in opposite causal
        # directions, and #1229's bool alone cannot separate them:
        #
        #   probe_not_optimal  -- the calibrator's own bracket probes did not
        #                         solve, so the model was already failing
        #                         BEFORE any weight was chosen. The blend
        #                         collapse is a symptom of this failure, not
        #                         its cause, and this line and the blend
        #                         warning are two views of one event.
        #   cost_not_preserved -- both probes solved and no weight in the
        #                         bracket held the primary cost. The only case
        #                         in which #1179's original hypothesis (that
        #                         handing HiGHS 1e-12 is itself what breaks the
        #                         solve) is even available.
        #
        # That distinction is what the 2026-09-20 episode could not answer:
        # blend warnings accompanied only 44 of 153 failures, which is equally
        # consistent with "one of several routes to the same failure" and with
        # "the warning is logged on a subset of the cycles that take it".
        # `probe_not_optimal` would explain the other 109 directly -- they
        # failed at a phase that never reaches calibration at all.
        # nimbus issue #1291 (Mark Purcell, IV&V #1289): report the weight
        # the calibrator actually settled on, not just whether it fell back.
        # #1179 put that number on LPResult for exactly this line and it
        # never arrived, so this warning has been describing the outcome
        # while withholding the one value that distinguishes "collapsed to
        # the floor" from "settled somewhere sane" -- which is the difference
        # between a candidate cause and a healthy cycle.
```

## Line 2137: above `solver_shared._LOGGER.warning(`

```text
        # nimbus issue #1179: HiGHS's own numbers, in THIS line.
        #
        # The 2026-09-20 episode published 153 of these lines carrying
        # `raw_status="Unknown"` and an elapsed time, and nothing else.
        # "Unknown" is HiGHS declining to say what happened, so a line that
        # stops there records only that a failure occurred -- which is what
        # made the episode undiagnosable four days later when it had already
        # stopped recurring.
        #
        # `plan.iterations` is the number that decides #1179's own open
        # question. solver/lp.py's own comment above `_SLOW_LP_CALL_SECONDS`
        # states it: a stall with a huge iteration count is degeneracy or
        # cycling, a stall with a small one "is stuck somewhere that is not
        # the simplex loop at all -- presolve, a MIP branch-and-bound tree,
        # or numerical trouble", and "those two answers point at completely
        # different fixes". A 0.1s failure against a 1.16s healthy solve
        # predicts ~0 iterations, i.e. the model rejected before the simplex
        # loop; the prediction has never been checkable because this line
        # never printed the count, although `_infeasible_plan()` has carried
        # it onto the Plan since #356.
        #
        # `%s` for the elapsed time, not `%.1f`: at 0.1s a 1-decimal format
        # is at the edge of its own resolution, and this issue's whole
        # signature is how SMALL that number is.
```

## Line 2190: above `horizon_days = sum(period_hours_arr) / 24.0`

```text
    # Real fixed daily charges (Network Access + LV Fee), reported
    # honestly alongside the LP's own total_cost -- NOT fed into the LP
    # itself (a flat, dispatch-independent cost can't change an optimal
    # LP decision, only shift the objective by a constant, so there's
    # nothing for the solver to do with it). Prorated to this horizon's
    # own real span (sum of period_hours_arr, NOT n_periods*a-fixed-width
    # -- the tiered grid has two different period widths) / 24, not just
    # added flat.
    #
    # nimbus issue #348 (Mark Purcell, fixed 2026-09-04): a real wizard
    # field now (number.nimbus_solver_fixed_daily_charge), not a module
    # constant applied to every install regardless of their own real
    # retailer's actual daily supply charge. 1.95 as the literal default
    # matches const.py's own DEFAULT_SOLVER_FIXED_DAILY_CHARGE -- byte-
    # identical behaviour for every existing install until this field is
    # explicitly changed.
```

## Line 2266: above `(`

```text
    # DEFENSIVE SAFETY NET (2026-08-22) -- this file's own real, found
    # root cause. THIS module IS the native in-process solve path
    # (solver_runtime.py imports it exactly once, at container startup,
    # via a lazy module-level singleton that's never re-imported for the
    # life of the process) -- see the sibling standalone script's own
    # matching comment (the reference household's
    # nimbus_solver_forecast_writer.py) for the full incident writeup.
    # Short version: this container's most recent restart (2026-08-22
    # ~15:25 AEST, to deploy the native runtime itself) happened nearly 2
    # hours BEFORE the real network.py fix landed (commit 3f90c1f,
    # 17:20:03) -- so THIS path, specifically, has been the one silently
    # running the old, unfixed battery_charge bound every minute since,
    # racing the standalone cron writer's own always-current code. This
    # clamp stays in place permanently regardless of cause, as a genuine
    # backstop -- see the sibling file's own comment for exactly what it
    # corrects and why. A future container restart flushes this module's
    # own stale in-memory code (there's no other way to force a re-import
    # here); until then, disabling the Nimbus integration was used as an
    # immediate same-night mitigation, since that correctly cancels this
    # module's own timer (entry.async_on_unload, __init__.py) without a
    # restart.
    # nimbus issue #923: `plan.batteries[0]` is the battery the LP gate was
    # applied to; the fleet aggregate is not. See the function's docstring.
```

## Line 2338: above `flow_decomp = _flow_decomposition_counted(`

```text
    # Real per-period seven-flow decomposition + shadow prices + PV/
    # Battery/Combined savings model (2026-08-28, nimbus issue #264, Mark
    # Purcell) -- see _flow_decomposition()'s and _compute_flow_economics()'s
    # own module-level docstrings for the full rationale, including why
    # this deliberately extends the issue's own sketch (separate pre-net
    # charge/discharge arrays instead of a single net_battery_kw, and a
    # real cross-period WACOG cost basis instead of a same-period lookup).
    # Byte-identical, additive to the existing dispatch_source_a/b fields
    # above -- nothing already published changes shape or value.
    # Household, 12 Oct 2026: the flows are split from the solar the plan
    # actually counted on (`effective_solar_kw`, the forecast after the
    # "Solar forecast risk" lean), not the raw forecast. The difference is
    # published as its own `pv_risk_margin` instead of landing in
    # pv_to_curtailment: at 0.25 the reference household's plan showed
    # 7.4 kWh of "curtailed" solar on 13 Oct that it never curtailed.
```

## Line 2389: above `forecast = [`

```text
    # Real per-period price/load/solar/net-cost fields added (2026-08-17,
    # direct ask: "still waiting for haeo like markdown table where I
    # can see forecasted costs fit load solar and soc% and period net")
    # -- previously ONLY battery/SoC/grid kW were pushed; a real forecast
    # TABLE needs the same real inputs the LP itself actually solved
    # against, not just its output. import_price/export_price/
    # export_bonus_price are all already computed above (this writer's
    # own real inputs, not re-derived); load_kw/solar_kw are the same
    # real per-period arrays already fed to LoadConfig/SolarConfig.
    # net_cost is the real grid-side cash flow for that period (import
    # cost minus base export revenue minus P2P bonus revenue) --
    # deliberately NOT including battery charge/discharge wear cost,
    # matching this project's own established "Net $" convention from
    # the HAEO forecast table this mirrors.
```

## Line 2474: above `"import_price_raw": round(spot_import_source[i], 4),`

```text
            # The raw commodity/spot price ALONE, before network TOU +
            # certificates are added on (2026-08-22, direct household
            # ask, after the real 8.4 vs 7.1c investigation: "normal
            # dumb folk user would look for buy price ot be what
            # localvolts_cost_flexup is... they would not get why you
            # added up costs to it... so maybe the table needs fees
            # column next ot cost?"). import_price above is UNCHANGED --
            # still the full landed cost, still what net_cost/the LP
            # itself actually uses -- this is purely an additional,
            # honest field so a dashboard can show Buy¢ = this (matches
            # what LocalVolts' own app shows) and Fees¢ = import_price
            # minus this, instead of one opaque combined number nobody
            # outside this codebase could verify against anything real.
            # True pre-blend source pass-through (2026-08-27, nimbus repo
            # issue #216, Mark Purcell) -- what the configured
            # solver_import_price_sensor itself said, before
            # blend_price_with_secondary_sources() folds in any
            # configured _sensor_2/_sensor_3. Previously this read
            # spot_import_raw AFTER blending, so on any install with a
            # secondary source configured, import_price_raw silently
            # stopped being a real "before any transformation" probe --
            # exactly Mark's own found-live gap ("on Config B... import_
            # price and import_price_raw are byte-identical... isn't
            # currently serving as a before-any-transformation
            # diagnostic"). On a single-source install (no _sensor_2/_3
            # configured) this is unchanged, byte-identical to before.
```

## Line 2780: above `"signal_role": "battery",`

```text
            # 2026-08-25, nimbus issue #189 (Mark Purcell, real-install
            # reproducer -- the follow-up to #187): this is the
            # "flagship diagnostic sensor" a Nimbus dashboard is most
            # likely to be built against, and it's a genuinely different
            # class again from both NimbusForecastSensor (fixed in
            # v0.89.1) and _NimbusSolverPushSensor's OTHER instance
            # (household load total, fixed in v0.92.1) -- this is the
            # Solver's own LP-derived dispatch plan, not a mirror or a
            # sum of upstream forecasts. "battery" (SIGNAL_ROLE_BATTERY's
            # own string value) is definitionally this entity's role.
            # source_sensor is the one real, measured entity this whole
            # plan is actually built around -- the live SoC reading the
            # LP solves forward from -- since there's no single upstream
            # "battery forecast" sensor the way load has
            # solver_load_forecast_sensor.
```

## Line 2942: above `"price_blend_algorithm": "primary_preferring_fallback_to_secondary_mean",`

```text
            # Nimbus issue #237 (Mark Purcell, 2026-08-27): "confirm and
            # expose the price-blend algorithm". Originally an unweighted
            # np.mean() across every source with real coverage at a
            # period, primary included -- confirmed live against Mark's
            # #236 repro (a genuinely-real Amber Express value averaged
            # 50/50 against a genuinely-real but far larger QLD1 PD7DAY
            # wholesale forecast, inverting the import/export price
            # relationship and causing an unphysical simultaneous-
            # import-and-export LP plan). Fixed same-day in #239
            # (primary-preferring): the primary now wins UNBLENDED
            # whenever it has real coverage, full stop -- a secondary
            # only ever fills a period where the primary itself lacks
            # real coverage yet (its one legitimate job, extending the
            # horizon past Amber's own ~24h reach). See
            # blend_price_with_secondary_sources()'s own docstring for
            # the full market-structure argument (import/export both
            # derive from the SAME underlying AEMO spot price for a
            # given region+interval -- they're not independent
            # estimators that can legitimately disagree while both are
            # live).
```

## Line 3074: above `"solve_diagnostics": {`

```text
            # nimbus issue #652 (Mark Purcell): sensor.nimbus_solver_
            # solve_seconds published duration alone, with no way to
            # tell "solve got slower because the problem got bigger" (a
            # real, legitimate reconfiguration -- Mark's own real case
            # was a 1-battery to 3-battery fleet change) from "solve got
            # slower because something regressed" without pulling raw
            # history and cross-referencing config-reload timestamps by
            # hand. Surfaced as extra_state_attributes on that flattened
            # sensor (see sensor_flattened.py's own attrs_source_key
            # mechanism) so a future jump can be attributed on sight.
            # Straight from the plan build_plan() itself just solved
            # against, so this can never drift from what actually ran.
            # nimbus issue #773 (2026-09-14): n_controllable_loads used to
            # sum only sheddable + adequacy loads, silently omitting
            # `thermal_loads` -- a first-class controllable-load kind
            # since #774/#800, and the kind the one real thermal load in
            # existence actually uses.
            #
            # Found live on devhub, not reasoned about: six controllable
            # loads were demonstrably in the plan (their own status
            # sensors reading "scheduled 09:00-15:30", "running", "done")
            # while this field reported 0. That is not a cosmetic
            # undercount -- #773's own triage reasoned directly from this
            # number ("whether the calibrated/secondary-cost lex phase has
            # an edge case that a large controllable-load + multi-battery-
            # participant combination can push into infeasibility"), so an
            # under-reporting field was actively misleading the
            # investigation into the problem shape.
            #
            # Broken out per kind rather than only corrected in total:
            # "which KIND of load grew" is the question a solve-time or
            # infeasibility regression actually needs answered, and a
            # single total cannot answer it. n_controllable_loads stays as
            # the honest sum so nothing consuming it breaks.
```

## Line 3188: above `"calibration_min_weight_fallback": plan.calibration_min_weight_fallback,`

```text
                # nimbus issue #1179: the blend-calibration outcome of the
                # cycle that produced this plan -- i.e. of a HEALTHY cycle,
                # since a failed one returns ~900 lines above this and
                # publishes nothing (#757).
                #
                # **This is the half that makes #1179 falsifiable, and it
                # was missing.** #1229 and #1291 both argue -- correctly --
                # that "the failures took the minimum-weight fallback"
                # explains nothing without the base rate among healthy
                # cycles, and both say the fields are "carried on success as
                # well as failure". They are carried onto `Plan`; nothing
                # then READ them on the success path. `solver_writer.py`
                # touches all three only inside the failure branch, so on a
                # deployed install the healthy-cycle distribution was
                # observable nowhere at all.
                #
                # Published here rather than logged, deliberately, and the
                # reason is measured: production's `/api/error_log` is
                # truncated at every container restart -- read 2026-09-27 at
                # 18:18 AEST it began at 16:09:58 AEST, the boot 2h 8m
                # earlier. An episode that recurs every few nights and lives
                # only in that log is routinely unreadable by the time
                # anyone looks. `solve_diagnostics` is the attribute payload
                # of `sensor.nimbus_solver_solve_seconds` (see
                # sensor_flattened.py's own `attrs_source_key`), which the
                # recorder keeps -- and that sensor's own history is already
                # how a #1179 investigation counts cycles, because a failed
                # solve never publishes it.
                #
                # Four scalars. No entity added, nothing renamed.
```
