# const.py: comment history

Moved out of `custom_components/nimbus_load/const.py` by #2289 item 7 (the method agreed with Mark Purcell, 7 Nov 2026), verbatim, keyed by the code line each block sat above. Line numbers are the file's at `main` before the move.

## Line 16: above `SUBENTRY_TYPE_SIGNAL: Final = "power_signal"`

```text
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
```

## Line 33: above `SUBENTRY_TYPE_POWER_SOURCE: Final = "power_source"`

```text
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
```

## Line 625: above `CONF_SOLVE_ON_PRICE_CHANGE: Final = "solve_on_price_change"`

```text
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
```

## Line 745: above `UPDATE_INTERVAL_MINUTES: Final = 2`

```text
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
```

## Line 876: above `CONF_SOLVER_BATTERY_CAPACITY_KWH: Final = "solver_battery_capacity_kwh"`

```text
# --- Solver configuration (2026-08-20) ---
# Everything the Solver (custom_components/nimbus_load/solver/) needs to
# build a real dispatch plan for ANY household, not just this one.
# Previously these lived as bare input_number/entity helpers this
# household hand-created (the reference household's own research scripts
# hold the exact entity IDs that predate this), with no structured onboarding at all -- a real installer
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
```

## Line 913: above `CONF_SOLVER_MAX_DISCHARGE_LIVE_ENTITY: Final = "solver_max_discharge_live_entity"`

```text
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
```

## Line 1013: above `CONF_SOLVER_AUTO_INCLUDE_KNOWN_SOLAR: Final = "solver_auto_include_known_solar"`

```text
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
# nimbus #1994: both integrations are found through the entity registry by
# their integration and built-in unique_id (known_sources.py), never by an
# entity_id, so a renamed or localised entity is still found. A brand-new
# install that already has Solcast starts with auto-include ON (1.0.0
# one-click install, #1930; Mark's #1914 recommendation): a household on
# Solcast should not have to find a switch to get the forecast it already
# has. An existing switch keeps its value.
```
