"""Nimbus's own solar forecaster: free weather models -> the install's own panels -> p10/p50/p90 kW.

Pure (no Home Assistant import), like `solver/` and `ml/`:

- `grib`: GRIB2 reader (simple, complex and CCSDS packing; lat/lon and ICON grids), numpy only;
- `sources`: ECMWF open data, NOAA GFS and GEFS, read straight from their public archives;
- `physics`: sun position, clear sky, direct/diffuse split, light on each array, heat loss;
- `site`: what is learned about the install's arrays (size, scattered-light gain, haze);
- `model`: the quantile GBRT that turns the three models into p10/p50/p90 kW.
"""
