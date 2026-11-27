"""The day-ahead model: three weather models -> quantile GBRT -> p10 / p50 / p90 kW every 5 minutes.

Features per 5-minute slot (columns in `features()`):

| cols  | from  | what |
|-------|-------|------|
| 0-3   | ECMWF | clear-sky index, its whole-day view, total cloud, low cloud |
| 4-7   | GEFS  | ensemble-mean clear-sky index, whole-day view, ensemble spread, mean cloud |
| 8-9   | GFS   | clear-sky index, whole-day view |
| 10-13 | sun   | height, direction (cos, sin), forecast air temperature |

Target: measured output / clear-sky output of the install's own arrays. The
sun's height and direction let it learn the install's own shading (trees or
buildings on one side) without any manual "dampening" factors.

Each tier is the same model trained without one or two sources; a slot that
lacks a source falls to the best tier that has what it needs, so one
provider's outage degrades the forecast a little instead of stopping it.

The p10 / p90 band is widened by a factor fitted in blocked cross-validation
(contiguous weeks held out) until 80 % of real readings fall inside it, so
the Solver's p10 / p90 scenarios are calibrated.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np

from ..ml import gbrt as G
from . import physics as ph
from .site import Array, Site, clear_index, clear_power, local_day

TIERS = {
    "ecmwf+gefs+gfs": list(range(14)),
    "ecmwf+gfs": [0, 1, 2, 3, 8, 9, 10, 11, 12, 13],
    "ecmwf+gefs": list(range(8)) + [10, 11, 12, 13],
    "gefs+gfs": list(range(4, 14)),
}
TIER_SOURCES = {
    "ecmwf+gefs+gfs": "egf",
    "ecmwf+gfs": "ef",
    "ecmwf+gefs": "eg",
    "gefs+gfs": "gf",
}
SOURCE_KEY = {"e": "ecmwf", "g": "gefs", "f": "gfs"}
# Simpler beat deeper on the reference household's 44 days (12.5 % against 13-14 % daily error).
PARAMS = {
    "n_estimators": 80,
    "max_depth": 2,
    "learning_rate": 0.1,
    "min_samples_leaf": 60,
}
QUANTILES = (0.1, 0.5, 0.9)
TARGET_COVER = 0.80


def _interp(t, src_t, src_v, default):
    ok = ~np.isnan(src_v)
    if ok.sum() < 2:
        return np.full(len(t), default)
    return np.interp(t, src_t[ok], src_v[ok])


def covered(t, weather: dict, need: str) -> np.ndarray:
    """Slots whose time lies inside every needed source's data."""
    m = np.ones(len(t), bool)
    for k in need:
        s = weather.get(SOURCE_KEY[k])
        if not s or len(s["t"]) < 2:
            return np.zeros(len(t), bool)
        m &= (t >= s["t"].min()) & (t <= s["t"].max())
    return m


def _kt(site, t, s):
    kt = clear_index(site, s["t"], s["ghi"])
    return _interp(t, s["t"], kt, 0.7)


def features(site: Site, arrays: list[Array], t, weather: dict, tz):
    """Feature matrix, clear-sky kW, sun height and local day for grid t. A missing source's
    columns are zeros (only tiers that do not use them read those slots)."""
    t = np.asarray(t, dtype=float)
    z = np.zeros(len(t))
    e, g, f = weather.get("ecmwf"), weather.get("gefs"), weather.get("gfs")
    ktc = _kt(site, t, e) if e else z
    ccc = _interp(t, e["t"], e.get("cloud", z[:0]), 50) if e and "cloud" in e else z
    lcc = _interp(t, e["t"], e["low_cloud"], 50) if e and "low_cloud" in e else ccc
    kte = _kt(site, t, g) if g else z
    if g and "ghi_sd" in g:
        sdi = clear_index(site, g["t"], g["ghi_sd"])
        spread = _interp(t, g["t"], sdi, 0.0)
    else:
        spread = z
    cc = _interp(t, g["t"], g["cloud"], 50) if g and "cloud" in g else z
    kt = _kt(site, t, f) if f else z
    air = z + 25.0
    for s in (e, f):
        if s and "t2m" in s:
            air = _interp(t, s["t"], s["t2m"], 25.0)
            break
    clear = clear_power(site, arrays, t)
    zen, az = ph.sun_position(t + 150, site.lat, site.lon)
    elev = 90 - zen
    day = local_day(t, tz)
    sun = elev > 3

    def whole_day(v):
        out = np.full(len(t), 0.7)
        for d in np.unique(day):
            k = (day == d) & sun
            if k.any():
                out[day == d] = np.average(v[k], weights=clear[k] + 1e-6)
        return out

    X = np.column_stack(
        [
            ktc,
            whole_day(ktc),
            ccc,
            lcc,
            kte,
            whole_day(kte),
            spread,
            cc,
            kt,
            whole_day(kt),
            elev,
            np.cos(np.radians(az)),
            np.sin(np.radians(az)),
            air,
        ]
    )
    return X, clear, elev, day


def _fit3(X, y):
    out = {}
    for q in QUANTILES:
        m = G.GBRT(**PARAMS, quantile=q)
        m.fit(X, y)
        out[q] = m
    return out


@dataclass
class Trained:
    tiers: dict
    band_scale: float
    trained_at: float
    report: dict = field(default_factory=dict)


def train(site, arrays, t, measured_total, weather, tz, folds: int = 6) -> Trained:
    """Blocked cross-validation for the honest score and the band width, then a fit on all."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(measured_total, dtype=float)
    m = covered(t, weather, "egf") & ~np.isnan(y)
    t, y = t[m], y[m]
    X, clear, elev, day = features(site, arrays, t, weather, tz)
    sun = elev > 3
    target = np.clip(np.where(clear > 0.05, y / np.maximum(clear, 0.05), 0), 0, 1.3)
    days = np.unique(day)
    report: dict = {"days": len(days)}
    scale = 1.2
    if len(days) >= folds * 2:
        fold_of = {d: k * folds // len(days) for k, d in enumerate(days)}
        fid = np.array([fold_of[d] for d in day])
        pr = {q: np.zeros(len(t)) for q in QUANTILES}
        for f in range(folds):
            tr, te = (fid != f) & sun, (fid == f) & sun
            ms = _fit3(X[tr], target[tr])
            for q in QUANTILES:
                pr[q][te] = ms[q].predict(X[te])
        cap = clear * 1.08
        p50 = np.clip(pr[0.5], 0, 1.08) * clear * sun
        lo = np.minimum(np.clip(pr[0.1], 0, 1.08) * clear * sun, p50)
        hi = np.maximum(np.clip(pr[0.9], 0, 1.08) * clear * sun, p50)
        judge = sun & (elev > 5)
        for s in np.arange(1.0, 2.51, 0.05):
            l2, h2 = p50 - s * (p50 - lo), np.minimum(p50 + s * (hi - p50), cap)
            if (
                np.mean((y[judge] >= l2[judge]) & (y[judge] <= h2[judge]))
                >= TARGET_COVER
            ):
                scale = round(float(s), 2)
                break
        l2, h2 = (
            np.maximum(p50 - scale * (p50 - lo), 0),
            np.minimum(p50 + scale * (hi - p50), cap),
        )
        rows = []
        for d in days:
            k = day == d
            if k.sum() >= 200:
                rows.append(
                    {
                        "date": str(dt.date(1970, 1, 1) + dt.timedelta(days=int(d))),
                        "actual_kwh": round(float(y[k].sum() / 12), 1),
                        "forecast_kwh": round(float(p50[k].sum() / 12), 1),
                        "p10_kwh": round(float(l2[k].sum() / 12), 1),
                        "p90_kwh": round(float(h2[k].sum() / 12), 1),
                    }
                )
        if rows:
            act = np.mean([r["actual_kwh"] for r in rows])
            report.update(
                {
                    "scored_days": len(rows),
                    "daily_error_pct": round(
                        100
                        * float(
                            np.mean(
                                [abs(r["forecast_kwh"] - r["actual_kwh"]) for r in rows]
                            )
                        )
                        / max(act, 1e-6),
                        1,
                    ),
                    "p10_p90_cover": round(
                        float(
                            np.mean((y[judge] >= l2[judge]) & (y[judge] <= h2[judge]))
                        ),
                        3,
                    ),
                    "rows": rows,
                }
            )
    report["band_scale"] = scale
    tiers = {name: _fit3(X[sun][:, cols], target[sun]) for name, cols in TIERS.items()}
    return Trained(
        tiers=tiers,
        band_scale=scale,
        trained_at=dt.datetime.now(dt.UTC).timestamp(),
        report=report,
    )


def predict(trained: Trained, site, arrays, t, weather, tz):
    """(p10, p50, p90, tier) on grid t; NaN where no tier's sources cover a slot."""
    t = np.asarray(t, dtype=float)
    out = [np.full(len(t), np.nan) for _ in range(3)]
    tier = np.full(len(t), "", dtype=object)
    done = np.zeros(len(t), bool)
    for name, cols in TIERS.items():
        m = covered(t, weather, TIER_SOURCES[name]) & ~done
        if m.sum() < 12:
            continue
        X, clear, elev, _ = features(site, arrays, t[m], weather, tz)
        up = elev > -1
        q = {
            k: np.clip(g.predict(X[:, cols]), 0, 1.08) * clear * up
            for k, g in trained.tiers[name].items()
        }
        p50, s = q[0.5], trained.band_scale
        out[0][m] = np.maximum(p50 - s * (p50 - np.minimum(q[0.1], p50)), 0)
        out[1][m] = p50
        out[2][m] = np.minimum(p50 + s * (np.maximum(q[0.9], p50) - p50), clear * 1.08)
        tier[m] = name
        done |= m
    return out[0], out[1], out[2], tier
