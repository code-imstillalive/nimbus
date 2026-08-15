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
# only, no retraining).
UPDATE_INTERVAL_MINUTES: Final = 15

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

# Entity attribute keys, matching HAEO's own native forecast-sensor format
# (custom_components/haeo/core/data/loader/extractors/haeo.py Parser.detect()/
# extract()): a "forecast" attribute holding a list of {"time": ..., "value": ...}
# dicts, plus a top-level unit_of_measurement / device_class.
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
