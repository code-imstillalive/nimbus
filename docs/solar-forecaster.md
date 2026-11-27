# Nimbus's own solar forecast

Nimbus can forecast your panels itself. It reads three free global weather models, learns
your own panels from what they actually produce, and publishes a forecast with an honest
likely range. No account, no key, no paid service.

## What it uses

| Weather model | From | What is read |
|---|---|---|
| ECMWF IFS (open data) | ECMWF's public archive (Google Cloud copy) | sunshine, total and low cloud, temperature |
| NOAA GFS | NOAA's Open Data on AWS | sunshine |
| NOAA GEFS (31 runs) | NOAA's Open Data on AWS | average sunshine of the 31 runs, how much they disagree, cloud |

Only the few numbers it needs are downloaded from each file, and only your own grid
point is kept. Your location is never sent anywhere: the files are global, and Nimbus
picks your point out of them itself.

ECMWF's data is used under CC-BY-4.0 ("Contains modified Copernicus/ECMWF open data");
NOAA's is public domain.

## What it learns from your panels

From the recorded output of each **PV String** (one per array, per MPPT or per string):

- each array's real size, including inverter and wiring losses;
- how much scattered light it gets (a wall facing open, pale ground gets more than the
  textbook says);
- your shading: the sun's height and direction are inputs, so a tree to the west that
  takes the late afternoon is learned. There are no "dampening" factors to set;
- the haze of your sky.

If you give a PV String its **tilt** and the **direction it faces**, those are used exactly.
Left blank, they are learned.

## Setting it up

1. **Configure → Solar forecaster**: switch it on.
2. Tick **Find my panels now**. Nimbus lists the per-MPPT or per-string power sensors from
   your inverter's own integration; tick the ones that are real arrays. Each becomes a PV
   String on the hub's device page. (Or add PV Strings by hand there.)
3. Open each PV String and enter its tilt and direction if you know them.
4. Choose **what the plan does with it**:
   - **Forecast only** (default): the plan does not use it. Compare it with your other
     solar forecasts for a while first;
   - **Blend**: it becomes one more member of the solar blend;
   - **Plan on it alone**: your other solar forecasts only fill in where it has none (past
     its four days, or if it ever has no forecast).

## What you see

`sensor.nimbus_solar_forecast`:

| | |
|---|---|
| state | expected PV power now, kW |
| `forecast` | the next four days, `[{time, value, lower, upper}]` in kW (p50, p10, p90): 15-minute rows for two days, hourly after |
| `days` | each day's p10 / p50 / p90 and clear-sky kWh |
| `arrays` | what it learned about each PV String |
| `score` | its own honest score: daily error on weeks it was not trained on, and how often real output fell inside p10 to p90 |
| `status` | starting, fetching_history, learning, ready, no_arrays or error, with `status_detail` |
| `live_correction` | how far the last 30 minutes measured are from the forecast (1.00 = on it) |

## How good it is

Measured on the reference household (44 days, forecasts made from the weather published
the evening before, scored on weeks the model never saw):

| | daily error |
|---|---|
| Nimbus's solar forecast | 12.5-13 % |
| Open-Meteo Solar Forecast | 16.6 % |
| Forecast.Solar | 39.4 % |

Its likely range held 80 % of real readings, which is what the Solver's
[Stochastic plan](configuration-reference.md) needs.

## Limits

- Its first forecast comes once it has about a week of history (it fetches the weather
  itself; your PV Strings' recorded output comes from Home Assistant's recorder and long-term
  statistics).
- It downloads about 85 MB a day, plus about 35 MB per day of history the first time.
- Cloud timing on a stormy day cannot be known the evening before, by it or by anyone.
  Through the day the last 30 minutes measured correct the next hours.
