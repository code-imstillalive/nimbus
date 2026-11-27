"""Nimbus's own solar forecaster (`solar_forecaster/`), on synthetic data only.

One test, from weather files to a forecast, in two named parts:

1. `_reads_weather_files`: the GRIB2 reader decodes
   simple packing and CCSDS (AEC) packing exactly, and each source's
   averages/accumulations become plain interval means.
2. `_learns_an_install_and_forecasts`: the physics is
   sane, an install's arrays are learned from its measured output (size,
   unused inputs), and the model trains, forecasts p10 <= p50 <= p90, and
   falls back to a smaller tier when one weather source is missing.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import struct

import _ml_path  # noqa: F401
import numpy as np
import pytest
from nimbus_load.solar_forecaster import (
    grib,
    model,
    physics,
    runner,
    site,
    sources,
    store,
)

# --- tiny GRIB2 writer (test side only) -------------------------------------


def _section(num: int, body: bytes) -> bytes:
    return struct.pack(">IB", 5 + len(body), num) + body


def _grib(ni, nj, la1, lo1, la2, lo2, sec5_body: bytes, data: bytes) -> bytes:
    sm = lambda v: struct.pack(">I", v if v >= 0 else (1 << 31) | -v)
    s3 = bytearray(72 - 5)
    s3[12 - 5 : 14 - 5] = struct.pack(">H", 0)
    s3[30 - 5 : 34 - 5] = struct.pack(">I", ni)
    s3[34 - 5 : 38 - 5] = struct.pack(">I", nj)
    s3[46 - 5 : 50 - 5] = sm(int(la1 * 1e6))
    s3[50 - 5 : 54 - 5] = sm(int(lo1 * 1e6))
    s3[55 - 5 : 59 - 5] = sm(int(la2 * 1e6))
    s3[59 - 5 : 63 - 5] = sm(int(lo2 * 1e6))
    body = (
        _section(1, bytes(16))
        + _section(3, bytes(s3))
        + _section(4, bytes(29))
        + _section(5, sec5_body)
        + _section(6, bytes([255]))
        + _section(7, data)
        + b"7777"
    )
    return b"GRIB" + bytes([0, 0, 0, 2]) + struct.pack(">Q", 16 + len(body)) + body


def _sec5(npts, tpl, ref, e, d, nbits, extra=b""):
    sm16 = lambda v: v if v >= 0 else 0x8000 | -v
    return struct.pack(">IHfHHB", npts, tpl, ref, sm16(e), sm16(d), nbits) + extra


class _Bits:
    def __init__(self):
        self.b = []

    def put(self, v, n):
        self.b += [(v >> (n - 1 - i)) & 1 for i in range(n)]

    def bytes(self):
        b = self.b + [0] * (-len(self.b) % 8)
        return bytes(
            int("".join(map(str, b[i : i + 8])), 2) for i in range(0, len(b), 8)
        )


def _aec_encode(x, nbits, bs, rsi, pad=False):
    """CCSDS 121 encoder with the unit-delay predictor, using every block option the decoder
    must read: split (k), a zero block, the second extension (with and without the reference
    sample), and stored-as-is; `pad` byte-aligns each RSI (flag 32)."""
    xmax = (1 << nbits) - 1
    id_len = 4  # 8 < nbits <= 16
    w = _Bits()
    for r0 in range(0, len(x), bs * rsi):
        if pad:
            w.b += [0] * (-len(w.b) % 8)
        seg = x[r0 : r0 + bs * rsi]
        mapped = [seg[0]]
        for prev, cur in itertools.pairwise(seg):
            th = min(prev, xmax - prev)
            diff = cur - prev
            if 0 <= diff <= th:
                mapped.append(2 * diff)
            elif -th <= diff < 0:
                mapped.append(-2 * diff - 1)
            else:
                mapped.append(th + abs(diff))
        for b in range(0, len(mapped), bs):
            blk = mapped[b : b + bs]
            first = b == 0
            vals = blk[1:] if first else blk
            if all(v == 0 for v in vals) and not first:
                w.put(0, id_len)  # low entropy
                w.put(0, 1)  # zero block
                w.put(1, 1)  # one zero block (fs = 0)
                continue
            if b == 2 * bs or (first and (r0 // (bs * rsi)) % 2):
                # second extension: each pair is one unary number, (a+b)(a+b+1)/2 + b; with the
                # reference sample the first code carries one value only
                w.put(0, id_len)
                w.put(1, 1)
                if first:
                    w.put(blk[0], nbits)
                rest = list(vals)
                if first:
                    v = rest.pop(0)
                    w.put(1, v * (v + 1) // 2 + v + 1)
                for a, c in zip(rest[::2], rest[1::2], strict=True):
                    w.put(1, (a + c) * (a + c + 1) // 2 + c + 1)
                continue
            if b == bs:  # second block of each RSI: stored as is
                w.put((1 << id_len) - 1, id_len)
                for v in vals:
                    w.put(v, nbits)
                continue
            k = 2
            w.put(k + 1, id_len)
            if first:
                w.put(blk[0], nbits)
            for v in vals:
                w.put(1, (v >> k) + 1)  # unary: (v >> k) zeros then a one
            for v in vals:
                w.put(v & ((1 << k) - 1), k)
    return w.bytes()


def _reads_weather_files():
    # part 1: simple packing (template 5.0), lat/lon grid, bilinear point read
    ni, nj, nbits = 5, 4, 12
    x = np.arange(ni * nj, dtype=np.int64) * 37 % 4000
    w = _Bits()
    for v in x:
        w.put(int(v), nbits)
    msg = _grib(
        ni, nj, 10.0, 100.0, 7.0, 104.0, _sec5(ni * nj, 0, 5.0, -1, 0, nbits), w.bytes()
    )
    f, lats, lons = grib.decode(msg)
    assert f.shape == (nj, ni)
    assert np.allclose(f.ravel(), 5.0 + x * 0.5)
    assert grib.at_point(f, lats, lons, 10.0, 101.0) == pytest.approx(f[0, 1])
    assert grib.at_point(f, lats, lons, 9.5, 101.5) == pytest.approx(f[:2, 1:3].mean())

    # part 2: CCSDS / AEC packing (template 5.42, what ECMWF and DWD ICON use), exact
    ni, nj, nbits, bs, rsi = 40, 30, 10, 16, 4
    rng = np.random.default_rng(1)
    field = (np.cumsum(rng.integers(-6, 7, ni * nj)) % 900 + 50).astype(np.int64)
    field[300:332] = field[299]  # a flat run: its mapped values are zero -> zero blocks
    data = _aec_encode([int(v) for v in field], nbits, bs, rsi)
    extra = bytes([0, 8, bs]) + struct.pack(
        ">H", rsi
    )  # type, flags (predictor), block size, RSI
    msg = _grib(
        ni,
        nj,
        -20.0,
        140.0,
        -27.25,
        149.75,
        _sec5(ni * nj, 42, 0.0, 0, 1, nbits, extra),
        data,
    )
    f, lats, lons = grib.decode(msg)
    assert np.allclose(f.ravel(), field / 10.0)
    # the same field with each RSI byte-aligned (flag 32) reads the same
    padded = _aec_encode([int(v) for v in field], nbits, bs, rsi, pad=True)
    extra_pad = bytes([0, 8 | 32, bs]) + struct.pack(">H", rsi)
    msg_pad = _grib(
        ni,
        nj,
        -20.0,
        140.0,
        -27.25,
        149.75,
        _sec5(ni * nj, 42, 0.0, 0, 1, nbits, extra_pad),
        padded,
    )
    assert padded != data and np.allclose(grib.decode(msg_pad)[0].ravel(), field / 10.0)
    # decoding can stop just past the site's rows and still read the site
    g, _, _ = grib.decode(msg, point=(-22.0, 145.0))
    assert grib.at_point(g, lats, lons, -22.0, 145.0) == pytest.approx(
        grib.at_point(f, lats, lons, -22.0, 145.0)
    )

    # part 3: each source's averages become plain interval means
    run = dt.datetime(2026, 1, 1, 6, tzinfo=dt.UTC)
    hourly = [
        100.0,
        300.0,
        500.0,
        200.0,
        0.0,
        400.0,
    ]  # the true hourly means, hours 13-18
    gfs_steps = {}
    for k in range(13, 19):
        a = 12
        gfs_steps[k] = {
            "dswrf": float(np.mean(hourly[: k - 12])),
            "ave": (a, k),
            "t2m": 300.15,
        }
    s = sources.series("gfs", run, gfs_steps)
    assert np.allclose(s["ghi"], hourly)
    assert s["t"][0] == run.timestamp() + 12.5 * 3600
    acc = (
        np.cumsum([0.0, 360.0, 720.0]) * 3 * 3600
    )  # ECMWF: J/m2 accumulated since the run start
    ec_steps = {
        h: {"ssrd": a, "tcc": 0.5, "lcc": 0.2, "2t": 293.15}
        for h, a in zip((0, 3, 6), acc)
    }
    s = sources.series("ecmwf", run, ec_steps)
    assert np.allclose(s["ghi"], [360.0, 720.0]) and np.allclose(s["cloud"], 50.0)
    ge = {
        15: {
            "dswrf_mean": 300.0,
            "ave_dswrf_mean": (12, 15),
            "dswrf_sd": 40.0,
            "ave_dswrf_sd": (12, 15),
            "tcdc_mean": 60.0,
            "ave_tcdc_mean": (12, 15),
        },
        18: {
            "dswrf_mean": 400.0,
            "ave_dswrf_mean": (12, 18),
            "dswrf_sd": 50.0,
            "ave_dswrf_sd": (12, 18),
            "tcdc_mean": 50.0,
            "ave_tcdc_mean": (12, 18),
        },
    }
    s = sources.series("gefs", run, ge)
    assert np.allclose(s["ghi"], [300.0, 500.0])  # 3-6 h mean = 2 x avg(0-6) - avg(0-3)


def _synthetic_install(days=40, seed=3):
    """A made-up install: one roof array and one vertical wall array, a third input with nothing
    on it, under synthetic weather from three 'models' that see the same clouds with errors."""
    st = site.Site(lat=-31.9, lon=115.9, altitude_m=20.0, linke=3.0)
    truth = [
        site.Array("roof", 20, 0, kwp=6.0),
        site.Array("wall", 90, 0, kwp=2.0, diffuse_gain=1.5),
    ]
    rng = np.random.default_rng(seed)
    t0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp()
    t = np.arange(t0, t0 + days * 86400, 300.0)
    hour_c = np.arange(t0 + 1800, t0 + days * 86400, 3600.0)
    cloud_day = np.clip(rng.beta(2, 2, days), 0, 1)
    cloud_day[::4] = 0.0  # some clear days, as any real install has
    kt_h = np.clip(
        1
        - 0.8
        * cloud_day[((hour_c - t0) // 86400).astype(int)]
        * rng.uniform(0.5, 1.2, len(hour_c)),
        0.1,
        1.0,
    )
    kt5 = np.interp(t, hour_c, kt_h)
    _, parts = site.power(st, truth, t, kt5, 22.0, per_array=True)
    measured = {k: v * rng.normal(1, 0.03, len(t)) for k, v in parts.items()}
    measured["empty"] = np.zeros(len(t))
    zen, _ = physics.sun_position(hour_c, st.lat, st.lon)
    cs, _, _ = physics.clear_sky(hour_c, zen, st.altitude_m, st.linke)

    def model_view(noise, step_h):
        tt = hour_c[::step_h]
        g = (
            np.clip(kt_h[::step_h] + rng.normal(0, noise, len(tt)), 0, 1.1)
            * cs[::step_h]
        )
        return {
            "t": tt,
            "ghi": g,
            "cloud": (1 - kt_h[::step_h]) * 100,
            "low_cloud": (1 - kt_h[::step_h]) * 60,
            "t2m": np.full(len(tt), 22.0),
            "ghi_sd": np.full(len(tt), 60.0),
        }

    weather = {
        "ecmwf": model_view(0.08, 3),
        "gefs": model_view(0.10, 3),
        "gfs": model_view(0.12, 1),
    }
    return st, truth, t, measured, weather


def _learns_an_install_and_forecasts():
    # part 1: physics. Equinox solar noon on the equator: the sun is overhead; a flat panel
    # sees the global light; the sun rises in the east.
    t_noon = dt.datetime(2026, 3, 20, 12, 7, tzinfo=dt.UTC).timestamp()
    zen, _ = physics.sun_position(np.array([t_noon]), 0.0, 0.0)
    assert zen[0] < 1.0
    _, az = physics.sun_position(np.array([t_noon - 5 * 3600]), 0.0, 0.0)
    assert 80 < az[0] < 100
    g, dni, dhi = physics.clear_sky(np.array([t_noon]), zen, 0.0, 3.0)
    assert 900 < g[0] < 1150
    assert physics.poa(g, dni, dhi, zen, np.array([0.0]), 0, 0, np.array([t_noon]))[
        0
    ] == pytest.approx(g[0], rel=0.03)

    # part 2: the install is learned: sizes within 5 %, the empty input marked unused, the
    # owner's directions kept exactly
    st, _truth, t, measured, weather = _synthetic_install()
    learned = [
        site.Array("roof", 20, 0),
        site.Array("wall", 90, 0),
        site.Array("empty", 20, 0),
    ]
    learned[0].learned = {
        "label": "Roof",
        "owner_set": True,
    }  # what Home Assistant put there
    site.fit(st, learned, t, measured, 8 * 3600)
    by = {a.key: a for a in learned}
    assert (by["roof"].learned["label"], by["roof"].learned["owner_set"]) == (
        "Roof",
        True,
    )
    assert by["roof"].kwp == pytest.approx(6.0, rel=0.05)
    assert by["wall"].kwp == pytest.approx(2.0, rel=0.10)
    assert not by["empty"].active
    assert (by["roof"].tilt, by["roof"].azimuth, by["wall"].tilt) == (20, 0, 90)

    # part 3: train on the first 34 days, forecast the last 6 from the weather alone
    total = measured["roof"] + measured["wall"]
    cut = t[0] + 34 * 86400
    trained = model.train(st, learned, t[t < cut], total[t < cut], weather, 8 * 3600)
    assert trained.report["days"] >= 30 and 1.0 <= trained.band_scale <= 2.5
    keep = (t >= cut) & (
        t < t[-1] - 86400
    )  # the synthetic 3-hourly weather ends before the last day
    future = t[keep]
    lo, mid, hi, tier = model.predict(trained, st, learned, future, weather, 8 * 3600)
    day = future.astype(int) // 86400
    act = np.array([total[keep][day == d].sum() / 12 for d in np.unique(day)])
    fc = np.array([mid[day == d].sum() / 12 for d in np.unique(day)])
    assert np.mean(np.abs(fc - act)) / np.mean(act) < 0.15
    assert np.all(lo <= mid + 1e-9) and np.all(mid <= hi + 1e-9)
    assert set(tier) == {"ecmwf+gefs+gfs"}

    # part 4: one source gone (here GEFS): the forecast continues from the tier without it
    no_gefs = {k: v for k, v in weather.items() if k != "gefs"}
    _, mid2, _, tier2 = model.predict(trained, st, learned, future, no_gefs, 8 * 3600)
    assert set(tier2) == {"ecmwf+gfs"} and not np.isnan(mid2).any()
    # days 3-5: past ECMWF's and GEFS's horizon only GFS is left; the forecast carries on from GFS
    # alone, with a wider likely range (its error grows with the lead)
    gfs_only = {"gfs": weather["gfs"]}
    _lo3, mid3, hi3, tier3 = model.predict(
        trained, st, learned, future, gfs_only, 8 * 3600
    )
    assert set(tier3) == {"gfs"} and not np.isnan(mid3).any()
    _, _, hi_all, _ = model.predict(trained, st, learned, future, no_gefs, 8 * 3600)
    sunny = (mid3 > 0.5) & (mid2 > 0.5)  # relative widths only where both have sun
    assert np.median((hi3[sunny] - mid3[sunny]) / mid3[sunny]) > np.median(
        (hi_all[sunny] - mid2[sunny]) / mid2[sunny]
    )
    # a model saved before this tier existed still forecasts (the far days wait for the night's training)
    old = dataclasses.replace(
        trained, tiers={k: v for k, v in trained.tiers.items() if k != "gfs"}
    )
    _, mid4, _, tier4 = model.predict(old, st, learned, future, no_gefs, 8 * 3600)
    assert set(tier4) == {"ecmwf+gfs"} and not np.isnan(mid4).any()


def _store_and_jobs(tmp_path):
    # part 1: a stored run reads back as written; the day-ahead view picks each day's evening-before
    # run; the live view joins the newest run onto the one before it
    ws = store.WeatherStore(tmp_path / "weather")
    day = dt.date(2026, 1, 10)
    run = sources.evening_before_run("ecmwf", day)
    t = run.timestamp() + 3600 * np.arange(15, 40, 3.0)
    ws.save(
        "ecmwf",
        run,
        {"t": t, "ghi": np.linspace(0, 600, len(t)), "cloud": np.full(len(t), 40.0)},
    )
    back = ws.load("ecmwf", run)
    assert (
        np.allclose(back["t"], t)
        and np.allclose(back["cloud"], 40.0)
        and "t2m" not in back
    )
    t0 = dt.datetime.combine(day, dt.time(0), dt.UTC).timestamp()
    view = ws.dayahead([day], lambda d: (t0, t0 + 86400))
    assert set(view) == {"ecmwf"} and view["ecmwf"]["t"].min() >= t0 - 3 * 3600
    nxt = run + dt.timedelta(days=1)
    ws.save(
        "ecmwf",
        nxt,
        {
            "t": nxt.timestamp() + 3600 * np.arange(15, 40, 3.0),
            "ghi": np.full(9, 500.0),
        },
    )
    live = ws.latest()["ecmwf"]
    assert live["t"].min() == t.min() and live["t"].max() == nxt.timestamp() + 39 * 3600
    assert np.all(np.diff(live["t"]) > 0)

    # part 2: recorded states become 5-minute means (time-weighted, half a slot needed); an hourly
    # long-term mean is spread over its hour in the clear-sky shape and keeps its mean
    _grid, kw = runner.measured_5min(
        [(0.0, 2.0), (150.0, 4.0), (600.0, 1.0)], 0.0, 900.0
    )
    assert np.allclose(kw, [3.0, 4.0, 1.0])
    st = site.Site(lat=-31.9, lon=115.9)
    noon = dt.datetime(2026, 1, 10, 3, 0, tzinfo=dt.UTC).timestamp()  # 11:00 local
    grid = np.arange(noon, noon + 3600, 300.0)
    spread = runner.hourly_to_5min(st, site.Array("roof", 20, 0), [(noon, 4.2)], grid)
    assert spread.mean() == pytest.approx(4.2) and spread.std() > 0

    # part 3: local days follow the time zone's own offset, daylight saving included
    import zoneinfo

    syd = zoneinfo.ZoneInfo("Australia/Sydney")
    jan = dt.datetime(
        2026, 1, 10, 13, 30, tzinfo=dt.UTC
    ).timestamp()  # 00:30 on the 11th, UTC+11
    jul = dt.datetime(
        2026, 7, 10, 13, 30, tzinfo=dt.UTC
    ).timestamp()  # 23:30 on the 10th, UTC+10
    days = site.local_day(np.array([jan, jul]), syd)
    assert [str(dt.date(1970, 1, 1) + dt.timedelta(days=int(d))) for d in days] == [
        "2026-01-11",
        "2026-07-10",
    ]
    # a local day's window runs from that day's own local midnight to the next, so the days daylight
    # saving starts (4 Oct 2026, 23 h) and ends (5 Apr 2026, 25 h) are not shifted by an hour
    utc = lambda *a: dt.datetime(*a, tzinfo=dt.UTC).timestamp()
    assert runner._day_window(dt.date(2026, 10, 4), syd) == (
        utc(2026, 10, 3, 14),
        utc(2026, 10, 4, 13),
    )
    assert runner._day_window(dt.date(2026, 4, 5), syd) == (
        utc(2026, 4, 4, 13),
        utc(2026, 4, 5, 14),
    )

    # part 4: through the day the last 30 minutes measured correct the forecast: each reading is
    # compared with what was forecast for that same moment (not for now), so on a morning ramp
    # output running at 1.5x the forecast gives a correction of 1.5
    from unittest.mock import patch

    now = dt.datetime(2026, 1, 10, 0, 0, tzinfo=dt.UTC)
    base = now.timestamp() - 3 * 3600

    def ramp(_tr, _st, _arr, tt, _w, _tz):
        mid = (np.asarray(tt, dtype=float) - base) / 3600.0  # 3 kW at `now`, rising
        return 0.5 * mid, mid, 1.5 * mid, np.array(["ecmwf"] * len(mid), dtype=object)

    rt = now.timestamp() - 1800 + 300.0 * np.arange(1, 7)
    seen = 1.5 * (rt - base) / 3600.0

    class _Weather:
        def latest(self):
            return {}

    with (
        patch.object(runner.M, "predict", ramp),
        patch.object(runner, "clear_power", lambda _s, _a, tt: np.full(len(tt), 100.0)),
    ):
        fc = runner.forecast(
            None, _Weather(), st, [], now, syd, recent=(rt, seen), hours=6
        )
    assert fc.live_ratio == pytest.approx(1.5, abs=0.01) and fc.tier == "ecmwf"
    assert fc.rows[0]["time"] == now.isoformat()  # the published rows still start now

    # with no weather covering now, the power now is unknown, not a confident 0 kW
    def gap(*a):
        lo, mid, hi, tier = ramp(*a)
        late = np.asarray(a[3], dtype=float) >= now.timestamp() + 3600
        return (*(np.where(late, x, np.nan) for x in (lo, mid, hi)), tier)

    with patch.object(runner.M, "predict", gap):
        fc = runner.forecast(None, _Weather(), st, [], now, syd, hours=6)
    assert fc.now_kw is None and fc.rows[0]["time"] > now.isoformat()


def test_solar_forecaster_from_weather_files_to_a_forecast(tmp_path):
    """One behaviour end to end: the free models' files are read exactly, and an install
    is learned from its output and forecast from the weather, and the weather store and the jobs Home
    Assistant runs keep what they fetch and read (parts above, in order)."""
    _reads_weather_files()
    _learns_an_install_and_forecasts()
    _store_and_jobs(tmp_path)
