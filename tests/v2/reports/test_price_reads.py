"""What the plan reads from each provider's price sensor, through the real
reader in `solver_writer` (#1550, #1578-#1581) (#1837).

A provider's price is read as published, in $/kWh: never rebuilt from its
spot or raw parts, never negated or re-added to. Coverage is real only
inside an interval the provider published, up to its own end; a hole
between rows is held but not real. The rows are the public price rows
captured for #1550 under tests/fixtures/pricing.
"""

from __future__ import annotations

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


# --- what is real coverage ---------------------------------------------------------------------


def test_coverage_is_real_only_inside_a_published_interval(sensor):
    # test_coverage_is_real_only_inside_a_published_interval
    for attrs, grid, real in [
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
    ]:
        assert sensor([t(g) for g in grid], **attrs)[1] == real

    # test_no_usable_price_is_no_forecast_never_a_filled_one
    for attrs in [
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
    ]:
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


def test_past_the_retail_feed_the_regional_price_plus_the_learned_offset_is_used():
    aemo = [(t("2026-10-06T13:30:00+10:00"), 0.0267)]
    retail = [{"time": "2026-10-06T13:00:00+10:00", "costsflexup": 0.20}]
    grid = [t("2026-10-06T13:00:00+10:00"), t("2026-10-06T13:40:00+10:00")]
    bucket = 13 * 12 + 40 // 5
    values, real = sw.resample_price_with_extrapolation(
        retail, "costsflexup", grid, aemo, {bucket: 0.05}
    )
    assert values[0] == 0.20 and values[1] == pytest.approx(0.0767)
    assert real == [True, False]
    held, _ = sw.resample_price_with_extrapolation(retail, "costsflexup", grid, [], {})
    assert held == [0.20, 0.20]


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


def test_no_answer_for_this_entity_is_no_forecast(openadr, spot_store, monkeypatch):
    # test_no_answer_for_this_entity_is_no_forecast
    for response in [{}, {"sensor.other": {"payload_type": "PRICE", "forecast": []}}]:
        rows = _ADR["openadr_import"]["selected_native_rows"]
        if response:
            response["sensor.other"]["forecast"] = rows
        assert openadr(["2026-10-06T13:12:00+10:00"], rows, response=response) is None

    # test_the_first_sensor_with_amber_rows_is_the_one_read
    with monkeypatch.context() as mp:
        states = {
            "sensor.lv_buy": {"state": "0.2", "attributes": {"forecast": []}},
            "sensor.amber_feed_in": _amber(_spot_row(0.1074139)),
        }
        mp.setattr(sw, "ha_get", lambda e: states[e])
        assert (
            len(
                sw.collect_amber_spot(
                    ["sensor.lv_buy", None, "sensor.amber_feed_in"], SPOT_NOW
                )
            )
            == 1
        )

    # test_without_amber_or_with_a_failed_read_nothing_is_collected
    with monkeypatch.context() as mp:
        mp.setattr(sw, "ha_get", lambda _e: {"state": "0.2", "attributes": {}})
        assert sw.collect_amber_spot(["sensor.lv_buy"], SPOT_NOW) == {}
        assert not spot_store.exists()

        def down(_e):
            raise OSError("down")

        mp.setattr(sw, "ha_get", down)
        assert sw.collect_amber_spot(["sensor.amber_general"], SPOT_NOW) == {}

    # test_a_spot_older_than_five_days_is_dropped
    with monkeypatch.context() as mp:
        old = int((SPOT_END - timedelta(days=6)).timestamp()) // 300
        spot_store.write_text(json.dumps({str(old): 0.05}))
        mp.setattr(sw, "ha_get", lambda _e: _amber(_spot_row(0.11)))
        out = sw.collect_amber_spot(["sensor.amber_general"], SPOT_NOW)
        assert old not in out and len(out) == 1

    # test_the_markup_uses_the_same_poll_spot_over_a_late_regional_post
    with monkeypatch.context() as mp:
        late = [
            (MARKUP_START + timedelta(seconds=60), 0.1357)
        ]  # the previous interval's
        mp.setattr(sw, "fetch_price_history", lambda *_a, **_k: late)
        same = {int(MARKUP_START.timestamp()) // 300: (MARKUP_START, 0.1697)}
        regional = sw.compute_5min_offset(RETAIL, regional_spot_sensor="sensor.aemo")
        assert regional[BUCKET] == pytest.approx(0.40 - 0.1357)
        assert sw.compute_5min_offset(
            RETAIL, regional_spot_sensor="sensor.aemo", same_poll_wholesale=same
        )[BUCKET] == pytest.approx(0.40 - 0.1697)
        assert (
            sw.compute_5min_offset(
                RETAIL, regional_spot_sensor="sensor.aemo", same_poll_wholesale={}
            )
            == regional
        )

    # test_an_amber_only_install_needs_no_regional_sensor
    with monkeypatch.context() as mp:
        mp.setattr(sw, "fetch_price_history", lambda *_a, **_k: [])
        same = {int(MARKUP_START.timestamp()) // 300: (MARKUP_START, 0.15)}
        assert sw.compute_5min_offset(RETAIL, same_poll_wholesale=same) == {
            BUCKET: pytest.approx(0.25)
        }
        assert sw.compute_5min_offset(RETAIL) == {}

    # test_no_usable_spot_in_progress_is_none
    for rows in [
        None,
        [],
        [{"spot_per_kwh": 0.1}],
        [_spot_row(float("nan"))],
        [{**_spot_row(0.1), "spot_per_kwh": None}],
        [_spot_row(0.1, SPOT_END - timedelta(minutes=10))],  # not in progress
    ]:
        import price_intervals as pi

        assert pi.amber_spot_in_progress(rows, SPOT_NOW) is None

    # test_a_rows_price_is_value_then_calibrated_never_raw
    with monkeypatch.context() as mp:
        for row, expected in [
            (
                {"value": 0.20, "calibrated": 0.99},
                [0.20],
            ),  # value first, as it always was
            (
                {"raw_value": 8.999, "calibrated": 0.105355},
                [0.105355],
            ),  # never raw_value
            ({"spike_credible": True}, None),
        ]:
            with mp.context() as mp:
                rows = [{"time": "2026-08-25T12:00:00+00:00", **row}]
                mp.setattr(sw, "ha_get", lambda _e: {"attributes": {"forecast": rows}})  # noqa: B023
                grid = [datetime(2026, 8, 25, 12, 0, tzinfo=UTC)]
                assert (
                    sw.resample_generic_price_forecast("sensor.price", grid) == expected
                )


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


MARKUP_START = datetime(2026, 10, 10, 9, 5, tzinfo=UTC)  # 19:05 local
RETAIL = [(MARKUP_START + timedelta(seconds=20), 0.40)]
BUCKET = 19 * 12 + 1


def test_amber_rows_that_cannot_be_read_are_skipped_one_by_one(monkeypatch):
    # test_amber_rows_that_cannot_be_read_are_skipped_one_by_one
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

    # test_a_price_historys_unit_that_cannot_be_read_scales_by_one
    with monkeypatch.context() as mp:

        def down(_e):
            raise OSError("down")

        mp.setattr(sw, "ha_get", down)
        assert sw.price_unit_scale_of_history("sensor.price") == 1.0
