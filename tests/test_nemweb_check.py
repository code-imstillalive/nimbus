"""nimbus #1786: scripts/nemweb_check.py's parsing and scoring, on synthetic
NEMWEB rows (the script's download is never run here)."""

from __future__ import annotations

import importlib.util
import io
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "nemweb_check", Path(__file__).resolve().parents[1] / "scripts" / "nemweb_check.py"
)
nc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nc)

NEM = timezone(timedelta(hours=10))
HEADER = (
    "I,DISPATCH,PRICE,5,SETTLEMENTDATE,RUNNO,REGIONID,DISPATCHINTERVAL,"
    "INTERVENTION,RRP,EEP\n"
)


def _row(when, region, rrp, intervention=0):
    return (
        f'D,DISPATCH,PRICE,5,"{when}",1,{region},20261020001,{intervention},{rrp},0\n'
    )


def test_parse_keeps_the_region_and_the_physical_run_in_dollars_per_kwh():
    text = (
        "C,NEMP.WORLD,DISPATCHIS\n"
        + HEADER
        + _row("2026/10/20 19:55:00", "QLD1", 143)
        + _row("2026/10/20 19:55:00", "NSW1", 148.1)
        + _row("2026/10/20 20:00:00", "QLD1", 150)
        + _row("2026/10/20 20:00:00", "QLD1", 999, intervention=1)
    )
    got = nc.parse_dispatch_prices(text, "QLD1")
    assert got == {
        datetime(2026, 10, 20, 19, 55, tzinfo=NEM): pytest.approx(0.143),
        datetime(2026, 10, 20, 20, 0, tzinfo=NEM): pytest.approx(0.150),
    }


def test_nested_archive_zips_are_read():
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("a.CSV", HEADER + _row("2026/10/20 19:55:00", "QLD1", 143))
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("PUBLIC_DISPATCHIS_202610201955.zip", inner.getvalue())
    texts = list(nc._csv_texts(outer.getvalue()))
    assert len(texts) == 1 and "QLD1" in texts[0]


def test_score_pairs_each_interval_with_the_state_at_its_end():
    t0 = datetime(2026, 10, 20, 10, 0, tzinfo=NEM)
    settled = {t0 + timedelta(minutes=5 * i): 0.10 + 0.01 * i for i in range(4)}
    factor = 1.05
    exact = [(t - timedelta(minutes=4), rrp * factor) for t, rrp in settled.items()]
    # A source 2c high throughout, and one starting after the first interval.
    high = [
        (t - timedelta(minutes=4), rrp * factor + 0.02) for t, rrp in settled.items()
    ]
    late = exact[2:]
    got = nc.score({"exact": exact, "high": high, "late": late}, settled, factor)
    assert got["exact"]["intervals"] == 4
    assert got["exact"]["within_0_1pct"] == 1.0
    assert got["exact"]["median_ratio"] == pytest.approx(1.05)
    assert got["exact"]["mean_abs_diff_c"] == pytest.approx(0.0, abs=1e-9)
    assert got["high"]["within_0_1pct"] == 0.0
    assert got["high"]["median_abs_diff_c"] == pytest.approx(2.0)
    assert got["late"]["intervals"] == 2
