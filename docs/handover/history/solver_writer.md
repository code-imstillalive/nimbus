# solver_writer.py: comment history

Moved out of `custom_components/nimbus_load/solver_writer.py` by #2289 item 7 (agreed with Mark Purcell, 7 Nov 2026), verbatim. Each block's rule, reason and issue number stay in the code as a short comment; the narrative it carried is kept here, keyed by the code line it sat above. Line numbers are the file's at `main` before the move.

## Line 504: above `import numpy as np`

```text
# Real, confirmed-live bug (2026-08-17): this script's own docstrings
# used to assert "this NUC runs Australia/Brisbane" and relied on plain
# `.astimezone()` (no argument -- converts to whatever the SYSTEM's own
# local timezone resolves to) throughout, on that assumption. Confirmed
# live this was WRONG: a real deploy run's own generated_at (compared
# directly against the real wall-clock AEST time at that exact moment,
# from the deploy conversation's own timeline) showed a UTC offset
# (+00:00), not +10:00 -- meaning `.astimezone()` on the NUC's own cron/
# shell environment silently resolves to UTC, not AEST, most likely
# because the environment this script actually runs in (cron, or the
# interactive shell used to test it) doesn't carry a correctly-resolved
# TZ, even though the box's own /etc/timezone may well be set correctly
# for everything else. Consequence: EVERY hour-of-day decision in this
# file (import_fee_rate's TOU schedule, battery_discharge_cost_rate,
# battery_salvage_value_rate, and resample_p2p_forecast's own P2P-window
# check) was evaluating against UTC hour, a consistent 10-hour offset
# from the real intended AEST hour -- e.g. the real 17:00-24:00 P2P
# window was actually being checked as UTC 17:00-24:00, which is AEST
# 03:00-10:00 the NEXT day.
#
# Fix: never trust the system's own local-timezone resolution. Every
# real-local-time value in this file is built from an explicitly-resolved
# zone (zoneinfo, Python 3.9+ stdlib), never a bare `.astimezone()`.
#
# nimbus issue #347 (Mark Purcell, 2026-09-03): this was hardcoded to
# Australia/Brisbane -- correct, and harmlessly DST-free, for THIS
# household's own install, but every hour-of-day decision in this file
# (TOU fee lookup, the P2P window, midnight SoC anchors, fixed-export
# blocks, quality-report day boundaries) ran in AEST for every other
# install too. A Sydney/Melbourne user during AEDT got all of these one
# hour late; a non-AU install was off by many hours -- the same bug
# class as the 2026-08-17 incident above, just moved from "the system's
# own tz" to "one specific household's tz" instead of resolving the
# REAL configured one. LOCAL_TZ now resolves in priority order: the
# NIMBUS_SOLVER_TIMEZONE env var (explicit override, works identically
# in standalone/addon/native mode) if set; otherwise this default
# (unchanged behaviour for this household and anyone else who never
# sets the env var) until set_native_hass() below runs, at which point
# native mode re-resolves it from hass.config.time_zone -- the real,
# already-configured value every HA install has, needing zero new user
# configuration. REST/standalone/addon mode has no equivalent live
# `hass` to ask, so it keeps whatever LOCAL_TZ already resolved to here
# (the env var, or this default) for the whole run.
#
# Deliberately NOT addressed here (nimbus issue #347's own second,
# separate finding, scoped out of this fix): wall-clock timedelta
# arithmetic on a tz-aware LOCAL_TZ datetime is genuinely unsafe across
# a real DST transition (a `timedelta` added to a ZoneInfo-aware
# datetime moves in WALL-CLOCK terms, not real elapsed time) -- the ML
# grid, solver period_starts, and per-day P2P cap grouping all build
# their own time grids this way. Harmless for Brisbane (no DST) and
# therefore never live-verified against a real transition by this
# household's own install; a real fix needs the grid built in UTC and
# converted to local only for feature/day-keying, per the issue's own
# suggested fix -- a materially larger, riskier change to the core ML/
# solver time-grid construction, deliberately left for a dedicated pass
# rather than rushed alongside this narrower, lower-risk timezone-
# resolution fix.
```

## Line 669: above `PLAN_STATE_PATH = os.environ.get(`

```text
# Real state file for plan-to-plan stability (2026-08-16, real finding:
# two solves 4 minutes apart, same code, produced total_cost -$31 vs
# -$15 -- a thin, marginal arbitrage opportunity (buy overnight at
# ~$0.22, sell tomorrow's P2P window at ~$0.336) flipped the LP's whole
# strategy). network.py's own module docstring already documents real,
# tested machinery for exactly this ("the user's own explicit concern:
# 'i do not want a dumb algorithm... clever and responsive but not
# chaotic'") -- this writer just never wired it up. See save_plan_state()/
# load_previous_plan() below. Per the usual /opt-is-root-owned gotcha,
# this file needs a one-time `sudo touch` + `chown` on first deploy.
#
# Env-var-overridable (2026-08-22, same "installable by anyone" reasoning
# as TOKEN_PATH/HA_BASE/NIMBUS_SOLVER_PATH above) -- /opt only makes
# sense on THIS household's own NUC host. Running natively inside the
# Nimbus integration itself (custom_components/nimbus_load/
# solver_runtime.py, same repo) points this at hass.config.path(...)
# instead -- HA's own real, persistent, always-writable storage
# directory, correct on any HA install regardless of platform.
```

## Line 690: above `LOCK_PATH = os.environ.get(`

```text
# Real PID-file overlap guard (2026-08-17, see the deploy docstring's own
# "* * * * *" comment above) -- per the usual /opt-is-root-owned gotcha,
# this file needs the same one-time `sudo touch` + `chown` on first
# deploy as PLAN_STATE_PATH. Same env-var-overridable reasoning as above.
```

## Line 697: above `LOAD_FORECAST_ERROR_NOTIFIED_PATH = os.environ.get(`

```text
# Real bug found live (nimbus repo issue #66, Mark Purcell, 2026-08-23):
# a load-forecast sensor with an unrecognized attribute shape degraded
# or crashed with zero operator-visible signal. The persistent
# notification this sentinel gates (see _notify_load_forecast_error_once())
# fires once per genuinely-new error message, not once ever and not
# every single cron cycle -- same env-var-overridable /opt-default
# convention as PLAN_STATE_PATH/LOCK_PATH above.
```

## Line 709: above `TIER0_MINUTES = 5.0  # ultra-fine tier: how far out 1-min resolution runs`

```text
# Tiered horizon (2026-08-16, real ask: "how about 5 days forecast?" /
# "how about 96hrs?"), same real architecture HAEO's own horizon already
# uses ("minute-resolution tiers for the first 5 minutes, then 5-min
# tiers, then hourly" -- this project's own documented HAEO design).
#
# Real, precisely measured finding that drove this: this solver's own
# dense-tableau simplex (see lp.py) scales roughly CUBICALLY in period
# count (measured: 24 periods=0.09s, 48=0.59s, 96=4.41s, 144=14.35s,
# 192=36.84s -- iteration count grows near-linearly, but time-per-
# iteration grows near-quadratically, since the dense tableau's own
# per-pivot cost is O(rows x cols) and both dimensions scale with period
# count). A flat, uniform 15-min grid across a real 96h horizon needs 384
# periods -- measured at 197s, too close to a 15-min cron cadence to
# trust. Tiering the SAME 96h into fine-near/coarse-far periods instead
# needs only ~168 periods (~23s estimated from the measured curve) for
# the identical real-world coverage.
#
# Also matches where the real underlying data itself runs out of
# precision, so coarsening far-out periods doesn't discard any real
# signal it never had: solar/load forecasts genuinely cover the full 96h
# (Nimbus's own Forecaster), but the LocalVolts price forecasts only
# cover ~12h (import) / ~36h (P2P export) -- resample_forecast() already
# just holds the last known value flat past that point regardless of how
# many periods represent it, so fewer, coarser periods there is more
# honest, not less accurate.
# 2026-08-17, direct real ask: "why is there 15min spans, not 1min...
# for first 5min and then every 5min" -- three tiers now, not two.
# TIER0: the first 5 real minutes at 1-min resolution (the genuinely
# imminent, most decision-relevant window). TIER1: 5-min resolution
# (was 15-min) for the SAME real 24h span tier1 has always covered.
# Real, accepted tradeoff, not free: this roughly DOUBLES the total
# period count (tier1 alone goes 96 -> 288 periods), and the real
# measured LP solve time at the OLD ~169-period size was already
# 44.95s-52.31s against a 60s cron tick (see acquire_lock()'s own
# comment). A slower solve here doesn't break anything -- the same
# overlap lock that already exists for exactly this reason just skips
# a tick cleanly if the previous run is still solving, degrading from
# "every minute" to "every ~1.5-2 minutes" in the worst case rather
# than ever running two solves concurrently or crashing.
```

## Line 780: above `# nimbus issue #348 (Mark Purcell, 2026-09-03 codebase review): several`

```text
# Real, bill-confirmed TOU network rates and certificates rate (2026-08-16,
# real ask: "it needs ot be super accurate") -- reused directly from this
# project's own already bill-verified lv_costs.yaml rate table (Energex
# NTC 6900 Residential TOU Energy), not re-derived. Baked directly into
# import_price[t] below (not just reported after the fact) so the LP's
# own DISPATCH decision correctly avoids importing during real peak
# hours, not just the reported total_cost number.
# REMOVED (2026-08-22, direct household demand: "how do they configure
# fees column... I TOLD U NO HARDCODED INPUTS - this has to work as
# user setting"). The plain NETWORK_ENERGY_*/CERTIFICATES_RATE Python
# constants that used to live here (this household's own real Energex
# NTC 6900 rates) are GONE -- replaced by import_fee_rate(), below,
# which reads real, live, dashboard-editable number.nimbus_solver_
# network_fee_*/flat_fee_rate entities instead (same "up to 3
# configurable TOU blocks" shape already proven by the P2P blocks).
# Genuinely portable now: someone on AGL/Amber/Origin/any other
# retailer sets their OWN real tariff on their OWN dashboard, nothing
# to hand-edit in this file.
```

## Line 799: above `_household_specific_overrides_logged = False`

```text
# nimbus issue #348 (Mark Purcell, 2026-09-03 codebase review): several
# real, deliberate household-specific tuning choices in this file
# silently override or ignore an install's own wizard-configured values
# with zero visibility into that happening.
#
# Three of the four findings are now real, wizard-configurable fields,
# removed from the list this function warns about below as each was
# fixed: the fixed daily charge and the post-midnight self-consume
# window (see number.py's own CONF_SOLVER_FIXED_DAILY_CHARGE/
# CONF_SOLVER_POST_WINDOW_SELF_CONSUME_HOURS comments, fixed 2026-09-04);
# the battery_discharge_cost_rate()/battery_salvage_value_rate() day/
# night schedule (see scheduled_discharge_cost_rate()'s own docstring,
# near DISCHARGE_COST_SCHEDULE_BLOCK_KEYS, fixed 2026-09-04); and the
# "generic" price-forecast-array field's LocalVolts-specific
# costsflexup/earningsflexup key parsing (now solver_price_forecast_
# array_import_key/export_key, fixed 2026-09-04). None of these are
# "silent" anymore -- each is a real, visible field (CORRECTED 13 Oct 2026:
# the discharge-cost/salvage-value schedule never was -- no wizard field or
# entity writes its keys -- and it now defaults to the visible settings, see
# DISCHARGE_COST_SCHEDULE_BLOCK_KEYS), even though that schedule and the
# costsflexup/earningsflexup keys both still only APPLY on the
# has_price_forecast_array branch (a deliberate, unchanged scope, not a
# new gap -- a generic install without that sensor was never affected by
# either).
#
# The one remaining finding (the P2P matched-rate sensor's own fixed
# 17:00-24:00 window, independent of the household's own configured P2P
# block hours) is still a genuine, considered tradeoff not yet
# generalised -- this module-level flag + _log_active_household_
# specific_overrides_once() (called once near the top of main(), see
# that function's own call site) keeps it visible in the log at
# startup, by name, instead of only discoverable by reading this file's
# source.
```

## Line 938: above `P2P_MATCH_FRACTION_FALLBACK = 0.65`

```text
# Real empirical fallback if the live confirmed-history sensor is
# unavailable for some reason -- roughly the 13-day all-time average
# (0.686) as of 2026-08-16, NOT the more-accurate live-computed recent
# average this writer normally uses (see p2p_match_fraction() below).
# Still computed/reported for informational context (pushed as
# p2p_match_fraction in the sensor's own attributes) -- no longer used
# to PRICE the LP, see export_bonus_price/export_bonus_volume_kwh below.
```

## Line 948: above `P2P_RECENT_AVG_VOLUME_FALLBACK_KWH = 60.0`

```text
# Real empirical fallback for the two-tier export bonus's own volume cap
# (see p2p_recent_avg_volume_kwh() below) -- roughly matches this
# household's own long-documented ~60-65kWh/night real P2P delivery
# (the reference household's session history).
```

## Line 954: above `# Real, known, permanent inverter self-consumption bias (2026-08-17,`

```text
# Real per-load demand, OPTIONALLY summed from a household's own
# individually-forecasted circuit breakers instead of one whole-house
# forecast sensor (2026-08-17, direct ask on this project's own
# reference install: "i was hoping not to need whole house load if all
# 18 loads could be individually input and measured and added into a
# total"). Real, richer-than-a-single-entity signal WHEN a household
# fills this in via the wizard -- a genuine per-circuit health dot, and
# a real cross-check against a separate whole-house meter (see
# whole_house_cross_check_sensor below, reported but never used to
# price/dispatch anything -- a real divergence between "sum of
# configured circuits" and "one real whole-house meter" is itself
# useful, honest information worth surfacing, not hiding). Genuinely
# optional: a fresh install with neither field filled in falls back
# cleanly to the wizard's own single load-forecast-sensor field below.
#
# 2026-08-20: the raw SOURCE sensor is what a household configures, not
# its own forecast entity_id -- the forecast entity name is derived
# from it at read time (see below) specifically so a future reconfigure
# (task #99's own auto-rename mechanism) can never leave this cross-
# check silently pointing at a dead, renamed entity_id again. Real,
# live-confirmed incident that shaped this design (2026-08-20, this
# project's own reference install): its "Whole House" Power Signal was
# reconfigured from sensor.logger_load_power (the raw, noisy Modbus
# meter -- see this project's own CLAUDE.md, "real P2P-window grid
# spikes root-caused") to sensor.cb_total_combined_power_adjusted_kw.
# Task #99's auto-rename correctly renamed the live forecast entity to
# match -- but at the time, this writer's own hardcoded cross-check
# pointer was still the OLD literal forecast entity_id, confirmed 404
# the very next run.
#
# Real bug found live (nimbus repo issues #56/#60, reported by an
# independent installer, 2026-08-22): these two used to be hardcoded
# Python constants here -- one household's own 18 real circuit entity
# IDs and one household's own real whole-house cross-check sensor. That
# meant every OTHER install summed 18 nonexistent entities every solve
# cycle (18 real 404 warnings per cycle, 216/hour at the default 5-min
# cadence) and the config-flow's own single-sensor fallback field
# below was permanently unreachable dead code for anyone but the
# maintainer. Fixed 2026-08-23: both are now read live from cfg
# (Nimbus's own Solver settings wizard) inside main() -- see
# load_forecast_entities / whole_house_cross_check_sensor further down.
# Genuinely empty/None by default for a fresh install; a household that
# wants per-circuit summation and/or a whole-house cross-check fills
# these in explicitly through the wizard.
```

## Line 999: above `# Same real, live config-flow keys as P2P_BLOCK_KEYS above, mirrored`

```text
# Real, known, permanent inverter self-consumption bias (2026-08-17,
# direct household confirmation: "the only thing which differs is
# adjustment sensor of 0.215kw") -- the Sungrow logger's own internal
# accounting draws a constant real ~215W nothing on the Zigbee CB
# network can ever see (it's wired into the inverter itself, not a
# monitored circuit), 24/7. Already a real, established correction in
# this project (the reference household's own adjusted total sensor
# adds the identical constant for the same reason).
# Confirmed live this same session: raw summed-18-circuits (2.18kW) +
# this constant (0.215kW) = 2.395kW, matching the real whole-house
# meter's own live reading (2.4kW) almost exactly -- without this, the
# 18-load sum would silently under-serve real demand by ~215W every
# single period (≈20.6kWh of real, invisible demand across a 96h
# horizon), biasing the LP toward under-provisioning battery/import.
#
# Real PORTABILITY BUG found and fixed (2026-08-24, nimbus repo issue
# #100, Mark Purcell -- an independent installer's own live health-check
# found his own load total's confidence band stuck dead flat at exactly
# 0.215 for 362/363 points, with no household hardware of his own that
# would explain that specific number). This used to be a bare, hardcoded
# module-level constant -- meaning EVERY OTHER Nimbus install got this
# household's own specific 215W bias silently added to their own load
# total and band, whether or not their own hardware has any such bias at
# all. No longer a module constant -- now read live from cfg (see
# main()'s own sum_load_forecasts() call site below), same
# number.nimbus_solver_* config-flow pattern every other economic/
# hardware Solver setting already uses (const.py's own
# CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW comment has the full story).
# 0.0 is the real, portable default for anyone else; this project's own
# reference install needs number.nimbus_solver_inverter_self_consumption_kw
# set to 0.215 once, manually, after this change first deploys -- there
# is nothing in entry.options for a brand-new field to seed itself from.
```

## Line 1180: above `# Block 1 alone reproduces the real historical schedule (a single`

```text
# Real, git-tracked battery cost schedule (config/automations.yaml, "HAEO
# Battery Cost Schedule - 5pm/Midnight/7am") -- reused directly, not
# re-derived. 2026-08-16, direct real finding: this writer used to read
# number.battery_discharge_cost's LIVE value ONCE and apply it flat
# across the whole multi-day horizon -- at whatever time the writer
# happens to run, that's the WRONG value for most of the horizon (e.g.
# captured 0.09, the daytime rate, applied to the real overnight window
# where the deployed automation actually uses 0.01). Confirmed this was
# the real, direct cause of a household reporting the Solver's own plan
# going idle overnight instead of discharging to serve load -- at the
# wrong flat 0.09, discharging looked far less obviously favourable than
# the real 0.01 makes it. charge_cost is deliberately NOT scheduled here
# -- the real automations explicitly never touch it (see automations.
# yaml's own description: "under manual real-time control"), so this
# writer still reads its current live value as a flat scalar, same as
# before. Requires the Solver's own BatteryConfig.discharge_cost to
# accept a real per-period array, not just a scalar (see the separate
# nimbus repo commit "BatteryConfig.charge_cost/discharge_cost: allow a
# real per-period array").
#
# nimbus issue #348 (Mark Purcell, codebase review, fixed 2026-09-04):
# these used to be hardcoded Python constants -- a real, tuned economic
# schedule with zero way for THIS household (let alone anyone else) to
# retune it short of editing source. Now the SCHEMA DEFAULTS for a real,
# optional, wizard-configurable schedule (mirroring the already-
# established solver_network_fee_1/2/3_rate/start_hour/end_hour and
# solver_p2p_block_1/2/3 pattern in this same file) -- every existing
# install (including this one) gets BYTE-IDENTICAL behaviour, since none
# of the new config keys below have ever been set, and `_cfg_num`/
# `_cfg_int` fall back to exactly these same literals whenever a key is
# absent. Kept as named constants (not inlined) so the schema-default
# call sites below read as "the historical schedule," not magic numbers.
#
# 13 Oct 2026: the defaults are now the install's own Discharge Cost and
# Salvage Value settings at every hour, and every block is off. The old
# defaults (0.09 for 07:00-17:00, 0.01 otherwise; salvage 0.30 for a horizon
# ending 17:00-24:00) were HAEO's "Battery Cost Schedule - 5pm/Midnight/7am"
# automations, off on the reference household since 6-7 Sep. No wizard
# field, number entity or setting ever wrote these keys, so the schedule was
# not editable: it silently overrode the visible Discharge Cost (0.01 on the
# reference household) with 0.09 every day from 07:00. Measured by replaying
# production's own solve read-only, 13 Oct 13:1x AEST, with the 12:46 spike
# (sell 43.5c, buy 49.1c) in the current interval: 0 kW sold at 0.09, the
# full 40 kW at 0.01. The quality scorer and counterfactual already used the
# flat setting, so the plan and its own scorer disagreed on every daytime kWh.
```

## Line 1414: above `# stdlib logging.Logger, NOT this file's own print() convention -- deliberately`

```text
# PURE INTEGRATION seam (2026-08-22, direct real-world push: Mark Purcell
# hit a private-repo git-clone-with-no-auth wall trying to install the
# nimbus_solver_app addon (since removed, #357), and separately flagged
# the deeper, correct architectural point -- "EMHASS had the addon, which
# was always a complication for access logs and sending commands... HAEO
# runs as a pure integration." This is the fix: a real, additive
# extension point that lets the EXACT SAME ~2400 lines below run natively
# inside HA Core's own process (via custom_components/nimbus_load/
# solver_runtime.py, same repo), with ZERO behaviour change to the
# existing standalone deployment (cron on this household's own NUC) --
# NATIVE.hass defaults to None (#1437; formerly _NATIVE_HASS), and
# every one of the ~2400 lines below still just calls ha_get(...)/
# ha_post_state(...)/fetch_price_history(...) by name, exactly as it
# always has. Only what THOSE THREE functions do internally branches on
# whether a real hass instance has been injected.
# nimbus issue #1437: the injected `hass` now lives on `solver_shared.NATIVE`,
# a holder whose IDENTITY never changes -- see that class's own docstring.
# `set_native_hass()` below mutates `NATIVE.hass`; nothing rebinds a module
# name any more. `NATIVE.hass is None` = standalone/REST mode (the default).
```

## Line 1479: above `_ENTITY_REAL_IDS: dict[str, str] = {}`

```text
# Real-entity-id side-mapping (2026-08-31, devhub flicker investigation):
# register_entity_handler() below is always called with a fixed, literal
# entity_id string (e.g. "sensor.nimbus_solver_quality_report") -- correct
# for dispatch, since ha_post_state() looks the handler up by that same
# literal string every time. But a handful of functions (publish_daily_
# quality_report / publish_efficiency_backtest_report / publish_nimbus_
# only_soc_counterfactual) ALSO read that same literal entity_id back via
# ha_get() as a cheap "did I already score this" idempotency check --
# silently wrong on any install where that literal name is already
# claimed by something else (confirmed live on devhub: a remote_
# homeassistant mirror of another Nimbus install's identically-named
# sensor wins the plain name first, so the local platform entity gets
# bumped to a "_2" suffix by HA's own dedup). ha_get() has no way to know
# about that bump -- it just reads whatever the literal string currently
# resolves to in the state machine, which is the UNRELATED mirror, not
# this install's own entity. Confirmed live: the mirror's own `latest_
# date` attribute matches "yesterday" almost every day (since it's a
# different, working install), so the idempotency check's fast path
# fires immediately every cycle, thinking it already scored today,
# without ever calling compute_daily_quality_report() again for THIS
# install -- and the local "_2" entity's own freshness stamp only gets
# refreshed on the rare cycle where the fast path happens to also
# succeed in writing something back through it, explaining the
# multi-hour stale/unavailable stretches actually observed.
#
# Fix: register_entity_handler() now optionally records the entity's own
# REAL, HA-resolved entity_id (self.entity_id, captured after async_add_
# entities -- see sensor.py's async_setup_entry) alongside the literal
# dispatch key. resolve_real_entity_id() below lets a self-read use the
# real id when one was recorded, falling back to the literal string
# unchanged for any caller/entity that never registered one (REST-only
# mode, or an entity that predates this fix) -- byte-identical behaviour
# there.
```

## Line 3349: above `def _solar_entries_from_attributes(attrs: dict) -> list[dict] | None:`

```text
# nimbus issue #542 item 1 (Mark Purcell, real household finding): a
# solar-forecast entity configured directly as solver_solar_forecast_
# sensor_1/2/3 was silently dropped from every solve whenever it wasn't
# already shaped as the generic forecast=[{time,value,lower,upper}]
# array every ML-produced Nimbus signal uses -- Solcast's own native
# entities publish detailedForecast=[{period_start,pv_estimate,...}]
# instead, and Open-Meteo Solar Forecast's own entities publish
# watts={timestamp: value}. This writer already knew how to read BOTH
# of those shapes (fetch_solcast_solar_raw()/fetch_open_meteo_solar_raw()
# below), just not when the household pointed a *configured* source at
# them directly rather than relying on the separate auto-include path --
# so the two readers could (and did) disagree about the exact same
# entity. One shared reshape function, used by every solar-source
# reader in this file, closes that gap for good.
```

## Line 3463: above `_SOLAR_SOURCE_WARNED: set[tuple[str, str]] = set()`

```text
# nimbus issue #543 (Mark Purcell, real household finding): a solar
# source dropped from the blend used to warn on EVERY solve -- 205
# copies in 4 hours on one real install (three per 5-min cycle: the
# scheduled solve plus price-change solves), burying the once-per-day
# quality warning (#538) and every genuine transient in the same
# logger. Same #313/#314 "log once per condition" discipline as
# _DONE_CONDITION_WARNED/_LOAD_POWER_SENSOR_UNIT_HINT_LOGGED, keyed on
# (entity_id, reason) so a genuinely NEW failure reason for the same
# entity still gets its own one-time log. Unlike those two, THIS
# condition is worth reporting recovery from (#543's own explicit ask)
# -- a solar source coming back after being down for hours is a real,
# useful signal a household would want to see without grepping the
# log, unlike a misconfiguration that stays broken until a human fixes
# it -- so entries are cleared (not kept forever) the moment the same
# entity_id next succeeds, and that recovery is logged once at INFO.
```

## Line 3556: above `if isinstance(raw_fc, list) and not raw_fc:`

```text
    # Real bug found live (devhub, 2026-08-24, first-ever solve on a freshly
    # configured install): `not raw_fc` is True for BOTH "missing/wrong-shape
    # attribute" and "attribute present, correct type, genuinely empty list"
    # -- a brand-new load subentry's ML forecaster hasn't trained yet and
    # legitimately publishes `forecast: []` for its first several days. The
    # old single combined branch below reported that as "has no usable
    # 'forecast' attribute (list-valued attributes present: ['forecast'])"
    # -- naming the very attribute it just rejected, which reads as a
    # malformed-sensor error when the real, expected cause is "not trained
    # yet." These are genuinely different operator actions (fix your sensor
    # vs. just wait) and need genuinely different messages.
```

## Line 3568: above `if "model_trained_at" in attrs and (`

```text
        # nimbus issue #374 (Mark Purcell, codebase review, #370 residual):
        # this branch used to return the identical message regardless of
        # WHY the forecast is empty -- confirmed live, that message never
        # matched _is_transient_startup_load_forecast_error()'s narrow
        # patterns, so main() treated it as a genuine, persistent
        # misconfiguration and substituted a flat 0.0kW load, publishing a
        # confidently "optimal" plan (idle battery, zero-width cost band)
        # for as long as the forecast stayed empty -- hours, for a
        # subentry whose model was discarded or never trained, not the
        # few-minutes startup race #370 fixed. Only a genuine nimbus
        # forecast sensor publishes `model_trained_at`/`training_points`
        # at all (checked via key presence, not just a falsy value, so a
        # third-party sensor that doesn't publish these keys keeps the
        # ORIGINAL "genuine misconfiguration" behaviour below unchanged --
        # this distinction only ever applies to nimbus's own sensors).
```

## Line 3766: above `nonzero_points = sum(1 for v in load_kw if v > 0.01)`

```text
    # Real bug found live (nimbus repo issue #118, Mark Purcell, a real
    # independent installer's own live health-check, 2026-08-24, direct
    # follow-up to #111): a genuinely common configuration mistake --
    # pointing solver_load_forecast_sensor at Nimbus's OWN household-
    # total aggregator (sensor.nimbus_household_load_total_forecast)
    # instead of a real per-signal forecast entity -- creates a
    # circular reference. With no individual circuits configured, the
    # aggregator's own upstream is empty, so it publishes a real,
    # structurally-valid {time, value} shape (passing every check
    # above) that's near-all-zero except the live "now" anchor point.
    # This function's own docstring used to call that "a valid, if
    # unusual, success" -- true in the abstract, but in practice a real
    # household's consumption essentially never sits at true zero for
    # the vast majority of a multi-day forecast (unlike solar, which
    # legitimately does every night -- see the SEPARATE, deliberately
    # different fallback for that in main()'s solar-fetching block).
    # Left unguarded, this produced a confident-looking "optimal" solve
    # with load_forecast_source_error still None (nothing flagged it) --
    # the household was told the battery could safely export ~$46/day
    # more than it actually could, because the solver believed nobody
    # was consuming anything. Threshold and reasoning are Mark's own
    # proposed fix direction from #118, applied here.
```

## Line 4441: above `# Real, live household automation design (config/automations.yaml):`

```text
# 2026-08-20, direct household finding, live chart evidence: the Solver's
# own proposed P2P-window dispatch was swinging between near-40kW and
# near-zero, chasing whichever 5-min period showed the highest real per-
# interval P2P rate (resample_real_p2p_rate() above, itself a genuine fix
# earlier the same night) -- while the household's own real, live
# automation holds one flat, pre-committed rate for the whole window.
# Direct household explanation: P2P is a matching arrangement where
# CONSISTENCY of delivery is itself part of what earns the rate, not a
# plain price-taking market where each period can be independently re-
# decided. See solver.elements.GridConfig.fixed_export_kw's own docstring
# (nimbus repo) for the full LP-level mechanism this feeds.
#
# Deliberately a bare Python constant, same honest-portability pattern
# 2026-08-21: up to 3 independent, optional fixed-rate P2P delivery
# blocks, read from Nimbus's own sensor.nimbus_solver_config (already
# fetched into `cfg` by fetch_solver_config() -- no separate HTTP call
# needed here at all). Replaces the old single-window,
# household-specific input_number.p2p_grid_export_target_kw +
# hardcoded 17-24h check: that coupled Nimbus's own shadow plan directly
# to this household's real automation's own setpoint entity, which was
# never going to be portable to anyone else's install (a different rate,
# a different window, or MULTIPLE windows needed source editing). The
# real, deterministic p2p_battery_sell_5pm_midnight automation (config/
# automations.yaml, this same repo) is completely untouched by this --
# it's the actual, live, real-money dispatch mechanism, still reading
# its own input_number directly; Nimbus remains purely observational
# either way. This household's own real values (11.5kW, 17-24h) need
# setting once, manually, as Block 1 on the dashboard
# (number.nimbus_solver_p2p_block_1_rate_kw/start_hour/end_hour) --
# nothing carries over automatically from the old entity.
```

## Line 4472: above `def resolve_price_spike_override(`

```text
# Real, live household automation design (config/automations.yaml):
# p2p_battery_sell_end_midnight switches the battery to Self-Consume the
# INSTANT the P2P window closes (00:00:00 sharp, no ramp), and
# p2p_haeo_resume_at_4am hands control back at 04:00:00. This is a
# deterministic SWITCH, not something reasoning about marginal profit --
# real, direct household finding (2026-08-22): Nimbus's own shadow plan
# kept discharging for ~30-40 minutes PAST that real cutoff, because the
# existing terminal_value_period_indices mechanism (solver/network.py,
# nimbus repo) is a SOFT economic nudge, and a soft nudge can always be
# outbid if the real export price is still positive enough to look
# marginally profitable. A hard, deterministic real-world rule needs a
# hard LP constraint to match it, not a stronger nudge.
#
# nimbus issue #348 (Mark Purcell, fixed 2026-09-04): the real hour
# count itself is now a wizard field (number.nimbus_solver_post_window_
# self_consume_hours, const.py's CONF_SOLVER_POST_WINDOW_SELF_CONSUME_
# HOURS) -- read via cfg inside fetch_p2p_fixed_export_kw() below, not a
# module constant, so another install's own real self-consume-switch
# timing can be genuinely different from this household's 4h. 4 stays
# here only as the literal default for an install that hasn't set the
# field, matching const.py's own DEFAULT_SOLVER_POST_WINDOW_SELF_
# CONSUME_HOURS -- byte-identical behaviour for every existing install.
```

## Line 5113: above `if real_masks[0][i]:`

```text
        # PRIMARY-PREFERRING (2026-08-27, nimbus repo issue #239, Mark
        # Purcell, following his own #236 report): whenever the primary
        # source has real coverage at this period, use it UNBLENDED --
        # never averaged against a secondary. Mark's own market-structure
        # argument (see #236's reopen-adjacent comment) is why this isn't
        # a preference call: on any real Australian NEM tariff, import
        # and export both derive from the SAME underlying AEMO spot price
        # for that (region, interval) -- they're not two independent
        # estimators that can legitimately disagree while both are live.
        # A genuine price-cap/spike event shows up as a real tick on the
        # PRIMARY sensor itself (e.g. Amber's own feed-in price hitting
        # $17.50/kWh) -- it doesn't need a secondary (a generic day-ahead
        # AEMO forecast) averaged in to "catch" it, and doing so only
        # ever dilutes/corrupts a real primary tick with a same-instant
        # forecast that isn't what any counterparty will actually settle
        # at. Confirmed exactly this way live: a real Amber value
        # (-$0.0037) averaged 50/50 against a real QLD1 PD7DAY forecast
        # (+$0.6701) inverted the import/export price relationship and
        # the LP planned simultaneous 22kW import + 30kW export as a
        # result.
        #
        # The secondary's own real, legitimate job -- extending the
        # horizon past the primary's own real coverage (Amber's ~24h vs
        # PD7DAY's 7 days) -- is completely unaffected: this only changes
        # behaviour for periods where BOTH the primary and a secondary
        # are real at the same instant (previously averaged, now primary
        # wins outright). Periods where the primary lacks real coverage
        # yet still fall through to blending whichever secondaries ARE
        # real there (unchanged), and periods where NOTHING has real
        # coverage anywhere still fall back to the old equal-weight mean
        # across every source's own held-flat value (unchanged) -- this
        # is the same "everyone's guessing" honest fallback the coverage-
        # aware rewrite for #216 already established, just no longer
        # reached when the primary alone would have been sufficient.
```

## Line 7908: above `_QUALITY_HISTORY_PROVISIONAL_FIELD = "p"`

```text
# nimbus issue #1200: whether this row's five figures were computed
# WITHOUT the day's real P2P settlement, and are therefore provisional.
#
# **Why a row needs this at all.** The settlement lands hours AFTER the
# day is first scored. Measured on the reference household: the
# settlement sensor gained 2026-09-23 at 09:00 local, where that day was
# scored at 00:00. Until it arrives `real_p2p_dollars` is 0, which also
# makes the `real_p2p_volume_kwh > 0.01` bonus gate false -- so `j_ref`,
# `j_ach` and `j_star` go P2P-blind TOGETHER and the day is priced as
# though the household had no P2P arrangement at all. Measured over four
# consecutive real days: EPR 51.9 / 38.5 / 36.4 / 41.6% against settled
# export revenue of $14.91 / $12.71 / $13.46 -- the scorer read WORST on
# the household's best-earning days, and read 88.6% on the one day that
# genuinely under-exported ($4.01). Anti-correlated with the money.
#
# `_keep_published_quality_score()` (#1082) already re-scores the
# CURRENTLY PUBLISHED day hourly while it is provisional. That is not
# enough, and the gap is structural rather than a tuning problem: its
# retry is reachable only while `latest_date == yesterday_key`, so it
# expires at local midnight. On three consecutive measured days the
# retry's last firing was 06:00 against a 09:00 settlement -- three
# hours early -- and the row was then frozen P2P-blind for good.
#
# Recording provisionality IN THE ROW removes the deadline: the row
# still says "these numbers are missing their settlement" tomorrow, next
# week, and after a restart, so the repair no longer has to win a race.
#
# **Written ONLY when provisional, the opposite convention to `"r"`
# above, and the reason is the byte budget**: this rides in the payload
# #944 measured at 20,738 bytes against the recorder's 16 KB cap. A key
# present only on days awaiting settlement costs nothing on the rows
# that are already correct, and self-clears when the day is re-scored
# with its real figures. Absence therefore means "not provisional, or
# written before this existed" -- safe here in a way it was not for
# `"r"`, because the repair sweep reads an absent key as "nothing to
# do": a wrongly-absent key costs a missed repair, never a wrong one.
```

## Line 7946: above `_QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD = "f"`

```text
# nimbus issue #937: this day's DAY-AHEAD `nimbus_value_add_dollars` --
# `j_persistence - j_forecast`, so NEGATIVE means naive persistence produced a
# cheaper real outcome than the ML load forecaster did.
#
# **Why a row needs this, and why stage 3 was not enough.** #1307 made the
# native path publish the day-ahead decomposition, which removed the reason
# there were no days. It did not make them accumulate. The decomposition is a
# headline attribute describing `latest_date` alone, so tomorrow's 06:00 scoring
# overwrites today's number and nothing keeps it -- which is the same
# retention-not-publication defect `"r"` above exists to fix, one field over.
#
# That matters more here than anywhere else in this row, because #937's own
# top-priority action is literally "More days":
#
#     days scored 14 / naive persistence beat Nimbus's forecast 11 /
#     mean nimbus_value_add_dollars -$0.71/day / worst -$3.42
#
# Those fourteen days were readable only because the standalone cron writer of
# the time put a `forecast_regret` sub-dict into `day_entry`, and the series
# stopped the day the reference household moved to the native path. A window
# like it cannot be rebuilt from headline attributes at all: they hold one day.
#
# **One character and one number, not the sub-dict.** The cron-era sub-dict is
# what the five-key allow-list above was written to exclude, and correctly --
# rows are kept small enough that a year of them fits in one attribute payload.
# Measured on the reference household 2026-09-27: the quality report's whole
# attribute payload is **22,750 bytes** and `history` is **1,548** of it across
# 11 rows, ~140 bytes/row. `"f":-0.7123` costs ~13, so at the 60-day cap this
# adds ~780 bytes to a 8.4 KB table -- against the eight separate figures a
# full `forecast_regret` sub-dict would have cost per row.
#
# The attribution terms (level/shape/solar) deliberately stay headline-only.
# They answer "why was this day bad", which is a question about one day; the
# value-add answers "is the forecaster worth using", which is only answerable
# over a window, and is the one the selection policy consumes.
#
# **Written ONLY when the decomposition actually computed**, the same
# convention as `"p"` and for the same byte reason: a day with no forecast
# snapshot, or with unusable previous-day history, has no verdict, and a
# fabricated 0.0 there would read as "the forecaster exactly matched
# persistence" -- the confident-wrong-number failure
# `_day_ahead_forecast_regret_attributes()`'s own six reason codes exist to
# prevent. Absence means "not computed, or written before this existed".
```

## Line 8477: above `SOLAR_DELIVERY_RATIO_PATH = os.environ.get(`

```text
# Nimbus issue #128 (Mark Purcell, 2026-08-25): "switch.solar_curtailment
# doesn't detect implicit inverter AC-clipping" (#114's own curtailment
# switch only sees EXPLICIT curtailment control -- an inverter silently
# capping AC output because real DC generation exceeds its own AC rating
# is invisible to it). His own proposed fix, agreed to and built here
# exactly as specced: "solar_delivery_ratio = rolling_avg(actual_solar_kw
# / forecast_solar_kw) over the last N hours where forecast > 5 kW."
#
# Genuinely optional and fully generic: reuses the SAME
# solver_solar_power_sensor wizard field already added for the EPR
# quality report (#162) -- no new config surface needed. A household
# with it unset gets a complete no-op, matching every other optional
# diagnostic in this file.
#
# Env-var-overridable /opt-default persisted state, same convention as
# PLAN_STATE_PATH/LOCK_PATH/LOAD_FORECAST_ERROR_NOTIFIED_PATH above --
# solver_runtime.py's own set_default_env_vars() points this at HA's
# real storage directory for the native in-process path.
```

## Line 8894: above `_POWER_SENSOR_DISCOVERY_LOGGED: set[str] = set()`

```text
# nimbus issue #768 (Mark Purcell, 2026-09-14): "MQTT heat pump device
# has a power sensor, please use it, through auto discovery."
#
# Context: #809 removed `power_sensor` as a wizard field and defaulted
# done_entity/temperature_entity to the load's own device_entity -- but a
# water_heater/climate entity cannot report its own draw, so there was no
# equivalent default and the field stayed unset. Consequence, confirmed
# live on Mark's install: `_async_fetch_thermal_history()` got
# power_sensor=None, returned [], `learn_thermal_rates()` found no
# segments, and `thermal_rates_source` read "fallback" -- meaning the
# real HWS was being scheduled off DEFAULT_HEATING_RATE_C_PER_KWH /
# DEFAULT_IDLE_DECAY_C_PER_HOUR, generic constants rather than its own
# tank. #800's whole value proposition was silently not delivering on
# the one real thermal load that exists.
#
# Discovery is via the DEVICE REGISTRY, never the entity's name. A
# name-shaped guess ("sensor.<device slug>_power") would work on Mark's
# `water_heater.wwk302` -> `sensor.wwk302_power` and fail on anything
# else, and this project has already had that exact lesson: the Power
# Signal `signal_role` field exists precisely because naming could not
# reliably distinguish a battery/solar/grid sensor on real hardware
# ("Combined Total DC Power" has no "solar" in it). Same rule here --
# the device registry knows which entities belong to the same physical
# device, so ask it.
#
# Ambiguity is NOT resolved by guessing. Zero matches, or more than one
# (a device exposing per-phase power, say), returns None and logs once;
# the explicit CONF_CONTROLLABLE_LOAD_POWER_SENSOR override still exists
# via the set_controllable_load service for those cases. Silently
# picking one of several would be the same class of confidently-wrong
# behaviour #118 already cost this project a $46/day misplan over.
```

## Line 9348: above `_LOGGER.warning("Nimbus: daily quality report publish failed: %s", e)`

```text
        # 2026-08-31: previously a bare `pass` -- made the entity-id-
        # collision incident this file's own resolve_real_entity_id()
        # fixes completely invisible in the log for days (confirmed live
        # on devhub: 200+ recent log lines matching "nimbus", zero
        # exceptions, zero tracebacks, because every failure here was
        # silently swallowed). Logging costs nothing towards "must never
        # break the real solve" -- it's still caught and ignored either
        # way -- but now a future failure of this specific publish is
        # actually diagnosable instead of only visible as a stale sensor.
```

## Line 9383: above `try:`

```text
    # Same "never break the real solve" wrapping as the two publishes
    # above -- see publish_nimbus_only_soc_counterfactual()'s own
    # docstring (2026-08-25, "i want u to build that into devbox
    # package").
```

## Line 9390: above `_LOGGER.warning("Nimbus: counterfactual SoC publish failed: %s", e)`

```text
        # 2026-08-31: see publish_daily_quality_report()'s own matching
        # comment -- same "bare pass hid a real, diagnosable bug for
        # days" lesson, now logged instead of silently swallowed.
```

## Line 9395: above `try:`

```text
    # Same "never break the real solve" wrapping -- see
    # publish_efficiency_backtest_report()'s own docstring (2026-08-25,
    # the "outstanding, unique" backtesting-engine ask).
```

## Line 9401: above `_LOGGER.warning("Nimbus: efficiency backtest publish failed: %s", e)`

```text
        # 2026-08-31: see publish_daily_quality_report()'s own matching
        # comment -- same "bare pass hid a real, diagnosable bug for
        # days" lesson, now logged instead of silently swallowed.
```

## Line 9506: above `_load = load_inputs.build_load_arrays(cfg, grid_times, n_periods, now)`

```text
    # Real household demand. OPTIONAL, richer path: sum a household's own
    # individually-forecasted circuits, read live from cfg (the Solver
    # settings wizard's own solver_load_forecast_entities field, 2026-08-23
    # fix for nimbus repo issues #56/#60) instead of one opaque whole-
    # house entity. When filled in, this is genuinely richer than a
    # single-entity config field could express (a real, live health dot
    # per circuit, a real cross-check against the whole-house meter
    # below). Genuinely empty by default -- a fresh install falls
    # straight to the single-sensor fallback below, same simple single-
    # entity pattern already used for solar above.
    # nimbus issue #735 stage 2: the load-forecast slice, extracted into
    # solver_inputs/load.py. Twelve outputs against solar's three, hence a
    # dataclass rather than a tuple. summed_18_now_kw and load_kw[0] are
    # BOTH carried deliberately -- they differ on any install with the
    # whole-house cross-check configured, and that difference is #100.
```

## Line 9541: above `solver_publish.publish_household_load_total_forecast(`

```text
    # Real, standalone Nimbus entity for the summed 18-load total
    # (2026-08-17, direct ask: "like haeo concept nimbus should sum up
    # all sub sensors into one nimbus entity") -- published the SAME
    # REST-push way every other computed Solver sensor in this file
    # already is (sensor.nimbus_solver_battery_forecast, sensor.
    # nimbus_solver_quality_report), not a new custom_component entity:
    # zero added risk to the live, deployed Nimbus integration itself,
    # reusing a pattern already proven dozens of times this project.
    # Pushed BEFORE the real solve below so it reflects this run's own
    # real inputs even if the LP itself later fails for an unrelated
    # reason (price data, battery config, etc.) -- this sensor's own
    # correctness never depends on the solve succeeding.
    #
    # failed_load_entities: real, honest list of which (if any) of the
    # 18 circuits were unavailable and defaulted to 0.0 this run (direct
    # ask: "the warning would appear in the topology card... green/red
    # dot?") -- exposed here so a future topology-card change can cross-
    # reference this list against its own already-built per-load health
    # dots (sibling repo's own topology-card.js, PR #611) without
    # separately polling all 18 entities itself. Not yet wired into the
    # topology card's own JS -- that's a real, separate follow-up.
```

## Line 9580: above `solar_delivery = _publish_side_reports(cfg, cfg_unmoded, now, grid_times, solar_kw)`

```text
    # Purely cosmetic devhub dashboard sensor (2026-08-25) -- never
    # referenced below, never feeds the LP. Wrapped exactly like
    # _notify_load_forecast_error_once() above: a failed weather-mirror
    # publish must never be allowed to break the actual solve.
    # nimbus issue #735 stage 6: the six non-essential publishes that
    # used to sit inline here. See _publish_side_reports() for the
    # measured seam and the shared 'must never break the real solve'
    # contract they all carry.
```

## Line 9590: above `price_forecast_sensor = cfg.get("solver_price_forecast_array_sensor")`

```text
    # Two paths, gated on whether a rich, forecast-array-shaped price
    # sensor is actually CONFIGURED (CONF_SOLVER_PRICE_FORECAST_ARRAY_
    # SENSOR) -- NOT, as of the 2026-09-02 hardcoded-foreign-entity
    # audit, on whether a specific LITERAL entity (sensor.localvolts_
    # price_forecast) happens to exist. Real households outside this
    # project's own setup can leave this blank, and that's fine: the
    # whole point of 2026-08-20's config-flow work (and this 2026-09-02
    # follow-up) is that the Solver still runs correctly for them, just
    # without this household's own extra sophistication -- see each new
    # field's own const.py comment for the full real-bug-audit story
    # (this same block used to hardcode FIVE separate LocalVolts/AEMO-
    # QLD1 entity names, none of them reachable by any other install
    # regardless of whether they had a real equivalent).
```

## Line 9630: above `_block_start = grid_times[0].replace(`

```text
    # The current settlement block must never be a forecast/blend value
    # (2026-08-27, nimbus repo issue #220, Mark Purcell): Amber,
    # LocalVolts, and AEMO all publish the SETTLED price for the block
    # containing right-now -- a contractual fact, not an estimate, and
    # it cannot legitimately be diluted by any secondary source. Both
    # resample_price_with_extrapolation() and
    # resample_generic_price_forecast_with_coverage() above answer
    # period 0 via a "nearest-at-or-before" lookup against the source's
    # own FORECAST array, which is a genuinely different read than the
    # same source's own live `state` -- confirmed live on Mark's install
    # (2026-08-27 11:07 AEST): forecast-array lookup produced 4.07
    # c/kWh for the current block while the source sensor's own settled
    # `state` was 4.89 c/kWh. grid_times[0] is always "now" by
    # construction (see build_tiered_grid()), so period 0 is always the
    # current settlement block -- override it here with a direct state
    # read, same pattern safe_num() already uses for every other live
    # scalar read in this file. Falls back to whatever the resampler
    # already produced (never crashes, never worse than before this
    # fix) if the state itself is unavailable/unparseable.
    #
    # PARTIAL-REGRESSION FIX (2026-08-27, issue #220 reopened, Mark
    # Purcell): the first version of this fix only touched index 0.
    # build_tiered_grid()'s own tier-0 stretch runs 1-minute periods from
    # "now" up to the next clean 5-minute mark (0-4 extra periods,
    # depending on where "now" falls) -- EVERY one of those tier-0 rows
    # is still inside the SAME real NEM 5-minute settlement block as
    # period 0, not a future one, so they need the identical settled-
    # state override, not the forecast-array value. Confirmed live on
    # Mark's install (2026-08-27 17:33 AEST): rows at :32 (period 0) held
    # the correct identity, but :33/:34 (still inside the [17:30,17:35)
    # block) had already fallen back to the blended forecast value.
    # Computed directly from grid_times rather than threading a new
    # "how many tier-0 rows" value out of build_tiered_grid() -- tier-0
    # rows are 1-minute apart and never cross a 5-minute mark by
    # construction, so counting how many leading grid_times share
    # period 0's own 5-minute bucket is exactly equivalent and needs no
    # change to that function's own signature.
```

## Line 9672: above `settled_import_sensor = cfg["solver_import_price_sensor"]`

```text
    # 2026-09-02 audit: this used to branch on has_localvolts (now
    # has_price_forecast_array) between a hardcoded LocalVolts literal
    # and the generic field -- collapsed to just the generic field
    # unconditionally, since CONF_SOLVER_IMPORT_PRICE_SENSOR/EXPORT_
    # PRICE_SENSOR are ALREADY the exact same real entities the
    # hardcoded literals pointed at on this household's own live
    # install (confirmed live before this change), and are the correct,
    # portable source for every other install too -- a genuine
    # simplification, not just a hardcode removal.
```

## Line 9700: above `spot_import_source = list(spot_import_raw)`

```text
    # True pre-blend source pass-through (2026-08-27, nimbus repo issue
    # #216, Mark Purcell's refined asks #1/#2: publish an
    # export_price_raw alongside import_price_raw, and make `_raw`
    # actually mean "what the configured source sensor itself said,
    # before any transform" rather than the post-blend value). Captured
    # here, BEFORE blend_price_with_secondary_sources() below can
    # overwrite spot_import_raw/spot_export with a blended value -- on a
    # single-source install (no secondary configured) these are
    # byte-identical to the post-blend values, so nothing changes for
    # the overwhelming majority of installs.
```

## Line 9713: above `spot_import_raw, import_price_cross_spread, import_price_source = (`

```text
    # Optional second/third price sources to BLEND (2026-08-25, direct
    # household ask: "u also are missing my blended price forecasts...
    # in case we can feed it more than one... e.g. aemo... and amber").
    # Applies uniformly to whichever spot_import_raw/spot_export either
    # branch above produced -- genuinely optional, and every secondary
    # field defaults to unset, so a single-source install (the
    # overwhelming majority today) sees these two variables completely
    # unchanged. See blend_price_with_secondary_sources()'s own
    # docstring for the full mechanism -- extracted into its own
    # function specifically so it's directly unit-testable without
    # needing to drive the whole of main().
```

## Line 9809: above `flat_fee_rate = _cfg_num(cfg, "solver_flat_fee_rate", 0.0)`

```text
    # Generic + real: TOU network fees and the flat fee rate apply to
    # EVERY install, LocalVolts or not (nimbus repo issue #152, fixed
    # 2026-08-24) -- these are genuinely portable, no-NEM-specific-data-
    # required config-flow fields (number.nimbus_solver_network_fee_*,
    # number.nimbus_solver_flat_fee_rate), unlike the LocalVolts branch's
    # own AEMO extrapolation / live P2P-window detection which genuinely
    # do need Australian-NEM-specific data and stay LocalVolts-only.
    # Uses whichever spot_import_raw either branch above produced.
    # A fresh/generic install with every fee field left at its 0.0
    # default still contributes exactly 0 -- same honest no-op as
    # before, just no longer silently dropped just because LocalVolts
    # isn't configured.
```

## Line 9829: above `capacity_kwh = resolve_effective_capacity_kwh(cfg)`

```text
    # 2026-08-20: reads Nimbus's own real Solver settings config-flow
    # (see fetch_solver_config()'s own docstring for the full "close this
    # gap... need its own installer and inputs period" context) --
    # REPLACES the old ad-hoc input_number.nimbus_solver_* helpers, which
    # had to be hand-created via a separate, undocumented YAML package
    # file. A fresh install now needs nothing more than filling in
    # Nimbus's own hub "Configure" -> "Solver settings" form.
    # nimbus issue #1013: SoH-derated. This is the LIVE dispatch path --
    # the one where a phantom ceiling actually costs something, since
    # #1012 established this pack reaches both rails every single day.
    # See resolve_effective_capacity_kwh()'s docstring.
```

## Line 9855: above `if has_price_forecast_array:`

```text
    # nimbus issue #348 (Mark Purcell, codebase review, fixed 2026-09-04):
    # this used to call battery_discharge_cost_rate()/battery_salvage_
    # value_rate(), hardcoded Python-constant functions with zero way to
    # retune the schedule short of editing source -- see
    # scheduled_discharge_cost_rate()'s/scheduled_salvage_value_rate()'s
    # own docstrings (near DISCHARGE_COST_SCHEDULE_BLOCK_KEYS, above) for
    # the full story. Byte-identical output for this household (and any
    # other has_price_forecast_array install that has never touched the
    # new fields) -- every new config key's own schema default reproduces
    # the exact historical 5pm/midnight/7am schedule. Still deliberately
    # gated on has_price_forecast_array, same as before this fix: a
    # generic install without that sensor configured is unaffected,
    # still reading the flat solver_discharge_cost/solver_salvage_value
    # fields in the `else` branch below -- silently applying ANY day/
    # night schedule (even a wizard-configurable one) to an install that
    # never asked for one would be a real, unrequested behaviour change.
```

## Line 9877: above `discharge_cost_arr = np.full(`

```text
        # FALLBACK (2026-08-20, for anyone else): flat values straight
        # from the config-flow's own Economic Policy step -- no day/night
        # schedule (that's tuned specifically around this household's own
        # P2P window, no portable equivalent yet).
```

## Line 9910: above `export_price = list(spot_export)`

```text
    # No clamp needed as of 2026-08-16 -- the solver's grid degeneracy
    # guard that used to require import_price - export_price >= 0.001 at
    # every period (and which this writer used to satisfy by clamping the
    # real P2P price down, suppressing the real signal from ever reaching
    # the LP) has been REMOVED and replaced with two real structural
    # constraints in network.py itself (see its own "SAME-PERIOD
    # WASH-TRADE PREVENTION" docstring section) that make the underlying
    # free-money exploit physically infeasible regardless of price. The
    # real, unclamped spot rate now goes straight to the LP as the base
    # export_price; the real P2P premium goes through export_bonus_price
    # instead (see above) -- neither is diluted or clamped.
```
