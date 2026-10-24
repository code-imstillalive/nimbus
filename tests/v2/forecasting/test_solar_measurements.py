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


@pytest.mark.parametrize(
    "cfg",
    [
        {**NOWCAST, "solver_nowcast_measurement_enabled": False},
        {**NOWCAST, "solver_solar_power_sensor": None},
    ],
    ids=["switched_off", "no_pv_sensor"],
)
def test_switched_off_or_without_a_pv_sensor_the_check_reads_and_writes_nothing(
    state, cfg
):
    assert _nowcast(NOON, 1.78, 15.89, cfg)[0] is None
    assert state.readings["calls"] == 0 and state.empty()


def test_a_sharp_disagreement_is_queued_for_the_next_periods_own_time(state):
    result, grid = _nowcast(NOON, 1.78, 15.89)
    (queued,) = state.read()["nowcast_pending"]
    assert queued["target_time"] == grid[1].isoformat()
    assert (queued["anchor_kw"], queued["forecast_kw"]) == pytest.approx((1.78, 15.89))
    assert (
        result["considered_count"],
        result["disagreement_count"],
        result["resolved_count"],
    ) == (1, 1, 0)
    assert result["disagreement_now_kw"] == pytest.approx(14.11, abs=1e-3)
    assert (
        result["disagreement_rate"] == 1.0
        and result["forecast_closer_fraction"] is None
    )
    assert state.readings["calls"] == 0  # queueing reads nothing


def test_an_event_not_yet_due_stays_queued_and_reads_nothing(state):
    _nowcast(NOON, 1.78, 15.89)
    _nowcast(NOON, 1.78, 15.89)
    assert len(state.read()["nowcast_pending"]) == 2 and state.readings["calls"] == 0


def test_agreement_counts_toward_the_rate_but_queues_nothing(state):
    result, _grid_times = _nowcast(NOON, 15.0, 15.5)
    assert state.read()["nowcast_pending"] == []
    assert (
        result["considered_count"],
        result["disagreement_count"],
        result["disagreement_rate"],
    ) == (1, 0, 0.0)


def test_twilight_levels_are_not_even_considered(state):
    """3.6 kW against 0.4 at first light clears the bar on arithmetic and
    means nothing physically; it must not dilute the rate either."""
    result, _ = _nowcast(datetime(2026, 9, 27, 5, 45, tzinfo=BRISBANE), 0.4, 3.6)
    assert state.read()["nowcast_pending"] == []
    assert (
        result["considered_count"],
        result["disagreement_rate"],
        result["disagreement_now_kw"],
    ) == (0, None, None)


@pytest.mark.parametrize(
    ("actual", "closer", "anchor_err", "forecast_err"),
    [(17.875, "forecast", 16.095, 1.985), (2.10, "anchor", None, None)],
    ids=["the_1259_event", "the_other_answer"],
)
def test_once_reality_arrives_the_closer_of_the_two_is_recorded(
    state, actual, closer, anchor_err, forecast_err
):
    """The event that opened #1259: the forecast was closer by 8x, which is
    why the anchor is not blended forward. The reverse must be recordable
    too, or the instrument measures nothing."""
    _r, grid = _nowcast(NOON, 1.78, 15.89)
    state.readings["rows"] = [(grid[1], actual)]
    result, _ = _nowcast(grid[1], 15.89, 15.89)
    assert result["resolved_count"] == 1
    assert result[f"{closer}_closer_count"] == 1
    assert result["forecast_closer_fraction"] == (1.0 if closer == "forecast" else 0.0)
    if anchor_err is not None:
        assert result["mean_anchor_abs_error_kw"] == pytest.approx(anchor_err, abs=1e-3)
        assert result["mean_forecast_abs_error_kw"] == pytest.approx(
            forecast_err, abs=1e-3
        )
    assert state.read()["nowcast_pending"] == []


def test_with_no_reading_at_the_target_the_event_is_dropped_not_a_tie(state):
    _r, grid = _nowcast(NOON, 1.78, 15.89)
    state.readings["rows"] = []
    result, _ = _nowcast(grid[1], 15.89, 15.89)
    assert result["resolved_count"] == 0 and state.read()["nowcast_pending"] == []


def test_the_nowcast_window_rolls(state):
    stale = NOON - timedelta(hours=sw.SOLAR_NOWCAST_ROLLING_WINDOW_HOURS + 1)
    fresh = NOON - timedelta(minutes=30)
    state.write(
        {
            "pending": [],
            "ratios": [],
            "nowcast_pending": [],
            "nowcast_events": [
                {
                    "time": stale.isoformat(),
                    "anchor_abs_error_kw": 1.0,
                    "forecast_abs_error_kw": 9.0,
                },
                {
                    "time": fresh.isoformat(),
                    "anchor_abs_error_kw": 9.0,
                    "forecast_abs_error_kw": 1.0,
                },
            ],
            "nowcast_considered": [stale.isoformat(), fresh.isoformat()],
            "nowcast_disagreements": [stale.isoformat(), fresh.isoformat()],
        }
    )
    result, _ = _nowcast(NOON, 15.0, 15.0)
    assert (
        result["resolved_count"],
        result["forecast_closer_count"],
        result["anchor_closer_count"],
    ) == (1, 1, 0)
    assert (result["considered_count"], result["disagreement_count"]) == (2, 1)


def test_the_check_never_changes_the_forecast(state):
    grid = _grid(NOON)
    solar_kw = [1.78] + [15.89] * 47
    before = list(solar_kw)
    sw.update_solar_nowcast_disagreement(NOWCAST, NOON, grid, solar_kw)
    assert solar_kw == before


# --- the delivery ratio (#128) -----------------------------------------------------------------

RATIO = {"solver_solar_power_sensor": SENSOR}


def _ratio(now, forecast_kw=10.0):
    return sw.update_solar_delivery_ratio(
        RATIO, now, _grid(now, minutes=15), [forecast_kw] * 48
    )


def test_without_a_pv_sensor_there_is_no_ratio_and_nothing_written(state):
    assert sw.update_solar_delivery_ratio({}, NOON, _grid(NOON), [10.0] * 48) is None
    assert state.empty()


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


@pytest.mark.parametrize(
    ("ratios", "kept"),
    [
        ([("now", 1121.05), ("now", 0.9)], 0.9),  # a unit mix-up's ratio is dropped
        ([("stale", 0.5), ("fresh", 0.9)], 0.9),  # the window rolls
    ],
)
def test_impossible_or_stale_ratios_are_dropped(state, ratios, kept):
    when = {
        "now": NOON,
        "stale": NOON - timedelta(hours=sw.SOLAR_DELIVERY_ROLLING_WINDOW_HOURS + 1),
        "fresh": NOON - timedelta(minutes=10),
    }
    state.write(
        {
            "pending": [],
            "ratios": [{"time": when[k].isoformat(), "ratio": r} for k, r in ratios],
        }
    )
    result = _ratio(NOON)
    assert result["solar_delivery_sample_count"] == 1
    assert result["solar_delivery_ratio"] == pytest.approx(kept, abs=1e-3)


# --- one state file, two measurements ---------------------------------------------------------


def test_each_measurement_leaves_the_others_records_alone(state):
    _nowcast(NOON, 1.78, 15.89)
    sw.update_solar_delivery_ratio(RATIO, NOON, _grid(NOON), [1.78] + [15.89] * 47)
    s = state.read()
    assert len(s["nowcast_pending"]) == 1 and len(s["nowcast_considered"]) == 1
    assert len(s["pending"]) == 1 and s["ratios"] == []
    _nowcast(NOON, 1.78, 15.89)
    assert len(state.read()["pending"]) == 1


def test_a_state_file_from_before_the_nowcast_check_still_loads(state):
    state.write(
        {
            "pending": [],
            "ratios": [
                {"time": (NOON - timedelta(minutes=10)).isoformat(), "ratio": 0.9}
            ],
        }
    )
    loaded = sw._load_solar_delivery_state()
    assert len(loaded["ratios"]) == 1
    assert loaded["nowcast_pending"] == [] and loaded["nowcast_events"] == []
