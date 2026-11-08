# Spec 002: `solver_reports/` — the eight Phase-2 reporting functions

Status: implemented (PR #1356)
Plan: docs/architecture/tech-debt-plan.md; phase: #1301 (Phase 2), steps 2b and 2c
Code cited at: `f2bc599` (pre-move) and `91415a8` (post-move)

**Written retrospectively, and that is a defect in the process rather than a
style choice.** The plan's method (section 3, steps 1–2) wants the spec before
any line moves. For these eight functions it did not happen: PR #1356 moved
them in one session, and @purcell-lab's spec 002 (PR #1369) — written in
parallel for two of the eight — was closed unmerged rather than retargeted,
because its two-of-eight shape is not what shipped. He asked for this
retrospective instead, on the grounds that whoever made the decisions should
record them. What follows therefore documents a merged change; every number in
it is measured, not planned.

## Responsibility

Phase 2 (#1301) moves the HA-facing *orchestration and publish* wrapper around
the reporting math out of `solver_writer.py`. It does **not** move the math:
that already lives in the pure, HA-import-free `solver/` package
(`solver/quality_report.py`, `solver/regret.py`, `solver/epr.py`), and this
phase does not touch it. What moved reads recorder history, assembles a payload
and publishes a sensor. Read-only computations; no dispatch surface.

Eight functions, `2,806` lines at `f2bc599`, into four modules:

| function | lines | destination |
|---|---:|---|
| `_compute_report_for_window` | 1,280 | `solver_reports/quality.py` |
| `publish_daily_quality_report` | 393 | `solver_reports/quality.py` |
| `_soc_discrepancy_stats` | 274 | `solver_reports/quality.py` |
| `rescore_quality_history` | 234 | `solver_reports/quality.py` |
| `_carry_forward_quality_history` | 199 | `solver_reports/quality.py` |
| `compute_nimbus_only_soc_counterfactual` | 341 | `solver_reports/counterfactual.py` |
| `compute_efficiency_backtest_report` | 242 | `solver_reports/backtest.py` |
| `_compute_flex_report_for_window` | 152 | `solver_reports/flex.py` |

### Why the quality five could not be split

They are one cluster, not five functions. `rescore_quality_history` calls both
`_compute_report_for_window` and `_carry_forward_quality_history`;
`publish_daily_quality_report` calls the latter; `_compute_report_for_window`
calls `_soc_discrepancy_stats`. Any split leaves a module that cannot import
itself. This is the measured reason PR #1369's planned two-function first step
turned into eight, and it is the one substantive thing this spec knows that
that one did not.

## Measured cost

`tests/analyse_module_dependencies.py --phase 2`, at `f2bc599`:

```
_compute_report_for_window       1280 lines   24 importable   15 blockers
publish_daily_quality_report      393 lines    8 importable   12 blockers
_carry_forward_quality_history    199 lines    2 importable   11 blockers
rescore_quality_history           234 lines    7 importable    4 blockers
compute_nimbus_only_soc_counterfactual 341     17 importable    3 blockers
_compute_flex_report_for_window   152 lines   11 importable    2 blockers
_soc_discrepancy_stats            274 lines    2 importable    1 blocker
compute_efficiency_backtest_report 242 lines  18 importable    0 blockers
```

Step order followed the tool's own conclusion that a low blocker count is the
natural first move: **2b** took backtest (0), flex (2) and counterfactual (3);
**2c** took the five-function cluster (15/12/11/4/1).

`--callers` for all eight, at `91415a8` (post-move):

```
production callers outside the module : 5
monkeypatch sites                     : 11
test files touching any target        : 35

A compatibility facade IS load-bearing for: _compute_report_for_window,
publish_daily_quality_report, rescore_quality_history,
_carry_forward_quality_history, _soc_discrepancy_stats,
compute_nimbus_only_soc_counterfactual.
```

## Current behaviour this must preserve

Every observable output, and how it is pinned. The short answer for all of them
is that **the code is provably identical**, which is a stronger claim than any
per-output test and is why it is stated once here rather than per row.

| Behaviour | Produced at | Pinned by |
|---|---|---|
| `sensor.nimbus_solver_quality_report` state + attributes | `solver_reports/quality.py` (`publish_daily_quality_report`) | byte/AST identity; `tests/test_solver_writer_quality_report_state_class.py`, `tests/test_1248_history_never_shrinks.py` |
| rolling `history` table on that sensor | `solver_reports/quality.py` (`_carry_forward_quality_history`) | `tests/test_quality_history_carry_forward.py`, `tests/test_1248_history_never_shrinks.py` |
| `soc_discrepancy_*` attributes | `solver_reports/quality.py` (`_soc_discrepancy_stats`) | `tests/test_1228_soc_discrepancy_compares_like_for_like.py` |
| `nimbus_load.rescore_history` service outcome | `solver_reports/quality.py` (`rescore_quality_history`) | `tests/test_1120_rescore_history_persists.py` |
| `sensor.nimbus_counterfactual_soc` | `solver_reports/counterfactual.py` | `tests/test_nimbus_only_soc_counterfactual.py` |
| `sensor.nimbus_efficiency_backtest` | `solver_reports/backtest.py` | `tests/test_quality_report_soc_clamp.py` |
| `sensor.nimbus_flex_report` | `solver_reports/flex.py` | `tests/test_daily_flex_report.py` |
| `main()`'s full posted attribute set | unchanged | `tests/test_main_golden_output_guardrail.py`, `tests/test_golden_master.py` (14 scenarios) |

### The verification actually used, and why it replaced the golden-output step

#1301's method asks for a golden-output test per report before the move. For a
**pure relocation** a strictly stronger check exists, and it is the technique
#1019 used for its 839-line re-indent:

1. **Byte-for-byte.** Strip every inserted `sw.` prefix from the moved source;
   the result reproduced the original lines exactly, for all eight functions.
2. **AST-identical.** `ruff format` then re-wrapped lines the longer prefixes
   pushed over the limit, so byte identity no longer holds by construction.
   Equivalence was re-established one level up: strip the prefixes, drop the one
   added `sw = _solver_writer()` binding, and `ast.dump(..., include_attributes=False)`
   matches the pre-move function exactly.

A golden payload test evidences that behaviour did not change on the inputs it
covers. AST identity proves the code is the same code, on all inputs. The
golden master (14 scenarios) and `test_main_golden_output_guardrail` both ran
green as well, unchanged.

**One function is deliberately NOT identical**, and it is the only one:
`_carry_forward_quality_history` differs by four lines, all of them the access
path of `_LAST_KNOWN_QUALITY_HISTORY` — see Invariants.

## Interfaces

No new protocols. Each module exposes exactly the functions it received, with
signatures unchanged, plus one private accessor:

```python
# solver_reports/{quality,backtest,counterfactual,flex}.py
def _solver_writer(): ...          # the solver_writer MODULE, imported late
```

### Divergence from the template, stated rather than glossed

The template says *"Dependencies arrive as parameters or protocols, never
through `_solver_writer()`"*, and its Acceptance list includes *"No
`_solver_writer()` late import added"*. **This spec's modules use that accessor
throughout, and so does every other extracted module in the repo — including
spec 001's own `solver_shared.py`.**

That is not laziness, and `solver_inputs/__init__.py` documents why at length:
the suite patches shared helpers as attributes on the `solver_writer` module
object, so `sw.helper(...)` resolves at call time and those patches keep
working, while a module-scope `from ..solver_writer import helper` binds at
import time and silently defeats every one of them. Measured for this phase:
**12** test files patch `fetch_entity_history_range` that way, **6** patch
`_LOGGER`, **1** patches `_kw_scale_factor`.

So the template's rule is currently unmet by the whole codebase, not just here.
Either it should be rewritten to describe the seam the repo actually relies on,
or a phase should be scoped to remove the seam first. Raised as a question for
@purcell-lab rather than silently ignored; this spec does not claim that
acceptance item.

## Façade decision

**All eight names re-exported from `solver_writer.py`.** The tool finds a
facade load-bearing for six of them (a production caller, a monkeypatch site,
or both); the remaining two — `compute_efficiency_backtest_report` and
`_compute_flex_report_for_window` — are re-exported anyway, because 153 test
files and 4 production modules import `solver_writer` directly and #1298 made
the facade non-negotiable for exactly that reason.

No patch path was retargeted. That is the point: every
`patch.object(solver_writer, "<name>", ...)` in the suite resolves the identical
object it did before.

Constants moved only where the move was provably safe, checked per constant:

| constant | moved with | why safe |
|---|---|---|
| `FLEX_SIGNALS_ENTITY_ID`, `_PRICE_BAND_WIDTH` | `flex.py` | only caller was the moved function; no test patches either |
| `_SOC_BOUNDARY_EDGE_TOLERANCE_PCT` | `quality.py` | same |
| `PRIOR_READ_OK`, `PRIOR_READ_ABSENT`, `PRIOR_READ_UNAVAILABLE`, `PRIOR_READ_UNREACHABLE`, `_PRIOR_READ_DEGRADED` | `quality.py` | **had to** move — see Invariants |

## Invariants

Each is checked by a test, and each came from a real failure rather than
foresight. `tests/test_solver_reports_extraction.py` holds the first four.

1. **`solver_writer.X` IS the moved object**, not a copy — identity, for all
   eight names plus the five constants.
2. **A patch on the `solver_writer` module object reaches moved code.** Real
   `patch.object` through each module's accessor.
3. **No module-scope `from ..solver_writer import`** anywhere in the package
   (AST-level guard, because the runtime check above only covers the helper it
   names).
4. **Every moved function binds `sw = _solver_writer()`.** The first cut
   qualified all names and omitted the binding: 24 test failures, ~92 ruff
   F821.
5. **No `global` statement in the package.** `_carry_forward_quality_history`
   carried `global _LAST_KNOWN_QUALITY_HISTORY`. A `global` binds in the
   namespace of the module the function is *defined* in, so relocating the
   function relocated the variable it writes, while every reader
   (`solver_writer.set_native_hass()`, `tests/golden/harness.py`,
   `test_1248_history_never_shrinks.py`) kept reading `solver_writer`'s — 5
   failures, all shaped `0 != 2`. **A facade alias cannot repair this**: an
   alias captures the current object, and this name is *rebound*. The variable
   stays in `solver_writer` (it has a real rebinding caller there) and is
   reached as `sw.X = ...`, which is exactly what `global X; X = ...` did. This
   is the four-line non-identity noted above.
6. **No default argument reaches through the accessor.**
   `prior_read: str = PRIOR_READ_OK` is evaluated at function-definition time,
   before `sw` is bound — `sw.PRIOR_READ_OK` there is a plain `NameError`. That
   is why the five `PRIOR_READ_*` constants had to move rather than be reached
   via `sw.`, and it is semantically exact: the original evaluated a
   module-level constant once at import, and patching
   `solver_writer.PRIOR_READ_OK` never affected this default either way.
7. **A name is safe to import directly only if nothing patches it on the
   `solver_writer` module object** — not because it comes from a pure package.
   `compute_quality_report` comes from `solver/` and looked safe;
   `test_battery_power_sign_convention.py` patches it on `solver_writer` to spy
   on the real per-battery arrays, so a direct import made the spy invisible
   (four failures, `KeyError: 'charge'`). Re-measured for the whole direct set:
   `compute_quality_report` 1 file, `elements`/`lp`/`network`/`np`/`json`/
   `urllib`/`datetime`/`timedelta` 0.

## Migration

What actually happened, in order:

1. **2b** — `backtest.py`, `counterfactual.py`, `flex.py` created; three
   functions moved; facade aliases added; byte-equivalence verified.
2. `ruff format` applied; equivalence re-established at AST level.
3. **2c** — `quality.py` created; the five-function cluster moved together;
   `PRIOR_READ_*` moved with it; the `global` converted to `sw.X = ...`.
4. Project gates updated for the new package: size ratchet baseline 61 → 53,
   `nimbus-layers` `ignore_imports` + `_EXPECTED_IGNORED_IMPORT_COUNT`,
   `test_docs_writer_function_set_drift`'s integration-side union.
5. Relocation-broken tests repaired (see Non-goals).

Steps 1–3 shipped in one PR rather than separately, which is the process
deviation this spec opens by naming.

## Non-goals

- **The reporting math.** `solver/quality_report.py`, `solver/regret.py`,
  `solver/epr.py` untouched.
- **`compute_daily_quality_report`, `compute_daily_flex_report`,
  `publish_efficiency_backtest_report`, `publish_nimbus_only_soc_counterfactual`**
  stay in `solver_writer.py`; only their private bodies moved.
- **The private helpers the moved functions depend on** — `_epr_reliability`,
  `_achieved_feasibility_stats`, `_history_coverage_by_series`,
  `battery_energy_balance`, `_regret_path_delta_share` and the rest. Several
  have other callers, and moving a helper moves its internal callers too, which
  is the seam hazard above.

### Defects and gaps found on the way

- **32 test failures across 7 files, none a real regression** — tests asserting
  which *file* code lives in. Five shapes: text search of one file;
  `ast.parse` of one file; AST matchers requiring a bare `ast.Name` where
  `sw.x` is an `ast.Attribute`; substring slices raising `ValueError`;
  hand-rolled two-file unions from Phase 2a. `tests/_writer_source.py` now
  provides `writer_source()`, `writer_trees()`, `find_function()`,
  `function_source()`, `find_constant()` over the union, so Phases 3–7 should
  hit far fewer. Note `function_source()` resolves a node with *its own*
  module's text: `ast.get_source_segment()` pairing a `quality.py` node with
  `solver_writer.py`'s text returns the wrong lines **silently**.
- **#1354** — the coverage-compare gate reported PASS having measured 0 lines
  (filed and fixed).
- **#1355** — `sensor.nimbus_solver_quality_report` has no recorded state
  transitions for days whose values were observed live, and an aged-out day is
  then unrecoverable. Not caused by this phase; found while reading the same
  sensor. @purcell-lab's **#1373** reports the same sensor `unavailable` at
  local midnight on his install, which may be the same mechanism.

## Acceptance

- [x] **Golden master identical for every scenario** — `tests/test_golden_master.py`, 14 scenarios, and `tests/test_main_golden_output_guardrail.py`.
- [x] **Full suite passes** in CI (8/8 checks on PR #1356). No existing assertion's *meaning* edited; the relocation-broken assertions listed above were widened from "this file" to "the writer's modules", each with the reason at the site. New tests: `tests/test_solver_reports_extraction.py`.
- [x] **No monkeypatch target became a no-op** — Invariants 2 and 3, plus the `compute_quality_report` finding (7), which is an instance of this check *catching* one.
- [x] **No line executed before is unexecuted after** — implied by AST identity for seven functions; the eighth differs only in one variable's access path.
- [x] **mypy count not higher** — 249 errors / 22 files on both sides, measured from a detached worktree at the merge base; this branch checks 4 more source files and adds none.
- [x] **Import contracts** — `nimbus-layers` KEPT, 13 ignored imports (8 + 1 for #768 + 4 for this package), 0 broken. Moved code sits in the `solver_inputs | (solver_reports)` layer.
- [x] **No new function over 60 lines; count not higher** — 61 → **53**, banked in `DEFAULT_BASELINE_COUNT` and the four assertions pinning it.
- [ ] **No `_solver_writer()` late import added** — **NOT met, deliberately.** Four added, one per module. See the template divergence above; this item is currently unmeetable by any extraction in this repo.
- [x] Spec-specific: byte-for-byte equivalence pre-format for all eight; AST identity post-format for seven, with the eighth's four-line difference documented at the site.

## Rollback

Revert `e3a3c2c` (the squash merge of PR #1356). No state file format changed
and no new state file was introduced, so nothing written by this code needs
attention on a revert. The project-gate numbers must go back with it — size
ratchet to 61, `_EXPECTED_IGNORED_IMPORT_COUNT` to 9, and the four
`solver_reports` `ignore_imports` entries removed — or the gates will fail on a
tree that no longer contains the package.
