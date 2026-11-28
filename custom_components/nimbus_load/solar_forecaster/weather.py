"""The weather strip under the Solar Lab chart: one picture per ECMWF interval (3 hours), day and
night, chosen from the forecast's own cloud, rain, shower rain and storm instability (CAPE).

Pictures (`sky`): clear, mostly_sunny, partly_cloudy, cloudy, sprinkle, showers_sun, rain,
heavy_rain, storm; at night clear_night, partly_cloudy_night and showers_night stand in for the
ones that show the sun.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from . import physics as ph

#: A storm: instability (most-unstable CAPE, J/kg) high enough for thunderstorms, with shower rain
#: actually forecast. Either alone is not enough: CAPE without a trigger stays dry, and showers in
#: stable air are plain showers.
STORM_CAPE = 800.0
STORM_SHOWER_MM_H = 0.3
#: Rain rates (mm per hour, the interval's average).
HEAVY_RAIN_MM_H = 4.0
RAIN_MM_H = 1.0
SPRINKLE_MM_H = 0.1
#: Below this total cloud (%) light rain falls from a broken sky: sun and showers.
SUN_SHOWER_CLOUD = 60.0
#: Total cloud (%) bands for the dry pictures.
CLEAR_CLOUD = 20.0
MOSTLY_SUNNY_CLOUD = 45.0
PARTLY_CLOUDY_CLOUD = 75.0

NIGHT = {
    "clear": "clear_night",
    "mostly_sunny": "partly_cloudy_night",
    "partly_cloudy": "partly_cloudy_night",
    "showers_sun": "showers_night",
}


def _num(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return x


def classify(cloud, rain, shower, cape, sun_up: bool = True) -> str:
    """The picture for one interval. Missing values (None or NaN) count as none."""
    c, r, s, k = (_num(v) for v in (cloud, rain, shower, cape))
    c = 0.0 if np.isnan(c) else c
    r = 0.0 if np.isnan(r) else r
    s = 0.0 if np.isnan(s) else s
    k = 0.0 if np.isnan(k) else k
    if k >= STORM_CAPE and s >= STORM_SHOWER_MM_H:
        sky = "storm"
    elif r >= HEAVY_RAIN_MM_H:
        sky = "heavy_rain"
    elif r >= RAIN_MM_H:
        sky = "rain"
    elif r >= SPRINKLE_MM_H:
        sky = "showers_sun" if c < SUN_SHOWER_CLOUD else "sprinkle"
    elif c < CLEAR_CLOUD:
        sky = "clear"
    elif c < MOSTLY_SUNNY_CLOUD:
        sky = "mostly_sunny"
    elif c < PARTLY_CLOUDY_CLOUD:
        sky = "partly_cloudy"
    else:
        sky = "cloudy"
    return sky if sun_up else NIGHT.get(sky, sky)


def strip(
    ecmwf: dict | None, lat: float, lon: float, t0: float, t1: float
) -> list[dict]:
    """One entry per ECMWF interval whose middle lies in [t0, t1] (UTC seconds)."""
    if not ecmwf or len(ecmwf.get("t", ())) == 0:
        return []
    t = np.asarray(ecmwf["t"], dtype=float)
    keep = (t >= t0) & (t <= t1)
    if not keep.any():
        return []
    zen, _ = ph.sun_position(t[keep], lat, lon)

    def col(name):
        v = ecmwf.get(name)
        return np.asarray(v, dtype=float)[keep] if v is not None else None

    cols = {n: col(n) for n in ("cloud", "low_cloud", "rain", "shower", "cape", "t2m")}

    def at(name, i):
        v = cols[name]
        if v is None or np.isnan(v[i]):
            return None
        return round(float(v[i]), {"rain": 2, "shower": 2, "t2m": 1}.get(name, 0))

    out = []
    for i, ti in enumerate(t[keep]):
        e = {n: at(n, i) for n in cols}
        e["temperature"] = e.pop("t2m")  # degrees C
        sun_up = bool(zen[i] < 90)
        out.append(
            {
                "time": dt.datetime.fromtimestamp(float(ti), dt.UTC).isoformat(),
                **e,
                "sun_up": sun_up,
                "sky": classify(e["cloud"], e["rain"], e["shower"], e["cape"], sun_up),
            }
        )
    return out
