"""Constants for the Nimbus integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "nimbus_load"

# Config subentry type key -- one Nimbus hub, many "load" subentries added via
# a "+" on the hub's device page (no repeated full "Add Integration" flow per
# load, no restart to add the 2nd through Nth load). Same mechanism HAEO uses
# for its own Battery/Load/Solar elements (confirmed against haeo_repo's own
# flows/hub.py -- async_get_supported_subentry_types).
SUBENTRY_TYPE_LOAD: Final = "load"

# Second subentry type (2026-08-15): forecasting a real power SIGNAL
# directly -- Battery, Solar, Grid, or any other real measured power
# sensor -- as its own genuine forecast target, using the exact same
# k-NN/GBRT/validation engine already proven for loads (coordinator.py
# and ml/model.py need zero changes to support this: they already read
# subentry.data[CONF_LOAD_SENSOR] generically, regardless of which
# subentry type created it). Deliberately a SEPARATE, simpler subentry
# type rather than folding into "load" -- a battery/solar/grid signal
# has no schedule/expected-load "deterministic mode" concept (it doesn't
# run on a fixed daily timer the way a pool pump does), so its own form
# (flows/signal_subentry.py) is intentionally just one field. Part of
# this repo's own stated roadmap (see CLAUDE.md): Battery/Solar/Grid
# becoming real Nimbus-forecasted targets, not just load-model inputs
# (see CONF_BATTERY_SENSOR etc. below for the input-feature side of
# this, a separate, earlier addition).
SUBENTRY_TYPE_SIGNAL: Final = "power_signal"

# Three more subentry types (2026-08-23) -- pure wiring/topology metadata
# for the topology dashboard card, genuinely NOT a forecasting target
# (no ML model, no coordinator, no _forecast sensor). Direct household
# ask, following the same "no hardcoded inputs" principle already
# applied to the Solver's own load-forecast-entities field: the
# topology card's physical wiring (which PV strings/battery towers
# belong to which power source, which switchboard sensors to use) used
# to live in a hand-edited YAML file specific to one household's own
# real Sungrow hardware -- genuinely unusable by anyone with different
# hardware.
#
# Real, independent validation this needed a flatter model than a
# naive "every PV string and every battery pack belongs to some
# inverter" hierarchy: Mark Purcell's own real system (SigenStor
# EC 30.0 battery/inverter unit + a wholly separate 3rd-party SolarEdge
# PV system, confirmed via his own live topology card build) has PV
# that is NOT wired through the same inverter as the battery at all --
# it's an independent power source feeding the switchboard on its own.
# "Power Source" (not "Inverter") is deliberately the more general term
# -- a real unit can be battery-only, PV-only, or hybrid; PV String and
# Battery Tower subentries both reference their OWN power source
# OPTIONALLY, not as a required parent -- left blank, a PV string or
# battery renders as its own independent branch on the diagram (Mark's
# SolarEdge case) rather than being forced under a specific inverter
# that doesn't actually own it (this household's Sungrow case still
# works exactly the same, by setting the link).
SUBENTRY_TYPE_POWER_SOURCE: Final = "power_source"
SUBENTRY_TYPE_PV_STRING: Final = "pv_string"
SUBENTRY_TYPE_BATTERY_TOWER: Final = "battery_tower"

# Power Source fields -- one real hardware unit that connects to the
# switchboard (an inverter, a hybrid battery/inverter unit, a battery-
# only BMS, etc). Both sensors optional: a PV-only unit has no real
# battery_power_sensor to give, and dc_power_sensor is a genuine bonus
# (total PV throughput regardless of whether the battery is doing
# anything -- see topology-card-v4.js's own real, direct household
# catch: "the card's own inverter header was showing ONLY
# battery_power, making a fully-charged, solar-producing inverter look
# falsely idle").
CONF_POWER_SOURCE_NAME: Final = "power_source_name"
CONF_POWER_SOURCE_BATTERY_SENSOR: Final = "power_source_battery_sensor"
CONF_POWER_SOURCE_DC_SENSOR: Final = "power_source_dc_sensor"

# PV String fields -- one real physical string/array. entity is the
# only required field (its own live power reading); power_source is
# OPTIONAL (see the module-level comment above SUBENTRY_TYPE_POWER_
# SOURCE for why) -- blank means "an independent PV source, not wired
# through any of my configured Power Sources," not an error. label is
# a free-text hint for the diagram (e.g. "MPPT2", "String A", "West
# array") -- deliberately not a structured MPPT-number field, since
# that's a Sungrow-specific concept, not universal.
CONF_PV_STRING_ENTITY: Final = "pv_string_entity"
CONF_PV_STRING_LABEL: Final = "pv_string_label"
CONF_PV_STRING_POWER_SOURCE: Final = "pv_string_power_source"

# Battery Tower fields -- one real physical battery pack/tower. Only
# the 4 fields topology-card-v4.js's own _batteryBox() actually renders
# (confirmed by reading that function directly, 2026-08-23, rather than
# assuming every field this household's own old hardcoded prefix
# convention implied -- current/status/lifetime-charge/lifetime-
# discharge are real Sungrow register readings but were never actually
# displayed on the diagram). soc_sensor is the one field genuinely
# worth treating as the "most important, fill this in first" one, but
# even it stays Optional -- a household mid-way through filling in the
# wizard shouldn't hit a hard validation error. power_source is
# OPTIONAL for the same reason as PV String above.
CONF_BATTERY_TOWER_SOC_SENSOR: Final = "battery_tower_soc_sensor"
CONF_BATTERY_TOWER_SOH_SENSOR: Final = "battery_tower_soh_sensor"
CONF_BATTERY_TOWER_VOLTAGE_SENSOR: Final = "battery_tower_voltage_sensor"
CONF_BATTERY_TOWER_TEMPERATURE_SENSOR: Final = "battery_tower_temperature_sensor"
CONF_BATTERY_TOWER_POWER_SOURCE: Final = "battery_tower_power_source"

CONF_LOAD_SENSOR: Final = "load_sensor"
# Power Signal role (2026-08-23) -- explicit, not inferred from naming.
# Real live check against this household's own Power Signals found
# "Logger Battery power" (contains "battery", would've matched a naive
# keyword guess) alongside "Combined Total DC Power" (the real Solar
# signal -- no "solar" anywhere) and "Logger Meter total active power"
# (the real Grid signal -- no "grid" anywhere) -- naming alone genuinely
# cannot distinguish these, for this household or anyone else's. A
# household picks the role ONCE when creating the Power Signal; the
# topology card then auto-wires Grid/Battery power straight from
# whichever Power Signal carries that role, same zero-config-file
# mechanism loads already use, no Switchboard wizard field needed for
# either. Optional, defaults to "other" -- every Power Signal that
# isn't specifically Battery/Solar/Grid (most of them) needs no change.
CONF_SIGNAL_ROLE: Final = "signal_role"
SIGNAL_ROLE_OTHER: Final = "other"
SIGNAL_ROLE_BATTERY: Final = "battery"
SIGNAL_ROLE_SOLAR: Final = "solar"
SIGNAL_ROLE_GRID: Final = "grid"
CONF_TEMPERATURE_SENSOR: Final = "temperature_sensor"
CONF_TEMPERATURE_FORECAST_SENSOR: Final = "temperature_forecast_sensor"
CONF_HUMIDITY_SENSOR: Final = "humidity_sensor"
# Shared, hub-level -- HAEO's own solar-curtailment status entity
# (switch.solar_curtailment on the real system this was built against),
# real and confirmed 2026-08-14: a boolean on/off with its own forward
# `forecast` attribute (HAEO already plans curtailment ahead of time),
# which is genuinely more useful at predict-time than any of this
# integration's other forecast inputs -- it's HAEO's own actual plan, not
# a held-flat approximation. Added specifically for curtailment-driven
# loads (e.g. a pool heater run only to soak up otherwise-curtailed
# solar, never for its own schedule or the weather).
CONF_CURTAILMENT_SENSOR: Final = "curtailment_sensor"

# Shared, hub-level -- REAL MEASURED power sensors only (this household's
# own Modbus meter/inverter readings), added 2026-08-15 so a load's model
# can see what else is happening on the switchboard at the same moment
# (a load can look identical at "10am, 22C" whether the battery happens
# to be mid-charge or not -- without this the model has no way to
# separate that confound from genuine load-driven signal). Deliberately
# NEVER an HAEO plan/forecast entity -- see this repo's own CLAUDE.md
# PRIME DIRECTIVE. All three optional and independent of each other; a
# household with only some of these wired up (or none) degrades
# gracefully, same as every other optional sensor on this form.
CONF_BATTERY_SENSOR: Final = "battery_sensor"
CONF_GRID_SENSOR: Final = "grid_sensor"
CONF_SOLAR_SENSOR: Final = "solar_sensor"

CONF_FORECAST_HORIZON_HOURS: Final = "forecast_horizon_hours"
CONF_RETRAIN_HOUR_LOCAL: Final = "retrain_hour_local"
CONF_TRAIN_DAYS: Final = "train_days"
# Where the retrain job pulls historical training data from.
#
# recorder -- the original, and still the safe default: reads the raw states table via
#   get_significant_states(). Full sample resolution (every state change), but bounded
#   by the recorder's own purge window -- an install with the HA default
#   purge_keep_days=10 (or lower) genuinely cannot produce more than that much training
#   history from this path, no matter what CONF_TRAIN_DAYS asks for. Confirmed live on
#   Mark Purcell's own install 2026-08-28: a CONF_TRAIN_DAYS=30 request returned just
#   5.9 days / 568 points because everything older had already been purged.
#
# lts -- long-term statistics only: reads the hour-bucketed statistics table via
#   recorder.statistics.statistics_during_period(). Kept indefinitely by the recorder
#   daemon (independently of purge_keep_days), so 30/90/365-day retrains are actually
#   possible on an install where the recorder itself only retains a few days. Cost:
#   hourly means, not full-resolution states -- fine for slowly-varying loads, blurs
#   sharp intra-hour transitions (a pool pump switching on/off).
#
# hybrid -- BOTH: recent CONF_HYBRID_RECENT_DAYS at full recorder resolution PLUS the
#   remainder of CONF_TRAIN_DAYS at hourly LTS, deduplicated at the boundary in favour
#   of the recorder points. Captures both sharp recent behaviour AND long-run
#   weekly/seasonal patterns. Best-quality option on a well-configured install,
#   graceful-degrades to just-recorder on an install where LTS is empty and to
#   just-LTS on an install where recorder has already purged everything.
CONF_TRAINING_SOURCE: Final = "training_source"
TRAINING_SOURCE_RECORDER: Final = "recorder"
TRAINING_SOURCE_LTS: Final = "lts"
TRAINING_SOURCE_HYBRID: Final = "hybrid"
TRAINING_SOURCE_CHOICES: Final = (
    TRAINING_SOURCE_RECORDER,
    TRAINING_SOURCE_LTS,
    TRAINING_SOURCE_HYBRID,
)
# In hybrid mode, how many trailing days come from recorder. The remainder
# (CONF_TRAIN_DAYS - CONF_HYBRID_RECENT_DAYS) comes from LTS. 5 is comfortably below
# HA's default 10-day purge_keep_days so almost every install produces both halves.
CONF_HYBRID_RECENT_DAYS: Final = "hybrid_recent_days"

# Nimbus issue #256 (Mark Purcell, 2026-08-27) -- Amber's own live price
# sensors are actively revised across the ~2 hours approaching each
# settlement block. Direct measured evidence on the 07:00 AEST 28-Aug
# block via HA's history API, sensor.amber_express_amber_feed_in_price:
# 15 discrete state updates across a single 30-min block window, revising
# by up to ~1c between adjacent ticks (0.0882 at solve time -> 0.0562 at
# block start -> 0.0508 mid-block -> 0.0145 end -- ~7c total amplitude on
# just this one block). Textbook "forecast block gets revised as
# settlement approaches" -- exact same behaviour LocalVolts P2P forecasts
# already documented for a different retailer.
#
# The periodic phase-locked cron (issue #244, __init__.py's own
# _SOLVER_CRON_MINUTES/_SECOND) catches the FIRST settled price of each
# 5-min block, ~30s past the boundary. It does NOT catch subsequent
# revisions to that block landing at arbitrary seconds in between. Amber's
# own coordinator polls on a 5-min cadence, so a revision can land 30s or
# 4:30 into a block -- the periodic cron then always waits up to another
# full 5 minutes before picking it up.
#
# nimbus_load.solve_now (issue #232/#254) shipped so a user's automation
# could react to state_changed on their own price sensor -- purely
# additive. This option pulls that same trigger *into* the integration
# so a fresh install gets it without a per-instance automation, and so
# the trigger is authoritatively tied to whatever price sensors the
# wizard already knows about (rather than duplicated in a per-user YAML
# block that can drift out of sync when the wizard reconfigures).
#
# Default OFF -- byte-identical behaviour on every existing install
# until deliberately enabled. When on, listens to state_changed on
# every configured solver_*_price_sensor{,_2,_3} and calls
# solver_runtime.async_run_solve() -- the same call the periodic cron
# already makes, so its own PID-lock overlap guard applies unchanged.
# A debounce coalesces bursts of state changes within the debounce
# window into one solve, so a retailer that updates several correlated
# price sensors in the same second doesn't fan out into N solves.
CONF_SOLVE_ON_PRICE_CHANGE: Final = "solve_on_price_change"
CONF_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S: Final = "solve_on_price_change_debounce_s"

# Per-load, NOT shared -- unlike temperature/humidity/curtailment (whole-
# house signals every load's model can reasonably use), a fixed daily
# schedule window (e.g. a pool pump timer running 8am-3pm) is specific to
# one particular load. Both optional; a load with neither configured gets
# a permanently-zero in_schedule feature (a no-op, not an error) since
# most loads genuinely have no fixed timer.
CONF_SCHEDULE_START_HOUR: Final = "schedule_start_hour"
CONF_SCHEDULE_END_HOUR: Final = "schedule_end_hour"

# Also per-load, also optional, and deliberately a THIRD, separate choice
# from the schedule window alone -- three real modes, not two:
#   1. No schedule set at all -- pure ML, in_schedule always 0 (unchanged
#      default behaviour for any load with no fixed timer).
#   2. Schedule set, expected load NOT set -- in_schedule is one ML input
#      feature among many; the model still statistically learns what
#      power looks like during the window from real history. This is a
#      genuine estimate, and can come out noisy/blurred if the load's
#      real on/off timing varies day to day (confirmed live 2026-08-15:
#      HWS L1's real finish time swings by hours some days -- manual
#      top-ups included -- so its ML-blended forecast during the window
#      came out smoothed to ~2kW instead of its real ~3.7kW capacity).
#   3. BOTH schedule and expected load set -- a hard, deterministic
#      override: predict exactly this value for the whole window, 0
#      outside it, bypassing the ML model for this load entirely. Not a
#      guess refined by a hint -- literally what the user told the
#      system to expect, full stop. User's own framing, 2026-08-15:
#      "that converts a guess from a guess to what we tell it to do."
CONF_EXPECTED_LOAD_KW: Final = "expected_load_kw"

DEFAULT_FORECAST_HORIZON_HOURS: Final = 48
DEFAULT_RETRAIN_HOUR_LOCAL: Final = 3
DEFAULT_TRAIN_DAYS: Final = 30
# Default stays recorder to preserve exact current behaviour on every existing install
# -- opt-in to lts/hybrid via the Forecaster options form. Confirmed safe by construction:
# a fresh install with no options set at all will resolve this default and take the
# original recorder-only path unchanged.
DEFAULT_TRAINING_SOURCE: Final = "recorder"
DEFAULT_HYBRID_RECENT_DAYS: Final = 5
# Default OFF preserves exact current behaviour on every existing install
# -- see CONF_SOLVE_ON_PRICE_CHANGE's own comment above. A 5s debounce is
# chosen deliberately: Amber's coordinator updates several correlated
# sensors within tens of milliseconds of each other; 5s comfortably
# coalesces that whole burst into one solve but is much shorter than the
# ~5-min cadence between real Amber pulls, so a genuine second update
# arriving 10s later still triggers its own solve.
DEFAULT_SOLVE_ON_PRICE_CHANGE: Final = False
DEFAULT_SOLVE_ON_PRICE_CHANGE_DEBOUNCE_S: Final = 5.0
DEFAULT_FALLBACK_TEMPERATURE_C: Final = 22.0
# No humidity-forecast integration exists to source a horizon-length
# humidity forecast from (unlike temperature, which has one) -- the
# coordinator instead holds humidity at its most-recently-observed value
# across the whole forecast horizon, and falls back to this constant when
# no humidity sensor is configured at all or has never reported.
DEFAULT_FALLBACK_HUMIDITY_PCT: Final = 50.0

# Forecast/prediction grid resolution.
RESAMPLE_MINUTES: Final = 15

# Coordinator polling cadence -- how often the published forecast is
# regenerated from whichever model is currently loaded (cheap: inference
# only, no retraining -- a k-NN lookup or a 60-tree GBRT forward pass,
# both well under a second). Deliberately shorter than RESAMPLE_MINUTES
# (the model's own forecast-grid resolution): the FIRST point of every
# forecast (published as this entity's own live state) is always a
# fresh prediction for "right now", using whatever real lag/temp/
# humidity/curtailment inputs exist at that exact moment -- tightening
# this makes the live value recompute more often, giving a finer,
# smoother-looking history graph made of real fresh predictions.
# Confirmed live 2026-08-15: at the original 15-minute interval, the
# history graph rendered as an obvious blocky staircase (one flat
# segment, one jump, repeat) since there was nothing between updates
# to plot. This is NOT the same thing as interpolating/faking values
# between real predictions -- every point is still a genuine, freshly
# computed forecast, just computed more often.
UPDATE_INTERVAL_MINUTES: Final = 2

# Sanity floor -- refuse to train on too little real history.
MIN_TRAINING_POINTS: Final = 500

# Lag features, in grid steps (RESAMPLE_MINUTES apart) -- e.g. with
# RESAMPLE_MINUTES=15, LAG_SHORT_STEPS=1 is "15 minutes ago",
# LAG_LONG_STEPS=4 is "60 minutes ago". Confirmed via real backtesting
# (2026-08-14, 30 days of this household's own actual history) that these
# two are consistently among the most important features GBRT uses across
# every load tested, well ahead of calendar features alone.
LAG_SHORT_STEPS: Final = 1
LAG_LONG_STEPS: Final = 4

# Fraction of available training data held out, chronologically (never
# randomly -- a random split leaks future information into training for a
# time series), to let the model pick GBRT vs k-NN for itself each retrain
# rather than assuming one is always better. Confirmed live: with too
# little data GBRT can tie or lose to k-NN; with a full 30-day window GBRT
# won clearly on every load tested. Rather than hardcode a day-count
# threshold from one household's numbers, let each load's own retrain
# decide from its own real, current data.
VALIDATION_HOLDOUT_FRACTION: Final = 0.2

# Entity attribute keys. ATTR_FORECAST: a "forecast" attribute holding a
# list of {"time": ..., "value": ...} dicts, plus a top-level
# unit_of_measurement / device_class -- a generic, self-describing shape,
# not modeled on or tied to any specific downstream consumer.
ATTR_FORECAST: Final = "forecast"
ATTR_MODEL_TRAINED_AT: Final = "model_trained_at"
ATTR_TRAINING_POINTS: Final = "training_points"
# One of "unscheduled" / "scheduled_ml" / "deterministic" -- see the
# CONF_EXPECTED_LOAD_KW comment above for what each mode means. Exposed
# on every load's own forecast sensor (not just deterministic ones) so
# any dashboard, script, or future tooling can read live which mode a
# load is actually in, instead of needing to be told by hand which
# entities are which -- config drives this attribute, nothing downstream
# should ever hardcode a list of entity names to get the same answer.
ATTR_MODE: Final = "mode"
# Model validation diagnostics (2026-08-15) -- raw MAE and its scale-
# independent MASE counterpart, both dicts keyed by candidate name
# ("knn"/"gbrt"/"naive"), exposed on every ML-path load's own forecast
# sensor. Empty dicts (not omitted) when there wasn't enough validation
# data to compute them -- e.g. a load that only just started training --
# so any dashboard/automation reading these can distinguish "genuinely
# nothing to show yet" from a missing attribute key.
ATTR_VALIDATION_MAE: Final = "validation_mae"
ATTR_VALIDATION_MASE: Final = "validation_mase"
# Nimbus issue #113 (Mark Purcell, 2026-08-25): makes ATTR_VALIDATION_
# MASE's own empty-dict-when-insufficient-data behaviour diagnosable
# instead of a bare {} -- how many real week-over-week points were
# actually found (out of the fixed threshold needed), plus the actual
# resample resolution and real elapsed calendar span the deployed model
# trained on (distinct from the configured train_days request, which a
# fresh-ish install's own real history may not yet be able to satisfy).
# See ml/model.py's own TrainedModel docstring for the full reasoning.
ATTR_MASE_SCALE_POINTS: Final = "mase_scale_points"
ATTR_RESAMPLE_MINUTES: Final = "resample_minutes"
ATTR_TRAINING_SPAN_DAYS: Final = "training_span_days"
# "load" or "power_signal" -- lets anything downstream (e.g. a dashboard
# chart script) tell the two subentry types apart generically, by
# reading this live attribute, rather than by hardcoding which specific
# entity names belong to which category.
ATTR_SUBENTRY_TYPE: Final = "subentry_type"
ATTR_SIGNAL_ROLE: Final = "signal_role"
# Real gap found live (2026-08-25, direct household report: "the
# topology card... still not working... make it respond to wizard
# selected nimbus only entities"): ATTR_SIGNAL_ROLE's own docstring
# above already states the intent -- "so the topology dashboard card
# can auto-discover which power signal is Grid/Battery/Solar directly
# from hass.states, zero config file needed" -- but a card CAN'T act on
# that discovery without also knowing the REAL LIVE entity_id this
# forecast is built from (self._source_sensor already existed
# internally, just was never actually exposed). Without this, any
# topology dashboard has no choice but to hardcode a guess at which
# real sensor corresponds to which role, exactly the class of hardcoding
# this project's own standing rule exists to prevent.
ATTR_SOURCE_SENSOR: Final = "source_sensor"

# --- Solver configuration (2026-08-20) ---
# Everything the Solver (custom_components/nimbus_load/solver/) needs to
# build a real dispatch plan for ANY household, not just this one.
# Previously these lived as bare input_number/entity helpers this
# household hand-created (see research scripts under the sibling
# 116KAT-HA-AI repo's own scripts/research/ for the exact entity IDs that
# predate this), with no structured onboarding at all -- a real installer
# would have had to know the precise entity names to create by hand.
# Deliberately mirrors HAEO's own real schema (haeo_repo/core/schema/
# elements/battery.py, grid.py, solar.py, inverter.py) as a proven
# reference for what a complete config needs to cover, while keeping
# Nimbus's own simpler, already-validated design: ONE aggregate battery
# envelope (capacity/power/efficiency), not HAEO's separate
# battery+inverter graph elements -- the owner's real inverter/EMS
# firmware already handles internal routing; Nimbus only needs the
# system-level numbers a real bill/nameplate/app screen would show. See
# Solver audit item #8 (topology) in the sibling repo's own CLAUDE.md for
# the full reasoning already validated on this exact point.
CONF_SOLVER_BATTERY_CAPACITY_KWH: Final = "solver_battery_capacity_kwh"
# Optional de-rating for real, aged battery capacity -- a real, honest
# gap this integration has had no answer for at all until now: capacity
# was previously a single static number, with no way to reflect real
# degradation over the battery's life. Deliberately simple for a first
# pass: one number the owner updates occasionally (from their own
# inverter app/BMS reading), NOT an automated fade-tracking model --
# effective_capacity = capacity_kwh * soh_percent / 100.
CONF_SOLVER_BATTERY_SOH_PERCENT: Final = "solver_battery_soh_percent"
# Entity reference, NOT a static number -- SoC changes continuously; this
# must track a real live % sensor, same convention as every other sensor
# field on this form.
CONF_SOLVER_BATTERY_SOC_SENSOR: Final = "solver_battery_soc_sensor"
CONF_SOLVER_BATTERY_MIN_SOC_PERCENT: Final = "solver_battery_min_soc_percent"
CONF_SOLVER_BATTERY_MAX_SOC_PERCENT: Final = "solver_battery_max_soc_percent"
# The AGGREGATE, grid-facing power envelope -- not per-inverter, not
# per-cell. See the module-level comment above for why this is
# deliberately one number each, not a per-device breakdown.
CONF_SOLVER_MAX_CHARGE_KW: Final = "solver_max_charge_kw"
CONF_SOLVER_MAX_DISCHARGE_KW: Final = "solver_max_discharge_kw"
# Genuinely optional, no DEFAULT_ constant -- unset/None means "just use
# solver_max_discharge_kw above, nothing more" (2026-08-24, nimbus #125,
# Mark Purcell's own real repro: this repo's reference household has a
# real, live Sungrow Logger charge/discharge setpoint entity whose own
# `max` attribute reflects the actual hardware ceiling in real time, more
# trustworthy than a static config value that can go stale -- but that
# entity_id used to be HARDCODED directly into solver_writer.py
# ("number.logger_charging_discharging_power_kw"), so on any OTHER
# install where an entity happens to exist at that exact name (a real,
# reproduced failure on Mark's own Sigen-based system -- 1.93kW, an
# unrelated entity's own max attribute, silently substituted in place of
# his real configured 24kW), the LP would clamp discharge to some
# completely unrelated number with zero warning. Making the entity_id
# itself a per-household config field, defaulting to unset, closes this
# for everyone except the reference household, which needs to set it
# once after this ships to keep the exact same protective behaviour it
# already had.
CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY: Final = "solver_max_discharge_live_entity"
# ONE blended round-trip efficiency number (battery chemistry loss +
# inverter AC-DC conversion loss combined) -- HAEO models these as two
# separate numbers on two separate elements; Nimbus deliberately doesn't,
# per the same already-validated simplification.
CONF_SOLVER_EFFICIENCY_PERCENT: Final = "solver_efficiency_percent"
CONF_SOLVER_GRID_MAX_IMPORT_KW: Final = "solver_grid_max_import_kw"
CONF_SOLVER_GRID_MAX_EXPORT_KW: Final = "solver_grid_max_export_kw"
CONF_SOLVER_IMPORT_PRICE_SENSOR: Final = "solver_import_price_sensor"
CONF_SOLVER_EXPORT_PRICE_SENSOR: Final = "solver_export_price_sensor"
# Optional SECOND/THIRD price sources (2026-08-25, direct household ask:
# "u also are missing my blended price forecasts... in case we can feed
# it more than one... e.g. aemo... and amber"). Same blend-don't-pick-one
# reasoning already proven for solar (see CONF_SOLVER_SOLAR_FORECAST_
# SENSOR_2/_3 below and ml/blend.py's own module docstring) -- a
# wholesale forecast (AEMO) and a retail forecast (Amber, LocalVolts,
# etc) are independently-modeled, so their disagreement is itself a
# real, earned uncertainty signal, not noise to average away. Optional
# and blank by default -- a single-source install (the overwhelming
# majority today) behaves byte-identically to before these fields
# existed; configuring a second/third source is what turns blending on.
CONF_SOLVER_IMPORT_PRICE_SENSOR_2: Final = "solver_import_price_sensor_2"
CONF_SOLVER_IMPORT_PRICE_SENSOR_3: Final = "solver_import_price_sensor_3"
CONF_SOLVER_EXPORT_PRICE_SENSOR_2: Final = "solver_export_price_sensor_2"
CONF_SOLVER_EXPORT_PRICE_SENSOR_3: Final = "solver_export_price_sensor_3"
# Entity references only -- panel size/tilt/azimuth/inverter model
# numbers are deliberately NOT asked for here. That's a separate solar-
# forecast integration's job (Solcast, Forecast.Solar, etc, which already
# handle those specs in their own config); the Solver only needs to know
# WHICH entity carries the resulting forecast, same division of
# responsibility HAEO's own schema uses (core/schema/elements/solar.py --
# a bare forecast entity reference, nothing about the panels themselves).
CONF_SOLVER_SOLAR_FORECAST_SENSOR: Final = "solver_solar_forecast_sensor"
# Optional SECOND solar source (2026-08-22, direct household ask, after
# real live evidence: on one real morning, Open-Meteo's own forecast
# (2.3kW) was significantly FURTHER from real conditions (5.1-7.3kW)
# than the primary source's own model (6.2kW) was -- proving neither
# single source is reliably right on its own, the exact scenario a real
# blend is meant to help with. Optional and blank by default -- a
# fresh/single-source install behaves byte-identically to before this
# field existed; only configuring it turns blending on at all. See
# ml/blend.py's own module docstring for the "every forecast is wrong,
# so blend rather than pick one" reasoning this implements.
CONF_SOLVER_SOLAR_FORECAST_SENSOR_2: Final = "solver_solar_forecast_sensor_2"
# Optional THIRD solar source (2026-08-22, direct household finding: a
# real live check showed the primary AND second source both
# overpredicting real measured solar by 36-57% at the SAME moment --
# proof two sources sharing the same directional bias don't cancel out
# when averaged, they just average the bias. A third, genuinely
# differently-modeled source (this household's own real install:
# Solcast, satellite-imagery-anchored, architecturally distinct from
# both a self-trained ML model and a different NWP provider) gives the
# blend a real chance at partially-uncorrelated error for the first
# time. Blank (the default) is a complete no-op, same guarantee as
# _2 above.
CONF_SOLVER_SOLAR_FORECAST_SENSOR_3: Final = "solver_solar_forecast_sensor_3"
# Real, live, dashboard-editable toggle (switch.py) -- deliberately NOT
# entangled with sources 1/2/3 above (2026-08-22, direct household
# design after a sharp catch: the writer script was silently including
# two hardcoded, known-integration solar sources -- Open-Meteo and
# Solcast -- OUTSIDE the 3 config fields entirely, with no way to see
# or turn it off. "then what is the purposed of having 3 inputs since
# it forces user ot autodetect... that feels wrong." Household's own
# spec, verbatim: "user should pick 1... or 1+2... or 1+2+3 to get
# their desired blend... or yes can tick auto detect from existing...
# for ease... otherwise it should be just a setting they chose."
#
# Default False, not True -- a genuinely fresh/different install (Mark
# Purcell, or anyone else) gets EXACTLY what they configured in sources
# 1/2/3, nothing more, matching every other "no hardcoded inputs" field
# built this session. This household's own live install needs the
# switch turned ON explicitly to keep its current blend behaviour --
# that is a one-time, visible, deliberate choice now, not silent magic.
#
# Worth remembering if this is ever revisited: the two anchors behind
# this toggle are NOT equally portable. Solcast's anchor entity_id
# (sensor.solcast_pv_forecast_forecast_today) is fixed by that
# integration's own code, genuinely the same for any install. Open-
# Meteo Solar Forecast's anchor (sensor.home_energy_production_today)
# is whatever THIS household happened to name their own config entry
# at setup time -- there is no guarantee a different household's install
# of the same integration uses that name at all. The toggle makes the
# behaviour visible and switchable either way, but does not by itself
# make the Open-Meteo half of it portable.
CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR: Final = "solver_auto_include_known_solar"
DEFAULT_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR: Final = False
# Real-dispatch groundwork, phase 1 (2026-08-27) -- see solver_runtime.py's
# own _log_dispatch_dry_run() docstring for the full story. Default False,
# same as the toggle above: Nimbus never assumes a household wants a new
# capability turned on just because an upgrade added the entity for it.
# This switch only ever produces a log line -- there is no write path in
# the codebase yet for it to enable even if someone flips it on.
CONF_SOLVER_DISPATCH_DRY_RUN: Final = "solver_dispatch_dry_run"
DEFAULT_SOLVER_DISPATCH_DRY_RUN: Final = False
CONF_SOLVER_LOAD_FORECAST_SENSOR: Final = "solver_load_forecast_sensor"
# Optional, more granular alternative to the single sensor above (2026-08-23,
# real bug found live: solver_writer.py used to hardcode a Python list of
# ONE household's own 18 circuit-breaker forecast entities, non-empty on
# every install, which made the config-flow-driven single-sensor fallback
# above permanently unreachable dead code for anyone else -- see nimbus
# repo issue #56, reported and precisely diagnosed by an independent
# installer). Multi-entity selector, genuinely empty by default -- when
# blank, the writer sums nothing here and falls through to
# CONF_SOLVER_LOAD_FORECAST_SENSOR above, exactly the portable, config-
# driven behaviour that field was always meant to provide. A household
# that wants per-circuit summation (this project's own reference install
# included) fills this in explicitly through the wizard; nobody else pays
# for that sophistication by default.
CONF_SOLVER_LOAD_FORECAST_ENTITIES: Final = "solver_load_forecast_entities"
# Same real bug class, same issue #56: a single hardcoded cross-check
# sensor name (this project's own sensor.cb_total_combined_power_adjusted_kw)
# silently produced a permanently-null whole_house_cross_check_now_kw
# attribute on every other install. Genuinely optional -- None/blank is a
# clean no-op (the cross-check simply doesn't run), not a degraded mode.
CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR: Final = (
    "solver_whole_house_cross_check_sensor"
)
# Built-in EPR/regret/tracking quality scoring (2026-08-25, direct ask:
# "it should be a part of the suite to monitor epr and trend and
# regret... nimbus should have it built in" -- previously only available
# via a real-world reference script, docs/real-world-integration/files/
# nimbus_solver_quality_writer.py, hardcoded to one household's own
# LocalVolts/Sungrow/Modbus stack). These two REAL MEASURED sensors are
# what compute_daily_quality_report() (solver_writer.py) needs to score
# yesterday's actual dispatch against a perfect-foresight oracle --
# genuinely different from every *_FORECAST_SENSOR field above (those
# feed the forward-looking plan; these feed a backward-looking score
# against what already, provably happened). Both optional: blank is a
# clean no-op (no quality score published, same "not yet configured"
# state the dashboard card already handles), not a degraded mode --
# matches CONF_SOLVER_WHOLE_HOUSE_CROSS_CHECK_SENSOR's own convention
# exactly, including reusing THAT field for the real load side (no new
# field needed there -- it's already a real measured sensor, not a
# forecast, whenever it's configured).
CONF_SOLVER_SOLAR_POWER_SENSOR: Final = "solver_solar_power_sensor"
CONF_SOLVER_BATTERY_POWER_SENSOR: Final = "solver_battery_power_sensor"
# Real, confirmed-live bug found by Mark Purcell (issue #299, 2026-08-31):
# compute_daily_quality_report() treats CONF_SOLVER_BATTERY_POWER_SENSOR's
# raw history as already following this project's own established sign
# convention (positive = discharge, negative = charge -- matches this
# household's own sensor.logger_battery_power) with NO way to say
# otherwise. A SigEnergy plant's own sensor.sigen_plant_battery_power uses
# the OPPOSITE convention (positive = charge). Confirmed live on Mark's
# install: while genuinely discharging 2kW, the sensor read -0.008kW.
# Every charge event was silently booked as a discharge and vice versa,
# producing EPR = -137.47% (structurally invalid -- EPR is bounded to
# [0%, ~100%+] since j_ach can never beat a perfect-foresight oracle
# scored under the correct sign).
#
# Fixed per Mark's own "Option A" (a config-flow flag, not silent
# auto-detection -- "silent-fixing conventions in prod is a code-smell",
# his words, correct). False (the default) reproduces the ORIGINAL,
# undocumented assumption byte-for-byte on every existing install --
# this project's own reference household's sensor already matches that
# convention, so nothing changes for it. True inverts the sign before any
# charge/discharge split happens, for households (like Mark's SigEnergy
# one) whose own hardware reports the opposite way.
CONF_SOLVER_BATTERY_POWER_POSITIVE_IS_CHARGE: Final = (
    "solver_battery_power_positive_is_charge"
)
# Optional retailer-specific settlement hook -- the one piece of the
# reference script that genuinely CAN'T be made retailer-agnostic by
# reading recorder history alone (a real bonus/P2P settlement amount is
# only ever known to the retailer's own API, days can't be re-solved to
# discover it). Retailer-agnostic in SHAPE, not tied to LocalVolts by
# name: any entity publishing `attributes.history: {"YYYY-MM-DD":
# {"export_cost": <$>, "export_volume": <kWh>}}` works -- LocalVolts'
# own sensor.lv_v2_p2p_confirmed_history already happens to publish
# exactly this shape (a real adapter for a different retailer would
# publish the same shape from its own settlement API). Blank (the
# default): compute_daily_quality_report() prices export at the plain
# configured export price only, no bonus term -- an honest, real EPR
# for any install with no bonus/P2P program at all, just a less precise
# one for a household that DOES have one but hasn't wired this field in.
CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR: Final = (
    "solver_p2p_settlement_history_sensor"
)
# Real hardcoded-foreign-entity audit (2026-09-02, Mark Purcell's real
# "online Claude" handoff report, direct household escalation: "STANDING
# DIRECTIVE WAS AND STILL IS: NO HARD WIRED NIMBUS PARTS... NO LOCAL
# ENTITIES NAMED IN CODE"). main()'s own has_localvolts branch hardcoded
# `sensor.localvolts_price_forecast` as BOTH the feature-detection check
# AND the read source for a real, richer per-5-minute price forecast
# array -- silently unreachable for every install without that exact
# LocalVolts entity, regardless of whether their own retailer/market
# integration publishes an equivalent forecast-array sensor under a
# different name. Retailer-agnostic in SHAPE, not tied to LocalVolts by
# name: any entity whose own `attributes.forecast` is a list of
# `{time, costsflexup, earningsflexup}`-shaped points works -- see
# resample_price_with_extrapolation()'s own docstring for the exact
# shape this reads. Blank (the default): falls straight through to the
# already-existing generic branch (CONF_SOLVER_IMPORT_PRICE_SENSOR/
# CONF_SOLVER_EXPORT_PRICE_SENSOR's own forecast-attribute-or-flat
# behaviour) -- a clean no-op, not a degraded mode, same convention as
# every other optional field in this file.
CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR: Final = (
    "solver_price_forecast_array_sensor"
)
# Same audit, same real bug class: fetch_aemo_forecast() hardcoded
# `sensor.nem_pd7day_qld1_nem_spot_price_forecast` -- Australian-
# NEM-region-specific by NATURE (a genuinely real, honest, disclosed
# limitation with no portable equivalent for a non-NEM market yet -- see
# fetch_aemo_forecast()'s own docstring), but hardcoded to one specific
# STATE'S region (QLD1) even within the NEM, which a household in NSW/
# VIC/SA/TAS running the same purcell-lab/nem_pd7day integration for
# their OWN region could never reach. Blank (the default): returns []
# (fetch_aemo_forecast()'s own already-existing "unavailable" fallback)
# -- the AEMO-anchored far-horizon extrapolation simply doesn't run,
# same graceful degradation every other optional Solver input already
# has, not a new failure mode.
CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR: Final = (
    "solver_regional_spot_forecast_sensor"
)
# Same audit, a different sensor from the forecast one above: a real,
# live/historical wholesale spot-price sensor (this household's own is
# `sensor.aemo_nem_qld1_current_5min_period_price`, a DIFFERENT AEMO
# integration from purcell-lab/nem_pd7day, from the older, separately-
# documented `aemo_nem`/HA_AemoNemData integration -- same "one specific
# region's entity name" hardcoding, same fix. Used to build a real
# (retail-markup vs wholesale) offset from RECORDED history, not a
# forward forecast -- genuinely a different consumer from
# CONF_SOLVER_REGIONAL_SPOT_FORECAST_SENSOR, not a duplicate field.
# Blank (the default): compute_5min_offset()'s own existing "either
# side's history unavailable" fallback (an empty dict, caller falls
# back further) -- same graceful no-op as every other optional field.
CONF_SOLVER_REGIONAL_SPOT_CURRENT_PRICE_SENSOR: Final = (
    "solver_regional_spot_current_price_sensor"
)
# Same audit, a third and last real gap: resample_real_p2p_rate()
# hardcoded `sensor.localvolts_p2p_forecast` -- a genuinely different
# CONCEPT from CONF_SOLVER_PRICE_FORECAST_ARRAY_SENSOR above (that one
# is a general retail price forecast; this one is real, live PER-
# INTERVAL P2P-MATCHING data -- matchedCost/volume/proportionP2P per
# point, the empirical rate a community-trading/peer-export program
# actually paid for each already-elapsed-or-upcoming interval, not a
# plain buy/sell price). Retailer-agnostic in shape: any entity whose
# own `attributes.forecast` is a list of `{time, matchedCost, volume,
# proportionP2P}`-shaped points works -- see resample_real_p2p_rate()'s
# own docstring for the exact formula this reads it with. Blank (the
# default): that function's own existing "missing/malformed source"
# fallback -- a flat 0.0 array, never a crash, same as every other
# optional field in this file. A household with no P2P/community-
# trading program at all (the vast majority of installs) leaves this
# blank forever, same as CONF_SOLVER_P2P_SETTLEMENT_HISTORY_SENSOR.
CONF_SOLVER_P2P_MATCHED_RATE_FORECAST_SENSOR: Final = (
    "solver_p2p_matched_rate_forecast_sensor"
)
# Real forward temperature/humidity mirror for the dashboard's own
# Forecaster chart (2026-08-25) -- points at a weather.* entity (Nimbus
# calls weather.get_forecasts for you) or a sensor.* whose own 'forecast'
# attribute already carries datetime/temperature[/humidity] entries,
# same two accepted shapes as CONF_TEMPERATURE_FORECAST_SENSOR. A
# SEPARATE field, not a reuse of that Forecaster-level one, deliberately:
# fetch_solver_config() (this file's only real consumer) reads config
# EXCLUSIVELY through sensor.nimbus_solver_config's own attributes, which
# only ever expose Solver-specific fields -- keeping this Solver-scoped
# avoids reaching across into the Forecaster's own settings surface. A
# real household is free to point both fields at the same entity. Blank
# (the default): no mirror published, a clean no-op -- never a hardcoded
# entity_id guess (an earlier version of this field's own driver
# function DID hardcode a weather.pirateweather/weather.home preference
# order, caught and corrected the same day: "make sure whatever u do
# nothing is hard wired it is all template wizard").
CONF_SOLVER_WEATHER_FORECAST_SENSOR: Final = "solver_weather_forecast_sensor"
# Real, PORTABILITY BUG found and fixed live (nimbus repo issue #100,
# Mark Purcell -- an independent installer's own health-check, findings
# 1 & 2): the multi-circuit summing path (sum_load_forecasts()) used to
# unconditionally add a bare, hardcoded module-level constant --
# INVERTER_SELF_CONSUMPTION_KW = 0.215 -- to every load total and every
# load confidence band, on EVERY install, regardless of whether that
# household's own hardware has any such bias at all, let alone one of
# exactly 215W. That constant's own original comment (still true, and
# still the right value -- for ONE specific household) explains where
# it came from: "the Sungrow logger's own internal accounting draws a
# constant real ~215W nothing on the Zigbee CB network can ever see."
# Real, specific, and completely inapplicable to anyone with different
# hardware -- exactly the class of thing this whole config-flow
# refactor (2026-08-20 onward) exists to eliminate.
#
# Concretely, this is what produced BOTH of Mark's own findings from a
# single mechanism: (1) his lower-band values sitting dead flat at
# 0.215 for 362/363 points -- his own source's real lower/upper keys
# resample to ~0 whenever unset, the household-specific constant was
# then unconditionally added on top, and the defensive upper-clamp
# (`max(total_upper_kw[i], total_kw[i])`) hid the same addition on the
# upper side once the real point value grew past it; (2) a genuinely
# wrong, if small (215W), bias baked into every OTHER install's own
# load total and band width, silently, with zero way to turn it off or
# even see it was being added at all before this field existed.
#
# 0.0 (the default) is a genuine no-op -- an install that's never set
# this returns exactly the sum of its own configured circuits, nothing
# added. This project's own reference household (116KAT-HA-AI) needs to
# set this to 0.215 ONCE, manually, on its own dashboard after this
# field first ships -- there is nothing in entry.options for a
# brand-new field to seed itself from, so even the household whose real
# hardware this constant was originally measured against starts at 0.0
# too, same as everyone else, until set.
CONF_SOLVER_INVERTER_SELF_CONSUMPTION_KW: Final = "solver_inverter_self_consumption_kw"
DEFAULT_SOLVER_INVERTER_SELF_CONSUMPTION_KW: Final = 0.0
# Economic POLICY, not hardware -- how cautious the solver should be
# about cycling the battery. Real, non-obvious history worth remembering
# if these are ever misconfigured: a household running this same solver
# design found that a zero-friction (both costs at $0, 100% efficiency)
# battery is mathematically a free wash-trade machine -- the LP can
# simultaneously charge AND discharge at zero net cost, producing rapid,
# meaningless oscillation. elements.py's own DegenerateConfigError guards
# against exactly this (strict 0<efficiency<1 required) -- these three
# fields are the other half of that same protection, and should never be
# left at zero/100% together.
CONF_SOLVER_CHARGE_COST: Final = "solver_charge_cost"
CONF_SOLVER_DISCHARGE_COST: Final = "solver_discharge_cost"
CONF_SOLVER_SALVAGE_VALUE: Final = "solver_salvage_value"
# Real economic cycle-wear cost (2026-08-22, Track B2). $/kWh, applied
# per kWh of THROUGHPUT in EITHER direction (charge OR discharge) --
# see BatteryConfig's own degradation_cost_per_kwh docstring (solver/
# elements.py) for the full "why separate from charge_cost/
# discharge_cost" reasoning and the real "(replacement cost) / (2 *
# capacity * rated EFC)" derivation. 0.0 (the default) is a genuine
# no-op -- byte-identical to every scenario built before this field
# existed.
CONF_SOLVER_DEGRADATION_COST_PER_KWH: Final = "solver_degradation_cost_per_kwh"
DEFAULT_SOLVER_DEGRADATION_COST_PER_KWH: Final = 0.0
# Optional, P2P-specific -- only relevant to an installer with a peer-to-
# peer energy trading platform (e.g. LocalVolts in Australia). Left blank
# (0) for anyone without one; the Solver simply never sees any bonus-
# priced export opportunity in that case.
CONF_SOLVER_P2P_BONUS_PRICE: Final = "solver_p2p_bonus_price"
CONF_SOLVER_P2P_BONUS_VOLUME_KWH: Final = "solver_p2p_bonus_volume_kwh"

# P2P fixed-rate delivery blocks (2026-08-21) -- distinct from the bonus
# price/volume pair above. The bonus pair is an ECONOMIC INCENTIVE (extra
# $/kWh up to a daily cap) that the LP is still free to decide WHEN to
# claim. These blocks are a HARD CONSTRAINT: a real P2P trading platform
# is a pre-committed delivery arrangement, not a price-taking market --
# chasing the momentary best price within the window breaks the actual
# deal (see GridConfig.fixed_export_kw's own docstring, solver/elements.py,
# for the full real-world finding this was built from). Up to 3 fixed,
# independent, optional blocks -- each is (rate_kw, start_hour, end_hour);
# rate_kw <= 0 is the "this block isn't configured" signal (a $0 fixed
# rate is meaningless, so no separate enable/disable flag is needed). A
# fresh install with no P2P platform at all leaves every block at its
# default (off) -- zero behaviour change, the LP decides export freely.
CONF_SOLVER_P2P_BLOCK_1_RATE_KW: Final = "solver_p2p_block_1_rate_kw"
CONF_SOLVER_P2P_BLOCK_1_START_HOUR: Final = "solver_p2p_block_1_start_hour"
CONF_SOLVER_P2P_BLOCK_1_END_HOUR: Final = "solver_p2p_block_1_end_hour"
CONF_SOLVER_P2P_BLOCK_2_RATE_KW: Final = "solver_p2p_block_2_rate_kw"
CONF_SOLVER_P2P_BLOCK_2_START_HOUR: Final = "solver_p2p_block_2_start_hour"
CONF_SOLVER_P2P_BLOCK_2_END_HOUR: Final = "solver_p2p_block_2_end_hour"
CONF_SOLVER_P2P_BLOCK_3_RATE_KW: Final = "solver_p2p_block_3_rate_kw"
CONF_SOLVER_P2P_BLOCK_3_START_HOUR: Final = "solver_p2p_block_3_start_hour"
CONF_SOLVER_P2P_BLOCK_3_END_HOUR: Final = "solver_p2p_block_3_end_hour"

# Real, per-kWh import FEES on top of the raw spot/commodity price --
# network TOU tariff + any flat always-on charge (certificates, etc.)
# (2026-08-22, direct household demand, after the 8.4c vs 7.1c Buy¢/
# Fees¢ split: "how do they configure fees column... I TOLD U NO
# HARDCODED INPUTS - this has to work as user setting" -- the writer
# script's own network_energy_rate()/CERTIFICATES_RATE had been plain
# hardcoded Python constants tuned to THIS household's own real Energex
# NTC 6900 tariff, with zero way for anyone on a different retailer/
# network/region to configure their own).
#
# SAME shape as the P2P blocks above, deliberately -- rate<=0 is the
# "not configured" signal for the 3 override blocks, no separate
# enable flag needed. Genuinely portable: a flat single-rate tariff
# just sets DEFAULT_RATE and leaves all 3 blocks off; a 2-tier
# peak/offpeak retailer sets DEFAULT_RATE (offpeak) + one block
# (peak); this household's own real 3-tier structure (peak/offpeak/
# shoulder) is DEFAULT_RATE=shoulder + Block1=peak + Block2=offpeak.
# A household that hasn't bothered figuring out their own network
# tariff at all (or is on a plan where it's simply not tracked) leaves
# everything at 0 -- Fees¢ correctly shows 0, exactly as honest/no-op
# as every other optional Solver field.
CONF_SOLVER_NETWORK_FEE_DEFAULT_RATE: Final = "solver_network_fee_default_rate"
CONF_SOLVER_NETWORK_FEE_1_RATE: Final = "solver_network_fee_1_rate"
CONF_SOLVER_NETWORK_FEE_1_START_HOUR: Final = "solver_network_fee_1_start_hour"
CONF_SOLVER_NETWORK_FEE_1_END_HOUR: Final = "solver_network_fee_1_end_hour"
CONF_SOLVER_NETWORK_FEE_2_RATE: Final = "solver_network_fee_2_rate"
CONF_SOLVER_NETWORK_FEE_2_START_HOUR: Final = "solver_network_fee_2_start_hour"
CONF_SOLVER_NETWORK_FEE_2_END_HOUR: Final = "solver_network_fee_2_end_hour"
CONF_SOLVER_NETWORK_FEE_3_RATE: Final = "solver_network_fee_3_rate"
CONF_SOLVER_NETWORK_FEE_3_START_HOUR: Final = "solver_network_fee_3_start_hour"
CONF_SOLVER_NETWORK_FEE_3_END_HOUR: Final = "solver_network_fee_3_end_hour"
# A separate, always-on flat per-kWh add-on (this household's own real
# use: Certificates $0.008246/kWh) -- distinct from the TOU blocks
# above since a flat fee applies regardless of hour, no window needed.
CONF_SOLVER_FLAT_FEE_RATE: Final = "solver_flat_fee_rate"

# Risk-aversion dials (2026-08-21) -- CONF_SOLVER_RISK_AVERSION already
# existed as a hardcoded writer-script constant (RISK_AVERSION=0.25);
# this is just exposing it as a real, live, dashboard-editable setting
# for the first time, same as every other Solver field.
CONF_SOLVER_RISK_AVERSION: Final = "solver_risk_aversion"
# Import/export price risk aversion (2026-08-21, second pass, same
# session) -- a single shared CONF_SOLVER_PRICE_RISK_AVERSION was built
# first, then split into two independent dials within hours, following
# real, direct Mark Purcell feedback: "price risk on the buy side says
# move energy now and on the sell side says the same thing in the
# opposite direction on the same state of charge, so a single control
# will fight itself" -- confirmed correct and mirrored, per Mark's own
# parallel, on how HAEO already handles this (separate weights for
# battery-to-grid vs battery-to-load, unlike EMHASS's own single shared
# weight covering everything). Genuinely replaced, not aliased --
# the single-scalar version had been live only a couple of hours with
# no real household usage to preserve, so a clean replacement is more
# honest than a deprecated-but-kept alias.
CONF_SOLVER_IMPORT_PRICE_RISK_AVERSION: Final = "solver_import_price_risk_aversion"
CONF_SOLVER_EXPORT_PRICE_RISK_AVERSION: Final = "solver_export_price_risk_aversion"

DEFAULT_SOLVER_SOH_PERCENT: Final = 100.0
DEFAULT_SOLVER_MIN_SOC_PERCENT: Final = 5.0
DEFAULT_SOLVER_MAX_SOC_PERCENT: Final = 100.0
DEFAULT_SOLVER_EFFICIENCY_PERCENT: Final = 95.0
DEFAULT_SOLVER_CHARGE_COST: Final = 0.01
DEFAULT_SOLVER_DISCHARGE_COST: Final = 0.01
DEFAULT_SOLVER_SALVAGE_VALUE: Final = 0.15
DEFAULT_SOLVER_P2P_BONUS_PRICE: Final = 0.0
DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH: Final = 0.0
# Every block defaults OFF (rate_kw=0) regardless of which block number --
# deliberately NOT seeded with any specific household's own real values,
# so a fresh install for anyone else starts genuinely blank (see this
# module's own comment above the block CONF_* keys). An already-configured
# household enables a block by setting its own real rate/hours once,
# manually, the same as tuning any other number.nimbus_solver_* entity.
DEFAULT_SOLVER_P2P_BLOCK_RATE_KW: Final = 0.0
DEFAULT_SOLVER_P2P_BLOCK_START_HOUR: Final = 0
DEFAULT_SOLVER_P2P_BLOCK_END_HOUR: Final = 0
# Every fee field defaults to 0 -- deliberately NOT seeded with this
# household's own real tariff (same "genuinely blank for anyone else"
# principle as the P2P blocks above). This household's own real values
# get set once, manually, the same as tuning any other number.nimbus_
# solver_* entity -- never hardcoded into the integration itself again.
DEFAULT_SOLVER_NETWORK_FEE_RATE: Final = 0.0
DEFAULT_SOLVER_NETWORK_FEE_START_HOUR: Final = 0
DEFAULT_SOLVER_NETWORK_FEE_END_HOUR: Final = 0
DEFAULT_SOLVER_FLAT_FEE_RATE: Final = 0.0
# Matches the writer script's own pre-existing RISK_AVERSION=0.25 default
# exactly -- rolling this platform out is a no-op for risk_aversion on an
# already-configured household. Both import/export price risk aversion
# default OFF (0.0) -- a fresh install (or this household, until set)
# sees zero behaviour change on either side.
DEFAULT_SOLVER_RISK_AVERSION: Final = 0.25
DEFAULT_SOLVER_IMPORT_PRICE_RISK_AVERSION: Final = 0.0
DEFAULT_SOLVER_EXPORT_PRICE_RISK_AVERSION: Final = 0.0

# Switchboard-level hub Configure step (2026-08-23) -- the topology
# dashboard card's own top-of-diagram sensors (grid meter, current buy/
# sell price, real switchboard-level battery power, daily kWh figures),
# read live from cfg the same way the Solver settings wizard's own
# fields are, instead of the household-specific topology_map.yaml this
# used to be hardcoded in. Genuinely everything here is Optional -- a
# fresh install with none of these filled in still gets a real diagram,
# just missing whichever rows depend on the blank fields (matches the
# already-established pattern: a blank field is a clean no-op, not a
# degraded/broken state).
CONF_SWITCHBOARD_GRID_METER_SENSOR: Final = "switchboard_grid_meter_sensor"
CONF_SWITCHBOARD_IMPORT_PRICE_SENSOR: Final = "switchboard_import_price_sensor"
CONF_SWITCHBOARD_EXPORT_PRICE_SENSOR: Final = "switchboard_export_price_sensor"
# Deliberately a SEPARATE field from solver_battery_soc_sensor (Solver
# settings) -- that one is a %, this one is the real, live, signed
# switchboard-level battery power sensor (kW) the topology card's own
# source-mix coloring needs. Different unit, different purpose, a
# household with only one of the two configured shouldn't lose either
# feature.
CONF_SWITCHBOARD_BATTERY_POWER_SENSOR: Final = "switchboard_battery_power_sensor"
CONF_SWITCHBOARD_IMPORT_ENERGY_DAILY_SENSOR: Final = (
    "switchboard_import_energy_daily_sensor"
)
CONF_SWITCHBOARD_EXPORT_ENERGY_DAILY_SENSOR: Final = (
    "switchboard_export_energy_daily_sensor"
)
CONF_SWITCHBOARD_HOUSE_LOAD_ENERGY_DAILY_SENSOR: Final = (
    "switchboard_house_load_energy_daily_sensor"
)
CONF_SWITCHBOARD_SOLAR_ENERGY_DAILY_SENSOR: Final = (
    "switchboard_solar_energy_daily_sensor"
)
CONF_SWITCHBOARD_BATTERY_CHARGE_DAILY_SENSOR: Final = (
    "switchboard_battery_charge_daily_sensor"
)
CONF_SWITCHBOARD_BATTERY_DISCHARGE_DAILY_SENSOR: Final = (
    "switchboard_battery_discharge_daily_sensor"
)
