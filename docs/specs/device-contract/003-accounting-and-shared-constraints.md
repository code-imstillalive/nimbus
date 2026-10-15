# Nimbus Device Contract Spec DC-003: accounting boundaries, inclusion, decomposition and shared constraints

Status: Draft for review, revision 0.1. Prepared 15 October 2026, Australia/Brisbane. Drafted, as DC-001 and DC-002 were, for Mark Purcell's review on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574) ("you draft, I sign off").

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/003-accounting-and-shared-constraints.md`. Depends on [DC-000](000-baseline-fixtures-and-gates.md), [DC-001](001-canonical-device-model.md) and [DC-002](002-bindings-and-normalisation.md).

This specification proposes behaviour. It does not claim any accounting code below exists. It authorises no consumer switch, storage or migration; those are DC-004 and DC-006.

## Responsibility

DC-003 takes a validated DC-001 document and DC-002's resolved values and answers one question: **which quantities add up to what, with every transfer counted once.** Its output is an **accounting plan** (how demand and site exchange are composed, and which limits bind combined quantities) or an **explicit unsupported result**. It never returns a guess.

| DC-003 owns | DC-003 does not own |
|---|---|
| What `supplies`, `included_in_measurement` and `shares_equipment` mean for arithmetic | Their shape and structural checks: references, `child != parent`, inclusion cycles (DC-001) |
| `inclusive` and `residual_plus_children` demand, and the residual's definition | Reading, unit, sign and interval of each value (DC-002) |
| Reconstructed site exchange, and its reconciliation with a measured Grid | Whether scoring, planning or a card uses the result, and what each shows (DC-006) |
| The semantics of a shared limit: which combined quantity, constrained once | Writing the limit into the LP, and migrating today's per-load copies (DC-006, DC-004) |
| The undeclared string-plus-total overlap DC-001 deferred, as a named case | Asking the household to confirm an inclusion (DC-005) |
| Unsupported-topology result codes | Per-function readiness built from them (DC-006) |

## Requirements owned (from DC-000's registry)

| ID | Requirement | Fixtures | What DC-003 must make checkable |
|---|---|---|---|
| **DC-R07** | Measurement inclusion differs from electrical topology and equipment sharing | F05, F07 | Only `included_in_measurement` changes arithmetic. `supplies` and `shares_equipment` never subtract anything |
| **DC-R08** (with 006) | Inclusive totals and residual-plus-children do not double-count | F05, F07 | Both modes give the same demand and site exchange from the same measurements; a schedule replaces a baseline once |
| **DC-R09** (with 006) | Shared limits constrain the declared combined quantity once | F02, F07 | Each shared limit names one combined quantity with a defined formula; a limit is stated once, never per member |
| **DC-R10** (with 006) | Measured/reconstructed site exchange and scoring evidence reconcile or visibly fail | F07, F08 | Reconstructed exchange is compared with the measured Grid under a tolerance declared in advance, and the verdict is always published |
| DC-001 hand-off | An undeclared string-plus-total pair through two different sensors | F02 | Reported by name, never silently summed or subtracted |

## The three relationships (DC-R07)

#1574 separates them, and so does the arithmetic: *"A common HA device, a common parent in a diagram or a shared integration does not prove measurement inclusion"* ([schema](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015647317)); *"Do not automatically subtract merely because two devices are nested"* ([device-first comment](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015284390)).

| relationship | means | arithmetic effect |
|---|---|---|
| `supplies` | electrical topology: power can flow from one to the other | none. Display and Topology only |
| `included_in_measurement` | the parent's reading **already contains** the child's quantity | the child is never added on top of the parent |
| `shares_equipment` | both depend on one piece of hardware | none by itself. A limit needs a `shared_constraints` entry |

**What an inclusion contributes.** The child's contribution to the parent is stated in the parent's canonical sign, using DC-002's resolved value:
- a `load` child contributes its consumption;
- a `battery` child (an AC EV behind the house meter) contributes **minus its canonical power**, so charging adds to the parent and V2H discharge reduces it. This is `decompose()` in `oracles.py`;
- a `solar` child inside a `solar` total (a string inside its inverter, DC-001's F02 case) contributes its generation.

**An inclusion is valid only where the arithmetic can hold.** Each is a named, unsupported result rather than a quiet assumption:
- child and parent on different `boundary.domain` (a DC-coupled charger is not inside an AC house meter): `inclusion_boundary_mismatch`;
- child phases not a subset of the parent's: `inclusion_boundary_mismatch`;
- `quantity` not one the parent's bound role measures: `inclusion_quantity_mismatch`;
- a bidirectional child (a V2H EV) inside a parent bound to a magnitude that cannot go below zero: `inclusion_sign_ambiguous`. See the decision below.

**Evidence for an inclusion is DC-005's.** The Energy Dashboard's `included_in_stat` is the household's own statement of containment ([Mark's note](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015470655)), already kept by `device_resolver.device_consumption()`. DC-003 reads only confirmed relationships.

## Accounting modes (DC-R08)

```yaml
accounting:
  demand:
    mode: residual_plus_children     # inclusive | residual_plus_children
    root_load_id: load_house
    separate_load_ids: [load_pool]
```

**`inclusive`.** Household demand is the root load's own measurement or forecast. Every included child is displayed and forecast, and **never added**. This is the default and the safe interim: a pool inside the house leaves demand unchanged ([acceptance case "Inclusive loads"](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015647317)).

**`residual_plus_children`.** Each load in `separate_load_ids` is taken out of every total that contains it, once, and modelled on its own:

```text
residual(n) = measured(n) − Σ contribution(c)  for each separated c whose nearest separated
                                                ancestor (or the root) is n
demand      = Σ residual(n) over the root and every separated load
```

- **Residuals sum to the inclusive demand,** interval by interval. That is the invariant every test checks (`residuals_kw()` and `inclusive_demand_kw()` in `oracles_structure.py`).
- **A schedule replaces the separated load's measured baseline exactly once.** It is never subtracted from an unrelated total, and suppressing the child is not the correction. With F05's house 14 kW, circuit 6 kW, pool 2 kW and a 5 kW pool schedule, demand is 17 kW (`demand_with_schedule_kw()`), not 14 and not 22.
- **The residual is computed per interval from aligned measurements** (DC-002 intervals over one window), then forecast in its own right. It is never "root forecast minus child schedule" (Mark: *"Derive the residual from aligned, comparable measurements or forecast components"*, [#1574](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015284390)).
- **A separated load must be included, directly or transitively, in the root:** otherwise `separate_not_included`. A separated load with no measurement leaves its ancestors' residuals unresolvable: `residual_unresolved`, never computed with the child as zero.
- **A negative residual is reported, never clamped** (#1574: *"Do not … hide inconsistent residuals by clamping them"*). An interval whose residual is below the declared tolerance is `residual_negative`, naming the parent and the children. The tolerance is a decision below.

**Each transfer is counted once (F07).** An AC EV inside the house total is either left in it (`inclusive`) or separated with its contribution removed (`residual_plus_children`), never both. F07's 14 kW house, 10 kW AC EV inside it, 2 kW DC EV outside it and 16 kW PV give 0 kW grid both ways; the double-counted 10 kW must fail.

**Overlapping sums are refused.** Today `solver_load_forecast_entities` is summed unconditionally (`build_load_arrays()` in `solver_inputs/load.py`, via `solver_writer.sum_load_forecasts()`; [Mark's check](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6014820372)). Summing loads where one is declared inside another, without a decomposition, is `overlap_declared_but_summed`, naming both. DC-003 supplies the refusal; changing the consumer is DC-006's.

### The undeclared string-plus-total pair (DC-001's hand-off)

Two different sensors, a string and its inverter total, bound to two `solar` devices with no inclusion declared, validate structurally (`test_f02_an_undeclared_string_plus_total_pair_validates_here_and_is_dc003s`). Only readings can show one contains the other. DC-003 checks every pair of same-kind, same-domain devices for **containment evidence** over a window: the candidate child never exceeds the candidate parent beyond tolerance, and the two move together. A match is `possible_undeclared_inclusion`, naming both. It is evidence for DC-005 to ask about. It never subtracts and never blocks on its own. The statistic is a decision below.

## Shared constraints (DC-R09)

A shared limit is stated once, against a combined quantity, and is **not copied into each member's own limit** (#1574: copies *"allow their combined output to exceed the equipment rating"*).

```yaml
shared_constraints:
  - id: inverter_1_ac
    equipment: inverter_1          # matches a shares_equipment label, when one exists
    devices: [solar_roof, battery_home]
    quantity: inverter_ac_power
    limit: {value: 10, unit: kW}
```

The combined quantity has one formula per `quantity`, all in canonical signs:

| `quantity` | combined value, per interval | members allowed | today's equivalent |
|---|---|---|---|
| `circuit_power` | Σ load consumption | `load` | `SharedCircuitConfig` (`solver/elements.py`), enforced per period in `solver/network.py`; adequacy loads only |
| `charger_power` | Σ (charge + discharge) of each member | `battery` | `BatteryConfig.shared_charger_group` / `shared_charger_max_kw`, enforced in `solver/network.py` (#563) |
| `inverter_ac_power` | \|Σ solar generation + Σ battery canonical power\| | `solar`, `battery` on the inverter's AC side | none: no LP constraint combines solar and battery under one rating today |

- **An unknown `quantity`** is `shared_quantity_unknown`, and **a member the quantity does not accept** (a `load` in a `charger_power` limit, a DC-domain device in an AC quantity) is `shared_member_mismatch`. No limit is enforced on a guess.
- **Constrained once:** two shared limits on the same equipment and quantity are `shared_limit_duplicate`. Today's code takes the minimum of members' declared charger limits (`network.py`, the #563 block); in the contract that conflict is a DC-004 migration question, not resolved silently here.
- **A member's own limit stays its own.** A battery's `discharge_limit` tighter than the shared limit still applies; nothing is derived from one into the other.
- **Measured combined quantity above the limit** is `shared_limit_exceeded_by_measurement`: a hint the limit or its membership is wrong, not permission to raise it. Scoring's charger answer today, `_widen_shared_charger_cap_to_achieved()` (#1109), is DC-006's to keep or drop.
- **`shares_equipment` without a shared constraint** imposes nothing, which is correct: shared capacity alone proves neither exclusivity nor which vehicle was connected (DC-000 §F07).

## Site exchange and reconciliation (DC-R10)

**Reconstructed exchange** is the site balance over the top-level participants, those included in no other measurement, per interval:

```text
grid_rec[t] = Σ top-level load[t] − Σ top-level solar[t] − Σ top-level battery[t]
```

with each value DC-002's canonical one (`site_grid_kw()` in `oracles.py`). Losses across a declared converter efficiency apply on the AC side (`ac_side_kw()`).

**The measured exchange** is the Grid device's `power`, or `import_power` and `export_power`, or the energy counters, each resolved by DC-002. Counter deltas follow `counter_delta()`'s reset and edge rules.

**The comparison:**
- **per direction,** because a net agreement can hide two equal errors;
- **against a tolerance declared before any day is evaluated** (DC-000's rule). The live check (#1605, `solver/meter_reconciliation.py`) uses 10 % of metered throughput, a 2 kWh floor, and `insufficient_coverage` above 10 % stale periods;
- **with the meter's declared sign.** `reconcile()` tries both signs because Nimbus never asked which way a meter reads; DC-001 now declares it;
- **with interval residuals published as evidence;** the verdict stays daily.

| status | meaning |
|---|---|
| `agrees` | both directions inside the declared tolerance |
| `disagrees` | at least one direction outside it, with the residuals |
| `insufficient_coverage` | the meter's history is too sparse to judge |
| `not_checked` | no Grid measurement is bound. The logical Grid still exists (DC-001) |
| `unsupported` | the reconstruction itself returned an unsupported code below |

**One plan for achieved, reference and oracle.** The same participant set, inclusion, initial energy, losses and availability apply to all three paths (DC-000 §F07; #1465 step 5, [Mark](https://github.com/code-imstillalive/nimbus/issues/1465#issuecomment-6016381858)). Correcting only the achieved cost would not give a valid EPR. Wiring the plan into scoring is DC-006's.

**Mobility is not site exchange.** A vehicle's driving and off-site charging change its pack energy but never cross the site boundary. An EV contributes to `grid_rec` only in intervals where a connection binding says it is connected at the site. Location at home is not connection evidence (#1465 step 2). Pack power and charger power are different boundaries: a pack-power binding needs a declared charger efficiency to reach the AC side, and is never compared with a charger counter as if they were the same quantity (F08's manifest).

**F08 is the evidence case.** 5 October reconstructed 71.579 kWh import against a 32.740 kWh meter delta, and 88.642 against 69.530 kWh export. DC-003 must reproduce `disagrees` from those aggregates and claim no root cause and no corrected EPR (`KD-1465-2026-10-05`). The AC-EV-inside-house hypothesis stays a hypothesis.

## Unsupported topologies: explicit results

Every refusal is a result with a code, the devices involved and one sentence. None returns zero, a partial sum or a clamped value.

| code | when |
|---|---|
| `inclusion_boundary_mismatch` | child and parent differ in AC/DC domain, or the child's phases are not within the parent's |
| `inclusion_quantity_mismatch` | the declared `quantity` is not what the parent's role measures |
| `inclusion_sign_ambiguous` | a bidirectional child inside a parent that cannot read below zero |
| `separate_not_included` | a separated load is not inside the root |
| `residual_unresolved` | a residual needs a value DC-002 resolved as missing or unavailable |
| `residual_negative` | a residual below the declared tolerance in an interval |
| `overlap_declared_but_summed` | loads summed while one is declared inside another |
| `possible_undeclared_inclusion` | readings suggest an inclusion nobody declared (evidence, not a block) |
| `shared_quantity_unknown`, `shared_member_mismatch`, `shared_limit_duplicate` | as above |
| `shared_limit_exceeded_by_measurement` | measured combined quantity above its limit (evidence, not a block) |
| `mobility_unresolved` | an EV with no connection evidence, so its share of site exchange is unknown |
| `multiple_grid_connections` | more than one `grid` device: reconstruction and reconciliation are per connection and not yet supported (DC-001: *"representable, readiness-limited"*; F11's `planner_supported()`) |

DC-006 turns these into per-function readiness. A topology DC-003 refuses still validates under DC-001 and still supports observation where its own evidence allows.

## Acceptance

Tests live under `tests/device_contract/`, beside DC-000's oracles. Each names its requirement, and each runs the **production accounting code against the oracle**, never against itself.

- **DC-R07 / F05, F07:**
  - a `supplies` and a `shares_equipment` relationship between the same pair change no demand and no exchange;
  - an `included_in_measurement` child is not added on top of its parent;
  - each `inclusion_*` code has one case: a DC child in an AC parent, a phase outside, a wrong quantity, a V2H child in a magnitude-only parent.
- **DC-R08 / F05, F07:**
  - F05: inclusive demand 14 kW; residuals 8 / 4 / 2 kW summing to 14; the flat 22 kW sum fails; the pool schedule gives 17 kW, and suppression does not;
  - F07: both representations give 0 kW grid; the double count gives 10 kW and fails; the AC EV at 6 kW gives −4 kW;
  - a negative residual is `residual_negative`, never clamped; a missing child is `residual_unresolved`, never zero;
  - F05's cyclic and conflicting trees are refused (cycles by DC-001, the conflict by DC-003).
- **DC-R09 / F02, F07:**
  - each quantity's formula against an oracle, including two EVs on one charger whose combined `charger_power` binds while each one's own limit does not;
  - two members each carrying the rating as their own limit can together exceed it, and the same site with one shared entry cannot;
  - the three `shared_*` codes each have a negative case.
- **DC-R10 / F07, F08:**
  - F07 reconstructs 0 kW and `agrees` with a 0 kW meter, and the double count `disagrees`;
  - F08's aggregates give `disagrees` in both directions and no corrected value;
  - a meter with sparse history gives `insufficient_coverage`; no Grid binding gives `not_checked`;
  - an EV away for part of the window contributes only while connected, and one with no connection evidence gives `mobility_unresolved`.
- **DC-001 hand-off / F02:** the undeclared string-plus-total pair gives `possible_undeclared_inclusion`; two independent inverters do not.
- **Every result code has a negative test**, as DC-001 does for its problem codes.
- **No consumer reads the plan yet** (until DC-006), pinned as in DC-001 and DC-002.

### DC-000 additions this needs

Not every acceptance case above has a fixture or oracle today. Each is a DC-000 addition, made before DC-003's implementation is judged against it:
- **F02 has no manifest and no oracle** (registry: `reference_oracle: not_started`). It needs the two-inverter, string-plus-total and shared-inverter-limit cases.
- **No oracle exists for a shared limit** (DC-R09) in `oracles*.py`. It needs one function per `quantity`.
- **No oracle produces a reconciliation verdict.** F08 is `evidence_recorded`, and F07 checks only the balance. It needs `reconcile(reconstructed, metered, tolerance)` stated independently of `meter_reconciliation.py`.
- **F07's own listed variants are partly missing:** partial-interval connection, two vehicles on one charger, and trip/reconnection have no cases (export, no EV flow, BESS discharge and losses do).
- **The oracles are single-interval.** A residual per interval over a window, and the negative-residual case, need a time-series variant of F05.
- **No oracle checks inclusion boundaries** (AC/DC, phases, sign ambiguity). `tree_problems()` checks only duplicates, cycles and child-above-parent.

## What already exists and is reused, not rewritten

| piece | where | role here |
|---|---|---|
| relationship and accounting shapes, inclusion cycles | `device_contract.py` (`RELATIONSHIP_KINDS`, `ACCOUNTING_MODES`, `_validate_relationships()`, `_validate_accounting()`) | the structure DC-003 reads. `ACCOUNTING_MODES` is declared there but `validate()` does not check the mode; DC-003 does |
| resolved values | `device_bindings.py` (PR #1774, open) | every quantity DC-003 adds |
| `included_in_stat` | `device_resolver.device_consumption()` | DC-005's evidence for an inclusion |
| meter reconciliation | `solver/meter_reconciliation.py` (#1605), called from `solver_reports/quality.py` | the verdict, its declared tolerance and status names |
| reconstructed grid | `solver/regret.py`, `evaluate_realized_cost_multi()` (`net_needed = load_real_kw + total_charge_kw − total_discharge_kw − solar_used`) | the formula that, per #1465, needs `load_real_kw` to exclude any EV added separately |
| shared circuit | `solver/elements.py` `SharedCircuitConfig`; `solver/network.py` | `circuit_power` |
| shared charger | `BatteryConfig.shared_charger_group` / `shared_charger_max_kw`; `solver/network.py`; `_widen_shared_charger_cap_to_achieved()` | `charger_power` |

DC-003 adds a pure module that produces the plan. The code above is unchanged unless a test against the oracle shows a divergence, which is then recorded in `contract_divergences.json` and decided, not silently changed.

## Out of scope

- **Storage, revisions and migrating today's settings** (`solver_load_forecast_entities`, shared-charger groups) into the contract: DC-004.
- **Discovering or proposing inclusions and shared equipment:** DC-005.
- **Consumers and readiness:** feeding the plan to the Forecaster, Solver LP, scoring, Topology and cards, and turning result codes into readiness: DC-006.
- **New dispatch behaviour,** and any correction factor fitted from a disagreement (ROADMAP: *"Automatic calibration"* is out of scope).

## Rollback

DC-003 adds a pure accounting module and tests, and no consumer reads them. Rolling back is reverting the PR; no stored state exists.

## Decisions for review

| decision | proposal | owner |
|---|---|---|
| Reconciliation tolerance | keep #1605's declared 10 % of metered throughput, 2 kWh floor, and the 10 % stale-coverage limit for the daily verdict. Publish interval residuals as evidence, with no interval verdict until a per-boundary budget is measured | Mark |
| Meter sign | use the binding's declared sign; try both only for a legacy binding without one | Mark |
| Negative residual | an interval is `residual_negative` below −max(0.1 kW, 5 % of the parent's reading). Above 10 % of a window's intervals, the decomposition is refused for that window. Inclusive demand stays available. Never clamped. The figures are unmeasured proposals | Mark |
| Sign-ambiguous inclusion | a bidirectional child is supported only inside a signed parent at the same boundary; inside a magnitude-only parent it is `inclusion_sign_ambiguous` unless the child is declared one-directional | Mark |
| Undeclared overlap test | `possible_undeclared_inclusion` when, over at least one day of both readings, the candidate child stays at or below the parent within the reconciliation tolerance and correlates at 0.9 or more. Evidence for DC-005 only | Mark |
| Shared quantities | start with `circuit_power`, `charger_power` and `inverter_ac_power`; any other by reviewed PR | Mark |
| EV without connection evidence | `mobility_unresolved`, and that EV's share of exchange is not reconstructed. Location at home does not count | Mark |
| Several reliability reasons | the reconciliation block is always published, and scoring lists every failing reason rather than the first (today a SoC reason hides `grid_meter_disagrees`, [Mark, 9 Oct](https://github.com/code-imstillalive/nimbus/issues/1465#issuecomment-6088958419)). Implemented in DC-006 | Mark |
| Where it lives | `device_accounting.py`, beside `device_contract.py` and `device_bindings.py`, pure, taking resolved values as input | Mark |

This draft authorises no implementation. Its next step is Mark's review.
