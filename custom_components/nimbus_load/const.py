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

CONF_LOAD_SENSOR: Final = "load_sensor"
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
# "load" or "power_signal" -- lets anything downstream (e.g. a dashboard
# chart script) tell the two subentry types apart generically, by
# reading this live attribute, rather than by hardcoding which specific
# entity names belong to which category.
ATTR_SUBENTRY_TYPE: Final = "subentry_type"

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
# ONE blended round-trip efficiency number (battery chemistry loss +
# inverter AC-DC conversion loss combined) -- HAEO models these as two
# separate numbers on two separate elements; Nimbus deliberately doesn't,
# per the same already-validated simplification.
CONF_SOLVER_EFFICIENCY_PERCENT: Final = "solver_efficiency_percent"
CONF_SOLVER_GRID_MAX_IMPORT_KW: Final = "solver_grid_max_import_kw"
CONF_SOLVER_GRID_MAX_EXPORT_KW: Final = "solver_grid_max_export_kw"
CONF_SOLVER_IMPORT_PRICE_SENSOR: Final = "solver_import_price_sensor"
CONF_SOLVER_EXPORT_PRICE_SENSOR: Final = "solver_export_price_sensor"
# Entity references only -- panel size/tilt/azimuth/inverter model
# numbers are deliberately NOT asked for here. That's a separate solar-
# forecast integration's job (Solcast, Forecast.Solar, etc, which already
# handle those specs in their own config); the Solver only needs to know
# WHICH entity carries the resulting forecast, same division of
# responsibility HAEO's own schema uses (core/schema/elements/solar.py --
# a bare forecast entity reference, nothing about the panels themselves).
CONF_SOLVER_SOLAR_FORECAST_SENSOR: Final = "solver_solar_forecast_sensor"
CONF_SOLVER_LOAD_FORECAST_SENSOR: Final = "solver_load_forecast_sensor"
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
# Optional, P2P-specific -- only relevant to an installer with a peer-to-
# peer energy trading platform (e.g. LocalVolts in Australia). Left blank
# (0) for anyone without one; the Solver simply never sees any bonus-
# priced export opportunity in that case.
CONF_SOLVER_P2P_BONUS_PRICE: Final = "solver_p2p_bonus_price"
CONF_SOLVER_P2P_BONUS_VOLUME_KWH: Final = "solver_p2p_bonus_volume_kwh"

DEFAULT_SOLVER_SOH_PERCENT: Final = 100.0
DEFAULT_SOLVER_MIN_SOC_PERCENT: Final = 5.0
DEFAULT_SOLVER_MAX_SOC_PERCENT: Final = 100.0
DEFAULT_SOLVER_EFFICIENCY_PERCENT: Final = 95.0
DEFAULT_SOLVER_CHARGE_COST: Final = 0.01
DEFAULT_SOLVER_DISCHARGE_COST: Final = 0.01
DEFAULT_SOLVER_SALVAGE_VALUE: Final = 0.15
DEFAULT_SOLVER_P2P_BONUS_PRICE: Final = 0.0
DEFAULT_SOLVER_P2P_BONUS_VOLUME_KWH: Final = 0.0
