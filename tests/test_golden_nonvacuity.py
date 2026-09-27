"""Spec 000: the golden scenarios reach the paths they exist for.

An exact snapshot of an early return is still an exact snapshot, so each
scenario's purpose is asserted separately, against its committed snapshot
(fast; ``test_golden_master.py`` proves the snapshot is current). Part D
also checks that the committed NEMWEB-derived inputs are the files their
provenance names.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

GOLDEN = Path(__file__).parent / "golden"
SPIKE = 5.0  # $/kWh: well above any retail price, well below the $23.32 cap

NEMWEB_FOLDERS = sorted(p.name for p in (GOLDEN / "nemweb").iterdir() if p.is_dir())


def _snapshot(name: str) -> dict:
    return json.loads(
        gzip.decompress((GOLDEN / "snapshots" / f"{name}.json.gz").read_bytes())
    )


def _battery(cycle: dict) -> dict:
    (post,) = [
        p
        for p in cycle["posted"]
        if p["entity_id"] == "sensor.nimbus_solver_battery_forecast"
    ]
    return post["attributes"]


def test_two_cycles_reads_back_the_plan() -> None:
    snap = _snapshot("guardrail_two_cycles")
    assert "plan_state.json" in snap["files"]
    first, second = (_battery(c) for c in snap["cycles"])
    # Same inputs, five minutes later: the only reason the LP dual moves
    # is the proximal term against the plan cycle 1 saved.
    assert first["forecast"][1]["shadow_price"] != second["forecast"][1]["shadow_price"]


@pytest.mark.parametrize("folder", NEMWEB_FOLDERS)
def test_nemweb_inputs_match_their_provenance(folder: str) -> None:
    base = GOLDEN / "nemweb" / folder
    manifest = [
        json.loads(line) for line in (base / "manifest.jsonl").read_text().splitlines()
    ]
    assert {row["feed"] for row in manifest} >= {"pd7day", "stpasa", "tradingis"}
    assert all(row["url"].startswith("https://nemweb.com.au/") for row in manifest)
    for variant in ("passthrough", "fitted"):
        fc = json.loads(
            gzip.decompress(
                (base / f"nem_pd7day_forecast.{variant}.json.gz").read_bytes()
            )
        )
        for name, sha in fc["provenance"]["inputs"].items():
            assert hashlib.sha256((base / name).read_bytes()).hexdigest() == sha, name
        sources = {p["calibrated_source"] for p in fc["attributes"]["forecast"]}
        assert sources == (
            {"passthrough"} if variant == "passthrough" else sources - {"passthrough"}
        )


@pytest.mark.parametrize("folder", NEMWEB_FOLDERS)
def test_pd7day_spikes_reach_the_lp_uncalibrated_and_not_calibrated(
    folder: str,
) -> None:
    raw = _battery(_snapshot(f"{folder}_passthrough")["cycles"][0])
    spikes = [p for p in raw["forecast"] if p["import_price"] >= SPIKE]
    assert spikes, "no PD7DAY spike reached import_price"
    assert any(p["battery_kw"] > 0 for p in spikes), (
        "battery never discharges into a spike"
    )

    # Beyond the first 30 minutes every price is a PD7DAY forecast; inside
    # them the live price and the retail array decide (the real SA1 spike
    # in sa_nemweb_spike_arrived is live, not forecast).
    snap = _snapshot(f"{folder}_fitted")
    horizon = datetime.fromisoformat(snap["cycles"][0]["instant"]) + timedelta(
        minutes=30
    )
    fitted = _battery(snap["cycles"][0])
    late = [
        p
        for p in fitted["forecast"]
        if datetime.fromisoformat(p["time"]) >= horizon and p["import_price"] >= SPIKE
    ]
    assert not late, "calibration left a forecast spike in import_price"


@pytest.mark.parametrize("variant", ["passthrough", "fitted"])
def test_real_spike_arms_the_override(variant: str) -> None:
    attrs = _battery(_snapshot(f"sa_nemweb_spike_arrived_{variant}")["cycles"][0])
    assert attrs["price_spike_active"] is True
    assert attrs["forecast"][0]["import_price"] >= SPIKE
    assert attrs["forecast"][0]["battery_kw"] == 5.0


def test_forecast_spikes_alone_do_not_arm_the_override() -> None:
    for folder in NEMWEB_FOLDERS:
        if folder == "sa_nemweb_spike_arrived":
            continue
        attrs = _battery(_snapshot(f"{folder}_passthrough")["cycles"][0])
        assert attrs["price_spike_active"] is False, folder
