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
| `fixtures/Fnn_*/fixture.json` | one per fixture with a started oracle: identity, origin, data semantics, expected result, tolerances, execution status, privacy. |
| `test_traceability.py` | the gate: every P0 requirement mapped, every reference resolved, every status explicit, no production pass claimed. |
| `test_reference_oracles.py` | F07, the exact-once BESS + DC EV + AC EV accounting oracle and its variants, including DC-000's partial-interval connection, two vehicles on one charger, and trip and reconnection. |
| `oracles_limits.py` | F02 (and F07's shared charger) reference oracle: a shared limit is one combined quantity (`circuit_power`, `charger_power`, `inverter_ac_power`) constrained once; the rating copied onto each member misses what it should catch. |
| `test_reference_oracles_limits.py` | F02 against its manifest: separate logical devices from one integration, shared limits, strings inside their total counted once, two inverters both counted, and the undeclared-overlap evidence test. |
| `oracles_accounting.py` | the oracles for DC-003's decisions, applied as proposed: the five-value reconciliation verdict (10 % of metered throughput, 2 kWh floor, 10 % stale limit, declared meter sign), per-interval residuals with the negative-residual rule (proposed, unmeasured figures), inclusion boundaries (AC/DC, phases, quantity, sign), `mobility_unresolved` without connection evidence, and undeclared-overlap evidence. |
| `test_reference_oracles_accounting.py` | those against F05 (time series, inclusion boundaries), F07 (reconciliation, mobility) and F08 (#1465's 5 October aggregates reproduce `disagrees`; no corrected EPR). |
| `oracles_signals.py` | F04 and F09 reference oracles: prices on their published `[start, end)` intervals with coverage reported, fees added once, case-sensitive power units, energy refused as power, missing distinct from zero, held counters measured, resets and interval edges counted once. |
| `oracles_structure.py` | F01, F05 and F11 reference oracles: the minimal Grid + Load site, unresolved grid power never an invented zero, nested inclusion (residuals, schedules replacing a baseline once), and acyclic derived-signal dependencies, one shared revision, readiness by capability. |
| `test_reference_oracles_signals.py`, `test_reference_oracles_structure.py` | those oracles against each fixture's manifest. |
| `inventory/8a9f503.json` | DC-000 step 2: the producer/consumer/write-boundary inventory **measured** at the baseline by `scripts/device_contract_baseline.py` (parses the code; never hand-written). |
| `test_inventory.py` | the inventory matches the baseline, is byte-identical across runs, and every equipment-operating service call lives in `solver_dispatch/` (DC-R15). |
| `test_nonvacuity.py` | negative controls: a double count, a W/kW mix-up, broken identities, a shared limit copied per member or stated twice, a netted charger, a double count or equal net-hiding errors in reconciliation, a declared meter sign second-guessed, a clamped residual, a child moved to DC, at-home read as connected, and a shuffled overlap candidate must fail or be rejected. |
| `snapshots/legacy/F09_power_units.json` | DC-000 step 4: what the production power-unit code did at the baseline for 21 unit spellings, written only by `scripts/device_contract_legacy.py <ref>` (reads that commit's source with `git show`; byte-identical on re-run). |
| `contract_divergences.json` | where that legacy behaviour differs from the oracle, each with an owner and a class: `open_decision` or `candidate_defect`. |
| `approved_changes.json` | the behaviour-change manifest: a legacy output may change only when listed here. Starts empty. |
| `test_legacy_characterisation.py` | legacy parity (today's code matches the record or an approved change) and the divergence ledger (every divergence declared, every declared one still real). |
| `test_dc002_bindings_and_normalisation.py` | DC-002: the production resolver `custom_components/nimbus_load/device_bindings.py` run against these oracles on F03, F04, F09 and F11 (units, signs, the five states, intervals and coverage, fees, read-only actions, derived cycles; F03 holds Amber Electric's and OpenADR's action responses in the exact field shapes Mark Purcell pasted on #1774, and LocalVolts v2 rows, all with synthetic values), plus the F04 entries in `contract_divergences.json` and a pin that no other module reads the resolver yet. |
| `test_repeatability.py` | the whole suite in three fresh interpreters and once in reversed order, per-test outcomes compared; a test that passes only on warm module state fails it. Its negative control runs an order-dependent pair and must see them differ. Node ids reach each child through a file, because past ~330 ids they exceed Windows' command-line limit. |

Status changes and expected results change only through a reviewed PR, never
by regenerating a snapshot.
