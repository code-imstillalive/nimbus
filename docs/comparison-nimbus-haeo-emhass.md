# Nimbus vs. HAEO vs. EMHASS — a source-level comparison

**Date:** 2026-09-06. **Method:** not a docs/marketing comparison — each system's
own source was cloned and read for this document. Nimbus: this repo, `main` at
the time of writing. HAEO: `purcell-lab/haeo` (a live household's own installed
fork; codeowners `@TrentHouliston`/`@BrendanAnnable`, upstream `hass-energy/haeo`),
`custom_components/haeo/core/`. EMHASS: `davidusb-geek/emhass`,
`src/emhass/{optimization,forecast,machine_learning_forecaster,web_server}.py`.

This exists because issue #418 asked a narrow question (how to model a second,
independently-schedulable bidirectional battery/EV) and answering it honestly
required actually reading HAEO's solver core rather than guessing from its
diagnostics. Once that research existed, extending it to EMHASS — the third
system this household's own README and CLAUDE.md repeatedly name as the other
established alternative — was a small additional step for a genuinely useful
reference. Per this repo's own PRIME DIRECTIVE, none of this is a proposal to
wire HAEO or EMHASS into Nimbus — it's prior-art research to inform Nimbus's own
independent design, the same spirit as CLAUDE.md's existing 2026-08-31
HAEO-source comparison (stochastic/power-curve capabilities).

## 1. Deployment model — the most consequential difference

| | Nimbus | HAEO | EMHASS |
|---|---|---|---|
| Runs | In-process inside HA (`custom_component`) | In-process inside HA (`custom_component`) | **Separate standalone service** (Flask web server + REST API), typically its own Docker container/add-on |
| HA integration surface | Direct `hass.states` reads, native entities | Direct `hass.states` reads, native entities | HA calls EMHASS's REST API (`/action/<name>`) via `rest_command`/automations; EMHASS's own `retrieve_hass.py` pulls history back from HA's API from *outside* the process |
| Dependency budget | Pure numpy + `highspy` only — no C-compiler in HA's own container ruled out scikit-learn from day one (documented at length in this repo's own ML bug-chain history) | numpy + `highspy` | Runs its own container, so it can and does depend on **scikit-learn, `skforecast`, `cvxpy`, pandas** — a full scientific-Python stack unavailable to the other two |

This single choice cascades into almost every other difference below. Nimbus
and HAEO share the same constraint (fit inside HA's own restricted add-on
environment) and reached similar answers (pure numpy, hand-rolled `highspy`
calls). EMHASS traded "one more moving part to keep running" for "the full
Python ML/optimization ecosystem," and its codebase reflects that trade
directly — see §3 and §4.

## 2. Optimization core architecture

**Nimbus** (`solver/network.py`, `solver/elements.py`): a single Python
function, `build_plan()`, that takes typed dataclass configs
(`BatteryConfig`, `GridConfig`, `SolarConfig`, `LoadConfig`,
`SheddableLoadConfig`, `AdequacyLoadConfig`, …) and hand-builds one `highspy`
LP directly — variables, bounds, and constraints written inline for each
concept. There is exactly one battery, one grid connection, one inverter
implicitly folded into the battery's own AC-side limits. This is a deliberate
choice, not an oversight: `BatteryConfig`'s own docstring documents a real
2026-08-18 audit item that concluded multiple *physical packs of the same
always-connected home ESS* should stay a single aggregate — that's the
inverter/BMS firmware's job, not the optimizer's. (See §5 for why that
reasoning doesn't extend to independently-schedulable EVs.)

**HAEO** (`core/model/`): a genuinely generic flow-network engine. Three
primitives — `Node` (source/sink/junction, two booleans), `Connection`
(one directional edge, chained through composable `Segment`s — efficiency
loss, power limit, pricing, SoC-dependent pricing), `Battery` (capacity +
initial charge + salvage value, nothing device-specific) — plus a domain
**adapter layer** (`core/adapters/elements/*.py`) that translates a config
element (Grid, Solar, Load, Inverter, Battery, Policy) into however many of
those generic primitives it needs. Adding a new participant *type* is an
adapter, not new LP code. This is how one codebase supports an arbitrary
topology (this household's own install: 16 participants, two separate
inverter branches, two independent EVs — see #418) with zero bespoke
per-participant solver logic.

Two structural details worth their own mention, independent of the
topology question:
- **Battery state is two monotonic cumulative variables** (`energy_in`,
  `energy_out`, both non-decreasing over the horizon; stored energy is
  their difference, per-period power is their finite difference) rather
  than a direct per-period charge/discharge variable pair. A different
  answer to the same simultaneous-charge/discharge degeneracy problem
  Nimbus's own `network.py` handles with an explicit combined-direction
  cap (issue #245/#246) — worth comparing directly if #245's residual gap
  is revisited.
- **Every connection carries a built-in deterministic tiebreak**: a
  secondary objective term (`priority * n + arange(1, n+1)` weights on
  power-in) that prefers *earlier* transfer whenever multiple solutions
  are otherwise equal-cost. A lightweight, always-on answer to LP
  degeneracy baked into the primitive itself, rather than a separate
  regularization pass — a different tool from Nimbus's own proximal
  regularization/rate-limiting mechanisms for the same class of problem.

**EMHASS** (`optimization.py`, 6266 lines, one `Optimization` class): built
on **`cvxpy`** (a declarative convex-optimization modeling layer) over
`highspy`/`scipy` as backend solvers — the same underlying HiGHS solver as
the other two, reached through a much higher-level API than either's
hand-rolled variable construction. Structurally the opposite of HAEO: not a
generic graph at all. It's a single, richly-parameterized model of *one*
house (`_add_battery_constraints`, `_add_deferrable_load_constraints`,
`_add_thermal_load_constraints`, `_add_shared_thermal_tank_constraints`,
`_add_hybrid_inverter_constraints`) — no `batteries[]` list, no generic
node/edge topology. Three solve modes: `perform_perfect_forecast_optim`
(oracle/backtest), `perform_dayahead_forecast_optim` (one shot), and
`perform_naive_mpc_optim` (receding-horizon MPC) — the last is the closest
operational analog to Nimbus's own rolling 96h-resolve-every-cycle design.

## 3. Physical/device modeling breadth

This is where EMHASS's heavier dependency budget shows up as real
capability, not just code size:

| Capability | Nimbus | HAEO | EMHASS |
|---|---|---|---|
| Single home battery | Yes (aggregate) | Yes (`Battery` element) | Yes |
| Multiple independent batteries/EVs | **No** — single `BatteryConfig` (the open question in #418) | **Yes**, structurally free (just more `Battery` elements) | No — one battery, "hybrid inverter" special-case only |
| Deadline-based flexible load (EV charging / HWS-style "must reach X by time T") | **Yes** — `AdequacyLoadConfig`: continuous power in `[0, max_power_kw]` over `[earliest_period, deadline_period]`, cumulative-energy-by-deadline constraint, zero cost until the deadline binds | Not found as a distinct construct in the core model read for this doc — closest equivalent is a time-varying `power_limit` forecast, not a first-class deadline/target primitive | **Yes**, and considerably richer: `_add_deferrable_load_constraints` supports discrete on/off-timestep scheduling, min/max on-time, sequential/grouped deferrable loads |
| Sheddable (curtailable) load | `SheddableLoadConfig` — per-period floor fraction, real shed cost | Curtailment flag exists on Solar/Load-type elements | Yes, integrated with deferrable-load machinery |
| Thermal storage (hot water tank, HVAC with real temperature dynamics) | No — hot water is handled via `AdequacyLoadConfig`'s energy-target shape, not a temperature model | Not found in the core model read for this doc | **Yes** — `_add_thermal_load_constraints`/`_add_thermal_battery_constraints`/`_add_shared_thermal_tank_constraints`: real draw-off demand, solar-gain terms, shared-tank membership |
| Demand-charge / capacity-interval tariffs (cost keyed to peak kW in a billing window, not just $/kWh) | **No** — grepped `solver/`, no match | Not found in the diagnostics or core model read for this doc | **Yes** — `_get_capacity_cost_per_kw`, `capacity_charge_window`/`capacity_charge_interval_timesteps` params, a real feature for tariffs that bill on peak demand |
| Cross-participant flow provenance / selective pricing (e.g. "price battery→grid differently from grid→load on the same wire") | No | **Yes** — per-connection integer tags + `Policies` rules keyed by `(connection, tag)` | No (single aggregate cost structure) |

## 4. Forecasting

| | Nimbus | HAEO | EMHASS |
|---|---|---|---|
| Own ML load/signal forecaster | **Yes** — from-scratch numpy k-NN + GBRT, nightly retrain, per-signal model selection via chronological validation against a seasonal-naive baseline, genuine confidence bands | Not found — relies on external forecast sources (Solcast, its own `data/loader/extractors/*` for AEMO/Amber/Nordpool/EMHASS/etc., or another integration's published forecast) | **Yes** — `MLForecaster` via `skforecast` wrapping real **scikit-learn** regressors (RandomForest, KNN, ElasticNet, Ridge, MLP, SVR, DecisionTree), with Optuna-style hyperparameter search |
| Forecast provenance philosophy | Learns from *this household's* own recorder history; explicitly zero HAEO involvement (PRIME DIRECTIVE) | Composable extractor plugins per data source, including — notably — an `emhass.py` extractor, i.e. HAEO is designed to *consume* another EMHASS instance's forecast as one input option | Self-contained; also consumes external price/PV forecast sources (Solcast, Open-Meteo) directly |

Nimbus's numpy-only forecaster is a genuine engineering constraint turned
into a real asset: it's the only one of the three that trains directly on a
specific household's own history with zero heavier dependencies, at the cost
of a materially smaller model-family search space than EMHASS's real
scikit-learn stack.

## 5. What this means for #418 specifically

Re-reading `elements.py`'s own settled reasoning against what both HAEO and
EMHASS actually do: the "single aggregate battery is correct" conclusion is
about physical packs of the *same* always-connected system, and stays right.
It was never a decision about *independently-schedulable* storage (an EV
that leaves, has its own deadline, and might trade differently). Nimbus
already carries the one piece of LP machinery that class of problem
actually needs — `AdequacyLoadConfig`'s deadline/target shape — as a
one-directional *load*, not a bidirectional *battery* with persistent SoC
and discharge capability. HAEO's answer (§2, §1 of the #418 thread) needs
none of EMHASS's on/off-timestep scheduling machinery and none of HAEO's own
tag/Policy system to get a first, useful version: a second storage
participant (list instead of singleton `BatteryConfig`) with an
array-valued, not scalar, `max_charge_kw`/`max_discharge_kw` (reusing the
exact forecast-entity shape Nimbus's solar/price/load inputs already use)
is the whole mechanism HAEO relies on for "the car isn't plugged in right
now."

## 6. Summary judgement (not a scorecard — three different bets)

- **Nimbus**: the youngest, narrowest-scoped, and most rigorously
  *instrumented* of the three — no other codebase read for this document has
  an equivalent to `regret.py`/`epr.py`/`tracking.py`/the counterfactual
  controllers/`quality_report.py`. It answers "is this actually helping,
  measurably, on my own data" more directly than either HAEO or EMHASS's own
  source shows evidence of doing. Its real gap for #418 is architectural,
  not conceptual: `build_plan()` needs to accept more than one storage
  participant.
- **HAEO**: the most *general* architecture — a genuinely reusable
  node/connection/segment graph that already solves the "arbitrary topology,
  arbitrary participant count" problem HAEO's own household install is
  actively using (two EVs, a miner, five separate loads, all first-class).
  Trades that generality for (as far as this read went) no built-in ML
  forecaster of its own and no equivalent evaluation/regret layer.
- **EMHASS**: the most *physically detailed* single-house model — real
  thermal tanks, demand-charge tariffs, discrete deferrable-load scheduling,
  a real scikit-learn forecasting stack — bought by running as a separate
  service outside HA's own process, and by staying a single-battery,
  non-graph model that doesn't generalize to a second independent battery
  the way HAEO's does.

None of these are "better" in the abstract; they're three different answers
to different constraints (fit inside HA vs. run anywhere; generalize the
topology vs. deepen a fixed one). Where they genuinely inform Nimbus's own
roadmap: the deployment-model choice (in-process, no heavy deps) is
already made and has real, documented advantages (§4) worth keeping: the
concrete gaps worth importing ideas from are the multi-storage-participant
architecture (HAEO, §5) and, if ever wanted, demand-charge tariff support
and richer deferrable-load scheduling (EMHASS, §3) — both addable
incrementally without adopting either project's own architecture wholesale.
