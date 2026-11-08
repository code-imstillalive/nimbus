# Known limitations

What Nimbus does not do, or does only partly, as of this release. Each item says
what to do about it where there is something to do.

## It does not drive your battery by itself

Nimbus publishes a plan. To make your battery follow it, an automation of
yours reads the plan and commands your inverter through the inverter's own
integration. See [Going live](going-live.md).

Nimbus's own dispatcher can run in **dry run** only: it records in
`sensor.nimbus_solver_dispatch_dry_run` what it would send, and has no write
path to your battery.

Controllable loads are different: Nimbus can switch a `switch`,
`water_heater` or `climate` entity you assign as its **Device to command**. See
[Controllable loads](controllable-loads.md).

## It needs history before it can forecast

Each load's forecaster trains on your Recorder history, and refuses to train
on fewer than 500 points, about five days at 15-minute steps. A new sensor,
or one with little history, has no forecast until then.

Home Assistant's Recorder keeps 10 days by default (`purge_keep_days`). Asking
Nimbus for 30 days of history cannot bring back what was purged; raise
`purge_keep_days` if you want a longer training window.

## One Nimbus hub per Home Assistant

Nimbus is set up once per Home Assistant. Loads, power signals, batteries and
EVs are added under that one hub.

## Built-in price providers are Australian

The providers Nimbus recognises and reads in full are Australian:
LocalVolts, Amber and AEMO's National Electricity Market forecasts. Anywhere
else, use [any other price sensor](providers/generic.md): a sensor whose state
is a price in $/kWh. If it carries no forecast Nimbus can read, the current
price is held across the whole plan, so the plan cannot shift energy toward
cheaper hours.

## Models retrain once a day

Load models retrain once a day at the **Retrain hour** (03:00 local by
default). A real change in habits shows in the forecast from the next retrain,
not straight away. Forecasts themselves refresh every 2 minutes from the
current model. See [How often Nimbus updates](how-nimbus-updates.md).

## The Regret cards solve when they open

Each Regret card works out, in hindsight, what the best plan for that day
would have been. That is a full solve, about 5 seconds per day on the test box
and longer on slower hardware. Results are remembered for 10 minutes, so a
reopen is instant, but the first open of the tab can take a while.

## Home Assistant 2026.8 or newer, 64-bit

Nimbus needs Home Assistant 2026.8.0 or newer on a supported 64-bit
installation, and the `numpy` and `highspy` Python packages, which Home
Assistant installs with it. If `highspy` cannot install on your hardware,
setup can fail, forecasting included; see
[What you need before you start](setup-guide.md#2-what-you-need-before-you-start).
