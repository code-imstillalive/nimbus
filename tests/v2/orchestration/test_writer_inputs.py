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
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
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


def test_instant_readings_edge_shapes(ha, monkeypatch):
    # test_instant_readings_edge_shapes
    assert sw.resample_instants([], "w", _grid(3)) == [0.0, 0.0, 0.0]
    one = [{"time": _iso(T0), "w": 7.0}]
    assert sw.resample_instants(one, "w", [T0]) == [7.0]

    # test_no_usable_forecast_is_none_not_zero
    for state in [
        None,
        {"state": "1", "attributes": {}},
        {"state": "1", "attributes": {"forecast": [{"x": 1}]}},
    ]:
        if state is not None:
            ha.states["sensor.p"] = state
        assert sw._resample_generic_price_forecast_raw("sensor.p", _grid(2)) is None
        assert (
            sw.resample_generic_price_forecast_with_coverage("sensor.p", _grid(2))
            is None
        )

    # test_a_generic_forecast_is_scaled_by_its_own_unit
    for unit, scale in [("$/kWh", 1.0), ("c/kWh", 0.01), ("$/MWh", 0.001)]:
        ha.states["sensor.p"] = _price([20.0, 40.0], unit=unit)
        values, real = sw.resample_generic_price_forecast_with_coverage(
            "sensor.p", _grid(2)
        )
        assert values == pytest.approx([20.0 * scale, 40.0 * scale])
        assert real == [True, True]

    # test_with_no_secondary_source_the_primary_passes_through_untouched
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

    # test_the_real_primary_wins_and_secondaries_only_fill_where_it_has_nothing
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

    # test_a_retail_forecast_is_extended_by_wholesale_plus_its_own_learned_offset
    with monkeypatch.context() as mp:
        grid = _grid(4)
        aemo = [
            (grid[0] - timedelta(minutes=5), 0.05),
            (grid[2], 0.30),
            (grid[3] + timedelta(hours=1), 9.0),
        ]
        mp.setattr(sw, "fetch_aemo_forecast", lambda eid: aemo)
        mp.setattr(sw, "price_unit_scale_of_history", lambda eid: 0.01)
        seen = {}

        def history(eid):
            return [(grid[0], 100.0)]

        def offset(hist, regional_spot_sensor):
            seen["hist"], seen["spot"] = hist, regional_spot_sensor
            b = sw._local(grid[2])
            return {b.hour * 12 + b.minute // 5: 0.10, 0: 0.02}

        mp.setattr(sw, "fetch_price_history", history)
        mp.setattr(sw, "compute_5min_offset", offset)
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


# --- secondary price sources ---------------------------------------------------

CFG = {"k2": "sensor.second", "k3": "sensor.third"}


# --- the wholesale tail -------------------------------------------------------


def _tail_cfg():
    return {
        "solver_regional_spot_forecast_sensor": "sensor.regional",
        "solver_regional_spot_current_price_sensor": "sensor.spot_now",
        "solver_import_price_sensor": "sensor.import",
        "solver_export_price_sensor": "sensor.export",
    }


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


def test_a_p2p_rate_unit_must_be_a_price_per_kwh(monkeypatch, ha, envelope):
    # test_a_p2p_rate_unit_must_be_a_price_per_kwh
    with monkeypatch.context() as mp:
        for unit, currency, scale in [
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
        ]:
            with mp.context() as mp:
                hass = (
                    None
                    if currency is None
                    else type(
                        "H", (), {"config": type("C", (), {"currency": currency})()}
                    )()
                )
                mp.setattr(sw.NATIVE, "hass", hass)
                assert sw._p2p_unit_scale(unit) == scale

    # test_rate_rows_become_intervals
    # test_rate_rows_become_intervals
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

    # test_volume_rows_become_matched_rates
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

    # test_a_p2p_source_that_is_absent_unknown_or_expired_is_unavailable
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

    # test_a_p2p_rate_source_with_a_non_price_unit_is_not_used
    state = _p2p_state(
        [{"time": _iso(T0), "end": _iso(T0 + timedelta(minutes=5)), "value": 1}],
        unit_of_measurement="kWh",
    )
    assert (
        sw._p2p_source_observations(state, "sensor.p2p", T0)["status"]
        == sw.P2P_UNAVAILABLE
    )

    # test_a_matched_rate_feed_reports_its_horizon_and_sorted_intervals
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

    # test_a_volume_shaped_source_is_read_as_triples
    end = T0 + timedelta(minutes=5)
    state = _p2p_state(
        [{"time": _iso(end), "volume": 1.0, "proportionP2P": 1.0, "matchedCost": 0.3}]
    )
    out = sw._p2p_source_observations(state, "sensor.p2p", None)
    assert out["intervals"] == [(T0, end, sw.P2P_RATE, 0.3)]

    # test_the_meter_sign_is_trusted_only_once_reconciled_for_this_meter
    for rec, sign in [
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
    ]:
        ha.states[sw.QUALITY_ENTITY_ID] = _quality(**rec)
        assert sw._grid_meter_sign("sensor.m") == sign

    # test_an_unreadable_quality_report_gives_no_meter_sign
    assert sw._grid_meter_sign("sensor.m") is None

    # test_the_live_meter_reads_in_kw_with_the_reconciled_sign
    with monkeypatch.context() as mp:
        for cfg, state, unit, expected in [
            ({"switchboard_grid_meter_sensor": "sensor.m"}, "-1500", "W", 1.5),
            ({"grid_sensor": "sensor.m"}, "2", "kW", -2.0),
            ({"grid_sensor": "sensor.m"}, "2", "A", None),
            ({"grid_sensor": "sensor.m"}, "nan", "kW", None),
            ({"grid_sensor": "sensor.m"}, "unavailable", "kW", None),
            ({}, "2", "kW", None),
        ]:
            with mp.context() as mp:
                mp.setattr(sw, "_grid_meter_sign", lambda m: -1.0)
                ha.states["sensor.m"] = {
                    "state": state,
                    "attributes": {"unit_of_measurement": unit},
                }
                got = sw._live_grid_meter_kw(cfg)
                assert (
                    got == expected
                    if expected is None
                    else got == pytest.approx(expected)
                )

    # test_an_envelope_forecast_is_resampled_and_scaled_to_kw
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


def _p2p_state(rows, **attrs):
    return {
        "state": "1",
        "last_updated": _iso(T0),
        "attributes": {"forecast": rows, **attrs},
    }


# --- the live grid meter ------------------------------------------------------


def _quality(**rec):
    return {"state": "ok", "attributes": {"meter_reconciliation": rec}}


# --- envelope limits ------------------------------------------------------------


@pytest.fixture
def envelope(ha, monkeypatch):
    monkeypatch.setattr(sw, "_ENVELOPE_LIMIT_WARNED", set())
    return ha


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


def test_power_history_coverage(cal):
    # test_power_history_coverage
    for times, expected in [
        ([], None),
        ([0], None),
        ([0, 0], None),
        (
            [0, 10, 20, 30, 100, 120],
            {"points": 6, "median_gap_s": 600.0, "max_gap_s": 4200.0},
        ),
    ]:
        pts = [(T0 + timedelta(minutes=m), 1.0) for m in times]
        out = sw._power_history_coverage(pts, _grid(4), 0.5)
        if expected is None:
            assert out is None
            continue
        assert {k: out[k] for k in expected} == expected
        # The 70-minute gap from 06:30 to 07:40 is past a 30-minute period, and
        # lies wholly inside the two-hour window.
        assert out["time_in_long_gaps_pct"] == pytest.approx(70 / 120 * 100, abs=0.01)
        assert 0.0 <= out["stale_periods_pct"] <= 100.0

    # test_calendar_events_become_trips_with_a_timezone_each
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
                    {"summary": "no times"},  # skipped, never defaulted
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


# --- the stored previous plan, and the derated capacity --------------------------------------


def test_a_stored_plan_that_did_not_solve_is_no_previous_plan(
    tmp_path, monkeypatch, caplog
):
    # test_a_stored_plan_that_did_not_solve_is_no_previous_plan
    tmp_0 = tmp_path / "test_a_stored_plan_that_did_not_solve_is_no_previous_plan"
    tmp_0.mkdir()
    with monkeypatch.context() as mp:
        path = tmp_0 / "plan_state.json"
        mp.setattr(sw, "PLAN_STATE_PATH", str(path))
        assert sw.load_previous_plan() is None  # nothing stored
        path.write_text('{"status": "infeasible"}', encoding="utf-8")
        assert sw.load_previous_plan() is None
        path.write_text("not json", encoding="utf-8")
        assert sw.load_previous_plan() is None

    # test_state_of_health_derates_the_pack_and_a_nonsense_value_is_ignored_once
    caplog.clear()
    with monkeypatch.context() as mp:
        for soh, capacity in [(80.0, 32.0), (100.0, 40.0), (0.0, 40.0), (130.0, 40.0)]:
            caplog.clear()
            with mp.context() as mp:
                shared = importlib.import_module("solver_shared")
                mp.setattr(shared, "_SOH_RANGE_WARNED", set())
                cfg = {
                    "solver_battery_capacity_kwh": 40.0,
                    "solver_battery_soh_percent": soh,
                }
                with caplog.at_level(logging.WARNING):
                    assert shared.resolve_effective_capacity_kwh(cfg) == pytest.approx(
                        capacity
                    )
                    shared.resolve_effective_capacity_kwh(cfg)
                assert caplog.text.count("State of Health") == (
                    0 if 0 < soh <= 100 else 1
                )

    # test_an_uncovered_period_inside_a_block_takes_the_median_and_outside_it_nothing
    # test_an_uncovered_period_inside_a_block_takes_the_median_and_outside_it_nothing
    feed = _source(
        T0, (0, sw.P2P_RATE, 0.2), (1, sw.P2P_RATE, 0.0), (2, sw.P2P_NO_MATCH, 0.0)
    )
    # Period 3 is uncovered: inside a block it takes the median of the
    # published rates (the $0 match counts, the no-match does not).
    assert _resolve(feed, blocks=[5.0, 5.0, 5.0, 5.0]) == [0.2, 0.0, 0.0, 0.1]
    assert _resolve(feed, blocks=[5.0, 5.0, 5.0, 0.0])[3] == 0.0  # outside the block
    assert _resolve(feed)[3] == 0.0  # no blocks configured
    down = _source(T0, (3, sw.P2P_RATE, 0.4), status=sw.P2P_UNAVAILABLE)
    assert (
        _resolve(down, blocks=[5.0] * 4) == [0.0] * 4
    )  # an unavailable feed gives nothing

    # test_inside_a_feeds_horizon_silence_is_no_match_and_past_it_uncovered
    horizon = (T0, T0 + 2 * M5)
    feed = _source(T0, (0, sw.P2P_RATE, 0.3), horizon=horizon)
    assert sw._p2p_observation_at(feed, T0 + M5) == (sw.P2P_NO_MATCH, 0.0)
    assert sw._p2p_observation_at(feed, T0 + 3 * M5) == (sw.P2P_UNCOVERED, 0.0)
    assert _resolve(feed, blocks=[5.0] * 4) == [0.3, 0.0, 0.3, 0.3]

    # test_no_source_one_unreadable_source_or_the_same_one_twice
    with monkeypatch.context() as mp:
        grid = _grid(3, minutes=5)
        assert sw.resample_real_p2p_rate(grid, None) == [0.0] * 3
        mp.setattr(sw, "ha_get", lambda eid: {"state": "1", "attributes": "broken"})
        assert sw._read_p2p_state("sensor.p2p") is None
        assert sw.resample_real_p2p_rate(grid, "sensor.p2p") == [0.0] * 3
        asked = []
        mp.setattr(sw, "ha_get", lambda eid: asked.append(eid) or {"attributes": {}})
        sw.resample_real_p2p_rate(grid, "sensor.p2p", fallback_sensor_id="sensor.p2p")
        assert asked == ["sensor.p2p"]  # configured twice, read once

    # test_one_raw_triple_feed_keeps_the_original_step_lookup: The single raw-triple path is the pre-#1537 algorithm, unchanged:
    with monkeypatch.context() as mp:
        rows = [
            {"time": _iso(T0), "volume": 2.0, "proportionP2P": 0.5, "matchedCost": 0.2},
            {"time": _iso(T0 + M5), "volume": "lots"},  # skipped, not fatal
            {
                "time": _iso(T0 + M5),
                "volume": 1.0,
                "proportionP2P": 1.0,
                "matchedCost": 0.4,
            },
        ]
        mp.setattr(
            sw, "ha_get", lambda eid: {"state": "1", "attributes": {"forecast": rows}}
        )
        grid = _grid(4, minutes=5)
        assert sw.resample_real_p2p_rate(grid, "sensor.p2p") == pytest.approx(
            [0.4, 0.0, 0.0, 0.0]
        )
        blocks = [5.0, 5.0, 5.0, 5.0]
        assert sw.resample_real_p2p_rate(grid, "sensor.p2p", blocks) == pytest.approx(
            [0.4, 0.3, 0.3, 0.3]
        )

    # test_a_second_rate_source_changes_neither_the_commitment_nor_the_volume: A rate observation creates no commitment. Without a settlement
    with monkeypatch.context() as mp:
        from solver_inputs import prices

        grid = _grid(3, minutes=5)
        n = len(grid)
        states = {
            "sensor.a": {"state": "1", "attributes": {"forecast": []}},
            "sensor.b": _p2p_state(
                [{"time": _iso(t), "value": 0.5} for t in grid],
                unit_of_measurement="$/kWh",
            ),
        }
        for name, value in {
            "ha_get": lambda e: states.get(e, {"attributes": {"forecast": []}}),
            "fetch_aemo_forecast": lambda _e: None,
            "fetch_price_history": lambda *a, **k: [],
            "compute_5min_offset": lambda *a, **k: {},
            "compute_price_percentile_band": lambda *a, **k: {},
            "resample_price_with_extrapolation": lambda *a, **k: (
                [0.1] * n,
                [True] * n,
            ),
            "check_aemo_p5min_disagreement": lambda *a, **k: None,
            "p2p_match_fraction": lambda **k: 0.65,
            "p2p_recent_avg_volume_kwh": lambda **k: 60.0,
        }.items():
            mp.setattr(sw, name, value)

        def build(cfg):
            return prices.build_price_arrays(
                {
                    "solver_import_price_sensor": "sensor.i",
                    "solver_export_price_sensor": "sensor.e",
                    "solver_p2p_matched_rate_forecast_sensor": "sensor.a",
                    **cfg,
                },
                grid,
                n,
                grid[0],
                True,
                "sensor.array",
            )

        with_fallback = {
            **BLOCKS,
            "solver_p2p_matched_rate_forecast_sensor_2": "sensor.b",
        }
        a, b = build(BLOCKS), build(with_fallback)
        assert a.p2p_recent_volume_kwh == b.p2p_recent_volume_kwh
        assert a.p2p_recent_volume_kwh == pytest.approx(
            sw.p2p_blocks_daily_energy_kwh(BLOCKS)
        )
        assert a.p2p_recent_volume_kwh != 60.0
        in_block = _grid(3, minutes=5, start=T0 + timedelta(hours=1))  # 17:00 Brisbane
        committed = sw.fetch_p2p_fixed_export_kw(BLOCKS, in_block)
        assert committed == pytest.approx([11.5] * 3)
        np.testing.assert_array_equal(
            committed, sw.fetch_p2p_fixed_export_kw(with_fallback, in_block)
        )

    # test_a_feeds_freshness_is_the_providers_own_stamp_before_home_assistants
    for state, snapshot in [
        ({"attributes": {"lastUpdate": _iso(T0)}, "last_updated": _iso(T0 + M5)}, T0),
        ({"attributes": {}, "last_updated": _iso(T0 + M5)}, T0 + M5),
        ({"attributes": {"lastUpdate": "garbage"}, "last_updated": _iso(T0)}, T0),
        ({"attributes": {}}, None),
    ]:
        assert sw._p2p_snapshot_time(state) == snapshot


# --- two matched-rate sources, one answer per period (#1537) ---------------------------------

M5 = timedelta(minutes=5)


def _source(snapshot, *intervals, horizon=None, status="ok"):
    return {
        "status": status,
        "snapshot": snapshot,
        "horizon": horizon,
        "intervals": [
            (T0 + M5 * i, T0 + M5 * (i + 1), state, rate)
            for i, state, rate in intervals
        ],
    }


def _resolve(*sources, blocks=None, n=4):
    return sw._resolve_p2p_observations(list(sources), _grid(n, minutes=5), blocks)


BLOCKS = {
    "solver_p2p_block_1_rate_kw": 11.5,
    "solver_p2p_block_1_start_hour": 17,
    "solver_p2p_block_1_end_hour": 24,
}


def test_the_access_token_comes_from_the_environment_then_its_file(
    monkeypatch, tmp_path
):
    for name in ("SUPERVISOR_TOKEN", "HA_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    token_file = tmp_path / "token"
    monkeypatch.setattr(sw, "TOKEN_PATH", str(token_file))

    def fresh():
        monkeypatch.setattr(sw, "_TOKEN_LOADED", False)
        monkeypatch.setattr(sw, "_TOKEN", None)
        return sw._load_token()

    assert fresh() is None  # no environment, no file
    token_file.write_text("from-file\n", encoding="utf-8")
    assert fresh() == "from-file"
    monkeypatch.setenv("HA_TOKEN", "from-env")
    assert fresh() == "from-env"
    token_file.write_text("changed", encoding="utf-8")
    assert sw._load_token() == "from-env"  # read once, then kept


# --- a price event the household arms to test the plan (#1213) ------------------------------

EVENT_DAY = datetime(2026, 9, 26, tzinfo=UTC)
ARMED = {
    "solver_price_event_enabled": True,
    "solver_price_event_sensor": "sensor.event",
}


def _steps(*pairs):
    return [
        {"time": (EVENT_DAY + timedelta(hours=h)).isoformat(), "value": v}
        for h, v in pairs
    ]


@pytest.fixture
def event(monkeypatch):
    def run(state, n=8, cfg=ARMED, minutes=30):
        monkeypatch.setattr(sw, "ha_get", lambda _e: state)
        return sw.resolve_price_event_delta(
            cfg, _grid(n, minutes=minutes, start=EVENT_DAY)
        )

    return run


def _forecast(points):
    return {"state": "0.0", "attributes": {"forecast": points}}


def test_an_event_that_is_not_armed_or_not_here_is_none_not_zeros(
    event, caplog, monkeypatch, array_branch, ha
):
    # test_an_event_that_is_not_armed_or_not_here_is_none_not_zeros
    for cfg, n, points in [
        (
            {**ARMED, "solver_price_event_enabled": False},
            8,
            _steps((0, 20.0), (2, 0.0)),
        ),
        ({"solver_price_event_enabled": True}, 8, _steps((0, 20.0))),
        (ARMED, 0, _steps((0, 20.0))),
        (ARMED, 8, _steps((48, 20.0), (50, 0.0))),  # misses this horizon
    ]:
        assert event(_forecast(points), n=n, cfg=cfg) is None

    # test_an_armed_event_that_cannot_be_read_warns_and_applies_nothing
    caplog.clear()
    for state in [
        None,
        {"state": "0.0", "attributes": {}},
        _forecast([{"time": "not-a-date", "value": "nope"}, {"nothing": 1}, "x"]),
    ]:
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            assert event(state, n=4) is None
        assert "#1213" in caplog.text

    # test_an_event_that_never_closes_holds_and_says_so
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert event(_forecast(_steps((0, 20.0))), n=6) == [20.0] * 6
    assert "never closes" in caplog.text and "#1213" in caplog.text

    # test_a_days_event_lands_on_its_own_hours_only
    for points, window, delta in [
        (_steps((18, 20.0), (20, 0.0)), range(36, 40), 20.0),  # a price-cap evening
        (_steps((11, -1.0), (15, 0.0)), range(22, 30), -1.0),  # a negative midday
    ]:
        out = event(_forecast(points), n=48)
        assert [i for i, d in enumerate(out) if d] == list(window)
        assert {out[i] for i in window} == {delta}

    # test_the_matched_rate_counts_inside_the_households_own_blocks
    with monkeypatch.context() as mp:
        for grid, window, expected in [
            (MIDDAY, [5.0] * 3, [LV_RATE] * 3),  # a block at any hour gets its rate
            (EVENING, [NAN] * 3, [0.0] * 3),  # outside the blocks, even at 17:00
            (MIDDAY, [NAN, 5.0, 0.0], [0.0, LV_RATE, 0.0]),  # per period; 0 kW pins
            (MIDDAY, None, [LV_RATE] * 3),  # no block: matched at any hour (#1559)
            (EVENING, None, [LV_RATE] * 3),
        ]:
            with mp.context() as mp:
                assert _lv_rates(mp, grid, window) == pytest.approx(expected)

    # test_the_generic_branchs_p2p_volume
    with monkeypatch.context() as mp:
        for cfg, volume in [
            (
                _blocks((11.5, 17, 24)),
                80.5,
            ),  # the wizard's default 0 becomes the blocks'
            ({**_blocks((11.5, 17, 24)), "solver_p2p_bonus_volume_kwh": 40}, 40.0),
            ({}, 0.0),
        ]:
            with mp.context() as mp:
                from solver_inputs import prices

                n = len(EVENING)
                mp.setattr(
                    sw,
                    "resample_generic_price_forecast_with_coverage",
                    lambda *_a: ([0.1] * n, [True] * n),  # noqa: B023
                )
                out = prices.build_price_arrays(
                    {
                        "solver_import_price_sensor": "sensor.i",
                        "solver_export_price_sensor": "sensor.e",
                        **cfg,
                    },
                    EVENING,
                    n,
                    EVENING[0],
                    False,
                    None,
                )
                assert out.p2p_recent_volume_kwh == pytest.approx(volume)

    # test_on_the_price_array_branch_the_blocks_are_the_window_and_the_volume
    blocks = _blocks((11.5, 17, 24))
    out = array_branch(blocks)
    assert array_branch.seen["window"] == [11.5] * 3
    assert out.p2p_recent_volume_kwh == pytest.approx(80.5)
    assert out.export_bonus_price == pytest.approx([0.4] * 3)  # premium over spot
    settled = {**blocks, "solver_p2p_settlement_history_sensor": "sensor.hist"}
    assert array_branch(settled, recent=47.7).p2p_recent_volume_kwh == 47.7
    none = array_branch({})
    assert array_branch.seen["window"] is None and none.p2p_recent_volume_kwh == 60.0

    # test_history_is_read_sorted_skipping_what_is_not_a_number
    def pt(minutes, state):
        return HistoryPoint(_iso(T0 + timedelta(minutes=minutes)), state, {})

    ha.history["sensor.price"] = [
        pt(60, "0.25"),
        pt(0, "0.10"),
        pt(10, "unknown"),
        pt(20, "unavailable"),
        pt(30, "not-a-number"),
    ]
    got = sw.fetch_entity_history_range(
        "sensor.price", T0 - timedelta(hours=1), T0 + timedelta(hours=2)
    )
    assert [v for _t, v in got] == [0.10, 0.25]
    assert got[0][0] < got[1][0]
    assert (
        sw.fetch_entity_history_range("sensor.none", T0, T0 + timedelta(hours=1)) == []
    )

    # test_a_failed_history_read_is_no_history
    with monkeypatch.context() as mp:

        def boom(*_a, **_k):
            raise urllib.error.HTTPError("url", 500, "server error", {}, None)

        mp.setattr(sw.NATIVE, "hass", None)
        mp.setattr(sw, "_load_token", lambda: "t")
        mp.setattr(urllib.request, "urlopen", boom)
        assert (
            sw.fetch_entity_history_range("sensor.price", T0, T0 + timedelta(hours=1))
            == []
        )


# --- P2P follows the household's own blocks (#1537) ------------------------------------------

LV_ROW = {"proportionP2P": 0.569915, "matchedCost": 0.268861, "volume": 0.9368}
LV_RATE = 0.268861 / (0.9368 * 0.569915)
MIDDAY = _grid(
    3, minutes=5, start=datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
)  # 11:00 local
EVENING = _grid(
    3, minutes=5, start=datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
)  # 17:00 local


def _lv_rates(monkeypatch, grid, window, rows=None, **row):
    if rows is None:
        rows = [
            {"intervalEnd": (t + timedelta(minutes=5)).isoformat(), **LV_ROW, **row}
            for t in grid
        ]
    monkeypatch.setattr(sw, "ha_get", lambda _e: {"attributes": {"forecast": rows}})
    return sw.resample_real_p2p_rate(grid, "sensor.p2p", window)


NAN = float("nan")


def _blocks(*blocks):
    cfg = {}
    for n, (rate, start, end) in enumerate(blocks, 1):
        cfg[f"solver_p2p_block_{n}_rate_kw"] = rate
        cfg[f"solver_p2p_block_{n}_start_hour"] = start
        cfg[f"solver_p2p_block_{n}_end_hour"] = end
    return cfg


@pytest.fixture
def array_branch(monkeypatch):
    from solver_inputs import prices

    n = len(EVENING)
    seen = {}

    def rate(grid, sensor_id=None, window=None, **_k):
        seen["window"] = window
        return [0.5] * len(grid)

    def build(cfg, recent=60.0):
        for name, value in {
            "ha_get": lambda _e: {"attributes": {"forecast": []}},
            "fetch_aemo_forecast": lambda _e: None,
            "fetch_price_history": lambda *a, **k: [],
            "compute_5min_offset": lambda *a, **k: {},
            "compute_price_percentile_band": lambda *a, **k: {},
            "resample_price_with_extrapolation": lambda *a, **k: (
                [0.1] * n,
                [True] * n,
            ),
            "resample_real_p2p_rate": rate,
            "check_aemo_p5min_disagreement": lambda *a, **k: None,
            "p2p_match_fraction": lambda **k: 0.65,
            "p2p_recent_avg_volume_kwh": lambda **k: recent,
        }.items():
            monkeypatch.setattr(sw, name, value)
        return prices.build_price_arrays(
            {
                "solver_import_price_sensor": "sensor.i",
                "solver_export_price_sensor": "sensor.e",
                **cfg,
            },
            EVENING,
            n,
            EVENING[0],
            True,
            "sensor.array",
        )

    build.seen = seen
    return build


# --- one entity's recorded history, and the price history that wraps it (#363) ---------------


@pytest.mark.parametrize(("kwargs", "days"), [({"days": 3}, 3), ({}, 5)])
def test_price_history_is_the_last_days_up_to_now(monkeypatch, kwargs, days):
    calls = []
    monkeypatch.setattr(
        sw, "fetch_entity_history_range", lambda *a: calls.append(a) or []
    )
    sw.fetch_price_history("sensor.price", **kwargs)
    ((entity, start, end),) = calls
    assert entity == "sensor.price" and end - start == timedelta(days=days)
    assert datetime.now(UTC) - end < timedelta(seconds=10)
