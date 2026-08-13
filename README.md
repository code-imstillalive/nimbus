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
4. Settings → Devices & Services → **Add Integration** → search "Nimbus"
5. Pick the power sensor you want it to learn from (and, optionally, a temperature sensor
   for training and a temperature-forecast sensor for prediction) — everything else has a
   sensible default

## What it publishes

One sensor per configured instance, `native_value` = the current predicted load (kW), and a
`forecast` attribute — a list of `{"time": ..., "value": ...}` points — in the same format
already used by [HAEO](https://github.com/hass-energy/haeo)'s own native forecast sensors, so
it can be wired straight into a HAEO Load element's forecast source.

## How it works

- `RandomForestRegressor` (scikit-learn), trained on cyclic time-of-day / day-of-week / month
  features plus temperature — not a heavier pipeline, deliberately, so it trains fast and
  needs no hyperparameter babysitting.
- Retrains itself once a day (configurable, defaults to 3am local) directly from Home
  Assistant's own recorder history — no external API calls, no credentials to manage.
- Everything (training + prediction) is offloaded to an executor thread, never blocking Home
  Assistant's own event loop.

## Status

Early — built for and being validated against a real house before wider use. Not yet
recommending production use elsewhere.

## License

MIT
