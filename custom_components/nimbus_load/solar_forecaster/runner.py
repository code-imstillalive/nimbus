"""The forecaster's jobs. Each is a plain blocking function: Home Assistant runs them in an executor.

- `fetch_weather`: the newest run of each model (once a day, the cycle the model trains on) and,
  a few days per call, the evening-before runs of the history it trains on;
- `measured_5min` / `hourly_to_5min`: the arrays' recorded output as 5-minute means (raw history
  where the recorder still has it, long-term hourly statistics spread by the clear-sky shape before);
- `train`: learn the arrays and the model, with the honest blocked-cross-validation score;
- `forecast`: p10 / p50 / p90 for the next days, corrected by the last 30 minutes measured.
"""

from __future__ import annotations

import datetime as dt
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import numpy as np

from . import model as M
from . import sources
from . import weather as W
from .site import SLOT_S, Array, Site, clear_power, fit, local_day, local_offset
from .store import WeatherStore

LIVE_WINDOW_S = 1800.0  # the last 30 minutes measured correct the next hours

USER_AGENT = "Nimbus solar forecaster (+https://github.com/code-imstillalive/nimbus)"


def http_get(url: str, byte_range=None, tries: int = 5, timeout: float = 60.0) -> bytes:
    """GET with an optional byte range; backs off on throttling (429/503) and network errors."""
    for k in range(tries):
        headers = {"User-Agent": USER_AGENT}
        if byte_range is not None:
            start, end = byte_range
            headers["Range"] = f"bytes={start}-{'' if end is None else end}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=timeout
            ) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or k == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if k == tries - 1:
                raise
        time.sleep(2 + 4 * k)
    raise RuntimeError("unreachable")


@dataclass
class FetchReport:
    fetched: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    backfilled_days: int = 0


def _day_window(day: dt.date, tz) -> tuple[float, float]:
    """A local day as UTC seconds, from its own local midnight to the next (23 or 25 hours on the
    days daylight saving changes)."""

    def midnight(d: dt.date) -> float:
        if isinstance(tz, (int, float)):
            return dt.datetime.combine(d, dt.time(0), dt.UTC).timestamp() - float(tz)
        return dt.datetime.combine(d, dt.time(0), tz).timestamp()

    return midnight(day), midnight(day + dt.timedelta(days=1))


def fetch_weather(
    store: WeatherStore,
    lat: float,
    lon: float,
    now: dt.datetime,
    tz,
    history_days: int,
    get=http_get,
    backfill_per_call: int = 3,
) -> FetchReport:
    """Bring the store up to date. Safe to call often: anything already stored is skipped."""
    rep = FetchReport()
    for source, sp in sources.SPECS.items():
        run = sources.newest_run(source, now)
        if not store.has(source, run):
            steps = sources.steps_for(
                source,
                run,
                now.timestamp() - 3 * 3600,
                run.timestamp() + sp.horizon_h * 3600,
                lat,
                lon,
            )
            night = []
            if (
                source == "ecmwf"
            ):  # the weather strip shows rain and storms at night too
                every = sources.steps_for(
                    source,
                    run,
                    now.timestamp() - 3 * 3600,
                    run.timestamp() + sp.horizon_h * 3600,
                )
                night = [s for s in every if s not in steps]
            got = sources.fetch_run(
                source, run, steps, lat, lon, get, night_steps=night
            )
            day_got = sum(s in got for s in steps)
            if day_got < 0.8 * len(steps):
                rep.failed[source] = f"{day_got} of {len(steps)} files"
            else:
                store.save(source, run, sources.series(source, run, got))
                rep.fetched.append(f"{source} {run:%Y-%m-%d %H}Z")
    # the weather refresh (the strip and weather entity only): ECMWF's newest 12Z run, when it is
    # newer than the 00Z run the forecast uses
    wr = sources.newest_refresh_run(now)
    main = store.runs("ecmwf")
    if (
        wr is not None
        and (not main or wr > main[-1])
        and not store.has(sources.WEATHER_REFRESH, wr)
    ):
        span = [
            s
            for s in range(
                0, sources.WEATHER_REFRESH_H + 1, sources.SPECS["ecmwf"].step_h
            )
            if wr.timestamp() + s * 3600 >= now.timestamp() - 3 * 3600
        ]
        got = sources.fetch_run("ecmwf", wr, [], lat, lon, get, night_steps=span)
        if span and len(got) >= 0.8 * len(span):
            store.save(sources.WEATHER_REFRESH, wr, sources.series("ecmwf", wr, got))
            rep.fetched.append(f"ecmwf weather {wr:%Y-%m-%d %H}Z")
    # history: the evening-before runs of the last `history_days` local days, newest first
    today = dt.datetime.fromtimestamp(
        now.timestamp() + float(local_offset(np.array([now.timestamp()]), tz)[0]),
        dt.UTC,
    ).date()
    budget = backfill_per_call
    for back in range(1, history_days + 1):
        if budget <= 0:
            break
        day = today - dt.timedelta(days=back)
        t0, t1 = _day_window(day, tz)
        missing = [
            s
            for s in sources.SPECS
            if not store.has(s, sources.evening_before_run(s, day))
        ]
        if not missing:
            continue
        for source in missing:
            run = sources.evening_before_run(source, day)
            steps = sources.steps_for(source, run, t0, t1, lat, lon)
            got = sources.fetch_run(source, run, steps, lat, lon, get)
            if len(got) >= 0.8 * len(steps):
                store.save(source, run, sources.series(source, run, got))
        rep.backfilled_days += 1
        budget -= 1
    store.prune(max(history_days, 7) + 10, now)
    return rep


def measured_5min(
    points: list[tuple[float, float]], t0: float, t1: float
) -> tuple[np.ndarray, np.ndarray]:
    """A sensor's state changes [(utc seconds, kW)] -> time-weighted 5-minute means on [t0, t1).
    A slot is NaN unless at least half of it was covered by a valid reading."""
    grid = np.arange(np.floor(t0 / SLOT_S) * SLOT_S, t1, SLOT_S)
    out = np.full(len(grid), np.nan)
    if not points:
        return grid, out
    pts = sorted(points)
    ts = np.array([p[0] for p in pts], dtype=float)
    vs = np.array([p[1] for p in pts], dtype=float)
    ends = np.append(ts[1:], t1)
    acc = np.zeros(len(grid))
    cov = np.zeros(len(grid))
    for a, b, v in zip(ts, ends, vs, strict=False):
        if not np.isfinite(v) or b <= a:
            continue
        i0 = max(int((a - grid[0]) // SLOT_S), 0)
        i1 = min(int((b - grid[0]) // SLOT_S) + 1, len(grid))
        for i in range(i0, i1):
            lo, hi = max(a, grid[i]), min(b, grid[i] + SLOT_S)
            if hi > lo:
                acc[i] += v * (hi - lo)
                cov[i] += hi - lo
    ok = cov >= SLOT_S / 2
    out[ok] = acc[ok] / cov[ok]
    return grid, out


def hourly_to_5min(
    site: Site, array: Array, hours: list[tuple[float, float]], grid: np.ndarray
) -> np.ndarray:
    """Long-term statistics (hourly mean kW, keyed by the hour's start) spread over each hour's twelve
    5-minute slots in the shape of this array's clear-sky output, so the hour's mean is kept."""
    out = np.full(len(grid), np.nan)
    if not hours:
        return out
    probe = Array(
        array.key,
        array.tilt if array.tilt is not None else 20.0,
        array.azimuth if array.azimuth is not None else 0.0,
        1.0,
    )
    for start, mean in hours:
        if mean is None or not np.isfinite(mean):
            continue
        i0 = round((start - grid[0]) / SLOT_S)
        if i0 < 0 or i0 + 12 > len(grid):
            continue
        cs = clear_power(site, [probe], grid[i0 : i0 + 12])
        out[i0 : i0 + 12] = mean * cs / cs.mean() if cs.mean() > 1e-3 else mean
    return out


def train(
    store: WeatherStore, site: Site, arrays: list[Array], t, measured: dict, tz
) -> M.Trained:
    """Learn the arrays (in place) and the model, from 5-minute measured means per array on grid t."""
    t = np.asarray(t, dtype=float)
    days = sorted(
        {
            dt.date(1970, 1, 1) + dt.timedelta(days=int(d))
            for d in np.unique(local_day(t, tz))
        }
    )
    weather = store.dayahead(days, lambda d: _day_window(d, tz))
    air = None
    if "ecmwf" in weather and "t2m" in weather["ecmwf"]:
        e = weather["ecmwf"]
        ok = ~np.isnan(e["t2m"])
        if ok.sum() > 1:
            air = np.interp(t, e["t"][ok], e["t2m"][ok])
    fit(site, arrays, t, measured, tz, air)
    keys = [a.key for a in arrays if a.active and a.key in measured]
    if not keys:
        raise ValueError("no array produces any output")
    total = np.sum([np.asarray(measured[k], dtype=float) for k in keys], axis=0)
    trained = M.train(site, arrays, t, total, weather, tz)
    trained.report["lead"] = lead_bias(trained, store, site, arrays, t, total, tz)
    return trained


#: Days ahead whose bias is learned (1 = the evening-before forecast the model is trained on).
LEADS = (2, 3, 4)
#: A lead's correction is used (and the plan may use that day) once this many past days scored it.
LEAD_MIN_DAYS = 10
LEAD_FACTOR_RANGE = (0.8, 1.3)


def lead_bias(
    trained, store: WeatherStore, site: Site, arrays: list[Array], t, total, tz
) -> dict:
    """{lead: {"days", "factor", "error_pct"}}: how the forecast made `lead` days ahead (from the
    runs this install stored then) compared with what the panels made. Without a correction the
    model, trained on evening-before runs, reads further-out runs low (measured on the reference
    household: -4 to -6 kWh a day 3-4 days out); the factor is total measured / total forecast."""
    t = np.asarray(t, dtype=float)
    total = np.asarray(total, dtype=float)
    day = local_day(t, tz)
    out = {}
    for lead in LEADS:
        pred, act = [], []
        for d in np.unique(day):
            k = (day == d) & ~np.isnan(total)
            if k.sum() < 200:
                continue
            date = dt.date(1970, 1, 1) + dt.timedelta(days=int(d))
            w = store.dayahead([date], lambda x: _day_window(x, tz), lead=lead)
            if not {"ecmwf", "gfs"} <= set(w):
                continue
            _, mid, _, tier = M.predict(trained, site, arrays, t[k], w, tz)
            full = np.isin(
                tier.astype(str), [n for n in M.TIERS if n not in M.NOT_FOR_PLAN]
            )
            if np.isnan(mid).any() or full.mean() < 0.9:
                continue
            pred.append(float(mid.sum() / 12))
            act.append(float(total[k].sum() / 12))
        if len(pred) < LEAD_MIN_DAYS or sum(pred) <= 0:
            out[lead] = {"days": len(pred)}
            continue
        p, a = np.array(pred), np.array(act)
        f = float(np.clip(a.sum() / p.sum(), *LEAD_FACTOR_RANGE))
        out[lead] = {
            "days": len(pred),
            "factor": round(f, 3),
            "error_pct": round(
                100 * float(np.mean(np.abs(f * p - a))) / float(a.mean()), 1
            ),
        }
    return out


def low_priority() -> None:
    """Worker-process initializer: run below everything else on the machine, so the weather decode
    and the nightly training never slow Home Assistant or the Solver (a no-op where unsupported)."""
    import os

    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass


def train_job(
    store: WeatherStore, site: Site, arrays: list[Array], t, measured: dict, tz
):
    """`train` for a worker process: it learns `site` and `arrays` in place, and a separate process
    cannot change the caller's copies, so they come back with the model."""
    trained = train(store, site, arrays, t, measured, tz)
    return trained, site, arrays


@dataclass
class Forecast:
    rows: list[dict]  # {"time": iso, "value": kW, "lower": kW, "upper": kW}
    days: list[dict]  # {"date", "p10", "p50", "p90", "clear"} kWh per local day
    tier: str
    live_ratio: float | None
    now_kw: float | None
    # the plan may use the forecast up to here (UTC seconds; None: not at all): the slots from now
    # on until the first one a tier in model.NOT_FOR_PLAN (or no tier) covers
    plan_until: float | None = None


def wh_hours(
    rows: list[dict], keep: dict[str, float] | None = None, since: float | None = None
) -> dict[str, float]:
    """Expected Wh per clock hour (keyed by the hour's start, UTC ISO) from the forecast rows (kW;
    15-minute rows, then hourly): what Home Assistant's Energy dashboard plots as a solar forecast.
    `keep`: the previous result, whose hours from `since` (local midnight) up to the first row's
    hour are kept, so today's earlier hours stay on the dashboard as they were forecast."""
    ts = [dt.datetime.fromisoformat(r["time"]).timestamp() for r in rows]
    out: dict[float, float] = {}
    for i, r in enumerate(rows):
        # each row stands for its own spacing (15 minutes, later an hour): the smaller gap to a
        # neighbour, so a gap with no rows (no weather there) is never filled in
        gaps = [ts[j + 1] - ts[j] for j in (i - 1, i) if 0 <= j and j + 1 < len(ts)]
        step = min([*gaps, 3600.0])
        h = ts[i] // 3600 * 3600
        out[h] = out.get(h, 0.0) + max(float(r["value"]), 0.0) * 1000 * step / 3600
    first = min(out) if out else None
    merged: dict[str, float] = {}
    for k, v in (keep or {}).items():
        t = dt.datetime.fromisoformat(k).timestamp()
        if since is not None and t >= since and (first is None or t < first):
            merged[k] = v
    for h, v in sorted(out.items()):
        merged[dt.datetime.fromtimestamp(h, dt.UTC).isoformat()] = round(v, 1)
    return merged


def weather_strip(store: WeatherStore, site: Site, now: dt.datetime) -> list[dict]:
    """The weather strip (and weather.nimbus_weather): one entry per 3-hour ECMWF interval from a
    day back to the end of the newest run. Needs only the downloaded weather, not a trained model,
    so it shows from the first download, a week before the first solar forecast."""
    t0 = now.timestamp()
    ec = store.latest().get("ecmwf")
    refresh = store.runs(sources.WEATHER_REFRESH)
    main = store.runs("ecmwf")
    if refresh and ec and (not main or refresh[-1] > main[-1]):
        ec = overlay(ec, store.load(sources.WEATHER_REFRESH, refresh[-1]))
    return W.strip(ec, site.lat, site.lon, t0 - 86400, t0 + 6 * 86400)


def overlay(base: dict, newer: dict | None) -> dict:
    """`base` with `newer`'s values wherever `newer` has them (same 3-hour interval times)."""
    if not newer or not len(newer.get("t", ())):
        return base
    out = {k: np.array(v, dtype=float, copy=True) for k, v in base.items()}
    pos = {float(t): i for i, t in enumerate(out["t"])}
    extra = [j for j, t in enumerate(newer["t"]) if float(t) not in pos]
    for k, v in newer.items():
        if k == "t" or k not in out:
            continue
        for j, t in enumerate(newer["t"]):
            i = pos.get(float(t))
            if i is not None and not np.isnan(v[j]):
                out[k][i] = v[j]
    if extra:  # intervals only the newer run has (past the end of the base)
        for k, v in list(out.items()):
            add = newer[k][extra] if k in newer else np.full(len(extra), np.nan)
            out[k] = np.concatenate([v, add])
        order = np.argsort(out["t"])
        out = {k: v[order] for k, v in out.items()}
    return out


def _most(tiers) -> str:
    """The tier that covers most of these slots ("" if none)."""
    used = [str(x) for x in tiers if x]
    return max(set(used), key=used.count) if used else ""


def forecast(
    trained: M.Trained,
    store: WeatherStore,
    site: Site,
    arrays: list[Array],
    now: dt.datetime,
    tz,
    recent: tuple[np.ndarray, np.ndarray] | None = None,
    hours: int = 96,
    tau_s: float = 5400.0,
) -> Forecast:
    """p10/p50/p90 kW in 5-minute steps for `hours` ahead, published as 15-minute rows for 48 h and
    hourly after. Corrected through the day by the last 30 minutes measured (`recent` = (t, kW)),
    the correction fading with an e-folding of 90 minutes."""
    learned = (trained.report.get("lead", {}) if trained is not None else {}) or {}
    t0 = np.floor(now.timestamp() / SLOT_S) * SLOT_S
    # predicted from 30 minutes back, so each recent reading is compared with what was forecast
    # for that same moment; only the rows from now on are published
    t_all = np.arange(t0 - LIVE_WINDOW_S, t0 + hours * 3600, SLOT_S)
    wx = store.latest()
    lo, mid, hi, tier = M.predict(trained, site, arrays, t_all, wx, tz)
    past = t_all < t0
    t_past, mid_past = t_all[past], mid[past]
    t, lo, mid, hi, tier = t_all[~past], lo[~past], mid[~past], hi[~past], tier[~past]
    clear = clear_power(site, arrays, t)
    ok = ~np.isnan(mid)
    # days ahead of each slot, counted from the newest ECMWF run (1 = what the evening-before run
    # forecasts); a lead whose bias this install has learned is corrected by it
    runs = store.runs("ecmwf") if hasattr(store, "runs") else []
    if runs:
        run_day = runs[-1].date()
        ahead = np.array(
            [
                (dt.date(1970, 1, 1) + dt.timedelta(days=int(d)) - run_day).days
                for d in local_day(t, tz)
            ]
        )
    else:
        ahead = np.ones(len(t), dtype=int)
    fac_lead = np.array(
        [
            float((learned.get(int(k)) or {}).get("factor", 1.0)) if k >= 2 else 1.0
            for k in ahead
        ]
    )
    lo, mid, hi = lo * fac_lead, mid * fac_lead, hi * fac_lead
    ratio = None
    if recent is not None and ok.any():
        rt, rk = recent
        k = (
            (rt > now.timestamp() - LIVE_WINDOW_S)
            & (rt <= now.timestamp())
            & np.isfinite(rk)
        )
        tb, mb = np.append(t_past, t), np.append(mid_past, mid)
        good = ~np.isnan(mb)
        f = np.interp(rt[k], tb[good], mb[good]) if k.sum() >= 3 else np.array([])
        if len(f) and f.mean() >= 0.3:
            ratio = float(np.clip(rk[k].mean() / f.mean(), 0.2, 2.0))
            fac = 1 + (ratio - 1) * np.exp(-np.maximum(t - now.timestamp(), 0) / tau_s)
            cap = clear * 1.08
            lo, mid, hi = (
                np.minimum(lo * fac, cap),
                np.minimum(mid * fac, cap),
                np.minimum(hi * fac, cap),
            )
    rows = []
    for i in range(len(t)):
        if not ok[i]:
            continue
        step = 3 if t[i] - t0 < 48 * 3600 else 12  # 15-minute rows, then hourly
        if (i % step) != 0:
            continue
        j = slice(i, min(i + step, len(t)))
        rows.append(
            {
                "time": dt.datetime.fromtimestamp(t[i], dt.UTC).isoformat(),
                "value": round(float(np.nanmean(mid[j])), 3),
                "lower": round(float(np.nanmean(lo[j])), 3),
                "upper": round(float(np.nanmean(hi[j])), 3),
                "clear": round(
                    float(np.nanmean(clear[j])), 3
                ),  # what a clear sky would give
            }
        )
    days = []
    day = local_day(t, tz)
    today = day[0] if len(day) else None
    for d in np.unique(day):
        m = (day == d) & ok
        if m.sum() < 200 and not (d == today and m.any()):
            continue  # the horizon's last, cut-off day (today's remainder is always kept)
        days.append(
            {
                "date": str(dt.date(1970, 1, 1) + dt.timedelta(days=int(d))),
                "p10": round(float(lo[m].sum() / 12), 1),
                "p50": round(float(mid[m].sum() / 12), 1),
                "p90": round(float(hi[m].sum() / 12), 1),
                "clear": round(float(clear[m].sum() / 12), 1),
                # the models behind that day's daylight (days 3-5: GFS alone)
                "model": _most(tier[m & (clear > 0)]) or _most(tier[m]),
                "partial": bool(d == today),  # today: from now to midnight only
            }
        )
    first = np.flatnonzero(ok)
    # 1 and 2 days ahead of the newest ECMWF run are today and tomorrow until the evening run
    # lands, always planned on (as before); 3 and 4 only once their bias is learned
    lead_ok = np.array(
        [k <= 2 or "factor" in (learned.get(int(k)) or {}) for k in ahead]
    )
    proven = ok & lead_ok & ~np.isin(tier.astype(str), list(M.NOT_FOR_PLAN))
    if proven.all():
        plan_until = float(t[-1] + SLOT_S) if len(t) else None
    else:
        cut = int(np.argmin(proven))  # the first slot the plan may not use
        plan_until = float(t[cut]) if cut > 0 else None
    return Forecast(
        rows=rows,
        days=days,
        tier=str(tier[first[0]])
        if len(first)
        else "",  # the models behind the forecast now
        plan_until=plan_until,
        live_ratio=None if ratio is None else round(ratio, 3),
        now_kw=round(float(mid[0]), 3)
        if ok[0]
        else None,  # no weather covers now: unknown
    )
