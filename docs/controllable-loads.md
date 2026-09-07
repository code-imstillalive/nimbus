# Controllable loads reference

Field reference for the **Controllable Load** subentry (nimbus issue #486,
sub-issue 10 of Mark Purcell's controllable-loads spec, #476). Same shape as
[`configuration-reference.md`](configuration-reference.md) — the wizard's own
`data_description` carries the same explanations inline; this page is a
scannable table version.

**Status: config surface + LP wiring only, as of this page.** A Controllable
Load subentry genuinely feeds `build_plan()` (its own `sheddable_kw`/
`adequacy_kw` show up in the Solver's real plan, and the LP genuinely decides
its timing/level) — but there is **no per-load output sensor yet**, so there
is nothing a real household automation can read to act on that decision. The
reference automation Mark's own spec calls for depends on that output
existing first; it isn't written yet. See "What's not built yet" below before
assuming this can drive a real device today.

## What a Controllable Load is

Distinct from a plain **Load** subentry (forecasted, but always fixed,
unavoidable demand as far as the Solver is concerned). A Controllable Load is
something the Solver actually decides the timing or level of.

Two kinds are wired to a real solver class today:

| Kind | Solver class | Real-world shape |
|---|---|---|
| **Sheddable** | `SheddableLoadConfig` | A load that CAN be reduced below its own forecast under price pressure — a pool pump, for instance. |
| **Deferrable** | `AdequacyLoadConfig` (#477's soft-shortfall version) | A load with a real energy target and deadline — hot water reaching temperature, EV charging by departure. |

Three more kinds are named in the wizard's own data model (`quota`, `thermal`,
`price_gated`) but not yet offered as a selectable option — each needs its own
solver-side class first (#479, #481, #482 respectively). Adding one later is a
schema extension, not a redesign.

## Wizard fields

Reached the same way as every other subentry: the Nimbus hub's device page →
**+ Add** → **Controllable Load**.

| Field | Purpose |
|---|---|
| Name | A short, real name for this load (e.g. "Pool Pump", "HWS L1") — becomes the subentry's device title. |
| Kind | Sheddable or Deferrable — selects which fields below are actually read, and which solver class this becomes. |
| Real power sensor (optional) | The device's actual measured power draw, if you have one. Not read by the LP itself — reserved for monitoring/tracking-fidelity (#484), not yet implemented. |

**Sheddable fields:**

| Field | Purpose |
|---|---|
| Nominal power (kW) | This load's real rated power when running at full draw — becomes a flat forecast (`nominal_kw` repeated across every period), since there's no linked Forecaster load feeding a real per-period shape yet. |
| Minimum fraction (0-1, optional) | The smallest fraction of nominal power that must always be delivered. Blank = can be shed all the way to zero. |
| Shed cost ($/kWh) | The real cost the Solver pays every kWh it sheds below nominal. Defaults to `2.00` (`DEFAULT_SHED_COST`) — high enough that shedding stays a genuine last resort, not a routine substitute for battery/grid dispatch. |

**Deferrable fields:**

| Field | Purpose |
|---|---|
| Max power (kW) | This load's own real power ceiling while running (e.g. a 3.7kW HWS element). |
| Target energy (kWh) | The real energy amount that must be delivered by the deadline. |
| Earliest start (24hr decimal, optional) | The earliest time of day this load may run at all. `0` = midnight. Blank = no restriction (can run any time from now). |
| Deadline (24hr decimal, optional) | The real deadline — cumulative energy delivered must reach the target by this time of day. Blank = the whole 96h horizon is the window. |
| Shortfall price ($/kWh) | The real cost the Solver pays for every kWh short of the target by the deadline. Defaults to `10.00` (`DEFAULT_ADEQUACY_SHORTFALL_PRICE`) — high enough that a genuinely reachable target still gets fully met at any real price; a genuinely unreachable one costs this instead of taking the whole plan infeasible (#477). |
| Value credit (optional, $/kWh) | A utility credit per kWh served, beyond the bare target — makes this load also run wherever the switchboard's own live shadow price is at or below this value (#482 groundwork), not just enough to hit the deadline. |

**Earliest/deadline hour resolution, in plain terms:** both fields are a
24-hour decimal ("hour of day"), resolved against *the next real occurrence
of that time from now* — e.g. asking for a deadline of `6.0` at 10pm tonight
resolves to 6am **tomorrow**, not a nonsensical negative offset into the
past. If the resolved deadline would land before the resolved earliest time
for this specific solve cycle (a real, if rare, edge case right around
midnight), that cycle skips this load with a warning in the log rather than
crash — the next cycle's own "now" almost always resolves it correctly.

## What's not built yet

Real, tracked gaps, not oversights being papered over:

- **No per-load output.** The Solver's plan genuinely includes this load's
  own scheduled power (visible in `sensor.nimbus_solver_battery_forecast`'s
  own `adequacy_loads`/`sheddable_loads` internals if you inspect a solve
  directly), but nothing publishes a per-load `commanded_state` sensor or
  sub-device yet (#465's own pattern, not yet applied here). Until that
  exists, there is nothing for a real household automation to read.
- **No reference automation.** Directly blocked on the point above — a
  chatter-guarded switch-call automation (the #484 sub-issue 8 pattern) needs
  a real published state to react to.
- **No linked-Forecaster-load option.** A sheddable load's forecast is always
  flat (`nominal_kw`) — there's no way yet to point it at an existing Load
  subentry's own real per-period forecast instead.
- **`quota`/`thermal`/`price_gated` kinds** aren't selectable — #479/#481/#482
  need to land first.
- **No shadow costing, monitoring, or household-mode wiring** (#483/#484/#485)
  — a Controllable Load's real running cost and tracking fidelity aren't
  computed or exposed anywhere yet.

## Diagnostics

`sensor.nimbus_solver_battery_forecast`'s own solve, each cycle, calls
`build_controllable_loads()` (`solver_writer.py`) to build the real
`SheddableLoadConfig`/`AdequacyLoadConfig` lists from every configured
Controllable Load subentry, native-HA-mode only. A misconfigured subentry
(a required field left blank) is skipped for that cycle with a `WARNING`
log line naming the load and the missing field — check the HA log, or
`sensor.nimbus_health_report`'s own `recent_warnings`, if a configured load
doesn't seem to be affecting the plan.
