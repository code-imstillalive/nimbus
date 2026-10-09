"""AEMO's 5-minute pre-dispatch (P5MIN) for one NEM region, parsed.

nimbus #1653 / #1658. On 9 Oct 2026 the reference household's plan sold its
battery at 28.6c at 04:25 because no forward price it could read showed the
05:20 spike. The 30-minute pre-dispatch said ~$270/MWh for that half-hour;
AEMO's own 5-minute pre-dispatch, published at 04:20, said ~$500/MWh. Every
NEM install can read that file: AEMO publishes it publicly every five
minutes at NEMWEB, about an hour ahead, for every region.

Pure functions only, with no `homeassistant` import (the same shape as
`nem_region.py` and `aemo_crosscheck.py`): the fetch and the entity live in
`sensor_p5min.py`.

The file format, from a real file (PUBLIC_P5MIN_202610091150_*.CSV):

    I,P5MIN,REGIONSOLUTION,10,RUN_DATETIME,INTERVENTION,INTERVAL_DATETIME,REGIONID,RRP,...
    D,P5MIN,REGIONSOLUTION,10,"2026/10/09 11:50:00",0,"2026/10/09 11:55:00",QLD1,-4.9999,...

`INTERVAL_DATETIME` is the interval's END, in NEM market time (UTC+10, no
daylight saving, in every region). `RRP` is $/MWh. Column positions come from
the `I` header row, not hard-coded, so an added column does not shift them.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta, timezone

NEM_TIME = timezone(timedelta(hours=10))
INTERVAL = timedelta(minutes=5)
BASE_URL = "https://nemweb.com.au/Reports/CURRENT/P5_Reports/"
NEM_REGIONS = ("NSW1", "QLD1", "VIC1", "SA1", "TAS1")

_FILE_RE = re.compile(r"PUBLIC_P5MIN_(\d{12})_(\d{14})\.zip", re.IGNORECASE)
_REGION_IN_ID_RE = re.compile(r"(?<![a-z])(nsw|qld|vic|sa|tas)1(?![0-9])")


def latest_file_name(listing: str) -> str | None:
    """The newest P5MIN zip named in a NEMWEB directory listing, or None.

    Names sort by run time then publish time, so the lexically greatest is
    the newest run."""
    names = {m.group(0) for m in _FILE_RE.finditer(listing)}
    return max(names) if names else None


def region_from_entity_ids(entity_ids: list[str | None]) -> str | None:
    """The NEM region named in a configured AEMO sensor's entity_id.

    `sensor.nem_pd7day_qld1_...` and `sensor.aemo_nem_qld1_...` both carry
    the region. Refuses (None) when the ids name more than one region, rather
    than picking one."""
    found = set()
    for eid in entity_ids:
        if eid:
            found.update(m.group(1) for m in _REGION_IN_ID_RE.finditer(eid.lower()))
    return f"{found.pop().upper()}1" if len(found) == 1 else None


def _nem_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=NEM_TIME
        )
    except ValueError:
        return None


def parse_region_solution(text: str, region: str) -> dict | None:
    """One region's forecast from a P5MIN CSV.

    Returns `{"run_datetime": iso, "forecast": [{start, end, value}]}` with
    `value` in $/kWh, sorted by start, or None when the file holds no rows
    for the region. Only the non-intervention run (INTERVENTION 0) is used:
    that is the price the market settles on."""
    header: list[str] | None = None
    rows: list[dict] = []
    run: datetime | None = None
    region = region.upper()
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) < 4 or rec[1] != "P5MIN" or rec[2] != "REGIONSOLUTION":
            continue
        if rec[0] == "I":
            header = rec
            continue
        if rec[0] != "D" or header is None or len(rec) != len(header):
            continue
        r = dict(zip(header, rec, strict=True))
        if r.get("REGIONID", "").upper() != region or r.get("INTERVENTION") != "0":
            continue
        end = _nem_time(r.get("INTERVAL_DATETIME", ""))
        this_run = _nem_time(r.get("RUN_DATETIME", ""))
        try:
            price = float(r["RRP"]) / 1000.0
        except (KeyError, ValueError):
            continue
        if end is None or this_run is None:
            continue
        run = this_run if run is None else max(run, this_run)
        rows.append(
            {
                "start": (end - INTERVAL).isoformat(),
                "end": end.isoformat(),
                "value": round(price, 6),
            }
        )
    if not rows or run is None:
        return None
    rows.sort(key=lambda row: row["start"])
    return {"run_datetime": run.isoformat(), "forecast": rows}


def peak(forecast: list[dict]) -> dict | None:
    """The highest-priced row of a parsed forecast, or None when empty."""
    return max(forecast, key=lambda row: row["value"]) if forecast else None
