# Nimbus Solver (app)

Runs [Nimbus](https://github.com/code-imstillalive/nimbus)'s LP battery/grid dispatch Solver
against your live Home Assistant sensors on a schedule, and publishes the result to
`sensor.nimbus_solver_battery_forecast` — same script, same output, as the reference NUC-cron
deployment documented in `docs/real-world-integration/`, just packaged to run anywhere a real
Home Assistant Supervisor can install and run an app/add-on, including Home Assistant OS.

## Why this exists

The Nimbus integration itself (installed separately via HACS — see the top-level README's
own "Install (HACS)" and "Solver" sections) works everywhere, HAOS included. But *producing*
a live Solver forecast needs real shell + a compiled LP library (`highspy`) running
somewhere, which a plain HACS custom_component can't provide on Home Assistant OS at all —
HAOS has no general shell/cron surface. This app is that "somewhere," packaged the way HAOS
actually expects: a real Supervisor-managed Docker container, talking to HA only over its own
REST API (no manual long-lived token needed — Supervisor injects one automatically), no
separate device required.

## Install

1. Settings → Add-ons → Add-on Store → the three-dot menu (top right) → **Repositories**
2. Add `https://github.com/code-imstillalive/nimbus`
3. Find **Nimbus Solver** in the store, install it
4. Before starting it: finish the Nimbus integration's own **Solver settings** wizard first
   (Settings → Devices & services → Nimbus → Configure → Solver settings, 6 steps — battery,
   power & efficiency, grid, price/forecast sources, economic policy, optional P2P). Confirm
   `sensor.nimbus_solver_config` reads `configured` in Developer Tools → States before
   starting this app — it'll fail loudly and name exactly what's missing if not.
5. Start the app. Check its own log — a real solve should complete in well under a second and
   print a line like `pushed sensor.nimbus_solver_battery_forecast: status=optimal ...`

## Status — genuinely a first pass, not yet live-verified

Built 2026-08-21 from real, current Home Assistant "apps" documentation and the official
[`home-assistant/addons-example`](https://github.com/home-assistant/addons-example) reference
template, and confirmed `highspy` has real published wheels for `amd64`/`aarch64` (covers
Docker/x86 and 64-bit Raspberry Pi 4/5 — the large majority of real HAOS installs). What
hasn't been possible to confirm from here, and needs a real Supervisor to actually try:

- Whether `homeassistant_api: true` really resolves to `http://supervisor/core` as this app's
  `nimbus_solver_forecast_writer.py` assumes (its own `PORTABILITY` comment block flags this
  precisely) — if wrong, every HA request in the log will fail loudly with an HTTPError, not
  silently, so it should be obvious on the very first run, not a subtle bug.
- Whether the multi-arch Docker build genuinely succeeds end-to-end on both `amd64` and
  `aarch64` inside a real Supervisor build (only the Dockerfile's own logic has been reviewed
  against the reference template, never actually built).
- 32-bit ARM (older Raspberry Pi 3/Zero) is deliberately unsupported — `highspy` has no wheel
  for it. The Nimbus Forecaster (the HACS half) has no such limitation.

If anything above turns out wrong on a real install, that's genuinely useful, new information
worth reporting back — not a sign this was rushed carelessly, just honest about the limits of
building something without a real Supervisor to test against.

## What it does NOT do

Same as the NUC-cron version: purely observational. Reads sensors via `GET`, writes exactly
one sensor via `POST`. Never calls `number.set_value`, never calls a script, never touches
Modbus, never makes a real dispatch decision for you.
