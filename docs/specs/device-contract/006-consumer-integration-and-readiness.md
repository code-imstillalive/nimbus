# Nimbus Device Contract Spec DC-006: consumers on the shared definitions, and per-function readiness

Status: Draft for review, revision 0.1. Prepared 19 October 2026, Australia/Brisbane. Drafted, as DC-001 to DC-005 were, for Mark Purcell's review on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574) ("you draft, I sign off").

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/006-consumer-integration-and-readiness.md`. Depends on [DC-002](002-bindings-and-normalisation.md) (resolution), [DC-003](003-accounting-and-shared-constraints.md) (accounting and reconciliation) and [DC-004](004-migration-and-compatibility.md) (the stored document, its modes and views). [DC-005](005-discovery-and-provider-selection.md) supplies confirmed bindings but is not a prerequisite: a document imported by DC-004 is enough.

This specification proposes behaviour. It does not claim that any consumer reads the document today. It authorises no change to an installation: every slice below changes behaviour only on an install whose household has chosen `active`.

## Responsibility

Until now the device document has been a store with views: in `active`, DC-004 generates today's settings from it, and every consumer still reads those settings and interprets the raw entity itself (its own unit handling, its own sign flag, its own idea of stale). DC-006 moves each consumer to **the document's resolved quantities**: DC-002's units, signs, freshness and intervals, and DC-003's accounting. It also says, per function, whether the install is ready and why not (DC-R14).

| DC-006 owns | DC-006 does not own |
|---|---|
| Per-function readiness: what each function needs, and the reason it is not ready | The screens that show it to a novice (DC-007) |
| Moving each consumer, one bounded slice at a time, from its settings key to the resolved quantity | What a role resolves to (DC-002) and how totals add up (DC-003) |
| The equivalence gate per slice: identical inputs on F10, except declared, approved differences | The stored document and its modes (DC-004) |
| Scoring's reliability reason when site exchange does not reconcile (DC-R10) | Correcting a historical score (out of scope in the roadmap) |
| Removing a consumer's own copy of a rule once the shared one serves it | Granting control (DC-007) |

## Requirements owned (from DC-000's registry)

| ID | Requirement | Fixtures | What DC-006 must make checkable |
|---|---|---|---|
| **DC-R14** | Readiness is per function; unsupported combinations are explicit | F01, F04, F09, F11 | Each function reports `ready`, `training`, `needs` or `unsupported`, with the roles and the reason; a Grid-and-Load site is ready to observe |
| **DC-R11** (with 004, 007) | Basic, Advanced and supported consumers use one revisioned authority | F10, F11 | In `active`, a moved consumer reads the stored revision through DC-002 and records which revision it used |
| **DC-R08** (with 003) | Inclusive totals and residual-plus-children do not double-count | F05, F07 | A moved consumer that sums loads uses DC-003's plan, never its own sum |
| **DC-R09** (with 003) | Shared limits constrain the declared combined quantity once | F02, F07 | The Solver's shared charger and circuit limits come from `shared_constraints` |
| **DC-R10** (with 003) | Site exchange and scoring evidence reconcile or visibly fail | F07, F08 | A score over a window whose site exchange does not reconcile carries a reliability reason; it is never silently published as reliable |
| **DC-R04** (with 002) | Missing, zero, unavailable and stale stay distinct | F04, F09 | A moved consumer treats each as DC-002 labels it, never as zero |
| **DC-R16** (with 001, 002) | Derived and model dependencies are acyclic | F11 | A consumer reading a model output never feeds that model |

## Who reads what today

From DC-000's inventory at `8a9f503` (`tests/device_contract/inventory/`): the twelve settings keys DC-004 maps to device roles are read by about thirty modules, across seven families.

| Key (device role) | Families reading it | Readers |
|---|---|---|
| `grid_sensor` (Grid power) | forecaster, setup | 3 |
| `solver_import_price_sensor` / `_export_` (Grid prices) | platform, scoring, setup, solver, topology | 12 / 10 |
| `solver_battery_soc_sensor` (Battery SoC) | cards, platform, scoring, setup, solver | 8 |
| `solver_battery_power_sensor`, `battery_sensor` (Battery power) | cards, platform, scoring, setup, solver, forecaster | 7, 3 |
| `solver_solar_power_sensor`, `solar_sensor` (Solar power) | cards, platform, scoring, setup, solver, forecaster | 9, 3 |
| `solver_solar_forecast_sensor` (Solar forecast) | platform, setup, solver | 4 |
| `solver_whole_house_cross_check_sensor` (Load power) | forecaster, platform, scoring, setup, solver | 9 |
| `solver_load_forecast_sensor` (Load forecast) | cards, platform, setup, solver | 6 |
| subentry `load_sensor` (a Load's power) | forecaster, platform, setup | 7 |

Two keys for one role (`battery_sensor` and `solver_battery_power_sensor`) are already one binding in the document (DC-004's `also_source_paths`). Moving their readers is what finally retires the second key.

## Readiness, per function (DC-R14)

A pure module, `device_readiness.py`, reads the stored document and DC-002's resolutions and returns one result per function:

| Function | Needs | `training` when |
|---|---|---|
| **observe** | Grid power, or Grid import and export energy | never |
| **forecast** | Load power or energy, and at least one trained model | a model is still in its first training window |
| **plan** | observe, a Load forecast, Grid import price (and export price where exporting is allowed), and for a Battery its SoC, power and capacity | the Load forecast is still training |
| **score** | plan's inputs over the scored window, and site exchange that reconciles (DC-003 `reconcile()`) | fewer than one full scored day |

Each result is `ready`, `training`, `needs` (with the missing roles and, for each, what would satisfy it) or `unsupported` (with the combination and why). `training` is a status, not a fault (roadmap: *"normal training is status, not a fault"*). A stale or unavailable binding does not make a function `needs`: it is reported as the binding's state, and the function's own fallback applies, as today.

Dispatch is not a function here. Control stays separately authorised (DC-007); readiness never enables it.

Readiness is published as `sensor.nimbus_device_readiness` (state: how many functions are ready; attributes: one entry per function) and in `device_contract_preview` and `device_contract_discover` responses. It replaces nothing in `setup_health.py` (#1588) until DC-007 merges the two surfaces.

## How a consumer moves

Each slice is one PR, and changes one consumer family:

1. **In `legacy` and `shadow`, nothing changes.** The consumer reads its settings key, exactly as today.
2. **In `active`, the consumer reads the resolved quantity** for its role from the stored revision, through DC-002, and records the revision in its own provenance (DC-R11).
3. **The equivalence gate.** On F10, in `active`, the moved consumer's inputs are compared with its inputs on the same install in `legacy`. They must be identical except for entries in `tests/device_contract/approved_changes.json`, each naming the case, field, old and new value, the requirement and the reviewer. No wildcards (DC-000).
4. **No fallback to the settings key.** If a role does not resolve in `active`, the consumer does not quietly read the old key (that would be a second authority). It behaves as it does today when that input is missing, and readiness says why.
5. **The old interpretation is removed** only when no consumer of that family still reads the key in any mode, which is after DC-008 makes `active` the default for new installs.

## The slices, in order

| # | Slice | Consumers moved | Expected approved differences |
|---|---|---|---|
| 1 | **Readiness** | none: adds `device_readiness.py` and its sensor | none; it is new output |
| 2 | **Observation** | the Forecaster's power inputs (`coordinator.py`, `power_input_check.py`), the Topology card | DC-002's units: `kw`, `KW`, ` kW `, lowercase `mw`, and **a power sensor with no unit at all** are read today (the last as kW, by design) and refused by the contract; sign from `normalization`, not `solver_battery_power_positive_is_charge` |
| 3 | **Forecasting** | Solar and Load forecast inputs (`solver_inputs/solar.py`, `solver_inputs/load.py`) | a held forecast past its end is a gap (DC-002, recorded in `contract_divergences.json`) |
| 4 | **Planning** | prices (`solver_inputs/prices.py`), Battery SoC and power (`solver_writer.py`), shared limits | overlapping provider rows refused, not first-kept; a price with no unit refused, not read as $/kWh; shared limits from `shared_constraints` |
| 5 | **Scoring** | `solver_reports/*` | a window whose site exchange does not reconcile is marked unreliable (DC-R10, F08) |
| 6 | **Cards and compatibility** | the dispatch, forecast and topology cards' entity reads | none expected: cards read entities the views already name |

Planning moves after observation and forecasting have run in `active` on devhub for a week each, because it is the slice that changes what the battery does.

### Open divergences each slice must settle first

`tests/device_contract/contract_divergences.json` records where today's readers and the contract disagree. Every entry is `open_decision`: none is approved yet. A slice cannot merge while an entry it touches is open. For each, the household (or Mark, on a technical one) decides **adopt the contract** (the entry moves to `approved_changes.json`, and households with such a sensor are told by a Repair before they choose `active`) or **keep today's reading** (DC-002's rule changes to match, with its own test).

| Slice | Open entries it touches |
|---|---|
| Observation | F09: `w`, `kw`, `KW`, `Kw`, ` kW `, `mw`, no unit, no unit attribute |
| Forecasting | F04: `expired_price` (held past its end) |
| Planning | F04: `overlap` (first row kept), `price_unit_missing` (read as $/kWh) |

## Acceptance

- **DC-R14 / F01:** a Grid-and-Load site is `ready` to observe and `needs` for plan, naming the price roles.
- **DC-R14 / F11:** a Solver whose Load forecast is a model still training is `training`, not `needs`.
- **Each slice:** its equivalence test passes on F10 in `active`, and the same test in `legacy` shows the consumer reading the settings key exactly as before.
- **DC-R10 / F08:** scoring the F08 window publishes its score with the reliability reason `site_exchange_disagrees`.
- **DC-R15:** no slice makes a service call the consumer did not make before.
- **devhub:** each slice runs in `active` on devhub for at least a day before the next is merged, with the consumer's inputs compared against `legacy` on the same data.

### DC-000 additions this needs

1. A **consumer-input capture** for F10: each slice's inputs as plain values, in `legacy` and in `active`, so the equivalence gate compares data, not code paths.
2. **F11 cases for readiness:** a training model, a missing price, an unsupported combination (a Battery with no capacity).

## What already exists and is reused, not rewritten

- `device_bindings.resolve` (DC-002) for every value a moved consumer reads.
- `oracles_accounting.reconcile`, `residual_series` and `decomposition_verdict` (DC-003 via DC-000) for scoring's reliability reason and the load sums.
- `setup_health.py` (#1588) keeps its Repairs; readiness adds the per-function view beside it.
- DC-004's views stay, for the previous release and for any consumer not yet moved.

## Out of scope

- New screens: DC-007.
- Changing the optimiser's objective or the battery strategy (roadmap: *"no dispatch redesign"*).
- Re-scoring past days with the new accounting (roadmap: *"retrospective score replacement"* is out of scope).
- Making `active` the default: DC-008.

## Rollback

Every slice is gated on `active`. Choosing `legacy` returns every consumer to its settings key at once, with the views DC-004 left in place. Reverting a slice's code reverts it on every install.

## Decisions for review

Each is proposed as written above. The build follows these unless @purcell-lab changes one.

1. **Only `active` moves a consumer.** `legacy` and `shadow` read today's settings exactly as before.
2. **One consumer family per PR**, in the order above, each with its F10 equivalence test and its entries in `approved_changes.json`.
3. **Readiness** has four functions (observe, forecast, plan, score) and four states (`ready`, `training`, `needs`, `unsupported`). Training is a status, not a fault. Dispatch is never a readiness function.
4. **Readiness is published** as `sensor.nimbus_device_readiness` and in the preview and discover responses. It sits beside `setup_health.py` until DC-007.
5. **No fallback to a settings key in `active`.** A role that does not resolve gives the consumer's existing missing-input behaviour, with the reason in readiness.
6. **Planning waits** for a week of observation and of forecasting in `active` on devhub.
7. **Scoring marks, never corrects:** a window that does not reconcile publishes its score with a reliability reason.
8. **Placement:** `device_readiness.py` pure, beside `device_contract.py`; each consumer's read goes through one helper, `device_contract_ha.resolved(hass, entry, device_kind, role)`, so the `active` check and the revision record are in one place.
9. **Open divergences are settled per slice, before it merges** (table above), each by an explicit decision recorded in `approved_changes.json` or in DC-002's rules. **Units, decided by the household on 19 Oct 2026:** adopt the contract, with one Repair per affected power sensor before `active` is offered; the Repair lets the household declare W or kW on the binding (`normalization.native_unit`, `overridden: true`) or fix the sensor (*"tell nimbus in the repair i think is easier than correcting the whole sensor... but it is good to have a choice... and i do agree with strict direction"*). DC-002 gains one rule with the observation slice: a declared unit applies when the sensor states none or a non-exact spelling; a valid stated unit that differs from the declared one is refused (`declared_unit_conflicts`), never resolved either way.
