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


# --- yesterday's quality report ---------------------------------------------------------------


def _report(ha, **kw):
    ha["series"].pop("sensor.real_soc", None)
    return sw.compute_daily_quality_report(
        cfg(solver_battery_soc_sensor=None, **kw), NOW
    )


def test_an_oracle_that_cannot_solve_is_no_report_not_a_crash(ha, monkeypatch):
    def fail(**_kw):
        raise RuntimeError("Oracle solve failed")

    monkeypatch.setattr(sw, "compute_quality_report", fail)
    assert _report(ha) is None


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


# --- history resampling -----------------------------------------------------------------------


# --- publishing yesterday's report ------------------------------------------------------------


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


# --- the day-ahead verdict is kept in the history row (#937) ----------------------------------


def test_the_row_field_is_one_character_and_its_own():
    # test_the_row_field_is_one_character_and_its_own
    field = sw._QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD
    taken = {
        sw._QUALITY_HISTORY_VERSION_FIELD,
        sw._QUALITY_HISTORY_RELIABILITY_FIELD,
        sw._QUALITY_HISTORY_PROVISIONAL_FIELD,
        *sw._QUALITY_HISTORY_FIELDS,
    }
    assert len(field) == 1 and field not in taken

    # test_only_a_real_verdict_is_recorded_never_a_made_up_zero
    for entry, expected in [
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
    ]:
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


# --- the row being written says which release scored it (#1120) ------------------------------


@pytest.fixture
def version():
    real = sw._nimbus_version
    real.cache_clear()
    yield
    real.cache_clear()


def test_no_version_costs_the_row_nothing(version, monkeypatch):
    monkeypatch.setattr(sw, "_nimbus_version", lambda: None)
    out = sw._carry_forward_quality_history({}, "2026-09-18", _row(90.0))
    assert "v" not in out["2026-09-18"] and out["2026-09-18"]["epr"] == 90.0


@pytest.mark.parametrize(
    "manifest",
    [OSError("boom"), "{not json", '{"domain": "nimbus_load"}', '{"version": 94}'],
    ids=["unreadable", "malformed", "no_version", "not_a_string"],
)
def test_a_manifest_that_cannot_say_reads_as_no_version(version, monkeypatch, manifest):
    import builtins
    import io

    def fake_open(*_a, **_k):
        if isinstance(manifest, Exception):
            raise manifest
        return io.StringIO(manifest)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert sw._nimbus_version() is None


# --- a row scored before its settlement says it is waiting (#1200) ----------------------------


def _scored(status):
    entry = _row(90.0)
    if status is not None:
        entry["real_p2p_settlement_status"] = status
    return entry


SETTLED = {"solver_p2p_settlement_history_sensor": "sensor.settlement"}


@pytest.mark.parametrize(
    ("only", "scored"),
    [
        ({"2026-09-21"}, ["2026-09-21"]),
        (None, ["2026-09-24", "2026-09-23", "2026-09-22"]),
    ],
    ids=["named_day_only", "every_day"],
)
def test_a_named_rescore_solves_only_the_day_named(monkeypatch, only, scored):
    seen = []

    def window(_cfg, start, _end, allow_partial):
        seen.append(start.date().isoformat())
        return _scored("applied")

    monkeypatch.setattr(sw, "ha_get", lambda _e: {"attributes": {}, "state": "90"})
    monkeypatch.setattr(sw, "_compute_report_for_window", window)
    monkeypatch.setattr(sw, "ha_post_state", lambda *_a, **_k: None)
    now = datetime(2026, 9, 25, 10, 0, tzinfo=TZ)
    if only is None:
        sw.rescore_quality_history({}, now, 3)
    else:
        sw.rescore_quality_history({}, now, 10, only_dates=only)
    assert seen == scored


# --- a day whose EPR denominator is unusable warns once (#1089) ------------------------------


def test_an_unusable_denominator_is_warned_about_once_for_the_day(
    ha, monkeypatch, caplog
):
    monkeypatch.setattr(sw, "_QUALITY_REPORT_EPR_DENOMINATOR_WARNED", set())
    real = sw._compute_report_for_window

    def flagged(*a, **k):
        entry = real(*a, **k)
        return {**entry, "epr_denominator_reason": "oracle_not_better_than_idle"}

    monkeypatch.setattr(sw, "_compute_report_for_window", flagged)
    first = [m for m in _published_warnings(caplog) if "DENOMINATOR" in m]
    assert len(first) == 1 and "oracle_not_better_than_idle" in first[0]
    assert DAY.date().isoformat() in first[0]
    ha["states"].pop(sw.QUALITY_ENTITY_ID, None)
    assert not [m for m in _published_warnings(caplog) if "DENOMINATOR" in m]


def test_a_re_scored_day_without_a_snapshot_keeps_its_earlier_verdict_and_only_that():
    """#937: a day scored once with its snapshot and re-pushed by a cycle
    that could not recompute it keeps its day-ahead verdict, and the row
    never carries the headline-only decomposition."""
    field = sw._QUALITY_HISTORY_FORECAST_VALUE_ADD_FIELD
    prior = {"history": {"2026-09-12": {**_row(90.0), field: -0.5224}}}
    entry = {
        **_row(91.0),
        "forecast_regret_reason": "no_forecast_snapshot_for_this_day",
        "forecast_regret_load_level_error_dollars": 1.0,
        "forecast_regret_load_shape_error_dollars": 1.0,
        "forecast_regret_solar_error_dollars": 1.0,
    }
    row = sw._carry_forward_quality_history(prior, "2026-09-12", entry)["2026-09-12"]
    assert row[field] == -0.5224
    assert not {k for k in row if k.startswith("forecast_regret_")}
