"""nimbus #1825: keep stale price-source data out of scoring.

On 16 Oct 2026 the reference household's LocalVolts rows stopped being
rebuilt at 10:01 AEST and carried a withdrawn pre-dispatch spike until
13:01. The Solver stopped planning on them (#1793), but the next morning's
quality report scored the day against them as if they were real: EPR 43.4%,
$28.93 regret, $33 of it from 12:00-13:00, for money that never existed.

The fixture below has 16 Oct's shape, measured on the reference install's
recorder: the current-rate stamp refreshes every 5 minutes, then from 10:35
each recorded row carries the 10:01 stamp until 13:01, when a fresh row
lands. In the evening the buy rate holds one value for hours and the
recorder keeps no row at all, which is the trap: an age measured from that
last row at the period being scored read 17:35-20:15 and 21:05-23:55 as
stale while the source was fine.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _solver_path  # noqa: F401
import solver_writer as sw

quality = sys.modules[sw.publish_daily_quality_report.__module__]
pf = sw.price_freshness

TZ = sw.LOCAL_TZ
DAY = datetime(2026, 10, 16, tzinfo=TZ)
HALF_HOUR = timedelta(minutes=30)
GRID = [DAY + i * HALF_HOUR for i in range(48)]


def _at(hh: int, mm: int, ss: int = 0) -> datetime:
    return DAY.replace(hour=hh, minute=mm, second=ss)


def _sixteen_october() -> list[tuple[datetime, datetime]]:
    """(recorded at, stamp) rows shaped like 16 Oct's current-rate sensor."""
    rows = []
    t = _at(0, 0, 30)
    while t < _at(10, 35):
        rows.append((t, t - timedelta(seconds=25)))
        t += timedelta(minutes=5)
    t = _at(10, 35, 52)
    while t < _at(13, 1):
        rows.append((t, _at(10, 1, 48)))  # the stall: same stamp every row
        t += timedelta(minutes=5)
    t = _at(13, 1, 33)
    while t < _at(17, 0):
        rows.append((t, t - timedelta(seconds=40)))
        t += timedelta(minutes=5)
    # Evening: one fresh row, then the state holds and nothing is recorded.
    rows.append((_at(17, 0, 12), _at(16, 33, 19)))
    rows.append((_at(20, 15, 18), _at(20, 2, 57)))
    return rows


def _stale_labels(mask: list[bool]) -> list[str]:
    return [f"{GRID[i]:%H:%M}" for i, flag in enumerate(mask) if flag]


class TestTheStaleWindowIsFound(unittest.TestCase):
    def test_sixteen_october_is_stale_across_the_spike_and_nowhere_else(self):
        mask = pf.stale_period_mask([_sixteen_october()], GRID)
        self.assertEqual(_stale_labels(mask), ["11:30", "12:00", "12:30", "13:00"])

    def test_a_held_state_in_the_evening_is_not_stale(self):
        """The age is read when the row was recorded. Read at the period
        instead, 17:00's row is three hours old by 20:00."""
        mask = pf.stale_period_mask([_sixteen_october()], GRID)
        evening = [i for i, gt in enumerate(GRID) if gt.hour >= 17]
        self.assertFalse(any(mask[i] for i in evening))

    def test_the_oldest_stamp_across_entities_decides(self):
        fresh = [(gt, gt - timedelta(minutes=1)) for gt in GRID]
        mask = pf.stale_period_mask([fresh, _sixteen_october()], GRID)
        self.assertEqual(_stale_labels(mask), ["11:30", "12:00", "12:30", "13:00"])

    def test_no_stamps_means_nothing_is_stale(self):
        self.assertEqual(pf.stale_period_mask([], GRID), [False] * len(GRID))
        self.assertEqual(pf.stale_period_mask([[]], GRID), [False] * len(GRID))

    def test_the_threshold_is_the_solvers_own(self):
        """Exactly an hour old is fresh; a second more is stale."""
        t = _at(12, 0)
        exactly = pf.stale_period_mask([[(t, t - pf.STALE_AFTER)]], [t])
        over = pf.stale_period_mask(
            [[(t, t - pf.STALE_AFTER - timedelta(seconds=1))]], [t]
        )
        self.assertEqual((exactly, over), ([False], [True]))

    def test_windows_are_named_in_local_time(self):
        mask = pf.stale_period_mask([_sixteen_october()], GRID)
        self.assertEqual(
            pf.stale_windows(GRID, mask, HALF_HOUR, sw._local), ["11:30-13:30"]
        )
        last = [False] * 47 + [True]
        self.assertEqual(
            pf.stale_windows(GRID, last, HALF_HOUR, sw._local), ["23:30-00:00"]
        )

    def test_stamps_parse_as_the_recorder_stores_them(self):
        self.assertEqual(
            pf.parse_stamp("2026-10-16T00:01:48Z"),
            datetime.fromisoformat("2026-10-16T00:01:48+00:00"),
        )
        self.assertIsNone(pf.parse_stamp("2026-10-16T00:01:48"))  # naive
        self.assertIsNone(pf.parse_stamp(12.5))


def _fake_sw(stamp_rows, aemo_hist, offset=0.04, fee=0.10):
    """The solver_writer surface _reprice_stale_price_periods() reads."""
    calls = []

    def attribute_history(entity_id, attribute, start, end, convert=float):
        calls.append((entity_id, attribute))
        return stamp_rows.get((entity_id, attribute), [])

    return SimpleNamespace(
        price_freshness=pf,
        fetch_entity_attribute_history_range=attribute_history,
        fetch_entity_history_range=lambda *_a, **_k: aemo_hist,
        resample_history_nearest=sw.resample_history_nearest,
        compute_5min_offset=lambda hist, **_k: {b: offset for b in range(288)},
        fetch_price_history=lambda *_a, **_k: [(DAY, 0.1)],
        import_fee_rate=lambda _cfg, _hour: fee,
        _local=sw._local,
        calls=calls,
    ), calls


# nimbus #1994: LocalVolts v2 as the entity registry finds it, which is how
# the scoring tells a LocalVolts price apart and finds its refresh stamp.
LV_STAMP_SIBLINGS = [
    "sensor.localvolts_v2_current_buy_rate",
    "sensor.localvolts_v2_current_sell_rate",
]
CFG = {
    "solver_import_price_sensor": "sensor.localvolts_v2_buy_flex_up",
    "solver_export_price_sensor": "sensor.localvolts_v2_sell_flex_up",
    "solver_regional_spot_current_price_sensor": "sensor.aemo_qld1_price",
    "known_sources": {
        "localvolts_v2": {
            "entities": [
                "sensor.localvolts_v2_buy_flex_up",
                "sensor.localvolts_v2_sell_flex_up",
                *LV_STAMP_SIBLINGS,
            ],
            "stamp_siblings": LV_STAMP_SIBLINGS,
        }
    },
}
SIBLING = ("sensor.localvolts_v2_current_buy_rate", "lastUpdate")


def _reprice(fake, cfg=CFG):
    imp = np.full(len(GRID), 0.30)
    exp = np.full(len(GRID), 0.20)
    imp[23:27] = [0.36, 1.12, 1.12, 10.5]  # the spike, as scored on 16 Oct
    result = quality._reprice_stale_price_periods(
        fake, cfg, GRID, 0.5, DAY, DAY + timedelta(days=1), imp, exp
    )
    return result, imp, exp


class TestStalePeriodsAreRepriced(unittest.TestCase):
    def test_the_spike_is_repriced_from_aemo_and_the_rest_is_untouched(self):
        fake, _ = _fake_sw({SIBLING: _sixteen_october()}, [(DAY, 0.05)])
        result, imp, exp = _reprice(fake)
        stale = [23, 24, 25, 26]
        np.testing.assert_allclose(imp[stale], 0.05 + 0.04 + 0.10)
        np.testing.assert_allclose(exp[stale], 0.05 + 0.04)
        others = [i for i in range(48) if i not in stale]
        np.testing.assert_allclose(imp[others], 0.30)
        np.testing.assert_allclose(exp[others], 0.20)
        self.assertEqual(result["price_stale_periods"], 4)
        self.assertEqual(result["price_stale_minutes"], 120)
        self.assertEqual(result["price_stale_windows"], ["11:30-13:30"])
        self.assertTrue(result["price_stale_repriced"])
        self.assertFalse(result["price_stale_unusable"])

    def test_the_lv_v2_siblings_are_read_as_the_solver_reads_them(self):
        fake, calls = _fake_sw({SIBLING: _sixteen_october()}, [(DAY, 0.05)])
        _reprice(fake)
        read = {entity for entity, _attr in calls}
        self.assertTrue(set(LV_STAMP_SIBLINGS) <= read)

    def test_no_aemo_history_means_the_day_is_not_a_measurement(self):
        fake, _ = _fake_sw({SIBLING: _sixteen_october()}, [])
        result, imp, _ = _reprice(fake)
        self.assertTrue(result["price_stale_unusable"])
        self.assertFalse(result["price_stale_repriced"])
        self.assertEqual(imp[26], 10.5)  # left as it was, and flagged

    def test_more_than_a_quarter_of_the_day_stale_is_not_a_measurement(self):
        start = _at(6, 0)
        frozen = [
            (start + timedelta(minutes=5 * k), start - timedelta(hours=2))
            for k in range(12 * 7)  # seven hours of rows on a 2-hour-old stamp
        ]
        fake, _ = _fake_sw({SIBLING: frozen}, [(DAY, 0.05)])
        result, _, _ = _reprice(fake)
        self.assertGreater(result["price_stale_periods"], 12)
        self.assertTrue(result["price_stale_repriced"])
        self.assertTrue(result["price_stale_unusable"])

    def test_a_fresh_day_changes_nothing(self):
        fresh = [(gt, gt - timedelta(minutes=2)) for gt in GRID]
        fake, _ = _fake_sw({SIBLING: fresh}, [(DAY, 0.05)])
        result, imp, _ = _reprice(fake)
        self.assertEqual(result["price_stale_periods"], 0)
        self.assertEqual(imp[26], 10.5)

    def test_a_failing_check_never_stops_the_day_being_scored(self):
        def boom(*_a, **_k):
            raise RuntimeError("recorder gone")

        fake, _ = _fake_sw({}, [])
        fake.fetch_entity_attribute_history_range = boom
        with self.assertLogs(quality.solver_shared._LOGGER, "WARNING"):
            result, imp, _ = _reprice(fake)
        self.assertEqual(result["price_stale_periods"], 0)
        self.assertEqual(imp[26], 10.5)


class TestTheVerdict(unittest.TestCase):
    def test_an_unusable_price_day_is_not_reliable(self):
        self.assertFalse(sw._epr_reliability(True, True, None, None, True))
        self.assertTrue(sw._epr_reliability(True, True, None, None, False))
        self.assertTrue(sw._epr_reliability(True, True))  # older callers

    def test_the_row_names_the_price_source_first(self):
        entry = {
            "epr_reliable": False,
            "regret_reliable": False,  # a consequence of the wrong prices
            "price_stale_unusable": True,
        }
        self.assertEqual(sw._epr_reliability_code(entry), sw._RELIABILITY_PRICE)

    def test_the_card_has_a_sentence_for_it(self):
        card = (
            Path(sw.__file__).resolve().parent / "frontend" / "nimbus-regret-card.js"
        ).read_text(encoding="utf-8")
        self.assertIn(f'code === "{sw._RELIABILITY_PRICE}"', card)
        self.assertIn('reason === "price_source_stale"', card)
        self.assertIn("attrs.price_stale_repriced === true", card)


class TestTheScorerUsesIt(unittest.TestCase):
    def test_prices_are_repriced_before_anything_is_priced_on_them(self):
        import inspect

        src = inspect.getsource(quality._compute_report_for_window)
        self.assertLess(
            src.index("_reprice_stale_price_periods("),
            src.index("grid_residual = "),
        )
        self.assertIn('price_stale["price_stale_unusable"],\n        ),', src)


if __name__ == "__main__":
    unittest.main()
