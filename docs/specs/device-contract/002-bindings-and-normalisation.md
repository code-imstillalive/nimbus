# Nimbus Device Contract Spec DC-002: resolving bindings into canonical quantities and intervals

Status: Draft for review, revision 0.1. Prepared 12 October 2026, Australia/Brisbane. Drafted, as DC-001 was, for Mark Purcell's review on [#1574](https://github.com/code-imstillalive/nimbus/issues/1574) ("you draft, I sign off").

Parent: [Device Contract roadmap](ROADMAP.md). Path: `docs/specs/device-contract/002-bindings-and-normalisation.md`. Depends on [DC-000](000-baseline-fixtures-and-gates.md) and [DC-001](001-canonical-device-model.md).

This specification proposes behaviour. It does not claim any resolver below exists. It authorises no consumer switch, storage or migration; those are DC-004 and DC-006.

## Responsibility

DC-002 turns one DC-001 **binding** into one **resolved value**: a quantity in a canonical unit and sign, or a series of half-open intervals, with what is known about its freshness, coverage and provenance. It reads current state, attributes, read-only action responses and statistics. It does **not** decide what to do with the value.

| DC-002 owns | DC-002 does not own |
|---|---|
| Unit conversion, dimension checks and canonical sign, once, at the binding | The document shape, IDs and structural validation (DC-001) |
| Distinguishing missing, unavailable, stale, held and a measured zero | Whether sums close, inclusion and decomposition (DC-003) |
| Each source variant's read path, including read-only action responses | Which candidate becomes a binding (DC-005) |
| Price and forecast intervals: boundaries, overlap, coverage, gaps | Whether a function is ready (DC-006); DC-002 reports the evidence readiness uses |
| Provenance carried on every resolved value | Storage, revisions and migration (DC-004) |

## Requirements owned (from DC-000's registry)

| ID | Requirement | Fixtures | What DC-002 must make checkable |
|---|---|---|---|
| **DC-R03** | Typed sources with unit, native/canonical sign, boundary and provenance | F02, F03, F04, F09 | Every resolved value states its canonical unit and sign, the boundary it belongs to, and where it came from |
| **DC-R04** | Missing, zero, unavailable and stale remain distinct | F04, F09 | Five states, never collapsed; a missing value is never 0 |
| **DC-R05** | Tariff is a Grid provider; directions, fees and settlement are distinct (with DC-005) | F03, F04 | Import and export are separate series; fee inclusion is declared, never inferred; forecast, matched and settled are separate roles |
| **DC-R06** | Time-aware intervals preserve coverage and quantity semantics | F04 | `[start, end)`, timezone-aware; coverage measured from the intervals; no gap filled, no expired price extended |
| **DC-R16** (shared with 001, 006) | Derived and model dependencies acyclic | F11 | Resolution of a `derived` binding refuses a cycle at run time too (DC-001 refuses it in the document) |

## The resolved value

```yaml
quantity:                  # for a measurement
  role: power              # the DC-001 role it resolves
  device_id: grid_main
  value: 1.52              # canonical unit and sign, or null
  unit: kW
  state: valid             # valid | missing | unavailable | stale | held
  observed_at: "2026-10-12T05:00:12+10:00"   # the source's own time
  provenance: {origin: manual, confirmed: true, source_kind: entity_state,
               entity_id: sensor.x, native_unit: W, native_sign: import,
               adapter: null}
series:                    # for a price or forecast
  role: import_price_forecast
  unit: AUD/kWh
  intervals:
    - {start: "2026-10-12T05:00:00+10:00", end: "2026-10-12T05:30:00+10:00",
       value: 0.1041, quality: forecast}
  coverage: {from: ..., to: ..., gaps: [[..., ...]], covered_fraction: 0.96}
  issued_at: null          # never invented when the source does not say
  fetched_at: "2026-10-12T04:59:58+10:00"
  provenance: {..., adapter: {name: localvolts_v2, version: 1}}
```

### The five states (DC-R04)

| state | meaning | value |
|---|---|---|
| `valid` | a reading or interval the source asserts, current by the source's own rules | the number, in canonical form |
| `missing` | nothing to read: no entity, no attribute, an empty array, no statistic | `null` |
| `unavailable` | the source exists and says `unavailable` or `unknown` | `null` |
| `stale` | a last value exists but is older than the source's freshness rule allows | the last value, flagged; consumers decide |
| `held` | a change-only source whose last change is old but which, by its own semantics, still holds | the number |

**Freshness is per source kind, not one age** (two-step setup §4.2; the #1556 lesson that an idle change-only sensor is not stale). The rule each adapter declares:
- **entity_state**, sampled sensor: stale past its declared maximum interval;
- **entity_state**, change-only sensor (a breaker reading 0 W all night): `held`, never stale on age alone;
- **forecast series:** stale when its own `fetched_at` or issue time passes the adapter's refresh period, whatever the age of the HA state.

A `0` is a measured zero. It is `valid` with value 0, and never `missing`.

## Units and dimension (DC-R03)

- **Power** converts to kW through `power_units.py` (#1570), the one converter in the codebase. It is case-sensitive: `mW` is not `MW`. It is the same table the DC-000 oracle states independently.
- **A declared power unit** (`normalization.native_unit`, DC-006's observation slice; household decision, 19 Oct 2026, #1893). A power sensor that states no unit, or a spelling other than Home Assistant's exact one (`kw`, ` kW `), is refused as above unless its binding declares `W` or `kW` (or another exact power unit), which the household sets in a Repair. The declared unit applies only in those two cases, and is recorded in provenance as `unit_declared`. A sensor that states a valid unit **different** from the declared one is refused (`declared_unit_conflicts`), never resolved either way; a declared unit that is not itself exact is refused (`declared_unit_invalid`); and declaring a power unit never makes `A`, `kVA` or an energy unit into power.
- **Energy is refused as power** and power as energy (#1562). A unit of the wrong dimension, an unknown unit, or `A`/`kVA` for power (#1643) resolves to `missing`, with the reason, never to a guessed kW.
- **Prices** convert c/kWh and $/MWh to the site currency per kWh once, at the adapter (#1615's rule). A currency other than the site's is **refused, never converted** (no FX).
- **Non-finite values** (`nan`, `inf`) are `missing`.

## Signs (DC-R03)

The binding's `normalization` declares `native_positive_direction`; DC-001 has already checked that `positive_direction` is canonical. DC-002 multiplies by −1 **exactly once** when they differ, at resolution, and records the native sign in provenance. A paired `import_power` / `export_power` resolves to two non-negative values; DC-002 does not net them (that is DC-003's, at a boundary).

Sign ambiguity, where a sensor's direction is not declared, is not resolved here. The binding is incomplete, and DC-005 offers the evidence (`solver_inputs/sign_convention.py`, #1241) for the user to confirm.

## Source variants

| `source.kind` | read path | notes |
|---|---|---|
| `entity_state` | current state, plus `last_reported`/`last_changed` for freshness | On a write with the same state and attributes, HA core's `StateMachine.async_set` only sets `old_state.last_reported` and fires `state_reported`; the cached `State._as_dict` (an `under_cached_property`) is not cleared, so REST, websocket `get_states` and other serialised views keep the old `last_reported` (read in HA core 2025.1.4's `core.py`; measured on HA 2026.7.4, #1705). Freshness for an unchanged value therefore uses the live state object, not the serialised one |
| `entity_attribute` | attribute path, through a **versioned adapter** | the adapter owns the provider's row shape: LocalVolts v2, AEMO NEM, NEM PD7DAY, Amber Express (`detailedForecast`, case-sensitive), Amber Electric (`forecasts`, plural), all already in `price_intervals.py`. Forecast adapters (DC-006 decision 10a, `device_forecasts.py`), from today's solar readers: `nimbus_forecast` (`forecast: [{time, value, lower, upper}]`), `solcast` (`detailedForecast`; `pv_estimate` is the period's average kW, p10 and p90 its band) and `open_meteo_solar` (`watts`, W to kW). A source spanning several entities names the rest in `continued_by`; without its anchor it does not resolve |
| `action_response` | a **known read-only** action, through a versioned adapter | only actions on an allow-list of read-only response actions (`openadr3_ven.get_forecast`, `amberelectric.get_forecasts`); any other action is refused, never called (DC-R15) |
| `recorder_statistic` | long-term statistics, per its `interval_semantics` | an energy statistic stays energy; a statistic with no current-state entity is still a valid energy binding |
| `constant` | the literal value and unit | through the same unit and dimension checks |
| `derived` | a registered calculation over its `depends_on` | each dependency is resolved first; a run-time cycle is refused (DC-R16); any dependency `missing` makes the result `missing`, never a partial sum |
| `model_output` | the Nimbus model's published output | its own coverage and training state are carried, not hidden |

### A combined forecast (`combine: mean`)

A Solar `generation_forecast` binding with an ordered `sources` list resolves each member on the plan's grid, then combines the members that resolved (DC-006 decision 10b; today's `build_solar_arrays`):

- each member reports its own state and reason (`adapter_unknown`, `unavailable`, `shape_not_recognized`, `malformed`); a failed member is dropped, **never zero-filled**, because a solar estimate of 0 kW in daylight would drag the mean down rather than degrade it;
- one member is its own value; several are the equal-weight mean, with the band the members' lowest lower and highest upper, each widened by half the members' spread;
- no member resolved is `missing` (`no_member_resolved`), never a zero forecast.

This is a separate mechanism from DC-003's totals over load devices, so the two failure rules cannot drift into each other (Mark Purcell, #1574).

## Intervals and coverage (DC-R06)

- **Half-open `[start, end)`, timezone-aware, compared in UTC**, displayed in the site timezone.
- **Boundaries come from the source:** explicit start and end, else start plus duration, else end minus duration, per the adapter. A point-only source uses its adapter's documented policy; the final point is **never assumed to last forever**. This is `price_intervals.py`'s existing behaviour for the six adapters, which DC-002 adopts as the reference.
- **Coverage is measured from the intervals,** not from the first and last timestamps. Gaps are listed, and `covered_fraction` is over the window asked for. This is OpenADR's 24-hour hole in a seven-day span (#1550's capture).
- **Overlapping intervals from one source are refused**, as the DC-000 oracle refuses them, rather than averaged.
- **Mixed resolutions are reconciled by overlap with duration weighting** when a consumer needs one value over a window; a spike in a 5-minute interval is never smoothed by sampling.
- **No gap is filled** and no expired price is extended. Continuation from a second source (pd7day after LocalVolts' 24 h) is a **separate binding with its own provenance**, chosen per direction, never silently stitched. The 30-minute seam in #1550's capture is the acceptance case.
- **Negative prices are kept.** A missing direction stays missing: a feed-in-only provider gives no import price, and no import price is invented from it.

## Tariff roles (DC-R05, with DC-005)

Import price, export price, the price forecasts, the P2P matched rate and P2P settlement are **distinct roles on the Grid**, resolved separately:
- **Fees:** a series declares whether network and retail fees are included (from its adapter: LocalVolts Flex Up includes them, a PD7DAY tariff includes its network rate, AEMO NEM is wholesale). The consumer adds fees only to a series that does not include them; DC-002 never adds them itself.
- **Forecast, matched and settled are separate values.** A settlement row is never read as a forecast, and an empty matched feed with an `unknown` state is `unavailable`, not a zero price (#1550's capture).

## Acceptance

Tests live under `tests/device_contract/`, beside DC-000's oracles. Each names its requirement, and each runs the **production resolver against the oracle**, never against itself.

- **DC-R03 / F09:**
  - the 21 unit spellings in DC-000's legacy characterisation resolve as the oracle says;
  - `mW` and `MW` differ;
  - Wh as power, and A or kVA as power, are refused with a reason;
  - a declared native sign is flipped exactly once.
- **DC-R04 / F09:** the five states each have a case:
  - a recorded "0" is `valid` 0;
  - `unavailable` and `unknown` are `unavailable`;
  - a missing entity is `missing`;
  - a breaker idle at 0 W overnight is `held`, not stale;
  - a forecast past its refresh period is `stale`;
  - `nan` is `missing`.
- **DC-R06 / F04:**
  - 5- and 30-minute series, and a multi-day series, resolve to the oracle's intervals;
  - coverage over a window with a hole reports the hole;
  - overlap is refused;
  - an expired price is not extended;
  - a negative price survives;
  - a feed-in-only provider yields no import series.
- **DC-R05 / F03, F04:**
  - fees are added once (included and not-included variants);
  - matched, forecast and settled are separate;
  - an `action_response` not on the read-only allow-list is refused, and the capture test sees zero service calls;
  - a statistic without an entity resolves as energy.
- **DC-R16 / F11:** a run-time `derived` cycle is refused; a missing dependency makes the result missing.
- **No consumer reads the resolver yet** (until DC-006), pinned as in DC-001.

## What already exists and is reused, not rewritten

| piece | where | role here |
|---|---|---|
| power-unit table | `power_units.py` (#1570) | the unit converter |
| energy-as-power refusal; A/kVA refusal | `power_input_check.py` (#1562, #1643) | the dimension check |
| six provider adapters, interval coverage | `price_intervals.py` (#1606–#1609, #1613) | the `entity_attribute` / `action_response` adapters |
| sign-convention evidence | `solver_inputs/sign_convention.py` (#1241) | DC-005's evidence, not used to resolve |
| c/kWh and $/MWh scaling | #1615 | the price unit step |

DC-002 wraps these behind one resolver interface. Their own behaviour is unchanged unless a test against the oracle shows a divergence, in which case the divergence is recorded in `contract_divergences.json` and decided, not silently changed.

## Rollback

DC-002 adds a resolver and tests, and no consumer reads them. Rolling back is reverting the PR; no stored state exists.

## Decisions for review

| decision | proposal | owner |
|---|---|---|
| A stale value's value | carried and flagged `stale`, not nulled, so a consumer can choose (show it greyed, or refuse to plan on it) | Mark |
| Freshness rules | declared per adapter (sampled, change-only, forecast), not a global age | Mark |
| Read-only action allow-list | starts with `openadr3_ven.get_forecast` and `amberelectric.get_forecasts`; additions by reviewed PR | Mark |
| Currency mismatch | refused, no FX | Mark (agreed in #1550's text) |
| Continuation sources | a separate binding per direction, never stitched inside one series | Mark |
| Where the resolver lives | beside `device_contract.py`, pure apart from the HA read adapters, which are injected | Mark |

This draft authorises no implementation. Its next step is Mark's review.
