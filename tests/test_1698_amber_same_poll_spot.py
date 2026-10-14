"""nimbus #1698 (Mark Purcell, on #1697): Amber Express's `detailedForecast`
carries `per_kwh` (retail) and `spot_per_kwh` (wholesale) for the SAME
interval in the SAME response. As the wholesale reference for the retail
markup (`compute_5min_offset`) it removes the cross-sensor timing problem
#1694/#1697 worked around: on 10 Oct the AEMO sensor posted the 19:05-19:10
price at 19:10:24, after that interval had ended.

Measured on the reference household (10 Oct): `spot_per_kwh` is 1.0998-1.1001
x AEMO RRP, i.e. GST-inclusive, so it is divided by 1.10. A first post can
carry the previous interval's spot; the last value per interval is kept.
`detailedForecast` is not recorded, so the pairs are collected each solve.
Row shapes below are copied from production's live Amber Express sensors
(14 Oct 20:45 AEST)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import price_intervals
import pytest
import solver_writer


def _row(end: datetime, spot: float, per_kwh: float = 0.357, duration: int = 5) -> dict:
    return {
        "per_kwh": per_kwh,
        "spot_per_kwh": spot,
        "duration": duration,
        # Amber's own start_time reads one second in
        "start_time": (end - timedelta(minutes=duration, seconds=-1)).isoformat(),
        "end_time": end.isoformat(),
        "estimate": False,
    }


END = datetime(2026, 10, 14, 10, 50, tzinfo=UTC)  # 20:50 AEST
NOW = END - timedelta(minutes=2)


# --- the pure parser ---------------------------------------------------------


def test_the_interval_in_progress_ex_gst():
    detailed = [_row(END, 0.1074139), _row(END + timedelta(minutes=5), 0.2)]
    start, spot = price_intervals.amber_spot_in_progress(detailed, NOW)
    assert start == END - timedelta(minutes=5)
    assert spot == pytest.approx(0.1074139 / 1.10)


def test_the_boundary_belongs_to_the_next_interval():
    detailed = [_row(END, 0.1), _row(END + timedelta(minutes=5), 0.2)]
    start, spot = price_intervals.amber_spot_in_progress(detailed, END)
    assert start == END
    assert spot == pytest.approx(0.2 / 1.10)


@pytest.mark.parametrize(
    "detailed",
    [
        None,
        "x",
        [],
        [{"spot_per_kwh": 0.1}],
        [_row(END, float("nan"))],
        [{**_row(END, 0.1), "spot_per_kwh": None}],
        [_row(END - timedelta(minutes=10), 0.1)],  # not in progress
    ],
)
def test_nothing_usable_is_none(detailed):
    assert price_intervals.amber_spot_in_progress(detailed, NOW) is None


# --- collection --------------------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "amber_spot.json"
    monkeypatch.setattr(solver_writer, "AMBER_SPOT_PATH", str(path))
    monkeypatch.setattr(solver_writer, "_AMBER_SPOT_CACHE", None)
    return path


def _sensor(*rows):
    return {"state": "0.357", "attributes": {"detailedForecast": list(rows)}}


def test_the_settled_post_replaces_the_provisional_one(store):
    key = int((END - timedelta(minutes=5)).timestamp()) // 300
    with patch.object(solver_writer, "ha_get", lambda _e: _sensor(_row(END, 0.153989))):
        solver_writer.collect_amber_spot(["sensor.amber_general"], NOW)
    with patch.object(
        solver_writer, "ha_get", lambda _e: _sensor(_row(END, 0.1514426))
    ):
        out = solver_writer.collect_amber_spot(["sensor.amber_general"], NOW)
    assert out[key][1] == pytest.approx(0.1514426 / 1.10)
    assert out[key][0] == END - timedelta(minutes=5)
    on_disk = json.loads(store.read_text())
    assert on_disk == {str(key): pytest.approx(0.1514426 / 1.10)}


def test_a_sensor_without_amber_rows_falls_through_to_the_next(store):
    states = {
        "sensor.localvolts_buy": {"state": "0.2", "attributes": {"forecast": []}},
        "sensor.amber_feed_in": _sensor(_row(END, 0.1074139)),
    }
    with patch.object(solver_writer, "ha_get", lambda e: states[e]):
        out = solver_writer.collect_amber_spot(
            ["sensor.localvolts_buy", None, "sensor.amber_feed_in"], NOW
        )
    assert len(out) == 1


def test_an_install_without_amber_collects_nothing(store):
    with patch.object(
        solver_writer, "ha_get", lambda _e: {"state": "0.2", "attributes": {}}
    ):
        assert solver_writer.collect_amber_spot(["sensor.lv_buy"], NOW) == {}
    assert not store.exists()


def test_a_failed_read_never_raises(store):
    def boom(_e):
        raise OSError("down")

    with patch.object(solver_writer, "ha_get", boom):
        assert solver_writer.collect_amber_spot(["sensor.amber_general"], NOW) == {}


def test_intervals_older_than_five_days_are_dropped(store):
    old_key = int((END - timedelta(days=6)).timestamp()) // 300
    store.write_text(json.dumps({str(old_key): 0.05}))
    with patch.object(solver_writer, "ha_get", lambda _e: _sensor(_row(END, 0.11))):
        out = solver_writer.collect_amber_spot(["sensor.amber_general"], NOW)
    assert old_key not in out
    assert len(out) == 1


# --- the markup ----------------------------------------------------------------


def test_the_same_poll_spot_wins_over_a_late_aemo_post():
    # One interval, 19:05-19:10 AEST. Retail settled 0.40. AEMO's sensor posted
    # the PREVIOUS interval's 0.1357 inside it; Amber's same poll says 0.1697.
    start = datetime(2026, 10, 10, 9, 5, tzinfo=UTC)
    retail = [(start + timedelta(seconds=20), 0.40)]
    aemo = [(start + timedelta(seconds=60), 0.1357)]
    same_poll = {int(start.timestamp()) // 300: (start, 0.1697)}
    bucket = 19 * 12 + 1
    with patch.object(solver_writer, "fetch_price_history", lambda *_a, **_k: aemo):
        without = solver_writer.compute_5min_offset(
            retail, regional_spot_sensor="sensor.aemo"
        )
        with_it = solver_writer.compute_5min_offset(
            retail, regional_spot_sensor="sensor.aemo", same_poll_wholesale=same_poll
        )
    assert without[bucket] == pytest.approx(0.40 - 0.1357)
    assert with_it[bucket] == pytest.approx(0.40 - 0.1697)


def test_an_amber_only_install_needs_no_aemo_sensor():
    start = datetime(2026, 10, 10, 9, 5, tzinfo=UTC)
    retail = [(start + timedelta(seconds=20), 0.40)]
    same_poll = {int(start.timestamp()) // 300: (start, 0.15)}
    with patch.object(solver_writer, "fetch_price_history", lambda *_a, **_k: []):
        out = solver_writer.compute_5min_offset(retail, same_poll_wholesale=same_poll)
    assert out == {19 * 12 + 1: pytest.approx(0.25)}


def test_without_amber_nothing_changes():
    start = datetime(2026, 10, 10, 9, 5, tzinfo=UTC)
    retail = [(start + timedelta(seconds=20), 0.40)]
    aemo = [(start + timedelta(seconds=30), 0.15)]
    with patch.object(solver_writer, "fetch_price_history", lambda *_a, **_k: aemo):
        a = solver_writer.compute_5min_offset(
            retail, regional_spot_sensor="sensor.aemo"
        )
        b = solver_writer.compute_5min_offset(
            retail, regional_spot_sensor="sensor.aemo", same_poll_wholesale={}
        )
    assert a == b
    assert solver_writer.compute_5min_offset(retail) == {}
