# Nimbus full codebase review — 2026-09-03

Reviewed at `main` @ `7f2ffc0` (v0.94.60). Scope: everything under `custom_components/nimbus_load/`, `nimbus_solver_app/`, `tests/`, `.github/`, `docs/`, `pyproject.toml`, `CLAUDE.md`.

## How this review was done

Seven parallel, area-scoped reviews (integration lifecycle, entity platforms, config flows, ML pipeline, solver library, `solver_writer.py`, repo hygiene/CI/tests), each reading the code rather than the journal. The highest-severity findings were then independently re-verified from this session, either by reading the relevant Home Assistant core source or by running a reproduction:

- **Soft-SoC / terminal-value unit mismatch** — reproduced with `build_plan()` on a 12 h horizon: on a 1 h or 15 min grid the plan holds at 90 kWh and exports nothing; on the production 5 min grid the identical inputs export 19.8 kWh down to the 70 kWh breakpoint, and a flat-price accounting case reports −$10.00 where −$9.00 is the true value.
- **Price-watcher double-unsub** — confirmed against HA core: `_remove_listener` does `callbacks[key].remove(job)` on a `defaultdict(list)` (raises `ValueError` on a second call) and `ConfigEntry.async_unload` catches that into `FAILED_UNLOAD`.
- **Subentry `vol.Optional(default=None)`** — confirmed against a real `EntitySelector`: `default=None` is rejected (`Entity None is neither a valid entity ID nor a valid UUID`), while the same key with no default validates cleanly.
- Cold-start band inversion, missing NaN guard, PID-lock self-check, hardcoded timezone, discharge-cost override, handler-registry key mismatch, `_unrecorded_attributes = frozenset()` on the quality-report parent, Store backfill ordering in `number.py` — each confirmed by reading the code at the cited lines.

Test baseline: the stub-based suite passes on Python 3.13 (903 passed, 13 skipped). The `hass_integration/` suite was not run here (needs the pinned HA harness on Python 3.14). Note that a fresh clone with only the base dependencies cannot run `pytest` at all — `pyproject.toml`'s `filterwarnings` entry for `aiohttp.web_exceptions.NotAppKeyWarning` makes pytest abort at config time if `aiohttp` is not installed.

## Headline

The project is in much better shape than its 14-day age and 40+ releases would suggest: the ML pipeline and LP solver are genuinely tested, the diagnostic surface is unusually rich, and most of the incident history in `CLAUDE.md` is already fixed in code. The review found **4 critical, 12 high, ~20 medium, and a long tail of low** items. The critical ones are all cheap to fix and all reachable in normal use:

1. Reloading the hub with *Solve on Price Change* enabled can leave the entry in `FAILED_UNLOAD` until a restart.
2. The #328 soft-SoC penalty is scaled by period length but the terminal-value credit is not, so on the production 5-minute grid the LP claims phantom credit and sells energy it should hold.
3. Three subentry flows (Power Source, PV String, Battery Tower) cannot be submitted with an optional field blank, and cannot clear a set optional field.
4. The Solver Sources step enforces `include_entities`, so a saved value outside the live candidate list makes the whole 3-step Solver wizard unsubmittable.

The strongest *structural* theme is **copies that were meant to be single sources**: `nimbus_solver_app/solver/` has silently fallen behind the integration copy (missing #328, #238, #297/#310), three writer scripts diverge by thousands of lines, `p2p_export.py` duplicates rather than replaces the inline `network.py` implementation, and `stochastic.py` re-implements a subset of `network.py`'s constraints and has already drifted (per-day P2P cap doubled, no soft SoC, no combined-direction caps).

## Findings by area

Severity: C = critical, H = high, M = medium, L = low. Each row is filed as a sub-issue of the parent report issue.

### Integration lifecycle (`__init__.py`, `solver_runtime.py`, `services.py`, `coordinator.py`)

| Sev | Finding | Where |
|---|---|---|
| C | Price-watcher `_combined_unsub` is registered with `entry.async_on_unload` **and** called explicitly in `async_unload_entry`; the state-change unsub is not idempotent → `ValueError` → `FAILED_UNLOAD`, hub does not come back on reload. Each switch toggle appends another stale unsub. | `__init__.py:830`, `:859-861`, `:737-738` |
| H | `asyncio.gather` over `_setup_one()` with no cleanup: one `ConfigEntryNotReady` leaks every sibling coordinator's daily retrain listener; each HA retry leaks another set, all writing the same `.pkl`. | `__init__.py:439-448`, `coordinator.py:484-490` |
| H | `compute_quality_report` `_coerce_datetime` returns naive datetimes unchanged; `end > now` then raises `TypeError` (what the `datetime:` selector in Developer Tools produces). | `services.py:79-97`, `:232` |
| M | Services registered per entry, never unregistered; `_retrain_all` gathers without `return_exceptions`; `_async_retrain` has no `except` (silent task failure, invisible to `health.py`); executor solve outlives unload; module globals not reset. | `services.py:271-303`, `coordinator.py:539-656`, `solver_runtime.py:571-585` |
| M | Startup "HTTP Error 404: sensor.nimbus_solver_config" is a synthetic `HTTPError` raised by `ha_get`'s native branch, not a `_NATIVE_HASS` routing gap (contrary to `CLAUDE.md`); it lands in the generic `except Exception` with a full traceback per attempt. | `solver_writer.py:1363-1394`, `solver_runtime.py:549-551` |
| L | Three switches poll every 30 s with no `async_update`. | `switch.py:103-152` |

### Entity platforms (`sensor.py`, `sensor_flattened.py`, `number.py`, `switch.py`)

| Sev | Finding | Where |
|---|---|---|
| H | `number.py` backfills the durable Store from the RestoreNumber value *before* consulting the Store; RestoreState dumps every 15 min, the Store on every set → an unclean stop within 15 min of an edit overwrites the newer Store value with the older one. Explains the "reset on some restarts" history combined with `_desc.default` = schema minimum (0.1). `switch.py` has no Store at all. | `number.py:722-761`, `switch.py:139-152` |
| H | `NimbusSolverConfigSensor._resolve()` reads literal `number.nimbus_<key>` / `switch.nimbus_<key>`; `number.py`/`switch.py` pin those literal entity_ids with no entry scoping. A mirror/orphan claiming the base name makes the LP read a foreign entity's value silently; a second hub entry is structurally broken. Related: `unregister_entity_handler(self.entity_id)` vs registration by literal key → a `_2` entity never unregisters; `entity.entity_id` captured right after `async_add_entities` and never refreshed on rename. | `sensor.py:1109-1121`, `:755-833`, `:1880`, `number.py:706`, `switch.py:129` |
| M | 77 flattened children have no staleness watchdog and rely on default polling to re-evaluate `available`; setting `_attr_should_poll = False` (as the sibling does) would freeze them. | `sensor_flattened.py:712-786` vs `sensor.py:1761-1767` |
| M | Recorder churn/bloat: health report emits a fresh `generated_at` on every 30 s poll (new `state_attributes` row each time); quality-report parent sets `_unrecorded_attributes = frozenset()` while carrying ~14.6 KB of hourly dicts (89 % of the 16 KB recorder cap). | `sensor.py:1394`, `:1328`, `:2101` |
| L | `_attr_should_poll = False  # DIAG: temporary` shipped on the push-sensor base; WARNING logs from inside a property getter; `register_price_latency_sensor` never unregistered; forecast entity_id derived from source sensor so two subentries on one meter collide; ~130 lines of duplicated push/staleness and `DeviceInfo` code. | `sensor.py:1552`, `:1163-1183`, `:651`, `:967-969` |

### Config flows (`config_flow.py`, `flows/*`, `strings.json`, `manifest.json`)

| Sev | Finding | Where |
|---|---|---|
| C | `vol.Optional(key, default=defaults.get(key))` → `default=None` handed to `EntitySelector`/`SelectSelector`, which reject it: Power Source, PV String, Battery Tower cannot be created with an optional blank, and cannot clear a set value (the #113/#114 fix in `hub_options.py` was never propagated). Deleting a Power Source permanently bricks reconfigure of its PV String/Battery Tower children (`value must be one of []`). | `flows/power_source_subentry.py:46,52`, `pv_string_subentry.py:50-64`, `battery_tower_subentry.py:61-88` |
| C | `_entity()` docstring says `include_entities` is "deliberately NOT enforced"; HA's `EntitySelector` enforces it via `vol.In`. A saved sensor outside the discovered list (own template forecast, mid-restart) makes step 3 — and so the whole Solver wizard — unsubmittable. | `flows/hub_options.py:278-289`, `:456`, `:470` |
| H | Solver wizard final merge `merged[key] = self._solver_data.get(key)` nulls every key not submitted across the 3 steps (23 keys, 5 of them Required). 33 `vol.Optional` fields across the options flow wipe on omission (forecaster 7/12, switchboard 8/8, solver 18). `async_step_init` full-overwrite risk in `CLAUDE.md` is no longer true (it is a menu now). | `flows/hub_options.py:1123-1124` |
| H | 8 options fields have no translation label (`training_source`, `hybrid_recent_days`, `solver_solar_forecast_sensor_2/_3`, `solver_price_forecast_array_sensor`, `solver_regional_spot_forecast_sensor`, `solver_regional_spot_current_price_sensor`, `solver_p2p_matched_rate_forecast_sensor`); `compute_quality_report` absent from `services` section. `quality_scale.yaml` claims `action-setup`/`docs-actions`/`action-exceptions: exempt` ("no service actions") and `data-description: done` — false. | `strings.json`, `translations/en.json`, `quality_scale.yaml` |
| M | `manifest.json` omits `frontend` from `dependencies` while `frontend.py` imports `homeassistant.components.frontend`; `single_config_entry` not declared despite `async_set_unique_id(DOMAIN)`; `hacs.json` says `homeassistant: 2025.1.0` while code guards for 2026.9 APIs and tests pin 2026.8.3; `highspy` in `requirements` has no armv7 wheel. | `manifest.json`, `hacs.json` |
| L | Flow tests call `async_step_user()` directly and never run schema validation (which is where all three flow bugs live); `OptionsFlowWithConfigEntry` is deprecated; five subentry flows duplicate ~150 lines. | `tests/test_flows_*_subentry.py`, `flows/hub_options.py:62,930` |

### ML pipeline (`ml/`, `coordinator.py`)

| Sev | Finding | Where |
|---|---|---|
| H | LTS/hybrid training forward-fills hourly means onto the 15-min grid, so 75 % of rows have `lag_short == y` exactly: validation MAE halves (0.36 vs 0.72 on the same data at hourly cadence), `training_points` inflates 4×, model learns persistence. | `ml/model.py:411-412`, `:514-517`, `coordinator.py:834-950` |
| H | Model selection scores k-NN/GBRT one-step-ahead with true lags but deploys a 96 h recursive forecast; naive is scored at a 7-day horizon; GBRT reports the early-stopping minimum on the same validation set. `validation_mae` does not measure published-forecast accuracy and naive can effectively never win. | `ml/model.py:585-650`, `ml/gbrt.py:229-237` |
| M | Cold-start band `point_value * COLD_START_BAND_FRACTION` is negative for negative points → `lower > upper` for Battery/Grid for the first ~10 cycles. | `ml/model.py:802-803`, `coordinator.py:1435-1439` |
| M | A single `"nan"` recorder state passes the `MAX_SANE_POWER_KW` guard, NaNs `x_mean` and all three MAEs, `min()` silently picks k-NN, `predict()` returns 0.0 everywhere, and the poisoned pickle is saved. No `isfinite` guard anywhere. `resample_last_value` has no staleness limit (outage gaps become flat training data). | `coordinator.py:774-829`, `:991-993`, `ml/model.py:321-335` |
| M | GBRT split search is a pure-Python loop: 7.2 s per fit at 2 880 rows, 22.8 s at 8 760, ×4 fits per subentry. Trivially vectorisable. `calibrated_band()` (an `np.percentile`) runs ~385× per tick on the event loop. | `ml/gbrt.py:95-108`, `coordinator.py:1426-1447` |
| L | Wall-clock `timedelta` arithmetic on tz-aware local datetimes breaks the grid and bisect on DST days (verified with Australia/Sydney); pickle-compat `getattr` pattern inconsistent, no schema version, shape check outside the `try`. | `ml/model.py:338-347`, `:1000-1066`, `coordinator.py:1211` |

### Solver library (`solver/`)

| Sev | Finding | Where |
|---|---|---|
| C | `underfill`/`overfill` cost is `penalty * hours[t]` but the terminal-value credit is a bare $/kWh; "penalty always dominates" holds on a 1 h grid and fails on the 5-min grid. Reproduced: phantom credit (−$10 vs −$9) and changed dispatch (exports 19.8 kWh instead of holding). Tests only use `hours = 1.0`. | `solver/network.py:830-836`, `:1083-1097`, `:1262-1265` |
| H | `stochastic.py` has drifted from `network.py`: per-calendar-day P2P cap applied once per stage (doubles the allowance on the branch day); no #328 soft-SoC (below-floor start → silent `infeasible`, `nan` cost); combined-direction caps (#245, #266) absent despite claiming parity. | `solver/stochastic.py:271-276`, `:358-382`, `:399-403` |
| M | `p2p_export.py` is a copy, not an extraction — `network.py` never imports it and carries its own inline implementation of all six mechanisms. | `solver/p2p_export.py`, `solver/network.py:775-1497` |
| M | `lp.py` maps every non-optimal HiGHS status (time/iteration limit, model error, solve error, unbounded-or-infeasible) to `"infeasible"`; no `time_limit` is ever set (MIP path can block the writer indefinitely). `_infeasible_plan` aliases one `zeros` array across nine `Plan` fields. `terminal_value_period_indices` not checked `< n` (raw `IndexError`); `max_charge_kw`/`max_discharge_kw` never checked `> 0`; efficiency error text contradicts its guard. | `solver/lp.py:355-367`, `solver/network.py:601-628`, `solver/elements.py:623-712` |
| M | `solver/README.md` says the package is "not wired into anything", `lp.py` is "pure numpy simplex, no highspy", and lists three test files that do not exist. | `solver/README.md:1-16`, `:107-127` |
| L | `quality_report.py` folds windows > 24 h onto 24 hour keys and emits 0.0 for empty hours; `_align_previous_periods` is O(n·m). | `solver/quality_report.py:130`, `solver/network.py:406-436` |

### `solver_writer.py` (7 077 lines)

| Sev | Finding | Where |
|---|---|---|
| H | `acquire_lock()` has no `old_pid == os.getpid()` check; in native mode the lock file holds HA's own PID, which is stable across container restarts → after an unclean stop every tick skips forever. | `solver_writer.py:3463-3473`, `solver_runtime.py:524` |
| H | `BRISBANE_TZ = ZoneInfo("Australia/Brisbane")` used in 17 places; `hass.config.time_zone` never consulted. Every hour-of-day decision (P2P window, TOU fees, midnight anchors, quality-report day boundaries) is wrong for any non-Brisbane install, by one hour during AEDT. | `solver_writer.py:189` |
| H | Household/LocalVolts-specific behaviour behind "generic" config: when `solver_price_forecast_array_sensor` is set, the user's `solver_discharge_cost`/`solver_salvage_value` are silently replaced by a hardcoded 0.01/0.09/0.30/0.15 schedule; array parsed with LocalVolts `costsflexup`/`earningsflexup` keys; P2P matched rate forced to 0 outside 17–24; `FIXED_DAILY_CHARGES = 1.95` added to everyone's `total_cost_with_fixed_costs`; export hard-pinned to 0 for 4 h after midnight. Contradicts the retailer-agnostic rule in `CLAUDE.md`. | `solver_writer.py:355`, `:1059-1078`, `:2425`, `:2500-2566`, `:6005-6009`, `:6286-6308`, `:6568-6570` |
| H | `sensor.py` does `from . import solver_writer` on the event loop; on first import that runs `open(TOKEN_PATH)`, `sys.path.insert`, `import numpy`, and `import highspy` on the loop (the same class of blocking call `solver_runtime.py` already moved off the loop once). | `sensor.py:743`, `solver_writer.py:200-218`, `:1168` |
| M | `sys.path.insert(0, <package dir>)` leaks `sensor`, `const`, `ml`, `solver`, … as top-level names into the whole HA process; `ml.blend` and `custom_components.nimbus_load.ml.blend` become two distinct modules. | `solver_writer.py:200-206` |
| M | 24 `print()` sites vs 15 `_LOGGER` calls: operationally important warnings go to stderr (invisible in HA logs) while an unconditional `#85 trace` print fires on every `ha_post_state`; 8 bare `except Exception: pass/return` with no log; `parse_iso` returns naive datetimes for offset-less strings and the per-source catch misses `TypeError`, so one bad third-party forecast crashes every cycle. | `solver_writer.py:683`, `:1500`, `:1760-1783`, `:2004-2016`, `:5384-5389`, `:3333`, `:5556`, `:6392-6411`, `:6638` |
| L | `fetch_price_history`/`fetch_entity_history_range` ~90 % identical incl. the recorder bridge; `n_clamped_periods` always 0; `cost_breakdown` sums rounded values; `main()` is 1 788 lines. | `solver_writer.py:2569-2659`, `:3486-3556`, `:5289-7077` |

### Repository hygiene, CI, tests, docs

| Sev | Finding | Where |
|---|---|---|
| H | `nimbus_solver_app/solver/` drifted from the integration copy (network.py 282 diff lines, missing #328/#238/#297/#310, 4 modules absent); `Dockerfile` says "keep in sync manually", nothing enforces it; the add-on is already marked DEPRECATED but still ships under the current version. Three writer scripts diverge by 3 683–3 745 lines; the only test loads the **docs** copy. | `nimbus_solver_app/`, `docs/real-world-integration/files/`, `tests/test_settlement_capture_timing.py:64` |
| M | Actions unpinned (`hassfest@master`, `hacs/action@main`, mutable tags); no `permissions:` on 3 of 4 workflows; coverage collected and discarded (no `--cov-fail-under`); mypy permanently `\|\| true`; ruff excludes `nimbus_solver_app/` and `docs/` (18 files unformatted); `ruff target-version = py312` and pre-commit `python3.12` vs CI/pyproject 3.14. | `.github/workflows/*.yml`, `pyproject.toml` |
| M | Two test runners with different semantics (`run_all.py` ignores `hass_integration/` and `regression/`; 0.94.59→0.94.60 deleted a test that only failed under pytest); 106 duplicated `__main__` collectors plus a BLE001 per-file-ignore to support them; real `sleep`-based timing tests; 4 assertion-free tests; duplicated flow/decomposition tests; `CLAUDE.md` "Testing" section is stale; fresh clone cannot run pytest without `aiohttp`. | `tests/run_all.py`, `pyproject.toml:filterwarnings`, `CLAUDE.md` |
| L | `solver/README.md` and `README.md` inaccurate (README omits ~8 entities and the `compute_quality_report` service); `CLAUDE.md` is an 85 KB journal with four "read this first" sections; 9 scripts hardcode `192.168.1.221` and `solver_writer.py` defaults `TOKEN_PATH` to `/home/homehub/.ha_token`; 592 KB `icon.psd` shipped inside `custom_components/`; `.gitignore` lacks `*.pkl`, `coverage.xml`, tool caches; exec bits inconsistent on shebang'd scripts. | various |

## What is in good shape

- No HAEO references in code (the ZERO-HAEO directive holds), no tokens/secrets committed, tokens never logged, no `eval`/SSL-disable, diagnostics redact nothing because nothing sensitive is in options.
- k-NN, chronological split, damping, seasonal-lookup bucketing, GBRT tree building, residual buffer cap, retrain idempotency, executor placement of training/prediction: all correct on inspection.
- `strings.json` and `translations/en.json` byte-identical; version lockstep holds; all 136 `CONF_*`/`DEFAULT_*` constants are used; schema-key lists are in sync; all flattened `device_class`/unit pairs valid; no duplicate `entity_id_suffix`.
- `elements.py` rejects zero-length horizons, degenerate cost floors, and inverted SoC bounds; no mutable default arguments in the solver.

## Suggested order of work

1. **Same-day fixes (each < 30 lines):** price-watcher double-unsub; cold-start `abs()`; `_coerce_datetime` tz; subentry `Optional` defaults; `include_entities` → suggestion-only; `terminal_value_period_indices < n`; `_infeasible_plan` per-field arrays; `generated_at` → unrecorded; quality-report parent `_unrecorded_attributes`.
2. **Before the next release:** soft-SoC penalty units (with an `hours`-parameterised regression test); `number.py` Store ordering; `sys.path`/blocking import on the loop; PID-lock self-check; NaN guard.
3. **Next sprint:** timezone from `hass.config.time_zone`; retailer-specific overrides behind explicit config; `stochastic.py` parity (or delete it until it shares `network.py`'s constraint builders); `p2p_export.py` actually used by `network.py`; coordinator setup cleanup on failure; entity_id scoping / handler registry keyed by entry.
4. **Structural:** single-source the solver and writer (delete or CI-diff `nimbus_solver_app/`); split `solver_writer.py`; one test runner; ML validation on the recursive path; move the `CLAUDE.md` journal to `docs/worklog/`.

## Sub-issue index

Parent report: [#336](https://github.com/code-imstillalive/nimbus/issues/336).

| # | Sev | Title |
|---|---|---|
| [#337](https://github.com/code-imstillalive/nimbus/issues/337) | C | Price-watcher unsub called twice on unload → `FAILED_UNLOAD` |
| [#338](https://github.com/code-imstillalive/nimbus/issues/338) | C | Soft-SoC penalty scaled by `hours[t]`, terminal credit not — phantom credit on the 5-min grid |
| [#339](https://github.com/code-imstillalive/nimbus/issues/339) | C | Power Source / PV String / Battery Tower flows: blank optional rejected, cannot clear, reconfigure bricked |
| [#340](https://github.com/code-imstillalive/nimbus/issues/340) | C | Solver Sources step enforces `include_entities` → wizard unsubmittable |
| [#341](https://github.com/code-imstillalive/nimbus/issues/341) | H | Solver wizard merge nulls unsubmitted keys; 33 Optional fields wipe |
| [#342](https://github.com/code-imstillalive/nimbus/issues/342) | H | `number.py` Store backfilled from older RestoreNumber value; `switch.py` has no Store |
| [#343](https://github.com/code-imstillalive/nimbus/issues/343) | H | Literal, non-entry-scoped entity_ids in config bridge and handler registry |
| [#344](https://github.com/code-imstillalive/nimbus/issues/344) | H | Failed first refresh leaks coordinators and retrain listeners |
| [#345](https://github.com/code-imstillalive/nimbus/issues/345) | H | `compute_quality_report` `TypeError` on naive datetimes |
| [#346](https://github.com/code-imstillalive/nimbus/issues/346) | H | PID-file lock has no self-PID check |
| [#347](https://github.com/code-imstillalive/nimbus/issues/347) | H | Hardcoded Australia/Brisbane timezone; DST wall-clock arithmetic |
| [#348](https://github.com/code-imstillalive/nimbus/issues/348) | H | Household/LocalVolts-specific behaviour behind generic config |
| [#349](https://github.com/code-imstillalive/nimbus/issues/349) | H | Blocking imports/file I/O on the event loop; `sys.path` shim leak |
| [#350](https://github.com/code-imstillalive/nimbus/issues/350) | H | LTS/hybrid training on 15-min grid manufactures `lag_short ≡ y` |
| [#351](https://github.com/code-imstillalive/nimbus/issues/351) | H | Model selection metric is one-step-ahead, product is 96 h recursive |
| [#352](https://github.com/code-imstillalive/nimbus/issues/352) | M | Cold-start confidence band inverted for negative values |
| [#353](https://github.com/code-imstillalive/nimbus/issues/353) | M | `"nan"` state poisons the model; unbounded forward-fill |
| [#354](https://github.com/code-imstillalive/nimbus/issues/354) | H | `stochastic.py` drift: P2P cap doubled, no soft SoC, no direction caps |
| [#355](https://github.com/code-imstillalive/nimbus/issues/355) | M | `p2p_export.py` is a copy, not an extraction |
| [#356](https://github.com/code-imstillalive/nimbus/issues/356) | M | `lp.py` status collapse, no time limit, `_infeasible_plan` aliasing, validation gaps |
| [#357](https://github.com/code-imstillalive/nimbus/issues/357) | H | Add-on solver and writer copies drifted; no sync enforcement |
| [#358](https://github.com/code-imstillalive/nimbus/issues/358) | M | CI hardening: unpinned actions, permissions, coverage, mypy, ruff scope, Python target |
| [#359](https://github.com/code-imstillalive/nimbus/issues/359) | M | manifest/hacs: `frontend` dependency, `single_config_entry`, min HA, highspy armv7 |
| [#360](https://github.com/code-imstillalive/nimbus/issues/360) | M | Test infrastructure: two runners, `__main__` collectors, timing tests, blind tests, flow tests skip validation |
| [#361](https://github.com/code-imstillalive/nimbus/issues/361) | H | 8 options fields + `compute_quality_report` untranslated; `quality_scale.yaml` false claims |
| [#362](https://github.com/code-imstillalive/nimbus/issues/362) | M | Recorder churn/bloat; flattened children rely on accidental polling |
| [#363](https://github.com/code-imstillalive/nimbus/issues/363) | M | `solver_writer.py` logging, silent excepts, naive timestamps, duplication, split plan |
| [#364](https://github.com/code-imstillalive/nimbus/issues/364) | L | Docs and repo hygiene |
| [#365](https://github.com/code-imstillalive/nimbus/issues/365) | M | Lifecycle robustness: services, retrain failures, executor solve, globals, startup 404 |
| [#366](https://github.com/code-imstillalive/nimbus/issues/366) | M | ML performance and pickle schema versioning |
