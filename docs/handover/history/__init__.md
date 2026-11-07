# `__init__.py`: comment history

Moved out of `custom_components/nimbus_load/__init__.py` by #2289 item 7 (the method agreed with Mark Purcell, 7 Nov 2026), verbatim. Each block's rule, reason and issue number stay in the code as a short comment; the narrative is kept here, keyed by the code line it sat above. Line numbers are the file's at `main` before the move.

## Line 64: above `_SOLVER_CRON_MINUTES = list(range(0, 60, 5))`

```text
# Nimbus issue #244 (Mark Purcell, 2026-08-27): a plain 1-minute
# `async_track_time_interval` has no phase relationship to the NEM 5-minute
# settlement boundary (:00/:05/:10/...), so its phase drifts freely with
# whatever second HA happened to start the integration on. Live-measured
# evidence (24h of `sensor.amber_express_amber_feed_in_price.last_changed`,
# 273 ticks): the settled AEMO tick lands in a tight [15s, 30s) window past
# each boundary 89% of the time (median 20.4s). A cron with no relationship
# to that window regularly solves just BEFORE the tick arrives, then waits
# up to another full interval to pick it up -- issue #244's own measured
# median expected loss was ~30s of stale-price dispatch per 5-minute block.
#
# Fix: phase-lock to the boundary instead of running on a free-running
# interval -- solve at :00:30, :05:30, :10:30, ... (30s past every NEM
# boundary, comfortably past the p90 tick-arrival window above). This is
# issue #244's own "Option A": fewer solves overall (12/hour vs 60/hour)
# AND a solve that's (almost) always looking at the current block's real
# settled price rather than last block's stale one.
#
# solver_runtime.async_run_solve()'s own PID-file overlap guard makes this
# safe even if a cycle occasionally runs long -- unchanged by this fix.
```

## Line 87: above `_CRON_SUPPRESS_WINDOW_S = 60`

```text
# Issue #295 (Mark Purcell, 2026-08-31): live-measured evidence (four
# consecutive NEM boundaries, switch.nimbus_solve_on_price_change on)
# showed the phase-locked cron above re-running the exact same solve an
# event-driven trigger had already completed 5s earlier -- same inputs,
# same LP, same plan, just a redundant CPU burn plus a spurious
# last_updated restamp on sensor.nimbus_solver_battery_forecast that
# makes latency measurement (issue #294) ambiguous about which write is
# which. 60s is deliberately conservative/short relative to the 5-minute
# cron cadence: a cron tick at :05 still fires even if a price tick at
# :00 produced a solve (5 minutes have passed, well past this window),
# but a cron tick at :30 is suppressed if an event-driven solve at :26
# already ran. The cron is a WATCHDOG (guarantee at least one solve per
# 5-min block even if Amber goes quiet for a whole window -- a real,
# if rare, possibility per #295's own "why not just disable cron when
# the switch is on" section) not a heartbeat that must always fire.
```

## Line 104: above `_STARTUP_RETRY_ATTEMPTS = 6`

```text
# Startup-retry constants for the "one immediate cycle at setup" call below
# -- a household-reported real gap (2026-08-30): on a full HA restart, the
# `number.nimbus_solver_*` required entities (battery capacity, max charge/
# discharge, grid max import/export) restore their own real values from the
# entity registry on their OWN schedule, genuinely independent of Nimbus's
# own setup. The immediate at-setup solve can race that restore and see
# `sensor.nimbus_solver_config` still reporting `unconfigured` -- a real,
# transient condition, not a genuine misconfiguration -- and
# async_run_solve() has no retry of its own (see its own docstring: "never
# raises," but also never retries). Before this fix, losing that race meant
# waiting for the next phase-aligned cron tick (up to 5 minutes) for a
# second chance -- confirmed live: a restart at 00:50 UTC produced its
# first real solve at 00:55:00 UTC, a genuine 5-minute boundary, not an
# early retry succeeding.
#
# 6 attempts, 15s apart (90s total) -- long enough to comfortably outlast
# the observed restore race, short enough that a genuinely-unconfigured
# fresh install (no wizard run yet) gives up quickly and falls back to
# exactly today's behaviour (silent until the wizard's run, picked up by
# the next periodic cron tick once it is) rather than retrying forever.
```

## Line 131: above `_solver_timer_unsub: dict[str, Callable[[], None]] = {}`

```text
# Module-level, NOT hass.data[DOMAIN] -- this project deliberately moved off
# that pattern to entry.runtime_data for Quality Scale Bronze (see
# coordinator.py's own comment next to NimbusConfigEntry). Same idempotent-
# registration technique solver_writer.py's own _ENTITY_UPDATE_HANDLERS
# already uses for an identical class of problem: keyed by entry_id, holds
# the unsub callable for _periodic_solve's own async_track_utc_time_change
# registration below, so a second async_setup_entry() call for the SAME
# entry_id can cancel the first one's timer before registering its own.
#
# Nimbus issue #211 (live devhub recurrence, 2026-08-27): sensor.nimbus_
# solver_battery_forecast / sensor.nimbus_household_load_total_forecast
# writing at ~2x/minute, a few seconds apart, every single minute --
# CONFIRMED via a live `ha core logs -f` capture to be two genuine, back-
# to-back solves (different computed values each time), not a single
# solve double-writing (solver_writer.py has exactly one ha_post_state()
# call site per entity). Two full solves a minute apart is exactly what
# TWO independent, live async_track_time_interval registrations for the
# SAME config entry would produce -- and solves finish in ~1-2s (per
# issue #85's own captured diagnostic dump), well under the then-1-minute
# interval (since superseded by issue #244's phase-locked cron above),
# so the PID-lock overlap guard in solver_writer.py's acquire_lock()
# never even sees them as concurrent; each one just runs to completion
# and pushes its own real result a few seconds after the other.
#
# Same root mechanism #210 already fixed for retrain (as #210's own
# regression test recorded it, since retired by #1837: HA
# abandoning/retrying a slow async_setup_entry() while the original
# attempt's own coroutine keeps running in the background on an executor
# job, eventually finishing and re-registering everything a second time)
# -- just a different slow step tripping the same abandon-and-retry path,
# since #210 only backgrounded the retrain call specifically. The
# abandoned attempt's own hass.config_entries.async_forward_entry_setups()
# call is a silent no-op the second time (platforms already forwarded),
# which is why this doesn't reproduce the loud "does not generate unique
# IDs" error #210 fixed -- but nothing stopped it from reaching the
# _periodic_solve registration below and creating a second, independent,
# permanently-live timer. Explains why a prior `reload_config_entry`
# didn't fix this live: if whatever makes setup slow is a standing
# condition (not a one-off timing fluke), the reload's own fresh
# async_setup_entry() call can retrigger the identical race immediately.
```

## Line 177: above `_P2P_BLOCK_START_SECOND = 5`

```text
# A solve at this second of each minute that is a configured P2P block's
# start (lead time included). The cron's :00:30, :05:30, ... cannot plan a
# lead-time minute such as 16:59, and the price watcher only fires when a
# price actually changes, so without this a block's first minute was
# planned only by luck. Measured on the reference household 1-5 Oct 2026:
# the battery reached the block rate ~30 s before 17:00 on 1-2 Oct, while
# its LocalVolts writer republished every 15 s, and 14-32 s AFTER 17:00 on
# 3-5 Oct, once that writer moved to the v2 API and an unchanged price no
# longer produced a state change. :05 leaves the minute's own plan period
# almost whole, and the 10 s debounce in a dispatch automation still lands
# inside it.
```

## Line 190: above `_P2P_BLOCK_REPLAN_SECONDS = (5, 20, 35, 50)`

```text
# nimbus #1678: inside a P2P block the battery setpoint is the plan's own
# prediction of block rate + house - solar, so every house-load step between
# solves lands on the meter. At the designed cadence (5-minute cron plus real
# price changes, ~23-37 solves an hour) a cycling ~1 kW load moved the
# reference household's net export 10.3-12.8 kW against a 12 kW block on
# 9 Oct; at ~15 s it stayed flat (30 Sep, meter sd 0.14 kW; 12 Oct, -11.2 kW
# through the block). So while a block is active the same per-minute check
# also runs at :20, :35 and :50, and solves unless any solve -- from any
# trigger, solve_now included -- finished within the last
# _P2P_BLOCK_REPLAN_MIN_GAP_S. An install that already calls solve_now every
# 15 s therefore gets no extra solves. Outside a block nothing changes.
```

## Line 228: above `_startup_solve_tasks: dict[str, asyncio.Task[None]] = {}`

```text
# The startup-retry task's own handle, keyed by entry_id (2026-09-01,
# real root cause of issue #312's residual -- see async_unload_entry()'s
# own comment for the full story). Registering this task's cancellation
# ONLY via entry.async_on_unload() left a real window open: HA core only
# processes those callbacks AFTER our async_unload_entry() has already
# torn down every platform, so a task still sleeping between retries could
# wake up, find its target entity's solver_writer handler just
# unregistered, and fall through to a raw, non-restored state write that
# then collided with the next setup's fresh entity. Tracked here so
# async_unload_entry() can cancel it directly, before platform teardown,
# instead of waiting on entry.async_on_unload()'s later timing.
```

## Line 241: above `_setup_tasks: dict[str, asyncio.Task[bool]] = {}`

```text
# Re-entrancy guard (2026-09-01, live-confirmed root cause of the household's
# own long-standing "number.nimbus_solver_* entities reset to their schema
# placeholder minimum on some restarts, not others" bug). Every fix above
# this comment (backgrounding retrain for #210, idempotent timer-unsub for
# #211) closed ONE specific slow step that could trip HA's own "abandon a
# slow async_setup_entry() and silently retry it while the original
# coroutine keeps running" behaviour -- but the per-subentry coordinator
# setup+first-refresh loop below (sequential, awaited, one real ML
# retrain/refresh per subentry) is itself just as capable of tripping the
# SAME general mechanism, and nothing stopped it from doing so.
#
# Confirmed live on devhub, 2026-09-01: a real restart's own log showed
# TWO separate, ~4.5-second-apart completions of this whole function for
# the SAME entry_id, the second one's entity_platform.py-level
# `async_add_entities()` calls colliding with the first's already-
# registered unique_ids across EVERY number/sensor/switch entity this
# integration owns ("Platform nimbus_load does not generate unique IDs...
# ignoring number.nimbus_solver_battery_capacity_kwh" etc., confirmed via
# a live `ha_get_logs` pull, not assumed). Whichever attempt's entities
# register FIRST wins; the second is silently dropped. This is genuinely
# non-deterministic across restarts -- there is no guarantee the SAME
# attempt wins every time -- which is exactly the "sometimes fine,
# sometimes reset, no obvious pattern" symptom reported repeatedly across
# this project's own history. A prior `reload_config_entry` doesn't
# reliably fix this either: if whatever makes setup slow (e.g. many
# subentries, per the reference household's own 18+ real circuits) is a
# standing condition rather than a one-off timing fluke, the reload's own
# fresh async_setup_entry() call can retrigger the identical race
# immediately.
#
# Fix, at the ROOT of the mechanism rather than patching each newly-
# discovered slow step one at a time: if a second call for the SAME
# entry_id arrives while a first is still genuinely in flight, don't run
# the whole setup (and its real side effects -- entity creation, timer
# registration, coordinator construction) a second time at all -- just
# wait for the original attempt to finish and hand its own result back.
# This makes the "abandoned" retry a true no-op instead of a parallel,
# colliding duplicate -- regardless of which future step ever becomes the
# next slow one.
```

## Line 476: above `_LOGGER.warning(`

```text
        # REPORTED, NOT REMOVED -- deliberately. See this function's own
        # docstring: the entities that motivated #1270 turned out not to be
        # orphans at all, so there is no confirmed instance of this condition
        # and deleting user-visible registry rows on speculation is the wrong
        # trade. WARNING rather than INFO so it is visible in a default HA log
        # without the household turning on debug logging -- the whole point is
        # that a real instance becomes findable. Promoting this to an
        # async_remove() is a one-word change once one is confirmed.
```

## Line 496: above `try:`

```text
        # nimbus issue #1320 (Mark Purcell, IV&V #1319): actively DISMISS, not
        # merely decline to re-create.
        #
        # The original #1294 fix returned here and nothing else, and its own
        # comment claimed that a stable notification_id made "dismissing it
        # meaningful: it returns only if the condition is still present at the
        # next setup". That describes non-recreation. A household that saw the
        # notification, did not act on it, and then had the condition resolve
        # kept a stale notification indefinitely.
        #
        # This is the same failure #416 fixed for the sibling notifier, whose
        # own docstring names it: "a stale, wrong, scary-looking notification
        # left behind with no way for the household to know it was already
        # stale." That sibling is a matched pair --
        # `_notify_load_forecast_error_once()` creates,
        # `_clear_load_forecast_error_notification_if_needed()` dismisses -- and
        # #1294 followed only half of it while asserting it had followed both.
        #
        # Folded in here rather than split into its own `_clear_..._if_needed()`:
        # the sibling is split because its two halves fire from different points
        # in the solve cycle, whereas this function already runs once per setup
        # and already branches on the condition. Keeping create and dismiss
        # adjacent makes it hard to add one without seeing the other.
        #
        # Unconditional, and the cost is why that is fine here but not there.
        # `persistent_notification.dismiss` on an id that is not showing is a
        # no-op, so there is no sentinel to track and nothing to get out of
        # step. The sibling guards with a sentinel file because its own
        # docstring says it is "called once per cycle on the healthy path" --
        # once a minute, so ~1440 no-op service calls a day. This function runs
        # once per SETUP, so the same call is a handful per restart and the
        # sentinel would be more state than it saves.
```

## Line 545: above `listed = "\n".join(`

```text
    # nimbus issue #1294: a real persistent_notification, not only the
    # WARNINGs above.
    #
    # This function's own docstring is the argument for it: an orphan holds
    # its entity_id reserved, which forces a live entity onto a `_2` suffix --
    # an id change on something dashboards and automations reference -- and
    # "a household cannot fix it by hand, because HA greys out delete for an
    # entity belonging to a loaded config entry". A consequence that real with
    # no user-side remedy, reported only in the log, requires the household to
    # already be watching the log; #594 names that as a known failure class in
    # this project.
    #
    # Matches the two established sibling notifiers (#66's
    # `_notify_load_forecast_error_once()`, solver_runtime's missing-dependency
    # notifier), with a stable notification_id so a reload REPLACES the message
    # rather than stacking another copy.
    #
    # nimbus issue #1320: this comment used to add "-- which also makes
    # dismissing it meaningful: it returns only if the condition is still
    # present at the next setup", and that sentence was the bug's own
    # description. Non-recreation is not dismissal, and the sibling pair this
    # claims to follow does both. The dismiss now lives in the `if not orphans`
    # branch above; this comment no longer claims something the code did not
    # do.
    #
    # Deliberately still not a removal. The entities that motivated #1270
    # turned out not to be orphans at all, so there is no confirmed instance of
    # this condition, and deleting user-visible registry rows on speculation
    # remains the wrong trade. What changes here is only whether a real
    # instance is FINDABLE.
```

## Line 666: above `task = asyncio.create_task(_async_setup_entry_impl(hass, entry))`

```text
    # Deliberately asyncio.create_task(), NOT hass.async_create_task() --
    # every other backgrounded task in this file uses the hass wrapper
    # specifically so HA's own shutdown/async_block_till_done() tracking
    # knows about it (a genuine fire-and-forget task the caller never
    # awaits). This one is different: its own result IS awaited, right
    # below, by the same coroutine that created it -- HA's own top-level
    # await of async_setup_entry() itself is what actually tracks this
    # work's lifecycle, the same as it always has. Using hass.async_
    # create_task() here would additionally require every existing test
    # (and any future one) that calls async_setup_entry() with a mocked
    # hass to make that mock's async_create_task() return a genuinely
    # awaitable object -- real breakage confirmed live in CI (2026-09-01):
    # several pre-existing tests mock hass.async_create_task() as a
    # fire-and-forget stub (closes the coroutine, returns a bare
    # MagicMock()) since every PRIOR call site here never awaited its own
    # return value. Bypassing hass for this specific, purely-internal
    # task sidesteps that entirely, with no loss of real tracking (see
    # above).
```

## Line 718: above `try:`

```text
    # Ship the nimbus-topology-card Lovelace resource -- served
    # over HTTP and registered as an extra JS module via
    # frontend.add_extra_js_url(), so a fresh HACS install gets the card
    # without a `www/` file copy or a manual Settings -> Dashboards ->
    # Resources step (issue #79). Works identically for storage-mode
    # and YAML-mode Lovelace. Non-fatal: a failure here (missing asset,
    # unexpected exception) still leaves the forecaster, sensors, and
    # solver fully functional -- the user can add the resource manually
    # the same way as before.
```

## Line 791: above `async def _setup_one(subentry) -> tuple[str, NimbusCoordinator]:`

```text
    # Concurrent, not sequential (2026-09-01) -- each subentry's own
    # coordinator is fully independent (own persisted model file, own
    # entry in the per-subentry-id-keyed _retrain_tasks dict above, no
    # shared mutable state between them), so there was never a real
    # correctness reason for a plain one-at-a-time for loop here. On an
    # install with many subentries (the reference household's own 18+
    # real circuits; devhub similarly loaded) this loop is genuinely the
    # slow part of setup -- real enough to trip HA's own "abandon a slow
    # async_setup_entry() and retry it" behaviour (see _setup_tasks' own
    # module-level comment for the live-confirmed mechanism this fix
    # complements, not replaces -- that guard protects correctness
    # regardless of speed; THIS change reduces how often the race even
    # gets a chance to trigger in the first place). asyncio.gather()'s
    # default (no return_exceptions=True) preserves the exact same
    # failure semantics as the old sequential loop: the first subentry
    # to raise aborts the whole setup, propagating that exception --
    # not a silent partial-setup regression.
```

## Line 821: above ``

```text
    # Real root cause found (2026-09-01), directly observed live on devhub
    # via a controlled reload with debug logging on -- NOT the theory
    # v0.94.47 shipped (a 0.1s settle sleep here, on the SETUP side).
    # That fix was tested live after shipping (both a full restart and a
    # plain `homeassistant.reload_config_entry`) and CONFIRMED NOT TO
    # WORK -- the identical collision reproduced both times, at the exact
    # same millisecond across every affected entity, disproving the
    # "post-unload settle timing" theory outright (a settle delay on the
    # SETUP side cannot matter if the thing racing setup is UNLOAD's own
    # side, and the 0.1s guess was never actually validated against a
    # measured gap). Removed rather than left in place as dead weight.
    #
    # The real mechanism, confirmed via a live log capture at DEBUG level
    # spanning a full reload: `sensor.nimbus_solver_quality_report`'s own
    # solve-cycle output (`ha_post_state()` in solver_writer.py) has a RAW
    # `hass.states.async_set()` fallback for whenever no SensorEntity has
    # registered itself as that entity_id's push handler yet (see
    # register_entity_handler()'s own docstring) -- originally meant only
    # for entity_ids that will NEVER have a native handler (the standalone/
    # cron/addon deployment). `async_unload_entry()` below calls `hass.
    # config_entries.async_unload_platforms()` FIRST, which tears down
    # every entity (unregistering its solver_writer handler as it goes --
    # see sensor.py's own `async_will_remove_from_hass()`) -- but the
    # periodic-solve cron timer, the optional price-watcher listener, and
    # the startup-retry task are all cancelled via `entry.async_on_unload()`
    # callbacks, which HA core (`config_entries.py`'s own `ConfigEntry.
    # async_unload()`) only processes AFTER our `async_unload_entry()` has
    # already returned -- confirmed by reading that method's source
    # directly, not guessed. That leaves a real, if narrow, window where
    # one of those OLD triggers can still fire a solve after its entity's
    # handler has just been unregistered, land on `ha_post_state()`'s raw
    # fallback, and write a state with no `ATTR_RESTORED` flag -- which
    # `EntityPlatform._entity_id_already_exists()` treats as a genuine
    # occupant, not a safely-overwritable restored placeholder. The FRESH
    # entity this same reload creates a moment later then collides with
    # that raw ghost state and gets rejected ("does not generate unique
    # IDs... ignoring <entity_id>"), exactly matching every live
    # reproduction this session (both a cold boot and a plain reload).
    #
    # Real fix: async_unload_entry() below now explicitly cancels the
    # periodic-solve timer / price-watcher listener / startup-retry task
    # itself, BEFORE tearing down platforms -- not relying on entry.
    # async_on_unload()'s later timing at all. See that function's own
    # comment for the detail. Defense in depth: solver_writer.ha_post_
    # state() also now skips (rather than raw-writes) its fallback for the
    # small, closed set of entity_ids sensor.py DOES register a native
    # handler for, so even an already-in-flight solve that started before
    # cancellation can no longer poison the state machine.
```

## Line 956: above `old_unsub = _solver_timer_unsub.pop(entry.entry_id, None)`

```text
    # Native Solver runtime (2026-08-22) -- see solver_runtime.py's own
    # module docstring for the full "why this exists" story. Scheduled
    # unconditionally, at the hub level (there is only ever one hub) --
    # solver_runtime.async_run_solve() itself already handles "Solver
    # settings not configured yet" gracefully (a clear, expected log
    # line, not an error), so scheduling it before the wizard's been run
    # is safe, not premature. entry.async_on_unload() with the interval
    # tracker's own returned unsubscribe callable is the same, already-
    # proven pattern as the update listener two lines above -- correctly
    # cancels the timer on unload/reload, no leaked callback.
    # Idempotent: cancel any timer already registered for this entry_id
    # before creating a new one -- see _solver_timer_unsub's own comment
    # above for why a second one can otherwise end up coexisting with the
    # first. Both async_track_time_interval() and async_track_utc_time_
    # change()'s own returned unsub are safe to call more than once (a
    # plain listener-removal, no-ops if already removed), so the entry.
    # async_on_unload() registration below still applies cleanly on top
    # of this.
```

## Line 995: above `unsub_periodic_solve = async_track_utc_time_change(`

```text
    # Phase-locked to the NEM 5-minute boundary + 30s (issue #244), not a
    # free-running interval -- see _SOLVER_CRON_MINUTES/_SOLVER_CRON_SECOND's
    # own comment above for why. AEST (this project's only real deployment
    # timezone so far) is a whole-hour, no-DST offset from UTC, so matching
    # on UTC minute/second here lands on the same wall-clock :00/:05/...
    # boundary a local-time match would -- local=False (the default) is
    # deliberately NOT switched to local=True, since that would need to
    # re-derive on every DST transition in a timezone that has one.
```

## Line 1069: above `from homeassistant.helpers.event import async_track_time_interval`

```text
    # One immediate cycle at setup too, in the background -- so a fresh
    # install (or a restart) doesn't sit with an empty forecast for up to
    # a full 5-minute cron period before anything shows up. Retried a
    # bounded number of times (see _STARTUP_RETRY_ATTEMPTS/_DELAY_SECONDS'
    # own comment above) since the very first attempt can race the
    # required number.nimbus_solver_* entities' own restore-from-registry
    # on a real HA restart -- a single lost race otherwise meant waiting
    # out the full periodic cron interval for a second chance.
    #
    # Real bug found in CI (2026-08-30, same day this retry loop shipped):
    # this task was never registered for cancellation on unload, unlike
    # the periodic-solve timer's own unsub two lines above. A bounded
    # retry loop that can genuinely sleep for up to
    # _STARTUP_RETRY_ATTEMPTS * _STARTUP_RETRY_DELAY_SECONDS (90s) is a
    # fundamentally different risk than the old single-shot call it
    # replaced -- if the config entry unloads/reloads while a retry is
    # still sleeping (a real HA integration-test harness reproduced this
    # directly: setup called repeatedly with no Solver ever configured,
    # so every single attempt returned False and the loop ran its full
    # course every time), the task keeps running and sleeping against a
    # hass instance that may itself be mid-shutdown, with nothing to stop
    # it. entry.async_on_unload(task.cancel) fixes this the same way the
    # periodic timer's own unsub already does -- cancelling an
    # in-progress asyncio.sleep() raises CancelledError immediately,
    # which naturally unwinds the loop with no special handling needed.
    #
    # Follow-up (same day): cancel-on-unload alone wasn't the full fix.
    # hass.async_create_task() is *tracked* by hass.async_block_till_done()
    # -- a real HA integration test that calls block_till_done() after
    # setup (before any unload ever triggers the cancel above) still
    # blocks for this task's full retry duration. hass.async_create_
    # background_task() is HA's own documented API for exactly this shape
    # of work: same fire-and-forget task, but explicitly exempt from
    # async_block_till_done()'s wait, while still being auto-cancelled on
    # HA shutdown. Requires a `name=` (used in log messages if the task
    # raises).
    # nimbus issue #1562: say so when a power input is an energy counter
    # (Wh/kWh). Run once HA has started, so every sensor's unit is known --
    # at setup time a sensor from a later-loading integration does not
    # exist yet, and a missing entity is not known to be wrong.
```

## Line 1153: above `startup_solve_task.cancel()`

```text
        # Real bug found in CI the same day this line was first written:
        # entry.async_on_unload(startup_solve_task.cancel) directly (i.e.
        # registering the bound method itself) crashes real HA core's own
        # ConfigEntry._async_process_on_unload(), which schedules ANY
        # truthy return value from an on-unload callback as if it were a
        # coroutine (`self.async_create_task(hass, job, ...)` where `job`
        # is that return value) -- not just an actual coroutine object.
        # asyncio.Task.cancel() returns True exactly when the task is
        # still running at the moment of cancellation (False if it had
        # already finished) -- i.e. precisely the case this cancellation
        # exists to handle. A bare wrapper with an explicit `-> None`
        # return avoids ever handing HA a truthy value to misinterpret.
```

## Line 1256: above `switch_state = hass.states.get(f"switch.nimbus_{CONF_SOLVE_ON_PRICE_CH`

```text
    # Issue #232 follow-up: read the live switch first, so a dashboard
    # toggle takes effect immediately with no hub reload. Fall through
    # to entry.options only if the entity hasn't been created yet (the
    # first async_setup_entry pass before switch.py's platform finishes
    # setting up, or a not-yet-migrated install still on the wizard-only
    # code path from before this change) -- switch.py's own async_added_
    # to_hass seeds the entity from entry.options in the same shape,
    # so this fallback is exact.
```

## Line 1312: above `pending: dict[str, object] = {`

```text
    # A small in-flight box so a burst of state_changed events within the
    # debounce window coalesces into a single solve. asyncio.Handle is
    # HA's own scheduler primitive -- created here by hass.loop.call_later
    # rather than asyncio.get_event_loop() so a test harness that swaps
    # the loop still lands on the harness's own loop.
    # "entity_id"/"changed_at" (issue #294): the debounced solve this
    # triggers needs to know WHICH price sensor's change actually caused
    # it and WHEN that sensor's own state last changed, to publish
    # sensor.nimbus_solver_price_response_latency's attributes. Captured
    # in _on_price_change below on every event within the debounce
    # window, so by the time _fire_solve actually runs, this holds the
    # LAST (most recent) triggering event in whatever burst caused the
    # eventual solve -- the same coalescing the debounce itself already
    # does for the solve trigger.
    # "trigger_kind" (nimbus issue #633, Mark Purcell): see _on_price_
    # change's own comment below for why this isn't always "price_change".
```

## Line 1371: above `is_real_price_change = (`

```text
        # nimbus issue #633 (Mark Purcell, real repro: latency spikes to
        # 280-600s every 5 minutes, matching "solve time minus the last
        # REAL price change" to the second): HA fires state_changed for
        # attribute-only updates too (a configured price integration's
        # own coordinator often refreshes forecast attributes a few
        # seconds before the interval price itself changes). On such an
        # event, new_state.last_changed still points at the PREVIOUS
        # real value change -- using it here would make this solve's own
        # latency sample measure "how stale was the last real price",
        # not "how fast did the solver respond to this event". A missing
        # old_state (the very first event this listener has ever seen
        # for this entity) is treated as a real change -- there's no
        # prior value to compare against, and the conservative default
        # (record the sample) matches this project's own fail-open
        # convention elsewhere rather than silently dropping it.
```

## Line 1494: above `await solver_runtime.wait_for_in_flight_solve()`

```text
    # nimbus issue #365 (Mark Purcell, codebase review), item 4 -- the
    # harder half. Every solve TRIGGER above is now cancelled/unsubbed, so
    # no NEW cycle can start, but a cycle already running on its worker
    # thread at this exact moment is not interruptible (cancelling
    # old_startup_task above only cancels whichever Task was AWAITING it,
    # not the thread itself) -- it would otherwise keep calling
    # ha_post_state() after platforms below are torn down. Waiting here,
    # before teardown, means an already-in-flight cycle finishes cleanly
    # against a still-fully-set-up entry instead of racing it.
```

## Line 1515: above `solver_runtime.reset_module_state()`

```text
        # nimbus issue #365 (Mark Purcell, codebase review), item 4: a
        # genuine unload used to leave solver_runtime's own module
        # globals (_solver_writer's set_native_hass() binding, the last-
        # solve timestamp, the missing-dependency notification flag)
        # alive for the rest of the PROCESS, not scoped to this entry --
        # since this integration is single_config_entry, the only way a
        # "different" entry ever exists is a genuine remove-then-re-add,
        # but even that rare case deserves a clean slate. See reset_
        # module_state()'s own docstring for the full reasoning.
```

## Line 1525: above `services.async_unregister_services(hass)`

```text
        # nimbus issue #365 (Mark Purcell, codebase review), item 1: this
        # integration only ever has at most one entry (single_config_
        # entry: true), so a successful unload always means no surviving
        # sibling entry still needs these services -- see async_
        # unregister_services()'s own docstring for the full reasoning,
        # including why this is also safe to do unconditionally on a
        # plain reload (async_setup_entry()'s own registration call is
        # idempotent, re-adding them immediately after).
```
