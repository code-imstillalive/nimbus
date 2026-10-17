# Nimbus Device Contract Spec DC-004: legacy import, dry run, transactional storage and compatibility views

Status: Draft for review, revision 0.1. Prepared 18 October 2026, Australia/Brisbane. Drafted, as DC-001 to DC-003 were, for Mark Purcell's review on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574) ("you draft, I sign off").

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/004-migration-and-compatibility.md`. Depends on [DC-000](000-baseline-fixtures-and-gates.md), [DC-001](001-canonical-device-model.md), [DC-002](002-bindings-and-normalisation.md) and [DC-003](003-accounting-and-shared-constraints.md).

This specification proposes behaviour. It does not claim that any of the storage, import or view code below exists. It authorises no consumer switch (DC-006) and no change to any installation.

## Responsibility

DC-004 is where the contract first touches stored state. It answers the two risks the roadmap assigns it: *"Writing canonical and legacy state could create two masters"* and *"Reverting code may not revert migrated storage"*. Its output is a stored, revisioned device document per config entry. Today's settings become **views generated from it, in one direction only**.

| DC-004 owns | DC-004 does not own |
|---|---|
| Reading today's settings into a DC-001 document (legacy import) | The document's shape, IDs and structural validation (DC-001) |
| The dry run: what the import would store, and whether any consumer's effective input would change | Whether a binding resolves, and to what value (DC-002) |
| Canonical storage, revisions, compare-and-swap commit and interrupted-write recovery | Whether the accounting closes (DC-003); DC-004 stores a plan's inputs, not the plan |
| Generating today's settings from the document (compatibility views) | Consumers reading the document directly instead of the views (DC-006) |
| Lifecycle events: explicit deletion, replacement, reverting to an earlier revision | Proposing devices, bindings or inclusions from discovery (DC-005) |
| Carrying existing control authority across, unchanged | Granting new control authority (DC-007) |
| The rollback boundary and the restore path | Rehearsing it on a consenting installation (DC-008) |

## Requirements owned (from DC-000's registry)

| ID | Requirement | Fixtures | What DC-004 must make checkable |
|---|---|---|---|
| **DC-R12** | Migration preserves intent, history links and existing control authorisation | F10, F12 | Every consumer's effective input is identical before and after, except for entries in an approved change manifest |
| **DC-R11** (with 006, 007) | Basic, Advanced and supported consumers use one revisioned authority | F10, F11 | Exactly one writable store per entry; every legacy key the contract owns is a generated view carrying the revision it came from |
| **DC-R02** (with 001, 005) | Logical identity survives rename, reorder and rediscovery | F02, F06 | Import assigns IDs once; re-import and restart assign none |
| **DC-R15** (with 005, 007) | Setup, migration and discovery send no actuator commands | F12 | Import, dry run, commit, recovery and revert make zero service calls outside `nimbus_load` |

## Where today's settings live

Read at `origin/main` on 18 October 2026. DC-004 must name every source it imports from, and every source it deliberately leaves alone.

| store | examples | contract role |
|---|---|---|
| Config entry options, Forecaster settings | `battery_sensor`, `grid_sensor`, `solar_sensor` | `power` bindings on the Battery, Grid and Solar devices |
| Config entry options, Solver settings | `solver_battery_power_sensor` (+ `solver_battery_power_positive_is_charge`), `solver_solar_power_sensor`, `solver_import_price_sensor` and `_2`/`_3`, `solver_export_price_sensor` and `_2`/`_3`, `solver_solar_forecast_sensor` and `_2`/`_3`, `solver_load_forecast_sensor`, `solver_load_forecast_entities`, `solver_grid_max_import_kw`, `solver_grid_max_export_kw` | bindings and constraints on the same devices. The same physical sensor is often named here *and* in Forecaster settings |
| Live number entities (`Store` key `nimbus_load_<entry_id>_solver_numbers`) | battery capacity, max charge, max discharge, grid limits | constraints. **The solve reads these live entities** (`sensor.py`, #565/#567), seeded from the options. Two copies of one value already exist today |
| Subentries | `load`, `power_signal`, `power_source`, `pv_string`, `battery_tower`, `battery_participant`, `controllable_load` | devices, bindings, relationships and shared constraints (a participant's `shared_charger_group`) |
| `controllable_load_device_entity` on a Controllable Load | `switch.*`, `water_heater.*`, `climate.*` | **existing control authority**: Nimbus already commands that entity |
| Setup-builder record | `setup_builder_done` | provenance: a field the household confirmed through a Repair (#1587) |

**Not imported, and not changed:**
- Behaviour tunables that are not device facts: risk aversion, feature toggles (`solver_offer_curve_enabled` and the rest), SoC discrepancy thresholds, household mode. They stay where they are.
- History and runtime state: quality history, forecast snapshots, load run state, decision inputs and the Forecaster dashboard store. Their keys include the entry ID or the subentry ID, and DC-004 changes neither.

`config_flow.py` declares `VERSION = 1` and defines no `async_migrate_entry`. Nothing in the config entry's own version changes under this spec. The canonical document has its own store and its own `schema_version` (DC-001).

## One authority: three modes per entry

The two-masters risk is real today: the battery capacity lives in the options and in a number entity, and the battery sensor lives in Forecaster settings and in Solver settings. DC-004 must not add a third copy that disagrees with both. Each config entry is in exactly one mode, and the mode is stored, never inferred:

| mode | canonical store | who is the master | legacy keys the contract owns |
|---|---|---|---|
| `legacy` (every install today) | absent | the legacy settings, as now | edited by today's forms, as now |
| `shadow` | present, revisioned | **still the legacy settings** | untouched. Each start, and each options update, re-imports and **compares**; a difference is published as evidence and never written anywhere |
| `active` | present, revisioned | **the canonical document** | generated from it; a direct edit is drift (below) |

- **`legacy` → `shadow`** is a household action: a Repair or a button after a clean dry run. It writes only the canonical store.
- **`shadow` → `active`** is a second household action. It requires the dry run to show no effective difference, or only differences listed in an approved change manifest. It is the first point at which any legacy key may be written.
- **`active` → `legacy`** is the revert path (see Rollback). It is always available and never needs a code downgrade.

DC-005's discovery can propose a document in any mode. It can commit one only in `active`, and only after DC-004's gates pass (ROADMAP: *"It cannot activate a configuration before DC-004's commit and rollback gates pass"*).

### Drift in `active` mode

A form, a service or a number entity can still write a legacy key after activation. That is the second master returning, so it is never resolved silently in either direction:
- **Detected.** Each view carries the revision it was generated from. A key whose value differs from the view at that revision is drift.
- **Shown.** One Repair per drifted key names the key, the stored value and the view value.
- **Resolved by the household.** The Repair offers *"Keep my change"*, which commits it as a new revision through the normal commit path, or *"Restore"*, which regenerates the view. Until one is chosen, consumers read the view value.
- **Number entities are the hard case.** They exist to be edited live from the dashboard. Proposal: in `active` mode a number entity that the contract owns writes through the commit path as a new revision. Its slider is then an editor of the document, not a second copy. Decision below.

## Legacy import

A pure function with no HA calls:

```text
import_legacy(snapshot) -> ImportResult(document, conflicts, unmapped, provenance)
```

`snapshot` is a plain dictionary: the entry's options, its subentries' data, the live number values and `setup_builder_done`. The caller reads it from HA. The function never reads it.

- **Deterministic.** The same snapshot always gives the same serialised document (DC-001's canonical form). IDs come from the role (`grid`, `battery_home`, `solar_home`, `load_house`), with `_2`, `_3` suffixes in subentry creation order. They are assigned on the first import and stored. A later import of a changed snapshot matches existing devices through `registry_id` (DC-001's identity rule) and never re-derives an ID.
- **Idempotent.** Importing again from an unchanged snapshot, after a stored document exists, produces no new revision and no new device (F10 "repeated migration").
- **An empty field is not permission.** An empty Solver field with a filled Forecaster field imports as one binding with the Forecaster's provenance. It does not fill the Solver field; that remains #1587's Repair, which needs a click.
- **Conflicts are named, never resolved by order.** Each one goes into `conflicts` with both sources. The document keeps neither value until the household chooses. A dry run with an unresolved conflict cannot enter `shadow`.

| code | case |
|---|---|
| `conflict_role_sources` | two different sensors for the same role on the same device (Forecaster `battery_sensor` ≠ `solver_battery_power_sensor`) |
| `conflict_sign` | the same sensor with two sign declarations |
| `conflict_setting_copies` | an option and its live number entity disagree (capacity in the options ≠ the capacity number) |
| `conflict_shared_group` | two participants in one `shared_charger_group` declare different `shared_charger_max_kw` |
| `unmapped_subentry` | a subentry whose type or fields have no mapping in this version. It is kept in `unmapped` and reported, never dropped |

- **Provenance.** Every binding and constraint records `origin: legacy_import`, the source key path (`options.solver_battery_power_sensor`, `subentry:<id>.power_sensor`) and, where the household confirmed it through a Repair, `confirmed_by: setup_builder`.
- **History links.** Each device built from a subentry records that `subentry_id`. DC-006 uses it to keep every Nimbus entity's `unique_id`, and with it its recorder history and long-term statistics.

## Dry run

```text
preview(snapshot, stored=None) -> Preview(document, conflicts, effective_diff, change_manifest_hits)
```

- **Writes nothing.** No store, no options, no subentry, no Repair; the caller decides what to show.
- **`effective_diff`** compares, for every consumer, the inputs it reads today with the inputs it would read from the generated views. The comparison covers the entity ID, attribute path, sign, unit and constant, per key. The pass condition is an empty diff, or a diff in which every entry matches an approved change manifest (`tests/device_contract/approved_changes.json`, DC-000).
- **Shown before anything is committed.** The household sees the document as devices ("Battery: *Sigenergy battery power*, charge positive"), the conflicts and the diff. This is the screen DC-007 later rebuilds; DC-004 needs only a Repair-sized version.

## Storage, revisions and commit

**One store per entry:** `Store(hass, 1, "nimbus_load_<entry_id>_device_contract", atomic_writes=True)`. HA's `Store` defaults to `atomic_writes=False`, so the flag is set explicitly.

```json
{
  "mode": "shadow",
  "revision": 3,
  "document": {"schema_version": 1, "site": {"...": "..."}},
  "committed_at": "2026-10-18T09:00:00+10:00",
  "origin": "legacy_import",
  "previous": [{"revision": 2, "document": {"...": "..."}, "committed_at": "..."}]
}
```

- **Compare-and-swap.** `commit(expected_revision, document, origin)` refuses with `revision_conflict` when the stored revision is not `expected_revision`. Two tabs, a Repair and a service cannot overwrite each other (F10 "revision race").
- **Validated before writing.** `device_contract.validate()` must return no problems. A refused commit leaves the stored revision exactly as it was.
- **Monotonic.** A revision number is never reused, including after a revert.
- **History kept.** The previous revisions stay in `previous`, so a revert is a new commit of an old document.

### Commit order in `active` mode, and recovery

The legacy keys live in the config entry, and the document lives in its own store. HA cannot write both atomically, so the order is fixed and the gap is recoverable:
1. Write the store with revision N+1 and `view_revision: N`.
2. Generate the views from revision N+1 and write them with `async_update_entry`. Record `device_contract_view_revision: N+1` in the options in the same update.
3. Write the store again with `view_revision: N+1`.

**On every start:** if the store's revision is ahead of the options' `device_contract_view_revision`, regenerate the views from the stored document. View generation is a pure function of the document, so repeating it is safe (F10 "interrupted commit", "restart"). If the options are *ahead* of the store, which cannot happen through this path, the entry falls back to `shadow` and a Repair names the inconsistency. Nimbus never guesses which side is right.

## Compatibility views

```text
legacy_view(document) -> {options: {...}, subentries: {subentry_id: {...}}, numbers: {...}}
```

- **One way.** Views are generated from the document and never read back into it, except through the drift Repair's explicit *"Keep my change"*.
- **Owned keys only.** A view writes only the keys in the import table. Every key outside it, including the behaviour tunables, is left byte-identical.
- **Readable by the previous release.** A view uses only keys and meanings that the last release before DC-004 understands. That is the rollback boundary (below). A view that needs a new key is a change to the boundary and needs its own decision.
- **No subentry is created or removed by a view.** A device added in the document with no subentry stays document-only until DC-006 gives it a consumer. Adding subentries from the document would create new entities and new history, which is DC-006's decision.

## Lifecycle events

- **Deletion is explicit** (DC-001). A device or binding is removed only by a commit that names it. An entity that disappears, goes unavailable or stops reporting is a readiness problem (DC-006), never a deletion.
- **Replacement** keeps the device ID and changes its binding in a new revision, with the old binding in `previous`. A new inverter is the same Solar device with a new source, so its forecasts and history links stay.
- **Revert to revision N** is a commit of `previous[N].document` as revision N+k, through the same validation and dry run.

## Control authority (DC-R12, DC-R15)

**Today's authority, read from the code:** Nimbus commands an entity only when a Controllable Load has `controllable_load_device_entity` set. Battery dispatch on the reference household is done by the household's own automations reading Nimbus's published plan. That is outside Nimbus and is not represented in the document.

- **Import carries authority across and adds none.** Each configured `controllable_load_device_entity` becomes a control binding on that load. The site's `control_policy` is `authorised` only if at least one exists, and `observation_only` otherwise. A load without a device entity gets no control binding, even if discovery later finds a switch for it.
- **Nothing in DC-004 changes authority.** Import, dry run, commit, recovery, revert and view generation never add, remove or alter a control binding except as an imported copy. A change to control is DC-007's separate authorisation.
- **Zero actuator calls.** A test wraps `hass.services.async_call` and asserts no call outside the `nimbus_load` domain across import, preview, commit (both modes), recovery and revert (F12).

## Acceptance

Tests live under `tests/device_contract/`, beside DC-000's oracles. Each names its requirement and fixture, and each runs the production code against an independent expected outcome, never against itself.

- **DC-R12 / F10, fully configured legacy site:**
  - import, then preview: `effective_diff` is empty;
  - commit to `shadow`, then `active`: every consumer input read through the views equals the input read before migration, key by key;
  - every Nimbus entity's `unique_id` is unchanged, and every subentry keeps its ID;
  - the behaviour tunables and the history stores are byte-identical.
- **F10, empty fields:** an empty Solver field with a filled Forecaster field imports one binding and leaves the Solver field empty.
- **F10, conflicting mappings:** each `conflict_*` code has one case. The document holds neither value, and `shadow` is refused while the conflict stands.
- **F10, interrupted commit:** a failure injected after each of the three commit steps, then a restart, gives the same views as an uninterrupted commit.
- **F10, revision race:** two commits from the same `expected_revision`; one succeeds and the other gets `revision_conflict`. The stored document is the winner's.
- **F10, repeated migration:** import twice; the second produces no revision and no device.
- **F10, drift:** a legacy key edited in `active` mode raises one Repair. Each of *"Keep my change"* and *"Restore"* gives the expected revision and view.
- **DC-R02 / F06:** a sensor rename (same `registry_id`) and a subentry reorder keep every device ID across re-import.
- **DC-R11 / F10, F11:** in `active` mode, exactly one writable store exists, and every owned legacy key carries `device_contract_view_revision`.
- **DC-R15 / F12:** the zero-actuator test above, plus: a site with an authorised Controllable Load stays authorised for exactly that load, and a site without one stays `observation_only`.
- **Rollback:** the views generated from F10's document load cleanly under the last pre-DC-004 release's config-entry reader (`tests/hass_integration/`). `active` → `legacy` leaves the options as the last view.
- **Every result code has a negative test**, as in DC-001 and DC-003.
- **No consumer reads the document yet** (until DC-006). The views are the only consumer-facing output.

### DC-000 additions this needs

- **F10 has no manifest and no oracle** (registry: `reference_oracle: not_started`). It needs a synthetic fully configured entry snapshot, its expected document and its expected views, written independently of `import_legacy()`. A sanitised real install is optional and needs the household's publication approval (ROADMAP risk table).
- **F06 has no manifest.** It needs the rename and reorder cases (the equal-candidates case is DC-005's).
- **F12 has no manifest.** DC-004 needs only its "existing authorised control" and "disabled control" cases.
- **A legacy characterisation of the import table above**, so a key added to the options later fails a test until it is classified as owned or left alone.

## What already exists and is reused, not rewritten

| piece | where | role here |
|---|---|---|
| schema, `validate()`, `serialise()`, `parse()`, `SCHEMA_VERSION` | `device_contract.py` | the document DC-004 stores, and the check before every commit |
| bindings resolution | `device_bindings.py` | not called by import. The dry run uses it only to show current readings beside each binding |
| "offer, don't fill" and the never-twice record | `setup_builder.py` (`setup_builder_done`) | the pattern for conflict and drift Repairs, and a provenance source |
| Repairs | `repairs.py`, `setup_health.py` | where conflicts and drift appear |
| HA `Store` | `helpers.storage` | the canonical store, with `atomic_writes=True` |
| diagnostics download | `diagnostics.py` | gains the stored document and its revisions (read only), as the export path |

## Out of scope

- **Consumers reading the document directly,** and readiness from it: DC-006.
- **Discovery proposing devices or changes:** DC-005. Its proposals reach storage only through `commit()`.
- **The device-led setup screens:** DC-007. DC-004 needs only Repairs and one confirmation step.
- **Changing any behaviour tunable, any history store or the config entry's own `VERSION`.**
- **A downgrade path that rewrites storage.** A revert goes through `active` → `legacy`, never through older code reading newer data.

## Rollback

The boundary is set by the views, not by the store:
- **Code rollback.** A release before DC-004 does not know the canonical store and ignores it. It reads the options, which in `active` mode are the last generated view. Because views use only keys the previous release understands, that release runs on the last committed configuration. This is tested (Acceptance, Rollback), not assumed.
- **Mode rollback.** `active` → `legacy` stops generating views and leaves the options as they are. The store is kept, so a return to `shadow` or `active` needs no re-import. Deleting it is a separate household action.
- **Revision rollback.** A revert to an earlier revision is a normal commit.
- **Export and restore.** The diagnostics download carries the document and its previous revisions. A restore from it is a commit. The household's HA backup covers the store file like any other `.storage` file. DC-008 rehearses the restore on devhub before any consenting installation.

What is **not** reversible by reverting code: a commit made in `active` mode has already rewritten the owned legacy keys. They hold the committed configuration, which is the intended state, but the pre-migration values exist only in the store's first revision and in the household's backup. That is the reason `shadow` exists and the reason `active` is a separate action.

## Decisions for review

| decision | proposal | owner |
|---|---|---|
| Three modes | `legacy` → `shadow` → `active`, each step a household action; `active` → `legacy` always available | Mark |
| Live number entities in `active` mode | a contract-owned number entity commits a new revision on change, so the slider edits the document rather than holding a second copy | Mark |
| Drift resolution | one Repair per drifted key, *"Keep my change"* or *"Restore"*; consumers read the view until chosen | Mark |
| Imported control authority | `authorised` only when a Controllable Load already has a device entity, with a control binding on exactly those loads; household battery automations outside Nimbus are not represented | Mark + household |
| ID assignment on import | from the role, suffixed in subentry creation order, stored once | Mark (DC-001 format already agreed) |
| Revisions kept | the last 10 in `previous`; older ones are dropped from the store but remain in backups | Mark |
| Storage key | `nimbus_load_<entry_id>_device_contract`, one per config entry, separate from the config entry so the entry's `VERSION` does not change | Mark |
| Where it lives | `device_storage.py` (store, commit, recovery, views) and `device_import.py` (pure import and preview), beside `device_contract.py` | Mark |
| Real-install fixture | synthetic F10 is mandatory; a sanitised reference-household snapshot only with the household's approval | household |

This draft authorises no implementation. Its next step is Mark's review.
