"""solver_writer's input readers, each against the golden master's fake Home
Assistant REST API (#1837).

Price forecasts (resampling, real-coverage masks, unit scaling, the
secondary-source blend and the wholesale tail), P2P matched-rate sources,
the live grid meter, envelope limits, power history, calendar trips and
power-history coverage. Every read goes through the module's own REST code;
only the far end is fake. Covers what lived in #216, #220, #239, #631, #1537,
#1550, #1570, #1643, #1694 and the P2P and envelope-limit files.
"""

from __future__ import annotations

import importlib
import logging
import types
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest
import solver_shared
import solver_writer as sw
from golden.fake_ha import BASE, FakeHA, HistoryPoint

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)  # 16:00 Brisbane


def _grid(n, minutes=30, start=T0):
    return [start + timedelta(minutes=minutes * i) for i in range(n)]


def _iso(t):
    return t.isoformat()


@pytest.fixture
def ha(monkeypatch):
    """A fake HA the module reaches over its own REST code."""
    fake = FakeHA(states={})
    monkeypatch.setattr(sw, "HA_BASE", BASE)
    monkeypatch.setattr(sw, "_load_token", lambda: "test")
    monkeypatch.setattr(sw.NATIVE, "hass", None)
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


# --- instantaneous readings on the plan grid -------------------------------


def test_instant_readings_take_each_periods_midpoint_and_hold_at_the_ends():
    grid = _grid(4)
    readings = [
        {"time": _iso(T0 + timedelta(minutes=30)), "watts": 1000.0},
        {"time": _iso(T0 + timedelta(minutes=90)), "watts": 3000.0},
        {"time": _iso(T0 + timedelta(minutes=95)), "watts": None},
    ]
    # Midpoints 06:15 (before the first: held), 06:45 (a quarter of the way),
    # 07:15 (three quarters), 07:45 (after the last: held; the last period is
    # as long as the one before it).
    assert sw.resample_instants(readings, "watts", grid) == [
        1000.0,
        1500.0,
        2500.0,
        3000.0,
    ]


def test_instant_readings_on_the_half_hour_are_the_reading_at_each_start():
    grid = _grid(3, start=T0)
    readings = [
        {"time": _iso(T0 + timedelta(minutes=15 + 30 * i)), "w": float(i)}
        for i in range(3)
    ]
    assert sw.resample_instants(readings, "w", grid) == [0.0, 1.0, 2.0]


def test_instant_readings_edge_shapes():
    assert sw.resample_instants([], "w", _grid(3)) == [0.0, 0.0, 0.0]
    one = [{"time": _iso(T0), "w": 7.0}]
    assert sw.resample_instants(one, "w", [T0]) == [7.0]


# --- generic price forecasts --------------------------------------------------


def _price(values, *, start=T0, step=30, unit="$/kWh", key="value"):
    return {
        "state": str(values[0]),
        "attributes": {
            "unit_of_measurement": unit,
            "forecast": [
                {"time": _iso(start + timedelta(minutes=step * i)), key: v}
                for i, v in enumerate(values)
            ],
        },
    }


def test_a_generic_forecast_holds_the_latest_point_and_marks_real_coverage(ha):
    ha.states["sensor.p"] = _price([0.1, 0.2], start=T0 + timedelta(minutes=30))
    values, real = sw._resample_generic_price_forecast_raw("sensor.p", _grid(4))
    assert values == [0.1, 0.1, 0.2, 0.2]
    assert real == [False, True, True, False]


def test_a_calibrated_only_forecast_is_read_and_bad_rows_are_skipped(ha):
    state = _price([0.3], key="calibrated")
    state["attributes"]["forecast"] += [{"time": "garbage", "value": 1}, {"value": 2}]
    ha.states["sensor.p"] = state
    assert sw._resample_generic_price_forecast_raw("sensor.p", _grid(2)) == (
        [0.3, 0.3],
        [True, False],
    )


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"state": "1", "attributes": {}},
        {"state": "1", "attributes": {"forecast": [{"x": 1}]}},
    ],
    ids=["missing_entity", "no_forecast", "no_usable_row"],
)
def test_no_usable_forecast_is_none_not_zero(ha, state):
    if state is not None:
        ha.states["sensor.p"] = state
    assert sw._resample_generic_price_forecast_raw("sensor.p", _grid(2)) is None
    assert (
        sw.resample_generic_price_forecast_with_coverage("sensor.p", _grid(2)) is None
    )


@pytest.mark.parametrize(
    ("unit", "scale"), [("$/kWh", 1.0), ("c/kWh", 0.01), ("$/MWh", 0.001)]
)
def test_a_generic_forecast_is_scaled_by_its_own_unit(ha, unit, scale):
    ha.states["sensor.p"] = _price([20.0, 40.0], unit=unit)
    values, real = sw.resample_generic_price_forecast_with_coverage(
        "sensor.p", _grid(2)
    )
    assert values == pytest.approx([20.0 * scale, 40.0 * scale])
    assert real == [True, True]


# --- secondary price sources ---------------------------------------------------

CFG = {"k2": "sensor.second", "k3": "sensor.third"}


def test_with_no_secondary_source_the_primary_passes_through_untouched(ha):
    primary = [0.1, 0.2]
    out, spread, labels = sw.blend_price_with_secondary_sources(
        primary, {}, ("k2", "k3"), _grid(2)
    )
    assert out is primary and spread is None and labels == ["primary", "primary"]
    # Configured but missing reads the same.
    out, spread, labels = sw.blend_price_with_secondary_sources(
        primary, CFG, ("k2", "k3"), _grid(2)
    )
    assert out is primary and spread is None


def test_the_real_primary_wins_and_secondaries_only_fill_where_it_has_nothing(ha):
    grid = _grid(4)
    # second: real for periods 1-2; third: real for period 2 only.
    ha.states["sensor.second"] = _price([0.5, 0.7], start=grid[1])
    ha.states["sensor.third"] = _price([0.9], start=grid[2])
    out, spread, labels = sw.blend_price_with_secondary_sources(
        [0.1, 0.2, 0.3, 0.4], CFG, ("k2", "k3"), grid, [True, False, False, False]
    )
    assert labels == ["primary", "secondary", "secondary", "fallback"]
    assert out[0] == 0.1  # never averaged with a live secondary
    assert out[1] == pytest.approx(0.5)  # the one real secondary, unchanged
    assert out[2] == pytest.approx((0.7 + 0.9) / 2)  # the real secondaries' mean
    assert out[3] == pytest.approx((0.4 + 0.7 + 0.9) / 3)  # nobody real: everyone
    assert spread is not None and len(spread) == 4


# --- the wholesale tail -------------------------------------------------------


def _tail_cfg():
    return {
        "solver_regional_spot_forecast_sensor": "sensor.regional",
        "solver_regional_spot_current_price_sensor": "sensor.spot_now",
        "solver_import_price_sensor": "sensor.import",
        "solver_export_price_sensor": "sensor.export",
    }


def test_a_retail_forecast_is_extended_by_wholesale_plus_its_own_learned_offset(
    monkeypatch,
):
    grid = _grid(4)
    aemo = [
        (grid[0] - timedelta(minutes=5), 0.05),
        (grid[2], 0.30),
        (grid[3] + timedelta(hours=1), 9.0),
    ]
    monkeypatch.setattr(sw, "fetch_aemo_forecast", lambda eid: aemo)
    monkeypatch.setattr(sw, "price_unit_scale_of_history", lambda eid: 0.01)
    seen = {}

    def history(eid):
        return [(grid[0], 100.0)]

    def offset(hist, regional_spot_sensor):
        seen["hist"], seen["spot"] = hist, regional_spot_sensor
        b = sw._local(grid[2])
        return {b.hour * 12 + b.minute // 5: 0.10, 0: 0.02}

    monkeypatch.setattr(sw, "fetch_price_history", history)
    monkeypatch.setattr(sw, "compute_5min_offset", offset)
    imp, exp = sw.extend_generic_price_tails(
        _tail_cfg(),
        grid,
        ([1.0, 1.0, 1.0, 1.0], [True, True, False, False], True),
        ([2.0, 2.0, 2.0, 2.0], [True, True, True, True], True),
    )
    # Periods 2 and 3 lie past the import forecast's last real point.
    assert imp[:2] == [1.0, 1.0]
    assert imp[2] == pytest.approx(0.30 + 0.10)  # its own time-of-day offset
    assert imp[3] == pytest.approx(0.30 + 0.06)  # no bucket: the mean offset
    assert exp == [2.0, 2.0, 2.0, 2.0]  # fully real: nothing to extend
    assert seen["hist"] == [(grid[0], 1.0)] and seen["spot"] == "sensor.spot_now"


@pytest.mark.parametrize(
    ("drop", "aemo", "offset", "has_fc"),
    [
        ("solver_regional_spot_forecast_sensor", [(T0, 1.0)], {0: 1.0}, True),
        ("solver_regional_spot_current_price_sensor", [(T0, 1.0)], {0: 1.0}, True),
        (None, [], {0: 1.0}, True),
        (None, [(T0, 1.0)], {}, True),
        (None, [(T0, 1.0)], {0: 1.0}, False),
    ],
    ids=["no_regional", "no_current", "no_wholesale", "no_offset", "no_forecast"],
)
def test_the_tail_is_left_alone_without_everything_it_needs(
    monkeypatch, drop, aemo, offset, has_fc
):
    cfg = _tail_cfg()
    cfg.pop(drop, None)
    monkeypatch.setattr(sw, "fetch_aemo_forecast", lambda eid: aemo)
    monkeypatch.setattr(sw, "price_unit_scale_of_history", lambda eid: 1.0)
    monkeypatch.setattr(sw, "fetch_price_history", lambda eid: [])
    monkeypatch.setattr(
        sw, "compute_5min_offset", lambda h, regional_spot_sensor: offset
    )
    side = ([1.0, 1.0], [True, False], has_fc)
    assert sw.extend_generic_price_tails(cfg, _grid(2), side, side) == (
        [1.0, 1.0],
        [1.0, 1.0],
    )


# --- P2P matched-rate sources ------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "currency", "scale"),
    [
        ("c/kWh", None, 0.01),
        ("¢/kWh", None, 0.01),
        ("$/kWh", None, 1.0),
        ("€/kWh", None, 1.0),
        ("AUD/kWh", None, None),
        ("AUD/kWh", "AUD", 1.0),
        ("aud/kWh", "AUD", 1.0),
        ("USD/kWh", "AUD", None),
        ("kWh", None, None),
        (None, None, None),
    ],
)
def test_a_p2p_rate_unit_must_be_a_price_per_kwh(monkeypatch, unit, currency, scale):
    hass = (
        None
        if currency is None
        else type("H", (), {"config": type("C", (), {"currency": currency})()})()
    )
    monkeypatch.setattr(sw.NATIVE, "hass", hass)
    assert sw._p2p_unit_scale(unit) == scale


def test_rate_rows_become_intervals():
    t = T0
    rows = [
        {"time": _iso(t), "end": _iso(t + timedelta(minutes=30)), "value": 20},
        {"time": _iso(t + timedelta(minutes=30)), "value": 10},  # no end
        {
            "time": _iso(t + timedelta(hours=1)),
            "end": _iso(t + timedelta(hours=1)),
            "value": 5,
        },
        {
            "time": _iso(t + timedelta(hours=2)),
            "end": _iso(t + timedelta(hours=3)),
            "value": "",
        },
        {
            "time": _iso(t + timedelta(hours=3)),
            "end": _iso(t + timedelta(hours=4)),
            "value": "nan",
        },
        {"time": "bad", "end": "bad", "value": 1},
        "not a row",
    ]
    assert sw._p2p_rate_intervals(rows, 0.01, lv_feed=False) == [
        (t, t + timedelta(minutes=30), sw.P2P_RATE, 0.2),
        (t + timedelta(hours=2), t + timedelta(hours=3), sw.P2P_NO_MATCH, 0.0),
    ]
    # A matched-rate feed's rows without an end are its own 5-minute intervals.
    lv = sw._p2p_rate_intervals(rows, 0.01, lv_feed=True)
    assert (
        t + timedelta(minutes=30),
        t + timedelta(minutes=35),
        sw.P2P_RATE,
        0.1,
    ) in lv


def test_volume_rows_become_matched_rates():
    end = T0 + timedelta(minutes=30)
    rows = [
        {
            "intervalEnd": _iso(end),
            "intervalDuration": 30,
            "volume": 2.0,
            "proportionP2P": 0.5,
            "matchedCost": 0.25,
        },
        {"time": _iso(end), "volume": 1.0, "proportionP2P": 0.005, "matchedCost": 1.0},
        {"time": _iso(end), "volume": 1.0, "proportionP2P": 0.5},  # no cost
        {"time": _iso(end), "volume": None, "proportionP2P": 0.5},
        {"time": _iso(end), "volume": "x", "proportionP2P": 0.5, "matchedCost": 1},
        42,
    ]
    assert sw._p2p_triple_intervals(rows) == [
        (T0, end, sw.P2P_RATE, 0.25),
        (end - timedelta(minutes=5), end, sw.P2P_NO_MATCH, 0.0),
    ]


def _p2p_state(rows, **attrs):
    return {
        "state": "1",
        "last_updated": _iso(T0),
        "attributes": {"forecast": rows, **attrs},
    }


def test_a_p2p_source_that_is_absent_unknown_or_expired_is_unavailable():
    for state in (
        None,
        {"state": "unavailable", "attributes": {}},
        {"state": "Unknown", "attributes": {}},
    ):
        assert (
            sw._p2p_source_observations(state, "sensor.p2p", T0)["status"]
            == sw.P2P_UNAVAILABLE
        )
    old = _p2p_state([])
    out = sw._p2p_source_observations(
        old, "sensor.p2p", T0 + sw.P2P_RATE_SOURCE_MAX_AGE + timedelta(seconds=1)
    )
    assert out["status"] == sw.P2P_UNAVAILABLE and out["snapshot"] == T0
    fresh = sw._p2p_source_observations(
        old, "sensor.p2p", T0 + sw.P2P_RATE_SOURCE_MAX_AGE
    )
    assert fresh["status"] == "ok"


def test_a_p2p_rate_source_with_a_non_price_unit_is_not_used():
    state = _p2p_state(
        [{"time": _iso(T0), "end": _iso(T0 + timedelta(minutes=5)), "value": 1}],
        unit_of_measurement="kWh",
    )
    assert (
        sw._p2p_source_observations(state, "sensor.p2p", T0)["status"]
        == sw.P2P_UNAVAILABLE
    )


def test_a_matched_rate_feed_reports_its_horizon_and_sorted_intervals():
    later = T0 + timedelta(minutes=10)
    rows = [
        {"time": _iso(later), "value": 30},
        {"time": _iso(T0 + timedelta(minutes=5)), "value": 20},
    ]
    state = _p2p_state(
        rows,
        unit_of_measurement="c/kWh",
        source_field="matchedCost",
        interpolation_mode="previous",
        lastUpdate=_iso(T0),
    )
    out = sw._p2p_source_observations(state, "sensor.p2p", T0)
    assert out["status"] == "ok"
    assert [i[0] for i in out["intervals"]] == [T0 + timedelta(minutes=5), later]
    assert out["horizon"] == (T0, later + timedelta(minutes=5))
    # A plain rate source has no horizon.
    plain = sw._p2p_source_observations(
        _p2p_state(rows, unit_of_measurement="c/kWh"), "sensor.p2p", T0
    )
    assert plain["horizon"] is None and plain["intervals"] == []


def test_a_volume_shaped_source_is_read_as_triples():
    end = T0 + timedelta(minutes=5)
    state = _p2p_state(
        [{"time": _iso(end), "volume": 1.0, "proportionP2P": 1.0, "matchedCost": 0.3}]
    )
    out = sw._p2p_source_observations(state, "sensor.p2p", None)
    assert out["intervals"] == [(T0, end, sw.P2P_RATE, 0.3)]


# --- the live grid meter ------------------------------------------------------


def _quality(**rec):
    return {"state": "ok", "attributes": {"meter_reconciliation": rec}}


@pytest.mark.parametrize(
    ("rec", "sign"),
    [
        (
            {
                "status": "agrees",
                "meter_sensor": "sensor.m",
                "meter_sign": "positive_is_import",
            },
            1.0,
        ),
        (
            {
                "status": "agrees",
                "meter_sensor": "sensor.m",
                "meter_sign": "positive_is_export",
            },
            -1.0,
        ),
        ({"status": "agrees", "meter_sensor": "sensor.m", "meter_sign": "?"}, None),
        (
            {
                "status": "disagrees",
                "meter_sensor": "sensor.m",
                "meter_sign": "positive_is_import",
            },
            None,
        ),
        (
            {
                "status": "agrees",
                "meter_sensor": "sensor.other",
                "meter_sign": "positive_is_import",
            },
            None,
        ),
    ],
)
def test_the_meter_sign_is_trusted_only_once_reconciled_for_this_meter(ha, rec, sign):
    ha.states[sw.QUALITY_ENTITY_ID] = _quality(**rec)
    assert sw._grid_meter_sign("sensor.m") == sign


def test_an_unreadable_quality_report_gives_no_meter_sign(ha):
    assert sw._grid_meter_sign("sensor.m") is None


@pytest.mark.parametrize(
    ("cfg", "state", "unit", "expected"),
    [
        ({"switchboard_grid_meter_sensor": "sensor.m"}, "-1500", "W", 1.5),
        ({"grid_sensor": "sensor.m"}, "2", "kW", -2.0),
        ({"grid_sensor": "sensor.m"}, "2", "A", None),
        ({"grid_sensor": "sensor.m"}, "nan", "kW", None),
        ({"grid_sensor": "sensor.m"}, "unavailable", "kW", None),
        ({}, "2", "kW", None),
    ],
)
def test_the_live_meter_reads_in_kw_with_the_reconciled_sign(
    ha, monkeypatch, cfg, state, unit, expected
):
    monkeypatch.setattr(sw, "_grid_meter_sign", lambda m: -1.0)
    ha.states["sensor.m"] = {
        "state": state,
        "attributes": {"unit_of_measurement": unit},
    }
    got = sw._live_grid_meter_kw(cfg)
    assert got == expected if expected is None else got == pytest.approx(expected)


def test_no_reconciled_sign_means_no_live_meter_reading(ha, monkeypatch):
    monkeypatch.setattr(sw, "_grid_meter_sign", lambda m: None)
    ha.states["sensor.m"] = {"state": "2", "attributes": {"unit_of_measurement": "kW"}}
    assert sw._live_grid_meter_kw({"grid_sensor": "sensor.m"}) is None


# --- envelope limits ------------------------------------------------------------


@pytest.fixture
def envelope(ha, monkeypatch):
    monkeypatch.setattr(sw, "_ENVELOPE_LIMIT_WARNED", set())
    return ha


def test_no_envelope_entity_is_the_static_limit(envelope):
    assert sw.resolve_envelope_limit_kw(None, 10.0, _grid(3)) == [10.0] * 3


def test_an_envelope_forecast_is_resampled_and_scaled_to_kw(envelope):
    grid = _grid(3)
    envelope.states["sensor.env"] = {
        "state": "5000",
        "attributes": {
            "unit_of_measurement": "W",
            "forecast": [
                {"time": _iso(grid[0]), "value": 5000},
                {"time": _iso(grid[2]), "value": 3000},
            ],
        },
    }
    assert sw.resolve_envelope_limit_kw("sensor.env", 10.0, grid) == pytest.approx(
        [5.0, 5.0, 3.0]
    )


def test_an_envelope_without_a_forecast_holds_its_live_value(envelope):
    envelope.states["sensor.env"] = {
        "state": "7",
        "attributes": {"unit_of_measurement": "kW"},
    }
    assert sw.resolve_envelope_limit_kw("sensor.env", 10.0, _grid(2)) == [7.0, 7.0]


def test_a_dropped_envelope_warns_once_falls_back_and_notes_recovery(envelope, caplog):
    grid = _grid(2)
    with caplog.at_level(logging.INFO, logger=sw._LOGGER.name):
        assert sw.resolve_envelope_limit_kw("sensor.env", 10.0, grid) == [10.0, 10.0]
        assert sw.resolve_envelope_limit_kw("sensor.env", 10.0, grid) == [10.0, 10.0]
        envelope.states["sensor.env"] = {"state": "unavailable", "attributes": {}}
        sw.resolve_envelope_limit_kw("sensor.env", 10.0, grid)
        envelope.states["sensor.env"] = {"state": "lots", "attributes": {}}
        assert sw.resolve_envelope_limit_kw("sensor.env", 10.0, grid) == [10.0, 10.0]
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 2
        assert "non-numeric" in warnings[1]
        envelope.states["sensor.env"] = {"state": "6", "attributes": {}}
        assert sw.resolve_envelope_limit_kw("sensor.env", 10.0, grid) == [6.0, 6.0]
        sw.resolve_envelope_limit_kw("sensor.env", 10.0, grid)
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len([m for m in infos if "reporting again" in m]) == 1
    assert sw._ENVELOPE_LIMIT_WARNED == set()


# --- power history ----------------------------------------------------------------


def test_power_history_reads_in_kw_skipping_what_is_not_a_power_reading(ha):
    def pt(minutes, state, unit="W"):
        return HistoryPoint(
            _iso(T0 + timedelta(minutes=minutes)), state, {"unit_of_measurement": unit}
        )

    ha.history["sensor.pv"] = [
        pt(10, "2000"),
        pt(0, "1.5", "kW"),
        pt(20, "unknown"),
        pt(30, "abc"),
        pt(40, "5", "A"),
        pt(50, "3", "kWh"),
    ]
    got = sw.fetch_entity_power_history_kw(
        "sensor.pv", T0 - timedelta(hours=1), T0 + timedelta(hours=1)
    )
    assert [v for _t, v in got] == pytest.approx([1.5, 2.0])
    assert [t for t, _v in got] == [T0, T0 + timedelta(minutes=10)]
    assert got[0][0].tzinfo == sw.LOCAL_TZ


def test_power_history_is_empty_when_there_is_none(ha):
    assert (
        sw.fetch_entity_power_history_kw("sensor.pv", T0, T0 + timedelta(hours=1)) == []
    )


@pytest.mark.parametrize(
    ("times", "expected"),
    [
        ([], None),
        ([0], None),
        ([0, 0], None),
        (
            [0, 10, 20, 30, 100, 120],
            {"points": 6, "median_gap_s": 600.0, "max_gap_s": 4200.0},
        ),
    ],
)
def test_power_history_coverage(times, expected):
    pts = [(T0 + timedelta(minutes=m), 1.0) for m in times]
    out = sw._power_history_coverage(pts, _grid(4), 0.5)
    if expected is None:
        assert out is None
        return
    assert {k: out[k] for k in expected} == expected
    # The 70-minute gap from 06:30 to 07:40 is past a 30-minute period, and
    # lies wholly inside the two-hour window.
    assert out["time_in_long_gaps_pct"] == pytest.approx(70 / 120 * 100, abs=0.01)
    assert 0.0 <= out["stale_periods_pct"] <= 100.0


def test_power_history_coverage_of_an_empty_grid_is_none():
    pts = [(T0, 1.0), (T0 + timedelta(minutes=5), 1.0)]
    assert sw._power_history_coverage(pts, [], 0.5) is None


# --- calendar trips ---------------------------------------------------------------
#
# Read through the package, as Home Assistant runs it: the reader's own
# relative import has no standalone fallback.


@pytest.fixture
def cal(monkeypatch):
    from _ha_stubs import install_ha_stubs

    install_ha_stubs()
    psw = importlib.import_module("custom_components.nimbus_load.solver_writer")
    fake = FakeHA(states={})
    monkeypatch.setattr(psw, "HA_BASE", BASE)
    monkeypatch.setattr(psw, "_load_token", lambda: "test")
    monkeypatch.setattr(psw.NATIVE, "hass", None)
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return psw, fake


def test_calendar_events_become_trips_with_a_timezone_each(cal):
    psw, ha = cal
    ha.service_responses = {
        "calendar.get_events": {
            "calendar.car": {
                "events": [
                    {
                        "start": "2026-09-21T08:00:00+10:00",
                        "end": "2026-09-21T17:00:00+10:00",
                        "summary": "Work",
                    },
                    {
                        "start": "2026-09-22",
                        "end": "2026-09-23",
                        "description": "All day",
                    },
                    {"start": "2026-09-24T08:00:00", "end": "2026-09-24T09:00:00"},
                    {"start": "nonsense", "end": "2026-09-24T09:00:00"},
                    "not an event",
                ]
            }
        }
    }
    trips = psw.fetch_calendar_trips("calendar.car", T0, T0 + timedelta(days=7))
    assert [t.summary for t in trips] == ["Work", "", ""]
    assert trips[1].description == "All day"
    assert trips[1].start == datetime(2026, 9, 22, tzinfo=sw.LOCAL_TZ)
    assert trips[2].start == datetime(2026, 9, 24, 8, tzinfo=UTC)
    (call,) = ha.service_calls
    assert call["data"] == {
        "entity_id": "calendar.car",
        "start_date_time": T0.isoformat(),
        "end_date_time": (T0 + timedelta(days=7)).isoformat(),
    }


@pytest.mark.parametrize(
    "response",
    [
        None,
        {"calendar.other": {}},
        {"calendar.car": []},
        {"calendar.car": {"events": "x"}},
    ],
)
def test_no_readable_events_is_no_trips(cal, response):
    psw, ha = cal
    if response is not None:
        ha.service_responses = {"calendar.get_events": response}
    assert psw.fetch_calendar_trips("calendar.car", T0, T0 + timedelta(days=1)) == []


# --- a scalar reading that is not a number ------------------------------------


@pytest.mark.parametrize(
    ("state", "fallback", "expected", "warned"),
    [
        ({"state": "0.2847"}, 0.0, 0.2847, False),
        ({"state": "-0.05"}, 0.0, -0.05, False),  # a negative price is real
        ({"state": "2026-08-24T13:00:00+10:00"}, 0.0, 0.0, True),  # the live crash
        ({"state": "unavailable"}, 0.0, 0.0, True),
        ({}, 0.0, 0.0, True),
        ({"state": "not-a-number"}, 0.15, 0.15, True),
    ],
    ids=["number", "negative", "timestamp", "unavailable", "no_state", "fallback"],
)
def test_a_scalar_that_cannot_be_read_as_a_number_falls_back_and_says_which(
    monkeypatch, caplog, state, fallback, expected, warned
):
    monkeypatch.setattr(solver_shared, "ha_get", lambda _eid: state)
    with caplog.at_level(logging.WARNING):
        assert sw.safe_num("sensor.export_price", fallback=fallback) == expected
    assert ("sensor.export_price" in caplog.text) is warned


# --- the stored previous plan, and the derated capacity --------------------------------------


def test_a_stored_plan_that_did_not_solve_is_no_previous_plan(tmp_path, monkeypatch):
    path = tmp_path / "plan_state.json"
    monkeypatch.setattr(sw, "PLAN_STATE_PATH", str(path))
    assert sw.load_previous_plan() is None  # nothing stored
    path.write_text('{"status": "infeasible"}', encoding="utf-8")
    assert sw.load_previous_plan() is None
    path.write_text("not json", encoding="utf-8")
    assert sw.load_previous_plan() is None


@pytest.mark.parametrize(
    ("soh", "capacity"), [(80.0, 32.0), (100.0, 40.0), (0.0, 40.0), (130.0, 40.0)]
)
def test_state_of_health_derates_the_pack_and_a_nonsense_value_is_ignored_once(
    caplog, monkeypatch, soh, capacity
):
    shared = importlib.import_module("solver_shared")
    monkeypatch.setattr(shared, "_SOH_RANGE_WARNED", set())
    cfg = {"solver_battery_capacity_kwh": 40.0, "solver_battery_soh_percent": soh}
    with caplog.at_level(logging.WARNING):
        assert shared.resolve_effective_capacity_kwh(cfg) == pytest.approx(capacity)
        shared.resolve_effective_capacity_kwh(cfg)
    assert caplog.text.count("State of Health") == (0 if 0 < soh <= 100 else 1)


@pytest.mark.parametrize(
    ("cfg", "number", "integer"),
    [
        ({}, 5.0, 7),
        ({"k": None}, 5.0, 7),
        ({"k": 0}, 0.0, 0),
        ({"k": "2.5"}, 2.5, None),
    ],
    ids=["missing", "none", "zero_is_kept", "string"],
)
def test_a_setting_reads_its_default_only_when_unset_never_when_zero(
    cfg, number, integer
):
    shared = importlib.import_module("solver_shared")
    assert shared._cfg_num(cfg, "k", 5.0) == number
    if integer is not None:
        assert shared._cfg_int(cfg, "k", 7) == integer


@pytest.mark.parametrize(
    ("zone", "expect"),
    [("Australia/Perth", "Australia/Perth"), ("Not/AZone", None), (None, None)],
    ids=["home_assistants_zone", "a_zone_that_does_not_exist", "no_config"],
)
def test_the_local_time_zone_is_home_assistants_own_unless_overridden(
    monkeypatch, caplog, zone, expect
):
    """Every local hour -- tariff periods, departures, day boundaries --
    reads one zone, the one Home Assistant is set to; one it cannot use
    keeps the zone already in force."""
    shared = importlib.import_module("solver_shared")
    monkeypatch.delenv("NIMBUS_SOLVER_TIMEZONE", raising=False)
    monkeypatch.setattr(sw, "LOCAL_TZ", sw.LOCAL_TZ)
    monkeypatch.setattr(shared, "LOCAL_TZ", shared.LOCAL_TZ)
    monkeypatch.setattr(shared.NATIVE, "hass", shared.NATIVE.hass)
    before = sw.LOCAL_TZ
    hass = types.SimpleNamespace(config=types.SimpleNamespace(time_zone=zone))
    with caplog.at_level(logging.ERROR):
        sw.set_native_hass(hass if zone else types.SimpleNamespace())
    assert str(sw.LOCAL_TZ) == (expect or str(before))
    if expect:
        assert shared.LOCAL_TZ is sw.LOCAL_TZ
    assert ("could not resolve" in caplog.text) is (expect is None)
