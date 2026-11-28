# Nimbus's own solar forecaster

Status: engine (this PR); Home Assistant wiring, wizard and card follow in their own PRs.

## Why

Every solar forecast Nimbus reads today comes from another integration (Solcast,
Open-Meteo Solar Forecast, Forecast.Solar). Each needs its own account or install,
none knows the install's real panels, and their bands are not calibrated, so the
two-stage stochastic plan (#1807) gets an envelope (p10 = 0, p90 several times p50
on the reference household), not a real p10/p90.

## What it does

1. **Weather, free and keyless:** the evening-before runs of ECMWF IFS open data
   (Google Cloud mirror), NOAA GFS and NOAA GEFS (31 members), read straight from
   the public archives. Only the needed GRIB fields are range-fetched; only the
   site's grid point is kept. The location is never sent to NOAA or ECMWF.
2. **Physics per array:** NOAA sun position, Ineichen-Perez clear sky, Erbs split,
   Hay-Davies plane of array with the owner's tilt and azimuth, glass reflection,
   cell temperature.
3. **Learned per install:** each array's effective size and scattered-light gain,
   and the site's haze, from its own clear moments; inputs that never produce are
   marked unused. Owner-given orientation is used exactly.
4. **Model:** Nimbus's numpy quantile GBRT (`ml/gbrt.py`) maps the three models'
   clear-sky index, cloud, whole-day view and ensemble spread, plus sun height and
   direction (which learns local shading: no manual "dampening"), to measured /
   clear-sky output: p10 / p50 / p90 every 5 minutes.
5. **Calibrated band:** p10/p90 widened in blocked cross-validation until 80 % of
   real readings fall inside.
6. **Resilient:** tiers trained without one source take over when it is missing; all three models are read to 120 h; the bias at 2-4 days ahead is learned nightly from the runs stored then (factor clipped to 0.8-1.3, used from 10 scored days), and the plan takes 3 and 4 days ahead only once learned (1-2, today and tomorrow, always); GFS alone, with a wider range, is a fallback never planned on.

## Measured (reference household, 44 days, contiguous weeks held out)

| | daily error |
|---|---|
| this forecaster | 12.5-12.8 % |
| Open-Meteo Solar Forecast | 16.6 % |
| Forecast.Solar | 39.4 % |

Grouped by what was forecast (never by the outcome): on forecast-sunny days the
mean miss is 3.9 kWh (bias +0.3) against Open-Meteo's 4.6 (+2.9); elsewhere
8.0 kWh against 11.1 (Open-Meteo +10.0 too sunny). p10-p90 holds 81 %.

Tried and left out: a Himawari-9 satellite nowcast (scored on 46 days, it did
not beat the plan's existing live correction; ~1 % in a blend).

## Prior art

EMHASS: pvlib with Open-Meteo weather and an optional linear `adjust_pv_forecast`
(scikit-learn). HAEO: none (reads other integrations' sensors).

Each ingredient exists elsewhere (review on #2646, 28 Nov 2026):
- **Several weather models blended:** Solcast (GFS, ECMWF IFS, ACCESS-G, WRF behind a
  satellite nowcast), meteoblue's MultiModel, Solargis (ECMWF to day 3, then GFS),
  Cornaro et al.'s WRF + ECMWF ensemble.
- **Per-site learning on measured output:** Solcast's paid Premium tier, Koster et al.
  (Solar RRL 2023), Verbois et al. (quantile GBRT on ECMWF).
- **Free and open:** Open Climate Fix's Quartz reads GFS and ICON through the Open-Meteo
  API with one model trained on a UK fleet.

None found combines them as here: raw multi-model archives read directly (no account, no
API call, the location never sent), a model trained from scratch on each install's own
output, tiers that take over as sources end, a band calibrated for the stochastic plan,
free and self-hosted. The combination is the new part, not any one ingredient.

## Package

`custom_components/nimbus_load/solar_forecaster/` (HA-import-free, like `solver/`
and `ml/`): `grib` (GRIB2 incl. CCSDS/AEC, lat/lon and ICON grids), `sources`,
`physics`, `site`, `model`.

## Data and licences

ECMWF open data: CC-BY-4.0, attribution "Contains modified Copernicus/ECMWF open
data". NOAA GFS/GEFS: public domain. Download per day for one site: about 60 MB.
