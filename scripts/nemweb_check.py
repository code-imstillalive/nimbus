#!/usr/bin/env python3
"""Score price sensors against AEMO's settled dispatch prices from NEMWEB
(nimbus #1786, Mark Purcell: "NEMWEB is the authoritative source of
settlement prices").

Never run by the test suite (it downloads); the parsing and scoring it does
are pure functions with their own tests.

    python scripts/nemweb_check.py --region QLD1 --history history.json \
        [--factor 1.0605] [--cache DIR]

``history.json`` is ``{"<name>": [["<ISO time>", <$/kWh>], ...], ...}``:
each sensor's recorded states, as Home Assistant's history returns them.
Kept out of the repository: a real install's history stays local.

For every 5-minute dispatch interval in the history's span, the region's
settled RRP is read from NEMWEB (the Current reports for the last two days,
the daily Archive before that) and each sensor is paired with its state at
the interval's end. ``--factor`` is the site's MLF x DLF, so a provider that
passes AEMO's price through by contract should land on RRP x factor.

Per sensor it prints the intervals paired, the median ratio to RRP, the
share within 0.1 % of RRP x factor, and the median and mean absolute
difference from RRP x factor in cents.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import re
import statistics
import sys
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

NEM = timezone(timedelta(hours=10))
INTERVAL = timedelta(minutes=5)
CURRENT = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"
ARCHIVE = "https://nemweb.com.au/Reports/Archive/DispatchIS_Reports/"
WITHIN = 0.001


def parse_dispatch_prices(text: str, region: str) -> dict[datetime, float]:
    """{interval end: RRP in $/kWh} for `region` from a DISPATCHIS CSV.

    Only the physical run (INTERVENTION 0): an intervention interval
    publishes a second, pricing-only row, and settlement uses the first."""
    out: dict[datetime, float] = {}
    cols: list[str] | None = None
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 4 or row[1:3] != ["DISPATCH", "PRICE"]:
            continue
        if row[0] == "I":
            cols = row
            continue
        if row[0] != "D" or cols is None:
            continue
        rec = dict(zip(cols, row, strict=False))
        if rec.get("REGIONID") != region or rec.get("INTERVENTION", "0") != "0":
            continue
        # NEMWEB times are NEM time (UTC+10, no daylight saving).
        when = datetime.strptime(
            rec["SETTLEMENTDATE"] + " +1000", "%Y/%m/%d %H:%M:%S %z"
        )
        out[when] = float(rec["RRP"]) / 1000.0
    return out


def _csv_texts(blob: bytes):
    """Every CSV inside a NEMWEB zip, descending into nested zips (an
    Archive day holds one zip per interval)."""
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            data = z.read(name)
            if name.lower().endswith(".zip"):
                yield from _csv_texts(data)
            elif name.lower().endswith(".csv"):
                yield data.decode("utf-8", errors="replace")


def _get(url: str, cache: Path | None) -> bytes:
    if cache is not None:
        path = cache / url.rsplit("/", 1)[-1]
        if path.exists():
            return path.read_bytes()
    with urllib.request.urlopen(url, timeout=120) as resp:
        blob = resp.read()
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / url.rsplit("/", 1)[-1]).write_bytes(blob)
    return blob


def _listing(url: str) -> list[str]:
    with urllib.request.urlopen(url, timeout=120) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    return sorted(set(re.findall(r"PUBLIC_DISPATCHIS_[0-9_]+\.zip", page)))


def fetch_settled(
    region: str, start: datetime, end: datetime, cache: Path | None = None
) -> dict[datetime, float]:
    """The region's settled RRP for every interval ending in (start, end]."""
    out: dict[datetime, float] = {}
    current = _listing(CURRENT)
    archived = set(_listing(ARCHIVE))
    day = start.astimezone(NEM).date()
    last = end.astimezone(NEM).date()
    while day <= last:
        name = f"PUBLIC_DISPATCHIS_{day:%Y%m%d}.zip"
        if name in archived:
            for text in _csv_texts(_get(ARCHIVE + name, cache)):
                out.update(parse_dispatch_prices(text, region))
        else:
            stamp = f"PUBLIC_DISPATCHIS_{day:%Y%m%d}"
            for name in (n for n in current if n.startswith(stamp)):
                for text in _csv_texts(_get(CURRENT + name, cache)):
                    out.update(parse_dispatch_prices(text, region))
        day += timedelta(days=1)
    return {t: v for t, v in out.items() if start < t <= end}


def _state_at(times: list[datetime], values: list[float], when: datetime):
    i = bisect.bisect_right(times, when) - 1
    return values[i] if i >= 0 else None


def score(
    history: dict[str, list[tuple[datetime, float]]],
    settled: dict[datetime, float],
    factor: float = 1.0,
) -> dict[str, dict[str, float | int | None]]:
    """Per sensor: each settled interval paired with the sensor's state at
    the interval's end (the last state recorded at or before it), and how
    far that is from RRP x `factor`. Intervals before the sensor's first
    state, or with a non-positive RRP (no ratio), pair for the differences
    but not the ratio."""
    out: dict[str, dict[str, float | int | None]] = {}
    for name, points in history.items():
        points = sorted(points)
        times = [t for t, _ in points]
        values = [v for _, v in points]
        ratios: list[float] = []
        diffs: list[float] = []
        within = 0
        for end, rrp in sorted(settled.items()):
            v = _state_at(times, values, end)
            if v is None:
                continue
            target = rrp * factor
            diffs.append(abs(v - target))
            if rrp > 0:
                ratios.append(v / rrp)
            if abs(v - target) <= WITHIN * max(abs(target), 1e-9):
                within += 1
        n = len(diffs)
        out[name] = {
            "intervals": n,
            "median_ratio": round(statistics.median(ratios), 5) if ratios else None,
            "within_0_1pct": round(within / n, 4) if n else None,
            "median_abs_diff_c": round(statistics.median(diffs) * 100, 3)
            if n
            else None,
            "mean_abs_diff_c": round(statistics.fmean(diffs) * 100, 3) if n else None,
        }
    return out


def load_history(path: Path) -> dict[str, list[tuple[datetime, float]]]:
    raw = json.loads(path.read_text())
    out: dict[str, list[tuple[datetime, float]]] = {}
    for name, rows in raw.items():
        pts = []
        for when, value in rows:
            try:
                t = datetime.fromisoformat(when)
                v = float(value)
            except (TypeError, ValueError):
                continue  # unknown / unavailable states
            pts.append((t if t.tzinfo else t.replace(tzinfo=UTC), v))
        out[name] = pts
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--region", required=True)
    ap.add_argument("--history", required=True, type=Path)
    ap.add_argument("--factor", type=float, default=1.0)
    ap.add_argument("--cache", type=Path)
    args = ap.parse_args(argv)
    history = load_history(args.history)
    starts = [p[0][0] for p in (sorted(v) for v in history.values()) if p]
    ends = [p[-1][0] for p in (sorted(v) for v in history.values()) if p]
    if not starts:
        print("no usable states in the history")
        return 1
    settled = fetch_settled(args.region, min(starts), max(ends), args.cache)
    print(f"{len(settled)} settled {args.region} intervals, factor {args.factor}")
    for name, row in score(history, settled, args.factor).items():
        print(f"{name}: {json.dumps(row)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
