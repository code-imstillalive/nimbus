"""Is the price forecast fresh? (#1793) A source whose own refresh stamp
stops moving is stale after an hour, however often its entities update;
the oldest stamp among the configured sources decides, and LocalVolts' own
sibling sensors count too (#1837).

Covers what lived in #1793, #1825 and the price-freshness files.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from solver_inputs import price_freshness as pf

NOW = datetime(2026, 10, 16, 2, 30, tzinfo=UTC)


def _reader(stamps):
    def read(entity_id):
        if entity_id == "sensor.broken":
            raise OSError("unreadable")
        return {"attributes": stamps.get(entity_id, {})}

    return read


def _ago(minutes):
    return (NOW - timedelta(minutes=minutes)).isoformat()


def test_the_oldest_source_stamp_decides_and_an_hour_is_stale(monkeypatch, caplog):
    # test_the_oldest_source_stamp_decides_and_an_hour_is_stale
    # test_the_oldest_source_stamp_decides_and_an_hour_is_stale
    read = _reader(
        {
            "sensor.buy": {"source_updated": _ago(10)},
            "sensor.sell": {"lastUpdate": _ago(150)},
        }
    )
    out = pf.check(read, ["sensor.buy", "sensor.sell", None], NOW)
    assert out == {
        "stale": True,
        "source_updated": _ago(150),
        "age_minutes": 150.0,
        "stale_after_minutes": 60.0,
        "entity_id": "sensor.sell",
    }
    fresh = pf.check(
        _reader({"sensor.buy": {"source_updated": _ago(60)}}), ["sensor.buy"], NOW
    )
    assert fresh["stale"] is False  # exactly an hour is not yet stale

    # test_a_source_without_a_usable_stamp_says_nothing
    for attrs in [
        {},
        {"source_updated": "not a time"},
        {"source_updated": "2026-10-16T00:01:55"},
        {"source_updated": 5},
    ]:
        assert (
            pf.check(
                _reader({"sensor.buy": attrs}), ["sensor.buy", "sensor.broken"], NOW
            )
            is None
        )

    # test_localvolts_siblings_are_read_when_a_configured_source_is_localvolts
    lv = {
        "entities": ["sensor.lv_buy"],
        "stamp_siblings": ["sensor.lv_rate", 5, "sensor.lv_buy"],
    }
    read = _reader(
        {
            "sensor.lv_buy": {"source_updated": _ago(5)},
            "sensor.lv_rate": {"lastUpdate": _ago(90)},
        }
    )
    stamp, entity = pf.source_updated(read, ["sensor.lv_buy"], lv)
    assert entity == "sensor.lv_rate" and stamp.isoformat() == _ago(90)
    assert pf.lv_v2_siblings(["sensor.amber"], lv) == []
    assert pf.lv_v2_siblings(["sensor.lv_buy"], None) == []

    # test_a_failing_probe_never_breaks_the_solve
    caplog.clear()
    with monkeypatch.context() as mp:

        def boom(*a):
            raise RuntimeError("x")

        mp.setattr(pf, "source_updated", boom)
        assert pf.check(_reader({}), ["sensor.buy"], NOW) is None
        assert "price freshness check failed" in caplog.text

    # test_the_fallback_is_wholesale_plus_the_learned_markup_and_now_is_the_live_price
    # test_the_fallback_is_wholesale_plus_the_learned_markup_and_now_is_the_live_price
    grid = [NOW + timedelta(minutes=m) for m in (0, 5, 30)]
    aemo = [(NOW - timedelta(minutes=5), 0.10), (NOW + timedelta(minutes=30), 0.50)]
    bucket = _local(grid[1]).hour * 12 + _local(grid[1]).minute // 5
    out = pf.aemo_series(grid, aemo, {bucket: 0.05}, _local, live_price=0.08)
    assert out == pytest.approx([0.08, 0.15, 0.50])
    assert pf.aemo_series([], aemo, {}, _local) is None
    assert pf.aemo_series(grid, [], {}, _local) is None
    far = [(NOW + timedelta(hours=2), 0.1)]
    assert pf.aemo_series(grid, far, {}, _local) is None  # nothing near enough to hold

    # test_past_staleness_is_marked_per_period_and_written_as_windows
    grid = [NOW + timedelta(minutes=30 * i) for i in range(5)]
    rows = [
        [
            (grid[0], grid[0] - timedelta(minutes=5)),
            (grid[1], grid[1] - timedelta(minutes=90)),
            (grid[3], grid[3] - timedelta(minutes=2)),
        ],
        [],
    ]
    mask = pf.stale_period_mask(rows, grid)
    assert mask == [False, True, True, False, False]
    assert pf.stale_windows(grid, mask, timedelta(minutes=30), _local) == [
        "13:00-14:00"
    ]
    assert pf.stale_windows(
        grid, [False, False, False, False, True], timedelta(minutes=30), _local
    ) == ["14:30-15:00"]
    assert pf.parse_stamp(_ago(1)) == NOW - timedelta(minutes=1)


# --- the fallback series and the stale history ------------------------------------------------------


def _local(t):
    return t + timedelta(hours=10)
