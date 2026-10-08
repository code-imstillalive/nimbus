# Nimbus Device Contract Spec DC-000: baseline, fixtures and acceptance gates

Status: Draft for review, revision 0.2. Prepared 7 October 2026, Australia/Brisbane.

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/000-baseline-fixtures-and-gates.md`.

This specification defines the evidence required before implementing the device contract. It proposes test and documentation work only; none of the harnesses, fixtures or passing results described below is claimed to have been implemented by drafting this document.

## Responsibility

Establish a reproducible baseline of Nimbus's current device-related behaviour, independent target expectations for the agreed contract, and enforceable acceptance gates for DC-001 onward. Capture what the software does today without treating known defects as correct behaviour.

The semantic authority is the [accepted device schema](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015647317), adopted in the [upstream response](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015801884). DC-001 will formalise it as the versioned repository contract; DC-000 establishes evidence and explicitly records open semantic decisions rather than resolving them implicitly in a test helper.

## Problem, goals and non-goals

### Problem

A device-led interface can still produce inconsistent results if its consumers retain separate mappings or incompatible accounting. The 5 October #1465 report explicitly included home and EV participants but did not reconcile with measured grid exchange, making it a useful reproduction of a measurement-boundary problem rather than proof that EVs were omitted ([live evidence](https://github.com/code-imstillalive/nimbus/issues/1465#issuecomment-6016381858)).

The previous solver-refactor Spec 000 established golden outputs and isolated execution before extraction. This programme should reuse that discipline, but distinguish intentional contract improvements from behaviour-preserving refactors ([existing golden-master specification](https://github.com/code-imstillalive/nimbus/blob/main/docs/specs/000-golden-master-and-gates.md)).

### Goals

- **Repeatability:** baseline records reproduce in isolated, offline runs at a pinned commit.
- **Traceability:** every P0 roadmap requirement has a fixture, expected outcome, owning specification and current evidence status.
- **Independent correctness:** synthetic arithmetic and boundary oracles do not call the production function being tested to derive their expected answer.
- **Safety:** the harness detects unexpected writes, external requests and actuator calls.
- **Honest gaps:** known defects, unsupported paths, missing evidence and future capabilities are visible and cannot pass through blanket skips.

### Non-goals

- Implementing canonical device storage, the final schema API, migration or a new wizard.
- Changing solver objectives, constraints, dispatch, training, tariffs or controls.
- Replacing published EPR reports or their historical archive.
- Certifying every provider or hardware combination from a small fixture suite.
- Collecting or publishing private household data without explicit scope and approval.

## Baseline and evidence provenance

The proposed initial `main` baseline is **`8a9f50305ca60eb480a1e504d25786763fe42a8a`**, checked while preparing this documentation PR ([baseline commit](https://github.com/code-imstillalive/nimbus/commit/8a9f50305ca60eb480a1e504d25786763fe42a8a)). This updates the first draft's `a70632d` baseline to include the intervening unit-conversion fix and coverage test. The implementation PR must confirm it or record a reviewed replacement before generating baseline snapshots.

The 5 October report records **Nimbus 0.94.438, report schema 2**, not that later `main` baseline. Keep its historical source/version and capture metadata separate; replaying it on a newer commit is a comparison, not reconstruction of the original environment ([report provenance](https://github.com/code-imstillalive/nimbus/issues/1465#issuecomment-6016381858)).

Open PRs are not part of baseline `main` merely because their CI passes. If testing #1585, #1587, #1590 or #1596, pin their head SHAs as separate candidates and record exactly which changes are present.

### Required measured inventory

The DC-000 implementation must produce the following inventory from the pinned code. No coverage percentage, caller count or supported-path claim is assumed by this draft.

| Inventory | Record |
|---|---|
| Configuration authorities | Hub options, subentries, live number/select entities, generated sensors, caches and state files; which is authoritative for each value |
| Producers and consumers | Discovery, providers, Forecaster, Solver, scoring, Topology and cards; where each resolves roles and signs |
| Read/write boundaries | Entity, history, statistics, action and Energy Dashboard reads; config writes, issue-registry writes, publications and control calls |
| Native and standalone paths | Which behaviours require real-shaped HA config entries/subentries or lifecycle events |
| Accounting paths | Source of load, solar, battery and grid quantities; inclusion, aggregation, price and terminal-energy treatment |
| Existing protection | Tests and golden scenarios that reach each path, measured coverage and uncovered early-return branches |
| Compatibility contract | Public entity identities/units, history references, service shapes, effective settings and existing automation-facing outputs |

Use file/function/line references at the named commit. Reuse the repository's existing dependency and golden tools where suitable; do not create a second disconnected gate framework without explaining the gap.

## User and maintainer stories

- **New household:** as a user with only Grid and Load, I want observation without fictitious battery prerequisites.
- **Existing household:** as an owner with working controls, I want previewable migration that does not alter dispatch authority or reinterpret measurements silently.
- **Complex site:** as a household with circuits, solar, a BESS and EVs, I want every transfer counted once regardless of which integration supplies it.
- **Maintainer:** as an implementer, I want a deterministic failing fixture and a precise target before changing accounting or migration behaviour.
- **Verifier:** as a reviewer, I want evidence of the production path exercised, not only a plausible snapshot or a passing stub.

## Two kinds of expected results

Keep these separate in both storage and review. Neither is sufficient on its own.

| Evidence class | Purpose | Change rule |
|---|---|---|
| **Legacy characterisation** | Reproduce current outputs, reads, writes and side effects, including identified defects | Exact preservation for no-behaviour-change work; approved change manifest for intentional corrections |
| **Contract acceptance** | Assert the agreed semantic result with independently derived expectations | Expected changes require contract review, not automatic snapshot regeneration |

For a known defect, retain its old reproducer and describe why it is wrong. An implementation PR should replace the defective production outcome with the independently approved result, not freeze the bug indefinitely.

Future production obligations remain in a machine-readable acceptance registry with an owning spec and status. They must not be counted as passed simply because the implementation does not yet exist. Prefer separate reference-oracle tests and pending integration obligations over broad `skip`/`xfail`; if a narrow strict expected failure is necessary, name the defect and expected failure mode, and fail on unexpected success.

## Proposed harness and fixture layout

All paths below are proposed new files, not commands or interfaces already available. Reuse existing golden isolation and native-HA helpers where they can provide the required observations.

```text
tests/device_contract/
  harness.py
  acceptance_registry.json
  known_defects.json
  fixtures/
    F01_minimal_site/
    ...
    F12_control_and_side_effects/
  snapshots/legacy/
  expected/contract/
  test_repeatability.py
  test_nonvacuity.py
  test_traceability.py
  test_no_unapproved_side_effects.py
  test_reference_oracles.py
scripts/device_contract_baseline.py
```

### Fixture manifest

Every fixture must declare:

- **Identity:** fixture ID/revision, requirement IDs, owner and purpose.
- **Origin:** synthetic or captured; capture time/window, timezone, source versions, baseline SHA and input hashes.
- **Data semantics:** quantity, unit, native/canonical sign, interval start/end, AC/DC boundary, phases, inclusion and independence/derivation provenance.
- **Configuration:** sanitised effective inputs, participant membership, availability and configuration revision; explicitly unknown historical settings.
- **Expected observations:** reads, writes, published states/attributes, config/storage changes, warnings/Repairs, readiness and actuator calls.
- **Coverage:** expected samples/intervals, gaps, stale/held-state interpretation and completeness limitations.
- **Comparison:** exact fields, justified numerical tolerances, approved volatile exclusions and expected unsupported outcomes.
- **Execution status:** reference oracle verified, legacy characterised, production obligation pending/passing/failing, with evidence location.
- **Privacy:** publication approval status and redaction mapping policy, with no secrets or private endpoint in the public manifest.

Use structured half-open intervals `[start, end)` and timezone-aware instants. A local day may not contain 24 hours outside AEST; the fixture's explicit window determines expected coverage.

### Isolation and repeatability

Run scenarios with a frozen clock, deterministic random seeds and isolated temporary storage. Record interpreter, dependencies, solver version and numerical settings; normalise only named nondeterministic fields.

Default-deny external network access. Stub expected reads explicitly, including read-only action responses; fail on an unlisted endpoint or request. Capture HA publications, config/issue-registry operations and actuator calls separately so legitimate test-state publications are not confused with equipment commands.

Run each deterministic fixture three times in fresh processes and once in a changed fixture order. A test that succeeds only due to retained module/global state fails the gate.

## Fixture catalogue

The fixtures below are required specifications for DC-000. A fixture can establish a reference oracle and a pending production obligation without pretending that the future implementation exists.

| ID | Inputs and case | Independent expected result | Owning specs |
|---|---|---|---|
| **F01** | Grid + inclusive whole-house Load; no Solar/Battery; variant with unresolved grid power | Structurally valid site; observation only where evidence exists; no invented zero; no battery-specific mandatory fields | 001, 006, 007 |
| **F02** | One integration exposes Grid/Solar/Battery; two inverters and string/total variants | Logical roles separate from hardware; no string-plus-total duplication; nominal/usable capacity explicit; shared inverter cap represented once | 001 to 003 |
| **F03** | Power, energy, prices and forecasts from different providers; attribute and read-only action sources | One logical device owns resolved roles; no duplication of identity; action is known read-only; statistic without entity remains valid energy input | 002, 005 |
| **F04** | 5/30-minute and multi-day prices; start/end timestamp formats; negative prices; gaps; import/feed-in-only; fees and settlement | Correct interval overlap, no fabricated coverage or missing direction, no doubled fee, distinct forecast/matched/settled values; failed switch preserves active revision | 002, 005, 006 |
| **F05** | Whole-house total includes circuit; circuit includes pool; cyclic and conflicting variants | Inclusive demand unchanged by displaying children; valid decomposition subtracts baseline once and adds schedule once; overlaps/cycles rejected | 003, 006 |
| **F06** | Entity rename, dashboard reorder, repeated discovery, equal candidates and replaced equipment | Stable IDs where continuity is established; no duplicate device; ties and ambiguous replacement unresolved; no writes from candidate generation | 001, 004, 005 |
| **F07** | Synthetic BESS + DC EV + AC EV embedded in load; trip/reconnection and shared charger variants | Mathematically closed site balance, exact-once transfer, consistent achieved/reference/oracle boundaries, explicit mobility and charger connection semantics | 003, 006 |
| **F08** | Sanitised #1465 5 October report and available measured aggregates | Reproduce the mismatch; flag insufficient reconciliation; no invented corrected EPR, raw samples or historical configuration | 003, 006 |
| **F09** | W/kW/MW/mW, wrong dimension, sign ambiguity, held zero, missing/nonfinite samples, resets and rollover | Case-sensitive unit conversion; energy not accepted as power; uncertainty distinct from zero; reset-aware evidence; no inferred rating from historical peak | 002, 005, 006 |
| **F10** | Fully configured legacy site, empty fields, conflicting mappings, interrupted commit, revision race and restart | Dry run leaves effective state unchanged; repeated migration idempotent; conflicting/stale commit refused; history/control links preserved; safe recovery | 004 |
| **F11** | Derived/model dependency graph; consumer revisions; external/deterministic forecasts; unsupported multi-grid planner | Reject self/cyclic sources; all supported consumers share revision; readiness based on capability rather than trained-model flag; unsupported remains explicit | 001, 002, 006 |
| **F12** | Discovered actuators, disabled control, existing authorised control, normal training and actionable faults | Zero actuator calls during setup/migration; existing authority preserved; optional/training status distinct from Repair; stable issues clear when faults resolve | 004 to 007 |

### F07: exact synthetic accounting oracle

Define a one-hour interval at a single AC site boundary with these deliberate idealisations: no losses, no curtailment, no other flows and compatible timestamps. Numbers are synthetic, not inferred from a household.

```text
PV generation                         16 kW
Whole-house measured consumption      14 kW
  of which AC EV charging             10 kW
DC EV charging, outside house total    2 kW
BESS net power                         0 kW
```

The directly balanced site is `14 + 2 − 16 = 0 kW` grid import. In canonical battery signs, both EVs have negative power while charging; the normalisation must produce the equivalent balance.

Two valid representations give the same result:

- **Inclusive:** keep house at 14 kW, add only the excluded DC EV at 2 kW.
- **Decomposed:** house residual is `14 − 10 = 4 kW`; add AC EV 10 kW and DC EV 2 kW once.

The invalid sum `14 + 10 + 2 − 16 = 10 kW` must fail the accounting expectation. Changing the AC EV schedule must remove its embedded baseline before adding the new schedule; suppressing the EV entirely is not the correction.

For reference and oracle paths, apply the same declared decomposition, participant membership, initial energy, losses and applicable availability. A car's propulsion energy or off-site charge does not enter site exchange; its arrival SoC can change the subsequent available energy and must be represented explicitly or scoring declared unsupported. Do not claim that a location-at-home sensor proves connection to a charger.

Add variants for export, no EV flow, non-unity declared losses, partial interval connection and two vehicles sharing equipment. Shared capacity alone does not prove which vehicle was connected, or impose exclusivity unless the equipment contract declares it.

### F08: real evidence without invented ground truth

The published reproduction includes a 9.89% EPR already marked unreliable, fleet charge/discharge of 115.013/100.045 kWh, and a 10:00 hour with approximately zero metered grid import against 10.1479 kW reconstructed import ([evidence comment](https://github.com/code-imstillalive/nimbus/issues/1465#issuecomment-6016381858)).

The test must preserve these as observed evidence with rounding and resolution metadata. It must not promote an hourly mean to a raw five-minute trace, treat pack and charger energy as identical quantities, or assume today's tariff/configuration was active in the original run.

Acceptance for this fixture is initially the ability to reproduce and identify the discrepancy, not an exact corrected score. F07 provides the independent exact-once arithmetic oracle while the physical cause in F08 remains under investigation.

Publishing household-derived inputs requires a reviewed sanitised package. If approval or inputs are unavailable, retain the public aggregate reproduction and record the richer fixture as deferred; the synthetic accounting gate remains mandatory.

## Baseline records and comparison rules

Each run produces a machine-readable record plus a concise human-readable diff. Record:

| Area | Evidence |
|---|---|
| Resolution | Devices/roles proposed or selected, source references, transformations, ambiguity and provenance |
| Effective configuration | Consumer inputs, sign/unit choices, participant lists, accounting mode and supported capability flags |
| Side effects | Ordered reads, publications, config/storage writes, Repairs, warnings and equipment calls |
| Outputs | Relevant forecasts/plans/quality fields and public identities/units, including known-invalid outputs labelled as such |
| Persistence | Before/after storage bytes or canonical content plus hashes, schema/revision and recovery outcome |
| Path execution | Entry point, native/standalone mode, function/branch coverage and non-vacuity assertions |

Use exact comparisons for IDs, roles, units, signs, interval endpoints, dependency sets, status/reason codes, side-effect counts and confirmed configuration. Numerical tolerance must never conceal a missing participant, wrong unit or duplicated quantity.

For F07's idealised arithmetic, propose absolute tolerance `1e-9` in canonical kW/kWh and zero relative tolerance. For rounded published fields in F08, compare within half the stated last displayed decimal where appropriate; this permits rounding only, not physical agreement.

Real meter reconciliation tolerances must be declared per fixture before candidate evaluation, justified by resolution, timing, meter accuracy and conversion boundaries. Where no defensible uncertainty budget exists, report the residual and mark reconciliation unresolved rather than choosing a tolerance that passes it.

An approved behaviour-change manifest must name the requirement, defect, fields allowed to change, old versus new semantic expectation and reviewer. It cannot wildcard all publications or regenerate unrelated snapshots.

## Non-vacuity and negative controls

Tests must prove that the relevant path executed. Configuration with EV subentries must reach participant reconstruction in a native-shaped environment; a standalone fallback returning an empty fleet does not satisfy that scenario.

Introduce controlled fixture mutations and assert failure or the required rejection:

- **Accounting mutation:** add the AC EV twice in F07; the site-balance expectation must fail.
- **Unit mutation:** exchange kW for Wh or multiply power by 1000; validation/reconciliation must detect it.
- **Identity mutation:** duplicate a logical ID or create an inclusion/dependency cycle; structural validation must reject it.
- **Discovery mutation:** make two candidates identical or remove evidence; selection must not remain a confident mapping.
- **Safety mutation:** inject an actuator call into the observed trace; the zero-command gate must fail.
- **Revision mutation:** change effective state after preview; an unreviewed stale commit must not succeed.

Reference-oracle mutation tests are executable in DC-000. Production mutation/behaviour obligations activate as each owning specification is implemented; their pending status remains visible until then.

## Execution and CI contract

The implementation PR should supply a small documented entry point with these logical operations. Exact CLI syntax is to be implemented and reviewed, not assumed to exist today.

| Operation | Required behaviour |
|---|---|
| Inventory | Emit pinned producers/consumers, source references and measured coverage gaps |
| Record legacy | Capture only against an explicit baseline; never update on a normal test run |
| Replay | Run offline in isolated processes and compare declared observables |
| Verify contract | Run independent reference expectations and applicable production obligations |
| Verify gates | Reject missing traceability, unexplained output changes, broad skips and unapproved side effects |
| Report | Separate reference success, legacy parity, production acceptance and deferred evidence |

Normal CI is read-only with respect to committed expectations and cannot access a live household. Baseline regeneration must be an explicit reviewed operation; unexpected network access or repository snapshot changes fail CI.

Run the existing golden master, relevant unit/frontend/native-HA suites and project lint/type checks in addition to the new tests. Quote actual commands and results in the implementation PR; do not turn existing checks off to get a device-contract pass.

## Allowed changes and interface boundaries

DC-000 may add tests, synthetic fixtures, sanitised approved evidence, documentation and narrowly scoped CI/gate tooling. It must not modify production behaviour under `custom_components/`, initialise canonical storage on a live system, migrate settings or switch any consumer.

If baseline capture genuinely requires new production instrumentation, stop and propose a separate, reviewed read-only instrumentation change with its own side-effect tests. Do not smuggle a resolver, migration or scoring fix into the baseline PR.

The harness may define a neutral observation record before the final runtime schema exists. That record is evidence tooling, not a second device model or an implementation of DC-001.

## Acceptance checklist

All checks below are **unverified draft obligations**. The verifier must attach evidence rather than mark them complete from this text.

- [ ] Baseline commit, environment and fixture input hashes are pinned; candidate branch code is not mistaken for baseline.
- [ ] Producer/consumer/write-boundary inventory is measured at that commit, with native/standalone distinctions.
- [ ] F01 to F12 each have a manifest, independent expected outcome, owning spec and explicit execution status.
- [ ] Every P0 requirement DC-R01 to DC-R16 maps to at least one fixture; no obligation disappears into an unowned deferred item.
- [ ] Existing reachable behaviour is characterised; paths that cannot be exercised are named with a reason, not claimed covered.
- [ ] F07's exact accounting oracle and its double-count mutation test pass independently of production accounting code.
- [ ] F08 reproduces only the evidence actually available and labels the corrected score/root cause unresolved.
- [ ] Deterministic cases reproduce across three isolated runs and a changed execution order.
- [ ] Network, actuator and unapproved-write traps are demonstrated by negative controls.
- [ ] Exact and numeric comparison policies are explicit; no tolerance is chosen after inspecting candidate errors.
- [ ] Known-defect ledger separates current wrong outputs from intended contract expectations.
- [ ] Future implementation obligations cannot be reported as completed through skipped tests or absent code paths.
- [ ] Fixture privacy review is recorded; no credentials, endpoints, personal travel history or unapproved raw diagnostics are published.
- [ ] Existing relevant suites remain enabled and their actual results are attached; new tooling does not alter normal production execution.
- [ ] An independent reviewer confirms the report distinguishes document approval, harness implementation and contract implementation.

DC-000 completion means the evidence framework is ready. It does not mean DC-001 to 008 are implemented or that all known production defects are corrected.

## Implementation steps

1. Agree namespace, scope, review owners and baseline; record unresolved decisions.
2. Inventory current paths and reuse suitable golden/native harness components.
3. Implement synthetic fixture manifests and independent semantic expectations.
4. Capture legacy observations at the pinned baseline, including known defects.
5. Add approved aggregate real evidence without manufacturing missing inputs.
6. Add non-vacuity, repeatability, traceability and side-effect gates.
7. Run the existing and new suites, review diffs and publish the measured baseline report.
8. Seek DC-000 acceptance; only then approve dependent implementation work.

Steps may be split into small PRs with explicit dependencies. No partial merge should be described as completion of the whole specification.

## Rollback

Because DC-000 changes only evidence tooling, reverting its implementation should require no production storage conversion or Home Assistant rollback. Verify that assumption by the production-file diff and side-effect record before merging.

If a capture fixture must be removed for privacy, remove the affected public data and update its evidence status while retaining a synthetic substitute and the requirement. Do not silently delete the acceptance obligation.

## Decisions still required

| Decision | Proposed position | Owner and blocking point |
|---|---|---|
| Namespace and upstream location | Separate `device-contract` directory; `DC-*` IDs | Maintainer; before implementation |
| Baseline SHA | `8a9f50305ca60eb480a1e504d25786763fe42a8a`, or reviewed replacement | Maintainer + verifier; before recording |
| Canonical schema governance | DC-001 owns normative schema and approved changes; setup docs reference it | Product owner + maintainer; before DC-001 approval |
| Shared harness reuse | Extend existing isolation/native support where suitable | Implementer + verifier; before new harness design |
| Captured fixture publication | Aggregate public evidence first; richer household captures require approval | Installation/fixture owner; before publication |
| Physical reconciliation tolerance | Per-boundary documented uncertainty budget; unresolved if unjustified | Accounting reviewer; before live acceptance |
| First supported integration slice | Minimum site plus named BESS/EV accounting fixture; provider breadth explicit | Product owner; before consumer cutover |

This draft authorises no upstream posting, implementation, configuration change or production deployment. Its next step is review of the roadmap and DC-000 scope.
