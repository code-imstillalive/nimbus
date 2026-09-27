# Spec 002: `solver_reports/{quality,flex}.py` — the two lowest-cost Phase 2 movers

Status: proposed
Plan: docs/architecture/tech-debt-plan.md; phase: #1301 (Phase 2), step "2b"
Code cited at: `f2bc599`

## Responsibility

Phase 2 (#1301) moves eight reporting functions out of `solver_writer.py` into
`solver_reports/{quality,backtest,counterfactual,flex}.py`, `_compute_report_for_window`
last per the plan's own sequence (#1315). This spec is the first real move: the two
targets `tests/analyse_module_dependencies.py --phase 2` measures with the lowest
blocker count of the eight (2 and 1 respectively, against a low of 9 and a high of 31
for the rest) — "a target with a low blocker count is the natural first move," the
tool's own conclusion line. Both are genuinely independent of the other six: neither
calls nor is called by `compute_nimbus_only_soc_counterfactual`,
`compute_efficiency_backtest_report`, `publish_daily_quality_report`,
`rescore_quality_history`, `_carry_forward_quality_history`, or
`_compute_report_for_window` itself — confirmed below, not assumed.

- `_soc_discrepancy_stats` → `solver_reports/quality.py`. Called once, from
  `_compute_report_for_window` (`solver_writer.py:7630`), which stays in
  `solver_writer.py` until #1315 — this spec creates the module `_compute_report_for_window`
  and its four still-to-move siblings will land in later.
- `_compute_flex_report_for_window` → `solver_reports/flex.py`. Called once, from
  `compute_daily_flex_report` (`solver_writer.py:6008`), which also stays put — it
  is not itself one of the eight Phase-2 targets (`tests/analyse_module_dependencies.py
  --phase 2` doesn't list it), only its own private body does.

This spec does not move `compute_daily_flex_report`, `_compute_report_for_window`, or
any of the other six Phase-2 targets — those are later steps in the same phase (2c–2e,
plus #1315), against `solver_reports/` once it exists.

## Prior art

Not applicable — pure internal module organisation of Nimbus's own reporting code, the
same conclusion spec 000/001 and the plan's own header both already reached for this
kind of change. Neither EMHASS nor HAEO has an equivalent of `solver_writer.py`'s own
file layout to check against.

## Measured cost

`python3 tests/analyse_module_dependencies.py --functions "_soc_discrepancy_stats,_compute_flex_report_for_window"`
(real, current `main`):

```
### _soc_discrepancy_stats  (274 lines)
    importable:              2
    module-level (blockers): 1
      _SOC_BOUNDARY_EDGE_TOLERANCE_PCT

### _compute_flex_report_for_window  (152 lines)
    importable:              11
    module-level (blockers): 2
      FLEX_SIGNALS_ENTITY_ID, _PRICE_BAND_WIDTH

lines in scope:      426
distinct blockers:   3
```

All three blockers are private to their one target — none is shared, so there is no
shared-core sub-step the way Phase 2a's own `_LOGGER` was for six modules. The
"importable" counts (2 and 11) are names each function already reaches through
`solver_shared.py` (Phase 2a, merged) rather than through `solver_writer.py` directly —
`_compute_flex_report_for_window` in particular depends on `resample_history_mean` and
`fetch_entity_attribute_history_range`, both already relocated; this is exactly the
"gives Phase 2's own extraction something to import from instead of reaching back into
`solver_writer.py`" payoff spec 001 built 2a for.

`python3 tests/analyse_module_dependencies.py --functions "_soc_discrepancy_stats,_compute_flex_report_for_window,_SOC_BOUNDARY_EDGE_TOLERANCE_PCT,FLEX_SIGNALS_ENTITY_ID,_PRICE_BAND_WIDTH" --callers`
(real, current `main`):

```
production callers outside the module : 0
monkeypatch sites                     : 0
test files touching any target        : 4
```

Per-name: `_soc_discrepancy_stats` 0 prod / 0 patch / 2 test files
(`tests/test_1228_soc_discrepancy_compares_like_for_like.py`,
`tests/test_daily_quality_report.py`); `_compute_flex_report_for_window` 0 prod /
0 patch / 2 test files (`tests/test_1181_home_battery_power_coverage_is_published.py`,
`tests/test_daily_flex_report.py`); the three constants 0 prod / 0 patch each.

That "0 production callers" figure needs one correction the tool cannot make on its
own, found by checking rather than trusting the count (the same discipline spec 001's
own `_LOGGER` footnote applied): `custom_components/nimbus_load/diagnostics.py:69`
defines its own `_FLEX_SIGNALS_ENTITY_ID = "sensor.nimbus_flex_signals"` — the
identical string, independently written, not an import of
`solver_writer.FLEX_SIGNALS_ENTITY_ID`. It is a coincidental duplicate, not a real
dependency; `diagnostics.py` has no reference to any name in this spec's scope. Noted
under Non-goals rather than silently left as an unexamined "0".

Despite 0 production callers and 0 monkeypatch sites, this is **not** a zero-cost move:
the two direct callers that stay in `solver_writer.py` (`_compute_report_for_window` at
`:7630`, `compute_daily_flex_report` at `:6008`) need the moved names to resolve, and
so do the module-attribute call sites in the 4 test files — see Façade decision.

## Current behaviour this must preserve

| Behaviour | Produced at | Pinned by |
|---|---|---|
| SoC discrepancy stats (per-battery max/mean pt disagreement, `soc_discrepancy_reliable`, `soc_discrepancy_reason`) | `_soc_discrepancy_stats` (`:8367`) | `tests/test_1228_soc_discrepancy_compares_like_for_like.py` (7 direct calls), `tests/test_daily_quality_report.py` (20 direct calls, including the fleet-blend #949 regression coverage) |
| Flex-signal window report (`flex_available_up_kw`/`flex_available_down_kw` history read, price-band bucketing) | `_compute_flex_report_for_window` (`:6016`) | `tests/test_daily_flex_report.py` (13 direct calls), `tests/test_1181_home_battery_power_coverage_is_published.py` (4 direct calls) |
| Edge-tolerance treatment at the 0%/100% SoC boundary | `_SOC_BOUNDARY_EDGE_TOLERANCE_PCT` (`:8364`) | same `_soc_discrepancy_stats` tests above |
| Flex-signal entity id and $/kWh price-band width | `FLEX_SIGNALS_ENTITY_ID` (`:5936`), `_PRICE_BAND_WIDTH` (`:6011`) | same `_compute_flex_report_for_window` tests above; `test_daily_flex_report.py:65,77` also reads `solver_writer.FLEX_SIGNALS_ENTITY_ID` directly (not just through the function under test) |

**No golden-master scenario reaches either function.** Checked directly:
`compute_daily_quality_report`/`compute_daily_flex_report` (the public entry points
that call these two) are invoked only from the `nimbus_load.compute_quality_report`
service handler and the daily-schedule callback, never from `main()`'s own per-cycle
path the golden harness drives — the same reason both functions are already listed
under `INTENTIONAL_NATIVE_ONLY` in `tests/test_docs_writer_function_set_drift.py`
(lines 268–271, 598–607): "the standalone/cron script computes no quality report at
all." This spec's only behavioural pin is the direct unit-test call sites above; that
is the existing, sufficient coverage, not a gap this spec needs to close.

## Interfaces

Plain functions and constants, bodies copied verbatim — no redesign.

```python
# solver_reports/quality.py — layer: solver_inputs/solver_reports (layer 2)
_SOC_BOUNDARY_EDGE_TOLERANCE_PCT: float

def _soc_discrepancy_stats(
    battery_socs: list[tuple[list[tuple[datetime, float]], float]],
    j_ach_hourly: dict[str, dict[str, float]],
    max_threshold_pct: float = 15.0,
    mean_threshold_pct: float = 8.0,
    ach_soc_pct_at_hour: dict[str, float] | None = None,
    power_coverage: dict[str, float | int] | None = None,
) -> dict[str, float | bool | str | list[dict[str, float | bool | str]] | None]: ...
```

```python
# solver_reports/flex.py — layer: solver_inputs/solver_reports (layer 2)
FLEX_SIGNALS_ENTITY_ID: str
_PRICE_BAND_WIDTH: float

def _compute_flex_report_for_window(
    cfg: dict, day_start: datetime, day_end: datetime
) -> dict | None: ...
```

Both import their own dependencies from `solver_shared` (`ha_get`, `resample_history_mean`,
`fetch_entity_attribute_history_range`, `_LOGGER`, `LOCAL_TZ`, etc., per the "importable"
counts above) — a downward import, layer 2 depending on nothing but layer 1's
`solver_shared.py`, never reaching back into `solver_writer.py`.

## Façade decision

Zero external production callers, zero monkeypatch sites — by the plan's own criterion
as literally worded ("a production caller or monkeypatch site... outside the module"),
neither name strictly needs a re-export. Applying that reading anyway would mean:
retargeting 2 internal call sites in `solver_writer.py` (trivial — `from
.solver_reports.quality import _soc_discrepancy_stats` / `from .solver_reports.flex
import _compute_flex_report_for_window`, same shape either way) **and** rewriting all
44 test call sites across 4 files from `solver_writer.<name>(...)` to
`solver_reports.quality.<name>(...)` / `solver_reports.flex.<name>(...)`, since every
one of them resolves the name through `solver_writer`'s module attribute, not through a
direct `from solver_writer import <name>`.

That is exactly the "large diff for zero behavioural gain" spec 001 declined for
`ha_get`/`ha_post_state` at 48/29+ sites — 44 is smaller but the same shape, and unlike
2a's constants-and-utilities case there is no test-suite value in forcing the 4 files to
import from the new location (Phase 1's own reason to do that kind of rewrite was
"no monkeypatch sites forcing the question" combined with only two call sites total;
here there are dozens). **Decision: both names keep a re-export in `solver_writer.py`**,
justified on caller-count grounds even though the plan's literal criterion (external
production caller / monkeypatch) doesn't strictly require it — the criterion's own
purpose, stated in its text, is "something outside the module resolves it at call
time," and a test file calling `solver_writer._soc_discrepancy_stats(...)` is exactly
that, just via direct call rather than a patch. `_SOC_BOUNDARY_EDGE_TOLERANCE_PCT` is
re-exported alongside its function for the same reason (referenced only in a comment in
`test_daily_quality_report.py:522`, not executed — no forcing call site, but zero cost
to alias it for symmetry with its sibling constants). `FLEX_SIGNALS_ENTITY_ID` and
`_PRICE_BAND_WIDTH` are re-exported because `test_daily_flex_report.py:65,77` reads
`solver_writer.FLEX_SIGNALS_ENTITY_ID` directly, outside the function call.

Concretely: `solver_writer.py` gets
`from .solver_reports.quality import _soc_discrepancy_stats,
_SOC_BOUNDARY_EDGE_TOLERANCE_PCT` and
`from .solver_reports.flex import _compute_flex_report_for_window,
FLEX_SIGNALS_ENTITY_ID, _PRICE_BAND_WIDTH`. No patch path changes — every existing
`solver_writer.<name>` reference (test or internal) keeps resolving to the identical
object.

## Invariants

- `solver_writer._soc_discrepancy_stats is solver_reports.quality._soc_discrepancy_stats`,
  and the equivalent `is` check for the other 4 re-exported names — a real test, not a
  claim.
- No call site inside `solver_writer.py` changes its own source text at `:7630`/`:6008`
  (only where the callee is *defined* changes).
- `_compute_report_for_window`'s own behaviour (still in `solver_writer.py`, unmoved) is
  byte-for-byte unaffected — it calls the same object it always did.
- `import-linter`'s `nimbus-layers` contract passes with the new
  `solver_reports/*.py` glob added, zero new violations: both modules import only from
  `solver_shared` (layer 1) and stdlib, never from `solver_writer` (layer 5).

## Migration

1. Create `custom_components/nimbus_load/solver_reports/__init__.py` (package
   docstring only, per the existing `solver_inputs/__init__.py` convention — not a
   candidate for porting to the docs script, same as that file).
2. Create `solver_reports/quality.py`: `_SOC_BOUNDARY_EDGE_TOLERANCE_PCT` and
   `_soc_discrepancy_stats`, bodies copied verbatim from `solver_writer.py:8364-8696`
   (through the line before the next top-level `def`, `_quality_history_cache_active`
   at `:8697`), imports repointed to `solver_shared` per the Interfaces section.
3. Create `solver_reports/flex.py`: `FLEX_SIGNALS_ENTITY_ID`, `_PRICE_BAND_WIDTH`, and
   `_compute_flex_report_for_window`, bodies copied verbatim from `solver_writer.py:5936`
   and `:6011-6172` (through the line before the next top-level `def`,
   `publish_daily_flex_report` at `:6173`), same import repointing.
4. In `solver_writer.py`, delete the four moved bodies/constants, replace with the two
   `from .solver_reports.<x> import (...)` blocks from the Façade decision section.
5. Extend `tests/test_docs_writer_function_set_drift.py`'s `_EXTRACTED_PACKAGE_GLOBS`
   (currently `solver_inputs/*.py`, `solver_publish.py`, `solver_shared.py`, lines
   152-156) to add `os.path.join(_NIMBUS_DIR, "solver_reports", "*.py")` — same
   mechanism, same one-line justification style as the existing entries. The four moved
   names stay in their existing `INTENTIONAL_NATIVE_ONLY` entries unchanged (they are
   still native-only; only the file they live in moves).
6. Run the golden master — no scenario reaches this code, so this step confirms
   *nothing else* moved, not that this specific change is covered (see Current
   behaviour section). Run the full suite; all 44 existing call sites across the 4 test
   files must pass unedited.
7. Update the `nimbus-layers` import-linter contract: add `"solver_reports"` alongside
   `"solver_inputs"` in the existing layer-2 entry (they share a layer per the plan's
   own section 2 table — both "depend on `solver/` and on protocols").
8. Run the Part C gates from spec 000: `size_ratchet.py` (`solver_writer.py`'s
   over-60-line count drops by the two functions moved — `_soc_discrepancy_stats` at
   274 lines and `_compute_flex_report_for_window` at 152 lines are both over 60;
   neither new module introduces a new over-60-line function, verbatim moves),
   `coverage_compare.py` (`--moved-code` mapping naming `solver_reports/quality.py` and
   `solver_reports/flex.py`), `noop_patches.py` (no monkeypatch sites in scope, so this
   should report zero relevant rows — confirms the "0 monkeypatch" measurement rather
   than just repeating it), `assertions_unchanged.py`.

## Non-goals

- Moving `compute_daily_flex_report`, `_compute_report_for_window`, or any of the
  other six Phase-2 targets (`compute_nimbus_only_soc_counterfactual`,
  `compute_efficiency_backtest_report`, `publish_daily_quality_report`,
  `rescore_quality_history`, `_carry_forward_quality_history`) — later steps in the
  same phase, their own specs.
- Deduplicating `diagnostics.py:69`'s independently-defined
  `_FLEX_SIGNALS_ENTITY_ID` against `solver_reports.flex.FLEX_SIGNALS_ENTITY_ID` — a
  real, harmless-today duplication noticed while measuring this spec's callers, not
  something this move causes or needs to fix. Worth a follow-up issue, not folded in
  here.
- Redesigning either function's signature or behaviour, including the fleet-blend SoC
  comparison `_soc_discrepancy_stats` implements (#949) — ported unchanged.

## Deploy timing

Not devhub-gated: the plan's own gate table (section 3) scopes the "Live: devhub pass"
requirement to Phases 5 and 6 only. This is also a pure internal move with the same
provable-by-construction shape spec 001 shipped under — `solver_writer.<name>` resolves
to the identical object before and after (a real test asserts this), full suite green,
no new or changed entity/service/config surface. "Devhub validation: not claimed — a
pure internal refactor with nothing for a live install to exercise differently."

## Acceptance

- [ ] Golden master identical for every scenario.
- [ ] Full suite passes; zero existing assertions edited; the 44 call sites across the
      4 named test files pass unedited.
- [ ] `noop_patches.py` reports zero relevant rows (no monkeypatch site was ever in
      scope for this spec).
- [ ] No line executed before is unexecuted after (`coverage_compare.py`).
- [ ] mypy count not higher; zero in `solver_reports/` itself.
- [ ] `nimbus-layers`: `solver_reports` added to the existing layer-2 entry, zero new
      violations.
- [ ] `size_ratchet.py`: `solver_writer.py`'s over-60-line count strictly decreases by
      2; neither new module adds one.
- [ ] No new `_solver_writer()` late-import call site added (neither target needs
      `_NATIVE_HASS` or any other module-level writer state).
- [ ] `solver_writer.<name> is solver_reports.<module>.<name>` for all 5 re-exported
      names (a real test).
- [ ] `tests/test_docs_writer_function_set_drift.py` passes with the extended glob.

## Rollback

Revert the single commit. `solver_reports/` is new and unreferenced outside
`solver_writer.py`'s own re-export block; no state file format changes, no migration to
undo.
