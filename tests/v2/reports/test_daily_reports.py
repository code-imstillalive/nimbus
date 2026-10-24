"""The daily reports published from yesterday's history (#1837).

The Nimbus-only SoC counterfactual, the efficiency backtest, and the daily
quality report: each scores a whole elapsed day once, re-pushes the same
result on every later cycle so its freshness stamp never lapses, and keeps
a history that only ever grows. Synthetic histories only.
"""

from __future__ import annotations

import logging
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import pytest
import solver_writer as sw
from _module_time import ModuleTime
from solver_reports.quality import QUALITY_REPORT_SCHEMA

quality = sys.modules[sw.publish_daily_quality_report.__module__]
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


# --- rescoring days already in the table (#1120) ----------------------------------------------


RESCORE_NOW = datetime(2026, 9, 20, 6, 30, tzinfo=TZ)


def _entry(epr):
    return {
        "epr": epr,
        "epr_pct": round(epr * 100.0, 2),
        "j_ref": 6.0,
        "j_ach": -24.0,
        "j_star": -25.0,
        "regret_dollars": 1.0,
    }


@pytest.fixture
def rescore(ha, monkeypatch):
    seen = []
    scores = {"fn": lambda day: _entry(0.9)}

    def window(_cfg, start, end, allow_partial):
        seen.append((start, end, allow_partial))
        return scores["fn"](start.date().isoformat())

    monkeypatch.setattr(sw, "_compute_report_for_window", window)
    monkeypatch.setattr(sw, "_nimbus_version", lambda: "0.94.396")

    def run(days, existing=None, **kw):
        ha["states"][sw.QUALITY_ENTITY_ID] = (
            existing
            if existing is not None
            else {"state": "94.81", "attributes": {"latest_date": None, "history": {}}}
        )
        ha["posts"].clear()
        result = sw.rescore_quality_history({}, RESCORE_NOW, days)
        return result, (ha["posts"][-1] if ha["posts"] else None)

    run.seen, run.scores = seen, scores
    return run


@pytest.mark.parametrize("bad", [0, -1, sw._RESCORE_MAX_DAYS + 1])
def test_the_day_count_is_bounded(ha, bad):
    with pytest.raises(ValueError):
        sw.rescore_quality_history({}, RESCORE_NOW, bad)


def test_the_cap_itself_is_allowed(rescore):
    rescore.scores["fn"] = lambda day: None
    result, _ = rescore(sw._RESCORE_MAX_DAYS)
    assert result["days_requested"] == sw._RESCORE_MAX_DAYS


def test_a_stale_row_is_rewritten_stamped_and_others_left_exactly(rescore):
    rescore.scores["fn"] = lambda day: _entry(0.9481)
    untouched = {"epr": 0.5, "v": "0.90.0"}
    existing = {
        "state": "94.81",
        "attributes": {
            "history": {
                "2026-08-01": dict(untouched),
                "2026-09-19": {"epr": 1.064, "j_star": -6.84, "v": "0.94.375"},
            }
        },
    }
    result, (_e, _s, attrs) = rescore(1, existing)
    row = attrs["history"]["2026-09-19"]
    assert (row["epr"], row["j_star"], row["v"]) == (
        pytest.approx(0.9481),
        pytest.approx(-25.0),
        "0.94.396",
    )
    assert attrs["history"]["2026-08-01"] == untouched
    assert result["rescored_count"] == 1
    assert (
        result["rescored"][0]["before"]["epr"],
        result["rescored"][0]["after"]["epr"],
    ) == (1.064, pytest.approx(0.9481))


@pytest.mark.parametrize(
    "existing",
    [{"state": "unknown", "attributes": {"latest_date": None}}, {"state": None}],
    ids=["no_history_key", "no_attributes"],
)
def test_a_table_that_never_existed_is_built(rescore, existing):
    result, (_e, _s, attrs) = rescore(2, existing)
    assert result["rescored_count"] == 2 and sorted(attrs["history"]) == [
        "2026-09-18",
        "2026-09-19",
    ]


def test_one_bad_day_is_skipped_with_its_reason_and_the_rest_still_publish(rescore):
    def score(day):
        if day == "2026-09-18":
            raise RuntimeError("oracle infeasible")
        return None if day == "2026-09-17" else _entry(0.9)

    rescore.scores["fn"] = score
    result, (_e, _s, attrs) = rescore(3)
    assert (result["rescored_count"], result["skipped_count"]) == (1, 2)
    reasons = {s["date"]: s["reason"] for s in result["skipped"]}
    assert (
        "RuntimeError" in reasons["2026-09-18"]
        and "oracle infeasible" in reasons["2026-09-18"]
    )
    assert "no usable real history" in reasons["2026-09-17"]
    assert "2026-09-19" in attrs["history"]
    rescore.scores["fn"] = lambda day: None
    result, post = rescore(3)
    assert result["rescored_count"] == 0 and not result["published"] and post is None


def test_rescoring_the_published_day_moves_the_headline_and_an_older_day_does_not(
    rescore,
):
    rescore.scores["fn"] = lambda day: _entry(0.9481)
    _r, (_e, state, attrs) = rescore(
        1,
        {
            "state": "106.43",
            "attributes": {
                "latest_date": "2026-09-19",
                "history": {"2026-09-19": {"epr": 1.064}},
            },
        },
    )
    assert state == 94.81 and attrs["epr"] == pytest.approx(0.9481)
    assert (attrs["j_star"], attrs["regret_dollars"]) == (
        pytest.approx(-25.0),
        pytest.approx(1.0),
    )
    rescore.scores["fn"] = lambda day: _entry(0.5) if day == "2026-09-17" else None
    old = {
        "state": "106.43",
        "attributes": {
            "latest_date": "2026-09-19",
            "epr": 1.064,
            "history": {"2026-09-17": {"epr": 0.2}},
        },
    }
    _r, (_e, state, attrs) = rescore(3, old)
    assert (state, attrs["epr"], attrs["history"]["2026-09-17"]["epr"]) == (
        "106.43",
        1.064,
        0.5,
    )


def test_each_day_is_rescored_whole_from_yesterday_back(rescore):
    rescore(3)
    assert [s.date().isoformat() for s, _e, _p in rescore.seen] == [
        "2026-09-19",
        "2026-09-18",
        "2026-09-17",
    ]
    for start, end, partial in rescore.seen:
        assert start.tzinfo == TZ and (start.hour, start.minute) == (0, 0)
        assert end - start == timedelta(days=1) and partial is False


# --- the history never shrinks on a degraded read (#1248) -------------------------------------


def _row(epr):
    return {
        "epr": epr,
        "j_ref": 1.0,
        "j_ach": -1.0,
        "j_star": -2.0,
        "regret_dollars": 1.0,
    }


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(sw, "_LAST_KNOWN_QUALITY_HISTORY", {})
    monkeypatch.setattr(sw.NATIVE, "hass", object())
    yield
    sw._LAST_KNOWN_QUALITY_HISTORY = {}


def _carry(prior, day, epr, read="ok"):
    return sw._carry_forward_quality_history(
        prior_attrs={} if prior is None else {"history": prior},
        day_key=day,
        day_entry=_row(epr),
        prior_read=read,
    )


TEN = {f"2026-09-{d:02d}": _row(90.0 + d) for d in range(16, 26)}


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_degraded_read_keeps_every_known_row_and_today_still_lands(native, caplog):
    assert len(_carry(TEN, "2026-09-25", 68.9)) == 10
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=sw._LOGGER.name):
        out = _carry(None, "2026-09-26", 55.0, read="unavailable")
    assert len(out) == 11 and set(TEN) <= set(out)
    (line,) = _warnings(caplog)
    assert all(
        s in line
        for s in (
            "#1248",
            "unavailable",
            "#1217",
            "2026-09-16",
            "2026-09-25",
            "10 earlier row(s)",
        )
    )


def test_ten_degraded_cycles_cannot_erode_it_and_todays_fresh_row_wins(native):
    _carry(TEN, "2026-09-25", 21.11)
    for _ in range(10):
        out = _carry(None, "2026-09-25", 68.9, read="unavailable")
    assert (
        len(out) == 10
        and out["2026-09-25"]["epr"] == 68.9
        and out["2026-09-16"]["epr"] == 106.0
    )


def test_the_cache_lives_only_in_one_native_process(native, monkeypatch):
    _carry({"2026-09-24": _row(87.3)}, "2026-09-25", 68.9)
    assert len(sw._LAST_KNOWN_QUALITY_HISTORY) == 2
    sw.set_native_hass(
        object()
    )  # a reload: another instance's rows must not carry over
    assert sw._LAST_KNOWN_QUALITY_HISTORY == {}
    sw._LAST_KNOWN_QUALITY_HISTORY = dict(TEN)
    monkeypatch.setattr(
        sw.NATIVE, "hass", None
    )  # the cron path: nothing to recover from
    assert len(_carry(None, "2026-09-26", 55.0, read="unavailable")) == 1
    sw._LAST_KNOWN_QUALITY_HISTORY = {}
    _carry({"2026-09-24": _row(87.3)}, "2026-09-25", 68.9)
    assert sw._LAST_KNOWN_QUALITY_HISTORY == {}  # and nothing written to it either


@pytest.mark.parametrize(
    ("read", "cached", "rows", "warning"),
    [
        ("absent", TEN, 1, None),  # a 404: a real first publish, whatever is cached
        ("unavailable", {}, 1, "rescore_history"),  # it existed and its rows are gone
        ("unreachable", {}, 1, None),  # says nothing about whether rows existed
        ("unavailable", {"2026-09-26": _row(55.0)}, 1, None),  # nothing to rescue
        (
            "unreachable",
            {"2026-09-24": _row(87.3), "2026-09-25": _row(68.9)},
            3,
            "recovering the 2 earlier row(s)",
        ),
        ("ok", {}, 1, None),
    ],
    ids=[
        "first_publish",
        "unavailable_uncached",
        "unreachable_uncached",
        "nothing_to_rescue",
        "unreachable_recovers",
        "fresh_install",
    ],
)
def test_how_an_empty_read_is_classified(native, caplog, read, cached, rows, warning):
    if cached:
        _carry(dict(cached), max(cached), 1.0)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=sw._LOGGER.name):
        out = _carry(None if read != "ok" else {}, "2026-09-26", 55.0, read=read)
    assert len(out) == rows
    found = _warnings(caplog)
    if warning:
        assert any(warning in m for m in found)
        if warning == "rescore_history":
            assert any("not retrievable" in m for m in found)
    else:
        assert found == []


def test_the_ordinary_paths_grow_rewrite_and_skip_a_malformed_row_quietly(
    native, caplog
):
    two = {"2026-09-24": _row(87.3), "2026-09-25": _row(21.11)}
    assert len(_carry(two, "2026-09-26", 55.0)) == 3
    rewritten = _carry(two, "2026-09-25", 68.9)
    assert len(rewritten) == 2 and rewritten["2026-09-25"]["epr"] == 68.9
    with caplog.at_level(logging.WARNING, logger=sw._LOGGER.name):
        out = _carry(
            {"2026-09-24": _row(87.3), "bad": "not a dict", 42: {"epr": 1}},
            "2026-09-25",
            68.9,
        )
    assert (
        {"2026-09-24", "2026-09-25"} <= set(out)
        and "bad" not in out
        and _warnings(caplog) == []
    )


def test_the_cap_holds_after_recovery_and_the_cache_is_a_trimmed_copy(native):
    cap = sw._QUALITY_HISTORY_MAX_DAYS
    full = {
        (date(2026, 1, 1) + timedelta(days=i)).isoformat(): _row(float(i))
        for i in range(cap)
    }
    out = _carry(full, "2026-12-31", 99.0)
    assert len(out) == cap and "2026-12-31" in out and "2026-01-01" not in out
    assert (
        len(sw._LAST_KNOWN_QUALITY_HISTORY) == cap
        and "2026-01-01" not in sw._LAST_KNOWN_QUALITY_HISTORY
    )
    assert len(_carry(None, "2027-01-01", 88.0, read="unavailable")) == cap
    out.clear()
    assert len(sw._LAST_KNOWN_QUALITY_HISTORY) == cap


# --- the last good score is held over an unscoreable new day (#1463) --------------------------


@pytest.fixture
def held(ha, monkeypatch):
    quality._QUALITY_HOLD_WARNED.clear()
    monkeypatch.setattr(sw, "compute_daily_quality_report", lambda *_a: None)
    yield
    quality._QUALITY_HOLD_WARNED.clear()


def _hold(ha, caplog, existing):
    ha["states"][sw.QUALITY_ENTITY_ID] = existing
    ha["posts"].clear()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=sw._LOGGER.name):
        sw.publish_daily_quality_report({}, datetime(2026, 9, 30, 0, 5, tzinfo=TZ))
    return ha["posts"], _warnings(caplog)


def test_after_midnight_the_previous_report_stays_alive_naming_its_own_day(
    ha, held, caplog
):
    prev = {
        "state": 76.78,
        "attributes": {"latest_date": "2026-09-28", "epr": 0.7678, "history": {}},
    }
    posts, warns = _hold(ha, caplog, prev)
    ((entity, state, attrs),) = posts
    assert (entity, state, attrs["latest_date"]) == (
        sw.QUALITY_ENTITY_ID,
        76.78,
        "2026-09-28",
    )
    assert len(warns) == 1 and "#1463" in warns[0]
    posts, warns = _hold(ha, caplog, prev)
    assert len(posts) == 1 and warns == []  # held every cycle, warned once a day


@pytest.mark.parametrize(
    "existing",
    [
        {"state": "unavailable", "attributes": {"latest_date": "2026-09-28"}},
        {"state": 50.0, "attributes": {}},
    ],
    ids=["already_unavailable", "undated"],
)
def test_nothing_is_held_without_a_genuine_dated_score(ha, held, caplog, existing):
    posts, _w = _hold(ha, caplog, existing)
    assert posts == []


# --- a day scored by an older release is rescored once (#1496) --------------------------------


@pytest.fixture
def schema(ha, monkeypatch):
    quality._SCHEMA_RESCORE_TRIED.clear()
    computed = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        sw, "compute_daily_quality_report", lambda *_a: computed.append(1)
    )
    monkeypatch.setattr(quality, "time", ModuleTime(lambda: clock["t"]))

    def run(**attrs):
        base = {
            "latest_date": "2026-09-30",
            "real_p2p_settlement_status": "no_sensor_configured",
            "generated_at": "x",
            **attrs,
        }
        ha["states"][sw.QUALITY_ENTITY_ID] = {"state": 76.78, "attributes": base}
        computed.clear()
        ha["posts"].clear()
        sw.publish_daily_quality_report({}, datetime(2026, 10, 1, 10, 0, tzinfo=TZ))
        return len(computed), len(ha["posts"])

    run.clock = clock
    yield run
    quality._SCHEMA_RESCORE_TRIED.clear()


@pytest.mark.parametrize(
    ("stamp", "rescored"),
    [(None, 1), ("garbage", 1), ("current", 0)],
    ids=["pre_stamp", "unreadable", "current"],
)
def test_an_older_or_unreadable_schema_is_rescored_a_current_one_is_not(
    schema, stamp, rescored
):
    attrs = (
        {}
        if stamp is None
        else {
            "report_schema": quality.QUALITY_REPORT_SCHEMA
            if stamp == "current"
            else stamp
        }
    )
    computed, posts = schema(**attrs)
    assert computed == rescored
    if not rescored:
        assert posts == 1


def test_a_failed_rescore_waits_out_its_backoff_before_trying_again(schema):
    schema()
    schema.clock["t"] += 17
    assert schema() == (0, 1)  # held, not re-solved every cycle
    schema.clock["t"] += quality._SCHEMA_RESCORE_RETRY_SECONDS
    assert schema()[0] == 1


# --- a provisional score is rescored once its settlement can land (#1082) ---------------------


PROV_NOW = datetime(2026, 9, 17, 21, 30, tzinfo=TZ)


def _prov(status, minutes_ago=180, **over):
    return {
        "latest_date": "2026-09-16",
        "real_p2p_settlement_status": status,
        "generated_at": (PROV_NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "report_schema": QUALITY_REPORT_SCHEMA,
        **over,
    }


@pytest.mark.parametrize(
    ("attrs", "keep"),
    [
        (_prov("applied"), True),
        (_prov("no_sensor_configured"), True),  # permanent: never re-solve forever
        (_prov("window_is_not_one_local_calendar_day"), True),
        (_prov("no_settlement_entry_for_this_date"), False),
        (_prov("settlement_sensor_unreadable"), False),
        (_prov("no_settlement_entry_for_this_date", minutes_ago=10), True),  # spaced
        (
            {
                k: v
                for k, v in _prov("no_settlement_entry_for_this_date").items()
                if k != "generated_at"
            },
            True,
        ),
        (_prov("no_settlement_entry_for_this_date", generated_at="not a date"), True),
        (
            _prov(
                "no_settlement_entry_for_this_date", generated_at="2026-09-17T00:00:00"
            ),
            False,
        ),  # naive, local
    ],
    ids=[
        "applied",
        "no_sensor",
        "not_a_calendar_day",
        "unsettled",
        "unreadable",
        "recent",
        "no_stamp",
        "bad_stamp",
        "naive_stamp",
    ],
)
def test_only_a_provisional_score_old_enough_is_rescored(attrs, keep):
    assert sw._keep_published_quality_score(attrs, PROV_NOW) is keep


def test_the_rescore_interval_is_the_boundary():
    interval = int(sw._PROVISIONAL_RESCORE_INTERVAL.total_seconds() // 60)
    status = "no_settlement_entry_for_this_date"
    assert sw._keep_published_quality_score(
        _prov(status, minutes_ago=interval - 1), PROV_NOW
    )
    assert not sw._keep_published_quality_score(
        _prov(status, minutes_ago=interval + 1), PROV_NOW
    )


@pytest.mark.parametrize(
    ("status", "minutes_ago", "recomputed"),
    [
        ("applied", 180, False),
        ("no_settlement_entry_for_this_date", 180, True),
        ("no_settlement_entry_for_this_date", 5, False),
    ],
    ids=["settled", "unsettled", "unsettled_recently"],
)
def test_the_publisher_falls_through_to_a_rescore_only_when_due(
    ha, monkeypatch, status, minutes_ago, recomputed
):
    calls = []
    ha["states"][sw.QUALITY_ENTITY_ID] = {
        "state": "90.6",
        "attributes": _prov(status, minutes_ago=minutes_ago),
    }
    monkeypatch.setattr(sw, "resolve_real_entity_id", lambda e: e)
    monkeypatch.setattr(sw, "compute_daily_quality_report", lambda *_a: calls.append(1))
    sw.publish_daily_quality_report({}, PROV_NOW)
    assert bool(calls) is recomputed
    if not recomputed:
        assert len(ha["posts"]) == 1


# --- the report always publishes its unit and state class -------------------------------------


def test_a_published_report_names_percent_and_measurement(ha, monkeypatch):
    """A raw state write before the entity is set up carries these keys
    verbatim; a null unit raised a recurring Repair."""
    entry = {
        "epr": 0.5,
        "epr_pct": 50.0,
        "j_ref": 10.0,
        "j_ach": 8.0,
        "j_star": 6.0,
        "regret_dollars": 2.0,
    }
    monkeypatch.setattr(sw, "compute_daily_quality_report", lambda *_a: dict(entry))
    sw.publish_daily_quality_report(cfg(), NOW)
    ((_entity, _state, attrs),) = ha["posts"]
    assert (attrs["unit_of_measurement"], attrs["state_class"]) == ("%", "measurement")


# --- the day-ahead verdict is kept in the history row (#937) ----------------------------------


def test_the_row_field_is_one_character_and_its_own():
    field = sw._QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD
    taken = {
        sw._QUALITY_HISTORY_VERSION_FIELD,
        sw._QUALITY_HISTORY_RELIABILITY_FIELD,
        sw._QUALITY_HISTORY_PROVISIONAL_FIELD,
        *sw._QUALITY_HISTORY_FIELDS,
    }
    assert len(field) == 1 and field not in taken


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {
                "forecast_regret_reason": None,
                "forecast_regret_nimbus_value_add_dollars": -0.71234567,
            },
            -0.7123,
        ),
        *[
            (
                {
                    "forecast_regret_reason": reason,
                    "forecast_regret_nimbus_value_add_dollars": -1.5,
                },
                None,
            )
            for reason in (
                "native_only",
                "no_forecast_snapshot_for_this_day",
                "snapshot_unusable_for_this_grid",
                "previous_day_history_unavailable",
                "no_persistence_baseline",
                "decomposition_solve_failed",
            )
        ],
        *[
            (
                {
                    "forecast_regret_reason": None,
                    "forecast_regret_nimbus_value_add_dollars": bad,
                },
                None,
            )
            for bad in (None, "sideways", float("nan"), float("inf"), True)
        ],
        ({"forecast_regret_reason": None}, None),
        ({"epr": 0.9}, None),  # an older release's entry
    ],
)
def test_only_a_real_verdict_is_recorded_never_a_made_up_zero(entry, expected):
    got = sw._day_ahead_value_add_for_history(entry)
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_the_trailing_record_is_read_from_this_installs_own_report(ha, monkeypatch):
    seen = []
    monkeypatch.setattr(
        sw, "resolve_real_entity_id", lambda e: seen.append(e) or "sensor.own_report"
    )
    ha["states"]["sensor.own_report"] = {
        "attributes": {
            "history": {
                "2026-09-11": {"epr": 0.9, "f": -0.5224},
                "2026-09-12": {"epr": 0.9, "f": 0.32},
                "2026-09-13": {"epr": 0.9},
                "2026-09-14": "not a row",
                17: {"f": -9.9},
                "2026-09-15": {"f": "sideways"},
                "2026-09-16": {"f": True},
            }
        }
    }
    assert sw.read_day_ahead_value_add_history() == {
        "2026-09-11": -0.5224,
        "2026-09-12": 0.32,
    }
    assert seen == [sw.QUALITY_ENTITY_ID]


@pytest.mark.parametrize(
    "attrs", [{}, {"history": None}, {"history": []}, {"history": "x"}]
)
def test_a_missing_history_is_no_record(ha, monkeypatch, attrs):
    monkeypatch.setattr(sw, "resolve_real_entity_id", lambda e: e)
    ha["states"][sw.QUALITY_ENTITY_ID] = {"attributes": attrs}
    assert sw.read_day_ahead_value_add_history() == {}


@pytest.mark.parametrize(
    "exc", [urllib.error.URLError("down"), TimeoutError(), OSError("refused")]
)
def test_an_unreachable_home_assistant_is_no_record(monkeypatch, exc):
    def boom(_e):
        raise exc

    monkeypatch.setattr(sw, "resolve_real_entity_id", lambda e: e)
    monkeypatch.setattr(sw, "ha_get", boom)
    assert sw.read_day_ahead_value_add_history() == {}
