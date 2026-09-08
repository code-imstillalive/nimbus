# CLAUDE.md — Nimbus

Instructions for any Claude instance working on this repo. Read this before touching any file.

---

## ⚠️ STANDING DIRECTIVE — UPDATE THIS FILE AT LEAST ONCE DAILY

> **If real work happens in this repo on a given day, append a dated entry to `docs/worklog/YYYY-MM-DD.md` before that day is done — not batched up across several days.** Same standing rule applies to `116KAT-HA-AI`'s own `CLAUDE.md` for any day real work happens there.
>
> Confirmed directly, 2026-08-31: this section was still dated "2026-08-27 night" while real work (issues #244-#252 and beyond, releases up to at least v0.94.19) had already happened across several more days — a future session reading this file cold would trust a stale version number and a stale open-issue list. Undocumented/un-updated work is invisible to the next session; don't let this drift.
>
> **2026-09-06 (nimbus issue #364, Mark Purcell, codebase review): this file's own dated "CURRENT STATE" journal (2026-08-15 through 2026-09-05, ~750 lines, 85 KB) moved verbatim to `docs/worklog/`, one file per date — see "Recent history" below.** The move was purely mechanical: content relocated unedited into per-date files, verified byte-identical against the text removed from this file before committing (concatenating the moved sections in original order and diffing against the removed block produced an empty diff). No editorial pass has happened yet — that's a deliberately separate, later step per Mark's own two-phase plan on #364. **Going forward: don't append a new dated section to THIS file — append to today's `docs/worklog/YYYY-MM-DD.md` instead**, creating it if it doesn't exist yet, and update the "Recent history" list below to include it.

---

## Recent history

Dated work-in-progress notes live in `docs/worklog/`, one file per date — this is where
the "CURRENT STATE" journal that used to live directly in this file now lives. Each
file is not re-summarized here; read it directly for the full detail. Most recent 5:

- [2026-09-09](docs/worklog/2026-09-09.md) — #582: a deferrable Controllable Load with
  a same-day window (the #534 heat pump's real 6am-4pm window) was silently dropped
  for its ENTIRE active window once `now` fell inside it, the opposite of intended
  behavior — `_resolve_hour_to_period_index()` rolled the earliest hour to tomorrow
  while the deadline stayed today, tripping the ordering guard written for the
  overnight case. Fixed per Mark's own diagnosis: detect the same-day-in-progress
  case directly and resolve earliest_period to "right now" instead of tomorrow.
  Known, honestly-scoped remaining gap: an overnight window already in progress when
  `now` falls inside it hits a related but different path, not fixed this pass.
  Released v0.94.187.
- [2026-09-08](docs/worklog/2026-09-08.md) — Continuation of the 09-07 marathon.
  Household re-engaged after the quiet overnight monitoring stretch; confirmed #519's
  topology-card fix genuinely ships (Mark's install just needed a client-side cache
  clear, not a code fix). Mark's own Claude Code posted a full day-ahead dispatch
  report (#531) with three real findings, all fixed same day: **#535** (most severe) —
  `_sample_load_run_state()` (#479) read a Watts power sensor as already being kW,
  making `currently_on` permanently true and `delivered_today_kwh` ~1000× too large
  for any Controllable Load with a plug/CT power sensor; fixed with the same
  `unit_of_measurement` check `_kw_scale_factor()` already applies elsewhere. **#532**
  — exposed `achieved_energy_in_kwh`/`achieved_energy_out_kwh` on the quality report
  (Mark's real case: a combined battery-power sensor summing the home pack + a shared
  EV DC charger fed a 100 kWh single-battery model, moving >100 kWh through it in one
  day) — deliberately NOT an automatic cause-classifier, just the honest numbers.
  **#533** — `epr_reliable` and the three `soc_discrepancy_*` fields flattened onto the
  Quality sub-device (previously invisible parent-only attributes), plus a log-once-
  per-scored-day warning. 32 new tests, zero regressions across the full pre-existing
  quality-report suite. Released as v0.94.166 — real, immediately impactful fixes, not
  foundation-only. Still deliberately NOT built: the #465-pattern per-load sensor
  platform every one of #479/#480/#484/#486's own gaps still points at — same
  verifiability reasoning as the day before (no real `hass_integration` harness
  available locally). **Continued same day**: #534 item 1 — a Controllable Load's Done
  sensor now accepts a `water_heater`/`climate` entity directly (reads
  `current_temperature` attribute, not the mode-string state; defaults an unset Done
  condition to the entity's own live setpoint), v0.94.167, no wizard change needed.
  Then **#538** (Mark, found the day after v0.94.166 shipped): `soc_discrepancy_
  reliable`/`epr_reliable` used to test ONLY the [0, 100] range — his own real case
  (raising configured capacity kept a 40.7pt/10.85pt real disagreement in-range) still
  read "reliable". Added a second, independent agreement test via two new dashboard
  numbers (`number.nimbus_solver_soc_discrepancy_max_threshold_pct`=15/`_mean_
  threshold_pct`=8, never hardcoded), plus `soc_discrepancy_reason` (`"out_of_range"`
  vs `"disagreement"`) on the report/WARNING/flattened sensor. **Auditing that same
  bug class while wiring the two new number entities found two more real, live,
  pre-existing instances**: `solver_fixed_daily_charge`/`solver_post_window_self_
  consume_hours` were both missing from `sensor.py`'s own live-entity resolution list
  — any household adjusting either from the dashboard had **zero effect on the actual
  solve**, silently, forever; fixed alongside #538, plus a new generic regression test
  guarding every `number.py` field against this pairing going forward. CI caught a
  real thing local verification missed (a pre-existing test asserting the exact
  behaviour #538 changes — this repo's own `pytest-asyncio` plugin conflict means
  this file's pytest-style tests aren't exercised locally at all); fixed, re-pushed,
  merged. Released as v0.94.168, deployed to devhub, verified live — the new
  reason-aware WARNING fired for real on devhub's own data
  (`reason=disagreement, max discrepancy 20.4 pt, mean 9.6 pt`). **Then #542/#543**
  (Mark, found reading the real log right after verifying v0.94.168): a solar source
  configured directly at Solcast/Open-Meteo's own entities was silently dropped every
  solve (the configured-source reader only recognized the generic `forecast=[...]`
  shape, not Solcast's `detailedForecast`/Open-Meteo's `watts`) — one shared
  `_solar_entries_from_attributes()` reshape function now backs every solar reader,
  ported to the standalone/cron docs copy too (caught by the anti-drift test when
  the port was still missing); also fixed the resulting double-weighting risk when
  the same entity is both configured AND auto-included. Plus the warning itself now
  logs once per `(entity_id, reason)` with a recovery INFO log — first of this
  project's log-once dedup sets to add that. 20 new tests, released as v0.94.169,
  deployed to devhub, restart verified clean (devhub's own source didn't happen to
  hit either path this cycle, so live confirmation rests on the unit tests here).
  **Then a real regression, found within minutes on Mark's own install (#546)**:
  v0.94.169's own dedup excluded just the ONE overlapping entity from the
  auto-include Solcast pair — but Solcast's own entity only ever covers ONE day, so
  the auto-include fetch was left reading only the OTHER day, fragmenting a healthy
  2-member blend into a 3-member blend with a near-zero holdover member every day
  (same real forecasts, 252.8 kWh → 172.4 kWh next-24h plan solar). Fixed by moving
  the dedup from the entity level to the integration level — a configured source
  that's one of Solcast's/Open-Meteo's own known entities is now skipped as a
  standalone member ENTIRELY when auto-include is on, restoring v0.94.168's own
  correct two-member structure. 5 new tests, ported to the docs copy, released as
  v0.94.170, deployed to devhub, restart verified clean. **Unblocked and merged
  Mark's own #522** (#459 dispatch-card compact-format rework, real merge conflict
  against main's v0.94.164→v0.94.170 movement) — resolved, CI green, released as
  v0.94.171, deployed. **Real issue-hygiene gap caught by a direct household
  question** ("why are there 34 issues, more than when I left?"): #532/#533/#535
  were genuinely fixed and shipped in v0.94.166 hours earlier but never actually
  closed (that PR's own description didn't use GitHub's auto-close keyword) — all
  three closed with the record straight; see `feedback_nimbus_close_fixed_issues`
  memory for the standing fix (always verify/close, don't just remember "handled").
  **Later, after a real 116KAT-HA-AI production detour** (first-ever live Nimbus-driven
  battery automation on NUC1, see that repo's own CLAUDE.md/worklog for the full detail —
  nimbus itself only gained a new `sensor.nimbus_solver_battery_forecast` grid-helper
  consumer, no code change here): resumed and shipped **#551** (Mark) — all three shipped
  cards now define `getStubConfig()`, and the topology card's `setConfig()` no longer
  throws on a missing `switchboard`/`inverters` (defaults to `{}`/`[]`), fixing the
  card-picker's own bare `{ type: ... }` insert erroring immediately. Released v0.94.172.
  Then **#553** (Mark, same cluster as #550) — topology card now shows a plain-HTML empty-
  state banner (deliberately outside the SVG's own coordinate math) when no Topology
  wizard subentries exist, while still drawing discovered Loads/signals underneath;
  caught and fixed a related doc staleness in `docs/dashboards.md`. Released v0.94.173,
  deployed to devhub, restart verified clean. Both fixes traced to real findings from
  Mark's own install (a picker-added card erroring; a near-empty topology diagram with
  no guidance). Remaining #550-cluster backlog: #552 (dashboard YAML), #554
  (auto-populate topology from Energy dashboard), #473 (flaky test) — not yet started.
- [2026-09-07](docs/worklog/2026-09-07.md) — #445/#453/#451 real bug fixes; dispatch-card
  risk-aversion live-effect proof, Solve Now button, nimbus_status sensor. Chart/table
  layout saga ran through SEVEN CSS iterations (four content-aware formulas, then two
  more real bugs after the fixed 2/3:1/3 containers landed — the total row's own label
  forcing the Time column wide after a wrong SOURCE-removal detour, then a horizontal-
  scroll-restore gap) before genuinely settling. Real lesson from the SOURCE-removal
  detour specifically: a plausible correlation (remove X, problem persists) is not
  causation — the real fix came from reading what was actually forcing the width, not
  another guess. Real NUC1 incident: battery charged instead of discharging at the
  17:00 P2P boundary; root cause NOT confirmed after three theories each disproven/
  unconfirmed by live evidence; reverted to HAEO for the night, live trace planned
  before next P2P window. #477, #486, #479, #484, AND #480 (controllable-loads
  foundation + config surface + per-load run-state store + relay-chatter guard
  decision layer + early completion) all completed and merged. #480 is real
  and RELEASED (v0.94.164) — a household can configure a deferrable load's
  Done sensor today and see the Solver stop scheduling once it fires. The
  other four remain foundation-only: real, honestly-documented gap left
  open — no per-load output SENSOR yet (#465's own sub-device pattern), so
  #484's own guarded commanded_state has nowhere real to be read from yet.
  Deliberately did NOT build that sensor platform next despite it being the
  clear blocker every prior section flags — a real new HA entity-registry
  lifecycle feature is exactly the kind of change the local dev venv can't
  fully verify (`hass_integration` tests need a newer `homeassistant`
  package than this machine has); picked #480 instead for its
  fully-verifiable, #479-shaped risk profile (reads an existing external
  entity, no new entity lifecycle). Household went to bed partway through
  with explicit authorization to keep working solo — everything after that
  point in the entry happened unsupervised, including Mark Purcell's own
  Claude Code becoming concurrently active on the repo (opened/merged #516
  for #459's mobile-clipping regression after a CI-lint assist, filed and
  got #519's topology-card discoverability fix merged, #522 left as his own
  active follow-up, reviewed #486 and confirmed it solid). Version reaches
  v0.94.165, twenty-one releases (v0.94.165 itself is a same-session follow-up:
  Mark's own review caught a real log-spam gap in #480's own done_when warning,
  fixed the same night).
- [2026-09-06](docs/worklog/2026-09-06.md) — #391/regression/#400 dispatch-card layout
  (three passes, root-caused with a shared CSS variable); #389 solver crash and #390
  whole-horizon infeasibility (penalized grid_import_excess slack); EPR-consistency fix
  in compute_quality_report(); #388 battery sign-convention auto-detect. Version reaches
  v0.94.131, seven releases.
- [2026-09-05](docs/worklog/2026-09-05.md) — Long run through Mark Purcell's #336 codebase
  review (#355 through #368, #372-375); version reaches v0.94.118; a real self-correction
  on a false "independently confirmed on devhub" claim for #375.
- [2026-09-02](docs/worklog/2026-09-02.md) — `solver_p2p_settlement_history_sensor`
  confirmed working end-to-end on devhub; the Solver wizard's cross-step field-wiping
  mechanics confirmed directly by reading `flows/hub_options.py`.
Earlier history: `docs/worklog/2026-09-01.md`, `docs/worklog/2026-08-31.md`,
`docs/worklog/2026-08-27.md`, `docs/worklog/2026-08-26.md`, `docs/worklog/2026-08-17.md`.

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
