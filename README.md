# Nimbus

A self-retraining ML load forecaster for Home Assistant.

Nimbus watches a power sensor you choose, learns your home's real consumption pattern
(time of day, day of week, season, weather), and publishes a rolling forecast — with zero
manual retraining, zero config-file editing, and no dependency on shell/cron/systemd access.
It runs as a normal Home Assistant integration, so it works the same way on Home Assistant
OS, Supervised, and Docker installs.

## Why

Most load forecasters in the Home Assistant energy-optimization world are either purely
weather-correlated (no real learning from your own house) or bundled inside a much larger,
harder-to-adopt optimizer. Nimbus does one thing: forecast the load. Feed its output into
whatever optimizer you already use (it publishes in the same `{time, value}` forecast shape
[HAEO](https://github.com/hass-energy/haeo) already reads natively).

## Install (HACS)

1. HACS → the three-dot menu → **Custom repositories**
2. Add `https://github.com/code-imstillalive/nimbus`, category **Integration**
3. Install **Nimbus**, restart Home Assistant
4. Settings → Devices & Services → **Add Integration** → search "Nimbus" — this creates the
   hub, no fields to fill in
5. On the Nimbus hub, click **Configure** to set the settings shared by every load once:
   temperature sensor (optional, improves accuracy), temperature-forecast sensor (optional,
   used for future predictions), forecast horizon, retrain hour, training window — all have
   sensible defaults if left alone
6. Click **"+ Add"** on the hub's device page to add a load — just pick the power sensor to
   learn from. Repeat for as many loads as you have (built for and tested against a real
   18-circuit-breaker household) — no restart, no repeated integration setup, no re-entering
   the shared settings each time

## Solver (optional, separate thing)

Nimbus is actually two things: the Forecaster above (predicts a load), and a real LP-based
battery/grid dispatch **Solver** (`custom_components/nimbus_load/solver/` —
`network.py`/`elements.py`/`lp.py`, HiGHS-backed, zero Home Assistant imports, fully
unit-tested on its own). The Solver's config surface — Nimbus hub → **Configure** → **Solver
settings**, a 6-step wizard (battery, power & efficiency, grid, price/forecast sources,
economic policy, optional P2P) — installs via HACS and works on any HA platform including
HAOS, same as the Forecaster.

Producing a *live* dispatch forecast from those settings is a separate step, though, and it
has a real requirement the Forecaster doesn't: it needs a small standalone Python script
(`nimbus_solver_forecast_writer.py`) running somewhere with real shell + cron access, not
something HACS or HA runs for you. **This does not have to be the same machine that runs
Home Assistant** — the script only ever talks to HA over plain HTTP (its own `HA_BASE`
constant), never touches HA's filesystem or process, so it's happy running from any
always-on device on the same network: the HA host itself if HA runs in Docker/Supervised,
or a completely separate box (a Raspberry Pi, an old laptop, a NAS, a cheap VPS) if HA is
Home Assistant OS specifically, which genuinely has no shell/cron surface of its own for
this to run on directly. It also needs `highspy` (the real, compiled LP solver) pip-installed
on whichever device runs it — confirmed working via `pip install --break-system-packages
highspy` on this project's own Debian-based host, genuinely untested elsewhere. If there's
truly no other device available at all, a proper HAOS Add-on (a Docker-packaged Supervisor
add-on, distinct from this bare script) would be the honest path — a real, separate, bigger
build, not something that exists yet.

A real, working (if household-specific) copy of that script, plus the LP-audit research
scripts used to validate it, live in `docs/real-world-integration/` — read that folder's own
README first; the deploy steps and platform requirement are documented in full in the writer
script's own header docstring.

## What it publishes

One sensor per configured instance, `native_value` = the current predicted load (kW), and a
`forecast` attribute — a list of `{"time": ..., "value": ...}` points — in the same format
already used by [HAEO](https://github.com/hass-energy/haeo)'s own native forecast sensors, so
it can be wired straight into a HAEO Load element's forecast source.

## How it works

- A pure-numpy weighted k-nearest-neighbors regressor, trained on cyclic time-of-day /
  day-of-week / month features plus temperature — no scikit-learn, no compiled dependencies
  at all beyond numpy itself (which already ships pre-built wheels everywhere HA runs).
  Started out on scikit-learn's `RandomForestRegressor`, but scikit-learn has no pre-built
  wheel for newer Python releases and fails to build from source inside HA's own container
  (no C compiler present) — numpy-only sidesteps that fragility entirely, not just for us.
- Retrains itself once a day (configurable, defaults to 3am local) directly from Home
  Assistant's own recorder history — no external API calls, no credentials to manage.
- Everything (training + prediction) is offloaded to an executor thread, never blocking Home
  Assistant's own event loop.

## Status

Early — built for and being validated against a real house before wider use. Not yet
recommending production use elsewhere.

## License

MIT
