# Two-step setup: Nimbus finds your site, you confirm it

**Status:** proposal for review ([#1574](https://github.com/code-imstillalive/nimbus/issues/1574)). Nothing here is built yet.

**The bar, in the household's words (2026-10-06):**

> *"it has to be very very clever and simple for the users... whatever we do... we must make it a breeze for the user... not a nightmare Chris has experienced"*

Every decision below is judged against that. Where a choice trades capability for simplicity, simplicity wins at the basic level and the capability moves to advanced.

**Revised 2026-10-06, twice, after Mark's review.** First *"devices with entities under each; power, energy, limits, forecasts"*, then his full [device contract](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015647317). The minimum site is one **Grid** connection and one whole-house **Load**; Solar and Battery are added when present. This document now covers only the setup experience on that contract (§2). The earlier "5 sensors" and "pick your meter device" framing is withdrawn.

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

## 2. The device contract is Mark's, and this document does not copy it

**The authoritative model is [Mark's device schema on #1574](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015647317)** (with the device-first rationale [here](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015284390) and the [Energy Dashboard handover note](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015470655)). As he asked, this document does not keep a second copy. It describes only the **setup experience** built on that contract. Where anything here seems to differ from it, the contract wins.

The parts of the contract the setup experience depends on, by name only:

- **Devices** are logical energy participants: **Grid, Solar, Load and Battery**. They are not Home Assistant device-registry entries and not meters. A whole house is a Load; a circuit or appliance is another Load. A **tariff is a provider attached to a Grid**, not a device.
- **The minimum site is one Grid and one whole-house Load.** Solar and Battery are added when present, never assumed. A Grid-and-Load site is a complete, valid install.
- **Bindings** say where each role's data comes from (`entity_state`, `entity_attribute`, `action_response`, `recorder_statistic`, `constant`, `derived`, `model_output`), each with its unit, sign, boundary and provenance. One canonical sign convention applies everywhere: Grid positive = import, Battery positive = discharge, Solar positive = generation, Load positive = consumption.
- **Relationships** are `supplies`, `included_in_measurement` and `shares_equipment`, and they are separate from **accounting**. **Shared constraints** cover things such as one hybrid-inverter limit across a Solar and a Battery.
- **Readiness is per function** (`observation`, `forecasting`, `planning`, `scoring`, `control`), each with its status and reason codes. Setup is observation-only.
- **Configure once, use everywhere.** Basic and Advanced edit the same device definitions. Forecaster, Solver, Topology, cards and Regret read them; today's settings become generated compatibility views, not independently editable copies.

---

## 3. The flow at a glance

```
Add Nimbus
  │
  ├─ 1. Discover        automatic, read-only: candidates for each role, with evidence
  │
  ├─ 2. "Here is the site Nimbus found. Is this correct?"
  │        Grid connection + Whole-house load (always)
  │        + Solar and Battery, if found
  │        + its tariff provider, shown on the Grid
  │        ambiguous roles asked as ONE targeted question each
  │
  ├─ 3. Confirm         saves the device definitions, nothing else
  │
  └─ 4. Readiness       per function: what works now, what's training,
                        what needs one more fact, and why. Control: not enabled.
```

**The novice path** (the contract's last acceptance case): the user confirms the detected site once, gets the matching topology and outputs, sees precise guidance for anything missing, and does not enable dispatch merely by finishing setup.

---

## 4. Discover (automatic, read-only)

Discovery proposes **candidates** with their **evidence**. It saves nothing, and it never writes the Energy Dashboard (per the handover note, `energy/save_prefs` replaces whole keys and the source list is the user's intent).

### 4.1 Where evidence comes from

| source | gives | how it is used |
|---|---|---|
| **Energy Dashboard** (`energy/get_prefs`, read only) | Grid, Solar and Battery sources; their **energy** statistics; tariff price entities; solar forecast config entries; individual devices with `included_in_stat`; and, where configured, `stat_rate` / `power_config` power | discovery **hints** for roles and relationships. Energy statistics stay `recorder_statistic` bindings, never substituted for power. `included_in_stat` is real `included_in_measurement` evidence. |
| **Known integration profiles** | role-tagged entities, attributes and response actions for recognised inverters, meters, batteries and pricing providers (LocalVolts v2 first, #1561) | the strongest evidence; one integration may supply several logical devices (an inverter gives Grid, Solar and Battery roles) |
| **The device registry** | which entities sit together | a **candidate-selection hint only**, never sufficient proof (Mark: "same Home Assistant device should be a candidate-selection hint") |
| **History** | how each candidate behaved | corroboration (§4.2) |

### 4.2 Corroboration, not proof

`device_resolver.py` (#1590) supplies two pieces of evidence. Both produce **candidates and hypotheses for the user to confirm**, never a mapping applied on their own.

- **Power ↔ energy pairing:** a candidate power sensor's history, integrated **hour by hour**, against the role's own energy statistic, trying both signs. On the reference household's real history it singled out each inverter's battery power sensor (with its sign) and inverter 1's PV power from the 247 power sensors on one Modbus device, at a 1–2.6% error, and **asked** where two sensors were the same reading. It is still evidence: the binding must also have an unambiguous role and boundary, compatible units, sign and timestamps.
- **Energy balance** (grid ≈ load − solar − battery): when it fails to close, it names the single change that would close it (a flipped sign, ×1000, partial solar) as a **hypothesis**. It is meaningful only when the series share a boundary, are aligned, and vary enough. A residual suggests a mismatch; it never proves a bad sensor or justifies a calibration correction.

**Freshness** uses each source's own update semantics, not just the age of the last state change: a change-only sensor that is idle is not stale (the #1556 lesson).

---

## 5. "Here is the site Nimbus found" (one screen)

One row per logical device, each expandable to show its bindings, their sources, and any uncertainty:

| row | always / when found | shown |
|---|---|---|
| **Grid connection** | always | live import/export; the binding it came from (*"via your Sungrow inverter's meter"*); its **tariff provider** (*"LocalVolts, prices and P2P"*) and forecast coverage |
| **Whole house** (a Load) | always | live consumption; source |
| **Solar** (one per array or inverter, as the evidence shows) | when found | live generation; its forecast provider |
| **Battery** (one per battery) | when found | live power and SoC, sign as found |

Rules:
- **The Grid connection is not a meter.** The row is named for the connection; its readings may come through an inverter. If no direct grid measurement exists, the Grid still exists. Its power is either unresolved or, only after the complete balance, signs, timing and boundaries validate, a clearly labelled **derived** binding.
- **Ambiguity becomes one targeted question,** phrased as a functional role: *"Which sensor measures the whole house?"*, *"These two read the same; which is the inverter's PV total?"*. It is never a list of internal Solver settings.
- **Limits and capacities are asked only for a capability that needs them** (planning needs battery capacity and charge/discharge limits; observation does not). Historical maxima are plausibility evidence, never a substitute for an equipment or connection rating.
- **Nothing for absent equipment:** no battery or solar fields on a site without them.

### 5.1 Loads beyond the whole house

Offered on the same screen as a pre-ticked list, from the Energy Dashboard's individual devices and the circuits on the house meter. Each ticked one becomes a Load.

**Accounting is not inferred from nesting** (the contract's accounting rules). A circuit or appliance that sits inside the whole-house measurement gets an `included_in_measurement` relationship: from the Energy Dashboard's `included_in_stat` where present, otherwise confirmed by one question. The household's demand stays **inclusive** (the whole-house Load alone). Children are displayed and forecast without being added to it. Until a validated residual-plus-children decomposition exists, **overlapping summation is refused and the conflicting loads are named** (the tester's pool inside its circuit is the acceptance case).

---

## 6. Confirm, and what it creates

Confirm saves the **device definitions** (devices, bindings, relationships, constraints, provenance), and nothing else:

- **Telemetry is registered, not forecast.** A confirmed Battery or Grid power binding is telemetry. It does **not** create a learned forecast, because planned dispatch and grid exchange are outputs of the plan. **Solar and Load** forecasts are planning inputs. Load forecasts are learned; solar comes from its forecast provider where one exists.
- **Today's settings are generated from the definitions** as compatibility views, so the Solver, Forecaster, Topology and cards keep working unchanged while they migrate to reading the definitions directly.
- **Control stays off.** A discovered switch or inverter control is not authorisation to actuate it. Completing setup sends no device commands (acceptance case).

## 7. Readiness: per function, with reasons

Readiness replaces any "everything works now" promise, which is the contract's readiness model made visible:

| function | ready when | otherwise shows |
|---|---|---|
| **observation** | the Grid and whole-house Load bindings resolve | which binding is missing |
| **forecasting** | each Load's history has trained (the #1557 rules) | per Load: training, or *"needs ~5 days of history"*, with the coordinator's own reason |
| **planning** | observation plus prices (Grid tariff) plus, if a Battery exists, its capacity and limits | the one missing fact |
| **scoring** (Regret/EPR) | a defined baseline, aligned actuals, applicable prices, and for a battery its initial/final stored energy and losses | **"insufficient evidence"** or **"not applicable"**, never a misleading score (a Grid-and-Load site has no battery score to report) |
| **control** | separately authorised, never by setup | *"not enabled"* |

The surfaces:
- **Settings → Repairs** for each missing fact. These clear themselves when fixed: stage 2 (#1588) ships the first four.
- **A readiness summary** on the Nimbus device page.

---

## 8. Advanced: the same devices, expanded

Advanced is **not a second configuration model**. It reveals more capabilities on the same devices:
- Loads become thermal, sheddable or schedulable, with windows, runtimes, comfort limits and deadlines;
- several batteries and EVs;
- more than one grid connection;
- shared constraints;
- secondary price and forecast sources;
- P2P blocks;
- overrides.

Every value is one binding or constraint on one device, used by every subsystem.

---

## 9. Existing installs

- **Migration builds the device definitions from the existing settings,** preserves explicit choices and history, and **flags conflicting mappings for review**. A conflict is, for example, two different battery sensors in Forecaster and Solver settings.
- **Re-running discovery is idempotent.** Nimbus IDs survive entity renames and Energy Dashboard reordering; no duplicate devices (acceptance case).
- **Gap-filling never changes the site's accounting model or enables control.** Stage 1 (#1587) is the first, narrow instance: it fills only the Solver's empty inputs from sensors already confirmed for the same quantity, creates no forecasts, and changes nothing on the reference household (pinned by a test on its real diagnostics).

---

## 10. Rules that hold everywhere

1. **Never overwrite** a confirmed choice; surface conflicts instead.
2. **Never silent:** everything created is listed, and everything missing is a Repair with its reason.
3. **One definition per device**, used by every subsystem.
4. **Evidence, then confirmation:** discovery proposes, the user confirms ambiguous roles.
5. **Refuse the wrong kind at the door:** energy for power (#1562), doubled fees (#1564), unit mismatches (#1570).
6. **Absence is never zero:** an unresolved binding is reported missing, never filled with 0.
7. **Setup never enables control.**
8. **Fewer fields, not moved fields:** ask for goals and unresolved facts, not plumbing Nimbus can determine.

---

## 11. Acceptance tests (the contract's, as tests)

| case | given | must hold |
|---|---|---|
| **Minimal site** | only a Grid and a whole-house Load | setup succeeds without battery or solar fields; planning and scoring show precise reasons |
| **Shared hardware** | one inverter integration exposing grid, PV and battery | three logical roles with correct boundaries, not three copies of the equipment |
| **Split providers** | power, prices and forecasts from different integrations | one Grid device owns the resolved roles, including action-returned forecasts |
| **Rename and rescan** | confirmed mappings | an entity rename or Energy Dashboard reorder keeps Nimbus IDs and creates no duplicates |
| **Inclusive loads** | a pool inside the household total | showing its forecast leaves planned demand unchanged until a decomposition is enabled |
| **Incomplete evidence** | stale data, ambiguous signs, a missing tariff direction, forecast gaps | the mapping is kept, and only the affected capabilities are blocked |
| **No accidental control** | discovered switches and inverter controls | completing setup sends no device commands |

How they are run:
- **Replay real installs:** the reference household, the tester's 6 Oct install, Mark's install and devhub.
- **The real Home Assistant harness** (`tests/hass_integration/`): drive the actual flow from "Add Nimbus" to a first observation.
- **Devhub end to end:** remove and re-add Nimbus, then restore the backup.
- **A real tester's first install last:** the only measure of "a breeze".

---

## 12. Delivery

Following the contract's own sequence:

| stage | delivers | status |
|---|---|---|
| **0. Agree the device contract** | Mark's schema on #1574 | proposed; this document builds on it |
| **1. Repair existing gaps** | fill the Solver's empty inputs from confirmed mappings, with the card falling back to the Solver's sensors (no forecasts created) | #1587 |
| **2. Gaps as Repairs** | the first readiness surface: untrained forecasts, energy-unit inputs, doubled fees, missing Solver inputs | #1588 |
| **2a. Discovery evidence** | `device_resolver.py`: both Energy Dashboard schemas (#1589), power↔energy pairing, the balance check | #1590 |
| **3. Device definitions** | the contract's types in storage; migration from today's settings, with conflicts surfaced; today's settings generated from them | next |
| **4. Device-first Basic** | discover → "here is your site" → confirm, starting with Grid + whole-house Load, then optional Solar and Battery | after 3 |
| **5. Expand progressively** | flexible loads, several batteries/EVs, several connections, advanced constraints, without duplicating definitions | after 4 |
| **6. Verify the novice path** | a real tester's first install | last |

Each stage is released and validated on devhub.

---

## 13. Questions

Mark answered the first six on #1574 (the answers are folded in above):
- a Grid device, preferring a boundary meter;
- same-device is a hint, not proof;
- telemetry, not forecasts;
- capability-dependent scoring;
- remove repeated entry, compulsory solar/battery fields and manual Power Signal steps;
- parent/child as relationships separate from accounting.

Two remain open:
1. **Repairs or notifications** for setup gaps (stage 2 ships Repairs).
2. **Which integration profiles first.** Proposed: the ones testers run (Sungrow, GoodWe, Fronius, Smappee, SigEnergy), one per PR, each with a captured fixture.
