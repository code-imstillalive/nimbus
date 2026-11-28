"""The free weather models the forecaster reads, straight from their public archives.

No account, no key and no paid service. Only the needed fields of each file
are fetched (HTTP range requests guided by each file's index) and only the
site's grid point is kept; the site's location is never sent anywhere.

| source | provider | what is read | licence |
|---|---|---|---|
| `ecmwf` | ECMWF IFS open data (Google Cloud mirror) | solar radiation, total and low cloud, 2 m temperature | CC-BY-4.0 |
| `gfs`   | NOAA GFS (NOAA Open Data Dissemination, AWS) | solar radiation, 2 m temperature | public domain |
| `gefs`  | NOAA GEFS 31-run ensemble (NODD, AWS) | mean and spread of solar radiation, mean cloud | public domain |

Every fetch takes `get(url, byte_range=None) -> bytes`, so the caller
decides how to do I/O (Home Assistant runs it in an executor) and the tests
can serve synthetic files.

Radiation in these files is an average (or accumulation) since an earlier
step; `series()` turns it into plain interval means, centred on each
interval, in W/m2.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import itertools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from . import grib
from . import physics as ph

Getter = Callable[..., bytes]

ECMWF_BASE = "https://storage.googleapis.com/ecmwf-open-data"
GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
GEFS_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"


@dataclass(frozen=True)
class Spec:
    name: str
    run_hour: int  # the cycle used, UTC: the one a day-ahead forecast for the next local day is made from
    step_h: int  # spacing of the files read
    publish_lag_h: float  # hours after the cycle time until its files out to `horizon_h` are reliably there
    horizon_h: int  # how far ahead of the run the live forecast reads
    attribution: str


SPECS = {
    "ecmwf": Spec(
        "ecmwf",
        0,
        3,
        9.0,
        120,
        "Contains modified Copernicus/ECMWF open data (CC-BY-4.0)",
    ),
    "gfs": Spec("gfs", 6, 3, 5.0, 120, "NOAA GFS (public domain)"),
    "gefs": Spec("gefs", 6, 3, 6.0, 120, "NOAA GEFS (public domain)"),
}


def _ecmwf_url(run: dt.datetime, step: int) -> str:
    return f"{ECMWF_BASE}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/{run:%Y%m%d%H}0000-{step}h-oper-fc"


def _gfs_url(run: dt.datetime, step: int) -> str:
    return f"{GFS_BASE}/gfs.{run:%Y%m%d}/{run:%H}/atmos/gfs.t{run:%H}z.pgrb2.0p25.f{step:03d}"


def _gefs_url(run: dt.datetime, step: int, kind: str) -> str:
    return f"{GEFS_BASE}/gefs.{run:%Y%m%d}/{run:%H}/atmos/pgrb2sp25/{kind}.t{run:%H}z.pgrb2s.0p25.f{step:03d}"


def _noaa_ranges(idx_text: str, wanted: dict[tuple[str, str], str]):
    """NOAA .idx lines -> {key: (start, end, description)} for the wanted (variable, level) pairs."""
    lines = idx_text.splitlines()
    out = {}
    for i, line in enumerate(lines):
        p = line.split(":")
        if len(p) < 6:
            continue
        key = wanted.get((p[3], p[4]))
        if key is None or key in out:
            continue
        end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else None
        out[key] = (int(p[1]), end, p[5])
    return out


def _ave_window(desc: str):
    m = re.match(r"(\d+)-(\d+) hour ave", desc)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _value(get: Getter, url: str, start: int, end, lat: float, lon: float) -> float:
    field, lats, lons = grib.decode(get(url, (start, end)), point=(lat, lon))
    return grib.at_point(field, lats, lons, lat, lon)


#: ECMWF fields read at each step: radiation, cloud and temperature for the forecast; total and
#: convective (shower) precipitation and most-unstable CAPE for the weather strip on the card.
ECMWF_PARAMS = ("ssrd", "tcc", "lcc", "2t", "tp", "cp", "mucape")
#: At night only what the weather strip and weather.nimbus_weather show (no sun to forecast).
ECMWF_NIGHT_PARAMS = ("tcc", "lcc", "2t", "tp", "cp", "mucape")


def fetch_step(
    source: str,
    run: dt.datetime,
    step: int,
    lat: float,
    lon: float,
    get: Getter,
    params: tuple[str, ...] | None = None,
) -> dict:
    """The site's values in one forecast file (one step of one run). `params`: ECMWF fields to read
    (default ECMWF_PARAMS)."""
    out: dict = {}
    if source == "ecmwf":
        wanted = params or ECMWF_PARAMS
        url = _ecmwf_url(run, step)
        index = [
            json.loads(x)
            for x in get(url + ".index").decode().splitlines()
            if x.strip()
        ]
        for rec in index:
            p = rec.get("param")
            if rec.get("levtype") == "sfc" and p in wanted and p not in out:
                end = rec["_offset"] + rec["_length"] - 1
                out[p] = _value(get, url + ".grib2", rec["_offset"], end, lat, lon)
        return out
    if source == "gfs":
        url = _gfs_url(run, step)
        want = {("DSWRF", "surface"): "dswrf"}
        for key, (s, e, desc) in _noaa_ranges(get(url + ".idx").decode(), want).items():
            out[key] = _value(get, url, s, e, lat, lon)
            if key == "dswrf":
                out["ave"] = _ave_window(desc)
        return out
    if source == "gefs":
        for kind, tag in (("geavg", "mean"), ("gespr", "sd")):
            url = _gefs_url(run, step, kind)
            want = {
                ("DSWRF", "surface"): "dswrf",
                ("TCDC", "entire atmosphere"): "tcdc",
            }
            for key, (s, e, desc) in _noaa_ranges(
                get(url + ".idx").decode(), want
            ).items():
                out[f"{key}_{tag}"] = _value(get, url, s, e, lat, lon)
                out[f"ave_{key}_{tag}"] = _ave_window(desc)
        return out
    raise ValueError(f"unknown source {source}")


def steps_for(
    source: str,
    run: dt.datetime,
    t0: float,
    t1: float,
    lat: float | None = None,
    lon: float | None = None,
) -> list[int]:
    """The file steps of `run` needed to cover [t0, t1] (UTC seconds), starting at an averaging boundary.

    With the site's `lat` / `lon`, night is left out: a solar forecast needs nothing from it, and it
    is about a third of every run. What is kept still gives exactly the same daylight values:
    ECMWF's accumulated radiation needs only the two steps around each daylight interval (the step
    before dawn included), and NOAA's averages restart every 6 hours, so a 6-hour block is kept
    whole whenever any of it is daylight."""
    sp = SPECS[source]
    h0 = int((t0 - run.timestamp()) // 3600)
    h1 = int(-(-(t1 - run.timestamp()) // 3600))
    h0 -= (
        h0 % 6
    )  # averages reset every 6 h (NOAA) / accumulate from the start (ECMWF): start on a boundary
    h0 = max(h0, 0)
    h1 = -(-h1 // sp.step_h) * sp.step_h
    steps = list(range(h0, h1 + 1, sp.step_h))
    if lat is None or lon is None:
        return steps
    r0 = run.timestamp()

    def light(a_h: float, b_h: float) -> bool:
        tt = r0 + np.linspace(a_h, b_h, 13) * 3600
        zen, _ = ph.sun_position(tt, lat, lon)
        return bool(
            (zen < 91.0).any()
        )  # the sun up (or just about) somewhere in (a, b]

    if source == "ecmwf":
        return [s for s in steps if light(s - sp.step_h, s) or light(s, s + sp.step_h)]
    # a NOAA file at step s holds the average over its 6-hour block ((s - 1) // 6): (0, 6] for 3 and 6
    blocks = {
        (s - 1) // 6 for s in steps if light((s - 1) // 6 * 6, (s - 1) // 6 * 6 + 6)
    }
    return [s for s in steps if (s - 1) // 6 in blocks]


def fetch_run(
    source, run, steps, lat, lon, get, workers=6, night_steps=()
) -> dict[int, dict]:
    """Every step of one run, in parallel. A failed step is left out (the series has a gap there).
    `night_steps` (ECMWF): read only the weather fields there."""
    out = {}
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_step, source, run, s, lat, lon, get): s for s in steps}
        futs.update(
            {
                ex.submit(
                    fetch_step, source, run, s, lat, lon, get, ECMWF_NIGHT_PARAMS
                ): s
                for s in night_steps
                if s not in steps
            }
        )
        for f in cf.as_completed(futs):
            try:
                out[futs[f]] = f.result()
            except Exception:  # noqa: BLE001, S110 -- one missing file must not lose the rest of the run
                pass
    return out


def series(
    source: str, run: dt.datetime, steps: dict[int, dict]
) -> dict[str, np.ndarray]:
    """Fetched steps -> interval means centred on each interval: t (UTC s), ghi (W/m2) and extras."""
    r0 = run.timestamp()
    t, ghi, extra = [], [], {}

    def add(centre, value, **kw):
        t.append(centre)
        v = float(value)
        ghi.append(v if np.isnan(v) else max(v, 0.0))
        for k, v in kw.items():
            extra.setdefault(k, []).append(np.nan if v is None else float(v))

    ks = sorted(steps)
    if source == "ecmwf":
        for a, b in itertools.pairwise(ks):
            sa, sb = steps[a], steps[b]
            if b - a > SPECS["ecmwf"].step_h or "tcc" not in sa or "tcc" not in sb:
                continue  # a gap (a missing file) is no interval

            hours = b - a

            def both(k, f, sa=sa, sb=sb):
                return f(sa[k], sb[k]) if k in sa and k in sb else None

            def per_hour(
                k, sa=sa, sb=sb, hours=hours
            ):  # accumulated metres -> mm per hour
                return (
                    max(sb[k] - sa[k], 0.0) * 1000 / hours
                    if k in sa and k in sb
                    else None
                )

            # at night only the weather fields are read: no radiation there (ghi left empty)
            mean = both(
                "ssrd", lambda x, y, h=hours: (y - x) / (h * 3600.0)
            )  # J/m2 -> W/m2
            add(
                r0 + (a + b) / 2 * 3600,
                float("nan") if mean is None else mean,
                cloud=(sa["tcc"] + sb["tcc"]) / 2 * 100,
                low_cloud=both("lcc", lambda x, y: (x + y) / 2 * 100),
                t2m=both("2t", lambda x, y: (x + y) / 2 - 273.15),
                # rain and shower rain in mm per hour; CAPE (J/kg) at the interval's middle
                rain=per_hour("tp"),
                shower=per_hour("cp"),
                cape=both("mucape", lambda x, y: (x + y) / 2),
            )
    elif source == "gfs":
        # Each file holds the average since the last 6-hourly reset, window (a, b). With the previous
        # file in the same window ending at pb: mean over (pb, b] = ((b-a)*A_b - (pb-a)*A_pb) / (b-pb).
        # The first file after a reset is already the mean over its own (a, b].
        prev = None
        for k in ks:
            s = steps[k]
            if "dswrf" not in s or not s.get("ave"):
                prev = None
                continue
            a, b = s["ave"]
            if prev is not None and prev["ave"][0] == a and prev["ave"][1] < b:
                pb = prev["ave"][1]
                mean, start = (
                    ((b - a) * s["dswrf"] - (pb - a) * prev["dswrf"]) / (b - pb),
                    pb,
                )
            elif b - a <= SPECS["gfs"].step_h:
                mean, start = s["dswrf"], a
            else:
                prev = s
                continue
            add(r0 + (start + b) / 2 * 3600, mean)
            prev = s
    elif source == "gefs":
        for k in ks:
            s, prev = steps[k], steps.get(k - 3, {})
            if "dswrf_mean" not in s:
                continue

            def deave(key, s=s, prev=prev):
                a, b = s["ave_" + key]
                if b - a == 3:
                    return s[key]
                if prev.get("ave_" + key) == (a, b - 3):
                    return 2 * s[key] - prev[key]
                return None

            mean = deave("dswrf_mean")
            if mean is None:
                continue
            sd, cc = deave("dswrf_sd"), deave("tcdc_mean")
            add(
                r0 + (k - 1.5) * 3600,
                mean,
                ghi_sd=None if sd is None else max(sd, 0.0),
                cloud=None if cc is None else min(max(cc, 0.0), 100.0),
            )
    out = {"t": np.array(t, dtype=float), "ghi": np.array(ghi, dtype=float)}
    out.update({k: np.array(v, dtype=float) for k, v in extra.items()})
    return out


def newest_run(source: str, now_utc: dt.datetime) -> dt.datetime:
    """The newest run of the cycle the model trains on whose files are reliably published."""
    sp = SPECS[source]
    day = now_utc.date()
    for back in range(3):
        run = dt.datetime.combine(
            day - dt.timedelta(days=back), dt.time(sp.run_hour), dt.UTC
        )
        if now_utc >= run + dt.timedelta(hours=sp.publish_lag_h):
            return run
    return dt.datetime.combine(day - dt.timedelta(days=3), dt.time(sp.run_hour), dt.UTC)


def evening_before_run(source: str, day: dt.date) -> dt.datetime:
    """The run a day-ahead forecast for local `day` is made from: the day before, at the source's `run_hour`."""
    return dt.datetime.combine(
        day - dt.timedelta(days=1), dt.time(SPECS[source].run_hour), dt.UTC
    )
