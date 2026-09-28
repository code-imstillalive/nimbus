# Spec 007: HA bridge, cycle lock, and standalone entrypoint

Status: **7a implemented 2026-09-29 (#1425). 7b WITHDRAWN -- see "Outcome
(Phase 7b)" below: its own premise was refuted by a decision spec 001 had
already made and documented, and this spec was wrong to propose it.**
Plan: docs/architecture/tech-debt-plan.md; phase: #1306

All line numbers and counts in this spec are measured at `main` = `f7c3800`
(post-#1407), stated so they can be re-derived rather than trusted.

## Responsibility

Three separable things still sit in `solver_writer.py` that are not solve logic
at all, and that change for reasons unrelated to everything they sit beside:

1. **The native/REST duality** — `_NATIVE_HASS` (`:1992`), `set_native_hass`
   (`:2075`, 48 lines), `ha_call_service` (`:2291`, 24), `ha_call_service_with_response`
   (`:2317`, 76), `_load_token` (`:1950`, 25), `TOKEN_PATH` (`:622`),
   `_TOKEN_LOADED` (`:1947`). This is the switch that decides whether a read goes
   through `hass.states` or over HTTP. It changes when Home Assistant's API
   changes, never when the LP changes.
2. **The cycle lock** — `acquire_lock` (`:5685`, 75 lines), `release_lock`
   (`:5762`, 15), `LOCK_PATH` (`:649`), `_IN_PROCESS_LOCK` (`:5682`). Mutual
   exclusion between overlapping solve cycles. Changes when the scheduling model
   changes.
3. **The standalone entrypoint** — the `if __name__ == "__main__":` block
   (`:12837`, 18 lines to EOF) and its environment handling. Never imported by the
   runtime; exists only for the cron/systemd deployment shape.

263 lines of function bodies plus five module-level names plus the entrypoint.

**Two claims in #1306's own scope are stale, and both change the design.** They
were written before Phases 2a and 3 landed, and this spec supersedes them:

- **"`ha_get`/`ha_post_state` … -> `solver/ha_bridge.py`"** — those two moved to
  `solver_shared.py` in **spec 001 (Phase 2a)**. `ha_get` is at
  `solver_shared.py:476` today. There is nothing left in `solver_writer.py` to
  move. What remains of the transport surface is the *service-call* pair and the
  native/REST switch itself.
- **"PID lock (the cron/standalone entrypoint, never imported by the runtime) ->
  `standalone_writer.py`"** — `acquire_lock` and `release_lock` each have a
  production caller in **`solver_runtime.py`**, the native runtime. The lock is
  *not* standalone-only and therefore cannot live in a standalone-only module. It
  gets its own module here.

## Why this phase is the one that pays off the architecture

Every phase so far has *added* users of the deferred `_solver_writer()` seam:
`solver_inputs/*` and `solver_reports/*` reach **upward** into `solver_writer` at
call time because the thing they need — principally `_NATIVE_HASS` — lives above
them. That is why `pyproject.toml` currently carries **14 `ignore_imports`
entries**, every one of them an exemption for that upward reach.

The layer map is:

```
solver_writer | (standalone_writer)                 <- layer 1
(solver_plan) | solver_publish | (solver_dispatch)
solver_inputs | (solver_reports)
solver_shared
solver                                              <- layer 5, lowest
```

`solver/` is the **lowest** layer, so anything placed there is importable
*downward* by every module that currently reaches upward for it. Moving
`_NATIVE_HASS` into `solver/ha_bridge.py` is therefore not a tidying move: it is
what makes the seam unnecessary. `solver_inputs/battery_participants.py` can
`from ..solver.ha_bridge import native_hass` at module scope, legally, and delete
its `_solver_writer()` accessor.

`standalone_writer` is already declared in that layer map, parenthesised — the
plan anticipated it.

This is also why Phase 7 goes last, and #1306's own reasoning ("re-validating it
after every subsequent phase instead of once") is right but understates it: the
seam's *user set* only stops growing once every other extraction has settled.

## Measured cost

`python tests/analyse_module_dependencies.py --callers` over the eleven names,
at `f7c3800`:

```
production callers outside the module : 9
monkeypatch sites                     : 27
test files touching any target        : 24

A compatibility facade IS load-bearing for: ha_call_service,
ha_call_service_with_response, acquire_lock, release_lock, set_native_hass,
_load_token, _NATIVE_HASS.
```

| name | prod callers | test files | patch sites (tool) |
|---|---|---:|---:|
| `_NATIVE_HASS` | `solver_inputs/battery_participants.py`, `solver_inputs/controllable_load_history.py`, `solver_inputs/controllable_loads.py`, `solver_inputs/extra_batteries.py`, `solver_shared.py` | 15 | 3 |
| `acquire_lock` | `solver_runtime.py` | 5 | 9 |
| `release_lock` | `solver_runtime.py` | 5 | 9 |
| `set_native_hass` | `solver_runtime.py` | 6 | 0 |
| `_load_token` | `solver_shared.py` | 1 | 1 |
| `ha_call_service_with_response` | — | 1 | 3 |
| `ha_call_service` | — | 0 | 2 |
| `LOCK_PATH` | — | 2 | 0 |
| `_TOKEN_LOADED` | — | 1 | 1 |
| `_IN_PROCESS_LOCK` | — | 1 | 0 |
| `TOKEN_PATH` | — | 0 | 0 |

### The number that decides this spec's shape

**`_NATIVE_HASS` is assigned directly — `solver_writer._NATIVE_HASS = …` — at 192
sites across 15 test files.** The tool reports 3 patch sites because it counts
`patch.object` and `monkeypatch.setattr`; direct assignment is invisible to it,
and invisible for the same reason to `tests/gates/noop_patches.py`, whose
instrumentation hooks `unittest.mock._patch.__enter__` and `MonkeyPatch.setattr`
and sees neither. That blindness is recorded in #1400 and #1401 and deliberately
left unfixed there.

Production reads it 37 times, all as `sw._NATIVE_HASS` through the seam.

**So the gate that exists to police exactly this class of move is blind to the
dominant patch form used on the riskiest name in the whole initiative.** A missed
retarget among 192 sites would not fail; it would pass having silently exercised
the real object. That is not a reason to skip the move — it is the reason this
spec splits in two.

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Native-mode reads resolve through `hass.states` rather than HTTP | `_NATIVE_HASS` set by `set_native_hass`, read by `solver_shared.ha_get` | every golden scenario named `native_*` (`tests/golden/scenarios_native.py`, `scenarios_thermal.py`) |
| REST/cron-mode reads resolve over HTTP with a bearer token | `_load_token` → `solver_shared.ha_get` | `tests/golden/fake_ha.py`-backed scenarios (`guardrail*`, `scenarios.py`) |
| A second solve cycle cannot overlap the first | `acquire_lock` `:5685` | `tests/test_main_load_forecast_startup_race.py`; `guardrail_two_cycles`, `native_controllable_loads_two_cycles` |
| A stale lock from a killed process is recoverable without a reboot | `acquire_lock`'s stale-PID branch | `acquire_lock`'s own docstring cites the 45–52 s baseline; `tests/test_main_*` lock patches |
| Controllable-load switches and climate/water-heater calls reach HA | `ha_call_service` `:2291` | `tests/test_read_load_forecast_sensor.py`, `tests/test_load_forecast_error_notification_lifecycle.py` |
| Calendar and weather-forecast reads that need a service **response** | `ha_call_service_with_response` `:2317` | `tests/test_467_calendar_fetch.py`, `tests/test_1290_calendar_all_day_event_is_local_not_utc.py`, `tests/test_weather_forecast_mirror.py` |
| The cron entrypoint still runs standalone | `__main__` `:12837` | **nothing** — see Non-goals |

**Golden coverage is adequate for 1 and 2 and thin for 3.** The native scenarios
exercise `_NATIVE_HASS` on every solve, and the REST scenarios exercise the token
path; both transport modes are therefore covered byte-for-byte by the existing
master. The lock is reached by the two `_two_cycles` scenarios but only on its
success path — no scenario exercises the stale-PID branch. **This spec adds a
unit test for that branch before moving it**, rather than moving 75 lines whose
recovery behaviour nothing pins.

## Interfaces

```python
# solver/ha_bridge.py  (layer 5)
class NativeHass(Protocol):
    """The subset of `hass` this package actually uses."""
    states: Any
    services: Any
    config_entries: Any
    loop: Any

def set_native_hass(hass: NativeHass | None) -> None: ...
def native_hass() -> NativeHass | None: ...
def call_service(domain: str, service: str, data: dict) -> None: ...
def call_service_with_response(domain: str, service: str, data: dict) -> dict | None: ...
def token() -> str: ...

# solver/cycle_lock.py  (layer 5)
def acquire(path: str | None = None) -> bool: ...
def release(path: str | None = None) -> None: ...

# standalone_writer.py  (layer 1, already in the layer map)
def main(argv: list[str] | None = None) -> int: ...
```

**`native_hass()` as an accessor function, not a re-exported variable, is the
whole point.** A module-level name that gets *rebound* cannot be aliased — spec
001 recorded this and deferred it here; Phase 2c's
`_LAST_KNOWN_QUALITY_HISTORY` cost five test failures proving it. An accessor is
readable downward at call time by every layer, needs no seam, and has one owner.

No `_solver_writer()` accessor appears in either new module, per the template's
own rule that dependencies arrive as parameters or protocols.

## Façade decision

Re-exported from `solver_writer.py` (a production caller or a patch site resolves
it there at call time):

| name | why |
|---|---|
| `acquire_lock`, `release_lock` | 9 patch sites each, all on `solver_writer`, in the nine `test_main_*`-shaped suites that drive the real `main()` |
| `ha_call_service`, `ha_call_service_with_response` | 2 and 3 patch sites; zero production callers outside, so the facade is purely for the tests |
| `set_native_hass` | `solver_runtime.py` calls it; `__init__.py` and `services.py` reach it too |
| `_load_token`, `TOKEN_PATH`, `_TOKEN_LOADED` | `solver_shared.py` and `sensor.py` read them |
| **`_NATIVE_HASS`** | **a facade is NOT sufficient here and that is the crux** — see below |

**`_NATIVE_HASS` cannot be handled by a re-export, because a re-export captures
the current object and this name is rebound by `set_native_hass()`.** Leaving
`solver_writer._NATIVE_HASS = None` as a real variable *and* putting the truth in
`ha_bridge` gives two variables and a silent divergence the moment either is
written. The options are exactly:

- **(A) Retarget all 192 direct assignments plus 3 patch sites plus 37 production
  reads.** Correct, and unverifiable by the no-op gate in its current state.
- **(B) Keep `_NATIVE_HASS` in `solver_writer.py` and move only the rest.** Loses
  the seam retirement, which is this phase's entire architectural payoff.
- **(C) Fix the gate's direct-assignment blindness first, then do (A).** Makes (A)
  verifiable mechanically instead of by 192 careful greps.

**This spec chooses (C), and splits accordingly.**

## Invariants

- `solver_writer.acquire_lock is solver.cycle_lock.acquire` and the same identity
  for every other re-exported name — `is`, not `==`.
- `solver/ha_bridge.py` and `solver/cycle_lock.py` contain **no** import of
  `solver_writer`, deferred or otherwise, and no `global` statement naming
  anything `solver_writer.py` also defines at module level.
- Exactly one `_NATIVE_HASS` storage location exists after 7b. Asserted by a test
  that sets it through `set_native_hass()` and reads it through every production
  path (`solver_shared`, each `solver_inputs` module) expecting the same object.
- After 7b, `ignore_imports` in `pyproject.toml` loses every entry whose only
  reason was the `_NATIVE_HASS` reach. **Measured today: 14 entries; the target
  is the number that remain for genuinely different reasons, stated in the PR
  rather than assumed to be zero.**
- `standalone_writer.py` is importable without a `hass` present and without a
  token file existing, and importing it runs no solve.

## Migration

**Part 7a — the lock, the service calls, the token. No `_NATIVE_HASS`.**

1. Add a unit test for `acquire_lock`'s stale-PID recovery branch, which no
   golden scenario reaches. Must pass before anything moves.
2. Create `solver/cycle_lock.py`; move `acquire_lock`/`release_lock`/`LOCK_PATH`/
   `_IN_PROCESS_LOCK` verbatim. Re-export all four from `solver_writer.py`.
3. Create `solver/ha_bridge.py`; move `ha_call_service`,
   `ha_call_service_with_response`, `_load_token`, `TOKEN_PATH`, `_TOKEN_LOADED`
   verbatim. Re-export.
4. Verify byte-identity per function before formatting and AST-identity after —
   the standard this initiative has used since Phase 2a.
5. Create `standalone_writer.py` with the `__main__` block; `solver_writer.py`
   keeps no `__main__`. Confirm the cron invocation path (see Non-goals).
6. Inventories: size ratchet baseline, `_EXPECTED_IGNORED_IMPORT_COUNT`,
   `test_docs_writer_function_set_drift.py`'s name sets, and the facade-identity
   test gains the seven re-exported names.

**Part 7b — `_NATIVE_HASS`, blocked on the gate.**

7. **Blocked until `tests/gates/noop_patches.py` can observe a direct attribute
   assignment.** #1400 records the blindness; #1401 records the measured scale
   (14 names, 231 sites, of which `_NATIVE_HASS` alone is 192 across 15 files).
   Not attempted here.
8. Move `_NATIVE_HASS` and `set_native_hass` into `solver/ha_bridge.py`, exposed
   as `native_hass()` / `set_native_hass()`.
9. Retarget the 37 production reads: each `solver_inputs`/`solver_reports` module
   replaces `sw._NATIVE_HASS` with a module-scope
   `from ..solver.ha_bridge import native_hass` and deletes its
   `_solver_writer()` accessor where nothing else needs it.
10. Retarget all 195 test sites (192 assignments + 3 patches) to `ha_bridge`.
11. Retire the `ignore_imports` entries that existed only for the seam, and lower
    `_EXPECTED_IGNORED_IMPORT_COUNT` to match.

## Non-goals

- **Fixing the gate's direct-assignment blindness.** That is 7b's blocker, not
  its content; it belongs to #1316's own thread (#1400, #1401) and needs its own
  design decision about how to observe an assignment.
- **Changing any transport behaviour.** No retry, no timeout, no error-mapping
  change. `ha_call_service_with_response`'s 76 lines move unchanged even where
  they look improvable.
- **Proving the standalone/cron path still runs.** #1306's own "done when" asks
  for this *"if the standalone/cron deployment path exists on any real install"*.
  **Nothing in this repository can answer whether it does** — that is a live-install
  question, and this spec does not pretend otherwise. The reference household runs
  the integration natively (`custom_components/nimbus_load` symlinked into a git
  clone), so its `__main__` may be dead code there. Recorded as a question for the
  household, not an assumption either way.
- **Splitting `acquire_lock`'s 75 lines.** Over the 60-line ratchet threshold
  before and after; relocation is not decomposition.

## Acceptance

- [ ] Golden master identical for every scenario, both transport modes — the
      native scenarios and the `fake_ha`-backed REST scenarios must be
      byte-identical, since both reach this code on every solve.
- [ ] Full suite passes; no existing assertion edited; new tests listed:
      `acquire_lock` stale-PID recovery, facade identity for the seven re-exported
      names, and (7b) the single-storage-location test named in Invariants.
- [ ] No monkeypatch target became a no-op. **For 7b this cannot be asserted by
      the gate today** — that is the blocker, and this box may not be ticked by
      inspection.
- [ ] No line executed before is unexecuted after (`coverage_compare.py`).
- [ ] mypy count not higher; zero in `solver/ha_bridge.py` and
      `solver/cycle_lock.py`.
- [ ] Import contracts pass; `_EXPECTED_IGNORED_IMPORT_COUNT` moves **down** in
      7b, and the PR states the new number and what each survivor is for.
- [ ] No new function over 60 lines; the over-60 count does not rise.
- [ ] **No `_solver_writer()` late import added** — and in 7b, the count of
      modules using it goes down.
- [ ] Spec-specific: `solver_writer.py` contains no `if __name__ == "__main__"`.
- [ ] Spec-specific: released, installed on devhub, and the integration loads
      (`config_entries` `state: loaded`) with entities stamping the new version —
      per the household's standing rule that a change is not done at "merged".

## Outcome (Phase 7b, 2026-09-29): NOT DONE, and should not be done as specified

7a shipped (#1425). **7b is withdrawn**, and the reason is a mistake in this spec
rather than a blocker outside it. Recorded here in full because the Migration
section above still reads as a plan, and following it would undo a decision an
earlier phase made deliberately.

### The blocker this spec named was not the real obstacle

Status above said 7b was blocked until `tests/gates/noop_patches.py` could observe
a direct attribute assignment. That gap is real -- it is now tracked on its own as
**#1434**, where the measured ratio is **9 of 201 sites (4.5%)** for `_NATIVE_HASS`
-- but it was never what made 7b wrong.

### Spec 001 had already decided that these names stay, with reasons

`solver_shared.py`'s own module docstring, written in Phase 2a:

> **`_NATIVE_HASS`/`HA_BASE`/`_load_token()` deliberately stay behind** in
> `solver_writer.py`, reached here via the same deferred, by-MODULE
> `_solver_writer()` seam ...

Two independently-discovered reasons, both still true:

1. **`_NATIVE_HASS` is spec 001's own named exception.** It is *reassigned* by
   `set_native_hass()`, so a plain re-export freezes the importing module's copy at
   import time.
2. **`HA_BASE`/`_load_token()`**:
   `tests/test_solver_writer_import_and_token_laziness.py` **source-scans
   `solver_writer.py` itself** for the literal `open(TOKEN_PATH` call site inside a
   function named `_load_token()`. Verified at `:130-145` rather than taken from the
   docstring -- it builds a path to `solver_writer.py`, reads it, counts
   `open(TOKEN_PATH`, and asserts the one real call site is inside `_load_token()`.
   **Moving that body breaks a real, existing, unedited test.**

**Step 3 of the Migration above proposes moving exactly those three names.** That
is this spec's error, not a discovery about the tree. Spec 006 is @purcell-lab's
and its `_LOGGER` invariant turned out to be right when Phase 6 argued with it;
spec 007 is mine and is wrong here.

### And the patch surface is far larger than the inventory says

Measured on `main` after Phase 6. The Measured-cost table above reports 0-3 patch
sites for these names; the real figures:

| name | patched/assigned on `solver_writer` | read as `solver_writer.<name>` |
|---|---:|---:|
| `ha_call_service` | **6** | 0 |
| `ha_call_service_with_response` | **7** | 3 |
| `_load_token` | **4** | 3 |
| `_TOKEN_LOADED` | **2** | 2 |
| `_TOKEN` | **2** | 2 |
| `TOKEN_PATH` | 0 | 0 |

**21 patch/assign sites.** Every name that was supposed to move is patched where it
lives now, so each would need a *wrapper* facade -- 7a's `acquire_lock` shape, not
an alias -- and for `_TOKEN`/`_TOKEN_LOADED` a wrapper is impossible at all:
`global _TOKEN, _TOKEN_LOADED` at `solver_writer.py:1345` binds in the *defining*
module.

The `ha_call_service` row is worth one correction: the two `services.py` hits that
made it look like it had a production caller are **comments**, not calls.

### What remains, and why it is not worth a phase

Subtract the names that must stay and 7b reduces to `ha_call_service` +
`ha_call_service_with_response`, about **100 lines** -- and both read
`_NATIVE_HASS`, which stays, so they would reach layer 1 from layer 5 through the
seam:

| | |
|---|---|
| lines out of an 8,777-line file | ~100 |
| `ignore_imports` | **+1** (15 -> 16) |
| patch sites needing a new wrapper facade | 13 |
| shared state leaving the top layer | **none** |

The Invariants section above states the goal as *"after 7b, `ignore_imports` loses
every entry whose only reason was the `_NATIVE_HASS` reach."* With `_NATIVE_HASS`,
the token trio and `TOKEN_PATH` all staying, that cannot happen -- the list grows.
The phase's own success condition is unreachable by the phase.

### The real item, stated so it is not lost

If the top layer owning `_NATIVE_HASS` and the token state is judged worth fixing,
the fix is **not a code move**. It is replacing a *reassigned module global* with
something aliasable -- `set_native_hass()` writing into a single-owner holder whose
identity never changes, so every layer holds a reference instead of re-resolving a
name through a seam. That removes the reason spec 001 made its exception, and with
it most of this spec.

It also has **201 test write-sites** attached and is a design change, not a
relocation. It deserves its own issue and its own decision rather than arriving as
the tail of a decomposition.

### Where the decomposition finished

```
solver_writer.py   18,708 -> 8,777 lines   (-53%)
size ratchet            67 -> 36            67 -> 61 -> 53 -> 47 -> 39 -> 38 -> 36
```

Phases 0, 2a, 2b/2c, 3, 4, 5, 6 and 7a landed. `standalone_writer.py` (step 5) is
withdrawn with the rest of 7b: it is 18 lines of `__main__` whose only purpose was
to leave `solver_writer.py` alongside the other moves, and on its own it is churn.

## Implementation findings (Phase 7a, 2026-09-29)

Two deviations from the Migration steps above, both measured rather than
preferred. The steps are left as written so the change is visible.

### Step 2: LOCK_PATH did not move; the path is a parameter

The inventory table records LOCK_PATH as having **0 patch sites**. It is rebound
on the solver_writer module object in **four** places, and none is visible to a
literal-name scan:

| site | how |
|---|---|
| tests/_isolated_state.py:74 | patch.object(solver_writer, const, ...), const from a tuple of strings |
| tests/hass_integration/conftest.py:85 | same shape, its own tuple, **behind an `if hasattr(...)` guard** |
| tests/test_solve_overlap_guard.py:63 | plain attribute assignment |
| tests/test_solver_writer_lock_self_pid.py:37 | plain attribute assignment |

Two are the direct-assignment blindness #1400 records. Two are a **new** shape
this spec did not anticipate: a `patch.object` whose target name is a runtime
string, invisible to patch-counting as well. So **#1401's "14 names, 231 sites"
is a floor, not a count**, and that matters because 7b's whole justification is
that the gate can then see every site.

The `hasattr` guard is the decisive detail: had the constant moved, that site
would not have errored, it would have **silently skipped**, and every test in
tests/hass_integration/ would have passed while acquire_lock wrote to the real
/opt PID file. That is #1330's defect class, invisible wherever /opt is absent.

So `solver/cycle_lock.py` takes `lock_path: str`, `solver_writer.py` keeps the
constant and two one-line wrappers, and all four rebind sites are untouched. It
also matches this spec's own interface sketch, which already wrote
`acquire(path=...)` rather than a module constant, and it keeps `solver/` free of
environment reading.

**Consequent invariant change.** `solver_writer.acquire_lock is
solver.cycle_lock.acquire` is false by construction. Replaced by three
assertions in tests/test_1306_phase7a_cycle_lock_facade.py: a rebind of
solver_writer.LOCK_PATH must change where the PID file lands, driven through the
real isolated_state_paths helper; the wrapper must pass that exact value
positionally; and cycle_lock must define no LOCK_PATH of its own. Mutation-
verified -- capturing the path at import time fails exactly two of the six.

`_IN_PROCESS_LOCK` **keeps** the `is` invariant: nothing rebinds it, it is only
acquired and released, so an alias is safe where a path string was not.

The moved functions are AST-identical to the originals modulo six code
substitutions (two signatures gaining the parameter, four LOCK_PATH ->
lock_path), and reversing those six reproduces the original slice byte-for-byte.

### Step 3: ha_bridge.py is blocked, and moves to 7b

The spec places the service calls and token trio in 7a as separable from
_NATIVE_HASS. Measured, they are not:

| function | lines | reads |
|---|---:|---|
| ha_call_service | 24 | _NATIVE_HASS |
| ha_call_service_with_response | 76 | _NATIVE_HASS |
| _load_token | 25 | TOKEN_PATH, _TOKEN_LOADED, **_TOKEN** |

Both service calls read _NATIVE_HASS, whose move is 7b. And _load_token reads a
fourth name step 3 omits -- _TOKEN, a **rebound** module global (`global _TOKEN,
_TOKEN_LOADED` at solver_writer.py:1316), which is the cannot-be-aliased class
spec 001 recorded and Phase 2c's _LAST_KNOWN_QUALITY_HISTORY proved at the cost
of five test failures.

### Step 5: standalone_writer.py follows separately

Kept out of 7a so each stays independently revertable, per Rollback below.

### Also verified

`test_1324`'s detector needed widening: the unlink now reads
os.remove(lock_path) in cycle_lock, so it resolves one wrapper hop to recover
which constant the caller supplies. Its assertion is unchanged -- confirmed by
assertions_unchanged.py, which flags the two ratchet strings and not this.

## Allowed assertion changes (Phase 7a)

`tests/gates/assertions_unchanged.py` fails on any assertion that changed in an
existing test, and it correctly flags two in Phase 7a. Both are the size
ratchet's own reported count, which the phase legitimately lowered, and both are
recorded here rather than waved through -- this section IS the allow-list the
gate reads, scraped from the entry shapes below.

tests/test_gates_size_ratchet.py::test_real_solver_writer_matches_the_tech_debt_plans_own_count
pins the gate's reported figure as a literal string, "39 function(s) over 60
lines". Phase 7a moved acquire_lock (75 lines) out, so the correct figure is 38.
That test's own docstring instructs exactly this change: *"a count BELOW the
baseline is progress that should be banked here, not a failure to work around."*

tests/test_gates_size_ratchet.py::test_real_whole_package_is_a_materially_different_larger_number
asserts "> baseline 39" for the whole-package scope. It moves to 38 with the
baseline so the file is self-consistent at one number rather than half-migrated;
the property it checks -- that the whole-package count materially exceeds the
solver_writer.py-only count -- is unchanged, and it holds against either
baseline.

**Nothing else in the suite is allow-listed for 7a, and that is the point of
listing these two.** In particular
tests/test_1324_deleted_runtime_files_are_not_in_config.py is NOT here: its
detector was widened to resolve a removed path through one wrapper hop, but its
assertion (assertIn("LOCK_PATH", removed)) is untouched -- which the gate itself
confirms by not flagging it.

## Rollback

Revert 7a and 7b as separate commits; they are independently shippable by
construction. Neither writes a state file, and neither changes the format of
`plan_state.json` or any `NIMBUS_SOLVER_*_PATH` file, so a revert needs no state
attention. The one operational caveat: if step 5 lands and a real cron/systemd
unit still invokes `solver_writer.py` as `__main__`, that invocation breaks until
the unit is repointed at `standalone_writer.py` — which is why the Non-goals
section refuses to guess whether such a unit exists.
