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

Getter = Callable[..., bytes]

ECMWF_BASE = "https://storage.googleapis.com/ecmwf-open-data"
GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
GEFS_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"


@dataclass(frozen=True)
class Spec:
    name: str
    run_hours: tuple[int, ...]  # cycles to use, UTC
    step_h: int  # spacing of the files read
    publish_lag_h: (
        float  # hours after the cycle time until the last needed file is reliably there
    )
    attribution: str


SPECS = {
    "ecmwf": Spec(
        "ecmwf",
        (0, 12),
        3,
        8.5,
        "Contains modified Copernicus/ECMWF open data (CC-BY-4.0)",
    ),
    "gfs": Spec("gfs", (0, 6, 12, 18), 1, 4.5, "NOAA GFS"),
    "gefs": Spec("gefs", (0, 6, 12, 18), 3, 5.5, "NOAA GEFS"),
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


def fetch_step(
    source: str, run: dt.datetime, step: int, lat: float, lon: float, get: Getter
) -> dict:
    """The site's values in one forecast file (one step of one run)."""
    out: dict = {}
    if source == "ecmwf":
        url = _ecmwf_url(run, step)
        index = [
            json.loads(x)
            for x in get(url + ".index").decode().splitlines()
            if x.strip()
        ]
        for rec in index:
            p = rec.get("param")
            if (
                rec.get("levtype") == "sfc"
                and p in ("ssrd", "tcc", "lcc", "2t")
                and p not in out
            ):
                end = rec["_offset"] + rec["_length"] - 1
                out[p] = _value(get, url + ".grib2", rec["_offset"], end, lat, lon)
        return out
    if source == "gfs":
        url = _gfs_url(run, step)
        want = {("DSWRF", "surface"): "dswrf", ("TMP", "2 m above ground"): "t2m"}
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


def steps_for(source: str, run: dt.datetime, t0: float, t1: float) -> list[int]:
    """The file steps of `run` needed to cover [t0, t1] (UTC seconds), starting at an averaging boundary."""
    sp = SPECS[source]
    h0 = int((t0 - run.timestamp()) // 3600)
    h1 = int(-(-(t1 - run.timestamp()) // 3600))
    h0 -= (
        h0 % 6
    )  # averages reset every 6 h (NOAA) / accumulate from the start (ECMWF): start on a boundary
    h0 = max(h0, 0)
    h1 = -(-h1 // sp.step_h) * sp.step_h
    return list(range(h0, h1 + 1, sp.step_h))


def fetch_run(source, run, steps, lat, lon, get, workers=6) -> dict[int, dict]:
    """Every step of one run, in parallel. A failed step is left out (the series has a gap there)."""
    out = {}
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fetch_step, source, run, s, lat, lon, get): s for s in steps}
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
        ghi.append(max(float(value), 0.0))
        for k, v in kw.items():
            extra.setdefault(k, []).append(np.nan if v is None else float(v))

    ks = sorted(steps)
    if source == "ecmwf":
        for a, b in itertools.pairwise(ks):
            sa, sb = steps[a], steps[b]
            if "ssrd" not in sa or "ssrd" not in sb:
                continue
            mean = (sb["ssrd"] - sa["ssrd"]) / (
                (b - a) * 3600.0
            )  # accumulated J/m2 -> W/m2
            lcc = (
                (sa["lcc"] + sb["lcc"]) / 2 * 100
                if "lcc" in sa and "lcc" in sb
                else None
            )
            add(
                r0 + (a + b) / 2 * 3600,
                mean,
                cloud=(sa["tcc"] + sb["tcc"]) / 2 * 100,
                low_cloud=lcc,
                t2m=(sa["2t"] + sb["2t"]) / 2 - 273.15,
            )
    elif source == "gfs":
        for k in ks:
            s = steps[k]
            if "dswrf" not in s or not s.get("ave"):
                continue
            a, b = s["ave"]
            prev = steps.get(k - 1, {})
            if b - a == 1:
                mean = s["dswrf"]
            elif prev.get("ave") == (a, b - 1):
                mean = (b - a) * s["dswrf"] - (b - 1 - a) * prev["dswrf"]
            else:
                continue
            add(
                r0 + (k - 0.5) * 3600,
                mean,
                t2m=s["t2m"] - 273.15 if "t2m" in s else None,
            )
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


def newest_run(source: str, now_utc: dt.datetime, horizon_h: float) -> dt.datetime:
    """The newest cycle whose files out to `horizon_h` should be published by `now_utc`."""
    sp = SPECS[source]
    t = now_utc.replace(minute=0, second=0, microsecond=0)
    for back in range(72):
        run = t - dt.timedelta(hours=back)
        if run.hour in sp.run_hours and now_utc >= run + dt.timedelta(
            hours=sp.publish_lag_h + horizon_h / 60.0
        ):
            return run
    return t - dt.timedelta(hours=72)


def evening_before_run(source: str, day: dt.date) -> dt.datetime:
    """The run a day-ahead forecast for local `day` is made from: the day before, ECMWF 00Z, NOAA 06Z."""
    hour = 0 if source == "ecmwf" else 6
    return dt.datetime.combine(day - dt.timedelta(days=1), dt.time(hour), dt.UTC)
