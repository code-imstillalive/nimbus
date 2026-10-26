"""Two solar measurements, recorded now and graded once reality arrives
(#128, #1259) (#1837).

The delivery ratio (#128) queues an hour-ahead prediction each solve and
grades it against the PV sensor, to catch inverter clipping. The nowcast
check (#1259) queues both the live anchor and the next period's forecast
whenever they disagree sharply and records which turned out closer -- a
measurement, never a correction. They share one state file and each must
leave the other's records alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import solver_writer as sw

BRISBANE = sw.LOCAL_TZ
NOON = datetime(2026, 9, 27, 12, 30, tzinfo=BRISBANE)
SENSOR = "sensor.real_solar"


def _grid(start, minutes=5, n=48):
    return [start + timedelta(minutes=minutes * i) for i in range(n)]


@pytest.fixture
def state(monkeypatch, tmp_path):
    path = tmp_path / "solar_state.json"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(sw, "SOLAR_DELIVERY_RATIO_PATH", str(path))
    readings = {"rows": None, "calls": 0}

    def fetch(eid, start, end):
        readings["calls"] += 1
        assert eid == SENSOR
        rows = readings["rows"]
        return rows(start) if callable(rows) else (rows or [])

    monkeypatch.setattr(sw, "fetch_entity_history_range", fetch)
    monkeypatch.setattr(sw, "_kw_scale_factor", lambda e: 1.0)

    class State:
        def read(self):
            return json.loads(path.read_text(encoding="utf-8"))

        def write(self, data):
            path.write_text(json.dumps(data), encoding="utf-8")

        def empty(self):
            return path.stat().st_size == 0

    s = State()
    s.readings = readings
    return s


NOWCAST = {
    "solver_solar_power_sensor": SENSOR,
    "solver_nowcast_measurement_enabled": True,
}


def _nowcast(now, anchor, forecast, cfg=NOWCAST):
    grid = _grid(now)
    return sw.update_solar_nowcast_disagreement(
        cfg, now, grid, [anchor] + [forecast] * 47
    ), grid


# --- the nowcast check (#1259) -----------------------------------------------------------------


def test_switched_off_or_without_a_pv_sensor_the_check_reads_and_writes_nothing(state):
    # test_switched_off_or_without_a_pv_sensor_the_check_reads_and_writes_nothing
    for cfg in [
        {**NOWCAST, "solver_nowcast_measurement_enabled": False},
        {**NOWCAST, "solver_solar_power_sensor": None},
    ]:
        assert _nowcast(NOON, 1.78, 15.89, cfg)[0] is None
        assert state.readings["calls"] == 0 and state.empty()

    # test_an_event_not_yet_due_stays_queued_and_reads_nothing
    _nowcast(NOON, 1.78, 15.89)
    _nowcast(NOON, 1.78, 15.89)
    assert len(state.read()["nowcast_pending"]) == 2 and state.readings["calls"] == 0

    # test_twilight_levels_are_not_even_considered: 3.6 kW against 0.4 at first light clears the bar on arithmetic and
    result, _ = _nowcast(datetime(2026, 9, 27, 5, 45, tzinfo=BRISBANE), 0.4, 3.6)
    assert state.read()["nowcast_pending"] == []
    assert (
        result["considered_count"],
        result["disagreement_rate"],
        result["disagreement_now_kw"],
    ) == (0, None, None)

    # test_once_reality_arrives_the_closer_of_the_two_is_recorded: The event that opened #1259: the forecast was closer by 8x, which is
    for actual, closer, anchor_err, forecast_err in [
        (17.875, "forecast", 16.095, 1.985),
        (2.10, "anchor", None, None),
    ]:
        _r, grid = _nowcast(NOON, 1.78, 15.89)
        state.readings["rows"] = [(grid[1], actual)]
        result, _ = _nowcast(grid[1], 15.89, 15.89)
        assert result["resolved_count"] == 1
        assert result[f"{closer}_closer_count"] == 1
        assert result["forecast_closer_fraction"] == (
            1.0 if closer == "forecast" else 0.0
        )
        if anchor_err is not None:
            assert result["mean_anchor_abs_error_kw"] == pytest.approx(
                anchor_err, abs=1e-3
            )
            assert result["mean_forecast_abs_error_kw"] == pytest.approx(
                forecast_err, abs=1e-3
            )
        assert state.read()["nowcast_pending"] == []

    # test_with_no_reading_at_the_target_the_event_is_dropped_not_a_tie
    _r, grid = _nowcast(NOON, 1.78, 15.89)
    state.readings["rows"] = []
    result, _ = _nowcast(grid[1], 15.89, 15.89)
    assert result["resolved_count"] == 0 and state.read()["nowcast_pending"] == []


# --- the delivery ratio (#128) -----------------------------------------------------------------

RATIO = {"solver_solar_power_sensor": SENSOR}


def _ratio(now, forecast_kw=10.0):
    return sw.update_solar_delivery_ratio(
        RATIO, now, _grid(now, minutes=15), [forecast_kw] * 48
    )


@pytest.mark.parametrize(
    ("actual", "scale", "ratio", "under"),
    [(7.5, 1.0, 0.75, True), (11200.0, 0.001, 1.12, False)],
)
def test_an_hour_ahead_prediction_is_graded_into_a_ratio(
    state, monkeypatch, actual, scale, ratio, under
):
    first = _ratio(NOON)
    assert (first["solar_delivery_ratio"], first["solar_delivery_sample_count"]) == (
        None,
        0,
    )
    (queued,) = state.read()["pending"]
    assert (
        queued["target_time"] == (NOON + timedelta(hours=1)).isoformat()
    )  # an hour ahead
    assert state.readings["calls"] == 0
    monkeypatch.setattr(
        sw, "_kw_scale_factor", lambda e: scale
    )  # W: a ratio, never 1,121
    state.readings["rows"] = lambda start: [(start + timedelta(minutes=5), actual)]
    graded = _ratio(NOON + timedelta(minutes=65))
    assert graded["solar_delivery_sample_count"] == 1
    assert graded["solar_delivery_ratio"] == pytest.approx(ratio, abs=1e-3)
    assert graded["solar_delivery_underperforming"] is under  # below 0.80


def test_a_small_forecast_is_dropped_not_graded(state):
    # test_a_small_forecast_is_dropped_not_graded
    dawn = datetime(2026, 8, 25, 6, 0, tzinfo=BRISBANE)
    _ratio(dawn, forecast_kw=1.0)  # under the 5 kW floor
    calls = state.readings["calls"]
    assert (
        _ratio(dawn + timedelta(minutes=65), forecast_kw=1.0)[
            "solar_delivery_sample_count"
        ]
        == 0
    )
    assert state.readings["calls"] == calls

    # test_impossible_or_stale_ratios_are_dropped
    for ratios, kept in [
        ([("now", 1121.05), ("now", 0.9)], 0.9),  # a unit mix-up's ratio is dropped
        ([("stale", 0.5), ("fresh", 0.9)], 0.9),  # the window rolls
    ]:
        when = {
            "now": NOON,
            "stale": NOON - timedelta(hours=sw.SOLAR_DELIVERY_ROLLING_WINDOW_HOURS + 1),
            "fresh": NOON - timedelta(minutes=10),
        }
        state.write(
            {
                "pending": [],
                "ratios": [
                    {"time": when[k].isoformat(), "ratio": r} for k, r in ratios
                ],
            }
        )
        result = _ratio(NOON)
        assert result["solar_delivery_sample_count"] == 1
        assert result["solar_delivery_ratio"] == pytest.approx(kept, abs=1e-3)


# --- one state file, two measurements ---------------------------------------------------------
