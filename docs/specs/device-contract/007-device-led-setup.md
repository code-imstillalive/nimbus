# Nimbus Device Contract Spec DC-007: device-led setup, Advanced editing and readiness guidance

Status: Draft for review, revision 0.1. Prepared 19 October 2026, Australia/Brisbane. Drafted, as DC-001 to DC-006 were, for Mark Purcell's review on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574).

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/007-device-led-setup.md`. Depends on [DC-004](004-migration-and-compatibility.md) (storage, modes), [DC-005](005-discovery-and-provider-selection.md) (discovery and confirmation) and [DC-006](006-consumer-integration-and-readiness.md) (readiness, consumers). The experience is the one in [the two-step setup design](../../design/two-step-setup.md) (#1585), §3 and §5 to §10. This spec does not repeat it; it says how Home Assistant shows it.

This specification proposes behaviour. It does not claim that any screen below exists. It authorises no change to an installation.

## Responsibility

DC-005 can propose a site and DC-004 can store it. DC-007 is the part a household sees: one screen that shows what Nimbus found, one targeted question per thing it could not decide, a confirmation, and readiness that says what works now and what needs one more fact. It also gives Advanced editing on the same devices, and keeps control a separate, explicit step.

| DC-007 owns | DC-007 does not own |
|---|---|
| The config-flow and options-flow steps a household walks through | What discovery proposes (DC-005) and how it is stored (DC-004) |
| Turning DC-005's questions into one HA form field each | Readiness rules (DC-006); DC-007 shows them |
| Advanced editing of devices, bindings, relationships and constraints | The document's schema (DC-001) |
| Control authorisation, as its own explicit step | Dispatch itself, and any actuator integration |
| Which surface shows what: forms, Repairs, the Solver tab, the device page | New dashboards: none are added (household rule) |

## Requirements owned (from DC-000's registry)

| ID | Requirement | Fixtures | What DC-007 must make checkable |
|---|---|---|---|
| **DC-R01** (with 001) | A Grid and an aggregate Load is a valid site | F01 | A setup with only a grid connection and the house completes, with observe ready and plan's missing inputs named |
| **DC-R11** (with 004, 006) | One revisioned authority for Basic, Advanced and consumers | F10, F11 | Every form writes the document through DC-004's commit; no form writes a setting the document owns |
| **DC-R15** (with 004, 005) | Setup sends no actuator commands | F12 | Walking every step, confirming and authorising a load makes zero service calls outside `nimbus_load` |
| **DC-R18** (P1) | Novice task time and guidance refined from recorded walkthroughs | F01, observed walkthroughs | Each walkthrough records time, questions asked and help needed |

## Where each part lives in Home Assistant

| Part | Surface | Why there |
|---|---|---|
| **New install** | the config flow: *Add Nimbus* → discovery → *Here is the site Nimbus found* → questions → confirm | the first thing a household sees; HA owns this screen |
| **Existing install** | the options flow: *Configure* → **Device setup** menu | already where settings live; no new place to learn |
| **What was found** | one form step per device, its bindings in the description, a confirm checkbox | HA forms cannot draw tables; one device per step keeps each short |
| **A question** | one selector field per DC-005 question (entity selector limited to the candidates, or a number with unit for a fact like capacity) | *"one targeted question each"* (design §5) |
| **Readiness** | the Solver tab's Device setup card (slice 6 of DC-006 adds the per-function rows), `sensor.nimbus_device_readiness`, and Repairs for actionable faults only | status stays status; only faults become Repairs (design §7) |
| **Mode** | `select.nimbus_device_setup_mode` and the Solver tab, as in 0.94.467 | unchanged |
| **Control** | its own options-flow step, per load, with an explicit tick and a statement of what Nimbus will command | design §6: *"Control stays off"* |

**No new dashboard or sidebar panel.** Household rule: none are added on any install without asking. Everything above is a form, a Repair, or the existing Solver tab.

## The new-install path

1. **Discover.** The config flow calls DC-005's discovery. While it runs, the step shows progress. Discovery reads; it writes nothing.
2. **Here is the site Nimbus found.** One step per device, in the order Grid, Whole house, Solar, Battery, then the other loads (pre-ticked, as design §5.1). Each shows its bindings in plain words (*"Grid power: sensor.x, via your inverter's meter"*) and its provider. The household can untick a device or a binding.
3. **Questions.** One step per DC-005 question that is still open, each a single field. A fact (battery capacity, usable or nominal) is asked only when a function the household wants needs it (design §5: *"Limits and capacities are asked only for a capability that needs them"*).
4. **Confirm.** The flow commits the document through DC-004 and enters `shadow` for a new install, because there are no legacy settings to compare with. It then generates the views and enters `active` once the dry run is clean, which for a new install it always is. That makes the device document master from the start.
5. **Readiness.** The last step shows the readiness result (DC-006): what works now, what is training, what needs one more fact and why. *"Control: not enabled."*

## The existing-install path

*Configure → Device setup* offers:

- **Review what Nimbus finds**: DC-005's rediscovery against the stored document, as one step per difference. *Fill*, *renamed* and *new device* are pre-ticked. An *alternative* to a confirmed binding is shown, never pre-ticked (DC-005 decision 5).
- **Answer open questions**: the same one-field steps as a new install.
- **Edit a device (Advanced)**: see below.
- **Mode**: the same choice as the select, with the dry run shown before `active`.

In `legacy`, *Device setup* offers only the dry run and the move to `shadow` (DC-005 decision 7: confirming needs a stored document).

## Advanced: the same devices, expanded

Advanced is not a second model (design §8). Each edit is a DC-004 commit of a changed document, through `device_contract_confirm`'s validation (DC-005's validated switch):

- **A binding:** choose another source for one role. It is validated by DC-002 before commit, and a refused one changes nothing.
- **A relationship:** inclusion, supplies, shared equipment. DC-003's checks run before commit: a cycle, a double path or an overlapping sum is refused with the loads named.
- **A constraint:** limits, windows, runtimes, comfort, deadlines, shared limits.
- **A device:** add a battery, an EV, a second grid connection, a Load with its schedule.

Today's subentries (loads, batteries, controllable loads, signals) stay as the way these are shown, because households already know them. In `active`, a subentry form reads from and writes to the document, and its saved values are DC-004's views. In `legacy`, they work exactly as today.

## Control authorisation

- A device's control binding (a switch, a climate entity) is a **candidate**, not consent (design §8).
- Authorising control is its own step per device: it names the entity, says what Nimbus will command and when, and needs an explicit tick.
- Authorisation is recorded in the document (`control_policy` and the device's `control`) with who and when. Revoking is one step and takes effect at once.
- Imported authority (DC-004) stays exactly as it was. DC-007 never enables control on its own, and finishing setup never enables it (DC-R15).

## Acceptance

- **DC-R01 / F01:** a new-install walk with only a grid connection and the house completes, observe is ready, and plan's missing prices are named with what would satisfy them.
- **The #1528 case (DC-005's acceptance):** a fresh install with a full Energy Dashboard reaches the readiness step with the Grid, Solar, Battery and loads, having asked only for the battery's capacity.
- **DC-R11 / F10:** after any form in `active`, the document's revision has moved by one, and every view equals what DC-004 generates from it. No form writes an owned setting directly.
- **DC-R15 / F12:** walking every step, confirming and authorising a load make zero service calls outside `nimbus_load`, and finishing setup leaves `control_policy` as it was.
- **Walkthroughs (DC-R18):** devhub and one consenting household, each recorded: time taken, questions asked, help needed.

## What already exists and is reused

- `flows/` (config flow, options flow, subentry flows): the steps are added there, not in a new UI framework.
- `setup_builder.py` and #1587's gap-filling Repairs: their one-click fills become DC-005 proposals in `active`.
- `setup_health.py` (#1588) Repairs: kept for faults; readiness takes over status.
- The Solver tab's Device setup card and mode select (0.94.467).

## Out of scope

- A web panel, sidebar dashboard or custom frontend for setup.
- Dispatch control logic and actuator integrations.
- Making `active` the default for existing installs (DC-008).

## Rollback

Every DC-007 edit is a DC-004 revision. DC-004's revert and `legacy` apply. Reverting DC-007's code removes the steps. The stored document and its views remain, and the subentry forms fall back to today's behaviour.

## Decisions for review

Each is proposed as written above. The build follows these unless @purcell-lab changes one.

1. **Surfaces:** the config flow for a new install, *Configure → Device setup* for an existing one, one HA form step per device and per question. No new dashboard.
2. **A new install goes straight to `active`** after the confirm, through `shadow` and a dry run that is clean by construction.
3. **Facts are asked only for a function the household wants** (for example capacity for planning, not for observation).
4. **Subentry forms stay** as the face of Advanced; in `active` they read from and write to the document.
5. **Control is its own step per device**, recorded with who and when, never enabled by setup, and revocable in one step.
6. **Readiness shows as status** (the Solver tab card and the sensor). Only actionable faults become Repairs.
7. **Walkthroughs are recorded** on devhub and one consenting household before DC-008's rollout.
