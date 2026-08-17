# CLAUDE.md — Nimbus

Instructions for any Claude instance working on this repo. Read this before touching any file.

---

## ⚠️ CURRENT STATE (2026-08-17 morning) — read this first

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
  clamped, see bug #5 above) rather than a genuine model-derived quantile band. Not
  root-caused; worth investigating if Grid's forecast quality matters more later.
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

## Git workflow

Branch + PR for every real change, same as `116KAT-HA-AI`. No direct pushes to `main`.
This was violated a few times early on (direct-to-main pushes) before being fixed —
don't repeat that either.

## Testing

No scikit-learn/pytest infra inside the HA container this trains in, so verification
happens locally: copy the relevant `custom_components/nimbus_load/{const.py,ml/*.py}`
files (pure numpy + stdlib, zero HA dependencies) into a scratch test package and run
real synthetic data through `train_model()`/`predict()` before shipping — not just a
syntax check. See recent PR descriptions for the pattern.

The config-flow/entity/sensor files (`config_flow.py`, `flows/*.py`, `sensor.py`,
`coordinator.py`) import `homeassistant.*` directly and can't be instantiated in that
same local test environment (no `homeassistant` package installed there) — `py_compile`
syntax checks + careful mirroring of an already-proven pattern is the most that can be
verified before a real deploy for these files.

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
