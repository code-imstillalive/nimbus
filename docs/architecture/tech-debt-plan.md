# Technical debt plan: decomposing `solver_writer.py`

Status: proposed. Carries [#1298](https://github.com/code-imstillalive/nimbus/issues/1298)'s
phases unchanged and adds the evidence each phase is accepted on. Line numbers are
at `d5b044b`.

Prior art: not applicable. This is Nimbus's own module organisation and test
harness, not a solver or scoring mechanism EMHASS or HAEO would have an
equivalent for (the conclusion #735 and #1298 both reached).

## 1. Where the debt is

Measured by AST at `d5b044b`:

| Measure | Value |
|---|---|
| `solver_writer.py` lines | 17,517 |
| Top-level functions (all functions) | 151 (166) |
| Functions over 60 lines | 67, together 12,638 lines |
| Wall-clock reads package-wide | 46 |
| Test files importing `solver_writer` | 124 |
| Production modules importing `solver_writer` | 9 |
| `patch` / `monkeypatch.setattr` sites | 512, across 80 test files |

The six largest functions:

| Function | Lines | Line |
|---|---:|---:|
| `apply_commanded_state_guard` | 1,300 | 15236 |
| `_compute_report_for_window` | 1,280 | 7659 |
| `publish_plan` | 1,051 | 12305 |
| `_update_all` | 872 | 15627 |
| `main` | 842 | 16657 |
| `build_controllable_loads` | 736 | 14054 |

Phase 1 ([#1308](https://github.com/code-imstillalive/nimbus/pull/1308)) has already
moved the battery-fleet functions. The modules extracted so far reach back into
the god module through a late import, `_solver_writer()`, defined seven times:

| Module | `_solver_writer()` at | Late-import call sites |
|---|---|---:|
| `solver_inputs/battery_participants.py` | 83 | 7 |
| `solver_inputs/solar.py` | 28 | 5 |
| `solver_inputs/battery_soc.py` | 68 | 2 |
| `solver_inputs/extra_batteries.py` | 80 | 2 |
| `solver_inputs/load.py` | 48 | 2 |
| `solver_inputs/prices.py` | 40 | 2 |
| `solver_publish.py` | 37 | 2 |

Each is a dependency pointing the wrong way: an extracted module that still
needs its parent at call time is moved text, not a new boundary.

## 2. Target architecture

The module shape is #1298's. This plan adds the rules that make it a boundary.

### SOLID, mapped to what the code does

| Principle | What it means here |
|---|---|
| Single responsibility | One module per reason to change: HA transport, input adapters, plan assembly, publish, dispatch guard, reports. `main()` orchestrates and owns nothing else. |
| Open/closed | A new input source is a new adapter behind the existing input protocol, not a new branch in `build_price_arrays` or `main()`. |
| Liskov | Native (`_NATIVE_HASS`) and REST bridges satisfy one `HaBridge` protocol with the same observable results; today several paths are silently skipped when `_NATIVE_HASS is None` (13528, 13680, 13871, 13964). |
| Interface segregation | Adapters receive the narrow reader they use (`get_state`, `get_history`), not the whole module. |
| Dependency inversion | Extracted modules depend on a protocol passed in, never on `solver_writer`. This replaces every `_solver_writer()` late import. |

### Layers

Imports may point down this list, never up:

1. `solver/` (pure LP and maths, HA-import-free today)
2. `solver_inputs/`, `solver_reports/` (depend on `solver/` and on protocols)
3. `solver_plan.py`, `solver_publish.py`, `solver_dispatch/`
4. `solver/ha_bridge.py` (implements the protocols)
5. `solver_writer.py` (façade and orchestration), `standalone_writer.py`

An import-linter contract (spec 000 Part C) records today's violations, the
seven late imports above among them, and fails on any new one.

### The façade criterion

From the #1298 discussion and #1316: a name keeps a re-export in
`solver_writer` if, and only if, something outside the module resolves it at
call time (a production caller or a `monkeypatch.setattr(solver_writer, ...)`
site). `tests/analyse_module_dependencies.py --callers` produces that table
for each phase before it starts, and the phase's spec quotes it.

## 3. Method: specification-driven extraction

#1298 names the method. The change here is the order: the evidence exists
before the first line moves, and it is one harness for the whole cycle rather
than seven per-phase tests.

1. **Specify.** `docs/specs/NNN-name.md` from `docs/specs/TEMPLATE.md`,
   written from the code, citing file and line for every behaviour it pins,
   with the `--callers` table and the dependency count from #1315.
2. **Review the spec.** The maintainer approves or amends it. This is the
   design decision; everything after it is checkable.
3. **Implement** on a branch from current `main`, never from another spec's
   branch. No test assertion changes. Patch and import paths may be retargeted
   only where the spec lists them.
4. **Verify** every acceptance item with evidence, then run the gates.
5. **Ship** after the maintainer confirms. Behaviour defects found on the way
   are filed and fixed in their own PRs, never inside a refactor.

### Gates every refactor PR must pass

| Gate | Tool | Pass condition |
|---|---|---|
| Behaviour | golden master (spec 000 Part A) | every scenario's record identical to its snapshot: every state posted, service called, entity read, warning logged and state file written |
| Tests | full suite | unchanged pass count except tests the spec adds; no assertion edited; retargeted patch paths listed in the spec |
| Silent patches | no-op monkeypatch detection (Part C) | every `patch`/`monkeypatch.setattr` target is read by the code under test during that test |
| Coverage | per-function coverage (Part C) | no line of `solver_writer.py` executed before is unexecuted after, mapped through the spec's list of moved code |
| Types | mypy ratchet | error count never rises; zero in modules the spec creates |
| Layers | import-linter | no new violation; moved code sits in its target layer |
| Size | AST ratchet | no new function over 60 lines; the count over 60 never rises |
| Live | devhub pass | #1298's rule for Phases 5 and 6, unchanged |

The golden master gate replaces #1298 method step 1 ("a golden-output test per
extraction target"). One harness for `main()` pins every phase at once, and it
cannot be skipped for a phase in the way #1300's pre-move test was (#1310).

## 4. Sequence

| Step | What | Depends on |
|---|---|---|
| Spec 000 | golden master, recorded market inputs, gate tooling | nothing |
| #1310 | Phase 1 baseline, from `0d0d553`, against the extracted code | spec 000 Part A |
| Phase 2 (#1301) | reports; `_compute_report_for_window` last (#1315) | spec 000 |
| Phase 3 (#1302) | `build_controllable_loads` | spec 000, native gap closed (spec 000 non-goals) |
| Phase 4 (#1303) | plan assembly | Phase 3 |
| Phase 5 (#1304) | `publish_plan` | Phase 4, devhub pass |
| Phase 6 (#1305) | `apply_commanded_state_guard` | Phase 5, native gap closed, devhub pass |
| Phase 7 (#1306) | HA bridge, standalone entrypoint; last `_solver_writer()` removed | Phase 6 |

## 5. Done means

- `solver_writer.py` is a façade of re-exports that pass the façade criterion,
  plus `main()` as orchestration under 60 lines of its own.
- No `_solver_writer()` late import remains.
- Every gate in section 3 runs in CI.
- The golden master snapshots recorded before Phase 2 still match, or each
  difference is a deliberate change with its own PR.

## 6. What this plan deliberately does not do

- Change behaviour. Defects found while building the harness are listed in
  spec 000 and filed separately.
- Decompose `sensor.py`, `solver/network.py`, `lp.py`, `elements.py` or
  `const.py` (#1298's out-of-scope list, unchanged).
- Migrate the 512 patch sites. Each phase retargets only what its spec lists.
