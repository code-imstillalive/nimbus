# Spec 003: `solver_inputs/controllable_loads.py` — controllable-load construction

Status: proposed
Plan: docs/architecture/tech-debt-plan.md; phase: #1302 (Phase 3)
Code cited at: `f2bc599`

## Responsibility

Phase 3 (#1302) moves `build_controllable_loads` — the second-largest function in
`solver_writer.py` (736 lines; the plan's own top-six table cites it at line 14054 as
measured at `d5b044b`, current `main` at `f2bc599` has it at `:13181` — cited fresh
below rather than trusted from the plan) — out of
the god module. It was explicitly sequenced after "native gap closed (done, #1335)":
before #1335 merged, this function returned on its first line outside native mode
(`_NATIVE_HASS is None`), so a move here had only 1.1%/2.4% real coverage behind it (per
the 2026-09-27 worklog). #1335's native-mode golden-master driver
(`tests/golden/scenarios_native.py`, `fake_native.py`) now exercises the real body —
see Current behaviour below — which is why this spec is unblocked today and wasn't
before.

`build_controllable_loads` reads every `controllable_load` subentry (sheddable,
deferrable, thermal), resolves its power sensor, tuning, adequacy windows and current
run state, and returns the `ControllableLoadConfig` list `build_plan()` consumes. It
owns 7 exclusively-private helpers plus 2 shared blockers — measured below.

## Prior art

Not applicable, same as spec 000/001/002 and the plan's own header — this is Nimbus's
own module organisation, not a solver/load/scoring mechanism EMHASS or HAEO would have
an equivalent for. (`ControllableLoadConfig`'s own three kinds — sheddable, deferrable,
thermal — were separately checked against EMHASS's deferrable-load model and HAEO's
element directory when each kind was originally designed, per #603/#774's own issue
threads; this spec moves the existing, already-justified construction code, it doesn't
introduce a new mechanism.)

## Measured cost

`python3 tests/analyse_module_dependencies.py --phase 3` (real, current `main`):

```
### build_controllable_loads  (736 lines)
    importable:              4
    module-level (blockers): 9
      _NATIVE_HASS, _build_daily_adequacy_windows,
      _earliest_period_for_same_day_window, _evaluate_done_condition,
      _resolve_controllable_load_tuning, _resolve_hour_to_period_index,
      _sample_load_run_state, resolve_controllable_load_power_sensor, safe_num

lines in scope:      736
distinct blockers:   9
```

Each blocker is "1x" — none is shared with any other Phase-3 target because
`build_controllable_loads` is the only target this phase has (unlike Phase 2's
multi-target sharing, there is no cheaper shared-core sub-step to peel off first). Per
the analyser's own per-blocker sizes:

```
_sample_load_run_state                 100 lines
_build_daily_adequacy_windows            80 lines
_earliest_period_for_same_day_window     75 lines
_resolve_controllable_load_tuning        70 lines
safe_num                                 47 lines
_evaluate_done_condition                 71 lines
_resolve_hour_to_period_index            30 lines
resolve_controllable_load_power_sensor   29 lines
_NATIVE_HASS                              -   (module-level state, not a function)
```

`python3 tests/analyse_module_dependencies.py --functions "build_controllable_loads,_NATIVE_HASS,_build_daily_adequacy_windows,_earliest_period_for_same_day_window,_evaluate_done_condition,_resolve_controllable_load_tuning,_resolve_hour_to_period_index,_sample_load_run_state,resolve_controllable_load_power_sensor,safe_num" --callers`
(real, current `main`):

```
production callers outside the module : 5
monkeypatch sites                     : 2
test files touching any target        : 18

A compatibility facade IS load-bearing for: _NATIVE_HASS, safe_num.
Each has a production caller, a monkeypatch site, or both.
```

Per-name:

```
name                                     prod callers                                monkeypatch
build_controllable_loads                 0                                            0  (3 test files)
_NATIVE_HASS                             3  [battery_participants.py, extra_batteries.py,
                                              solver_shared.py]                        2  (test_controllable_load_power_sensor_discovery.py,
                                                                                            test_participant_power_per_row_units.py)
_build_daily_adequacy_windows            0                                            0  (1 test file)
_earliest_period_for_same_day_window     0                                            0  (1 test file)
_evaluate_done_condition                 0                                            0  (1 test file)
_resolve_controllable_load_tuning        0                                            0  (2 test files)
_resolve_hour_to_period_index            0                                            0  (1 test file)
_sample_load_run_state                   0                                            0  (2 test files)
resolve_controllable_load_power_sensor   0                                            0  (1 test file)
safe_num                                 2  [extra_batteries.py, prices.py]            0  (2 test files)
```

`_NATIVE_HASS`'s 3rd production caller (`solver_shared.py`) is new since spec 001 —
it's the deferred `_solver_writer()` seam 2a itself added to read that state, exactly
the precedent this spec reuses (see Migration step 2).

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Sheddable + deferrable load construction, LP scheduling, guard debounce/dispatch (native mode) | `build_controllable_loads` (`:13181`), `apply_commanded_state_guard` | `tests/golden/scenarios_native.py`'s `native_controllable_loads` and `native_controllable_loads_two_cycles` scenarios — both loads configured so the LP's answer is forced (sheddable `min_fraction=1.0`; deferrable's window can't meet `target_kwh` at `max_power_kw`, so every in-window period runs at the cap), not merely optimal, per that file's own module docstring on why a snapshot needs a unique answer |
| Debounce / no-re-dispatch-when-unchanged path | same two functions, cycle 2 | `native_controllable_loads_two_cycles` specifically: cycle 2 reads back the `LoadRunState` cycle 1 persisted, exercising `min_hold_minutes`/`DEFAULT_MIN_HYSTERESIS_PERIODS` and the already-commanded no-re-dispatch branch |
| Power-sensor auto-discovery via entity registry (not by name — #768) | `resolve_controllable_load_power_sensor` | same golden scenarios' `_registry_entries()` fixture, which deliberately includes a same-name-different-device decoy sensor the discovery must not pick; `tests/test_controllable_load_power_sensor_discovery.py` (also the one file monkeypatching `_NATIVE_HASS`) |
| Adequacy window resolution, same-day-in-progress handling (#582) | `_build_daily_adequacy_windows`, `_earliest_period_for_same_day_window`, `_resolve_hour_to_period_index` | direct unit tests (1 file each, per callers table above) — not separately exercised by the golden scenarios, which use a fixed window that doesn't cross the same-day boundary |
| Done-condition evaluation (climate/water_heater entity read, #534) | `_evaluate_done_condition` | direct unit test |
| Per-load tuning resolution (adequacy earliness budget, smoothness weight, etc.) | `_resolve_controllable_load_tuning` | direct unit tests (2 files) |
| Run-state sampling (`currently_on`, `delivered_today_kwh`, unit-scale correction — #535) | `_sample_load_run_state` | direct unit tests (2 files) |
| Generic HA-entity-read-as-float with WARN+fallback (#603-cited Mark Purcell fix) | `safe_num` | direct unit tests (2 files); already shared with `solver_inputs/extra_batteries.py` and `solver_inputs/prices.py` |

`kind=thermal` is explicitly **not** covered by either golden scenario — the native
scenarios file's own module docstring states why: a thermal load's target temperature
is a hard LP constraint with an infeasibility-relaxation retry behind it, so the only
configuration that forces a unique snapshot answer sits within a rounding error of
infeasible, "exactly the knife edge a snapshot must not be balanced on. Filed
separately." This spec moves the `kind=thermal` branch of `build_controllable_loads`
exactly as it exists today (verbatim), without adding golden coverage for it — doing so
is out of scope here (see Non-goals) and would need its own scenario design.

## Interfaces

Plain functions, bodies copied verbatim — no redesign.

```python
# solver_inputs/controllable_loads.py — layer: solver_inputs/solver_reports (layer 2)

def resolve_controllable_load_power_sensor(...) -> str | None: ...
def _resolve_hour_to_period_index(...) -> int: ...
def _earliest_period_for_same_day_window(...) -> int: ...
def _build_daily_adequacy_windows(...) -> list[...]: ...
def _evaluate_done_condition(...) -> bool: ...
def _resolve_controllable_load_tuning(...) -> ...: ...
def _sample_load_run_state(...) -> ...: ...
def build_controllable_loads(...) -> list[ControllableLoadConfig]: ...
```

`_NATIVE_HASS` is **not** moved — see Façade decision. `safe_num` moves to
`solver_shared.py` (an amendment to this spec's own module target, not a new module),
not into `solver_inputs/controllable_loads.py` — it has two existing callers unrelated
to controllable-load construction (`extra_batteries.py`, `prices.py`), so putting it
in a controllable-loads-specific module would recreate the exact "dependency pointing
the wrong way" problem the plan's own layer map exists to prevent: those two callers
would gain a new, semantically-unrelated dependency on `solver_inputs/controllable_
loads.py` just to reach a generic utility. `solver_shared.py` already holds exactly
this class of function (`_cfg_num`, `_kw_scale_factor`, etc., per spec 001) — `safe_num`
belongs beside them.

```python
# solver_shared.py — addition to spec 001's existing module
def safe_num(entity_id: str, fallback: float = 0.0) -> float: ...
```

## Façade decision

`_NATIVE_HASS` and `safe_num` are the two names the tool's own conclusion line names as
facade-load-bearing; the other seven have zero production callers and zero monkeypatch
sites.

- **`_NATIVE_HASS` stays in `solver_writer.py`.** This is not a facade decision so much
  as a non-move: per spec 001 Migration step 1, this module-level state variable's real
  home is Phase 7's problem (`solver/ha_bridge.py`), and every downward-layered module
  that needs to read it already does so through the same deferred `_solver_writer()`
  late-import seam (`solver_inputs/battery_participants.py:83`,
  `extra_batteries.py:80`, and now `solver_shared.py`, added by spec 001). This spec
  adds a fourth: `solver_inputs/controllable_loads.py` reads `_NATIVE_HASS` via
  `_solver_writer()._NATIVE_HASS` at call time, exactly the established pattern — not a
  new design decision, a precedent being followed for the fourth time.
- **`safe_num` moves to `solver_shared.py`, with a facade re-export left in
  `solver_writer.py`** (`from .solver_shared import safe_num`) for its two existing
  external production callers (`extra_batteries.py`, `prices.py` — both already import
  from `solver_writer` today and would otherwise need retargeting for zero behavioural
  gain) and its 2 test files' `solver_writer.safe_num(...)` call sites. Once
  `solver_inputs/controllable_loads.py` exists, it imports `safe_num` from
  `solver_shared` directly — a downward import, not through the facade.
- **The other seven names get no facade.** Zero production callers, zero monkeypatch
  sites, and (per the callers table) the "test files" that reference them are exactly
  the direct unit tests named in Current behaviour, one or two per name — small enough
  that updating each to import from `solver_inputs.controllable_loads` directly is the
  cheap, Phase-1-style choice, not the facade-preserving one spec 002 made for its
  44-call-site case. Concretely: `tests/test_controllable_load_power_sensor_discovery.py`
  (also the `_NATIVE_HASS` monkeypatch site — that patch target is unaffected, since
  `_NATIVE_HASS` itself doesn't move), plus the six other single/double-file tests
  named in the callers table, switch their `solver_writer.<name>` call sites to
  `from solver_inputs.controllable_loads import <name>` / `controllable_loads.<name>`.

## Invariants

- `solver_writer.safe_num is solver_shared.safe_num` — a real test.
- `solver_inputs/controllable_loads.py` never imports `solver_writer` at module load
  time; its one read of `_NATIVE_HASS` goes through `_solver_writer()`, called inside
  the function body, matching the three existing precedents exactly.
- The 7 non-facade names resolve to the *same objects* under their new import path —
  the tests that move to import them directly are testing identical code, not a
  behavioural change; a diff on any of those 7 function bodies is out of scope.
- `import-linter`'s `nimbus-layers` contract gains one new `ignore_imports` entry for
  `solver_inputs/controllable_loads.py -> solver_writer` (the `_solver_writer()` seam),
  the eighth such exception (following the seven the plan's own section 1 table lists,
  six of which spec 001 already retired) — not zero, and this spec doesn't reduce the
  count further.

## Migration

1. Create `solver_inputs/controllable_loads.py` with the 7 non-facade functions
   (`resolve_controllable_load_power_sensor`, `_resolve_hour_to_period_index`,
   `_earliest_period_for_same_day_window`, `_build_daily_adequacy_windows`,
   `_evaluate_done_condition`, `_resolve_controllable_load_tuning`,
   `_sample_load_run_state`) and `build_controllable_loads` itself, bodies copied
   verbatim from `solver_writer.py:13181` and its helpers' current line ranges. Add the
   `_solver_writer()` deferred-import seam for `_NATIVE_HASS`, mirroring
   `solver_inputs/extra_batteries.py:80`'s own call-site shape exactly.
2. Move `safe_num` into `solver_shared.py` (extending spec 001's module, not creating a
   new one), bodies copied verbatim from `solver_writer.py`'s current line range. In
   `solver_writer.py`, delete the body, add `from .solver_shared import safe_num` to
   the existing spec-001 facade import block.
3. In `solver_writer.py`, delete the 8 moved bodies (7 helpers +
   `build_controllable_loads`), replace with
   `from .solver_inputs.controllable_loads import (resolve_controllable_load_power_sensor,
   _resolve_hour_to_period_index, _earliest_period_for_same_day_window,
   _build_daily_adequacy_windows, _evaluate_done_condition,
   _resolve_controllable_load_tuning, _sample_load_run_state,
   build_controllable_loads)` — a facade re-export for all 8, even the 7 that don't
   strictly need one by the caller-count criterion, because `main()` itself
   (`solver_writer.py:15784`, calling `build_controllable_loads(...)` at `:16436`) calls
   it as a bare name and that internal call site is cheaper to leave resolving through
   an import than to
   rewrite; this mirrors spec 002's same reasoning for its own two internal callers.
4. Update the 8 test files named in the callers table to import from
   `solver_inputs.controllable_loads` directly (per the Façade decision's "no facade
   for the 7" call) — `test_controllable_load_power_sensor_discovery.py`'s
   `_NATIVE_HASS` monkeypatch target stays `solver_writer._NATIVE_HASS` unchanged
   (that name didn't move).
5. Add `_NATIVE_HASS`'s new fourth late-import call site
   (`solver_inputs/controllable_loads.py`) to the existing
   "`_solver_writer()` late-import call sites" count wherever it's tracked (the plan's
   own section 1 table, spec 000's tooling) — 4 sites instead of 3, one new module.
6. Run the golden master — `native_controllable_loads` and
   `native_controllable_loads_two_cycles` must be snapshot-identical; this is the real
   behavioural gate for this specific spec, given #1335 built it for exactly this
   purpose. Run the full suite.
7. Update the `nimbus-layers` contract: `solver_inputs/controllable_loads.py` joins the
   existing layer-2 entry; add the one new `ignore_imports` exception for its
   `_solver_writer()` seam (see Invariants).
8. Run the Part C gates: `size_ratchet.py` (`build_controllable_loads` itself, at 736
   lines, was and remains a single over-60-line function — moving it doesn't split it;
   this spec's job is relocation, not decomposition of the function's own internals,
   which is a separate future concern not raised by #1302), `coverage_compare.py`
   (`--moved-code` mapping `solver_inputs/controllable_loads.py` and the `safe_num`
   line in `solver_shared.py`), `noop_patches.py` (confirms the 2 monkeypatch sites on
   `_NATIVE_HASS` still patch a name the code under test actually reads — that name
   didn't move, so this should be a clean pass by construction, checked not assumed),
   `assertions_unchanged.py`.

## Non-goals

- Splitting `build_controllable_loads` itself into smaller functions — it stays 736
  lines and over the 60-line ratchet threshold after this move; #1302 is Phase 3's
  extraction target, not a decomposition target. A future spec could split it, not
  this one.
- Building golden-master coverage for `kind=thermal` — the native scenarios file's own
  docstring already states why this is deliberately deferred and filed separately
  (infeasibility-relaxation knife-edge, no configuration forces a unique snapshot
  answer without risking flakiness). This spec moves the thermal branch's code
  unchanged; adding its own coverage is a different, harder problem for whoever files
  that follow-up.
- Retargeting `extra_batteries.py`'s or `prices.py`'s own imports of `safe_num` away
  from the `solver_writer` facade — they keep resolving through it, unchanged, per the
  Facade decision.
- Phase 4 (#1303, plan assembly) and anything downstream of `build_controllable_loads`'s
  own output shape — this spec's contract is "same `ControllableLoadConfig` list out,"
  nothing about what consumes it next.

## Deploy timing

Not devhub-gated by the plan's own gate table (scoped to Phases 5-6). Pure internal
move, same provable-by-construction shape as specs 001/002 — `solver_writer.<name>`
resolves to the identical object before and after (a real test asserts this for all 8
facade names plus `safe_num`), golden master snapshot-identical for both native
scenarios, no new or changed entity/service/config surface. "Devhub validation: not
claimed — a pure internal refactor with nothing for a live install to exercise
differently than before."

## Acceptance

- [ ] Golden master identical for `native_controllable_loads` and
      `native_controllable_loads_two_cycles` (and all other scenarios, unaffected).
- [ ] Full suite passes; the 8 test files named in Migration step 4 import from the new
      location; no other file's assertions edited.
- [ ] `noop_patches.py`: the 2 `_NATIVE_HASS` monkeypatch sites still patch a name the
      code under test reads (unaffected by this move, checked not assumed).
- [ ] No line executed before is unexecuted after (`coverage_compare.py`).
- [ ] mypy count not higher; zero in `solver_inputs/controllable_loads.py`; unchanged in
      `solver_shared.py` beyond `safe_num`'s own addition.
- [ ] `nimbus-layers`: `solver_inputs/controllable_loads.py` added to layer 2; exactly
      one new `ignore_imports` exception (`_NATIVE_HASS` via `_solver_writer()`), zero
      other new violations.
- [ ] `size_ratchet.py`: no new function over 60 lines introduced; `build_controllable_
      loads` itself remains the one already-known over-60-line function, unchanged in
      length (verbatim move).
- [ ] Exactly one new `_solver_writer()` late-import call site (the `_NATIVE_HASS` read
      in `solver_inputs/controllable_loads.py`), matching the established precedent —
      no other new late import anywhere.
- [ ] `solver_writer.<name> is solver_inputs.controllable_loads.<name>` for all 8
      facade names, and `solver_writer.safe_num is solver_shared.safe_num` — real
      tests, not claims.

## Rollback

Revert the single commit. `solver_inputs/controllable_loads.py` is new and unreferenced
outside `solver_writer.py`'s own re-export block; `safe_num`'s move within
`solver_shared.py` is likewise a pure relocation. No state file format changes, no
migration to undo.
