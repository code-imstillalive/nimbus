# Spec 006: `solver_dispatch/guard.py` — `apply_commanded_state_guard`

Status: **approved — re-measured against the post-Phase-3/Phase-4 tree, no longer
provisional.** Phase 3 (#1302) merged `58832e2`/`2241923`; Phase 4 (#1303) merged
`22bd990`. Both re-measurements below were run independently — once by
`code-imstillalive` on the PR review thread (#1305, two comments), once by this
revision against current `main` — and agree exactly.
Plan: docs/architecture/tech-debt-plan.md; phase: #1305 (Phase 6)
Code cited at: `7ad927b`

## Responsibility

Phase 6 (#1305) moves `apply_commanded_state_guard` — the function that turns each
controllable load's plan-derived target into a real `dispatch_commanded_state()` call
against household hardware (climate entities, relays, EV chargers), subject to min-hold,
max-activations-per-day, relay-chatter and done-condition guards — out of
`solver_writer.py` into `solver_dispatch/guard.py`.

Measured fresh at `7ad927b`: `apply_commanded_state_guard` is still **1,300 lines**,
now at `solver_writer.py:8172`–`9471` (shifted up ~3,500 lines by Phases 1/3/4/5
extracting code ahead of it in the file; the function itself is untouched). Its one
production call site is `main()`, `:10196`, immediately after the tariff-attribution
block spec 004 anticipated (`_flow_decomp_for_tariff`/`tariff_attributed_cost_by_
subentry`, `:10176`–`10192`, built from `solver_publish._flow_decomposition` — Phase
5's own module, confirming both dependent phases have genuinely landed in code, not
just spec).

#1305's own "Scope" section also names a second function, `_widen_shared_charger_cap_
to_achieved` (cited at "123 lines, `solver_writer.py:16163`"). **That citation is stale
and the function is no longer in scope for this phase at all** — it left
`solver_writer.py` during Phase 1 (`git log -S` traces it to the #1308 commits) and
lives today in `solver_inputs/battery_participants.py:923`. Phase 6's real scope is
`apply_commanded_state_guard` alone — unchanged from this spec's first draft, and
confirmed again here since Phase 1's own move is long since merged.

#1305 calls this "the highest-risk phase, done deliberately last," for a real, cited
reason: *"every prior real bug in this area (#726, #741, #733, #769, #770/#782, the
shared-charger widening bug fixed in #1140) lived in exactly this kind of guard logic."*
This spec treats that history as load-bearing, not as scene-setting — see Current
behaviour and the Migration gate. **A second, independent reason has since been added by
Phase 3's own release**: v0.94.428 (Phase 3's first release) could not solve at all in
production, for reasons directly applicable to this phase's own move shape — see "A new
risk this spec must carry, found since the first draft" below.

## Prior art

Not applicable — pure internal module organisation, same as specs 000–005. The dispatch-
safety mechanisms themselves (min-hold, max-activations-per-day, relay-chatter guard,
the hard-thermal-deadline-vs-soft-shortfall split) were each separately checked against
EMHASS/HAEO when originally designed (per #603/#774's own issue threads, the latter
citing EMHASS's `docs/thermal_model.md` directly); this spec relocates the existing,
already-justified guard, it introduces no new dispatch mechanism.

## The re-measurement this spec's first draft required before implementation

The first draft of this spec (cited at `4bec705`, pre-Phase-3/4) found 10 module-level
blockers and was explicit that the count was provisional: *"do not treat the Façade/
Migration sections as final until Phase 3 and Phase 4 have actually merged and this
spec's own measurement has been re-run against the post-merge tree."* Both have now
merged. The re-measurement:

```
$ python tests/analyse_module_dependencies.py --phase 6      # at main = 7ad927b

### apply_commanded_state_guard  (1300 lines)
    importable:              13
    module-level (blockers):  6
      _FLOOR_CROSSING_WARNED, _NATIVE_HASS, _REAFFIRM_CAP_WARNED,
      _fetch_weather_hourly_forecast, _resolve_reaffirm_after_seconds,
      dispatch_commanded_state

lines in scope:      1300
distinct blockers:   6
```

**Six, not ten** — the four names Phase 3 claimed (`_resolve_controllable_load_tuning`,
`_resolve_hour_to_period_index`, `_earliest_period_for_same_day_window`,
`resolve_controllable_load_power_sensor`) are gone from this list because they now live
in `solver_inputs/controllable_loads.py`, exactly as the first draft predicted. The
remaining six are **precisely** the names the first draft already identified as
"genuinely Phase 6's own" — nothing in the final answer contradicts the original
design, it only confirms it. `code-imstillalive` independently re-ran the same
measurement twice on the PR review thread (once after Phase 3 alone merged, finding an
intermediate 19-name superset before correctly narrowing it — a method note on that
thread is worth reading: a first pass missed names arriving through a module-level
`try:`/`except ImportError:` block, because a naive AST walk over `tree.body` only
doesn't see inside it; walk `ast.Try` and `ast.If` at module scope too, not just the
top level); once again after Phase 4 merged, confirming the six above are final. This
revision re-ran the same command independently rather than trusting either prior number,
and got the same six.

**One correction to the first draft's own facade table, from the same review**: spec
003 as implemented kept a facade for all eight moved names, not only the three its own
caller-count criterion strictly required — because `main()` calls
`build_controllable_loads` as a bare name, and re-exporting all eight in one statement
was cheaper than a mixed import. That means `_resolve_controllable_load_tuning`,
`_resolve_hour_to_period_index` and `_earliest_period_for_same_day_window` are in fact
reachable via `solver_writer`'s own facade today, not only via a direct
`solver_inputs.controllable_loads` import — moot for this spec regardless, since none
of the three are Phase 6's own blockers any more, but worth recording so nobody re-opens
the "no facade" claim in spec 003's own text against what's actually shipped.

## Measured cost

Per-blocker role, final:

| Name | Lines | Disposition |
|---|---|---|
| `dispatch_commanded_state` | 92 | moves with the guard — its whole purpose |
| `_resolve_reaffirm_after_seconds` | 42 | moves with the guard |
| `_fetch_weather_hourly_forecast` | 35 | **stays in `solver_writer.py`** — genuinely shared, see Façade decision |
| `_NATIVE_HASS` | — (module state) | stays in `solver_writer.py`, reached via `sw._NATIVE_HASS` — the established 6th-and-counting use of the same seam every `solver_inputs/*` module already uses |
| `_FLOOR_CROSSING_WARNED` | — (module set) | moves with the guard |
| `_REAFFIRM_CAP_WARNED` | — (module set) | moves with the guard |

Second-level scan (checking `_fetch_weather_hourly_forecast`'s own body for further free
variables, same discipline the first draft applied):

```
_fetch_weather_hourly_forecast  (35 lines)  blockers: ha_call_service_with_response
dispatch_commanded_state        (92 lines)  blockers: none
_resolve_reaffirm_after_seconds (42 lines)  blockers: none
```

`ha_call_service_with_response` has zero production callers outside the module and 3
monkeypatch sites, none dispatch-related (`test_1290_calendar_all_day_event_is_local_
not_utc.py`, `test_467_calendar_fetch.py`, `test_weather_forecast_mirror.py`) —
confirmed unchanged from the first draft. It stays in `solver_writer.py` alongside
`_fetch_weather_hourly_forecast`.

`--callers` for the six final blockers plus `apply_commanded_state_guard` itself, run
fresh against `7ad927b`:

```
### apply_commanded_state_guard        production callers: 0   test files: 3
### _FLOOR_CROSSING_WARNED             production callers: 0   test files: 0
### _REAFFIRM_CAP_WARNED               production callers: 0   test files: 1
### dispatch_commanded_state           production callers: 0   test files: 0
### _resolve_reaffirm_after_seconds    production callers: 0   test files: 0
### _NATIVE_HASS                       production callers: 5   test files: 15
    MONKEYPATCHED IN: test_1357_step2_oracle_bundle_builder.py,
      test_controllable_load_power_sensor_discovery.py,
      test_participant_power_per_row_units.py

A compatibility facade IS load-bearing for: _NATIVE_HASS.
```

`_NATIVE_HASS`'s own production-caller count grew from 4 to 5 since the first draft
(Phase 3 added `solver_inputs/controllable_loads.py` as a fifth reader, plus a new
monkeypatch site from #1357's own oracle work) — expected drift, same shape every prior
phase's own `_NATIVE_HASS` count has shown, not a surprise. The other five blockers:
zero production callers, zero monkeypatch sites, confirmed clean for a Phase-1-style
move.

## A new risk this spec must carry, found since the first draft

**Phase 3's own release (v0.94.428) could not solve at all in production**, for a reason
this phase will face in identical shape. Seven relative imports in the moved
`solver_inputs/controllable_loads.py` were written one level too shallow — correct for
`solver_writer.py` at the package root, wrong one level down in `solver_inputs/`.
`ModuleNotFoundError` on every native-mode solve; `solve_now` returned `success: true`
because the service call itself didn't error, while every actual solve failed. The full
stub suite passed throughout, because each broken import sits in a `try:`/`except
ImportError:` pair whose absolute fallback succeeds under pytest (`pyproject.toml`'s
`pythonpath` puts `custom_components/nimbus_load` on the path) — the exact masking this
project's own `_solver_writer()` pattern relies on elsewhere, working against detection
here. Fixed in v0.94.429 (#1415), with a new static guard,
`tests/test_relative_import_depth_resolves.py`.

**This phase is the same shape of move** — `solver_writer.py` at the package root into
a brand-new `solver_dispatch/` subpackage, one level down, exactly like Phase 3's own
`solver_inputs/` move. Any relative import inside `apply_commanded_state_guard`'s 1,300
lines (or in the deferred-import blocks this spec adds) is exposed to the identical trap.
`tests/test_relative_import_depth_resolves.py` now exists and will catch it statically —
but only if it's actually run and its result read, not assumed passing because the rest
of CI is green. Folded into Migration and Acceptance below as a named, non-optional step,
not left to be rediscovered the same way twice.

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Real `dispatch_commanded_state()` service calls (climate/switch/etc.) per load, gated by activation-allowed/min-hold/max-activations/relay-chatter/done-condition | `solver_writer.py:8172`–`9471` | `tests/test_commanded_state_guard_reports_its_own_failure.py`, `tests/test_dispatch_failure_does_not_consume_an_activation.py`, `tests/test_command_divergence_detector.py`, `tests/test_reaffirm_cap_exhaustion_is_silent.py`; golden scenarios (`tests/golden/scenarios.py`, `tests/golden/scenarios_native.py`, `tests/golden/scenarios_thermal.py` — native-mode and thermal coverage) |
| Thermal-kind branch (target-temperature deadline, comfort-floor reheat) | inside the guard's own thermal-kind case | `tests/test_thermal_loads_learn_their_own_rates.py`, `tests/golden/scenarios_thermal.py` |
| `_FLOOR_CROSSING_WARNED`/`_REAFFIRM_CAP_WARNED` log-once-per-key WARNING behaviour | `:9149`, `:9404` | `tests/test_reaffirm_cap_exhaustion_is_silent.py` |
| `load_run_state.py`'s own activation bookkeeping (day_key, activations_today) read/written by this guard | cross-file, not moving | `tests/test_load_run_state.py` |
| Relative-import depth (new, see above) | any relative import added inside the moved span or its new deferred-import blocks | `tests/test_relative_import_depth_resolves.py` — **must be run and read, not assumed** |

**This spec does not itself write the characterization-test matrix #1305's own Method
step 1 calls for** (*"every load kind crossed with every guard branch"*) — the existing
test files above give real, if not necessarily exhaustive, coverage of most of that
matrix already, built incrementally across #726/#741/#733/#769/#770/#782/#1140's own
fixes, plus the golden-master thermal scenarios that landed since the first draft.
Confirming the matrix is actually complete (not just "a test file with a plausible name
exists") remains implementation-time work.

**Checked, not assumed: `apply_commanded_state_guard`, `dispatch_commanded_state` and
`_resolve_reaffirm_after_seconds` bind no module-level state via `global`.** Same gotcha
spec 002/003 already surfaced (`_carry_forward_quality_history`'s own `global
_LAST_KNOWN_QUALITY_HISTORY`, unfixable by a facade alias). AST-walked all three
`FunctionDef`/`AsyncFunctionDef` nodes at `7ad927b`: zero `ast.Global` hits — unchanged
from the first draft's own check. This is also what makes `_FLOOR_CROSSING_WARNED`/
`_REAFFIRM_CAP_WARNED` safe to move despite being module-level state: both are
`set[tuple[str, str]]` values **mutated in place** via `.add()` (`:9149`, `:9404`), never
rebound — no function anywhere in `solver_writer.py` declares either name `global`. A
rebound name would have to stay put and be reached as `sw.X = ...` (the `_NATIVE_HASS`
treatment); an in-place-mutated one moves cleanly either way.

**Checked, not assumed: no default argument on the Phase-6-owned functions evaluates a
`solver_writer` module-level name.** `apply_commanded_state_guard` carries four
(`period_hours_arr`, `import_price_arr`, `tariff_attributed_cost_by_subentry`, `cfg`,
each `=None`), `dispatch_commanded_state` carries one (`climate_on_hvac_mode=None`),
`_resolve_reaffirm_after_seconds` carries none — every one a plain `ast.Constant`
`None`, none a `Name` node resolving to anything at module scope, so none is subject to
spec 002's own `prior_read: str = PRIOR_READ_OK` trap.

## Interfaces

Signature unchanged — a placement move, matching Phases 1–5's own convention, not a
redesign:

```python
# solver_dispatch/guard.py

def apply_commanded_state_guard(...) -> None: ...  # signature as it exists today,
                                                     # copied verbatim, not re-derived
async def dispatch_commanded_state(...) -> ...: ...
def _resolve_reaffirm_after_seconds(...) -> float: ...
_FLOOR_CROSSING_WARNED: set[tuple[str, str]] = set()
_REAFFIRM_CAP_WARNED: set[tuple[str, str]] = set()
```

`_fetch_weather_hourly_forecast` and `ha_call_service_with_response` are **not** part of
this module's interface — reached via `_solver_writer()` (see Façade decision).
`_NATIVE_HASS` is reached the same way, for the same reason every `solver_inputs/*`
module already reaches it. Nothing from Phase 3's own territory
(`solver_inputs.controllable_loads`) appears in this module's own free-variable set any
more — confirmed by the final six-name blocker list above, not assumed from the first
draft's prediction.

## Façade decision

Final, not provisional:

- `apply_commanded_state_guard`, `dispatch_commanded_state`,
  `_resolve_reaffirm_after_seconds`, `_FLOOR_CROSSING_WARNED`, `_REAFFIRM_CAP_WARNED`:
  **no facade** — zero production callers, zero monkeypatch sites outside
  `solver_writer.py`, confirmed fresh at `7ad927b`. Any touching test file retargeted,
  same Phase-1 convention.
- **`_NATIVE_HASS` stays in `solver_writer.py`, reached via `sw._NATIVE_HASS`** — a real
  facade requirement, not a non-move-by-default: 5 production callers
  (`solver_inputs/battery_participants.py`, `solver_inputs/controllable_load_history.py`,
  `solver_inputs/controllable_loads.py`, `solver_inputs/extra_batteries.py`,
  `solver_shared.py`) and 3 monkeypatch sites all resolve it as a `solver_writer` module
  attribute. This is the sixth genuine use of the same established
  `_solver_writer()`-late-import pattern, not a new design decision — per spec 001
  Migration step 1, this module-level state variable's real home is Phase 7's own
  concern.
- `_fetch_weather_hourly_forecast` and its own blocker `ha_call_service_with_response`:
  **do not move at all**, confirmed unchanged. `_fetch_weather_hourly_forecast` still
  has a second, non-dispatch caller — `publish_weather_forecast_mirrors()`,
  `solver_writer.py:1785`–`1867` — and `ha_call_service_with_response` remains a generic
  HA-service-call helper shared with calendar/weather-mirror tests, none dispatch-related.
  `apply_commanded_state_guard` reaches `_fetch_weather_hourly_forecast` via the same
  `_solver_writer()` seam `save_plan_state` uses for `PLAN_STATE_PATH` in spec 005 — the
  identical shape, for the identical reason (a name shared across a module boundary with
  a real caller on the side that isn't moving).

## Invariants

- `apply_commanded_state_guard(**same args)` produces byte-identical
  `dispatch_commanded_state`/`record_activation` call sequences pre- and post-move, for
  every existing regression test and golden scenario (including `scenarios_thermal.py`,
  new since the first draft).
- Every `_LOGGER` use in the moved code reaches `solver_shared._LOGGER` directly (per
  `test_callers_mode_counts_only_real_references.py`), never `sw._LOGGER` — the same
  point specs 003/004/005 already had to make explicit.
- **Every relative import added to `solver_dispatch/guard.py`, or to any deferred-import
  block within it, resolves at the correct depth for a subpackage one level below the
  package root** — `tests/test_relative_import_depth_resolves.py` must be run and pass,
  not merely assumed passing because the rest of CI is green (per "A new risk" above,
  this exact class of bug shipped once already and the full stub suite passed anyway).
- `_fetch_weather_hourly_forecast`'s own non-dispatch caller
  (`publish_weather_forecast_mirrors`, `:1785`–`1867`) is untouched by this move — it is
  reached FROM `solver_dispatch/guard.py`, never moved TO it.
- No name Phase 3 already claims is re-claimed here — confirmed by the final six-name
  blocker list containing none of Phase 3's own four.

## Migration

1. Characterization pass (#1305's own Method step 1): for each load kind
   (sheddable/deferrable/thermal) crossed with each guard branch (activation allowed/
   denied, min-hold, max-activations-per-day, relay-chatter, done-condition, thermal
   hard-deadline, thermal comfort-floor reheat), confirm an existing test captures the
   exact `dispatch_commanded_state`/`record_activation` calls made, or add one that
   does, **before** moving a line. Treat a gap found here as a genuine pre-existing
   coverage hole to fix first, not something to paper over with a new test that merely
   re-asserts current (possibly buggy) behaviour.
2. Add `apply_commanded_state_guard`, `dispatch_commanded_state`,
   `_resolve_reaffirm_after_seconds`, `_FLOOR_CROSSING_WARNED`, `_REAFFIRM_CAP_WARNED` to
   `solver_dispatch/guard.py`, verbatim except: `_LOGGER.*` → `solver_shared._LOGGER.*`;
   the `_NATIVE_HASS` read goes through `_solver_writer()` (`sw._NATIVE_HASS`); the
   `_fetch_weather_hourly_forecast` read goes through the same seam
   (`sw._fetch_weather_hourly_forecast(...)`); **every relative import in the moved code
   is written at the correct depth for `solver_dispatch/` (one level below the package
   root), checked against Phase 3's own v0.94.428 failure before assuming a `..`/`.`
   pattern copied from `solver_writer.py` transfers unchanged.**
3. Delete the five moved names from `solver_writer.py`; retarget its own single call
   site (`main()`, `:10196`, immediately after the tariff-attribution block) to
   `solver_dispatch.guard.apply_commanded_state_guard(...)`, imported the same dual-mode
   way every sibling extracted module already is.
4. Retarget the test files touching `apply_commanded_state_guard` (3, per the fresh
   `--callers` run) and `_REAFFIRM_CAP_WARNED` (1) to the new module; re-check the wider
   set that merely mentions the function name (`test_582_same_day_window_helper.py`,
   `test_thermal_loads_learn_their_own_rates.py`, `test_solver_writer_controllable_
   loads.py`, `test_load_run_state.py`, `test_docs_writer_function_set_drift.py`) for
   whether any of those references are real call sites rather than incidental mentions,
   at implementation time.
5. Add `solver_dispatch/guard.py` to `test_diagnostic_log_levels.py`'s
   `_scanned_paths()` if any `#N diag:`-style line lands inside the moved span (check at
   implementation time; none confirmed in this spec's own reading of the current body).
6. **Run `tests/test_relative_import_depth_resolves.py` explicitly and read its result**
   — not implied by "full suite green" — before considering the move complete. This is
   the one gate Phase 3's own release skipped in practice (it existed only after the
   fact) and the reason this phase gets it named as its own step.
7. Full stub suite + real-HA-harness CI green; golden master and golden-nonvacuity
   scenarios byte-identical; mypy delta clean.

## Non-goals

- Re-deciding any name Phase 3 already owns — this spec cites the final, merged result,
  it does not re-derive it.
- Fixing or re-investigating any of #726/#741/#733/#769/#770/#782/#1140 — all already
  closed, cited here only as the reason this move gets the highest verification bar in
  the whole initiative.
- Building the full characterization-test matrix inline in this document — Migration
  step 1 is real, required implementation-time work.
- Any change to the thermal-load hard-deadline-vs-soft-shortfall LP mechanism itself (a
  separate, solver-side concern; this spec only moves the dispatch guard that reads the
  LP's own output).
- Fixing #1360 (the still-open, separate `Unknown`-solve diagnostic issue) or any other
  in-flight issue that happens to touch dispatch — unrelated to this move.

## Deploy timing

**Devhub-gated, mandatory**, per #1305's own step 5 and the plan's gate table.
#1305's own wording is already stronger than any earlier phase's: *"exercise a real
partial-input path (a real load's config subentry), not just a marker check."* Phase 3's
own release sharpens exactly why: **v0.94.428's `solve_now` service call returned
`success: true` while every actual solve failed** — a clean-looking service response is
not evidence of a working solve. Per the standing release-validation directive's own
step 2, and now with a concrete, recent, on-this-repo example of the failure mode it
guards against: confirm `status: optimal` (or a genuinely expected non-optimal) from a
real solve, not merely that the service call didn't error. A clean restart and a
non-erroring `solve_now` are necessary and nowhere near sufficient here.

## Acceptance

- [ ] Full per-kind, per-branch characterization tests pass byte-for-byte pre/post move
      (Migration step 1).
- [ ] Golden master and golden-nonvacuity scenarios (`tests/golden/scenarios.py`,
      `tests/golden/scenarios_native.py`, `tests/golden/scenarios_thermal.py`)
      identical.
- [ ] Full suite passes; no existing assertion's meaning edited.
- [ ] **`tests/test_relative_import_depth_resolves.py` run and confirmed passing** — not
      inferred from a green suite (Migration step 6).
- [ ] `test_callers_mode_counts_only_real_references.py` stays green (`_LOGGER` reached
      via `solver_shared`, never `sw.`).
- [ ] mypy count not higher; zero in `solver_dispatch/guard.py`'s own additions.
- [ ] `nimbus-layers`: `solver_dispatch` added as its own layer entry, with exactly the
      `_solver_writer()` exceptions this spec's own Façade section documents
      (`_NATIVE_HASS`, `_fetch_weather_hourly_forecast`) — no undocumented new exception.
- [ ] Devhub deploy + restart + `solve_now` **with `status: optimal` confirmed from the
      actual solve, not just a non-erroring service response** + a real dispatch
      exercise against a real load's config subentry + log sweep for new WARNING/ERROR
      — required before production deploy, per #1305's own step 5.
- [ ] No line executed before is unexecuted after.

## Rollback

Revert the single commit. `solver_dispatch/guard.py` is new; `solver_writer.py` regains
its own `apply_commanded_state_guard` and its one call site (`main()`, `:10196`) is
pointed back at itself. No state-file format change — `load_run_state.py`'s own
bookkeeping format is untouched by this move regardless of which side of the revert is
active.
