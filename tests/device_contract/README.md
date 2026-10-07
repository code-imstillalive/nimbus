# Device contract evidence (Spec DC-000)

The evidence framework for the device contract roadmap
([`docs/specs/device-contract/`](../../docs/specs/device-contract/), PR #1600).
It proves nothing about production behaviour yet; it makes sure that, when
DC-001 onward land, there is an independent target to check them against.

| file | what it is |
|---|---|
| `acceptance_registry.json` | every requirement DC-R01–DC-R18 with its owning spec(s) and fixtures; every fixture F01–F12 with an explicit status. Nothing is marked passed because code is absent. |
| `known_defects.json` | defects observed in current behaviour, kept as evidence with why they are wrong. No corrected value or root cause is asserted until established. |
| `oracles.py` | reference oracles written from first principles. **Never imports Nimbus production code**, so it cannot agree with a bug by construction. |
| `fixtures/Fnn_*/manifest.json` | one per fixture with a started oracle: identity, origin, data semantics, expected result, tolerances, execution status, privacy. |
| `test_traceability.py` | the gate: every P0 requirement mapped, every reference resolved, every status explicit, no production pass claimed. |
| `test_reference_oracles.py` | F07, the exact-once BESS + DC EV + AC EV accounting oracle and its variants. |
| `oracles_signals.py` | F04 and F09 reference oracles: prices on their published `[start, end)` intervals with coverage reported, fees added once, case-sensitive power units, energy refused as power, missing distinct from zero, held counters measured, resets and interval edges counted once. |
| `oracles_structure.py` | F01, F05 and F11 reference oracles: the minimal Grid + Load site, unresolved grid power never an invented zero, nested inclusion (residuals, schedules replacing a baseline once), and acyclic derived-signal dependencies, one shared revision, readiness by capability. |
| `test_reference_oracles_signals.py`, `test_reference_oracles_structure.py` | those oracles against each fixture's manifest. |
| `inventory/8a9f503.json` | DC-000 step 2: the producer/consumer/write-boundary inventory **measured** at the baseline by `scripts/device_contract_baseline.py` (parses the code; never hand-written). |
| `test_inventory.py` | the inventory matches the baseline, is byte-identical across runs, and every equipment-operating service call lives in `solver_dispatch/` (DC-R15). |
| `test_nonvacuity.py` | negative controls: a double count, a W/kW mix-up and broken identities must fail or be rejected. |
| `snapshots/legacy/F09_power_units.json` | DC-000 step 4: what the production power-unit code did at the baseline for 21 unit spellings, written only by `scripts/device_contract_legacy.py <ref>` (reads that commit's source with `git show`; byte-identical on re-run). |
| `contract_divergences.json` | where that legacy behaviour differs from the oracle, each with an owner and a class: `open_decision` or `candidate_defect`. |
| `approved_changes.json` | the behaviour-change manifest: a legacy output may change only when listed here. Starts empty. |
| `test_legacy_characterisation.py` | legacy parity (today's code matches the record or an approved change) and the divergence ledger (every divergence declared, every declared one still real). |

Status changes and expected results change only through a reviewed PR, never
by regenerating a snapshot.
