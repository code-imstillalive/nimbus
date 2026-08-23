# Nimbus

*Just a different type of cloud.*

> ⚠️ **Work in progress — active shadow-mode project, not a finished product.**
> Both the Forecaster and Solver are under real ongoing development. Neither drives any
> live battery/grid dispatch today — the Solver runs entirely in observe-only shadow mode
> against real household data, and stays that way until the reference-household evidence
> bar is cleared. Expect rough edges, breaking changes, and real bugs — several have
> been found and fixed in the days right around this repo going public. If you install
> this, please open a GitHub issue rather than expect a polished, plug-and-play experience.

**Current version: `0.73.0`** — see [`CHANGELOG.md`](CHANGELOG.md) for the release history.

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
- Multi-tower battery modelling in `elements.py` — one aggregate battery today,
  per-tower on the roadmap.
- Sheddable loads: LP scaffolding exists; the config surface + reference
  automations are next.
- v1.0.0 removes the deprecated `nimbus_solver_app` add-on.

## Contributing

- `pip install -e '.[dev]' && pytest` from a fresh clone runs the full 249-test
  suite green.
- Every PR must pass `ruff format --check`, `ruff check`, `pytest`, and the
  `Version lockstep (integration <-> add-on)` job — all strict gates on `main`.
- Real household validation on the reference site is a load-bearing part of the
  merge criteria — see `CLAUDE.md` and `docs/real-world-integration/` for the
  full context.

## License

MIT — see [`LICENSE`](LICENSE).
