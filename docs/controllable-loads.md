# Controllable loads reference

Field reference for the **Controllable Load** subentry (nimbus issue #486,
sub-issue 10 of Mark Purcell's controllable-loads spec, #476). Same shape as
[`configuration-reference.md`](configuration-reference.md) — the wizard's own
`data_description` carries the same explanations inline; this page is a
scannable table version.

**Status: config surface, LP wiring, and real device dispatch, as of this
page.** A Controllable Load subentry feeds `build_plan()` (its own
`sheddable_kw`/`adequacy_kw` show up in the Solver's real plan, and the LP
genuinely decides its timing/level), and — since #476/#534's output layer
landed — an optional **Device entity** field now lets Nimbus itself actually
COMMAND a real `switch` or `water_heater` through that decision, with a real
`sensor.nimbus_<load>_commanded_state` entity to watch it happen. This
replaces the household-written reference automation the earlier version of
this page called for: with Device entity set, Nimbus dispatches directly, no
separate automation needed. See "Output / dispatch" below for the full
mechanism, and "What's not built yet" for what's still missing (`climate`
support, tracking-fidelity sensors, price-gated/thermal/quota kinds).

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
| Device to command (optional) | The real entity this load is physically commanded through once the Solver decides on/off — a `switch` (turned on/off directly) or a `water_heater` (commanded via `set_operation_mode`, "performance" for on / "eco" for off — #534). Blank = this load is scored/planned exactly as before, never physically commanded. `climate` isn't implemented yet — see "What's not built yet". |
| Minimum hold between commands (optional, minutes) | Overrides the shared default anti-chatter debounce (`DEFAULT_MIN_HYSTERESIS_PERIODS`, currently 2 real solve periods) for just this load. Blank = use the shared default. |
| Max activations per day (optional) | Caps how many times this device may be commanded ON per day, regardless of what the Solver would otherwise schedule — the real device-side constraint #534's own investigated bridge enforces (3/day). Blank = no cap. An OFF command is never capped. |

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
| Done sensor (optional) | A `binary_sensor`, numeric sensor, or `water_heater`/`climate` entity telling the Solver this load is genuinely finished — see "Early completion" below. Leave blank to always follow the target/deadline as configured. |
| Done condition (optional) | For a numeric done sensor, e.g. `>= 60` for a tank reaching 60°C. Leave blank when the done sensor is a `binary_sensor` (its own `on` state is the done condition) or a `water_heater`/`climate` entity (defaults to its own live setpoint — see "`water_heater`/`climate` done sensors" below). |

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

- **`climate` domain dispatch.** #534's own "written for both domains" note
  flags `climate.*` (an HVAC zone) as sharing the same `current_temperature`/
  `temperature` shape `water_heater` already has — Device entity accepts any
  domain in the selector, but `dispatch_commanded_state()` (`solver_writer.py`)
  only recognizes `switch`/`water_heater` today. Pointing Device entity at a
  `climate.*` entity logs a WARNING and dispatches nothing, rather than
  guessing at a service call against hardware this project hasn't verified
  against yet.
- **No per-load, per-domain mode-string override for `water_heater`.**
  `dispatch_commanded_state()` always issues `"performance"`/`"eco"` — the
  exact convention #534's own investigated device uses. A future household
  whose bridge uses different mode names would need that made configurable;
  not built speculatively ahead of a real need.
- **No thermal-state target (#481).** Device entity lets Nimbus command a
  `water_heater` on/off, but the LP still schedules it as a plain
  Sheddable/Deferrable load (kW and kWh) — it does not yet understand "heat
  to 60°C by 17:00" as a real temperature-band target. #481 (thermal kind) is
  the largest unbuilt piece of the whole spec — a genuine new LP model
  (heat-transfer dynamics, a comfort-band construction), not a wiring task.
- **No tracking-fidelity/monitoring sensors** (`scheduled_kw`, `actual_kw`,
  `tracking_fidelity_24h`, `tracking_error_cost_24h`, the plain-language
  `sensor.nimbus_<load>_status`, `delivered_today_kwh` vs `target_today_kwh`
  display) — the rest of #484's own spec. `sensor.nimbus_<load>_commanded_
  state` (below) covers the real on/off decision and its own run-state
  fields; the analytics layer on top is still deferred.
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

**Status: decision layer, now with a real output stage (see "Output /
dispatch" below).** The guarded `commanded_state` is computed and persisted
every solve, published as `sensor.nimbus_<load>_commanded_state`, and — when
Device entity is configured — actually dispatched to a real HA service.
`scheduled_kw`/`actual_kw`/`tracking_fidelity_24h`/`tracking_error_cost_24h`/
the plain-language status sensor/`delivered_today_kwh` vs `target_today_kwh`
display remain deferred (see "What's not built yet") — the analytics layer on
top of the now-real dispatch mechanism, not a blocker for it.

## Output / dispatch (nimbus issue #476/#534, real device commanding)

The gap every earlier section of this page used to point at as the reason
nothing could actually run: `apply_commanded_state_guard()`'s own guarded
`commanded_state` had "no consumer yet" — nothing read it, nothing called a
real HA service. That gap is closed. Every solve, when a load's own
Device entity is configured AND `commanded_state` genuinely CHANGES (never
on every solve tick — a load that stays on across many re-solves is
commanded once, not spammed), `dispatch_commanded_state()` (`solver_writer.py`)
calls the right service for that entity's domain:

- **`switch.*`**: `switch.turn_on`/`switch.turn_off`, unconditional.
- **`water_heater.*`**: `water_heater.set_operation_mode`, `"performance"`
  for on / `"eco"` for off — #534's own investigated real device's exact
  convention.

Two real device-side constraints, configurable per load rather than
hard-coded (#534 item 3's own explicit ask):

- **Minimum hold between commands** overrides the shared debounce above for
  just this load.
- **Max activations per day** caps real ON dispatches specifically — an OFF
  command is never capped. When the cap is already reached, the Solver's own
  desired `commanded_state` is still persisted (so `sensor.nimbus_<load>_
  commanded_state` honestly shows "wants ON, capped" rather than silently
  lying that nothing changed) — the real physical command is simply not
  issued that cycle. `load_run_state.activation_allowed()`/
  `record_activation()` are the pure functions behind this; the count rolls
  over at local midnight the same way `delivered_today_kwh` does.

A dispatch failure (a bad entity_id, the target service unavailable) is
caught per-load and logged — it never blocks another load's own dispatch in
the same solve cycle, or the solve cycle itself. No Device entity configured
is a complete no-op, byte-identical to this page's pre-#534 behaviour for
that load: scored and planned, never physically commanded.

**`sensor.nimbus_<load>_commanded_state`** (one per Controllable Load
subentry, its own device, same per-subentry-device pattern as a Load/Signal
subentry's own forecaster sensor): state is `"on"`/`"off"` reflecting
`commanded_state`; attributes carry the full persisted run-state
(`currently_on`, `on_since`/`off_since`, `delivered_today_kwh`,
`activations_today`, `commanded_since`, and this load's own configured
`device_entity`/`min_hold_minutes`/`max_activations_per_day`) — everything
needed to see WHY a value is what it is without cross-referencing the
wizard.

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

### `water_heater`/`climate` done sensors (nimbus issue #534, item 1 only)

A `water_heater` or `climate` entity's own *state* is a mode string
(`eco`, `heat`) — never a number — so the fixed comparison DSL above
can't run against it directly the way it does for a plain numeric
sensor. When the **Done sensor** is one of these two domains, the
Solver instead reads its **`current_temperature`** attribute as the
value to compare, and — if **Done condition** is left blank — defaults
it to `>= <the entity's own "temperature" attribute>` (its live
setpoint), instead of the `binary_sensor` "state == on" default every
other domain uses. Concretely, pointing Done sensor at
`water_heater.hot_water_heat_pump_hot_water_sg_ready` with no Done
condition is enough on its own: the tank counts as done the moment
`current_temperature >= temperature`, without a separate
`sensor.*_current_temperature` helper entity. The same
unavailable/malformed-condition fail-open and once-per-condition
logging above apply unchanged — real value for this specific device:
its bridge republishes MQTT availability roughly hourly, blipping every
entity on it through `unavailable`/`unknown`, and each blip must be
ignored rather than read as "not done, restart the schedule".

**Status: reading/evaluation, and now real `water_heater` command dispatch
too (see "Output / dispatch" above) — thermal seeding still not built.**
The Done-sensor reading piece needed no wizard change (the field's
`EntitySelector` already accepts any domain). Commanding a `water_heater`
load via `set_operation_mode` with a per-load minimum-hold and
daily-activation cap (#534 item 3) is real now, through the separate Device
entity field. NOT built: seeding a thermal-kind load's own
`min_temp`/`max_temp`/`temperature` fields from the entity, or the "reach
X°C by deadline" target itself (#481, not started — see "What's not built
yet"), and `climate` domain dispatch (same section).

## Diagnostics

`sensor.nimbus_solver_battery_forecast`'s own solve, each cycle, calls
`build_controllable_loads()` (`solver_writer.py`) to build the real
`SheddableLoadConfig`/`AdequacyLoadConfig` lists from every configured
Controllable Load subentry, native-HA-mode only. A misconfigured subentry
(a required field left blank) is skipped for that cycle with a `WARNING`
log line naming the load and the missing field — check the HA log, or
`sensor.nimbus_health_report`'s own `recent_warnings`, if a configured load
doesn't seem to be affecting the plan.
