"""What the forecaster knows about one install's panels: where, which way, how big.

The owner gives each array's tilt and direction (the wizard's PV String
step). Those are used exactly. What is learned from the install's own
measured output, from its clear moments:

- each array's effective size (kWp, including inverter and wiring losses);
- each array's scattered-light gain (a wall facing pale open ground sees
  more scattered light than the textbook sky-and-ground model gives it);
- the site's clear-sky haze (Linke turbidity).

An array with no owner-given direction gets one fitted from the data
(coarse, then refined). An input that never produces is marked unused.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import physics as ph

SLOT_S = 300.0  # measured output arrives as 5-minute means, labelled by their start


@dataclass
class Site:
    lat: float
    lon: float
    altitude_m: float = 0.0
    linke: float = 3.0


@dataclass
class Array:
    """One PV string / MPPT input. `tilt` and `azimuth` None means: learn them."""

    key: str
    tilt: float | None = None
    azimuth: float | None = None
    kwp: float = 0.0
    diffuse_gain: float = 1.0
    active: bool = True
    learned: dict = field(default_factory=dict)

    @property
    def owner_set(self) -> bool:
        return self.tilt is not None and self.azimuth is not None


def clear_points(
    t, y, zen, tz_offset_s: float, window_days: int = 7, frac: float = 0.92
):
    """Clear-sky moments: readings within `frac` of the highest reading at the same time of
    day over nearby days. Sturdier than whole clear days: a mostly cloudy day still
    contributes its sunny spells."""
    local = (np.asarray(t) + tz_offset_s) / 86400.0
    day = np.floor(local).astype(int)
    slot = np.round((local - day) * 288).astype(int)
    ok = ~np.isnan(y) & (zen < 85)
    keep = np.zeros(len(t), bool)
    for s in np.unique(slot[ok]):
        idx = np.where(ok & (slot == s))[0]
        for i in idx:
            near = idx[np.abs(day[idx] - day[i]) <= window_days]
            top = np.nanmax(y[near])
            if top > 0.05 and y[i] >= frac * top:
                keep[i] = True
    return keep


def _score(y, g, dni, dhi, zen, az, tilt, azim, tc, air):
    """Best (rmse, kWp, scattered-light gain) for one orientation."""
    d, s = ph.poa_parts(g, dni, dhi, zen, az, tilt, azim, tc)
    tot = d + s
    heat = ph.dc_power(tot, 1.0, air) / np.maximum(tot / 1000.0, 1e-9)
    xd, xs = d / 1000.0 * heat, s / 1000.0 * heat
    (k1, k2), *_ = np.linalg.lstsq(np.vstack([xd, xs]).T, y, rcond=None)
    gain = float(np.clip(k2 / k1, 0.5, 3.0)) if k1 > 0 else 1.0
    x = xd + gain * xs
    kwp = float(np.sum(y * x) / max(np.sum(x * x), 1e-9))
    return float(np.sqrt(np.mean((y - kwp * x) ** 2))), kwp, gain


def fit(
    site: Site, arrays: list[Array], t, measured: dict, tz_offset_s: float, air_c=None
):
    """Learn haze, and each array's size and scattered-light gain (and direction where the
    owner gave none), from measured 5-minute means. Updates `site` and `arrays` in place."""
    t = np.asarray(t, dtype=float)
    tc = t + SLOT_S / 2  # the sun at the middle of each slot
    zen, az = ph.sun_position(tc, site.lat, site.lon)
    air = np.full(len(t), 25.0) if air_c is None else np.asarray(air_c, dtype=float)
    keys = [a.key for a in arrays if a.key in measured]
    total = (
        np.sum([np.asarray(measured[k], dtype=float) for k in keys], axis=0)
        if keys
        else np.full(len(t), np.nan)
    )
    clear = clear_points(t, total, zen, tz_offset_s)
    m = clear & (zen < 80) & ~np.isnan(total)
    if m.sum() > 50:  # haze: the clear-sky shape that best matches the clear moments
        best = None
        for tl in np.arange(2.0, 6.01, 0.25):
            g, _, _ = ph.clear_sky(tc, zen, site.altitude_m, tl)
            k = np.sum(total[m] * g[m]) / np.sum(g[m] ** 2)
            err = np.mean((total[m] - k * g[m]) ** 2)
            if best is None or err < best[0]:
                best = (err, float(tl))
        site.linke = best[1]
    g, dni, dhi = ph.clear_sky(tc, zen, site.altitude_m, site.linke)
    for a in arrays:
        y = np.asarray(measured.get(a.key, np.full(len(t), np.nan)), dtype=float)
        mk = clear_points(t, y, zen, tz_offset_s) & ~np.isnan(y)
        if mk.sum() < 100 or np.nanmax(np.where(np.isnan(y), 0, y)) < 0.2:
            a.active, a.kwp = False, 0.0
            a.learned = {"note": "no output: unused input"}
            continue
        a.active = True
        args = (g[mk], dni[mk], dhi[mk], zen[mk], az[mk])
        if a.owner_set:
            e, kwp, gain = _score(y[mk], *args, a.tilt, a.azimuth, tc[mk], air[mk])
        else:
            best = None
            for tilt in range(0, 91, 5):
                for azim in range(0, 360, 10):
                    r = _score(y[mk], *args, tilt, azim, tc[mk], air[mk])
                    if best is None or r[0] < best[0]:
                        best = (*r, tilt, azim)
            _, _, _, t0, a0 = best
            for tilt in range(max(0, t0 - 5), min(90, t0 + 5) + 1):
                for azim in range(a0 - 10, a0 + 11, 2):
                    r = _score(y[mk], *args, tilt, azim % 360, tc[mk], air[mk])
                    if r[0] < best[0]:
                        best = (*r, tilt, azim % 360)
            e, kwp, gain, a.tilt, a.azimuth = best
        a.kwp, a.diffuse_gain = round(kwp, 3), round(gain, 3)
        a.learned = {
            "rmse_kw": round(e, 3),
            "clear_points": int(mk.sum()),
            "orientation": "owner" if a.owner_set else "learned",
        }
    return site, arrays


def power(
    site: Site,
    arrays: list[Array],
    t,
    kt,
    air_c,
    mid_s: float = SLOT_S / 2,
    per_array=False,
):
    """DC kW from a clear-sky index on grid t (slots labelled by their start)."""
    t = np.asarray(t, dtype=float) + mid_s
    zen, az = ph.sun_position(t, site.lat, site.lon)
    cs, _, _ = ph.clear_sky(t, zen, site.altitude_m, site.linke)
    ghi = np.asarray(kt) * cs
    dni, dhi = ph.erbs(ghi, zen, t)
    tot, parts = np.zeros(len(t)), {}
    for a in arrays:
        if not a.active or not a.kwp:
            continue
        p = ph.poa(ghi, dni, dhi, zen, az, a.tilt, a.azimuth, t, a.diffuse_gain)
        parts[a.key] = ph.dc_power(p, a.kwp, air_c)
        tot = tot + parts[a.key]
    return (tot, parts) if per_array else tot


def clear_power(
    site: Site, arrays: list[Array], t, mid_s: float = SLOT_S / 2, per_array=False
):
    """DC kW under a clear sky."""
    t = np.asarray(t, dtype=float) + mid_s
    zen, az = ph.sun_position(t, site.lat, site.lon)
    cs, dni, dhi = ph.clear_sky(t, zen, site.altitude_m, site.linke)
    tot, parts = np.zeros(len(t)), {}
    for a in arrays:
        if not a.active or not a.kwp:
            continue
        p = ph.poa(cs, dni, dhi, zen, az, a.tilt, a.azimuth, t, a.diffuse_gain)
        parts[a.key] = ph.dc_power(p, a.kwp)
        tot = tot + parts[a.key]
    return (tot, parts) if per_array else tot


def clear_index(site: Site, t_centres, ghi_means):
    """Each weather interval's clear-sky index: forecast GHI over clear-sky GHI averaged over
    the same interval (hourly or 3-hourly, inferred from the spacing)."""
    t_centres = np.asarray(t_centres, dtype=float)
    kt = np.full(len(t_centres), np.nan)
    half = float(np.median(np.diff(t_centres))) / 2 if len(t_centres) > 1 else 5400.0
    half = min(max(half, 1800.0), 5400.0)
    for i, c in enumerate(t_centres):
        tt = np.arange(c - half, c + half, SLOT_S) + SLOT_S / 2
        z, _ = ph.sun_position(tt, site.lat, site.lon)
        cs, _, _ = ph.clear_sky(tt, z, site.altitude_m, site.linke)
        if cs.mean() > 20:
            kt[i] = np.clip(ghi_means[i] / cs.mean(), 0.0, 1.1)
    return kt
