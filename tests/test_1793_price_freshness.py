"""nimbus #1793 (Mark Purcell): a price source whose rows have not been
rebuilt for over an hour is not planned on.

16 Oct 2026, the reference household: LocalVolts' rows carried
`lastUpdate` 2026-10-16T00:01:55Z from 10:01 AEST for more than two and a
half hours while every poll succeeded. Over the 30 hours before, the stamp
was 0-28.4 minutes old (median 0.9).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
from _ha_stubs import install_ha_stubs

install_ha_stubs()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from custom_components.nimbus_load.solver_inputs import price_freshness as pf

AEST = timezone(timedelta(hours=10))
NOW = datetime(2026, 10, 16, 2, 5, tzinfo=UTC)  # 12:05 AEST
STAMP = "2026-10-16T00:01:55Z"


def _reader(states):
    def read(entity_id):
        if entity_id not in states:
            raise KeyError(entity_id)
        return {"attributes": states[entity_id]}

    return read


LV2 = (
    "sensor.localvolts_v2_flex_up_forecast",
    "sensor.localvolts_v2_buy_flex_up",
    "sensor.localvolts_v2_sell_flex_up",
)


def test_16_oct_lv_v2_before_53_reads_the_sibling_stamp():
    """LV v2 before purcell-lab/localvolts_v2#53: the flex-up sensors carry
    no stamp; `current_buy_rate` does."""
    read = _reader(
        {
            LV2[0]: {"forecast": []},
            LV2[1]: {"last_update": "2026-10-16T02:03:00Z"},  # fetch time, ignored
            LV2[2]: {},
            "sensor.localvolts_v2_current_buy_rate": {"lastUpdate": STAMP},
        }
    )
    got = pf.check(read, LV2, NOW)
    assert got["stale"] is True
    assert got["entity_id"] == "sensor.localvolts_v2_current_buy_rate"
    assert got["age_minutes"] == 123.1


def test_source_updated_is_preferred_and_the_newest_stamp_wins():
    read = _reader(
        {
            LV2[0]: {"source_updated": "2026-10-16T01:32:00+00:00"},
            LV2[1]: {"lastUpdate": STAMP},
            LV2[2]: {},
        }
    )
    got = pf.check(read, LV2, NOW)
    assert got["stale"] is False
    assert got["entity_id"] == LV2[0]
    assert got["age_minutes"] == 33.0


def test_household_template_sensor_lastupdate():
    read = _reader({"sensor.localvolts_costs_flex_up": {"lastUpdate": STAMP}})
    got = pf.check(read, ["sensor.localvolts_costs_flex_up"], NOW)
    assert got["stale"] is True


def test_normal_ages_are_not_stale():
    """The worst normal age measured on 15-16 Oct was 28.4 minutes."""
    for minutes in (0.9, 12.7, 28.4, 59.9):
        stamp = (NOW - timedelta(minutes=minutes)).isoformat()
        got = pf.check(_reader({"sensor.x": {"lastUpdate": stamp}}), ["sensor.x"], NOW)
        assert got["stale"] is False, minutes


def test_no_stamp_anywhere_is_unchanged_behaviour():
    """An Amber or other install whose provider publishes no refresh stamp."""
    read = _reader({"sensor.amber_general_price": {"forecast": []}})
    assert pf.check(read, ["sensor.amber_general_price", None], NOW) is None


def test_unreadable_entities_never_raise():
    assert pf.check(_reader({}), LV2, NOW) is None
    assert (
        pf.check(_reader({"sensor.x": {"lastUpdate": "garbage"}}), ["sensor.x"], NOW)
        is None
    )
    assert (
        pf.check(
            _reader({"sensor.x": {"lastUpdate": "2026-10-16T00:01:55"}}),
            ["sensor.x"],
            NOW,
        )
        is None
    )


def _local(t):
    return t.astimezone(AEST)


def _grid(start, n, step_min=5):
    return [start + timedelta(minutes=step_min * i) for i in range(n)]


def test_aemo_series_prices_every_period_from_aemo_plus_markup():
    grid = _grid(datetime(2026, 10, 16, 2, 7, tzinfo=UTC), 4)
    aemo = [
        (datetime(2026, 10, 16, 2, 0, tzinfo=UTC), 0.06),
        (datetime(2026, 10, 16, 2, 15, tzinfo=UTC), 0.08),
    ]
    markup = {_local(t).hour * 12 + _local(t).minute // 5: 0.03 for t in grid}
    out = pf.aemo_series(grid, aemo, markup, _local)
    assert out == [0.09, 0.09, 0.11, 0.11]


def test_aemo_series_current_interval_takes_the_live_price():
    grid = [
        datetime(2026, 10, 16, 2, 7, tzinfo=UTC),
        datetime(2026, 10, 16, 2, 8, tzinfo=UTC),
    ] + _grid(datetime(2026, 10, 16, 2, 10, tzinfo=UTC), 2)
    aemo = [(datetime(2026, 10, 16, 2, 0, tzinfo=UTC), 0.06)]
    out = pf.aemo_series(grid, aemo, {}, _local, live_price=0.0529)
    assert out == [0.0529, 0.0529, 0.06, 0.06]


def test_aemo_series_without_aemo_keeps_the_caller_data():
    grid = _grid(datetime(2026, 10, 16, 2, 10, tzinfo=UTC), 3)
    assert pf.aemo_series(grid, [], {}, _local) is None
    late = [(datetime(2026, 10, 16, 4, 0, tzinfo=UTC), 0.06)]
    assert pf.aemo_series(grid, late, {}, _local) is None


def test_prices_wiring_falls_back_only_when_stale():
    src = (Path(pf.__file__).parent / "prices.py").read_text(encoding="utf-8")
    at = src.index("price_freshness.check(")
    block = src[at : at + 2400]
    assert 'price_source_freshness["stale"]' in block
    assert "price_freshness.aemo_series(" in block
    assert "import_real_mask = [False] * len(grid_times)" in block


def test_main_keeps_the_aemo_price_for_the_current_block_and_relabels():
    src = (Path(pf.__file__).parent.parent / "solver_writer.py").read_text(
        encoding="utf-8"
    )
    assert (
        "if price_source_on_aemo:\n        _settled_import_value = spot_import_raw[0]"
        in src
    )
    assert "price_freshness.FALLBACK_LABEL" in src
    assert "price_source_freshness=price_source_freshness," in src
