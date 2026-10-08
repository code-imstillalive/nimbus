# Nimbus Device Contract Spec DC-001: canonical device model, identity, versioning and structural validation

Status: Draft for review, revision 0.1. Prepared 8 October 2026, Australia/Brisbane. Drafted at Mark Purcell's request on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574) ("you draft, I sign off").

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/001-canonical-device-model.md`. Depends on [DC-000](000-baseline-fixtures-and-gates.md).

This specification proposes the normative schema. It does not claim any of the types, validators or stored data below exist. It authorises no consumer switch, migration or storage change; those are DC-004 and DC-006.

## Responsibility

DC-001 owns the repository's normative device schema: the shapes of `Site`, `Device`, `Binding`, `Relationship`, `Constraint` and `Accounting`; stable identity; `schema_version` and how it changes; and **structural** validation, meaning everything that can be checked without reading a sensor.

- **Semantic authority:** the [accepted #1574 schema](https://github.com/code-imstillalive/nimbus/issues/1574#issuecomment-6015647317). Every field below maps to it; the mapping table says where.
- **Other specs reference this one rather than copying it** (ROADMAP: *"New field names or semantic changes require a reviewed contract decision"*).

| DC-001 owns | DC-001 does not own |
|---|---|
| Field names, types, required/optional, enumerations | Converting a sensor's value, unit or sign into canonical quantities (DC-002) |
| Stable IDs and what preserves them | Whether two candidates are the same equipment (DC-005 evidence; DC-001 only says what a confirmed continuity looks like) |
| `schema_version` and the change policy | Storage, transactions, migration from today's settings (DC-004) |
| Reference integrity, uniqueness, acyclicity, kind-role compatibility | Accounting correctness: decomposition, overlap, energy balance (DC-003). DC-001 checks that accounting *refers* to real devices, not that the arithmetic closes |
| Round-trip serialisation | Readiness evaluation (DC-006) |

## Requirements owned (from DC-000's registry)

| ID | Requirement | Fixtures | What DC-001 must make checkable |
|---|---|---|---|
| **DC-R01** | Valid minimum Grid + aggregate Load; Solar/Battery optional | F01 | A site with exactly one `grid` and one `load` (class `aggregate`) and nothing else validates. No Solar or Battery field is required. An unresolved binding is **absent**, not zero |
| **DC-R02** | Logical identity survives rename, reorder and rediscovery | F02, F06 | IDs are assigned once and are independent of entity IDs, HA device-registry IDs and Energy Dashboard order |
| **DC-R16** | Derived/model dependencies acyclic and not self-referential | F11 | A `derived` or `model_output` binding cannot depend, directly or transitively, on itself |

## The model

The YAML shapes below are the normative structure. Names are #1574's, unchanged, except where the mapping table says otherwise.

### Site

```yaml
schema_version: 1          # integer, see "Versioning"
site:
  id: site_home            # ID rules below
  name: Home
  timezone: Australia/Brisbane   # IANA name, required
  currency: AUD                  # ISO 4217, required
  control_policy: observation_only   # observation_only | authorised ; default observation_only
devices: [...]
relationships: [...]
accounting: {...}
shared_constraints: [...]
```

- **One site per document.** Several grid connections are representable (several `grid` devices) but DC-001 does not promise any consumer supports them. Unsupported topologies are reported by readiness (DC-006), not refused here.
- **`control_policy` defaults to `observation_only`.** Nothing in this schema, including discovered control endpoints, changes it (F12).

### Device

```yaml
- id: battery_home
  kind: battery            # grid | solar | load | battery
  name: Home battery       # display only; never used for identity
  boundary:
    id: battery_ac_terminals   # free-form boundary identifier, unique per site
    domain: ac             # ac | dc
    phases: all            # all | L1 | L2 | L3 | list of those
  bindings: {role: Binding, ...}
  constraints: {...}       # kind-specific, see below
  # kind-specific fields:
  load_class: aggregate    # load only: aggregate | circuit | appliance
  flexibility: {mode: none}  # load only
  capacity: {value: 40.3, unit: kWh, basis: usable}   # battery only
```

**Roles each kind accepts.** A binding under a role the kind does not accept is a structural error. Role names are fixed by this table; DC-002 defines their quantities and normalisation.

| kind | measurement roles | forecast/plan roles | constraint keys |
|---|---|---|---|
| `grid` | `power` (net), `import_power`, `export_power`, `import_energy`, `export_energy`, `import_price`, `export_price` | `import_price_forecast`, `export_price_forecast`; output `planned_exchange` | `import_limit`, `export_limit`, `tariff_provider`, `network_fees`, `p2p_settlement` |
| `solar` | `power`, `energy` | `generation_forecast`; output `planned_curtailment` | `generation_limit`, `inverter_limit`, `curtailment_allowed` |
| `load` | `power`, `energy`, `temperature` | `demand_forecast`; output `scheduled_consumption` | `fixed_hours`, `min_runtime`, `energy_requirement`, `operating_window`, `thermal_comfort`, `sheddable` |
| `battery` | `soc`, `power` (signed), `charge_power`, `discharge_power`, `charge_energy`, `discharge_energy` | `availability`, `target_soc`; outputs `planned_dispatch`, `planned_soc` | `soc_min`, `soc_max`, `reserve`, `charge_limit`, `discharge_limit`, `charge_efficiency`, `discharge_efficiency`, `departure_target` |

- **Battery capacity states its basis** (`nominal` or `usable`); a capacity without one is a structural error. This is #1574's "explicit nominal/usable basis", and F02's.
- **Paired versus net:** `grid` may have `power`, or `import_power` + `export_power`, or both. Whether a pair and a net agree is DC-002/DC-003, not structure.

### Binding

```yaml
power:
  source: {kind: entity_state, entity_id: sensor.x, registry_id: 0123abcd}
  normalization:            # declared, applied by DC-002
    target_unit: kW
    positive_direction: import
    native_positive_direction: import
  provenance:
    origin: manual          # manual | energy_dashboard | provider_profile | derived
    confirmed: true
    overridden: false
```

Source variants, from #1574, each with its required fields:

| `source.kind` | required | optional |
|---|---|---|
| `entity_state` | `entity_id` | `registry_id`, `config_entry_id` |
| `entity_attribute` | `entity_id`, `attribute_path`, `adapter` (name + version) | `registry_id` |
| `action_response` | `action`, `target`, `response_path`, `adapter` (name + version) | `parameters` |
| `recorder_statistic` | `statistic_id`, `interval_semantics` | |
| `constant` | `value`, `unit` | |
| `derived` | `calculation` (registered name), `depends_on` (list of `device_id.role`) | |
| `model_output` | `model`, `target` (`device_id.role`) | |

- **`registry_id` is how identity survives a rename** (DC-R02). When present, it is the reference of record; `entity_id` is a cached display value refreshed from the registry.
- **Canonical signs** (#1574) are declared per role in `normalization.positive_direction`. DC-001 checks the declared direction is the canonical one for the role: Grid `import`, Battery `discharge`, Solar `generation`, Load `consumption`. Converting is DC-002's.

### Relationship

```yaml
- kind: supplies                 # supplies | included_in_measurement | shares_equipment
  from: grid_main                # supplies only
  to: load_house
- kind: included_in_measurement
  child: load_pool
  parent: load_house
  quantity: consumption
  confirmed: true
- kind: shares_equipment
  devices: [solar_roof, battery_home]
  equipment: inverter_1          # free-form label
```

The three kinds are distinct (#1574: *"a common HA device... does not prove measurement inclusion"*). Structure only: references resolve, `child != parent`, and `included_in_measurement` contains no cycle.

### Constraint and shared constraint

A device's own constraints sit under `constraints`. A limit shared across devices (a hybrid inverter's AC rating) is a `shared_constraints` entry naming the devices and the constrained quantity, and **must not also be copied into each device's own limit** (#1574). DC-001 checks that each referenced device exists. Whether the copies are consistent is DC-003's job.

### Accounting

```yaml
accounting:
  demand:
    mode: inclusive              # inclusive | residual_plus_children
    root_load_id: load_house
    separate_load_ids: []
```

Structure only: `root_load_id` is a `load`, and every ID in `separate_load_ids` is a `load`. Whether the decomposition is valid is DC-003's.

## Identity

- **Format:** `^[a-z][a-z0-9_]{0,62}$`, unique within the site across devices, boundaries and the site itself. Readable on purpose, because it appears in logs and Repairs.
- **Assigned once, never derived again.** Discovery (DC-005) proposes an ID from the role and name. Once confirmed, it is stored and never recomputed from entity IDs, names or order. A rename changes `name`, never `id`.
- **What carries continuity across a rescan:** a `registry_id` (or `statistic_id`) match on a confirmed binding. Equal candidates with no such match are **not** merged here; DC-005 surfaces them as unresolved (F06).
- **Deletion is explicit.** A device absent from a rescan is not deleted; that is a DC-004 lifecycle event.

## Versioning

- **`schema_version` is an integer, starting at 1.**
- **Additive and optional fields do not bump it.** A reader ignores unknown optional fields and preserves them on write; see the round-trip rule.
- **A rename, removal, changed meaning or newly required field bumps it, and needs a reviewed contract decision** (ROADMAP's governance). Each bump ships with a pure migration function from N−1 to N, tested on F01 and F02.
- **A reader refuses a version newer than it knows,** with a reason. It never guesses.

## Structural validation

A pure function, `validate(document) -> list[Problem]`. It reads no sensors and makes no HA calls. Each `Problem` has a stable code, a path into the document, and a sentence.

| code | rule |
|---|---|
| `schema_version_unsupported` | the version is not one this reader knows |
| `id_invalid`, `id_duplicate` | ID format; uniqueness across site, devices and boundaries |
| `reference_unresolved` | any `device_id` in relationships, accounting, shared constraints, `derived.depends_on` or `model_output.target` does not exist |
| `role_not_allowed_for_kind` | a binding role the device's kind does not accept |
| `sign_not_canonical` | `normalization.positive_direction` is not the role's canonical direction |
| `source_fields_missing` | a source variant lacks a required field |
| `capacity_basis_missing` | battery capacity without `nominal`/`usable` |
| `relationship_self`, `inclusion_cycle` | `child == parent`; a cycle in `included_in_measurement` |
| `dependency_cycle` | a `derived`/`model_output` dependency reaches itself (DC-R16) |
| `accounting_kind_mismatch` | the accounting root or a separate ID is not a `load` |
| `grid_missing` | no `grid` device. #1574: the logical Grid exists even when its measurement does not, so the device is required and its `power` binding is not |

What it deliberately does **not** report: an absent binding (that's a capability gap for readiness); a sensor that is stale, unavailable or in the wrong unit (DC-002); and a sum that doesn't close (DC-003).

## Serialisation

- **Round-trip:** `serialise(parse(doc)) == doc` for every valid document, in canonical form: keys sorted, lists kept in their stored order, unknown optional fields preserved.
- **Representation:** JSON-compatible types only, no YAML tags. YAML in this document is illustration.

## Mapping from #1574

| #1574 | DC-001 | note |
|---|---|---|
| `Device`, `Binding`, `Relationship`, `Constraint` | same | unchanged |
| `Readiness` | not stored | #1574: *"derived evidence"*; DC-006 computes it |
| `site.control_policy: observation_only` | same, plus `authorised` | the only other value; authorising is outside setup (F12) |
| binding `source` variants (7) | same 7 | required fields made explicit |
| `provenance.origin` / `confirmed` | same, plus `overridden` | #1574 asks to record *"whether it was user-confirmed or overridden"* |
| `relationships` kinds (3) | same 3 | `shares_equipment` shape added (#1574 names it, does not show it) |
| `accounting.demand` | same | the modes' semantics are DC-003's |
| canonical signs | same | checked as a declaration here; applied in DC-002 |
| `registry_id` | **added** | #1574: *"Retain registry/config-entry references where available"* |
| `capacity.basis` | **added** | #1574: *"Capacity with explicit nominal/usable basis"* |

## Acceptance

Tests live under `tests/device_contract/`, next to DC-000's. Each test references its requirement ID.

- **DC-R01 / F01:** the minimum site validates with zero problems. The same site with Grid `power` unbound still validates; it carries no zero. Removing the Grid device gives exactly `grid_missing`.
- **DC-R02 / F06:**
  - an entity rename (same `registry_id`, new `entity_id`) keeps every device ID;
  - reordering `devices` changes neither IDs nor validation;
  - the canonical form of a reordered document differs only in list order.
- **DC-R16 / F11:** a self-dependency and a 3-cycle each give `dependency_cycle`; an acyclic chain validates.
- **F02 (structure):** one integration's entities bound to three devices (`grid`, `solar`, `battery`) validates. A shared inverter limit in `shared_constraints` validates, and a battery without a capacity basis gives `capacity_basis_missing`.
- **Each problem code has one negative test**, so the table above cannot be dead text (the DC-000 non-vacuity rule).
- **Round-trip** on every fixture document.
- **No consumer reads this model yet.** A test pins that no module under `custom_components/` imports it until DC-006.

## Rollback

DC-001 adds a schema, validators and tests, and no consumer reads them. Rolling back is reverting the PR; no stored state exists to unwind.

## Decisions for review

| decision | proposed | owner |
|---|---|---|
| ID format | readable `^[a-z][a-z0-9_]{0,62}$`, not ULIDs | Mark + household |
| Unknown fields | preserved on round-trip, ignored on read | Mark |
| `control_policy` values | `observation_only`, `authorised` only | Mark |
| Several grid connections | representable, readiness-limited | Mark |
| Where the schema lives in code | a pure, HA-free module (like `flex_telemetry.py`), so the standalone path can validate too | Mark |
| `phases` | `all` or a list of `L1`–`L3` | Mark |

This draft authorises no implementation. Its next step is Mark's review.
