# IV&V confirmation report — v0.94.113 → v0.94.114 on a live install (2026-09-05)

Filed as [#371](https://github.com/code-imstillalive/nimbus/issues/371) with sub-issues #372, #373, #374.

Independent verification and validation of the fixes shipped against the 2026-09-03 codebase review (#336, sub-issues #337–#366, #368, #370). Scope: every sub-issue that was closed between v0.94.60 and v0.94.114 (44 releases since the previously validated v0.94.69), plus the five still-open ones, checked at code level against the tagged tree and, wherever the mechanism is observable, live on one real Home Assistant install.

**Bottom line.** 24 of 29 closed findings are confirmed resolved (18 with direct live evidence, 6 at code/test level where the mechanism is not observable live). Two closed findings are **partially** resolved (#366, #370) and their interaction produced a **new high-severity outage on this install**: the v0.94.107 model-schema bump discarded all three persisted models on the first restart, the replacement retrain crashes on every attempt (`statistics_during_period` returns float timestamps, not datetimes), and with no forecast the solver publishes a confident zero-load "optimal" plan on every cycle — on v0.94.114 as well as v0.94.113. Three new sub-issues carry that. The five open findings are in the partial states their issues already describe; nothing there regressed.

## Method

| Step | What was done |
|---|---|
| Baseline | Live install on v0.94.69 (validated 2026-09-04), HA Core 2026.9.0 / HAOS 18.2 / Python 3.14.6, timezone Australia/Brisbane, `training_source: hybrid`, three subentries (2 loads, 1 power signal), Nimbus in shadow (HAEO automation drives the battery). |
| Code level | Worktree of tag `v0.94.113`; every closed sub-issue's CHANGELOG claim mapped to the file/line that implements it; `ruff check` + `ruff format --check` clean; `pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant`: **1174 passed, 13 skipped**. |
| Live 1 | HACS `update_information` → download `v0.94.113` → `ha_restart` at 06:55:35 AEST. Entity state, config-entry diagnostics, `system_log` and raw `error_log` captured 06:59–07:04. |
| Live tests | `homeassistant.reload_config_entry` with the price watcher **on** (#337); `nimbus_load.compute_quality_report` with a naive `"2026-09-03 00:00:00"` window (#345). |
| Live 2 | v0.94.114 was released during the run (closes #370); HACS download → `ha_restart` at 07:08:08 AEST; the same captures repeated at 07:11 and 07:15. |

All timestamps below are AEST (UTC+10). Evidence dumps (diagnostics, quality report, raw logs) are on the verifying machine; excerpts are quoted inline.

## Verification matrix

Legend: ✅ confirmed · ☑️ confirmed at code/test level only (not observable live) · ⚠️ partial · ❌ not resolved / regressed · ⏸️ open by design (issue still open)

| Issue | Sev | Fixed in | Verdict | Evidence |
|---|---|---|---|---|
| #337 price-watcher double unsub → FAILED_UNLOAD | C | v0.94.61 | ✅ | `reload_config_entry` at 07:02:48 with `switch.nimbus_solve_on_price_change = on`; entry `loaded` at 07:04:05, no `ValueError`/`list.remove` in the log, solver published again at 07:03:25. |
| #338 soft-SoC penalty not scaled by `hours[t]` | C | v0.94.61 | ☑️ | `solver/network.py:901-902` prices `underfill[t]` with the per-kWh penalty; regression test in suite. Not observable live (no soft-SoC event during the window). |
| #339 subentry flows reject blank Optional | C | v0.94.61 | ☑️ | Optional fields no longer carry `default=None`; `_absorb_step` (v0.94.69) keeps cleared values cleared. Not exercised live. |
| #340 Solver Sources `include_entities` lockout | C | v0.94.61 | ☑️ | Selector no longer restricted to the discovered list; 1174-test suite green. |
| #341 wizard final merge nulls unsubmitted keys | H | v0.94.62 → v0.94.69 | ✅ | v0.94.62's first fix regressed clear-stays-cleared (reported 2026-09-04); `flows/hub_options.py:1158 _absorb_step` fixes both. Live: every option value survived two core restarts and one reload unchanged. |
| #342 `number.py` backfills Store from older RestoreNumber | H | v0.94.63 | ✅ | Five hardware limits restored identically across restart 06:56, reload 07:02 and restart 07:09: 100.0 / 21.0 / 24.0 / 30.0 / 30.0 kWh·kW; no placeholder resets. |
| #343 literal entity_ids couple bridge + push registry | H | v0.94.73 | ✅ | Zero `Platform nimbus_load does not generate unique IDs` across both restarts and the reload (the only such error is `lg_thinq`); no `*_2`/`*_3` nimbus entities in the registry enumeration. |
| #344 failed first refresh leaks sibling retrain listeners | H | v0.94.64 | ☑️ | Listener registration moved after all coordinators succeed; no `ConfigEntryNotReady` occurred live, so not exercised. |
| #345 `compute_quality_report` TypeError on naive datetime | H | v0.94.65 | ✅ | Service call with `start="2026-09-03 00:00:00"` returned `window_start 2026-09-02T14:00Z`, EPR 26.2 % — naive input interpreted as local, no exception. |
| #346 PID lock without self-PID check | H | v0.94.66 | ✅ | After three non-graceful restarts the first solve ran within ~65 s each time; no "previous run still in progress" lines. |
| #347 timezone hardcoded / DST arithmetic | H | v0.94.67, v0.94.86 | ☑️ | `LOCAL_TZ = ZoneInfo(os.environ.get("NIMBUS_SOLVER_TIMEZONE", "Australia/Brisbane"))`; the remaining 3 mentions are the default and comments. Install tz is Brisbane, so not discriminating live. |
| #348 household constants hidden as "generic" | H | v0.94.88, v0.94.96 | ✅ | New `number.nimbus_solver_fixed_daily_charge` (1.95) and `number.nimbus_solver_post_window_self_consume_hours` (4) exist and restore across restarts. |
| #349 blocking I/O + heavy imports on the event loop | H | v0.94.89 | ✅ | No `Detected blocking call` attributed to `nimbus_load` in either boot (the one present is `ha_power_predictor`); `solver_writer.py:252-256` uses package-relative imports. |
| #350 LTS forward-fill duplicates training rows | H | v0.94.75 | ☑️ ⚠️ | Code present. **Cannot be validated live: the LTS/hybrid path crashes before it gets here (S1 / #372 below).** |
| #351 model selection by one-step MAE | H | v0.94.74, v0.94.90 | ☑️ ⚠️ | `ml/model.py` rolling-origin recursive MAE present. Same live caveat as #350. |
| #352 cold-start band inverted for negative values | M | v0.94.71 | ☑️ | `ml/model.py:1333` uses `max(abs(point) * fraction, min_kw)`; no trained power-signal model exists live right now to observe a band. |
| #353 NaN poisons the model | M | v0.94.70, v0.94.93 | ☑️ ⚠️ | `isfinite` guard and staleness cap present; not reachable live until #372 is fixed. |
| #354 stochastic.py drift | H | v0.94.81, v0.94.91 | ☑️ | Shadow-only module; tests green. |
| #355 p2p_export.py never imported | M | v0.94.82 | ☑️ | `solver/network.py:276 from . import p2p_export`; inline copy removed. |
| #356 lp.py status collapse, no time limit | M | v0.94.72, v0.94.94, v0.94.95 | ☑️ | `solver/lp.py:330 time_limit`, `:413 status="error"`; quality report window-splitting test present. |
| #359 manifest/hacs gaps | M | v0.94.76, v0.94.99 | ✅ | `dependencies: frontend, http, recorder`, `integration_type: hub`, `single_config_entry: true`, `hacs.json homeassistant: 2026.8.0`; loads on 2026.9.0. |
| #361 missing translations | H | v0.94.77 | ☑️ | `strings.json` carries 43 option data labels and all 3 services; `translations/en.json` byte-identical. |
| #362 recorder churn / attribute bloat | M | v0.94.70 → v0.94.112 | ✅ | Flattened children `_attr_should_poll = False` with a staleness timer (`sensor_flattened.py:752/883`); parent forecast attributes trimmed; `sensor.nimbus_health_report` now updates once a minute. Duplicate source-sensor subentries rejected (v0.94.112, not exercised). |
| #365 lifecycle robustness | M | v0.94.78, v0.94.101, v0.94.108 | ✅ ⚠️ | Retrain failures now logged as ERROR with traceback — **this is exactly what surfaced #372**. Services unregistered on unload (`services.py:367`); startup 404 handled as a clean warning (`solver_runtime.py:623`). Residual: the "Nimbus Solver is not configured yet" startup warning still fires 3–5× per boot (known benign race, documented). |
| #366 ML performance / robustness | M | v0.94.80, v0.94.105–107 | ⚠️ | GBRT vectorisation and event-loop reduction present. Finding 3 (pickle schema version) is present and **active**: all three persisted models were discarded at 06:56:27 (`schema_version=0 … expects 1`). With the replacement retrain failing there is no fallback, so every forecast entity has been `unknown` since. → **S2 / #373**. |
| #368 DST wall-clock arithmetic | M | v0.94.86 | ☑️ | Tests green; no DST transition in the window. |
| #370 zero-load startup plan | H | v0.94.114 | ⚠️ | Restart transient **fixed**: 07:09:40–07:10:36 nine cycles logged `Load forecast not ready yet …` and published nothing. But once the coordinator published an *empty* forecast list (07:11:05) the "present but empty" branch fell through to the zero fallback: 07:11 and 07:15 plans `status: optimal`, `cost_band.width 0.0`, `load_kw` 0 for 360 of 361 periods. → **S3 / #374**. |
| #357 add-on / writer drift | H | open | ⏸️ | `nimbus_solver_app/` deleted (v0.94.85); drift guard (v0.94.92) in place; issue tracks the remaining docs shim. |
| #358 CI hardening | M | open | ⏸️ | Actions SHA-pinned, `permissions:` blocks, `--cov-fail-under=75` present. Residual: `pyproject target-version = "py312"` while the runtime is 3.14; mypy still advisory. |
| #360 test infrastructure | M | open | ⏸️ | `tests/run_all.py` removed. Residual: 3 files still carry `__main__` collectors (`run_reference_benchmark.py`, `test_settlement_capture_timing.py`, `test_solver_writer_no_silent_failures.py`); `test_price_watcher.py` still sleep-timed. |
| #363 solver_writer logging | M | open | ⏸️ | All 9 remaining `except Exception:` sites are annotated degrade paths that log; the 3 remaining `print()` calls are the standalone CLI entry point (`__main__`) and one stdout summary — acceptable for the script mode, the issue's split plan remains. |
| #364 docs / repo hygiene | L | open | ⏸️ | Household IPs removed from shipped scripts except one example-URL comment (`solver_writer.py:299`); `CLAUDE.md`, `CHANGELOG.md` and the review doc still quote historical IPs. |

## New findings (filed as sub-issues of this report)

**S1 (#372) — [H] LTS/hybrid training path crashes on every retrain: `statistics_during_period` returns `start` as a float, `coordinator.py` treats it as a datetime.** Every retrain since at least 2026-09-02 on this install has failed with `AttributeError: 'float' object has no attribute 'tzinfo'` at `coordinator.py:1018 _fetch → dt_util.as_local(ts_utc)`. Reproduced at 06:57:10–13, 07:02:50 and 07:09:47–48 for all three subentries. HA Core has returned `start`/`end` as epoch floats (`start_ts`) since 2023.4; the code's own comment expects "a UTC datetime object (or ISO string in older HA cores)". Before v0.94.78 the failure was swallowed silently, which is why the stale 2026-09-01 model kept serving.

**S2 (#373) — [H] Schema-version mismatch discards a working model with no fallback and no early retry.** v0.94.107's `TRAINED_MODEL_SCHEMA_VERSION = 1` deletes any `schema_version=0` pickle at load and relies on the immediate retrain. When that retrain fails (S1, or any transient recorder error) the subentry has *nothing* until the next scheduled retrain hour (03:00 local here) — and on this install that one fails too. Net effect: three forecast sensors `unknown` for 20+ hours after an upgrade, `sensor.nimbus_household_load_total_forecast` reporting only the current meter reading, and the solver planning against zero load.

**S3 (#374) — [H] #370 residual: a present-but-empty load forecast still yields a confident zero-load "optimal" plan.** Confirmed on v0.94.114 at 07:11:20 and 07:15:07: `load_forecast_source_error = "… 'forecast' attribute is present but empty (0 points)"`, `load_kw = [6.98, 0, 0, …]`, `total_cost` negative, `cost_band {lower: -12.70, upper: -12.70, width: 0.0}`, `status: optimal`, pushed to `sensor.nimbus_solver_battery_forecast` and every flattened child. The classification in `_is_transient_startup_load_forecast_error()` deliberately treats this shape as a misconfiguration; on an install whose model has been discarded (S2) it is the steady state for hours. Side effect noticed: while the cycle raises (the fixed transient case), the quality report, counterfactual and efficiency-backtest sensors are withheld too, although they do not depend on the load forecast.

## Observations (not filed)

- **Retry cadence while not ready.** Nine solve attempts in 56 s after the 07:08 restart (startup retry loop at 15 s, price watcher and periodic tick overlapping). Harmless, but each attempt re-reads every source sensor and logs two warnings.
- **Solcast** `sensor.solcast_pv_forecast_forecast_today` was `unavailable ('forecast')` for every solve in the 06:57–07:15 window on both boots, so all plans in this report also carried no solar. A third-party startup ordering issue, not Nimbus's; worth knowing when reading the plans.
- **Yesterday's scorecard (2026-09-04)** came out at **EPR −12.0 %** (`j_ref 4.97`, `j_ach 5.68`, `j_star −0.87`, regret $6.54; top regret hours 06:00 = $2.79, 23:00 = $1.30, 05:00 = $0.92). The achieved trajectory charged 08:00–13:00 at ~5 kW and again 05:00/23:00 while the oracle held. This scores the HAEO-driven dispatch, not Nimbus, and the usual caveats apply (degradation-free oracle, achieved SoC integrated from battery power). Flagged because a negative EPR is a first for this install.
- **`sensor.nimbus_solver_config`** reported `configured` within ~30 s of both boots with no unresolved keys; the documented transient `unconfigured` reading was not observed this time.
- Pre-existing, unrelated: `automation.nimbus_battery_soc_control_ecoflow` still errors (`Value 20.0 … outside valid range 22 - 100`) on every solver publish.

## Suggested immediate workaround for affected installs

Until #372 ships, an install with `training_source: hybrid` or `lts` will not train. Switching the hub option to `recorder` and calling `nimbus_load.retrain` restores forecasts within a few minutes (recorder retention permitting). Not applied here — left for the operator.
