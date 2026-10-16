# 2026-10-16: two-stage stochastic dispatch inside the main solve (#1797)

All times AEST. Production reads were read-only: `devhub_tools/nimbus_prod_replay.py`
runs `solver_writer.main()` on this PC against the VIP's REST API with every
non-GET request refused before it leaves the machine. Nothing was run on NUC1
or NUC2, nothing was written to production or to devhub.

## What was built

`solver/stochastic_dispatch.py`. The full `build_plan()` model, once per solar
scenario (p10 / p50 / p90, weights 0.3 / 0.4 / 0.3), in ONE problem:

- `network._build_plan_once()` takes an `lp_solve_hook` (threaded through
  `_build_plan_thermal_fallback()` and `build_plan()`); None is the old solve.
  The stochastic hook builds the p10 and p90 models through the same function
  with the same per-call flags, captures them unsolved and absorbs all three
  into one `LPProblem` (`LPProblem.absorb()`, prefix `p10::` / `p90::`). No
  model code is copied, so every feature is in every scenario.
- Objective: p50's costs at 1, the others at w_s / w_p50, so p50's rows keep
  their units and `total_cost` is p50's. Expected cost = sum w_s * cost_s.
- Non-anticipativity on every decision (battery, grid, solar use, load power,
  binaries) for the periods ending inside 30 minutes. Solar is p50 in every
  scenario there too, so stage 1 is one decision on one input.
- `solver_plan.run_stochastic_dispatch()`: shadow by default (at most every
  5 minutes, hourly after a solve over 20 s), drive behind
  `switch.nimbus_solver_stochastic_dispatch` (every cycle, deterministic
  fallback on failure / not optimal / over 30 s, with `fallback_reason`).
- `stochastic` and `stochastic_period0_battery_kw` on
  `sensor.nimbus_solver_battery_forecast`.

Prior art: EMHASS 0.18 optimises one forecast path; its chance-constrained /
finite-scenario controllers are a documented research prototype
(`docs/study_cases/chance_constrained_mpc.md`), not a feature. HAEO: none.

## Measured

Production's live inputs, two cycles (14:37 and 14:55), 198-204 periods:

| | deterministic | stochastic |
|---|---:|---:|
| 14:37, no previous plan | 0.50 s (1.02 s with offer curve) | 3.16 s |
| 14:55 inside `main()`, no previous plan | 1.06 s | 3.00 s |
| 14:55 inside `main()`, previous plan (proximal terms) | 1.55 s | 4.84 s |
| 14:37 + two semi-continuous deferrable loads (MIP) | 3.09 s | 20.6 s |

Stage 1 against a p50 deterministic plan (risk aversion 0), five starting SoCs
on each capture: equal in 8 of 10; 1.04 kW and 2.99 kW apart in the other two
(14:55 at 15% and 98%), worth $0.044 and $0.006 of expected cost (EEV - RP).
Against the LIVE plan, stage 1 differs by up to ~0.8 kW and the gap is up to
$0.51, but pinning a p50 plan's stage 1 instead closes it to zero: that gap is
the live plan's solar risk aversion (0.25 x (p50 - lower)), not the hedge.

Hand-built hedge (test `test_hedge_differs_from_the_p50_plan`): cheap now,
uncertain midday solar, an evening load. p50 buys 3 kWh, the hedge buys 8;
value of the stochastic solution $0.137.

## Found on the way, not fixed here

**Production's solar "band" is not a p10/p90.** At 14:37 `lower_kw` was 0 in
every period and `upper_kw` reached 6-17 kW at midnight on every night. It is
the min/max envelope across sources plus half their spread
(`solver_inputs/solar.py`). The stochastic module excludes night periods
(p50 below 0.05 kW) rather than hedge against a 3 am sunrise; in daylight it
uses the envelope as given, so the shadow numbers on that install are only as
good as that band.

## Not done

- Live money benefit: needs days of the shadow summary scored against the
  realised day (the quality report / counterfactual machinery).
- devhub validation: not run this session (no devhub tools in it). The
  golden-master scenarios carry no solar band, so they exercise only the
  no-op path; the new tests and the production replay exercise the rest.
- The standalone/cron copy has no stochastic orchestration (registered as
  open drift).

## Afternoon: devhub found an ERROR (#1805), fixed

devhub's validation (head 4aff7830) logged `Nimbus #773 diag: lex/calibration
phase 'phase2_secondary' failed to reach optimal (status='Time limit reached',
n_vars=37392, n_binary=4128 ...)` at ERROR: the joint shadow model ran the
calibrated objective's phase 2 on devhub's test controllable loads. Reproduced
locally with a regression test, which also showed the worse half: the failed
phase started lp.py's module-wide 300 s calibration cooldown, so a shadow
failure would have skipped the LIVE solve's calibration.

Fix: the joint solve never runs lex/calibrated phases. It is one blended solve
at the deterministic solve's calibrated weight (`joint_options()`); primary-only
if no weight is known. `LPProblem.quiet_diagnostics` (thread-local) keeps
lp.py's #773/#1179 diagnostics at DEBUG, dumps no model and starts no cooldown
for that problem; `run_stochastic_dispatch()` logs the failure once per reason
as a WARNING with `fallback_reason`, in shadow and drive.

Blended vs calibrated joint, production inputs, two cycles x five SoCs: period 0
identical 10/10, stage 1 identical 9/10 (one tie-break 4.4 kW apart at 98%),
expected cost $0.0006-0.0033 higher. Times: LP 2.6-4.6 s (deterministic
0.5-0.7 s without offer curve); MIP with two deferrable loads 11.2 s and
14.2 s (deterministic 3.0 s and 5.2 s), down from 20.6 s calibrated.
