# Nimbus

*Just a different type of cloud.*

> ⚠️ **Work in progress, not a finished product.** Both the Forecaster and Solver
> are under ongoing development. The Solver now drives real, live battery and grid
> dispatch on the reference household — it graduated out of observe-only shadow
> mode. Expect rough edges, breaking changes, and bugs. Several have been found
> and fixed in the days around this repo going public. If you install this,
> please open a GitHub issue rather than expect a polished, plug-and-play
> experience.

> 🔌 **Nimbus controls real equipment and makes decisions with real financial
> consequences.** Read [What Nimbus controls](#what-nimbus-controls) before you
> install it. It is provided with no warranty of any kind — you are responsible
> for your own hardware, your own electricity costs, and for any automation you
> build on its output.

## What Nimbus controls

Stated up front, because it is the first thing a new user should know and the
answer is not "nothing":

| | |
|---|---|
| **Your battery / inverter** | Nimbus **publishes a plan** and never commands your inverter itself. To make the plan move your battery you write your own HA automation that reads `sensor.nimbus_solver_battery_forecast` and commands your hardware. That is a deliberate boundary — the inverter-specific, money-moving step stays yours. |
| **Controllable loads** | Nimbus **does command these directly** — `switch` (`turn_on`/`turn_off`), `water_heater` (`set_operation_mode`: `performance`/`eco`) and `climate` (`set_hvac_mode`) — but **only** when you explicitly set a "Device to command" on a Controllable Load subentry. Leave that field blank and Nimbus plans and scores the load without ever touching it. |
| **Everything else** | Read-only. Nimbus never writes to your config and never calls a service outside the controllable-load path above. |

With no Controllable Load configured, **Nimbus changes nothing in your house.** It
watches and it plans. Turn on `switch.nimbus_solver_dispatch_dry_run` to record
what it *would* have dispatched, as real recorder history, before you let anything
act.

Step-by-step setup is in **[`docs/setup-guide.md`](docs/setup-guide.md)** — Part 1
is the minimum that gets it solving.

See [`CHANGELOG.md`](CHANGELOG.md) for the release history and current version — versioned from `custom_components/nimbus_load/manifest.json`, not restated here to avoid drifting stale again (this line previously read a hardcoded `0.92.2` for many releases after `manifest.json` had moved well past v0.94).

Nimbus is the first open-source load Forecaster and LP battery-dispatch Solver that ships as a single HACS integration and runs in Home Assistant's own process. Two cooperating pieces under one hub:

1. **A self-retraining ML load Forecaster.** Watches your power sensors, learns your
   consumption pattern (time-of-day, day-of-week, season, weather, recent lags),
   and publishes rolling per-load and whole-house forecasts. Zero manual retraining,
   zero config-file editing, no shell, cron, or systemd access needed.

2. **An LP-based battery and grid dispatch Solver.** A linear-programming optimiser
   (HiGHS-backed) that plans imports, exports, battery charge/discharge, and (optionally)
   sheddable-load timing over a rolling multi-day horizon, given price forecasts, PV
   forecasts, and the Forecaster's load forecast. Full-featured: time-varying
   network fees, two-tier P2P export bonuses, salvage terminal value, per-throughput
   degradation cost, and price-uncertainty risk aversion (CVaR-style).

Both pieces are pure-Python, run in-process inside Home Assistant, and are portable to
HA OS, Supervised, and Docker installs. The Solver requires a 64-bit host (`amd64` or
`aarch64`) because of `highspy`. The Forecaster runs anywhere numpy runs.

## Why

Most load forecasters in the Home Assistant energy-optimization world are either purely
weather-correlated (no learning from your house) or bundled inside a much
larger, harder-to-adopt optimizer. Most battery optimisers are either closed-source
cloud-hosted (Amber Shifty, ChargeHQ, Emberpulse, Reposit) or require a separate
long-running Python process outside HA (EMHASS). Nimbus is the first open-source
"forecast + optimise + monitor" stack that fits inside HA as one HACS integration,
runs on your hardware, and is instrumented enough for you to
know whether it's helping.

**What Nimbus gets you:**

- **Realistic load forecasting.** Learns from *your* household history, not a
  weather-only proxy. Two model families (k-NN and gradient-boosted regression trees)
  are trained and validated on chronologically-split held-out data each night;
  whichever performs better on *that* load wins that day.
- **A working LP dispatch plan.** You get a 96-hour battery and grid plan updated
  every minute, published as normal HA sensors so any dashboard, automation, or third-
  party MPC layer can consume it. No black-box "trust us": the plan's SoC trajectory,
  net cost, binding constraints, and shadow prices are exposed as sensor attributes.
- **Honest performance measurement.** Regret, Economic Performance Ratio (EPR), and
  three counterfactual controllers (no-control, threshold-rule, oracle-with-perfect-
  foresight) are built into the solver package. You can measure the fraction of the
  naive-to-oracle economic gap Nimbus closes on your data, over any
  window you choose. `quality_report.py` runs the same measurement on the
  reference household continuously and publishes the current fraction as a
  sensor attribute, so the claim is verifiable rather than asserted.
- **A plain `{time, value}` forecast shape.** If you'd rather run your own
  optimiser (EMHASS, a custom Python script), Nimbus's Forecaster feeds it
  straight in as a load-forecast source.

## Install (HACS)

The short version. [`docs/setup-guide.md`](docs/setup-guide.md) is the same thing
explained in plain English with screenshots, split into Basic and Advanced — use
that if this is your first install.

### Before you start

You need, at minimum:

- **Home Assistant with HACS.**
- **A battery State-of-Charge sensor** (0–100 %).
- **An import price sensor and an export price sensor** ($/kWh). A fixed-value
  template sensor is fine on a flat tariff.
- **A solar generation forecast** (Solcast, Open-Meteo Solar Forecast,
  Forecast.Solar — any of them).
- **A whole-house power sensor.** Nimbus builds the household load forecast from it.
- **Recorder history** on those sensors. Nimbus learns from it; day one works but
  is rough.

> **Architecture limit.** The Solver needs `highspy`, a compiled LP solver that
> Home Assistant installs automatically. Prebuilt wheels exist for **amd64 and
> aarch64 only** — a 64-bit x86 box, a Pi 4/5 on a 64-bit OS, most NUCs and VMs.
> On 32-bit ARM (`armv7`) there is no wheel and the Solver will not start. The
> Forecaster still works.

### 1. Install

1. HACS → three-dot menu → **Custom repositories**
2. Add `https://github.com/code-imstillalive/nimbus`, category **Integration**
3. Install **Nimbus**, then **restart Home Assistant** — a full restart, not a
   reload. Home Assistant only loads new Python on a restart.

### 2. Add the integration

**Settings → Devices & Services → Add Integration → "Nimbus".** No fields; it just
creates the hub. A persistent notification then points you at Solver settings —
that notification exists because of step 5.

On the integration page you get **seven `+` buttons** (Load, Power Signal, Power
Source, PV String, Battery Tower, Controllable Load, Battery Participant) and a
**gear icon** on the `Nimbus` hub row, which is where all the settings live.

### 3. Create the whole-house Power Signal — do this BEFORE the Solver wizard

The Solver needs a household load forecast, and Nimbus makes one for you — but it
has to exist before the wizard can offer it. **+ Power Signal →** point it at your
whole-house power sensor, leave Role as Other.

Skipping this is the single most common reason the Solver wizard looks impossible
to finish: its required load-forecast field has nothing to select.

### 4. Configure — the gear icon on the hub row

Three options:

- **Forecaster settings.** Shared across every load and signal. Temperature /
  humidity / battery / grid / solar context sensors, forecast horizon, retrain
  hour, training window and source. **Every field is optional**; submitting it
  blank is a complete working configuration.
- **Solver settings.** A 3-step sub-wizard (Battery → Grid Prices → Solar & Load
  Forecasts). **Only 5 fields across all three screens are required**: SoC sensor;
  import and export price sensors; solar forecast; load forecast (the Power Signal
  from step 3). Everything else is a refinement — leave it blank. Required fields
  are marked 🔴 on screen.
- **Topology diagram settings.** Cosmetic, for the topology card. Grid power,
  battery power and Loads are auto-detected; the fields worth filling are the six
  daily-kWh totals, since Nimbus forecasts power and has no equivalent. Suggestions
  are pre-populated from your Energy Dashboard config where possible.

### 5. Set your real battery and grid numbers — do not skip this

Every numeric Solver setting is a live `number.*` entity, **not** a wizard field,
and they all start at a defensive placeholder minimum. **Battery capacity starts at
0.1 kWh.**

A 0.1 kWh battery solves perfectly happily and produces a completely useless plan,
with no error and no warning. Set at least:

```
number.nimbus_solver_battery_capacity_kwh     your real usable capacity
number.nimbus_solver_max_charge_kw            real charge limit
number.nimbus_solver_max_discharge_kw         real discharge limit
number.nimbus_solver_grid_max_import_kw       your connection import limit
number.nimbus_solver_grid_max_export_kw       your approved export limit
number.nimbus_solver_battery_min_soc_percent  floor, e.g. 10
number.nimbus_solver_battery_max_soc_percent  ceiling, e.g. 100
number.nimbus_solver_efficiency_percent       round-trip, e.g. 90
number.nimbus_solver_battery_soh_percent      100 if new
```

All live-editable forever — you never re-run the wizard to change a number.

### 6. Add what you want forecast or controlled

Use the `+` buttons, as many times as you like, no restart:

| Button | What it is |
|---|---|
| **Load** | One circuit or appliance to learn and forecast |
| **Power Signal** | A whole-system quantity to forecast (battery, solar, grid, weather) |
| **Controllable Load** | A load the Solver schedules — and, if you give it a device entity, **physically switches** |
| **Battery Participant** | An additional battery or an EV the Solver dispatches separately |
| **Power Source** / **PV String** / **Battery Tower** | Topology-diagram metadata only, never forecast targets |

The reference household runs 18 circuit-breaker loads, 2 inverters and 4 battery
towers this way.

### 7. Verify

Developer Tools → States:

| Entity | Expected |
|---|---|
| `sensor.nimbus_solver_config` | `configured` |
| `sensor.nimbus_solver_lp_status` | `optimal` — **this is the one that means it works** |
| `sensor.nimbus_solver_solve_seconds` | a number, typically 0.5–3 |
| `number.nimbus_solver_battery_capacity_kwh` | your real capacity, **not `0.1`** |
| `sensor.nimbus_solver_binding_constraint_now` | a plain-English reason for the current plan |

At least one `sensor.nimbus_<your_load>_forecast` should also have a non-null
`forecast` attribute. If something is missing, see the two gotchas below
(load-forecast override, and the aggregator trap), and the Troubleshooting section
of [`docs/setup-guide.md`](docs/setup-guide.md).

### 8. Put it on a dashboard

The wizard configures entities; it places no Lovelace cards.
[`docs/dashboards.md`](docs/dashboards.md) has a copy-paste three-view dashboard
(Control Panel / Topology / Regret) using the custom cards the integration
registers for you.

### A naming quirk worth knowing

The integration's internal **domain** is `nimbus_load`, not `nimbus` — a historical
accident from when it was load-forecasting only, before the Solver existed. You see
it in service names (`nimbus_load.retrain`, `nimbus_load.solve_now`) and in logger
paths (`custom_components.nimbus_load.*`).

**Entity IDs are not affected.** They are `sensor.nimbus_solver_config`,
`number.nimbus_solver_grid_max_export_kw` and so on — `nimbus_`, never
`nimbus_load_`. Read `nimbus_load` and `Nimbus` as the same project; renaming the
domain now would break every existing user's entity IDs, long-term statistics and
automations ([#43](https://github.com/code-imstillalive/nimbus/issues/43)).

**Want the long version?** [`docs/setup-guide.md`](docs/setup-guide.md) walks the
same steps with screenshots and a troubleshooting section;
[`docs/configuration-reference.md`](docs/configuration-reference.md) is the
field-by-field lookup for afterwards.

## Understanding the configuration model

Everything lives under one Nimbus hub (one "Add Integration"), but the model has
grown — **7 subentry types** plus a 3-way settings menu — and it splits into
**three** separate concerns sharing that one hub:

1. **Forecasting.** `Load` and `Power Signal` subentries. Both feed the same
   k-NN/GBRT ML engine (`ml/model.py`, `coordinator.py`) and both produce a
   `sensor.nimbus_<x>_forecast` entity with training/validation.
2. **Dispatch.** `Controllable Load` and `Battery Participant` subentries. These
   are things the **Solver decides about** — when a load runs, how an extra
   battery or EV charges. No ML model and no forecast sensor, but very much not
   cosmetic: a Controllable Load with a device entity is the one place Nimbus
   **physically commands your hardware**.
3. **Topology and wiring.** `Power Source`, `PV String`, `Battery Tower` subentries.
   Pure metadata for the dashboard's topology diagram card. **No ML model, no
   coordinator, no forecast sensor.** These exist purely so the diagram knows what's
   physically wired to what.

The useful heuristic is **not** "no forecast sensor means topology" — that was true
when there were five types and is wrong now, because the dispatch pair produce no
forecast sensor either. The real test is what consumes the subentry: **the ML
engine (forecasting), the LP solver (dispatch), or only the diagram (topology).**

### 1. Load subentry

One appliance or circuit: a pool pump, a hot water system, an EV charger, an AC
zone. **One required field** (the source power sensor). Everything else
that used to be per-load (temperature, retrain hour, training window) moved to the
hub's shared Forecaster settings, since it's the same answer for every load in the
same house.

Two optional extras, only useful for a load on a fixed daily timer:
- `schedule_start_hour` and `schedule_end_hour`. Lets the model learn a sharp on/off
  boundary directly instead of approximating it through hour-of-day sin/cos features.
  Blank = no-op.
- `expected_load_kw`. A "deterministic mode" hint for a load whose draw is
  basically constant whenever it's on (a resistive heater, say) rather than
  something worth training a full model against.

Title is auto-derived from the source sensor's `friendly_name`.

### 2. Power Signal subentry

Same ML engine as Load, but forecasting a whole-system quantity (Battery, Solar,
Grid, or "other") as its own forecast target, not just as context for a
load's model. **No schedule or expected-load fields at all.** A system-level power
signal doesn't run on a timer the way an appliance does.

Two fields: the source sensor (required), and `signal_role`, an explicit dropdown
(Battery, Solar, Grid, Other), **not inferred from the entity's
name**. A check against a live install found the Battery sensor named
"Logger Battery power" (would match a naive keyword guess), but the Solar
sensor was "Combined Total DC Power" (no "solar" anywhere) and the Grid sensor
was "Logger Meter total active power" (no "grid" anywhere). Naming alone can't
reliably tell these apart on anyone's hardware, so it's a one-time explicit
choice instead. The topology card auto-wires its Grid and Battery flow lines
straight from whichever Power Signal carries that role, zero extra config needed.

### 3. Power Source subentry

Pure topology metadata: one physical hardware unit that connects to the
switchboard: an inverter, a hybrid battery/inverter, or a battery-only BMS.
Named "Power Source" rather than "Inverter" because a household might have a
battery-only unit, or PV wired through something that isn't a battery inverter at
all.

Fields: a name (required), plus two **optional** sensors: total battery power, and
total DC/PV throughput. Both optional because a PV-only unit has no battery power to
report, and vice versa.

### 4. PV String subentry

One physical PV string or array. One required field (its live power sensor),
one optional free-text label ("West array", "MPPT2", not a structured
MPPT-number field, since that's Sungrow-specific, not universal), and an **optional**
link to a Power Source.

That link is optional on purpose. It came out of testing against
different hardware (a SigenStor battery/inverter plus a separate third-party
SolarEdge PV system). The SolarEdge strings aren't wired through the Sigen inverter
at all; they are an independent source feeding the switchboard on their own. Leaving
the Power Source field blank renders that string as its own independent branch on
the diagram, rather than forcing it under a Power Source that doesn't own
it.

### 5. Battery Tower subentry

One physical battery pack. Four sensor fields, all optional (SoC, SoH, Voltage,
Temperature: the four the topology card's rendering function displays),
plus the same optional Power Source link as PV String, same reasoning.

SoC is the one field worth filling in first if you only do one. It drives the
visible fill-bar on the diagram. The form won't block you from submitting with
nothing filled in yet.

### 6. Controllable Load subentry

A load the Solver decides the **timing or level** of, rather than merely forecasting.
Three kinds, and only the fields for the chosen kind are read:

- **Sheddable** — can be reduced under price pressure, down to an optional minimum
  fraction, at a configurable shed cost that keeps shedding a last resort rather
  than the LP's free first choice.
- **Deferrable** — a real energy target by a real deadline (hot water, EV charging).
  The Solver chooses *when* inside that window, as a soft-priced preference.
- **Thermal** — a temperature tracked as a genuine **hard** constraint, not a priced
  preference. An unreachable target is relaxed once, automatically, to a soft
  shortfall price rather than breaking the whole plan.

**This is the only subentry that commands hardware.** Set "Device to command" and
Nimbus drives it directly: `switch` (`turn_on`/`turn_off`), `water_heater`
(`set_operation_mode`, `performance`/`eco`) or `climate` (`set_hvac_mode`). Any
other domain logs a warning and does nothing. **Leave that field blank and the load
is planned and scored but never touched** — the right way to try one first.

Rate limits are per-load and worth setting: minimum hold between commands, maximum
activations per day, and a re-send interval for a device that did not obey (default
15 minutes; a re-send never counts against the daily cap).

### 7. Battery Participant subentry

An **additional**, independently-metered battery the Solver dispatches on its own —
a second inverter, or an EV. Your main household battery is configured in Solver
settings and is always the `home` participant; do not add it again here.

Beyond the obvious (capacity, SoC sensor, power sensor, charge/discharge limits,
min/max SoC, efficiency) the fields that matter are the EV ones: an **availability**
binary_sensor that blocks charge and discharge entirely while off, a **departure
hour plus required SoC** enforced as a hard requirement so the car is drivable, a
**live charge-limit entity** so changing the limit in the vehicle's own app just
works, a **trip calendar** that reserves energy for a distance named in the event
title, and a **shared charger group** so two EVs on one physical charger cannot both
be planned at full rate.

Note the per-participant **"positive reading means charging"** flag. Conventions
differ by vendor; check a real reading while the thing is actually charging.

### The hub wizard: how you reach all of this

Two separate buttons on the Nimbus hub's device page, doing two separate things:

**"+ Add"** opens a menu of the 5 subentry types above. Pick one, fill in its (short)
form, submit. No restart, repeat as many times as needed. This is what makes
adding 18 circuit breakers, or several PV strings and towers, fast.

**"Configure"** opens a 3-way menu of shared, hub-level settings that apply across
everything:

- **Forecaster settings.** The ML input features every Load and Power Signal model can
  use for context: temperature, a temperature forecast sensor, humidity, a
  curtailment sensor, plus measured battery, grid, and solar power sensors.
  **Important gotcha:** these battery/grid/solar sensors are a *different concept*
  from a Power Signal subentry. They are not forecast targets, they are context
  features so a load's model can tell "was the battery charging at this exact
  moment" apart from load-driven signal. All independent and optional. Also:
  forecast horizon, retrain hour, training window (days of history).
- **Solver settings.** The 3-step wizard (Battery → Grid → Sources) described
  below, pointing the LP dispatch optimizer at its SoC sensor, import and export
  price sensors, and solar and load forecast sources. The numeric settings
  (capacity, max charge and discharge, efficiency, cost and salvage values) live as
  dashboard-editable `number.*` entities, not in this wizard, so they can be tuned
  live without reopening Configure.
- **Switchboard.** Everything the topology card can show beyond what it
  auto-detects: import and export price sensors, a switchboard-level battery power
  sensor (separate from the Solver's SoC sensor: different unit,
  different purpose), and the 6 daily-kWh headline stats. All optional; a blank form
  is a valid config. This form also auto-suggests entities pulled from Home
  Assistant's Energy Dashboard config wherever a field is still unset, filtered
  to `device_class: energy` sensors first, and only ever shown as an editable,
  pre-filled suggestion a human still has to confirm, never silently applied.

### The recurring gotcha, spelled out directly

"Battery" and "Grid" show up in **four different places** with different
meanings:

| Where | What it is |
|---|---|
| Forecaster settings → `battery_sensor` / `grid_sensor` / `solar_sensor` | Measured power, used only as ML **context features** for Load and Power Signal models |
| Power Signal subentry with `signal_role=battery` | The battery's power, forecasted as an ML **output target** |
| Solver settings → battery SoC sensor | The **LP optimizer's input**: a %, not a power |
| Switchboard → battery power sensor | The **topology card's** signed kW reading, for diagram coloring |
| Battery Tower subentry → SoC/SoH/Voltage/Temp | **Per-physical-pack** topology metadata, no forecasting involved at all |

None of these are wrong or redundant. They serve different
subsystems (ML context, ML forecast target, LP optimizer, dashboard diagram,
per-hardware-unit display), but nothing else currently explains that they're
different, which is the kind of thing that reads as confusing or broken on a
fresh install.

### Two more gotchas: which load-forecast field wins, and the aggregator trap

Solver settings has **two** fields that both sound like "my household load":

| Field | What it does |
|---|---|
| **Household load forecast sensor** (`solver_load_forecast_sensor`) | A single entity's forecast, used as-is |
| **Optional: individual circuit forecast sensors to sum instead** (`solver_load_forecast_entities`) | Sums N entities together. **Wins outright over the field above the instant it has even one entry**, regardless of what's configured there |

Found live (issue [#111](https://github.com/code-imstillalive/nimbus/issues/111)): if you've ever pointed the "individual circuits" field at a single third-party forecast sensor while experimenting, it silently keeps winning even after you change the single-sensor field to something else. There's no warning, no error. The Solver quietly keeps reading whichever field is non-empty. **If you only want one forecast source, leave "individual circuit sensors" completely blank.**

The second trap is sharper: **never point "Household load forecast sensor" at `sensor.nimbus_household_load_total_forecast`** (or any other Nimbus aggregator sensor). That entity **is** the thing this field feeds *into*, not a valid source for it. With the "individual circuits" list empty, the aggregator has nothing to sum. It publishes a structurally-valid series that's near-all-zero except a single live "now" reading. Result: a confident-looking plan built on the belief nobody in the house consumes anything. Reported impact (issue [#118](https://github.com/code-imstillalive/nimbus/issues/118)): a $46/day misplan, every health-check field green.

This specific case is now caught automatically (a load forecast under 10% non-trivially-nonzero is rejected with a message naming this mistake), but picking the right entity in the first place (a `sensor.nimbus_<your_load_signal>_forecast`, from a Load or Power Signal subentry you created yourself) avoids hitting that guard at all.

## Running the Solver

**Running the Solver settings wizard is mandatory, not optional, if you want the
Solver at all.** Every `number.nimbus_solver_*` entity (battery capacity, max
charge and discharge, grid limits, costs, risk aversion, network fees, salvage value,
efficiency) starts at a defensive placeholder minimum. A persistent notification
fires the moment the hub is created pointing you at **Configure → Solver settings**.
If you dismiss it, edit the `number.nimbus_solver_*` entities directly instead.
Confirm `sensor.nimbus_solver_config` reads `configured` in Developer Tools → States
before expecting a plan.

**If you only want load forecasting, skip this whole section.** The Forecaster works
standalone with zero further Solver setup.

The Solver runs natively in-process on a 1-minute timer as soon as `highspy` (the
compiled LP solver, an automatic `manifest.json` requirement, prebuilt wheels for
amd64 and aarch64 only) finishes installing. Every solve is a pure function:
forecast and price inputs in, a `Plan` dataclass out, and writes its result to two
sensor entities:

- `sensor.nimbus_solver_battery_forecast`. 96-hour battery power/SoC plan, plus
  the solved `total_cost`, `equivalent_full_cycles`, `binding_constraint_now`,
  shadow prices, and every planning-horizon interval as an attribute.
- `sensor.nimbus_household_load_total_forecast`. The whole-house load forecast
  consumed by the Solver, plus a `whole_house_cross_check_now_kw` field
  that compares the summed 18-circuit forecast against a single independent
  whole-house meter for real-time integrity.

Both are `SensorEntity` classes attached to the Nimbus hub device, with the
`forecast` list excluded from the Recorder (`_unrecorded_attributes`) so long-term
statistics keep working without tripping the 16 KB per-attribute limit.

### Legacy standalone-script path

One older path remains fully supported for the one case the native path
can't cover: you'd rather run the Solver on a separate always-on device than
inside HA's process. The standalone script (`nimbus_solver_forecast_writer.py`)
lives in `docs/real-world-integration/`. Both paths run byte-identical solve logic.

**Removed:** the `nimbus_solver_app` Supervisor add-on (deprecated since
v0.73.0) has been removed from this repo entirely — ahead of the v1.0.0
shadow-mode-graduation milestone, as its own standalone cleanup (see
[#357](https://github.com/code-imstillalive/nimbus/issues/357)), since it had
already drifted out of sync with the integration's own solver code and had no
real path to staying maintained as a third copy. The native in-process path
above covered every architecture the add-on did, with no separate container,
no version-lockstep discipline, and no three-way copy sync to maintain. If
you're still on the add-on: uninstall it (Settings → Add-ons →
**Nimbus Solver** → **Uninstall**, then remove the repository from Add-on
Store → Repositories) and finish the integration's Solver wizard instead; the
native path takes over the same `sensor.nimbus_solver_*` outputs with no
config migration. Tracking:
[#76](https://github.com/code-imstillalive/nimbus/issues/76).

## What Nimbus publishes

### Per-load sensors (one per Load subentry)

- `native_value`. Current predicted load in kW.
- `forecast` attribute. A plain `{time, value, lower, upper}` list, generic by
  design so it can drop into EMHASS or any other optimiser as a load-forecast
  source, or be graphed with ApexCharts, plotly, or lovelace-plotly.

### Whole-house rollup

- `sensor.nimbus_household_load_total_forecast`. The sum of every configured
  Load's forecast, plus the whole-house cross-check field. This is what the Solver
  should be pointed at as its load-forecast source (Configure → Solver settings →
  Sources).

### Solver plan

- `sensor.nimbus_solver_battery_forecast`. Full battery and grid dispatch plan.
  Attributes include `forecast` (per-interval `battery_kw`, `soc_pct`,
  `grid_import_kw`, `grid_export_kw`, `import_price`, `export_price`, `load_kw`,
  `solar_kw`, `net_cost`), `status`, `total_cost`, `total_cost_with_fixed_costs`,
  `equivalent_full_cycles`, `total_throughput_kwh`, `n_clamped_periods`,
  `binding_constraint_now`, and both shadow prices. `battery_kw` is AC-side
  (grid-side of the inverter) and **positive means discharging, negative
  means charging** — see `battery_kw_side`/`battery_kw_sign_convention` on
  the entity's own attributes for the machine-readable form of this.
- `sensor.nimbus_solver_config`. A live mirror of every Solver setting the wizard
  captured, for one-glance sanity checks in Developer Tools → States.
- `sensor.nimbus_topology_config`. A bridge of every Power Source, PV String, and
  Battery Tower subentry plus the switchboard output, driving the topology
  dashboard card.

### Live tuning knobs (all editable from the dashboard)

Every plain numeric Solver setting is its `number.nimbus_solver_*` entity.
Edit inline on the dashboard without touching the wizard:

- Battery: `capacity_kwh`, `soh_percent`, `min_soc_percent`, `max_soc_percent`,
  `max_charge_kw`, `max_discharge_kw`, `efficiency_percent`.
- Grid: `max_import_kw`, `max_export_kw`, `flat_fee_rate`, three
  `network_fee_*` blocks plus `network_fee_default_rate`.
- Economics: `charge_cost`, `discharge_cost`, `degradation_cost_per_kwh`,
  `salvage_value`.
- Risk: `risk_aversion`, `import_price_risk_aversion`,
  `export_price_risk_aversion`.
- Sources: `auto_include_known_solar` switch, plus an optional multi-entity
  load-forecast list for granular per-circuit summation.

### Diagnostics, dry-run, and other hub sensors

- `sensor.nimbus_health_report`. Always-on "what's failing, what's flatlined,
  what's not running" summary. `native_value` is a plain ERROR-level count
  from recent log activity; `recent_errors`/`recent_warnings` (up to 20 each)
  and a `subentry_status` entry per forecastable subentry give the detail
  behind that count without digging through the log file.
- `switch.nimbus_solver_dispatch_dry_run` / `sensor.nimbus_solver_dispatch_dry_run`.
  Real-dispatch groundwork — while the switch is on, every solve cycle
  records what the Solver *would* have dispatched (`battery_kw`, `soc_pct`,
  `grid_import_kw`/`grid_export_kw`, `import_price`/`export_price`) as a
  durable, gap-free history via HA's own recorder and long-term statistics.
  Purely observational: nothing on this path ever calls a service or writes
  to real hardware, on or off.
- `sensor.nimbus_solver_price_response_latency`. Seconds between a
  configured price sensor changing and the resulting event-driven solve
  completing — the ongoing health signal for the `solve_on_price_change`
  feature. Only updates on a price-triggered solve; a cron- or
  startup-triggered solve leaves it at its last real value, since neither
  has a meaningful "time since the price changed" to report.
- `sensor.nimbus_mirror_temperature_forecast` / `sensor.nimbus_mirror_humidity_forecast`.
  A read-only dashboard mirror of whatever temperature/humidity forecast
  source is configured under Solver settings — never fed into the LP solve
  itself, purely for a single dashboard to show weather context alongside
  the dispatch plan without a second card pointed at a different entity.

### Services

- `nimbus_load.compute_quality_report` (`start`, `end` timestamps, optional
  `allow_partial`, default `true`). Scores an arbitrary historical window
  the same way the daily quality report scores "yesterday" — useful for
  diagnostics, backfilling a report after a gap, or comparing two specific
  days head-to-head. A window shorter than 24h needs `allow_partial: true`
  to run at all; `allow_partial: false` restricts scoring to full real
  calendar days, matching the daily report's own behaviour exactly.
- `nimbus_load.rescore_history` (optional `days`, default `1`, max `30`).
  The write-back counterpart to the above. A day is scored once, the
  morning after, and frozen into the quality report's `history` table — so
  a scoring-formula change leaves every earlier day on the old formula,
  sitting beside newer ones with nothing saying they are not comparable.
  This re-scores the last N complete days and writes the corrected figures
  back, stamping each row with the release that produced it. Each day costs
  a full oracle solve, so it is deliberately explicit, capped, and never
  runs automatically or on upgrade. A day whose real history is too thin to
  score is skipped with its reason rather than failing the whole run.
- `nimbus_load.retrain` (optional `entity_id`). Forces an immediate retrain
  of one Load/Power Signal, or every configured one if `entity_id` is
  omitted, without waiting for the next scheduled retrain window.
- `nimbus_load.solve_now`. Triggers an immediate Solver cycle on demand,
  reusing the exact same solve path the periodic timer and price-triggered
  solve both call — not a separate implementation.
- `nimbus_load.flex_telemetry_record`. Builds and publishes one
  `nem-flex-telemetry` schema-v2.0 record to `sensor.nimbus_flex_telemetry`
  from Nimbus's own numbers — boundary-aligned, period-averaged, with a
  counterfactual baseline — on demand rather than waiting for the next
  cycle. The record is nested under a single `record` attribute, so the
  thing to POST is `attributes.record` whole: the schema declares
  `additionalProperties: false`, and a spread record plus the housekeeping
  attributes Home Assistant adds would not validate as read. Takes no
  fields.
- `nimbus_load.set_controllable_load` (`controllable_load_name`,
  `controllable_load_kind`, optional `subentry_id`, plus every field the
  Controllable Load wizard itself accepts). Creates or updates one
  Controllable Load subentry in a single, atomic call — an alternative to
  the config_subentries wizard for scripting, automations, or an MCP tool
  (`ha_call_service`), reusing the wizard's own schema so validation can
  never drift between the two. Without `subentry_id`, matches an existing
  load by exact name (raises if more than one matches) or creates a new
  one; `subentry_id` targets a specific load unambiguously regardless of
  its title. Returns the subentry_id and the exact data now persisted, so
  the result is verifiable in the same call. Reloads the hub automatically
  so the change takes effect immediately.

### Quality, Backtest, and Counterfactual sub-devices

Three sub-devices parented to the hub, each with a legacy parent entity plus per-attribute flattened child sensors:

- **Nimbus Quality**. Publishes the Efficiency Performance Ratio (EPR) and its cost decomposition: `J_ref`, `J_ach`, `J_star`, `value_captured`, `uplift_available`, `theoretical_maximum_yield`, `regret_dollars`, `tracking_fidelity`, `tracking_cost`. Identity math: `value_captured + uplift_available = TMY`.
- **Nimbus Backtest**. Publishes reference-benchmark results: `nimbus_efficiency_backtest`, configured efficiency percent, best and worst candidate 24h costs.
- **Nimbus Counterfactual**. Publishes what Nimbus's plan would have produced against what the plant actually did: `real_soc_anchor_pct`, `real_soc_close_pct`, `nimbus_only_soc_close_pct`.

See [`docs/entities.md`](docs/entities.md) for the full per-entity table (unit, meaning, formula) and the known state_class warnings tracked in [#283](https://github.com/code-imstillalive/nimbus/issues/283).

Three ready-made Lovelace cards ship with the integration itself — a "Control Panel" dispatch card, a "Regret" (dispatch-vs-oracle) card, and a real-time switchboard "Topology" diagram, all installed automatically via HACS with no `www/` copy needed, and all findable in the card picker by searching "nimbus". See [`docs/dashboards.md`](docs/dashboards.md) for setup and the full config-field reference.

See [`docs/configuration-reference.md`](docs/configuration-reference.md) for
every field across every wizard step and subentry type, plus the full
default, range, and unit table for every `number.nimbus_solver_*` entity above.

## How it works

### Forecaster

- Two pure-numpy model families, both compiled from scratch to avoid the fragility
  of installing scikit-learn, XGBoost, or LightGBM inside HA's container (no C compiler
  present):
  - **k-NN.** A lazy learner that finds past moments that "look like" the current
    one (time-of-day, day-of-week, month, temperature, recent lags) and averages
    what the load was then.
  - **GBRT.** A from-scratch gradient-boosted regression tree ensemble, the same
    algorithm XGBoost and LightGBM implement, without the compiled speed advantage
    (not needed at this data scale).
- Every retrain (once a day, configurable, defaults to 3am local) chronologically
  splits held-out data, validates both models, and picks whichever performs better
  for *that specific load*.
- **Lag features** are included ("what was this load doing LAG_SHORT / LAG_LONG
  grid-steps ago") and turned out to be among the most important inputs on every
  load tested in the reference household's 30-day backtest.
- At forecast time, beyond the first couple of steps, no "future" lag value
  exists yet: `predict()` recursively feeds each step's prediction back in as
  the lag for the next step (standard direct-recursive forecasting).
- Everything (training plus prediction) is offloaded to an executor thread so it
  never blocks Home Assistant's event loop.
- Trains directly from Home Assistant's recorder history. No external API
  calls, no credentials to manage.
- Confidence bands: `predict()` returns calibrated `lower` and `upper` percentiles
  from the GBRT residuals so the forecast can be shown as a fan chart, not just
  a point estimate.
- A cross-source blend (`ml/blend.py`) can weight multiple forecast inputs by
  their inverse MAE.

### Solver

- **LP-based, HiGHS-backed.** `network.py`'s `build_plan()` is a pure function:
  element configs plus a time horizon in, a `Plan` dataclass out, zero HA imports
  and zero side effects.
- **Elements modelled:** `Grid` (with time-varying network fees plus P2P bonus),
  `Battery` (single-aggregate today; per-tower is on the roadmap), `Solar`,
  `Load`, `SheddableLoad` (fully implemented from day one even though the
  reference household has zero configured; the LP scaffolding is there).
- **Stability mechanisms** (`network.py`, extended 2026-08-20): proximal
  regularisation against the previous plan, per-interval rate limiting, and
  confidence-aware dispatch. A rolling re-solve where two near-tied optima flip
  arbitrarily was a known pathology in earlier prototypes, so
  Nimbus refuses to repeat it structurally.
- **Structural degeneracy guards.** `BatteryConfig` and `GridConfig` refuse to
  construct if `charge_cost + discharge_cost` falls below the wash-trade spread
  threshold, the zero-friction shape that produces wash-trade
  degeneracy. Not a warning; a `DegenerateConfigError`.
- **Rolling refinement** (`rolling.py`, Layer 2): standard receding-horizon
  control (solve, act, observe, re-solve) with the previous plan threaded
  through automatically so the stability mechanisms above have something
  to stabilise against.
- **Two-stage stochastic LP** (`stochastic.py`, opt-in, Track A2): a
  two-stage program with stage-1 decisions shared across every scenario and
  stage-2 variables scenario-indexed, for hedging against solar
  uncertainty. A separate module from `network.py`.
- **Counterfactuals** (`counterfactuals.py`, `regret.py`, `epr.py`): no-control,
  a tuned two-threshold price rule with no forecasting, and an oracle with
  perfect foresight. `quality_report.py` ties these plus tracking (measured
  vs. commanded) into a single "how good is the current dispatch, right now"
  live report.

### Both

- Persist across restarts. The Forecaster saves trained models to
  `.storage/nimbus_load_*.pkl` and `.json`. The Solver optionally caches plan state
  and holds a lock file at env-var-overridable paths (see `solver_writer.py`).
- The suite passes on a fresh clone with `pip install -e '.[dev]' && pytest`.
  ruff format, ruff check, and pytest are strict CI gates on every PR.
  See "Contributing" below for the current size and the exact commands.

## Compatibility

- **Home Assistant.** Tested on 2025.7+.
- **Architecture.** Forecaster: any. Solver: `amd64` or `aarch64` only (needs
  a `highspy` wheel; no wheel exists for 32-bit armv7, Pi 3, or Zero). `uname -m`
  tells you which you have.
- **Recorder.** Required (declared in `manifest.json`). Nimbus trains from the
  recorder's history, so if you've disabled the recorder or purge it aggressively
  the Forecaster has less to learn from.
- **Amber, Solcast, Sungrow/Sigen, EMHASS.** Nimbus reads whatever price, PV, and
  load sensors you point it at. It doesn't depend on any specific brand.

## Removing Nimbus

1. Settings → Devices & Services → **Nimbus** → the three-dot menu on the hub
   card → **Delete**. This removes the hub and every Load, Power Signal,
   Power Source, PV String, and Battery Tower subentry under it, plus all of
   their entities and devices.
2. HACS → **Nimbus** → the three-dot menu → **Remove**, to uninstall the
   integration itself.
3. Two things Nimbus writes to disk that neither of the above steps clears
   (harmless to leave, but here in case you want a completely clean uninstall):
   each load's persisted model and residual files at
   `.storage/nimbus_load_*.pkl` and `.json`, and, if you ever ran the Solver's
   integration mode, its plan-state and lock files at the paths shown in
   `solver_writer.py`'s `PLAN_STATE_PATH` and `LOCK_PATH` (both env-var
   overridable; defaults live under `.storage/` too).
4. If you also installed the `nimbus_solver_app` Supervisor add-on (removed
   from this repo, see "Legacy standalone-script path" above): Settings →
   Add-ons → **Nimbus Solver** → **Uninstall**, then remove the repository
   from Add-on Store → Repositories.

## Proven in production

**Nimbus has been driving live battery and grid dispatch on the reference
household continuously since early September 2026** — not a trial, not shadow
mode, not a dry run. It plans and dispatches across a two-inverter, four-tower
battery fleet every day, including the household's real peer-to-peer export
window where the money is.

### The hardware it runs on

| | |
|---|---|
| **Inverters** | **2 ×** Sungrow hybrids — one **SH25T**, one **SH15T** — over Modbus TCP |
| **Battery** | **2 dual stacks = 4 towers**, 7 and 8 modules per stack (BCU firmware `SBHBCU-S_22011.04.10`) |
| **Usable capacity** | **122.2 kWh** |
| **Power limits** | **40 kW** charge / **40 kW** discharge |
| **Grid connection** | **42 kW** import / **40 kW** export |
| **Measured round-trip efficiency** | **85.8 %** |
| **Battery State of Health** | towers at 99 / 99 / 96 / 94 %, combined **96.5 %** |
| **Monitored loads** | **18** individually-metered circuit breakers |
| **Retailer / market** | LocalVolts, 5-minute wholesale settlement, with a real P2P export contract |
| **Host** | Two NUCs in a keepalived active/standby pair, Nimbus running in-process inside Home Assistant |

### How it actually performs on that hardware

All measured on the live install, not asserted:

| | |
|---|---|
| Solve time | **~1.2 s** for a **202-period, 96-hour** horizon, re-planned continuously |
| Economic Performance Ratio | **76 %** of the theoretically-available value captured (1 Oct 2026), scored against a perfect-foresight oracle |
| Export performance | 1 Oct 2026 settled at **$18.02** P2P export revenue on **41.8 kWh**, **$19.69** net for the day |
| Dispatch precision | battery reaches its committed P2P rate **~28 seconds before** the window opens, after the block lead-time was tuned |
| Stability | 42 independent job-health checks green; no unplanned failover since 8 September |

**This is one household, not a fleet.** It is a genuinely demanding one — real
money, real export commitments, 122 kWh of battery across two inverters — and
every rough edge below was found on it. But a single reference install is still a
single install: **there is no production-use recommendation for other households
yet.** Nimbus remains actively developed, with bugs found and fixed regularly
(tracked in `docs/real-world-integration/` and `CLAUDE.md`).

## Status and roadmap

The next milestones (as tracked in GitHub Issues):

- Continue hardening real live dispatch against the reference household's own
  findings (multi-battery/EV participants, cross-battery wash-trade guards,
  deferrable-load scheduling).
- Sheddable loads: LP scaffolding exists; the config surface and reference
  automations are next.

`BatteryConfig` is a single aggregate, not a per-tower or per-inverter
list. Internal battery-to-inverter routing and load-sharing is the
hardware's BMS or inverter firmware's job, not something an external dispatch
optimizer should model or second-guess. The Solver only ever needs the whole
system's aggregate envelope: total usable capacity, grid-facing max
charge and discharge power, and a blended round-trip efficiency.

## Contributing

- `pip install -e '.[dev]' && pytest` from a fresh clone runs the suite green.
  It splits the way CI splits it: the stub-based suite, which is the bulk of
  it, and a much smaller real-HA-harness suite under `tests/hass_integration/`
  that needs a Home Assistant matching `manifest.json`'s minimum version.

  ```bash
  pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant   # the stub suite
  pytest tests/hass_integration/                                       # the real-HA harness
  ```

  On 2026-09-15 the first of those reported **2580 passed, 14 skipped, 328
  subtests**. That figure is here to convey scale, not as a number to keep
  in sync — this line previously read "313" in one place and "249" in
  another, both several thousand tests out of date, which is exactly the
  drift the version line at the top of this file was already rewritten to
  avoid. Run the command for the real answer.
- Every PR must pass `ruff format --check`, `ruff check`, `pytest`, `hassfest`
  and the HACS `validate` job. All strict gates on `main`. `Type Check (mypy)`
  runs advisory. (A `Version lockstep (integration <-> add-on)` job used to be
  listed here; it went with the `nimbus_solver_app` add-on in v0.94.85, #357,
  and no such job exists.)
- **Quality Scale.** Bronze, Silver, Gold, and Platinum tier-gap work has
  landed (issues [#37](https://github.com/code-imstillalive/nimbus/issues/37),
  [#38](https://github.com/code-imstillalive/nimbus/issues/38),
  [#39](https://github.com/code-imstillalive/nimbus/issues/39),
  [#40](https://github.com/code-imstillalive/nimbus/issues/40)). The
  `quality_scale` key in `manifest.json` will be set on the run into v1.0.0.
- **Maintainer capacity.** Nimbus is currently maintained by a single author
  against one reference household running it live. Expect issue response
  within a few days, not hours. A real test report from a second household is
  worth as much as a code fix.
- See [`docs/TESTERS.md`](docs/TESTERS.md) for who's running Nimbus on
  hardware today, and what to capture in a bug report so it carries its own
  version anchor.
- Reference-household validation is a load-bearing part of the
  merge criteria. See `CLAUDE.md` and `docs/real-world-integration/` for the
  full context.

## License

MIT. See [`LICENSE`](LICENSE).
