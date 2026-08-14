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

CONF_FORECAST_HORIZON_HOURS: Final = "forecast_horizon_hours"
CONF_RETRAIN_HOUR_LOCAL: Final = "retrain_hour_local"
CONF_TRAIN_DAYS: Final = "train_days"

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
