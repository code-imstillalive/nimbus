# Spec 005: `solver_publish.py` — `publish_plan` (Plan → `sensor.nimbus_solver_battery_forecast`)

Status: proposed
Plan: docs/architecture/tech-debt-plan.md; phase: #1304 (Phase 5)
Code cited at: `4bec705`

## Responsibility

Phase 5 (#1304) moves `publish_plan` — the function that turns a solved `Plan`
plus the cycle's own inputs into `sensor.nimbus_solver_battery_forecast`'s
published state and attributes — out of `solver_writer.py` and into
`solver_publish.py`.

Unlike Phase 4, this is a relocation of an **already-standalone, already-named**
function, the same shape as Phases 1–3: `publish_plan` is not inline `main()`
code today. Its own docstring records that it was pulled out of `main()`
once before, under a different initiative — *"Extracted from main() (nimbus
issue #363 step 2, Mark Purcell's own approved staged-extraction plan ...).
Pure move, zero behavior change -- guarded by the #363 step-1 golden-output
test (tests/test_main_golden_output_guardrail.py)"*. Phase 5 is the second
move of the same code, not the first — see "A real prior finding" below for
why that matters to this spec's own risk assessment.

Measured fresh at `4bec705`: `publish_plan` is 1,175 lines,
`solver_writer.py:8653`–`9827`, already-keyword-only, 51 parameters (50
required, `load_forecast_source_decision=None` the sole default, added
last "so every existing caller ... keeps working unchanged"). #1304's own
body cites "1,031 lines, `solver_writer.py:12035`" — a pre-#1298-era
measurement, stale by ~150 lines and ~2,800 line numbers; re-measured here,
not trusted, same discipline spec 004 applied to #1303's own stale figure.

`main()` calls it exactly once, at `solver_writer.py:13934` — the single
production call site this move must retarget.

#1304 calls this "high risk" because *"every sensor's published data flows
through this function."* Checked directly: `publish_plan` itself issues
exactly **one** `ha_post_state()` call (`:9418`, to `ENTITY_ID` =
`sensor.nimbus_solver_battery_forecast`) — the "every sensor" framing is
about blast radius of that one entity (this repo's own "flagship diagnostic
sensor ... most likely to be built against," per its own comment at
`:9424`), not about the function touching many entities directly. The risk
is real (one large, dense payload assembled from thirteen helper
functions), but the *mechanical* shape of the move — one function, no
inline-code boundary problem like Phase 4's — is closer to Phases 1–3 than
to Phase 4.

## Prior art

Not applicable — pure internal module organisation, same as specs 000–004.
No new mechanism, no new solver/scoring/load-model concept; `publish_plan`'s
own construction (cost breakdown, binding-constraint label, per-battery
forecast, flow decomposition) was each separately justified when originally
written, cited in-line at their own definitions.

## A real prior finding this spec must not overlook

Two things, found by reading rather than assumed from #1304's own text:

**1. `solver_publish.py` already exists.** #1304 frames the destination as a
new module. It is not: created by `642a342` ("Hoist the household-load-total
publish into solver_publish.py (#735) (#888)"), currently 318 lines with two
functions (`publish_household_load_total_forecast`,
`publish_flex_telemetry_record`), both reached via `_solver_writer()`
deferred imports for their own solver_writer-only dependencies. Its own
module docstring states a design position directly relevant to this spec's
own interface: *"every blocker found on #735 so far ... was invisible from
reading the code being moved. The list is exactly what the call reads, no
more: passing a bag would hide which of them this publish actually depends
on."* `publish_plan`'s own 51-keyword-only-argument signature already
follows the identical convention, independently arrived at under #363 —
this spec's job is placement into an existing, already-justified pattern,
not designing a new one.

**2. #1304's own step 1 ("full sensor-attribute snapshot test ... before any
move") describes work already done, for the *previous* move.**
`publish_plan`'s docstring names the exact guard:
`tests/test_main_golden_output_guardrail.py`, written for #363's own
step-1/step-2 split and confirmed still present and passing at `4bec705`
(see Current behaviour, below) — extended since by the golden-master harness
(spec 000, `tests/test_golden_master.py`) driving the same code path across
real recorded market scenarios. This spec's job is confirming that existing
coverage still reaches every line of the code being moved *now*, not
building new snapshot machinery from nothing.

## Verification method: identity, not just behavioural equivalence

PR #1380's own review of spec 004 makes a distinction worth applying here
before it has to be raised a second time: *"Phases 1–3 relocate **existing
named functions**, which is why Phase 2 could be *proved* rather than
evidenced: strip the inserted `sw.` prefixes and the bytes match; after
`ruff format`, `ast.dump()` matches. ... Phase 4 has no pre-existing function
to compare against ... Phase 4's safety rests entirely on behavioural
equivalence via the golden master."*

Phase 5 is on the Phase 1–3 side of that line, not Phase 4's — `publish_plan`
and its thirteen helpers are already-named, already-signatured functions
(none of this spec's own doing; #363 did that). So the stronger, Phase-2-
style proof is available and is the bar this spec holds itself to, not the
weaker one: for every moved function except `save_plan_state`, the body
moves with a single mechanical substitution (`_LOGGER.` →
`solver_shared._LOGGER.`, matching the fix PR #1380's review already
required for spec 004 and spec 003's own `_LOGGER` point on #1370 — done
here from the start, not corrected after review); strip that one
substitution back out and `ast.dump(..., include_attributes=False)` must
match the pre-move function exactly. `save_plan_state` gets the same
treatment with one additional substitution (`PLAN_STATE_PATH` →
`sw.PLAN_STATE_PATH`, `sw = _solver_writer()` added), the same "four-line
non-identity, documented at the site" shape spec 002 accepted for
`_carry_forward_quality_history`.

The golden master and `test_main_golden_output_guardrail.py` remain required
(Acceptance, below) as corroboration on real inputs — the same relationship
spec 002 describes ("a golden payload test evidences that behaviour did not
change on the inputs it covers; AST identity proves the code is the same
code, on all inputs") — not as the only proof, the way they necessarily are
for Phase 4.

## Measured cost

`python tests/analyse_module_dependencies.py --phase 5` (`solver_writer.py`
at `4bec705`):

```
### publish_plan  (1175 lines)
    importable:              7
    module-level (blockers): 13
      ENTITY_ID, _compute_flow_economics, _dispatch_source_breakdown, _flow_decomposition, _load_forecast_source_attributes, _risk_aversion_effect_now, build_per_battery_forecast, compute_binding_constraint_label, compute_cost_band, compute_cost_breakdown, periods_within_hours, resolve_fixed_export_charge_clamp, save_plan_state

lines in scope:      1175
distinct blockers:   13
```

Per-blocker sizes (`--phase 5`'s own breakdown):

| Name | Lines | Role |
|---|---|---|
| `ENTITY_ID` | 1 (constant) | the published entity_id |
| `_risk_aversion_effect_now` | 49 | risk-aversion display term |
| `resolve_fixed_export_charge_clamp` | 68 | fixed-export-charge diagnostic |
| `compute_binding_constraint_label` | 235 | "what's binding right now" label |
| `compute_cost_breakdown` | 109 | cost-by-category attribute |
| `periods_within_hours` | 11 | small time-window helper |
| `compute_cost_band` | 87 | #630 cost-band diagnostic |
| `save_plan_state` | 38 | persists `PLAN_STATE_PATH` for next cycle |
| `_dispatch_source_breakdown` | 74 | per-source dispatch attribution |
| `_flow_decomposition` | 143 | grid/battery/solar/load flow split |
| `_compute_flow_economics` | 147 | $ value of the flow split |
| `build_per_battery_forecast` | 47 | per-participant forecast rows |
| `_load_forecast_source_attributes` | 26 | load-forecast-source diagnostics |

**This spec measures one level deeper than the preset does, and that found
three more real blockers the single-level scan misses**, because they are
referenced inside the thirteen helpers' own bodies, not by `publish_plan`
directly:

```
$ python tests/analyse_module_dependencies.py --functions "ENTITY_ID,_compute_flow_economics,_dispatch_source_breakdown,_flow_decomposition,_load_forecast_source_attributes,_risk_aversion_effect_now,build_per_battery_forecast,compute_binding_constraint_label,compute_cost_band,compute_cost_breakdown,periods_within_hours,resolve_fixed_export_charge_clamp,save_plan_state"

### _load_forecast_source_attributes  (26 lines)
    module-level (blockers): 1
      _LOAD_FORECAST_SOURCE_KEYS
### compute_binding_constraint_label  (235 lines)
    module-level (blockers): 1
      _PIN_MATCH_TOLERANCE_KW
### save_plan_state  (38 lines)
    module-level (blockers): 1
      PLAN_STATE_PATH
```

If this move had gone by the single-level preset alone, all three would
have been missed until CI (or worse, a real solve) hit a `NameError`. Each
is a private constant used by exactly one of the thirteen helpers — except
`PLAN_STATE_PATH`, which is genuinely shared with `load_previous_plan()` (a
function that stays behind in `solver_writer.py`; not this phase's or any
currently-scoped phase's target — see Non-goals). That distinction drives
the façade decision below.

`--callers` for all 14 names moving as `publish_plan`'s own set (plus the
one production call site inside `main()`, which does not count as
"outside", but is the retarget target in Migration):

```
$ python tests/analyse_module_dependencies.py --functions "publish_plan,ENTITY_ID,_compute_flow_economics,_dispatch_source_breakdown,_flow_decomposition,_load_forecast_source_attributes,_risk_aversion_effect_now,build_per_battery_forecast,compute_binding_constraint_label,compute_cost_band,compute_cost_breakdown,periods_within_hours,resolve_fixed_export_charge_clamp,save_plan_state" --callers

production callers outside the module : 0
monkeypatch sites                     : 0
test files touching any target        : 14

No production caller and no monkeypatch site outside the module.
A compatibility facade here would preserve an interface nothing uses.
```

`--callers` for the three transitively-found constants (run separately,
since they weren't in the preset's own list):

- `_PIN_MATCH_TOLERANCE_KW`: 1 test file, read-only (`assertLessEqual`/
  `assertGreater` on the value) — no monkeypatch.
- `_LOAD_FORECAST_SOURCE_KEYS`: 1 test file, read-only (`getattr` via a
  `_const()` helper) — no monkeypatch.
- `PLAN_STATE_PATH`: **2 real monkeypatch sites**,
  `tests/test_solver_writer_load_previous_plan.py:86,105,112` —
  `patch.object(solver_writer, "PLAN_STATE_PATH", ...)`, exercising *both*
  `load_previous_plan()` (staying in `solver_writer.py`) and
  `save_plan_state()` (moving here). This is the one real exception to the
  otherwise-clean "no facade" result.

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| `sensor.nimbus_solver_battery_forecast` state (net battery kW) + full attributes dict | `solver_writer.py:9418` (`ha_post_state(ENTITY_ID, ...)`) | `tests/test_main_golden_output_guardrail.py` — byte-exact on every non-time-dependent key, per-period `forecast[]` sampled at indices `[0, 1, -1]`, `batteries[]` checked structurally; plus the golden master (`tests/test_golden_master.py`, spec 000) across real recorded scenarios |
| `PLAN_STATE_PATH` written (`plan_state.json` shape: `status`, `period_start`, `period_hours`, `battery_charge_kw`, `battery_discharge_kw`, `grid_import_kw`, `grid_export_kw`, `battery_name`) | `solver_writer.py:5530` inside `save_plan_state` | `tests/test_solver_writer_load_previous_plan.py` (round-trips through `load_previous_plan()`); `tests/test_1330_main_driving_tests_isolate_state_paths.py` (guards every `main()`-driving test isolates this path) |
| `"Nimbus #757 diag: about to ha_post_state(...)"` at DEBUG (`:9413`) | inside `publish_plan` | `tests/test_diagnostic_log_levels.py` — scans a fixed file list (`_scanned_paths()`) for the `#N diag:` convention and asserts DEBUG, not WARNING; **must add `solver_publish.py` to that list** (see Migration) |
| `save_plan_state`'s own best-effort `OSError` swallow + `_LOGGER.warning` | `solver_writer.py:5553` | implicit in `test_solver_writer_load_previous_plan.py`'s own coverage; no behavior change |

Golden-scenario coverage: `publish_plan` sits on every successful-solve path
through `main()` (confirmed for Phase 4's own span — no early return between
`main()`'s start and the `build_plan()` call; the same holds from there
through to `publish_plan()`'s own call at `:13934`, since the only code
between them is the tariff-attribution block Phase 4's spec explicitly
excluded, which reads `plan` but does not return early), so every golden
scenario that reaches an optimal solve exercises the whole moved span.

## Interfaces

Signature is **unchanged** — this is a placement move, not a redesign; per
the module's own stated philosophy (see "A real prior finding" above),
inventing a bundling object here would be exactly the mistake that
docstring warns against.

```python
# solver_publish.py

def publish_plan(
    *,
    cfg, now, plan, previous_plan, solve_started,
    period_hours_arr, grid_times, n_periods,
    capacity_kwh, fleet_capacity_kwh, battery_capacity_by_name,
    charge_discharge_efficiency, grid,
    import_limit_kw, export_limit_kw,
    static_import_limit_kw, static_export_limit_kw,
    max_charge_kw, max_discharge_kw,
    charge_cost, discharge_cost_arr, salvage_value,
    risk_aversion, import_price_risk_aversion, export_price_risk_aversion,
    import_price, export_price,
    spot_import_source, spot_export_source,
    import_price_source, export_price_source,
    export_bonus_price,
    load_kw, solar_kw, load_lower_kw, load_upper_kw,
    initial_soc_kwh, match_fraction,
    summed_18_now_kw, whole_house_now_kw, live_load_kw,
    load_forecast_coverage_hours, load_forecast_error,
    load_forecast_source_used, load_forecast_warnings,
    failed_load_entities, n_clamped, solar_delivery,
    p2p_recent_volume_kwh, price_spike_active,
    load_forecast_source_decision=None,
) -> None: ...
```

Plus the thirteen helpers and `ENTITY_ID`, moving as a closed set (each
keeps its own existing signature, zero change):
`_risk_aversion_effect_now`, `resolve_fixed_export_charge_clamp`,
`compute_binding_constraint_label`, `compute_cost_breakdown`,
`periods_within_hours`, `compute_cost_band`, `save_plan_state`,
`_dispatch_source_breakdown`, `_flow_decomposition`,
`_compute_flow_economics`, `build_per_battery_forecast`,
`_load_forecast_source_attributes`, plus the two private constants
`_PIN_MATCH_TOLERANCE_KW` and `_LOAD_FORECAST_SOURCE_KEYS` (each moves with
its one caller). `network`, `elements`, `np`, `json`, `_LOGGER` reach
`solver_publish.py` the same way they already reach every other
`solver_inputs/*.py` module — ordinary top-level imports (`from .solver
import elements, network`, `import numpy as np`, `import json`,
`from . import solver_shared` for `_LOGGER`), not `_solver_writer()`. This
module already imports `solver_shared` for its own `publish_flex_telemetry_
record`; nothing new in kind, only in which names are pulled from it.
**Every moved `_LOGGER.*` call becomes `solver_shared._LOGGER.*`, never
`sw._LOGGER`** — `tests/test_callers_mode_counts_only_real_references.py::
test_the_real_tree_now_finds_zero_logger_callers` asserts zero real
`sw._LOGGER`-shaped references anywhere outside `solver_writer.py` (Phase
2a's own success condition), and PR #1380's review already caught this
exact gap on spec 004 (and #1370 on spec 003) — done correctly here from
the start rather than fixed after review.

**One exception, not a plain import**: `save_plan_state` reads
`PLAN_STATE_PATH` via the existing `_solver_writer()` deferred-import seam
already defined at the top of this module (`sw = _solver_writer(); ...
sw.PLAN_STATE_PATH`), the same pattern its two module-mates already use for
their own solver_writer-owned dependencies. See Façade decision for why
this one name can't be a plain import.

## Façade decision

**No re-export kept in `solver_writer.py`** for `publish_plan` or twelve of
its thirteen helpers, plus `ENTITY_ID`: measured zero production callers and
zero monkeypatch sites outside the module for all of them (see Measured
cost). Per the plan's own façade criterion, restated in specs 001 and 004:
a re-export here would preserve an interface nothing resolves dynamically.

This is a deliberate departure from #1304's own step 4 ("`solver_writer.py`
re-exports the name") — worth saying plainly rather than silently
overriding: that text predates the `--callers` tooling this initiative later
built, and reads as the same conservative default #1304 uses everywhere
("high risk" throughout), not a measured requirement. Phase 1 already
established that the measured criterion, not a per-issue default, is what
decides a façade in this plan (spec 001: "no re-export... all calling test
files updated" for exactly this same zero-caller shape). Phase 5 follows
that precedent.

**`PLAN_STATE_PATH` is the one name that does not move.** It stays defined
in `solver_writer.py` (its primary reader, `load_previous_plan()`, stays
there too — not this phase's target, see Non-goals), and
`save_plan_state()` reaches it through `_solver_writer()` at call time, so
`patch.object(solver_writer, "PLAN_STATE_PATH", ...)` — the real monkeypatch
`test_solver_writer_load_previous_plan.py` already uses against **both**
functions — keeps resolving correctly for the one that moved. A plain
`from .solver_writer import PLAN_STATE_PATH` would silently break that test
the moment it runs after the patch (name bound once at import time; #861's
exact failure mode, cited in this module's own docstring).

`_PIN_MATCH_TOLERANCE_KW` and `_LOAD_FORECAST_SOURCE_KEYS` move cleanly with
their one caller each — no other reader anywhere in `solver_writer.py`, no
monkeypatch, only a read-only test assertion each, retargeted in Migration.

Fourteen test files reach one or more of the fourteen moving names via
`import solver_writer; solver_writer.<name>(...)` and need that one line
retargeted to `solver_publish` (mechanical, same shape as Phase 1's own
test-file updates):

```
tests/regression/test_flow_invariants.py
tests/test_cost_band.py
tests/test_cost_breakdown_reconciliation.py
tests/test_dispatch_source_breakdown.py
tests/test_flow_decomposition.py
tests/test_main_golden_output_guardrail.py
tests/test_main_p2p_pin_binding_label.py
tests/test_main_report_failures_never_break_the_solve.py
tests/test_solver_writer_binding_constraint_label.py
tests/test_solver_writer_fixed_export_charge_clamp.py
tests/test_solver_writer_load_previous_plan.py
tests/test_solver_writer_per_battery_forecast.py
tests/test_solver_writer_risk_aversion_effect.py
tests/test_whole_house_live_load_diagnostic.py
```

(`tests/test_1179_solve_failure_is_self_diagnosing.py`,
`tests/test_1330_main_driving_tests_isolate_state_paths.py`,
`tests/test_937_value_add_retention_and_wiring.py`,
`tests/test_docs_writer_function_set_drift.py`,
`tests/test_failed_solve_not_published.py`,
`tests/test_forecast_sensor_lts_unit_remediation.py`,
`tests/test_solver_battery_forecast_signal_role.py`,
`tests/test_solver_price_risk_aversion.py` reference these names in
docstrings/comments or via a helper, not a direct `solver_writer.<name>`
call — checked individually, no import-line change needed, but re-verify at
implementation time since a docstring reference today can be a real call
site by then.)

`test_main_golden_output_guardrail.py` itself is the one file in that list
needing extra care: it patches `ha_get`/reads `solver_writer.ENTITY_ID` and
drives `solver_writer.main()` — `main()` stays in `solver_writer.py` and
calls `solver_publish.publish_plan(...)` internally after this move, so the
test's own entry point is unchanged; only its own direct
`solver_writer.ENTITY_ID` reference (if any — verify at implementation
time) needs retargeting to `solver_publish.ENTITY_ID`.

## Invariants

- `publish_plan(**same kwargs)` produces a byte-identical `(state, attrs)`
  tuple pre- and post-move, for `test_main_golden_output_guardrail.py`'s
  fixture and every golden-master scenario.
- `solver_writer.main()`'s single call site becomes
  `solver_publish.publish_plan(...)` (imported the same way `main()` already
  imports its other `solver_inputs`/`solver_plan` siblings — a plain
  top-level dual-mode `try/except` import, not `_solver_writer()`, since the
  call runs forward from `solver_writer.py` into a module that does not
  call back into it for this function).
- `patch.object(solver_writer, "PLAN_STATE_PATH", ...)` continues to affect
  both `load_previous_plan()` (unchanged, still in `solver_writer.py`) and
  `save_plan_state()` (moved, reached via `_solver_writer()`).
- `tests/test_diagnostic_log_levels.py`'s `_scanned_paths()` includes
  `solver_publish.py`, so the `#757 diag` line moving with `publish_plan`
  stays covered (same convention Phases 1–3 already followed for
  `solver_shared.py`/`solver_inputs/*.py`).
- `tests/test_callers_mode_counts_only_real_references.py::test_the_real_
  tree_now_finds_zero_logger_callers` stays green — every `_LOGGER` use in
  the moved code reaches `solver_shared._LOGGER` directly, never `sw._LOGGER`.
- No new `_solver_writer()` deferred import beyond the one already required
  for `PLAN_STATE_PATH` — every other dependency is a plain top-level
  import, confirmed by the zero-monkeypatch measurement above.
- `solver_writer.py`'s over-60-line function count drops by the thirteen
  functions leaving (`publish_plan` itself was already counted once, at
  1,175 lines, against that ratchet — it re-appears in `solver_publish.py`
  post-move, so the *file-level* count for `solver_writer.py` drops while
  the *project-total* count is unchanged, same accounting Phase 4 used).

## Migration

1. Add `publish_plan` and its thirteen helpers (`ENTITY_ID`,
   `_risk_aversion_effect_now`, `resolve_fixed_export_charge_clamp`,
   `compute_binding_constraint_label`, `compute_cost_breakdown`,
   `periods_within_hours`, `compute_cost_band`, `save_plan_state`,
   `_dispatch_source_breakdown`, `_flow_decomposition`,
   `_compute_flow_economics`, `build_per_battery_forecast`,
   `_load_forecast_source_attributes`) plus `_PIN_MATCH_TOLERANCE_KW` and
   `_LOAD_FORECAST_SOURCE_KEYS` to `solver_publish.py`, verbatim except:
   `_LOGGER.*` calls become `solver_shared._LOGGER.*` (matching this
   module's own existing convention in `publish_flex_telemetry_record`);
   `save_plan_state`'s `PLAN_STATE_PATH` read becomes `sw =
   _solver_writer(); ... sw.PLAN_STATE_PATH`; add the plain imports
   (`from .solver import elements, network`, `import numpy as np`,
   `import json`) this module doesn't yet have.
2. Delete the thirteen functions/two constants/`ENTITY_ID`/`publish_plan`
   itself from `solver_writer.py`.
3. Retarget `main()`'s one call site (`:13934`) to
   `solver_publish.publish_plan(...)`, importing `solver_publish` the same
   dual-mode way `solver_inputs`/`solver_plan` are already imported.
4. Retarget the fourteen test files' `solver_writer.<name>` call sites to
   `solver_publish.<name>` (list above); re-check the eight docstring/
   comment-only files at implementation time in case any has since grown a
   real call site.
5. Add `solver_publish.py` to `test_diagnostic_log_levels.py`'s
   `_scanned_paths()`.
6. Run the full stub suite + real-HA-harness CI; confirm
   `test_main_golden_output_guardrail.py` and the golden master
   (`tests/test_golden_master.py`) are byte-identical; confirm
   `test_solver_writer_load_previous_plan.py`'s `save_plan_state`-patching
   cases pass unmodified in their assertions (only the module the call
   targets changes).
7. mypy delta check (fresh `origin/main` worktree diff, same technique as
   #791/#795 and specs 001/004).

## Non-goals

- Moving `load_previous_plan()` — genuinely shares `PLAN_STATE_PATH` with
  `save_plan_state`, but its own read side has no home in this plan's
  current phase list; stays in `solver_writer.py`, reached from
  `solver_publish.py` only in the direction this spec requires (none — the
  dependency runs the other way, `save_plan_state` reading a
  `solver_writer`-owned constant).
- Splitting `publish_plan` itself into smaller functions once moved — it
  will remain a single ~1,150-line function (after the thirteen helpers
  leave, `publish_plan`'s own body, excluding what it calls out to, is
  roughly 250-300 lines of assembly and one `ha_post_state()` call; exact
  figure to be confirmed once the helpers are physically removed).
  Decomposing that further is a future spec's call, matching Phase 4's own
  stance on `assemble_and_solve_plan()`.
- Re-deriving or changing any of the thirteen helpers' own logic, units, or
  the payload's key set — zero behavior change, enforced by the golden
  master and the snapshot guardrail.
- Building new sensor-snapshot test infrastructure — #1304's step 1 is
  already satisfied by `test_main_golden_output_guardrail.py` (written for
  #363, extended by spec 000's golden master); this spec confirms and
  reuses it rather than duplicating it.

## Deploy timing

**Devhub-gated**, per the plan's own gate table (`docs/architecture/
tech-debt-plan.md` §3: "Live | devhub pass | #1298's rule for Phases 5 and
6, unchanged"; §4 sequence table: "Phase 5 (#1304) | publish_plan | Phase 4,
devhub pass"). Unlike specs 001–004, this is not a claim of "nothing for a
live install to exercise differently" — #1304's own reasoning holds: this is
the function behind the sensor a real dashboard is most likely built
against, and a marker check alone (version string, log line count) cannot
confirm the payload a household's own dashboard reads is unchanged. Per the
standing release-validation directive: deploy to devhub, restart, `solve_
now`, confirm `status: optimal`, sweep the log for new WARNING/ERROR, and
specifically diff `sensor.nimbus_solver_battery_forecast`'s own attributes
against a pre-move capture on the same install — not just "the entity has a
value," the same the-marker-confirms-arrival-not-the-feature discipline the
directive's own item 8 states.

## Acceptance

- [ ] Golden master identical for every scenario (`tests/test_golden_master.py`).
- [ ] `test_main_golden_output_guardrail.py` passes unmodified in its
      assertions (only its own `solver_writer.<name>` references, if any,
      retargeted).
- [ ] Full suite passes; the fourteen retargeted test files pass with only
      the import/call-site line changed, no assertion edited.
- [ ] `test_solver_writer_load_previous_plan.py`'s `save_plan_state`
      monkeypatch cases (lines ~86, 105, 112) pass with `patch.object(
      solver_writer, "PLAN_STATE_PATH", ...)` unchanged and the call
      retargeted to `solver_publish.save_plan_state(...)`.
- [ ] `test_diagnostic_log_levels.py` passes with `solver_publish.py` added
      to `_scanned_paths()`.
- [ ] `test_1330_main_driving_tests_isolate_state_paths.py` unaffected
      (`PLAN_STATE_PATH` still resolved through `solver_writer`, still
      isolable the same way).
- [ ] No line executed before is unexecuted after (`coverage_compare.py`
      if available, else a manual before/after line-hit diff on the moved
      span).
- [ ] mypy count not higher; zero in `solver_publish.py`'s new additions.
- [ ] `nimbus-layers`: `solver_publish` import graph unchanged in kind
      (already a valid layer-3-or-equivalent module; confirm no new
      `ignore_imports` exception needed).
- [ ] `size_ratchet.py`: `solver_writer.py`'s own over-60-line count drops
      by the number of moved functions over 60 lines
      (`compute_binding_constraint_label`, `compute_cost_breakdown`,
      `_flow_decomposition`, `_compute_flow_economics`,
      `_dispatch_source_breakdown`, `compute_cost_band`,
      `resolve_fixed_export_charge_clamp`, `publish_plan` itself — 8);
      `solver_publish.py` gains the same 8, net project-wide count
      unchanged, same accounting as Phase 4.
- [ ] `test_docs_writer_function_set_drift.py`: no new native-only helper
      introduced by this move; if the standalone/cron copy of
      `solver_publish.py` needs updating, confirmed byte-parity with the
      packaged copy the same way every other `solver_inputs/*.py` sibling
      already is.
- [ ] Devhub deploy + restart + `solve_now` + log sweep + a direct
      before/after attribute diff on `sensor.nimbus_solver_battery_forecast`
      (per Deploy timing above) — required before this lands in a
      production deploy, per #1304's own step 5 and the plan's gate table.
- [ ] **Identity, not just evidence** (per Verification method above):
      `publish_plan` and twelve of its thirteen helpers are AST-identical to
      their pre-move bodies after stripping the one `_LOGGER` →
      `solver_shared._LOGGER` substitution; `save_plan_state` is AST-identical
      after stripping both that substitution and `PLAN_STATE_PATH` →
      `sw.PLAN_STATE_PATH` (`sw = _solver_writer()` added) — the one
      documented non-identity, same shape as spec 002's
      `_carry_forward_quality_history`.

## Rollback

Revert the single commit. `solver_publish.py` gains 1,175+ lines and loses
nothing it depended on from elsewhere; `solver_writer.py` loses the same
span and regains its single `publish_plan()` call site pointed back at
itself. `PLAN_STATE_PATH`'s own file format and location are untouched by
this spec regardless of which side of the revert is active — no state-file
migration to undo.
