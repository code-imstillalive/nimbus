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
| Done sensor (optional) | A `binary_sensor` or numeric sensor telling the Solver this load is genuinely finished — see "Early completion" below. Leave blank to always follow the target/deadline as configured. |
| Done condition (optional) | For a numeric done sensor only, e.g. `>= 60` for a tank reaching 60°C. Leave blank when the done sensor is a `binary_sensor` (its own `on` state is the done condition). |

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

- **No per-load output SENSOR yet.** The relay-chatter-guarded
  `commanded_state`/`commanded_since` decision itself is now computed and
  persisted every solve (see "Relay-chatter guard" below) — but nothing
  publishes it as a real HA sensor or sub-device yet (#465's own pattern,
  not yet applied here). Until that exists, there is still nothing for a
  real household automation to actually READ, even though the guarded
  decision genuinely exists in the run-state store.
- **No reference automation.** Directly blocked on the point above — a
  chatter-guarded switch-call automation (the #484 sub-issue 8 pattern) needs
  a real published state to react to.
- **No tracking-fidelity/monitoring sensors** (`scheduled_kw`, `actual_kw`,
  `tracking_fidelity_24h`, `tracking_error_cost_24h`, the plain-language
  `sensor.nimbus_<load>_status`, `delivered_today_kwh` vs `target_today_kwh`
  display) — the rest of #484's own spec, deferred alongside the sensor
  gap above since building the analytics layer before the entities exist
  to show it would be built twice.
- **No linked-Forecaster-load option.** A sheddable load's forecast is always
  flat (`nominal_kw`) — there's no way yet to point it at an existing Load
  subentry's own real per-period forecast instead.
- **`quota`/`thermal`/`price_gated` kinds** aren't selectable — #481/#482
  need to land first, and #479's own daily-carry math (below) needs a
  `quota` wizard kind to actually attach to.
- **No shadow costing or household-mode wiring** (#483/#485) — a
  Controllable Load's real running cost isn't computed or exposed
  anywhere yet.
- **No `completed_early_periods`/`kwh_released` reporting, no EMA
  learning hook** (#480's own remaining scope) — see "Early completion"
  below for what IS built (the core stop-scheduling mechanic).

## Run-state store (nimbus issue #479, foundation only)

Every configured load's `power_sensor` (if set) is now sampled once per
solve tick and folded into a small per-hub JSON store
(`custom_components/nimbus_load/load_run_state.py`) tracking
`currently_on`/`on_since`/`off_since`/`delivered_today_kwh` — real
restart-survivable state, same durability pattern as the Solver's own
`number.nimbus_solver_*` settings. The sample is scaled to kW first if
the sensor itself reports Watts (`unit_of_measurement: "W"`) — a real
bug (nimbus issue #535, Mark Purcell) had a 4.6W standby reading read
as 4.6 kW, making `currently_on` permanently true and
`delivered_today_kwh` ~1000× too large for any load whose power sensor
is a plug/CT sensor reporting native Watts, the common case. Fixed the
same way `_kw_scale_factor()` already fixes it for the Solver's own
solar/load/battery quality-report sensors; logs once per sensor
(`#313`/`#314` discipline) when the scaling actually fires. This lands ahead of the things that
actually need it (#484's relay-chatter guard, below, needs a place to
persist its own guarded decision; #480's early completion needs
`delivered_today_kwh`), rather than alongside them — scoped down the
same way #486 was, building the shared foundation once instead of
duplicating a state store per consuming feature.

Also landed: the daily quota carry/rollover math itself
(`compute_rollover()`/`effective_target_kwh()`/`remaining_kwh()`) —
fully implemented and tested against #479's own synthetic 3-day
scenario, but with nothing to attach to yet, since `quota` isn't a
selectable wizard kind (see above). **Nothing reads this store or this
math today** — no sensor exposes `delivered_today_kwh`, no LP field
consumes `remaining_kwh`. It exists so the next feature that needs it
doesn't have to build it from scratch.

## Relay-chatter guard (nimbus issue #484, decision layer only)

Mark's own cited HAEO incident motivates this: "a plan re-solved every
few seconds drove 131 spurious relay states in a night." Every solve
now computes a raw on/off decision for each Controllable Load from its
own just-solved period-0 scheduled power, and persists a GUARDED
`commanded_state`/`commanded_since` in the same per-load run-state store
above (`apply_commanded_state_guard()`, `solver_writer.py`) — genuinely
separate from `currently_on`/`on_since`/`off_since`, which track what
the load's real power sensor MEASURED, not what the Solver last decided
to command.

The guard is a real debounce, not a rate limit: a raw decision that
disagrees with the currently-published `commanded_state` only gets
adopted once it has held *consecutively* for `DEFAULT_MIN_HYSTERESIS_
PERIODS` (2, the spec's own default — sub-issue 2/#478's own
`min_on_periods` override doesn't exist yet, so this default always
applies) real solve periods. A raw decision that flips back to agreeing
with the current `commanded_state` — even once — clears any in-progress
challenge entirely, rather than pausing it. This is what makes an
"indifferent" load whose period-0 decision flips on literally every
single re-solve produce **zero** real published changes across ten
consecutive solves, not one every `min_hysteresis` window — the exact
acceptance scenario #484 itself specifies. See
`load_run_state.decide_commanded_state()`'s own docstring and
`tests/test_load_run_state.py`'s `TestDecideCommandedState` for the
full worked traces.

**Status: decision layer only, same honest partial-scope pattern as
everything else on this page.** The guarded `commanded_state` is real
and persisted every solve — but, per "What's not built yet" above,
nothing publishes it as an HA sensor yet, so there is still no automation
this can actually drive today. `scheduled_kw`/`actual_kw`/
`tracking_fidelity_24h`/`tracking_error_cost_24h`/the plain-language
status sensor/`delivered_today_kwh` vs `target_today_kwh` display are
all deferred to the same follow-up that builds the sensor/sub-device
itself (#465's own pattern) — building the analytics layer with nothing
to show it on would mean building it twice.

## Early completion (nimbus issue #480, core mechanic only)

"Hot water scheduled for 3h, tank reaches setpoint after 2h — the third
hour is still bought." When a deferrable load's **Done sensor** reports
done (see the wizard fields above), the Solver stops scheduling any
further energy for it **this solve cycle onward** — the just-finished
load simply doesn't appear in the LP's own adequacy-load list at all
(`build_controllable_loads()`), the same way a misconfigured load is
skipped, except this is the expected, successful case, logged at INFO
rather than WARNING.

A `binary_sensor` done sensor's own `on` state alone means done. Any
other sensor (a tank-temperature reading, say) needs the **Done
condition** field too — a small, fixed comparison (`>= 60`, `== 1`,
etc.), deliberately not a free-form expression evaluator (a household-
supplied config string never runs as code). **Fails open**: a missing
entity, an `unknown`/`unavailable` state, or a malformed done condition
is all treated as "not done" — the load keeps its normal schedule,
exactly matching #480's own acceptance criterion.

**Status: the core skip mechanic only.** Real and working — a load that
reports done genuinely stops being scheduled. NOT built: the
`completed_early_periods`/`kwh_released` reporting fields #480's own
spec also asks for (needs a NEW per-requirement-window delivered-energy
tracker — genuinely different from #479's own calendar-day-scoped
`delivered_today_kwh`, since a deferrable load's own deadline window
doesn't necessarily align with local midnight), and the optional EMA
run-duration learning hook (explicitly marked optional in #480's own
spec). Both are real, deferred follow-ups, not silently dropped.

## Diagnostics

`sensor.nimbus_solver_battery_forecast`'s own solve, each cycle, calls
`build_controllable_loads()` (`solver_writer.py`) to build the real
`SheddableLoadConfig`/`AdequacyLoadConfig` lists from every configured
Controllable Load subentry, native-HA-mode only. A misconfigured subentry
(a required field left blank) is skipped for that cycle with a `WARNING`
log line naming the load and the missing field — check the HA log, or
`sensor.nimbus_health_report`'s own `recent_warnings`, if a configured load
doesn't seem to be affecting the plan.
