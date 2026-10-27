# Going live: letting Nimbus drive your battery

Nimbus **plans**; it never commands your inverter by itself. Out of the box it
is in shadow: it publishes a plan every minute or so, scores itself against
what really happened, and changes nothing. Going live means adding one Home
Assistant automation that reads the plan and sends your inverter the matching
command. That step is deliberately yours, because it is specific to your
hardware and it moves real money.

This page takes you from shadow to live, safely. It comes with a ready-made
automation **blueprint** you can import, so you only supply the parts that are
specific to your inverter.

Read it through once before you start. Take each step in order, and do not
skip the observation days.

## Quick start

If you have done this kind of thing before, here is the whole job:

1. **Watch** Nimbus's plan for a few days with the dry-run switch on ([step 1](#step-1-watch-it-in-shadow-at-least-a-few-days)).
2. **Write** your inverter's actions and test each by hand ([step 2](#step-2-build-your-inverters-actions)).
3. **Create** two helpers: an arm switch and a mode text ([step 3](#step-3-create-two-helpers)).
4. **Import** the blueprint and fill it in ([step 4](#step-4-import-the-blueprint-and-create-the-automation)).
5. **Test the stop**, then arm it and watch for an hour ([step 5](#step-5-the-first-supervised-trial)).

Everything below explains each step, what keeps you safe, and what to do when
something looks wrong.

## What you need before you start

- **An inverter Home Assistant can command.** Your inverter's integration must
  let you switch its mode (forced charge, forced discharge, self-consumption)
  and set a power, from Home Assistant, usually over a local connection such
  as Modbus. If you can only *read* your inverter, Nimbus can plan but not
  drive it.
- **Nimbus solving properly.** `sensor.nimbus_solver_battery_forecast` shows a
  number and its `status` attribute reads `optimal`.
- **Your battery's real numbers** on the Solver tab: capacity, maximum charge
  and discharge power, and minimum charge. The plan is only as good as these.
- **About an hour** for the first supervised trial, at a time you can watch the
  battery and the grid meter.

## The words used on this page

| Word | Means |
|---|---|
| **Charge / Discharge** | The inverter forced to charge or discharge the battery at a set power, whatever the house is doing. |
| **Self-Consume** | The inverter's own everyday mode: solar feeds the house first, spare solar goes into the battery, and the battery covers the house when there is no sun. |
| **Hold** | The battery kept idle while the grid supplies the house, to save the battery's charge for later (only if your inverter can do this). |
| **Fallback** | What the inverter does whenever Nimbus is not in control. Normally Self-Consume. |
| **Armed** | Your arm switch is on, so the plan may move the battery. |
| **Deadband** | A small band around 0 kW (±0.05 kW) treated as "idle", so tiny plan values don't send commands. |
| **Dwell** | The minimum time (3 minutes) the battery stays in one mode before it may switch to another. |

## How it fits together

```
 Nimbus (plans)                    Your automation (acts)            Your inverter
 sensor.nimbus_solver_     ──►     reads the plan every minute  ──►  Charge / Discharge /
 battery_forecast                  checks it is safe to follow       Self-Consume / Hold
 (kW, + discharge, − charge)       runs YOUR actions for the mode    at the planned kW
                                   gated by YOUR arm switch
```

| Part | Who owns it | What it does |
|---|---|---|
| The plan | Nimbus | What the battery should do now, and for the next days |
| The arm switch | You (an `input_boolean` helper) | The single on/off for "Nimbus may move my battery" |
| The dispatch automation | You (from the blueprint) | Turns the plan into your inverter's commands, only while armed |
| The inverter actions | You | The actual service calls or scripts your inverter integration needs |
| The fallback | You | What the inverter does whenever Nimbus is not in control |

## What Nimbus publishes for you to act on

| Entity / attribute | Meaning |
|---|---|
| `sensor.nimbus_solver_battery_forecast` (state) | Planned battery power **now**, in kW, AC side. **Positive = discharge, negative = charge**, 0 = idle. |
| … attribute `status` | `optimal` when the plan solved properly. Anything else: do not follow it. |
| … attribute `generated_at` | When this plan was made. A plan that stops updating must not be followed. |
| … attribute `absorb_surplus` (`absorb_surplus_reason`) | True when there is spare solar the plan did not count on, and exporting it pays less than your *Don't sell below* price: put the inverter in self-consumption so the spare goes into the battery. See [Configuration reference](configuration-reference.md). |
| … attribute `price_spike_active` | A price spike is on. Never delay acting on it. |
| `sensor.nimbus_solver_current_dispatch_hold` | True when the plan keeps the battery idle **while the grid supplies the house** (self-consumption would drain the battery instead). |
| `sensor.nimbus_solver_current_dispatch_solar_charge` | True when the plan charges the battery **from solar alone** and sells nothing: self-consumption does what the plan wants, and on a duller morning than forecast it charges from the sun there is instead of the grid. |
| `sensor.nimbus_solver_current_dispatch_direction` | The plan's direction now, in words: handy for dashboards. |
| `switch.nimbus_solver_dispatch_dry_run` | Records what would have been dispatched, as history, without commanding anything. |

The sign convention is fixed and declared on the plan itself
(`battery_kw_sign_convention: positive_discharge_negative_charge`). Your
inverter's own sign may differ: the blueprint always hands your actions a
**positive** power, and you choose charge or discharge by which action runs.

## Step 1: watch it in shadow (at least a few days)

1. Leave any existing battery automation as it is, or disabled; Nimbus changes
   nothing yet.
2. Turn on `switch.nimbus_solver_dispatch_dry_run`.
3. For a few days, compare the plan (the Solver tab's **Battery: plan against
   measured** chart, and `sensor.nimbus_solver_dispatch_dry_run` in History)
   with what you would have wanted. Check:
   - it charges and discharges at sensible times and prices;
   - your battery capacity, power limits and minimum charge are right
     (Solver tab, Basic settings);
   - the daily score (Performance) is steady.

Only go on when the plan is one you would be happy to follow.

## Step 2: build your inverter's actions

The blueprint asks you for these. Each is a list of ordinary Home Assistant
actions (a script call, a `select.select_option`, a `number.set_value`, …).
Write and test each one **by hand first** (Settings → Developer tools →
Actions, or run the script), with the battery watched, before any
automation uses it.

Your inverter's integration decides what these look like. A typical *Enter
Charge mode* action, written for an inverter whose integration offers a mode
`select` and a power `number` (replace the entity names with your own, and
check your integration's documentation for its exact options):

```yaml
- action: select.select_option
  target:
    entity_id: select.my_inverter_remote_mode   # your inverter's mode entity
  data:
    option: "Charge"                            # its option for forced charge
```

and *Set charge/discharge power*:

```yaml
- action: number.set_value
  target:
    entity_id: number.my_inverter_remote_power  # your inverter's power entity
  data:
    value: "{{ (power_kw * 1000) | int }}"      # if it wants watts; just {{ power_kw }} for kW
```

If your integration already has scripts for these, call the script instead.
The blueprint passes `power_kw` (positive, in kW, capped at your maximum) to
the power action; you choose charge or discharge by which mode action runs.

| Action | What it must do | Typical example |
|---|---|---|
| **Enter Charge mode** | Put the inverter in forced/remote **charging** | remote-control mode on, command "charge" |
| **Enter Discharge mode** | Forced/remote **discharging** | remote-control mode on, command "discharge" |
| **Set charge/discharge power** | Write the power setpoint. Use `{{ power_kw }}`: always positive, kW, already capped at your maximum | `number.set_value` on the inverter's power setpoint |
| **Enter Self-Consume mode** | The inverter's own self-consumption mode: solar to the house first, spare solar into the battery | remote-control off / "self-consumption" mode |
| **Enter Hold mode** (optional) | Keep the battery idle while the grid supplies the house | remote mode with a stop / 0 kW command |
| **Keep-alive** (optional) | For inverters whose remote mode times out without a periodic command | the vendor's heartbeat |
| **Fallback** | What the inverter should do whenever Nimbus is **not** in control | normally the same as Self-Consume |

Mode actions run **when the mode changes** (with the mode helper below), after
every Home Assistant start, and again every *Re-send the mode command* minutes
(10 by default) while the mode stays the same; the power setpoint and
keep-alive run every cycle. So a mode action may safely do several writes, you
are not re-writing the inverter's mode every minute (which some Modbus
integrations do not tolerate), and a mode command that was lost, or an
inverter that reverted on its own, is put right within minutes. The fallback
is re-sent the same way.

## Step 3: create two helpers

Settings → Devices & services → Helpers → Create helper:

1. **Toggle** named *Nimbus dispatch armed* → `input_boolean.nimbus_dispatch_armed`.
   This is the arm switch. Leave it **off** for now.
2. **Text** named *Nimbus dispatch mode* → `input_text.nimbus_dispatch_mode`.
   The automation writes the mode it set here (`CHG`, `DIS`, `SC`, `HOLD`,
   `FALLBACK`). Leave it empty.

## Step 4: import the blueprint and create the automation

1. Import the blueprint with this button:

   [![Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fcode-imstillalive%2Fnimbus%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fnimbus%2Fbattery_dispatch.yaml)

   or by hand: Settings → Automations & scenes → Blueprints → **Import
   blueprint**, and paste
   `https://github.com/code-imstillalive/nimbus/blob/main/blueprints/automation/nimbus/battery_dispatch.yaml`
2. Select **Nimbus battery dispatch** → **Create automation**, and fill in:
   - **Nimbus plan sensor:** `sensor.nimbus_solver_battery_forecast`;
   - **Nimbus hold signal:** `sensor.nimbus_solver_current_dispatch_hold`
     (only if you wrote a Hold action);
   - **Nimbus solar-charge signal:** `sensor.nimbus_solver_current_dispatch_solar_charge`
     (recommended: a charge the plan takes from solar then uses
     Self-Consume, never the grid);
   - **Arm switch:** `input_boolean.nimbus_dispatch_armed`;
   - **Maximum battery power:** your inverter's real limit or below;
   - your actions from step 2;
   - **Mode helper:** `input_text.nimbus_dispatch_mode` (Behaviour section).
3. Save. With the arm switch off, it does nothing.

## Step 5: the first supervised trial

Pick a daytime hour when you can watch the battery and the grid meter.

1. **Test the stop first:** turn the arm switch on, wait a minute, turn it off.
   The fallback must run (check the automation's trace and the inverter).
2. Turn the arm switch on and watch for an hour:
   - the mode in `input_text.nimbus_dispatch_mode` matches the plan's direction;
   - the battery's measured power follows the plan's kW (within a few %);
   - the inverter really changed mode (its own status, not only Home
     Assistant's);
   - nothing exports when the plan is soaking up spare solar.
3. If anything does not match, **turn the arm switch off**. That is always
   safe: the fallback runs.

Then leave it armed, and keep an eye on the Performance score for the first
days.

### A card to keep an eye on it

Add this to any dashboard (Edit dashboard → Add card → Manual) to see at a
glance whether Nimbus is in control and what it is doing. Use your own helper
names if you chose different ones.

```yaml
type: entities
title: Nimbus battery control
entities:
  - entity: input_boolean.nimbus_dispatch_armed
    name: Nimbus may move the battery
  - entity: input_text.nimbus_dispatch_mode
    name: Mode the automation set
  - entity: sensor.nimbus_solver_current_dispatch_direction
    name: What the plan wants now
  - entity: sensor.nimbus_solver_battery_forecast
    name: Planned battery power (kW, + discharge, − charge)
  - entity: sensor.nimbus_solver_current_dispatch_solar_charge
    name: Charging from solar only
  - entity: sensor.nimbus_solver_current_dispatch_hold
    name: Holding the battery
```

## What the arm switch does, and why it is separate

The arm switch is the **only** thing that lets the plan move your battery.

- **Off:** the automation does nothing at all, except run your fallback **once**,
  at the moment you switch it off, so the inverter is not left in a forced mode.
- **On:** the automation follows the plan, subject to every safety check below.

It is a separate helper, not the automation's own on/off, on purpose. An
automation can be switched back on without anyone meaning to (a restart, a
reload, a restore), and disabling an automation **does not undo the last
command your inverter received**: a battery left in forced discharge stays in
forced discharge. Keeping "the code is loaded" (the automation) apart from
"dispatch is authorised" (the arm switch) means only a deliberate switch arms
it, and switching it off always hands the inverter back.

## Failsafes at a glance

What happens when something goes wrong, with the blueprint as shipped:

| If… | The automation… |
|---|---|
| you turn the arm switch **off** | runs your fallback once, then does nothing until you arm it again |
| Nimbus stops publishing a plan (it is disabled, removed, or stuck) | sees the plan go stale after 10 minutes (*Stale plan after*) and runs the fallback |
| the plan is `unknown`, `unavailable` or not a number | runs the fallback, never a power command |
| the plan did not solve properly (`status` is not `optimal`) | runs the fallback |
| Home Assistant restarts | runs again at start: re-sends the mode for a valid plan, or the fallback for one that isn't valid yet |
| a mode command is lost, or the inverter reverts on its own | re-sends the mode every 10 minutes (*Re-send the mode command*) |
| the plan asks for more power than your inverter can give | caps the power at your *Maximum battery power* |
| the plan wobbles around zero | ignores anything inside the deadband (±0.05 kW) and keeps each mode at least 3 minutes, except for a price spike, a big reversal or the fallback |
| a slow inverter action is still running when the next trigger comes | waits, then reads everything afresh; more than three waiting is logged as a warning |

Your inverter's own protections (its minimum charge, temperature and current
limits) always stay in force: Nimbus asks, the inverter decides.

## What the automation does, every minute

It runs 10 seconds after each new plan, every minute as a watchdog, when Home
Assistant starts, and when the arm switch changes.

1. **Armed?** If not, stop (after running the fallback once on disarm).
2. **Is the plan safe to follow?** Its state is a number, its `status` is
   `optimal`, and it is no older than *Stale plan after* (10 minutes by
   default). If not: **fallback**, never a power command.
3. **Which mode?**
   - spare solar to soak up (`absorb_surplus`): **Self-Consume**;
   - plan above the deadband (+0.05 kW): **Discharge** at the plan's kW;
   - plan below it (−0.05 kW) and charging from solar alone (the solar-charge
     signal, if you gave it): **Self-Consume**, so the battery charges from the
     sun there really is and never buys a shortfall from the grid;
   - plan below it otherwise: **Charge** at the plan's kW;
   - idle with the grid supplying the house (hold signal, and a Hold action
     given): **Hold**;
   - otherwise: **Self-Consume**.
4. **Minimum dwell:** once in a mode, stay at least *Minimum dwell* (3 minutes)
   before switching to another, so the battery does not flip back and forth.
   Never delayed: a price spike, a switch straight between Charge and Discharge
   of *Reversal* kW (5) or more, or the fallback. The power setpoint within
   the current mode still follows the plan every cycle.
   Nimbus damps flips on its own side too. Its *Direction Change Margin*
   (`number.nimbus_solver_period0_hysteresis`, 1 cent by default) keeps the
   plan's first period charging, holding or discharging unless switching
   saves more than that. A held charge that would buy from the grid is cut to
   what the solar surplus carries rather than dropped, so a sunny midday
   holds a steady charge instead of flipping between charge and idle every
   few minutes (0.94.492). The plan's `period0_hysteresis` attribute says what
   it did on each solve (`kept`, `kept_from_surplus`, `switched`, ...).
5. **Act:** run the mode's action if the mode changed, after a Home Assistant
   start, or when its re-send is due; while charging or discharging, set the
   power (capped at your maximum); run the keep-alive.
6. **Record** the mode in the mode helper.

A trigger that arrives while a slow action is still running waits its turn
and then reads everything afresh; more than three waiting is logged as a
warning rather than dropped silently.

## Stopping, and going back

- **To stop now:** turn the arm switch off. The fallback runs.
- **To stop for good:** turn the arm switch off, then disable or delete the
  automation. Nimbus keeps planning in shadow.
- **If you had a previous battery automation:** keep it disabled while Nimbus
  is armed (two controllers fighting over one inverter is the most common way
  to get erratic behaviour), and re-enable it only after disarming Nimbus.

## When something looks wrong

| You see… | Check |
|---|---|
| **Armed, but nothing happens** | The automation is enabled (Settings → Automations). Open its trace: it says which check stopped it. `sensor.nimbus_solver_battery_forecast`'s `status` is `optimal`, and its `generated_at` is recent. |
| **The mode helper says `FALLBACK`** | The plan was refused: the trace says why (stale, failed or not a number). Nimbus's own log (Settings → System → Logs, filter "Nimbus") usually names the cause. |
| **The battery doesn't reach the planned power** | Your *Maximum battery power* setting, your inverter's own limits, and your *Set charge/discharge power* action's units (W or kW). |
| **It charged from the grid on a dull morning** | Set the blueprint's *Nimbus solar-charge signal* input (step 4): a charge the plan takes from solar then runs as Self-Consume. |
| **It switches mode often** | The plan's `period0_hysteresis` attribute shows what Nimbus did each solve. A mode switch within 3 minutes of the last one is held back by the dwell. If it still switches more than you'd like, raise *Minimum dwell*. |
| **Something else is also controlling the battery** | Disable any other battery automation while Nimbus is armed (see below). |

If you're unsure, turn the arm switch off. That is always safe.

## Before you arm it: the checklist

The [control-readiness gate](setup-guide.md#control-readiness-gate) in the
setup guide lists everything to confirm before a supervised trial. The
blueprint covers the automation's side (explicit arming, a tested stop,
invalid and stale plans refused, a watchdog, a deadband and minimum dwell, a
defined fallback); the rest is your equipment's own protections, which must
stay in force whatever Nimbus asks for.

## Writing your own instead

You do not have to use the blueprint. If you write your own automation, keep
the same guarantees: a separate arm switch, a fallback on disarm and on any
plan that is unknown, failed or stale, a power cap, and a minimum dwell. The
blueprint's source is a readable reference:
[battery_dispatch.yaml](https://github.com/code-imstillalive/nimbus/blob/main/blueprints/automation/nimbus/battery_dispatch.yaml).
