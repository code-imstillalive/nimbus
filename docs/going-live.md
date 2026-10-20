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

| Action | What it must do | Typical example |
|---|---|---|
| **Enter Charge mode** | Put the inverter in forced/remote **charging** | remote-control mode on, command "charge" |
| **Enter Discharge mode** | Forced/remote **discharging** | remote-control mode on, command "discharge" |
| **Set charge/discharge power** | Write the power setpoint. Use `{{ power_kw }}`: always positive, kW, already capped at your maximum | `number.set_value` on the inverter's power setpoint |
| **Enter Self-Consume mode** | The inverter's own self-consumption mode: solar to the house first, spare solar into the battery | remote-control off / "self-consumption" mode |
| **Enter Hold mode** (optional) | Keep the battery idle while the grid supplies the house | remote mode with a stop / 0 kW command |
| **Keep-alive** (optional) | For inverters whose remote mode times out without a periodic command | the vendor's heartbeat |
| **Fallback** | What the inverter should do whenever Nimbus is **not** in control | normally the same as Self-Consume |

Mode actions run only **when the mode changes** (with the mode helper below);
the power setpoint and keep-alive run every cycle. So a mode action may
safely do several writes, and you are not re-writing the inverter's mode every
minute (which some Modbus integrations do not tolerate).

## Step 3: create two helpers

Settings → Devices & services → Helpers → Create helper:

1. **Toggle** named *Nimbus dispatch armed* → `input_boolean.nimbus_dispatch_armed`.
   This is the arm switch. Leave it **off** for now.
2. **Text** named *Nimbus dispatch mode* → `input_text.nimbus_dispatch_mode`.
   The automation writes the mode it set here (`CHG`, `DIS`, `SC`, `HOLD`,
   `FALLBACK`). Leave it empty.

## Step 4: import the blueprint and create the automation

1. Import the blueprint: Settings → Automations & scenes → Blueprints →
   **Import blueprint**, and paste:
   `https://github.com/code-imstillalive/nimbus/blob/main/blueprints/automation/nimbus/battery_dispatch.yaml`
2. Select **Nimbus battery dispatch** → **Create automation**, and fill in:
   - **Nimbus plan sensor:** `sensor.nimbus_solver_battery_forecast`;
   - **Nimbus hold signal:** `sensor.nimbus_solver_current_dispatch_hold`
     (only if you wrote a Hold action);
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
   - plan below it (−0.05 kW): **Charge** at the plan's kW;
   - idle with the grid supplying the house (hold signal, and a Hold action
     given): **Hold**;
   - otherwise: **Self-Consume**.
4. **Minimum dwell:** once in a mode, stay at least *Minimum dwell* (3 minutes)
   before switching to another, so the battery does not flip back and forth.
   Never delayed: a price spike, a switch straight between Charge and Discharge
   of *Reversal* kW (5) or more, or the fallback. The power setpoint within
   the current mode still follows the plan every cycle.
5. **Act:** run the mode's action if the mode changed; while charging or
   discharging, set the power (capped at your maximum); run the keep-alive.
6. **Record** the mode in the mode helper.

## Stopping, and going back

- **To stop now:** turn the arm switch off. The fallback runs.
- **To stop for good:** turn the arm switch off, then disable or delete the
  automation. Nimbus keeps planning in shadow.
- **If you had a previous battery automation:** keep it disabled while Nimbus
  is armed (two controllers fighting over one inverter is the most common way
  to get erratic behaviour), and re-enable it only after disarming Nimbus.

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
