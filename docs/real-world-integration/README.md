# Nimbus Solver — real-world integration reference

The rest of this repo (`solver/network.py`, `solver/elements.py`,
`solver/lp.py`, `ml/gbrt.py`, `ml/features.py`, `ml/model.py`,
`coordinator.py`, `flows/`) is the actual `nimbus_load` custom_component
— the LP Solver and ML Forecaster themselves. Pure Python, zero Home
Assistant dependencies, hardware-agnostic by design.

What that code doesn't show is **how it's actually wired into a real,
live Home Assistant install**. That wiring lives in a separate, much
bigger, private repo (`116KAT-HA-AI`) that also holds this household's
own security-system automations, personal dashboard config, and a very
long running session log — not something to open up wholesale just to
see how the Solver is deployed. This folder is a targeted export
instead: the real files that show the forecaster, the topology
dashboard, and the whole Solver setup in practice, pulled straight out
of that live repo.

These are reference copies, not a working standalone package — every
file below assumes it's running on that specific system, reading that
system's own real entity IDs. That's deliberate: the point is to show
the real, live wiring, not a sanitized toy example.

## `files/nimbus_solver_forecast_writer.py` — the whole Solver setup

This is the actual glue: a plain host cron script (runs every minute,
no HA restart ever needed for a change here) that reads live sensors,
builds `GridConfig`/`BatteryConfig`/`SolarConfig`/`LoadConfig` from
them, calls this repo's own `network.build_plan()`, and pushes the
result to `sensor.nimbus_solver_battery_forecast`. Nimbus itself never
writes to Modbus or touches the battery — this stays purely
observational/shadow-mode.

Two things worth knowing before reading it:

- It reads its economic settings (risk aversion, P2P bonus, battery
  capacity, etc.) live from `sensor.nimbus_solver_config`, itself
  sourced from the `number.nimbus_solver_*` dashboard entities you can
  see in `custom_components/nimbus_load/number.py` — so a household
  can tune these from a dashboard slider, not just the config-flow
  wizard.
- Grep for `has_localvolts` — every branch gated on it is genuinely
  this-household-specific (real LocalVolts P2P pricing, a real bill-
  verified TOU network tariff table, a real 5pm/midnight/7am cost
  schedule). The `else:` fallback branches next to each one are the
  portable path: a household's own configured import/export price
  sensor, held flat, no assumptions about retailer/region/tariff
  structure. That's the split to look at for "what would this need to
  look like for someone who isn't on this specific setup."

## `files/topology-card-v4.js` + `files/topology_map.yaml` + `files/lovelace_build_topology_dashboard.py`

A from-scratch, vanilla-JS custom Lovelace card (`switchboard-topology-
card`, no framework) that renders a live SVG power-flow diagram —
Switchboard bus, Inverters, PV strings, battery towers, Grid, and every
Nimbus Load, with live proportional color-mixing showing what's
actually supplying the bus right now.

`topology_map.yaml` is the real, hand-confirmed physical wiring facts
(which PV strings are active, which battery towers belong to which
inverter, which entities are the real measurements vs. HAEO's own
plan/forecast sensors) — genuinely can't be inferred from sensor
naming alone, has to be confirmed against the real hardware once. Read
its own header comment for the full story. As of the current version,
**loads are the one thing NOT in this file** — every Nimbus load
subentry (HWS, pool, any circuit breaker) auto-publishes its own
forecast sensor, and the card discovers all of them directly from live
`hass.states` on every render, no config file edit needed. See
`topology-card-v4.js`'s own `_discoverLoads()` for the mechanism (a
deterministic entity_id transform, not a guess).

`lovelace_build_topology_dashboard.py` is the generator that turns
`topology_map.yaml` into the actual dashboard view config.

## `files/research/*.py` — the Solver audit scripts

These are the scripts used to work through (and mostly close) a real,
external 9-item Solver audit — 8 of 9 items closed as of this export.
All read-only against live HA history via the REST API; none write
anything. Worth reading in roughly this order if you want the story:

- `measurement_integrity_check.py` — audit item #1. Reconstructs real
  exported energy from raw meter history and checks it against
  independently-settled LocalVolts figures, under a strict tolerance.
- `objective_completeness_ablation_test.py` — audit item #2. Zeroes
  each real cost/value term in the LP one at a time and confirms it
  actually has a measurable effect — catches a silently-inert term.
- `contract_risk_injection_test.py` — audit item #4. Re-prices the
  Solver's own real plan under reduced P2P-match scenarios (100/75/
  50/25/0%) to quantify real financial exposure if a night's P2P match
  comes in below what was assumed.
- `hard_service_constraint_stress_test.py` — audit item #5. Sweeps a
  hypothetical hard-deadline load's cost under 1x/2x/5x/10x price to
  confirm the deadline holds regardless of price, and that a genuinely
  infeasible target fails honestly rather than silently.
- `forward_value_comparison.py` — audit item #7. Compares a flat
  terminal battery value against a piecewise-linear concave one on
  real recent data — the concave version is what's actually shipped
  now (`BatteryConfig.terminal_value_breakpoints`, this same repo).
- `forecast_capture.py` / `forecast_accuracy_compare.py` /
  `forecast_regret_decomposition.py` — audit item #9 (forecasting,
  deliberately last priority). Capture-then-compare mechanism for
  measuring real forecast error, plus a first attempt at decomposing
  total regret into forecasting's own share vs. optimisation/control's
  share.

## Where to actually look for the things you flagged

- Risk aversion / price risk aversion, and the import/export split —
  `custom_components/nimbus_load/solver/network.py`'s `build_plan()`
  own docstring (same repo), wired live via this writer's
  `risk_aversion` / `import_price_risk_aversion` /
  `export_price_risk_aversion` reads.
- Battery throughput/degradation exposure — search this writer for
  `equivalent_full_cycles`.
- Dual/shadow-price extraction — `solver/network.py`'s `LPResult.duals`
  (same repo), consumed here via `binding_constraint_now` /
  `energy_shadow_price_now` / `p2p_volume_cap_shadow_price`.
