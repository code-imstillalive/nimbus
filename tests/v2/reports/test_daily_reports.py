"""The daily reports published from yesterday's history (#1837).

The Nimbus-only SoC counterfactual, the efficiency backtest, and the daily
quality report: each scores a whole elapsed day once, re-pushes the same
result on every later cycle so its freshness stamp never lapses, and keeps
a history that only ever grows. Synthetic histories only.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest
import solver_writer as sw
from solver_reports.quality import QUALITY_REPORT_SCHEMA

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


# --- yesterday's quality report ---------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        {"solver_solar_power_sensor": None},
        {"solver_battery_power_sensor": None},
        {"solver_whole_house_cross_check_sensor": None},
    ],
    ids=["no_solar", "no_battery", "no_load"],
)
def test_yesterdays_report_needs_its_sensors_and_their_history(ha, missing):
    assert sw.compute_daily_quality_report(cfg(**missing), NOW) is None
    ha["series"]["sensor.real_solar"] = lambda s, e: []
    assert sw.compute_daily_quality_report(cfg(), NOW) is None


def _report(ha, **kw):
    ha["series"].pop("sensor.real_soc", None)
    return sw.compute_daily_quality_report(
        cfg(solver_battery_soc_sensor=None, **kw), NOW
    )


def test_an_idle_battery_on_an_arbitrage_day_leaves_regret_on_the_table(ha):
    r = _report(ha)
    assert r["scored_participants"] == ["home"]
    assert r["epr_pct"] == pytest.approx(r["epr"] * 100, abs=0.01)
    assert (r["tracking_fidelity"], r["tracking_cost"]) == (1.0, 0.0)
    assert r["j_ach"] == pytest.approx(r["j_ref"], abs=1e-6)  # idle is the reference
    assert (
        r["j_star"] <= r["j_ach"] + 1e-9
        and r["regret_dollars"] > 0.0
        and r["epr"] < 1.0
    )
    assert (r["real_p2p_dollars"], r["real_p2p_volume_kwh"]) == (0.0, 0.0)
    assert {"theoretical_maximum_yield", "value_captured", "uplift_available"} <= set(r)
    assert (
        r["soc_discrepancy_max_pct"],
        r["soc_discrepancy_mean_pct"],
        r["soc_discrepancy_reliable"],
    ) == (None, None, None)


def test_each_hour_is_one_self_describing_row_that_balances(ha):
    r = _report(ha)
    fields = [
        "import_price_aud_per_kwh",
        "export_price_aud_per_kwh",
        "load_kw",
        "solar_kw",
        "battery_kw",
        "grid_kw",
        "soc_pct",
    ]
    for key in ("j_ref_hourly", "j_ach_hourly", "j_star_hourly"):
        rows = r[key]
        assert list(rows) == [(DAY + timedelta(hours=h)).isoformat() for h in range(24)]
        for row in rows.values():
            assert list(row) == fields and all(
                isinstance(row[f], float) for f in fields
            )
            assert row["load_kw"] - row["solar_kw"] + row["battery_kw"] - row[
                "grid_kw"
            ] == pytest.approx(0.0, abs=1e-4)


def test_an_elapsed_day_credits_no_leftover_energy_and_splits_the_efficiency(
    ha, monkeypatch
):
    built = []
    real = sw.elements.BatteryConfig
    monkeypatch.setattr(
        sw.elements, "BatteryConfig", lambda **kw: built.append(kw) or real(**kw)
    )
    _report(ha, solver_salvage_value=0.12, solver_efficiency_percent=90.0)
    last = built[-1]
    assert (
        last["salvage_value"] == 0.0 and last.get("terminal_value_breakpoints") is None
    )
    assert (
        last["charge_efficiency"]
        == last["discharge_efficiency"]
        == pytest.approx(0.9**0.5)
    )


def test_an_oracle_that_cannot_solve_is_no_report_not_a_crash(ha, monkeypatch):
    def fail(**_kw):
        raise RuntimeError("Oracle solve failed")

    monkeypatch.setattr(sw, "compute_quality_report", fail)
    assert _report(ha) is None


@pytest.mark.parametrize(
    ("thresholds", "reliable", "reason"),
    [
        ({}, False, "disagreement"),
        (
            {
                "solver_soc_discrepancy_max_threshold_pct": 100.0,
                "solver_soc_discrepancy_mean_threshold_pct": 100.0,
            },
            True,
            None,
        ),
    ],
    ids=["default", "wide"],
)
def test_a_real_soc_sensor_drifting_away_is_measured(ha, thresholds, reliable, reason):
    ha["series"]["sensor.real_soc"] = lambda s, e: [
        (DAY + timedelta(hours=h), 50.0 + 2.0 * h) for h in range(24)
    ]
    r = sw.compute_daily_quality_report(cfg(**thresholds), NOW)
    assert (r["soc_discrepancy_max_pct"], r["soc_discrepancy_mean_pct"]) == (
        pytest.approx(46.0, abs=0.01),
        pytest.approx(23.0, abs=0.01),
    )
    assert (r["soc_discrepancy_reliable"], r["soc_discrepancy_reason"]) == (
        reliable,
        reason,
    )


@pytest.mark.parametrize(
    ("history", "dollars", "volume"),
    [
        ({"2026-08-24": {"export_cost": 12.5, "export_volume": 50.0}}, 12.5, 50.0),
        ({"2026-08-01": {}}, 0.0, 0.0),
    ],
    ids=["yesterday", "other_day_only"],
)
def test_the_p2p_settlement_is_yesterdays_own_or_nothing(ha, history, dollars, volume):
    ha["states"]["sensor.settled"] = {"attributes": {"history": history}}
    r = _report(ha, solver_p2p_settlement_history_sensor="sensor.settled")
    assert (r["real_p2p_dollars"], r["real_p2p_volume_kwh"]) == (dollars, volume)


def test_a_brief_spike_at_the_hour_does_not_set_the_hours_direction(ha):
    """#428: -2.66 kW all hour with one +20.9 kW sample at 04:00 is an
    export hour, as the meter said."""
    spike = DAY + timedelta(hours=4)
    ha["series"]["sensor.real_load"] = flat(1.058)
    ha["series"]["sensor.real_battery"] = lambda s, e: [
        (t, 20.939 if t == spike else v) for t, v in flat(-2.66, step=1)(s, e)
    ]
    r = _report(ha, solver_battery_power_positive_is_charge=True)
    assert r["j_ach_hourly"][spike.isoformat()]["grid_kw"] < 0.0


# --- history resampling -----------------------------------------------------------------------


def test_the_nearest_reading_at_or_before_each_time_or_the_default():
    pts = [(DAY, 1.0), (DAY + timedelta(hours=1), 2.0), (DAY + timedelta(hours=2), 3.0)]
    assert sw.resample_history_nearest(pts, [DAY + timedelta(minutes=90)]) == [2.0]
    assert sw.resample_history_nearest(
        [], [DAY, DAY + timedelta(hours=1)], default=0.42
    ) == [0.42, 0.42]


def test_a_period_mean_is_time_weighted_and_dilutes_a_brief_spike():
    pts = [
        (DAY, 1.0),
        (DAY + timedelta(minutes=5), 2.0),
        (DAY + timedelta(minutes=10), 3.0),
        (DAY + timedelta(minutes=20), 100.0),
    ]
    assert sw.resample_history_mean(pts, [DAY], period_hours=0.25) == [2.0]
    at = DAY + timedelta(hours=4)
    spike = [
        (at, 20.939),
        (at + timedelta(minutes=1), -2.6),
        (at + timedelta(minutes=5), -2.7),
        (at + timedelta(minutes=10), -2.6),
    ]
    assert sw.resample_history_nearest(spike, [at]) == [20.939]
    (mean,) = sw.resample_history_mean(spike, [at], period_hours=0.25)
    assert mean == pytest.approx(
        (20.939 * 60 - 2.6 * 240 - 2.7 * 300 - 2.6 * 300) / 900, abs=1e-6
    )
    assert sw.resample_history_mean(
        [(DAY - timedelta(hours=2), 7.0)], [DAY], period_hours=0.25
    ) == [7.0]
    assert sw.resample_history_mean([], [DAY], period_hours=0.25, default=0.42) == [
        0.42
    ]


# --- publishing yesterday's report ------------------------------------------------------------


def test_a_scored_day_is_re_pushed_and_says_so(ha, monkeypatch, caplog):
    existing = {
        "state": "0.75",
        "attributes": {
            "latest_date": "2026-08-24",
            "report_schema": QUALITY_REPORT_SCHEMA,
        },
    }
    ha["states"][sw.QUALITY_ENTITY_ID] = existing
    monkeypatch.setattr(
        sw, "compute_daily_quality_report", lambda *_a: pytest.fail("recomputed")
    )
    with caplog.at_level(logging.DEBUG, logger=sw._LOGGER.name):
        sw.publish_daily_quality_report(cfg(), NOW)
    assert ha["posts"] == [
        (sw.QUALITY_ENTITY_ID, existing["state"], existing["attributes"])
    ]
    assert "fast-path hit" in caplog.text


def test_a_new_day_is_published_with_its_score_and_nothing_is_published_without_one(
    ha, monkeypatch, caplog
):
    entry = {
        "epr": 0.5,
        "epr_pct": 50.0,
        "theoretical_maximum_yield": 1.0,
        "value_captured": 0.5,
        "uplift_available": 0.5,
        "j_ref": 10.0,
        "j_ach": 8.0,
        "j_star": 6.0,
        "regret_dollars": 2.0,
        "tracking_fidelity": 1.0,
        "tracking_cost": 0.0,
        "real_p2p_dollars": 0.0,
        "real_p2p_volume_kwh": 0.0,
    }
    monkeypatch.setattr(sw, "compute_daily_quality_report", lambda *_a: dict(entry))
    sw.publish_daily_quality_report(cfg(), NOW)
    ((entity, state, attrs),) = ha["posts"]
    assert (entity, state, attrs["latest_date"], attrs["epr"], attrs["epr_pct"]) == (
        sw.QUALITY_ENTITY_ID,
        50.0,
        "2026-08-24",
        0.5,
        50.0,
    )
    ha["posts"].clear()
    monkeypatch.setattr(sw, "compute_daily_quality_report", lambda *_a: None)
    with caplog.at_level(logging.DEBUG, logger=sw._LOGGER.name):
        sw.publish_daily_quality_report(cfg(), NOW)
    assert ha["posts"] == [] and "no report for" in caplog.text


@pytest.fixture
def warned():
    saved = set(sw._QUALITY_REPORT_UNRELIABLE_WARNED)
    sw._QUALITY_REPORT_UNRELIABLE_WARNED.clear()
    yield
    sw._QUALITY_REPORT_UNRELIABLE_WARNED.clear()
    sw._QUALITY_REPORT_UNRELIABLE_WARNED.update(saved)


def _published_warnings(caplog):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=sw._LOGGER.name):
        sw.publish_daily_quality_report(cfg(), END)
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.mark.parametrize("shape", ["out_of_range", "disagreement"])
def test_an_unreliable_day_warns_once_naming_why(ha, warned, caplog, shape):
    if shape == "out_of_range":
        ha["series"]["sensor.real_battery"] = flat(20.0)
    else:
        ha["series"]["sensor.real_battery"] = flat(
            lambda t: 3.0 if t < DAY + timedelta(hours=3) else 0.0
        )
    first = _published_warnings(caplog)
    assert any(
        "soc_discrepancy_reliable=False" in m and f"reason={shape}" in m for m in first
    )
    assert not any(
        "soc_discrepancy_reliable=False" in m for m in _published_warnings(caplog)
    )


def test_a_reliable_day_never_warns(ha, warned, caplog):
    assert not any("soc_discrepancy_reliable" in m for m in _published_warnings(caplog))
