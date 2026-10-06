# Two-step setup: pick your devices, Nimbus builds everything

**Status:** proposal for review ([#1574](https://github.com/code-imstillalive/nimbus/issues/1574)). Nothing here is built yet.

**The bar, in the household's words (2026-10-06):**

> *"it has to be very very clever and simple for the users... whatever we do... we must make it a breeze for the user... not a nightmare Chris has experienced"*

Every decision below is judged against that. Where a choice trades capability for simplicity, simplicity wins at the basic level and the capability moves to advanced.

**Revised 2026-10-06 after Mark's review:** *"I don't like the 5 sensor construct, I think they should be devices with entities under each; power, energy, limits, forecasts."*
The unit of setup is now the **device**, not the sensor. The user picks the 3-4 things they recognise ("my GoodWe", "my Smappee", "my LocalVolts account"), and Nimbus resolves every entity under each one: power, energy, limits and forecasts. The earlier "5 sensors" wording is superseded throughout.

---

## 1. What a user faces today (measured on `main`, 9d8e20d)

| surface | what it holds |
|---|---|
| Add integration | one empty hub step |
| Configure → **Forecaster settings** | 12 fields, none required |
| Configure → **Switchboard** | daily energy and price sensors |
| Configure → **Solver: Battery** | 2 fields (SoC required) |
| Configure → **Solver: Grid Prices** | 9 fields (import and export price required) |
| Configure → **Solver: Sources** | 16 fields (solar forecast and load forecast required) |
| **Add** → Load / Power Signal / Power Source / PV String / Battery Tower / Controllable Load / Battery Participant | 8 / 2 / 3 / 4 / 4 / 5 / 14 fields, one item at a time |
| **Dashboard numbers** | 56 `number.nimbus_solver_*` settings, including **battery capacity, max charge and max discharge**, which are not in the wizard at all |

**What already exists and is worth keeping:**
- Energy Dashboard suggestions: battery SoC and tariff prices (#1067), and switchboard daily sensors (#554).
- Device-class suggestions when exactly one candidate exists: temperature, humidity, SoC.
- `sensor_discovery.py`'s rule: exactly one survivor or ask, never guess.
- LocalVolts v2 detect, pre-fill and notify (#1561).
- The battery power-sign detector (`solver_inputs/sign_convention.py`, #1241).
- Refusal of energy units on power fields (#1562).
- The shared unit converter (#1570).

**What a careful tester hit on 2026-10-06 (#1526),** every item silent:

| what he saw | why |
|---|---|
| no solar forecast | the wizard's solar sensor is only a feature; a forecast needs a separate "Add → Power Signal → Solar" the wizard never mentions |
| no battery forecast | same, for the battery |
| blank Control Panel history | the card's battery field is filled from the Solver's battery power sensor, which the wizard left empty |
| 6 of 10 circuits never trained | idle history dropped (#1556, fixed in 0.94.440), shown only as a 0 in diagnostics |
| a Wh total used as solar power | accepted by the form, read as kW (#1562) |
| chart shading flattened | a W battery sensor read as kW (#1570) |
| days 3-4 planned on one flat load | Forecaster horizon 48 h against a 96 h plan (#1566) |
| the pool counted twice | a Load and the circuit it sits on, with no parent link (#1574 Q6) |

The pattern: **each physical sensor is asked for in up to four places, nothing links them, and a gap is only visible to someone who reads diagnostics.**

---

## 2. The new flow at a glance

```
Add Nimbus
  │
  ├─ 1. Scan            (automatic, no screen; a few seconds)
  │
  ├─ 2. "Your home"     ONE screen: your devices (battery, inverters, grid, house), all pre-filled
  │
  ├─ 3. "Your prices"   ONE confirm screen, pre-filled from what was detected
  │
  └─ 4. Build           (automatic) → one summary of everything created
        │
        └─ Health        Home Assistant Repairs entries with a Fix button,
                         for anything still missing or wrong

Configure later → Advanced, grouped by what you want to do, never by Nimbus's internals
```

A user with a typical install, an Energy Dashboard already configured and a supported battery integration, **presses Submit twice** and gets the Forecaster, Solver, Topology, Control Panel and Regret working, with no "Add" steps.

---

## 3. Step 1: Scan (automatic)

The scan runs before any screen is shown. It finds candidates. It saves nothing.

### 3.1 Where candidates come from, in order of trust

| source | gives | rule |
|---|---|---|
| **Energy Dashboard** (`energy` manager prefs) | battery, solar and grid energy sources; battery SoC (`stat_soc`); tariff price entities; individual devices | each source identifies a **device**. Its energy entity is kept *as energy* (§3.4), never used as power (#1067's reason). |
| **The device registry** | every candidate device's entities, grouped by role: power, energy in/out, SoC/SoH, limits, temperature, switches | a role is filled only when exactly one entity of that role exists on the device. Several or none means ask, and say why. |
| **Known integration profiles** | role-tagged entities for supported inverters and batteries, and capacity and limit entities where the integration publishes them | matched by integration and unique_id, never by entity name (the #1561 pattern). Starts with the integrations testers actually run; each profile ships with a captured fixture. |
| **Pricing profiles** | import, export, forecast, P2P (#1550 and its provider sub-issues #1578-#1583) | the same detect → pre-fill → notify shape; LocalVolts v2 is done |
| **Solar forecast integrations** | Solcast or Forecast.Solar forecast entity | exactly-one rule |
| **Device class, house-wide** | a fallback for any role still empty | exactly-one rule, per `sensor_discovery.py` |

### 3.2 Checks every candidate passes before it is offered

1. **Kind.** A power field only takes a power unit; Wh/kWh is refused (#1562). SoC must be `%` with `device_class: battery`.
2. **Alive.** The current state is numeric, not `unknown`/`unavailable`, and has updated within the last hour.
3. **History.** The recorder holds enough to train. This is shown, not blocking: *"6 days of history; forecasts improve as it grows."*
4. **Not already used for a different role.** The same sensor is never offered as both battery and grid.

### 3.3 The clever part: the devices check each other

Physics ties them together: **grid ≈ load − solar − battery** (with this project's battery sign, positive = discharge). Once candidates exist for all four power roles, the scan reads the last hour of history for each and fits that balance:

| what the residual shows | what it means | what the user sees |
|---|---|---|
| near zero | the devices agree | ✅ |
| fits only if one sensor's sign is flipped | that sensor uses the opposite convention | the sign option pre-set, with *"your battery reads positive when charging; Nimbus will flip it"* |
| fits only if one sensor is scaled ×1000 | a W/kW mix-up or a mislabelled unit | that field flagged with the factor |
| fits only without solar, or solar looks like one inverter of two | solar is partial (e.g. Fronius and GoodWe both produce) | *"solar looks like one inverter of two; pick a total, or Nimbus can add the two for you"* |
| no fit | the sensors cover different parts of the house | named, never guessed at |

This replaces "the user discovers the mistake a week later in a chart" with "the form says which sensor is wrong before Submit". It reuses #1241's detector for the battery sign and extends the same idea to the other three.

### 3.4 Energy entities become a second witness

Because each device brings its **energy** counters as well as its power, every power reading gets an independent check. A device's power integrated over a day should match the rise in its own energy counter, to within losses:

- **They match:** the power sensor is trusted.
- **Off by ×1000:** a unit mislabel, named.
- **Off by a steady ~5%:** recorded as that sensor's own calibration (the reference household's battery power reads 5.3% under its BMS counter). Shown, not corrected silently.
- **Power reads 0 while energy climbs:** a stale or wrong power entity, named.

Energy entities also give the quality report and Regret a measured daily total to score against, rather than one rebuilt from integrated power.

---

## 4. Step 2: "Your home" (one screen of devices)

The screen lists **devices**, each pre-filled and each expandable to show the entities Nimbus resolved under it:

| device role | the user picks | Nimbus resolves under it | shown on the row |
|---|---|---|---|
| **Battery** (one or more) | e.g. *"GoodWe battery"*, *"SigEnergy plant"* | power (signed), energy charged/discharged, SoC, SoH, **capacity**, **max charge/discharge**, temperature | live power and SoC (*"charging 4.2 kW, 63%"*); the sign as detected |
| **Solar / inverter** (one per inverter) | e.g. *"Fronius Symo"*, *"GoodWe inverter"* | PV power, PV energy, inverter AC limit; the **forecast** device (Solcast or Forecast.Solar) attached to it | live PV per inverter, and the **total**, so two inverters are explicit, never partial |
| **Grid** | the meter device | import/export power, import/export energy, connection **limits** | live import/export |
| **Tariff / prices** | the provider device or account (LocalVolts, Amber...) | import, export, price **forecast**, P2P matched rate and settlement (§5) | current prices and forecast coverage |
| **House** | the consumption meter device (e.g. Smappee) | house power, house energy, and its **circuits** as candidate loads (§4.1) | live house load |

Each row carries its source (*"from your Energy Dashboard"*, *"GoodWe integration"*) and the §3.3/§3.4 check result.

**Limits come from the device when it publishes them:** battery capacity, charge/discharge limits, the inverter's AC limit and the grid connection limit. When a device does not publish one, that single number is asked for on the row, **required** and never a placeholder default (the old "0.1 kWh" trap). It's sanity-checked against the largest value seen in history.

Rules for this screen:
- **No empty-looking field.** A field Nimbus could not fill carries a one-line reason: *"two power sensors on your inverter; pick one"*.
- **Optional, but recommended:** the grid device. Without it the balance check in §3.3 has nothing to compare against, so leaving it out is allowed and explained.
- **Multiple of one kind are normal:** two inverters, a home battery plus an EV, or two meters. Each is its own row; the Solver sums inverters, and treats batteries as separate participants (the existing #563 model).
- **Nothing else** appears at this level. Temperature, weather, circuits, EVs and fees are all advanced, and most are detected anyway.

### 4.1 Loads, offered on the same screen

Below the devices, a pre-ticked list: *"Also forecast these 9 loads? (from your Energy Dashboard's individual devices and your Smappee circuits)"*.

- **Candidates:** each Energy Dashboard individual device (`device_consumption`), resolved to the power sensor on the same device. The kWh total is never used, the same rule as §3.1. Circuit-level power sensors on the house-load device are added too (Smappee, Emporia, IoTaWatt and similar).
- **Pre-ticked, one tap to accept:** unticking removes a load. Loads are optional at the basic level, so none is created without this confirmation.
- **Children of Whole House** (§8.2): the Solver's load still comes from Whole House, so these never change the plan. They add per-appliance forecasts, the Topology breakdown, and the groundwork for controllable loads.
- **A load whose device also has a switch or climate entity** (a pool pump, a hot-water relay) is remembered, and pre-filled later under Advanced → "Devices Nimbus can switch" without asking again.
- **Overlaps are resolved here:** a candidate that sits inside another candidate (the pool inside its circuit) is nested under it rather than listed beside it.

---

## 5. Step 3: "Your prices" (one confirm)

Pre-filled from the pricing profile that was detected: LocalVolts v2 today, the others as #1578-#1583 land.

- **Shown:** the provider and account, current import and export, forecast coverage (*"forecast to 14:00 tomorrow"*), and P2P if the provider has it.
- **Fees:** LocalVolts' Flex Up already includes network and LocalVolts fees, so they are set to 0 and greyed out, with the reason (#1564). Other providers ask only if their price excludes them.
- **No provider found:** two fields (import and export price), plus a link to the manual tariff helper.

---

## 6. Step 4: Build (automatic), and what it creates

One press of Submit creates or fills, **only where empty**:

| from | creates or fills |
|---|---|
| House device: power | **Whole House** Power Signal; the Solver's load forecast (its forecast entity); the whole-house cross-check |
| Solar/inverter devices: PV power (summed across inverters) | **Solar** Power Signal; the Solver's live solar power; the Forecaster's solar feature; one Topology Power Source per inverter |
| Battery device: power | **Battery** Power Signal; the Solver's battery power and sign; the Forecaster's battery feature |
| Grid device: power and limits | **Grid** Power Signal; the Forecaster's grid feature; the Solver's grid import/export limits |
| Battery device: SoC, SoH | the Solver's SoC and SoH |
| Battery device: capacity and limits | `number.nimbus_solver_battery_capacity_kwh`, `…max_charge_kw`, `…max_discharge_kw` |
| Every device: energy counters | the §3.4 power-vs-energy check, and measured daily totals for the quality report and Regret |
| Solar forecast | the Solver's solar forecast source |
| Device registry | a **Topology** Power Source per inverter device (PV and battery attached by device), so the diagram draws itself (#575, #1528) |
| Everything above | **Regret** and quality scoring, which need nothing more |
| Dashboards | the Forecaster, Solver and Control Panel tabs, as sections views with titles (#1558). The Control Panel card reads the Solver's sensors whenever its own fields are blank (#1574 stage 1). |
| Dispatch | **dry-run on, live dispatch off.** Setup never enables control. |
| Energy Dashboard **Individual devices** (`device_consumption`), and circuit power sensors on the house-load device | **Loads**, one per device, **only those the user left ticked** on "Your home" (§4.1), each a **child of Whole House** (§8.2), so nothing is counted twice |

Then a **single summary notification**:

> **Nimbus is set up.** Created: Whole House, Solar, Battery and Grid forecasts (training now, about 3 minutes). Solver: planning from LocalVolts v2 prices and Solcast. Topology: GoodWe inverter with battery; Fronius inverter. Nothing has been sent to your battery: dry-run is on. **Next:** forecasts appear on the Forecaster tab when training finishes.

---

## 7. Health: gaps become Repairs with a Fix button

Notifications are easy to miss and do not go away when the problem is fixed. Home Assistant's **Repairs** (issue registry, with fix flows) is built for exactly this: an entry appears under Settings → Repairs, explains itself, and its **Fix** button opens the one screen that resolves it. It clears itself when the condition clears.

| condition | Repair says | Fix opens |
|---|---|---|
| a forecast has not trained | *"Hot Water has no forecast yet: 0 usable hours of history. It needs about 5 days."* with the real reason from `last_retrain_error` | that Load, or nothing if waiting is the fix |
| a power field reports an energy unit | *"Solar sensor is a Wh total, not power"* | the field |
| fees set on top of Flex Up | (#1564's text) | Solver prices |
| a sign mismatch found after setup | *"Battery power and SoC disagree on direction"* | the sign option |
| a Load and the circuit it sits on are both summed | *"Pool Pump is counted inside Circuit Power 3"* | the parent link (§8.2) |
| a Forecaster horizon shorter than the plan | can no longer happen (#1566) | n/a |
| a Solver input missing | *"The Solver has no load forecast"* | the field, pre-filled |

The existing persistent notifications for these move to Repairs. The startup summary in §6 stays a notification, because it is news, not a problem.

---

## 8. Advanced, grouped by what the user wants to do

The ~30 remaining settings regroup by goal, each screen pre-filled by the same scan:

| group | holds | the clever bit |
|---|---|---|
| **Circuits and appliances** | per-circuit Loads | **bulk add:** *"We found 9 power sensors on your Smappee device. Add them as loads?"*, multi-select, instead of 9 separate Add flows. Each new load gets a **parent** (§8.2). |
| **Devices Nimbus can switch** | controllable loads (pool, hot water, dryer...) | offers the switch or climate entity on the same device as the load's power sensor |
| **EVs** | battery participants | pre-filled from car integrations' SoC, odometer and charger entities, by device |
| **P2P and tariffs** | P2P blocks, fees, secondary price sources | blocks defaulted from the provider's history where it has one |
| **Weather** | temperature, humidity, weather forecast | auto-filled when exactly one exists today; offered as a pick otherwise |
| **Fine tuning** | smoothing weights, costs, salvage, horizons | today's dashboard numbers, unchanged, behind one link |

### 8.1 Rule for every advanced field

**An advanced value overrides what basic created; it never creates a second copy.** Setting a different solar sensor in Advanced repoints the Solar signal, the Solver field and the topology source together.

### 8.2 Parent/child loads (#1574 Q6)

Bulk-added circuits are children of Whole House; a Load added on a circuit sensor's device is offered as a child of that circuit. A summed load then subtracts children from parents, and the Topology nests them. The alternative, refusing to sum overlapping sets and naming them, is simpler; the choice is open (question 6).

---

## 9. Existing installs

- On upgrade, the scan runs once in **fill-gaps mode**: it never overwrites, and creates only what is missing. The summary notification says exactly what changed.
- **The reference household sees no change:** every field is already set. That's pinned by a test that runs the build against its configuration snapshot and asserts zero changes.
- Chris's install, from his 6 Oct diagnostics, would gain the Solar and Battery signals, the Solver's battery power, solar power and cross-check, and a Topology that draws itself. That's a second fixture test.

---

## 10. Rules that hold everywhere

1. **Never overwrite** a value the user set.
2. **Never silent.** Everything created is listed; everything missing is a Repair.
3. **One source per physical sensor**, derived everywhere else.
4. **Refuse the wrong kind at the door:** energy for power, doubled fees, unit mismatches.
5. **Exactly one, or ask.** No sorting-order guesses (`sensor_discovery.py`'s rule).
6. **Setup never enables dispatch.**
7. **Fewer fields, not moved fields.** A field that can be derived does not exist at the basic level.

---

## 11. How it is tested

- **Setup-outcome fixtures:** each takes a real diagnostics snapshot plus captured states and Energy Dashboard prefs, and asserts what the build creates. Snapshots: the reference household, Chris (6 Oct), Mark's SigEnergy install, and devhub. Real data, not hand-written mirrors (#954's lesson).
- **The balance check (§3.3):** synthetic histories for each failure row (flipped sign, ×1000, partial solar, unrelated sensors).
- **Repairs:** each condition appears, its Fix flow opens the right step, and it clears when the condition clears.
- **No-change test** for the reference household (§9).
- **Devhub:** run a fresh install end to end, from Add Nimbus to a first optimal solve, and time it.

---

## 12. Delivery, each stage released and validated on devhub

| stage | delivers | user-visible result |
|---|---|---|
| **1. Fill gaps** | create missing Power Signals from the Forecaster's battery/solar/grid; fill empty Solver power fields; cards fall back to the Solver's sensors; summary notification. **Uses only entities the user already chose and adds no new construct**; a bridge for existing installs, consistent with the device model because every filled entity belongs to a device stage 3 then adopts | Chris's install fixes itself on upgrade |
| **2. Health as Repairs** | §7 | every silent gap becomes a Repair with a Fix button |
| **3. "Your home" + scan** | §3-§4, starting with Energy-Dashboard device lookup and the balance check | new installs: one screen |
| **4. "Your prices" + build** | §5-§6, battery numbers in the wizard | new installs: two Submits |
| **5. Advanced regroup** | §8, bulk circuit add, parent/child | the ~30 settings, grouped and pre-filled |
| **6. More profiles** | inverter, battery and pricing profiles, one per PR, each with a captured fixture | wider "it just works" coverage |

Stages 1 and 2 need no new screens and fix today's testers first.

---

## 13. Open questions (for Mark)

1. ~~Are these the right five?~~ Answered: devices, with power, energy, limits and forecasts under each. Remaining: is the grid device optional at basic, as proposed in §4?
2. Is "same device as the Energy Dashboard source" a safe enough link for pre-filling a power sensor, on SigEnergy and multi-inverter installs?
3. Should basic create Battery and Grid Power Signals, or only Whole House and Solar, which the Solver consumes?
4. Is battery power + SoC + prices enough for a sensible first Regret/EPR score?
5. What would you remove rather than add?
6. Parent/child loads, or refuse-and-name (§8.2)?
7. **New:** Repairs (§7) or persistent notifications for setup gaps? Repairs clear themselves and carry a Fix button, but they are a larger surface to maintain.
8. **New:** which inverter and battery integration profiles first? The proposal is to start with what testers actually run (GoodWe, Fronius, Smappee, SigEnergy) and add one per PR with a captured fixture.
