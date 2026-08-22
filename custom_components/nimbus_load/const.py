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
