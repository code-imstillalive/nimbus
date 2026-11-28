"""Where the forecaster keeps the weather it has fetched: one small CSV per model run.

Each file holds only the site's own values from one run of one model (a few
dozen rows), so a year of history is a few megabytes. Two views are built
from them:

- `dayahead(days)`: for each local day, the run that existed the evening
  before it (what a real day-ahead forecast had to go on): what the model is
  trained and scored on;
- `latest(now)`: the newest run of each model, for the live forecast.
"""

from __future__ import annotations

import csv
import datetime as dt
import os
from pathlib import Path

import numpy as np

from . import sources

COLUMNS = ("t", "ghi", "cloud", "low_cloud", "t2m", "ghi_sd")


class WeatherStore:
    def __init__(self, directory: str | os.PathLike) -> None:
        self.dir = Path(directory)

    def _path(self, source: str, run: dt.datetime) -> Path:
        return self.dir / f"{source}_{run:%Y%m%d%H}.csv"

    def has(self, source: str, run: dt.datetime) -> bool:
        return self._path(source, run).exists()

    def save(self, source: str, run: dt.datetime, series: dict) -> None:
        """Write one run (`sources.series()` output). Written to a temporary file and renamed, so a
        half-written file is never read."""
        if not len(series.get("t", ())):
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(source, run)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            n = len(series["t"])
            for i in range(n):
                row = []
                for c in COLUMNS:
                    v = series.get(c)
                    x = float(v[i]) if v is not None and i < len(v) else float("nan")
                    row.append("" if np.isnan(x) else round(x, 3))
                w.writerow(row)
        os.replace(tmp, path)

    def load(self, source: str, run: dt.datetime) -> dict | None:
        path = self._path(source, run)
        if not path.exists():
            return None
        cols: dict[str, list[float]] = {c: [] for c in COLUMNS}
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                for c in COLUMNS:
                    v = r.get(c, "")
                    cols[c].append(float(v) if v not in ("", None) else float("nan"))
        out = {c: np.array(v, dtype=float) for c, v in cols.items()}
        return {
            k: v for k, v in out.items() if k in ("t", "ghi") or not np.isnan(v).all()
        }

    def runs(self, source: str) -> list[dt.datetime]:
        out = []
        for p in self.dir.glob(f"{source}_*.csv"):
            try:
                out.append(
                    dt.datetime.strptime(p.stem.split("_")[-1], "%Y%m%d%H").replace(
                        tzinfo=dt.UTC
                    )
                )
            except ValueError:
                continue
        return sorted(out)

    def prune(self, keep_days: int, now: dt.datetime) -> int:
        """Delete runs older than `keep_days`. Returns how many went."""
        cut = now - dt.timedelta(days=keep_days)
        gone = 0
        for source in sources.SPECS:
            for run in self.runs(source):
                if run < cut:
                    self._path(source, run).unlink(missing_ok=True)
                    gone += 1
        return gone

    @staticmethod
    def _concat(parts: list[dict]) -> dict:
        if not parts:
            return {}
        keys = set().union(*parts)
        out = {}
        for k in keys:
            out[k] = np.concatenate(
                [p[k] if k in p else np.full(len(p["t"]), np.nan) for p in parts]
            )
        order = np.argsort(out["t"], kind="stable")
        out = {k: v[order] for k, v in out.items()}
        _, first = np.unique(out["t"], return_index=True)
        return {k: v[first] for k, v in out.items()}

    def dayahead(self, days: list[dt.date], day_window, lead: int = 1) -> dict:
        """Per source, each day as the run made `lead` days before it saw it (1: the evening-before
        run), cut to that day's window. `day_window(day) -> (t0, t1)` gives the day's UTC seconds."""
        out = {}
        for source in sources.SPECS:
            parts = []
            for d in days:
                run = sources.evening_before_run(
                    source, d - dt.timedelta(days=lead - 1)
                )
                s = self.load(source, run)
                if s is None:
                    continue
                t0, t1 = day_window(d)
                k = (s["t"] >= t0 - 3 * 3600) & (s["t"] <= t1 + 3 * 3600)
                if k.sum() >= 2:
                    parts.append({c: v[k] for c, v in s.items()})
            if parts:
                out[source] = self._concat(parts)
        return out

    def latest(self) -> dict:
        """Per source, its newest stored run, extended back with the run before it where the newest
        run starts later than the previous one's data (so 'now' is always covered)."""
        out = {}
        for source in sources.SPECS:
            runs = self.runs(source)
            if not runs:
                continue
            parts = [
                s for s in (self.load(source, r) for r in runs[-2:]) if s is not None
            ]
            if len(parts) == 2:
                newest = parts[1]
                older = parts[0]
                k = older["t"] < newest["t"].min()
                parts = [{c: v[k] for c, v in older.items()}, newest]
            out[source] = self._concat(parts)
        return out
