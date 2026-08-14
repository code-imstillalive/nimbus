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

CONF_FORECAST_HORIZON_HOURS: Final = "forecast_horizon_hours"
CONF_RETRAIN_HOUR_LOCAL: Final = "retrain_hour_local"
CONF_TRAIN_DAYS: Final = "train_days"

DEFAULT_FORECAST_HORIZON_HOURS: Final = 48
DEFAULT_RETRAIN_HOUR_LOCAL: Final = 3
DEFAULT_TRAIN_DAYS: Final = 30
DEFAULT_FALLBACK_TEMPERATURE_C: Final = 22.0

# Forecast/prediction grid resolution.
RESAMPLE_MINUTES: Final = 15

# Coordinator polling cadence -- how often the published forecast is
# regenerated from whichever model is currently loaded (cheap: inference
# only, no retraining).
UPDATE_INTERVAL_MINUTES: Final = 15

# Sanity floor -- refuse to train on too little real history.
MIN_TRAINING_POINTS: Final = 500

# Entity attribute keys, matching HAEO's own native forecast-sensor format
# (custom_components/haeo/core/data/loader/extractors/haeo.py Parser.detect()/
# extract()): a "forecast" attribute holding a list of {"time": ..., "value": ...}
# dicts, plus a top-level unit_of_measurement / device_class.
ATTR_FORECAST: Final = "forecast"
ATTR_MODEL_TRAINED_AT: Final = "model_trained_at"
ATTR_TRAINING_POINTS: Final = "training_points"
