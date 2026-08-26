# Setting up Nimbus — a plain-English guide

This is the "explain it like I'm not a developer" version. If you want the
exhaustive field-by-field table, see
[`configuration-reference.md`](configuration-reference.md) — that one is
for looking things up. This one is for reading start to finish the first
time you set Nimbus up.

Nothing here is simplified to the point of being wrong — every field name,
default, and gotcha below matches the real code. It's just written the way
you'd explain it to a mate over the fence, not the way you'd write a spec.

## What you're actually installing

Nimbus does two jobs, and you can use either one on its own:

1. **It watches your power sensors and learns your household's habits.**
   After a couple of weeks it can tell you "the pool pump usually draws
   1.2kW between 8am and 3pm" or "the whole house typically pulls 2kW
   overnight" — without you typing any of that in. This is the
   **Forecaster**.
2. **It can plan when to charge/discharge your battery and when to
   import/export from the grid**, based on real prices and those
   forecasts, to minimise your bill. This is the **Solver**. It currently
   only *plans* and *reports* — as of this writing it does not physically
   push commands to your inverter. Think of it as a very well-informed
   spreadsheet that updates itself every minute, not an autopilot (yet).

You can install Nimbus and use just the Forecaster and ignore the Solver
completely. Nothing breaks if you skip it.

## Before you start, know these three things

- **Nimbus doesn't care what brand of inverter/battery/retailer you have.**
  Everywhere it needs a number (your battery's charge level, your current
  electricity price, your solar output) it asks *you* to point it at
  whatever sensor already has that number in Home Assistant. If your
  inverter's own integration already shows you a battery percentage, a
  solar-power reading, and so on, Nimbus can use them.
- **You need Home Assistant's Recorder actually recording your sensors**
  for a few weeks before the learning part gets good. Day one, it'll work
  but be rough — that's normal, not broken.
- **Every screen tells you what's optional.** As of this version, any
  field that's genuinely required to make that screen work is marked with
  a 🔴 next to its name, and the screen's own description reminds you
  "🔴 = required." Nothing else on that screen needs filling in unless you
  want to.

## Step 1 — Install it

1. In Home Assistant, open **HACS**.
2. Click the three-dot menu (top right) → **Custom repositories**.
3. Paste in `https://github.com/code-imstillalive/nimbus`, set the category
   to **Integration**, add it.
4. Find "Nimbus" in HACS and install it.
5. Restart Home Assistant (a normal integration restart, not your router).

## Step 2 — Add it

1. **Settings → Devices & Services → Add Integration** → search "Nimbus".
2. There's nothing to fill in on this screen — it just creates the Nimbus
   "hub" (a device that everything else attaches to). Click through.

You'll land on the Nimbus device page. You'll see two buttons that matter:
**"+ Add"** (for adding things you want forecast — appliances, batteries,
etc.) and **Configure** (for settings that apply to everything at once).
Do **Configure** first.

## Step 3 — Configure: the shared settings

Click **Configure** on the Nimbus device page. You'll see three options.

### 3a. Forecaster settings

This screen has **no required fields at all** — you can click straight
through with everything blank and the Forecaster still works, just a
little less accurately. Fill in whichever of these you actually have a
sensor for:

- **Temperature sensor / Temperature forecast sensor** — if it's hot,
  your AC probably runs more. Pointing Nimbus at a real thermometer (and
  ideally a weather forecast too) lets it factor that in.
- **Humidity sensor** — same idea, for anything humidity-sensitive.
- **Curtailment switch** — only relevant if you have a specific appliance
  you run purely to soak up solar that would otherwise be wasted (a pool
  heater is the classic case). Most people leave this blank.
- **Battery / Grid / Solar power sensors** — real, measured values (not
  a target or a plan — an actual live reading). These just give every
  appliance's model a bit of "what else is going on right now" context.
  Leave blank if you don't have them handy; nothing requires this.
- **Forecast horizon, retrain hour, training window** — sensible defaults
  are pre-filled. Only touch these if you have a specific reason to
  (e.g. you want retraining to happen at 3am instead of whatever the
  default is, so it doesn't compete with something else on your system).

### 3b. Solver settings — only if you want the planning/optimisation part

**Skip this entire step if you only want forecasts.** If you do want the
battery/grid planner, this is a 3-screen mini-wizard, and unlike the
Forecaster screen, several fields here really are required (marked 🔴):

**Screen 1 — Battery**
- 🔴 **Battery State of Charge sensor** — whatever sensor already shows
  you your battery's charge percentage. This is the one thing the Solver
  genuinely can't guess.
- Everything about your battery's *capacity*, *max charge/discharge
  power*, and *efficiency* is **not** on this screen — see "the dials you
  tune afterwards" below.

**Screen 2 — Grid prices**
- 🔴 **Live import (buy) price sensor** and 🔴 **Live export (sell) price
  sensor** — whatever your retailer's own Home Assistant integration
  already publishes as your current buy/sell rate. If you're not sure,
  check Developer Tools → States for something like
  `sensor.<retailer>_price` or similar.
- You can optionally add a second or third price source per direction if
  you want Nimbus to blend more than one forecast together — most people
  leave these blank.

**Screen 3 — Solar & load forecasts**
- 🔴 **Solar generation forecast sensor** — Solcast, Open-Meteo, or a
  Nimbus forecast you've set up yourself for your own solar (see "Adding
  things to forecast" below).
- 🔴 **Household load forecast sensor** — this is where you point at
  Nimbus's own whole-house forecast, once you've added your appliances
  in Step 4. Chicken-and-egg is fine: come back to this screen after
  Step 4 if you haven't added any loads yet.
- The rest of this screen is optional extras for people who want more
  detail (summing individual circuits instead of one whole-house number,
  a sanity-check sensor, historical P2P settlement data, and so on). Skip
  them on a first pass.

### 3c. Switchboard — purely cosmetic, for the topology diagram

Everything here is optional, and it's just for a visual diagram of your
system (grid → battery → loads). If you don't care about that picture,
skip this screen entirely — nothing else depends on it.

## Step 4 — Add the things you want forecast

Back on the Nimbus device page, click **"+ Add"**. You'll get a menu —
the two you'll actually use most are:

### Load — "forecast one appliance or circuit"

Use this for anything with its own power sensor you want Nimbus to learn:
a pool pump, hot water system, EV charger, an AC zone, whatever your
switchboard breaks out individually.

- 🔴 **Power sensor to learn from** — the only required field. Point it
  at that circuit's real power sensor.
- Two optional extras, only worth touching if that appliance runs on a
  strict daily timer (e.g. a pool pump on 8am–3pm): a start/end hour, and
  roughly how much power it draws while running. Leave both blank for
  anything without a fixed schedule.

You can add as many Loads as you have circuits — one household running
this has 18 of them. No restart needed between each one.

### Power Signal — "forecast the whole battery/solar/grid, not one appliance"

Use this if you want Nimbus to forecast your *battery*, *solar*, or
*grid* power itself (as opposed to a single appliance). This is what
feeds the Solver's Solar/Load fields from Step 3b, and it's also how the
topology diagram knows which sensor is which.

- 🔴 **Power sensor to forecast** — a real, measured whole-system sensor
  (total battery power, total solar/DC power, or total grid meter power).
- **Role** — a simple dropdown: Battery / Solar / Grid / Other. This just
  tells the diagram what it's looking at; it doesn't change the
  forecasting itself.

### The other three ("+ Add" menu) — purely for the diagram

**Power Source**, **PV String**, and **Battery Tower** don't forecast
anything — they only exist to draw an accurate picture of your physical
setup (which inverter, which PV strings, which battery packs) on the
topology dashboard card. If you don't care about that diagram, you can
ignore all three completely; nothing else depends on them. If you do want
the diagram, Power Source's **Name** field is the only required one
across all three — everything else is "fill in whatever your hardware
happens to expose."

## Step 5 — Check it actually worked

Go to **Developer Tools → States** and look for:

- `sensor.nimbus_<something>_forecast` for each Load/Power Signal you
  added — its `forecast` attribute should eventually be a real list of
  time/value pairs (give it a little while after the first retrain).
- If you set up the Solver: `sensor.nimbus_solver_config` should say
  `configured`. If it doesn't, go back to Configure → Solver settings and
  check for a screen you skipped past — the 🔴 markers make it obvious
  which fields are still empty.
- `sensor.nimbus_solver_battery_forecast`'s `status` attribute should
  read `optimal` once it's running (give it a minute after everything's
  configured).

If something's stuck on `unknown`, the most common reason is simply "not
enough history yet" — check the entity's `model_trained_at` attribute;
`null` means it hasn't trained for the first time yet, which is expected
for a day or so on a brand-new install.

## The dials you tune afterwards (no wizard needed)

Once the Solver is running, everything to do with your actual battery
hardware — its size, its max charge/discharge speed, round-trip
efficiency, your grid connection limits, and every cost/price setting —
lives as ordinary, editable Home Assistant `number.` entities (e.g.
`number.nimbus_solver_battery_capacity_kwh`), not buried in the wizard.
You'll get a notification pointing you here the moment the hub is
created, because these all start at a safe-but-useless placeholder
minimum until you set your real numbers. Just edit them on your
dashboard like any other input — no need to reopen Configure.

## Three things that confuse almost everyone at first

**"Battery" and "Grid" show up in four different places, and that's on
purpose.** The Forecaster's battery/grid/solar sensors (Step 3a) are just
context clues for an appliance's model. A Power Signal with role
"Battery" (Step 4) is Nimbus *forecasting* your battery's future power.
The Solver's battery sensor (Step 3b) is your battery's *charge
percentage*, not its power. The Switchboard's battery sensor (Step 3c) is
only for the diagram's colours. None of these are duplicates you can
merge — they're genuinely different jobs.

**If you fill in the "individual circuit sensors to sum" list, it wins,
even by accident.** Solver settings has both a single "Household load
forecast sensor" field and an optional list of individual sensors to add
together. The instant that list has even one entry in it, the Solver
uses the list and silently ignores the single-sensor field — with no
warning. If you only want one source, leave that list completely empty.

**Never point "Household load forecast sensor" at Nimbus's own
whole-house total.** That total (`sensor.nimbus_household_load_total_forecast`)
is something the Solver *produces*, not a valid thing to feed back into
itself. Point it at one of your own Load/Power Signal forecasts instead.
Nimbus now catches the worst version of this mistake automatically and
tells you what went wrong, but picking the right sensor the first time
avoids it entirely.

## If something's actually broken, not just confusing

Open a GitHub issue at
[code-imstillalive/nimbus](https://github.com/code-imstillalive/nimbus/issues).
This project is still under active development — rough edges are
expected, and real bugs get fixed fast when they're reported with
specifics (which entity, what you expected, what you saw).
