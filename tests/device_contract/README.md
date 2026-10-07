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
| `test_nonvacuity.py` | negative controls: a double count, a W/kW mix-up and broken identities must fail or be rejected. |

Status changes and expected results change only through a reviewed PR, never
by regenerating a snapshot.
