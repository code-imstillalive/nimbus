# Changelog

All notable changes to Nimbus are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adopts [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and both the Nimbus integration (`custom_components/nimbus_load`) and the Nimbus Solver add-on (`nimbus_solver_app`) share a single version line — the `version-lockstep` CI job enforces that they never drift.

Entries call out real, user-visible changes. They are not a `git log` dump; the commit history is the source of truth for the underlying diffs.

## [Unreleased]

_Add new entries here as each PR lands. They roll into the next tagged release._

## [0.92.0] — 2026-08-25

### Added
- **Retrospective efficiency-sensitivity backtesting engine**: the first
  check in a new offline backtesting engine that proves Nimbus's own
  decisions against reality rather than a bigger LP or a fancier model.
  Re-solves a real, already-elapsed day's real known load/solar/price
  under alternative round-trip efficiency candidates (85/90/95/99%),
  scores each with the existing regret evaluator, and reports the $
  spread between best and worst -- "how sensitive is your economics to
  efficiency, on a day that actually happened." New
  `sensor.nimbus_efficiency_backtest`, plus a dashboard section on the
  Solver view. Deliberately does not attempt to test `risk_aversion` or
  forecast-source choice -- both are mathematically inert under a
  perfect-foresight backtest (no forecast uncertainty band exists once
  ground truth is known); see `solver/backtest.py`'s own module
  docstring for the full reasoning. Strictly observational -- never
  writes back to config automatically.

## [0.91.0] — 2026-08-25

### Added
- **Cross-signal anomaly layer, first check**: forecast residual drift detection.
  Compares a signal's recent one-step-ahead forecast error against that same
  signal's own historical baseline (self-calibrated, no fixed global
  threshold) and logs a WARNING -- already surfaced via the existing health
  report -- when it drifts significantly worse. Reuses the confidence-band
  calibration data every forecaster already maintains; no new data
  collection. Strictly observational: cannot affect a solve or the published
  forecast. First step toward automatically catching the same shape of
  problem (silent data-quality issues, model degradation, sensor drift) that
  previously needed a human staring at a live chart to notice.

## [0.90.0] — 2026-08-25

### Added
- **Blended multi-source price forecasting**: optional
  `solver_import_price_sensor_2`/`_3` and `solver_export_price_sensor_2`/`_3`
  wizard fields, mirroring the existing solar multi-source blending pattern.
  Feed more than one independently-modeled price forecast (e.g. a real AEMO
  wholesale forecast alongside a retailer's own forecast such as Amber) and
  the Solver blends them into one point estimate, using the real disagreement
  between sources to widen the existing `price_risk_aversion` band as an
  earned uncertainty signal. All new fields are optional and blank by
  default -- a single-source install is byte-identical to before.
- `resample_generic_price_forecast()` now also accepts a `calibrated` key as
  a fallback when `value` is absent, so a blend source can be pointed
  directly at a spike-calibrated forecast sensor (e.g. Mark Purcell's own
  NEM PD7, `sensor.nem_pd7day_qld1_nem_spot_price_forecast`) without its
  points being silently dropped.

## [0.89.1] — 2026-08-25

### Added
- **`source_sensor` attribute on load/power-signal forecast sensors**: the
  missing other half of `signal_role`'s own "auto-discover which power signal
  is Grid/Battery/Solar directly from hass.states, zero config file needed"
  mechanism -- a dashboard can now genuinely resolve the real live entity_id
  behind a role without hardcoding a guess.

## [0.89.0] — 2026-08-25

### Added
- **Solar delivery ratio diagnostic** (nimbus issue #128, Mark Purcell):
  `solar_delivery_ratio` / `solar_delivery_sample_count` /
  `solar_delivery_underperforming` on `sensor.nimbus_solver_battery_forecast` --
  a rolling actual-vs-forecast solar comparison that catches implicit inverter
  AC-side clipping `switch.solar_curtailment` (#114) can't see, since that
  switch only reports EXPLICIT curtailment control. Fully generic and
  optional: reuses the existing `solver_solar_power_sensor` wizard field, no
  new config surface.

## [0.88.0] — 2026-08-25

### Added
- **Training-diagnostic visibility for the Forecaster** (nimbus issue #113, Mark
  Purcell): `mase_scale_points` (how many real week-over-week points MASE's own
  scale actually found, even below the threshold needed), `resample_minutes`
  (the fixed grid spacing every training row is built on), and
  `training_span_days` (the real elapsed calendar span the deployed model
  actually trained on, distinct from the configured `train_days` request) --
  exposed on every load/power-signal forecast sensor's attributes. Makes
  `validation_mase`'s own honest "can't compute this yet" empty-dict behavior
  diagnosable instead of looking broken.

## [0.87.1] — 2026-08-25

### Fixed
- **Efficiency-convention mismatch in the EPR quality report and Nimbus-only
  counterfactual replay** (nimbus issue #168, Mark Purcell): both scorers applied
  `solver_efficiency_percent` (a round-trip figure) directly to both charge and
  discharge, instead of `sqrt()`-splitting it the way the live plan's own real
  `main()` solve does -- meaning they evaluated a battery physically more lossy
  than the one actually being scored. Fixed to match `main()`'s convention exactly.

### Added
- **Energy-balance diagnostic fields** on `sensor.nimbus_solver_battery_forecast`
  (nimbus issue #168): `battery_kw_side` ("AC"), `efficiency_convention`
  ("round_trip_symmetric_sqrt"), `charge_efficiency`/`discharge_efficiency` (the
  actual derived floats), and `ac_bus_losses_kwh` -- so a diagnostic reader no
  longer has to reverse-engineer the storage-identity convention by hand.

## [0.87.0] — 2026-08-25

### Added
- **Nimbus-only counterfactual SoC replay** (`sensor.nimbus_counterfactual_soc`,
  "nuc one nimbus solver view has counterfactual table.... i want u to build that
  into devbox package"): generic, wizard-config-driven port of the reference
  household's own NUC1 script. Replays an already-elapsed calendar day with a real
  receding-horizon LP re-solve every 15 minutes, feeding each tick's own simulated
  SoC into the next (never the real, possibly HAEO-influenced SoC), and compares
  the result against what actually happened -- answering "would Nimbus's own
  reasoning have kept the battery in a sane state, if it had been driving all
  along." Every P2P-related input is the same optional wizard field the live
  writer already reads; a household with no P2P scheme configured gets a complete
  no-op (no checkpoint/viability verdict), never a crash or a leaked
  household-specific default.

## [0.86.1] — 2026-08-25

### Fixed
- **Diagnostics: `data.solver` was silently dropping newly-added attributes**
  (nimbus issue #116, Mark Purcell): `_solver_diagnostics()` copied a curated,
  hand-picked subset of `sensor.nimbus_solver_battery_forecast`'s attributes by
  name into the downloaded diagnostics dump. That allowlist stopped tracking
  `solver_writer.py`'s real output -- `cost_breakdown` (v0.82 #149) and
  `load_forecast_source_used` (v0.83 #148) both landed correctly on the live
  entity but showed as `null` in the dump, a false-negative that could make a
  real fix look like it hadn't shipped. Fixed by spreading the entity's full
  attribute dict instead of naming fields one at a time -- any current or
  future solver attribute is now automatically visible with zero maintenance.

## [0.86.0] — 2026-08-25

### Added
- **`cost_band` diagnostic on the Solver plan** (nimbus issue #147, Mark Purcell):
  re-costs the LP's own committed dispatch at the load forecast's stated lower/upper
  confidence bounds (`compute_cost_band()`, `regret.evaluate_realized_cost()`), so an
  operator can tell at a glance whether a plan is a confident call or a coin flip.
  No LP change -- read-only post-hoc analysis on the already-solved plan. Deliberately
  excludes P2P bonus revenue; documented as an honest lower bound on the true swing.
- **Always-on health report** (`sensor.nimbus_health_report`): a plain `logging.Handler`
  now captures every WARNING+/ERROR record from this integration into a bounded,
  always-populated buffer -- no more relying on someone noticing a symptom and then
  grepping `error_log` after the fact. Exposes `recent_errors`, `recent_warnings`,
  `never_trained` (subentries whose model has genuinely never produced output), and
  per-subentry `mode`/`training_points`/`model_trained_at`/`forecast_point_count`.

## [0.85.0] — 2026-08-25

### Added
- **Built-in EPR/regret/tracking quality score** (`sensor.nimbus_solver_quality_report`,
  "it should be a part of the suite to monitor epr and trend and
  regret... nimbus should have it built in"): a from-scratch,
  retailer-agnostic generalization of the real-world reference script
  (`docs/real-world-integration/files/nimbus_solver_quality_writer.py`),
  which hardcodes one household's own LocalVolts/Sungrow/Modbus stack.
  Scores yesterday's real dispatch against a perfect-foresight oracle
  using only genuinely portable inputs: two new optional Solver
  settings fields (`solver_solar_power_sensor`,
  `solver_battery_power_sensor` -- real measured, not forecast) plus
  the existing `solver_whole_house_cross_check_sensor` for load. An
  optional `solver_p2p_settlement_history_sensor` field improves
  accuracy for a household with a real P2P/VPP settlement program,
  without requiring one. Blank on any required field is a clean no-op.
- **`solver_weather_forecast_sensor`** -- a new optional Solver settings
  field pointing at any `weather.*` or `sensor.*` forecast source (same
  dual-shape support as the existing `temperature_forecast_sensor`),
  driving a real forward temperature/humidity mirror for the dashboard
  Forecaster chart. Humidity is only published when the configured
  source's own forecast actually carries one (e.g. Pirate Weather;
  Open-Meteo doesn't). Purely cosmetic -- never feeds the LP solve.

## [0.84.1] — 2026-08-25

### Fixed
- **Weather forecast mirror silently no-op'd in native mode** (real
  regression, found live on devhub immediately after v0.84.0 deployed):
  `ha_call_service_with_response()` returned `None` unconditionally in
  native mode (`solver_runtime`, in-process -- devhub's actual real
  deployment shape), so `publish_weather_forecast_mirrors()` never
  published anything there. Fixed with the same
  `asyncio.run_coroutine_threadsafe()` bridge `fetch_price_history()`'s
  own native branch already uses.

## [0.84.0] — 2026-08-25

### Added
- **`load_forecast_coverage_hours` on the Solver plan and the household
  load rollup** (#112, "solver horizon 96.3h exceeds subentry forecast
  horizon 48h"): `read_load_forecast_sensor()`/`sum_load_forecasts()` now
  capture each source's real forecast coverage, in hours ahead of `now`,
  on the raw forecast list -- before `resample_forecast()`'s flat-hold
  padding makes a real point and a padded-flat point indistinguishable.
  For the multi-circuit sum, coverage is the minimum across every circuit
  that fetched successfully. Compare directly against `horizon_hours` to
  see when part of a plan's own load input is padding, not a real
  forecast.
- **`publish_weather_forecast_mirrors()`** -- a real forward temperature
  forecast for the devhub test dashboard, sourced from an installed
  weather forecaster (Open-Meteo) via `weather.get_forecasts` (modern HA
  weather entities no longer expose a plain `forecast` attribute).
  Purely cosmetic/devhub-dashboard; never referenced by the actual LP
  solve.

## [0.83.0] — 2026-08-25

### Added
- **`load_forecast_source_used` on the Solver plan and the household load
  rollup** (Mark Purcell, real repro, #148, plus the same-shaped ask in
  #116): `solver_load_forecast_sensor` can be configured, correct, and
  completely silently overridden the instant
  `solver_load_forecast_entities` has even one entry -- documented in
  prose in this project's own README precedence table, but never
  surfaced as a diagnostic field. `sensor.nimbus_household_load_total_
  forecast` and `sensor.nimbus_solver_battery_forecast` now both carry
  `load_forecast_source_used`, naming plainly either the single
  configured sensor or exactly which circuits got summed instead.

## [0.82.0] — 2026-08-25

### Added
- **Named cost-component breakdown on the Solver plan** (Mark Purcell,
  real repro + executable reconciliation tests, #149): `total_cost`
  could not be reconstructed from anything else in the diagnostics
  dump -- off by exactly degradation + charge_fee + discharge_fee minus
  the #144 terminal-value credit, with no field naming any of those
  terms. `sensor.nimbus_solver_battery_forecast` now carries a
  `cost_breakdown` attribute (`grid_net`, `degradation`, `charge_fee`,
  `discharge_fee`, `terminal_value_credit`) that reconciles exactly to
  `total_cost` by construction.

### Fixed
- **Network TOU fees and the flat fee rate were silently ignored for
  any non-LocalVolts install** (#152): `import_fee_rate()` and the flat
  fee rate were applied only inside the LocalVolts-specific branch of
  the price-building block, even though both are generic, retailer-
  agnostic config-flow fields. Any other install that filled them in
  via the wizard had them silently ignored -- zero fee applied, zero
  warning. Fixed by applying fees uniformly regardless of retailer path.
- **Empty forecast attribute conflated with a missing/malformed one --
  confusing error on cold-start installs** (#150): a brand-new load
  subentry whose forecaster hasn't trained on enough history yet
  produces a genuinely empty `forecast: []`, which was being reported
  with the same "no usable forecast attribute" message as a truly
  broken sensor. Fixed with a distinct, honest "empty, not yet
  trained" message for the empty-list case.

## [0.81.0] — 2026-08-24

### Fixed
- **Terminal-value credit compounded across multiple day-boundary
  checkpoints, making the LP treat holding charge as worth up to ~4x
  its configured value** (Mark Purcell, real repro, #144): on a real
  4-day horizon (4 real midnight checkpoints + the true final period),
  the LP idled the battery at ~60% SoC for 5+ hours while grid-buying
  at 15-22c/kWh, only discharging once import price crossed ~40c,
  despite a configured marginal discharge cost of ~9c/kWh. Root cause:
  the 2026-08-22 fix that lets the Solver respect multi-day P2P windows
  applied the full terminal-value curve at EVERY real midnight in the
  horizon, not just the true end -- the same physical stored energy was
  earning a full terminal-value credit at every boundary it survived
  through. Confirmed directly: `total_cost` kept getting artificially
  "better" the more checkpoints existed, and SoC hours before any
  boundary snapped from empty to full the moment a second checkpoint
  was added. Fix: only the true final period gets the full, unscaled
  curve; every intermediate day-boundary checkpoint gets it divided by
  the number of intermediate checkpoints, so the cumulative incentive a
  single unit of energy can ever collect stays bounded to roughly one
  terminal-value-equivalent, not one per checkpoint. A genuine no-op
  for the original single-intermediate-checkpoint shape.

## [0.80.0] — 2026-08-24

### Added
- **Diagnostics now include the Solver's full resolved config and full
  plan/forecast arrays** (direct household + Mark Purcell instruction:
  "diagnostics must have everything in it incl pre-set values" /
  "get more data into the diagnostic file so we can actually understand
  the reason its making decisions rather than just speculation without
  any data to backup"). Battery capacity, min/max SoC, max
  charge/discharge, costs, salvage value, both risk_aversion values,
  P2P bonus blocks, network fee schedule -- every field
  `sensor.nimbus_solver_config` resolves, verbatim. Plus the full real
  Solver plan, household load total, and every subentry's own forecast
  array -- previously excluded as "already visible on the entity,"
  which didn't hold up: a downloaded diagnostics file has no
  16384-byte recorder-attribute limit, and the live entity's own
  forecast keeps moving, so a diagnostics dump is the only genuine
  point-in-time snapshot available.

## [0.79.0] — 2026-08-24

### Fixed
- **Solver crashed every cycle with Min SoC configured to 0%** (Mark
  Purcell, real repro, direct follow-up to #58 — 20 consecutive crashes
  over 24 minutes): #58's own fix (Mark's PR #64) correctly stopped an
  intentional 0.0 from being silently reverted to the 5.0% default, but
  never addressed what happens once that genuine 0% actually reaches
  `BatteryConfig`'s own strict `0 < min_soc_kwh` invariant — a real,
  deliberate LP-level degeneracy/safety floor, not negotiable at that
  layer. `resolve_min_soc_kwh()` closes the gap: floors to a
  negligible-but-strictly-positive value (0.05% of capacity —
  effectively "no reserve," not a literal 0%) and warns, instead of
  crashing.
- **Solver crashed on a non-numeric price sensor state**
  (`ValueError: could not convert string to float:
  '2026-08-24T13:00:00+10:00'`, same real install, same session): the
  price-sensor scalar-fallback read had zero protection against a
  configured entity's real state not being numeric. `safe_num()`
  replaces the old, untestable inline closure with a standalone
  function that warns and falls back to `0.0` instead of crashing the
  whole solve — used for both price-sensor fallback reads and the live
  battery SoC read.

## [0.78.0] — 2026-08-24

### Fixed
- **`temperature_forecast_sensor` crashed the coordinator on `weather.*`
  entities emitting naive datetimes** (Mark Purcell, real repro, #137,
  direct follow-up to #123): some real weather integrations (his own
  `weather.noosa_heads_hourly`) return forecast datetimes with no
  timezone offset at all — the #123 fix widened the field to accept
  `weather.*` entities but never normalised the datetimes it got back,
  so a naive value compared against an always-aware one crashed every
  coordinator tick outright (`TypeError: can't compare offset-naive and
  offset-aware datetimes`). Naive timestamps are now treated as the
  installation's own local time (a pure relabel, no numeric shift) —
  confirmed against a real, subtle bug in the first suggested fix
  (`dt_util.as_local()` on a naive value assumes UTC first, which would
  have shifted the numbers by a full UTC offset instead of leaving them
  alone). Same fix applied to the curtailment-forecast path, the other
  real site parsing external datetimes.

## [0.77.0] — 2026-08-24

### Fixed
- **`binding_constraint_now` could report the wrong story** (Mark
  Purcell, real repro, #125/#133): a nonzero LP reduced cost fires
  whenever a variable is pinned at EITHER of its own bounds, not only
  its upper/capacity bound — a genuine "not economical to discharge
  right now" decision (pinned at 0) and a genuine "hit the real
  ceiling" decision (pinned at the configured max) both produced a
  nonzero reduced cost on the same variable, but this diagnostic
  labelled both identically as e.g. "Battery max discharge power".
  Confirmed on a real install: the battery was genuinely CHARGING at
  period 0 (not discharging at all), while this field still reported
  "Battery max discharge power" — actively misleading, and the direct
  cause of a false "second override path" bug report (the real config,
  #125's own fix, and the underlying LP bound were all confirmed
  correct by direct source read). Now disambiguated by checking the
  variable's own real solved value against its two bounds: the genuine
  ceiling case keeps the exact original 4 label strings (no
  compatibility break for that case), the "pinned at zero" case gets
  its own new, distinct, honest label instead.

## [0.76.0] — 2026-08-24

Both pieces of the Solver settings wizard-simplification scoping (direct
Mark Purcell critique: "wizard complexity... how confusing the process is
and entities are") — makes the entity-pointer fields easier to fill in
cold, on a fresh install with dozens or hundreds of unrelated entities.

### Changed
- **Solver settings wizard — "Group A" of the config-flow simplification**
  (direct Mark Purcell critique: entity-pointer fields are hard to fill
  in cold on a fresh install). The Sources step's two load-forecast
  fields (`solver_load_forecast_sensor`, `solver_load_forecast_entities`)
  now restrict their own entity pickers to Nimbus's real, live forecast
  output instead of offering every `sensor.*` on the system. The single-
  sensor field deliberately excludes
  `sensor.nimbus_household_load_total_forecast` — pointing it there is
  exactly the circular-reference bug [#118](https://github.com/code-imstillalive/nimbus/issues/118)
  fixed defensively; this closes the same gap at the source by never
  offering it as a choice. Purely a picker restriction — an already-saved
  value, or an install where discovery finds nothing yet, is completely
  unaffected (a picker with no candidates falls back to showing
  everything, same as before this change).
- **"Group B" of the same simplification**: the Forecaster step's
  temperature/humidity fields and the Solver Battery step's SoC field
  now get a pre-filled *suggestion* (never a picker restriction — these
  are raw household hardware sensors, not Nimbus's own self-tagged
  output, so a hard filter risked hiding a genuinely correct but
  untagged sensor) whenever exactly one live entity of the matching
  `device_class` exists system-wide. Deliberately narrower than a naive
  "filter every raw sensor field by device_class" approach — the
  battery/grid/solar power fields and both import/export price fields
  are excluded on purpose, since even a single matching entity there is
  genuinely ambiguous about *which* of two or three fields it belongs
  to; guessing wrong would be confidently misleading, worse than no
  suggestion at all. An already-saved value is never overwritten by a
  suggestion, same discipline as every other suggestion mechanism in
  this file.

## [0.75.0] — 2026-08-24

Mark Purcell's own independent-install health-check found four real bugs
this release — three that could produce a confidently-wrong plan with no
warning, one that silently overrode a configured value with an unrelated
entity's own reading.

### Added
- **`solver_max_discharge_live_entity`** (Solver settings wizard, Battery
  step, optional): lets a household point the Solver at a real, live
  hardware setpoint entity whose own `max` attribute should override the
  static `solver_max_discharge_kw` number — a genuine safety margin
  against a real hardware ceiling changing without the number entity
  being updated. Unset (the default) is a complete no-op.
- **`number.nimbus_solver_inverter_self_consumption_kw`** (optional,
  default `0.0`): a per-household inverter self-consumption bias, added
  to the household load total. Replaces a hardcoded `0.215` constant that
  used to apply to every install regardless of hardware.
- `temperature_forecast_sensor` now accepts `weather.*` entities
  directly, calling `weather.get_forecasts` (hourly) internally — modern
  Home Assistant (2024.x+) removed the `forecast` state attribute from
  weather entities entirely, so pointing straight at one used to silently
  produce zero forecast data. `sensor.*` template-sensor configs are
  unaffected. A WARNING now logs once per coordinator instance if a
  configured `temperature_forecast_sensor` yields zero entries.

### Fixed
- **High-priority, real $ impact**: the Solver's own discharge-power
  cap could be silently overridden by an unrelated entity that happened
  to exist at a hardcoded name
  (`number.logger_charging_discharging_power_kw`), clamping real
  discharge capability to whatever that unrelated entity's own `max`
  attribute reported — with zero warning ([#125](https://github.com/code-imstillalive/nimbus/issues/125)). Fixed by making the
  entity_id a genuine, optional config field instead (see Added, above).
- The Solver used to `raise RuntimeError` and refuse to solve at all
  when every configured solar forecast source produced no data — a
  condition that recurs every single night on every solar install (0.0
  kW overnight is correct, not a failure). Now solves with a real,
  honest 0.0 kW placeholder and a loud warning instead of going dark for
  hours ([#115](https://github.com/code-imstillalive/nimbus/issues/115)).
- A structurally-valid but near-all-zero load forecast (e.g. from a
  circular reference — `solver_load_forecast_sensor` accidentally
  pointed at Nimbus's own household-total aggregator) used to be
  accepted as a genuine forecast, producing a confidently-wrong,
  fully-costed dispatch plan with no error surfaced anywhere. Now
  rejected with a specific, actionable error message ([#118](https://github.com/code-imstillalive/nimbus/issues/118)).
- The multi-circuit load-summing path (`solver_load_forecast_entities`)
  never had the shape/unit validation the single-sensor path already
  had, letting a malformed source silently corrupt the household load
  total. Both paths now share the same validation ([#105](https://github.com/code-imstillalive/nimbus/issues/105)).
- `sensor.nimbus_household_load_total_forecast` could report a
  `native_value` that silently disagreed with its own `forecast[0]`.

### Changed
- `flows/hub_options.py`'s options-flow merge logic is documented
  in-line at the exact point of a real, structural tension: a genuine
  partial config-patch and a real UI form submission need the same
  "key absent" signal to mean opposite things, and no code-only fix
  serves both. Callers needing a genuine partial patch should use
  `hass.config_entries.async_update_entry(entry, options={**entry.options, **partial})`
  directly rather than the options-flow step ([#121](https://github.com/code-imstillalive/nimbus/issues/121)).
- Repo-wide `ruff` cleanup: every blind-except, naive-datetime, and
  unnecessary-collection-call finding from the #72 backlog now has its
  own individually-reasoned fix or suppression comment, replacing a
  blanket rule-level ignore.
- The "Understanding the configuration model" section is now a
  permanent part of `README.md`.

## [0.74.0] — 2026-08-23

### Added
- **Topology card ships bundled with the integration**: install Nimbus via HACS, restart HA, drop `type: custom:switchboard-topology-card` into any dashboard view — it resolves immediately, with no `www/` file copy and no manual Settings → Dashboards → Resources step ([#79](https://github.com/code-imstillalive/nimbus/issues/79), [#92](https://github.com/code-imstillalive/nimbus/pull/92)). `custom_components/nimbus_load/frontend/switchboard-topology-card.js` is the same file as `docs/real-world-integration/files/topology-card-v4.js`; a new `frontend.py` module owns registration. HTTP serving via `hass.http.async_register_static_paths([StaticPathConfig(...)])` at `/nimbus_load/switchboard-topology-card.js`, plus registration as an extra JS module via `homeassistant.components.frontend.add_extra_js_url(hass, url)` — HA's own public, documented API for injecting frontend JS from an integration, so both storage-mode and YAML-mode Lovelace dashboards see the module with no writes to the user's `.storage/lovelace_resources`. Cache-busting via `?v=<manifest-version>`. Both steps are idempotent and non-fatal — a failure at registration is logged and the forecaster, sensors, and solver all still work.

### Changed
- `manifest.json` declares `"http"` as a dependency (needed by the static-path registration, caught by hassfest during PR #92 CI review).
- `nimbus_solver_app/config.yaml` version bumped in lockstep to `0.74.0`.

## [0.73.3] — 2026-08-23

### Added
- `sensor.nimbus_solver_config` now logs on every real `configured → unconfigured` transition (WARNING) and on recovery (INFO), instead of staying silent — a startup race (`RestoreEntity` still restoring the wizard's `number.nimbus_solver_*` entities when this sensor is first polled) is now directly visible in the log rather than only surfacing as an unattributed "not configured yet" warning from the Solver runtime. Logging is on-transition only; a stable `configured` or `unconfigured` state produces no repeated log spam.
- New `unresolved_required_keys` extra state attribute on `sensor.nimbus_solver_config` — empty on the happy path, otherwise names exactly which of the 10 required Solver fields aren't resolved yet. Readable over plain REST (`/api/states/sensor.nimbus_solver_config`) for triage without needing HA's own logs.
- 9 new regression tests (`tests/test_sensor_solver_config_flap.py`) covering the transition logging, the new attribute, and log-on-transition discipline (no per-poll spam).

No behavioural change to `native_value` itself — this release is observation-only. Real, live-verified data (Mark Purcell, [#85](https://github.com/code-imstillalive/nimbus/issues/85)): every HA restart produces a single ~30-second `configured → unconfigured → configured` window while the wizard's number entities restore; this is a separate, narrower, self-healing race distinct from the mid-session flap fixed in v0.73.2/[#90](https://github.com/code-imstillalive/nimbus/pull/90) — a real fix for the startup race is still open for follow-up.

## [0.73.2] — 2026-08-23

### Fixed
- **Critical**: `sensor.nimbus_solver_battery_forecast` and `sensor.nimbus_household_load_total_forecast` flapped between a real plan and `unknown` on a ~60-second cycle on real HA — found by Mark Purcell within hours of the v0.73.1 fix landing ([#83](https://github.com/code-imstillalive/nimbus/issues/83)). Root cause: `_async_recheck_availability()` (the periodic self-driven timer added in v0.73.1 to catch a Solver going genuinely stale) called `async_write_ha_state()` unconditionally on every tick, republishing `native_value` regardless of whether anything had actually changed — including before the very first real push, when `native_value` is honestly `None`. Racing against `update_from_solver()`'s own real pushes on the same ~60s cadence, this periodically clobbered a fresh plan with `unknown` moments after it was published, only for the next solve to overwrite it again. Fixed: the recheck now only calls `async_write_ha_state()` (and logs) on a genuine transition in `available`'s own value — a no-op tick (the overwhelming majority of ticks, by design) does nothing at all. Two new regression tests lock in the exact flap scenario: a first tick before any push has landed, and repeated ticks while nothing has changed.

## [0.73.1] — 2026-08-23

### Fixed
- **Critical**: `sensor.nimbus_solver_battery_forecast` and `sensor.nimbus_household_load_total_forecast` were stuck at `unknown` forever, crashing on every single solve tick, on any real (non-test-stub) Home Assistant instance — found by Mark Purcell's own live v0.73.0 install within hours of release ([#82](https://github.com/code-imstillalive/nimbus/issues/82)). Root cause: `update_from_solver()` (the entity method `solver_writer.ha_post_state()`'s dispatch table calls via `hass.add_job()`) and `_async_recheck_availability()` (registered directly as `async_track_time_interval`'s own callback) were both plain, undecorated methods — real HA's `add_job()` inspects a target for the `_hass_callback` marker to decide whether to run it directly on the event loop or dispatch it to the executor thread pool; undecorated, both were routed to a worker thread, where their own `async_write_ha_state()` call (genuinely requires the event loop) raised `RuntimeError` silently on every call. Fixed by marking both `@callback`, the textbook-correct fix for a fast, non-blocking, pure state-machine method. Neither the unit test suite nor its `homeassistant.core.callback` stub (previously a plain identity lambda) could have caught this — the stub is now a faithful replica of real HA's own marker-setting behaviour, and two new regression tests assert the marker directly.

## [0.73.0] — 2026-08-23

### Deprecated
- `nimbus_solver_app` (the HAOS Supervisor add-on) is deprecated as of v0.73.0 and will be removed in v1.0.0. The Nimbus HACS integration now runs the Solver natively in-process on HAOS (`custom_components/nimbus_load/solver_runtime.py`, one-minute timer, `highspy` auto-installed as a `manifest.json` requirement), covering every architecture the add-on covered (both need a `highspy` wheel, so both are amd64/aarch64 only) with no separate container, no three-way copy sync, and no version-lockstep discipline. Existing installs: uninstall the add-on and finish the integration's own **Solver settings** wizard — the native path takes over the same `sensor.nimbus_solver_*` outputs with no config migration needed. See the add-on's own README.md and its startup log for the same notice; the top-level README has moved the add-on out of the supported-paths list into a Deprecated note pointing at the native path. Tracking: [#76](https://github.com/code-imstillalive/nimbus/issues/76).

### Added
- `sensor.nimbus_solver_battery_forecast` and `sensor.nimbus_household_load_total_forecast` are now real `SensorEntity` classes attached to the Nimbus hub device, instead of raw `states.async_set` writes. Existing `entity_id`s are preserved, so long-term stats and history continue uninterrupted. Both entities now carry `device_class=power`, `state_class=measurement`, `unit_of_measurement=kW`, and `_unrecorded_attributes = frozenset({"forecast"})` on the entity itself, so the Recorder no longer trips its 16 KB per-attribute limit on the tiered 96h `forecast` list (fixes [#55](https://github.com/code-imstillalive/nimbus/issues/55) point 1, [#59](https://github.com/code-imstillalive/nimbus/issues/59), [#61](https://github.com/code-imstillalive/nimbus/issues/61), [#62](https://github.com/code-imstillalive/nimbus/issues/62)).
- `solver_writer.ha_post_state()` grew a dispatch-table seam (`register_entity_handler` / `unregister_entity_handler`) that routes native-mode writes for migrated `entity_id`s through the entity's own `update_from_solver()`; unregistered `entity_id`s still fall through to the original `states.async_set` path and the standalone REST branch is unchanged.
- `pyproject.toml` with a `[dev]` extra: `pip install -e '.[dev]'` from a fresh clone now runs the full test suite ([#69](https://github.com/code-imstillalive/nimbus/pull/69), #36 Stage 1).
- CI: `Unit Tests (pytest)` is now a **strict** gate on every PR, sourced from `pyproject.toml`'s `[dev]` extra ([#73](https://github.com/code-imstillalive/nimbus/pull/73), #36 Stage 2).
- CI: `Version lockstep (integration <-> add-on)` job blocks merges when `custom_components/nimbus_load/manifest.json` and `nimbus_solver_app/config.yaml` versions drift ([#74](https://github.com/code-imstillalive/nimbus/pull/74), #36 Stage 4).
- `nimbus_solver_app` add-on directory is now genuinely self-contained ([#48](https://github.com/code-imstillalive/nimbus/pull/48)).
- `.pre-commit-config.yaml`: local pre-commit hooks pinning ruff at 0.6.0 to match CI ([#70](https://github.com/code-imstillalive/nimbus/pull/70)).

### Changed
- `nimbus_solver_app/config.yaml` version bumped `0.72.0` → `0.73.0` in lockstep with the integration.
- `nimbus_solver_app/config.yaml` version bumped from `0.1.0` to `0.72.0` to match the integration; both move in lockstep from now on ([#74](https://github.com/code-imstillalive/nimbus/pull/74)).
- Ran repo-wide `ruff format` (0.6.0) across `custom_components/` and `tests/`; ruff pinned to 0.6.0 in CI and pre-commit ([#70](https://github.com/code-imstillalive/nimbus/pull/70)).

### Fixed
- `manifest.json`: declared `after_dependencies: [energy]` so hassfest passes ([a5949ae](https://github.com/code-imstillalive/nimbus/commit/a5949ae)).
- `hacs.json`: pinned `homeassistant` min-version, dropped redundant `zip_release` ([#49](https://github.com/code-imstillalive/nimbus/pull/49)).
- Ruff F401: removed unused imports in `test_flows_pv_string_subentry.py` ([#71](https://github.com/code-imstillalive/nimbus/pull/71)).
- Test E402 noqa placement: moved onto the import statement's own start line ([fc29321](https://github.com/code-imstillalive/nimbus/commit/fc29321)).
- `manifest.json`: pinned `numpy>=1.24,<3` and `highspy>=1.15.1,<2` instead of an unpinned `highspy` requirement (Quality Scale Bronze `dependency-transparency`).
- `solver_runtime.py`: an `ImportError`/`ModuleNotFoundError` from a missing `highspy` wheel now fires a real, one-time `persistent_notification` and returns cleanly, instead of propagating out of the periodic solve timer as a bare, buried exception (Bronze `test-before-setup`).
- `strings.json`: every config-flow/options field now has a `data_description` (Bronze `data-description`); `translations/en.json` was found stale relative to it (still describing a removed 2-step switchboard flow) and re-synced.
- `sensor.py`: `_NimbusSolverPushSensor` (the Solver's own `battery_forecast`/`household_load_total_forecast` outputs) now goes `unavailable` if the Solver stops producing a fresh plan for more than 5 minutes, instead of showing a stale plan indefinitely — the same class of fix `NimbusForecastSensor` already had, extended to the entities that didn't have a coordinator to drive it automatically (Silver `entity-unavailable`).
- `custom_components/nimbus_load/quality_scale.yaml` added: every Bronze rule re-verified directly against current code rather than an older snapshot — 21/22 resolved, `brands` genuinely still open (tracked separately, [#80](https://github.com/code-imstillalive/nimbus/issues/80)).
- README refreshed for v0.73.0 — Forecaster, Solver (native in-process path), and Topology all documented against current behaviour ([#81](https://github.com/code-imstillalive/nimbus/pull/81), thanks @purcell-lab).

## [0.72.0] — 2026-08-23

### Added
- Topology card: explicit **Power Signal** role plus auto-detect for Grid and Battery sources ([e1abb32](https://github.com/code-imstillalive/nimbus/commit/e1abb32)).
- CI: `ruff format` + `ruff check` are now **strict** gates on every PR ([#68](https://github.com/code-imstillalive/nimbus/pull/68)).

### Changed
- Ran repo-wide `ruff format` pass. Documented the F401/E402 noqa fixes it uncovered ([#68](https://github.com/code-imstillalive/nimbus/pull/68)).

### Fixed
- `topology_map.yaml` framing: shipped as a per-household example rather than as a configuration template ([d654a18](https://github.com/code-imstillalive/nimbus/commit/d654a18)).

## [0.71.0] — 2026-08-23

### Added
- `diagnostics.py`: Solver health section with status, timing, and load-forecast errors surfaced to the diagnostics download ([5df77bc](https://github.com/code-imstillalive/nimbus/commit/5df77bc)).
- Battery Tower `subentry.title` now exposed via `NimbusTopologyConfigSensor` ([91e6816](https://github.com/code-imstillalive/nimbus/commit/91e6816)).

## [0.69.0] — 2026-08-23

### Added
- Switchboard wizard: suggests fields based on the household's existing Home Assistant Energy Dashboard configuration ([0f2955e](https://github.com/code-imstillalive/nimbus/commit/0f2955e)).
- Shadow Mode chart, Solver Operations and Counterfactual cards, and risk-aversion sliders shared with the community ([5d787e5](https://github.com/code-imstillalive/nimbus/commit/5d787e5)).

## [0.68.0] — 2026-08-23

### Added
- `NimbusTopologyConfigSensor`: bridges Power Source / PV String / Battery Tower subentries plus switchboard output into a single readable sensor ([0c06617](https://github.com/code-imstillalive/nimbus/commit/0c06617)).

## [0.67.0] — 2026-08-23

### Added
- Topology wizard: Power Source, PV String, and Battery Tower subentries plus a switchboard hub step ([f8341cd](https://github.com/code-imstillalive/nimbus/commit/f8341cd)).
- `tests/run_all.py`: complete test runner replacing scattered ad-hoc invocations ([a8495e8](https://github.com/code-imstillalive/nimbus/commit/a8495e8)).

## [0.66.0] — 2026-08-23

### Fixed
- Load-forecast sensor shape now validated at ingestion; EMHASS auto-detected ([#66](https://github.com/code-imstillalive/nimbus/issues/66), [2717255](https://github.com/code-imstillalive/nimbus/commit/2717255)).
- Topology card JS resynced (was two fixes stale); README now covers real deploy steps ([5c0f9e6](https://github.com/code-imstillalive/nimbus/commit/5c0f9e6)).

## [0.65.0] — 2026-08-22

### Fixed
- Two Solver wizard fields never exposed by the bridge sensor ([be17c7a](https://github.com/code-imstillalive/nimbus/commit/be17c7a)).
- `solver_writer`: preserved intentional `0` in numeric defaults; clamped initial SoC to `[min, max]` ([2309c65](https://github.com/code-imstillalive/nimbus/commit/2309c65)).
- `LOAD_FORECAST_ENTITIES` and `WHOLE_HOUSE_CROSS_CHECK` made config-driven ([#56](https://github.com/code-imstillalive/nimbus/issues/56), [#60](https://github.com/code-imstillalive/nimbus/issues/60), [a72260d](https://github.com/code-imstillalive/nimbus/commit/a72260d)).
- Blocking call removed from the event loop ([02cdae8](https://github.com/code-imstillalive/nimbus/commit/02cdae8)).

## [0.64.0] — 2026-08-22

First tagged release. Baseline for everything above.

---

## How to write a changelog entry

Sections use these labels (in order): **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**. Omit any that don't apply.

One line per user-visible change, present tense, human phrasing (not the commit subject verbatim). Link the relevant PR (`[#NN]`) or, when there is no PR, the commit hash (`[abcdef1]`). If an issue drove the change, prefer linking the issue over the commit.

New PRs add entries under `## [Unreleased]`. When a release PR lands, that PR renames `## [Unreleased]` to the new `## [X.Y.Z] — YYYY-MM-DD` heading and creates a fresh empty `## [Unreleased]` above it. See `.github/PULL_REQUEST_TEMPLATE.md` for the release-PR checklist.
