# Nimbus Solver — real-world integration reference

The rest of this repo (`solver/network.py`, `solver/elements.py`,
`solver/lp.py`, `ml/gbrt.py`, `ml/features.py`, `ml/model.py`,
`coordinator.py`, `flows/`) is the actual `nimbus_load` custom_component
— the LP Solver and ML Forecaster themselves. Pure Python, zero Home
Assistant dependencies, hardware-agnostic by design.

What that code doesn't show is **how it's actually wired into a real,
live Home Assistant install**. That wiring lives in a separate, much
bigger, private repo (`116KAT-HA-AI`) that also holds this household's
own security-system automations, personal dashboard config, and a very
long running session log — not something to open up wholesale just to
see how the Solver is deployed. This folder is a targeted export
instead: the real files that show the forecaster, the topology
dashboard, and the whole Solver setup in practice, pulled straight out
of that live repo.

These are reference copies, not a working standalone package — every
file below assumes it's running on that specific system, reading that
system's own real entity IDs. That's deliberate: the point is to show
the real, live wiring, not a sanitized toy example.

## Trying this on your own install

1. **Install Nimbus via HACS** (custom repository, this repo's URL),
   configure your Battery/Grid/Solar/Load Power Signals through the
   integration's own config-flow wizard — that's what makes each
   `sensor.nimbus_*_forecast` entity (and its `lower`/`upper` bands)
   exist in the first place. Nothing in this folder replaces that step.
2. **Static assets** (`topology-card-v4.js`) go in your own `www/`
   folder, registered once as a Lovelace resource
   (Settings → Dashboards → ⋮ → Resources). No restart needed for
   changes to this file after the first add — it's a plain static JS
   file, HA just serves it.
3. **The `lovelace_*.py` scripts** are meant to run once, from inside
   your own `homeassistant` container, against your own dashboard's
   `.storage/lovelace.*` file — e.g.
   `docker cp lovelace_build_merged_forecast_chart.py
   <container>:/tmp/ && docker exec <container> python3
   /tmp/lovelace_build_merged_forecast_chart.py && docker restart
   <container>`. Each one is self-locating (finds its own target view/
   card by title) and safe to re-run. Read each script's own top-of-
   file constants first — a couple (like `lovelace_build_topology_
   dashboard.py`'s `topology_map.yaml`) need your own real entity IDs
   filled in before running; most (like the merged forecast chart)
   need nothing at all, they discover everything live.
4. **What you should actually see once it's running**: the merged
   forecast chart (below) is the fastest way to confirm the Forecaster
   itself is healthy — a real point-estimate line per configured Power
   Signal, with dashed confidence-band lines bracketing it. A flat
   point-estimate line with no real variation, or missing bound lines
   entirely, means something upstream (the sensor's own `forecast`
   attribute) isn't populating correctly yet — check `sensor.nimbus_
   solver_config`/your Power Signal entities directly before assuming
   the chart itself is wrong.

## `files/nimbus_solver_forecast_writer.py` — the whole Solver setup

This is the actual glue: a plain host cron script (runs every minute,
no HA restart ever needed for a change here) that reads live sensors,
builds `GridConfig`/`BatteryConfig`/`SolarConfig`/`LoadConfig` from
them, calls this repo's own `network.build_plan()`, and pushes the
result to `sensor.nimbus_solver_battery_forecast`. Nimbus itself never
writes to Modbus or touches the battery — this stays purely
observational/shadow-mode.

Two things worth knowing before reading it:

- It reads its economic settings (risk aversion, P2P bonus, battery
  capacity, etc.) live from `sensor.nimbus_solver_config`, itself
  sourced from the `number.nimbus_solver_*` dashboard entities you can
  see in `custom_components/nimbus_load/number.py` — so a household
  can tune these from a dashboard slider, not just the config-flow
  wizard.
- Grep for `has_localvolts` — every branch gated on it is genuinely
  this-household-specific (real LocalVolts P2P pricing, a real bill-
  verified TOU network tariff table, a real 5pm/midnight/7am cost
  schedule). The `else:` fallback branches next to each one are the
  portable path: a household's own configured import/export price
  sensor, held flat, no assumptions about retailer/region/tariff
  structure. That's the split to look at for "what would this need to
  look like for someone who isn't on this specific setup."

## `files/topology-card-v4.js` + `files/topology_map.yaml` + `files/lovelace_build_topology_dashboard.py`

A from-scratch, vanilla-JS custom Lovelace card (`switchboard-topology-
card`, no framework) that renders a live SVG power-flow diagram —
Switchboard bus, Inverters, PV strings, battery towers, Grid, and every
Nimbus Load, with live proportional color-mixing showing what's
actually supplying the bus right now.

**The card's own JS has zero hardcoded device-entity references —
every `sensor.*` it displays comes from configuration, never a
built-in assumption.** Two sources feed that configuration, and the
recommended one needs no file edits at all:

1. **Recommended: Nimbus's own hub -> Configure -> "Solver settings" ->
   Topology wizard** (Power Source / PV String / Battery Tower /
   Switchboard steps — real HA UI forms with real entity pickers for
   *your* system). The moment even one Power Source subentry exists,
   the card's own `_discoverTopologyConfig()` reads it live from
   `sensor.nimbus_topology_config` on every render and wholesale-
   overrides everything below — no file to edit, no redeploy, no
   restart.
2. **Fallback: `topology_map.yaml`** — a plain data file, only used
   before the wizard's ever been run. **This copy in the repo is
   116KAT's own real hardware's literal entity IDs — read its own
   header comment before touching it, do not copy it verbatim
   expecting it to work on your system.** Use the wizard instead;
   this file exists as a worked example of the shape, and as
   `lovelace_build_topology_dashboard.py`'s initial-generate input for
   anyone who'd genuinely rather hand-edit YAML than use a config-flow
   UI.

**Loads are the one thing never in either source above** — every
Nimbus load subentry (HWS, pool, any circuit breaker) auto-publishes
its own forecast sensor, and the card discovers all of them directly
from live `hass.states` on every render, fully independent of both the
wizard and the static file. See `topology-card-v4.js`'s own
`_discoverLoads()` for the mechanism (a deterministic entity_id
transform, not a guess).

`lovelace_build_topology_dashboard.py` is the generator that turns
`topology_map.yaml` into the dashboard view's *initial* card config —
only relevant if you're going the fallback/hand-edit route above.

**2026-08-22 update, worth knowing if your own inverter has a real DC
power sensor:** each inverter's header used to show only its signed
battery-flow sensor, which made a fully-charged, solar-producing
inverter (battery legitimately idle at 0W) look falsely dead — real
solar was flowing straight through to the switchboard and nothing on
the card said so. Fixed by adding an optional `dc_power:` field per
inverter in `topology_map.yaml` (a real per-inverter total-DC-power
sensor, genuinely different from battery flow — it's nonzero whenever
that inverter's own PV strings are producing, regardless of what the
battery is doing) and showing it as the primary header reading, with
battery flow appended only when actually nonzero. `dc_power:` is
optional — omit it and the header falls back to battery-flow-only,
same as before.

## `files/lovelace_add_nimbus_solver_view.py` — the Solver dashboard scaffold

Builds the actual "Solver" view (`type: sections`) that the forecast
table, quality card, and merged chart below all get added into. Self-
locating and idempotent (finds the dashboard by title, skips cleanly if
the view already exists) — same pattern as every lovelace-editing
script in this project. Run this first if you're building the Solver
dashboard from scratch.

## `files/lovelace_add_nimbus_solver_forecast_table.py` — the forecast table card

A markdown card iterating `sensor.nimbus_solver_battery_forecast`'s own
`forecast` array into a real table: Time / Buy¢ / Fees¢ / Sell¢ / P2P¢ /
Load / Solar / Batt / SoC% / Net$, one row per period. The Fees¢ column
(network TOU + certificates, split out from the raw commodity price) is
genuinely household-specific rate data — see `number.nimbus_solver_
network_fee_*`/`flat_fee_rate` (this repo's own `const.py`/`number.py`)
for the live, dashboard-configurable fields that drive it; a fresh
install with those left at their 0.0 defaults gets a Fees¢ column that
correctly reads 0 throughout, not a crash.

## `files/lovelace_add_nimbus_solver_quality_card.py` — the EPR / regret quality card

Renders `sensor.nimbus_solver_quality_report`'s own pushed attributes
(EPR, regret in dollars, tracking fidelity, real settled P2P dollars)
as a real dashboard card, plus the day-over-day history trend. Purely a
display layer over whatever `nimbus_solver_quality_writer.py` (below)
computes — genuinely portable on its own, since it just reads whatever
that sensor happens to contain.

## `files/lovelace_add_nimbus_shadow_mode_chart.py` — Nimbus vs your real controller vs measured reality

One chart, history AND forecast, for three things at once: Nimbus's own
plan, your real controller's own plan (this household compares against
HAEO — see the file's own header for exactly which two entities are
household-specific and what to swap them for), and the real measured
battery power. This is the actual "is Nimbus's shadow-mode plan any
good" answer over time — not a single current-forecast number, a real
trend you can watch.

**Read the file's own docstring before deploying** — two of the three
series are genuinely household-specific (this household's own HAEO
plan sensor and its own real battery-power sensor name). The one
portable entity every install shares is `sensor.nimbus_solver_battery_
forecast`; swap the other two for your own equivalents, or delete the
"HAEO plan" series pair entirely for a clean 2-way Nimbus-vs-Real chart
if you don't run a comparable second controller at all.

## `files/lovelace_add_nimbus_solver_operations_card.py` — live solve status, plan economics, EPR snapshot, counterfactual readiness

Two markdown cards, fully portable (every entity is a standard Nimbus-
published sensor, present on any install with the same name):

- **Operations**: current solve status/time/periods, tonight's plan
  economics (total cost, P2P match fraction), the latest EPR quality
  score, and (if you run it) the P2P nightly-volume threshold.
- **Counterfactual (Stage 1)**: the real, live version of "would
  Nimbus alone have been ready for tonight's delivery window" —
  reads `sensor.nimbus_counterfactual_soc_5pm` (see `nimbus_
  counterfactual_writer.py` above), including the real day-over-day
  trend table, not just today's snapshot.

## `files/lovelace_add_nimbus_risk_aversion_sliders.py` — the risk-hedging dials, live-adjustable from the dashboard

Three `mushroom-number-card` sliders (`risk_aversion`, `import_price_
risk_aversion`, `export_price_risk_aversion` — real `number.py`
entities, 0.0–1.0, live-adjustable with zero restart) plus a legend
card explaining what each one actually does. Needs two HACS frontend
cards this repo doesn't provide (`mushroom`, `card-mod` — see the
file's own header for exactly which ones) — both widely used,
independently maintained.

## `files/lovelace_build_merged_forecast_chart.py` — the combined Power Signal + Load chart, INCLUDING confidence bands

One apexcharts-card showing every Nimbus Power Signal (Battery/Grid/
Solar/Whole House, header tiles) and every Nimbus Load (small legend
entries, live-discovered from `hass.states` the same way the topology
card discovers loads — see that card's own `_discoverLoads()`) on one
chart, history and forecast both. Colors are hash-derived from each
entity_id by default (see this file's own `_hash_color_for()`) so a
different household's own real load list gets sensible, distinct
colors automatically, no manual palette needed.

**This is where the Forecaster's confidence bands actually show up.**
Every Power Signal's own `forecast[*].lower`/`.upper` attributes (the
real GBRT-quantile or split-conformal bands `ml/model.py` computes,
same repo) get plotted as dashed "(lower bound)"/"(upper bound)" lines
right alongside its point-estimate line — not a separate card, not a
separate feature to wire in. If you deploy this chart against your own
configured Power Signals and see two dashed lines bracketing each solid
one, that's confirmation the Forecaster's own uncertainty estimation is
working end-to-end on your install, not just the point forecast.

## `files/nimbus_counterfactual_writer.py` — Stage 1: "would Nimbus alone have been ready tonight"

Runs once daily, replays the PREVIOUS day using the real production
`network.build_plan()` (same solver, same code path as the live
forecast writer) with SoC evolved PURELY from Nimbus's own decisions —
never reading the real, externally-influenced SoC at any step. This is
what actually answers "if Nimbus alone had been deciding since
midnight, would the battery still have been ready for tonight's
delivery window" — a real, evidence-based readiness signal, not a
guess. See this repo's own `custom_components/nimbus_load/solver/
network.py` — nothing here is special-cased for this household, the
counterfactual re-derivation itself is generic; only the specific
sensors it reads (battery SoC, load, solar, price history) are wired
to this household's real entity IDs.

## `files/nimbus_solver_quality_writer.py` — the regret / EPR scorer (NOT directly portable)

Unlike every other file in this folder, this one is genuinely NOT
meant to be copied as-is — it reads this household's own real
LocalVolts P2P settlement API directly (`secrets.yaml` credentials,
`sensor.lv_v2_p2p_confirmed_history`), this household's own real
automation entity names (`input_number.p2p_grid_export_target_kw`,
`config/automations.yaml`'s own P2P/self-consume automations), and a
real, bill-verified Energex TOU tariff table. Included anyway, same
"show the real wiring, not a sanitized example" philosophy as
`nimbus_solver_forecast_writer.py` above -- specifically because of a
real, general, worth-reusing FINDING made fixing it (2026-08-22, direct
Mark Purcell question: "why is it missing so much of the mark?"):

**If you build your own regret/EPR scorer against a perfect-foresight
oracle, make sure the oracle is held to the SAME real-world
constraints your actual controller operates under** -- any fixed-rate
delivery commitment, any hard self-consume/blackout window, anything
the real system can't deviate from even with perfect price knowledge.
This household's own oracle was silently free of two such constraints
(a fixed P2P export rate, a hard midnight-to-4am self-consume lock) --
fixing both dropped a $16.44 regret reading to something meaningfully
smaller and genuinely trustworthy. Search this file for `fixed_export_
kw`/`SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE` for the real mechanism;
`solver/regret.py`'s own `oracle_dispatch()` (this repo) is the
underlying, fully generic piece -- it takes whatever `GridConfig` you
give it, so applying the same real constraint on your own install is a
config change here, not a new mechanism to build.

## `files/research/*.py` — the Solver audit scripts

These are the scripts used to work through (and mostly close) a real,
external 9-item Solver audit — 8 of 9 items closed as of this export.
All read-only against live HA history via the REST API; none write
anything. Worth reading in roughly this order if you want the story:

- `measurement_integrity_check.py` — audit item #1. Reconstructs real
  exported energy from raw meter history and checks it against
  independently-settled LocalVolts figures, under a strict tolerance.
- `objective_completeness_ablation_test.py` — audit item #2. Zeroes
  each real cost/value term in the LP one at a time and confirms it
  actually has a measurable effect — catches a silently-inert term.
- `contract_risk_injection_test.py` — audit item #4. Re-prices the
  Solver's own real plan under reduced P2P-match scenarios (100/75/
  50/25/0%) to quantify real financial exposure if a night's P2P match
  comes in below what was assumed.
- `hard_service_constraint_stress_test.py` — audit item #5. Sweeps a
  hypothetical hard-deadline load's cost under 1x/2x/5x/10x price to
  confirm the deadline holds regardless of price, and that a genuinely
  infeasible target fails honestly rather than silently.
- `forward_value_comparison.py` — audit item #7. Compares a flat
  terminal battery value against a piecewise-linear concave one on
  real recent data — the concave version is what's actually shipped
  now (`BatteryConfig.terminal_value_breakpoints`, this same repo).
- `forecast_capture.py` / `forecast_accuracy_compare.py` /
  `forecast_regret_decomposition.py` — audit item #9 (forecasting,
  deliberately last priority). Capture-then-compare mechanism for
  measuring real forecast error, plus a first attempt at decomposing
  total regret into forecasting's own share vs. optimisation/control's
  share.

## Where to actually look for the things you flagged

- Risk aversion / price risk aversion, and the import/export split —
  `custom_components/nimbus_load/solver/network.py`'s `build_plan()`
  own docstring (same repo), wired live via this writer's
  `risk_aversion` / `import_price_risk_aversion` /
  `export_price_risk_aversion` reads.
- Battery throughput/degradation exposure — search this writer for
  `equivalent_full_cycles`.
- Dual/shadow-price extraction — `solver/network.py`'s `LPResult.duals`
  (same repo), consumed here via `binding_constraint_now` /
  `energy_shadow_price_now` / `p2p_volume_cap_shadow_price`.
