# Nimbus

*Just a different type of cloud.*

> ⚠️ **Work in progress — active shadow-mode project, not a finished product.**
> Both the Forecaster and Solver are under real ongoing development. Neither drives any
> live battery/grid dispatch today — the Solver runs entirely in observe-only shadow mode
> against real household data, and stays that way until the reference-household evidence
> bar is cleared. Expect rough edges, breaking changes, and real bugs — several have
> been found and fixed in the days right around this repo going public. If you install
> this, please open a GitHub issue rather than expect a polished, plug-and-play experience.

**Current version: `0.73.1`** — see [`CHANGELOG.md`](CHANGELOG.md) for the release history.

Nimbus is a single Home Assistant HACS integration that ships two cooperating pieces:

1. **A self-retraining ML load Forecaster** — watches your own power sensors, learns your
   real consumption pattern (time-of-day, day-of-week, season, weather, recent lags),
   and publishes rolling per-load and whole-house forecasts. Zero manual retraining,
   zero config-file editing, no shell/cron/systemd access needed.

2. **An LP-based battery/grid dispatch Solver** — a real linear-programming optimiser
   (HiGHS-backed) that plans imports, exports, battery charge/discharge, and (optionally)
   sheddable-load timing over a rolling multi-day horizon, given price forecasts, PV
   forecasts, and the Forecaster's own load forecast. Full-featured: time-varying
   network fees, two-tier P2P export bonuses, salvage terminal value, per-throughput
   degradation cost, and price-uncertainty risk aversion (CVaR-style).

Both pieces are pure-Python, run in-process inside Home Assistant, and are portable to
HA OS, Supervised, and Docker installs. The Solver requires a 64-bit host (`amd64` /
`aarch64`) because of `highspy`; the Forecaster runs anywhere numpy runs.

## Why

Most load forecasters in the Home Assistant energy-optimization world are either purely
weather-correlated (no real learning from your own house) or bundled inside a much
larger, harder-to-adopt optimizer. Most battery optimisers are either closed-source
cloud-hosted (Amber Shifty, ChargeHQ, Emberpulse, Reposit) or require a separate
long-running Python process outside HA (EMHASS). Nimbus is the first open-source
"forecast + optimise + monitor" stack that fits inside HA as one HACS integration,
runs entirely on your own hardware, and is honestly instrumented enough for you to
know whether it's actually helping.

**What Nimbus gets you:**

- **Realistic load forecasting** — learns from *your* household history, not a
  weather-only proxy. Two model families (k-NN + gradient-boosted regression trees)
  are trained and validated on chronologically-split held-out data each night; whichever
  performs better on *that* load wins that day.
- **A working LP dispatch plan** — you get a real 96-hour battery/grid plan updated
  every minute, published as normal HA sensors so any dashboard, automation, or third-
  party MPC layer can consume it. No black-box "trust us" — the plan's SoC trajectory,
  net cost, binding constraints, and shadow prices are all exposed as sensor attributes.
- **Honest performance measurement** — regret, Economic Performance Ratio (EPR), and
  three counterfactual controllers (no-control, threshold-rule, oracle-with-perfect-
  foresight) are built into the solver package. You can measure the fraction of the
  naive-to-oracle economic gap Nimbus actually closes, on your own data, over any
  window you choose.
- **A plain `{time, value}` forecast shape** — so if you'd rather run your own
  optimiser (HAEO, EMHASS, a custom Python script), Nimbus's Forecaster feeds it
  straight in as a load-forecast source.

## Install (HACS)

1. HACS → the three-dot menu → **Custom repositories**
2. Add `https://github.com/code-imstillalive/nimbus`, category **Integration**
3. Install **Nimbus**, restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → search "Nimbus" — this
   creates the hub, no fields to fill in
5. On the Nimbus hub, click **Configure** to walk the wizard:
   - **Forecaster settings** — temperature sensor (optional, improves accuracy),
     temperature-forecast sensor, forecast horizon, retrain hour, training window.
     Every field has a sensible default.
   - **Solver settings** — a 3-step sub-wizard (Battery → Grid → Sources) that
     points the Solver at your SoC sensor, price sensors, PV forecast, and load
     forecast. See "Running the Solver" below.
   - **Switchboard** — describes your household's real per-circuit topology so
     the topology dashboard card can render it. Pre-populated from Home Assistant's
     own Energy Dashboard config when possible.
6. Click **+ Add** on the hub's device page to add a **Load** (a power sensor Nimbus
   should learn from and forecast), a **Power Signal** (a raw pass-through signal
   for topology mapping), a **Power Source** (an inverter/BMS unit for topology),
   a **PV String**, or a **Battery Tower**. Repeat as many as you need — the
   reference household has 18 circuit-breaker loads plus 2 inverters and 4 battery
   towers, no restart or repeat wizard needed.

**A naming quirk worth knowing** ([#43](https://github.com/code-imstillalive/nimbus/issues/43)):
every entity this integration creates carries the internal domain `nimbus_load` —
e.g. `sensor.nimbus_load_solver_config`, `number.nimbus_load_solver_grid_max_export` —
not `nimbus`, even though you search for and install "Nimbus." This is a historical
accident from before the Solver existed (the integration used to be load-forecasting
only) and changing it now would break every existing user's entity IDs, long-term
statistics, and automations — not something to do lightly. Read `nimbus_load` and
`Nimbus` as the same thing; the domain name doesn't reflect current scope, and
there's no plan to silently migrate it.

## Understanding the configuration model

Everything lives under one Nimbus hub (one "Add Integration"), but the model has
genuinely grown — 5 subentry types plus a 3-way Configure menu — and it splits into
two completely separate concerns sharing that one hub:

1. **Forecasting** — `Load` and `Power Signal` subentries. Both feed the same
   k-NN/GBRT ML engine (`ml/model.py`, `coordinator.py`) and both produce a real
   `sensor.nimbus_<x>_forecast` entity with training/validation.
2. **Topology/wiring** — `Power Source`, `PV String`, `Battery Tower` subentries.
   Pure metadata for the dashboard's topology diagram card. **No ML model, no
   coordinator, no forecast sensor.** These exist purely so the diagram knows what's
   physically wired to what.

If a subentry doesn't produce a `_forecast` sensor, it's topology metadata, not a
forecast target. That single fact resolves most of the "why doesn't X have a
forecast line" confusion.

### 1. Load subentry

One real appliance/circuit — a pool pump, a hot water system, an EV charger, an AC
zone. Deliberately **one required field** (the source power sensor); everything else
that used to be per-load (temperature, retrain hour, training window) moved to the
hub's shared Forecaster settings, since it's the same answer for every load in the
same house.

Two genuinely optional extras, only useful for a load on a real fixed daily timer:
- `schedule_start_hour` / `schedule_end_hour` — lets the model learn a sharp on/off
  boundary directly instead of approximating it through hour-of-day sin/cos features.
  Blank = no-op.
- `expected_load_kw` — a "deterministic mode" hint for a load whose draw is
  basically constant whenever it's on (a resistive heater, say) rather than
  something worth training a full model against.

Title is auto-derived from the source sensor's own `friendly_name`.

### 2. Power Signal subentry

Same ML engine as Load, but forecasting a whole-system quantity — Battery, Solar,
Grid, or "other" — as its own genuine forecast target, not just as context for a
load's model. **No schedule/expected-load fields at all** — a system-level power
signal doesn't run on a timer the way an appliance does.

Two fields: the source sensor (required), and `signal_role` — an explicit dropdown
(Battery / Solar / Grid / Other), **deliberately not inferred from the entity's
name**. A real check against a live install found the actual Battery sensor named
"Logger Battery power" (would match a naive keyword guess), but the real Solar
sensor was "Combined Total DC Power" (no "solar" anywhere) and the real Grid sensor
was "Logger Meter total active power" (no "grid" anywhere). Naming alone can't
reliably tell these apart on anyone's real hardware, so it's a one-time explicit
choice instead. The topology card auto-wires its own Grid/Battery flow lines
straight from whichever Power Signal carries that role — zero extra config needed.

### 3. Power Source subentry

Pure topology metadata — one real physical hardware unit that connects to the
switchboard: an inverter, a hybrid battery/inverter, or a battery-only BMS.
Deliberately called "Power Source," not "Inverter" — a real household might have a
battery-only unit, or PV wired through something that isn't a battery inverter at
all.

Fields: a name (required), plus two **optional** sensors — total battery power, and
total DC/PV throughput. Both optional because a PV-only unit has no battery power to
report, and vice versa.

### 4. PV String subentry

One real physical PV string/array. One required field (its own live power sensor),
one optional free-text label ("West array", "MPPT2" — deliberately not a structured
MPPT-number field, since that's Sungrow-specific, not universal), and an **optional**
link to a Power Source.

That link is optional on purpose — it came directly out of testing against real,
different hardware (a SigenStor battery/inverter plus a wholly separate third-party
SolarEdge PV system). The SolarEdge strings aren't wired through the Sigen inverter
at all — they're an independent source feeding the switchboard on their own. Leaving
the Power Source field blank renders that string as its own independent branch on
the diagram, rather than forcing it under a Power Source that doesn't actually own
it.

### 5. Battery Tower subentry

One real physical battery pack. Four sensor fields, all optional (SoC, SoH, Voltage,
Temperature — the only four the topology card's own rendering function actually
displays), plus the same optional Power Source link as PV String, same reasoning.

SoC is the one field worth filling in first if you only do one — it drives the
visible fill-bar on the diagram — but the form won't block you from submitting with
nothing filled in yet.

### The hub wizard — how you actually reach all of this

Two separate buttons on the Nimbus hub's device page, doing two separate things:

**"+ Add"** → a menu of the 5 subentry types above. Pick one, fill in its (short)
form, submit. No restart, repeat as many times as needed — this is what makes
adding 18 circuit breakers, or several PV strings/towers, fast.

**"Configure"** → a 3-way menu of shared, hub-level settings that apply across
everything:

- **Forecaster settings** — the ML input features every Load/Power Signal model can
  use for context: temperature, a temperature forecast sensor, humidity, a
  curtailment sensor, plus real measured battery/grid/solar power sensors.
  **Important gotcha**: these battery/grid/solar sensors are a *different concept*
  from a Power Signal subentry — they're not forecast targets, they're context
  features so a load's model can tell "was the battery charging at this exact
  moment" apart from genuine load-driven signal. All independent and optional. Also:
  forecast horizon, retrain hour, training window (days of history).
- **Solver settings** — the 3-step wizard (Battery → Grid → Sources) described
  below, pointing the LP dispatch optimizer at its own SoC sensor, import/export
  price sensors, and solar/load forecast sources. The actual numeric settings
  (capacity, max charge/discharge, efficiency, cost/salvage values) live as real
  dashboard-editable `number.*` entities, not in this wizard — so they can be tuned
  live without reopening Configure.
- **Switchboard** — everything the topology card can show beyond what it
  auto-detects: import/export price sensors, a switchboard-level battery power
  sensor (deliberately separate from the Solver's own SoC sensor — different unit,
  different purpose), and the 6 daily-kWh headline stats. All optional; a blank form
  is a valid config. This form also auto-suggests entities pulled from Home
  Assistant's own Energy Dashboard config wherever a field is still unset — filtered
  to real `device_class: energy` sensors first, and only ever shown as an editable,
  pre-filled suggestion a human still has to confirm, never silently applied.

### The recurring gotcha, spelled out directly

"Battery" and "Grid" show up in **four genuinely different places** with different
meanings:

| Where | What it actually is |
|---|---|
| Forecaster settings → `battery_sensor`/`grid_sensor`/`solar_sensor` | Real measured power, used only as ML **context features** for Load/Power Signal models |
| Power Signal subentry with `signal_role=battery` | The battery's own power, forecasted as a genuine ML **output target** |
| Solver settings → battery SoC sensor | The **LP optimizer's own input** — a %, not a power |
| Switchboard → battery power sensor | The **topology card's** own signed kW reading, for diagram coloring |
| Battery Tower subentry → SoC/SoH/Voltage/Temp | **Per-physical-pack** topology metadata, no forecasting involved at all |

None of these are wrong or redundant — they're genuinely serving different
subsystems (ML context, ML forecast target, LP optimizer, dashboard diagram,
per-hardware-unit display) — but nothing else currently explains that they're
different, which is exactly the kind of thing that reads as confusing/broken on a
fresh install.

### Two more real gotchas: which load-forecast field actually wins, and the aggregator trap

Solver settings has **two** fields that both sound like "my household load":

| Field | What it actually does |
|---|---|
| **Household load forecast sensor** (`solver_load_forecast_sensor`) | A single entity's own forecast, used as-is |
| **Optional: individual circuit forecast sensors to sum instead** (`solver_load_forecast_entities`) | Sums N entities together — **wins outright over the field above the instant it has even one entry**, regardless of what's configured there |

Found live (issue [#111](https://github.com/code-imstillalive/nimbus/issues/111)): if you've ever pointed the "individual circuits" field at a single third-party forecast sensor while experimenting, it silently keeps winning even after you change the single-sensor field to something else — there's no warning, no error, the Solver just quietly keeps reading whichever field is non-empty. **If you only want one forecast source, leave "individual circuit sensors" completely blank.**

The second trap is sharper: **never point "Household load forecast sensor" at `sensor.nimbus_household_load_total_forecast`** (or any other Nimbus aggregator sensor) — that entity **is** the thing this field feeds *into*, not a valid source for it. With the "individual circuits" list empty, the aggregator has nothing to sum, so it publishes a real, structurally-valid series that's near-all-zero except a single live "now" reading — a genuine confident-looking plan built on the belief nobody in the house consumes anything (real reported impact, issue [#118](https://github.com/code-imstillalive/nimbus/issues/118): a $46/day misplan with every health-check field reporting green). This specific case is now caught automatically (a load forecast under 10% non-trivially-nonzero is rejected with a message naming this exact mistake) — but picking the right entity in the first place (a real `sensor.nimbus_<your_load_signal>_forecast`, from a Load or Power Signal subentry you created yourself) avoids hitting that guard at all.

## Running the Solver

**Running the Solver settings wizard is mandatory, not optional, if you want the
Solver at all.** Every `number.nimbus_solver_*` entity (battery capacity, max
charge/discharge, grid limits, costs, risk aversion, network fees, salvage value,
efficiency) starts at a defensive placeholder minimum. A persistent notification
fires the moment the hub is created pointing you at **Configure → Solver settings**;
if you dismiss it, edit the `number.nimbus_solver_*` entities directly instead.
Confirm `sensor.nimbus_solver_config` reads `configured` in Developer Tools → States
before expecting a real plan.

**If you only want load forecasting, skip this whole section.** The Forecaster works
standalone with zero further Solver setup.

The Solver runs natively in-process on a 1-minute timer as soon as `highspy` (the
compiled LP solver, an automatic `manifest.json` requirement, prebuilt wheels for
amd64/aarch64 only) finishes installing. Every solve is a pure function — real
forecast/price inputs in, a `Plan` dataclass out — and writes its result to two
sensor entities:

- `sensor.nimbus_solver_battery_forecast` — 96-hour battery power/SoC plan, plus
  the solved `total_cost`, `equivalent_full_cycles`, `binding_constraint_now`,
  shadow prices, and every planning-horizon interval as an attribute.
- `sensor.nimbus_household_load_total_forecast` — the whole-house load forecast
  actually consumed by the Solver, plus a `whole_house_cross_check_now_kw` field
  that compares the summed 18-circuit forecast against a single independent
  whole-house meter for real-time integrity.

Both are real `SensorEntity` classes attached to the Nimbus hub device, with the
`forecast` list excluded from the Recorder (`_unrecorded_attributes`) so long-term
statistics keep working without tripping the 16 KB per-attribute limit.

### Legacy standalone-script path

One older path remains fully supported for the one case the native path genuinely
can't cover — you'd rather run the Solver on a separate always-on device than
inside HA's own process. The standalone script (`nimbus_solver_forecast_writer.py`)
lives in `docs/real-world-integration/`; both paths run byte-identical solve logic.

**Deprecated (v0.73.0):** the `nimbus_solver_app` Supervisor add-on will be removed
in v1.0.0 — the native in-process path above covers every architecture the add-on
covered, with no separate container, no version-lockstep discipline, and no
three-way copy sync. Existing installs: uninstall the add-on and finish the
integration's Solver wizard; the native path takes over the same
`sensor.nimbus_solver_*` outputs with no config migration. Tracking:
[#76](https://github.com/code-imstillalive/nimbus/issues/76).

## What Nimbus publishes

### Per-load sensors (one per Load subentry)

- `native_value` — current predicted load in kW.
- `forecast` attribute — a plain `{time, value, lower, upper}` list, generic by
  design so it can drop into HAEO, EMHASS, or any other optimiser as a load-forecast
  source, or be graphed with ApexCharts / plotly / lovelace-plotly.

### Whole-house rollup

- `sensor.nimbus_household_load_total_forecast` — the sum of every configured
  Load's forecast, plus the whole-house cross-check field. This is what the Solver
  should be pointed at as its load-forecast source (Configure → Solver settings →
  Sources).

### Solver plan

- `sensor.nimbus_solver_battery_forecast` — full battery/grid dispatch plan.
  Attributes include `forecast` (per-interval `battery_kw`, `soc_pct`,
  `grid_import_kw`, `grid_export_kw`, `import_price`, `export_price`, `load_kw`,
  `solar_kw`, `net_cost`), `status`, `total_cost`, `total_cost_with_fixed_costs`,
  `equivalent_full_cycles`, `total_throughput_kwh`, `n_clamped_periods`,
  `binding_constraint_now`, and both shadow prices.
- `sensor.nimbus_solver_config` — a live mirror of every Solver setting the wizard
  captured, for one-glance sanity checks in Developer Tools → States.
- `sensor.nimbus_topology_config` — a bridge of every Power Source / PV String /
  Battery Tower subentry plus the switchboard output, driving the topology
  dashboard card.

### Live tuning knobs (all editable from the dashboard)

Every plain numeric Solver setting is its own `number.nimbus_solver_*` entity —
edit inline on the dashboard without touching the wizard:

- Battery: `capacity_kwh`, `soh_percent`, `min_soc_percent`, `max_soc_percent`,
  `max_charge_kw`, `max_discharge_kw`, `efficiency_percent`.
- Grid: `max_import_kw`, `max_export_kw`, `flat_fee_rate`, three
  `network_fee_*` blocks + `network_fee_default_rate`.
- Economics: `charge_cost`, `discharge_cost`, `degradation_cost_per_kwh`,
  `salvage_value`.
- Risk: `risk_aversion`, `import_price_risk_aversion`,
  `export_price_risk_aversion`.
- Sources: `auto_include_known_solar` switch, plus an optional multi-entity
  load-forecast list for granular per-circuit summation.

See [`docs/configuration-reference.md`](docs/configuration-reference.md) for
every field across every wizard step and subentry type, plus the full
default/range/unit table for every `number.nimbus_solver_*` entity above.

## How it works

### Forecaster

- Two pure-numpy model families, both compiled from scratch to avoid the fragility
  of installing scikit-learn/XGBoost/LightGBM inside HA's container (no C compiler
  present):
  - **k-NN** — a lazy learner that finds past moments that "look like" the current
    one (time-of-day, day-of-week, month, temperature, recent lags) and averages
    what the load actually was then.
  - **GBRT** — a from-scratch gradient-boosted regression tree ensemble, the same
    algorithm XGBoost/LightGBM implement, without the compiled speed advantage
    (not needed at this data scale).
- Every retrain (once a day, configurable, defaults to 3am local) chronologically
  splits held-out data, validates both models, and picks whichever performs better
  for *that specific load*.
- **Lag features** are included — "what was this load doing LAG_SHORT / LAG_LONG
  grid-steps ago" — and turned out to be among the most important inputs on every
  load tested in the reference household's 30-day backtest.
- At forecast time, beyond the first couple of steps, no real "future" lag value
  exists yet: `predict()` recursively feeds each step's own prediction back in as
  the lag for the next step (standard direct-recursive forecasting).
- Everything (training + prediction) is offloaded to an executor thread so it
  never blocks Home Assistant's event loop.
- Trains directly from Home Assistant's own recorder history — no external API
  calls, no credentials to manage.
- Confidence bands: `predict()` returns calibrated `lower`/`upper` percentiles
  from the GBRT residuals so the forecast can be shown as a fan chart, not just
  a point estimate.
- A cross-source blend (`ml/blend.py`) can weight multiple forecast inputs by
  their inverse MAE for a robust ensemble.

### Solver

- **LP-based, HiGHS-backed.** `network.py`'s `build_plan()` is a pure function:
  element configs + a time horizon in, a `Plan` dataclass out, zero HA imports
  and zero side effects.
- **Elements modelled:** `Grid` (with time-varying network fees + P2P bonus),
  `Battery` (single-aggregate today; per-tower is on the roadmap), `Solar`,
  `Load`, `SheddableLoad` (fully implemented from day one even though the
  reference household has zero configured — the LP scaffolding is there).
- **Stability mechanisms** (`network.py`, extended 2026-08-20): proximal
  regularisation against the previous plan, per-interval rate limiting, and
  confidence-aware dispatch. A rolling re-solve where two near-tied optima flip
  arbitrarily was the exact pathology HAEO's "flash" replan spikes exhibited, so
  Nimbus refuses to repeat it structurally.
- **Structural degeneracy guards** — `BatteryConfig` and `GridConfig` refuse to
  construct if `charge_cost + discharge_cost` falls below the wash-trade spread
  threshold, the exact zero-friction shape that produced HAEO's wash-trade
  degeneracy. Not a warning; a `DegenerateConfigError`.
- **Rolling refinement** (`rolling.py`, Layer 2): standard receding-horizon
  control — solve, act, observe, re-solve — with the previous plan threaded
  through automatically so the stability mechanisms above have something real
  to stabilise against.
- **Two-stage stochastic LP** (`stochastic.py`, opt-in, Track A2): a genuine
  two-stage program with stage-1 decisions shared across every scenario and
  stage-2 variables scenario-indexed, for real hedging against solar
  uncertainty. Deliberately a separate module from `network.py`.
- **Counterfactuals** (`counterfactuals.py`, `regret.py`, `epr.py`): no-control,
  a tuned two-threshold price rule with no forecasting, and an oracle with
  perfect foresight. `quality_report.py` ties these plus tracking (measured
  vs. commanded) into a single "how good is the current dispatch, right now"
  live report.

### Both

- Persist across restarts. The Forecaster saves trained models to
  `.storage/nimbus_load_*.pkl`/`.json`. The Solver optionally caches plan state
  and holds a lock file at env-var-overridable paths (see `solver_writer.py`).
- 249 unit tests pass on a fresh clone with `pip install -e '.[dev]' && pytest`;
  ruff format + ruff check + pytest are strict CI gates on every PR.

## Compatibility

- **Home Assistant** — tested on 2025.7+.
- **Architecture** — Forecaster: any. Solver: `amd64` or `aarch64` only (needs
  a `highspy` wheel; no wheel exists for 32-bit armv7 / Pi 3 / Zero). `uname -m`
  tells you which you have.
- **Recorder** — required (declared in `manifest.json`). Nimbus trains from the
  recorder's own history, so if you've disabled the recorder or purge it aggressively
  the Forecaster has less to learn from.
- **Amber, Solcast, Sungrow/Sigen, EMHASS** — Nimbus reads whatever price/PV/
  load sensors you point it at. It doesn't depend on any specific brand.

## Removing Nimbus

1. Settings → Devices & Services → **Nimbus** → the three-dot menu on the hub
   card → **Delete**. This removes the hub and every Load / Power Signal /
   Power Source / PV String / Battery Tower subentry under it, plus all of
   their entities and devices.
2. HACS → **Nimbus** → the three-dot menu → **Remove**, to uninstall the
   integration itself.
3. Two things Nimbus writes to disk that neither of the above steps clears
   (harmless to leave, but here in case you want a completely clean uninstall):
   each load's persisted model/residual files at
   `.storage/nimbus_load_*.pkl` / `.json`, and, if you ever ran the Solver's
   integration mode, its plan-state/lock files at the paths shown in
   `solver_writer.py`'s own `PLAN_STATE_PATH` / `LOCK_PATH` (both env-var
   overridable — defaults live under `.storage/` too).
4. If you also installed the (now-deprecated) `nimbus_solver_app` Supervisor
   add-on: Settings → Add-ons → **Nimbus Solver** → **Uninstall**, then remove
   the repository from Add-on Store → Repositories.

## Status & roadmap

Early — built for and being validated against a real house before wider use. Not
yet recommending production use elsewhere. The next milestones (as tracked in
GitHub Issues):

- Clear the reference-household "Nimbus → HAEO Replacement Readiness Checklist"
  and graduate the Solver out of shadow mode.
- Sheddable loads: LP scaffolding exists; the config surface + reference
  automations are next.
- v1.0.0 removes the deprecated `nimbus_solver_app` add-on.

`BatteryConfig` is deliberately a single aggregate, not a per-tower/per-inverter
list — internal battery-to-inverter routing and load-sharing is the real
hardware's own BMS/inverter firmware's job, not something an external dispatch
optimizer should model or second-guess. The Solver only ever needs the whole
system's aggregate envelope: total usable capacity, real grid-facing max
charge/discharge power, and a blended round-trip efficiency.

## Contributing

- `pip install -e '.[dev]' && pytest` from a fresh clone runs the full 313-test
  suite green.
- Every PR must pass `ruff format --check`, `ruff check`, `pytest`, `hassfest`,
  and the `Version lockstep (integration <-> add-on)` job — all strict gates
  on `main`. `Type Check (mypy)` runs advisory (tracked toward Gold/Platinum,
  see issue #40).
- See [`docs/TESTERS.md`](docs/TESTERS.md) for who's running Nimbus on real
  hardware today, and what to capture in a bug report so it carries its own
  version anchor.
- Real household validation on the reference site is a load-bearing part of the
  merge criteria — see `CLAUDE.md` and `docs/real-world-integration/` for the
  full context.

## License

MIT — see [`LICENSE`](LICENSE).
