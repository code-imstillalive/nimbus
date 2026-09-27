# Spec 006: `solver_dispatch/guard.py` — `apply_commanded_state_guard`

Status: proposed — **measurement is provisional, see "A real prior finding" below; do
not treat the Façade/Migration sections as final until Phase 3 (spec 003) and Phase 4
(spec 004) have actually merged and this spec's own measurement has been re-run against
the post-merge tree.**
Plan: docs/architecture/tech-debt-plan.md; phase: #1305 (Phase 6)
Code cited at: `4bec705`

## Responsibility

Phase 6 (#1305) moves `apply_commanded_state_guard` — the function that turns each
controllable load's plan-derived target into a real `dispatch_commanded_state()` call
against household hardware (climate entities, relays, EV chargers), subject to min-hold,
max-activations-per-day, relay-chatter and done-condition guards — out of
`solver_writer.py` into `solver_dispatch/guard.py`.

Measured fresh at `4bec705`: `apply_commanded_state_guard` is **1,300 lines**,
`solver_writer.py:11708`–`13007`.

#1305's own "Scope" section also names a second function, `_widen_shared_charger_cap_
to_achieved` (cited at "123 lines, `solver_writer.py:16163`"). **That citation is stale
and the function is no longer in scope for this phase at all** — see "A real prior
finding" below.

#1305 calls this "the highest-risk phase, done deliberately last," for a real, cited
reason: *"every prior real bug in this area (#726, #741, #733, #769, #770/#782, the
shared-charger widening bug fixed in #1140) lived in exactly this kind of guard logic."*
This spec treats that history as load-bearing, not as scene-setting — see Current
behaviour and the Migration gate.

## Prior art

Not applicable — pure internal module organisation, same as specs 000–005. The dispatch-
safety mechanisms themselves (min-hold, max-activations-per-day, relay-chatter guard,
the hard-thermal-deadline-vs-soft-shortfall split) were each separately checked against
EMHASS/HAEO when originally designed (per #603/#774's own issue threads, the latter
citing EMHASS's `docs/thermal_model.md` directly); this spec relocates the existing,
already-justified guard, it introduces no new dispatch mechanism.

## A real prior finding this spec must not overlook

**Two findings, found by reading rather than trusted from #1305's own text — both
change this spec's scope, one of them materially.**

**1. `_widen_shared_charger_cap_to_achieved` already left `solver_writer.py`, during
Phase 1.** `git log -S"_widen_shared_charger_cap_to_achieved" --oneline --all` finds it
first appearing in Phase 1's own commits (#1308, "Phase 1 of #1298: extract
battery-fleet functions into solver_inputs/"); it lives today at
`solver_inputs/battery_participants.py:923`, called from that same file's own
`:802`, with zero references anywhere in `solver_writer.py`. #1305's own scope
line is thirteen days stale, from before Phase 1 landed. **Phase 6's real scope is
`apply_commanded_state_guard` alone** — one function, not two, and the shared-charger
widening logic is already someone else's already-shipped move, not this spec's to
re-litigate.

**2. Half of `apply_commanded_state_guard`'s own measured blockers are ALSO Phase 3's
already-spec'd targets, at a different, more advanced stage of the same plan.**
Running `tests/analyse_module_dependencies.py --phase 6` against `4bec705` finds ten
blockers (below). Cross-checking each against spec 003 (`docs/specs/
003-controllable-loads-input.md`, on the still-open `spec-003-controllable-loads-input`
branch, PR #1370) finds **five of the ten already claimed there**:
`_NATIVE_HASS`, `_resolve_controllable_load_tuning`, `_resolve_hour_to_period_index`,
`_earliest_period_for_same_day_window`, and `resolve_controllable_load_power_sensor` are
all named in spec 003's own "Measured cost" table as `build_controllable_loads`'s own
blockers, moving to `solver_inputs/controllable_loads.py` — not staying in
`solver_writer.py` for this spec to find there.

This is not a conflict to resolve by picking a winner — it is the exact reason #1305's
own "Dependencies" line names Phase 3 explicitly (*"needs stable battery/load
configs"*): `apply_commanded_state_guard` and `build_controllable_loads` are neighbours
in the same file today (`resolve_controllable_load_power_sensor` at `:10495`,
`build_controllable_loads` starting three lines later at `:10526`) and share several
small helpers between them. **Once Phase 3 lands, those five names will already be gone
from `solver_writer.py`**, three with no facade (`_resolve_controllable_load_tuning`,
`_resolve_hour_to_period_index`, `_earliest_period_for_same_day_window` — spec 003's own
"no facade, retarget the caller" list) and two with one (`_NATIVE_HASS`,
`resolve_controllable_load_power_sensor` — spec 003's own explicit façade decision,
*already written to account for this exact future caller*: spec 003's own text reads
*"`resolve_controllable_load_power_sensor` gained a real external caller ...
`sw.resolve_controllable_load_power_sensor(data)` through the same seam"* — the "real
external caller" spec 003 is describing is this function, moving here).

**Consequence for this spec**: the Façade/Migration sections below describe today's
state (`4bec705`, pre-Phase-3-merge) so the shape of the problem is on record, but they
are not this spec's final answer. Once Phase 3 (and Phase 4, for the `Plan` shape this
guard also reads) actually merge, re-run `--phase 6`'s equivalent measurement against
the new tree before implementation starts — the blocker set, and which names need a
`_solver_writer()` seam versus a plain `solver_inputs.controllable_loads` import, will
be different from what is measured here.

## Measured cost

`python tests/analyse_module_dependencies.py --phase 6` (`solver_writer.py` at
`4bec705`):

```
### apply_commanded_state_guard  (1300 lines)
    importable:              9
    module-level (blockers): 10
      _FLOOR_CROSSING_WARNED, _NATIVE_HASS, _REAFFIRM_CAP_WARNED,
      _earliest_period_for_same_day_window, _fetch_weather_hourly_forecast,
      _resolve_controllable_load_tuning, _resolve_hour_to_period_index,
      _resolve_reaffirm_after_seconds, dispatch_commanded_state,
      resolve_controllable_load_power_sensor

lines in scope:      1300
distinct blockers:   10
```

Per-blocker role, and which phase actually owns each one:

| Name | Lines | Owner | Notes |
|---|---|---|---|
| `_FLOOR_CROSSING_WARNED` | — (module set) | **Phase 6** | log-once-per-key state for a floor-crossing WARNING |
| `_REAFFIRM_CAP_WARNED` | — (module set) | **Phase 6** | log-once-per-key state for a reaffirm-cap-exhaustion WARNING |
| `dispatch_commanded_state` | 92 | **Phase 6** | the actual HA service-call dispatch |
| `_resolve_reaffirm_after_seconds` | 42 | **Phase 6** | reaffirm-cadence resolution |
| `_fetch_weather_hourly_forecast` | 35 | **shared, stays in `solver_writer.py`** | called elsewhere too (`:2482`, ambient-temperature covariate, #481) — see Façade decision |
| `_NATIVE_HASS` | — (module state) | **Phase 3 (spec 003)** | facade kept there specifically for this caller |
| `_resolve_controllable_load_tuning` | 70 | **Phase 3 (spec 003)** | no facade there; retarget the import |
| `_resolve_hour_to_period_index` | 30 | **Phase 3 (spec 003)** | no facade there; retarget the import |
| `_earliest_period_for_same_day_window` | 75 | **Phase 3 (spec 003)** | no facade there; retarget the import |
| `resolve_controllable_load_power_sensor` | 29 | **Phase 3 (spec 003)** | facade kept there specifically for this caller |

Second-level scan (one level past the ten direct blockers, same discipline spec 005
applied — checking each blocker's own body for further free variables), run against the
four names genuinely Phase 6's own:

```
$ python tests/analyse_module_dependencies.py --functions "_fetch_weather_hourly_forecast,dispatch_commanded_state,_resolve_reaffirm_after_seconds"

_fetch_weather_hourly_forecast  (35 lines)  blockers: ha_call_service_with_response
dispatch_commanded_state        (92 lines)  blockers: none
_resolve_reaffirm_after_seconds (42 lines)  blockers: none
```

`--callers` for `ha_call_service_with_response` (checked because
`_fetch_weather_hourly_forecast` needs it):

```
production callers outside the module : 0
monkeypatch sites                     : 3
    (test_1290_calendar_all_day_event_is_local_not_utc.py,
     test_467_calendar_fetch.py, test_weather_forecast_mirror.py)
```

None of those three monkeypatch sites are dispatch-related — `ha_call_service_with_
response` is a generic HA-service-call helper shared by the calendar and weather-
forecast-mirror features, unrelated to controllable-load dispatch. It stays in
`solver_writer.py` (see Façade decision) rather than moving into a module whose whole
purpose is dispatch guarding.

`--callers` for the four genuinely-Phase-6 names plus `apply_commanded_state_guard`
itself:

```
### apply_commanded_state_guard        production callers: 0   test files: 3
### _FLOOR_CROSSING_WARNED             production callers: 0   test files: 0
### _REAFFIRM_CAP_WARNED               production callers: 0   test files: 1
### dispatch_commanded_state           production callers: 0   test files: 0
### _resolve_reaffirm_after_seconds    production callers: 0   test files: 0

No production caller and no monkeypatch site outside the module.
```

All five clear for a Phase-1-style move: no facade, plain relocation, any touching
test file retargeted.

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Real `dispatch_commanded_state()` service calls (climate/switch/etc.) per load, gated by activation-allowed/min-hold/max-activations/relay-chatter/done-condition | `solver_writer.py:11708`–`13007` | `tests/test_commanded_state_guard_reports_its_own_failure.py`, `tests/test_dispatch_failure_does_not_consume_an_activation.py`, `tests/test_command_divergence_detector.py`, `tests/test_reaffirm_cap_exhaustion_is_silent.py`; golden scenarios (`tests/golden/scenarios.py`, `tests/golden/scenarios_native.py` — native-mode coverage per spec 003's own citation of #1335) |
| Same-day-window earliest-period resolution (#582) | via `_earliest_period_for_same_day_window` (Phase 3's own target) | `tests/test_582_same_day_window_helper.py` |
| Thermal-kind branch (target-temperature deadline, comfort-floor reheat) | inside the guard's own thermal-kind case | `tests/test_thermal_loads_learn_their_own_rates.py` (and whatever thermal-guard-specific coverage exists at implementation time — re-check against current `main`, this repo moves fast) |
| `_FLOOR_CROSSING_WARNED`/`_REAFFIRM_CAP_WARNED` log-once-per-key WARNING behaviour | `:12685`-`12686`, `:12940`-`12941` | `tests/test_reaffirm_cap_exhaustion_is_silent.py` |
| `load_run_state.py`'s own activation bookkeeping (day_key, activations_today) read/written by this guard | cross-file, not moving | `tests/test_load_run_state.py` |

**This spec does not itself write the characterization-test matrix #1305's own Method
step 1 calls for** (*"every load kind crossed with every guard branch"*) — the existing
test files above give real, if not necessarily exhaustive, coverage of most of that
matrix already, built incrementally across #726/#741/#733/#769/#770/#782/#1140's own
fixes. Confirming the matrix is actually complete (not just "a test file with a
plausible name exists") is implementation-time work, to be done against the tree that
exists once Phase 3/4 have merged — listing it here as fact-checked today would be
exactly the kind of confident-but-unverified claim this project's own CLAUDE.md warns
against making.

## Interfaces

Signature unchanged — a placement move, matching Phases 1–3 and 5's own convention, not
a redesign:

```python
# solver_dispatch/guard.py

def apply_commanded_state_guard(...) -> None: ...  # signature as it exists today,
                                                     # copied verbatim, not re-derived
def dispatch_commanded_state(...) -> ...: ...
def _resolve_reaffirm_after_seconds(...) -> float: ...
_FLOOR_CROSSING_WARNED: set[tuple[str, str]] = set()
_REAFFIRM_CAP_WARNED: set[tuple[str, str]] = set()
```

`_fetch_weather_hourly_forecast` and `ha_call_service_with_response` are **not** part of
this module's interface — reached via `_solver_writer()` (see Façade decision).
`_NATIVE_HASS`, `resolve_controllable_load_power_sensor`, `_resolve_controllable_load_
tuning`, `_resolve_hour_to_period_index`, `_earliest_period_for_same_day_window` are
reached via whatever spec 003 lands as (facade through `solver_writer` for the first
two, a plain `solver_inputs.controllable_loads` import for the other three) — this
spec does not re-specify them; spec 003 already has.

## Façade decision

**Provisional — five of the ten measured names belong to spec 003, not this spec; see
"A real prior finding."** For the five genuinely Phase-6-owned names, measured today:

- `apply_commanded_state_guard`, `dispatch_commanded_state`,
  `_resolve_reaffirm_after_seconds`, `_FLOOR_CROSSING_WARNED`, `_REAFFIRM_CAP_WARNED`:
  **no facade** — zero production callers, zero monkeypatch sites outside
  `solver_writer.py`, for all five. Any touching test file retargeted, same Phase-1
  convention.
- `_fetch_weather_hourly_forecast` and its own blocker `ha_call_service_with_response`:
  **do not move at all.** Both are genuinely shared with code outside this guard
  (`_fetch_weather_hourly_forecast` has a second, non-dispatch caller at
  `solver_writer.py:2482`; `ha_call_service_with_response` is a generic HA-service-call
  helper with monkeypatch sites in calendar/weather-forecast-mirror tests, none of them
  dispatch-related). Moving either into a module named for dispatch-guarding would
  misrepresent what the module is for, and would strand `:2482`'s own caller needing a
  new deferred-import pointed the wrong direction. `apply_commanded_state_guard`
  reaches `_fetch_weather_hourly_forecast` via the same `_solver_writer()` seam
  `save_plan_state` uses for `PLAN_STATE_PATH` in spec 005 — the identical shape,
  applied for the identical reason (a name shared across a module boundary with a
  real caller on the side that isn't moving).

## Invariants

- `apply_commanded_state_guard(**same args)` produces byte-identical
  `dispatch_commanded_state`/`record_activation` call sequences pre- and post-move, for
  every existing regression test and golden scenario.
- Every `_LOGGER` use in the moved code reaches `solver_shared._LOGGER` directly
  (per the same `test_callers_mode_counts_only_real_references.py` guard cited in spec
  005), never `sw._LOGGER`.
- `_fetch_weather_hourly_forecast`'s own non-dispatch caller (`:2482`, or wherever it
  lands once other phases have moved code around it) is untouched by this move — it is
  reached FROM `solver_dispatch/guard.py`, never moved TO it.
- No name spec 003 already claims is re-claimed or re-decided here. If spec 003's own
  eventual implementation differs from what this spec assumes (e.g., a different facade
  choice than currently documented), this spec's own Façade section is re-measured
  against the merged result, not silently patched to match a stale assumption.

## Migration

**Step 0, before any of the below**: confirm Phase 3 (spec 003) and Phase 4 (spec 004)
have both merged, and re-run `tests/analyse_module_dependencies.py --phase 6` (or its
successor once `build_controllable_loads` has moved) against the new `main`. If the
blocker set differs from what's measured above — expected, since five of today's ten
are mid-move elsewhere — revise this spec's Façade/Migration sections before writing
any code, not after.

1. Characterization pass (#1305's own Method step 1): for each load kind
   (sheddable/deferrable/thermal) crossed with each guard branch (activation allowed/
   denied, min-hold, max-activations-per-day, relay-chatter, done-condition, thermal
   hard-deadline, thermal comfort-floor reheat), confirm an existing test captures the
   exact `dispatch_commanded_state`/`record_activation` calls made, or add one that
   does, **before** moving a line. Treat a gap found here as a genuine pre-existing
   coverage hole to fix first, not something to paper over with a new test that merely
   re-asserts current (possibly buggy) behaviour.
2. Add `apply_commanded_state_guard`, `dispatch_commanded_state`,
   `_resolve_reaffirm_after_seconds`, `_FLOOR_CROSSING_WARNED`, `_REAFFIRM_CAP_WARNED`
   to `solver_dispatch/guard.py`, verbatim except `_LOGGER.*` → `solver_shared._LOGGER.*`
   and the `_fetch_weather_hourly_forecast` read going through `_solver_writer()`.
3. Delete the five from `solver_writer.py`; retarget its own single call site (wherever
   `main()` calls `apply_commanded_state_guard` once Phase 4's own tariff-attribution
   block has settled) to `solver_dispatch.guard.apply_commanded_state_guard(...)`.
4. Retarget the three test files touching `apply_commanded_state_guard` and the one
   touching `_REAFFIRM_CAP_WARNED` to the new module.
5. Add `solver_dispatch/guard.py` to `test_diagnostic_log_levels.py`'s
   `_scanned_paths()` if any `#N diag:`-style line lands inside the moved span (check
   at implementation time — none confirmed in this spec's own reading, but the span is
   1,300 lines and this project's own history (#757) says check, don't assume).
6. Full stub suite + real-HA-harness CI green; golden master and golden-nonvacuity
   scenarios byte-identical; mypy delta clean.

## Non-goals

- Re-deciding any name spec 003 already owns — this spec cites spec 003's own
  conclusions, it does not re-derive them.
- Fixing or re-investigating any of #726/#741/#733/#769/#770/#782/#1140 — all already
  closed, cited here only as the reason this move gets the highest verification bar in
  the whole initiative.
- Building the full characterization-test matrix inline in this document — Migration
  step 1 is real, required work, deliberately left as implementation-time work against
  a tree that will look different once Phase 3/4 land, not written speculatively now
  against code that's about to move out from under it.
- Any change to the thermal-load hard-deadline-vs-soft-shortfall LP mechanism itself
  (a separate, solver-side concern; this spec only moves the dispatch guard that reads
  the LP's own output).

## Deploy timing

**Devhub-gated, mandatory**, per #1305's own step 5 and the plan's gate table (Phase 6
listed alongside Phase 5 in `docs/architecture/tech-debt-plan.md` §3's "Live | devhub
pass" row). #1305's own wording is stronger than any earlier phase's: *"exercise a real
partial-input path (a real load's config subentry), not just a marker check, since this
function has the worst history of any in the file for 'looked fine, broke in
production.'"* This spec adopts that verbatim as its own bar, not a softened version of
it — a clean restart and an optimal `solve_now` are necessary and nowhere near
sufficient here.

## Acceptance

- [ ] Step 0 (re-measurement against post-Phase-3/4 `main`) completed and this spec's
      Façade/Migration sections updated to match, before implementation starts.
- [ ] Full per-kind, per-branch characterization tests pass byte-for-byte pre/post move
      (Migration step 1).
- [ ] Golden master and golden-nonvacuity scenarios (`tests/golden/scenarios.py`,
      `tests/golden/scenarios_native.py`) identical.
- [ ] Full suite passes; no existing assertion's meaning edited.
- [ ] `test_callers_mode_counts_only_real_references.py` stays green (`_LOGGER` reached
      via `solver_shared`, never `sw.`).
- [ ] mypy count not higher; zero in `solver_dispatch/guard.py`'s own additions.
- [ ] `nimbus-layers`: `solver_dispatch` added as its own layer entry, with exactly the
      `_solver_writer()` exceptions this spec's own Façade section documents (currently
      one: `_fetch_weather_hourly_forecast`) — no undocumented new exception.
- [ ] Devhub deploy + restart + `solve_now` + **a real dispatch exercise against a real
      load's config subentry** + log sweep for new WARNING/ERROR — required before
      production deploy, per #1305's own step 5.
- [ ] No line executed before is unexecuted after.

## Rollback

Revert the single commit. `solver_dispatch/guard.py` is new; `solver_writer.py` regains
its own `apply_commanded_state_guard` and its one call site is pointed back at itself.
No state-file format change — `load_run_state.py`'s own bookkeeping format is untouched
by this move regardless of which side of the revert is active.
