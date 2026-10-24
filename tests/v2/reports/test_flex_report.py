"""The daily flex report: what flexibility was offered against what the
battery really did, the solar the export limit cut, and net import by
price band, from yesterday's history (#489, #496) (#1837).

The offer is unknown, never a made-up zero, on a day the flex signals were
off. Synthetic histories only.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import solver_writer as sw

TZ = sw.LOCAL_TZ
DAY = datetime(2026, 9, 13, tzinfo=TZ)
END = DAY + timedelta(days=1)


def cfg(**kw):
    return {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_grid_max_export_kw": 5.0,
        **kw,
    }


def flat(value, start, end):
    out, t = [], start
    while t < end:
        out.append((t, value))
        t += timedelta(minutes=30)
    return out


@pytest.fixture
def day(monkeypatch):
    """Solar 0, load 2 kW, battery idle, 20 c/kWh, flex signals off."""
    s = {
        "sensor.real_solar": 0.0,
        "sensor.real_load": 2.0,
        "sensor.real_battery": 0.0,
        "sensor.import_price": 0.20,
        sw.FLEX_SIGNALS_ENTITY_ID: None,
        "down": None,
    }

    def fetch(entity, start, end):
        value = s.get(entity)
        return [] if value is None else flat(value, start, end)

    def fetch_attr(entity, attribute, start, end):
        if (
            entity == sw.FLEX_SIGNALS_ENTITY_ID
            and attribute == "flex_available_down_kw"
        ):
            return [] if s["down"] is None else flat(s["down"], start, end)
        return []

    monkeypatch.setattr(sw, "fetch_entity_history_range", fetch)
    monkeypatch.setattr(sw, "fetch_entity_attribute_history_range", fetch_attr)

    def offline(*_a, **_k):
        raise sw.urllib.error.URLError("no Home Assistant in this test")

    monkeypatch.setattr(sw.urllib.request, "urlopen", offline)
    return s


def report(c=None, start=DAY, end=END):
    return sw._compute_flex_report_for_window(c or cfg(), start, end)


@pytest.mark.parametrize(
    ("c", "end"),
    [
        (cfg(solver_solar_power_sensor=None), END),
        (None, DAY + timedelta(hours=6)),
        (None, DAY),
    ],
    ids=["no_solar_sensor", "short", "empty"],
)
def test_a_day_that_cannot_be_reported_is_none(day, c, end):
    assert report(c, end=end) is None


def test_no_base_history_is_no_report(day):
    for entity in ("sensor.real_solar", "sensor.real_load", "sensor.real_battery"):
        day[entity] = None
    assert report() is None


def test_an_offer_never_made_is_unknown_and_one_made_is_integrated(day):
    off = report()
    assert (off["offered_up_kwh"], off["offered_down_kwh"]) == (None, None)
    day[sw.FLEX_SIGNALS_ENTITY_ID], day["down"] = 1.5, 2.5
    on = report()
    assert on["offered_up_kwh"] == pytest.approx(36.0, abs=0.1)
    assert on["offered_down_kwh"] == pytest.approx(60.0, abs=0.1)


@pytest.mark.parametrize(
    ("reading", "positive_is_charge"), [(-3.0, False), (3.0, True)]
)
def test_charging_is_realised_up_whichever_sign_the_sensor_uses(
    day, reading, positive_is_charge
):
    day["sensor.real_battery"] = reading
    c = cfg(solver_battery_power_positive_is_charge=positive_is_charge)
    r = report(c)
    assert r["realised_up_kwh"] == pytest.approx(72.0, abs=0.1)
    assert r["realised_down_kwh"] == pytest.approx(0.0, abs=0.1)


@pytest.mark.parametrize(
    ("solar", "limit", "curtailed"),
    [(1.0, 5.0, 0.0), (10.0, 5.0, 72.0), (10.0, 1.0, 168.0), (10.0, 20.0, 0.0)],
)
def test_curtailment_is_the_solar_past_load_plus_the_configured_limit(
    day, solar, limit, curtailed
):
    day["sensor.real_solar"] = solar
    r = report(cfg(solver_grid_max_export_kw=limit))
    assert r["envelope_curtailment_kwh"] == pytest.approx(curtailed, abs=0.1)


def test_net_import_is_banded_by_price(day):
    day["sensor.import_price"] = 0.22
    (band,) = report()["price_response_curve"]
    assert (band["price_band_low"], band["price_band_high"], band["n_samples"]) == (
        0.20,
        0.25,
        48,
    )
    day.update(
        {"sensor.real_solar": 2.0, "sensor.real_load": 5.0, "sensor.import_price": 0.30}
    )
    (band,) = report()["price_response_curve"]
    assert band["mean_net_import_kw"] == pytest.approx(3.0, abs=0.1)


def test_yesterdays_report_is_dated_yesterday(day):
    r = sw.compute_daily_flex_report(cfg(), END + timedelta(hours=2))
    assert r["latest_date"] == DAY.date().isoformat()
