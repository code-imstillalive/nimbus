"""Sun, sky and panel physics (numpy only).

All standard methods, the same ones pvlib documents:

- sun position: the NOAA solar calculator equations (well under 0.1 degree);
- extraterrestrial irradiance with the Earth-Sun distance factor (Spencer);
- clear sky: Ineichen-Perez with a Linke turbidity and the site's altitude;
- direct/diffuse split of a global horizontal value: Erbs;
- plane of array: Hay-Davies (direct plus circumsolar, isotropic sky) and
  isotropic ground reflection, with a glass reflection loss (ASHRAE);
- cell temperature: a NOCT-style rise with irradiance, wind cooling; power
  falls 0.4 % per degree C above 25 degrees C.

Times are UNIX seconds (UTC). Angles are degrees. Azimuths are measured from
north, clockwise (north 0, east 90, south 180, west 270), for the sun and
for panels alike, so the same convention serves both hemispheres.
"""

from __future__ import annotations

import numpy as np

DEG = np.pi / 180.0
_J2000_S = 946684800.0  # 2000-01-01 00:00 UTC


def sun_position(t_utc_s, lat: float, lon: float):
    """(zenith, azimuth) in degrees at the given UTC instants."""
    t = np.asarray(t_utc_s, dtype=float)
    jd = t / 86400.0 + 2440587.5
    jc = (jd - 2451545.0) / 36525.0
    l0 = np.mod(280.46646 + jc * (36000.76983 + jc * 0.0003032), 360.0)
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    c = (
        np.sin(m * DEG) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + np.sin(2 * m * DEG) * (0.019993 - 0.000101 * jc)
        + np.sin(3 * m * DEG) * 0.000289
    )
    omega = 125.04 - 1934.136 * jc
    app_long = l0 + c - 0.00569 - 0.00478 * np.sin(omega * DEG)
    eps0 = (
        23 + (26 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60) / 60
    )
    eps = eps0 + 0.00256 * np.cos(omega * DEG)
    decl = np.arcsin(np.sin(eps * DEG) * np.sin(app_long * DEG))
    y = np.tan(eps * DEG / 2) ** 2
    eqt = (
        4
        / DEG
        * (
            y * np.sin(2 * l0 * DEG)
            - 2 * e * np.sin(m * DEG)
            + 4 * e * y * np.sin(m * DEG) * np.cos(2 * l0 * DEG)
            - 0.5 * y * y * np.sin(4 * l0 * DEG)
            - 1.25 * e * e * np.sin(2 * m * DEG)
        )
    )  # equation of time, minutes
    tst = np.mod(np.mod(t, 86400.0) / 60.0 + eqt + 4 * lon, 1440.0)
    ha = (tst / 4 - 180) * DEG
    phi = lat * DEG
    cos_z = np.clip(
        np.sin(phi) * np.sin(decl) + np.cos(phi) * np.cos(decl) * np.cos(ha), -1, 1
    )
    az = (
        np.arctan2(np.sin(ha), np.cos(ha) * np.sin(phi) - np.tan(decl) * np.cos(phi))
        / DEG
        + 180.0
    )
    return np.arccos(cos_z) / DEG, np.mod(az, 360.0)


def extra_dni(t_utc_s):
    """Irradiance at the top of the atmosphere, normal to the sun (W/m2)."""
    days = (np.asarray(t_utc_s, dtype=float) - _J2000_S) / 86400.0
    b = 2 * np.pi * np.mod(days, 365.2422) / 365.2422
    return 1361.0 * (
        1.00011
        + 0.034221 * np.cos(b)
        + 0.00128 * np.sin(b)
        + 0.000719 * np.cos(2 * b)
        + 0.000077 * np.sin(2 * b)
    )


def airmass(zen_deg):
    """Kasten-Young relative air mass."""
    z = np.minimum(np.asarray(zen_deg, dtype=float), 89.9)
    return 1.0 / (np.cos(z * DEG) + 0.50572 * (96.07995 - z) ** -1.6364)


def clear_sky(t_utc_s, zen_deg, altitude_m: float = 0.0, linke: float = 3.0):
    """Ineichen-Perez clear-sky (GHI, DNI, DHI) in W/m2."""
    zen_deg = np.asarray(zen_deg, dtype=float)
    cosz = np.cos(zen_deg * DEG)
    am = airmass(zen_deg) * np.exp(-altitude_m / 8434.5)
    i0 = extra_dni(t_utc_s)
    fh1, fh2 = np.exp(-altitude_m / 8000.0), np.exp(-altitude_m / 1250.0)
    cg1, cg2 = 5.09e-5 * altitude_m + 0.868, 3.92e-5 * altitude_m + 0.0387
    ghi = (
        cg1
        * i0
        * cosz
        * np.exp(-cg2 * am * (fh1 + fh2 * (linke - 1)))
        * np.exp(0.01 * am**1.8)
    )
    dni = (0.664 + 0.163 / fh1) * i0 * np.exp(-0.09 * am * (linke - 1))
    dni = np.minimum(dni, ghi / np.maximum(cosz, 1e-6))
    up = zen_deg < 90
    ghi = np.where(up, np.maximum(ghi, 0), 0.0)
    dni = np.where(up, np.maximum(dni, 0), 0.0)
    dhi = np.maximum(ghi - dni * np.maximum(cosz, 0), 0.0)
    return ghi, dni, dhi


def erbs(ghi, zen_deg, t_utc_s):
    """Split global horizontal irradiance into (DNI, DHI) with the Erbs correlation."""
    zen_deg = np.asarray(zen_deg, dtype=float)
    cosz = np.maximum(np.cos(zen_deg * DEG), 0.065)
    kt = np.clip(ghi / (extra_dni(t_utc_s) * cosz), 0, 1.0)
    df = np.where(
        kt <= 0.22,
        1 - 0.09 * kt,
        np.where(
            kt <= 0.8,
            0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4,
            0.165,
        ),
    )
    dhi = df * ghi
    dni = np.where(zen_deg < 87, (ghi - dhi) / cosz, 0.0)
    return np.maximum(dni, 0), np.maximum(dhi, 0)


def poa_parts(ghi, dni, dhi, zen_deg, az_deg, tilt, azimuth, t_utc_s, albedo=0.2):
    """Light on a tilted plane, split into (direct + circumsolar, sky + ground), W/m2.

    Kept apart so an array's scattered-light gain can be learned: a wall
    facing open, pale ground sees more scattered light than the textbook
    sky-and-ground model gives it."""
    zen_deg = np.asarray(zen_deg, dtype=float)
    tz, ta = tilt * DEG, azimuth * DEG
    z, a = zen_deg * DEG, np.asarray(az_deg, dtype=float) * DEG
    cos_aoi = np.clip(
        np.cos(z) * np.cos(tz) + np.sin(z) * np.sin(tz) * np.cos(a - ta), 0, 1
    )
    ai = np.clip(dni / np.maximum(extra_dni(t_utc_s), 1), 0, 1)  # anisotropy index
    rb = cos_aoi / np.maximum(np.cos(z), 0.087)
    iam = np.clip(1 - 0.05 * (1 / np.maximum(cos_aoi, 0.05) - 1), 0, 1)
    up = zen_deg < 90
    direct = np.where(up, dni * cos_aoi * iam + dhi * ai * rb, 0.0)
    scattered = np.where(
        up,
        dhi * (1 - ai) * (1 + np.cos(tz)) / 2 + ghi * albedo * (1 - np.cos(tz)) / 2,
        0.0,
    )
    return direct, scattered


def poa(ghi, dni, dhi, zen_deg, az_deg, tilt, azimuth, t_utc_s, diffuse_gain=1.0):
    """Plane-of-array irradiance, W/m2: direct part + diffuse_gain x scattered part."""
    d, s = poa_parts(ghi, dni, dhi, zen_deg, az_deg, tilt, azimuth, t_utc_s)
    return d + diffuse_gain * s


def cell_temp(poa_w, air_c, wind_ms=1.0):
    return air_c + poa_w * (45.0 - 20.0) / 800.0 * 9.5 / (
        5.7 + 3.8 * np.maximum(wind_ms, 0.0)
    )


def dc_power(poa_w, kwp, air_c=25.0, wind_ms=1.0, gamma=-0.004):
    """DC kW of an array of `kwp` peak kW under `poa_w`, after heat loss."""
    t = cell_temp(poa_w, air_c, wind_ms)
    return np.maximum(kwp * poa_w / 1000.0 * (1 + gamma * (t - 25.0)), 0.0)
