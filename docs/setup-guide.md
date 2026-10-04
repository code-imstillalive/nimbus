# Setting up Nimbus

Use this guide to install Nimbus and check its plans before you connect them to
hardware. It is for Home Assistant users who already have working energy sensors.

- **Part 1: Basic.** Install Nimbus, create a load forecast, complete five required
  Solver fields, and set the numeric limits for your equipment.
- **Part 2: Advanced.** Everything else: dashboards, per-appliance learning,
  controllable loads Nimbus physically switches, EVs, the daily quality score,
  price events, tuning.

If you want the exhaustive field-by-field table instead, see
[`configuration-reference.md`](configuration-reference.md). That one is for looking
individual fields. This guide follows the installation sequence.

**First-run goal:** a current plan based on your sensors and equipment limits.
Completing the wizard alone does not meet this goal. Use the checks in
[§8](#8-check-it-actually-worked) before you consider control.

**Control boundary:** leave every **Device to command** field blank during
observation. Keep external dispatch automations disabled. The dry-run switch
records observations but does not disable other control paths.

This guide follows the repository's `main` branch. Your installed release can
differ. Check its version before you report a mismatch. Entity IDs below are
defaults. If you renamed an entity, use its current ID.

> **About the images:** screenshots illustrate one installation. Their capacities,
> prices, timings and entity names are not recommended settings. Follow the written
> steps if your screen differs.

---

## Contents

**Part 1: Basic**
1. [What Nimbus is, in one minute](#1-what-nimbus-is-in-one-minute)
2. [What you need before you start](#2-what-you-need-before-you-start)
3. [Install it](#3-install-it)
4. [Add the integration](#4-add-the-integration)
5. [Create the forecast the Solver needs](#5-create-the-forecast-the-solver-needs)
6. [Run the Solver settings wizard](#6-run-the-solver-settings-wizard)
7. [Set your real battery and grid numbers](#7-set-your-real-battery-and-grid-numbers)
8. [Check it actually worked](#8-check-it-actually-worked)
9. [Put a dashboard on it](#9-put-a-dashboard-on-it)
10. [What "working" looks like after a week](#10-what-working-looks-like-after-a-week)

**Part 2: Advanced**
11. [Forecaster settings: make the learning better](#11-forecaster-settings--make-the-learning-better)
12. [Loads: learn one appliance at a time](#12-loads--learn-one-appliance-at-a-time)
13. [Controllable loads: let Nimbus switch things](#13-controllable-loads--let-nimbus-switch-things)
14. [Battery participants: a second battery or an EV](#14-battery-participants--a-second-battery-or-an-ev)
15. [The daily quality score](#15-the-daily-quality-score)
16. [The topology diagram](#16-the-topology-diagram)
17. [Tuning knobs](#17-tuning-knobs)
18. [Optional inputs](#18-optional-inputs)
19. [Services you can call](#19-services-you-can-call)
20. [Actually driving your battery with the plan](#20-actually-driving-your-battery-with-the-plan)

**Reference**
- [Troubleshooting](#troubleshooting)
- [Every check in one place](#every-check-in-one-place)
- [Removing Nimbus](#removing-nimbus)

---

<a id="part-1--basic"></a>

# Part 1: Basic

## 1. What Nimbus is, in one minute

Nimbus does two separate jobs. You can use either on its own.

**The Forecaster** uses measured power history to estimate future demand.
Accuracy depends on the available history and how regularly the household uses
each load.

**The Solver** combines forecasts, prices and equipment limits to calculate a
plan. It publishes proposed battery charging, discharging, imports and exports as
sensors. The plan is optimal for its model, not a guarantee of the lowest real bill.

### What Nimbus does and does not control

This matters, so it is stated plainly rather than buried:

| | |
|---|---|
| **Your battery / inverter** | Nimbus publishes a plan. An external automation can read that plan and command your inverter. See [§20](#20-actually-driving-your-battery-with-the-plan) before you connect one. |
| **Controllable loads** (hot water, pool pump, aircon) | Nimbus can directly command `switch`, `water_heater` and `climate` entities. Assigning a **Device to command** enables this path. Read [§13](#13-controllable-loads--let-nimbus-switch-things) first. |
| **Everything else** | Nimbus only reads. |

On a new installation, Part 1 configures observation and planning only.
An existing automation that reads these sensors can still act on them.

### Brand independence

Nimbus uses Home Assistant sensors rather than a single inverter or retailer
brand. Compatibility still depends on the sensor's units, forecast format and
available history. Hardware control requires a separate, device-specific integration.

### Terms used in this guide

| Term | Meaning |
|---|---|
| SoC | State of charge, expressed as a percentage of the battery's capacity basis |
| SoH | State of health, used here to derate configured capacity |
| LP | Linear programming, the mathematical optimisation method |
| EPR | Economic Performance Ratio, a comparison with modelled available value |
| Oracle | A comparison model that uses known past inputs instead of forecasts |
| kW / kWh | Power / energy. These units are not interchangeable. |

---

## 2. What you need before you start

**Required:**

- **Home Assistant 2026.8.0 or later**, with HACS installed. This is the minimum
  declared in [`hacs.json`](../hacs.json).
- **A battery with a State-of-Charge sensor** (a `sensor.*` reading 0 to 100 %).
- **An import price sensor and an export price sensor** in $/kWh.
  Fixed-value template sensors can represent a flat tariff.
- **A solar forecast** with a supported time-series format. Examples include
  Solcast, Open-Meteo Solar Forecast and Forecast.Solar.
- **A whole-house power sensor** (kW or W). Nimbus builds the household load
  forecast from this itself.

**Strongly recommended:**

- **Recorded measurements** for the sensors Nimbus must learn from.
  Initial training needs usable history. A newly created sensor might not have enough.

**Architecture note:** Nimbus declares `highspy` as an installation requirement
in its [manifest](../custom_components/nimbus_load/manifest.json).
Use a supported 64-bit Home Assistant installation.
If this dependency cannot install, Nimbus setup can fail, including forecasting.
Do not assume that the Forecaster remains available on an unsupported platform.

### Check the inputs before installation

1. Open **Developer Tools → States**.
2. Check that each source has a numeric state rather than `unknown` or `unavailable`.
3. Confirm the unit of each source.
4. Check the forecast attributes for future timestamps and values.
5. Check the source sensors in **History**.

Use power sensors in W or kW, not cumulative energy meters in kWh.
For prices, `30 c/kWh` means `0.30 $/kWh`, not `30 $/kWh`.
Confirm that the selected solar forecast exposes intervals, not only a daily total.
Use the [configuration reference](configuration-reference.md) for the available fields.

### A note on required fields

The Solver wizard marks required fields with **🔴** and explains the marker.
For the basic path, leave optional fields blank.
Advanced features can require related fields together, even when each field is
individually optional. Read that feature's instructions before enabling it.

---

## 3. Install it

1. Open **HACS**.
2. From the three-dot menu, select **Custom repositories**.
3. Enter `https://github.com/code-imstillalive/nimbus` in **Repository**.
4. Select **Integration** as the type or category.
5. Select **Add**.
6. Find **Nimbus** in HACS.
7. Open its page.
8. Select **Download**.
9. Restart Home Assistant from **Settings → System**.

![HACS Custom repositories dialog, with Nimbus listed as code-imstillalive/nimbus](images/setup/hacs-custom-repo.png)

The custom repository appears in HACS after you add it.

**Expected outcome:** after the restart, Nimbus appears in Settings → Devices &
Services → **Add Integration** search.

> **Restart Home Assistant after installation or an update.**
> A config-entry reload does not reliably load changed Python modules.

---

## 4. Add the integration

1. Open **Settings → Devices & Services → + Add Integration**.
2. Search for **Nimbus**.
3. Select **Nimbus**.
4. Select **Submit** in the **"Set up Nimbus"** dialog.

This dialog has no fields. It creates the Nimbus hub.

![The Select brand dialog with "nimbu" typed, showing Nimbus and JustNimbus](images/setup/setup-nimbus-dialog.png)

> **Select the integration named exactly "Nimbus", not "JustNimbus".**
> Use the name to identify it. Icons can vary between Home Assistant versions.

**Expected outcome:** you land on the Nimbus device page, and a **persistent
notification** appears pointing you at Configure → Solver settings. That
notification is not an error: it exists because of the trap in [§7](#7-set-your-real-battery-and-grid-numbers).

On the Nimbus integration page, two things matter:

- **The `+` buttons along the top**: one per thing you can add (Load, Power
  Signal, Controllable Load, Battery Participant, and three diagram-only types).
  This guide writes these as **"+ Add → Power Signal"** and so on.
- **The gear icon on the `Nimbus` hub row**: settings that apply to everything at
  once. This guide calls it **Configure**, which is what Home Assistant calls it
  elsewhere.

![The Nimbus integration page](images/setup/integration-page.png)

The integration page has controls for adding devices and signals.
The gear icon opens the hub configuration. Nimbus creates reporting sub-devices
such as Backtest and Counterfactual automatically.

> If you only want **load forecasting** and no optimisation at all, you can stop
> after [§12](#12-loads--learn-one-appliance-at-a-time) and never touch the Solver.
> Nothing breaks.

---

## 5. Create the forecast the Solver needs

Create the household load forecast before opening the Solver wizard.
The wizard needs the resulting forecast entity.

Create a **Power Signal** from your measured whole-house power sensor.
Do not select a forecast or an optimiser output as its source.

### Create the whole-house load signal

1. On the Nimbus device page: **+ Add → Power Signal**.
2. Dialog: **"Add a power signal"**.
3. **🔴 Sensor to forecast** → your **whole-house power sensor**.
4. **Role** → leave as **Other**. (Role only affects the topology diagram.)
5. Submit.

![The Add a power signal dialog, with Role left as Other](images/setup/add-power-signal.png)

The Power Signal screen takes a measured sensor as its input.
The **Other** role is suitable for a whole-house consumption signal.

**Expected outcome:** a new sensor named after your source:
`sensor.nimbus_<your_sensor_name>_forecast`. The name is derived from your source sensor's own name, so a
whole-house sensor called `house_total_power` produces a Nimbus forecast entity
ending `_house_total_power_forecast`.

> **Initial training can take several minutes.**
> If the forecast remains `unknown`, check its training status and source history.
> See [Troubleshooting](#troubleshooting). Fifteen minutes is a diagnostic prompt,
> not a guaranteed training deadline.

### Optionally, a solar signal

If your solar forecast integration already gives you a forecast sensor (Solcast
etc.), you do **not** need this: use theirs. Only add a Power Signal for solar if
you want Nimbus to forecast your inverter's DC power itself.

### The two traps on this screen

> **Never point a Power Signal at an optimiser's plan entity.** Use real,
> measured sensors only. Point it at HAEO's, EMHASS's or Nimbus's own output and
> the model learns from its own predictions, and the forecast slowly becomes
> nonsense.

> **Do not use `sensor.nimbus_household_load_total_forecast` as a forecasting or Solver input.**
> It is an output for display and diagnostics. Feeding it back into planning creates
> a circular dependency. Select the Power Signal forecast instead.

---

## 6. Run the Solver settings wizard

**Nimbus integration page → the gear icon on the `Nimbus` hub row.**

You get a menu, **"Nimbus settings"**, with three options:

| Menu option | Do it now? |
|---|---|
| Forecaster settings (shared sensors + tuning) | **No**: entirely optional, [§11](#11-forecaster-settings--make-the-learning-better) |
| **Solver settings (your real battery/grid/solar setup)** | **Yes: this one** |
| Topology diagram settings | **No**: cosmetic, [§16](#16-the-topology-diagram) |

![The Nimbus settings menu](images/setup/settings-menu.png)

The basic path uses **Solver settings**.
Forecaster tuning and topology settings are optional.

Pick **Solver settings**. It is a three-screen wizard.

<a id="screen-1-of-3--solver-battery"></a>

### Screen 1 of 3: "Solver: Battery"

**Exactly one required field.**

| Field | What to put |
|---|---|
| **🔴 Battery State of Charge sensor (%)** | Your inverter's own live SoC sensor. A real 0 to 100 % measurement, not a target or setpoint. |
| Live max-discharge setpoint entity | **Leave blank.** Correct for almost every install. |

![Solver Battery, screen 1 of 3, with the State of Charge sensor selected](images/setup/solver-wizard-1-battery.png)

> **Set capacity and power limits after the wizard.**
> These are live `number.*` entities.
> Complete [§7](#7-set-your-real-battery-and-grid-numbers) before evaluating the plan.

<a id="screen-2-of-3--solver-grid-prices"></a>

### Screen 2 of 3: "Solver: Grid Prices"

Two fields are required for the basic path.
Leave the remaining fields blank initially.

| Field | What to put |
|---|---|
| **🔴 Live import (buy) price sensor ($/kWh)** | What you pay to import, right now. |
| **🔴 Live export (sell) price sensor ($/kWh)** | What you are paid to export, right now. |
| Everything else (2nd/3rd price sources, price-event simulation, DNSP envelopes) | **Leave blank.** [§18](#18-optional-inputs) |

![Solver Grid Prices, screen 2 of 3, with the two required price sensors set](images/setup/solver-wizard-2-prices.png)

> **For a flat tariff:** create two template sensors with your fixed rates.
> Select them as the import and export sources. Savings depend on your equipment
> and usage, not only the tariff.
>
> **If the source provides a supported price forecast, Nimbus uses it for planning.**
> Otherwise, it can hold the current price across the horizon.
> Check the resulting plan prices rather than assuming a forecast was accepted.

<a id="screen-3-of-3--solver-solar--load-forecasts"></a>

### Screen 3 of 3: "Solver: Solar & Load Forecasts"

**Two required fields out of sixteen.**

| Field | What to put |
|---|---|
| **🔴 Solar generation forecast sensor** | Your Solcast / Open-Meteo / Forecast.Solar sensor. |
| **🔴 Household load forecast sensor** | **The Power Signal you made in [§5](#5-create-the-forecast-the-solver-needs)**: `sensor.nimbus_<your_sensor>_forecast`. |
| Optional: individual circuit forecast sensors | **Leave completely blank.** See the warning below. |
| Everything else | **Leave blank.** [§15](#15-the-daily-quality-score) and [§18](#18-optional-inputs) cover them. |

![Solver Solar and Load Forecasts, screen 3 of 3](images/setup/solver-wizard-3-forecasts.png)

> **Individual circuit forecasts take priority over the household forecast.**
> For the basic whole-house path, leave the circuit list empty.
> If you previously added entries, remove all of them to restore the household source.
> Check `load_forecast_source_used` in the [template check](#every-check-in-one-place).

Submit. **Expected outcome:** the dialog closes and a batch of new
`number.nimbus_solver_*` entities exists on the Nimbus device.

---

## 7. Set your real battery and grid numbers

**This is the single most important step in this guide, and the easiest to miss.**

Numeric Solver settings live on `number.*` entities, not in the wizard.
On a fresh installation, capacity and several power limits start at placeholder
values of **0.1**. Other settings have their own defaults.
Existing installations can restore previously saved values.

An incorrect capacity can still produce an optimal solve.
Replace the placeholders before you evaluate the plan.

### Set these now

On the Nimbus device page (or Settings → Devices & Services → Nimbus → entities):

| Entity | Set it to |
|---|---|
| `number.nimbus_solver_battery_capacity_kwh` | Capacity before the configured SoH derating. See the capacity example below. |
| `number.nimbus_solver_max_charge_kw` | Real max charge power |
| `number.nimbus_solver_max_discharge_kw` | Real max discharge power |
| `number.nimbus_solver_grid_max_import_kw` | Your permitted import power in kW. Do not enter a breaker rating in amperes. |
| `number.nimbus_solver_grid_max_export_kw` | Your approved export limit |
| `number.nimbus_solver_battery_min_soc_percent` | Your floor, e.g. `10` |
| `number.nimbus_solver_battery_max_soc_percent` | Your ceiling, e.g. `100` |
| `number.nimbus_solver_efficiency_percent` | Round-trip efficiency, e.g. `90` |
| `number.nimbus_solver_battery_soh_percent` | State of health (SoH), applied to the capacity above |

![Nimbus number entities on a dashboard](images/setup/load-controls.png)

The number entities can appear together on a dashboard.
The screenshot includes optional settings from Part 2 as well as the basic limits.

### Match capacity to the SoC sensor

Nimbus calculates effective capacity as:

```text
effective capacity = configured capacity × SoH / 100
minimum stored energy = effective capacity × minimum SoC / 100
maximum stored energy = effective capacity × maximum SoC / 100
```

This follows the [capacity calculation](../custom_components/nimbus_load/solver_shared.py).
The SoC limits apply after SoH derating.

For example, `10 kWh` with `90%` SoH gives `9 kWh` effective capacity.
A `10%` to `100%` SoC range then permits `0.9 kWh` to `9 kWh`.
These are illustrative values, not recommended limits.

Use capacity and SoC values that describe the same battery measurement basis.
If capacity already includes degradation, do not apply that reduction again through SoH.
Likewise, do not subtract a reserve from capacity and apply the same reserve through
the SoC floor. Confirm the correct basis with your equipment documentation.

Use manufacturer and connection limits for power settings.
Do not convert an amperage rating to a kW limit without confirming the supply
voltage, phase configuration and applicable connection limits.

> Every one of these is live-editable forever. You never need to re-run the wizard
> to change a number.

---

## 8. Check it actually worked

Open **Developer Tools → States**. First confirm that Nimbus has produced a plan.
Then complete the input and plausibility checks below.

| # | Entity | Expected | If it is wrong |
|---|---|---|---|
| 1 | `sensor.nimbus_solver_config` | **`configured`** | Wizard did not complete: re-run [§6](#6-run-the-solver-settings-wizard) |
| 2 | `sensor.nimbus_solver_lp_status` | **`optimal`** | See [Troubleshooting](#troubleshooting) |
| 3 | `sensor.nimbus_solver_solve_seconds` | A numeric solve duration | If absent, check whether a solve completed |
| 4 | `sensor.nimbus_solver_battery_forecast` | a kW number with a long `forecast` attribute | If `unknown`, no plan yet |

![Nimbus Solver dashboard showing config, solve status and parameters](images/setup/solver-overview.png)

This dashboard shows solver status and configured parameters.
Its example capacity and solve time are not acceptance criteria.

**`optimal` describes the mathematical solve, not the installation's safety or accuracy.**
It does not prove correct inputs, physical delivery, forecast accuracy or realised
savings. Some model limits can carry penalties rather than prevent a solution.

### Sanity-check the plan is about your house

| Entity | Should look like |
|---|---|
| `sensor.nimbus_solver_current_soc_pct` | The plan's current-period SoC. Compare it with the independent battery measurement. |
| `sensor.nimbus_solver_current_load_kw` | The current-period load used in the plan, not an independent household measurement |
| `sensor.nimbus_solver_current_import_price` | The price used for the current plan period. Compare it with the source tariff. |
| `sensor.nimbus_solver_horizon_hours` | The planning horizon, which can exceed the coverage of the source forecasts |
| `sensor.nimbus_solver_binding_constraint_now` | An explanation such as "Grid export at zero (not economical right now)" |

`binding_constraint_now` explains the model's current constraint.
It helps diagnose a plan but does not confirm that hardware followed it.

### First-run acceptance gate

- [ ] Capacity, SoH, SoC limits and power limits match the equipment and connection.
- [ ] The selected inputs have correct units and plausible values.
- [ ] The load forecast comes from measured consumption, not another plan.
- [ ] The plan's `forecast` timestamps cover the current time and future periods.
- [ ] A new solve produces current results, rather than leaving an old plan on display.
- [ ] The proposed flows are plausible against independent measurements.
- [ ] `recent_errors`, `recent_warnings` and `subentry_status` have no unexplained problems.
- [ ] Device command fields are blank and external dispatch automations are disabled during observation.

If a check fails, correct it before enabling control.
A week of observation can reveal recurring patterns, but elapsed time alone is not
an acceptance test.

### A paste-ready template check

Developer Tools → **Template**, paste this:

```jinja
Config:      {{ states('sensor.nimbus_solver_config') }}
LP status:   {{ states('sensor.nimbus_solver_lp_status') }}
Solve time:  {{ states('sensor.nimbus_solver_solve_seconds') }} s
Periods:     {{ states('sensor.nimbus_solver_n_periods') }}
Horizon:     {{ states('sensor.nimbus_solver_horizon_hours') }} h
Capacity:    {{ states('number.nimbus_solver_battery_capacity_kwh') }} kWh
SoC now:     {{ states('sensor.nimbus_solver_current_soc_pct') }} %
Load now:    {{ states('sensor.nimbus_solver_current_load_kw') }} kW
Why:         {{ states('sensor.nimbus_solver_binding_constraint_now') }}
Health:      {{ states('sensor.nimbus_health_report') }} errors
```

The following is an example from one installation, not an acceptance threshold:

```
Config:      configured
LP status:   optimal
Solve time:  1.17 s
Periods:     202
Horizon:     96.3 h
Capacity:    122.2 kWh
SoC now:     97.52 %
Load now:    0.89 kW
Why:         Grid export pinned at 12.00 kW by P2P export commitment
Health:      0 errors
```

**If capacity is still a placeholder, return to [§7](#7-set-your-real-battery-and-grid-numbers).**
Check power limits too. An `optimal` result can still use incorrect configuration.

---

## 9. Put a dashboard on it

Nimbus ships three custom cards, installed automatically with the integration:
there is nothing to copy into `www/` and no resource to register by hand.

| Card | Shows |
|---|---|
| **Control Panel** (`custom:nimbus-dispatch-card-v4`) | The live plan: what it is doing now, why, prices, SoC, upcoming schedule |
| **Topology** (`custom:nimbus-topology-card`) | An animated diagram of power flowing between solar, battery, house and grid |
| **Regret** (`custom:nimbus-regret-card`) | Yesterday's score: what the plan captured vs what perfect foresight would have |

### The fastest way in

[`dashboards.md`](dashboards.md) has a **complete three-view dashboard you can
copy-paste whole**. Do that rather than building cards one at a time:

1. **Settings → Dashboards → + Add Dashboard** → "New dashboard from scratch".
   Name it **Nimbus**.
2. Open it, pencil icon (top right) → three-dot menu → **Raw configuration editor**.
3. In this new dashboard only, replace the contents with the block from
   [`dashboards.md`](dashboards.md#full-three-view-nimbus-dashboard-copy-paste),
   **Save**.

![The dashboard edit menu, with Raw configuration editor highlighted](images/setup/raw-config-editor.png)

The dashboard edit menu opens the raw YAML editor.
Use a new dashboard so that you do not overwrite an existing layout.

**Expected outcome:** three views: Control Panel, Topology, Regret.

![Nimbus Control Panel view](images/setup/control-panel.png)

The Control Panel explains the proposed dispatch and displays prices and risk settings.
Its ARMED/OFF toggle controls the helper configured as `armed_entity`.

> **The card toggle is not a built-in hardware interlock.**
> It changes an `input_boolean` helper only.
> Your external dispatch automation must check that same helper and implement a
> safe response when it turns off. It does not stop Nimbus controllable-load commands.

![Nimbus Topology view](images/setup/topology.png)

The Topology view illustrates a two-inverter system with four battery towers.
Its detail depends on the sensors and topology metadata you configure.

![Nimbus Regret view](images/setup/regret.png)

The Regret view compares actual performance with modelled alternatives.
`J_REF` is the baseline, `J_ACH` the achieved cost, and `J_STAR` the perfect-foresight cost.
`REGRET` is the gap between achieved and perfect-foresight costs.

> **"Custom element doesn't exist"?** Hard-refresh the browser (Ctrl-F5 /
> Cmd-Shift-R). The cards are registered by the integration at startup, and the
> browser caches the old resource list. If it persists, restart Home Assistant.

> The Regret view stays empty until the quality score is switched on and has had
> a full day to score: see [§15](#15-the-daily-quality-score). An empty Regret card
> on day one is expected, not broken.

### Other views worth building

The three cards above are what Nimbus ships. The reference household adds a few
ordinary-Lovelace views on top, shown here as ideas rather than as something you
have to reproduce:

![Forecaster view: combined forecast chart](images/setup/forecaster-chart.png)

This example compares forecasts and proposed battery and grid trajectories.
The `now` line separates history from future estimates.

![Shadow-mode comparison chart](images/setup/solver-shadow-comparison.png)

This example compares a proposed plan with actual operation.
It helps evaluate a change of controller, but it does not prove that the alternative
dispatch was physically achievable.

![Solver parameters and counterfactual](images/setup/solver-fees-counterfactual.png)

This example groups tuning settings with the daily counterfactual.
The counterfactual estimates another dispatch outcome from the configured model.

### If you would rather not use the custom cards

Everything Nimbus publishes is a plain sensor, so an ordinary Entities card works:

```yaml
type: entities
title: Nimbus
entities:
  - sensor.nimbus_solver_lp_status
  - sensor.nimbus_solver_current_dispatch_direction
  - sensor.nimbus_solver_binding_constraint_now
  - sensor.nimbus_solver_current_soc_pct
  - sensor.nimbus_solver_current_import_price
  - sensor.nimbus_solver_current_export_price
  - sensor.nimbus_solver_total_cost
  - number.nimbus_solver_battery_capacity_kwh
```

---

## 10. What "working" looks like after a week

- Check that plans continue to update across changes in prices, demand and solar output.
- Compare forecasts with measured consumption over several representative days.
- Investigate non-optimal results and unexplained health errors or warnings.
- Review planned costs as model estimates, not settled bills or proven savings.

`sensor.nimbus_solver_total_cost` is the model objective over its horizon.
It can include configured costs, penalties and terminal energy value.
A negative value alone does not demonstrate a financial benefit.

Proceed to Part 2 when the first-run checks pass.
Add one feature at a time and repeat the relevant checks.

---

<a id="part-2--advanced"></a>

# Part 2: Advanced

Everything below is optional. Add one thing at a time and check
`sensor.nimbus_solver_lp_status` is still `optimal` after each.

<a id="11-forecaster-settings--make-the-learning-better"></a>

## 11. Forecaster settings: make the learning better

Open **Configure → Forecaster settings** to change shared model settings.
The basic path can use the defaults. These settings apply across loads and signals.

### Context sensors

Context sensors help the model relate demand to other conditions.
For example, a load can behave differently while the battery charges.

| Field | Why bother |
|---|---|
| Temperature sensor | Additional context for weather-sensitive loads |
| Temperature forecast sensor | Select your weather entity. Nimbus calls `weather.get_forecasts`. |
| Humidity sensor | Smaller effect, same idea |
| Battery / Grid / Solar power sensors | System context |
| Curtailment switch | Only if you have a load run specifically to soak up curtailed solar |

> **"Never an optimizer's own plan/forecast"** appears on three of these fields
> and it is not boilerplate. Measured sensors only.

### Tuning

| Field | Default | Change it when |
|---|---|---|
| Forecast horizon (hours) | 48 | Longer forecasts need more computation. Check coverage against the Solver horizon. |
| Retrain at this hour | 3 | Choose a quiet hour for model training |
| Days of history to train on | 30 | More = steadier, but slower to adapt to a genuine habit change |
| **Training data source** | Recorder history | **See below** |
| Hybrid: recent days | Not applicable to Recorder-only mode | Used when the source is Hybrid |

> **Match the training window to available history.**
> Recorder retention limits the full-resolution data available for training.
> Requesting 30 days cannot restore records purged after 10 days.
>
> Long-term statistics use hourly summaries where suitable statistics exist.
> They reduce detail for short appliance cycles.
> Hybrid mode combines recent full-resolution data with older statistics.
> Neither option creates history that was never recorded.

<a id="12-loads--learn-one-appliance-at-a-time"></a>

## 12. Loads: learn one appliance at a time

Select **+ Add → Load** for each appliance or circuit you want to forecast.
Examples include hot water, a pool pump and an EV charger.

| Field | Notes |
|---|---|
| **🔴 Power sensor to learn from** | One real circuit/appliance power sensor |
| Fixed schedule start / end hour | Only for a load on a real fixed timer. **24-hour decimal**: `12.5` = 12:30pm |
| Expected power while running (kW) | Used with a schedule. Enter the equipment's actual power. |

**Expected outcome:** `sensor.nimbus_<name>_forecast` per load, plus the whole-house
roll-up `sensor.nimbus_household_load_total_forecast`.

> **Load vs Power Signal.** A **Load** is one appliance or circuit. A **Power
> Signal** is a whole-system quantity (total battery power, total solar, the grid
> meter). Same engine, different intent. If unsure: one appliance → Load.

<a id="13-controllable-loads--let-nimbus-switch-things"></a>

## 13. Controllable loads: let Nimbus switch things

**This is the one place Nimbus physically commands your hardware.** Everything else
only reads and plans.

**+ Add → Controllable Load.** Pick a **Kind**:

| Kind | Meaning | Use for |
|---|---|---|
| **Sheddable** | Can be reduced under price pressure, down to an optional floor | Pool heater, a dump load |
| **Deferrable** | Has an energy target and a deadline. Nimbus chooses the planned timing. | Hot water, EV charging |
| **Thermal** | Plans against a temperature constraint. Actual temperature depends on measurement and hardware response. | A tank with a temperature target |

Complete the fields for your chosen Kind. Leave unrelated fields blank.

> **WARNING: Do not use a Nimbus schedule as the only protection for hot-water hygiene.**
> A missed temperature target can create a health risk.
> Retain the equipment's independent hygiene controls and applicable temperature requirements.
>
> **CAUTION: Retain the equipment's independent operating limits.**
> Incorrect commands or rapid cycling can damage equipment.
> Software hold times and activation caps do not replace manufacturer protections.

### The field that makes it act

**"Device to command"**: the entity Nimbus switches:

| Domain | What Nimbus does |
|---|---|
| `switch` | `turn_on` / `turn_off` |
| `water_heater` | `set_operation_mode`: `performance` for on, `eco` for off |
| `climate` | `set_hvac_mode`: your configured mode for on, `off` for off |

Other domains log a warning and receive no command.
Before assigning a device, confirm that it supports the exact modes in this table.
Not every `water_heater` supports `performance` and `eco`.

> **Leave "Device to command" blank during observation.**
> Assigning it enables Nimbus to command the load.
> Verify supported modes, equipment limits and the stop procedure first.

> **For a `climate` device you must also set "HVAC mode to command when ON".**
> Nimbus never guesses it from the device's supported modes. Left blank, ON commands
> are skipped with a warning. OFF still works.

### Safety rails worth setting

| Field | Does |
|---|---|
| Minimum hold between commands (minutes) | Stops rapid cycling |
| Max activations per day | Hard cap, whatever the plan wants |
| Re-send command after (minutes) | Re-send interval, default 15. `0` disables re-sends. Re-sends do not count against the daily cap. |

### Deferrable, the common case

```
Kind:                   Deferrable
Device to command:      [leave blank for observation]
Deferrable: max power:  3.7      kW
Deferrable: target:     10       kWh by the deadline
Earliest start:         0        (midnight)
Deadline:               6.5      (6:30am)
Done condition:         [configure the actual sensor and threshold]
```

This example describes a planning target, not a safe hot-water operating procedure.
Configure the done-condition sensor and threshold for your equipment.
Do not infer that an energy target proves a hygiene temperature was reached.

Two optional refinements:

- **"Run as soon as it's this cheap"**: allows early delivery below your price threshold.
- **"Don't wait unless it saves at least $X"**: sets the saving required to justify a delay.

The quoted labels match the interface. Select values appropriate to the appliance.

<a id="14-battery-participants--a-second-battery-or-an-ev"></a>

## 14. Battery participants: a second battery or an EV

**+ Add → Battery Participant.** Only for an **additional** battery. Your main
household battery is already configured in Solver settings and is always the `home`
participant: do not add it again.

Core fields: name, capacity, SoC sensor, power sensor, **"Positive reading means
charging"** (check a real reading while it is charging: conventions differ by
vendor), max charge/discharge power, min/max SoC, efficiency.

EV-specific fields worth knowing:

| Field | Does |
|---|---|
| Availability sensor | A `binary_sensor` for "plugged in" / "at home". While off, this participant cannot charge or discharge at all |
| Departure hour + Required SoC by departure | Set both to define the planning target. Check availability, achievable charging time and actual SoC before departure. |
| Live charge-limit entity | Uses the vehicle's charge limit instead of the configured Max SoC for that solve |
| Trip calendar | Include distance in the event title, such as "Trip to Brisbane 120 km". Events without distance are ignored. |
| Shared charger group + max kW | Constrains combined planned charging power for a shared charger |

## 15. The daily quality score

Nimbus can score **yesterday's real dispatch against a perfect-foresight oracle**
and publish `sensor.nimbus_solver_quality_report`. The headline number is **EPR**:
the percentage of theoretically-available value you actually captured. This is what
feeds the **Regret** dashboard view.

**To turn it on**, fill these three in Solver settings screen 3: **all three, or
there is no score at all**:

1. Household load forecast sensor (already set in Part 1)
2. **Real, measured solar power sensor**: a measurement, not a forecast
3. **Real, measured net battery power sensor**

Plus one checkbox that matters enormously:

> **Check the measured battery sensor's sign during known charging and discharging.**
> Tick **"Tick ONLY if your battery sensor reports positive = charging"** only when
> that describes the source. Leave it unticked for positive discharge.
> An incorrect sign corrupts the score. A negative EPR alone does not prove a sign error.

**Expected outcome:** after the next midnight, `sensor.nimbus_solver_quality_report`
holds a percentage plus a `history` attribute with per-day detail, and the Regret
card fills in.

> **Treat scores as conditional on their inputs.**
> Incomplete measurements, tariff data or settlement data can affect the result.
> A negative score can also represent underperformance against the baseline.
> Check the report details before changing the sign setting.

## 16. The topology diagram

Use **Configure → Topology diagram settings** for the card's display settings.
Add **Power Source**, **PV String** and **Battery Tower** entries for its layout.
These entries describe the diagram, not Solver equipment limits.

Nimbus detects configured power sources and Loads for the diagram.
Use the "today's kWh" fields for your daily energy sensors or `utility_meter` helpers.

Add one **Power Source** per inverter.
Add **PV String** and **Battery Tower** entries for additional detail.

## 17. Tuning knobs

All live `number.nimbus_solver_*` entities, editable any time, no wizard, no restart.

| Group | Entities |
|---|---|
| Battery | `capacity_kwh`, `soh_percent`, `min_soc_percent`, `max_soc_percent`, `max_charge_kw`, `max_discharge_kw`, `efficiency_percent` |
| Grid | `max_import_kw`, `max_export_kw`, `flat_fee_rate`, three `network_fee_*` blocks, `network_fee_default_rate` |
| Economics | `charge_cost`, `discharge_cost`, `degradation_cost_per_kwh`, `salvage_value` |
| Risk | `risk_aversion`, `import_price_risk_aversion`, `export_price_risk_aversion` |

The two most useful:

- **`salvage_value` ($/kWh)**: what energy left in the battery at the end of the
  horizon is worth. Too low and Nimbus happily empties the battery at the horizon
  edge. Compare the resulting plan before changing this value again.
- **`degradation_cost_per_kwh`**: a real wear cost per kWh cycled. Raise it if
  Nimbus cycles the battery harder than you are comfortable with for small gains.

> **Change one at a time**, and give it a day. These interact.

## 18. Optional inputs

Everything you left blank in Part 1, in rough order of usefulness:

| Field | Add it when |
|---|---|
| 2nd/3rd price or solar source | You have an independent forecast. Differences between sources inform the model's uncertainty. |
| Richer per-5-minute price forecast | Your retailer publishes one |
| Regional wholesale spot forecast / history | Australian NEM only: extends the price plan beyond your retailer's horizon |
| Whole-house cross-check sensor | A diagnostic comparison for the first forecast period, not a dispatch input |
| DNSP dynamic import/export limits | You are on a dynamic connection (e.g. Open Dynamic Export) |
| Price-spike alert entity | Your retailer has a named spike product. The plain $/kWh threshold works without it |
| P2P/VPP settlement history | Your retailer pays a real export bonus: improves score accuracy |
| Price-event simulation sensor | A test input enabled by `switch.nimbus_solver_price_event_enabled`. See the warning below. |
| Weather forecast sensor | Purely for the dashboard's temp/humidity chart |

**Disable hardware control before testing simulated prices.**
An enabled simulation changes the plan that external automations can consume.
End each simulation window with an explicit `0`.
Otherwise, its step value continues into later periods.

## 19. Services you can call

From Developer Tools → Actions, or any automation:

| Service | Does |
|---|---|
| `nimbus_load.retrain` | Force an immediate retrain. Blank entity = everything |
| `nimbus_load.solve_now` | Run one solve immediately: useful on a real price change rather than guessing a cron time |
| `nimbus_load.compute_quality_report` | Score any past window on demand |
| `nimbus_load.rescore_history` | Re-score the last N days after a formula change. **Each day is a full oracle solve: start with 2 or 3** |
| `nimbus_load.flex_telemetry_record` | Returns a nem-flex-telemetry v2.0 record. Needs `switch.nimbus_solver_flex_signals_enabled` on, which costs real solve time |

## 20. Actually driving your battery with the plan

Nimbus publishes the plan. An external automation translates it into
device-specific commands. This section defines the checks that automation needs,
not a ready-to-use controller.

### Plan power and direction

The state of `sensor.nimbus_solver_battery_forecast` is planned net battery power
for the current period, in kW at the AC side of the inverter.
Its sign is fixed:

| Value | Meaning |
|---|---|
| Positive | Discharge |
| Negative | Charge |
| Zero | No net battery power |

The plan attributes declare `battery_kw_side: AC` and
`battery_kw_sign_convention: positive_discharge_negative_charge`.
These values come from the [plan publisher](../custom_components/nimbus_load/solver_publish.py).
Compare the proposed direction with `sensor.nimbus_solver_current_dispatch_direction`.
Your inverter's measurement and command signs can differ from Nimbus's sign.
Verify the conversion against the hardware documentation.

### Observe without enabling control

1. Keep external dispatch automations disabled.
2. Leave every controllable load's **Device to command** field blank.
3. Turn on `switch.nimbus_solver_dispatch_dry_run`.
4. Review `sensor.nimbus_solver_dispatch_dry_run` in History.
5. Compare the recorded plan with actual measurements.

**The dry-run switch is a recorder, not a global stop switch.**
It does not disable external automations or controllable-load commands.
The dashboard's `armed_entity` helper also needs an explicit check in your automation.
See the [observation path](../custom_components/nimbus_load/solver_runtime.py)
and [card configuration](dashboards.md) for these separate mechanisms.

### Control-readiness gate

Before a supervised trial, verify each item:

- [ ] The first-run acceptance checks in [§8](#8-check-it-actually-worked) pass.
- [ ] Only one controller has authority to command this equipment.
- [ ] The automation requires explicit arming and has a tested stop action.
- [ ] Unknown, unavailable, non-numeric and stale plans cannot issue a power command.
- [ ] A watchdog detects stale plans even when no new state event arrives.
- [ ] The automation checks plan status, current-period timestamps and source availability.
- [ ] Device-side power, SoC and connection limits remain enforced independently.
- [ ] Sign conversion, units and command acknowledgement have been checked.
- [ ] A deadband, direction debounce and minimum dwell prevent rapid mode changes.
- [ ] Restart, communication failure and disarming invoke a defined hardware-safe fallback.

Do not convert invalid data to `0` and treat it as a valid plan.
Do not rely only on a debounce of the raw power state.
Frequent plan changes can restart that timer without producing an accepted command.
Choose timing and stale-data limits for your equipment and observed update cadence.

### First supervised trial

1. Confirm that the independent equipment protections are active.
2. Test the stop action before enabling scheduled commands.
3. Enable one control path at a time.
4. Observe actual power, SoC and command acknowledgement.
5. Stop the trial if measurements do not match the intended command.

Use your equipment's approved fallback procedure after stopping.
Disabling an automation does not necessarily clear the last inverter command.

---

# Reference

## Troubleshooting

### `sensor.nimbus_solver_config` is not `configured`

The Solver wizard did not complete. Re-run **Configure → Solver settings** all the
way through screen 3.

### `sensor.nimbus_solver_lp_status` is not `optimal`

| Status | Meaning | Fix |
|---|---|---|
| `infeasible` | Constraints contradict each other | Usually min SoC > max SoC, an impossible deferrable target/deadline, or a grid limit of 0. Check your `number.nimbus_solver_*` values |
| `unknown` / absent | No usable solve result is available | Check input availability and logs, including dependency installation errors |
| missing entirely | Solver never started | Confirm `sensor.nimbus_solver_config` is `configured` |

### The plan looks wrong / nonsensical

In order:

1. Check capacity, SoH and power limits against [§7](#7-set-your-real-battery-and-grid-numbers).
2. Remove individual circuit forecasts if you intend to use the household source.
3. Check that the load input is a Power Signal forecast, not the Solver output.
4. Read `sensor.nimbus_solver_binding_constraint_now`.
5. Compare units, timestamps and values with independent source measurements.

### A forecast sensor reads `unknown`

Check progress while the initial model trains:

- Does the **source** sensor have real Recorder history? Nimbus cannot learn from
  nothing.
- Is `purge_keep_days` shorter than "days of history to train on"? See the purge
  trap in [§11](#11-forecaster-settings--make-the-learning-better).
- After checking the inputs, call `nimbus_load.retrain` once and inspect the log.
- Check `sensor.nimbus_health_report` for `subentry_status` and `never_trained`.
  These show training progress and entries without a trained model.

### A controllable load is not being switched

1. Is **"Device to command"** actually set? Blank means plan-only, by design.
2. Is the device a `switch`, `water_heater` or `climate`? Nothing else is supported,
   and an unsupported domain logs a warning.
3. `climate` device → is **"HVAC mode to command when ON"** set? Blank means ON
   commands are skipped.
4. Hit the **max activations per day** cap?

### The dashboard cards do not render

Hard-refresh the browser (Ctrl-F5 / Cmd-Shift-R). The cards are registered by the
integration at startup and the browser caches the old resource list. If it persists,
restart Home Assistant.

### The quality score is missing or impossible-looking

- **Missing**: all three real-measurement sensors must be set: load, real solar
  power, real battery power. Any one blank means no score at all.
- **Unexpected**: verify measured power signs, units, data coverage and tariff or
  settlement data. A negative EPR alone is not proof of a configuration error.

### Nothing works after an update

Restart Home Assistant after an update.
If the problem persists, record both versions and the error before changing configuration.

## Every check in one place

```jinja
{# Developer Tools → Template: paste this whole block #}
Config:      {{ states('sensor.nimbus_solver_config') }}          {# want: configured #}
LP status:   {{ states('sensor.nimbus_solver_lp_status') }}       {# want: optimal #}
Solve time:  {{ states('sensor.nimbus_solver_solve_seconds') }} s
Capacity:    {{ states('number.nimbus_solver_battery_capacity_kwh') }} kWh  {# NOT 0.1 #}
Max charge:  {{ states('number.nimbus_solver_max_charge_kw') }} kW
Max dischg:  {{ states('number.nimbus_solver_max_discharge_kw') }} kW
SoC now:     {{ states('sensor.nimbus_solver_current_soc_pct') }} %
Load now:    {{ states('sensor.nimbus_solver_current_load_kw') }} kW
Solar now:   {{ states('sensor.nimbus_solver_current_solar_kw') }} kW
Import now:  {{ states('sensor.nimbus_solver_current_import_price') }} $/kWh
Direction:   {{ states('sensor.nimbus_solver_current_dispatch_direction') }}
Why:         {{ states('sensor.nimbus_solver_binding_constraint_now') }}
Horizon:     {{ states('sensor.nimbus_solver_horizon_hours') }} h
Plan cost:   {{ states('sensor.nimbus_solver_total_cost') }}      {# model objective, not a bill #}
Health:      {{ states('sensor.nimbus_health_report') }} errors
Load source: {{ state_attr('sensor.nimbus_household_load_total_forecast','load_forecast_source_used') }}
```

That last line is worth knowing about: it tells you **which** load-forecast field
actually won, which settles the silent-override trap in one read.

## Removing Nimbus

1. Disable external automations that consume Nimbus plans.
2. Remove controllable-load command assignments.
3. Return each device to its approved local or fallback mode.
4. Verify the actual device state.
5. Open **Settings → Devices & Services → Nimbus**.
6. Select the three-dot menu, then **Delete**.
7. Remove Nimbus from HACS if you also want to remove its integration files.
8. Remove obsolete dashboard cards, helpers and automations you created separately.

Deleting Nimbus is not a hardware stop command.
Recorder history and external configuration can remain after removal.

---

## Getting help

- **Bugs**: [open an issue](https://github.com/code-imstillalive/nimbus/issues).
  Include the Nimbus and Home Assistant versions, installation architecture,
  reproduction steps, expected result and actual result.
  Add the relevant template output and redacted error messages.
- **Field-by-field reference**: [`configuration-reference.md`](configuration-reference.md)
- **Dashboards**: [`dashboards.md`](dashboards.md)
- **What every entity means**: [`entities.md`](entities.md)

**Before posting publicly, review all diagnostics and screenshots.**
Remove tokens, credentials, addresses, location details and unnecessary household
or commercial data. Do not attach a complete configuration or `.storage` directory.

If the guide and your installation disagree, report both versions and the exact
step. The mismatch can be a documentation error, a software defect or a release
difference. Do not assume that either side is correct without checking.
