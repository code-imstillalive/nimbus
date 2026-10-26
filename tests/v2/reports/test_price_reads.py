"""What the plan reads from each provider's price sensor, through the real
reader in `solver_writer` (#1550, #1578-#1581) (#1837).

A provider's price is read as published, in $/kWh: never rebuilt from its
spot or raw parts, never negated or re-added to. Coverage is real only
inside an interval the provider published, up to its own end; a hole
between rows is held but not real. The rows are the public price rows
captured for #1550 under tests/fixtures/pricing.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import solver_writer as sw

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "pricing"


def _fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


AEMO = _fixture("1550_aemo_nem_qld.json")["selected_native_rows"]
_PD7 = _fixture("1550_pd7day_qld_wholesale.json")
PD7 = _PD7["selected_native_rows"]
PD7_TARIFF = {
    s["sample_alias"]: s["selected_native_rows"] for s in _PD7["tariff_samples"]
}
_CORE = _fixture("1550_amber_core.json")["samples"]
_EXPRESS = _fixture("1550_amber_express.json")["samples"]
SIMPLE = _EXPRESS["amber_express_general_simple"]["selected_native_rows"]
DETAIL = _EXPRESS["amber_express_general_detail"]["selected_native_rows"]


def _core(alias):
    return [
        {k: v for k, v in r.items() if not k.startswith("advanced_price")}
        for r in _CORE[alias]["selected_native_rows"]
    ]


def t(s):
    return datetime.fromisoformat(s)


@pytest.fixture
def sensor(monkeypatch):
    """One price sensor whose attributes the test sets."""
    attrs = {}
    monkeypatch.setattr(sw, "ha_get", lambda _e: {"attributes": attrs})

    def read(grid, **set_attrs):
        attrs.clear()
        attrs.update(set_attrs)
        return sw.resample_generic_price_forecast_with_coverage("sensor.price", grid)

    read.attrs = attrs
    return read


# --- the price is the one published ------------------------------------------------------------


@pytest.mark.parametrize(
    ("attrs", "at", "value"),
    [
        (
            {"forecast": AEMO},
            "2026-10-06T13:00:00+10:00",
            0.0297,
        ),  # $/kWh, not /1000 again
        (
            {"forecast": PD7},
            "2026-10-06T12:40:00+10:00",
            0.043006,
        ),  # calibrated, not raw
        ({"forecast": PD7_TARIFF["pd7_import"]}, "2026-10-06T12:40:00+10:00", 0.081069),
        ({"forecast": PD7_TARIFF["pd7_export"]}, "2026-10-06T12:40:00+10:00", 0.024859),
        (
            {"forecasts": _core("amber_import_action")},
            "2026-10-06T13:22:00+10:00",
            0.0620461,
        ),
        (  # per_kwh, never spot_per_kwh or the advanced prediction
            {"forecasts": _CORE["amber_import_action"]["selected_native_rows"]},
            "2026-10-06T14:10:00+10:00",
            0.039132099999999996,
        ),
        (
            {"forecast": SIMPLE, "detailedForecast": DETAIL},
            "2026-10-06T13:20:00+10:00",
            0.0613,
        ),
        ({"forecast": SIMPLE}, "2026-10-06T13:20:00+10:00", 0.0613),
    ],
    ids=[
        "aemo",
        "pd7day_wholesale",
        "pd7day_import_tariff",
        "pd7day_export_tariff",
        "amber_core",
        "amber_core_not_advanced",
        "amber_express",
        "amber_express_without_detail",
    ],
)
def test_each_providers_price_is_read_as_published(sensor, attrs, at, value):
    values, real = sensor([t(at)], **attrs)
    assert values == [value] and real == [True]


def test_a_feed_in_price_keeps_its_published_sign(sensor):
    values, _ = sensor(
        [t("2026-10-06T13:22:00+10:00"), t("2026-10-07T12:45:00+10:00")],
        forecasts=_core("amber_export_action"),
    )
    assert values == [0.0263986, -0.0038316]  # paying to export stays negative
    negative = [
        {
            "start_time": "2026-10-06T11:00:00+10:00",
            "end_time": "2026-10-06T11:30:00+10:00",
            "price": -0.0412,
        }
    ]
    assert sensor([t("2026-10-06T11:10:00+10:00")], forecast=negative) == (
        [-0.0412],
        [True],
    )


def test_amber_express_is_neither_negated_nor_given_its_demand_window_twice(sensor):
    at = [t("2026-10-06T13:20:00+10:00")]
    flipped = copy.deepcopy(DETAIL)
    for d in flipped:
        d["per_kwh"] = -d["per_kwh"]
    simple = [dict(r, value=abs(r["value"])) for r in SIMPLE]
    assert sensor(at, forecast=simple, detailedForecast=flipped)[0] == [0.0613]
    window = [dict(r) for r in SIMPLE]
    window[1]["value"] = 0.0613 + 0.20  # upstream already added it
    flagged = copy.deepcopy(DETAIL)
    flagged[1]["demand_window"] = True
    assert sensor(at, forecast=window, detailedForecast=flagged)[0] == [0.0613 + 0.20]


# --- what is real coverage ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attrs", "grid", "real"),
    [
        (  # in AEMO's hole; then at its final END
            {"forecast": AEMO},
            ["2026-10-07T12:00:00+10:00", "2026-10-08T04:00:00+10:00"],
            [False, False],
        ),
        (  # inside PD7DAY's last interval, at its end, in its hole
            {"forecast": PD7},
            [
                "2026-10-14T03:45:00+10:00",
                "2026-10-14T04:00:00+10:00",
                "2026-10-08T12:00:00+10:00",
            ],
            [True, False, False],
        ),
        (  # the 30-minute seam before the continuation starts
            {"forecast": PD7_TARIFF["pd7_continuation"]},
            ["2026-10-07T13:10:00+10:00"],
            [False],
        ),
        (
            {"forecasts": _core("amber_import_action")},
            [
                "2026-10-07T12:59:00+10:00",
                "2026-10-07T13:00:00+10:00",
                "2026-10-06T18:00:00+10:00",
            ],
            [True, False, False],
        ),
        (  # Express runs to the detailed end; a row hours before the next is not hours
            {"forecast": SIMPLE, "detailedForecast": DETAIL},
            [
                "2026-10-07T12:59:00+10:00",
                "2026-10-07T13:00:00+10:00",
                "2026-10-06T18:00:00+10:00",
            ],
            [True, False, False],
        ),
        (  # the key is case-sensitive: plain {time, value}, real to the last START
            {"forecast": SIMPLE, "detailed_forecast": DETAIL},
            ["2026-10-07T12:45:00+10:00"],
            [False],
        ),
        (
            {"forecast": [{"time": "2026-10-06T13:15:00+10:00", "value": 0.0848}]},
            ["2026-10-06T13:15:00+10:00", "2026-10-06T13:20:00+10:00"],
            [True, False],
        ),
    ],
    ids=[
        "aemo",
        "pd7day",
        "pd7day_seam",
        "amber_core",
        "amber_express",
        "express_wrong_key",
        "time_value",
    ],
)
def test_coverage_is_real_only_inside_a_published_interval(sensor, attrs, grid, real):
    assert sensor([t(g) for g in grid], **attrs)[1] == real


def test_a_hole_is_held_but_not_real(sensor):
    values, _ = sensor(
        [t("2026-10-07T12:00:00+10:00"), t("2026-10-08T04:00:00+10:00")], forecast=AEMO
    )
    assert values == [0.0267, 0.1176]


@pytest.mark.parametrize(
    "attrs",
    [
        {"forecast": [dict(PD7_TARIFF["pd7_import"][0], value=None)]},
        {
            "forecast": [
                {
                    "start_time": "2026-10-06T11:00:00+10:00",
                    "end_time": "2026-10-06T11:30:00+10:00",
                    "price": None,
                }
            ]
        },
        {"forecasts": []},
        {"forecasts": [{"time": "x"}]},
    ],
    ids=["pd7day_null", "aemo_null", "empty", "unreadable"],
)
def test_no_usable_price_is_no_forecast_never_a_filled_one(sensor, attrs):
    assert sensor([t("2026-10-06T12:40:00+10:00")], **attrs) is None


# --- the regional forecast past the retail feed --------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "points"),
    [
        (
            AEMO,
            [
                ("2026-10-06T13:00:00+10:00", 0.0297),
                ("2026-10-06T13:30:00+10:00", 0.0267),
                ("2026-10-08T03:30:00+10:00", 0.1176),
            ],
        ),
        (
            PD7,
            [
                ("2026-10-06T12:30:00+10:00", 0.043006),
                ("2026-10-14T03:30:00+10:00", 0.081885),
            ],
        ),
    ],
    ids=["aemo", "pd7day"],
)
def test_the_regional_forecast_reads_aemo_and_pd7day_alike(monkeypatch, rows, points):
    monkeypatch.setattr(sw, "ha_get", lambda _e: {"attributes": {"forecast": rows}})
    assert sw.fetch_aemo_forecast("sensor.regional") == [(t(a), v) for a, v in points]


def test_a_grid_point_exactly_at_an_interval_start_reads_that_interval(sensor):
    start = t("2026-10-06T13:00:00+10:00")
    rows = [
        {
            "start_time": (start + timedelta(minutes=30 * i)).isoformat(),
            "end_time": (start + timedelta(minutes=30 * (i + 1))).isoformat(),
            "price": 0.01 * (i + 1),
        }
        for i in range(2)
    ]
    assert sensor([start + timedelta(minutes=30)], forecast=rows) == ([0.02], [True])


# --- OpenADR: the forecast comes from the integration's own action (#1583) ----------------------

_ADR = _fixture("1550_openadr.json")["samples"]
ADR_EID = "sensor.program_price"
ADR_SUMMARY = {
    "payload_type": "PRICE",
    "forecast_rows": 338,
    "forecast_start": "2026-10-06T13:10:00+10:00",
    "forecast_end": "2026-10-13T13:00:00+10:00",  # the last START, not the end
    "unit_of_measurement": "$/kWh",
}


@pytest.fixture
def openadr(monkeypatch):
    calls = []

    def read(grid, rows=None, payload="PRICE", attrs=None, response=None):
        a = {**ADR_SUMMARY, **(attrs or {})}
        if response is None:
            response = {
                ADR_EID: {"payload_type": payload, "unit": "$/kWh", "forecast": rows}
            }
        monkeypatch.setattr(sw, "ha_get", lambda _e: {"attributes": a})
        monkeypatch.setattr(
            sw,
            "ha_call_service_with_response",
            lambda d, s, data: calls.append((d, s, data)) or response,
        )
        return sw.resample_generic_price_forecast_with_coverage(
            ADR_EID, [t(g) for g in grid]
        )

    read.calls = calls
    return read


def test_openadr_rows_keep_their_own_lengths_and_end_after_the_last_row(openadr):
    rows = _ADR["openadr_import"]["selected_native_rows"]
    grid = [
        "2026-10-06T13:12:00+10:00",
        "2026-10-06T13:14:59+10:00",  # the 5-minute row
        "2026-10-06T13:20:00+10:00",  # the 15-minute row
        "2026-10-06T13:45:00+10:00",  # the first 30-minute row
        "2026-10-13T13:20:00+10:00",  # the last row runs 30 minutes past its start
        "2026-10-13T13:30:00+10:00",
    ]
    values, real = openadr(grid, rows)
    assert values[:4] == [0.062571, 0.062571, 0.081069, 0.053818]
    assert real == [True, True, True, True, True, False]
    assert openadr.calls == [("openadr3_ven", "get_forecast", {"entity_id": ADR_EID})]


def test_an_hourly_programs_hole_is_not_coverage_and_export_reads_too(openadr):
    hourly = _ADR["openadr_second_program"]["selected_native_rows"]
    _, real = openadr(
        ["2026-10-10T16:30:00+10:00", "2026-10-11T03:00:00+10:00"],
        hourly,
        attrs={"forecast_rows": 144},
    )
    assert real == [True, False]
    values, _ = openadr(
        ["2026-10-06T13:12:00+10:00"],
        _ADR["openadr_export"]["selected_native_rows"],
        payload="EXPORT_PRICE",
        attrs={"payload_type": "EXPORT_PRICE"},
    )
    assert values == [0.027605]


@pytest.mark.parametrize(
    "response",
    [{}, {"sensor.other": {"payload_type": "PRICE", "forecast": []}}],
    ids=["failed", "another_entity"],
)
def test_no_answer_for_this_entity_is_no_forecast(openadr, response):
    rows = _ADR["openadr_import"]["selected_native_rows"]
    if response:
        response["sensor.other"]["forecast"] = rows
    assert openadr(["2026-10-06T13:12:00+10:00"], rows, response=response) is None


def test_a_sensor_that_is_not_openadr_never_calls_the_action(sensor, monkeypatch):
    monkeypatch.setattr(
        sw,
        "ha_call_service_with_response",
        lambda *_a: pytest.fail("the action was called"),
    )
    rows = [{"time": "2026-10-06T13:15:00+10:00", "value": 0.0848}]
    assert sensor([t("2026-10-06T13:15:00+10:00")], forecast=rows)[0] == [0.0848]


# --- Amber's own spot, from the same poll as its retail price (#1698) ---------------------------

SPOT_END = datetime(2026, 10, 14, 10, 50, tzinfo=UTC)
SPOT_NOW = SPOT_END - timedelta(minutes=2)
SPOT_KEY = int((SPOT_END - timedelta(minutes=5)).timestamp()) // 300


def _spot_row(spot, end=SPOT_END):
    return {
        "per_kwh": 0.357,
        "spot_per_kwh": spot,
        "duration": 5,
        "start_time": (end - timedelta(minutes=5, seconds=-1)).isoformat(),
        "end_time": end.isoformat(),
        "estimate": False,
    }


def _amber(*rows):
    return {"state": "0.357", "attributes": {"detailedForecast": list(rows)}}


@pytest.fixture
def spot_store(tmp_path, monkeypatch):
    path = tmp_path / "amber_spot.json"
    monkeypatch.setattr(sw, "AMBER_SPOT_PATH", str(path))
    monkeypatch.setattr(sw, "_AMBER_SPOT_CACHE", None)
    return path


def test_the_settled_spot_replaces_the_provisional_one_ex_gst(spot_store, monkeypatch):
    for spot in (0.153989, 0.1514426):
        monkeypatch.setattr(sw, "ha_get", lambda _e, s=spot: _amber(_spot_row(s)))
        out = sw.collect_amber_spot(["sensor.amber_general"], SPOT_NOW)
    assert out[SPOT_KEY] == (
        SPOT_END - timedelta(minutes=5),
        pytest.approx(0.1514426 / 1.10),
    )
    assert json.loads(spot_store.read_text()) == {
        str(SPOT_KEY): pytest.approx(0.1514426 / 1.10)
    }


def test_the_first_sensor_with_amber_rows_is_the_one_read(spot_store, monkeypatch):
    states = {
        "sensor.lv_buy": {"state": "0.2", "attributes": {"forecast": []}},
        "sensor.amber_feed_in": _amber(_spot_row(0.1074139)),
    }
    monkeypatch.setattr(sw, "ha_get", lambda e: states[e])
    assert (
        len(
            sw.collect_amber_spot(
                ["sensor.lv_buy", None, "sensor.amber_feed_in"], SPOT_NOW
            )
        )
        == 1
    )


def test_without_amber_or_with_a_failed_read_nothing_is_collected(
    spot_store, monkeypatch
):
    monkeypatch.setattr(sw, "ha_get", lambda _e: {"state": "0.2", "attributes": {}})
    assert sw.collect_amber_spot(["sensor.lv_buy"], SPOT_NOW) == {}
    assert not spot_store.exists()

    def down(_e):
        raise OSError("down")

    monkeypatch.setattr(sw, "ha_get", down)
    assert sw.collect_amber_spot(["sensor.amber_general"], SPOT_NOW) == {}


def test_a_spot_older_than_five_days_is_dropped(spot_store, monkeypatch):
    old = int((SPOT_END - timedelta(days=6)).timestamp()) // 300
    spot_store.write_text(json.dumps({str(old): 0.05}))
    monkeypatch.setattr(sw, "ha_get", lambda _e: _amber(_spot_row(0.11)))
    out = sw.collect_amber_spot(["sensor.amber_general"], SPOT_NOW)
    assert old not in out and len(out) == 1


MARKUP_START = datetime(2026, 10, 10, 9, 5, tzinfo=UTC)  # 19:05 local
RETAIL = [(MARKUP_START + timedelta(seconds=20), 0.40)]
BUCKET = 19 * 12 + 1


def test_an_amber_only_install_needs_no_regional_sensor(monkeypatch):
    monkeypatch.setattr(sw, "fetch_price_history", lambda *_a, **_k: [])
    same = {int(MARKUP_START.timestamp()) // 300: (MARKUP_START, 0.15)}
    assert sw.compute_5min_offset(RETAIL, same_poll_wholesale=same) == {
        BUCKET: pytest.approx(0.25)
    }
    assert sw.compute_5min_offset(RETAIL) == {}


def test_the_spot_in_progress_belongs_to_the_interval_starting_at_the_boundary():
    import price_intervals as pi

    rows = [_spot_row(0.1), _spot_row(0.2, SPOT_END + timedelta(minutes=5))]
    assert pi.amber_spot_in_progress(rows, SPOT_END) == (
        SPOT_END,
        pytest.approx(0.2 / 1.10),
    )


@pytest.mark.parametrize(
    "rows",
    [
        None,
        [],
        [{"spot_per_kwh": 0.1}],
        [_spot_row(float("nan"))],
        [{**_spot_row(0.1), "spot_per_kwh": None}],
        [_spot_row(0.1, SPOT_END - timedelta(minutes=10))],  # not in progress
    ],
)
def test_no_usable_spot_in_progress_is_none(rows):
    import price_intervals as pi

    assert pi.amber_spot_in_progress(rows, SPOT_NOW) is None


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"value": 0.20, "calibrated": 0.99}, [0.20]),  # value first, as it always was
        ({"raw_value": 8.999, "calibrated": 0.105355}, [0.105355]),  # never raw_value
        ({"spike_credible": True}, None),
    ],
    ids=["value_over_calibrated", "calibrated_not_raw", "neither"],
)
def test_a_rows_price_is_value_then_calibrated_never_raw(monkeypatch, row, expected):
    rows = [{"time": "2026-08-25T12:00:00+00:00", **row}]
    monkeypatch.setattr(sw, "ha_get", lambda _e: {"attributes": {"forecast": rows}})
    grid = [datetime(2026, 8, 25, 12, 0, tzinfo=UTC)]
    assert sw.resample_generic_price_forecast("sensor.price", grid) == expected


def test_amber_rows_that_cannot_be_read_are_skipped_one_by_one():
    import price_intervals as pi

    good = _core("amber_import_action")[0]
    rows = [
        "not a row",
        {k: v for k, v in good.items() if k != "per_kwh"},
        {**good, "end_time": "garbage"},
        {**good, "per_kwh": float("nan")},
        good,
    ]
    (only,) = pi.amber_core_intervals(rows)
    assert only.value == good["per_kwh"]


def test_a_price_historys_unit_that_cannot_be_read_scales_by_one(monkeypatch):
    def down(_e):
        raise OSError("down")

    monkeypatch.setattr(sw, "ha_get", down)
    assert sw.price_unit_scale_of_history("sensor.price") == 1.0


def test_amber_express_rows_are_ordered_by_time_never_by_price(sensor):
    """Rows arriving dearest first still read in time order."""
    rows = sorted(SIMPLE, key=lambda r: -float(r["value"] or 0.0))
    ordered, _ = sensor(
        [t("2026-10-06T13:20:00+10:00")], forecast=SIMPLE, detailedForecast=DETAIL
    )
    shuffled, _ = sensor(
        [t("2026-10-06T13:20:00+10:00")], forecast=rows, detailedForecast=DETAIL
    )
    assert shuffled == ordered == [0.0613]


def test_without_detail_each_amber_express_price_runs_to_the_next_in_time():
    import price_intervals as pi

    t0 = t("2026-10-06T13:00:00+10:00")
    rows = [
        {"time": (t0 + timedelta(minutes=10)).isoformat(), "value": 0.10},
        {"time": t0.isoformat(), "value": 0.30},
        {"time": (t0 + timedelta(minutes=5)).isoformat(), "value": 0.20},
    ]
    got = [(i.start, i.end, i.value) for i in pi.amber_express_intervals(rows, [])]
    assert got[:2] == [
        (t0, t0 + timedelta(minutes=5), 0.30),
        (t0 + timedelta(minutes=5), t0 + timedelta(minutes=10), 0.20),
    ]
