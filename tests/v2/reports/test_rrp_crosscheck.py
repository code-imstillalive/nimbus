"""Every price source brought back to the RRP and compared (#2190).

Each source's spot figure is reduced to the AEMO RRP by the shape it
publishes, compared per 5-minute interval, and a source that stays put while
two others move is reported stale. A report only. Synthetic inputs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from solver_inputs import rrp_crosscheck as rc

NOW = datetime(2026, 10, 31, 0, 2, tzinfo=UTC)  # 10:02 Brisbane
SITE = 1.0605
HOUR = NOW.replace(minute=0)


def _lv(direction, values, start=HOUR):
    return {
        "source_field": "spotCost",
        "direction": direction,
        "forecast": [
            {"time": (start + timedelta(minutes=5 * i)).isoformat(), "value": v}
            for i, v in enumerate(values)
        ],
    }


def _amber(spot):
    end = NOW.replace(minute=5)
    return {
        "detailedForecast": [
            {
                "spot_per_kwh": spot,
                "duration": 5,
                "start_time": (end - timedelta(minutes=5, seconds=-1)).isoformat(),
                "end_time": end.isoformat(),
            }
        ]
    }


def test_each_source_reaches_the_rrp_and_disagreement_and_staleness_are_reported():
    # test_each_kind_is_divided_by_its_own_factor
    rrp = 0.0560
    sell = rc.read_source("lv_sell", None, _lv("Sell", [rrp * SITE] * 3), SITE, NOW)
    buy = rc.read_source("lv_buy", None, _lv("Buy", [rrp * SITE * 1.10] * 3), SITE, NOW)
    amber = rc.read_source("amber", None, _amber(rrp * 1.10), SITE, NOW)
    aemo = rc.read_source("aemo", str(rrp), {}, SITE, NOW)
    key = int(NOW.replace(minute=0).timestamp()) // 300
    for read in (sell, buy, amber, aemo):
        assert read.points[key] == pytest.approx(rrp), read.kind
    assert (sell.divisor, buy.divisor) == pytest.approx((SITE, SITE * 1.10))
    assert amber.kind == rc.KIND_AMBER and aemo.kind == rc.KIND_CURRENT

    # test_a_pd7day_row_is_read_as_its_wholesale_figure
    pd7 = rc.read_source(
        "pd7",
        None,
        {
            "forecast": [
                {
                    "time": NOW.replace(minute=0).isoformat(),
                    "nemtime": NOW.replace(minute=30).isoformat(),
                    "raw_value": rrp,
                    "value": 0.2,
                }
            ]
        },
        SITE,
        NOW,
    )
    assert pd7.kind == rc.KIND_PD7DAY and len(pd7.points) == 6
    assert pd7.points[key] == pytest.approx(rrp)

    # test_an_aemo_forecast_row_is_read_as_published
    aemo_fc = rc.read_source(
        "aemo_fc",
        "0.05",
        {
            "forecast": [
                {
                    "start_time": HOUR.isoformat(),
                    "end_time": NOW.replace(minute=30).isoformat(),
                    "price": rrp,
                }
            ]
        },
        SITE,
        NOW,
    )
    assert aemo_fc.kind == rc.KIND_AEMO_FORECAST
    assert aemo_fc.points[key] == pytest.approx(rrp) and len(aemo_fc.points) == 6

    # test_a_localvolts_row_left_out_is_no_figure_not_the_previous_one
    gappy = _lv("Sell", [rrp * SITE] * 3)
    gappy["interval_minutes"] = 5
    del gappy["forecast"][1]  # 00:05 has no spot row (fully matched)
    gap = rc.read_source("lv_gap", None, gappy, SITE, NOW)
    assert sorted(gap.points) == [key, key + 2]

    # test_without_a_site_loss_factor_localvolts_says_why
    blind = rc.read_source("lv", None, _lv("Sell", [0.06]), None, NOW)
    assert blind.points == {} and blind.reason == "no_site_loss_factor"

    # test_sources_that_agree_report_nothing_and_one_that_differs_is_listed
    agree = rc.compare([sell, buy, amber, aemo, pd7])
    assert agree["compared_intervals"] >= 1 and agree["disagreeing_intervals"] == 0
    off = rc.read_source("aemo", str(rrp + 0.02), {}, SITE, NOW)
    worst = rc.compare([sell, off])["worst"][0]
    assert worst["spread"] == pytest.approx(0.02)
    assert set(worst["rrp"]) == {"lv_sell", "aemo"}

    # test_a_source_that_stays_put_while_two_move_is_stale_on_any_interval
    def _aemo(values):
        return {
            "forecast": [
                {
                    "start_time": (HOUR + timedelta(minutes=5 * i)).isoformat(),
                    "end_time": (HOUR + timedelta(minutes=5 * i + 5)).isoformat(),
                    "price": v,
                }
                for i, v in enumerate(values)
            ]
        }

    def _pd7(values):
        return {
            "forecast": [
                {
                    "time": (HOUR + timedelta(minutes=5 * i)).isoformat(),
                    "nemtime": (HOUR + timedelta(minutes=5 * i + 5)).isoformat(),
                    "raw_value": v,
                }
                for i, v in enumerate(values)
            ]
        }

    def _three(aemo, pd7, lv):
        return [
            rc.read_source("a", None, _aemo(aemo), SITE, NOW),
            rc.read_source("b", None, _pd7(pd7), SITE, NOW),
            rc.read_source("c", None, _lv("Sell", [v * SITE for v in lv]), SITE, NOW),
        ]

    watch = rc.StaleWatch()
    flat, moved = [0.05] * 3, [0.05, 0.07, 0.07]  # two future intervals move
    assert watch.update(_three(flat, flat, flat)) == []
    stale = watch.update(_three(moved, moved, flat))
    assert [s["source"] for s in stale] == ["c"] and stale[0]["intervals"] == 2
    second = _three(moved, moved, flat)

    # test_one_retailer_feeds_buy_and_sell_moving_together_count_once
    one = rc.StaleWatch()
    base = [0.05 * SITE] * 3
    up = [0.05 * SITE, 0.07 * SITE, 0.07 * SITE]
    pd = {
        "forecast": [
            {
                "time": HOUR.isoformat(),
                "nemtime": NOW.replace(minute=30).isoformat(),
                "raw_value": 0.05,
            }
        ]
    }
    one.update(
        [
            rc.read_source("lv_s", None, _lv("Sell", base), SITE, NOW),
            rc.read_source(
                "lv_b", None, _lv("Buy", [v * 1.10 for v in base]), SITE, NOW
            ),
            rc.read_source("pd", None, pd, SITE, NOW),
        ]
    )
    assert (
        one.update(
            [
                rc.read_source("lv_s", None, _lv("Sell", up), SITE, NOW),
                rc.read_source(
                    "lv_b", None, _lv("Buy", [v * 1.10 for v in up]), SITE, NOW
                ),
                rc.read_source("pd", None, pd, SITE, NOW),
            ]
        )
        == []
    )

    # test_the_published_report_carries_every_source_and_the_factor
    ages = {
        "a": "2026-10-31T00:00:00+00:00",
        "b": None,
        "c": "2026-10-30T21:30:00+00:00",
    }
    report = rc.report(second, stale, SITE, ages)
    assert [s["as_of"] for s in report["sources"]] == [ages["a"], None, ages["c"]]
    assert (
        rc.as_of({"attributes": {"forecast_generated_at": "x"}, "last_updated": "y"})
        == "x"
    )
    assert rc.as_of({"attributes": {}, "last_updated": "y"}) == "y"
    assert report["site_loss_factor"] == pytest.approx(SITE)
    assert [s["source"] for s in report["sources"]] == ["a", "b", "c"]
    assert report["stale"] == stale and report["tolerance"] == rc.TOLERANCE
