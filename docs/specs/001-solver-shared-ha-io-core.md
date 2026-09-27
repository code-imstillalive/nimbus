# Spec 001: solver_shared.py — the HA-I/O and config-resolution core

Status: proposed
Plan: docs/architecture/tech-debt-plan.md; phase: #1301 (Phase 2), prerequisite step "2a"
Code cited at: `7a3187e`

## Responsibility

Phase 2 (#1301) needs to move eight reporting functions out of `solver_writer.py`
into `solver_reports/{quality,backtest,counterfactual,flex}.py`. Measured by
`tests/analyse_module_dependencies.py --phase 2` (below): all eight depend on a
shared set of module-level names still living in `solver_writer.py` — none of
which are reporting logic. They are recorder access (`ha_get`, `ha_post_state`,
`fetch_entity_history_range`, `fetch_entity_attribute_history_range`,
`resample_history_nearest`, `resample_history_mean`), config resolution
(`_cfg_num`, `_cfg_int`, `resolve_effective_capacity_kwh`, `import_fee_rate`,
`fetch_p2p_fixed_export_kw`, `p2p_bonus_price_by_period`), and small utilities
(`_LOGGER`, `LOCAL_TZ`, `_local`, `_kw_scale_factor`, `_version_stamp`,
`TIER1_PERIOD_HOURS`, `TIER2_PERIOD_HOURS`, `MAX_TIER1_HOURS`). This spec gives
them a home below `solver_reports/` in the plan's own layer map (section 2), so
Phase 2's own extraction has something to import from instead of reaching back
into `solver_writer.py` — the exact "dependency pointing the wrong way" pattern
the plan names as the reason Phase 1's own `_solver_writer()` seams aren't a
finished migration.

This spec does not move the eight reporting functions themselves — that is
Phase 2 proper (2b–2e), against this module once it exists.

## Measured cost

`python tests/analyse_module_dependencies.py --phase 2` (real, current `main`):

```
_compute_report_for_window          1280 lines   31 blockers
publish_daily_quality_report          393 lines   16 blockers
rescore_quality_history                234 lines    9 blockers
_carry_forward_quality_history        199 lines   13 blockers
_soc_discrepancy_stats                274 lines    2 blockers
compute_nimbus_only_soc_counterfactual 341 lines  14 blockers
compute_efficiency_backtest_report    242 lines   11 blockers
_compute_flex_report_for_window       152 lines   10 blockers
```

Shared blockers this spec moves, by how many Phase-2 targets need them (the
quality-history-specific blockers — `PRIOR_READ_*`, `QUALITY_ENTITY_ID`, the
`_QUALITY_HISTORY_*`/`_epr_reliability_code` family — are NOT in this list;
they are reporting state, not I/O, and stay with `_carry_forward_quality_history`
in Phase 2 proper):

```
6x  _LOGGER
5x  resample_history_nearest        [56 lines]
4x  LOCAL_TZ
4x  _cfg_num                        [24 lines]
4x  fetch_entity_history_range      [81 lines]
3x  TIER2_PERIOD_HOURS
3x  _kw_scale_factor                [26 lines]
3x  _local                          [35 lines]
3x  ha_get                          [23 lines]
3x  import_fee_rate                 [35 lines]
3x  resolve_effective_capacity_kwh  [77 lines]
2x  MAX_TIER1_HOURS
2x  TIER1_PERIOD_HOURS
2x  fetch_p2p_fixed_export_kw       [89 lines]
2x  ha_post_state                   [109 lines]
2x  p2p_bonus_price_by_period       [46 lines]
2x  resample_history_mean           [107 lines]
1x  fetch_entity_attribute_history_range (compute_flex_report only)
1x  _cfg_int                        (compute_nimbus_only_soc_counterfactual only)
1x  _version_stamp                  (rescore_quality_history only; also used
                                      by publish_daily_quality_report)
```

`python tests/analyse_module_dependencies.py --functions <names above> --callers`
(real, current `main` — full per-name test-file lists omitted here for length,
see the command's own output):

```
name                                  prod callers   monkeypatch sites
_LOGGER                                6 [1]          6
resample_history_nearest               1              0
resample_history_mean                  1              2
LOCAL_TZ                               0               (referenced, never patched)
_cfg_num                               4 (+1 standalone/cron)   0
_cfg_int                               0 (+1 standalone/cron)   0
fetch_entity_history_range             2              24
fetch_entity_attribute_history_range   0              1
_kw_scale_factor                       1              1
_local                                 3 (+1 standalone/cron)   0
ha_get                                 3 (+3 standalone/cron)   48
ha_post_state                          2 (+2 standalone/cron)   29+
import_fee_rate                        not yet re-measured individually — treat as facade-required, same as its siblings
resolve_effective_capacity_kwh         not yet re-measured individually — treat as facade-required
fetch_p2p_fixed_export_kw              not yet re-measured individually — treat as facade-required
p2p_bonus_price_by_period              not yet re-measured individually — treat as facade-required
_version_stamp                         not yet re-measured individually — treat as facade-required
```

`ha_get` and `ha_post_state` alone carry 48 and 29+ monkeypatch sites. This is
the single fact that decides this spec's façade decision below — see that
section before reading Migration.

[1] `_LOGGER`'s production-caller count was originally measured at 19 by
`tests/analyse_module_dependencies.py --callers`. That figure was wrong: the
tool counted a bare name reference even when a file defines its own
`_LOGGER = logging.getLogger(__name__)` and depends on `solver_writer` for
nothing. Fixed in #1348 (merged), which narrowed a reference to attribute
access through a known module alias (`sw._LOGGER`) or an explicit
`from ... import`. The corrected figure is **6**, and — found while
verifying it, not assumed — those six are, name for name, six of the seven
layer violations #1338's `nimbus-layers` contract already records as
`ignore_imports` exceptions: `solver_inputs/battery_participants.py`,
`battery_soc.py`, `extra_batteries.py`, `load.py`, `prices.py`, `solar.py`.
This is a *stronger* case for the façade than the original 19 suggested —
`_LOGGER`'s real dependency is six specific, already-tracked architectural
violations reaching up for it, not diffuse usage, and Phase 2a gives those
six modules something *below* them to import instead. See the new
acceptance item below: this is checkable, not just argued.

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| Every HA state read the writer makes | `ha_get` (`solver_writer.py:2276`) | `tests/golden/fake_ha.py`'s own `GET /api/states/<id>` handling — every golden scenario exercises this via `main()`'s own price/load/entity reads |
| Every HA state posted | `ha_post_state` (`:2450`) | golden master `cycles[].posted`, all 12 scenarios |
| Recorder history reads | `fetch_entity_history_range`/`fetch_entity_attribute_history_range` (`:6211`/`:6499`) | golden master `cycles[].requests`; `tests/test_fetch_price_history_and_entity_history_range.py` |
| Nearest/mean resampling arithmetic | `resample_history_nearest`/`resample_history_mean` (`:6586`/`:6644`) | `tests/test_solver_writer_battery_participant_history.py` and the 5+2 test files listed above |
| Config-value resolution with defaults | `_cfg_num`/`_cfg_int` (`:803`/`:829`) | every test constructing a `cfg` dict and reading a solver number/int back |
| Effective capacity (SoH-derated) | `resolve_effective_capacity_kwh` (`:889`) | `tests/test_effective_capacity_soh_derating.py` |
| Import fee $/kWh by hour | `import_fee_rate` (`:1684`) | referenced throughout the quality-report test suite |
| P2P fixed-export kWh / bonus price by period | `fetch_p2p_fixed_export_kw`/`p2p_bonus_price_by_period` (`:4565`/`:4656`) | `tests/test_p2p_bonus_is_gated_to_committed_periods.py`, `tests/test_p2p_bonus_pricing_reaches_the_oracle.py` |
| Unit-scale factor for a W-vs-kW sensor | `_kw_scale_factor` (`:6753`) | `tests/test_kw_scale_factor.py` |
| `nimbus_version`/schema-version stamp | `_version_stamp` (`:9840`) | `tests/test_1292_rescore_history_version_stamp_none_guard.py` |
| Local-timezone conversion | `_local` (`:323`), `LOCAL_TZ` (`:320`) | every test asserting a local-hour boundary (#1290-class) |
| Period-grid tier constants | `TIER1_PERIOD_HOURS`/`TIER2_PERIOD_HOURS`/`MAX_TIER1_HOURS` (`:556`/`:557`/`:569`) | `tests/test_compute_report_for_window.py` and siblings |
| Diagnostic logging | `_LOGGER` (`:2032`) | `tests/test_diagnostic_log_levels.py`'s own `_scanned_paths()`/`_SW_LOGGER_CALL_RE` scan, which already globs `solver_inputs/*.py` too (per #1298 Phase 1) — must be extended to `solver_shared.py` |

All 12 golden-master scenarios reach `ha_get`, `ha_post_state`, and the
price/history-fetch helpers through `main()`'s own top-level flow (fetching
prices, posting the plan) — this is the highest-coverage code in the whole
module (see spec 000's own table: `main` at 94.4%). No new golden scenario is
needed for this spec; the existing 12 already exercise every name being moved.

## Interfaces

Plain functions and constants, no new abstraction — the plan's own Dependency
Inversion principle (section 2) applies to phases that take a genuine `HaBridge`
protocol; these are already-parametrized leaf functions (`ha_get(entity_id)`,
`_cfg_num(cfg, key, default)`) with no implicit module state of their own to
invert. `solver_shared.py` sits in a new layer between `solver/` (layer 1) and
`solver_inputs/`+`solver_reports` (layer 2):

```python
# solver_shared.py — layer: below solver_inputs/solver_reports, above solver/
LOCAL_TZ: ZoneInfo
TIER1_PERIOD_HOURS: float
TIER2_PERIOD_HOURS: float
MAX_TIER1_HOURS: float
_LOGGER: logging.Logger

def _local(ts: datetime) -> datetime: ...
def _cfg_num(cfg: dict, key: str, default: float) -> float: ...
def _cfg_int(cfg: dict, key: str, default: int) -> int: ...
def resolve_effective_capacity_kwh(cfg: dict) -> float: ...
def import_fee_rate(cfg: dict, hour: int) -> float: ...
def ha_get(entity_id: str) -> dict: ...
def ha_post_state(entity_id: str, state, attributes: dict) -> None: ...
def fetch_p2p_fixed_export_kw(...) -> ...: ...
def p2p_bonus_price_by_period(...) -> ...: ...
def fetch_entity_history_range(...) -> ...: ...
def fetch_entity_attribute_history_range(...) -> ...: ...
def resample_history_nearest(...) -> ...: ...
def resample_history_mean(...) -> ...: ...
def _kw_scale_factor(entity_id: str) -> float: ...
def _version_stamp() -> dict[str, str]: ...
```

Signatures copied verbatim from the current `solver_writer.py` definitions at
the lines cited above — this spec moves bodies unchanged, it does not redesign
any of these functions. `ha_get`/`ha_post_state` keep reading module-level
`_NATIVE_HASS` from `solver_shared.py`'s own copy — see Migration step 2 for
how that seam itself moves without breaking the native/standalone dual-mode
split `_solver_writer()` exists for today.

## Façade decision

Every single name in scope has either a production caller outside
`solver_writer.py` (many via the existing `_solver_writer()` late-import seam
in `solver_inputs/*.py`) or a monkeypatch site (`ha_get`: 48, `ha_post_state`:
29+, `_LOGGER`: 6, `resample_history_mean`: 2, `fetch_entity_attribute_history_range`: 1,
`_kw_scale_factor`: 1) or both. Per the plan's own façade criterion
(section 2): **every name in this spec keeps a re-export in `solver_writer.py`.**

This is deliberately different from Phase 1's own convention ("no re-export
facade... all calling test files updated to import the new module directly").
Phase 1's two functions had no monkeypatch sites forcing the question. These
do, at a scale (48 sites on `ha_get` alone) where retargeting every call site
in the same PR would be a large, error-prone diff for zero behavioural gain —
the façade criterion exists precisely so a name this load-bearing doesn't have
to be retargeted just to move its storage location.

Concretely: `solver_writer.py` gets one block,
`from .solver_shared import (ha_get, ha_post_state, fetch_entity_history_range,
fetch_entity_attribute_history_range, resample_history_nearest,
resample_history_mean, _cfg_num, _cfg_int, resolve_effective_capacity_kwh,
import_fee_rate, fetch_p2p_fixed_export_kw, p2p_bonus_price_by_period,
_kw_scale_factor, _version_stamp, _local, LOCAL_TZ, TIER1_PERIOD_HOURS,
TIER2_PERIOD_HOURS, MAX_TIER1_HOURS)` (`_LOGGER` handled separately, see
Migration step 3). Every existing call site inside `solver_writer.py` and
every existing `patch.object(solver_writer, "ha_get", ...)`-style test
continues to resolve identically — this is a pure relocation, zero patch
paths retargeted, by construction.

The point of doing this at all: once the eight Phase-2 target functions
themselves move to `solver_reports/*.py` in a later step, THEIR OWN internal
calls to e.g. `ha_get(...)` will import it from `solver_shared` directly (a
downward, plan-compliant import) rather than from `solver_writer` (upward,
forbidden by the layer contract). Retargeting the monkeypatch sites that test
those eight functions specifically is Phase 2 proper's own cost, listed in
its own spec — not this one's.

## Invariants

- `solver_writer.<name>` resolves to the identical object (`is`, not just
  `==`) for every name in scope, before and after this spec — a test checking
  `solver_writer.ha_get is solver_shared.ha_get` after the move must pass.
- No call site inside `solver_writer.py` changes its own source text (the
  functions it calls are still named `ha_get`, `_cfg_num`, etc. — only where
  they're *defined* changes).
- `import-linter`'s `nimbus-layers` contract (pyproject.toml) is updated to
  declare the new layer and passes with **zero** new `ignore_imports`
  entries for anything this spec touches (the seven existing exceptions from
  #1338 are untouched — they're `solver_inputs/* -> solver_writer`, a
  different, still-open problem this spec doesn't claim to fix).
- `tests/test_diagnostic_log_levels.py`'s `_scanned_paths()` glob is extended
  to include `solver_shared.py` so a `_LOGGER.warning(...)` call moved there
  keeps being checked for the diagnostic-log-level conventions #357/#1298
  already enforce on `solver_inputs/*.py`.

## Migration

1. Create `solver_shared.py` with the 19 names above, bodies copied verbatim
   from their current line ranges in `solver_writer.py`. `ha_get`/`ha_post_state`
   need `_NATIVE_HASS` — this spec moves the **read** of that seam (both
   functions already just read a module-level name at call time) but leaves
   the module-level `_NATIVE_HASS` variable itself, and every function that
   *sets* it (`set_native_hass()` and friends), in `solver_writer.py`.
   `solver_shared.py` reads it via the same deferred `_solver_writer()`
   pattern `solver_inputs/*.py` already uses for exactly this reason — this
   is the one place this spec adds a late import rather than removing one,
   because `_NATIVE_HASS`'s own home is Phase 7's problem (`solver/ha_bridge.py`),
   not this spec's. Add it to `INTENTIONAL_NATIVE_ONLY` in
   `test_docs_writer_function_set_drift.py` with that one-line reason.
2. In `solver_writer.py`, delete each moved body, replace with the
   `from .solver_shared import (...)` re-export block from the Façade
   Decision section above. `_LOGGER` is not re-exported by name the same
   way — `solver_writer.py` keeps its own `_LOGGER = logging.getLogger(__name__)`
   (a genuinely different logger identity, `nimbus_load.solver_writer` vs
   `nimbus_load.solver_shared`, and every existing test asserting on a log
   line's own logger name would break otherwise); `solver_shared.py` gets
   its own `_LOGGER = logging.getLogger(__name__)`. This is a deliberate,
   named exception to "everything is re-exported" — call it out in review.
3. Update the six `solver_inputs/*.py` files that currently reach up into
   `solver_writer` for `_LOGGER` (`battery_participants.py`, `battery_soc.py`,
   `extra_batteries.py`, `load.py`, `prices.py`, `solar.py`) to import it from
   `solver_shared` instead. This changes each affected log line's own logger
   name from `nimbus_load.solver_writer` to `nimbus_load.solver_shared` — a
   real, visible change, not a no-op — so grep the suite for any test
   asserting a logger name or `caplog` fixture scoped to `"solver_writer"` on
   these six files' own log lines before assuming it's silent, and update any
   found. This is what retires 6 of the 7 `ignore_imports` exceptions in step
   4 below; skipping this step leaves the layer violations in place and the
   new Acceptance item unmet.
4. Run the golden master (`GOLDEN_UPDATE` must NOT be needed — a snapshot
   diff here means a real behaviour change slipped in). Run the full suite.
   Run `tests/test_docs_writer_function_set_drift.py` (confirms the
   standalone/cron docs copy of `solver_writer.py` doesn't silently diverge —
   extend its own function-set glob to `solver_shared.py` the same way
   Phase 1 extended it to `solver_inputs/*.py`).
5. Update the `nimbus-layers` import-linter contract: insert
   `"solver_shared"` as its own layer between `"solver_inputs | (solver_reports)"`
   and `"solver"`, and remove the 6 `ignore_imports` entries step 3 just
   retired. Confirm `lint-imports` reports exactly 1 ignored import (the
   remaining one named in Non-goals), zero new ones.
6. Run the new Part C gates built alongside spec 000: `size_ratchet.py`
   (`solver_writer.py`'s own over-60-line count should drop, `solver_shared.py`
   should introduce zero new one — these are verbatim moves), `coverage_compare.py`
   (`--base <pre-this-spec> --head <this-spec>` with a `--moved-code` mapping
   naming `solver_shared.py` as every moved name's destination), `noop_patches.py`
   (`--base <pre> --head <post>` over every test file listed as a monkeypatch
   site in Measured Cost above — this is the gate this spec exists to pass
   cleanly, given the `ha_get`/`ha_post_state` scale), `assertions_unchanged.py`.

## Non-goals

- Moving the eight Phase-2 reporting functions themselves (2b–2e, separate
  specs).
- Retiring the ONE remaining `solver_inputs/* -> solver_writer` layer
  violation this spec doesn't touch (`solver_publish.py`'s own reach into
  `solver_writer`, if it isn't for `_LOGGER` — re-check against the real
  #1338 exception list before assuming which one survives). Six of the seven
  ARE retired by this spec — see the new Acceptance item below — the
  seventh, whatever it turns out to be once the six are gone, is orthogonal,
  Phase 1's own debt.
- Redesigning any moved function's signature or behaviour, including ones
  with known rough edges (e.g. `_cfg_int`'s standalone/cron caller in
  `nimbus_solver_forecast_writer.py` — ported unchanged, not audited here).
- The `solver/ha_bridge.py` layering question #1338 already flagged (nested
  inside `solver/`, breaks the `layers` contract) — Phase 7's problem.

## Deploy timing

This is the first spec in the plan that creates a new production module —
Phase 1 (#1308) and this plan's own tooling PRs were test-only or pure
internal moves within already-existing files. `solver_shared.py` changes the
import graph of the live dispatch path.

That said, the façade decision above means the change is provably
behaviour-preserving by construction: `solver_writer.<name>` resolves to the
identical object before and after (a real test asserts this), the golden
master must be snapshot-identical, and every existing monkeypatch site keeps
resolving the same target. This is the same shape Phase 1 shipped under —
"Devhub validation: not claimed — a pure internal refactor with no new or
changed entity, service, or config surface has nothing for a live install to
exercise differently than before." The same reasoning applies here: this
ships as its own release, verified by the gates in Acceptance, not batched
behind Phase 2 proper and not requiring a devhub pass to justify (there is
nothing for a live install to do differently). Phase 2 proper's own later
specs (2b–2e), which DO change what gets published and when, get their own
devhub verification per the plan's existing rule for Phases 5/6.

## Acceptance

- [ ] Golden master identical for every scenario (`tests/test_golden_master.py`).
- [ ] Full suite passes; no existing assertion edited; zero new test files
      required (this is a pure relocation with full façade coverage — if a
      test breaks, the façade decision above was wrong for that name).
- [ ] `noop_patches.py --base <pre> --head <post>` reports zero regressions
      across every file listed under `ha_get`/`ha_post_state`/`_LOGGER`/etc.
      in Measured Cost — this is the load-bearing check for this specific
      spec, given the monkeypatch counts involved.
- [ ] No line executed before is unexecuted after (`coverage_compare.py`).
- [ ] mypy count not higher; zero in `solver_shared.py` itself.
- [ ] `nimbus-layers` import contract: new layer inserted, zero new
      violations, and exactly 6 of the existing 7 `ignore_imports` exceptions
      are removed — `solver_inputs/battery_participants.py`, `battery_soc.py`,
      `extra_batteries.py`, `load.py`, `prices.py`, `solar.py` each updated to
      import `_LOGGER` from `solver_shared` instead of reaching up into
      `solver_writer`. The contract has 1 exception remaining after this spec
      (see Non-goals), not 0 and not 7.
- [ ] `size_ratchet.py`: `solver_writer.py`'s over-60-line count strictly
      decreases; `solver_shared.py` adds zero new one (verbatim bodies).
- [ ] No new `_solver_writer()` late-import call site added, except the one
      named in Migration step 1 for `_NATIVE_HASS` (documented exception).
- [ ] `solver_writer.<name> is solver_shared.<name>` for every re-exported
      name (a real test, not just a claim).

## Rollback

Revert the single commit. `solver_shared.py` is new and unreferenced outside
`solver_writer.py`'s own re-export block; no state file format changes, no
migration to undo.
