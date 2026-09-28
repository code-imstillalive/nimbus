# Spec 003: `solver_inputs/controllable_loads.py` — controllable-load construction

Status: approved (#1370, merged) — implementation not yet started
Plan: docs/architecture/tech-debt-plan.md; phase: #1302 (Phase 3)
Code cited at: `91415a8`

**Revised from the version reviewed on PR #1370** — every number below was re-measured
against `91415a8`, not carried over from the `f2bc599` draft. Phase 2 landed in full
between drafts (PR #1356, Phase 2b/2c, all eight functions moved in one PR rather than
the two-step split this plan's own sequence names — see that PR's own review comment on
#1369 for why), `solver_writer.py` dropped from 17,517 to 14,031 lines, and a second
real production caller (`solver_inputs/controllable_load_history.py`, #1363) attached
itself to one of this spec's own "no facade" names while this was in review. Every
citation below is fresh, not inherited.

**Amended post-merge (2026-09-28), before Phase 3 implementation starts.** #1370's own
final review approved with one explicit condition — *"Merge it once `solver_shared.
_LOGGER` is in Invariants and the two absent traps are recorded as checked. I am not
going to start `build_controllable_loads` until you have."* — and #1370 merged 23
minutes later without that condition met: `solver_shared._LOGGER` appears zero times
in the merged text, and the default-argument trap the same review checked and cleared
was never recorded, only the `global` one was. Since #1370 is merged, this isn't a
comment on it (a closed PR isn't read); it's an amendment landing before the one
concrete piece of unfinished work — Phase 3 hasn't started, by the same review's own
stated commitment. See Invariants and Migration step 1 for the actual content; both
gaps are closed below, not just flagged.

## Responsibility

Phase 3 (#1302) moves `build_controllable_loads` — 736 lines, currently at `:10526` —
out of the god module. It was explicitly sequenced after "native gap closed (done, #1335)":
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
(real, current `main` at `91415a8`):

```
production callers outside the module : 7
monkeypatch sites                     : 3
test files touching any target        : 20

A compatibility facade IS load-bearing for: _NATIVE_HASS,
resolve_controllable_load_power_sensor, safe_num.
Each has a production caller, a monkeypatch site, or both.
```

Per-name:

```
name                                     prod callers                                       monkeypatch
build_controllable_loads                 0                                                   0  (3 test files)
_NATIVE_HASS                             4  [battery_participants.py,
                                              controllable_load_history.py,
                                              extra_batteries.py, solver_shared.py]           2  (test_controllable_load_power_sensor_discovery.py,
                                                                                                   test_participant_power_per_row_units.py)
_build_daily_adequacy_windows            0                                                   0  (1 test file)
_earliest_period_for_same_day_window     0                                                   0  (1 test file)
_evaluate_done_condition                 0                                                   0  (1 test file)
_resolve_controllable_load_tuning        0                                                   0  (2 test files)
_resolve_hour_to_period_index            0                                                   0  (1 test file)
_sample_load_run_state                   0                                                   0  (2 test files)
resolve_controllable_load_power_sensor   1  [controllable_load_history.py]                   1  (test_768_controllable_load_delivery_reconstruction.py)
safe_num                                 2  [extra_batteries.py, prices.py]                   0  (2 test files)
```

**Two names changed shape since the `f2bc599` draft this replaces, both real, not
measurement noise:**

- `_NATIVE_HASS`'s production-caller count rose from 3 to 4. #1363 (nimbus issue #768,
  merged the same day this spec was first opened) added
  `solver_inputs/controllable_load_history.py`, which reads `sw._NATIVE_HASS` via the
  same deferred `_solver_writer()` seam every other caller uses — not a new pattern,
  a fourth user of the existing one, following spec 001's own addition of a third.
  `test_callers_mode_counts_only_real_references.py`'s own inventory assertion
  (`test_the_real_tree_still_finds_the_native_hass_callers`) already counts exactly
  this: "four real `sw._NATIVE_HASS` references (`battery_participants.py`,
  `extra_batteries.py`, `controllable_load_history.py`, and `solver_shared.py`
  itself)." This spec's own move adds the fifth, not the fourth — see Façade decision.
- `resolve_controllable_load_power_sensor` gained a real external caller it didn't
  have when this spec was first drafted: `controllable_load_history.py:254` reads
  `sw.resolve_controllable_load_power_sensor(data)` through the same seam, and
  `tests/test_768_controllable_load_delivery_reconstruction.py` monkeypatches it. This
  moves it from the "no facade needed" group into the facade group — the tool's own
  conclusion line now names three load-bearing names, not two.

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Sheddable + deferrable load construction, LP scheduling, guard debounce/dispatch (native mode) | `build_controllable_loads` (`:10526`), `apply_commanded_state_guard` | `tests/golden/scenarios_native.py`'s `native_controllable_loads` and `native_controllable_loads_two_cycles` scenarios — both loads configured so the LP's answer is forced (sheddable `min_fraction=1.0`; deferrable's window can't meet `target_kwh` at `max_power_kw`, so every in-window period runs at the cap), not merely optimal, per that file's own module docstring on why a snapshot needs a unique answer |
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

**Checked, not assumed: none of the 9 moved names binds module-level state via
`global`.** Phase 2b's own review (PR #1370 comment, `e3a3c2c`) surfaced a real gotcha
from that move — `_carry_forward_quality_history`'s `global _LAST_KNOWN_QUALITY_HISTORY`
binds in the namespace of whichever module the function is *defined* in, so relocating
the function silently relocated the variable too, and a facade alias cannot fix it
(an alias captures the object at import time; a `global`-rebound name is reassigned,
not read, so the alias goes stale). Checked directly against the current source with
`ast.walk()` over each of the 9 target `FunctionDef` nodes, collecting every
`ast.Global` inside: zero hits, for `build_controllable_loads` and all 8 helpers. None
of this spec's targets rebind module-level state — they read `_NATIVE_HASS` (a
different mechanism, the established `_solver_writer()` seam, unaffected by this class
of bug) and otherwise take/return plain values. This is stated as a checked fact, not
inferred from "no other spec found one," because the applicable answer is not the same
for every extraction target and PR #1370's own comment asked for it to be checked here.

**Checked, not assumed: no default argument on any of the 9 targets evaluates a
`solver_writer` module-level name.** The companion gotcha to the `global` one above,
from the same review thread — spec 002's own `prior_read: str = PRIOR_READ_OK` trap
(a default is evaluated once at function-definition time, before `sw = _solver_writer()`
runs, so `sw.PRIOR_READ_OK` there is a plain `NameError`, and a module-level constant
used this way has to move with the function rather than being reached through the
seam). AST-inspected each of the 9 targets' own `args.defaults`/`kw_defaults`: three
carry a default (`safe_num`'s `fallback=0.0`, `_sample_load_run_state`'s
`import_price_now=None`, `build_controllable_loads`'s own `import_price_arr=None`),
and all three are plain `ast.Constant` literals (`0.0`, `None`) — none is a `Name`
node resolving to anything defined in `solver_writer.py` at module scope, so none is
subject to this trap. Recorded as checked rather than left to be inferred, for the
same reason as the `global` check above.

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

`_NATIVE_HASS`, `resolve_controllable_load_power_sensor`, and `safe_num` are the three
names the tool's own conclusion line names as facade-load-bearing (three, not the two
the `f2bc599` draft found — `resolve_controllable_load_power_sensor` gained its
production caller from #1363 while this spec was in review). The other six have zero
production callers and zero monkeypatch sites.

- **`_NATIVE_HASS` stays in `solver_writer.py`.** This is not a facade decision so much
  as a non-move: per spec 001 Migration step 1, this module-level state variable's real
  home is Phase 7's problem (`solver/ha_bridge.py`), and every downward-layered module
  that needs to read it already does so through the same deferred `_solver_writer()`
  late-import seam. Current real users: `solver_inputs/battery_participants.py`,
  `solver_inputs/extra_batteries.py`, `solver_inputs/controllable_load_history.py`
  (#1363), and `solver_shared.py` (spec 001) — four today, confirmed by
  `test_callers_mode_counts_only_real_references.py`'s own inventory assertion. This
  spec adds a **fifth**: `solver_inputs/controllable_loads.py` reads `_NATIVE_HASS` via
  `_solver_writer()._NATIVE_HASS` at call time, the established pattern for a fifth
  time, not a new design decision.
- **`resolve_controllable_load_power_sensor` keeps a facade re-export.**
  `controllable_load_history.py:254` (#1363, nimbus issue #768) reads
  `sw.resolve_controllable_load_power_sensor(data)` through the deferred
  `_solver_writer()` seam, and `tests/test_768_controllable_load_delivery_
  reconstruction.py` monkeypatches it on `solver_writer`. Both need the name to keep
  resolving as a `solver_writer` module attribute after the move — retargeting
  `controllable_load_history.py` to import from the new module directly is available
  (it already reads everything else it needs through the same seam, so it would be a
  one-line change), but the point of a facade here is exactly the same as `_NATIVE_HASS`
  and `safe_num`'s: `controllable_load_history.py` is Phase 3's *sibling* extraction
  target from the same #768 thread, not this spec's own concern, so this spec leaves it
  reading through the facade rather than reaching into another issue's module to
  retarget it.
- **`safe_num` moves to `solver_shared.py`, with a facade re-export left in
  `solver_writer.py`** (`from .solver_shared import safe_num`) for its two existing
  external production callers (`extra_batteries.py`, `prices.py` — both already import
  from `solver_writer` today and would otherwise need retargeting for zero behavioural
  gain) and its 2 test files' `solver_writer.safe_num(...)` call sites. Once
  `solver_inputs/controllable_loads.py` exists, it imports `safe_num` from
  `solver_shared` directly — a downward import, not through the facade.
- **The other six names get no facade**: `_build_daily_adequacy_windows`,
  `_earliest_period_for_same_day_window`, `_evaluate_done_condition`,
  `_resolve_controllable_load_tuning`, `_resolve_hour_to_period_index`,
  `_sample_load_run_state`. Zero production callers, zero monkeypatch sites, and (per
  the callers table) the "test files" that reference them are exactly the direct unit
  tests named in Current behaviour, one or two per name — small enough that updating
  each to import from `solver_inputs.controllable_loads` directly is the cheap,
  Phase-1-style choice, not the facade-preserving one spec 002 made for its
  44-call-site case. `tests/test_controllable_load_power_sensor_discovery.py` (the
  `_NATIVE_HASS` monkeypatch site — unaffected, since `_NATIVE_HASS` itself doesn't
  move) plus the other single/double-file tests named in the callers table switch their
  `solver_writer.<name>` call sites to `from solver_inputs.controllable_loads import
  <name>` / `controllable_loads.<name>`. `build_controllable_loads` itself also gets a
  facade re-export despite having zero external callers — see Migration step 3 for why
  (the internal `main()` call site).

## Invariants

- `solver_writer.safe_num is solver_shared.safe_num`,
  `solver_writer.resolve_controllable_load_power_sensor is
  solver_inputs.controllable_loads.resolve_controllable_load_power_sensor`, and
  `solver_writer.build_controllable_loads is
  solver_inputs.controllable_loads.build_controllable_loads` — real tests, not claims.
- `solver_inputs/controllable_loads.py` never imports `solver_writer` at module load
  time; its one read of `_NATIVE_HASS` goes through `_solver_writer()`, called inside
  the function body, matching the four existing precedents exactly (making it the
  fifth).
- **Every `_LOGGER` call in the moved code reaches `solver_shared._LOGGER` directly,
  never `sw._LOGGER`.** Flagged in #1370's own final review: qualifying the moved
  code's own `_LOGGER` references (`build_controllable_loads` 11,
  `_sample_load_run_state` 2, `_evaluate_done_condition` 2,
  `_resolve_controllable_load_tuning` 1, `safe_num` 1 — 17 total, per that review's own
  count) as `sw._LOGGER` fails
  `test_callers_mode_counts_only_real_references.py::test_the_real_tree_now_finds_
  zero_logger_callers`, which asserts zero real `sw._LOGGER`-shaped references outside
  `solver_writer.py` (Phase 2a's own success condition). `solver_writer._LOGGER` is an
  identity alias of `solver_shared._LOGGER` (spec 001), so the five test files that
  `patch.object(solver_writer, "_LOGGER", ...)` are unaffected by using the
  `solver_shared` form directly — verified empirically on #1370's own review, not
  merely argued. **Regeneration is not idempotent with this fix**: if `main()`'s own
  layout shifts under Phase 3 (or a later phase) and any of these 9 functions gets
  regenerated from the then-current `solver_writer.py` source rather than hand-edited,
  the regenerated body will carry bare `_LOGGER` again, and a `_LOGGER` → `sw._LOGGER`
  qualifier pass (the naive fix, which looks patch-preserving but is not — see above)
  will turn it back into the wrong form. Check for `sw._LOGGER` in
  `solver_inputs/controllable_loads.py`/`solver_shared.py` specifically, not just for
  the presence of a `_LOGGER` reference, after any regeneration.
- The 6 non-facade names (`_build_daily_adequacy_windows`,
  `_earliest_period_for_same_day_window`, `_evaluate_done_condition`,
  `_resolve_controllable_load_tuning`, `_resolve_hour_to_period_index`,
  `_sample_load_run_state`) resolve to the *same objects* under their new import path —
  the tests that move to import them directly are testing identical code, not a
  behavioural change; a diff on any of those 6 function bodies is out of scope.
- `test_callers_mode_counts_only_real_references.py`'s
  `test_the_real_tree_still_finds_the_native_hass_callers` assertion moves from
  `len(hits) == 4` to `len(hits) == 5`, and its own explanatory message (which names
  each of the four current callers by file) gains `controllable_loads.py` as the fifth
  — this is the "explicit inventory, deliberately" the test's own docstring describes,
  and this spec is exactly the kind of legitimate new reach the test expects to update
  for, not silently pass around.
- `import-linter`'s `nimbus-layers` contract (`pyproject.toml:596-620`, current `main`)
  carries 13 `ignore_imports` entries today — 6 original `solver_inputs/*.py` (spec-1
  precedent), `controllable_load_history` (#1363), 4 `solver_reports/*.py` (Phase 2b/2c),
  `solver_publish`, `solver_shared` — re-counted fresh rather than trusted from the
  plan's own section 1 table, which predates all of these. This spec adds a 14th:
  `solver_inputs.controllable_loads -> solver_writer`, for its own `_solver_writer()`
  seam. Acceptance is "14 total, the new one is this spec's own seam, zero others new,"
  checked against `pyproject.toml` at merge time since this number moves under
  concurrent phases the way it already has twice.

## Migration

1. Create `solver_inputs/controllable_loads.py` with all 8 functions — the 6 that need
   no facade (`_resolve_hour_to_period_index`, `_earliest_period_for_same_day_window`,
   `_build_daily_adequacy_windows`, `_evaluate_done_condition`,
   `_resolve_controllable_load_tuning`, `_sample_load_run_state`) plus the 2 that do
   (`resolve_controllable_load_power_sensor`, `build_controllable_loads` itself) — all
   physically move here regardless of facade status; a facade re-export left in
   `solver_writer.py` is a separate question from where the body lives. Bodies copied
   verbatim from their current line ranges (`solver_writer.py:10526` for
   `build_controllable_loads`, `:10495` for `resolve_controllable_load_power_sensor`,
   `:9856`-`:10391` for the other 6, at `91415a8`). Add the `_solver_writer()`
   deferred-import seam for `_NATIVE_HASS`, mirroring
   `solver_inputs/controllable_load_history.py:95`'s own call-site shape exactly (the
   most recently added of the four precedents, since it's also a #768-thread module).
   **Qualify every moved `_LOGGER` call as `solver_shared._LOGGER`, not `sw._LOGGER`**
   (see Invariants) — if any of these 9 bodies is regenerated from a shifted
   `solver_writer.py` rather than hand-edited during this step, re-check for `sw.
   _LOGGER` specifically afterward, since regeneration reintroduces the bare form this
   qualifier already fixed once.
2. Move `safe_num` into `solver_shared.py` (extending spec 001's module, not creating a
   new one), bodies copied verbatim from `solver_writer.py`'s current line range. In
   `solver_writer.py`, delete the body, add `from .solver_shared import safe_num` to
   the existing spec-001 facade import block.
3. In `solver_writer.py`, delete the 8 moved bodies (6 non-facade helpers,
   `resolve_controllable_load_power_sensor`, `build_controllable_loads`), replace with
   `from .solver_inputs.controllable_loads import (resolve_controllable_load_power_sensor,
   _resolve_hour_to_period_index, _earliest_period_for_same_day_window,
   _build_daily_adequacy_windows, _evaluate_done_condition,
   _resolve_controllable_load_tuning, _sample_load_run_state,
   build_controllable_loads)` — a facade re-export for all 8, even the 6 that don't
   strictly need one by the caller-count criterion, because `main()` itself
   (`solver_writer.py:13146`, calling `build_controllable_loads(...)` at `:13804`) calls
   `build_controllable_loads` as a bare name, and leaving that one internal call site
   resolving through an import is cheaper than rewriting it; this mirrors spec 002's
   same reasoning for its own two internal callers. Re-exporting all 8 together (rather
   than only the 3 that strictly need it) also keeps the import block a single
   statement instead of two.
4. Update the 6 no-facade-needed test files named in the callers table to import from
   `solver_inputs.controllable_loads` directly — `test_controllable_load_power_sensor_
   discovery.py`'s `_NATIVE_HASS` monkeypatch target stays `solver_writer._NATIVE_HASS`
   unchanged (that name didn't move), and `test_768_controllable_load_delivery_
   reconstruction.py`'s `resolve_controllable_load_power_sensor` monkeypatch target
   stays `solver_writer.resolve_controllable_load_power_sensor` unchanged (facade name,
   not moved either).
5. Bump `test_callers_mode_counts_only_real_references.py`'s
   `test_the_real_tree_still_finds_the_native_hass_callers` assertion from 4 to 5, and
   its explanatory message to name `controllable_loads.py` as the fifth caller (see
   Invariants) — this is the same test the peer review on this spec's own PR pointed at
   after #1363 bumped it 3→4; this spec is the next legitimate bump, not a regression.
6. Run the golden master — `native_controllable_loads` and
   `native_controllable_loads_two_cycles` must be snapshot-identical; this is the real
   behavioural gate for this specific spec, given #1335 built it for exactly this
   purpose. Run the full suite. If any test fails on a "which file is this code in"
   assertion (text search, `ast.parse` + name lookup, a bare-`ast.Name` matcher tripped
   by the new `sw.`/`controllable_loads.` prefix, a substring slice, or a hand-rolled
   file union that doesn't know about the new module) rather than a real behavioural
   difference, prefer `tests/_writer_source.py` (`writer_source()`, `writer_trees()`,
   `find_function()`, `function_source()`, `find_constant()` — built during Phase 2b
   specifically to absorb this class of breakage across the union of `solver_writer.py`
   plus every extracted module) over a one-off fix in each test file.
7. Update the `nimbus-layers` contract: `solver_inputs/controllable_loads.py` joins the
   existing layer-2 entry; add the one new `ignore_imports` exception for its
   `_solver_writer()` seam (14th total — see Invariants).
8. Run the Part C gates: `size_ratchet.py` (`build_controllable_loads` itself, at 736
   lines, was and remains a single over-60-line function — moving it doesn't split it;
   this spec's job is relocation, not decomposition of the function's own internals,
   which is a separate future concern not raised by #1302), `coverage_compare.py`
   (`--moved-code` mapping `solver_inputs/controllable_loads.py` and the `safe_num`
   line in `solver_shared.py`), `noop_patches.py` (confirms all 3 monkeypatch sites —
   the 2 on `_NATIVE_HASS` plus the 1 on `resolve_controllable_load_power_sensor` from
   `test_768_controllable_load_delivery_reconstruction.py` — still patch a name the
   code under test actually reads; both names keep a facade, so this should be a clean
   pass by construction, checked not assumed), `assertions_unchanged.py`.

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
facade names, plus `solver_writer.safe_num is solver_shared.safe_num`), golden master
snapshot-identical for both native scenarios, no new or changed entity/service/config
surface. "Devhub validation: not claimed — a pure internal refactor with nothing for a
live install to exercise differently than before."

## Acceptance

- [ ] Golden master identical for `native_controllable_loads` and
      `native_controllable_loads_two_cycles` (and all other scenarios, unaffected).
- [ ] Full suite passes; the 6 no-facade-needed test files named in Migration step 4
      import from the new location; the 2 monkeypatch-site test files keep patching
      `solver_writer.<name>` unchanged; no other file's assertions edited.
- [ ] `noop_patches.py`: all 3 monkeypatch sites (2 on `_NATIVE_HASS`, 1 on
      `resolve_controllable_load_power_sensor`) still patch a name the code under test
      reads (unaffected by this move, checked not assumed).
- [ ] No line executed before is unexecuted after (`coverage_compare.py`).
- [ ] mypy count not higher; zero in `solver_inputs/controllable_loads.py`; unchanged in
      `solver_shared.py` beyond `safe_num`'s own addition.
- [ ] `nimbus-layers`: `solver_inputs/controllable_loads.py` added to layer 2; total
      `ignore_imports` count is 14 (13 today, per Invariants); the new one is this
      spec's own `_solver_writer()` seam, zero other new violations.
- [ ] `size_ratchet.py`: no new function over 60 lines introduced; `build_controllable_
      loads` itself remains the one already-known over-60-line function, unchanged in
      length (verbatim move).
- [ ] Exactly one new `_solver_writer()` late-import call site (the `_NATIVE_HASS` read
      in `solver_inputs/controllable_loads.py`, the fifth such site overall), matching
      the established precedent — no other new late import anywhere.
- [ ] `test_the_real_tree_still_finds_the_native_hass_callers` reads 5, not 4.
- [ ] `solver_writer.<name> is solver_inputs.controllable_loads.<name>` for all 8
      facade names, and `solver_writer.safe_num is solver_shared.safe_num` — real
      tests, not claims.

## Rollback

Revert the single commit. `solver_inputs/controllable_loads.py` is new and unreferenced
outside `solver_writer.py`'s own re-export block; `safe_num`'s move within
`solver_shared.py` is likewise a pure relocation. No state file format changes, no
migration to undo.
