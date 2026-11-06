# Dashboards — Control Panel, Power Flow, Regret, Topology and Forecaster cards

Four ready-made Lovelace cards ship with this integration (issue #364,
Mark Purcell) — no `www/` file copy, no manual Settings -> Dashboards ->
Resources step. Installing/updating Nimbus via HACS is the entire setup
step; see `custom_components/nimbus_load/frontend.py` for how they're
served and registered. All four are findable in the card picker by
searching "nimbus" (issue #519) — each `type` starts with `nimbus-` and
each picker entry's name says Nimbus.

**Registering a card is not the same as it reaching a screen (issue
#550/#552, Mark Purcell)**: a household that never hand-authors dashboard
YAML has no way to discover a card exists just because HACS installed
it — confirmed live on Mark's own install, where the Topology card
shipped for several releases before ever reaching a view. The
sections below (one per card) are the field reference; the full
copy-paste dashboard right here is the fastest path to actually seeing
all four cards on screen.

## The standard Nimbus dashboard: one action, then paste

Nimbus does **not** create, update or rewrite any dashboard. Your dashboards
are yours: Nimbus only ever **returns** the standard one when you ask for it
(HACS store review, hacs/default#12099).

To get it:

1. **Settings → Dashboards → + Add Dashboard → New dashboard from scratch.**
   Name it as you like (for example **Nimbus**), then open it.
2. **Developer Tools → Actions**, choose **Get the Nimbus dashboard**
   (`nimbus_load.get_dashboard`) and select **Perform action**. The response is
   this install's dashboard, with every entity already filled in.
3. Copy the response. On your new dashboard select **Edit → ⋮ → Raw
   configuration editor**, replace its contents with the response, and save.

It holds six tabs, in this order:

| Tab | Card | What it shows |
|---|---|---|
| **Forecaster** | `custom:nimbus-forecast-card` ×2 | Power Signals and Load Forecasts (needs ApexCharts Card) |
| **Solver** | standard Home Assistant cards | solve status, plan against the measured battery, money, EPR and regret, the risk sliders, and every Solver setting grouped into **Basic settings**, **P2P (optional)** and **Advanced settings** (issue #1594) |
| **Topology** | `custom:nimbus-topology-card` | the site diagram, discovered from your Nimbus config |
| **Power Flow** | `custom:nimbus-flow-card` | a live animated flow through your inverters, strings and battery towers to the house and grid, the price being paid or earned now, whether the plan is being followed, the circuits drawing now, the Control Panel's forecast table beside it and today's totals |
| **Control Panel** | `custom:nimbus-dispatch-card-v4` | the plan and live dispatch; battery and solar follow the Solver settings |
| **Regret** | `custom:nimbus-regret-card` ×3 | yesterday's dispatch and the two days before, each against the best possible plan |
| **Help** | standard Home Assistant cards | setting Nimbus up and what every figure means (issue #1823) |

Every tab has a title and no icon, and is a 4-column **sections** view, so you
can add your own sections and cards. After an update that changes a tab, run
the action again and paste the tab you want; nothing changes until you do.

A dashboard an earlier Nimbus release created or filled is left exactly as it
is.

<a id="full-three-view-nimbus-dashboard-copy-paste"></a>

### Keeping the tabs up to date: the Nimbus Tabs card

A release can change a standard tab (for example a new setting on the Solver
tab). The **Nimbus Tabs** card (`custom:nimbus-tabs-card`, first on the Help
tab; or add it from the card picker by searching "nimbus") brings those
changes to your dashboard without any YAML.

**Tabs pasted before 0.94.507 don't have it**, so there is no *Check for
updates* button on your Help tab. Add the card once: **Edit dashboard → + Add
card**, search **nimbus**, pick **Nimbus Tabs** and save (or add a Manual card
with `type: custom:nimbus-tabs-card`). The current Help tab it then offers has
the card built in. Until then, **Settings → Repairs** shows a reminder that
clears itself once the card is on a dashboard.

Then:

1. Press **Check for updates**. The card compares this dashboard's tabs with
   the installed release and lists each as up to date, changed or missing.
2. Press **Add updated tabs beside mine**. Each changed tab is added right
   after yours as **"<Tab> (new)"**; a missing tab is added under its own
   name. **Your own tabs are never changed or removed**, and pressing it
   again replaces only the "(new)" copies it added.
3. Look at the new tab; when you're happy, delete your old one (and rename
   the new one if you like). The card still knows it as yours after the
   rename, though its web address keeps the "-new" ending. A tab you made
   by hand with no web address counts as yours when it has the standard
   name (Forecaster, Solver, ...).

It works on the dashboard the card is on, needs an admin login (as any
dashboard edit does), and saves the same way the dashboard editor does:
from your browser, on your click. Nimbus itself still writes no dashboard.
A YAML-mode dashboard can't be changed this way; the card says so.

## Full four-view "Nimbus" dashboard (copy-paste)

One complete dashboard — Control Panel / Regret / Topology / Forecaster,
one view each — using the same `sections` layout and `max_columns: 4` this doc
already recommends per-card below (see the Control Panel section's own
"Sections-view width gotcha" for why). Replace every
`sensor.your_*`/`input_select.nimbus_dispatch_mode`/
`input_boolean.nimbus_live_dispatch_armed` placeholder with your own
real entity IDs — none of these are Nimbus's own hub-level entities, see
each card's own field table below for what's required vs. optional.
Add this as a new dashboard (Settings -> Dashboards -> **+ Add
Dashboard** -> **New dashboard from scratch**, then Edit -> raw
configuration editor) or a new view on an existing one:

```yaml
title: Nimbus
views:
  - title: Control Panel
    path: control
    type: sections
    max_columns: 4
    sections:
      - type: grid
        column_span: 4
        cards:
          - type: custom:nimbus-dispatch-card-v4
            mode_select_entity: input_select.nimbus_dispatch_mode
            armed_entity: input_boolean.nimbus_live_dispatch_armed
            battery_power_entity: sensor.your_battery_power_sensor
            battery_soc_entity: sensor.your_battery_soc_sensor
            grid_power_entity: sensor.your_grid_meter_sensor
            solar_power_entity: sensor.your_solar_power_sensor
            grid_options:
              columns: full
              rows: auto
  - title: Regret
    path: regret
    type: sections
    max_columns: 4
    sections:
      - type: grid
        column_span: 4
        cards:
          - type: heading
            heading: Yesterday
          - type: custom:nimbus-regret-card
            days_ago: 1
            grid_options:
              columns: full
              rows: auto
      - type: grid
        column_span: 4
        cards:
          - type: heading
            heading: 2 Days Ago
          - type: custom:nimbus-regret-card
            days_ago: 2
            grid_options:
              columns: full
              rows: auto
  - title: Topology
    path: topology
    type: sections
    max_columns: 4
    sections:
      - type: grid
        column_span: 4
        cards:
          - type: heading
            heading: Nimbus Topology
            icon: mdi:sitemap
          - type: custom:nimbus-topology-card
            switchboard: {}
            inverters: []
            grid_options:
              columns: full
              rows: auto
  - title: Forecaster
    path: forecaster
    type: sections
    max_columns: 4
    sections:
      - type: grid
        column_span: 4
        cards:
          - type: custom:nimbus-forecast-card
            chart: signals
            grid_options:
              columns: full
              rows: auto
          - type: custom:nimbus-forecast-card
            chart: loads
            grid_options:
              columns: full
              rows: auto
```

The Topology view's minimal `switchboard: {}`/`inverters: []` config is
correct as-is for a wizard-configured household (issue #551 — both
default and auto-fill from `sensor.nimbus_topology_config`); see the
Topology card's own section below for hand-authoring instead of running
the wizard, and its empty-state banner (issue #553) if the wizard
hasn't been run yet.

## Nimbus Dispatch Card (`custom:nimbus-dispatch-card-v4`) — "Control Panel"

A single card: mode selector + kill switch, a SoC gauge with live
reasoning text, risk-aversion sliders, a 96h dispatch timeline (plan vs.
actual), a forecast-interval table, and Economics & Quality stats.

This card has **no hardcoded entities** — every real sensor/helper it
reads is a config field, same "no hardcoding" convention the rest of
this project follows. Every field below is optional in the sense that
the card won't crash without it, but the CORE fields are what make the
card actually useful; leaving them unset just shows "Idle"/zero/unknown
throughout.

```yaml
type: custom:nimbus-dispatch-card-v4
mode_select_entity: input_select.nimbus_dispatch_mode
armed_entity: input_boolean.nimbus_live_dispatch_armed
battery_power_entity: sensor.your_battery_power_sensor
battery_soc_entity: sensor.your_battery_soc_sensor
grid_power_entity: sensor.your_grid_meter_sensor
solar_power_entity: sensor.your_solar_power_sensor
# Optional -- omit entirely if you don't have one:
ev_charger_power_entity: sensor.your_ev_charger_power_sensor
p2p_threshold_entity: sensor.your_p2p_nightly_volume_threshold_kwh
# Optional -- arbitrary-length list of extra status chips in the
# footer (e.g. a cron-writer health-check entity per node). Each entry
# needs `entity` (must report state "ok" to show green); `label` is
# optional and defaults to the entity_id.
health_checks:
  - entity: sensor.job_health_writer_1
    label: Writer 1
  - entity: sensor.job_health_writer_2
    label: Writer 2
```

**Sections-view width gotcha (issue #400, Mark Purcell):** if this card sits inside a Home Assistant **Sections** view (`type: sections`) with `grid_options: {columns: full, rows: auto}`, the view's own `max_columns` setting — not anything in the card's own CSS — controls how much real width the card's section ever gets. HA's own default/common choice is `max_columns: 2`, which silently caps a single `column_span: 4` section too narrow for this card's landscape (two-column) layout to ever reach a genuinely comfortable width, regardless of container-query breakpoints. Confirmed live: raising the view's `max_columns` from 2 to 4 (nothing else changed) took this card from a cramped, columns-cut-off forecast table to all 12 columns rendering cleanly with no scrolling needed. If the landscape layout looks cramped or the forecast table seems to be missing columns, check this setting before assuming it's a card bug:

```yaml
title: Control Panel
path: control
type: sections
max_columns: 4  # not the HA default of 2 -- see above
sections:
  - type: grid
    column_span: 4
    cards:
      - type: custom:nimbus-dispatch-card-v4
        # ... your fields here ...
        grid_options:
          columns: full
          rows: auto
```

**Core fields:**

| Field | What it needs |
|---|---|
| `mode_select_entity` | An `input_select` helper with options `Automatic`, `Charge`, `Discharge`, `Preserve`, `Self-Consume`. The card writes to it (mode chips) and reads it (current mode). |
| `armed_entity` | An `input_boolean` helper acting as the master kill switch. When off, the card shows real live measured state and ignores whatever mode is selected — matches the "everything off means nothing is followed" behaviour any live-dispatch automation gating on this same entity should implement. |
| `battery_power_entity` | Real measured battery power (kW, signed: positive = discharging). Also used for the 18h "Actual" history line on the timeline. If your own sensor reports the opposite convention (positive = charging — a real SigEnergy-install case, see `battery_power_positive_is_charge` below), point this at the **raw hardware sensor** anyway; the card auto-corrects the sign, no template-sensor workaround needed. **⚠️ Upgrading from before v0.94.131:** if you built a manual sign-inverting template sensor as a workaround for [#388](https://github.com/code-imstillalive/nimbus/issues/388) (the only fix available at the time), **remove it and repoint this field at the raw hardware sensor once you're on v0.94.131+.** Leaving the old template helper in place after upgrading double-inverts the sign — the card's own auto-detect (`_battSign()`) assumes `battery_power_entity` is the raw, unconverted sensor; stacking it on top of an already-inverted helper silently reintroduces #388's exact original bug (CHARGING shown for a battery that's actually discharging), with no error or warning — confirmed live, [#421](https://github.com/code-imstillalive/nimbus/issues/421). |
| `battery_soc_entity` | Real measured battery state of charge (%). |
| `grid_power_entity` | Real measured grid power (kW, signed: positive = importing). |
| `solar_power_entity` | Real measured solar power. Native W or kW both work — the card reads the entity's own `unit_of_measurement` and converts. |

**Optional fields:** `ev_charger_power_entity` (adds an "+ EV CHARGING" note to the status line when the load exceeds 0.05kW), `p2p_threshold_entity` (adds a "Tonight's P2P Threshold" stat), `health_checks` (footer status chips — omit for none), `battery_power_positive_is_charge` (nimbus issue #388 — a `true`/`false` override for the sign the card applies to `battery_power_entity`; almost never needed, since the card already auto-detects this from the Solver's own `solver_battery_power_positive_is_charge` setting on `sensor.nimbus_solver_config` — the same flag a SigEnergy-style install already sets during the Solver wizard's Battery step. Only set this explicitly if that sensor is unavailable for some reason).

**Always-on, not configurable** (these are Nimbus's own stable, hub-level entity names, not household-specific): the Solver's own `number.nimbus_solver_*` tuning/risk entities, `sensor.nimbus_solver_battery_forecast` (the plan itself), and `sensor.nimbus_solver_quality_report` (yesterday's EPR/P2P stats).

## Nimbus Power Flow Card (`custom:nimbus-flow-card`) — "Power Flow"

What is happening right now, as a live animated flow. Dots run along each
line, faster for more power, coloured by where the energy comes from (solar
gold, battery green, grid blue).

- **Layout:** Grid on the inverters' row between them (beside a single
  inverter), House in the centre below, circuits fanned out under House.
- **Your site, drawn from Nimbus's setup:** each inverter (a Power Source),
  its PV strings above it while they produce, and its battery towers beside
  it with their level, health and temperature, feeding the house and the
  grid. Without inverters set up, one combined "Solar & battery" node.
  Towers sit in a column on their inverter's side.
- **Your EV** (a battery participant, darker green) is one node off the house, with its
  charge level in the ring and whether it is charging, supplying or idle.
- **The price now,** on the grid: what is being paid or earned this period,
  marked P2P when the P2P rate applies.
- **On or off plan:** the battery is checked against the plan's current
  period. Off by more than about 1.5 kW (or a quarter of the plan) for two
  minutes, the badge turns amber and a planned line that isn't happening
  shows as a grey dashed ghost.
- **Solar against forecast,** while the plan expects solar.
- **The circuits drawing now:** small bubbles under House, one per circuit
  drawing over 5 W (the Topology card's own mark for "active"), up to the
  twelve largest, each under the name you gave its load or power signal,
  fanned below House (up to a half circle as they grow). A load or power
  signal set as **part of** another circuit (setup guide §12) is listed under
  that circuit's bubble with its power, then "other" for the rest, instead of
  as a bubble of its own.
- **The plan table beside it** is the Control Panel card's own forecast
  table (`custom:nimbus-dispatch-card-v4` with `table_only: true`), so the
  two always match.
- **Today's totals** from the Switchboard's daily sensors (solar, house,
  import, export, battery in and out); a tile without a sensor is left out.

It has no settings: everything comes from Nimbus. Add it from the card
picker ("Nimbus Power Flow") or use the standard **Power Flow** tab.

## Nimbus Regret Card (`custom:nimbus-regret-card`) — "Regret"

Reconstructs a real day's dispatch-vs-oracle comparison: three
trajectories (Reference/idle, Achieved/real, Star/oracle) plus an
hourly regret bar chart. Calls the `nimbus_load.compute_quality_report`
service directly — no config needed beyond which day to show:

```yaml
type: custom:nimbus-regret-card
days_ago: 1   # optional, default 1 = yesterday. 2/3/etc. tile a trend view.
title: "Custom title"  # optional, defaults to "Dispatch Regret — N days ago"
```

A typical "Regret" view stacks a few of these at different `days_ago`
values:

```yaml
type: sections
path: regret
title: Regret
sections:
  - type: grid
    cards:
      - type: heading
        heading: Yesterday
      - type: custom:nimbus-regret-card
        days_ago: 1
  - type: grid
    cards:
      - type: heading
        heading: 2 Days Ago
      - type: custom:nimbus-regret-card
        days_ago: 2
```

## Nimbus Forecaster Card (`custom:nimbus-forecast-card`) — "Forecaster"

The Forecaster's own charts (issue #1529): **"Nimbus Power Signals"** and
**"Nimbus Load Forecasts"**, the reference household's own Forecaster view,
built automatically for any install. Needs **ApexCharts Card**
(`apexcharts-card`, from HACS → Frontend); without it the card says so.

**A new install gets it without doing anything** — see "The Nimbus
dashboard on a new install" above. On an install that already had a Nimbus
dashboard, add it yourself: a **sections** view (4 columns) with one
full-width section holding the two cards, `chart: signals` and
`chart: loads`, each with `grid_options: {columns: full, rows: auto}`.

Nothing is configured: every series is discovered from this install's own
entities each time the dashboard loads, so a new Load appears by itself.

- **Power Signals chart:** each Power Signal's measured history, forecast
  and lower/upper bounds (Battery green, Grid blue, Solar orange, classified
  by the signal's role or entity name), the temperature and humidity
  forecasts if you have those Power Signals, and the Solver's proposed
  battery, grid import/export and SoC from
  `sensor.nimbus_solver_battery_forecast`.
- **Load Forecasts chart:** each Load's measured history and forecast, each
  circuit in its own stable colour, pool and hot-water circuits drawn bold
  and filled, plus the whole-house forecast the Solver plans with (white).
- Measured lines come from each forecast's own source sensor; a source in W
  is converted to kW. Axes size themselves to your system.

To add it to a view of your own, add it **as a card** in a sections view
(**+ Add card** → Manual), one card per chart. The card sizes itself full
width; `grid_options` is only needed to override that:

```yaml
type: custom:nimbus-forecast-card
chart: signals   # optional: both (default), signals or loads
grid_options:
  columns: full
  rows: auto
```

Paste it as a card, not as a section, and do not wrap it in a `type: grid`
card: inside a card editor `type: grid` is Home Assistant's Grid card, which
lays its children out in narrow columns. For a section that wide, set the
view's max columns to 4.

## Nimbus Topology Card (`custom:nimbus-topology-card`) — "Topology"

A real SVG switchboard power-flow diagram: switchboard, inverters (each
with its own PV strings + battery towers), auto-discovered Loads, and a
whole-house readout, with live arrows/coloring showing which direction
power is actually flowing right now.

Step-by-step setup, including which entry draws which box and what a
fresh install shows, is in
[setup guide §16](setup-guide.md#16-the-topology-diagram). This section is
the card reference.

The card draws its inverters from `sensor.nimbus_topology_config`, which
publishes the hub's **Power Source**, **PV String** and **Battery Tower**
entries (added with the integration page's **+ Add** buttons, not the
Configure menu). When at least one Power Source exists, that live data
wholesale-overrides the card's own `inverters`. When none of the three
entry types exists, the sensor publishes a stand-in inverter named
"Nimbus" from the Solver's measured battery and solar power sensors
(when either is set) and its SoC sensor, plus one per Battery Participant (nimbus issue #575). Add
any real entry and the stand-in is dropped. A PV String or Battery Tower
is drawn only under the Power Source it is assigned to.

`switchboard` and `inverters` are both **optional** (nimbus issue #551 —
a card added directly from HA's own card picker, with no config at all,
no longer errors; both default to `{}`/`[]`), so the minimal config is
just:

```yaml
type: custom:nimbus-topology-card
switchboard: {}
inverters: []
```

Every Nimbus **Load** subentry (HWS, pool, an individual circuit
breaker — anything added via the hub's own "+ Add" → Load) appears on
the diagram automatically, with no config at all — added the moment its
forecast sensor exists, removed the moment it doesn't. The card pairs
each `sensor.nimbus_<your_sensor_name>_forecast` with the source sensor
named inside it for the live reading, so keep the default entity ID.

**Power Signals on this card.** Only two roles are read, and neither
draws a box of its own:

- A Power Signal with Role **Grid** supplies the Grid box's live power
  and arrow direction (positive = importing).
- A Power Signal with Role **Battery** supplies the battery's share of
  the switchboard colour (positive = discharging).

Both win over any `switchboard.grid_meter` / `switchboard.battery_power`
in the card YAML. Where neither a role nor the card names one, the
Grid box uses the **Grid sensor** and **Battery sensor** from the
Forecaster step, so a new install with its signals left at Role
**Other** still shows the grid. Roles **Solar**, **Other**, **Temperature** and
**Humidity** are not read here; solar comes from PV Strings. The
whole-house Power Signal from setup guide §5 is therefore not drawn
either. It appears on the Forecaster card instead, as the white Whole
House line once it is the Solver's household load forecast, and every
other Power Signal appears on the Forecaster's Power Signals chart.

**Whole-house headline.** The house's live and forecast power show at
the top left, with the daily solar, battery and house-load kWh from
**Configure → Topology diagram settings** beside them. They come from
the Solver's whole-house sensor and household load forecast, so the
generated dashboard shows them with nothing to add. A `whole_house`
block in the card's YAML replaces that choice:

```yaml
type: custom:nimbus-topology-card
switchboard: {}
inverters: []
whole_house:
  live: sensor.<your_sensor_name>
  forecast: sensor.nimbus_<your_sensor_name>_forecast
```

**Battery participants** (an EV) are drawn as their own source and
battery beside your inverters, with their SoC and capacity.

**Controllable loads** (a heat pump, pool pump, hot water or appliance
under Nimbus's control) get a dashed row each after the Loads: whether
Nimbus has commanded it on (or its live power, where it has a power
sensor) and today's delivered energy against its target. A row lights
up while the load is commanded on.

**Prices** on the Grid box come from the Solver's import and export
price sensors (`sensor.nimbus_solver_config`). The Topology diagram
settings price fields are a fallback for an install without them.

**Empty state (nimbus issue #553):** when there is no inverter to draw —
no Power Source and no stand-in, or `sensor.nimbus_topology_config`
doesn't exist yet (an older integration version, or the entity
disabled) — the card shows a banner reading *"No topology configured.
Nimbus hub → Configure → add a Power Source, PV Strings and Battery
Towers; this card fills in automatically."* Despite the wording, those
are **+ Add** buttons on the integration page. Load tiles and the Grid
box still draw underneath the banner, so the card stays useful on a
loads-only install.

If you'd rather hand-author the topology instead of running the wizard
(or are still migrating a static file from a hand-copied `www/`
install), `switchboard`/`inverters` accept the same shape documented in
`docs/real-world-integration/files/topology_map.yaml` — read that file
for the full field reference (grid meter, import/export price, per-
inverter battery/DC power, PV strings, battery tower ID prefixes), not
to copy its entity IDs, which are one specific household's own hardware.

**Renamed in issue #519** (was `switchboard-topology-card` / "Topology
Card" — neither said "Nimbus", so it wasn't findable by searching the
card picker the way the other two cards are). The old
`custom:switchboard-topology-card` type is kept registered as a
back-compat alias for one or two releases; update to
`custom:nimbus-topology-card` when convenient.

## Migrating from a hand-copied `www/` install

If any of these cards was previously added by hand-copying the JS file
into a `www/` folder and registering it as a Lovelace resource, remove
that resource and the file once this integration's own bundled version
is active — having both loaded on the same dashboard raises a real
`customElements.define()` collision (the browser refuses to register
the same custom element tag twice). Update the view's card config to
the field names above; the entity_ids themselves don't need to change.
