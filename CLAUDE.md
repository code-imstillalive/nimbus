# CLAUDE.md — Nimbus

Instructions for any Claude instance working on this repo. Read this before touching any file.

---

## ⚠️ STANDING DIRECTIVE — UPDATE THIS FILE AT LEAST ONCE DAILY

> **If real work happens in this repo on a given day, this file's own CURRENT STATE section gets updated before that day is done — not batched up across several days.** Same standing rule applies to `116KAT-HA-AI`'s own `CLAUDE.md` for any day real work happens there.
>
> Confirmed directly, 2026-08-31: this section was still dated "2026-08-27 night" while real work (issues #244-#252 and beyond, releases up to at least v0.94.19) had already happened across several more days — a future session reading this file cold would trust a stale version number and a stale open-issue list. Undocumented/un-updated work is invisible to the next session; don't let this drift.

---

## ⚠️ CURRENT STATE (2026-09-05) — read this first

**Version: v0.94.103** (up from v0.94.81 at the start of 2026-09-04). A long, continuous run through Mark Purcell's codebase-review parent issue (#336) across 2026-09-04 into 2026-09-05: #355, #357 (partial, see below), #368, #349, #351, #354, #353, #356 (all 4 items), #348 (all 4 findings), and #359 (all 5 findings, either fixed or a documented deliberate deferral) are now CLOSED. Several more are substantial partial fixes, each left open for a specific, named remaining piece rather than force-closed — read each issue's own GitHub comments for the itemized breakdown rather than re-deriving it here: #360 (test infrastructure: findings 1/2/3/4/6 done, finding 5 investigated with one half found to be a non-issue, finding 7 — ml/ coverage gaps — not started), #358 (CI hardening: action SHA-pinning and coverage gating done, mypy baseline and ruff-scope-extension-to-docs/ deliberately deferred as their own separate efforts), #364 (docs/hygiene: both README files' stale/missing-entity content fixed, CLAUDE.md's own 85KB-journal split and hardcoded research-script IPs still open), #365 (lifecycle: service unregistration + module-global resets + a startup-race log fix all done, the harder "in-flight executor solve outlives unload" half of item 4 still open), #363 (solver_writer.py: every print()/silent-except now logs — findings 1/2 done on top of finding 3 from an earlier session, findings 4/5 — duplicated history fetchers and the 1,788-line main() split — still open as their own larger efforts). #357 itself stays open, not because more code work is needed, but because a precise, itemized 25+1 real writer-script content-difference list was handed to Mark for his own judgment call on whether it needs a stricter enforcement mechanism — don't re-litigate that list yourself if you pick this up, read the issue's own comments for where it was left. Full issue-by-issue detail (exact fixes, mutation-testing evidence, real numbers) lives in each release's own `CHANGELOG.md` entry (v0.94.89 through v0.94.103) — this section intentionally doesn't re-summarize all of it.

**One real CI-only break, caught and fixed same-day**: v0.94.101 hit a genuine, deterministic (confirmed via a rerun, not a flake) failure on CI's Python 3.14.7 specifically, never reproduced on local 3.12.10 — `pytest.PytestUnraisableExceptionWarning` from a garbage-collected, never-explicitly-closed temp file (almost certainly `highspy`/HiGHS's own C-extension internals, not this repo's own code) surfacing during an unrelated, later test. Fixed in v0.94.102 with a message-based `filterwarnings` ignore entry in `pyproject.toml`, same shape as the existing aiohttp `NotAppKeyWarning` entry. Worth remembering: a real CI-only failure on a Python-version-specific GC-timing artifact is a known class of flake with compiled C-extension dependencies — check whether it reproduces on a rerun (this one did, twice, ruling out pure randomness) before assuming it's either "just flaky, ignore it" or "a real regression in my diff."

**One process lesson worth keeping**: a commit message that said "fix #356 items 1 and 3" (intended as a PARTIAL-fix note) still triggered GitHub's auto-close keyword detection on the whole issue, since it parses "fix #NNN" as a substring regardless of surrounding qualifying text ("items 1 and 3"). Caught immediately via the repo's own issue-activity Monitor, reopened with a clarifying comment within the same turn. When shipping a deliberately partial fix, avoid the words fix/fixes/fixed/close/closes/closed/resolve/resolves/resolved anywhere near a real `#NNN` in the commit message at all — not just outside a trailer — until every item is actually done.

**A real, live production incident tonight, found and fixed the same session it happened.** NUC1 was manually updated from a stale v0.94.49 straight to the day's latest release (a ~34-version jump, not the household's usual incremental pace) as part of an unrelated OS-package/kernel restart. Immediately after, the Nimbus hub's own "Configure" button 500'd outright — `NimbusHubOptionsFlow.__init__()` (added by the #341 fix, v0.94.4x era) read `self.config_entry.options` directly inside `__init__`, but `config_entry` is a property HA core's own `OptionsFlow` deliberately raises `ValueError("The config entry is not available during initialisation")` on until the flow manager finishes attaching the entry — which only happens *after* `__init__` returns. This crashed on HA core 2026.9.0 specifically (an unrelated core upgrade that landed on NUC1 at the same time). Root-caused from a real `docker logs` traceback the household pasted directly (not guessed), fixed by deferring the seed to a new `_ensure_solver_data_seeded()` called lazily from the wizard's real entry point, shipped as v0.94.84, and confirmed live on NUC1 (options flow opens cleanly) within the same session. **Lesson recorded 2026-09-01 in the "Testing" section below turned out to be exactly right and still isn't fully closed** — this project's own local HA stubs don't reproduce the real `OptionsFlow.config_entry` property guard, so this class of bug is structurally invisible to `tests/test_flows_hub_options.py`'s existing mocks; the 2 tests touched by this fix were rewritten to assert the new lazy-seeding contract, but the stub gap itself (a `MagicMock` never raises) is real and could hide a similar regression again. Worth a dedicated look — see #360's own test-infrastructure findings below, which are a related but distinct set of gaps.

**Also confirmed live the same night, unrelated to the crash**: two Power Signal subentries this household had never configured (Temperature, Humidity) were missing entirely — not a bug, just genuinely never set up before tonight's `SIGNAL_ROLE_TEMPERATURE`/`SIGNAL_ROLE_HUMIDITY` roles existed to make them worth adding (see the 2026-09-03 section below for why those roles were built). Humidity's own Power Signal was found already correctly configured and working on the household's real install; Temperature's was created live during this session (`sensor.archerfield_temp` -> new Power Signal, Role: Temperature). Separately, right after the NUC1 restart, the Solver's very first solve cycle ran before every per-load ML coordinator had finished its own post-restart retrain, so it treated every load as 0.0 kW for that one cycle (`sensor.nimbus_cb_*` forecasts had no usable `forecast` attribute yet) — a real, but self-resolving, one-time startup race, confirmed cleared within ~10 minutes once each load's own retrain finished. Not a new bug, and not specific to today's release.

**#355 (v0.94.82/83) — `p2p_export.py`'s six P2P mechanisms were a copy, not an extraction.** `network.py` never actually imported the shared module (only `stochastic.py` did) despite `p2p_export.py`'s own docstring claiming "extracted verbatim... network.py was never touched" as a single-source-of-truth guarantee. `build_plan()` now calls the shared helpers directly; all six inline copies deleted. v0.94.83 was a same-day follow-up fixing a `ruff format` CI failure v0.94.82 shipped with — verify the exact CI command locally before tagging, this has bitten this project more than once now.

**#357 (v0.94.85) — the `nimbus_solver_app` HAOS Supervisor add-on removed from this repo entirely**, per the household's own explicit "delete it now" decision rather than adding a CI sync-check to keep maintaining a third copy of the solver logic. It had drifted out of sync with the integration's own solver code (missing #328/#238/#297/#310) with zero CI check ever catching it. `repository.yaml` and the `version-lockstep` CI job went with it — `manifest.json`'s `version` is now the only version number in this repo. The standalone bare-script + cron path (`docs/real-world-integration/`) is untouched and still fully supported; that's a genuinely different, still-needed deployment path, not the same thing as the add-on.

**#368 (v0.94.86) — DST wall-clock arithmetic, fixed and tested despite this reference household never observing DST.** `ml/model.py`'s `_build_grid()`, `PeriodGrid.period_starts`, and `quality_report.py`'s hourly ISO keys all did `local_dt + timedelta(...)` directly — pure wall-clock arithmetic that silently breaks across a real DST transition (skips or duplicates real hours). Separately, `resample_last_value()`'s bisect and `predict()`'s own lag-lookback compared aware datetimes sharing the same `tzinfo` object, which Python does NOT correct for UTC offset on (fold-blind wall-clock comparison) — a real, second, independent bug class from the arithmetic one. Fixed by doing every step/accumulation/comparison in UTC, converting back to local only for returned values. Tested against `Australia/Sydney` (a real DST zone) since Brisbane can't exercise this at all — 7 new tests, each confirmed via mutation testing to genuinely fail against the pre-fix code. Matters because this is a **public, portable HACS integration** — a European or US/NZ/AEDT installer would hit this on every DST transition, not just a theoretical concern.

**#360 (test infrastructure, in progress) — 7 separate findings, working through the safest/highest-value ones first, not attempting all 7 in one pass.** Two genuinely divergent test runners (`tests/run_all.py` vs. real `pytest`, different semantics — `run_all.py` doesn't even run `tests/hass_integration/` or `tests/regression/`), 106 files carrying an identical `__main__` collector boilerplate (needing its own `pyproject.toml` BLE001 exemption), 5 timing-dependent real-`sleep()` tests, 4 assertion-free "must not raise" tests, real duplicate test files, subentry flow tests that bypass `FlowManager.async_configure`'s own schema validation (exactly where 3 real flow bugs this review found actually live), and coverage gaps in `ml/` (nothing exercises `gbrt.py` in isolation, `calibrated_band()`, or several of `predict()`'s own documented bug-fix paths). See the issue itself for the full detail — this section will get a real update once a meaningful chunk of it ships, not left stale at "in progress" for multiple days.

---

## ⚠️ CURRENT STATE (2026-09-02) — read this first

**`solver_p2p_settlement_history_sensor` confirmed working end-to-end for the first time, on a real install (devhub) — and the Solver settings wizard's real, live field-wiping mechanics confirmed directly, not just theorized.**

A real household reported the `compute_quality_report` service (the code path behind any live Regret/dispatch dashboard) disagreeing sharply with `sensor.nimbus_solver_quality_report`'s own retrospective score for the identical real day — EPR 82.7% vs 95.14%, real P2P income $0 vs $30.66. Root cause: `solver_p2p_settlement_history_sensor` (the config field `_compute_report_for_window()` requires before it will ever price real P2P settlement) was unset. Once set to a real per-day `{export_cost, export_volume}`-shaped sensor, the identical `compute_quality_report` call for the same day returned EPR 95.84% / real P2P income $30.6599 — matching the separately-computed quality-report figure closely (small residual gap expected/methodological). This is the field's first confirmed real-world use since it was added.

**Applying it required restating every field across all 3 Solver wizard steps, not just the one being changed — confirmed directly by reading `flows/hub_options.py`, not assumed.** `async_step_solver_sources` (the final step in the `solver_battery` → `solver_grid` → `solver_sources` chain) rebuilds `merged[key] = self._solver_data.get(key)` for every key in `_SOLVER_WIZARD_SCHEMA_KEYS` — spanning all 3 steps' schemas, not just the current one. `self._solver_data` only ever accumulates keys that were genuinely present in a submission to `solver_battery`/`solver_grid`/`solver_sources`; any key omitted across all three submissions resolves to `None` at this final merge. A programmatic caller (e.g. an MCP tool driving this flow directly, not the real HA frontend walking the form) that submits only the one field it means to change will silently wipe every other Solver config value — battery capacity, max charge/discharge, grid limits, price sensors, load-forecast entities, the lot. **Any future programmatic Solver-wizard submission must pull the full current config first (`sensor.nimbus_solver_config`'s own attributes) and restate every field across all 3 steps, changing only the intended one** — same discipline this file already documents for the Forecaster/Switchboard steps' own Optional-field-wiping risk, now confirmed to apply here too, and confirmed to span the *whole* multi-step Solver chain, not just one step in isolation.

**A real, benign transient worth knowing about, not a bug**: immediately after a Solver-wizard submission that triggers a hub reload, `sensor.nimbus_solver_config` can briefly report `state: "unconfigured"` with real hardware-limit fields (battery capacity, max/discharge, grid import/export) showing `null` under `unresolved_required_keys` — even though the actual `number.nimbus_solver_*` entities themselves are fully intact. This is `sensor.nimbus_solver_config` snapshotting its own attributes a split-second before the reload-triggered re-registration of those number entities completes, not real data loss. Confirmed live: re-checking ~30 seconds later showed `state: "configured"`, zero unresolved keys, correct values throughout. **Don't trust this sensor's very first reading immediately after a Solver-wizard submission — always re-check a few seconds later before concluding anything was lost.** Distinct from the separately-documented, real `number.py` restore-on-restart bug (see the 2026-09-01 section below) — that bug is about a genuine reset to placeholder values that doesn't self-correct; this one self-corrects within seconds and never actually touches the real entity values at all.

---

## ⚠️ CURRENT STATE (2026-09-01, continued further) — read this first

Supersedes nothing below within the same day's own section — appended, not rewritten.

**v0.94.48 — issue #312's residual genuinely root-caused and fixed, after v0.94.47's own fix
was live-tested and found NOT to work.** Direct household instruction mid-investigation:
"keep going until you resolve this... not push it aside until we forget." v0.94.47's settle-
delay theory (a 100ms sleep before platform re-forwarding) was tested live on devhub
immediately after being written up here — both a full restart and a plain
`homeassistant.reload_config_entry` reproduced the identical collision at the identical
millisecond, disproving the theory outright (a cold boot has no prior unload to "settle"
after at all). CHANGELOG's v0.94.47 entry corrected in place rather than deleted, per this
project's own "record the mistake, don't quietly erase it" convention.

**Two genuinely separate causes were found, conflated as one bug until this session's own
live DEBUG-level log capture separated them:**

1. **A real, general nimbus_load code bug (fixed, affects every install).**
   `async_unload_entry()` called `hass.config_entries.async_unload_platforms()` (which tears
   down entities and unregisters their `solver_writer.py` push handlers) BEFORE the periodic-
   solve cron timer, price-watcher listener, and startup-retry task got cancelled — those were
   only ever cancelled via `entry.async_on_unload()`, which HA core's own `ConfigEntry.
   async_unload()` only processes AFTER our `async_unload_entry()` has already returned
   (confirmed by reading that source directly). An old trigger firing in that gap would find
   its handler just unregistered and fall through to `ha_post_state()`'s raw
   `states.async_set()` fallback — writing a state with no `ATTR_RESTORED` flag that then
   collided with the fresh entity's own registration a moment later. Fixed at both ends:
   `async_unload_entry()` now cancels all three triggers itself, directly, before any platform
   teardown; `ha_post_state()`'s fallback additionally skips (rather than raw-writes) for the
   closed set of entity_ids sensor.py does register a native handler for. 6 new regression
   tests. This is the real explanation for the EPR/flattened-children "freeze" symptom too
   (`sensor.nimbus_quality_epr` and siblings stuck stale while their parent kept updating) --
   the flattened child lost this same race and its own `self.hass is None` guard silently
   no-op'd forever.

2. **A devhub-environment-specific cause, NOT a nimbus_load code bug at all (fixed via devhub
   config, not code) — this turned out to be the DOMINANT cause of what was actually
   reproducing live.** devhub's own `remote_homeassistant` integration mirrors NUC1's entire
   state 1:1 with `entity_prefix` unset -- meaning every nimbus_load entity NUC1 creates
   (fixed, non-entry-scoped names like `switch.nimbus_solve_on_price_change`) gets mirrored
   onto devhub under the IDENTICAL literal entity_id string that devhub's OWN local nimbus_load
   hub also uses. Proven independently and conclusively: the exact same "does not generate
   unique IDs" error was ALSO firing for devhub's completely unrelated `cast` integration's
   media_player entities in the identical pattern at the identical restart -- ruling out
   anything nimbus-specific as the sole cause. Confirmed by checking one of the known orphaned
   duplicate entities directly: `number.nimbus_solver_battery_capacity_kwh_3` belongs to
   platform `remote_homeassistant`, not `nimbus_load`. **Fixed live on devhub**: set
   `entity_prefix: "mirror_"` on the `remote_homeassistant` config entry (`ha_set_integration`),
   reloaded it, then did a full HA restart. Confirmed zero `nimbus_load` collisions in the log
   from that restart onward (verified precisely: `switch.nimbus_solve_on_price_change`'s own
   `last_changed` landed at 20:51:02, after the restart; a direct log query for "does not
   generate unique" ordered newest-first showed nothing past 20:47:52, the PRIOR, pre-fix
   restart). Mirrored entities now live at `switch.mirror_nimbus_*`/`number.mirror_nimbus_*`/
   etc. -- genuinely distinct strings, can never collide with devhub's own local names again.
   **This means Mark Purcell's own original #312 report almost certainly has a different, or
   at minimum a much narrower, explanation than what reproduced so reliably on devhub** -- his
   install is very unlikely to run a `remote_homeassistant` mirror of a second Nimbus install.
   Cause 1 above (the real code race) is the one that could still affect his install or
   production NUC1/NUC2 directly.

**Known residual, found live during this same verification session (separate from #312) —
theory below corrected 2026-09-05 (nimbus issue #365, Mark Purcell, codebase review, item 6):
the original write-up's guess was wrong, verified against the real current code, not just
re-guessed.** Immediately after the fixed restart, `_async_run_solve_with_startup_retries()`'s
first several attempts (20:50:30 through at least 20:52:23) failed with `urllib.error.
HTTPError: HTTP Error 404: Entity sensor.nimbus_solver_config not found`. The ORIGINAL theory
here ("`_NATIVE_HASS` genuinely isn't registered yet") is **not what the code does** —
`_run_one_cycle()` always calls `_ensure_ready()` (which calls `set_native_hass()`) before
`sw.main()` runs at all (`solver_runtime.py`), so native mode is registered on every single
attempt, including the very first. `ha_get()`'s native branch *deliberately* raises this exact
shaped `urllib.error.HTTPError(404)` (see `solver_writer.py`'s own `_native_http_error()`)
whenever `hass.states.get(entity_id)` returns `None` for that specific entity — i.e. the 404
is real and correctly reported, it just means "this one entity doesn't exist YET," not "native
mode isn't wired up." The real gap is a genuine race between the startup-solve task
(`__init__.py`) firing and the `sensor` PLATFORM's own `async_setup_entry` actually finishing
and adding `NimbusSolverConfigSensor` — two independent async setup steps with no ordering
guarantee between them, not a native-mode registration problem. Still self-recovers via the
existing bounded retry loop (or the periodic 5-min cron as a last resort) with zero real
user-visible impact — the suggested, not-yet-built fix (per #365) is either catching this
specific 404 by name and logging one clean line instead of a full traceback, or having the
startup task explicitly wait for `sensor.nimbus_solver_config` to exist before its first
attempt.

**Still not picked back up this session, per the household's own explicit "don't push it aside
until we forget" instruction (separate from #312, about the 116KAT-HA-AI repo's NUC2 status
reporter, not this repo) — see that repo's own CLAUDE.md for the current state of that thread.
The single most important still-missing piece of evidence there (`/opt/nuc_status_reporter.log`'s
actual content) has still never been read.**

---

## ⚠️ CURRENT STATE (2026-09-01, continued) — read this first

Supersedes nothing below within the same day's own section — same date, later work, appended
rather than rewritten since the earlier 2026-09-01 write-up (NUC1 deploy) is still accurate.

**v0.94.42 → v0.94.44 — Mark Purcell's own IV&V work on the #312 freeze incident, root-caused
and diagnosed independently on his side, cross-confirming this project's own v0.94.40/41
fix.** Direct household ask: "help mark as much as you can... deep research before you
palm off a reply." Before drafting anything, checked GitHub directly rather than trusting
the conversation's own stale framing (his "wait for tomorrow's clean number" comment on #273
was already superseded) — found he'd independently opened #312 (parent tracker) with #313/
#314/#315 (three specific, well-evidenced sub-issues) and already built PR #317 (#316's own
feature request) himself.

**PR #317 reviewed and merged (v0.94.42)** — `nimbus_load.compute_quality_report` service,
scores an arbitrary `[start, end]` window on demand via `_compute_report_for_window()`
(extracted verbatim from `compute_daily_quality_report()`, zero behaviour change for the
existing "yesterday" wrapper, proven by a direct equivalence test). Genuinely sound, tested
work — merged on review, no changes requested.

**#313/#314 fixed and shipped (v0.94.42, corrected to v0.94.43 after a ruff lint/format CI
failure in the new test file — same "verify against the exact CI command before calling it
done" discipline as the v0.94.40→41 fix earlier the same day).** Every silent-skip path in
the quality-report pipeline (missing sensor config, no history yet, oracle LP infeasible,
the routine already-scored fast path) now logs a specific reason at the level matching how
routine it is — DEBUG for expected skips, INFO for a real history gap (with exact per-sensor
row counts), WARNING for oracle infeasibility (with `initial_soc`/`min_soc`/`max_soc`, the
exact values Mark hand-diagnosed this failure shape from on 2026-08-30). 6 new tests,
including a live mutation test (removed the oracle-infeasible WARNING, confirmed the test
catches it, reverted). Commented on both issues individually with the exact log-line text
so Mark can grep for it directly rather than trusting a summary.

**#312's own residual (the platform-level entity collision both his install and this
project's own NUC1 restart independently hit) — investigated further, not fully
root-caused, said so plainly rather than guessing.** Checked whether subentry-scoped
platform forwarding could explain a same-platform self-collision without a second full
`async_setup_entry` call — ruled out (`async_forward_entry_setups()` has exactly one
call site in `__init__.py`, entry-scoped, not per-subentry). Shipped v0.94.44: DEBUG-level
start/completion/duration logging around that one call, diagnostic-only, so a recurrence is
at least timeable against the "does not generate unique IDs" error's own timestamp next
time — narrowing, not yet answering, whether HA core's own platform-forwarding/eager-task
machinery is genuinely re-entering this call. Posted this honestly on #312 rather than
presenting a guess as a fix; issue stays open per Mark's own explicit recommendation.

---

## ⚠️ CURRENT STATE (2026-09-01) — read this first

Supersedes the 2026-08-31 night section below, which is kept as historical record only.

**v0.94.40/v0.94.41 — the long-standing "`number.nimbus_solver_*` resets to placeholder
minimum on some restarts" bug is root-caused and fixed, deployed to NUC1, verified live.**
Live evidence via `ha_get_logs` on devhub: two genuinely concurrent runs of
`async_setup_entry()` for the SAME config entry, ~4.5s apart, produced a hub-wide "Platform
nimbus_load does not generate unique IDs" collision storm across every `number`/`sensor`/
`switch` entity — HA's own "abandon a slow `async_setup_entry()` and silently retry it while
the original coroutine keeps running" behaviour (the original coroutine is never cancelled;
if both eventually finish, both try to register every entity, and whichever wins the race
is non-deterministic across restarts — exactly the "sometimes fine, sometimes reset"
pattern reported repeatedly since 2026-08-26). Same general mechanism issues #210/#211
already partially addressed (a slow inline retrain, duplicate timers) — this was a third,
not-yet-backgrounded slow step: the per-subentry coordinator setup+first-refresh loop.

Fixed at the root: `async_setup_entry()` now guards against re-entry for the same
`entry_id` (module-level `_setup_tasks` dict) — a second, genuinely concurrent call waits
for the first attempt's own result instead of duplicating every entity/timer it creates.
Also parallelized the coordinator setup loop itself (`asyncio.gather`, was a sequential
`for`) per direct request ("speed up the startup... in some clever way") — faster setup
means fewer opportunities for the race to trigger at all, defense-in-depth alongside the
guard. 3 new regression tests including a live mutation test.

**Real process mistake, caught and corrected same session**: v0.94.40's first shipped fix
wrapped the guarded setup body in `hass.async_create_task(...)` and awaited its own return
value — broke 5 pre-existing tests in CI (`test_init_cron_suppression.py`,
`test_init_periodic_solve_timer_idempotent.py`), which mock `hass.async_create_task` as
fire-and-forget (closes the coroutine, returns a bare `MagicMock()`), since no prior call
site had ever awaited that return value. My own lighter `tests/run_all.py` custom runner
did NOT catch this — only running the real `pytest` command CI actually uses did. Caught via
`gh run watch`, fixed by switching to plain `asyncio.create_task(...)` (this one task's
result is awaited by its own creator; HA's own top-level await of `async_setup_entry()`
already tracks its real lifecycle, no `hass` wrapper needed), verified against the exact
CI command locally (836 passed) before shipping the corrected release as v0.94.41 — a new
patch version, not an amend of the already-tagged, briefly-broken v0.94.40, per this
project's own established convention for a same-day follow-up. **Lesson for next time**:
before trusting any change to `hass.async_create_task`/`hass.async_create_task`-adjacent
code, grep existing tests for how they mock it — a call site's assumptions about a mock's
shape are easy to violate silently.

**Deployed to NUC1 (confirmed live MASTER at deploy time — VIP `192.168.1.221` matched
NUC1's own `sun.sun` state exactly, NUC2 unreachable on 8123, consistent with its BACKUP
role) and verified end-to-end**: `git pull` + `docker restart`, all 5 hardware-limit number
entities restored their real values (122.2/40.0/40.0/42.0/40.0 — battery capacity/max
charge/max discharge/grid import/grid export), zero placeholder resets. Full stack healthy
within ~11 minutes of restart (`sensor.nimbus_solver_config`="configured" within 30s,
`sensor.nimbus_solver_battery_forecast` and `sensor.nimbus_solver_dispatch_dry_run` both
producing fresh real values, `sensor.nimbus_solver_quality_report` live at a real value).

**One narrower, non-destructive residual found live during this same verification, not
yet root-caused — worth a real look before claiming this fully closed.** A single
`Platform nimbus_load does not generate unique IDs` ERROR still fired for exactly ONE
entity (`sensor.nimbus_solver_quality_report`) on this same NUC1 restart, despite the
entry-level re-entrancy guard being in place. Confirmed via log line count: only ONE
`Setting up nimbus_load.sensor` line appears for the whole restart (i.e. the sensor
PLATFORM's own `async_setup_entry` — called via `hass.config_entries.
async_forward_entry_setups`, not the top-level integration `async_setup_entry` my guard
covers — was not invoked twice this run), yet the entity still collided with itself.
Working theory, not yet confirmed: HA's abandon-and-retry behaviour may apply
independently at the PLATFORM-forwarding level (`entity_platform.py`'s own eager-task
setup), a narrower granularity than the entry-level guard covers — meaning the general
race is still reachable, just far less often and with a much smaller blast radius (one
entity instead of every entity across three platforms) now that the entry-level guard and
the parallelized coordinator loop both reduce the odds of tripping it at all. **Zero
functional impact this time** (first registration won, `sensor.nimbus_solver_quality_report`
had a real, correct value — 0.9642 — both times checked), but non-deterministic by the
same nature as the original bug, so a future restart could unluckily drop a
more consequential entity instead. Next session picking this up should start by checking
whether `entity_platform.py`'s own `_async_setup_platform`/`create_eager_task` path has an
independent timeout/retry mechanism separate from `config_entries.py`'s entry-level one,
before assuming the entry-level guard alone was sufficient.

---

## ⚠️ CURRENT STATE (2026-08-31 night) — historical, superseded by the section above

**Version: v0.94.37** (up from v0.94.12 as of the last write-up — a large number of releases
landed across 2026-08-27 through 2026-08-31 that were never captured in this section; see
`CHANGELOG.md` for the full, real list, this paragraph is not attempting to re-derive it).
Open issues as of writing: #114, #217, #236, #273 (mostly resolved-in-comments/deferred-scope),
plus a real, live, still-open bug (see below).

**#307 (fix, PR #309, v0.94.37) — Mark Purcell caught a real bug in this project's own
v0.94.35 fix.** `solver_battery_power_positive_is_charge` (the SigEnergy sign-convention flag
from issue #299) was registered on the Solver Sources wizard form but missing from
`_SOLVER_WIZARD_SCHEMA_KEYS`, so the save loop silently dropped it — submitting the wizard
reported success while the value stayed `None` forever, leaving v0.94.35's own fix
unreachable from the UI. One-line fix, real regression test, verified end-to-end by Mark on
his own live SigEnergy install before opening the PR. A good, concrete example of exactly
why his review is treated as high-trust — this shipped a genuine, well-evidenced fix, not a
guess.

**⚠️ Live, unresolved, deferred to 2026-08-31/09-01 — number.py's RestoreNumber mechanism
loses real values on some (not all) HA restarts, root cause not yet found.** Confirmed again
live on devhub the night of 2026-08-31: after a `ha_restart`, the five hardware-limit
`number.nimbus_solver_*` entities (battery capacity, max charge/discharge, grid max
import/export) came back correctly restored this time — but this is the same class of bug
already documented multiple times in this project's own history (116KAT-HA-AI's CLAUDE.md,
"2026-08-26 evening" section and others) where these exact entities have, on other restarts,
reset to their own schema placeholder minimum (`0.1`) instead of restoring. `number.py`'s own
`async_added_to_hass()` calls `self.async_get_last_number_data()` (the standard `RestoreNumber`
API) and falls back to seeding from `entry.options` only when no restored state is found — this
looks correct on a read, but the bug is real and has recurred across multiple sessions on a
full HA **core restart** specifically (not a config-entry reload, which never shows this).
**Not yet investigated**: whether this is a genuine race in HA core's own restore-state
timing on a cold boot (entity added before the restore-state cache is fully populated), a
`RestoreNumber`/`NumberEntity` interaction quirk, or something specific to how many entities
this platform registers at once on `async_setup_entry`. Household explicitly asked to defer
digging into this until 2026-09-01 rather than risk another restart late at night — picking
this back up should start by reading `homeassistant.helpers.restore_state`'s own real source
(`RestoreEntity.async_get_last_state()`'s timing guarantees relative to `async_added_to_hass`)
before touching `number.py` again.

**PR #310 (Mark Purcell, merged 2026-08-31) — `quality_report.py` refactor, row-major hourly
reconstruction keyed by ISO local timestamp (#297 follow-up).** Real CI-only issues found and
fixed before merge, neither one in the PR's own diff: (1) `aiohttp.web_exceptions.
NotAppKeyWarning` from HA core's own `http` component's internal `app["hass"]` string-key
usage, promoted to a hard error by this repo's `filterwarnings = ["error", ...]` — added a
third `filterwarnings` ignore entry, same reasoning as the two already there (a dependency's
own internal housekeeping warning). (2) Mark's new `http_and_frontend_set_up` conftest fixture
needed HA core's real `frontend` component to complete setup, which does a bare `import
hass_frontend` — a separate PyPI package (`home-assistant-frontend`, date-versioned) not
bundled by `pytest-homeassistant-custom-component`. Added as an unpinned dev dependency. All 7
CI checks green after both fixes; this also unblocked the earlier PR #92 (topology-card
frontend bundling), which used the same fixture.

**v0.94.38 — real diagnostic-logging fix for a genuinely mysterious live incident, root cause
still not fully pinned down.** `sensor.nimbus_solver_dispatch_dry_run` silently dropped exactly
one solve cycle live on NUC1 the night of 2026-08-31: last real push `17:10:32`, nothing at
`17:15` (despite the sibling `sensor.nimbus_solver_battery_forecast` getting a fresh push from
the SAME cycle, proving the main solve loop ran fine), the staleness watchdog correctly marked
it `unavailable` at `17:15:37`, and it resumed cleanly on its own at `17:20:32`. Confirmed
precisely, not guessed: the dry-run switch never flickered off (checked its full state
history — stayed `on` throughout), and zero exception was ever logged (`_log_dispatch_dry_run`'s
own `except Exception: _LOGGER.exception(...)` never fired — grepped the exact text, zero
matches). This means one of the function's three remaining silent early-return guards
(forecast sensor missing / forecast list empty / `battery_kw` key missing) tripped for exactly
one cycle, with literally no way to tell which after the fact, since none of the four
early-return paths logged anything. **Fixed**: all four early-return branches now log a
`WARNING` naming exactly which condition fired and the relevant live state — purely additive,
no behavior change, so if this recurs it'll be diagnosable instead of a mystery. Shipped
directly to `main`, tagged, released; a real merge of Mark's own PR #310 landed in between the
commit and the push, resolved with a clean `git merge` (not a rebase, since the tag had already
been pushed pointing at the pre-merge commit — rebasing would have orphaned it).

**A real, evidence-based, multi-day validation of Nimbus's own dispatch logic — 12/12 days
viable, not "just one test night."** `sensor.nimbus_counterfactual_soc_5pm`'s full `history`
attribute was pulled and reviewed in detail: 12 consecutive real days (2026-08-19 through
2026-08-30), every single one `viable: true` — i.e. if Nimbus alone had been deciding since
midnight, the battery would have stayed genuinely ready for that evening's P2P window every
single night, with Nimbus-only SoC at 5pm consistently landing within a few points of what the
real system actually achieved (96.9–97.3% vs. real 99.2–100%), including through the real
NUC2 failover incident on 08-28/29. This is meaningfully stronger evidence than a single
night's shadow-mode comparison — it directly moves the "Confidence/evidence for live control"
axis of the Readiness Checklist forward for real, not just anecdotally.

**Two real self-corrections made live, worth recording precisely so they aren't repeated**:
tonight's own readiness-comparison discussion with the household twice asserted an unverified
claim about Nimbus's own capabilities being weaker than they actually are, without checking
the code first — both caught by direct household pushback, both confirmed wrong by grepping
the actual repo:
1. **"Stochastic/uncertainty handling: Not built" was false.** `solver/stochastic.py` (Track
   A2) is a real, correctly-structured two-stage stochastic LP — genuine shared stage-1
   variables, scenario-indexed stage-2 variables, a weighted-expected-cost objective (the real
   structure, not a "solve twice" shortcut). It's real and tested (`tests/test_solver_
   stochastic.py`), just deliberately scoped as a separate, v1, shadow-mode-only module — its
   own docstring is explicit that it's missing sheddable loads, adequacy loads, the two-tier
   P2P bonus, `fixed_export_kw`, SoC-dependent power curves, `terminal_value_breakpoints`, and
   all three stability mechanisms (proximal/max_rate/smoothness) relative to `build_plan()`.
2. **"SoC-dependent power curves: Not built (Nimbus)" was also false.** `BatteryConfig.
   charge_power_curve`/`discharge_power_curve` are real, tested fields wired directly into the
   PRODUCTION LP path (`elements.py`'s `_validate_power_curve` + `network.py`'s own concave
   piecewise-linear-upper-bound constraint construction, ~line 1174) — confirmed via
   `tests/test_solver_battery_power_curve.py`. The real gap was narrower than claimed: built
   and correct, just never actually POPULATED by any writer script until tonight (see the
   sibling `116KAT-HA-AI` repo's own CLAUDE.md, PR #812).
3. **Separately confirmed, not assumed**: HAEO's own real source (a local clone,
   `116KAT-HA-AI/haeo_repo`, 375 real Python files, genuine battery model code present — not an
   empty/broken clone, though dated 2026-06-08, ~3 months stale) has **zero matches for
   "stochastic" and zero matches for any power-curve/SoC-dependent-power terminology anywhere**.
   Neither capability exists in HAEO at all, as far as this clone shows — the earlier assumed
   comparison ("HAEO does this, Nimbus doesn't") had the direction backwards on both counts.

**v0.94.39, shipped — the stochastic-P2P extension scoped above is now built, tested, and
released.** New `solver/p2p_export.py` — the P2P mechanism (hard charge-gate during a
committed export period, `grid_export[t]` pinning, `export_bonus[t]` capped per real
calendar day with the latest-preferred tie-breaker) extracted **verbatim** from `network.py`,
as a genuinely separate module — `network.py`/`build_plan()` were never touched, confirmed
via a clean diff showing zero changes to that file. Wired into `solver/stochastic.py`'s
`_add_period_vars_and_constraints()` closure, applied per-family (stage 1 shared decision,
and independently per stage-2 scenario) — every P2P field stays `None` = complete no-op,
same convention as every other optional mechanism in this codebase.

**A real, pre-existing gap closed as a genuine prerequisite, not scope creep**:
`StochasticPlan` never exposed `grid_import_kw`/`grid_export_kw` at all before this — since
`fixed_export_kw` pins `grid_export` directly (not battery charge/discharge), the mechanism
would have been completely unobservable from the result without this fix. Added
`stage1_grid_import_kw`/`stage1_grid_export_kw`/`stage2_grid_import_kw`/`stage2_grid_export_kw`
alongside the new `stage1_export_bonus_kw`/`stage2_export_bonus_kw` fields.

**5 new tests** (`tests/test_solver_stochastic_p2p.py`), including two real live mutation
tests run before shipping (not just "the tests pass") to confirm they genuinely catch the
bug each mechanism exists to prevent: removing the hard charge gate reproduces real charging
during the committed window (up to 40kW — matching the shape of the original 2026-08-22
live incident); removing the export-bounds pinning confirms `grid_export` reverts to 0kW
instead of the committed rate. Both mutations reverted immediately after confirming the
failure. Full suite: 723/723 passing (up from the 718 baseline), zero regressions to anything
pre-existing. Also caught and corrected a real overclaim in this module's own docstring
during development: `lp.py`'s constraint `name` field is "purely for readability, never
required" (confirmed via its own docstring) — a scenario-name collision on the per-day cap's
constraint name can never leak volume between scenarios (each scenario's own LP variables
are already distinct), it can only make one scenario's shadow price unreachable in
`LPResult.duals`. The `label=suffix` mechanism is correctly kept for that reason, but the
earlier draft's claim that a collision would risk correctness was wrong and has been fixed
in the shipped docstring.

**The one real caller: `116KAT-HA-AI`'s `scripts/nimbus_stochastic_comparison_writer.py`**
(PR #814, not yet merged) — deliberately devhub-only and fully reversible: `HA_BASE`
hardcoded to devhub's real IP (192.168.1.151), never the NUC1/NUC2 VIP; writes a standalone
`sensor.nimbus_stochastic_comparison` reporting sensor only, never touches Modbus/automations.
Verified end-to-end with a local smoke test against mocked devhub-shaped HA responses
(config fetch → forecast fetch → P2P window scoping → stochastic solve → sensor push) —
confirmed the fixed-export mechanism correctly pins exactly 84/288 5-min periods over a
24h horizon (matching the real 7h window), leaving every other period free. **Not yet run
against a real, live devhub** — needs a one-time devhub-scoped Long-Lived Access Token
(see the script's own "Deploy" docstring section) before its first real run.

---

## ⚠️ CURRENT STATE (2026-08-27 night) — historical, superseded by the section above

**Version: v0.94.12** (up from v0.94.2). PRs #246, #247, #249, #250, #251, #252 merged this
session; releases v0.94.9 → v0.94.12 cut. Open issues as of writing: #114, #217, #236 (mostly
resolved-in-comments/deferred-scope, not active work).

**#245 (partial fix, #246) — LP degeneracy, simultaneous charge/discharge.** Added a linear
`charge[t] + discharge[t] <= max(max_charge_kw, max_discharge_kw)` cap to `network.py`'s
wash-trade-prevention section (Mark's own proposed fix). Tested honestly rather than trusting
the issue's own claim: a same-period charge-then-export round trip stays genuinely profitable
whenever `export_price` exceeds `import_price` by more than round-trip loss — the cap *bounds*
that, doesn't eliminate it. Full elimination needs #238's MILP complementarity, not attempted
here. `tests/test_solver_combined_direction_cap.py` documents both the hard guarantee and the
honest residual gap.

**#244 (fix, #247) — solver cron phase-aligned to the real NEM 5-minute settlement boundary.**
The native in-process solver's own timer (`__init__.py`) was a free-running
`async_track_time_interval(minutes=1)` with no phase relationship to when a real settled price
tick actually lands. Mark's own 24h measurement (273 real tick arrivals) found the settled
value lands in `[15s, 30s)` past each boundary 89% of the time — swapped to
`async_track_utc_time_change(..., second=30)`, locked to `:00:30, :05:30, ...`. Also *fewer*
solves overall (12/hour vs 60/hour), not more.

**#238 groundwork (binary variables + dual recovery, #249) — recovered, not built fresh.** A
real, previously-orphaned commit (pushed to a stale branch by a disconnected session, no PR
ever opened, zero test coverage) was found, verified sound, cherry-picked onto current main,
and given real test coverage (12 new tests, the first direct `LPProblem`-level tests in this
suite) before merging — `LPProblem.add_variable(binary=True)` + `is_mip`, HiGHS MIP solve with
duals recovered via pin-and-relax (branch-and-bound alone gives no meaningful duals). Groundwork
only — the complementarity constraint itself is still not written, `network.py` is untouched.
#238 itself stays closed (COMPLETED) — this is groundwork for its explicitly-kept-open,
non-blocking MILP follow-up, not a reopening.

**Standalone-writer phase-alignment (#251, v0.94.12) — a genuine, general fix, with an honest
caveat about where it actually applies.** The same #244/#247 phase-alignment gap exists in the
standalone `nimbus_solver_forecast_writer.py` cron script too (documented to deploy on a bare
`* * * * *`, never touched by #247's fix, which only lives in the native runtime's own timer).
`seconds_to_settlement_capture()` adds a short, bounded wait to only the one tick per 5-minute
cycle landing near a real boundary — the other four ticks stay a complete no-op, preserving
the every-minute cadence itself. Real, tested (8 new tests), and correct for any install that
actually runs this standalone script (e.g. via the `nimbus_solver_app` HAOS add-on, which
builds from this same file). **Caveat, found via live follow-up investigation, not assumed**:
an install that only runs the native in-process runtime (no standalone cron at all) sees zero
benefit from this specific fix — don't assume a live symptom is explained by this path without
first confirming the standalone script is actually scheduled and running on that install.

**A real collaboration pattern worth naming**: Mark Purcell (`purcell-lab`) is an active,
highly-engaged tester filing well-evidenced issues with real timestamped data, not vague
reports — treat his input accordingly (see the global `CLAUDE.md`'s own "Working preferences"
entry on this). Also worth knowing: more than one Claude session can be actively working this
repo at once (a household may run sessions from more than one device) — check `gh pr list`/
`gh issue list` for genuinely current state before assuming a symptom is unexplained or
unaddressed; don't re-diagnose something another session already fixed minutes ago.

---

## ⚠️ CURRENT STATE (2026-08-26 evening) — historical, superseded by the section above

**Version: v0.94.2** (bumped from v0.94.1 this session). PRs #204, #206, #207 merged;
#205 closed. Nothing currently open on the repo as of this writing.

**#204 — required wizard fields now visually marked.** Every genuinely `vol.Required`
field across the Solver Battery/Grid/Sources options-flow steps and the Load, Power
Signal, Power Source, and PV String subentry steps gets a 🔴 prefix on its label, plus a
"🔴 = required." legend on that step's own description — HA's generic `ha-form` gives
every field the same plain look regardless of required/optional, so this was previously
invisible until a validation error. Companion doc: `docs/setup-guide.md`, a plain-English
start-to-finish setup walkthrough, cross-linked from the README's Install section
alongside the existing field-by-field `docs/configuration-reference.md`.

**#205/#206 — Mark Purcell's overnight-reserve question, answered from real code, not
guessed.** Mark asked whether an 11.4kWh dawn SoC reserve (above the configured 5% floor)
meant a hidden soft-floor bug. Verified directly in `solver/network.py:723`:
`battery_soc_{t}`'s LP bound is `lb=battery.min_soc_kwh` at **every** period, uniformly —
no separate floor exists anywhere. The real cause is `terminal_value_breakpoints`
(the piecewise concave terminal-value curve) being evaluated at **every midnight boundary
in the horizon, not just the true horizon end** — `solver_writer.py`'s
`terminal_value_period_indices=sorted(set(midnight_boundary_period_indices(grid_times) +
[len(grid_times) - 1]))`. That mechanism itself was built 2026-08-18 partly in response to
Mark's own audit item #7, and is a soft economic nudge (not a hard constraint) — same
caveat as its own 2026-08-22 origin story (shadow plan discharging 30-40min past a real
P2P midnight cutoff). #206 closed the one real gap Mark's question surfaced: no entity
exposed `salvage_value`/`degradation_cost_per_kwh` for regression against the price stack
— both now live as attributes on `sensor.nimbus_solver_battery_forecast`, same pattern as
the existing `risk_aversion`-style attributes.

**A real, reproducible, NOT YET FIXED bug found live on devhub this session — needs a
proper fix, not just documentation.** Every `homeassistant.reload_config_entry` (or any
event that reloads the Nimbus config entry — editing wizard settings, HA restart, adding/
removing a subentry) fires a burst of `Platform nimbus_load does not generate unique IDs`
ERRORs for every static Solver `number.*`/`switch.*` entity and the three static
`sensor.*` entities (`nimbus_solver_config`, `nimbus_topology_config`,
`nimbus_solver_battery_forecast`... — NOT subentry-based forecast sensors, which are
unaffected). No functional impact so far (first registration wins, entities keep working)
but it's a genuine setup-code bug — reproduced 3 times live tonight
(16:32:36, 17:19:07/17:19:23, 19:00:09 AEST), each time a config-entry reload happened.
Root cause not yet investigated (likely: the platform's own `async_setup_entry` re-adding
already-registered entities on every reload instead of being idempotent). Worth a proper
fix + regression test before the next release — any real user who edits Solver settings
via the wizard will hit this.

**Devhub-side note, not a nimbus-code issue — for context only.** devhub (116KAT-HA-AI's
Nimbus dev/test box, `192.168.1.151`) had accumulated a parallel, hand-rolled `rest:`
platform "mirror" sensor layer (`sensor.mirror_*`, pulling NUC1's real values over its own
REST API) plus matching Nimbus subentries, built before/alongside the real
`remote_homeassistant` 1:1 mirror connection that devhub also has. Confirmed genuinely
redundant (real, working non-mirror equivalents already existed for every one) and removed
entirely tonight, at the household's explicit direction — see 116KAT-HA-AI's own CLAUDE.md
for the full removal record. Also found and fixed, independently, a Watts/kW scaling bug
on a devhub dashboard chart plotting `sensor.combined_total_dc_power` (real, native-Watts
NUC1 solar sensor) without the same `x/1000` transform every sibling circuit series in
that chart already used — a dashboard-config fix, not a nimbus code change. Neither of
these affects the nimbus repo itself; noted here only so a future session doesn't
rediscover the same devhub architecture from scratch.

**Real, load-bearing discovery about this integration's options-flow behavior — found
the hard way, cost real (recoverable, but real) config damage before being understood.**
On `hub_options.py`'s options flow (and, by the same mechanism, the subentry flows): a
`vol.Required` field's schema is built with `default=<current stored value>`, so omitting
that key from a submission safely re-saves whatever was already there. A `vol.Optional`
field has **no such default wired to the current value** — omitting it from ANY submission
that touches that step, even one aimed at a completely different field on the same step,
silently resets it to `None`. Confirmed by direct incident tonight: fixing
`solver_solar_forecast_sensor` (required, submitted alone) silently blanked
`solver_whole_house_cross_check_sensor`, `solver_solar_power_sensor`, and
`solver_battery_power_sensor` (all optional, all on the same `solver_sources` step) —
recovered by restating them explicitly in a follow-up call. **Any future programmatic
options-flow submission to this integration (via `ha_set_integration`/`ha_config_set_helper`
or equivalent) must restate every field on that step it wants to keep, not just the one
being changed** — this is a real UX footgun worth fixing in `hub_options.py` itself
(give every `vol.Optional` the same `default=current_value` treatment `vol.Required`
already gets) before it bites a real end user editing their own wizard by hand.

**Separately, a real and still-unexplained bug**: on the actual HA **core restart** used to
deploy v0.94.2 tonight (not a plain config-entry reload — dozens of those happened tonight
with no issue), 5 `number.nimbus_solver_*` entities (`battery_capacity_kwh`,
`max_charge_kw`, `max_discharge_kw`, `grid_max_import_kw`, `grid_max_export_kw`) reset to
their own schema placeholder minimum (`0.1`) instead of restoring their real, previously-set
values. Recovered from real recorder history (confirmed stable and correctly-restored across
many earlier reloads that same evening), not guessed, then re-set via `number.set_value`.
Root cause not investigated — worth a real look at this platform's own state-restore path
specifically on a full HA restart (as opposed to a config-entry-only reload) before this
recurs on a real user's install and costs them real configured hardware limits silently.

**Topology diagram — confirmed, this session, to already be genuinely wizard-live, not
hardcoded, once real Power Source/PV String/Battery Tower subentries exist.** The real
`switchboard-topology-card` (116KAT-HA-AI repo, `config/www/topology-card-v4.js`) has a
`_discoverTopologyConfig()` method that reads `sensor.nimbus_topology_config`'s
`power_sources`/`pv_strings`/`battery_towers` attributes directly and swaps them in over
any static YAML config the instant at least one Power Source subentry exists — by design,
confirmed by reading the card's own source, not assumed. Devhub had simply never had those
subentries created, so it was silently running on its card's own static-YAML fallback path
despite looking "wizard-driven." Fixed by creating the real subentries (2 Power Sources, 3
PV Strings, 4 Battery Towers, matching the real NUC1 household's own physical layout) —
the card now genuinely reflects add/remove of these subentries live, no card edit needed.

**A genuinely portable field-semantics pitfall, found the same night on a devhub dashboard
table but worth flagging here since ANY dashboard built against the Solver's forecast data
could hit it.** The per-interval plan dict's `bonus_price` field (see `solver_writer.py`
line ~5393, `"bonus_price": round(export_bonus_price[i], 4)`) is, by design,
`max(0.0, p2p_export[i] - spot_export[i])` — the real INCREMENTAL P2P premium over spot
(see `elements.py`'s own `export_bonus_price` docstring: "the real INCREMENTAL premium (P2P
rate minus spot)"). That's the economically correct quantity for LP pricing purposes, and
nothing in the solver itself is wrong. But a household reading `bonus_price` alone and
expecting it to equal "the real P2P rate" (the number a live gauge/tariff plan would show)
will see a mismatch — devhub's own Solver Forecast table did exactly this
(`P2P¢` column showing `bonus_price*100` alone, ~28-40c, while the real live gauge read
~43c). **The real absolute P2P rate for display purposes is `export_price + bonus_price`**,
not `bonus_price` alone. Not a code bug — `bonus_price` is correctly named and correctly
computed for its actual (LP-internal) purpose — but worth calling out explicitly in any
future docs/dashboard-example code so "P2P price" always means the real absolute rate to
an end user, never the bare incremental component, unless clearly labeled otherwise.

---

## ⚠️ CURRENT STATE (2026-08-17 morning) — historical, superseded by the section above

### Thread 1 — Forecaster fixes, still pending deploy

**Still true, re-confirmed live 2026-08-17**: `main` has real, verified fixes (the bug
chain below, #1-#7) that have **never been deployed to the live NUC**. Confirmed directly
this session, not assumed: pulled `sensor.nimbus_logger_load_power_forecast`'s live
`forecast` array and found a real, repeated flat value (`4.447` at 12:31, 12:46, AND 13:01
— three consecutive 15-min points holding one exact value) — this is bug #7's own
stair-step signature, still present live. `model_trained_at` on that same pull was
`2026-08-16T07:51:13`, well before any of this session's own work. **Deploy this before
building anything further on the Forecaster side** — same commands as before, still
accurate:
```bash
cd /opt/homeassistant/config/nimbus_repo
git pull origin main
rm -f /opt/homeassistant/config/.storage/nimbus_load_*.pkl
docker restart opt_homeassistant_1
```
Verify: `model_trained_at` fresh, forecast array smooth (no repeated flat runs, no isolated
spikes).

### Thread 2 — Solver: full regret/EPR/tracking framework built, two real bugs found and
fixed in its first real live application

Substantial progress since the 2026-08-15 draft described lower down this section (kept as
historical record — still accurate for what it describes, just superseded by everything
below). The three stability mechanisms asked for that night (proximal regularization, rate
limiting, confidence-aware dispatch) are **all built** — see `network.py`'s own module
docstring for the full, current mechanism-by-mechanism detail; nothing more to add here.

**New this session (2026-08-16/17), working through Mark Purcell's own 9-item Solver audit**:
- `regret.py` — `evaluate_realized_cost()` now exposes `cost_per_period` (not just the
  summed total); `hourly_regret_breakdown()` bins (actual − oracle) cost by hour-of-day,
  reproducing Mark's own "report regret hourly, never the ratio" chart pattern (rust =
  value left on the table that hour, teal = the optimum deliberately spending more that
  hour to earn more later).
- `epr.py` — Economic Performance Ratio, `EPR = (J_ref − J_ach) / (J_ref − J*)`, Mark's own
  positive-framing correction to a plain regret number (a solar plant reports a performance
  ratio, not "irradiance regret" — same idea here). Full vocabulary glossary in the
  docstring (value headroom, theoretical maximum yield, peak capture rate, etc.).
- `elements.py` / `network.py` — `GridConfig.min_export_kwh` (default `None`, complete
  no-op): forces a solve to deliver at least a given real total export volume across the
  whole horizon. Built specifically to fix a real EPR-computation bug: comparing an
  idle-battery reference case against a perfect-foresight oracle under a REAL household's
  P2P program (a fixed, pre-committed nightly revenue, not a price-taking market) let the
  oracle collect the real settled P2P credit without being forced to physically discharge
  the volume that revenue actually requires. Forcing the oracle to deliver the SAME real
  volume the actual dispatch delivered turned a mathematically-impossible EPR (>1.0) into a
  genuine, defensible one (0.3069 → 0.3691 once fixed, using one real household's own Aug
  16 P2P window: $29.426251 real settled revenue, 63.148235 kWh real settled volume).
- `tracking.py` — control tracking fidelity (Mark's audit item #6): how well the ACTUAL
  dispatch matched its OWN commanded setpoint (not a foresight comparison at all) —
  `compute_tracking_fidelity()`, `tracking_error_cost()`. Applied to one real household's
  own inv1/inv2 discharge-duty rotation gaps: 98.46% tracking fidelity, ~$0.35/night real
  cost of the gaps.

All of the above verified with real synthetic tests before being trusted (see each file's
own docstring/test description) — none of it has ever touched a live HA system; it's a
pure-Python library, zero HA imports, same "genuinely dead code, zero deploy risk" status
as the original 2026-08-15 draft.

**First real live application of the Solver (`scripts/nimbus_solver_forecast_writer.py`,
lives in the sibling `116KAT-HA-AI` repo, not here) surfaced two real, confirmed bugs —
both found by the household directly flagging the live chart as "not right by any count,"
neither a Solver logic bug, both real input-data-handling bugs in the writer script**:

1. **P2P price forecast freezing on a stale in-window value (PR #615, merged and deployed
   2026-08-17).** `sensor.localvolts_p2p_price_forecast`'s real coverage runs out ~36h
   ahead; past that, the writer's plain nearest-before resampling just held the LAST real
   point flat forever — and that point can land mid-window (0.5) as easily as
   post-window (0.0) depending purely on what moment "now" happens to fall at. Confirmed
   live this froze `p2p_export=0.5` through entire overnight/self-consume hours, driving
   the LP into a fantasy overnight arbitrage (grid_import AND grid_export both large and
   nonzero in the same period). Fixed with `resample_p2p_forecast()`: beyond real coverage,
   reproduces the signal's own known repeating-daily-window shape (flat rate only during
   real local hour 17-24, zero otherwise) instead of assuming persistence.

2. **The writer's own local-time resolution was silently UTC, not AEST (PR #616, merged
   2026-08-17, not yet redeployed as of writing).** `now = datetime.now(timezone.utc).
   astimezone()` — bare `.astimezone()` converts to whatever the SYSTEM's own local
   timezone resolves to; on this NUC's own cron/shell environment that's UTC, not AEST,
   despite the file's own prior (never actually verified) assumption that "this NUC runs
   Australia/Brisbane." Confirmed precisely: a real deploy run's own `generated_at` showed
   `2026-08-16T23:44:00+00:00`, directly compared against the real known AEST wall-clock
   time at that same moment (09:44 AEST) — should have been `+10:00`. This corrupted EVERY
   hour-of-day decision in the file by a consistent 10-hour offset — the real 17:00-24:00
   P2P window was actually being evaluated as UTC 17:00-24:00 = AEST 03:00-10:00 the next
   day — which is why the household still saw real nonsense in the live chart even after
   fix #1 deployed. Fixed with an explicit `BRISBANE_TZ = ZoneInfo("Australia/Brisbane")`
   used everywhere real-local-time matters, instead of trusting system resolution.

Both fixes verified together against real live data (read-only local diagnostic, nothing
pushed): 0/169 simultaneous import+export periods (was 43-plus), total export over a ~97h
horizon 337.56 kWh (2.76x battery capacity — matches ~4 real nights of the household's own
~84kWh/night P2P pattern, was 6.4x), export outside the real P2P window 1.56 kWh (was 449),
8 charge/discharge direction reversals (~2/day, matches the real pattern).

**Process lesson, worth remembering — a real mistake made and corrected the same session**:
mid-investigation, a git checkout of `main` was done to prepare PR #616's branch, but this
happened using an ALREADY-STALE local `main` (pulled right after opening PR #615, before it
was actually merged — never re-pulled after being told it was merged). This produced a
genuinely confusing false alarm: a re-test appeared to show the SAME bad numbers as before
ANY fix, which briefly looked like either a reverted merge or a second, independent
reviewer editing the same file — neither was true. The real, mundane cause: testing
against old code by accident. Always `git fetch origin && git rev-parse origin/main` (or a
plain fresh `git pull`) immediately before re-testing anything after a PR merge — don't
trust a local checkout's freshness just because it was correct minutes earlier in the same
session.

**Not yet redeployed as of writing**: PR #616 (the timezone fix) — deploy same pattern as
#615:
```bash
cd /opt/homeassistant && git pull origin main
git show origin/main:scripts/nimbus_solver_forecast_writer.py > /opt/nimbus_solver_forecast_writer.py
python3 /opt/nimbus_solver_forecast_writer.py
```
Check the printed line's own timestamp shows `+10:00`, not `+00:00`.

### Original 2026-08-15 draft notes (historical, still accurate for what they describe)

New subpackage `custom_components/nimbus_load/solver/` (PRs #19, #20, both merged) — a
from-scratch, pure-numpy LP solver (`lp.py`), a real element model with structural
degeneracy guards (`elements.py`), a network/plan builder (`network.py`), and a shadow-mode
translator into this project's own real Sungrow language (`modes.py`). See
`custom_components/nimbus_load/solver/README.md` for the full detail, including two real
bugs found and fixed during the build (an LP sign bug, an efficiency-validation bug), both
caught by dedicated tests before they ever touched real data.

Also captured that night, then corrected within the same session — worth being precise
about: the first draft of a reporting requirement (architecture sketch's §4a) pointed
directly at LocalVolts-specific files (`lv_costs.yaml`, `lv_p2p_daily_recalibrate.py`) as
the design reference for the solver's own cost/earnings reporting. Explicit correction from
the user: *"this is not just LV design should work with any supplier api or retailer... i
do not want LV to be LV exclusive only"* and, specifically about the next-morning
settlement-reconciliation pattern: *"the recalibrating tools we use are a patch for us...
not a solution... that should not be a part of the solver."*

This household has already changed retailer once (Amber → LocalVolts) — the solver's
reporting layer must not assume today's retailer, or today's retailer's specific API
limitations, are permanent. §4a is now revised to separate two genuinely different things:

- **Retailer-agnostic, keep this shape**: per-interval cost/earnings detail in a real table,
  a live daily accumulator that resets at real local midnight. Neither of these depends on
  which retailer is behind the numbers.
- **NOT retailer-agnostic, must never be core solver design**: LocalVolts' own next-morning
  "Confirmed" settlement reconciliation is a patch for THAT retailer's own specific API
  limitation (its real settlement data doesn't arrive until the next morning). A different
  retailer might settle in real time, or on a different schedule entirely. The correct shape
  is a genuinely retailer-agnostic price/settlement interface — "give me the price data you
  have right now, and how confident/settled it is" — with any retailer-specific settlement-
  lag quirks (LocalVolts' current lag included) living entirely inside that retailer's own
  adapter, never inside the solver's own reporting logic.

The full architecture sketch (published as a Claude artifact, not committed to this repo)
has the complete, current writeup — check with the user for the current link if picking this
thread back up. **Read §4a and §5's own scope note directly before building any reporting
code for the solver** — don't rebuild the LV-specific version from memory of this summary.

---

## ⚠️ PRIME DIRECTIVE — ZERO HAEO

> **NEVER REFERENCE HAEO IN NIMBUS. NOT A SENSOR. NOT AN ENTITY. NOT A FEATURE. NEVER.**
>
> Nimbus is being built to eventually become the household's own replacement for HAEO —
> not a companion tool that reads HAEO's sensors, not a fallback, not a comparison. A full
> independent replacement.
>
> **Why:** HAEO has been a genuine, repeated source of instability for this household
> (Infeasible/unavailable crashes, LP wash-trade degeneracy, dead forecast-source
> references, intermittent failures needing a second restart to clear — see the sibling
> `116KAT-HA-AI` repo's own CLAUDE.md for the extensive documented history). The whole
> point of Nimbus is to not be dependent on any of that.
>
> **Rules:**
> - Never wire in an entity that is HAEO's own plan/forecast output — identifiable by
>   carrying a `forecast` attribute that mirrors HAEO's optimizer plan (e.g.
>   `sensor.battery_active_power`, `sensor.grid_active_power`, `sensor.solar_power`,
>   `number.grid_export_price`, `number.grid_import_price`, `sensor.battery_discharge_power`,
>   `sensor.battery_charge_power`) — not as a training feature, not as a display source,
>   not "just for comparison."
> - If Nimbus (or a Nimbus-adjacent dashboard) needs visibility into Battery/Solar/Grid,
>   use REAL MEASURED entities instead — genuinely independent of whether HAEO is
>   installed, running, or healthy at all.
> - If a genuine forward-looking forecast of Battery/Solar/Grid is wanted, the only
>   honest paths are: (a) a real non-HAEO forecaster for that specific signal (e.g.
>   Solcast/Open-Meteo for solar), or (b) Nimbus's own ML pipeline learning to forecast
>   it from real recorder history, the same way it already forecasts loads — never
>   borrowing HAEO's own LP-derived plan.
> - This was violated once already (2026-08-15, a "Power Balance" dashboard chart built
>   against `sensor.grid_active_power`/`battery_active_power`/`solar_power` — all three
>   HAEO plan sensors) — caught, PR closed unmerged. Don't repeat it.

---

## What Nimbus is

A Home Assistant custom_component (`custom_components/nimbus_load`) with two subentry
types: **loads** (HWS, pool, EV charger, AC zones, etc.) and, as of v0.11.0 (2026-08-15),
**power signals** (Battery/Grid/Solar forecast as genuine targets in their own right, not
just load-model input features). Both forecast from real recorder history — pure numpy, no
scikit-learn (no C compiler / no wheel available inside HA's own container). Two model
types (k-NN, GBRT), validated against each other and a seasonal-naive baseline on every
retrain, with genuine model-derived confidence bands where available. See
`custom_components/nimbus_load/ml/model.py`'s own module docstring for the full technical
detail — it's kept current there, not duplicated here.

That's stage 1 of a longer destination, not the finished product.

## Recursive-forecast bug chain (v0.13.0 → v0.20.0, 2026-08-15) — read before touching predict()

A single, very productive debugging day found and fixed **seven separate, real, confirmed-
live bugs**, all in the same area of code (`predict()`'s recursive multi-step forecasting
and its confidence-band computation). Documented here in detail because they're subtle,
interact with each other, and the next person touching this code needs the full picture,
not just "there was a bug, it's fixed."

**1. Clamp bug (v0.13.0)** — `predict()`'s own `pred = max(0.0, pred)` (correct for a
load, which can never draw negative power) was silently zeroing every negative Battery
prediction (i.e. every "it's charging" prediction). Fixed with a new `allow_negative`
parameter, `True` only for power-signal subentries.

**2. Self-reference / cross-reference stale-feature bug (v0.14.0 → v0.15.0)** — a power
signal's own `battery_kw`/`grid_kw`/`solar_kw` input features are held FLAT at whatever
the real sensor read at the moment the forecast cycle ran (no forward-looking source
exists for a real measured value, unlike temperature). For a power-signal subentry, this
stale flat value dominates/anchors the recursive forecast toward "whatever was true right
now" regardless of which signal it's stale for. Fixed by excluding all three features
entirely for ANY power-signal subentry (`coordinator.py`'s `_battery_sensor`/
`_grid_sensor`/`_solar_sensor` properties return `None` when
`subentry.subentry_type == SUBENTRY_TYPE_SIGNAL`). Loads are unaffected — their own
"what's the system doing right now" hint from these three features is a real, working use
case, confirmed via backtest.

**3. Exposure bias in the recursive lag chain (v0.16.0) — the big one.** `predict()` only
has REAL ground-truth lag values (`lag_short`/`lag_long`) for the first ~hour of any
forecast (`LAG_LONG_STEPS` grid steps); every step beyond that feeds its own prior
prediction back in as the next lag input. Since lag features are heavily weighted
(confirmed by real backtesting), a chain that starts from an atypical/transitional moment
— which "now" very often is — never reverts to the true, clean hour-of-day pattern.
Confirmed live: forecasting Battery power from a real "charging just stopped" moment,
trained on real 45-day history, converged to ~5.9-6.1kW evening peaks, repeated near-
identically day after day, against a real 45-day evening median of ~13kW every single
night (tight IQR, ~12.4-14.0). This is classic exposure bias / error accumulation in
autoregressive multi-step forecasting — invisible to single-step validation, which is
always fed real ground-truth lag.

Fix: `TrainedModel` gained a `seasonal_lookup: dict[(weekday, hour), float]` table, built
from the FULL resampled training grid with shrinkage toward the overall per-hour average
(`SHRINKAGE_K = 5` — a 45-day window only gives ~6-7 real samples per individual weekday
bucket, and one anomalous day can badly skew a thin bucket; shrinkage pulls a low-sample
bucket toward the more robust all-weekday hourly mean). `predict()`'s `lag_at()` uses this
table (power-signal callers only) once a step's lag lookback crosses past the real data
horizon, instead of the self-generated buffer.

**Two real implementation mistakes made and caught in the SAME debugging session, worth
remembering:**
- First version keyed `seasonal_lookup` by `(weekday, hour, minute)` — silently matched
  nothing at all. Both the training grid (built from whatever arbitrary wall-clock instant
  the daily retrain job happened to start at) and the predict-time grid
  (`dt_util.utcnow()` at whatever instant the coordinator's own 2-min tick lands) step in
  exact 15-min increments from two DIFFERENT, essentially-random starting offsets — a
  target's `minute` is almost never one of the training grid's own values. Caught by
  tracing the recursive loop step by step and finding every single lookup falling through
  to the buffer despite the fix compiling and running without error. `(weekday, hour)`
  granularity sidesteps the alignment problem entirely.
- Even after the seasonal fix, one weekday (Sunday) still showed a real residual — traced
  to the model's own `dow_sin`/`dow_cos` calendar features having directly learned a real
  (if noisy) "Sunday looks different" split from only ~6-7 real Sunday samples, which
  lag-shrinkage alone can't override (shrinkage only touches the LAG input, not the
  model's own learned dependence on the calendar features themselves). Understood as an
  inherent small-sample limitation, not a bug — expected to improve naturally as more
  training history accumulates.

**4. Damping blurred a genuine step transition into a fake gradual ramp (v0.17.0).**
`DAMPING_ALPHA`'s exponential smoothing, applied uniformly across the whole 96h sequence,
was designed to smooth noisy step-to-step model jitter when consecutive raw predictions
are genuinely correlated via real recent momentum. That reasoning stops applying once a
step's lag inputs come from `seasonal_lookup` (already a pre-averaged historical value,
not noisy momentum) — a genuine hour-to-hour level CHANGE there (e.g. this household's
real P2P-sell-to-self-consume automation cutting battery discharge from ~13kW to ~1-2kW in
under a minute, every night, at exactly 00:00:00) is real signal, not noise to smooth away.
Confirmed live: the midnight transition was smeared across ~45 minutes (00:00→8.83,
00:15→3.28, 00:30→2.11, 00:45→1.70) instead of settling within one grid step. Fixed by
tracking a per-step `seasonal_anchored` flag (same condition `lag_at()` itself uses for its
`lag_long` lookup) and skipping damping entirely (`alpha=1.0`) for those steps. Damping
still applies normally for the first ~hour of any forecast. Verified: the same real
midnight boundary now settles within one 15-min grid step past the boundary instead of
~45 minutes, on all 3 real boundaries checked in a 4-day test forecast.

**5. Unbounded confidence band (v0.18.0).** `calibrated_band()`'s `sqrt(1 + lead_hours)`
growth (the residual-based fallback, used when a signal's GBRT candidate doesn't win model
selection — e.g. Grid, whose GBRT badly overfit: `validation_mae['gbrt']=6220` vs k-NN's
`7.55`) has no ceiling at all. Confirmed live: Grid's upper bound had grown to +100kW by
96h out — nearly 2.5x this household's real ~44kW physical grid limit, still visibly
climbing. Genuine model-derived quantile bounds aren't immune either — a tree-based
quantile model can still extrapolate past its own training range for a feature
combination it never saw. Fixed in `coordinator.py` (not `model.py`, since it needs
`self._trained.y_train`): both bound sources are clamped to this specific signal/load's own
real observed training range (`y_train` min/max) + a 20% margin — grounded in real
per-entity data, not an arbitrary fixed kW constant, so it works identically for a 3kW
load and a 44kW battery with zero per-entity tuning.

**Also fixed the same session, smaller but real:** a plain `@dataclass`'s default pickling
restores an old persisted object's `__dict__` verbatim on unpickle, skipping `__init__`
and any `field(default_factory=...)` entirely — every `.pkl` on disk when `seasonal_lookup`
was first added would have unpickled with the attribute genuinely missing, raising a real
`AttributeError` on the very first `predict()` call after deploying. Caught before it ever
ran in production (constructed an old-style object via `object.__new__()` + manual
`__dict__`, confirmed `predict()` crashes without the fix, confirmed clean with it). Fixed
with `getattr(trained, "seasonal_lookup", {})` instead of direct attribute access — any
future new `TrainedModel` field should use the same defensive-getattr pattern in `predict()`
until every currently-deployed `.pkl` has been through at least one retrain under the new
code.

**6. Whole House load hitting the same exposure bias as Battery/Grid/Solar (v0.19.0).**
Bug #3's fix (`seasonal_lookup` anchoring) was deliberately scoped to power-signal
subentries only (`allow_negative=True` callers) — the reasoning at the time: individual
loads (a pool pump, an AC zone) genuinely benefit from real near-term momentum carry-over
in their lag features, unlike a power signal. That reasoning doesn't hold for the "Whole
House" load specifically — confirmed live, first thing the user noticed once a smoothly-
varying chart made it visible at all: a real, isolated ~1-1.5kW spike at exactly 00:05
every single simulated day (1.50→3.40→2.78, 2.82→4.27→3.14, 2.82→3.82→2.97,
2.81→3.81→2.97) — the identical exposure-bias signature as Battery's own midnight problem,
because Whole House's own real meter reading bleeds in the SAME real automation-driven
transition (the P2P-sell-to-self-consume cutover) that Battery has, even though it's
registered as a "load" subentry, not a "power signal" one — it's a system-level aggregate,
not a genuinely momentum-driven individual appliance.

Fix: decoupled the seasonal-anchor treatment from `allow_negative` into its own new
`predict()` parameter (`seasonal_anchor`) and coordinator property (`_seasonal_anchor`) —
`True` for power-signal subentries AND for the one load whose sensor matches the real
whole-house meter entity (`sensor.logger_load_power`, hardcoded rather than a new config
field — this fixes one specific, confirmed-live bug, not a general feature). `allow_negative`
itself is untouched and still gates the zero-clamp separately — Whole House still
physically can't draw negative power, so these two questions ("should this signal's lag be
seasonally anchored" vs. "can this signal's value go negative") needed to be genuinely
separate flags, not the one conflated parameter they'd been sharing since bug #1 (harmless
until now, since only power signals had ever needed either).

**7. Hard stair-step in seasonal-anchored forecasts (v0.20.0)** — found live literally
minutes after #6 shipped, the first time anyone actually looked closely at a smoothly-
varying chart post-fix. `seasonal_lookup` bucketed by `(weekday, hour)` only meant every
15-min grid point within the same hour got an IDENTICAL lag input — combined with bug #4's
damping-skip (needed to keep a genuine sharp transition from blurring), there was nothing
left to smooth the model's own flat, repeated output. Confirmed live: Whole House's
published forecast held exactly `1.399` for 2.5 hours straight (16:18 through 18:48) then
jumped instantly at the next hour boundary — a real, visible hard stair-step instead of a
continuously-varying curve.

Fix: `seasonal_lookup` now buckets by `(weekday, hour, 15-min-of-hour)` instead of
`(weekday, hour)` alone — every grid point gets its own real seasonal value. The
predict-time lookup floors the target's minute to the nearest 15-min mark (0/15/30/45)
rather than matching it exactly — same underlying technique already used to fix the
original exact-minute-match bug from #3 (two timestamps within the same 15-min window
now correctly collapse to the identical bucket regardless of their own arbitrary
sub-15-min wall-clock offset). Midnight itself stays exactly as sharp as before this
change — 23:45 and 00:00 are still fully separate buckets, nothing spans across that
boundary; this fix only removes the artificial flatness WITHIN an hour, which was never a
deliberate goal of the `(weekday, hour)` version, just an unexamined side effect of
choosing hourly granularity to sidestep the exact-minute-match bug the simplest way
possible at the time.

Verified against REAL Battery household data (which has genuine within-hour variation,
unlike a flat synthetic test): 18:04/18:19/18:34/18:49 now read `6.058/6.078/6.085/6.132`
(smoothly increasing) instead of one flat repeated value, while the real midnight
transition (re-checked via the existing synthetic test) still settles within one grid
step, completely unchanged by this fix.

**Process lesson, not a code bug:** verify claims about a fix against REAL data before
declaring success. Six separate synthetic reproduction attempts (clean baseline, realistic
noise, damping-alpha variation, an afternoon-start with real charging lag, real temperature
data, a longer 45-day training window) all FAILED to reproduce bug #3 above — the actual
repro only appeared once BOTH the real 45-day household data AND the real live starting
condition (today's actual charging-to-zero transition) were used together. Don't trust a
synthetic test that "looks representative" over pulling the real data when a live bug
report and a passing local test disagree. Bugs #6 and #7 reinforce the same lesson from a
different angle: both were found by a HUMAN actually looking closely at a live chart, not
by any automated test — the existing synthetic tests couldn't have caught either one (no
flat-repeat check existed for #7 until it was added specifically because of this).

## Roadmap — Forecasters → Topology → Solver

Stated goal (2026-08-15, verbatim): *"we are building NIMBUS to be a solver optimised
down the track... smarter than emhass haeo etc etc... we are combing through FORECASTERS
FIRST - building better forecaster, then TOPOLOGY MAP better system to see loads, then
SOLVER to manage and optimise batteries solar and loads."* Three deliberate stages, in
this order, each one a real prerequisite for the next:

**1. Forecasters (current stage).** Get individual-signal prediction genuinely right
before trying to optimize anything. This started as load-only (HWS/pool/EV/AC/etc.), then
extended (2026-08-15) to give the load models real system-context visibility — battery/
grid/solar power as additional input features — because a load's real behaviour is
confounded by what else is happening on the switchboard at the same moment (a load looks
different at "10am, 22C" depending on whether the battery happens to be mid-charge right
then). Same day: Battery/Solar/Grid themselves became genuine forecast targets in their
own right (the new `power_signal` subentry type, `SUBENTRY_TYPE_SIGNAL`), the same
k-NN/GBRT/validation machinery already proven for loads — real measured history in, real
validated forecast out, no HAEO involved at any point. See the "Recursive-forecast bug
chain" section above for the substantial hardening this went through the same day it
shipped — real, but not yet exhaustively proven over many nights; treat as "genuinely
fixed and verified against real data" not "battle-tested over weeks."

**Known, honest, currently-open limitations of stage 1 (2026-08-15), not yet solved:**
- **Genuinely bimodal/rapidly-oscillating signals** (confirmed live: this household's own
  daytime battery charging alternates between ~-40kW and near-idle every few minutes, not
  a smooth plateau) can only ever be represented by a regression point-estimate as
  something close to the time-weighted average of the two states — never either extreme.
  The confidence bounds (`lower`/`upper`) carry the honest information here (correctly
  reaching close to the real extremes), the point value structurally can't. A real fix
  would mean either a duty-cycle/probability-of-state model instead of point regression,
  or richer state features that explain WHY the switching happens (e.g. site import
  headroom), not just when.
- **Grid's own GBRT candidate badly overfits** on this household's real data
  (`validation_mae['gbrt']=6220` vs k-NN's `7.55` on one real check) — k-NN wins model
  selection for Grid, meaning Grid relies on the residual-based confidence band (now
  clamped, see bug #5 above) rather than a genuine model-derived quantile band.
  **Root-caused (2026-08-17), by reading `_knn_predict_batch()` and `GBRT.predict()`
  side by side, not guessed:** `_knn_predict_batch()`'s own prediction is a WEIGHTED
  AVERAGE of real, observed `y_train` values (`np.sum(weights * y_train[nearest_idx]) /
  np.sum(weights)`) -- a convex combination, structurally bounded between
  `min(y_train)` and `max(y_train)` no matter how far out-of-distribution a query point
  is. `GBRT.predict()` is `init_value + Σ(learning_rate * tree_output)` for every tree
  in the ensemble -- an UNBOUNDED additive sum, no clipping anywhere. Ruled out the
  "same known noisy-Modbus-sensor" theory this session's own bug #5-adjacent
  investigation raised as the likely culprit: pulled 3 real days
  (`sensor.logger_meter_total_active_power`, 12248 real points) and found a real, wide,
  legitimate range (-24.4kW to +50.4kW, mean 3.05kW) with ZERO isolated glitch-like
  spikes (the specific rapid-swing signature already confirmed and fixed for
  `sensor.logger_load_power`, a DIFFERENT sensor on the same Logger, in the sibling
  116KAT-HA-AI repo's own 2026-08-16 session) -- Grid's real data is clean, this is a
  genuine structural property of additive tree ensembles under recursive multi-step
  forecasting, not sensor noise. Grid is exactly the kind of signal (large, discrete,
  automation-driven swings between real extremes -- P2P selling vs self-consume vs
  charging) where recursive lag-feature drift can push a later forecast step's own
  feature vector into a combination no training tree leaf ever saw; k-NN's convex-
  combination guarantee makes it structurally immune to blowing up from this, GBRT's
  additive-sum design is not. **No code fix needed or applied**: this is precisely the
  failure mode the chronological validation + automatic model-selection machinery
  (2026-08-15) already exists to catch, and it already does -- k-NN correctly wins for
  Grid, and the confidence band's own clamp (bug #5) independently bounds the
  fallback's own upper/lower reach regardless. Worth remembering as a general principle
  for any future signal: additive-ensemble models (GBRT) are a real structural risk for
  recursive multi-step forecasting on volatile, automation-driven signals in a way
  average-based models (k-NN) are not -- the existing validation gate is the correct,
  sufficient mitigation, not a hyperparameter tweak.
- **Hub-level shared config (Battery/Grid/Solar sensor selectors, on the "Nimbus settings"
  options form) can be silently cleared by a stale form resubmission** — confirmed live
  2026-08-15: these 3 fields were found unset despite having been correctly selected
  earlier the same day. `flows/hub_options.py`'s own schema code is correct (reads
  `self.config_entry.options` and pre-fills exactly what's stored) — the real risk is
  behavioral: `async_step_init`'s `async_create_entry(title="", data=user_input)`
  overwrites the ENTIRE options dict with whatever the currently-open dialog instance
  shows, every time it's submitted. If that dialog was opened/cached before a field was
  ever set (or before a browser tab refreshed to pick up a later value), submitting it
  again silently wipes that field even though it was correctly set moments earlier in a
  different dialog instance. No code fix applied for this — worth considering a merge-
  with-existing-options approach instead of full overwrite if this recurs.

**2. Topology map.** A real visual representation of how the household's power system is
actually wired — switchboard, inverters, battery towers, PV strings, loads, and how they
connect — so both a human and (eventually) the solver can see the real system shape, not
just a flat list of sensors. `custom_components/nimbus_load` itself won't contain this
(it's a dashboard/visualization concern, not a forecasting one) — see the sibling
`116KAT-HA-AI` repo's own topology-card work for the current state of this piece.

**3. Solver.** The actual optimization/decision engine — deciding how to manage battery
charge/discharge, solar (curtail or not), and load scheduling, i.e. HAEO's and EMHASS's
own actual job today, done independently and (the whole point) more reliably. Will need
its own real configuration surface eventually — efficiencies, cost policies, salvage
value, the same category of settings HAEO exposes today, but Nimbus's own, not borrowed.
Not started, not scoped in detail yet — noted here so the eventual shape of `const.py`/
the config flows isn't a surprise when this stage begins.

**Why this order, not solver-first:** a solver making decisions on top of forecasts it
can't trust is worse than no solver at all — this is explicitly why forecasting accuracy
(seasonal-naive baseline, MASE, real validated model selection, genuine confidence bands)
got this much rigor before any optimization logic was even discussed.

## Deploy

Deployed on the household's own NUC (see the sibling `116KAT-HA-AI` repo) as a direct
git clone at `/opt/homeassistant/config/nimbus_repo`, symlinked into
`custom_components/nimbus_load`. Deploy:
```
cd /opt/homeassistant/config/nimbus_repo && git fetch origin && git pull origin main
docker restart opt_homeassistant_1
```
A Python custom_component change always needs a full restart — a config reload cannot
reload changed Python modules, only YAML/config.

**Standing directive, 2026-09-01, cross-referenced from `116KAT-HA-AI`'s own CLAUDE.md
(that file is authoritative — this is a pointer, not a duplicate)**: NUC1/NUC2's own OS-level
`apt-daily-upgrade.timer` is now disabled on both boxes (a real failover was traced to its
own automatic-upgrade restart cascade) — package updates on the NUC1/NUC2 host itself are
fully manual now, and a standing weekly check-in (`apt list --upgradable` on both boxes,
walked through together, never auto-applied) replaces it. This is about the HOST's own OS
packages, not Nimbus's own release process below — but since deploying Nimbus to NUC1 means
touching the same host, any Claude session preparing a NUC1 deploy should be aware this
check-in cadence exists. See `116KAT-HA-AI`'s own CLAUDE.md, "PRIME DIRECTIVE — WEEKLY MANUAL
UPDATE CHECK-IN," for the full rule and the dated log of when it was last done.

**devhub (and any other HACS install) needs a cut release, not just a merged PR.**
Confirmed live, 2026-08-27: merged 4 real fixes to `main` (#210/#212/#213/#214),
then found `ha_get_hacs_info` on devhub still showed `installed_version` ==
`available_version` == the last tagged release, `pending_update: false` — HACS
tracks GitHub *releases* (`vX.Y.Z` tags, published by `.github/workflows/release.yml`
on tag push), never raw commits on `main`. A merge alone is invisible to every HACS
install, including devhub, until a release is actually cut. **After merging PR(s) to
main, always follow up**: add a `## [X.Y.Z]` section to `CHANGELOG.md`, bump
`manifest.json`'s `version`, commit as its own "Release vX.Y.Z" PR, merge it,
then tag that commit `vX.Y.Z` and push the tag. Only then will HACS's update
entity on any install see the new version and offer it.

**2026-09-04, nimbus issue #357: the `nimbus_solver_app` Supervisor add-on and
its `version-lockstep` CI job are both gone.** The add-on had silently drifted
out of sync with the integration's own solver code (missing several fixes)
with no real path to staying maintained as a third copy of the same logic —
removed entirely rather than kept in sync. `manifest.json`'s `version` is now
the only version number in this repo; there is nothing left to keep in
lockstep with it.

## Git workflow

Branch + PR for every real change, same as `116KAT-HA-AI`. No direct pushes to `main`.
This was violated a few times early on (direct-to-main pushes) before being fixed —
don't repeat that either.

## Testing

**Corrected 2026-09-04 (nimbus issue #360, Mark Purcell, codebase review)** — this
section used to say "no scikit-learn/pytest infra" and recommend copying files into a
scratch package. That was true once, in 2026-08-15-era history; it stopped being true
long before this correction landed, and the stale text was actively misleading anyone
who read it. This repo has a real, full `pytest` suite (`tests/`, 900+ tests) wired via
`pyproject.toml`'s `[dev]` extra — **pytest is the only test runner this project has.**
`tests/run_all.py` (a hand-rolled dual-mode runner working around a genuine gap that no
longer exists — see its own git history if curious) and the ~125 duplicated
`if __name__ == "__main__":` bare-function collectors every test file used to carry are
both gone, removed in the same pass as this correction.

**Local commands** (identical to what CI actually runs, `.github/workflows/ci.yml`):
```
pip install -e '.[dev]'
pytest tests/ --ignore=tests/hass_integration/ -p no:homeassistant -v
pytest tests/hass_integration/ -v
```
The first run is stub-based (`tests/_ha_stubs.py` fakes `homeassistant.*`) and fast —
use this for iterating. The second uses a real
`pytest-homeassistant-custom-component` harness (genuine `hass` fixture, real event
loop) and is what actually exercises `config_flow.py`/`flows/*.py`/`sensor.py`/
`coordinator.py` against real HA internals — **this is the step that has caught real
regressions the stub-based run and this project's own now-deleted local-only checks
both missed** (the three-release #82/#83/#85 flap chain, and two same-day CI-only
failures during this same 2026-09-04 session). Never call a fix verified from the
stub-based run alone if it touches anything HA-facing — always confirm the real-HA-
harness job is green too, e.g. via `gh run watch` after pushing.

`solver/` and `ml/` are genuinely HA-import-free (pure numpy + stdlib) and are directly
importable without any stub at all via `tests/_solver_path.py`/`tests/_ml_path.py`'s own
sys.path shims — see any `test_solver_*.py`/`test_train_model_*.py` file for the pattern.

## Translations — keep `strings.json` and `translations/en.json` byte-identical

Confirmed live 2026-08-15: `config`/`options` schema sections render correctly from
`strings.json` alone (Home Assistant's documented runtime fallback for a locally-installed
custom_component with no `translations/` directory), but `config_subentries` did NOT —
both "+ Add" menu buttons rendered with no label, and the per-field data labels fell back
to the raw field name, even after `strings.json` had the correct `flow_title`/
`entry_type`/`initiate_flow`/`step.*.data` content and a full HA restart. `config_subentries`
is a newer, less mature part of HA's config-flow schema than `config`/`options` — this
project's own leading theory is that its strings.json-fallback support just isn't as
reliable yet, though this wasn't independently confirmed against HA's own source.
Fixed by adding `translations/en.json` as an exact copy of `strings.json`'s content — this
is HAEO's own file layout too (`custom_components/haeo/translations/en.json`), used to
directly source every string in this file's `config_subentries` section.

**There is no build step generating one from the other in this repo** — both must be
edited together, kept byte-identical, every time either one changes. If they ever drift,
`strings.json` should be treated as the source of truth (it's what a real HA translation
pipeline would compile from), and `translations/en.json` regenerated to match it.
