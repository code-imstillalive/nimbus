"""Price forecasts from each provider, read as half-open intervals in $/kWh
and placed on the plan's grid (#1550, #1578-#1581, #1606-#1613) (#1837).

Each adapter reads the provider's own rows (the public price rows captured
for #1550 under tests/fixtures/pricing) plus the shapes that break it:
overlaps, gaps, missing fields, non-finite prices. The invariants hold for
every provider: ordered, non-overlapping, end after start, finite.
"""

from __future__ import annotations

import itertools
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import price_intervals as pi
import pytest

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "pricing"
T0 = datetime.fromisoformat("2026-10-06T13:00:00+10:00")


def _fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _rows(name, sample=None):
    d = _fixture(name)
    return (d["samples"][sample] if sample else d)["selected_native_rows"]


def _well_formed(intervals):
    assert intervals, "the adapter read nothing"
    for a, b in itertools.pairwise(intervals):
        assert a.end <= b.start
    for i in intervals:
        assert i.end > i.start and math.isfinite(i.value)


# --- each provider's own rows -----------------------------------------------------------------


def test_aemo_nem_rows_are_their_own_intervals():
    rows = _rows("1550_aemo_nem_qld.json")
    assert pi.is_aemo_nem_rows(rows) and not pi.is_pd7day_rows(rows)
    got = pi.aemo_nem_intervals(rows)
    _well_formed(got)
    first = min(rows, key=lambda r: r["start_time"])
    assert (got[0].start.isoformat(), got[0].value) == (
        datetime.fromisoformat(first["start_time"]).isoformat(),
        float(first["price"]),
    )
    assert pi.intervals_from_rows(rows) == got


def test_amber_express_pairs_its_simple_forecast_with_the_detailed_intervals():
    d = _fixture("1550_amber_express.json")["samples"]
    simple = d["amber_express_general_simple"]["selected_native_rows"]
    detail = d["amber_express_general_detail"]["selected_native_rows"]
    got = pi.amber_express_intervals(simple, detail)
    _well_formed(got)
    assert pi.amber_express_intervals(simple, "x") is None
    assert pi.amber_express_intervals(None, detail) is None


# --- what breaks an adapter -------------------------------------------------------------------


def _aemo(start_min, end_min, price):
    return {
        "start_time": (T0 + timedelta(minutes=start_min)).isoformat(),
        "end_time": (T0 + timedelta(minutes=end_min)).isoformat(),
        "price": price,
    }


def test_overlaps_keep_the_first_row_and_bad_rows_are_skipped():
    rows = [
        _aemo(30, 60, 0.3),
        _aemo(0, 30, 0.1),
        _aemo(0, 30, 9.9),
        _aemo(45, 90, 9.9),
        _aemo(60, 60, 1.0),
        _aemo(90, 120, "nan"),
        {"start_time": "x"},
        "junk",
    ]
    got = pi.aemo_nem_intervals(rows)
    assert [(i.start - T0, i.value) for i in got] == [
        (timedelta(0), 0.1),
        (timedelta(minutes=30), 0.3),
    ]


@pytest.mark.parametrize(
    ("rows", "expected_minutes"),
    [
        ([{"datetime": T0.isoformat(), "value": 0.2, "interval_minutes": 15}], 15),
        ([{"datetime": T0.isoformat(), "value": 0.2, "interval_minutes": "x"}], 60),
        ([{"datetime": T0.isoformat(), "value": 0.2}], 60),
    ],
)
def test_an_openadr_row_without_a_usable_length_is_an_hour(rows, expected_minutes):
    (got,) = pi.openadr_intervals(rows)
    assert got.end - got.start == timedelta(minutes=expected_minutes)


def test_openadr_rows_come_from_the_get_forecast_response_for_price_programs_only():
    response = {
        "sensor.adr": {"payload_type": "PRICE", "forecast": [1]},
        "sensor.lvl": {"payload_type": "LEVEL", "forecast": [2]},
    }
    assert pi.openadr_rows_from_response(response, "sensor.adr") == [1]
    assert pi.openadr_rows_from_response(response, "sensor.lvl") is None
    assert pi.openadr_rows_from_response(response, "sensor.none") is None
    assert pi.openadr_rows_from_response("x", "sensor.adr") is None
    assert pi.is_openadr_price_attrs(
        {"forecast_rows": 3, "payload_type": "EXPORT_PRICE"}
    )


def test_amber_express_without_a_detailed_end_uses_the_next_point_up_to_half_an_hour():
    forecast = [
        {"time": T0.isoformat(), "value": 0.1},
        {"time": (T0 + timedelta(minutes=5)).isoformat(), "value": 0.2},
        {"time": (T0 + timedelta(hours=2)).isoformat(), "value": 0.3},
        {"time": "bad", "value": 1},
    ]
    got = pi.amber_express_intervals(forecast, [])
    assert [(i.start - T0, i.end - T0) for i in got] == [
        (timedelta(0), timedelta(minutes=5))
    ]


# --- LocalVolts v2 -----------------------------------------------------------------------------


def _lv(end_min, **row):
    return {"intervalEnd": (T0 + timedelta(minutes=end_min)).isoformat(), **row}


def test_localvolts_rates_and_matched_rates_by_direction_and_quality():
    rows = [
        _lv(5, direction="Buy", quality="Fcst", flexUp=30.0),
        _lv(10, direction="Sell", quality="Fcst", flexUp=10.0),
        _lv(15, direction="Buy", quality="Exp", flexUp=25.0),
        _lv(20, direction="Buy", quality="Fcst", flexUp="x"),
        _lv(25, intervalDuration=-5, flexUp=1.0),  # 0 means "not stated": 5 minutes
        "junk",
    ]
    got = pi.localvolts_v2_intervals(
        rows, "flexUp", direction="Buy", qualities=pi.LV_FORECAST_QUALITIES
    )
    assert [(i.end - T0, i.value) for i in got] == [(timedelta(minutes=5), 30.0)]
    matched = [
        _lv(5, volume=2.0, proportionP2P=0.5, matchedCost=0.4),
        _lv(10, volume=1.0, proportionP2P=0.005, matchedCost=1.0),
        _lv(15, volume=1.0, proportionP2P=0.5),
        _lv(20, volume=None, proportionP2P=0.5),
    ]
    rows_out = pi.localvolts_v2_rows(matched, pi.LV_MATCHED_RATE)
    assert [(r.interval.value, r.matched) for r in rows_out] == [
        (0.4, True),
        (0.0, False),
    ]


# --- on the plan's grid --------------------------------------------------------------------------


def test_intervals_on_the_grid_hold_the_latest_value_and_mark_real_coverage():
    intervals = pi.aemo_nem_intervals([_aemo(0, 30, 0.1), _aemo(60, 90, 0.3)])
    grid = [
        T0 - timedelta(minutes=30),
        T0,
        T0 + timedelta(minutes=30),
        T0 + timedelta(minutes=60),
        T0 + timedelta(minutes=120),
    ]
    values, covered = pi.on_grid(intervals, grid)
    assert values == [0.1, 0.1, 0.1, 0.3, 0.3]
    assert covered == [False, True, False, True, False]
    cov = pi.coverage(intervals)
    assert (cov.first_start, cov.final_end, cov.covered_hours) == (
        T0,
        T0 + timedelta(minutes=90),
        1.0,
    )
    assert cov.gaps == ((T0 + timedelta(minutes=30), T0 + timedelta(minutes=60)),)
    assert pi.coverage([]).covered_hours == 0.0
    assert pi.as_step_points(intervals) == [
        (T0, 0.1),
        (T0 + timedelta(minutes=60), 0.3),
    ]


def test_a_provider_sensor_is_recognised_by_its_attributes():
    aemo = {"forecast": _rows("1550_aemo_nem_qld.json")}
    assert pi.is_provider_shape(aemo)
    _values, covered = pi.provider_forecast_on_grid(aemo, [T0 + timedelta(minutes=10)])
    assert covered == [True]
    amber_core = {
        "forecast": [],
        pi.AMBER_CORE_ROWS_KEY: _rows("1550_amber_core.json", "amber_import_action"),
    }
    assert pi.is_provider_shape(amber_core)
    assert not pi.is_provider_shape({"forecast": [{"time": "t", "value": 1}]})
    assert not pi.is_provider_shape("x")
    adr = {"forecast_rows": 3, "payload_type": "PRICE"}
    assert pi.provider_forecast_on_grid(
        adr, [T0], lambda: [{"datetime": T0.isoformat(), "value": 0.5}]
    ) == ([0.5], [True])
    assert pi.provider_forecast_on_grid(adr, [T0]) is None


def test_amber_spot_for_the_interval_in_progress_is_ex_gst():
    detailed = [
        {
            "end_time": (T0 + timedelta(minutes=30)).isoformat(),
            "duration": 30,
            "spot_per_kwh": 0.11,
        },
        "junk",
        {"end_time": "x"},
    ]
    start, spot = pi.amber_spot_in_progress(detailed, T0 + timedelta(minutes=10))
    assert start == T0 and spot == pytest.approx(0.1)
    assert pi.amber_spot_in_progress(detailed, T0 + timedelta(hours=1)) is None
    assert pi.amber_spot_in_progress("x", T0) is None
