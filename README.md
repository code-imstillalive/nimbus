# Nimbus

*Just a different type of cloud.*

> ⚠️ **Work in progress — active shadow-mode project, not a finished product.**
> The Forecaster and Solver are both under real, ongoing development. Neither drives any
> live battery/grid dispatch today — the Solver runs entirely in observe-only shadow mode
> against real household data, and stays that way until a lot more evidence accumulates (see
> the reference household's own "Nimbus → HAEO Replacement Readiness Checklist" for exactly
> what that bar looks like). Expect rough edges, breaking changes, and real bugs — several
> have been found and fixed in the days right around this repo going public. If you install
> this, please open a GitHub issue rather than expect a polished, plug-and-play experience.

A self-retraining ML load forecaster for Home Assistant.

Nimbus watches a power sensor you choose, learns your home's real consumption pattern
(time of day, day of week, season, weather), and publishes a rolling forecast — with zero
manual retraining, zero config-file editing, and no dependency on shell/cron/systemd access.
It runs as a normal Home Assistant integration, so it works the same way on Home Assistant
OS, Supervised, and Docker installs.

## Why

Most load forecasters in the Home Assistant energy-optimization world are either purely
weather-correlated (no real learning from your own house) or bundled inside a much larger,
harder-to-adopt optimizer. Nimbus does one thing well: forecast the load, from your own real
history, with zero manual retraining. It publishes a plain, stable `{time, value}` forecast
shape — usable standalone, or fed into whatever dispatch/optimization layer you already run.
Nimbus also ships its own optional LP-based Solver (see below) for households that want the
whole stack — forecasting through to a real battery/grid dispatch plan — in one integration.

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

**Requires a 64-bit host (`amd64` or `aarch64`)** — the Solver depends on `highspy`, a real
compiled LP library with no wheel for 32-bit ARM (older Raspberry Pi 3/Zero). Check with
`uname -m` before relying on it if you're unsure. The Forecaster above has no such
restriction and runs fine on any architecture regardless.

Nimbus is actually two things: the Forecaster above (predicts a load), and a real LP-based
battery/grid dispatch **Solver** (`custom_components/nimbus_load/solver/` —
`network.py`/`elements.py`/`lp.py`, HiGHS-backed, zero Home Assistant imports, fully
unit-tested on its own). The Solver's config surface — Nimbus hub → **Configure** → **Solver
settings**, a 3-step wizard (Battery → Grid → Sources — sensor pointers only; every plain
numeric setting, from battery capacity to salvage value, is its own live, dashboard-editable
`number.nimbus_solver_*` entity instead) — installs via HACS and works on any HA platform
including HAOS, same as the Forecaster.

**Running the Solver settings wizard is mandatory, not optional, if you want the Solver at
all.** Skip it and every `number.nimbus_solver_*` entity (battery capacity, max charge/
discharge power, grid import/export limits) sits at its own defensive placeholder minimum
(e.g. 0.1 kWh) — deliberately dispatch-safe, but useless: the Solver will happily run against
these and produce a meaningless plan, with nothing in the UI to tell you it's not your real
hardware. A persistent notification fires the moment the hub is created pointing you at
Configure → Solver settings — but don't rely on the notification alone; if you dismiss it or
restart HA before running the wizard, the placeholder values become sticky and won't
auto-update from the wizard afterward (edit `number.nimbus_solver_*` directly in that case).
Confirm `sensor.nimbus_solver_config` reads `configured` in Developer Tools → States before
expecting a real plan. **If you only want load forecasting, skip this whole section** — the
Forecaster works standalone with zero further setup.

**Producing a live dispatch forecast (2026-08-22, now genuinely pure-integration):** install
via HACS, run the wizard above, done — no separate device, no cron, no addon. The Solver runs
natively in-process (`solver_runtime.py`, on a 1-minute timer) the moment `highspy` (the real,
compiled LP solver — an automatic `manifest.json` requirement, real prebuilt wheels for
amd64/aarch64, **not** for 32-bit armv7) finishes installing. This is what actually closes the
gap a real, live third-party install (Mark Purcell) hit trying the addon path below against
this private repo — Supervisor's own "Add repository" flow does a raw, unauthenticated git
clone with no token support at all, so it can't reach a private repo regardless of HACS
itself working fine.

One older, still fully supported path remains, for the one case the native path genuinely
can't cover — you'd rather run the Solver on a separate always-on device than inside HA's
own process: the standalone Python script (`nimbus_solver_forecast_writer.py`, real shell +
cron access, any always-on device on the same network, not just the HA host — see its own
header docstring for the full deploy story). Both paths run the exact same, byte-identical
solve logic — see `solver_writer.py`'s own "PURE INTEGRATION seam" comment for how one
script serves both without being forked.

**Deprecated (2026-08-23):** the third path, `nimbus_solver_app` (a Supervisor add-on that
wrapped the same standalone script for HAOS), is deprecated as of v0.73.0 and will be
removed in v1.0.0 — the native in-process path above covers every architecture the add-on
covered (both need a `highspy` wheel, so both are amd64/aarch64 only), with no separate
container, no version-lockstep discipline, and no three-way copy sync. If you have it
installed: uninstall it and finish the integration's own Solver settings wizard — the
native path takes over the same `sensor.nimbus_solver_*` outputs with no config migration
needed. Tracking: [#76](https://github.com/code-imstillalive/nimbus/issues/76).

A real, working (if household-specific) copy of that script, plus the LP-audit research
scripts used to validate it, live in `docs/real-world-integration/` — read that folder's own
README first.

## What it publishes

One sensor per configured instance, `native_value` = the current predicted load (kW), and a
`forecast` attribute — a list of `{"time": ..., "value": ...}` points. A plain, stable,
generic shape by design, so it can be wired straight into any dispatch/optimization layer's
own forecast source, or used entirely on its own via the Solver below.

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

## Removing Nimbus

1. Settings → Devices & Services → **Nimbus** → the three-dot menu on the hub card →
   **Delete** — this removes the hub and every load/power-signal subentry under it, along
   with all of their entities and devices.
2. HACS → **Nimbus** → the three-dot menu → **Remove**, to uninstall the integration itself.
3. Two things Nimbus writes to disk that neither of the above steps clears (harmless to leave,
   but here in case you want a completely clean uninstall): each load's persisted
   model/residual files at `.storage/nimbus_load_*.pkl`/`.json`, and, if you ever ran the
   Solver's pure-integration mode, its plan-state/lock files at the paths shown in
   `solver_writer.py`'s own `PLAN_STATE_PATH`/`LOCK_PATH` (both env-var overridable, see that
   file — defaults live under `.storage/` too).
4. If you also installed the separate `nimbus_solver_app` Supervisor add-on: Settings →
   Add-ons → **Nimbus Solver** → **Uninstall**, then remove the repository from Add-on Store →
   Repositories if you added it there.

## Status

Early — built for and being validated against a real house before wider use. Not yet
recommending production use elsewhere.

## License

MIT
