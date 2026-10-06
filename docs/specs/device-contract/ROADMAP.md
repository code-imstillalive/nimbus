# Nimbus Device Contract: specification-driven development roadmap

Status: Draft for review, revision 0.2. Prepared 7 October 2026, Australia/Brisbane.

This roadmap proposes a gated implementation of Nimbus's agreed device-first contract. It is not approval to change an installation, a statement that the contract is implemented, or a replacement for the existing solver-refactor specifications.

## Problem and intended outcome

The user should configure their home once. Grid, Solar, Load and Battery should own their measurement, forecast, price and constraint roles; integrations should supply those roles; all Nimbus consumers should use the same definitions. Upstream has explicitly adopted that direction, including tariff providers attached to Grid and measurement inclusion separate from topology ([contract adoption](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015801884)).

The difficulty is not only the number of setup fields. Inconsistent measurement boundaries can produce plausible-looking but incorrect economics: the published 5 October report included both EVs yet reconstructed 71.579 kWh of grid import against a 32.740 kWh Sigenergy counter delta. EV charging already included in house load is a hypothesis to investigate, not an established root cause ([#1465 reproduction](https://github.com/code-imstillalive/nimbus/issues/1465#issuecomment-6016381858)).

The programme therefore puts semantic correctness and compatibility ahead of a new wizard. Its outcome is one versioned device contract, explicit accounting, tested migration, and a simple interface over those foundations.

## Authority, scope and numbering

The authoritative starting point is the [device schema on #1574](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015647317). The setup experience is described in [PR #1585](https://github.com/code-imstillalive/nimbus/pull/1585). This roadmap operationalises that agreement; it does not introduce a competing schema.

Use a separate namespace:

```text
docs/specs/device-contract/
  ROADMAP.md
  000-baseline-fixtures-and-gates.md
  001-canonical-device-model.md
  002-bindings-and-normalisation.md
  003-accounting-and-shared-constraints.md
  004-migration-and-compatibility.md
  005-discovery-and-provider-selection.md
  006-consumer-integration-and-readiness.md
  007-device-led-setup.md
  008-installation-acceptance-and-rollout.md
```

This PR adds the roadmap and DC-000 at the locations above; DC-001 onward are proposed, not existing files. Refer to the new specifications as **DC-000** through **DC-008**, leaving the existing `docs/specs/000-golden-master-and-gates.md` and numbered refactor series unchanged. The companion draft is [Spec DC-000](000-baseline-fixtures-and-gates.md).

The repository's existing specification template provides the useful discipline of explicit responsibility, invariants, acceptance and rollback; the device programme adds new behaviour, so exact legacy equivalence applies only where compatibility is intended ([existing template](https://github.com/code-imstillalive/nimbus/blob/main/docs/specs/TEMPLATE.md)).

### In scope

- **Logical devices:** stable identities for Grid, Solar, Load and Battery, independent of HA device-registry entries.
- **Typed roles:** sources, units, signs, timestamps, AC/DC and phase boundaries, provenance and versioned adapters.
- **Accounting:** inclusive measurements, validated residuals, shared equipment and constraints, with each transfer counted once.
- **Lifecycle:** migration, revisions, conflict handling, compatibility views, readiness and explicit consumer activation.
- **Experience:** read-only discovery, confirmation, manual fallback, understandable readiness and separate control authorisation.

### Out of scope

- **Dispatch redesign:** no new optimisation objective, battery strategy or actuator integration merely to implement the contract.
- **Universal support:** representing multiple devices does not promise that every solver or provider supports every combination.
- **Automatic calibration:** agreement or disagreement between sensors does not authorise invented correction factors.
- **Uncontrolled migration:** no silent source switching, tariff rewriting, energy-history replacement or enabling of control.
- **Retrospective score replacement:** a corrected historical EPR requires consistent achieved, reference and oracle inputs, not a one-sided adjustment.

## User outcomes and acceptance measures

The following are proposed release gates, not measured performance claims. Spec DC-000 establishes the baseline and leaves unmeasured values explicitly unmeasured.

| User outcome | Proposed acceptance measure |
|---|---|
| A novice can confirm a supported site without assembling internal sensors repeatedly | In the unambiguous fixture, one site confirmation; no repeated entry of a confirmed role across Forecaster, Solver and cards |
| A Grid-and-Load installation is valid | Completes observation setup with no Solar or Battery required; unsupported functions explain their prerequisites |
| Device totals mean the same thing everywhere | Every adopted consumer uses the same contract revision and validated accounting plan |
| Existing installations remain safe | Zero unintended effective mapping, accounting, history-link or control-authorisation changes in migration fixtures |
| Discovery does not guess | Every ambiguous fixture asks or returns insufficient evidence; no lexical tie-breaking or absence-as-zero |
| Economic results are defensible | Scoring checks applicable measurement reconciliation and provenance; failing evidence produces a visible reliability reason |
| Setup does not actuate equipment | Zero actuator calls in discovery, migration preview, confirmation and readiness tests |

Completion time, assistance required and number of unresolved questions should also be recorded during novice trials. Do not invent a percentage improvement before measuring the current path.

## Delivery sequence

### Now: establish evidence before new behaviour

| Spec | Responsibility | Dependencies | Exit evidence |
|---|---|---|---|
| **DC-000** | Pin the baseline; inventory producers and consumers; build characterisation fixtures, independent target oracles and gate tooling | Approval of scope and fixture privacy | Repeatable offline runs; explicit known-defect ledger; all P0 requirements mapped to fixtures and owning specs |
| **DC-001** | Define the canonical model, stable IDs, schema versioning and structural validation | DC-000 | Minimal site, identity stability, round-trip serialisation, invalid-reference rejection; no consumer switch |
| **DC-002** | Resolve typed bindings into canonical quantities and intervals | DC-001 | Unit/sign conversion, source variants, freshness, interval overlap, coverage and provenance tests |
| **DC-003** | Define accounting boundaries, inclusion, decomposition and shared constraints | DC-001 and DC-002 | Independent energy-balance oracles; pool/circuit and BESS/EV cases; explicit unsupported-topology results |

DC-001 owns the approved normative schema in the repository, with an explicit mapping from the accepted #1574 proposal. Other specifications reference it rather than copying it. New field names or semantic changes require a reviewed contract decision, not an incidental implementation choice.

### Next: migrate and integrate without creating two authorities

| Spec | Responsibility | Dependencies | Exit evidence |
|---|---|---|---|
| **DC-004** | Legacy import, dry-run comparison, transactional canonical storage and compatibility views | DC-001 to 003 | Idempotent migration; named conflicts; revision checks; interrupted-write recovery; tested rollback boundary |
| **DC-005** | Discovery, evidence ranking, provider adapters and validated source selection | DC-001 to 002; DC-003 for accounting assertions; DC-004 for activation | No discovery writes; confirmed provenance retained; ambiguous evidence surfaced; failed source switch leaves active revision intact |
| **DC-006** | Move consumers to shared definitions, accounting and per-function readiness | DC-002 to 004 | One bounded PR per consumer; declared permitted differences; metered reconciliation and reliable source/version labels |

DC-005's candidate-generation work can progress alongside DC-004 after the shared interfaces are approved. It cannot activate a configuration before DC-004's commit and rollback gates pass.

DC-006 should be split into reviewable PRs: observation/Topology, forecasting, planning, scoring, then cards and compatibility completion as dependencies permit. Do not give one consumer a new accounting model while another silently retains a contradictory one; retain legacy mode or mark the unsupported capability until a coherent slice is ready.

### Later: make the validated model simple to use

| Spec | Responsibility | Dependencies | Exit evidence |
|---|---|---|---|
| **DC-007** | Device-led discovery/confirmation, Advanced editing and readiness guidance | DC-004 to 006 | Minimal-site and multi-device walkthroughs; no repeated plumbing; normal training is status, not a fault; control remains separately authorised |
| **DC-008** | Clean-install, upgrade, limited rollout and release acceptance | Each supported slice of DC-001 to 007 | Independent review, devhub evidence, consenting-install acceptance, rollback rehearsal and explicit support matrix |

Design prototyping may occur early. User-visible activation of a slice is gated by that slice's data, accounting, migration and consumer acceptance, not by a calendar date or completion of a screen.

## Core requirements and traceability

P0 requirements gate the relevant supported slice. P1 items improve breadth after the core gates; deferred capabilities must remain explicit rather than be simulated with defaults.

| ID | Priority | Requirement | Owning spec | DC-000 fixtures |
|---|---|---|---|---|
| DC-R01 | P0 | Valid minimum Grid + aggregate Load; Solar/Battery optional | 001, 007 | F01 |
| DC-R02 | P0 | Logical identity survives rename, reorder and rediscovery | 001, 004, 005 | F02, F06 |
| DC-R03 | P0 | Typed sources with unit, native/canonical sign, boundary and provenance | 002 | F02 to F04, F09 |
| DC-R04 | P0 | Missing, zero, unavailable and stale remain distinct | 002, 006 | F04, F09 |
| DC-R05 | P0 | Tariff is a Grid provider; directions, fees and settlement are distinct | 002, 005 | F03, F04 |
| DC-R06 | P0 | Time-aware intervals preserve coverage and quantity semantics | 002 | F04 |
| DC-R07 | P0 | Measurement inclusion differs from electrical topology and equipment sharing | 003 | F05, F07 |
| DC-R08 | P0 | Inclusive totals and residual-plus-children do not double-count | 003, 006 | F05, F07 |
| DC-R09 | P0 | Shared limits constrain the declared combined quantity once | 003, 006 | F02, F07 |
| DC-R10 | P0 | Measured/reconstructed site exchange and scoring evidence reconcile or visibly fail | 003, 006 | F07, F08 |
| DC-R11 | P0 | Basic, Advanced and supported consumers use one revisioned authority | 004, 006, 007 | F10, F11 |
| DC-R12 | P0 | Migration preserves intent, history links and existing control authorisation | 004 | F10, F12 |
| DC-R13 | P0 | Discovery proposes; ambiguous evidence is never silently activated | 005 | F06, F09 |
| DC-R14 | P0 | Readiness is per function; unsupported combinations are explicit | 006 | F01, F04, F09, F11 |
| DC-R15 | P0 | Setup/migration/discovery send no actuator commands | 004, 005, 007 | F12 |
| DC-R16 | P0 | Derived/model dependencies are acyclic and not self-referential | 001, 002, 006 | F11 |
| DC-R17 | P1 | Expand provider and equipment profiles after the adapter contract is proven | 005 | Additional versioned F03/F04 variants |
| DC-R18 | P1 | Refine novice task time and guidance using recorded walkthrough results | 007, 008 | F01 plus observed walkthroughs |

## Relationship to work already upstream

The following is the status reviewed on 7 October, not a promise that these branches remain unchanged. Treat each PR as a candidate contribution to a spec and rerun the applicable acceptance tests at its actual merge commit.

| Existing work | Role in this programme | Boundary |
|---|---|---|
| [#1585](https://github.com/code-imstillalive/nimbus/pull/1585) | Setup design feeding DC-007 | Open design, not device storage or runtime implementation |
| [#1590](https://github.com/code-imstillalive/nimbus/pull/1590) | Resolver groundwork for DC-005 | Open; pure candidate generation, not called by production yet |
| [#1587](https://github.com/code-imstillalive/nimbus/pull/1587) | Transitional gap repairs relevant to DC-004/007 | Open; confirm before filling a field; no new independent device authority |
| [#1588](https://github.com/code-imstillalive/nimbus/pull/1588) | Existing health evidence feeding DC-006/007 | Merged Repairs implementation, not proof of full per-function readiness |
| [#1596](https://github.com/code-imstillalive/nimbus/pull/1596) | Energy Dashboard compatibility for DC-005 | Open, independently useful fix; not the canonical device model |
| [#1550](https://github.com/code-imstillalive/nimbus/issues/1550) | Provider discovery/normalisation requirements | Provider support must be declared individually |
| [#1465](https://github.com/code-imstillalive/nimbus/issues/1465#issuecomment-6016381858) | Accounting/scoring evidence for DC-003/006 | Reproduced mismatch; no corrected EPR or unique root cause asserted |

Useful bug fixes do not need to wait for the whole programme. They must retain their own review and tests, declare their relationship to the contract, and not establish a conflicting second configuration model.

## Gates, review and change control

Each specification progresses through **draft → approved → implemented → independently verified → released → installation-accepted**. Approval of a document is not implementation; passing CI is not live acceptance; a release tag is not evidence that a particular installation has adopted it.

- **Specification gate:** product intent, scope, interface, fixtures, exclusions, dependencies and rollback are reviewed before implementation.
- **Prior-art gate:** before approving new solver, load, scoring or output mechanisms, record the repository-required EMHASS and HAEO source review, applicable semantics, licence constraints and any justified departure. This roadmap proposes the development process; it does not claim that mechanism-level review has been completed.
- **Implementation gate:** tests exercise the actual path; native HA code cannot pass only because a standalone path returned early.
- **Compatibility gate:** effective settings and existing observable outputs remain unchanged unless an approved change manifest identifies the intended difference.
- **Defect-correction gate:** a known defect has a reproducer and independent corrected oracle; do not regenerate a golden file merely to bless the new output.
- **Installation gate:** run clean-install and upgrade scenarios on a test installation, then an explicitly authorised household slice. Do not use a production energy system as an unannounced test.

Every implementation PR should list requirement IDs, fixture IDs, baseline and candidate SHAs, test commands/results, permitted behaviour changes, observed side effects, unresolved defects and rollback instructions. One bounded implementation unit per PR is the default; a large specification can use several dependent PRs.

Proposed review roles are: product owner for user outcomes and contract decisions; upstream maintainer for implementation and release acceptance; independent verifier for evidence and gate checks; installation owner for deployment and any control authority. Named assignments remain to be agreed.

## Risks and decisions before implementation

| Decision or risk | Required resolution | Gate owner |
|---|---|---|
| New series could be confused with refactor Spec 000 | Agree the `DC-*` namespace and proposed directory | Maintainer, before DC-000 merge |
| Existing schema is a discussion, not a frozen machine contract | Approve field semantics and version policy in DC-001 | Product owner + maintainer |
| AC/DC, phase and inclusive-load semantics differ across integrations | Declare supported boundaries; reject or defer unproven combinations | Accounting owner, DC-003 |
| Writing canonical and legacy state could create two masters | Agree atomic revision model and one-way compatibility policy | Maintainer, DC-004 |
| Real household fixtures contain private data | Obtain publication approval for sanitised fixtures; keep synthetic equivalents mandatory | Fixture owner, before publication |
| Provider coverage and hardware breadth could expand scope indefinitely | Publish a capability matrix; gate only declared supported slices | Product owner, DC-005/008 |
| Passing data reconciliation may still conceal correlated inputs | Record independence and derivation provenance; do not call agreement proof | Verifier, DC-000/003 |
| Reverting code may not revert migrated storage | Document and rehearse restore/export path; no blind downgrade | Maintainer + installation owner, DC-004/008 |

## Immediate proposed action

Approve this roadmap and the scope of DC-000, then implement only the baseline, fixtures and gate tooling. At that checkpoint, review the measured gaps and approve DC-001 to 003 before any device-model migration or consumer cutover.

No public issue, PR, deployment or configuration change is authorised by this draft alone. Posting and implementation are separate next steps.
