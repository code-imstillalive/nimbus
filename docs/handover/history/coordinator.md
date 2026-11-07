# coordinator.py: comment history

Moved out of `custom_components/nimbus_load/coordinator.py` by #2289 item 7 (the method agreed with Mark Purcell, 7 Nov 2026), verbatim. Each block's rule, reason and issue number stay in the code as a short comment; the narrative is kept here, keyed by the code line it sat above. Line numbers are the file's at `main` before the move.

## Line 141: above `_retrain_tasks: dict[str, Any] = {}`

```text
# Idempotent cold-start-retrain task tracking, module-level (survives across
# a re-entrant async_setup_entry() call the same way __init__.py's own
# _solver_timer_unsub already does for the periodic-solve timer -- see that
# dict's own comment for the full "HA abandons and retries a slow
# async_setup_entry()" story, nimbus repo issue #211).
#
# PR #210 backgrounded this cold-start retrain via a bare
# hass.async_create_task(self._async_retrain()) specifically to stop it
# blocking hub setup -- but unlike the periodic-solve timer #213 later
# fixed the SAME way, that task was never tracked or cancelled anywhere.
# self._retraining (an instance attribute) only guards a SINGLE coordinator
# object against being told to retrain twice concurrently -- it does
# nothing for a genuinely SECOND, independent NimbusCoordinator object for
# the SAME subentry_id, which is exactly what a re-entrant
# async_setup_entry() call produces (a fresh `NimbusCoordinator(hass,
# entry, subentry)` per subentry, every time that loop runs -- see
# __init__.py's own async_setup_entry()). Two such coordinators would each
# independently kick off their own untracked, uncancelled retrain against
# the same subentry, each eventually calling _save_model_to_disk()/writing
# their own forecast sensor -- a real, live-plausible contributor to the
# "no longer has a state class" repair recurring on restarts/reloads, the
# same underlying mechanism #210/#213 already fixed for two other call
# sites in this exact class of bug.
```

## Line 183: above `MAX_SANE_POWER_KW = 1000.0`

```text
# physical sanity ceiling on any convert_power=True history point:
# comfortably above anything a real household's own load/battery/grid/
# solar sensor could ever legitimately report (this household's own
# documented real ceiling is ~50kW for Grid/Battery, ~17kW for Solar),
# while safely far below the magnitude a unit-conversion or integer-
# overflow-class sensor glitch produces (these land in the thousands to
# millions, never a plausible near-miss on a real value that this
# threshold could wrongly reject).
```

## Line 207: above `type NimbusConfigEntry = ConfigEntry[dict[str, NimbusCoordinator]]`

```text
# runtime-data (Quality Scale, Bronze): entry.runtime_data replaces the old
# hass.data[DOMAIN][entry.entry_id] pattern. Defined HERE, not in
# __init__.py -- __init__.py already imports FROM sensor.py
# (object_id_from_source), and sensor.py/diagnostics.py both already import
# NimbusCoordinator from THIS module, so this is the one place every
# consumer can import the type alias from without creating an import
# cycle. Forward-references NimbusCoordinator (defined a few lines below,
# not yet at this point in the file) -- safe because PEP 695 `type`
# statements are lazily evaluated on first access, not at definition time,
# by which point the whole module has finished executing.
```

## Line 274: above `self._last_retrain_error: str | None = None`

```text
        # nimbus issue #373 (Mark Purcell, codebase review): the only
        # operator-facing signal a retrain failure had before this was a
        # log line -- last_update_success stayed True (the *coordinator*
        # update succeeded; it's _async_retrain(), a separate background
        # task, that failed) with nothing on the forecast entity itself
        # hinting that training is broken. Published as a diagnostic
        # attribute (see _async_update_data()'s own dict) so this is
        # visible without reading the log. None once a retrain has never
        # failed, or after the next one succeeds -- see _async_retrain()'s
        # own try/except for where this is set/cleared.
```

## Line 299: above `self._residual_path = Path(`

```text
        # Confidence-band calibration state (see ml/model.py's own
        # calibrated_band() for the actual math). Deliberately persisted
        # SEPARATELY from the trained model, in a plain JSON file, not
        # bundled into TrainedModel -- a retrain replaces self._trained
        # wholesale, but the calibration buffer should survive across
        # retrains (it's tracking how far off PUBLISHED forecasts have
        # been from reality, which stays meaningful regardless of which
        # model produced them).
```

## Line 320: above `self._residual_drift_status: ResidualDriftStatus = ResidualDriftStatus`

```text
        # Always-descriptive telemetry (2026-08-25, nimbus issue #187,
        # Mark Purcell's real-install ask: "a positive 'I'm watching...'
        # telemetry field"). Unlike the flag above (which only tracks
        # whether a WARNING is currently active), this is read by
        # NimbusHealthReportSensor every cycle regardless of whether
        # anything has ever fired -- see _check_residual_drift()'s own
        # comment for why "watching: False" during cold-start is itself
        # a real, honest answer, not a placeholder.
```

## Line 338: above `self._temp_forecast_cache: list[tuple[datetime, float]] = []`

```text
        # #269 (Mark Purcell, real repro, 2026-08-28, direct follow-up to
        # #123): a transient weather.get_forecasts failure -- most
        # commonly the HA-restart startup-window race between this
        # coordinator's first tick and the weather integration's first
        # successful fetch -- used to silently degrade that cycle's
        # training input to zero temperature signal, with no recovery
        # path, plus the one-shot warning below made a later CHRONIC
        # failure invisible after its first occurrence. Two independent
        # mitigations, both from Mark's own real proposal:
        #
        # Mitigation A: cache the last real, non-empty forecast on the
        # instance and fall back to a trimmed slice of it on a failed/
        # empty fetch, instead of training on zero signal. Keyed by
        # entity_id too -- a mid-session reconfigure to a DIFFERENT
        # temperature_forecast_sensor must never leak the old sensor's
        # stale cached values onto the new one.
```

## Line 356: above `self._last_temp_forecast_ok: bool | None = None`

```text
        # Mitigation B: replaces the old one-shot _temp_forecast_empty_
        # warned flag with a state-change tracker -- warns on every
        # success->failure transition (not just the first ever) and logs
        # an INFO on every failure->success recovery, so a chronic
        # problem starting DAYS after the first tick is still loud, and
        # a household triaging a report can tell "did it recover on its
        # own." None = never fetched yet (no state change to log on the
        # very first tick either way).
```

## Line 454: above `def _power_feature_or_none(self, sensor: str | None, role: str) -> str`

```text
    # Real measured power sensors only -- never an optimizer's own plan/
    # forecast entity, see this repo's own CLAUDE.md PRIME DIRECTIVE.
    #
    # All three return None (fully disabled) for ANY "power signal"
    # subentry, not just a self-referential one -- widened 2026-08-15
    # after the narrower self-reference-only guard (still correct, still
    # in place below) made ZERO observable difference to a real,
    # confirmed-live-broken forecast. Root cause is broader than self-
    # reference: EVERY one of these three features is held FLAT across
    # the entire forecast horizon (there's no future value for a real
    # measured sensor -- see _current_measured_power()'s own docstring),
    # and a stale flat feature dominates/anchors a recursive multi-step
    # forecast toward "whatever was true at the one moment the forecast
    # cycle ran" regardless of WHICH signal it's stale for -- Battery's
    # own model using a stale grid_kw or solar_kw is just as broken as
    # using a stale battery_kw. Confirmed via two matched synthetic
    # tests: identical training data, WITHOUT any of these three
    # features reproduced a real +13kW evening plateau almost exactly
    # (12.97 vs 13.0); WITH one stale flat feature added, the same
    # model predicted -26kW (the opposite sign) for the same window.
    # A power signal's own recent behaviour is already correctly
    # captured by lag_short/lag_long (which update per-step during
    # recursive forecasting, unlike these three) -- there's no version
    # of "another signal's current value, held flat for 96 hours" that
    # is ever useful signal for a power signal's OWN forecast, only
    # ever noise. Loads are unaffected -- a load's own forecast
    # genuinely benefits from a coarse "what's the system doing right
    # now" hint, which is the intended, working use case these three
    # features were originally built for.
```

## Line 521: above `@property`

```text
    # Per-load, unlike everything else above -- a fixed schedule window
    # (if any) is specific to this one load, not shared across the hub.
    # Stored as a plain HH:MM(:SS) time string (what the config flow's
    # TimeSelector actually produces -- a real time picker, not a
    # decimal-hour number box) and converted to the decimal hour
    # ml/features.py's in_schedule comparison expects right here, so
    # nothing downstream of these two properties needs to know the
    # storage format changed.
```

## Line 790: above `self.entry.async_on_unload(self.async_unload)`

```text
        # nimbus issue #344 (Mark Purcell): a safety net for the ONE real
        # teardown path __init__.py's own async_unload_entry() can never
        # reach -- a sibling subentry's async_config_entry_first_refresh()
        # raising ConfigEntryNotReady inside __init__.py's asyncio.gather()
        # call. That exception propagates out of gather() before entry.
        # runtime_data is ever assigned, so async_unload_entry()'s own
        # `for coordinator in entry.runtime_data.values(): coordinator.
        # async_unload()` loop has nothing to iterate -- THIS coordinator's
        # listener (and every other sibling's, constructed earlier in the
        # same gather batch) leaks, and HA's own retry-with-backoff on
        # ConfigEntryNotReady means every retry leaks another full set.
        # entry.async_on_unload() is HA's own guarantee for exactly this:
        # it fires on ANY teardown of this entry, including one that never
        # reaches async_unload_entry() at all (a setup that fails before
        # completing). Registering the COORDINATOR'S OWN async_unload
        # (already idempotent -- guards on `self._unsub_retrain is not
        # None` and sets it back to None) rather than the raw `unsub`
        # callable directly is deliberate: on the NORMAL successful-setup
        # path, async_unload_entry() ALSO calls coordinator.async_unload()
        # explicitly, and HA core only processes async_on_unload hooks
        # after that function returns -- registering the raw callable here
        # instead would call the SAME underlying HA listener-removal twice
        # in that case, reproducing the exact double-unsub ValueError /
        # FAILED_UNLOAD crash issue #337 (fixed the same way, for the
        # price-watcher's own equivalent callable) already closed.
```

## Line 817: above `needs_immediate_retrain = (`

```text
        # nimbus issue #373: a loaded model with an older schema_version
        # is being served as a genuine, predict()-able fallback (see
        # _load_model_from_disk()'s own comment) rather than discarded,
        # but it should still be replaced with a properly-versioned
        # model as soon as possible -- same immediate-background-retrain
        # trigger as the "nothing on disk yet" case below, just for a
        # different reason. getattr-defensive, same reasoning as every
        # other TrainedModel field accessed this way in this file --
        # schema_version is guaranteed on any real TrainedModel (a plain
        # dataclass default, or __setstate__'s own backfill on unpickle),
        # but treating it as "already current" if it's ever genuinely
        # absent is a safe, conservative default (skips the extra
        # retrain rather than risk an unexpected AttributeError here).
```

## Line 839: above `old_task = _retrain_tasks.pop(self.subentry.subentry_id, None)`

```text
            # Nothing on disk yet (or what's there is schema-stale) --
            # train immediately (in the background, NOT awaited here) so
            # the sensor has real, properly-versioned data soon after setup,
            # instead of sitting empty for up to 24h waiting for the next
            # scheduled retrain hour. Same "kick it off, don't block setup"
            # pattern __init__.py already uses for the Solver's own first
            # cycle (hass.async_create_task(solver_runtime.async_run_solve)).
            #
            # Real, live-reproduced bug this fixes (2026-08-26, devhub): this
            # used to be `await self._async_retrain()`, awaited INLINE inside
            # __init__.py's own per-subentry setup loop, before
            # async_forward_entry_setups() (i.e. before ANY entity gets
            # registered) ever runs. _async_retrain() does several sequential
            # recorder history fetches plus a real ML training job -- on an
            # install with several subentries simultaneously lacking a
            # persisted model (e.g. right after a `.pkl` reset, or several
            # subentries added at once), that blocking chain can genuinely
            # take minutes, comfortably risking HA's own slow-setup timeout.
            # Confirmed live: a real "Platform nimbus_load does not generate
            # unique IDs" ERROR burst (every hub-level number/switch/sensor
            # entity duplicate-registering) landed at 19:00-19:01 the same
            # night devhub had several untrained subentries -- consistent
            # with HA abandoning/retrying a setup that ran long, while the
            # original attempt's still-executing training work (executor
            # jobs don't get interrupted by task cancellation) finished and
            # tried to register the same entities a second time. A plain
            # isolated reload_config_entry AND a full restart both failed to
            # reproduce it once every subentry already had a persisted model
            # on disk -- consistent with training time, not reload/restart
            # itself, being the real trigger. _async_update_data() already
            # returns a well-defined, already-exercised "untrained" state
            # dict (state=None, forecast=[], training_points=0) whenever
            # self._trained is None, so nothing downstream needs the
            # training to have finished by the time hub setup completes --
            # this was blocking behaviour with no correctness reason behind
            # it, only a "get real data displayed a bit sooner" one, which a
            # background task still satisfies.
            #
            # Idempotent (2026-08-27, nimbus repo issue #211): cancel any
            # retrain task already tracked for this subentry_id before
            # scheduling a new one -- see _retrain_tasks' own module-level
            # comment for why a second, independent coordinator object for
            # the same subentry otherwise leaves an orphaned, untracked
            # retrain running forever.
```

## Line 925: above `temp_events = (`

```text
            # Real bug found live (2026-08-31): all six of the calls below used
            # to read `_async_fetch_history`, a name that stopped existing the
            # moment #257/#259 (2026-08-28) renamed it to
            # `_async_fetch_recorder_history` and introduced this training-
            # source-aware wrapper -- only the load_events call above got
            # migrated at the time, leaving these six still pointed at a
            # nonexistent method. Every subentry with ANY of temp/humidity/
            # curtailment/battery/grid/solar configured (temp/humidity are
            # shared HUB-level options, so this is effectively every
            # subentry on a real install) hit an immediate AttributeError
            # here, before train_model() was ever reached -- explaining why
            # neither of train_model()'s own "no history"/"too few points"
            # warnings ever appeared for an affected subentry while
            # training_points stayed 0 forever. A subentry with a
            # pre-#257 .pkl already on disk masked this silently: _trained
            # is not None, so async_setup() never calls _async_retrain() at
            # startup, and the crash only bites (also silently -- an
            # unhandled exception inside a create_task) at the next
            # scheduled nightly retrain.
```

## Line 989: above `max_staleness_minutes = (`

```text
            # nimbus issue #375 (Mark Purcell, codebase review): lts/
            # hybrid sources feed events derived from HA's own hourly
            # long-term-statistics buckets -- coarser than the training
            # grid's 15-min step. The default lag-staleness cap (sized
            # for recorder-cadence data) silently drops every single
            # LTS-derived training row's own lag lookup -- confirmed
            # live, a real install training on 60 days of hybrid history
            # (55 LTS + 5 recorder) produced only ~470 usable rows,
            # exactly "5 days at native cadence." Sized to comfortably
            # cover one full LTS bucket period plus one grid step of
            # slack (75 min for the current 15-min grid) -- see train_
            # model()'s own max_staleness_minutes docstring for the full
            # mechanism. None (recorder-only) keeps the original,
            # unchanged 45-min cap.
```

## Line 1065: above `_LOGGER.exception(`

```text
            # nimbus issue #365 (Mark Purcell): this coroutine is
            # scheduled via hass.async_create_task() (background retrain,
            # see async_setup()'s own docstring for why) -- with no
            # except clause here, any real failure (a bad history fetch,
            # a training bug) surfaced ONLY as an "unretrieved task
            # exception" asyncio logs on its own, invisible to health.py's
            # own WARNING+ ring buffer. This is precisely how a real
            # AttributeError (2026-08-31, see the comment above this
            # method) stayed silent for days: the health report kept
            # showing 0 errors while training was completely dead for
            # every affected subentry. _LOGGER.exception() here reaches
            # NimbusLogBufferHandler the same way every other real
            # WARNING+ in this integration does.
```

## Line 1508: above `if not math.isfinite(value):`

```text
                # nimbus issue #353 (Mark Purcell): float("nan") does not
                # raise, so a template/REST/Modbus sensor without a numeric
                # device_class/state_class (HA core's own state-write guard
                # only rejects non-numeric states for those) can publish
                # "nan" straight into recorder history -- and abs(nan) >
                # MAX_SANE_POWER_KW below is False, so the existing sanity
                # ceiling silently passes it through. Reproduced: one NaN
                # training row poisons x_mean/x_std for the whole retrain,
                # every model's validation_mae comes out NaN, min() over a
                # NaN dict still "picks" a model, and predict() then
                # returns 0.0 for every step (max(0.0, nan) == 0.0) --
                # silently, with no error, until the next successful
                # retrain overwrites the poisoned pickle. Dropped here,
                # same as an unparseable state already is. (#1556: as a
                # gap marker when training asks for them -- a marker never
                # becomes a training row, see ml/model.py
                # resample_last_value().)
```

## Line 1545: above `if abs(value) > MAX_SANE_POWER_KW:`

```text
                    # Real, live-confirmed bug (2026-08-17): a single bad
                    # Modbus read in this household's real recorder history
                    # (sensor.logger_meter_total_active_power, 2026-07-07,
                    # value 21474836.5 -- a classic 32-bit signed-integer-
                    # overflow artifact, 2^31/100) sat harmlessly in
                    # recorder history for over a month until a full
                    # cold retrain genuinely re-read it as real training
                    # data. k-NN's own prediction is mathematically bounded
                    # by real y_train values (a weighted average can never
                    # exceed the observed range) -- which is exactly why a
                    # later recursive forecast step, once its own feature
                    # vector happened to land near that one poisoned
                    # training point, correctly (if uselessly) reproduced
                    # something close to 21 million kW. The bound guarantee
                    # protects against extrapolation, not against bad DATA
                    # inside the bound. MAX_SANE_POWER_KW is comfortably
                    # above any real signal this household (or any
                    # realistic household) could ever produce (documented
                    # real ceiling here is ~50kW for Grid/Battery, ~17kW
                    # for Solar) while safely far below the magnitude any
                    # unit-conversion/integer-overflow-class glitch
                    # produces (these tend to be in the thousands to
                    # millions, never a plausible near-miss on a real
                    # value) -- dropped entirely (never trained on, never
                    # displayed), same as an unparseable state already is.
```

## Line 1701: above `ts_raw = r.get("start")`

```text
                # nimbus issue #372 (Mark Purcell, codebase review): the
                # comment this replaces claimed "start" is a UTC datetime
                # object (or ISO string in older HA cores) -- wrong on
                # every currently-supported core. HA core's own
                # statistics_during_period() (homeassistant/components/
                # recorder/statistics.py, _sorted_statistics_to_dict) has
                # returned "start"/"end" as float epoch SECONDS
                # (start_ts) since HA 2023.4, not a datetime or ISO
                # string at all. This crashed every single retrain on
                # training_source lts/hybrid with AttributeError: 'float'
                # object has no attribute 'tzinfo' the moment #365 made
                # a retrain failure loud instead of silent -- confirmed
                # live, real IV&V report, 2026-09-05. str/datetime
                # branches kept for genuinely older cores and this
                # file's own existing test fixtures, which predate this
                # fix and mock datetime rows.
```

## Line 1914: above `now_ok = bool(effective)`

```text
        # #269 mitigation B -- warn on every genuine success->failure
        # transition (not just the very first tick ever), and log an
        # INFO on every failure->success recovery, so a household
        # triaging a report can tell whether it recovered on its own.
        # Deliberate, verified improvement over Mark's own literal sketch
        # (which treated the very first-ever fetch as "no state change to
        # log" regardless of outcome): a household whose configured
        # sensor has NEVER once worked would otherwise never see a single
        # warning under that literal reading, which is a real regression
        # from the previous one-shot-on-first-empty-result behaviour this
        # is replacing. First tick still logs a real warning if it's
        # already failing; only a genuinely NEW value (not None) skips
        # logging when the state hasn't changed.
```

## Line 2043: above `_LOGGER.warning(`

```text
            # A real external service call (unsupported forecast type,
            # entity gone, transient HA-core issue) must never take down
            # a coordinator tick -- degrades to "no forecast this cycle"
            # instead, surfaced via the caller's own empty-result
            # warning. No lint suppression comment needed here (unlike
            # the two sibling blind-except sites elsewhere in this
            # file) -- confirmed directly that ruff's blind-except rule
            # doesn't flag a broad except when the log call includes
            # exc_info=True, which this one already does.
```

## Line 2098: above `if trained.x_mean.shape[0] != len(FEATURE_NAMES):`

```text
            # A persisted model's feature count is frozen at whatever
            # FEATURE_NAMES looked like the day it was trained. Confirmed
            # live 2026-08-15: loading an incompatible model straight into
            # predict() raises a raw numpy broadcast ValueError deep
            # inside an executor thread, which HA surfaces as an opaque
            # "Config entry not ready yet" retry loop with no obvious fix
            # -- every retry hits the same crash since the stale pickle
            # never gets replaced on its own. Discarding it here instead
            # means a future feature-set change self-heals with one fresh
            # retrain, not a stuck integration.
            #
            # nimbus issue #366 (Mark Purcell): this check used to sit
            # OUTSIDE the try block below -- `trained.x_mean` itself
            # raises a bare, uncaught AttributeError if a pickle
            # deserializes into something that doesn't even HAVE that
            # attribute (an even older/more broken schema than a mere
            # feature-count mismatch), propagating out of this method and
            # crashing async_setup() the same "Config entry not ready"
            # way the comment above already describes -- for exactly the
            # class of stale-pickle problem this whole check exists to
            # protect against. Moved inside the try so ANY shape of
            # persisted-model incompatibility degrades to "discard and
            # retrain fresh," never a crash.
```

## Line 2131: above `if trained.schema_version > TRAINED_MODEL_SCHEMA_VERSION:`

```text
            # nimbus issue #373 (Mark Purcell, codebase review), real
            # live incident: the original #366 finding 3 fix discarded
            # ANY schema_version mismatch outright and relied on an
            # immediate retrain to replace it -- but "discard first,
            # replace later" means a subentry has genuinely NOTHING the
            # moment that retrain fails for any reason (confirmed live:
            # the #372 LTS float-epoch crash turned one version bump
            # into a 20+ hour, every-forecast-`unknown` outage on a real
            # install, and the exact same gap exists for ANY OTHER
            # retrain failure -- a transient recorder error, the
            # recorder daemon still starting, a briefly-unavailable
            # sensor). An OLDER pickle (schema_version < current) is
            # SAFE to keep serving as a stale-but-compatible fallback:
            # TrainedModel.__setstate__ has ALREADY backfilled every
            # field it lacks with the same sane defaults predict() would
            # otherwise need a defensive getattr() for, so it predicts
            # correctly, just possibly without a newer field's full
            # benefit. async_setup() below still schedules an immediate
            # retrain regardless of this fallback, so it's replaced with
            # a properly-versioned model as soon as one succeeds, and
            # this stale copy is never re-persisted to disk (only a
            # genuinely fresh, correctly-versioned retrain result is).
            #
            # A NEWER pickle (schema_version > current, e.g. a real
            # downgrade) is a fundamentally different risk -- THIS code
            # doesn't know what that future schema means and has no
            # backfill logic for it, so that direction still discards
            # outright, same as before.
```

## Line 2452: above `bound_floor, bound_ceiling = band_limits(`

```text
        # Real, observed physical range for this signal/load -- confirmed
        # live 2026-08-15 that calibrated_band()'s sqrt(1+lead_hours)
        # growth has no ceiling at all: for Grid (whose GBRT candidate
        # badly overfit -- validation_mae=6220 vs knn's 7.55, so k-NN won
        # and there's no bounded quantile model, forcing the residual
        # fallback), the band had widened to +/-100kW by 96h out, nearly
        # 2.5x this project's own long-documented ~44kW physical grid
        # limit, and visibly still growing ("spirals up and up," reported
        # live). Genuine model-derived quantile bounds (has_model_bounds)
        # aren't immune to the same risk either -- a tree-based quantile
        # model can still extrapolate past its own training range for a
        # timestamp/feature combination it never saw. Clamping BOTH
        # sources to what this signal has actually, really done (with a
        # modest 20% margin -- a genuine future extreme shouldn't be
        # impossible, just not unboundedly implausible) is honest in a
        # way an arbitrary fixed kW constant wouldn't be: it's grounded
        # in this specific signal's own real data, works identically for
        # a 3kW load and a 44kW battery with zero per-entity tuning.
```
