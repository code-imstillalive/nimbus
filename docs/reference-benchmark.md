# Reference-household benchmark

Nimbus issue [#273](https://github.com/code-imstillalive/nimbus/issues/273)
(Mark Purcell), item #3: *"a standardized reference-household scenario
(synthetic, not any one real install) to be a fair, comparable benchmark
across releases."* Items #1 (`compute_forecast_regret()`) and #2 (Nimbus
staying independent of HAEO) on that same issue are both already
resolved — this is #3.

## What it is

`solver/reference_benchmark.py` defines one fixed, fully synthetic
"household" (a generic 15kWh battery, 10kW inverter, a plausible
time-of-use price shape, a plausible daily solar/load curve) and runs it
through the real, production `compute_forecast_regret()`. Nothing about the scenario is randomly
sampled without a fixed seed, and nothing in it is tied to any one real
installation's own numbers.

## Where that function actually runs (read this before trusting the number)

Until 2026-09-15 this section claimed the benchmark exercised *"the exact
same function `nimbus_solver_quality_writer.py` calls against real
household data every day."* That was true of the standalone/cron
deployment when it was written, and it is not true of the way installs
run Nimbus now.

**Updated 2026-09-16 (#919): a native install now does call it**, via
`solver/nowcast_skill.py`, which `_compute_report_for_window()` drives
once per scored day.

**Updated 2026-09-26 (#937 stage 3): a native install now calls it a
second time, directly**, from `solver_writer.py`'s own
`_day_ahead_forecast_regret_attributes()`, also driven once per scored day
by `_compute_report_for_window()`. So `compute_forecast_regret()` has four
non-test callers:

| caller | horizon | comparable to this benchmark? |
|---|---|---|
| this benchmark (`reference_benchmark.py`) | day-ahead, synthetic | — |
| the standalone/cron `nimbus_solver_quality_writer.py` | day-ahead, real | yes, in horizon |
| the integration's `solver/nowcast_skill.py` (#919) | **one-step-ahead** | **no — see below** |
| the integration's `_day_ahead_forecast_regret_attributes()` (#937) | day-ahead, real | yes, in horizon |

**Which of those the caveat below applies to matters, so read the row
before comparing numbers.** It was written about the #919 nowcast fields
and it is specific to them. The #937 day-ahead fields
(`forecast_regret_j_forecast` / `_j_persistence` /
`_nimbus_value_add_dollars`) are built on the *same* whole-day-ahead
horizon this benchmark uses, so point 1 does not apply to them at all --
they are the field equivalent of this benchmark's own figures, differing
only in that they run against one real household's real day rather than
the fixed synthetic scenario. Point 2 (scenario vs. real conditions) still
applies to every field number, #919's and #937's alike.

**The #919 nowcast fields are deliberately not the same measurement, and
the difference matters before you compare the two numbers.** A deployed
install publishes `load_nowcast_skill_j_forecast` / `_j_persistence` /
`_value_add_dollars`, which differ from this benchmark's figures in two
ways by design:

1. **Horizon.** The field number is built from a *nowcast trail*
   recovered from recorder history — for each moment, what the
   forecaster said about that moment. That is **one-step-ahead** skill.
   This benchmark's scenario supplies a whole-day-ahead forecast array.
   A day-ahead forecast is a harder problem, so the field number is not
   the pessimistic or optimistic version of this one; it is a different
   question.
2. **Scope.** The field number holds solar at its real measured values
   in every scenario, because Nimbus does not forecast solar
   (Solcast/Open-Meteo do) and their accuracy is not this project's to
   report. Its delta is therefore attributable to **load** forecast
   quality alone. This benchmark varies both.

The naming is the guardrail: every field attribute carries the
`load_nowcast_skill_` prefix so nobody reads it as this document's
number. Treat the benchmark as a property of the `solver/` package that
detects regressions in that code, and the `load_nowcast_skill_*`
attributes as the evidence about live behaviour — related, not
interchangeable.

## Why synthetic, not a captured real day

A real day's own weather, price shocks, and one-off incidents (a NUC
failover, a noisy sensor) dominate the difference between any two real
days far more than a code change ever could. Comparing v0.94.20's real
Tuesday against v0.94.25's real Thursday tells you almost nothing about
whether the *code* got better. Holding every input fixed means a change
in the reported numbers can only come from a genuine change in the
Solver/regret logic itself — the property that actually makes a
before/after comparison meaningful.

## Running it

```bash
python tests/run_reference_benchmark.py
```

Prints a human-readable summary plus one JSON line (Nimbus version, the
scenario's own version, and the four headline numbers: `j_star`,
`j_forecast`, `j_persistence`, `nimbus_value_add_dollars`).

## How to use the result

This is a number to **watch and record**, not a CI pass/fail gate — see
issue [#217](https://github.com/code-imstillalive/nimbus/issues/217)'s
own conclusion that a soak-window/gating decision belongs to the project
owner, not something to impose unilaterally here.

Recommended workflow for any change that could plausibly affect the
Solver's own LP or regret/EPR math (`network.py`, `regret.py`, `epr.py`,
`forecast_regret.py`, `elements.py`):

1. Run the benchmark on `main` before your change, note the JSON line.
2. Make the change.
3. Run the benchmark again.
4. If `nimbus_value_add_dollars` (or `j_star`/`j_forecast`) moved,
   mention the before/after numbers in the PR description or the
   CHANGELOG entry — the same way a performance-sensitive change would
   quote a benchmark, not gate merge on an exact threshold.

A genuine improvement is expected to move these numbers. A regression
(the oracle scenario itself getting more expensive, or
`nimbus_value_add_dollars` shrinking or going negative) is worth
investigating before merging, but is a judgment call, not an automatic
block — `tests/test_reference_benchmark.py` only enforces the
*structural* invariants every regret/EPR result in this project must
satisfy (the oracle can never be beaten, regret is never negative),
never an exact value.

## Versioning

`REFERENCE_HOUSEHOLD_VERSION` (in `reference_benchmark.py`) versions the
*scenario itself* — its battery/grid/solar/load shape and forecast-error
mechanism — separately from Nimbus's own `manifest.json` version. If the
scenario's own inputs ever need to change, bump this constant so a
benchmark result tagged with a different scenario version is never
silently compared to an earlier, genuinely different scenario.

## What this deliberately does not cover

This benchmarks the **Solver** package only (`build_plan`, `regret`,
`epr`) via a synthetic solar/load/price scenario. It does **not**
exercise the ML Forecaster (`coordinator.py`, `ml/model.py`) — the real
k-NN/GBRT forecaster needs genuine Home Assistant recorder history to
train against, so there is no portable, zero-HA-dependency way to run
it standalone the way every other tool in `solver/` is built to run.
The `solar_forecast_kw`/`load_forecast_kw` inputs in this benchmark are
a synthetic, fixed-error *proxy* for "whatever a forecaster produced,"
not a claim about how accurate Nimbus's own Forecaster is on real data.
A synthetic Forecaster-accuracy benchmark would need its own, separately
scoped design.
