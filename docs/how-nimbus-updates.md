# How often Nimbus updates

Nimbus runs entirely inside Home Assistant. It reads your sensors, works out
forecasts and a plan, and publishes them as sensors. Nothing is polled from a
cloud service by Nimbus itself; prices and forecasts come from the integrations
you already have (your retailer, Solcast, Open-Meteo, AEMO and so on), on their
own schedules.

This page lists what refreshes, how often, and what triggers it.

## The plan

The Solver works out the battery plan and publishes it on
`sensor.nimbus_solver_battery_forecast` and the sensors around it.

| Trigger | When |
|---|---|
| Regular solve | Every 5 minutes, 30 seconds after each 5-minute mark (:00:30, :05:30, …), just after the market's 5-minute price settles |
| A price sensor changes | Only if the **Solve on Price Change** switch (`switch.nimbus_solve_on_price_change`) is on. Every configured price sensor is watched; changes within the **Solve on Price Change Debounce** (5 s by default) become one solve |
| A P2P export block starts | A re-plan within seconds of the block's start, so the battery doesn't wait for the next 5-minute solve |
| Home Assistant starts or Nimbus reloads | A first solve as soon as the inputs are available |

Two solves never run at once; a solve that would overlap another waits or is
skipped.

**How far ahead, and in what steps:** the plan covers **96 hours**. The first
5 minutes are planned minute by minute, then 5-minute steps for up to an hour,
then 30-minute steps to the end, which matches how far ahead prices are
published.

**`solved_at`** on `sensor.nimbus_solver_lp_status` shows when the plan
currently shown was worked out.

## Load forecasts

Each load you add (the whole house, a circuit, a controllable load) has its
own forecaster.

| What | When |
|---|---|
| The forecast is refreshed from the trained model | Every **2 minutes** (inference only, under a second) |
| The model is retrained from your history | Once a day, at the **Retrain hour** (03:00 local by default). Loads are spread across that hour so they don't all retrain at once |

The forecast has a point every 15 minutes and reaches at least 97 hours ahead,
so it always covers the plan.

## Daily reports

The Quality, Backtest, Counterfactual and Flex reports score a whole finished
day. Each day is scored once, on a solve after the day has ended; between days
the sensors keep the last score.

## Setup health and Repairs

Setup health is rechecked every **15 minutes**. A problem that needs you (a
setup that failed, a sensor whose unit no longer matches, a dashboard with
Nimbus cards but no Tabs card) appears under **Settings → Repairs**, and clears
itself once fixed.

## When something looks stale

- `sensor.nimbus_solver_lp_status` older than about 10 minutes: the Solver
  isn't running. See [Troubleshooting](troubleshooting.md).
- A load forecast that stops moving: check its source sensor still reports.
- A price that looks old: the price comes from your provider's integration;
  check that integration's own sensor first.
