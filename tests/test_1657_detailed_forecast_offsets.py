"""nimbus #1671 follow-up (Mark Purcell's review): `detailedForecast` across a
UTC-offset change. Every #1671 fixture used one fixed +10:00.

A DST region publishes rows whose offsets change mid-series (Sydney: +10:00 ->
+11:00 at 02:00 on the first Sunday of October, back at 03:00 on the first
Sunday of April). Each row's `start` must keep its own offset, `end` must be
`start + hours` as an instant, and consecutive rows must stay contiguous as
instants even though their wall-clock strings jump or repeat.
"""

from __future__ import annotations

import sys
from datetime import datetime
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load import sensor_flattened as sf


def _row(time: str, hours: float, price: float) -> dict:
    return {
        "time": time,
        "hours": hours,
        "import_price": price,
        "import_price_raw": price,
        "import_price_source": "primary",
    }


def _series(rows: list[dict]) -> list[dict]:
    out = sf._detailed_price_forecast(
        {"forecast": rows, "status": "optimal"}, "import_price"
    )
    assert out is not None
    return out["detailedForecast"]


def _assert_contiguous(series: list[dict]) -> None:
    for a, b in pairwise(series):
        assert datetime.fromisoformat(a["end"]) == datetime.fromisoformat(b["start"]), (
            a,
            b,
        )


def test_spring_forward_keeps_each_rows_offset_and_stays_contiguous():
    # Sydney, Sunday 4 Oct 2026: 01:30 +10:00 is followed by 03:00 +11:00.
    rows = [
        _row("2026-10-04T01:00:00+10:00", 0.5, 0.20),
        _row("2026-10-04T01:30:00+10:00", 0.5, 0.21),
        _row("2026-10-04T03:00:00+11:00", 0.5, 0.22),
        _row("2026-10-04T03:30:00+11:00", 0.5, 0.23),
    ]
    series = _series(rows)
    assert [r["start"] for r in series] == [r["time"] for r in rows]
    # 01:30 +10:00 plus 30 minutes is the same instant as 03:00 +11:00.
    assert series[1]["end"] == "2026-10-04T02:00:00+10:00"
    _assert_contiguous(series)
    assert [r["value"] for r in series] == [0.20, 0.21, 0.22, 0.23]


def test_fall_back_keeps_the_repeated_hour_as_two_distinct_periods():
    # Sydney, Sunday 4 Apr 2027: 02:00-03:00 happens twice, +11:00 then +10:00.
    rows = [
        _row("2027-04-04T02:00:00+11:00", 0.5, 0.30),
        _row("2027-04-04T02:30:00+11:00", 0.5, 0.31),
        _row("2027-04-04T02:00:00+10:00", 0.5, 0.32),
        _row("2027-04-04T02:30:00+10:00", 0.5, 0.33),
    ]
    series = _series(rows)
    starts = [datetime.fromisoformat(r["start"]) for r in series]
    assert len(set(starts)) == 4  # the repeated wall-clock hour is not merged
    assert starts == sorted(starts)
    _assert_contiguous(series)
    assert [r["value"] for r in series] == [0.30, 0.31, 0.32, 0.33]


def test_mixed_period_lengths_across_the_change():
    # A 5-minute tier into a 30-minute tier, straddling the spring change.
    rows = [
        _row("2026-10-04T01:50:00+10:00", 5 / 60, 0.10),
        _row("2026-10-04T01:55:00+10:00", 5 / 60, 0.11),
        _row("2026-10-04T03:00:00+11:00", 0.5, 0.12),
    ]
    series = _series(rows)
    _assert_contiguous(series)
    assert series[-1]["end"] == "2026-10-04T03:30:00+11:00"
