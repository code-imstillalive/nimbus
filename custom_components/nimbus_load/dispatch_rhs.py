"""Dispatch-side RHS check for a P5MIN false spike (nimbus #1778, Mark
Purcell's "stronger version", 16 Oct 2026). DIAGNOSTIC ONLY: it changes no
price; the guard in p5min_guard.py does that, on its own rules.

On both false-spike days AEMO's forecast runs applied a `CA_BRIS_*`
control-room constraint at about half the right-hand side dispatch used:

==============================  ==================  ============  ======
run                             forecast RHS (MW)   dispatch RHS  ratio
==============================  ==================  ============  ======
15 Oct P5MIN 15:50-16:30        1,378-1,892         2,952-3,531   ~0.5
16 Oct P5MIN 09:55-10:50        1,189-1,270         2,526-2,665   ~0.47
16 Oct P5MIN 10:55 (corrected)  2,548               2,548         1.00
==============================  ==================  ============  ======

Dispatch met the constraint at its own limit throughout and stayed at
$44-$295/MWh, while the forecast ran to the $23,200 cap. Mark's test, which
this implements: for each constraint BINDING in the latest dispatch interval
(non-zero marginal value, no violation, RHS > 0), compare the P5MIN run's
first-interval RHS for the same constraint. A row is suspect when the ratio
is under `RATIO_MAX` (0.6) or the forecast violates what dispatch meets
(FCAS requirement rows excluded, see `FCAS_PREFIX`). `rhs_suspect` is that
evidence with dispatch normal; the check flags when a row is suspect AND the forecast spikes AND dispatch is
normal (under `NORMAL_DISPATCH_MWH`). The ratio returning to ~1.0 clears it on
the same 5-minute boundary as AEMO's own correction.

It reads the regional RRP from DispatchIS's own PRICE row, so it needs no
configured sensor. One DispatchIS listing plus one zip per new P5MIN run.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

BASE_URL = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"
_FILE = re.compile(r"PUBLIC_DISPATCHIS_(\d{12})_\d+\.zip")
# Mark Purcell, #1778, 16 Oct 2026: the false-spike periods read 0.38-0.49.
RATIO_MAX = 0.6
# Mark Purcell, #1778: "the dispatch price is normal (under $1,000/MWh)".
NORMAL_DISPATCH_MWH = 1000.0
# FCAS requirement constraints. Their RHS is an FCAS volume, not a network
# limit, and differs between P5MIN and dispatch on ordinary runs. Measured on
# the 15 and 16 Oct NEMWeb files (dispatch interval before each P5MIN run):
# - kept, `F_Q+86_L1` / `F_Q+86_R1` read 0.0-0.49 of dispatch's on normal
#   runs, so the evidence fired at 16 Oct 10:55, the run in which AEMO
#   corrected `CA_BRIS_59E3F2B6_1` to dispatch's RHS (ratio 1.00);
# - excluded, 16 Oct reads suspect 10:00 and 10:10-10:50 and clears at 10:55;
#   15 Oct flags 15:55, 16:00, 16:10 and 16:15 and misses 16:05, where
#   dispatch still carried `CA_BRIS_59E2F52D_1` and P5MIN had moved to
#   `CA_BRIS_59E2F7DC_1`: an id rollover, which rule (c) of p5min_guard.py
#   (the forecast's own violation) still covers.
FCAS_PREFIX = "F_"
# Rows kept on the attribute: enough to explain a flag, small enough to record.
MAX_ROWS = 5
_NEM = timezone(timedelta(hours=10))


def latest_file_name(listing: str) -> str | None:
    """The newest DispatchIS zip named in a NEMWeb directory listing."""
    names = sorted(set(_FILE.findall(listing)))
    if not names:
        return None
    latest = names[-1]
    full = [m.group(0) for m in _FILE.finditer(listing) if m.group(1) == latest]
    return max(full)


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _nem_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M:%S").replace(tzinfo=_NEM)
    except ValueError:
        return None


def _rows(
    text: str, family: str, tables: tuple[str, ...]
) -> Iterator[tuple[str, dict[str, str]]]:
    headers: dict[str, list[str]] = {}
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) < 4 or rec[1] != family or rec[2] not in tables:
            continue
        if rec[0] == "I":
            headers[rec[2]] = rec
            continue
        header = headers.get(rec[2])
        if rec[0] != "D" or header is None or len(rec) != len(header):
            continue
        row = dict(zip(header, rec, strict=True))
        if row.get("INTERVENTION") == "0":
            yield rec[2], row


def _constraint(row: dict[str, str]) -> dict[str, float | None]:
    return {
        "rhs": _float(row.get("RHS")),
        "marginal_value": _float(row.get("MARGINALVALUE")),
        "violation": _float(row.get("VIOLATIONDEGREE")),
    }


def parse_dispatch(text: str, region: str) -> dict[str, Any] | None:
    """The DispatchIS interval: its end, the region's RRP ($/MWh) and every
    BINDING constraint (non-zero marginal value), or None without a price
    row for the region. CPU-bound: call it off the event loop."""
    end: datetime | None = None
    rrp: float | None = None
    binding: dict[str, dict[str, float | None]] = {}
    for table, row in _rows(text, "DISPATCH", ("PRICE", "CONSTRAINT")):
        if table == "PRICE":
            if row.get("REGIONID") == region:
                rrp = _float(row.get("RRP"))
                end = _nem_time(row.get("SETTLEMENTDATE", ""))
            continue
        c = _constraint(row)
        if c["marginal_value"]:
            binding[row.get("CONSTRAINTID", "")] = c
    if rrp is None or end is None:
        return None
    return {"interval_end": end.isoformat(), "rrp": rrp, "binding": binding}


def first_interval_constraints(text: str) -> dict[str, dict[str, float | None]]:
    """Every P5MIN CONSTRAINTSOLUTION row of the run's FIRST interval, by
    constraint id (~1,100 rows; kept in memory, never published)."""
    by_interval: dict[str, dict[str, dict[str, float | None]]] = {}
    for _table, row in _rows(text, "P5MIN", ("CONSTRAINTSOLUTION",)):
        by_interval.setdefault(row.get("INTERVAL_DATETIME", ""), {})[
            row.get("CONSTRAINTID", "")
        ] = _constraint(row)
    if not by_interval:
        return {}
    return by_interval[min(by_interval)]


def compare(
    dispatch: dict[str, Any] | None,
    forecast_first: dict[str, dict[str, float | None]],
    *,
    forecast_peak_mwh: float | None,
    spike_mwh: float,
) -> dict[str, Any]:
    """The check's attribute. `flag` is Mark's three conditions together; the
    rows are the binding constraints P5MIN also carries, suspect first, then
    lowest ratio."""
    out: dict[str, Any] = {
        "mode": "diagnostic",
        "flag": False,
        "ratio_max": RATIO_MAX,
        "dispatch_interval_end": None,
        "dispatch_rrp": None,
        "forecast_peak": forecast_peak_mwh,
        "rhs_suspect": False,
        "constraints": [],
    }
    if dispatch is None:
        out["reason"] = "no_dispatch"
        return out
    out["dispatch_interval_end"] = dispatch["interval_end"]
    out["dispatch_rrp"] = dispatch["rrp"]
    rows: list[dict[str, Any]] = []
    for cid, d in dispatch["binding"].items():
        if cid.startswith(FCAS_PREFIX):
            continue
        f = forecast_first.get(cid)
        d_rhs, f_rhs = d.get("rhs"), (f or {}).get("rhs")
        if f is None or d_rhs is None or d_rhs <= 0 or (d.get("violation") or 0) > 0:
            continue
        ratio = round(f_rhs / d_rhs, 3) if f_rhs is not None else None
        f_violation = f.get("violation") or 0.0
        suspect = (ratio is not None and ratio < RATIO_MAX) or f_violation > 0
        rows.append(
            {
                "constraint_id": cid,
                "dispatch_rhs": d_rhs,
                "forecast_rhs": f_rhs,
                "ratio": ratio,
                "forecast_violation": round(f_violation, 3),
                "suspect": suspect,
            }
        )
    rows.sort(
        key=lambda r: (not r["suspect"], r["ratio"] if r["ratio"] is not None else 9)
    )
    spike = forecast_peak_mwh is not None and forecast_peak_mwh >= spike_mwh
    normal = dispatch["rrp"] < NORMAL_DISPATCH_MWH
    out["constraints"] = rows[:MAX_ROWS]
    # The constraint evidence on its own: on 16 Oct the spike was in the
    # 30-minute pre-dispatch (13:00 on), past P5MIN's one-hour horizon, so a
    # P5MIN-spike gate never fired while the RHS ratio read 0.46-0.49 from
    # 10:00. A consumer holding the plan's own forward prices gates on this.
    out["rhs_suspect"] = bool(normal and any(r["suspect"] for r in rows))
    out["flag"] = bool(spike and out["rhs_suspect"])
    out["reason"] = (
        "suspect_forecast_rhs"
        if out["flag"]
        else "no_forecast_spike"
        if not spike
        else "dispatch_spiked"
        if not normal
        else "forecast_rhs_matches_dispatch"
    )
    return out
