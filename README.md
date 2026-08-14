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
