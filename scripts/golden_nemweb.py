#!/usr/bin/env python3
"""Build the nem_pd7day forecast sensor a Nimbus Part D scenario reads.

Spec 000 Part D (docs/specs/000-golden-master-and-gates.md). Never run by
the test suite: it needs a purcell-lab/nem_pd7day checkout, and its output
is committed.

    python scripts/golden_nemweb.py history <archive_dir> <scenario> <region> <now>
    python scripts/golden_nemweb.py forecast <nem_pd7day_checkout> <scenario> <region> <now>

``<now>`` is the frozen instant, ISO 8601 with an explicit offset.

``history`` writes ``TRADINGIS_<region>_history.price.csv.gz``: the region's
5-minute TradingIS ``TRADING,PRICE`` rows from 14 days before ``<now>`` (or
the start of the harvest, if later) to 8 days after, taken from a
purcell-lab/nem_pd7day harvest (``scripts/nemweb_harvest.py harvest``
there). NEMWEB rows are kept unchanged, with the ``I`` header row, and a
manifest row is appended per weekly archive read. Nimbus serves the rows
before ``<now>`` as the recorded history of the regional spot price sensor;
the rows after ``<now>`` are what actually happened, for the non-vacuity
checks.

``forecast`` reads the NEMWEB files committed under
``tests/golden/nemweb/<scenario>/`` in this repository (PD7DAY run, STPASA
run, market notices), passes them through nem_pd7day's own parsers and its
golden-master harness (tests/golden/harness.py there), and writes the
region's ``sensor.nem_pd7day_<region>_nem_spot_price_forecast`` state and
attributes, twice:

- ``nem_pd7day_forecast.passthrough.json.gz``: an empty calibration store, so
  ``calibrated`` equals the raw PD7DAY price (``calibrated_source`` is
  ``passthrough``). This is what a new nem_pd7day install publishes, and
  the case in which every PD7DAY price-cap forecast reaches Nimbus whole.
- ``nem_pd7day_forecast.fitted.json.gz``: nem_pd7day's synthetic observation
  seed (``ObservationSeed``, 14 days), fitted by the real calibration
  engine. The fit is synthetic; the market inputs are real.

Each file records its provenance: the nem_pd7day commit, and the SHA-256 of
every input file it read.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _one(folder: Path, pattern: str) -> Path:
    found = sorted(folder.glob(pattern))
    if len(found) != 1:
        raise SystemExit(f"expected one {pattern} in {folder}, found {len(found)}")
    return found[0]


def history(argv: list[str]) -> int:
    import datetime as dt
    import re

    archive = Path(argv[0]).resolve()
    scenario, region, now_iso = argv[1], argv[2], argv[3]
    folder = REPO / "tests" / "golden" / "nemweb" / scenario
    now = datetime.fromisoformat(now_iso)
    nem = dt.timezone(dt.timedelta(hours=10))
    start = (now - dt.timedelta(days=14)).astimezone(nem)
    end = (now + dt.timedelta(days=8)).astimezone(nem)
    header = None
    rows: list[str] = []
    read: list[str] = []
    for path in sorted((archive / "tradingis").glob("PUBLIC_TRADINGIS_*.price.csv.gz")):
        first, last = (
            dt.datetime.strptime(x, "%Y%m%d").replace(tzinfo=nem)
            for x in re.match(r"PUBLIC_TRADINGIS_(\d{8})_(\d{8})", path.name).groups()
        )
        if last + dt.timedelta(days=1) < start or first > end:
            continue
        read.append(path.name.replace(".price.csv.gz", ".zip"))
        for line in gzip.decompress(path.read_bytes()).decode("utf-8").splitlines(True):
            if line.startswith("I,TRADING,PRICE"):
                header = line
                continue
            cols = line.split(",")
            if len(cols) < 7 or cols[0] != "D" or cols[6] != region:
                continue
            stamp = dt.datetime.strptime(
                cols[4].strip('"'), "%Y/%m/%d %H:%M:%S"
            ).replace(tzinfo=nem)
            if start < stamp <= end:
                rows.append(line)
    if header is None or not rows:
        raise SystemExit("no TradingIS rows in the window")
    rows.sort(key=lambda r: r.split(",")[4])
    name = f"TRADINGIS_{region}_history.price.csv.gz"
    (folder / name).write_bytes(
        gzip.compress((header + "".join(rows)).encode("utf-8"), mtime=0)
    )
    manifest = folder / "manifest.jsonl"
    kept = [
        line
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("feed") != "tradingis"
    ]
    src = {
        json.loads(line)["file"]: json.loads(line)
        for line in (archive / "tradingis" / "manifest.jsonl").read_text().splitlines()
    }
    for zname in read:
        kept.append(
            json.dumps(
                {"feed": "tradingis", "trimmed": name, **src[zname]}, sort_keys=True
            )
        )
    manifest.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
    for old in folder.glob(f"TRADINGIS_{region}_2*.price.csv.gz"):
        old.unlink()
    print(scenario, name, len(rows), "rows", read)
    return 0


def forecast(argv: list[str]) -> int:
    pd7_repo = Path(argv[0]).resolve()
    scenario, region, now_iso = argv[1], argv[2], argv[3]
    folder = REPO / "tests" / "golden" / "nemweb" / scenario
    pd7_file = _one(folder, "PUBLIC_PD7DAY_*.trimmed.csv.gz")
    stpasa_file = _one(folder, "PUBLIC_STPASA_*.regionsolution.csv.gz")
    notice_files = sorted(folder.glob("NEMITWEB1_MKTNOTICE_*"))
    commit = subprocess.run(
        ["git", "-C", str(pd7_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    sys.path.insert(0, str(pd7_repo / "tests"))
    from golden import harness, snapshot  # nem_pd7day's golden package
    from golden import scenarios as pd7_scenarios

    now = datetime.fromisoformat(now_iso)
    source_zip = pd7_file.name.replace(".trimmed.csv.gz", ".zip")

    def builder_for(seed):
        def builder(mods):
            const = mods.const
            ids = set()
            for r in const.REGION_INTERCONNECTORS:
                ids |= set(const.REGION_INTERCONNECTORS[r])
            client = mods.pd7day_client.PD7DayClient(None, interconnector_ids=ids)

            async def _bytes(url):
                return gzip.decompress(pd7_file.read_bytes())

            client._fetch_bytes = _bytes
            result = asyncio.run(
                client.fetch_all(
                    [region],
                    ids,
                    file_meta={"name": pd7_file.name[:-3], "url": source_zip},
                )
            )
            result = type(result)(
                source_file=source_zip,
                case=result.case,
                prices=result.prices,
                market_summary=result.market_summary,
                interconnectors=result.interconnectors,
                updated_at=pd7_scenarios.iso(now),
            )
            for price in result.prices.values():
                price.source_file = source_zip
            stpasa_all = mods.stpasa_client._parse_all_regions(
                gzip.decompress(stpasa_file.read_bytes()), now=now
            )
            stpasa = stpasa_all[region]
            notices = []
            for f in notice_files:
                body = f.read_text(encoding="utf-8", errors="ignore")
                notices.append((int(f.suffix.lstrip(".R")), body))
            return pd7_scenarios.Scenario(
                name=f"nimbus_{scenario}",
                region=region,
                now=now,
                options={"forecast_mode": "days_1_7"},
                pd7day=result,
                stpasa=stpasa,
                stpasa_fetched_at=now,
                calibration=seed,
                market=pd7_scenarios.SyntheticMarket(seed=41, region=region),
                dispatch=None,
                notices=tuple(notices),
                usage_fee=None,
                entry_id=f"golden_nimbus_{scenario}",
            )

        builder.__name__ = f"nimbus_{scenario}"
        return builder

    provenance = {
        "nem_pd7day_commit": commit,
        "generator": "scripts/golden_nemweb.py",
        "frozen_at": now.isoformat(),
        "region": region,
        "inputs": {p.name: _sha256(p) for p in [pd7_file, stpasa_file, *notice_files]},
    }
    for variant, seed in (
        ("passthrough", pd7_scenarios.EmptySeed()),
        ("fitted", pd7_scenarios.ObservationSeed()),
    ):
        with harness.build_entities(builder_for(seed)) as built:
            want = "_nem_spot_price_forecast"
            found = [
                e
                for e in built.entities
                if (snapshot._read(e, "unique_id", "") or "").endswith("_forecast")
                and built.domains.get(id(e)) == "sensor"
                and "tariff" not in (snapshot._read(e, "unique_id", "") or "")
            ]
            records = [snapshot.entity_record(e, "sensor") for e in found]
            records = [
                r
                for r in records
                if r["unique_id"].lower().endswith(f"{region.lower()}_forecast")
            ] or records
            if len(records) != 1:
                raise SystemExit(
                    f"{variant}: expected one spot price forecast sensor, found "
                    + ", ".join(r["unique_id"] for r in records)
                )
            rec = records[0]
            out = {
                "provenance": dict(
                    provenance, variant=variant, unique_id=rec["unique_id"]
                ),
                "entity_id": f"sensor.nem_pd7day_{region.lower()}{want}",
                "state": rec["state"],
                "attributes": rec["extra_state_attributes"],
            }
        path = folder / f"nem_pd7day_forecast.{variant}.json.gz"
        text = json.dumps(snapshot.canonical(out), sort_keys=True, indent=1) + "\n"
        path.write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))
        fc = out["attributes"]["forecast"]
        print(variant, path.name, len(fc), "points")
    return 0


def main(argv: list[str]) -> int:
    commands = {"history": history, "forecast": forecast}
    if not argv or argv[0] not in commands:
        raise SystemExit(__doc__)
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
