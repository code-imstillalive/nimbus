# Dashboards — Control Panel, Regret, and Topology cards

Three ready-made Lovelace cards ship with this integration (issue #364,
Mark Purcell) — no `www/` file copy, no manual Settings -> Dashboards ->
Resources step. Installing/updating Nimbus via HACS is the entire setup
step; see `custom_components/nimbus_load/frontend.py` for how they're
served and registered. All three are findable in the card picker by
searching "nimbus" (issue #519) — each `type` starts with `nimbus-` and
each picker entry's name says Nimbus.

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

## Nimbus Topology Card (`custom:nimbus-topology-card`) — "Topology"

A real SVG switchboard power-flow diagram: switchboard, inverters (each
with its own PV strings + battery towers), auto-discovered Loads, and a
whole-house readout, with live arrows/coloring showing which direction
power is actually flowing right now.

Auto-discovers almost everything once the Nimbus hub's own Topology
wizard (hub → Configure → Power Source / PV String / Battery Tower
steps) has at least one Power Source subentry configured — its live
data (`sensor.nimbus_topology_config`) wholesale-overrides whatever the
card's own config says. `switchboard` and `inverters` are still
**required keys in the card config itself** (the card throws without
them), even though their actual content gets replaced by the live
wizard data — so the minimal config for a wizard-configured household
is:

```yaml
type: custom:nimbus-topology-card
switchboard: {}
inverters: []
```

Every Nimbus **Load** subentry (HWS, pool, an individual circuit
breaker — anything added via the hub's own "+ Add" → Load) appears on
the diagram automatically, with no config at all — added the moment its
forecast sensor exists, removed the moment it doesn't.

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
