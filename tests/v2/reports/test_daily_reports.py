"""The daily reports published from yesterday's history (#1837).

The Nimbus-only SoC counterfactual, the efficiency backtest, and the daily
quality report: each scores a whole elapsed day once, re-pushes the same
result on every later cycle so its freshness stamp never lapses, and keeps
a history that only ever grows. Synthetic histories only.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest
import solver_writer as sw

TZ = sw.LOCAL_TZ
NOW = datetime(2026, 8, 25, 10, 0, tzinfo=TZ)
DAY = datetime(2026, 8, 24, tzinfo=TZ)
END = DAY + timedelta(days=1)


def flat(value, step=15):
    def rows(start, end):
        out, t = [], start
        while t < end:
            out.append((t, value(t) if callable(value) else value))
            t += timedelta(minutes=step)
        return out

    return rows


def prices(cheap=0.05, expensive=0.35, hour=17):
    return flat(lambda t: expensive if t.hour >= hour else cheap)


def cfg(**kw):
    return {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_battery_soc_sensor": "sensor.real_soc",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_capacity_kwh": 50.0,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 10.0,
        "solver_max_discharge_kw": 10.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.1,
        "solver_grid_max_import_kw": 20.0,
        "solver_grid_max_export_kw": 20.0,
        **kw,
    }


@pytest.fixture
def ha(monkeypatch):
    """History from `series`, the published states in `states`, every post
    recorded; anything else Home Assistant would answer is refused at once."""
    site = {
        "series": {
            "sensor.real_solar": flat(0.0),
            "sensor.real_load": flat(2.0),
            "sensor.real_battery": flat(0.0),
            "sensor.real_soc": flat(50.0),
            "sensor.import_price": prices(),
            "sensor.export_price": prices(0.02, 0.10),
        },
        "states": {},
        "posts": [],
        "fetched": [],
    }

    def fetch(eid, start, end):
        site["fetched"].append(eid)
        rows = site["series"].get(eid)
        return rows(start, end) if rows else []

    def get(eid):
        if eid in site["states"]:
            return site["states"][eid]
        raise urllib.error.URLError("not found")

    def offline(*_a, **_k):
        raise urllib.error.URLError("no Home Assistant in this test")

    monkeypatch.setattr(sw, "fetch_entity_history_range", fetch)
    monkeypatch.setattr(sw, "ha_get", get)
    monkeypatch.setattr(
        sw,
        "ha_post_state",
        lambda eid, state, attrs: site["posts"].append((eid, state, attrs)),
    )
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    return site


# --- the Nimbus-only SoC counterfactual -------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        {"solver_solar_power_sensor": None},
        {"solver_whole_house_cross_check_sensor": None},
        {"solver_battery_soc_sensor": None},
        {"solver_battery_capacity_kwh": 0.0},
    ],
    ids=["no_solar", "no_load", "no_soc", "no_capacity"],
)
def test_the_counterfactual_needs_its_inputs(ha, missing):
    assert sw.compute_nimbus_only_soc_counterfactual(cfg(**missing), DAY) is None
    if "solver_solar_power_sensor" in missing:
        assert ha["fetched"] == []


def test_the_counterfactual_needs_history_for_every_signal(ha):
    ha["series"]["sensor.real_solar"] = lambda s, e: []
    assert sw.compute_nimbus_only_soc_counterfactual(cfg(), DAY) is None


def test_without_p2p_every_p2p_field_is_an_honest_blank(ha):
    ha["series"]["sensor.import_price"] = flat(0.20)
    ha["series"]["sensor.export_price"] = flat(0.05)
    result = sw.compute_nimbus_only_soc_counterfactual(cfg(), DAY)
    assert (result["date"], result["real_soc_anchor_pct"]) == (
        "2026-08-24",
        pytest.approx(50.0, abs=0.5),
    )
    for blank in (
        "checkpoint_hour",
        "nimbus_only_soc_checkpoint_pct",
        "real_soc_checkpoint_pct",
        "viable_threshold_pct",
    ):
        assert result[blank] is None
    assert result["viable"] is False and result["p2p_configured"] is False
    assert 5.0 <= result["nimbus_only_soc_close_pct"] <= 100.0


def test_a_p2p_block_sets_the_checkpoint_from_its_own_hours(ha):
    ha["series"]["sensor.real_load"] = flat(1.0)
    ha["series"]["sensor.real_soc"] = flat(80.0)
    block = cfg(
        solver_p2p_block_1_rate_kw=5.0,
        solver_p2p_block_1_start_hour=18,
        solver_p2p_block_1_end_hour=22,
        solver_p2p_bonus_price=0.3,
        solver_p2p_bonus_volume_kwh=10.0,
    )
    result = sw.compute_nimbus_only_soc_counterfactual(block, DAY)
    assert result["p2p_configured"] and result["checkpoint_hour"] == 18
    assert result["nimbus_only_soc_checkpoint_pct"] is not None
    assert result["real_soc_checkpoint_pct"] is not None
    assert result["viable_threshold_pct"] == pytest.approx(22.0, abs=0.1)


def test_the_counterfactual_splits_the_round_trip_efficiency(ha, monkeypatch):
    built = []
    real = sw.elements.BatteryConfig
    monkeypatch.setattr(
        sw.elements, "BatteryConfig", lambda **kw: built.append(kw) or real(**kw)
    )
    sw.compute_nimbus_only_soc_counterfactual(cfg(solver_efficiency_percent=90.0), DAY)
    assert built and all(
        kw["charge_efficiency"] == kw["discharge_efficiency"] == pytest.approx(0.9**0.5)
        for kw in built
    )


# --- the efficiency backtest ------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        {"solver_solar_power_sensor": None},
        {"solver_battery_power_sensor": None},
        {"solver_whole_house_cross_check_sensor": None},
    ],
    ids=["no_solar", "no_battery", "no_load"],
)
def test_the_backtest_needs_its_sensors(ha, missing):
    assert sw.compute_efficiency_backtest_report(cfg(**missing), NOW) is None
    ha["series"]["sensor.real_solar"] = lambda s, e: []
    assert sw.compute_efficiency_backtest_report(cfg(), NOW) is None


def test_the_backtest_ranks_four_candidates_on_a_day_with_arbitrage(ha, monkeypatch):
    ha["series"]["sensor.real_solar"] = flat(lambda t: 6.0 if 9 <= t.hour < 15 else 0.0)
    ha["series"]["sensor.real_load"] = flat(1.0)
    grids = []
    real = sw.elements.PeriodGrid
    monkeypatch.setattr(
        sw.elements,
        "PeriodGrid",
        lambda *a, **kw: grids.append(kw["hours"]) or real(*a, **kw),
    )
    report = sw.compute_efficiency_backtest_report(cfg(), NOW)
    assert (
        len(report["candidates"]) == 4
        and report["configured_efficiency_percent"] == 95.0
    )
    assert report["best_candidate_cost"] <= report["worst_candidate_cost"]
    assert report["spread_dollars"] > 0.0
    assert {"best_candidate", "worst_candidate"} <= set(report)
    ((hours),) = grids
    assert (len(hours), float(hours[0])) == (48, pytest.approx(0.5))


# --- publishing once, re-pushing after (#289, #292) -------------------------------------------


@pytest.mark.parametrize(
    ("publish", "entity", "compute"),
    [
        (
            sw.publish_efficiency_backtest_report,
            sw.BACKTEST_ENTITY_ID,
            "compute_efficiency_backtest_report",
        ),
        (
            sw.publish_nimbus_only_soc_counterfactual,
            sw.COUNTERFACTUAL_ENTITY_ID,
            "compute_nimbus_only_soc_counterfactual",
        ),
    ],
    ids=["backtest", "counterfactual"],
)
def test_a_day_already_scored_is_re_pushed_as_it_is_not_recomputed(
    ha, monkeypatch, publish, entity, compute
):
    existing = {"state": "1.23", "attributes": {"latest_date": "2026-08-24"}}
    ha["states"][entity] = existing
    monkeypatch.setattr(sw, compute, lambda *_a: pytest.fail("recomputed"))
    publish(cfg(), NOW)
    assert ha["posts"] == [(entity, existing["state"], existing["attributes"])]


def test_a_new_day_is_scored_and_published_with_its_date(ha):
    ha["series"]["sensor.real_solar"] = flat(3.0)
    ha["series"]["sensor.real_load"] = flat(1.0)
    sw.publish_efficiency_backtest_report(cfg(), NOW)
    ((entity, _state, attrs),) = ha["posts"]
    assert entity == sw.BACKTEST_ENTITY_ID and "candidates" in attrs
    assert attrs["latest_date"] == "2026-08-24"


def test_nothing_to_publish_publishes_nothing(ha):
    sw.publish_nimbus_only_soc_counterfactual(cfg(solver_solar_power_sensor=None), NOW)
    assert ha["posts"] == []
