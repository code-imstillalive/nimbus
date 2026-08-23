# Changelog

All notable changes to Nimbus are recorded here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project adopts [Semantic Versioning](https://semver.org/spec/v2.0.0.html), and both the Nimbus integration (`custom_components/nimbus_load`) and the Nimbus Solver add-on (`nimbus_solver_app`) share a single version line — the `version-lockstep` CI job enforces that they never drift.

Entries call out real, user-visible changes. They are not a `git log` dump; the commit history is the source of truth for the underlying diffs.

## [Unreleased]

_Add new entries here as each PR lands. They roll into the next tagged release._

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
