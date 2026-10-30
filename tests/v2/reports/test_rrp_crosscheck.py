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
    watch = rc.StaleWatch()
    first = [
        rc.read_source("a", None, _lv("Sell", [0.05 * SITE] * 3), SITE, NOW),
        rc.read_source("b", None, _lv("Sell", [0.05 * SITE] * 3), SITE, NOW),
        rc.read_source("c", None, _lv("Sell", [0.05 * SITE] * 3), SITE, NOW),
    ]
    assert watch.update(first) == []
    moved = [0.05 * SITE, 0.07 * SITE, 0.07 * SITE]  # a future interval moves
    second = [
        rc.read_source("a", None, _lv("Sell", moved), SITE, NOW),
        rc.read_source("b", None, _lv("Sell", moved), SITE, NOW),
        rc.read_source("c", None, _lv("Sell", [0.05 * SITE] * 3), SITE, NOW),
    ]
    stale = watch.update(second)
    assert [s["source"] for s in stale] == ["c"] and stale[0]["intervals"] >= 2

    # test_the_published_report_carries_every_source_and_the_factor
    report = rc.report(second, stale, SITE)
    assert report["site_loss_factor"] == pytest.approx(SITE)
    assert [s["source"] for s in report["sources"]] == ["a", "b", "c"]
    assert report["stale"] == stale and report["tolerance"] == rc.TOLERANCE
