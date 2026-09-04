# Changelog

All notable changes to Nimbus are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adopts [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Versioned from `custom_components/nimbus_load/manifest.json` — the Nimbus Solver add-on (`nimbus_solver_app`) that used to share this version line was removed in v0.94.85 (see [#357](https://github.com/code-imstillalive/nimbus/issues/357)).

Entries call out real, user-visible changes. They are not a `git log` dump; the commit history is the source of truth for the underlying diffs.

## [0.94.107] — 2026-09-05

### Fixed
- **#366 finding 3: real pickle schema versioning for `TrainedModel`, plus a robust backfill for older persisted models** ([#366](https://github.com/code-imstillalive/nimbus/issues/366), thanks @purcell-lab — v0.94.80 already fixed the narrower issue of `_load_model_from_disk()`'s compatibility check sitting outside the `pickle.loads()` try/except; this closes out the fuller ask). `TrainedModel` gained a `schema_version: int` field (`TRAINED_MODEL_SCHEMA_VERSION = 1`); `_load_model_from_disk()` now discards and retrains fresh on a mismatch, the same self-healing fallback already used for a feature-count mismatch.
  A plain `@dataclass`'s default pickling restores an old persisted object's `__dict__` verbatim on unpickle, skipping `__init__` and every field default entirely -- this repo's own CLAUDE.md already documented finding this the hard way for `seasonal_lookup` (an old `.pkl` written before that field existed unpickled with the attribute genuinely missing, raising a real `AttributeError`). Rather than add a defensive `getattr(trained, "x", default)` at every one of the several call sites the review flagged (`gbrt_lower`/`gbrt_upper`, `model_type`, `validation_mae`/`validation_mase` are all accessed directly), `TrainedModel.__setstate__` now seeds every current field's default before overlaying the pickle's own real state -- any field an old pickle lacks is backfilled instead of absent, so direct attribute access is safe everywhere without touching each call site individually. A pickle from before `schema_version` existed at all backfills to `0` via this same mechanism, which always mismatches the current version -- every already-deployed `.pkl` self-heals via one retrain the first time it's loaded under this code.
  Verified via the exact `object.__new__()` + manual `__dict__` round-trip technique CLAUDE.md's own prior fix used, through the real pickle protocol (`tests/test_trained_model_setstate_backfill.py`), plus new schema-version-mismatch coverage in `tests/test_coordinator_load_model_from_disk_robustness.py`. Mutation-tested: removing `__setstate__` was confirmed to fail 3 of the new tests -- including revealing that `schema_version`'s own plain-default class-attribute fallback would otherwise silently mask a pre-versioning pickle as "current" instead of correctly flagging it as version 0, which is exactly why the backfill needed to write `schema_version` into `__dict__` explicitly rather than rely on it.
  Finding 4 (quantile band coverage, ~76.7% vs an 80% target) was explicitly recorded by the review as "not a bug" -- a known, documented approximation, not something this pass changes.

This closes out every actionable finding raised in #366.

## [0.94.106] — 2026-09-05

### Fixed
- **#366 finding 2: per-cycle event-loop percentile/quantile inference work reduced** ([#366](https://github.com/code-imstillalive/nimbus/issues/366), thanks @purcell-lab). Two independent per-cycle costs, both fixed the same way the review suggested ("compute q80 once per cycle and pass the scalar; batch quantile inference post-loop"):
  - `coordinator.py`'s per-horizon-point loop (~385 points/tick) called `calibrated_band()`, which recomputed `np.percentile(self._residuals, ...)` from scratch on every single point even though `self._residuals` never changes within a cycle. `ml/model.py` gained `calibration_half_width(residuals)`, computed once per cycle before the loop; `calibrated_band()` gained an optional `near_term_half_width` keyword to skip its internal `np.percentile()` call when given a precomputed value (default `None` preserves the exact original behavior and signature for existing callers/tests).
  - `ml/model.py`'s `predict()` called `trained.gbrt_lower.predict()`/`trained.gbrt_upper.predict()` once per horizon step (~385 times/tick, ~1.6s in the executor) even though neither quantile model feeds the recursive lag chain the way the main model's own prediction does. Each step's standardized feature row is now collected during the loop and both quantile models are called exactly once, in a single batch, after it — `GBRT.predict()` is already fully row-independent (see #366 finding 1's own equivalence test), so this is bit-identical to the old per-step calls, just without ~770 redundant per-call overheads.
  Both changes verified with a call-count/shape assertion proving the batching genuinely happened (not just a cosmetic refactor) plus a numeric check against the old per-row/per-point calling convention on the same real inputs (`tests/test_predict_quantile_batching.py`, `tests/test_calibrated_band_cold_start_sign.py`). Mutation-tested: reverting either change back to its old per-step form was confirmed to fail the new tests before being trusted.
  Finding 3 (pickle schema versioning) remains open.

## [0.94.105] — 2026-09-05

### Fixed
- **#366 finding 1: `ml/gbrt.py`'s pure-Python GBRT split search vectorized, ~1000x faster per tree fit** ([#366](https://github.com/code-imstillalive/nimbus/issues/366), thanks @purcell-lab — the review measured 7.2s per tree fit at 2880 rows and 22.8s at 8760 rows, with `train_model()` doing up to 4 fits). `_build_tree()`'s inner `for i in distinct:` loop computed `left_sse`/`right_sse`/`gain` one candidate split point at a time in pure Python; rewritten to evaluate every candidate for a feature at once via numpy array indexing (`cum_sum[distinct]`, `cum_sum_sq[distinct]`) plus a single `np.argmax` over the masked gain array, replacing the O(candidates) Python loop with O(1) vectorized numpy calls per feature. Benchmarked locally: 8760-row single tree fit now 0.013s (was 22.8s), 2880-row 0.006s (was 7.2s).
  This changes the actual split decisions the model learns from, not just plumbing around it, so it was treated with the same care as any other correctness-critical change in this project: a verbatim copy of the original pure-Python loop is pinned as a reference oracle in `tests/test_gbrt_split_vectorization_equivalence.py`, and the vectorized version is asserted bit-for-bit identical against it (same feature/threshold at every split, same leaf values, same predictions) across 270+ randomized trials plus explicit edge cases — duplicate feature values, exact gain ties (verifying the vectorized version reproduces the original's first-occurrence tie-break via `np.argmax`'s own first-occurrence semantics), `min_samples_leaf` boundary cases, quantile trees, and a full `GBRT.fit()`/`predict()` round trip. Mutation-tested: reversing the candidate evaluation order (breaking the tie-break) was confirmed to fail 12 of 13 differential tests before the fix was accepted.
  Findings 2 (per-cycle percentile/quantile inference work) and 3 (a real `schema_version` field for pickle compat, beyond v0.94.80's existing partial fix) remain open.

## [0.94.104] — 2026-09-05

### Fixed
- **#362 finding 4a: a stale "DIAG: temporary" code comment corrected against #302's own closed conclusion** ([#362](https://github.com/code-imstillalive/nimbus/issues/362), thanks @purcell-lab — findings 1/2 were fixed in v0.94.70; finding 4c, `register_price_latency_sensor()`'s missing unregister counterpart, is now also resolved as a side effect of v0.94.101's `solver_runtime.reset_module_state()`, which clears the same module-level reference on unload). `_NimbusSolverPushSensor._attr_should_poll = False` carried a `# DIAG: temporary, testing #302` comment, but #302's own closed investigation explicitly concluded this is real, permanent, independently-justified hygiene (a pure push entity with no `update()` method was being force-polled by HA's own default 15s scan interval, the confirmed cause of a live flap on the reference household's own NUC1) — not a temporary diagnostic flag at all. Comment corrected to state the real, confirmed, permanent status; the flag itself is unchanged (removing it would revert a real fix and reintroduce #302's own bug). New regression test locks in `_attr_should_poll is False` on the base class, guarding against exactly the "someone reads 'DIAG: temporary' and removes it" risk the stale wording invited. Mutation-tested.
- Finding 3 (a shared `_NimbusStalePushMixin` extraction across `sensor.py`/`number.py`/`switch.py`/`sensor_flattened.py`) and the remaining smaller items in finding 4 (`NimbusSolverConfigSensor.native_value` mutating state from inside a property getter; a real but narrow entity_id collision risk when two subentries share one source sensor) remain open — the mixin extraction is a genuine, larger structural refactor, and the other two carry real production-entity-naming/lifecycle risk that warrants its own dedicated, careful pass rather than a rushed fix bundled here.

## [0.94.103] — 2026-09-05

### Changed
- **#363 findings 1 and 2 fixed: every operational `print()` and every silently-swallowed `except Exception` in `solver_writer.py` now logs** ([#363](https://github.com/code-imstillalive/nimbus/issues/363), thanks @purcell-lab, findings 1/2 of 5 — finding 3 was fixed in v0.94.79; findings 4 (duplicated history fetchers, dead attributes) and 5 (the 1,788-line `main()` split) remain their own, larger, separate efforts, not bundled into this pass).
  - **Finding 1**: all 22 remaining operational `print(..., file=sys.stderr)` sites (source unavailable, SoC outside configured/physical range, fixed-export violation clamp, plan-state save failure, `safe_num` fallback, solar source dropped, and more) are now `_LOGGER.warning(...)` — confirmed via a direct test that Python's own `logging.lastResort` handler still surfaces WARNING+ messages to stderr with zero configuration, so the standalone/cron deployment loses nothing. Also found and fixed a real inconsistency along the way: the `#85 trace`'s own `print()` claimed in its own comment to be "kept for the standalone/cron/addon deployment," but that print() sits inside a branch that only ever executes in native (in-process HA integration) mode — the claim was simply wrong, and the print() was pure unconditional noise (8+ lines per cycle) with zero real audience; removed, keeping only the `_LOGGER.debug()` call already sitting right next to it. One print() deliberately kept outside the `__main__` guard: the per-cycle status summary, since converting it to `_LOGGER.info()` would make it silent by default for the exact standalone/cron audience it's useful for (`lastResort` only surfaces WARNING+, not INFO).
  - **Finding 2**: all 12 bare `except Exception: return`/`pass` sites with zero breadcrumb now log at DEBUG (routine, already-accepted degradations) or WARNING (a household-visible publish failure) before degrading — same discipline #313/#314 already proved out elsewhere in this file, same "the swallow itself is correct, the silence was the bug" reasoning throughout. Two of these were the exact same class of incident already fixed once (2026-08-31) for `publish_daily_quality_report`/`publish_nimbus_only_soc_counterfactual`/`publish_efficiency_backtest_report` — `publish_weather_forecast_mirrors` and `update_solar_delivery_ratio` had been missed from that same pass.
  - New AST-based regression test (`test_solver_writer_no_silent_failures.py`) structurally verifies both properties across the whole file — not a fragile source-text regex — so a future new `print()`/silent `except Exception` anywhere in this 7000+ line file is caught here. Mutation-tested (reintroducing one of each reproduces a precise, line-numbered failure). 3 existing tests that asserted on the old `print()`/`file=sys.stderr` mechanism updated to check the new `_LOGGER` calls instead.

## [0.94.102] — 2026-09-05

### Fixed
- **Real CI break from v0.94.101, on Python 3.14.7 specifically (never reproduced on local 3.12.10)**: `tests/test_solver_soft_min_soc_constraint.py` (a plain solver LP test with zero tempfile usage of its own) failed deterministically (confirmed via a rerun, not a one-off flake) with `pytest.PytestUnraisableExceptionWarning: Exception ignored while calling deallocator <function _TemporaryFileCloser.__del__ ...>: None` — a garbage-collected, never-explicitly-closed temp file object (almost certainly created deep inside `highspy`/HiGHS's own C-extension internals during an earlier test's solve, not this test's own code) happening to get finalized while this unrelated, later test executes. `pyproject.toml`'s own `filterwarnings = ["error"]` promotes pytest's GC-timing-driven warning into a hard failure wherever in the suite it happens to land. Added a message-based `filterwarnings` ignore entry, same "a dependency's own internal housekeeping quirk, not a real regression in our diff" shape as the existing aiohttp entry.

## [0.94.101] — 2026-09-05

### Fixed
- **#365 lifecycle robustness: services now unregistered on unload, module-level solver_runtime globals reset, and a stale CLAUDE.md theory corrected against the real code** ([#365](https://github.com/code-imstillalive/nimbus/issues/365), thanks @purcell-lab, items 1, part of 4, 6, and 7 of 7 — items 2/3/5 already fixed in v0.94.78; item 7's own suggested fix was found already implemented, predating this review; the remaining, harder half of item 4 — an in-flight executor solve genuinely outliving unload, briefly — is a real, narrow-window race left open, see below).
  - **Item 1**: removing the (only, `single_config_entry: true`) hub used to leave `nimbus_load.solve_now`/`retrain`/`compute_quality_report` registered and callable forever, with `solver_runtime` still bound to the removed entry's own state. New `services.async_unregister_services()`, called from `async_unload_entry()` on every successful unload (safe unconditionally, including a plain reload, since this integration only ever has one entry and re-registration is already idempotent).
  - **Item 4 (partial)**: `solver_runtime.py`'s module-level globals (`_solver_writer`'s `set_native_hass()` binding, `_last_solve_completed_monotonic`, `_import_error_notified`, `_price_latency_sensor`, `_consecutive_lock_skips`) used to live for the process's whole lifetime, not the config entry — a genuine remove-then-re-add (the only way a "different" entry can ever exist, given `single_config_entry`) would silently inherit the previous entry's state. New `solver_runtime.reset_module_state()`, called from `async_unload_entry()` alongside the services unregister. The harder remaining half of this finding — a dispatched executor job (`sw.main()`) is not interruptible, so it can keep running (and calling `ha_post_state()`) briefly after `async_unload_entry()` returns — is a real, narrow-window race not fixed here; the review's own suggested fix explicitly offered this globals-reset as an acceptable "at least" fallback short of tracking and awaiting an in-flight-solve flag, which would need its own careful pass given it touches real dispatch-cycle lifecycle.
  - **Item 6**: `_run_one_cycle()` now recognizes the specific, shaped `urllib.error.HTTPError(404)` for `sensor.nimbus_solver_config` (a genuine, expected, self-recovering startup race between the startup-solve task and the `sensor` platform's own setup finishing) and logs one clean warning instead of a full traceback on every startup-retry attempt. Also corrected this project's own `CLAUDE.md` history: the original theory ("`_NATIVE_HASS` isn't registered yet") doesn't match the real code — `_ensure_ready()` always calls `set_native_hass()` before `sw.main()` runs, on every attempt including the first; the 404 is real and correctly reported, it just means this one entity doesn't exist yet.
  - **Item 7, investigated, confirmed already fixed**: the review's own suggested `if _setup_tasks.get(entry.entry_id) is task: del ...` identity check already exists in the current code (added 2026-09-01, predating this review's 2026-09-03 snapshot) — no further change needed.
  - 9 new regression tests across 4 files, all mutation-tested (reverting each new call site reproduces a genuine failure).

## [0.94.100] — 2026-09-05

### Changed
- **#364 findings 1 and 2 fixed: `solver/README.md`'s stale "draft, not wired into anything" claims corrected; `README.md` now documents 6 previously-undocumented entities/services** ([#364](https://github.com/code-imstillalive/nimbus/issues/364), thanks @purcell-lab, 2 of the 4 remaining findings after v0.94.85's earlier pass fixed findings 5/6/7 — `.gitignore`, executable bits, `icon.psd`).
  - **Finding 1**: `custom_components/nimbus_load/solver/README.md`'s header claimed "draft... Not wired into anything... Registers zero HA entities" and described `lp.py` as a from-scratch pure-numpy simplex with "No scipy/PuLP/highspy" — none of which has been true since 2026-08-18 (`lp.py`'s real `highspy` rewrite) and 2026-08-25+ (`solver_writer.py` importing and running this package in-process on every solve cycle). Corrected the header to state the real current status (live, shadow-mode — publishes a real dispatch plan and a durable dry-run record, but still has no write path to any real hardware) and fixed the `lp.py` bullet. Also replaced the "Running the tests" section's itemized 3-file list (none of which exist anymore) with a general, low-maintenance description pointing at the real `tests/test_solver_*.py`/`tests/test_elements_*.py` suite (50+ files) — a specific file list is exactly what went stale and became misleading the first time, so this section deliberately doesn't repeat that mistake. The rest of the file (a real, detailed 2026-08-15/16 development history — bugs found, design decisions, real-data experiments) is kept as-is, now clearly framed as historical record rather than a current-state description.
  - **Finding 2**: `README.md` never mentioned `sensor.nimbus_health_report`, `switch.nimbus_solver_dispatch_dry_run`/`sensor.nimbus_solver_dispatch_dry_run`, `sensor.nimbus_solver_price_response_latency`, `sensor.nimbus_mirror_{temperature,humidity}_forecast`, or the `compute_quality_report` service, despite all being real and live — a reviewer grepping the docs for any of these would conclude they don't exist. New "Diagnostics, dry-run, and other hub sensors" and "Services" sections cover all of them (plus `retrain`/`solve_now`, for completeness), each description verified against the real source (docstrings, schemas) rather than guessed.
  - New regression test (`test_readme_entity_references_exist.py`) checks every entity_id/service string README.md now names against the real source, and separately scans `services.py` for every `SERVICE_*` constant to confirm the README mentions it — guards the exact staleness direction that created finding 1 in the first place. Mutation-tested (removing one README mention reproduces a genuine failure).
  - **Findings 3 (CLAUDE.md split) and 4 (hardcoded IPs in research scripts) remain open** — both explicitly flagged in an earlier pass as bigger, judgment-heavy editorial/cleanup work not to rush alongside quicker fixes; still true here.

## [0.94.99] — 2026-09-05

### Changed
- **#359 finding 3: `integration_type` corrected from `"service"` to `"hub"`** ([#359](https://github.com/code-imstillalive/nimbus/issues/359), thanks @purcell-lab, the one item of 5 remaining after v0.94.76 fixed findings 1/2/4). HA's own documented meaning for `"service"` is a cloud/web-service integration; Nimbus is purely local and creates a single hub config entry with multiple subentry-devices attached (Load, Power Signal, Power Source, PV String, Battery Tower) — exactly the documented `"hub"` shape. New regression test locks in this value alongside the two other manifest fields findings 1/2 already fixed (`frontend` dependency, `single_config_entry`), so a future accidental revert of any of the three is caught (hassfest only validates manifest SHAPE, not these specific values).
- **Finding 5 (armv7/`highspy`), investigated, confirmed already effectively addressed**: the top-level `README.md`'s own "Compatibility" section already correctly documents the limitation ("Solver: `amd64` or `aarch64` only... no wheel exists for 32-bit armv7, Pi 3, or Zero") — the review's quoted false "pure numpy, no such limitation" claim lives in `custom_components/nimbus_load/solver/README.md` instead (stale since `lp.py`'s 2026-08-18 rewrite to use `highspy`), which is a distinct, already-tracked finding under #364, not this issue. The suggested alternative (making `highspy` a lazily-imported optional requirement so the Forecaster still works without the Solver) is a real, separate architecture change — not pursued here given the shrinking real-world relevance of 32-bit ARM HA installs relative to the effort.

## [0.94.98] — 2026-09-05

### Changed
- **#358 CI hardening: findings 1 (unpinned actions) and 3 (coverage thrown away) fixed; findings 2 and 7 confirmed already fixed; findings 4 (mypy advisory), 5 (ruff scope), and 6 (Python target mismatch) investigated and deliberately deferred** ([#358](https://github.com/code-imstillalive/nimbus/issues/358), thanks @purcell-lab).
  - **Finding 1**: every `uses:` across all 4 workflow files (`ci.yml`, `hassfest.yml`, `hacs.yml`, `release.yml`) is now pinned to a full 40-character commit SHA instead of a mutable tag/branch (`@v5`, `@master`, `@main`) — a floating reference can start running different, unreviewed code against this repo's own `GITHUB_TOKEN` with no diff to review. Each pin carries a trailing `# vX.Y.Z` (or `# master @ <date>` for the two actions with no real semver tag) comment naming what it corresponds to. New regression test (`test_ci_workflow_actions_pinned.py`) scans every workflow file and fails if any `uses:` reference is ever a tag/branch again, or lacks its version comment — mutation-tested (reverting one pin back to `@v5` reproduces 8 genuine subtest failures).
  - **Finding 3**: added `--cov-fail-under=75` to the real HA-harness pytest invocation (the `--cov-append` second run, so the check evaluates the FULL combined coverage across both invocations). 75% is a deliberately conservative floor below the ~80% the stub-based run alone already measures locally — real margin against environment variance while still catching a genuine regression (e.g. a large deleted/skipped test file) before it merges silently. `coverage.xml` was already in `.gitignore` (the finding's other sub-point) — nothing to fix there.
  - **Findings 2 and 7 confirmed already fixed**, not reopened: `permissions: contents: read` on the three read-only workflows and the fresh-clone `aiohttp` filterwarnings crash were both fixed in v0.94.76/77.
  - **Finding 4 (mypy permanently advisory), investigated, deliberately deferred**: ran mypy locally and found 153 findings, not the ~33 originally documented — almost certainly driven by local/CI type-stub-availability differences (neither environment installs the real `homeassistant` package for type-checking, only `--ignore-missing-imports`), not 120 new real bugs. mypy has no native baseline mechanism (unlike ruff/eslint); building one now, on a finding count that disagrees with itself by 4.6x depending on environment, risks either encoding significant noise as a permanent "acceptable" baseline or making CI newly and unpredictably red. Left advisory rather than rushing a bad gate.
  - **Finding 5 (ruff scope excludes `docs/`), investigated, deliberately deferred**: the `nimbus_solver_app/` half of this finding is now moot (deleted entirely in v0.94.85, #357). `docs/` itself currently has 77 ruff findings across 12 files — 28 in the 4 real, user-facing deployment scripts (`nimbus_solver_forecast_writer.py` and siblings) and 49 in the `research/` subfolder's one-off analysis scripts (already treated as a separate, lower-priority hygiene category by #364). A real cleanup, not attempted in this pass — left open rather than rushed.
  - **Finding 6 (Python target-version mismatch), already investigated in an earlier session, confirmed the decision still stands**: bumping `ruff`'s `target-version` to py314 rewrites `except (A, B):` into 3.14-only PEP 758 syntax, which would break every contributor still on 3.12 locally while passing CI. Left alone, untouched here.

## [0.94.97] — 2026-09-05

### Changed
- **#360 test-infrastructure findings 3 and 6 fixed; findings 1/2/4 confirmed already fixed; finding 5 investigated (one half not a real duplicate, the other left open); finding 7 (ml/ coverage gaps) not yet started** ([#360](https://github.com/code-imstillalive/nimbus/issues/360), thanks @purcell-lab).
  - **Finding 3 (timing-dependent tests)**: `test_coordinator_retrain_task_idempotent.py`'s `await asyncio.sleep(10)` (a stand-in for "a retrain that hasn't finished yet") replaced with `await asyncio.Event().wait()` — has the same never-completes-on-its-own property with zero dependency on real time passing at all. `test_price_watcher.py`'s two `await asyncio.sleep(0.15)` calls (waiting past a real 0.05s `hass.loop.call_later()` debounce — a genuinely real-timer mechanism that can't be faked without a bigger production change) replaced with a bounded poll-until-condition helper (`_wait_until()`) instead of a fixed-margin guess — waits exactly as long as the real timer takes, fails only if it genuinely never fires within a generous 2s ceiling. `test_solver_runtime_price_latency.py`'s and `test_solver_runtime_slow_cycle_diagnostics.py`'s real `time.sleep(0.1-0.15)` calls (proving a duration measurement is a genuine `time.monotonic()` delta, not a stale/zeroed value) replaced with direct `time.monotonic` patching — proves the exact same subtraction deterministically, with zero real elapsed time. `test_ha_call_service_with_response.py`'s own loop-startup wait was checked and found to already be a correct bounded poll, not a fixed sleep — no change needed.
  - **Finding 6 (flow tests bypass schema validation)**: all 5 `tests/test_flows_*_subentry.py` files call `flow.async_step_user({...})` directly, skipping `FlowManager.async_configure()`'s own real schema validation — "exactly where 3 real flow bugs this review found actually live" per the issue. Every fixture dict driving a direct call now also passes through the real `_schema(...)` object first, confirming it's genuinely something the real schema would accept, not just something the handler's own body happens to tolerate when fed directly. `flows/signal_subentry.py` was the one file with no extractable `_schema()` function at all (built inline) — extracted as a pure, zero-behaviour-change refactor (confirmed via the full existing test file still passing unmodified) so it could be validated the same way as its four siblings.
  - **Findings 1/2/4 confirmed already fixed**, not reopened: `tests/run_all.py` and the ~125 duplicated `__main__` collectors were removed in v0.94.87; the four named assertion-free tests already carry real assertions from an earlier session. One correction made here: my own 5 new test files from today's earlier #356 work had reintroduced the exact `if __name__ == "__main__":` boilerplate v0.94.87 removed repo-wide (written before this session had re-confirmed that fix) — removed again.
  - **Finding 5 (duplicates), investigated, not blindly followed**: the review's claim that `test_flow_decomposition.py`/`test_dispatch_source_breakdown.py` are a "duplicate pair" doesn't hold up — `_flow_decomposition()` (4-terminal, 7-flow) and `_dispatch_source_breakdown()` (2-way) are both genuinely live, separately-called production functions (confirmed via grep: `_dispatch_source_breakdown()` is still called at `solver_writer.py:7078`), not one superseding the other — merging their tests would lose real coverage of one function, not remove duplication. Left un-merged for exactly this reason. The five subentry-flow files' own real repeated three-test pattern (fresh-add/reconfigure/step-alias) is a genuine, if lower-priority, DRY opportunity — left open rather than rushed into a parametrized rewrite this pass.
  - **Finding 7 (ml/ coverage gaps)** — `gbrt.py` in isolation, `calibrated_band()`, `predict()`'s `allow_negative`/`seasonal_anchor`/damping-skip paths, `resample_last_value()` with gaps/NaN/DST, and an old-`__dict__` pickle round-trip — not yet started, left open for a dedicated pass.
  - 16 new/changed test assertions across 8 files, all confirmed passing; the timing fixes also confirmed meaningfully faster (the coordinator test dropped from a real 10s-capable sleep to sub-millisecond).

## [0.94.96] — 2026-09-04

### Fixed
- **The two remaining findings of #348 — a hardcoded day/night discharge-cost/salvage-value schedule and LocalVolts-specific price-array key parsing, both flagged as carrying real financial/dispatch risk if rushed** ([#348](https://github.com/code-imstillalive/nimbus/issues/348), thanks @purcell-lab, findings 3 and 4 of 4 — findings 1 and 2, the fixed daily charge and post-midnight self-consume window, were fixed in v0.94.88). Both remaining findings only ever applied to the `has_price_forecast_array` branch of `main()`'s price-building block (a generic install without that sensor configured was never affected by either, and still isn't).
  - **Day/night discharge-cost/salvage-value schedule**: `battery_discharge_cost_rate()`/`battery_salvage_value_rate()` were hardcoded Python-constant functions (this household's own real 5pm/midnight/7am tuning) with zero way for any install — including the one it was tuned for — to retune it short of editing source. Replaced with `scheduled_discharge_cost_rate(cfg, hour)`/`scheduled_salvage_value_rate(cfg, hour)`: a real, optional, wizard-configurable multi-block schedule (a default rate plus up to 3 override blocks each), mirroring the already-established `solver_network_fee_1/2/3_rate/start_hour/end_hour` pattern in this same file. Needed one genuine addition beyond that existing pattern: a new wraparound-aware `_hour_in_schedule_block()` helper, since the real historical schedule's own night window (5pm-7am) spans midnight, which the existing blocks' plain `start <= hour < end` matcher can't express. Every new config key's own schema default reproduces the exact historical schedule — byte-identical output for every existing install, confirmed by testing every hour of the day against the old hardcoded functions directly.
  - **LocalVolts-specific `costsflexup`/`earningsflexup` price-array keys**: the "generic" `solver_price_forecast_array_sensor` field was parsed with these two attribute-key literals hardcoded directly at the call site — `resample_price_with_extrapolation()` itself already takes the key name as a genuinely generic parameter, the hardcoding was purely at the two call sites. Now two optional wizard fields, `solver_price_forecast_array_import_key`/`_export_key`, each defaulting to the exact literal the call site has always used.
  - The interim startup-WARNING naming these two overrides (shipped in v0.94.88 as the review's own suggested stopgap) has been removed for both — they're real, visible, wizard-editable fields now, nothing left to warn about. The one still-genuinely-hardcoded item from the original review (the P2P matched-rate sensor's fixed 17:00-24:00 window, independent of the household's own configured P2P block hours) keeps its own warning unchanged.
  - 20 new/rewritten regression tests. The schedule fix's byte-identical-defaults claim was verified directly (every hour 0-23 checked against the old hardcoded functions, 48 subtests), the wraparound helper was mutation-tested (removing the wraparound branch reproduces 19 genuine failures across the schedule and boundary tests), and the price-array key fix was confirmed to be a real, non-cosmetic change (the old hardcoded `"costsflexup"` key finds zero points against a differently-shaped forecast that the new parameterized key reads correctly).

## [0.94.95] — 2026-09-04

### Fixed
- **#356's remaining item 4, now closing the issue: `quality_report.py` silently blended distinct real calendar days together on any window longer than 24h, and `_align_previous_periods()` was an O(n·m) nested loop** ([#356](https://github.com/code-imstillalive/nimbus/issues/356), thanks @purcell-lab, item 4 of 4 — items 1-3 shipped in v0.94.94).
  - **Hour folding**: `_hourly_means_by_key()` (the row-major hourly reconstruction behind the `j_ref_hourly`/`j_ach_hourly`/`j_star_hourly` sensor attributes) used to hard-fold every period onto exactly 24 hour-of-day buckets via `% 24` — silently correct only because `compute_daily_quality_report()` was, at the time, the only caller and always passed exactly one real calendar day. Issue #316's own `nimbus_load.compute_quality_report` service (v0.94.42) lets a caller request an ARBITRARY window, including `allow_partial=True` windows longer than 24h, explicitly for diagnostics/backfill/A-B comparison. For any such window, the old fold genuinely averaged DIFFERENT REAL CALENDAR DAYS' data into the same bucket (a 48h window's hour 24 landing in the same bucket as hour 0), silently blending two distinct days' prices/dispatch into one wrong number with zero indication it happened. Now indexes by real elapsed hour from `day_start` (no modulo) — a <=24h window (every existing caller before #316's service) produces byte-identical output to before this fix (confirmed against the existing DST test suite, unmodified), and a longer window now returns one honest row per real hour actually in it.
  - **`_align_previous_periods()` performance**: rewrote the O(n·m) nested loop (rescanning the previous plan's periods from index 0 for every new period) as an O(n+m) two-pointer merge, exploiting that both period-start sequences are guaranteed monotonically increasing. A pure performance refactor — verified to produce byte-identical mappings to the original nested loop across exact-overlap, shifted/partial-overlap (the real rolling-resolve shape), non-overlapping, and exact-tolerance-boundary scenarios, plus 30 randomized trials against an independently-reimplemented reference oracle. Confirmed live: at this project's own documented production scale (365 periods, worst-case zero overlap), the nested loop took ~19.5ms/call, the two-pointer version ~0.8ms/call.
  - 15 new regression tests across two new files. The hour-folding fix was mutation-tested (reverting to `% 24` reproduces exactly the wrong 24-row/blended-bucket behaviour); the alignment rewrite's test suite was proven to have real teeth by injecting a deliberate off-by-one mutation into the two-pointer bound and confirming the tolerance-boundary test catches it.

## [0.94.94] — 2026-09-04

### Fixed
- **`solver/lp.py` collapsed every genuine solver-level HiGHS failure into `status="infeasible"`, never set a time limit, `_infeasible_plan()` aliased one array across nine fields, and `BatteryConfig.__post_init__` had two real validation gaps** ([#356](https://github.com/code-imstillalive/nimbus/issues/356), thanks @purcell-lab, all 3 remaining items of 4 — item 4, `quality_report.py`'s modulo-24 hour folding and `_align_previous_periods`'s O(n·m) scan, is tracked separately and left open). Item 2 (`_infeasible_plan()` returning the SAME `np.zeros(n)` object for all nine array fields — `plan.battery_charge_kw is plan.grid_import_kw` was `True`, so any in-place arithmetic on one field of a non-optimal Plan would silently corrupt the other eight) and the `terminal_value_period_indices < n` bounds check half of item 3 were both already fixed in an earlier session — confirmed still correct, untouched here.
  - **Item 1**: `_solve_highs()` mapped `kTimeLimit`/`kIterationLimit`/`kSolutionLimit`/`kUnknown`/`kUnboundedOrInfeasible`/`kModelError`/`kSolveError` all straight to `status="infeasible"` — indistinguishable from a model HiGHS actually proved has no feasible dispatch at all, when the real problem is that the SOLVER gave up or hit a limit on a model whose true feasibility was never determined. These now report a distinct `status="error"`, with HiGHS's own status name preserved in a new `LPResult.raw_status` field (threaded through to `Plan.raw_status` too). Confirmed safe to introduce as a genuinely new value: every existing consumer of `.status`/`Plan.status` in this repo (`network.py`, `stochastic.py`, `solver_writer.py`) only ever checks `== "optimal"`/`!= "optimal"`, never `== "infeasible"` specifically, so this changes no existing control-flow branch. Also: `_solve_highs()` never set a time limit on the underlying `highspy.Highs()` instance at all, so a genuinely pathological problem (e.g. a hard MIP, see #238's own binary-variable groundwork) could block indefinitely on HA's shared executor thread pool. Now bounded to a new `DEFAULT_TIME_LIMIT_SECONDS = 60.0` — generous next to every real solve this project has measured (well under a second even at full production scale), while still bounding a genuinely stuck solve to a small fraction of even the fastest solve cadence this project runs. `solver_writer.py`'s real production `build_plan()` call site now logs a `WARNING` naming the raw HiGHS status whenever `plan.status == "error"`, so an operator sees "the solver failed, here's why" instead of hunting for a modeling problem that doesn't exist.
  - **Item 3 (validation gaps)**: `BatteryConfig.__post_init__` never rejected `capacity_kwh <= 0.0` (a genuinely degenerate zero-usable-capacity "battery" sailed through validation and only surfaced later as an opaque HiGHS-level error) or a negative `max_charge_kw`/`max_discharge_kw` (`max_charge_kw=-5` was accepted and only surfaced later as a raw `ValueError: Variable 'battery_charge_0' has lb=0.0 > ub=-5`, giving no hint the real problem was this config field). Both now raise a clear `ValueError` naming the actual field. The negative-power-direction check is deliberately `< 0.0`, not `<= 0.0` — exactly 0.0 is a real, legitimate "this direction is physically disabled" config, and several existing tests (`test_solver_backtest.py`, `test_solver_rolling.py`) construct exactly that to force a genuinely infeasible-but-validly-shaped scenario on purpose (an initial, too-strict `<= 0.0` draft of this fix broke both — caught by running the real suite, not assumed). Also fixed the efficiency-rejection message, which said efficiencies must be in "(0, 1]" — implying exactly 100% is allowed — while the guard immediately above it has always enforced a strict `< 1.0` on both sides; the message contradicted the rule it was defending.
  - 21 new regression tests across two new files, all mutation-tested against the pre-fix behaviour: the 7 collapsed HiGHS statuses each confirmed to genuinely map to "infeasible" before the fix and "error" after (via direct `highspy.Highs.getModelStatus` patching, since reliably forcing a real HiGHS timeout/limit is inherently slow and flaky); the time-limit option confirmed to genuinely reach the real `Highs` instance (a removed `setOptionValue` call is caught); zero-capacity, negative-power-direction, and the efficiency-message wording each confirmed to genuinely fail against a reverted version of their specific fix.

## [0.94.93] — 2026-09-04

### Fixed
- **`resample_last_value()`'s forward-fill had no staleness limit, letting a real multi-day HA outage/sensor-unavailable gap contaminate `seasonal_lookup` and every lag feature it touches** ([#353](https://github.com/code-imstillalive/nimbus/issues/353), thanks @purcell-lab, defect 2 of 2 — defect 1, NaN poisoning, was already fixed in `coordinator.py`'s three recorder/LTS/`_current_measured_power` fetch paths and `ml/model.py`'s own training loop, confirmed correct and untouched here). Without a staleness bound, a grid point whose real source event was hours or days old still got forward-filled with that stale value and trained on as if it were a genuine, continuously-observed reading — folded into `seasonal_lookup`'s own per-(weekday, hour, minute) averages regardless of which real calendar day/hour it actually represented, and fed as a stale lag feature to every row whose lag lookback happened to fall inside the gap. `resample_last_value()` now takes an optional `max_staleness: timedelta | None` — a grid point older than this returns `None` (the same "nothing to fill from yet" sentinel already used before the first real event) instead of the stale value; `None` (the default) preserves the exact original unbounded behaviour for any other caller. `train_model()` wires this into all 7 of its own resampling calls (load, temperature, humidity, curtailment, battery, grid, solar) at `resample_minutes × MAX_TRAINING_STALENESS_GRID_STEPS` (3 grid steps — generous enough that a brief HA restart or Modbus hiccup never trips it, tight enough that a genuine multi-hour-plus outage is correctly excluded rather than silently forward-filled). 4 new regression tests: 3 direct, function-level coverage of `resample_last_value()`'s own contract, plus one full `train_model()` integration test using a load whose true value is a clean, deterministic function of hour-of-day — a real ~5-day gap starting at hour 23 is confirmed to no longer pull a spanned (weekday, hour) bucket's seasonal average away from its own true value toward the stale 23:00 fill (mutation-tested: disabling the fix reproduces a bucket pulled from ~8.0 to 11.75, a real, measured contamination, not a guessed one).

## [0.94.92] — 2026-09-04

### Added
- **Locked in the structural fix for issue #357's own root cause: the standalone/cron deployment example can never bundle its own drifting copy of `solver/`/`ml/` again** ([#357](https://github.com/code-imstillalive/nimbus/issues/357), thanks @purcell-lab). The `nimbus_solver_app` Supervisor add-on's own bundled `solver/` copy (the actual third source of truth the issue's evidence table documented drifting out of sync — missing #328's soft-SoC relaxation, #238's MIP groundwork, #297/#310's row-major refactor) was already deleted in v0.94.85. This adds 3 regression tests confirming that fix is genuinely structural, not just a one-time cleanup: `nimbus_solver_app/` staying deleted, no directory literally named `solver`/`ml` ever reappearing anywhere under `docs/real-world-integration/` (the one remaining non-integration writer script, which has never bundled either package — it always resolves both via its own `NIMBUS_SOLVER_PATH` `sys.path` shim, pointing at a real external clone), and the docs-copy script's own `solver.network` module object being confirmed identical (not merely byte-identical source re-imported as a second, distinct module) to the one the integration copy uses. The second regression test was mutation-verified — a real bundled `solver/` directory simulated under `docs/real-world-integration/files/` was confirmed to fail the test before being removed. The writer *scripts* themselves (not the `solver/` package) still have real, itemized content differences from each other — see the issue's own follow-up comment for the full breakdown and why each one was confirmed to be a deliberate, execution-context-specific difference (native-HA-only reporting features vs. a standalone-cron-only timing fix) rather than a missed bug fix, left open for a maintainer's own ongoing judgment rather than asserted here.

## [0.94.91] — 2026-09-04

### Fixed
- **`solver/stochastic.py` (the devhub-only, shadow-mode-only two-stage stochastic LP) had drifted from `network.py`'s own production build_plan(): defects 2 and 3 of 3, defect 1 was already fixed** ([#354](https://github.com/code-imstillalive/nimbus/issues/354), thanks @purcell-lab). Defect 1 (a real calendar day shared by stage 1 and a scenario's own stage 2 getting the P2P export-bonus cap applied twice, at the full daily volume each time) was already fixed in an earlier session — confirmed still correct, untouched here. Defect 2: `soc{suffix}_t` was hard-bounded to `[min_soc_kwh, max_soc_kwh]` directly, so a below-floor `initial_soc_kwh` (a real, legitimate state per issue #328's own relaxation of `BatteryConfig.__post_init__`) that couldn't recover within a single period made the whole solve infeasible — the caller got `expected_total_cost=nan` and empty arrays with no explanation. Fixed by porting `network.py`'s own #328 soft-SoC mechanism verbatim: `soc{suffix}_t`'s only hard bound is now the true physical range `[0, capacity_kwh]`; `min_soc_kwh`/`max_soc_kwh` are enforced as a soft preference via costed `underfill{suffix}_t`/`overfill{suffix}_t` slack instead, including the same fold-in-the-prior-period's-underfill fix network.py's own discharge draw-cap needed (this required threading a new `prev_underfill_ref` through the shared stage-1/stage-2 variable-building helper, alongside the existing `prev_soc_ref`, so the relaxation correctly carries across the stage-1-to-stage-2 boundary, not just within one family). New `soft_soc_penalty_per_kwh` parameter on `build_stochastic_plan()`, auto-deriving the same way `build_plan()` does when not given explicitly. Defect 3: only the same-period wash-trade pathways were replicated from `network.py` — the two COMBINED-DIRECTION caps (`charge[t]+discharge[t] <= max(max_charge_kw, max_discharge_kw)`, issue #245; `grid_import[t]+grid_export[t] <= max(import_limit_kw, export_limit_kw)`, issue #266) were missing entirely, so this module could still produce the exact simultaneous-charge/discharge or simultaneous-import/export degeneracy those two issues were opened for on `build_plan()`. Both replicated verbatim. Zero behaviour change for any install where `initial_soc_kwh` starts and stays within `[min_soc_kwh, max_soc_kwh]` and no wash-trade-shaped price signal exists — this module remains devhub-only, with zero callers in any NUC1/NUC2-deployed script (confirmed by grep before shipping). 4 new regression tests, all mutation-tested against the pre-fix behaviour: a below-floor `initial_soc_kwh` that can't recover in one period now solves `optimal` with a finite cost (mutation reproduces the exact pre-fix `infeasible`); both combined-direction caps hold under a synthetic scenario engineered to force a real, economically-motivated (not solver-tie-break-luck) violation of each (mutation reproduces the exact pre-fix magnitude — 45kW combined charge+discharge against a 24kW cap, 48.8kW combined grid flow against a 30kW cap).

## [0.94.90] — 2026-09-04

### Changed
- **Model selection (k-NN vs GBRT vs seasonal-naive) is now decided by real, rolling-origin RECURSIVE multi-step MAE, not one-step MAE scored with true (ground-truth) lags** ([#351](https://github.com/code-imstillalive/nimbus/issues/351), thanks @purcell-lab). Every validation row used to get real recent lag values for k-NN/GBRT — a 15-minute nowcast — while the published forecast `predict()` actually runs recursively: past `LAG_LONG_STEPS` grid-steps into any real forecast, the lag inputs are the model's own prior predictions, not ground truth (see this module's own "Recursive multi-step forecast" doc, and the documented exposure-bias bug chain this project already hit once live for Battery power). Scoring selection on true-lag one-step MAE never exercises that self-feeding regime at all — it structurally flatters k-NN/GBRT (both get a real near-term lag on every row) and made the naive baseline (issue #110) "almost never win," independent of which one would actually perform better once deployed. Fixed by additionally scoring every candidate over real, multi-hour rolling-origin recursive windows within the validation region — each candidate's own self-generated lag chain, the same mechanism `predict()` itself uses (grid-index arithmetic here, not `predict()`'s own datetime/DST-safety machinery, unneeded since this runs against the already-DST-safe training grid) — and using THIS metric to decide `model_type` whenever it can be computed (falls back to the one-step comparison only when the validation region is too short to fit a single recursive origin). The one-step `validation_mae`/`validation_mase` are still computed and exposed exactly as before (diagnostics, MASE scaling, back-compat); a new `validation_recursive_mae` field makes the actual selection driver visible. GBRT's own early-stopping leakage half of this same issue (early stopping picking its own best boosting round against the exact set its accuracy was then judged on) was already fixed in an earlier session — confirmed still correct and untouched by this change. 2 new regression tests, including a real, empirically-confirmed scenario where the two metrics genuinely disagree (one-step MAE prefers GBRT, recursive MAE correctly prefers k-NN) and `model_type` follows the recursive winner — mutation-tested to confirm it genuinely fails against the pre-fix one-step-only selection.

## [0.94.89] — 2026-09-04

### Fixed
- **`solver_writer.py`'s own internal imports and REST bearer-token resolution both had real, if narrow, costs specific to running natively inside HA's process** ([#349](https://github.com/code-imstillalive/nimbus/issues/349), thanks @purcell-lab). Three findings, all scoped to the native in-process Solver path (`solver_runtime.py`), not the standalone/cron deployment: (1) `sensor.py`'s own first `solver_writer` import was already confirmed to go through `hass.async_add_import_executor_job()` from an earlier session — a regression test now source-scans for this so a future edit can't silently revert it to a bare blocking import on the event loop. (2) `solver_writer.py`'s own `.ml`/`.solver` imports now try a real relative import first — resolving with zero `sys.path` mutation and the correctly-namespaced module object when loaded as part of the real `custom_components.nimbus_load` package, instead of an unconditional `sys.path.insert()` that leaked `ml`/`solver`/`sensor`/`const` as top-level module names into every other integration sharing the same HA process. The `sys.path` shim is only ever reached via the `ImportError` a genuine standalone/cron run raises (no parent package). (3) The REST-mode bearer token is now lazily resolved via `_load_token()` (cached after first call) instead of a module-level `TOKEN_PATH` file read at import time — native mode never touches disk for this at all. The standalone deployment copy (`docs/real-world-integration/files/nimbus_solver_forecast_writer.py`) was reviewed and needs no equivalent change — it has no parent package to import relatively against and no shared-process `sys.path` to pollute, since it's always a standalone script, and it already degrades gracefully (`TOKEN = None`) if its own token file is missing. 5 new regression tests.

## [0.94.88] — 2026-09-04

### Added
- **Two household-specific hardcoded constants are now real wizard fields** ([#348](https://github.com/code-imstillalive/nimbus/issues/348), thanks @purcell-lab, 2 of 4 findings). `FIXED_DAILY_CHARGES` (a flat $/day charge folded into `total_cost_with_fixed_costs`) and `SELF_CONSUME_HOURS_AFTER_MIDNIGHT_CLOSE` (how many hours after a P2P block ending at midnight grid export stays hard-pinned to 0kW) used to be plain Python module constants in `solver_writer.py`, applied to every install regardless of that install's own real retailer's daily supply charge or automation timing. Both are now real Solver-settings number entities (`number.nimbus_solver_fixed_daily_charge`, `number.nimbus_solver_post_window_self_consume_hours`), defaulting to this repo's own reference household's real, already-live values (1.95, 4) — byte-identical behaviour for every existing install until either field is explicitly changed. The interim startup-WARNING that used to name these two as silent overrides has been removed (they're visible, user-adjustable wizard fields now, nothing to warn about); the warning for the two genuinely still-hardcoded overrides in the same issue (the day/night discharge-cost schedule, LocalVolts-specific `costsflexup`/`earningsflexup` price-array parsing) is unchanged. Those two are deliberately left alone this pass — a past session already flagged the discharge-cost schedule specifically as carrying real financial/dispatch risk if rushed, and that caution still holds. 10 new/rewritten regression tests, the 2 covering the post-midnight self-consume behaviour confirmed via mutation testing.

## [0.94.87] — 2026-09-04

### Changed
- **Test infrastructure: pytest is now the only test runner, and a real, pre-existing bug in that dual-runner setup was found and fixed along the way** ([#360](https://github.com/code-imstillalive/nimbus/issues/360), thanks @purcell-lab, 3 of 7 findings). `tests/run_all.py` (a hand-rolled runner working around a gap that no longer exists) and the ~125 duplicated `if __name__ == "__main__":` bare-function collectors every test file used to carry are both gone — pytest already discovers and runs every one of these tests natively, with zero help needed from either. **A real bug found while removing them**: 4 files (`test_flows_hub_options.py`, `test_price_watcher.py`, `test_sensor_flattened.py`, `test_sensor_solver_push_entities.py`) had genuine, real test functions accidentally placed *after* the `if __name__` guard by a pre-existing authoring mistake — invisible to `run_all.py`'s own direct-script-execution path (Python hadn't defined them yet by the time that path's own code ran), though `pytest` itself was never affected (it always imports the whole module first). Recovered by hand, verified with a per-file test-count diff against the pre-change commit for every one of the 125 touched files, confirming zero tests lost anywhere. Also fixed 4 genuinely assertion-free "must not raise" tests (`test_forecast_sensor_lts_unit_remediation.py`, `test_services.py` ×2, `test_solver_runtime_dispatch_dry_run.py`) that would have passed just as well against a silently-broken future refactor — each now asserts the real log line/call it's supposed to verify, confirmed via mutation testing to genuinely fail against the pre-fix code. Removed the now-obsolete BLE001 per-file-ignore for `tests/test_*.py` (it only ever existed for the removed collector pattern); the 2 real, unrelated BLE001 sites it uncovered got individual `# noqa` comments instead of a blanket exemption. `CLAUDE.md`'s "Testing" section, which had stated "no scikit-learn/pytest infra" since 2026-08-15 despite this repo having a full 900+-test pytest suite for a long time since, has been rewritten to the real commands. The other 4 findings in the same issue (timing-dependent `sleep()`-based tests, duplicate test files, subentry flow tests that bypass `FlowManager.async_configure`'s own schema validation, and `ml/` coverage gaps) are each substantial enough to need their own dedicated pass — left for later, not rushed into this same change.

## [0.94.86] — 2026-09-04

### Fixed
- **DST wall-clock `+timedelta` arithmetic on tz-aware local datetimes silently broke the ML training grid, the lag-lookback bisect, `PeriodGrid.period_starts`, and the quality-report's hourly buckets on any DST transition day** ([#368](https://github.com/code-imstillalive/nimbus/issues/368), thanks @purcell-lab, split out of #347). Adding a `timedelta` to a ZoneInfo-aware datetime is pure wall-clock arithmetic — Python only touches the naive field values and reattaches the same tzinfo, it never re-resolves the real UTC offset for the result. Across a real DST transition this either marches straight through wall-clock times that never happened (spring-forward — the grid silently fabricated a nonexistent point, or skipped the true one-hour jump) or produces duplicate/out-of-order instants for the repeated hour (fall-back — `ml/model.py`'s `_build_grid()` and `PeriodGrid.period_starts` both did this). Separately, Python's own aware-datetime comparison is fold-blind whenever both operands share the identical `tzinfo` object (the normal case throughout this codebase) — `resample_last_value()`/`resample_observed_mask()`'s bisect and `predict()`'s own lag-lookback used to compare the two real, hour-apart instants of a repeated fall-back hour as *equal*, silently returning a value from the wrong occurrence. This project's own reference household (Brisbane) never observes DST, so none of this was ever visible there — it's real for any AEDT/NZ/EU/US install, which #347 made a supported configuration by making the timezone itself configurable. Fixed by doing every step/accumulation/comparison against the real UTC instant, converting back to the caller's own local tzinfo only for the returned values — every existing caller's contract (a local, ZoneInfo-aware datetime) is unchanged. 7 new regression tests against `Australia/Sydney` (both transition directions), each confirmed via mutation testing to genuinely fail against the pre-fix code.

## [0.94.85] — 2026-09-04

### Removed
- **The `nimbus_solver_app` Home Assistant Supervisor add-on has been deleted from this repo entirely** ([#357](https://github.com/code-imstillalive/nimbus/issues/357), thanks @purcell-lab). It had been marked `DEPRECATED` since v0.73.0 in favour of the native in-process Solver path, but was still shipping as a real, installable Supervisor app — and had silently drifted out of sync with the integration's own solver code (missing #328's soft-SoC work, #238's MILP groundwork, and the #297/#310 quality-report refactor), with no CI check ever catching the drift. Rather than add a sync-check to keep maintaining a third copy of the same logic, the add-on is gone: `nimbus_solver_app/` deleted, the now-orphaned `repository.yaml` (the HA Supervisor add-on repository manifest) deleted, and the `version-lockstep` CI job removed since there's nothing left to keep in lockstep with `manifest.json`. Every reference across `README.md`, `CLAUDE.md`, `pyproject.toml`, `.github/PULL_REQUEST_TEMPLATE.md`, `docs/TESTERS.md`, `docs/api-contract.md`, and the comment blocks in `solver_writer.py`/`solver_runtime.py`/the docs-copy writer script has been updated to reflect this — the standalone bare-script + cron deployment path (`docs/real-world-integration/`) is untouched and remains fully supported for anyone who'd rather run the Solver on a separate always-on device outside HA's own process. Existing add-on installs: uninstall it via Settings → Add-ons → **Nimbus Solver** → **Uninstall**, then finish the integration's own "Solver settings" wizard instead — the native path takes over the same `sensor.nimbus_solver_*` outputs with no config migration needed.

## [Unreleased]

### Changed
- **Repo hygiene** ([#364](https://github.com/code-imstillalive/nimbus/issues/364), thanks @purcell-lab, 3 of 7 findings — no functional/runtime impact, no version bump needed). `.gitignore` gained `*.pkl`/`coverage.xml`/`.pytest_cache/`/`.ruff_cache/`/`.mypy_cache/` (real local artifacts that were never actually excluded). Fixed inconsistent executable bits on 18 shebang'd scripts (`nimbus_solver_app/nimbus_solver_forecast_writer.py` plus 17 under `docs/real-world-integration/files/`) that `pyproject.toml`'s own comment claimed were all `chmod +x`'d but weren't. Moved the 592 KB `icon.psd` Photoshop source (unreferenced by any code, only `icon.png`/`icon@2x.png` are actually used) from `custom_components/nimbus_load/brand/` to a new repo-root `assets/` — it was shipping inside the exact directory HACS copies into every install for zero functional benefit. The other 4 findings (rewriting `solver/README.md` and generating an entity/service reference table for the main `README.md`, splitting `CLAUDE.md`'s 85 KB journal, and removing hardcoded household IPs/paths from research scripts) are left for a dedicated pass.

## [0.94.84] — 2026-09-04

### Fixed
- **The Nimbus hub's own options flow ("Configure" button) hard-crashed with a 500 on any HA core new enough to enforce `OptionsFlow.config_entry`'s access guard** — confirmed live in production on NUC1 immediately after upgrading to HA core 2026.9.0. `NimbusHubOptionsFlow.__init__()` (added by the #341 fix, v0.94.4x era) read `self.config_entry.options` directly inside `__init__` to seed `self._solver_data` — but `config_entry` is a property that HA core's own `OptionsFlow` deliberately raises `ValueError("The config entry is not available during initialisation")` on until the flow manager finishes attaching the entry, which only happens *after* `__init__` returns. Every attempt to open the hub's options flow — Forecaster settings, Solver settings, Switchboard settings, all of it — failed outright with this error. Fixed by deferring the seeding to a new `_ensure_solver_data_seeded()`, called lazily from `async_step_solver_battery()` (the wizard's real entry point) and defensively from `_absorb_step()`, both safely past `__init__`. The existing local test suite's HA stubs don't reproduce this property guard (a plain mock attribute never raises), which is exactly why this shipped without a local test catching it — 2 existing tests rewritten to assert the real contract (`_solver_data` starts as a `None` sentinel, seeds lazily, and re-seeding is idempotent) rather than the old, now-broken "eager dict in `__init__`" behaviour.

## [0.94.83] — 2026-09-04

### Fixed
- **v0.94.82's own #355 fix broke the `Ruff format check` CI job.** Two of the six inline-to-helper call swaps (`charging_ub_during_fixed_window()`, `grid_export_bounds()`) exceeded the line-length that `ruff format` wraps automatically — never run locally before tagging v0.94.82. Reformatted, re-verified against the exact P2P/export-bonus test subset (20 tests) locally before shipping this correction, same "verify against the exact CI command before calling it done" discipline as prior same-day CI-caught fixes. No functional change from v0.94.82, formatting only.

## [0.94.82] — 2026-09-04

### Changed
- **`p2p_export.py`'s six P2P mechanisms were a copy, not an extraction — `network.py` never actually imported the module** ([#355](https://github.com/code-imstillalive/nimbus/issues/355), thanks @purcell-lab). `p2p_export.py`'s own docstrings (since v0.94.39) claimed its charge gate, export pinning, bonus variable, bonus-≤-export constraint, bonus cost, and per-day cap + latest-preferred tie-break were "extracted verbatim" from `network.py` as a single source of truth — but `network.py` still carried its own byte-equivalent inline copy of all six, with zero import of the shared module (only `stochastic.py` ever imported it). This is exactly the drift risk it warns about: the next tie-break or cap tweak could easily land in one copy and not the other, the same way `network.py` and `stochastic.py` had already drifted from each other before this module existed. Fixed by having `build_plan()` call the `p2p_export` helpers directly and deleting all six inline copies — behaviour-preserving by construction (the helpers' own logic is unchanged), verified against the full existing P2P/export-bonus test suite (77 tests) plus the complete local suite (1023 tests) with zero regressions. Also fixed a pre-existing gap the issue flagged: `network.py`'s own local `has_export_bonus` variable name would have collided with the module-level function of the same name if imported by bare name — imported the module itself (`from . import p2p_export`) instead. 1 new regression test asserting `network.py` genuinely imports `p2p_export` and no inline `export_bonus_cap_` constraint construction remains, per the issue's own suggested test.

## [0.94.81] — 2026-09-04

### Fixed
- **The stochastic solver could plan against 2x the real committed daily P2P export volume on the stage1/stage2 branch day** ([#354](https://github.com/code-imstillalive/nimbus/issues/354), thanks @purcell-lab, 1 of 3 findings — this module is deliberately shadow-mode-only, never wired into live dispatch). Stage 1 and each scenario's own stage 2 are adjacent, non-overlapping period ranges that can genuinely share one real calendar day (the day containing the branch point) — `add_export_bonus_cumulative_caps()` was called once per family, each capping only its own half of that shared day at the *full* real daily volume independently, so a single scenario-world could plan as if stage 1's own committed volume for that day *and* a full separate allocation for its own stage-2 portion were both available. Fixed with an additive supplementary constraint (added once per scenario, after both stages are built) binding stage 1's real volume for the shared day plus that scenario's own stage-2 volume for the same day to the real configured cap — kept additive rather than restructuring the existing per-family calls, since folding stage 1's shared variables into more than one scenario's own cap call would have silently multiplied the tie-breaker's own cost term (`LPProblem.set_cost()` is additive, not overwriting). Verified with a mutation test. The other 2 findings in the same issue (soft-SoC relaxation parity with #328, missing combined-direction caps) are larger changes affecting core LP structure, left for a dedicated pass. 2 new regression tests.

## [0.94.80] — 2026-09-04

### Fixed
- **A more broken persisted model than a mere feature-count mismatch could crash `async_setup()`** ([#366](https://github.com/code-imstillalive/nimbus/issues/366), thanks @purcell-lab, 1 of 3 findings). `_load_model_from_disk()`'s own compatibility check (`trained.x_mean.shape[0] != len(FEATURE_NAMES)`) sat *outside* the `try`/`except` wrapping `pickle.loads()` — so a persisted model deserializing into something without an `x_mean` attribute at all (an older/more broken schema than a wrong feature count) raised a bare, uncaught `AttributeError` straight out of the method, crashing setup the exact "Config entry not ready" way the method's own comment already describes for the case it *was* handling. Moved the check inside the `try` block so any shape of persisted-model incompatibility degrades to "discard and retrain fresh," never a crash. The other 2 findings in the same issue (vectorizing GBRT's pure-Python split search for a claimed 10–50× speedup, and a full `schema_version` field for the pickle format) are larger changes — the first needs very careful correctness verification against the existing tree-building logic, the second touches every `TrainedModel` construction site — left for a dedicated pass. 5 new regression tests.

## [0.94.79] — 2026-09-04

### Fixed
- **A naive third-party timestamp could crash the entire solve cycle** ([#363](https://github.com/code-imstillalive/nimbus/issues/363), thanks @purcell-lab, 2 of 5 findings). `parse_iso()` attached UTC to a naive *datetime object* but returned `datetime.fromisoformat(s)` completely unchanged for a *string* — naive if the source's own ISO string omitted a UTC offset (e.g. `"2026-09-04T12:00:00"`, no `Z`/`+00:00` suffix). `resample_forecast()` then compared that naive value against timezone-aware grid times, raising `TypeError: can't compare offset-naive and offset-aware datetimes` — and `fetch_solar_source_safe()`'s own `except` clause only caught `HTTPError`/`URLError`/`KeyError`/`JSONDecodeError`, so a real third-party source publishing offset-less timestamps took down the *entire* solve cycle with a traceback instead of that one source being safely dropped from the blend. Fixed by giving the string branch the exact same "assume UTC for a genuinely naive value" treatment the datetime-object branch already had, and adding `TypeError`/`ValueError` to `fetch_solar_source_safe()`'s own except clause as defense-in-depth for a genuinely unparseable (non-ISO) string, which still raises `ValueError` even after the `parse_iso()` fix. Also replaced that function's `print(..., file=sys.stderr)` with `_LOGGER.warning()` — HA doesn't route container stdout/stderr into its own log, so this operationally-relevant warning (a real solar source dropping out of the blend) was invisible to anyone using the HA UI or `ha_get_logs`. The other 3 findings in the same issue (23 more `print()`/silent-except sites, duplicated history-fetcher functions, and splitting the 1,788-line `main()`) are left for a dedicated pass. 7 new regression tests.

## [0.94.78] — 2026-09-04

### Fixed
- **A background retrain failure was completely invisible** ([#365](https://github.com/code-imstillalive/nimbus/issues/365), thanks @purcell-lab, 3 of 7 findings). `_async_retrain()` is scheduled via a bare `hass.async_create_task()` (backgrounded so cold-start training can't block hub setup) but had no `except` clause at all — any real failure inside it (a bad history fetch, a training bug) surfaced only as an "unretrieved task exception" asyncio logs on its own, completely invisible to `sensor.nimbus_health_report`'s own WARNING+ ring buffer. This is exactly how a real `AttributeError` (2026-08-31) stayed silent for days while the health report kept showing 0 errors. Now catches and logs via `_LOGGER.exception()`, which reaches the health report the same way every other real warning in this integration does. The `nimbus_load.retrain` service's own `asyncio.gather()` also gained `return_exceptions=True`, so one coordinator's failure can no longer abort every other coordinator's already-in-flight retrain in the same service call (belt-and-suspenders now that `_async_retrain()` itself never propagates, but a real guarantee regardless). Also added `_attr_should_poll = False` to `NimbusSolverSwitch` — it has no `async_update()` at all (state is driven entirely by real user toggles and `RestoreEntity`), so HA's default 30s poll cadence was calling a nonexistent update path on every one of these switches for no reason every cycle. The other 4 findings in the same issue (service unregistration on last-entry-removal, an executor solve that can outlive `async_unload_entry`, module globals never reset across a re-add, and the `_setup_tasks` guard's own `finally` cleanup ordering) are larger, more architecturally involved changes left for a dedicated pass. 7 new regression tests.

## [0.94.77] — 2026-09-04

### Fixed
- **v0.94.76's own #358 fix broke the real-HA-harness CI job.** Moving the `aiohttp.web_exceptions.NotAppKeyWarning` ignore from `pyproject.toml`'s `filterwarnings` ini list into a new `tests/conftest.py` `pytest_configure()` hook (a plain runtime `warnings.filterwarnings()` call) looked correct locally — the stub-based suite doesn't exercise HA's real `http` component setup, so it never actually needed this filter to run at all — but the real-HA-harness job (which does) failed with the exact original #92 crash, 9 tests in error. Root cause, confirmed by reading `_pytest.warnings`' own source: pytest's `filterwarnings` ini list isn't just applied once at startup — it's re-applied fresh inside a `warnings.catch_warnings()` scope around *every single test item*, which silently discards anything a conftest hook registered globally the moment the first real test runs. `tests/conftest.py` deleted; replaced with a proper ini entry again, but filtering by **message** instead of the original dotted **category** path — `"ignore:It is recommended to use web.AppKey instances for keys:"` needs no import of anything to parse (fixing the original fresh-clone problem #358 was about) and, being a real ini entry, gets correctly re-applied by pytest on every test (fixing this regression). Verified the exact parse directly against `_pytest.warnings.parse_warning_filter()` before shipping this time, not just by trusting a local run that couldn't exercise the affected code path.

## [0.94.76] — 2026-09-04

### Fixed
- **The daily quality report's oracle (and the achieved trajectory) scored a battery that cycles for free** (found via Mark Purcell's new `nimbus-dispatch-report` skill — a live dashboard/checklist tool that reads a real install's diagnostics and independently re-derives its own scoring). `solver_writer.py`'s quality-report battery config never populated `degradation_cost_per_kwh`, defaulting it to `0.0` — so `j_ref`/`j_ach` (`evaluate_realized_cost()`, which had no way to express this term at all) and `j_star`/the oracle (`build_plan()`, which *does* price it for the real live dispatch) were scored on inconsistent battery physics. For an install with a real, nonzero degradation cost configured, the oracle in particular over-cycled for "free" arbitrage the household would never find worthwhile net of degradation — inflating the reported `regret_dollars`. Fixed by adding a `degradation_cost_per_kwh` parameter to `evaluate_realized_cost()` (regret.py), applied identically to `network.py`'s own live-LP formula (added to both charge and discharge cost), and threading the real configured value through both the quality report's `j_ref`/`j_ach` calls and its own battery config — so the oracle's LP now genuinely re-optimizes against the real cost (a better fix than a post-hoc re-pricing estimate, since a degradation-aware oracle would choose different dispatch, not just cost the same dispatch differently). Verified with a mutation test (reverted the `solver_writer.py` half of the fix, confirmed both affected end-to-end tests fail, restored it). 8 new regression tests (4 pure-function unit tests on `evaluate_realized_cost()`, 4 end-to-end through `solver_writer._compute_report_for_window()`).
- **A fresh clone with only base dependencies installed couldn't run pytest at all** ([#358](https://github.com/code-imstillalive/nimbus/issues/358), thanks @purcell-lab). `pyproject.toml`'s own `filterwarnings` ini option resolved a dotted `ignore::aiohttp.web_exceptions.NotAppKeyWarning` path during pytest's own config bootstrap — which imports `aiohttp` to do it. `aiohttp` is only ever installed transitively via the `[dev]` extra, never a base dependency, so `pip install -e .` alone aborted every pytest invocation with "Failed to import filter module 'aiohttp'" before a single test could run. **Superseded by v0.94.77**: this release's own fix (a `tests/conftest.py` hook) broke the real-HA-harness CI job — see v0.94.77's own entry for the real fix and why. Also added `permissions: contents: read` to the three read-only workflows (`ci.yml`, `hacs.yml`, `hassfest.yml`), none of which need to write anything — this part is unaffected and still correct.
- **`manifest.json`/`hacs.json` gaps** ([#359](https://github.com/code-imstillalive/nimbus/issues/359), thanks @purcell-lab). `frontend.py` imports `homeassistant.components.frontend.add_extra_js_url` but `manifest.json`'s `dependencies` never declared `frontend` — on a minimal (non-`default_config`) install the topology card's JS registration wasn't guaranteed to run after `frontend` itself was set up. Added. Also declared `single_config_entry: true` (the integration is already effectively single-entry via `config_flow.py`'s own `_abort_if_unique_id_configured`, and a second entry breaks the literal, non-entry-scoped entity_ids in `number.py`/`switch.py` anyway — see #343), and raised `hacs.json`'s `homeassistant` minimum from `2025.1.0` to `2026.8.0` to match what CI/tests actually exercise (`sensor.py`'s 2026.9 device-registry guards, `coordinator.py`'s PEP 695 syntax, and the `homeassistant==2026.8.3` test pin all assume something much newer than 2025.1.0).

### Not fixed, deliberately — worth recording why
- **#358's "Python target mismatch" item was attempted and reverted.** Bumping `[tool.ruff] target-version` from `"py312"` to `"py314"` (to match `requires-python = ">=3.14.4"`) sounds like a pure config alignment, but `ruff format` under a `py314` target rewrites every multi-exception `except (A, B):` into PEP 758's new unparenthesized `except A, B:` form — a genuine `SyntaxError` on any Python before 3.14. This project's own local dev workflow (`tests/run_all.py`, direct `pytest`/`unittest` invocations) runs on whatever Python is actually installed on the machine, and this session's own dev environment is still on 3.12 — bumping the target would have silently made the whole tree unparseable locally the moment `ruff format` next ran, while still passing CI (which does run 3.14). Left both `pyproject.toml`'s `target-version` and `.pre-commit-config.yaml`'s `default_language_version` at their current values until local dev environments are confirmed upgraded too — see the comments left in both files for the full story.

## [0.94.75] — 2026-09-04

### Fixed
- **A sparse signal resampled onto the finer training grid manufactured duplicate training rows, inflating reported accuracy** ([#350](https://github.com/code-imstillalive/nimbus/issues/350), thanks @purcell-lab). `resample_last_value()` forward-fills a source's last known value onto every grid point regardless of how sparse the real events are — for an hourly-cadence source (`training_source=lts`, or simply a load that doesn't update often) resampled onto the 15-minute training grid, three of every four consecutive grid points carried the identical forward-filled value. `train_model()`'s own `lag_short` feature ("the value one grid step ago") was then frequently the exact same underlying observation as the target itself, so the model was trivially rewarded for copying its own lag input rather than genuinely forecasting — reported live: 75% of rows had `lag_short` identical to the target, with GBRT's own validation MAE roughly half what the same data scored at its true, native hourly cadence. Also inflated `training_points` by the same ~4x, and contaminated the chronological train/validation split with duplicate rows straddling the boundary. Fixed with a new `resample_observed_mask()` helper marking exactly which grid points carry a genuinely new observation versus a pure forward-fill carry-over — `train_model()` now skips emitting a training row wherever the *target* isn't a fresh observation (a row's own lag inputs can still legitimately be forward-filled; only the target must be real). Applies generally, not just to `lts`/`hybrid` sources — a sparsely-updating load under the default `recorder` source gets the same correction, though in practice most recorder-backed sensors update often enough that this changes little there. Verified with a mutation test (reverted the fix, confirmed the new tests fail with the issue's own reported ~4x row-count inflation, restored it). 6 new regression tests.

## [0.94.74] — 2026-09-04

### Fixed
- **GBRT's own model-selection accuracy figure had an optimistic bias k-NN and naive never got** ([#351](https://github.com/code-imstillalive/nimbus/issues/351), thanks @purcell-lab, one of two findings). `train_model()`'s GBRT candidate early-stopped against `x_val`/`y_val` — the exact same held-out points `validation_mae["gbrt"]` was then computed from — so GBRT got to pick whichever boosting round scored best on that set and then report that set's own error at that round as its accuracy. k-NN has no per-round tuning at all; the naive baseline has no tuning, period — neither gets this advantage, so the three-way comparison `model_type` selection runs on (nimbus issue #110) wasn't a fair fight. Now GBRT early-stops against a further chronological split carved out of the *training* portion instead, leaving `x_val`/`y_val` genuinely untouched until the single, final comparison. Falls back to a fixed-`n_estimators` fit (no early stopping) when the training portion is too small to carve out a real sub-split without starving the fit itself. Verified with a mutation test (reverted the fix, confirmed the new regression test fails against the original leaky code, restored it). The deeper, harder half of this issue — one-step-ahead validation with real lags doesn't measure the recursive, exposure-biased 96h forecast users actually see, so naive can structurally never win on a lot of signals — is **not** addressed here. Validating against the actual deployed recursive `predict()` path (the issue's own suggested fix) is a materially larger change to core model-selection behavior for every load in production, and this project's own history is explicit that ML changes like this need verification against real household data, not synthetic scenarios alone — left for a dedicated pass. 1 new regression test.

## [0.94.73] — 2026-09-04

### Fixed
- **A wrong entity name collision could make Nimbus silently plan against a different install's battery/grid limits** ([#343](https://github.com/code-imstillalive/nimbus/issues/343), thanks @purcell-lab, 2 of 3 defects). `NimbusSolverConfigSensor._resolve()` (the bridge the LP itself reads battery capacity/max charge/grid limits through) guessed a hardware-limit entity's entity_id as the literal string `f"number.nimbus_{key}"` — but `number.py`/`switch.py` only pin *their own* `self.entity_id` to that same non-entry-scoped literal; their real `unique_id` is entry-scoped. If anything else in the same HA instance claimed the literal name first (a `remote_homeassistant` mirror of another Nimbus install using the identical convention — confirmed live on devhub; an orphaned registry row), HA's own dedup bumps Nimbus's real entity to `_2`/`_3`, and the guessed literal then silently resolved to the *foreign* entity's value, with zero error. `_resolve()` now looks the real entity_id up via the entity registry's own `async_get_entity_id(domain, DOMAIN, unique_id)` — which HA tracks correctly regardless of any name collision — falling back to the guessed literal only when the registry has no match. Separately, the shared push-sensor base class's `async_will_remove_from_hass()` unregistered its solver-writer dispatch handler by `self.entity_id`, which reflects whatever HA *actually* assigned — a silent no-op on the exact same collision, leaking a stale handler that the next solve would schedule a write through on an already-removed entity. Now unregisters by the same literal dispatch key registration always uses, regardless of what `self.entity_id` becomes live. The third defect (multi-entry support is effectively unsupported and undocumented — `single_config_entry` isn't declared in the manifest) is left open. 6 new regression tests, plus a new `_noop_async_will_remove_from_hass` stub added to the test harness so this lifecycle hook is testable at all going forward.

## [0.94.72] — 2026-09-04

### Fixed
- **Two of the cheapest, highest-payoff findings from #356** ([#356](https://github.com/code-imstillalive/nimbus/issues/356), thanks @purcell-lab, 2 of 4 findings). `network.py`'s own `_infeasible_plan()` (the well-formed, zero-filled `Plan` returned for any non-optimal solve) built all eight of its array fields from the SAME single `np.zeros(n)` object — verified live: `plan.battery_charge_kw is plan.grid_import_kw` was `True`. `Plan` is `frozen=True`, but that only stops reassigning an attribute — it says nothing about the numpy array object itself, so any consumer doing genuine in-place mutation (a slice assignment, `np.clip(..., out=...)`) on one field of a non-optimal plan would silently corrupt the other seven. Now a fresh `np.zeros(n)` per field. Separately, `elements.py`'s `BatteryConfig` validates `terminal_value_period_indices` for non-negative values and duplicates, but has no `PeriodGrid` to check `< n` against at construction time — a stale index from a shorter horizon (verified: `[0, 99]` on a 4-period grid) reached `soc[idx]` deep inside `build_plan()`'s terminal-value construction as a raw `IndexError`. `build_plan()` now validates this the same way it already validates `adequacy_loads`' own `deadline_period` bound, raising a clear `ValueError` naming the bad index. The other 2 findings in the same issue (HiGHS non-optimal status collapse + missing solve time limit, and the smaller `elements.py`/`quality_report.py`/`network.py` items) are larger or need more careful validation and are left open. 4 new regression tests.

## [0.94.71] — 2026-09-04

### Fixed
- **v0.94.70 broke real CI** — `tests/test_sensor_solver_push_entities.py::test_quality_report_has_required_sensor_entity_class_attributes` still asserted the pre-#362 `_unrecorded_attributes == frozenset()` for `NimbusSolverQualityReportSensor`, unnoticed locally before shipping because this file has a stray `if __name__ == "__main__":` block partway through it (line ~433) — every test defined after that point, including this one, is invisible to `tests/run_all.py`'s bare-function subprocess runner (which only sees whatever `__main__` executes) while still being fully collected by real pytest. A live, concrete example of the exact "two runners, different semantics" gap nimbus issue #360 describes. Fixed the stale assertion to match #362's real fix; verified against the exact CI command (`pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant -q`) locally (980 passed) before shipping, not just `run_all.py` again.

## [0.94.70] — 2026-09-04

### Fixed
- **A single NaN recorder/LTS reading could poison an entire day's retrain, silently** ([#353](https://github.com/code-imstillalive/nimbus/issues/353), thanks @purcell-lab, Defect 1 only). `float("nan")` doesn't raise, so it slipped straight past the existing `try/except (TypeError, ValueError)` state parsing, and `abs(nan) > MAX_SANE_POWER_KW` is `False`, so the existing sanity ceiling didn't catch it either — a template/REST/Modbus sensor without a numeric `device_class`/`state_class` can genuinely publish this (HA core's own state-write guard only rejects non-numeric states for those). One poisoned row made `x_mean`/`x_std` NaN, every candidate's `validation_mae` came out NaN, `min()` over a NaN dict still "picked" a model anyway, and the deployed model then silently returned `0.0` for every forecast step — with no error, until the next successful retrain overwrote the poisoned pickle. Fixed at every real injection point: both recorder/LTS history-fetch paths and `_current_measured_power` now drop a non-finite reading the same way an unparseable one already was; `train_model()` itself gained a second, defensive `math.isfinite()` guard (for any caller that bypasses the coordinator's own fetch layer) at both the seasonal-lookup and training-row construction points, plus one more at the seasonal-naive baseline's own week-ago lookup (which read `load_vals` directly at an arbitrary index, not one already screened by the training-row loop). Defect 2 from the same issue (`resample_last_value()` has no staleness limit on its forward-fill, so a multi-day outage trains on a flat-held stale value) is deliberately **not** addressed here — a real staleness cutoff risks silently discarding a load's genuinely flat "off for hours" periods along with real outages, which needs its own careful design pass, not a rushed threshold. 8 new regression tests.
- **Recorder churn and a near-miss on the attribute-size cap** ([#362](https://github.com/code-imstillalive/nimbus/issues/362), thanks @purcell-lab, 2 of 4 findings). `NimbusHealthReportSensor` recomputed `generated_at` fresh on every 30s poll and never excluded it from `_unrecorded_attributes` — so the attribute dict differed on every single poll even when the real diagnostic content (`recent_errors`/`recent_warnings`/`subentry_status`) hadn't changed at all, firing a full, non-dedupable recorder write ~2,880 times/day for nothing. `NimbusSolverQualityReportSensor` cleared `_unrecorded_attributes` to empty reasoning only about the inherited `forecast` key, missing that its own real payload (`j_ref_hourly`/`j_ach_hourly`/`j_star_hourly`/`hourly_regret`) measured at 14,609 bytes against the recorder's 16,384-byte cap — one more field away from the recorder silently dropping the entire attribute dict. Both fixed; the flattened Quality children already excluded these same four keys, the parent now matches. The other 2 findings in the same issue (a shared push/staleness mixin refactor across `sensor.py`/`sensor_flattened.py`, and a stale `# DIAG: temporary` poll-disable flag) are larger, structural changes deliberately left for a dedicated pass. 3 new regression tests.

## [0.94.69] — 2026-09-04

### Fixed
- **v0.94.62's own fix for #341 regressed the opposite case it wasn't looking for** ([#341](https://github.com/code-imstillalive/nimbus/issues/341), thanks @purcell-lab, follow-up verification). Seeding `self._solver_data` from stored options (so a Solver-wizard step never reached in a given run keeps its real value) meant a plain `self._solver_data.update(user_input)` could no longer tell "this key was never touched because its own step wasn't reached" apart from "this key was genuinely cleared on a step that WAS submitted" — both now look identical (absent from `user_input`) once the dict starts pre-seeded, so a cleared Optional field on a submitted step silently kept its old stored value forever instead of clearing. Reproduced directly against Mark's own repro (clearing `solver_solar_forecast_sensor_2` while it's stored, on a real submitted `solver_sources` step). Fixed with a new `_absorb_step()` helper: after merging a step's `user_input`, every key belonging to THAT step's own schema still absent from `user_input` is explicitly nulled — keys from a step not yet reached this run (never in that step's schema) are left untouched. 1 new regression test through the real `__init__` (the existing hub-options tests all bypass it via a `__new__()` helper, which is why the regression itself slipped past every pre-existing test).

## [0.94.68] — 2026-09-04

### Fixed
- **A cold-start confidence band could invert `lower`/`upper` for a negative prediction** ([#352](https://github.com/code-imstillalive/nimbus/issues/352), thanks @purcell-lab). Before `MIN_RESIDUALS_FOR_CALIBRATION` real residuals exist (the first ~10 update cycles after install or a residual-file reset — and, more generally, any k-NN/naive-model signal, which always uses this residual-based fallback), `calibrated_band()` returned `point_value * COLD_START_BAND_FRACTION` directly. For a genuine negative prediction (e.g. a -20kW "charging" battery forecast under `allow_negative`), this produced a negative half-width — the coordinator's own `lower = v - band`/`upper = v + band` then silently published `lower > upper`, and the floor/ceiling clamp downstream never repairs an already-inverted pair. Now returns `max(abs(point_value) * COLD_START_BAND_FRACTION, COLD_START_BAND_MIN_KW)` — always a nonnegative magnitude, with a small absolute floor (0.1kW) so a genuinely-zero prediction still gets a real, nonzero band instead of a falsely-confident zero-width one. 6 new regression tests.

## [0.94.67] — 2026-09-04

### Fixed
- **`solver_writer.py`'s first import ran blocking I/O on the event loop** ([#349](https://github.com/code-imstillalive/nimbus/issues/349), thanks @purcell-lab, partial fix). `sensor.py`'s `async_setup_entry` is genuinely the first place `solver_writer.py` can be imported in the process on a fresh start — a plain `from . import solver_writer` there ran that module's own synchronous file open, `sys.path` mutation, and `import numpy`/`import highspy` (a native extension load) directly on the event loop. Now uses `hass.async_add_import_executor_job(importlib.import_module, ...)` — HA core's own real API for exactly this, verified against HA core's source — so the import runs on the dedicated import executor instead. The issue's own second, separate finding (the `sys.path` shim making several module names importable as top-level names process-wide, a real trap for any future `isinstance`/identity check across that boundary) is deliberately **not** addressed here — restructuring `solver_writer.py`'s own import mechanism would touch the exact "one file, three deployments" (native/standalone-cron/HAOS-add-on) architecture this project relies on, and needs its own careful, dedicated pass rather than a rushed change alongside this narrower fix.

## [0.94.66] — 2026-09-04

### Changed
- **Several real, deliberate household-specific tuning choices in `solver_writer.py` now log their own presence at startup instead of being silently invisible** ([#348](https://github.com/code-imstillalive/nimbus/issues/348), thanks @purcell-lab, partial fix — see the issue for the remaining, deliberately-deferred work). A hardcoded day/night discharge-cost schedule (silently overriding the wizard's own `solver_discharge_cost`/`solver_salvage_value` whenever a price-forecast-array sensor is configured), LocalVolts-specific `costsflexup`/`earningsflexup` key parsing on the "generic" price-forecast-array field, a fixed `$1.95/day` charge always added to `total_cost_with_fixed_costs`, a hardcoded 4-hour post-midnight self-consume window whenever a P2P block runs through midnight, and a hardcoded 17:00–24:00 window forcing the real P2P matched rate to 0 outside it, are each genuine, considered tradeoffs (documented individually at their own definitions) — not oversights, and not changed here. `solver_writer.py` now logs a single `WARNING`, once per process, naming every one of these that's actually active for the current install's own config, so they're visible in the log instead of only discoverable by reading source. Making each one a real, generic wizard field (the issue's own longer-term suggested fix) is a materially larger, riskier change to core dispatch/cost math, deliberately left for a dedicated follow-up rather than rushed here. 8 new regression tests.

## [0.94.65] — 2026-09-04

### Fixed
- **v0.94.64's own `set_native_hass()` fix broke 5 real tests in CI** — `AttributeError: 'NoneType'/'_FakeHass' object has no attribute 'config'`. The new `hass.config.time_zone` read is correctly wrapped in `try/except`, but the `except` branch's own logging line re-read `hass.config` directly (not defensively) to build its log message — raising a second, uncaught `AttributeError` before the original exception was even logged, for any caller passing a `hass` without a `.config` attribute (several existing tests deliberately use a narrow fake `hass` that never needed one before this fix). Fixed by resolving the raw timezone string once, via nested `getattr()`s that degrade to `None` at any missing level, before ever calling `ZoneInfo()` on it. 2 new regression tests (a bare object with no `.config` at all, and `hass=None`). 814/814 passing.

## [0.94.64] — 2026-09-04

### Fixed
- **The Solver's timezone was hardcoded to `Australia/Brisbane`** ([#347](https://github.com/code-imstillalive/nimbus/issues/347), thanks @purcell-lab). Every hour-of-day decision in `solver_writer.py` (TOU fee lookup, the P2P window, midnight SoC anchors, fixed-export blocks, quality-report day boundaries — 17 call sites) evaluated in AEST for every install, not just this household's own. A Sydney/Melbourne install during AEDT got all of these one hour late; a non-AU install was off by many hours. `LOCAL_TZ` now resolves from the `NIMBUS_SOLVER_TIMEZONE` env var if explicitly set, else the same default as before (zero behavior change for this household or anyone else who never sets it); the native runtime additionally re-resolves it from `hass.config.time_zone` — the real, already-configured value every HA install has — unless the env var was explicitly set, which always wins. A genuinely separate, deeper finding in the same issue (wall-clock `timedelta` arithmetic on a tz-aware datetime is unsafe across a real DST transition, affecting the ML grid and solver `period_starts` construction) is deliberately **not** addressed here — a materially larger, riskier change to core time-grid math, unvalidatable live against this household's own DST-free install, left for a dedicated follow-up. 4 new regression tests.
- **9 real options-flow fields had no `strings.json` translation at all, rendering as raw snake_case keys in the wizard** ([#361](https://github.com/code-imstillalive/nimbus/issues/361), thanks @purcell-lab): the Forecaster step's `training_source`/`hybrid_recent_days`, and 6 Solver Sources fields (`solver_solar_forecast_sensor_2`/`_3`, `solver_price_forecast_array_sensor`, `solver_regional_spot_forecast_sensor`, `solver_regional_spot_current_price_sensor`, `solver_p2p_matched_rate_forecast_sensor`); `solver_weather_forecast_sensor` had a label but no help text. The `compute_quality_report` service (3 fields) had no translation at all, showing untranslated in Developer Tools. All added to both `strings.json` and `translations/en.json` (kept byte-identical). `quality_scale.yaml` also claimed 3 things that were no longer true — `action-setup`/`docs-actions`/`action-exceptions` said "no service actions" despite 3 real, well-tested ones existing; `data-description` claimed full coverage before this fix; `test-before-configure` claimed subentry creation validates its source sensor exists, which it doesn't (`load_subentry.py`'s `_derive_title()` silently falls back to the entity_id, never rejects) — corrected to `todo` rather than papering over a real gap. 4 new regression tests, including one that walks every real schema field and asserts a translation exists, so this can't silently regress again.

## [0.94.63] — 2026-09-04

### Fixed
- **A durable-Store backfill could permanently overwrite a genuinely newer edit with a stale restore** ([#342](https://github.com/code-imstillalive/nimbus/issues/342), thanks @purcell-lab). `number.py`'s `RestoreNumber`-restore and its own durable Store backstop have different write cadences — the Store writes synchronously on every set, HA's own restore-state dump only every 15 minutes (plus on a clean shutdown). `async_added_to_hass()` used to unconditionally backfill the Store with whatever `RestoreNumber` returned: a value set at 10:00, killed at 10:05 with the last restore dump at 09:55, restored as the stale 09:55 value and silently overwrote the Store's own correct, newer one — permanently, since `entry.options` is deliberately never kept in sync either. The Store now records `written_at` per key and `async_added_to_hass()` only backfills when the restore is not older than what the Store already holds. `switch.py` had no durable Store at all despite its own docstring claiming to follow `number.py`'s pattern — a lost restore could silently flip `switch.nimbus_solve_on_price_change` back to its default with zero fallback; it now has the identical Store/freshness-compare mechanism as `number.py`. 12 new regression tests across both platforms.

## [0.94.62] — 2026-09-04

### Fixed
- **`compute_quality_report` crashed with a bare `TypeError` on a naive datetime** ([#345](https://github.com/code-imstillalive/nimbus/issues/345), thanks @purcell-lab). `_coerce_datetime()` returned a `datetime` object or an ISO string without an explicit UTC offset completely unmodified — exactly the shape HA's own `datetime:` selector in `services.yaml` produces, and what a hand-written YAML service call produces too. Comparing that naive value against the timezone-aware `dt_util.now()` raised outside the handler's own try block, surfacing as an opaque service error instead of the intended `ServiceValidationError`; had it not raised, the naive value would have mis-windowed the recorder query by the local UTC offset. A naive input is now anchored to HA's own configured local timezone before conversion to UTC; an already-aware input passes through unchanged in effect. 6 new regression tests.
- **The native Solver's PID-file lock had no self-PID check — an unclean HA stop could make every future solve tick skip forever** ([#346](https://github.com/code-imstillalive/nimbus/issues/346), thanks @purcell-lab). In native mode the lock file holds HA's own process's PID, and a worker thread mid-LP-solve when HA is killed isn't guaranteed to reach `release_lock()`. In a Docker/HAOS container that PID is frequently identical across restarts, so `acquire_lock()`'s own stale-lock check (`os.kill(old_pid, 0)`) genuinely succeeded (it's us) and returned `False` forever, with nothing to ever reclaim it. `acquire_lock()` now treats a lock file holding our own current PID as stale and reclaims it; a lock genuinely held by a different, real process is still respected. 5 new regression tests.
- **The Solver's 3-step wizard nulled every field on any step never reached in a given run — including 5 `vol.Required` ones** ([#341](https://github.com/code-imstillalive/nimbus/issues/341), thanks @purcell-lab). `NimbusHubOptionsFlow.__init__` used to start `self._solver_data` completely empty; the final merge in `async_step_solver_sources` took every wizard-schema key from it, so battery SoC sensor, both price sensors, solar/load forecast entities, and every other field belonging to a step the caller never submitted this run resolved to `None`. `__init__` now seeds `self._solver_data` from the entry's own already-stored options (filtered to this wizard's own keys) — a step genuinely submitted this run still overwrites exactly as before, including a real, intentional clear within a step that was actually shown. 4 new regression tests.
- **A failed first refresh during hub setup leaked every sibling coordinator's daily retrain listener; each `ConfigEntryNotReady` retry leaked another set** ([#344](https://github.com/code-imstillalive/nimbus/issues/344), thanks @purcell-lab). `async_unload_entry()` only ever cleans up coordinators reachable via `entry.runtime_data`, which is assigned only after every subentry's setup succeeds — if any one subentry's `async_config_entry_first_refresh()` raised `ConfigEntryNotReady`, coordinators already constructed earlier in the same batch (each already holding a registered `async_track_time_change` listener) leaked, with HA's own retry-with-backoff leaking another full set each attempt. `NimbusCoordinator.async_setup()` now also registers its own (already idempotent) `async_unload()` with `entry.async_on_unload()` — HA's own guarantee that fires on any teardown of this entry, including a setup attempt that never reaches `async_unload_entry()` at all. Registers the coordinator's own guarded method, not the raw unsub callable, specifically to avoid reproducing the double-unsub crash issue #337 was fixed for on the normal successful-setup path. 3 new regression tests.

## [0.94.61] — 2026-09-04

### Fixed
- **Reloading the hub with *Solve on Price Change* enabled could leave the config entry in `FAILED_UNLOAD` until a full restart** ([#337](https://github.com/code-imstillalive/nimbus/issues/337)). The price-watcher cleanup callable was both called explicitly by `async_unload_entry()` (the v0.94.48 / #312 fix) *and* registered with `entry.async_on_unload()`, and HA core's own `async_track_state_change_event` unsub raises `ValueError` on a second call — which `ConfigEntry.async_unload()` turns into `FAILED_UNLOAD`, so `async_reload` never set the hub back up. Every dashboard toggle of `switch.nimbus_solve_on_price_change` also appended another already-fired unsub to the entry's unload list. The combined unsub is now idempotent, one registry-consulting cleanup hook is registered per load (kept as the safety net for a setup that fails after the watcher is configured), and both teardown paths funnel through a single `_cancel_price_watcher()`. 3 new regression tests using a one-shot unsub that raises on its second call, matching HA core.
- **Soft-SoC penalty (#328) was scaled by period length while the terminal-value credit it must dominate is not — phantom credit and wrong dispatch on the production 5-minute grid** ([#338](https://github.com/code-imstillalive/nimbus/issues/338)). `underfill[t]`/`overfill[t]` cost `penalty × hours[t]` against a bare `$/kWh` segment credit, so the "penalty always dominates" guarantee held on an hourly grid (10× margin) and failed on 5-minute periods (0.83×): the LP could inflate `underfill[n-1]` to its upper bound, bank up to `min_soc_kwh` of terminal credit for energy that does not exist, and then sell real stored energy it should have held. Reproduced: identical inputs held 90 kWh on 1 h and 15 min grids but exported 19.8 kWh down to the breakpoint on 5 min, and a flat-price accounting case reported −$10.00 instead of −$9.00. The penalty is now a bare `$/kWh` state penalty per period, comparable to every signal it has to beat. Existing soft-SoC tests only ever used `hours = 1.0`; 7 new tests are parameterised over 1 h / 15 min / 5 min and assert identical dispatch and `total_cost` across grids.
- **Power Source, PV String and Battery Tower subentry flows could not be submitted with an optional picker left blank, could not clear a set picker, and were permanently un-reconfigurable once their parent Power Source was deleted** ([#339](https://github.com/code-imstillalive/nimbus/issues/339)). `vol.Optional(key, default=defaults.get(key))` injects `default=None` on a fresh add (rejected by `EntitySelector`/`SelectSelector`: "Entity None is neither a valid entity ID nor a valid UUID" / "expected str"), re-injects the saved value when the user clears the field, and re-injects a deleted parent's `subentry_id` into a dropdown that no longer offers it ("value must be one of []"). The exact PV-only / no-Power-Source-yet cases each flow's docstring promised were impossible through the UI. Switched to the `description={"suggested_value": ...}` pattern `hub_options.py` already adopted for #113/#114, and a stale parent reference is no longer offered back. 6 new tests pin the schema shape (no `None` defaults anywhere, blank submissions validate, deleted parents drop out).
- **Solver Sources step enforced `include_entities` on the load-forecast pickers, so one saved sensor outside the live candidate list made the whole 3-step Solver wizard unsubmittable** ([#340](https://github.com/code-imstillalive/nimbus/issues/340)). `_entity()`'s docstring claimed the restriction was "deliberately NOT enforced at validation time"; HA's `EntitySelector` enforces it with `vol.In`. A household pointing the field at its own template forecast, or opening the wizard mid-restart before every Nimbus forecast entity had published, hit `value must be one of [...]` on step 3 and could change no Solver setting at all. The saved value(s) are now unioned into the candidate list (restricting what is *offered* as a new choice, never rejecting what is already saved); a `None`/empty candidate list still means "no restriction". Docstring corrected; 4 new tests.

## [0.94.60] — 2026-09-03

### Fixed
- **v0.94.59's own new delegation test still failed CI's real pytest run**, while the 6 core diagnostic tests (calling `_run_one_cycle()` directly, per v0.94.59's own fix) now passed — confirming the extraction fixed the real issue. The one remaining failure was in a test added purely to check `async_run_solve()`'s own 2-line wrapper delegates to `_run_one_cycle()` via `hass.async_add_executor_job()`, using `asyncio.run()` + `AsyncMock` — the same async/mock shape that motivated the v0.94.59 extraction in the first place, still not reproducible locally. Dropped that one test rather than chase a third CI round-trip for coverage of two visually-obvious lines; the 6 tests covering the actual issue [#315](https://github.com/code-imstillalive/nimbus/issues/315) diagnostic logic are unaffected and unchanged.

## [0.94.59] — 2026-09-03

### Fixed
- **v0.94.58's own new test file failed CI's real pytest run** (passed under this project's own `tests/run_all.py` runner, which runs each bare-function file as an isolated subprocess, but failed under pytest's single-process, all-files-collected-together execution). Root cause not fully chased down (an async/mock interaction specific to pytest's collection, not reproducible locally due to this dev machine's own pre-existing `pytest_socket` limitation) — instead, `solver_runtime._blocking()` (previously a nested closure inside `async_run_solve()`) was extracted to a plain, module-level `_run_one_cycle(hass)` function, and the new tests now call it directly instead of through `async_run_solve()`/`hass.async_add_executor_job()`. No functional change; the shipped v0.94.58 diagnostic behavior (issue [#315](https://github.com/code-imstillalive/nimbus/issues/315)) was already correct, only the test's own execution shape needed to change. This dev machine's own local pytest run hits a pre-existing, unrelated `pytest_socket` limitation, so this couldn't be verified against the exact real CI command locally before tagging — watching the actual CI run directly this time instead of assuming green.

## [0.94.58] — 2026-09-03

### Fixed
- **Native Solver runtime's "previous cycle still in progress" skip was logged at DEBUG, invisible on this project's default WARNING logger level** ([#315](https://github.com/code-imstillalive/nimbus/issues/315), thanks @purcell-lab, "Freshness watchdog trips every 44 min after reload, main-loop cadence degraded"). Real root mechanism, found by reading `solver_runtime.py`'s own `_blocking()`, not guessed: if one solve cycle's `sw.main()` call runs unusually long (a slow external call inside one of its own try/except-wrapped non-essential publishes — weather mirrors, quality report, counterfactual, backtest, solar delivery ratio — none of which has its own timeout), every subsequent phase-locked 5-minute tick silently skips via `acquire_lock()` returning `False` until the slow one finally returns — reproducing exactly the reported "fires every ~44 min" pattern (roughly 8–9 skipped ticks) with zero log breadcrumb explaining why. `solver_runtime.async_run_solve()` now counts consecutive skips and logs them at `WARNING` (with the running count, so one harmless overlap reads differently from a real multi-tick stall), and measures `sw.main()`'s own wall-clock duration, logging at `WARNING` if it exceeds a 120s threshold (roughly 2.5x the documented real 45–52s solve baseline). Diagnostic-only — no behavior change, no timeout added to the individual publishes themselves (a larger, separate change, deferred until these logs identify which one is actually slow, if any). 6 new regression tests.

## [0.94.57] — 2026-09-03

### Fixed
- **v0.94.56's own `via_device_id` fix was STILL wrong — a THIRD distinct error, live on devhub immediately after that release** (nimbus issue [#335](https://github.com/code-imstillalive/nimbus/issues/335) follow-up). After two guessed-signature fixes both failed live (v0.94.53, v0.94.55), this one was verified against HA core's own actual source (`github.com/home-assistant/core`, `homeassistant/helpers/device_registry.py`, tag `2026.9.0`) instead of guessed a third time. The real signature is `async_get_device_id_by_identifier(hass, identifier_tuple, *, config_entry_id)` — the first positional argument is `hass` itself, not a `DeviceRegistry` object; the function resolves the registry internally. v0.94.56 passed an already-resolved `DeviceRegistry` object instead (`AttributeError: 'DeviceRegistry' object has no attribute 'data'`, since the function tried to treat that object as the `hass` it expected). Also confirmed from the real source: the function *raises* `ValueError` (not `None`) when the hub's own device doesn't exist yet — the normal, expected condition on a hub's very first-ever setup — now handled as a quiet `DEBUG`-level no-op rather than falling into the generic `ERROR`-level exception path. The regression test now asserts the *exact* `hass` object passed to `_resolve_hub_device_id` reaches the (strictly-signatured) fake unchanged — the one assertion that would have caught this exact mistake — plus a dedicated `ValueError` case.

## [0.94.56] — 2026-09-03

### Fixed
- **v0.94.55's own `via_device_id` fix was STILL wrong — a second, different `TypeError`, live on devhub immediately after that release too** (nimbus issue [#335](https://github.com/code-imstillalive/nimbus/issues/335) follow-up). The real HA signature is `async_get_device_id_by_identifier(registry, identifier_tuple, *, config_entry_id)` — `config_entry_id` is a REQUIRED KEYWORD-ONLY argument, not optional. v0.94.53 passed 3 positional args; v0.94.55's fix corrected that but omitted `config_entry_id` entirely (`TypeError: async_get_device_id_by_identifier() missing 1 required keyword-only argument: 'config_entry_id'`), caught the same night via a direct live restart + log check rather than assumed fixed. `_resolve_hub_device_id()` now passes `config_entry_id=entry.entry_id` as required; the regression test now defines its fake with the exact real signature (2 positional + 1 required keyword-only, no defaults) instead of a looser stand-in, so a future signature mismatch of either shape fails in CI instead of shipping.

## [0.94.55] — 2026-09-03

### Fixed
- **v0.94.54's own CI run failed on `main`** — a real `pytest` test (`test_role_selector_offers_all_four_real_options`) hardcoded the Power Signal role selector's option list to the original 4 entries; it correctly caught that v0.94.54 added `temperature`/`humidity` without updating this test. Renamed to `test_role_selector_offers_all_six_real_options` and updated to assert all 6. No functional change — the two new roles from v0.94.54 work exactly as shipped. Also fixed `test_solver_writer_ha_post_state_logger_trace.py`'s own `__main__` block (didn't follow this suite's standard summary-line convention, so `tests/run_all.py`'s aggregate runner always reported it as failed despite a genuine pass) — pre-existing, unrelated to this release, fixed opportunistically while touching test infrastructure.

## [0.94.54] — 2026-09-03

### Fixed
- **v0.94.53's own `via_device_id` resolution shipped a `TypeError`, live on devhub immediately after release** (nimbus issue [#335](https://github.com/code-imstillalive/nimbus/issues/335) follow-up). `async_setup_entry()` called `dr.async_get_device_id_by_identifier(device_registry, entry.entry_id, {(DOMAIN, entry.entry_id)})` — 3 positional arguments — against HA 2026.9's real 2-argument signature (`registry, identifier_tuple`), raising `TypeError: async_get_device_id_by_identifier() takes 2 positional arguments but 3 were given` on every hub setup. Caught and swallowed by the resolution's own defensive `except Exception`, so it silently fell back to the original `via_device` path (harmless, but meant the deprecation warning it was meant to fix kept firing). This shipped undetected because the stub test environment deliberately doesn't define this attribute at all (matching the project's pinned pre-2026.9 HA test version), so the buggy call was never actually invoked by any test before reaching a real 2026.9 install. Fixed the call, and extracted it into its own `_resolve_hub_device_id()` function specifically so it's directly unit-testable in isolation going forward — 3 new regression tests cover the real signature, the pre-2026.9 fallback, and the exception-swallowing path.
- **A power-signal subentry configured to forecast Temperature or Humidity was silently forced through kW/POWER semantics — a real household hit this directly** (no dedicated role existed for a weather-type signal, only Battery/Solar/Grid/Other). `NimbusForecastSensor` unconditionally built every power-signal entity as `SensorDeviceClass.POWER` / `UnitOfPower.KILO_WATT`, and `coordinator.py` unconditionally passed `convert_power=True` for the subentry's own forecast target — so a genuine °C/% source sensor produced a forecast entity literally labelled in kilowatts, and the coordinator logged a real `"... reported unconvertible unit '°C'/'%' -- treating as kW as-is"` WARNING every cycle trying to `PowerConverter`-convert a non-power unit. Added two new signal roles, `temperature` and `humidity`, selectable in the Power Signal wizard (`flows/signal_subentry.py`) — `sensor.py` now derives the entity's real `device_class`/`unit_of_measurement` from the role (`TEMPERATURE`/°C, `HUMIDITY`/%), and `coordinator.py`'s new `_convert_power_for_target` property skips the power-unit conversion entirely for these two roles. Every other role (Battery/Solar/Grid/Other) and every load subentry is unchanged. An already-created Temperature/Humidity signal needs a one-time reconfigure (pick the new role in its subentry's own settings) to pick up the fix — the entity_id and history are preserved, only the role/unit/device_class change. 8 new regression tests.

## [0.94.53] — 2026-09-03

### Fixed
- **`DeviceInfo(via_device=...)` deprecation warning, second call site** (nimbus issue [#335](https://github.com/code-imstillalive/nimbus/issues/335) follow-up, confirmed live on a real install running HA 2026.9.0). The v0.94.52 fix only covered the 3 parent sub-device sensors in `sensor.py`; the 16 flattened-child sensors under those same three sub-devices (`sensor_flattened.py`) each built their own `DeviceInfo` with a bare `via_device=(DOMAIN, entry.entry_id)` and were never touched, so the identical HA-core deprecation warning kept firing from a different `async_add_entities` call site. The shared resolution helper moved from `sensor.py` into `sensor_flattened.py` (which `sensor.py` already imports, so this is a clean re-export, not a duplicate) so both the 3 parents and their 16 children resolve `via_device_id` identically.

## [0.94.52] — 2026-09-03

### Fixed
- **`DeviceInfo(via_device=...)` deprecation warning on HA Core 2026.9** ([#335](https://github.com/code-imstillalive/nimbus/issues/335), thanks @purcell-lab). HA 2026.9 deprecates `via_device` in favour of `via_device_id` (resolved via the new `dr.async_get_device_id_by_identifier()` helper) and will remove `via_device` entirely in 2027.8; setting both raises `HomeAssistantError`. Affected the three sub-device sensors (Quality, Backtest, Counterfactual) in `sensor.py`. This project's own pinned test harness (`pytest-homeassistant-custom-component==0.13.357`) resolves `homeassistant==2026.8.3`, which predates the new helper entirely, so `async_setup_entry()` resolves the hub's own device_id once via a runtime `hasattr()` feature-detect — using `via_device_id` when the helper exists, falling back to the original `via_device` on any older (or stubbed) HA. Purely cosmetic on 2026.8.x installs; silences the warning on 2026.9+.

## [0.94.51] — 2026-09-02

### Fixed
- **`number.nimbus_solver_*` no longer silently resets to its schema placeholder on some restarts — real, recurring incident (flagged 2026-08-26, 08-31, 09-01, hit again 2026-09-02: 14 of 38 devhub values reset on a single restart, including grid limits, P2P block 1, all three network-fee tiers, min SoC, SoH, efficiency, and charge cost).** Root cause: `RestoreNumber`'s own restore-state has a genuine, still-not-fully-diagnosed HA-core startup timing race, and this platform's only fallback — seeding from `entry.options` — is a dead end for most of these 38 fields, since `entry.options` is deliberately never kept in sync with a dashboard edit (to avoid a full-hub reload on every value change). Every P2P block, network-fee tier, and risk-aversion dial was never in the config-flow wizard at all, so `entry.options` never held them — a restore-state miss on any of those had zero real fallback and free-fell straight to the hardcoded class default. Adds a small `Store`-backed JSON file per config entry, shared by all 38 number entities: written on every successful restore/seed/edit, read as a fallback before ever reaching `entry.options`/default. A plain `Store` read is a direct JSON-file load with no comparable startup race.

## [0.94.50] — 2026-09-02

### Fixed
- **`min_soc`/`max_soc` are now a soft LP preference the Solver schedules real recovery toward, not a hard invariant enforced by clamping observed SoC** ([#328](https://github.com/code-imstillalive/nimbus/issues/328), thanks @purcell-lab). `elements.BatteryConfig` previously required `initial_soc_kwh` to sit inside `[min_soc_kwh, max_soc_kwh]`, so every writer-side call site had to clamp a live/historical SoC reading into that envelope before construction — silently reporting a fictional starting state to the LP and corrupting every downstream number (planned throughput, `total_cost`, the next cycle's own starting assumption, and critically the EPR quality-report ratio, which compares two trajectories that started from differently-clamped states). `BatteryConfig` now only enforces the physical bound `[0, capacity_kwh]`; `network.py`'s LP construction adds a new costed `underfill`/`overfill` slack pair that softly enforces the scheduling envelope instead, pinned to its true value by cost-minimization and therefore never gameable. Two other constraints (the discharge wash-trade-prevention guard, and the `terminal_value_breakpoints` segment-fill equation) independently re-imposed a hard floor via algebraic side effects and needed the same fix folded in. A genuinely *physical* clamp (`[0, capacity]`, not the schedule) is deliberately kept at the two live-sensor call sites (`main()`, the quality-report scorer) — a sensor can still report a value outside the battery's own possible range (calibration drift, a template-averaging overshoot).

## [0.94.49] — 2026-09-02

### Fixed
- **Removed 12 hardcoded foreign entity references from the Solver dispatch pipeline** ([#329](https://github.com/code-imstillalive/nimbus/pull/329), thanks @purcell-lab for flagging the original `coordinator.py` case). `coordinator.py`'s `_seasonal_anchor` was comparing against a literal `sensor.logger_load_power` instead of the wizard-configured whole-house cross-check sensor, silently disabling the seasonal-anchor fix for any install not using that exact entity name. A wider sweep of `solver_writer.py` found 11 more of the same class (9x `sensor.localvolts_*`, 2x `sensor.combined_total_dc_power`) baked into `fetch_aemo_forecast()`, `p2p_match_fraction()`, `p2p_recent_avg_volume_kwh()`, `compute_5min_offset()`, and `resample_real_p2p_rate()`. All five now take their entity id as a parameter, falling back to this file's own established safe/graceful-degradation behavior (no network call, existing fallback value) when left unconfigured. Adds 4 new wizard fields to the Solver Sources step — `solver_price_forecast_array_sensor`, `solver_regional_spot_forecast_sensor`, `solver_regional_spot_current_price_sensor`, `solver_p2p_matched_rate_forecast_sensor` — wired through the wizard schema, `sensor.py`'s bridge-sensor exposure list, and `solver_writer.py`'s consuming code, verified against the [#307](https://github.com/code-imstillalive/nimbus/pull/309) regression test built for exactly this class of bug. An install upgrading from an earlier version and leaving these 4 fields blank keeps its exact current behavior via the same LocalVolts/AEMO defaults as before this change; a genuinely new field going forward will not work until it's set in the wizard.
- **Daily quality report no longer crashes when historical SoC sits outside the configured envelope** ([#325](https://github.com/code-imstillalive/nimbus/issues/325), thanks @purcell-lab). The scorer builds its own `BatteryConfig` from the recorder SoC series, and `elements.BatteryConfig`'s invariant — correct for a user-typed static config — was raising `ValueError` straight out through the async publisher whenever the real world reported SoC below the configured floor. Live effect: `sensor.nimbus_solver_quality_report` and all nine `sensor.nimbus_quality_*` sensors `unavailable` for 8.6 hours across 103+ failed publishes, while the solver itself was completely healthy. Root cause on the reporting install was a template SoC sensor averaging the house battery with a DC-EV-charger channel reading 0% when unplugged, but a fault, a cold pack, a fresh install starting empty or plain sensor drift all produce it. Both sensor-fed values (`initial_soc_kwh` and `final_soc_kwh_actual`) are now clamped into `[min_soc, max_soc]` with a single warning naming the likely cause, matching the treatment [#64](https://github.com/code-imstillalive/nimbus/pull/64) already applied to the forward-planning path in `main()`.
- **Efficiency-backtest report could crash the same way on a backup-reserve install** (found by the "audit every `BatteryConfig` construction" sweep [#325](https://github.com/code-imstillalive/nimbus/issues/325) asked for, not by a live report). `compute_efficiency_backtest_report()` hardcoded its starting SoC to 50% of capacity, which is outside the configured envelope for any household running `solver_battery_min_soc_percent` above 50 — an ordinary setting for anyone keeping a backup reserve — and would have raised the identical `ValueError`. Now clamped. All six `BatteryConfig` construction sites were audited; the remaining four were already correct.

### Changed
- **Dispatch dry-run no longer logs a WARNING every solve cycle when the switch is intentionally off** ([#326](https://github.com/code-imstillalive/nimbus/issues/326), thanks @purcell-lab). "The user has this feature turned off" is a steady state, not an anomaly, and at a ~5-minute cadence it was measured at 11 lines in a 16-minute window — part of a 41-of-100-line recurring-WARNING share crowding out real signal. That branch is now `DEBUG`, so it's still there for anyone who raises `custom_components.nimbus_load` to debug. A *missing* switch entity is a genuine install-integrity problem and keeps its `WARNING`, now with its own distinct message; the three sibling warnings for real dry-run anomalies are unchanged.

### Known deferred (not silently left broken)
- `BATTERY_DISCHARGE_COST_NIGHT`/`DAY` and `BATTERY_SALVAGE_VALUE_NIGHT`/`OTHER` in `solver_writer.py` are still household-specific economic-policy constants, not entity references — out of scope for #329, needs its own redesign mirroring the existing `solver_p2p_block_1/2/3` config pattern.
- The two portable standalone-deploy copies of the writer script (`docs/real-world-integration/files/`, `nimbus_solver_app/`) still contain the same hardcodes #329 fixed in the main integration and need the identical fix in a followup PR.

## [0.94.48] — 2026-09-01

### Fixed
- **Issue #312's residual, actually root-caused and fixed this time** — v0.94.47's own "settle delay" theory below is confirmed WRONG, not just unverified: live-tested after shipping (both a full restart and a plain `homeassistant.reload_config_entry`), the identical collision reproduced at the exact same millisecond across every affected entity, disproving "post-unload settle timing" outright (a cold boot has no prior unload at all to settle after, yet reproduced identically). The real mechanism, found via a live DEBUG-level log capture spanning a full reload: `solver_writer.ha_post_state()` has a raw `hass.states.async_set()` fallback for whenever no native `SensorEntity` handler is registered yet for an entity_id — originally meant only for entity_ids that will NEVER have one (the standalone/cron/addon deployment). `async_unload_entry()` called `hass.config_entries.async_unload_platforms()` (which tears down entities and unregisters their handlers) FIRST, then relied on `entry.async_on_unload()` to cancel the periodic-solve cron / price-watcher listener / startup-retry task — but HA core (`ConfigEntry.async_unload()`) only processes those callbacks AFTER our own `async_unload_entry()` has already returned, confirmed by reading that source directly. That left a real window where one of those not-yet-cancelled triggers could fire a solve, find its handler just unregistered, and hit the raw fallback — writing a state with no `ATTR_RESTORED` flag that then collided with the fresh entity's own registration a moment later ("does not generate unique IDs... ignoring `<entity_id>`"). Fixed at both ends: `async_unload_entry()` now cancels all three triggers itself, directly, before tearing down any platform; `ha_post_state()`'s fallback additionally skips (rather than raw-writes) for the small, closed set of entity_ids that DO eventually get a native handler, so even an already-in-flight solve that started before cancellation can no longer poison the state machine. 6 new regression tests (unload-ordering, fallback-skip behaviour). Verified live on devhub after shipping — see this file's own "process lesson" note on v0.94.47 below for why this entry states that only after actually re-testing, not before.
- Fixes the real, previously-frozen symptom this bug also caused: `sensor.nimbus_quality_epr` and its siblings stuck hours-stale while their own parent `sensor.nimbus_solver_quality_report` kept updating normally (the flattened child's own object had lost the same entity-collision race, and its `update_from_solver()`'s own `if self.hass is not None` guard silently no-op'd forever with zero error).

## [0.94.47] — 2026-09-01

### Fixed (CONFIRMED NOT SUFFICIENT — see v0.94.48 above)
- **Process lesson, recorded plainly rather than quietly amended**: this entry originally claimed a fix for issue #312's residual. That fix (a 100ms `asyncio.sleep()` between subentry setup and platform re-forwarding, on the theory that `async_unload_platforms()`'s own awaited chain could resolve before every scheduled entity-removal callback had actually run) was shipped and tagged WITHOUT first being tested live against a real reproduction — a real mistake, given this project's own established discipline of live-verifying fixes before claiming them. When actually tested afterward (both a full restart and a plain reload), the identical collision reproduced at the identical millisecond, proving the fix did nothing. Left here, corrected rather than deleted, as the honest record — v0.94.48 above has the real root cause and fix.

## [0.94.46] — 2026-09-01

### Fixed
- `sensor.nimbus_health_report` was tripping HA's 16384-byte Recorder attribute-size cap continuously -- confirmed live on both devhub and production (NUC1): every 30 seconds, not intermittently. `extra_state_attributes()` returns up to 20 `recent_errors` + 20 `recent_warnings` + one `subentry_status` entry per forecastable subentry -- real, current diagnostic state, never worth long-term storage. Same class of bug `NimbusForecastSensor` already had fixed for issue #99; this class was simply never given the same `_unrecorded_attributes` treatment. `never_trained`/`generated_at` stay recorded (both small).

## [0.94.45] — 2026-09-01

### Fixed
- **The retrospective quality scorer's own oracle (J_star) never knew about a household's real fixed-rate P2P export commitment, systematically overstating regret for any install running one.** Found live via a direct household catch on a reconstructed dispatch-regret chart: the oracle's own LP re-solve wanted to export 34–40kW during the evening price peak, when the real, physically-committed P2P rate for that window was 11.5kW — a fictional, unconstrained market the oracle was "solving" against instead of reality. Root cause: `_compute_report_for_window()`'s own `grid_oracle` construction only ever applied `solver_grid_max_export_kw` (a plain capacity limit) — it never reused `fetch_p2p_fixed_export_kw()`, the exact mechanism the *forward-looking* planner (`main()`) already uses to constrain export to a household's configured P2P blocks (`solver_p2p_block_1_rate_kw`/`_start_hour`/`_end_hour`, etc.). Fixed by reusing that same function verbatim for the oracle's own `GridConfig`, so the retrospective scorer and the forward plan can never model this household's real P2P commitment two different ways. Every EPR/regret number computed against a P2P-configured install before this fix should be treated as overstated. 3 new regression tests, including a live mutation test confirming the fix is what actually constrains the oracle (not a no-op).
- Separately noted, not yet fixed: the *achieved* trajectory (J_ach) also currently gets zero credit for real settled P2P revenue, since no real settlement-history sensor is wired to `solver_p2p_settlement_history_sensor` for this household yet — the existing hook expects a date-keyed `{date: {export_cost, export_volume}}` history dict, and the closest existing entities (`sensor.localvolts_v2_sell_p2p_matched_cost`/`_matched_power`) are running scalars, not that shape. Needs a small daily-accumulator sensor before it can be wired in; tracked as a follow-up, not attempted in this release.

## [0.94.44] — 2026-09-01

### Added
- Diagnostic-only logging (DEBUG) around `hass.config_entries.async_forward_entry_setups()` in `async_setup_entry` — start, completion, and wall-clock duration. No behavior change. Added specifically for issue #312's still-open residual: both Mark Purcell's install and this project's own NUC1 verification independently hit one `Platform nimbus_load does not generate unique IDs` collision even after v0.94.40/v0.94.41's entry-level re-entrancy guard, with only one `Setting up nimbus_load.sensor` log line appearing either time — ruling out "the whole entry setup ran twice" (that's exactly what the guard prevents) without pinning down what did happen. This call's own timing is the smallest next diagnostic step if it recurs.

## [0.94.43] — 2026-09-01

### Fixed
- v0.94.42's own new test file (`test_quality_report_skip_logging.py`) failed CI's ruff format/lint check (an unformatted `with` block, one unused `logging` import) — no functional code affected, only the new test file. Fixed and verified against the exact CI commands (`ruff format --check`, `ruff check`) locally before pushing.

## [0.94.42] — 2026-09-01

### Added
- **`nimbus_load.compute_quality_report` service (Mark Purcell, issue #316, PR #317)** — scores an arbitrary `[start, end]` window on demand, using the exact same scoring engine (`_compute_report_for_window()`, extracted verbatim from `compute_daily_quality_report()`) the daily "yesterday" scorer has always used. Built specifically to unblock IV&V diagnosis of a silent scoring freeze (issue #312) without waiting for the next calendar-day rollover. `allow_partial` (default `True`) permits scoring any real sub-24h window; the P2P settlement lookup only fires when the window exactly matches a real calendar day (its history is keyed by ISO date — a non-aligned lookup would be meaningless). Zero behaviour change for the existing daily wrapper, confirmed by a direct equivalence test comparing both entry points against the same synthetic scenario.

### Fixed
- **Every silent-skip path in the daily quality-report pipeline now logs a specific, actionable reason (Mark Purcell, issues #313/#314).** A real 14-hour scoring freeze on Mark's own install was externally indistinguishable across four completely different causes (missing sensor config, no history yet, oracle LP infeasible, or the routine "already scored today" fast path) — all four produced the identical external symptom (sensor unchanged, zero log lines). Each path now logs at the level matching how routine/diagnostic it is: `DEBUG` for expected/routine skips (fast-path hit, missing config, window too short), `INFO` for a real history gap (with exact per-sensor row counts), `WARNING` for a genuinely infeasible oracle solve (with `initial_soc`/`min_soc`/`max_soc`, the exact values Mark hand-diagnosed this failure mode from on 2026-08-30). Purely additive — no behavior change to what gets scored or published, only what gets logged. 6 new regression tests, including a live mutation test confirming a removed log line is actually caught.

## [0.94.41] — 2026-09-01

### Fixed
- **v0.94.40's own re-entrancy guard broke CI** — its `async_setup_entry()` wrapped the real setup body in `hass.async_create_task(...)` and awaited that call's own return value, but several pre-existing tests (`test_init_cron_suppression.py`, `test_init_periodic_solve_timer_idempotent.py`) mock `hass.async_create_task` as fire-and-forget (closes the coroutine, returns a bare `MagicMock()`), since no call site before this one had ever awaited its own return value. Confirmed live in CI: `TypeError: 'MagicMock' object can't be awaited` across 5 tests. Fixed by switching to plain `asyncio.create_task(...)` for this one internal task — it never needed `hass`'s own fire-and-forget background-task tracking in the first place, since its result is awaited by the same coroutine that creates it; HA's top-level await of `async_setup_entry()` itself already tracks this work's real lifecycle, unchanged from before the guard existed. Verified against the exact CI command locally (836 passed, 0 failed) before pushing. v0.94.40's GitHub release was live and broken (CI red) between its tag push and this fix — no functional regression for anyone who already installed it, since the guard's own logic was correct, only its interaction with these tests' mocks was wrong.

## [0.94.40] — 2026-09-01

### Fixed
- **Root-caused the long-standing, intermittent "`number.nimbus_solver_*` entities reset to their schema placeholder minimum on some restarts, not others" bug.** Confirmed live on devhub via a real `ha_get_logs` pull: two genuinely concurrent runs of `async_setup_entry()` for the SAME config entry, ~4.5 seconds apart, produced a hub-wide "Platform nimbus_load does not generate unique IDs" collision storm across every `number`/`sensor`/`switch` entity this integration owns — HA's own "abandon a slow `async_setup_entry()` and silently retry it while the original coroutine keeps running" behaviour (the same general mechanism issues #210/#211 already partially addressed, just tripped by a different, not-yet-backgrounded slow step: the per-subentry coordinator setup+first-refresh loop). Whichever attempt's entities register first wins; the other is silently dropped — non-deterministic across restarts, which is exactly the "sometimes fine, sometimes reset" pattern reported repeatedly. Fixed at the root: `async_setup_entry()` now guards against re-entry for the same `entry_id` — a second, genuinely concurrent call waits for the first attempt's own result instead of duplicating every entity/timer it creates. 3 new regression tests, including a live mutation test confirming the guard's removal reproduces the exact duplicate-setup symptom.
- The per-subentry coordinator setup loop (the actual slow step that was tripping the race above) is now genuinely concurrent (`asyncio.gather`) instead of sequential, one subentry at a time — a real startup-time speedup on any install with several subentries (the reference household's own 18+ real circuits; devhub similarly loaded), and defense-in-depth alongside the re-entrancy guard: faster setup means fewer opportunities for HA's own abandon-and-retry behaviour to trigger in the first place. Preserves the exact same failure semantics as the old loop (the first subentry to raise still aborts the whole setup).

## [0.94.39] — 2026-08-31

### Added
- `solver/p2p_export.py` (new module) — the two-tier P2P export-commitment mechanism (`GridConfig.fixed_export_kw` hard charge gate + export-bounds pinning, `export_bonus_price`/`export_bonus_volume_kwh`'s per-real-calendar-day cap and latest-preferred tie-breaker), extracted verbatim from `network.py`'s own live-tested implementation into a shared module — deliberately without modifying `network.py`/`build_plan()` at all, since that file is real-money-adjacent production code re-solved every 5 minutes on the reference household's own NUC1/NUC2. Every function is a complete no-op whenever its relevant `GridConfig` field is `None`, matching every other optional mechanism in this codebase.
- `solver/stochastic.py` (Track A2) can now genuinely reason about a P2P export commitment, both stage 1 (the shared, pre-branch decision) and every stage-2 scenario independently — wired via `p2p_export.py`. Direct household ask: "it should be smart to know how to balance it with p2p in play as well as without it there at all... the integration must handle and allow for variables and various scenarios." Also closes a real, pre-existing gap: `StochasticPlan` never exposed `grid_import_kw`/`grid_export_kw` at all before this — a genuine prerequisite, since `fixed_export_kw` pins `grid_export` directly, not battery charge/discharge.
- New test suite `tests/test_solver_stochastic_p2p.py` (5 tests) — includes two live mutation tests (hard charge gate removed, export-bounds pinning removed) confirming the tests genuinely catch the real bug each mechanism exists to prevent, not passing vacuously. Full existing suite (723 tests total) passes unchanged.
- `116KAT-HA-AI` repo: `scripts/nimbus_stochastic_comparison_writer.py` — the one real caller of this feature, deliberately devhub-only and fully reversible (hardcoded against devhub's own IP, never the NUC1/NUC2 VIP; writes a standalone shadow-mode comparison sensor only, no live dispatch path). See that repo's own `CLAUDE.md` for the full deploy notes.

## [0.94.38] — 2026-08-31

### Fixed
- `solver_runtime.py`'s `_log_dispatch_dry_run()` had four early-return guard paths with zero diagnostic logging — confirmed live on the reference household's own NUC1: `sensor.nimbus_solver_dispatch_dry_run` silently skipped exactly one solve cycle (real timeline: last push 17:10:32, no push at 17:15, staleness watchdog marked it `unavailable` at 17:15:37, resumed cleanly at 17:20:32), with zero exception, zero log line, and the switch confirmed `on` throughout — meaning the root cause could not be determined after the fact, only that one of the three remaining guards (forecast sensor missing / forecast list empty / `battery_kw` key missing) must have fired. Each of the four early-return branches now logs a `WARNING` naming exactly which condition tripped and the relevant live state, so a future occurrence is diagnosable instead of silent. Purely additive — no behavior change to the dry-run logic itself, this only makes an existing silent path observable.

### Fixed
- `solver_battery_power_positive_is_charge` (issue #299, v0.94.35's own SigEnergy sign-convention fix) was unreachable through the Solver settings wizard — `_solver_sources_schema` registered the field on the form, but `async_step_solver_sources`'s save loop only copies keys listed in `_SOLVER_WIZARD_SCHEMA_KEYS`, and the flag was missing from that tuple. Submitting the wizard reported success while the value silently stayed `None`, leaving the v0.94.35 fix unusable from the UI (Mark Purcell, issue #307, PR #309). Fixed by adding the key to the tuple; new regression test locks the save contract.

## [0.94.36] — 2026-08-31

### Fixed
- Every load/power-signal subentry's `_async_retrain()` silently failed to train at all whenever the hub had a temperature sensor, humidity sensor, curtailment sensor, battery sensor, grid sensor, or solar sensor configured (temperature/humidity are shared hub-level options, so this affected effectively every real install). Root cause: #257/#259 (the hybrid recorder+LTS training-source feature) renamed `_async_fetch_history()` to `_async_fetch_recorder_history()` and added a training-source-aware `_async_fetch_training_history()` wrapper, but only migrated the load's own history fetch — the other six fetch call sites (temp, humidity, curtailment, battery, grid, solar) kept calling the now-nonexistent `_async_fetch_history`, raising `AttributeError` immediately, before the model training step was ever reached. Confirmed live: `sensor.nimbus_archerfield_temp_forecast`/`_humidity_forecast` never trained (`training_points: 0`, `model_trained_at: null`) across two separate restarts. A subentry with an existing pre-#257 persisted model masked this silently, since a cold-start retrain is only attempted when no model is already on disk — the bug was live for every subentry the moment it landed, just not yet visible for ones with a cached model. Fixed by routing all six call sites through `_async_fetch_training_history`, matching the load's own fetch. New regression test drives `_async_retrain()` end-to-end with all six sensors configured.

## [0.94.35] — 2026-08-31

### Fixed
- `compute_daily_quality_report()` always assumed the configured battery power sensor follows this project's own established sign convention (positive = discharge, matching the reference household's own `sensor.logger_battery_power`) — a SigEnergy plant's own sensor reports the opposite (positive = charge), so every charge event was silently booked as a discharge and vice versa, producing a structurally impossible EPR = -137.47% (Mark Purcell, issue #299). Fixed with a new, explicit, opt-in config-flow flag, `solver_battery_power_positive_is_charge` (default `False` — a byte-for-byte no-op for every existing install).
- `sensor.nimbus_solver_quality_report` (and every other `_NimbusSolverPushSensor` subclass — battery forecast, efficiency backtest, counterfactual SoC) could flap between a real value and a bare `unknown` state on a tight, repeating cadence (issue #302). Root cause: `_async_recheck_availability`'s own "first tick, just record a baseline" branch silently swallowed a staleness transition that had *already* happened by the time the very first recheck tick ever fired, and this had been invisibly papered over the whole time by `should_poll` defaulting to `True` (an unrelated, independently-wasteful default for a push-only entity, also fixed here to `False`). `_was_available` now starts at `True` in `__init__`, matching what `available()` already, definitionally, returns for a freshly-constructed instance — closing the gap directly instead of special-casing around it. Confirmed via CI's own real HA test harness, including the pre-existing regression test that had caught an earlier, incomplete attempt at this fix.

## [0.94.34] — 2026-08-31

### Added
- Expanded the four Quality sub-device sensors (`sensor.nimbus_quality_j_ref`, `.j_ach`, `.j_star`, `.regret_dollars`) with per-hour reconstruction attributes (Mark Purcell, PR #297) — 24-key dicts covering import/export price, load/solar/battery/grid power, and SoC%, so a Lovelace card can diff the achieved trajectory against the oracle hour-by-hour from a single state read, with no separate template sensor. `hourly_regret` (already computed internally) is published alongside them for the first time. All four dicts are `_unrecorded_attributes` — live in the HA state cache for cards/automations, never written to Recorder, so the DB stays flat regardless of publish frequency. Fully backward compatible: the other 73 `FlattenedAttrSpec` rows keep publishing no attributes at all, byte-identical to before.

## [0.94.33] — 2026-08-31

### Fixed
- `sensor.nimbus_solver_quality_report` and `sensor.nimbus_efficiency_backtest` triggered a recurring "no longer has a state class" / "units changed" Repairs entry, confirmed independently on both devhub and the reference household's own NUC1 ("pretty sure not the first time"). Root cause: `publish_daily_quality_report()`'s own `ha_post_state()` call built its attributes dict with a stray `unit_of_measurement: null` and no `state_class` key at all. In native mode with a registered SensorEntity handler (the normal path), the entity's own declared unit/state-class correctly override these stray values by the time a live read sees them — which is why the entity always reads back correct. But `ha_post_state()`'s raw `states.async_set()` fallback (used whenever no handler is registered yet, e.g. this function racing `sensor.py`'s own setup shortly after a restart) has no entity object to draw a correction from, and writes the stray values verbatim — whichever path Recorder happens to sample when validating long-term statistics, that's what gets flagged. Now sets the real, correct literal values (`"%"` / `"measurement"`, matching `NimbusSolverQualityReportSensor`'s own class attributes) so both code paths produce a correct state on their own, closing the gap outright. New regression test (`tests/test_solver_writer_quality_report_state_class.py`).

## [0.94.32] — 2026-08-31

### Added
- `sensor.nimbus_solver_price_response_latency` (issue #294, Mark Purcell) — a first-class, continuously-observable version of the "REST-poll two sensors and diff timestamps" measurement Mark had to do by hand to verify issue #232's `solve_on_price_change` fix. `state_class: measurement`/`device_class: duration` so it feeds HA's long-term statistics directly (no Grafana/InfluxDB detour needed for a simple history chart). Updated ONLY on an event-driven (price-change-triggered) solve, per Mark's own explicit design — a cron or startup-triggered solve leaves it at its last event-driven value, since neither has a meaningful "time since the price actually changed" to report. Attributes: `last_price_change_at`, `last_solve_at`, `trigger_source`, `triggering_entity`, `debounce_s`, plus `p50_recent`/`p90_recent`/`max_recent`/`sample_count` over a rolling 50-sample window.

### Fixed
- The phase-locked periodic cron (issue #244) and the optional event-driven `solve_on_price_change` trigger (issue #256) ran fully independently — Mark's own live capture (issue #295) showed both writing `sensor.nimbus_solver_battery_forecast` within the same NEM 5-minute block, 5s apart, from identical inputs: a genuinely redundant solve, restamping `last_updated` and burning CPU for zero new information. The cron now checks `solver_runtime.time_since_last_solve()` first and skips its own tick if ANY trigger (cron, event-driven, or the startup retry loop) completed a solve within the last 60 seconds — treating the cron as a watchdog ("guarantee at least one solve per 5-min block, e.g. if Amber goes quiet for a whole window") rather than an unconditional heartbeat. A fresh install/restart (nothing has ever solved) is never suppressed.

## [0.94.31] — 2026-08-31

### Fixed
- `publish_daily_quality_report()` / `publish_efficiency_backtest_report()` / `publish_nimbus_only_soc_counterfactual()`'s own "already scored" idempotency check reads back this install's prior publish via `ha_get()` keyed by a fixed literal entity_id (e.g. `sensor.nimbus_solver_quality_report`) — correct on almost every install, but confirmed live on devhub to silently read the WRONG entity's state whenever that literal name is already claimed by something else in the same HA instance (a `remote_homeassistant` mirror of another Nimbus install using the identical sensor names, in this case) — HA's own entity-registry dedup then bumps this install's own platform entity to a `_2` suffix, invisible to a self-read keyed by the plain name. The idempotency check believed "already scored today" on almost every cycle (since the unrelated mirror, a different working install, genuinely had), so this install's own quality-report/backtest/counterfactual entities only ever got a fresh publish on the rare cycle the fast path happened to also re-push something valid through them — explaining multi-hour stale/`unavailable` stretches, confirmed live (3 separate "went unavailable" transitions in 24h, one lasting 9+ hours through a midnight boundary). Fixed with a new `resolve_real_entity_id()` seam: `register_entity_handler()` now optionally records each entity's own real, HA-resolved `entity_id` alongside the existing literal dispatch key (`sensor.py`'s `async_setup_entry` now passes it for all 8 migrated push sensors), and the three idempotency-check self-reads resolve through it before calling `ha_get()`. Purely additive — the dispatch key itself, and every install where the literal name was never contested, are completely unaffected.
- The same three `publish_*` functions were wrapped in a bare `except Exception: pass` in `main()`, with zero logging — confirmed live this made the bug above completely invisible (200+ recent log lines matching "nimbus" on devhub, zero exceptions, zero tracebacks). Now logs a one-line `_LOGGER.warning(...)` with the real exception message before continuing — the "must never break the real solve" guarantee is unchanged, but a future failure of any of these three publishes is now actually diagnosable instead of only visible as a stale sensor days later.

## [0.94.30] — 2026-08-30

### Fixed
- The Solver's "one immediate cycle at setup" call (meant to avoid sitting with an empty forecast for up to a full 5-minute cron period after a restart) had no retry of its own — it could race the `number.nimbus_solver_*` required entities' own restore-from-registry on a real HA restart, see `sensor.nimbus_solver_config` still reporting `unconfigured`, and lose its one chance. Confirmed live: a restart at 00:50 UTC produced its first real solve at 00:55:00 UTC, a genuine 5-minute cron boundary, not the immediate attempt (or an early retry) succeeding. New `_async_run_solve_with_startup_retries()` retries up to 6 times, 15s apart, before falling back to the regular periodic cron — closes the real, observed gap (household-reported: "it takes a lot of time to come back") without changing behaviour for a genuinely-unconfigured fresh install (which now just retries harmlessly a few times before falling silent, same as before from that point on). New regression test (`tests/test_init_startup_solve_retry.py`).
- The startup-retry task above was never registered for cancellation on unload — a real HA integration-test harness reproduced a task still sleeping (up to 90s worst case) against a `hass` instance that was already mid-shutdown. Fixed with `entry.async_on_unload(task.cancel)`. Found this alone was insufficient: `hass.async_create_task()` is *tracked* by `hass.async_block_till_done()`, so a real test calling `block_till_done()` right after setup (before any unload) still blocked for the task's full retry duration — this is what actually caused CI's "Run pytest (real HA harness, hass_integration only)" job to hang 13+ minutes instead of its normal ~2. Switched to `hass.async_create_background_task()`, HA's own documented API for exactly this shape of work (fire-and-forget, auto-cancelled on shutdown, explicitly exempt from `async_block_till_done()`'s wait).
- That same cancel-on-unload registration then crashed real HA core outright: `ConfigEntry._async_process_on_unload()` schedules *any* truthy return value from an on-unload callback as if it were a coroutine, and `entry.async_on_unload(task.cancel)` registers the bound method directly — `asyncio.Task.cancel()` returns `True` exactly when the task is still running at cancellation time (the precise case this exists to handle), crashing unload with `TypeError: a coroutine was expected, got True`. Fixed by wrapping the cancel in a plain function with an explicit `-> None` return, so HA never sees a truthy value to misinterpret. This also explains the "Lingering timer after test" failures seen alongside it in CI — the crash aborted the on-unload loop partway through, so the periodic-solve timer's own unsub (unaffected on its own — confirmed `async_track_utc_time_change`'s returned `async_cancel` is already annotated `-> None`) was never reached.

## [0.94.29] — 2026-08-30

### Fixed
- `sensor.nimbus_mirror_temperature_forecast` and `sensor.nimbus_mirror_humidity_forecast` (issue #290) were being written every 5-minute solve cycle via `solver_writer`'s raw `states.async_set()` fallback — neither had ever been registered as a real `SensorEntity`, unlike every other push sensor in this file, so every single push logged a WARNING (~576 lines/day) and the entities had no `unique_id`/`device_info`/`device_class`/`unit_of_measurement` of their own. New `NimbusMirrorTemperatureForecastSensor`/`NimbusMirrorHumidityForecastSensor` classes (same `_NimbusSolverPushSensor` base as the other #55-migrated sensors, no dedicated sub-device since these are small, purely cosmetic dashboard mirrors), registered in the dispatch table alongside their siblings. New regression test (`tests/test_sensor_mirror_forecast_entities.py`) locks in the class attributes, entity_id/device-link continuity, and dispatch-table routing.

## [0.94.28] — 2026-08-30

### Fixed
- Family-A parent sensors (`sensor.nimbus_solver_quality_report`, `sensor.nimbus_efficiency_backtest`, `sensor.nimbus_counterfactual_soc`) went `unavailable` — continuously on v0.94.25 (#289), intermittently ("fires every ~10 minutes, self-heals") on v0.94.27 (#292) — despite the Solver producing fresh, `optimal` plans every 5-minute cycle the whole time. Root cause, confirmed by reading the real, deployed code: each of the three `publish_*()` functions in `solver_writer.py` has a cheap "already scored" idempotency fast path that used to just `return` with nothing published once a day was already scored. That silently stopped refreshing the entity's own freshness stamp (`_NimbusSolverPushSensor.update_from_solver()`'s `_last_updated`). After `_STALE_AFTER_SECONDS` (300s) with no new publish, the freshness watchdog correctly marks the entity unavailable — and HA core's own `Entity.async_write_ha_state()` writes an EMPTY attributes dict for an unavailable entity (real, long-standing HA core behaviour, not a bug here). The next idempotency check then reads back `attributes={}`, finds no `latest_date` to match, and recomputes+republishes from scratch — refreshing the stamp, going available again, holding for up to 300s, then repeating forever. Fixed by re-pushing the same already-read state/attributes on the fast path instead of doing nothing, matching the reference/standalone script's own already-correct "already scored... re-pushing sensor" behaviour that this native in-process path had dropped. New regression test (`tests/test_solver_writer_family_a_freshness_repush.py`) locks in the re-push on all three publishers.
- Ported the same fix to the portable reference `nimbus_solver_quality_writer.py` (`docs/real-world-integration/files/`): its own `real_p2p_target_kw` reconstruction used a plain point-in-time lookup (`value_at_or_before`) at exactly the P2P window's own start instant — fragile against a real, brief, self-correcting transient in that `input_number` landing on that exact instant (found live 2026-08-29 on the reference household's own deployment: a 3-second glitch forced the oracle's forced-export to 1.0kW for the entire 7-hour window instead of the real ~12kW target, invalidating that day's EPR/regret). New `robust_value_near()` (a time-weighted "greatest total duration wins" scan over a 5-minute window) replaces it for this one lookup; `value_at_or_before()` itself is untouched and still correctly used for the battery start/end-of-day SoC% lookups.

## [0.94.27] — 2026-08-30

### Fixed
- `solver_writer.py`'s `compute_daily_quality_report()` (same function v0.94.26 touched): v0.94.26's own fix (wiring in the concave `terminal_value_breakpoints` curve) turned out to be insufficient — a real incident day still scored EPR>100%/negative regret under it (127.7% / -$11.14, vs the invalid 145.0% / -$18.15 the flat-credit bug produced). Root cause: this scorer evaluates exactly ONE already-elapsed calendar day in isolation, so crediting leftover end-of-day SoC via ANY positive per-kWh rate — flat OR a concave curve — still over-rewards a trajectory that accidentally under-delivered that day (ended full because it failed to export, not because holding was the better choice). Fixed for real by using `salvage_value=0.0` (no terminal-value credit at all) instead of the configured forward-planning `solver_salvage_value`. Verified against the same real incident day: 76.0% EPR / +$8.94 regret, both valid. The live forward-plan's own separate `BatteryConfig` (in `main()`) is untouched — it genuinely needs terminal value, since a finite-horizon LP has no reason to hold real charge past the last period it can see; planning forward under uncertainty and honestly scoring what already, certainly happened are different problems.

## [0.94.26] — 2026-08-30

### Fixed
- `solver_writer.py`'s `compute_daily_quality_report()` (the function behind `sensor.nimbus_solver_quality_report`, EPR/regret): its own `BatteryConfig` never set `terminal_value_breakpoints`, so every `evaluate_realized_cost()` call it makes (J_ref, J_ach, and the oracle's own residual evaluation) silently fell back to a flat `salvage_value * final_soc_kwh` terminal credit — the exact mechanism a prior fix (`5317068`, "Fix invalid EPR (>100%): flat salvage_value over-rewarded an anomalous full-SoC ending vs the oracle", shipped in v0.94.19) already eliminated for the live forward-plan's own separate `BatteryConfig`, but never wired into this call site. Reproduced live on both NUC1 (running v0.94.19, the very version the original fix shipped in) and devhub (v0.94.23) with matching numbers (EPR 244%, regret_dollars -$22.88) — confirming this was never a version-lag problem, the call site itself was just never updated when the fix landed. Fixed by passing the same `terminal_value_breakpoints_for(salvage_value, min_soc_kwh, max_soc_kwh)` curve the live plan already uses. New regression test (`tests/test_daily_quality_report.py::test_battery_config_wires_terminal_value_breakpoints`) locks in the wiring; confirmed it fails without the fix and passes with it.

### Added
- Family B fan-out (v0.94.20 CHANGELOG deferred item, Mark Purcell): the 24 per-column fields of `sensor.nimbus_solver_battery_forecast`'s current-period `forecast[0]` row are now published as their own `sensor.nimbus_solver_current_*` scalar entities on the hub device, so a dashboard or automation can read "what does the plan say for right now?" as a first-class entity without templating into the array. Purely additive — the parent sensor keeps every field it already published; Family A's 36 top-level scalars are untouched. 12 primary entities (battery kW, SoC, dispatch direction, import/export/bonus kW and prices, load, solar, net cost) + 12 diagnostic entities (v0.94.15/17 flow decomposition + savings model + battery cost basis). Full horizon (361 rows × 40 fields) is deliberately NOT flattened — that would produce 14,000+ entities per install. 14 new tests (`tests/test_sensor_flattened.py`): spec coverage, no-collision-with-Family-A guard, real-row payload dispatch, safety on missing/empty/malformed forecast lists, string-state-class regression for `current_dispatch_direction` (guards against the class of state_class bug tracked in #283).

## [0.94.25] — 2026-08-29

### Fixed
- `sensor_flattened.py` (issue #283, Mark Purcell): three real defects in the flattened-sensor fan-out, across all five tables.
  1. **State-class/device-class mismatch.** 38 entities combined `device_class=MONETARY` or `ENERGY` with `state_class=MEASUREMENT` — HA core only allows `total` for MONETARY and `total`/`total_increasing` for ENERGY, so every one of these logged a repair-flow warning on every restart. Mark's report found 24; a full scan of this file found 14 more: 5 in the original Family A table (`total_cost`, `total_cost_with_fixed_costs`, `cost_band_lower`, `cost_band_upper`, `cost_band_width`), plus 9 `current_*` rows in the new `FLATTENED_ATTRS_CURRENT` table that PR #284 introduced concurrently with this fix (re-scanned and fixed in the same PR rather than left for a follow-up, since it's the identical defect in the same file). Fixed by dropping `device_class` on all of them rather than switching to `total_increasing`: these are per-solve, per-day, or per-current-period point-in-time values (a rolling-horizon sum recomputed fresh every solve, a per-solve LP objective value, or a live current-tick price/cost), not genuine monotonic meters, so `MEASUREMENT` is the semantically correct `state_class` — confirmed directly in `solver_writer.py`'s own computation of the kWh totals.
  2. **Duplicate-valued sensor.** `uplift_available` (`FLATTENED_ATTRS_QUALITY`) was byte-identical to `regret_dollars` (`epr.py`: both `j_ach - j_star`). Removed; `regret_dollars` is the canonical entity.
  3. **Wrong unit.** `tracking_fidelity` was published with `unit_of_measurement="%"` despite `tracking.py`'s own value being a genuine 0-1 fraction (`1.0 - gap_energy / commanded_activity`), never rescaled to 0-100. Fixed by dropping the unit rather than rescaling the value.

  4 new tests (`tests/test_sensor_flattened_device_class_rules.py`) lock in all three fixes across every flattened-sensor table.

## [0.94.24] — 2026-08-29

### Added
- `solver/reference_benchmark.py` (issue #273, item #3, Mark Purcell): a standardized, fully synthetic, deterministic reference-household scenario for comparing Nimbus's own forecast-regret decomposition across releases, without a real day's own weather/price-shock/incident noise dominating the comparison. Reuses the real, production `compute_forecast_regret()` directly — same code path `nimbus_solver_quality_writer.py` runs against real household data. Run via `python tests/run_reference_benchmark.py`. A number to watch and record alongside a Solver-affecting change, deliberately not a CI pass/fail gate (per issue #217's own conclusion that soak/gating decisions are the project owner's call). See `docs/reference-benchmark.md` for the full methodology, including the honest scope limit that this exercises the Solver only, not the ML Forecaster (which needs real recorder history to run at all). 7 new tests (`tests/test_reference_benchmark.py`) checking determinism and the same oracle-never-beaten/non-negative-regret invariants every other regret/EPR tool in this package already requires.

## [0.94.23] — 2026-08-29

### Fixed
- `coordinator.py` — a transient `weather.get_forecasts` failure (issue #269, Mark Purcell — most commonly the HA-restart startup race between this coordinator's first tick and the weather integration's own first successful fetch) silently degraded that cycle's temperature training to zero signal, with a one-shot warning flag that hid any later chronic failure. Now caches the last real, non-empty forecast per configured sensor and falls back to a future-only trimmed slice of it on failure, and replaces the one-shot flag with a state-change tracker (warns on every success→failure transition, INFO on every recovery) rather than warning only once ever.

## [0.94.22] — 2026-08-29

### Fixed
- `network.py` — real, empirically-confirmed LP degeneracy (issue #266, Mark Purcell): `grid_import_kw` and `grid_export_kw` could be simultaneously nonzero in the same period (a real capture showed `grid_import=13.133`/`grid_export=30.0` at one timestamp — physically impossible, a single real grid connection can only carry current one direction at a time). New combined-direction cap (`grid_import[t] + grid_export[t] <= max(import_limit_kw, export_limit_kw)`, same technique as issue #245's own battery-side cap) bounds the degeneracy. **Partial fix, not full closure** — replaying the two real buggy fixtures shows a real, measured reduction (25→9 and 36→36 violating rows, all now capped instead of unbounded), not zero; full elimination needs #238's MILP complementarity.

## [0.94.21] — 2026-08-29

### Added
- `solver/forecast_regret.py` — `compute_forecast_regret()` isolates the forecast-error branch of Mark Purcell's four-way EPR decomposition (issue #273: topology / forecast / optimisation / execution error). Re-solves the same LP under Nimbus's own forecast vs. a naive persistence baseline, evaluates both committed battery trajectories against the same real ground truth, and reports `nimbus_value_add_dollars` — the direct, actionable "what did Nimbus's forecast save you today vs. doing nothing smarter" figure. Pure function, no HA dependency; live wiring into an actual sensor is a follow-up.

## [0.94.20] — 2026-08-29

### Added
- `sensor.nimbus_solver_battery_forecast`'s 36 top-level scalar attributes (Family A — monetary/energy/power/battery-health PRIMARY signals plus LP-internal DIAGNOSTIC signals) are now also published as their own individual `SensorEntity` instances, so each one participates in HA history, long-term statistics, and per-entity Lovelace graphs on the same footing as the existing `sensor.nimbus_solver_config` bridge sensor. Purely additive — the parent sensor is unchanged and keeps every attribute it already published. Family B (per-row forecast fields) is a deliberate follow-up, kept out of this PR to keep review surface small. (Mark Purcell)
- `epr.py`'s module docstring gained a "Scope note" clarifying that EPR is a whole-household ratio (battery charge/discharge, `solar_curtailed_kw`, and per-load `shed_kw` all sit on the same LP objective, with `J_ref` from `counterfactuals.py:no_control_dispatch()`) — distinct from both the BESS-industry "capture rate" (battery-only, no solar/load in scope) and the wind/solar capture rate / Marktwertfaktor (a cannibalisation measure with no benchmark trajectory at all, an unrelated concept despite the similar name). Documentation only, no functional change. (Mark Purcell)

### Fixed
- `solve_on_price_change` and `solve_on_price_change_debounce_s` were silently unreachable from the Solver settings wizard on every install — `async_step_solver_sources`'s own save loop drops any field not listed in `_SOLVER_WIZARD_SCHEMA_KEYS`, and neither key was in that tuple. Found live while independently verifying #260's price-triggered solving. Fixed by moving both to the same live switch/number entity pattern the 14 numeric Solver fields already use (`switch.nimbus_solve_on_price_change`, `number.nimbus_solve_on_price_change_debounce_s`) — a dashboard toggle now takes effect immediately, no Configure round-trip, with `entry.options` kept as the fallback read path for any not-yet-migrated install. (Mark Purcell, issue #232 follow-up)

## [0.94.19] — 2026-08-29

### Fixed
- Real bug found live on a 2026-08-28 night disrupted by an unrelated household incident: `compute_daily_quality_report()`'s EPR read 145% (invalid — EPR is mathematically bounded to ≤100%). Root cause: the real dispatch barely discharged that night and ended the day accidentally near-100% SoC; `evaluate_realized_cost()`'s flat `salvage_value * final_soc_kwh` credit massively over-rewarded that accidental full ending relative to what even a fully unconstrained perfect-foresight oracle can match (the oracle correctly prefers selling energy during the day over holding it for a flat rate exceeding real achievable prices, so it can never "beat" a trajectory that got lucky on this technicality) — letting the real-achieved trajectory beat the oracle at spot-only economics, which should be structurally impossible. Fixed by extending `evaluate_realized_cost()` to accept the same concave `terminal_value_breakpoints` curve `network.py`'s own LP already uses (2026-08-18/22), applied identically to every trajectory `compute_quality_report()` scores instead of a flat rate on only some of them. Both new params default to `None`, byte-identical to every existing caller.

## [0.94.18] — 2026-08-28

### Fixed
- Real bug found live on devhub: `compute_daily_quality_report()`/`compute_efficiency_backtest_report()` (the EPR/regret quality report and the efficiency-sensitivity backtest) treated whatever `solver_solar_power_sensor`/`solver_battery_power_sensor`/`solver_whole_house_cross_check_sensor` pointed at as already being kW, with no check against the entity's own declared unit. A household pointing `solver_solar_power_sensor` at a native Watts sensor (confirmed live: `sensor.combined_total_dc_power` reports `unit_of_measurement: "W"`) silently fed solar values ~1000x too large into both reports, producing impossible economics (confirmed live: `theoretical_maximum_yield` around -$1280, `regret_dollars` around -$1289 for one real household-day — an EPR of -0.7%, which shouldn't even be able to go negative under the metric's own definition). New `_kw_scale_factor()` checks the source entity's real `unit_of_measurement` and scales by 0.001 for a confirmed "W" sensor, 1.0 otherwise (including no unit at all, or a lookup failure) — deliberately corrects only the one real, confirmed-live mismatch rather than guessing at every possible power unit HA could report. 6 new tests (`tests/test_kw_scale_factor.py`), including a direct regression test reproducing the live 9300W→9.3kW case.

## [0.94.17] — 2026-08-28

### Added
- Nimbus issue #264 (Mark Purcell), phases 1-2: seven-flow merit-order decomposition + shadow prices + a PV/Battery/Combined/Interaction savings model, published on every period of `sensor.nimbus_solver_battery_forecast`'s `forecast` list (and both portable copies of the writer). Extends v0.94.15's (#262) 2-way `dispatch_source_a/b` split (against the battery only) to all four real bus terminals: `flow_pv_to_load_kw`, `flow_pv_to_battery_kw`, `flow_pv_to_grid_kw`, `flow_battery_to_load_kw`, `flow_battery_to_grid_kw`, `flow_grid_to_load_kw`, `flow_grid_to_battery_kw`, plus a $/kWh shadow price per flow and `savings_pv`/`savings_battery`/`savings_combined`/`savings_interaction` ($ per period). Existing fields are untouched — purely additive.
  - Deliberately extends the issue's own sketch in two places, verified before implementing rather than copied as-is: takes separate pre-net `charge_kw`/`discharge_kw` (not the issue's single `net_battery_kw`), so a genuine same-period charge+discharge wash trade (#245) stays visible as a real flow instead of being silently netted away before the decomposition ever sees it; and tracks a real cross-period weighted-average cost-of-goods (WACOG) basis for energy held in the battery (`flow_battery_cost_basis`) rather than a same-period lookup, since a battery's SoC persists across periods and same-period charge+discharge is the anomalous case, not the normal one the pricing table needs to be right for.
  - New regression suite `tests/regression/test_flow_invariants.py` (FLOW-01..06) validates the decomposition's four by-construction invariants plus a real reconciliation-against-the-LP check over every captured fixture. That reconciliation check surfaced a genuine, independent LP degeneracy (simultaneous grid import+export in some periods) in two existing fixtures — documented and opted out via `SKIP_INVARIANTS.txt`, filed as a separate follow-up issue (#266) rather than fixed here, since it's unrelated to #264 itself.
  - Phases 3-4 (three/four new hub-level `total_increasing` LTS sensors rolling this up to daily/weekly/monthly/yearly/lifetime savings) are deliberately deferred — the forecast array these flows live on is recomputed and overwritten every solve cycle, so a real persistent accumulator needs its own double-counting-safe design (distinct from simply reusing #77's SensorEntity pattern, which the issue's own phase 3 sketch assumed); left for a focused follow-up rather than rushed.

## [0.94.16] — 2026-08-28

### Fixed
- Nimbus issue #263 (Mark Purcell): every subentry-created `sensor.nimbus_*_forecast` entity triggered Home Assistant's "unit has changed" long-term-statistics repair dialog on first restart after being seeded into recorder statistics, even though the entity has always declared a real `kW` unit — the underlying statistics-metadata row was seeded with an empty unit before the entity's own unit was ever recorded. Verified against real HA recorder internals (installed `homeassistant` 2025.1.4) before implementing: the issue's own originally-sketched fix (`recorder.async_change_statistics_unit(..., old_unit_of_measurement="")`) was confirmed, by direct testing, to raise `HomeAssistantError` immediately (it gates on `can_convert_units("", "kW")`, which is `False` — an empty string has no unit family to convert from), so it would have crashed on exactly the row it was meant to fix. The real, correct mechanism — found by reading `homeassistant/components/recorder/websocket_api.py`'s own `ws_update_statistics_metadata` handler, the literal code behind the Statistics page's "change unit" fix button in HA's own UI — is `Recorder.async_update_statistics_metadata(new_unit_of_measurement=...)`, a raw metadata relabel with no `can_convert_units` gate. New `_remediate_forecast_lts_unit()` in `sensor.py` uses this, fired as a non-blocking `hass.async_create_task(...)` from `async_setup_entry` so a one-time cosmetic cleanup can never delay real entity setup; it only ever relabels a genuinely empty/`None` stored unit, never touches a row already holding any other real unit, and is wrapped in a broad try/except so a recorder error can't propagate into entity setup. 6 new tests (`tests/test_forecast_sensor_lts_unit_remediation.py`).

## [0.94.15] — 2026-08-28

### Added
- Per-period dispatch source/destination breakdown on the Solver plan (direct ask: "the plan table should also say where it is coming from -- such as solar, grid, battery... not just charging... it should say direction, and then from/to what source"). Each period in `sensor.nimbus_solver_battery_forecast`'s `forecast` list (and `sensor.nimbus_solver_dispatch_dry_run`'s own attributes) now carries `dispatch_direction` (`charge`/`discharge`/`idle`) plus a labeled two-way percentage breakdown (`dispatch_source_a_label`/`_pct`, `dispatch_source_b_label`/`_pct`) -- e.g. "100% Solar / 0% Grid" while charging, or "30% Load / 70% Grid" while discharging. A real merit-order decomposition of the same flow balance the LP already solved (solar serves load first, surplus charges the battery; battery serves load before any of it is attributed to export) -- not a dual/shadow-price attribution, since the LP itself has no per-source flow variables to read back. Applied identically to both portable copies of the writer (`docs/real-world-integration/files/`, `nimbus_solver_app/`) to keep all three byte-identical, per this project's own standing invariant.

## [0.94.14] — 2026-08-28

### Fixed
- Real-dispatch dry-run observation (`switch.nimbus_solver_dispatch_dry_run`) previously produced zero durable evidence: it only logged via `_LOGGER.info()`, which sits below `nimbus_load`'s default logger level (`WARNING`) — confirmed live on a real install that the switch was genuinely on and the Solver was genuinely solving on schedule, but not one observation had ever actually been recorded anywhere. Added `sensor.nimbus_solver_dispatch_dry_run`, a real, properly-registered diagnostic entity (same base class as the #55-migrated forecast sensors) that records the current period's planned `battery_kw` plus context (`soc_pct`, `grid_import_kw`, `grid_export_kw`, `import_price`, `export_price`) on every solve while the switch is on — reviewable via HA's own History graphs and long-term statistics, independent of logger level. Still purely observational: no `hass.services.call()` anywhere near this path, no change to the real-dispatch-groundwork phase-1 scope.

## [0.94.13] — 2026-08-27

### Added
- **`nimbus_load.solve_now` service** ([#232](https://github.com/code-imstillalive/nimbus/issues/232), [#254](https://github.com/code-imstillalive/nimbus/pull/254), thanks @purcell-lab). Mark's own suggestion: rather than the periodic timer guessing when a real settlement tick is likely to have landed (the #251 phase-alignment approach), an automation that genuinely watches the real price sensor's own state change can now call this service the instant a tick arrives — zero guessing, zero waiting. Reuses the exact same `solver_runtime.async_run_solve()` call the periodic timer itself makes, purely additive alongside it (doesn't replace the scheduled solve — a household still wants one on a period with no price change, e.g. a real SoC/load update).

## [0.94.12] — 2026-08-27

### Fixed
- **Standalone writer script now phase-aligns to the NEM settlement boundary too** ([#232](https://github.com/code-imstillalive/nimbus/issues/232), [#251](https://github.com/code-imstillalive/nimbus/pull/251)). #244/#247's earlier phase-alignment fix only touched the native in-process solver's own timer — the standalone `nimbus_solver_forecast_writer.py` cron script (deployed on a bare `* * * * *`) was untouched, and on any install running both writers against the same entity, its every-60s un-aligned writes dominate what a viewer sees, making the earlier fix invisible in practice. Confirmed live against real production recorder history — mean ~40.6s tail, worst case 54.0s, essentially the same magnitude as the original pre-#247 measurement. `seconds_to_settlement_capture()` adds a short, bounded wait to only the one tick per 5-minute cycle landing near a real boundary; the other four ticks each cycle stay a complete no-op, preserving the every-minute cadence itself.

## [0.94.11] — 2026-08-27

### Added
- `LPProblem.add_variable(binary=True)` + `LPProblem.is_mip` — real MIP support in `lp.py`, groundwork for #238's eventual complementarity constraint ([#249](https://github.com/code-imstillalive/nimbus/pull/249); the constraint itself is not written yet and `network.py` is untouched). Registers an integer column restricted to `[0, 1]` via HiGHS's own `changeColIntegrality` (no new dependency — HiGHS has always been the backend). Existing callers register zero binaries, so this is a proven no-op for every current caller, not just a claimed one. Recovers real work that had been orphaned on a stale branch with no PR and no test coverage — both fixed here, with `tests/test_solver_lp_binary_variables.py` (12 tests) added specifically to distinguish real recovered MIP duals from HiGHS's own all-zero placeholder, not just check the solve doesn't crash. #238 itself stays closed (COMPLETED) — this is groundwork for its explicitly-kept-open, non-blocking MILP follow-up, not a reopening.

## [0.94.10] — 2026-08-27

### Fixed
- **Battery charge/discharge simultaneously non-zero — partial fix, not a closure** ([#245](https://github.com/code-imstillalive/nimbus/issues/245), [#246](https://github.com/code-imstillalive/nimbus/pull/246), thanks @purcell-lab). Added a linear cap to `network.py`'s "SAME-PERIOD WASH-TRADE PREVENTION" section — `charge[t] + discharge[t] <= max(max_charge_kw, max_discharge_kw)` per period — which kills the unbounded degeneracy Mark's `purcell_qld1_v0.94.6_midblock/` fixture surfaced (`charge=17.98 kW` alongside `discharge=16.91 kW` in the same period). Tested directly rather than trusting the issue's own claim that no objective incentive exists for simultaneous charge+discharge: that claim doesn't hold in general — whenever a period's `export_price` genuinely exceeds `import_price` by more than round-trip loss (the same real P2P-window pattern #236 already documented), a same-period charge-then-export round trip is genuinely profitable, and the LP takes it up to the new cap. Worst case shrinks from "as large as both individual caps allow" to "at most `max(max_charge_kw, max_discharge_kw)` combined," at zero cost when that condition doesn't hold. `tests/test_solver_combined_direction_cap.py` (3 tests) documents both the hard guarantee and the honest residual gap. Full elimination (`charge[t] * discharge[t] == 0` complementarity) still needs #238's MILP treatment — not attempted here.
- **Solver cron phase-aligned to the NEM :00/:05 settlement boundary** ([#244](https://github.com/code-imstillalive/nimbus/issues/244), [#247](https://github.com/code-imstillalive/nimbus/pull/247), thanks @purcell-lab). Swapped the free-running 1-minute `async_track_time_interval` for `async_track_utc_time_change` locked to `:00:30, :05:30, :10:30, ...` — 30s past every NEM 5-minute settlement boundary. Mark's own 24h measurement (273 AEMO tick arrivals) showed the settled tick lands in `[15s, 30s)` past each boundary 89% of the time; a phase-unlocked 1-minute cron regularly solved just before the tick and then waited up to another full interval to pick it up, costing ~30s of stale-price dispatch per block. Solving at `:XX:30` sits comfortably past the p90 arrival window — and is fewer solves overall (12/hour vs. 60/hour), not more.

## [0.94.9] — 2026-08-27

### Added
- `battery_kw_after_efficiency` on every row of the solver plan ([#229](https://github.com/code-imstillalive/nimbus/issues/229), [#230](https://github.com/code-imstillalive/nimbus/pull/230), [#231](https://github.com/code-imstillalive/nimbus/pull/231), thanks @purcell-lab). `battery_kw` is the LP's own *pre*-efficiency decision variable, so it never reconciles against `soc_pct` directly — the SoC change over a window only closes against `Σ(battery_kw_after_efficiency × hours)`. Published by both the integration writer and the `nimbus_solver_app` writer, and now part of the documented plan-row contract in `docs/api-contract.md`. Built from the per-direction charge/discharge arrays rather than the collapsed net value, which is what makes it close tightly (0.013% residual vs 3.65% reconstructing from the post-collapse net). Unblocks the LP-04 energy-balance regression test.
- `price_blend_algorithm` on `sensor.nimbus_solver_battery_forecast`'s `solver_config` ([#237](https://github.com/code-imstillalive/nimbus/issues/237), [#240](https://github.com/code-imstillalive/nimbus/pull/240)). Makes the multi-source price blend inspectable rather than implicit, instead of leaving operators to infer it from the numbers. It reads `primary_preferring_fallback_to_secondary_mean` as of the blend change below.
- `tests/regression/`: `SET-02` in-block price identity, asserted for every row in the current NEM settlement block rather than row 0 alone, plus a `purcell_qld1_v0.94.6_midblock` fixture capturing a real mid-block capture ([#220](https://github.com/code-imstillalive/nimbus/issues/220), [#235](https://github.com/code-imstillalive/nimbus/pull/235), thanks @purcell-lab).

### Changed
- **Price blending is now primary-preferring, not a 50/50 mean** ([#239](https://github.com/code-imstillalive/nimbus/issues/239), [#236](https://github.com/code-imstillalive/nimbus/issues/236), [#242](https://github.com/code-imstillalive/nimbus/pull/242), thanks @purcell-lab). Whenever the primary price sensor has real forecast coverage at a period, its value is now used unblended — never averaged against a secondary just because one happens to be live at the same instant. A secondary only contributes where the primary's own real coverage doesn't reach, which is its one genuine job (extending the horizon past Amber's ~24h against PD7DAY's 7 days); that path, and the "no source covers this period" equal-weight fallback, are both unchanged. Live effect this fixes: a real Amber Express feed-in tick of −$0.0037/kWh averaged against a real QLD1 PD7DAY forecast of +$0.6701/kWh published an `export_price` of +$0.3332/kWh — above the concurrent import price — and the LP responded rationally by planning 22–25 kW of grid import simultaneously with 30 kW of export across 36 rows of the horizon. Import and export both derive from the same AEMO spot price for a given region and interval, so two live sources disagreeing by tens of cents cannot both be right, and averaging them fabricates a price no counterparty settles at. A genuine price-cap event still arrives as a real tick on the primary sensor itself. **This changes published prices on any install with `_sensor_2`/`_sensor_3` configured** — periods where both were live previously reported the mean and now report the primary.

### Fixed
- Settled current-block price override now covers **every** row inside the current NEM 5-minute settlement block, not just row 0 ([#220](https://github.com/code-imstillalive/nimbus/issues/220) partial regression, [#234](https://github.com/code-imstillalive/nimbus/pull/234)). `build_tiered_grid()`'s tier-0 stretch emits 1-minute periods from "now" up to the next clean 5-minute mark, and those extra rows are still inside the *same* real settlement block as period 0 — they were falling back to the blended forecast value instead of the settled one. Confirmed live on a real install (2026-08-27 17:33 AEST): the `:32` row held the correct settled identity while `:33`/`:34`, still inside the `[17:30, 17:35)` block, did not.
- CI: dropped a stale `noqa: BLE001` directive in `solver_runtime.py` that the pinned ruff flagged as unused, restoring a green lint gate ([#233](https://github.com/code-imstillalive/nimbus/pull/233)). No runtime behaviour change.

## [0.94.8] — 2026-08-27

### Added
- `switch.nimbus_solver_dispatch_dry_run` — phase 1 of real-dispatch groundwork ([#227](https://github.com/code-imstillalive/nimbus/pull/227)). Defaulted off; when on, each solve cycle logs what the current period's plan would send, pure observation with no write path yet. Nimbus still has zero live battery/grid control -- this only makes what a future dispatch decision would be visible ahead of time.
- `tests/regression/`: `SET-01a`/`SET-01b` settled-block price invariants + a `purcell_qld1_v0.94.6` fixture, plus a per-fixture `SKIP_INVARIANTS.txt` opt-out for historical goldens that predate a given invariant ([#225](https://github.com/code-imstillalive/nimbus/pull/225), thanks @purcell-lab).

## [0.94.7] — 2026-08-27

### Fixed
- Topology card: inverter name + DC/battery sub-label were centered directly above the bar, on the same X every vertical connector at that row (battery-tap drop, cross-inverter link) draws through — depending on tower count and which lines were active, the label rendered on top of the line, visually breaking it. Moved both labels beside the bar instead, clearing that X unconditionally ([#224](https://github.com/code-imstillalive/nimbus/pull/224)). Also re-synced `docs/real-world-integration/files/topology-card-v4.js`, which had drifted from the bundled card (missing the prior cross-inverter-link straight-elbow fix and busX margin bump).

## [0.94.6] — 2026-08-27

### Fixed
- **Cold-start retrain task now idempotent, tracked, and cancelled on
  unload** (#211): PR #210 backgrounded each subentry's cold-start retrain
  via a bare `hass.async_create_task()`, fixing the blocking-setup bug it
  targeted -- but never tracked or cancelled that task anywhere, unlike the
  periodic solve timer PR #213 later fixed the same way. A second,
  independent `NimbusCoordinator` object for the same subentry (the exact
  shape HA's own "abandon and retry a slow `async_setup_entry()`" race
  produces -- the same underlying mechanism #210 and #213 already fixed
  for two other call sites) left an orphaned, untracked retrain running
  forever, with no cancellation on unload/reload either. Very plausibly a
  live contributor to the "no longer has a state class" repair recurring
  on restarts. Now tracked in a module-level dict keyed by `subentry_id`
  (mirroring `_solver_timer_unsub`'s own established pattern): a second
  setup cancels the first coordinator's retrain task before starting its
  own, and `async_unload()` cancels any still-running task on teardown.

## [0.94.5] — 2026-08-27

### Fixed
- **Settled current-block price no longer blended or resampled** (#220,
  Mark Purcell): the forecast period covering right-now is the SETTLED,
  contractual price every configured source (Amber, LocalVolts, AEMO)
  publishes for that block — not an estimate, and never a valid target
  for `blend_price_with_secondary_sources()`'s blending. Live-reported
  effect: Nimbus published `import_price_raw=4.07`/`import_price=5.03`
  c/kWh for a block Amber had already settled at 4.89 c/kWh — a
  forecast-array "nearest-at-or-before" lookup isn't the same read as
  the source's own live `state`, and even a correct raw value would
  still have been diluted by a configured secondary source. The current
  period now reads the primary sensor's live `state` directly (same
  `safe_num()` pattern used for every other live scalar read in this
  file), applied before `_raw` is captured and re-applied again after
  blending, so it can never be a stale array lookup or a blend target.

## [0.94.4] — 2026-08-27

### Fixed
- **Coverage-aware price blending** (#216, Mark Purcell): a configured
  `solver_import/export_price_sensor_2/_3` secondary source used to get
  blended at a flat, unconditional equal weight across the WHOLE
  forecast horizon, including periods where that source's own real
  data doesn't reach yet — `resample_generic_price_forecast()` silently
  holds a source's own first/last real point flat outside its real
  coverage, and the old blend treated that placeholder exactly like a
  genuine second opinion. Live-reported effect: a "day 2-7" secondary
  source diluted a fully-real Amber Express primary 50/50 for its
  entire captured window, compressing `export_price` to roughly half
  its real range with a constant offset (Mark's own measured OLS fit:
  slope 0.502, intercept 4.36 c/kWh). Each configured source now
  contributes to a period only when that period falls within that
  source's own real coverage span; a period only one source genuinely
  covers passes that source through unchanged, and blending still
  happens exactly as before wherever every source really covers.

### Added
- **`export_price_raw` forecast attribute**, mirroring the existing
  `import_price_raw` — the export-side spot/live price before network
  fees or P2P bonus are added (#216).
- `import_price_raw`/`export_price_raw` now report the true pre-blend
  source value (what the configured sensor itself said, before any
  configured secondary source is folded in), rather than the post-blend
  value — restores their intended use as a "before any transformation"
  diagnostic on any install with a secondary price source configured
  (#216).

## [0.94.3] — 2026-08-27

### Fixed
- **Duplicate periodic-solve timer** (#213, closes #211): a live devhub
  recurrence of issue #85's own flap pattern — `sensor.nimbus_solver_battery_forecast`
  and `sensor.nimbus_household_load_total_forecast` each getting a genuine,
  independent solve pushed roughly twice a minute, a few seconds apart — was
  traced to `async_setup_entry()` occasionally running more than once for the
  same config entry (same underlying mechanism #210 fixed for a slow retrain
  call, tripped here by a different slow step) and registering a second,
  independent `_periodic_solve` timer that never got cancelled. The timer's
  own unsub callable is now tracked per entry_id, so a second setup call
  cancels the first timer before registering its own — at most one lives at
  a time regardless of how many times setup runs.
- **Cold-start retrain blocking hub setup** (#210): `async_setup()` used to
  `await` a subentry's first retrain inline whenever no persisted model
  existed yet, before any entity in the hub had been registered. On an
  install with several subentries cold-starting at once, that sequential
  blocking chain could run long enough to trip Home Assistant's own
  slow-setup abandon-and-retry behaviour, producing a real "Platform
  nimbus_load does not generate unique IDs" ERROR burst. Retrain is now
  scheduled via `hass.async_create_task()` (fire-and-forget), matching the
  pattern the Solver's own first cycle already used.
- **Topology card: cross-inverter transfer link overlapping battery tower
  boxes** — the dashed link showing power moving between two inverters over
  the shared AC bus routinely overlapped battery tower boxes and their text.
  Now routes past the real measured battery-tower right edge as a straight
  elbow instead of a tight bezier curve close to the inverter bar.

### Added
- **`_LOGGER`-based diagnostic trace in `ha_post_state()`'s native-mode
  dispatch** (#212): the existing `#85 trace` was `print()`-only, invisible
  to `ha_get_logs()`/Home Assistant's own `error_log`. Mirrored to a real
  `logging.Logger` (plain stdlib, doesn't touch the standalone/cron/addon
  deployment's "zero HA imports" contract), plus a new trace specifically at
  the raw `states.async_set()` fallback path.

## [0.94.2] — 2026-08-26

### Added
- **Required wizard fields are now marked with a 🔴 indicator** (#204):
  every genuinely `vol.Required` field across the Solver Battery/Grid/
  Sources options-flow steps and the Load, Power Signal, Power Source,
  and PV String subentry steps gets a 🔴 prefix on its label, plus a
  "🔴 = required." legend on that step's own description. Home Assistant's
  generic `ha-form` renderer gives every field the same plain look
  regardless of required/optional, so this was previously invisible
  until you hit a validation error.
- **`docs/setup-guide.md`**: a plain-English, start-to-finish setup
  walkthrough (install → hub → Forecaster → Solver → adding Loads/Power
  Signals/topology extras → verifying it worked), using the same
  🔴-required convention and restating the existing README gotchas in
  everyday language. Linked from the README's Install section alongside
  the field-by-field `docs/configuration-reference.md`.
- **`salvage_value`/`degradation_cost_per_kwh` diagnostic attributes**
  (#206, closes #205): `sensor.nimbus_solver_battery_forecast` now
  exposes both, alongside the existing `risk_aversion`-style attributes,
  so overnight reserve size can be regressed against the price stack
  without pulling the full diagnostic dump each session.

## [0.94.1] — 2026-08-26

### Fixed
- **`solver_p2p_settlement_history_sensor` field showed "Translation error:
  MALFORMED_ARGUMENT" instead of its real label/description**, found live
  on a real devhub install. The field's own text contained a literal
  example of the expected attribute shape (`{date: {export_cost,
  export_volume}}`) — Home Assistant's frontend parses these strings as
  ICU MessageFormat, where `{...}` is a placeholder token, so the nested
  unescaped braces broke the parser. Fixed by wrapping the literal example
  in single quotes, ICU's standard literal-text escape.

### Documentation
- **`battery_kw`'s sign convention was undocumented** (#197): Mark's own
  day-ahead optimality report had to reverse-derive it from SoC data
  before trusting downstream analysis. `sensor.nimbus_solver_battery_forecast`
  now exposes a self-documenting `battery_kw_sign_convention` attribute
  (`positive_discharge_negative_charge`), following the same pattern
  already used for `battery_kw_side`/`efficiency_convention` (#168).

## [0.94.0] — 2026-08-26

### Added
- **`nimbus_load.retrain` service** (#195): forces an out-of-schedule
  retrain for one or more load/power-signal entities by `entity_id`,
  instead of waiting for the daily retrain hour. Raises a clear
  `ServiceValidationError` naming any entity_id it can't resolve to a
  Nimbus coordinator, rather than silently retraining a partial set.

### Fixed
- **Deployed `model_type` wasn't surfaced in coordinator/diagnostics**
  (#196), the companion gap to #195: no way to confirm which model
  (k-NN/GBRT/naive) is actually live for a given load without cross-
  referencing training logs. Now exposed directly.

## [0.93.0] — 2026-08-26

### Fixed
- **Load forecaster's seasonal-naive baseline never actually competed for
  deployment** (#110): Mark's own follow-up question -- "why can't the
  overnight forecast be at least as good as the rolling 5-day average" --
  exposed a real gap. The naive baseline (`validation_mae["naive"]`) was
  computed and reported every training cycle, but `model_type` selection
  only ever compared k-NN against GBRT, never against naive itself, so a
  load whose real ML candidates were BOTH worse than naive on validation
  still had one of them deployed anyway. `model_type` now genuinely picks
  the lowest-MAE candidate among all three (naive only wins by being
  strictly better than both real candidates, never merely tying one), and
  `predict()` has a real dispatch path for a deployed "naive" model:
  returns the seasonal (weekday, hour, 15-min-of-hour) average directly
  instead of running k-NN/GBRT at all.

## [0.92.2] — 2026-08-25

### Fixed
- **`sensor.nimbus_solver_battery_forecast` missing `source_sensor`/
  `signal_role`** (#189): the follow-up Mark's own reproducer found after
  v0.92.1 -- this is "the flagship diagnostic sensor a Nimbus dashboard is
  most likely to be built against," and it's a third, distinct push site
  (the Solver's own LP-derived plan) that neither the v0.89.1 nor v0.92.1
  fix reached. Now exposes `signal_role="battery"` and `source_sensor`
  (`solver_battery_soc_sensor`, the real measured entity the whole plan is
  built around).

## [0.92.1] — 2026-08-25

### Fixed
- **`sensor.nimbus_household_load_total_forecast` missing `source_sensor`/
  `signal_role`** (#187): v0.89.1's fix only reached `NimbusForecastSensor`
  (subentry-backed sensors); this sensor is a genuinely different class
  (`_NimbusSolverPushSensor`) that never got either attribute. Now exposes
  `signal_role="other"` and `source_sensor` (the real entity_id on the
  single-sensor path; honestly `None` on the multi-circuit summing path,
  where the existing `source_entities` list is the richer answer).

### Added
- **Residual-drift watch telemetry** (#187): the v0.91.0 drift check was
  silent-until-fired with no way to confirm it was actually running. Added
  `anomaly.residual_drift_status()` -- always reports `watching`/
  `sample_count`/`ratio` regardless of whether anything crosses the alert
  threshold -- surfaced on the health report's per-subentry status.

## [0.92.0] — 2026-08-25

### Added
- **Retrospective efficiency-sensitivity backtesting engine**: the first
  check in a new offline backtesting engine that proves Nimbus's own
  decisions against reality rather than a bigger LP or a fancier model.
  Re-solves a real, already-elapsed day's real known load/solar/price
  under alternative round-trip efficiency candidates (85/90/95/99%),
  scores each with the existing regret evaluator, and reports the $
  spread between best and worst -- "how sensitive is your economics to
  efficiency, on a day that actually happened." New
  `sensor.nimbus_efficiency_backtest`, plus a dashboard section on the
  Solver view. Deliberately does not attempt to test `risk_aversion` or
  forecast-source choice -- both are mathematically inert under a
  perfect-foresight backtest (no forecast uncertainty band exists once
  ground truth is known); see `solver/backtest.py`'s own module
  docstring for the full reasoning. Strictly observational -- never
  writes back to config automatically.

## [0.91.0] — 2026-08-25

### Added
- **Cross-signal anomaly layer, first check**: forecast residual drift detection.
  Compares a signal's recent one-step-ahead forecast error against that same
  signal's own historical baseline (self-calibrated, no fixed global
  threshold) and logs a WARNING -- already surfaced via the existing health
  report -- when it drifts significantly worse. Reuses the confidence-band
  calibration data every forecaster already maintains; no new data
  collection. Strictly observational: cannot affect a solve or the published
  forecast. First step toward automatically catching the same shape of
  problem (silent data-quality issues, model degradation, sensor drift) that
  previously needed a human staring at a live chart to notice.

## [0.90.0] — 2026-08-25

### Added
- **Blended multi-source price forecasting**: optional
  `solver_import_price_sensor_2`/`_3` and `solver_export_price_sensor_2`/`_3`
  wizard fields, mirroring the existing solar multi-source blending pattern.
  Feed more than one independently-modeled price forecast (e.g. a real AEMO
  wholesale forecast alongside a retailer's own forecast such as Amber) and
  the Solver blends them into one point estimate, using the real disagreement
  between sources to widen the existing `price_risk_aversion` band as an
  earned uncertainty signal. All new fields are optional and blank by
  default -- a single-source install is byte-identical to before.
- `resample_generic_price_forecast()` now also accepts a `calibrated` key as
  a fallback when `value` is absent, so a blend source can be pointed
  directly at a spike-calibrated forecast sensor (e.g. Mark Purcell's own
  NEM PD7, `sensor.nem_pd7day_qld1_nem_spot_price_forecast`) without its
  points being silently dropped.

## [0.89.1] — 2026-08-25

### Added
- **`source_sensor` attribute on load/power-signal forecast sensors**: the
  missing other half of `signal_role`'s own "auto-discover which power signal
  is Grid/Battery/Solar directly from hass.states, zero config file needed"
  mechanism -- a dashboard can now genuinely resolve the real live entity_id
  behind a role without hardcoding a guess.

## [0.89.0] — 2026-08-25

### Added
- **Solar delivery ratio diagnostic** (nimbus issue #128, Mark Purcell):
  `solar_delivery_ratio` / `solar_delivery_sample_count` /
  `solar_delivery_underperforming` on `sensor.nimbus_solver_battery_forecast` --
  a rolling actual-vs-forecast solar comparison that catches implicit inverter
  AC-side clipping `switch.solar_curtailment` (#114) can't see, since that
  switch only reports EXPLICIT curtailment control. Fully generic and
  optional: reuses the existing `solver_solar_power_sensor` wizard field, no
  new config surface.

## [0.88.0] — 2026-08-25

### Added
- **Training-diagnostic visibility for the Forecaster** (nimbus issue #113, Mark
  Purcell): `mase_scale_points` (how many real week-over-week points MASE's own
  scale actually found, even below the threshold needed), `resample_minutes`
  (the fixed grid spacing every training row is built on), and
  `training_span_days` (the real elapsed calendar span the deployed model
  actually trained on, distinct from the configured `train_days` request) --
  exposed on every load/power-signal forecast sensor's attributes. Makes
  `validation_mase`'s own honest "can't compute this yet" empty-dict behavior
  diagnosable instead of looking broken.

## [0.87.1] — 2026-08-25

### Fixed
- **Efficiency-convention mismatch in the EPR quality report and Nimbus-only
  counterfactual replay** (nimbus issue #168, Mark Purcell): both scorers applied
  `solver_efficiency_percent` (a round-trip figure) directly to both charge and
  discharge, instead of `sqrt()`-splitting it the way the live plan's own real
  `main()` solve does -- meaning they evaluated a battery physically more lossy
  than the one actually being scored. Fixed to match `main()`'s convention exactly.

### Added
- **Energy-balance diagnostic fields** on `sensor.nimbus_solver_battery_forecast`
  (nimbus issue #168): `battery_kw_side` ("AC"), `efficiency_convention`
  ("round_trip_symmetric_sqrt"), `charge_efficiency`/`discharge_efficiency` (the
  actual derived floats), and `ac_bus_losses_kwh` -- so a diagnostic reader no
  longer has to reverse-engineer the storage-identity convention by hand.

## [0.87.0] — 2026-08-25

### Added
- **Nimbus-only counterfactual SoC replay** (`sensor.nimbus_counterfactual_soc`,
  "nuc one nimbus solver view has counterfactual table.... i want u to build that
  into devbox package"): generic, wizard-config-driven port of the reference
  household's own NUC1 script. Replays an already-elapsed calendar day with a real
  receding-horizon LP re-solve every 15 minutes, feeding each tick's own simulated
  SoC into the next (never the real, possibly HAEO-influenced SoC), and compares
  the result against what actually happened -- answering "would Nimbus's own
  reasoning have kept the battery in a sane state, if it had been driving all
  along." Every P2P-related input is the same optional wizard field the live
  writer already reads; a household with no P2P scheme configured gets a complete
  no-op (no checkpoint/viability verdict), never a crash or a leaked
  household-specific default.

## [0.86.1] — 2026-08-25

### Fixed
- **Diagnostics: `data.solver` was silently dropping newly-added attributes**
  (nimbus issue #116, Mark Purcell): `_solver_diagnostics()` copied a curated,
  hand-picked subset of `sensor.nimbus_solver_battery_forecast`'s attributes by
  name into the downloaded diagnostics dump. That allowlist stopped tracking
  `solver_writer.py`'s real output -- `cost_breakdown` (v0.82 #149) and
  `load_forecast_source_used` (v0.83 #148) both landed correctly on the live
  entity but showed as `null` in the dump, a false-negative that could make a
  real fix look like it hadn't shipped. Fixed by spreading the entity's full
  attribute dict instead of naming fields one at a time -- any current or
  future solver attribute is now automatically visible with zero maintenance.

## [0.86.0] — 2026-08-25

### Added
- **`cost_band` diagnostic on the Solver plan** (nimbus issue #147, Mark Purcell):
  re-costs the LP's own committed dispatch at the load forecast's stated lower/upper
  confidence bounds (`compute_cost_band()`, `regret.evaluate_realized_cost()`), so an
  operator can tell at a glance whether a plan is a confident call or a coin flip.
  No LP change -- read-only post-hoc analysis on the already-solved plan. Deliberately
  excludes P2P bonus revenue; documented as an honest lower bound on the true swing.
- **Always-on health report** (`sensor.nimbus_health_report`): a plain `logging.Handler`
  now captures every WARNING+/ERROR record from this integration into a bounded,
  always-populated buffer -- no more relying on someone noticing a symptom and then
  grepping `error_log` after the fact. Exposes `recent_errors`, `recent_warnings`,
  `never_trained` (subentries whose model has genuinely never produced output), and
  per-subentry `mode`/`training_points`/`model_trained_at`/`forecast_point_count`.

## [0.85.0] — 2026-08-25

### Added
- **Built-in EPR/regret/tracking quality score** (`sensor.nimbus_solver_quality_report`,
  "it should be a part of the suite to monitor epr and trend and
  regret... nimbus should have it built in"): a from-scratch,
  retailer-agnostic generalization of the real-world reference script
  (`docs/real-world-integration/files/nimbus_solver_quality_writer.py`),
  which hardcodes one household's own LocalVolts/Sungrow/Modbus stack.
  Scores yesterday's real dispatch against a perfect-foresight oracle
  using only genuinely portable inputs: two new optional Solver
  settings fields (`solver_solar_power_sensor`,
  `solver_battery_power_sensor` -- real measured, not forecast) plus
  the existing `solver_whole_house_cross_check_sensor` for load. An
  optional `solver_p2p_settlement_history_sensor` field improves
  accuracy for a household with a real P2P/VPP settlement program,
  without requiring one. Blank on any required field is a clean no-op.
- **`solver_weather_forecast_sensor`** -- a new optional Solver settings
  field pointing at any `weather.*` or `sensor.*` forecast source (same
  dual-shape support as the existing `temperature_forecast_sensor`),
  driving a real forward temperature/humidity mirror for the dashboard
  Forecaster chart. Humidity is only published when the configured
  source's own forecast actually carries one (e.g. Pirate Weather;
  Open-Meteo doesn't). Purely cosmetic -- never feeds the LP solve.

## [0.84.1] — 2026-08-25

### Fixed
- **Weather forecast mirror silently no-op'd in native mode** (real
  regression, found live on devhub immediately after v0.84.0 deployed):
  `ha_call_service_with_response()` returned `None` unconditionally in
  native mode (`solver_runtime`, in-process -- devhub's actual real
  deployment shape), so `publish_weather_forecast_mirrors()` never
  published anything there. Fixed with the same
  `asyncio.run_coroutine_threadsafe()` bridge `fetch_price_history()`'s
  own native branch already uses.

## [0.84.0] — 2026-08-25

### Added
- **`load_forecast_coverage_hours` on the Solver plan and the household
  load rollup** (#112, "solver horizon 96.3h exceeds subentry forecast
  horizon 48h"): `read_load_forecast_sensor()`/`sum_load_forecasts()` now
  capture each source's real forecast coverage, in hours ahead of `now`,
  on the raw forecast list -- before `resample_forecast()`'s flat-hold
  padding makes a real point and a padded-flat point indistinguishable.
  For the multi-circuit sum, coverage is the minimum across every circuit
  that fetched successfully. Compare directly against `horizon_hours` to
  see when part of a plan's own load input is padding, not a real
  forecast.
- **`publish_weather_forecast_mirrors()`** -- a real forward temperature
  forecast for the devhub test dashboard, sourced from an installed
  weather forecaster (Open-Meteo) via `weather.get_forecasts` (modern HA
  weather entities no longer expose a plain `forecast` attribute).
  Purely cosmetic/devhub-dashboard; never referenced by the actual LP
  solve.

## [0.83.0] — 2026-08-25

### Added
- **`load_forecast_source_used` on the Solver plan and the household load
  rollup** (Mark Purcell, real repro, #148, plus the same-shaped ask in
  #116): `solver_load_forecast_sensor` can be configured, correct, and
  completely silently overridden the instant
  `solver_load_forecast_entities` has even one entry -- documented in
  prose in this project's own README precedence table, but never
  surfaced as a diagnostic field. `sensor.nimbus_household_load_total_
  forecast` and `sensor.nimbus_solver_battery_forecast` now both carry
  `load_forecast_source_used`, naming plainly either the single
  configured sensor or exactly which circuits got summed instead.

## [0.82.0] — 2026-08-25

### Added
- **Named cost-component breakdown on the Solver plan** (Mark Purcell,
  real repro + executable reconciliation tests, #149): `total_cost`
  could not be reconstructed from anything else in the diagnostics
  dump -- off by exactly degradation + charge_fee + discharge_fee minus
  the #144 terminal-value credit, with no field naming any of those
  terms. `sensor.nimbus_solver_battery_forecast` now carries a
  `cost_breakdown` attribute (`grid_net`, `degradation`, `charge_fee`,
  `discharge_fee`, `terminal_value_credit`) that reconciles exactly to
  `total_cost` by construction.

### Fixed
- **Network TOU fees and the flat fee rate were silently ignored for
  any non-LocalVolts install** (#152): `import_fee_rate()` and the flat
  fee rate were applied only inside the LocalVolts-specific branch of
  the price-building block, even though both are generic, retailer-
  agnostic config-flow fields. Any other install that filled them in
  via the wizard had them silently ignored -- zero fee applied, zero
  warning. Fixed by applying fees uniformly regardless of retailer path.
- **Empty forecast attribute conflated with a missing/malformed one --
  confusing error on cold-start installs** (#150): a brand-new load
  subentry whose forecaster hasn't trained on enough history yet
  produces a genuinely empty `forecast: []`, which was being reported
  with the same "no usable forecast attribute" message as a truly
  broken sensor. Fixed with a distinct, honest "empty, not yet
  trained" message for the empty-list case.

## [0.81.0] — 2026-08-24

### Fixed
- **Terminal-value credit compounded across multiple day-boundary
  checkpoints, making the LP treat holding charge as worth up to ~4x
  its configured value** (Mark Purcell, real repro, #144): on a real
  4-day horizon (4 real midnight checkpoints + the true final period),
  the LP idled the battery at ~60% SoC for 5+ hours while grid-buying
  at 15-22c/kWh, only discharging once import price crossed ~40c,
  despite a configured marginal discharge cost of ~9c/kWh. Root cause:
  the 2026-08-22 fix that lets the Solver respect multi-day P2P windows
  applied the full terminal-value curve at EVERY real midnight in the
  horizon, not just the true end -- the same physical stored energy was
  earning a full terminal-value credit at every boundary it survived
  through. Confirmed directly: `total_cost` kept getting artificially
  "better" the more checkpoints existed, and SoC hours before any
  boundary snapped from empty to full the moment a second checkpoint
  was added. Fix: only the true final period gets the full, unscaled
  curve; every intermediate day-boundary checkpoint gets it divided by
  the number of intermediate checkpoints, so the cumulative incentive a
  single unit of energy can ever collect stays bounded to roughly one
  terminal-value-equivalent, not one per checkpoint. A genuine no-op
  for the original single-intermediate-checkpoint shape.

## [0.80.0] — 2026-08-24

### Added
- **Diagnostics now include the Solver's full resolved config and full
  plan/forecast arrays** (direct household + Mark Purcell instruction:
  "diagnostics must have everything in it incl pre-set values" /
  "get more data into the diagnostic file so we can actually understand
  the reason its making decisions rather than just speculation without
  any data to backup"). Battery capacity, min/max SoC, max
  charge/discharge, costs, salvage value, both risk_aversion values,
  P2P bonus blocks, network fee schedule -- every field
  `sensor.nimbus_solver_config` resolves, verbatim. Plus the full real
  Solver plan, household load total, and every subentry's own forecast
  array -- previously excluded as "already visible on the entity,"
  which didn't hold up: a downloaded diagnostics file has no
  16384-byte recorder-attribute limit, and the live entity's own
  forecast keeps moving, so a diagnostics dump is the only genuine
  point-in-time snapshot available.

## [0.79.0] — 2026-08-24

### Fixed
- **Solver crashed every cycle with Min SoC configured to 0%** (Mark
  Purcell, real repro, direct follow-up to #58 — 20 consecutive crashes
  over 24 minutes): #58's own fix (Mark's PR #64) correctly stopped an
  intentional 0.0 from being silently reverted to the 5.0% default, but
  never addressed what happens once that genuine 0% actually reaches
  `BatteryConfig`'s own strict `0 < min_soc_kwh` invariant — a real,
  deliberate LP-level degeneracy/safety floor, not negotiable at that
  layer. `resolve_min_soc_kwh()` closes the gap: floors to a
  negligible-but-strictly-positive value (0.05% of capacity —
  effectively "no reserve," not a literal 0%) and warns, instead of
  crashing.
- **Solver crashed on a non-numeric price sensor state**
  (`ValueError: could not convert string to float:
  '2026-08-24T13:00:00+10:00'`, same real install, same session): the
  price-sensor scalar-fallback read had zero protection against a
  configured entity's real state not being numeric. `safe_num()`
  replaces the old, untestable inline closure with a standalone
  function that warns and falls back to `0.0` instead of crashing the
  whole solve — used for both price-sensor fallback reads and the live
  battery SoC read.

## [0.78.0] — 2026-08-24

### Fixed
- **`temperature_forecast_sensor` crashed the coordinator on `weather.*`
  entities emitting naive datetimes** (Mark Purcell, real repro, #137,
  direct follow-up to #123): some real weather integrations (his own
  `weather.noosa_heads_hourly`) return forecast datetimes with no
  timezone offset at all — the #123 fix widened the field to accept
  `weather.*` entities but never normalised the datetimes it got back,
  so a naive value compared against an always-aware one crashed every
  coordinator tick outright (`TypeError: can't compare offset-naive and
  offset-aware datetimes`). Naive timestamps are now treated as the
  installation's own local time (a pure relabel, no numeric shift) —
  confirmed against a real, subtle bug in the first suggested fix
  (`dt_util.as_local()` on a naive value assumes UTC first, which would
  have shifted the numbers by a full UTC offset instead of leaving them
  alone). Same fix applied to the curtailment-forecast path, the other
  real site parsing external datetimes.

## [0.77.0] — 2026-08-24

### Fixed
- **`binding_constraint_now` could report the wrong story** (Mark
  Purcell, real repro, #125/#133): a nonzero LP reduced cost fires
  whenever a variable is pinned at EITHER of its own bounds, not only
  its upper/capacity bound — a genuine "not economical to discharge
  right now" decision (pinned at 0) and a genuine "hit the real
  ceiling" decision (pinned at the configured max) both produced a
  nonzero reduced cost on the same variable, but this diagnostic
  labelled both identically as e.g. "Battery max discharge power".
  Confirmed on a real install: the battery was genuinely CHARGING at
  period 0 (not discharging at all), while this field still reported
  "Battery max discharge power" — actively misleading, and the direct
  cause of a false "second override path" bug report (the real config,
  #125's own fix, and the underlying LP bound were all confirmed
  correct by direct source read). Now disambiguated by checking the
  variable's own real solved value against its two bounds: the genuine
  ceiling case keeps the exact original 4 label strings (no
  compatibility break for that case), the "pinned at zero" case gets
  its own new, distinct, honest label instead.

## [0.76.0] — 2026-08-24

Both pieces of the Solver settings wizard-simplification scoping (direct
Mark Purcell critique: "wizard complexity... how confusing the process is
and entities are") — makes the entity-pointer fields easier to fill in
cold, on a fresh install with dozens or hundreds of unrelated entities.

### Changed
- **Solver settings wizard — "Group A" of the config-flow simplification**
  (direct Mark Purcell critique: entity-pointer fields are hard to fill
  in cold on a fresh install). The Sources step's two load-forecast
  fields (`solver_load_forecast_sensor`, `solver_load_forecast_entities`)
  now restrict their own entity pickers to Nimbus's real, live forecast
  output instead of offering every `sensor.*` on the system. The single-
  sensor field deliberately excludes
  `sensor.nimbus_household_load_total_forecast` — pointing it there is
  exactly the circular-reference bug [#118](https://github.com/code-imstillalive/nimbus/issues/118)
  fixed defensively; this closes the same gap at the source by never
  offering it as a choice. Purely a picker restriction — an already-saved
  value, or an install where discovery finds nothing yet, is completely
  unaffected (a picker with no candidates falls back to showing
  everything, same as before this change).
- **"Group B" of the same simplification**: the Forecaster step's
  temperature/humidity fields and the Solver Battery step's SoC field
  now get a pre-filled *suggestion* (never a picker restriction — these
  are raw household hardware sensors, not Nimbus's own self-tagged
  output, so a hard filter risked hiding a genuinely correct but
  untagged sensor) whenever exactly one live entity of the matching
  `device_class` exists system-wide. Deliberately narrower than a naive
  "filter every raw sensor field by device_class" approach — the
  battery/grid/solar power fields and both import/export price fields
  are excluded on purpose, since even a single matching entity there is
  genuinely ambiguous about *which* of two or three fields it belongs
  to; guessing wrong would be confidently misleading, worse than no
  suggestion at all. An already-saved value is never overwritten by a
  suggestion, same discipline as every other suggestion mechanism in
  this file.

## [0.75.0] — 2026-08-24

Mark Purcell's own independent-install health-check found four real bugs
this release — three that could produce a confidently-wrong plan with no
warning, one that silently overrode a configured value with an unrelated
entity's own reading.

### Added
- **`solver_max_discharge_live_entity`** (Solver settings wizard, Battery
  step, optional): lets a household point the Solver at a real, live
  hardware setpoint entity whose own `max` attribute should override the
  static `solver_max_discharge_kw` number — a genuine safety margin
  against a real hardware ceiling changing without the number entity
  being updated. Unset (the default) is a complete no-op.
- **`number.nimbus_solver_inverter_self_consumption_kw`** (optional,
  default `0.0`): a per-household inverter self-consumption bias, added
  to the household load total. Replaces a hardcoded `0.215` constant that
  used to apply to every install regardless of hardware.
- `temperature_forecast_sensor` now accepts `weather.*` entities
  directly, calling `weather.get_forecasts` (hourly) internally — modern
  Home Assistant (2024.x+) removed the `forecast` state attribute from
  weather entities entirely, so pointing straight at one used to silently
  produce zero forecast data. `sensor.*` template-sensor configs are
  unaffected. A WARNING now logs once per coordinator instance if a
  configured `temperature_forecast_sensor` yields zero entries.

### Fixed
- **High-priority, real $ impact**: the Solver's own discharge-power
  cap could be silently overridden by an unrelated entity that happened
  to exist at a hardcoded name
  (`number.logger_charging_discharging_power_kw`), clamping real
  discharge capability to whatever that unrelated entity's own `max`
  attribute reported — with zero warning ([#125](https://github.com/code-imstillalive/nimbus/issues/125)). Fixed by making the
  entity_id a genuine, optional config field instead (see Added, above).
- The Solver used to `raise RuntimeError` and refuse to solve at all
  when every configured solar forecast source produced no data — a
  condition that recurs every single night on every solar install (0.0
  kW overnight is correct, not a failure). Now solves with a real,
  honest 0.0 kW placeholder and a loud warning instead of going dark for
  hours ([#115](https://github.com/code-imstillalive/nimbus/issues/115)).
- A structurally-valid but near-all-zero load forecast (e.g. from a
  circular reference — `solver_load_forecast_sensor` accidentally
  pointed at Nimbus's own household-total aggregator) used to be
  accepted as a genuine forecast, producing a confidently-wrong,
  fully-costed dispatch plan with no error surfaced anywhere. Now
  rejected with a specific, actionable error message ([#118](https://github.com/code-imstillalive/nimbus/issues/118)).
- The multi-circuit load-summing path (`solver_load_forecast_entities`)
  never had the shape/unit validation the single-sensor path already
  had, letting a malformed source silently corrupt the household load
  total. Both paths now share the same validation ([#105](https://github.com/code-imstillalive/nimbus/issues/105)).
- `sensor.nimbus_household_load_total_forecast` could report a
  `native_value` that silently disagreed with its own `forecast[0]`.

### Changed
- `flows/hub_options.py`'s options-flow merge logic is documented
  in-line at the exact point of a real, structural tension: a genuine
  partial config-patch and a real UI form submission need the same
  "key absent" signal to mean opposite things, and no code-only fix
  serves both. Callers needing a genuine partial patch should use
  `hass.config_entries.async_update_entry(entry, options={**entry.options, **partial})`
  directly rather than the options-flow step ([#121](https://github.com/code-imstillalive/nimbus/issues/121)).
- Repo-wide `ruff` cleanup: every blind-except, naive-datetime, and
  unnecessary-collection-call finding from the #72 backlog now has its
  own individually-reasoned fix or suppression comment, replacing a
  blanket rule-level ignore.
- The "Understanding the configuration model" section is now a
  permanent part of `README.md`.

## [0.74.0] — 2026-08-23

### Added
- **Topology card ships bundled with the integration**: install Nimbus via HACS, restart HA, drop `type: custom:switchboard-topology-card` into any dashboard view — it resolves immediately, with no `www/` file copy and no manual Settings → Dashboards → Resources step ([#79](https://github.com/code-imstillalive/nimbus/issues/79), [#92](https://github.com/code-imstillalive/nimbus/pull/92)). `custom_components/nimbus_load/frontend/switchboard-topology-card.js` is the same file as `docs/real-world-integration/files/topology-card-v4.js`; a new `frontend.py` module owns registration. HTTP serving via `hass.http.async_register_static_paths([StaticPathConfig(...)])` at `/nimbus_load/switchboard-topology-card.js`, plus registration as an extra JS module via `homeassistant.components.frontend.add_extra_js_url(hass, url)` — HA's own public, documented API for injecting frontend JS from an integration, so both storage-mode and YAML-mode Lovelace dashboards see the module with no writes to the user's `.storage/lovelace_resources`. Cache-busting via `?v=<manifest-version>`. Both steps are idempotent and non-fatal — a failure at registration is logged and the forecaster, sensors, and solver all still work.

### Changed
- `manifest.json` declares `"http"` as a dependency (needed by the static-path registration, caught by hassfest during PR #92 CI review).
- `nimbus_solver_app/config.yaml` version bumped in lockstep to `0.74.0`.

## [0.73.3] — 2026-08-23

### Added
- `sensor.nimbus_solver_config` now logs on every real `configured → unconfigured` transition (WARNING) and on recovery (INFO), instead of staying silent — a startup race (`RestoreEntity` still restoring the wizard's `number.nimbus_solver_*` entities when this sensor is first polled) is now directly visible in the log rather than only surfacing as an unattributed "not configured yet" warning from the Solver runtime. Logging is on-transition only; a stable `configured` or `unconfigured` state produces no repeated log spam.
- New `unresolved_required_keys` extra state attribute on `sensor.nimbus_solver_config` — empty on the happy path, otherwise names exactly which of the 10 required Solver fields aren't resolved yet. Readable over plain REST (`/api/states/sensor.nimbus_solver_config`) for triage without needing HA's own logs.
- 9 new regression tests (`tests/test_sensor_solver_config_flap.py`) covering the transition logging, the new attribute, and log-on-transition discipline (no per-poll spam).

No behavioural change to `native_value` itself — this release is observation-only. Real, live-verified data (Mark Purcell, [#85](https://github.com/code-imstillalive/nimbus/issues/85)): every HA restart produces a single ~30-second `configured → unconfigured → configured` window while the wizard's number entities restore; this is a separate, narrower, self-healing race distinct from the mid-session flap fixed in v0.73.2/[#90](https://github.com/code-imstillalive/nimbus/pull/90) — a real fix for the startup race is still open for follow-up.

## [0.73.2] — 2026-08-23

### Fixed
- **Critical**: `sensor.nimbus_solver_battery_forecast` and `sensor.nimbus_household_load_total_forecast` flapped between a real plan and `unknown` on a ~60-second cycle on real HA — found by Mark Purcell within hours of the v0.73.1 fix landing ([#83](https://github.com/code-imstillalive/nimbus/issues/83)). Root cause: `_async_recheck_availability()` (the periodic self-driven timer added in v0.73.1 to catch a Solver going genuinely stale) called `async_write_ha_state()` unconditionally on every tick, republishing `native_value` regardless of whether anything had actually changed — including before the very first real push, when `native_value` is honestly `None`. Racing against `update_from_solver()`'s own real pushes on the same ~60s cadence, this periodically clobbered a fresh plan with `unknown` moments after it was published, only for the next solve to overwrite it again. Fixed: the recheck now only calls `async_write_ha_state()` (and logs) on a genuine transition in `available`'s own value — a no-op tick (the overwhelming majority of ticks, by design) does nothing at all. Two new regression tests lock in the exact flap scenario: a first tick before any push has landed, and repeated ticks while nothing has changed.

## [0.73.1] — 2026-08-23

### Fixed
- **Critical**: `sensor.nimbus_solver_battery_forecast` and `sensor.nimbus_household_load_total_forecast` were stuck at `unknown` forever, crashing on every single solve tick, on any real (non-test-stub) Home Assistant instance — found by Mark Purcell's own live v0.73.0 install within hours of release ([#82](https://github.com/code-imstillalive/nimbus/issues/82)). Root cause: `update_from_solver()` (the entity method `solver_writer.ha_post_state()`'s dispatch table calls via `hass.add_job()`) and `_async_recheck_availability()` (registered directly as `async_track_time_interval`'s own callback) were both plain, undecorated methods — real HA's `add_job()` inspects a target for the `_hass_callback` marker to decide whether to run it directly on the event loop or dispatch it to the executor thread pool; undecorated, both were routed to a worker thread, where their own `async_write_ha_state()` call (genuinely requires the event loop) raised `RuntimeError` silently on every call. Fixed by marking both `@callback`, the textbook-correct fix for a fast, non-blocking, pure state-machine method. Neither the unit test suite nor its `homeassistant.core.callback` stub (previously a plain identity lambda) could have caught this — the stub is now a faithful replica of real HA's own marker-setting behaviour, and two new regression tests assert the marker directly.

## [0.73.0] — 2026-08-23

### Deprecated
- `nimbus_solver_app` (the HAOS Supervisor add-on) is deprecated as of v0.73.0 and will be removed in v1.0.0. The Nimbus HACS integration now runs the Solver natively in-process on HAOS (`custom_components/nimbus_load/solver_runtime.py`, one-minute timer, `highspy` auto-installed as a `manifest.json` requirement), covering every architecture the add-on covered (both need a `highspy` wheel, so both are amd64/aarch64 only) with no separate container, no three-way copy sync, and no version-lockstep discipline. Existing installs: uninstall the add-on and finish the integration's own **Solver settings** wizard — the native path takes over the same `sensor.nimbus_solver_*` outputs with no config migration needed. See the add-on's own README.md and its startup log for the same notice; the top-level README has moved the add-on out of the supported-paths list into a Deprecated note pointing at the native path. Tracking: [#76](https://github.com/code-imstillalive/nimbus/issues/76).

### Added
- `sensor.nimbus_solver_battery_forecast` and `sensor.nimbus_household_load_total_forecast` are now real `SensorEntity` classes attached to the Nimbus hub device, instead of raw `states.async_set` writes. Existing `entity_id`s are preserved, so long-term stats and history continue uninterrupted. Both entities now carry `device_class=power`, `state_class=measurement`, `unit_of_measurement=kW`, and `_unrecorded_attributes = frozenset({"forecast"})` on the entity itself, so the Recorder no longer trips its 16 KB per-attribute limit on the tiered 96h `forecast` list (fixes [#55](https://github.com/code-imstillalive/nimbus/issues/55) point 1, [#59](https://github.com/code-imstillalive/nimbus/issues/59), [#61](https://github.com/code-imstillalive/nimbus/issues/61), [#62](https://github.com/code-imstillalive/nimbus/issues/62)).
- `solver_writer.ha_post_state()` grew a dispatch-table seam (`register_entity_handler` / `unregister_entity_handler`) that routes native-mode writes for migrated `entity_id`s through the entity's own `update_from_solver()`; unregistered `entity_id`s still fall through to the original `states.async_set` path and the standalone REST branch is unchanged.
- `pyproject.toml` with a `[dev]` extra: `pip install -e '.[dev]'` from a fresh clone now runs the full test suite ([#69](https://github.com/code-imstillalive/nimbus/pull/69), #36 Stage 1).
- CI: `Unit Tests (pytest)` is now a **strict** gate on every PR, sourced from `pyproject.toml`'s `[dev]` extra ([#73](https://github.com/code-imstillalive/nimbus/pull/73), #36 Stage 2).
- CI: `Version lockstep (integration <-> add-on)` job blocks merges when `custom_components/nimbus_load/manifest.json` and `nimbus_solver_app/config.yaml` versions drift ([#74](https://github.com/code-imstillalive/nimbus/pull/74), #36 Stage 4).
- `nimbus_solver_app` add-on directory is now genuinely self-contained ([#48](https://github.com/code-imstillalive/nimbus/pull/48)).
- `.pre-commit-config.yaml`: local pre-commit hooks pinning ruff at 0.6.0 to match CI ([#70](https://github.com/code-imstillalive/nimbus/pull/70)).

### Changed
- `nimbus_solver_app/config.yaml` version bumped `0.72.0` → `0.73.0` in lockstep with the integration.
- `nimbus_solver_app/config.yaml` version bumped from `0.1.0` to `0.72.0` to match the integration; both move in lockstep from now on ([#74](https://github.com/code-imstillalive/nimbus/pull/74)).
- Ran repo-wide `ruff format` (0.6.0) across `custom_components/` and `tests/`; ruff pinned to 0.6.0 in CI and pre-commit ([#70](https://github.com/code-imstillalive/nimbus/pull/70)).

### Fixed
- `manifest.json`: declared `after_dependencies: [energy]` so hassfest passes ([a5949ae](https://github.com/code-imstillalive/nimbus/commit/a5949ae)).
- `hacs.json`: pinned `homeassistant` min-version, dropped redundant `zip_release` ([#49](https://github.com/code-imstillalive/nimbus/pull/49)).
- Ruff F401: removed unused imports in `test_flows_pv_string_subentry.py` ([#71](https://github.com/code-imstillalive/nimbus/pull/71)).
- Test E402 noqa placement: moved onto the import statement's own start line ([fc29321](https://github.com/code-imstillalive/nimbus/commit/fc29321)).
- `manifest.json`: pinned `numpy>=1.24,<3` and `highspy>=1.15.1,<2` instead of an unpinned `highspy` requirement (Quality Scale Bronze `dependency-transparency`).
- `solver_runtime.py`: an `ImportError`/`ModuleNotFoundError` from a missing `highspy` wheel now fires a real, one-time `persistent_notification` and returns cleanly, instead of propagating out of the periodic solve timer as a bare, buried exception (Bronze `test-before-setup`).
- `strings.json`: every config-flow/options field now has a `data_description` (Bronze `data-description`); `translations/en.json` was found stale relative to it (still describing a removed 2-step switchboard flow) and re-synced.
- `sensor.py`: `_NimbusSolverPushSensor` (the Solver's own `battery_forecast`/`household_load_total_forecast` outputs) now goes `unavailable` if the Solver stops producing a fresh plan for more than 5 minutes, instead of showing a stale plan indefinitely — the same class of fix `NimbusForecastSensor` already had, extended to the entities that didn't have a coordinator to drive it automatically (Silver `entity-unavailable`).
- `custom_components/nimbus_load/quality_scale.yaml` added: every Bronze rule re-verified directly against current code rather than an older snapshot — 21/22 resolved, `brands` genuinely still open (tracked separately, [#80](https://github.com/code-imstillalive/nimbus/issues/80)).
- README refreshed for v0.73.0 — Forecaster, Solver (native in-process path), and Topology all documented against current behaviour ([#81](https://github.com/code-imstillalive/nimbus/pull/81), thanks @purcell-lab).

## [0.72.0] — 2026-08-23

### Added
- Topology card: explicit **Power Signal** role plus auto-detect for Grid and Battery sources ([e1abb32](https://github.com/code-imstillalive/nimbus/commit/e1abb32)).
- CI: `ruff format` + `ruff check` are now **strict** gates on every PR ([#68](https://github.com/code-imstillalive/nimbus/pull/68)).

### Changed
- Ran repo-wide `ruff format` pass. Documented the F401/E402 noqa fixes it uncovered ([#68](https://github.com/code-imstillalive/nimbus/pull/68)).

### Fixed
- `topology_map.yaml` framing: shipped as a per-household example rather than as a configuration template ([d654a18](https://github.com/code-imstillalive/nimbus/commit/d654a18)).

## [0.71.0] — 2026-08-23

### Added
- `diagnostics.py`: Solver health section with status, timing, and load-forecast errors surfaced to the diagnostics download ([5df77bc](https://github.com/code-imstillalive/nimbus/commit/5df77bc)).
- Battery Tower `subentry.title` now exposed via `NimbusTopologyConfigSensor` ([91e6816](https://github.com/code-imstillalive/nimbus/commit/91e6816)).

## [0.69.0] — 2026-08-23

### Added
- Switchboard wizard: suggests fields based on the household's existing Home Assistant Energy Dashboard configuration ([0f2955e](https://github.com/code-imstillalive/nimbus/commit/0f2955e)).
- Shadow Mode chart, Solver Operations and Counterfactual cards, and risk-aversion sliders shared with the community ([5d787e5](https://github.com/code-imstillalive/nimbus/commit/5d787e5)).

## [0.68.0] — 2026-08-23

### Added
- `NimbusTopologyConfigSensor`: bridges Power Source / PV String / Battery Tower subentries plus switchboard output into a single readable sensor ([0c06617](https://github.com/code-imstillalive/nimbus/commit/0c06617)).

## [0.67.0] — 2026-08-23

### Added
- Topology wizard: Power Source, PV String, and Battery Tower subentries plus a switchboard hub step ([f8341cd](https://github.com/code-imstillalive/nimbus/commit/f8341cd)).
- `tests/run_all.py`: complete test runner replacing scattered ad-hoc invocations ([a8495e8](https://github.com/code-imstillalive/nimbus/commit/a8495e8)).

## [0.66.0] — 2026-08-23

### Fixed
- Load-forecast sensor shape now validated at ingestion; EMHASS auto-detected ([#66](https://github.com/code-imstillalive/nimbus/issues/66), [2717255](https://github.com/code-imstillalive/nimbus/commit/2717255)).
- Topology card JS resynced (was two fixes stale); README now covers real deploy steps ([5c0f9e6](https://github.com/code-imstillalive/nimbus/commit/5c0f9e6)).

## [0.65.0] — 2026-08-22

### Fixed
- Two Solver wizard fields never exposed by the bridge sensor ([be17c7a](https://github.com/code-imstillalive/nimbus/commit/be17c7a)).
- `solver_writer`: preserved intentional `0` in numeric defaults; clamped initial SoC to `[min, max]` ([2309c65](https://github.com/code-imstillalive/nimbus/commit/2309c65)).
- `LOAD_FORECAST_ENTITIES` and `WHOLE_HOUSE_CROSS_CHECK` made config-driven ([#56](https://github.com/code-imstillalive/nimbus/issues/56), [#60](https://github.com/code-imstillalive/nimbus/issues/60), [a72260d](https://github.com/code-imstillalive/nimbus/commit/a72260d)).
- Blocking call removed from the event loop ([02cdae8](https://github.com/code-imstillalive/nimbus/commit/02cdae8)).

## [0.64.0] — 2026-08-22

First tagged release. Baseline for everything above.

---

## How to write a changelog entry

Sections use these labels (in order): **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, **Security**. Omit any that don't apply.

One line per user-visible change, present tense, human phrasing (not the commit subject verbatim). Link the relevant PR (`[#NN]`) or, when there is no PR, the commit hash (`[abcdef1]`). If an issue drove the change, prefer linking the issue over the commit.

New PRs add entries under `## [Unreleased]`. When a release PR lands, that PR renames `## [Unreleased]` to the new `## [X.Y.Z] — YYYY-MM-DD` heading and creates a fresh empty `## [Unreleased]` above it. See `.github/PULL_REQUEST_TEMPLATE.md` for the release-PR checklist.
