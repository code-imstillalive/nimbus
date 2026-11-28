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
     its horizon, or if it ever has no forecast).

   Either way the plan only takes the days it has proved itself on: today and tomorrow always;
   the days after only once your install has scored at least 10 past days at that range and
   learned its bias there (see below). Until then those days are shown on the card but not
   planned on.

## What you see

`sensor.nimbus_solar_forecast`:

| | |
|---|---|
| state | expected PV power now, kW (unknown, with the reason in `status_detail`, if no weather covers now) |
| `forecast` | about four days ahead (ECMWF, GEFS and GFS all read to 120 hours), `[{time, value, lower, upper, clear}]` in kW (p50, p10, p90, clear sky): 15-minute rows for two days, hourly after |
| `today` | today so far and the rest: kWh made since midnight, the forecast for the rest of the day, and the day's total p10 / p50 / p90 |
| `measured_today` | what the PV Strings made since midnight, 15-minute means in kW |
| `days` | each day ahead: p10 / p50 / p90 and clear-sky kWh, and the models behind it (`gfs` if only GFS was available) |
| `scoreboard` | every scored day: the forecast made from the evening-before weather by a model trained without that week, its range, what was made, and the other solar forecasts' evening-before figures (`others`) |
| `yardsticks` | the other solar forecasts installed (see below) and which of their sensors give each day |
| `arrays` | what it learned about each PV String |
| `score` | its own honest score: daily error on weeks it was not trained on, and how often real output fell inside p10 to p90 |
| `status` | starting, fetching_history, learning, ready, no_arrays or error, with `status_detail` |
| `live_correction` | how far the last 30 minutes measured are from the forecast (1.00 = on it) |
| `plan_until` | how far ahead the plan may use it: the days it shows but has not proved itself on (a range whose bias it has not learned yet, or GFS alone) are never planned on, even on *Blend* or *Plan on it alone*; there your other solar forecasts carry the plan |
| `made_at`, `haze` | when this forecast was made; the haze of your sky it learned (Linke turbidity) |

## The card

It is the **Solar Lab** tab of the standard [Nimbus dashboard](dashboards.md), or add a card with
`type: custom:nimbus-solar-card` (no settings needed). It shows:

- today (made so far and the rest), tomorrow, the days ahead and its own day-ahead error;
- the forecast for every day with its likely range, the clear-sky line and today's measured output;
- under it, on the same time line, the weather every 3 hours, day and night (from ECMWF): sun,
  cloud, light rain, showers, rain, heavy rain and thunderstorms, with rain as bars and night
  shaded. Thunderstorms need both unstable air (CAPE of 800 J/kg or more) and shower rain
  forecast. The pictures move gently (the sun glows, rain falls, lightning flashes; still if your
  device asks for reduced motion); hover or tap one for its cloud, rain, storm energy and
  solar power. On a phone, neighbouring hours share a picture showing the worst of them;
- the forecast can be the solar forecast line on Home Assistant's own **Energy dashboard**:
  Configure → Solar forecaster → *Show it in the Energy dashboard for* → pick one of your
  Energy solar sources (Nimbus covers all your panels, so one is enough; untick other forecasts
  on it so they do not add up). Home Assistant's Energy settings list only service-type
  integrations as forecasts and Nimbus is a hub, so Nimbus adds itself and puts itself back
  whenever those settings are saved without it. Today, the rest of today and tomorrow are also
  kWh sensors, like other solar forecast integrations offer;
- the same weather is also `weather.nimbus_weather`, a standard Home Assistant weather entity
  (now, every 3 hours, and per day), for the standard weather card and for automations such as
  "thunderstorms in the next few hours" ([entities](entities.md)). The weather needs only the
  download, not the trained model, so both appear within minutes of switching the forecaster on,
  a week before the first solar forecast;
- a table of every day, the next two hours, and your PV Strings as it learned them;
- its scoreboard: every scored day's evening-before forecast against what your panels made
  (in amber), with a summary.

It fits a phone: the two columns stack and the charts are drawn at the card's own size.

### Other solar forecasts, side by side (automatic)

If Forecast.Solar, Open-Meteo Solar Forecast or Solcast is installed, the card finds it by itself
(no setup) and shows its daily totals beside Nimbus's in the days table, and in the scoreboard its
"tomorrow" total as it stood at 21:00 the evening before, against what your panels made. Several
Forecast.Solar or Open-Meteo planes are added up. They are a comparison only: Nimbus never uses
them for its own forecast. Nimbus also notes each one's "tomorrow" itself at 21:00, so a forecaster
whose sensors your recorder does not keep (Solcast, on some installs) is scored from the day after
you switch the forecaster on.

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

### Further ahead (2 to 4 days)

The model learns from the evening-before forecasts, and read further out the same weather runs
come in low: on the reference household its forecasts 3 and 4 days ahead were 4 to 6 kWh a day
under. So every night it compares the forecasts it made 2, 3 and 4 days ahead (from the runs it
stored then) with what your panels made, and corrects each range by what it learned there.

Measured on the reference household (about 40 days, runs actually made that far ahead, the
correction learned on other days than the ones scored):

| days ahead | Nimbus, all three models, corrected | Open-Meteo Solar Forecast |
|---|---|---|
| 1 | 14.1 % | 15.8 % |
| 3 | 16.8 % | 16.6 % |
| 4 | 13.7 % | 16.5 % |

### Newer weather runs

ECMWF also publishes runs at 06Z, 12Z and 18Z. Measured on the reference household (45 days, the
same model fed each newer run instead of the evening-before one), none forecasts a day's solar
better: daily error 14.4 % for the evening-before run, 14.4 % for the 12Z run (out about 06:00 in
eastern Australia), 15.1 % for 06Z and 15.3 % for 18Z; the hour-by-hour shape is under a point
better, which the live correction from your own panels already covers through the day. So the
solar forecast stays on the evening-before run. The weather strip and `weather.nimbus_weather`
do use the newest 12Z run for their next 48 hours, so the weather you see is the freshest.

A new install has none of these older forecasts yet, so for its first ten days or so the days
after tomorrow are shown but not planned on. If ECMWF and GEFS are ever unavailable, GFS alone
carries the forecast with a wider likely range; that is never planned on.

## Limits

- It works anywhere: your location and time zone come from Home Assistant's own settings, and
  the weather models are global (checked against real runs for the southern and northern
  hemispheres, east and west of Greenwich).
- Each PV String needs a **power** sensor (W or kW). An inverter that reports only energy
  (kWh totals) is not supported yet.
- The weather's temperature is the model's for its grid cell (about 25 km). On a coast or a hillside
  that can be a few degrees off what you measure; the solar forecast is unaffected, because it
  learns from your own panels.

- Its first forecast comes once it has about a week of history (it fetches the weather
  itself; your PV Strings' recorded output comes from Home Assistant's recorder and long-term
  statistics).
- It downloads about 230 MB a day in summer (less in winter): the three models' light and cloud
  for the daylight hours (about 145 MB), plus ECMWF's rain, shower rain and storm energy day and
  night and its cloud and temperature at night for the weather strip and
  `weather.nimbus_weather` (about 50 MB), plus ECMWF's newer 12Z run, weather only, for the next
  48 hours (about 40 MB). The history it learns from is daylight only: up to about 35 MB per day
  the first time.
- It learns from your recorded output, so that output has to be honest:
  - gaps (a sensor unavailable, the recorder missing hours) are left out;
  - **curtailment is not detected yet**: hours when output was held back (export limits,
    negative prices, an inverter switched off but reporting 0) are learned as if cloud had
    caused them, which pulls the forecast down;
  - a change on the roof (new shading, panels added or moved) works its way through as
    the 30 days of history it trains on roll forward.
- Cloud timing on a stormy day cannot be known the evening before, by it or by anyone.
  Through the day the last 30 minutes measured correct the next hours.
