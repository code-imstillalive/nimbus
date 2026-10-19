# Nimbus Device Contract Spec DC-005: discovery, evidence, provider adapters and validated source selection

Status: Draft for review, revision 0.1. Prepared 19 October 2026, Australia/Brisbane. Drafted, as DC-001 to DC-004 were, for Mark Purcell's review on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574) ("you draft, I sign off").

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/005-discovery-and-provider-selection.md`. Depends on [DC-001](001-canonical-device-model.md) and [DC-002](002-bindings-and-normalisation.md); on [DC-003](003-accounting-and-shared-constraints.md) for inclusion evidence; and on [DC-004](004-migration-and-compatibility.md) for anything that is committed. The setup it feeds is [the two-step setup design](../../design/two-step-setup.md) (#1585), §4.

This specification proposes behaviour. It does not claim that the discovery code below exists. It authorises no consumer switch (DC-006), no new setup screen (DC-007) and no change to any installation.

## Responsibility

DC-005 turns what Home Assistant already knows into a **proposal**: devices, bindings and inclusions for the household to confirm. It writes nothing on its own. A proposal becomes stored state only through DC-004's commit, and only when the household confirms it.

| DC-005 owns | DC-005 does not own |
|---|---|
| Reading evidence: the Energy Dashboard, known integration profiles, the entity and device registries, and history | The document's shape and validation (DC-001) |
| Turning evidence into candidate devices and bindings, each with its provenance and evidence | Whether a binding resolves now, and to what value (DC-002); DC-005 calls it |
| Saying when evidence is ambiguous or insufficient, and asking one targeted question per role | Whether the accounting closes (DC-003); DC-005 supplies `included_in_stat` as inclusion evidence |
| Provider adapters: one declared profile per supported integration, and the support matrix | Storage, revisions and commit (DC-004) |
| Rediscovery against a stored document: stable identity, no duplicates, differences shown | The screens that show a proposal (DC-007) |
| Validating a source switch before it is committed | Consumers reading the document (DC-006) |

## Requirements owned (from DC-000's registry)

| ID | Requirement | Fixtures | What DC-005 must make checkable |
|---|---|---|---|
| **DC-R13** | Discovery proposes; ambiguous evidence is never silently activated | F06, F09 | Every proposed binding is `confirmed: false`; an ambiguous role carries all its candidates and no binding; nothing is committed without a confirmation |
| **DC-R02** (with 001, 004) | Logical identity survives rename, reorder and rediscovery | F06 | Rediscovery matches stored devices by registry identity, never by entity_id or list position; it creates no duplicate device |
| **DC-R05** (with 002) | Tariff is a Grid provider; directions, fees and settlement are distinct | F03, F04 | A provider profile proposes each price role on the Grid device, with its quality (forecast, matched, settled) declared |
| **DC-R15** (with 004, 007) | Setup, migration and discovery send no actuator commands | F12 | Discovery, proposal, rediscovery and source-switch validation make zero service calls, except DC-002's read-only allow-list |
| **DC-R17** (P1) | Expand provider and equipment profiles after the adapter contract is proven | F03, F04 variants | Each profile is declared individually, versioned, and listed in the support matrix |

## Evidence, and what each kind may decide

Evidence is collected per role. Each kind has a declared strength, but **strength never breaks a disagreement**: when two kinds point at different sources for one role, the role is ambiguous and is asked (Mark's review of #1590: *"entity order never decides"*).

| Evidence | Read through | Gives | May decide alone? |
|---|---|---|---|
| **Integration profile** | config entries and the entity registry, by `unique_id` (never entity_id) | role-tagged sources for a recognised integration; one integration may supply several devices | Yes, for the roles the profile declares, when exactly one instance is configured |
| **Energy Dashboard** | `energy/get_prefs` equivalent, read only | grid, solar and battery sources: energy statistics, `stat_rate` power, `stat_soc`, tariff entities, individual devices with `included_in_stat` | Yes, for what it states explicitly (`stat_rate`, `stat_soc`, the energy statistics, the tariff entities) |
| **History pairing** | recorder history, via `device_resolver.match_power_sensor` (#1590) | a power sensor for a role whose Energy Dashboard source names only energy | No: it proposes a candidate with its error; the household confirms |
| **Same HA device** | the device registry | which entities sit together | Never: it only narrows which candidates history pairing tries |

Two kinds agreeing on one source is recorded as corroboration, not proof, when they share a derivation (an Energy Dashboard `stat_rate` that is HA's own template over the profile's sensor).

Results per role, using the resolver's existing names: `candidate` (one source, with its evidence), `ambiguous` (two or more, all listed), `insufficient_evidence` (nothing can be decided from what was read), `no_candidates`.

## The proposal

Discovery returns a **proposal**, not a document:

- `document`: a DC-001 document holding only what the evidence decides. Every binding has `provenance.origin` set to `energy_dashboard` or `provider_profile` (DC-001's existing values), `confirmed: false` and `overridden: false`. A role that is ambiguous or insufficient has **no binding**.
- `questions`: one per ambiguous role, listing each candidate with its evidence (kind, error where history decided it, sign where it matters).
- `findings`: what was read and not used, and why (a second LocalVolts account, a statistic with no current-state entity, a unit the role refuses).
- `structural`: DC-001's validation of `document`, so an incomplete proposal is shown as incomplete, never padded with defaults.

The minimum proposal is a Grid and an aggregate Load (DC-R01). Solar and Battery appear only when evidence names them. Absence is never a zero.

A statistic with no current-state entity stays a `recorder_statistic` energy binding. It is never replaced by a power sensor discovery invented.

### Inclusion evidence (with DC-003)

An Energy Dashboard device's `included_in_stat` becomes a proposed inclusion relationship for the energy statistics it names. It is not proof of the boundary of any power or forecast binding on the same device. Where the reference is to a statistic no proposed device owns, the finding is reported and no relationship is proposed.

## Provider adapters and the support matrix

A provider profile is a declared, versioned mapping from one integration's `unique_id` keys to DC-001 roles and DC-002 adapters:

```yaml
profile: localvolts_v2
version: 1
integration: localvolts_v2
instances: exactly_one        # several = ambiguous, nothing proposed
roles:
  grid.import_price:          {key: buy_flex_up}
  grid.export_price:          {key: sell_flex_up}
  grid.import_price_forecast: {key: current_buy_rate,  attribute: forecast,
                               adapter: {name: localvolts_v2, version: 1, field: flexUp, direction: Buy}}
  grid.export_price_forecast: {key: current_sell_rate, attribute: forecast,
                               adapter: {name: localvolts_v2, version: 1, field: flexUp, direction: Sell}}
```

The first matrix covers the six providers DC-002's adapters read, plus the Energy Dashboard. Three can be bound today; three are recognised and reported, because DC-001 has no role to hold what they publish yet or the provider does not say enough:

| Profile | Proposes | Bound through |
|---|---|---|
| LocalVolts v2 | Grid import and export price (`buy_flex_up`, `sell_flex_up`); their forecasts (`current_buy_rate`, `current_sell_rate`, attribute `forecast`) | entity state; adapter `localvolts_v2` with `field: flexUp` and `direction` |
| Amber Express | Grid import and export price (`general_price`, `feed_in_price`) and their `forecast` | entity state; adapter `amber_express` |
| Amber Electric (core) | Grid import and export price forecast (`forecasts-general`, `forecasts-feed_in`, attribute `forecasts`) | adapter `amberelectric` |
| Energy Dashboard | Grid, Solar, Battery, devices and inclusion | `device_resolver.parse_energy_sources`, `device_consumption` |
| AEMO NEM | **reported only**: DC-001 has no regional-spot role yet | |
| PD7DAY | **reported only**: DC-001 has no continuation role yet (DC-002 decision 5 keeps it a separate binding, but names no role) | |
| OpenADR 3 VEN | **reported only**: it states neither currency nor whether a price is a full tariff (`pricing_autodetect`'s existing rule) | |

LocalVolts' P2P matched rate and settlement history are also reported, not bound. DC-002 carries the matched rate on `export_price` with `quality: matched`, and that role already holds the current sell price, so one of them would have to go (DC-002's open question 2 on #1574). The LocalVolts keys were checked against purcell-lab/localvolts_v2 at b4cfbaee: the per-direction current-rate sensors carry the raw rows DC-002's adapter reads.

Each profile wraps the existing detection, which already resolves by `unique_id` and proposes empty fields only. DC-005 changes where its output goes (a proposal), not what it detects. An integration not in the matrix is not guessed at: its roles stay manual.

## Rediscovery against a stored document

When a document is stored (`shadow` or `active`), discovery runs against it:

- **Identity.** A discovered source matches a stored binding by `registry_id` (the entity registry entry's id, which survives a rename), then by `config_entry_id` plus profile key, then by statistic id for a `recorder_statistic`. Never by entity_id or by its position in a list.
- **No duplicates.** A device whose sources all match a stored device is that device. Discovery never creates a second Grid.
- **Confirmed bindings are kept.** A binding with `confirmed: true` is never replaced by discovery. A different discovered source for it is a finding: *found an alternative*, with its evidence.
- **Replaced equipment** (a new registry entry for a role whose old one has gone) is a question, not an automatic swap.
- **Empty roles** can be proposed into, as in the first discovery.

Rediscovery's result is a list of differences against the stored revision. It is shown; it is not committed.

## Confirming a proposal, and switching a source

Confirmation is the household's action. What it does depends on the mode (DC-004):

| Mode | Confirming a proposal |
|---|---|
| `legacy` | Refused (`choose_shadow_first`). Nothing is stored: keeping a confirmation outside the store would make a second authority. Choose `shadow` first; the dry run then shows what discovery would add. |
| `shadow` | A compare-and-swap commit of a new revision. Today's settings stay master; the dry run shows what `active` would change. |
| `active` | A commit in DC-004's order (store, views, marker). The changed views reload the entry. |

Confirmed bindings carry `confirmed: true`. A binding the household changed from the proposal carries `overridden: true`.

**A source switch is validated before it is committed.** The new binding is resolved by DC-002 first. The switch is refused, and the stored revision left exactly as it was, when the resolution is any of: `adapter_unknown`, a refused or missing unit, a wrong dimension, a currency mismatch, or a source that does not exist. A source that resolves `stale` is allowed and flagged, because stale is a state of the data, not of the binding (DC-002 decision 1).

## Where it lives

- `custom_components/nimbus_load/device_discovery.py`: pure. Evidence in, proposal out. No Home Assistant import, like `device_import.py`.
- `device_contract_ha.py`: `async_discover(hass, entry)` gathers the evidence (registries, Energy Dashboard preferences, states, history for the roles that need pairing) and calls the pure module.
- A read-only service, `nimbus_load.device_contract_discover`, returns the proposal (or, with a stored document, the differences). It changes nothing, as `device_contract_preview` does not.
- Confirmation goes through a `nimbus_load.device_contract_confirm` service in this spec and through DC-007's screens later. Both call DC-004's commit.

## Acceptance

Tests live under `tests/device_contract/`. Each references its requirement ID.

- **DC-R13 / F06, F09:** two power sensors whose history matches a battery's energy equally well give a question with both, and no binding. A sensor in kWh offered for a power role is a finding, not a candidate. A held zero is not taken as evidence of either sign.
- **DC-R02 / F06:** renaming a bound entity and rediscovering gives no difference. Reordering the Energy Dashboard's sources gives no difference. Running discovery twice gives the same proposal. A replaced battery sensor gives a question.
- **DC-R05 / F03, F04:** each provider profile proposes its price roles on the Grid device with the declared quality; two LocalVolts accounts propose nothing for LocalVolts and say why.
- **DC-R15 / F12:** discovery, rediscovery and a refused and an accepted source switch, run on real Home Assistant, make zero service calls (the DC-002 read-only allow-list excepted, and not exercised by these fixtures).
- **Validated switch:** a switch to a source with no unit, or to an unknown adapter, is refused and leaves the stored revision byte-identical. A switch to a stale source is committed and flagged.
- **#1528, carried here 18 Oct:** a fresh install whose Energy Dashboard has grid, solar, battery and individual devices configured gets a proposal with the Grid, Battery, Solar and the loads, and their inclusion, with **no hand-typed entity id**. It reads the preferences only; it never writes them.

### DC-000 additions this needs

1. **F06 fixtures:** rename, reorder, repeated discovery, equal candidates and replaced equipment. F06 is `not_started` in the registry today.
2. **An Energy Dashboard fixture** for the #1528 case: synthetic preferences with grid, solar, battery, `stat_rate`, `stat_soc` and three devices, two with `included_in_stat`.
3. **A provider-profile fixture per matrix row**, synthetic, resolving by `unique_id` keys.

## What already exists and is reused, not rewritten

- `device_resolver.py` (#1590): `parse_energy_sources`, `device_consumption`, `match_power_sensor`, `check_energy_balance` and `entities_on_device`. Its constants stay labelled as provisional, as they are today.
- `pricing_autodetect.py` (#1550, #1686): every `detect_*` function, which already resolves by `unique_id` and proposes empty fields only.
- `device_bindings.py` (DC-002): resolution, units, staleness and the read-only allow-list.
- `device_import.py`, `device_storage.py` and `device_contract_ha.py` (DC-004): the document, the store and the commit.

## Out of scope

- Any new screen. DC-007 owns the setup that shows a proposal.
- Writing the Energy Dashboard. Discovery reads `energy/get_prefs` and never calls `energy/save_prefs`.
- Profiles for equipment (inverters, meters, chargers) beyond what the Energy Dashboard states. DC-R17 adds them, one reviewed PR each.
- Calibration. A mismatch between two sources is reported, never corrected (roadmap: *"agreement or disagreement between sensors does not authorise invented correction factors"*).

## Rollback

Discovery writes nothing, so reverting its code reverts it. A confirmed proposal is a DC-004 revision, and DC-004's rollback applies: the previous revision, or `legacy`.

## Decisions for review

Each is proposed as written above. The build follows these unless @purcell-lab changes one.

1. **Disagreement is a question.** Evidence strength is recorded, never used to break a disagreement between two kinds.
2. **Identity on rediscovery** is `registry_id`, then `config_entry_id` plus profile key, then statistic id. Never entity_id or list position.
3. **The first support matrix** binds LocalVolts v2, Amber Express and Amber Electric, plus the Energy Dashboard. AEMO NEM, PD7DAY and OpenADR are reported until DC-001 has roles for them (or, for OpenADR, the provider states its currency and tariff basis). Everything else stays manual until its own profile PR.
4. **History pairing** runs only for a role whose Energy Dashboard source names energy but no power, over 7 days, with the resolver's existing constants labelled provisional.
5. **Confirmed bindings are never replaced** by rediscovery; an alternative is shown as a finding.
6. **A stale source may be switched to**, flagged; every other resolution failure refuses the switch.
7. **Confirmation in `legacy`** is refused (`choose_shadow_first`), so there is never a pending confirmation outside the store.
8. **Placement:** `device_discovery.py` pure, `async_discover` in `device_contract_ha.py`, and two services, `device_contract_discover` (read-only) and `device_contract_confirm`.

**Build order**, each its own PR, each changing nothing on an install until a household confirms:

1. The pure proposal from the Energy Dashboard, with the #1528 fixture.
2. Provider profiles over `pricing_autodetect`, with the matrix.
3. History pairing for roles with energy but no power.
4. Rediscovery against a stored document, with F06.
5. The HA side: `async_discover`, the two services, and the validated source switch.
