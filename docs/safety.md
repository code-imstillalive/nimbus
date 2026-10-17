# Safety features

Nimbus plans every five minutes against forecasts that are sometimes wrong and
inputs that sometimes stop. These are the guards between a bad input and your
battery or devices. Each one links to the issue where it was found and fixed.

## Prices

- **A stale price source is not planned on.** If your price source's refresh
  stamp is more than an hour old, the plan prices from AEMO's forecast plus
  your learned markup until fresh data returns
  ([#1793](https://github.com/code-imstillalive/nimbus/issues/1793)). The
  Solver tab's **Price safety** row shows the source's freshness.
- **False AEMO price spikes are not planned on.** A spike in AEMO's own
  forecast, from a run that violated its own network constraints and that
  dispatch for the same interval disagrees with, is held back
  ([#1778](https://github.com/code-imstillalive/nimbus/issues/1778)).
- **A frozen price sensor is refused.** LocalVolts "Rate All Var" sensors are
  fixed at forecast build and do not follow a spike. Nimbus refuses them in the
  price fields and raises a Repair if one is set
  ([#1661](https://github.com/code-imstillalive/nimbus/issues/1661)).
- **The daily score ignores stale prices.** Periods priced from a stale source
  are re-priced from AEMO before the day is scored
  ([#1825](https://github.com/code-imstillalive/nimbus/issues/1825)).

## Solving

- **A failed solve never publishes an empty plan.** The last good plan stays
  in place, and the failure is reported.
- **Solves never overlap,** and each has a time budget. A cycle that is still
  running when the next one is due is skipped, not stacked.
- **Targets are soft where they must be.** An EV departure target that cannot
  be reached, for example, no longer makes the whole plan infeasible. The plan
  gets as close as it can and reports the shortfall.
- **Stochastic dispatch drives only when you switch it on.** It is off by
  default (**Drive the battery** on the Solver tab). When on, it falls back to
  the main plan whenever it cannot be trusted, and says why.

## Devices

- **Nimbus only commands what you give it.** A controllable load commands a
  device only when **Device to command** is set; blank means plan only. The
  battery follows the plan only through an automation you write.
- **The dispatch guard sits between the plan and a device.** It enforces each
  load's minimum hold time and maximum activations per day, and does not
  switch back and forth (relay chatter).
- **Dry run:** turn on `switch.nimbus_solver_dispatch_dry_run` to record what
  the plan would command each period, in
  `sensor.nimbus_solver_dispatch_dry_run`, without commanding anything. Watch a
  few days of it before letting an automation act on the plan. See
  [Observe without enabling control](setup-guide.md#observe-without-enabling-control).

## Setup

Mistakes in setup appear under **Settings → Repairs** and clear themselves once
fixed:

- a forecast that has not trained;
- an energy or non-power sensor used as a power input;
- fees counted twice;
- a frozen price input;
- a missing regional forecast;
- an empty required Solver input.
