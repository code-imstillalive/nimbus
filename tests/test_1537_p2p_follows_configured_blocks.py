"""nimbus #1537 items 3 and 5: P2P follows the household's own blocks.

Two things were this repository's reference household hardcoded into every
install:

* `resample_real_p2p_rate` counted a matched rate only inside 17:00-24:00
  local, so a household whose blocks sat anywhere else had its P2P rate
  zeroed inside its own blocks.
* With no settlement-history sensor (LocalVolts v2 publishes none yet,
  purcell-lab/localvolts_v2#40), the P2P volume cap fell back to that
  household's 60 kWh. On the generic branch, the wizard's default volume
  of 0 let the LP claim no P2P volume at all.

A block is a fixed commitment (a locked window at a locked kW, every day),
so the blocks themselves are the window and their energy is the volume.
"""

from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_shared
import solver_writer
from solver_inputs import prices

# The reference household's evening row (17:00-17:05 AEST, 5 Oct 2026),
# $0.5036/kWh; see test_1537_p2p_rate_reads_intervalend.py.
REAL = {"proportionP2P": 0.569915, "matchedCost": 0.268861, "volume": 0.9368}
RATE = 0.268861 / (0.9368 * 0.569915)


def _grid(start_utc: datetime, n: int) -> list[datetime]:
    return [start_utc + timedelta(minutes=5 * i) for i in range(n)]


def _row_ending(t: datetime) -> dict:
    return {"intervalEnd": (t + timedelta(minutes=5)).isoformat(), **REAL}


def _rates(grid, window):
    rows = [_row_ending(t) for t in grid]
    with patch.object(
        solver_writer, "ha_get", return_value={"attributes": {"forecast": rows}}
    ):
        return solver_writer.resample_real_p2p_rate(grid, "sensor.p2p", window)


class RateFollowsTheBlocks(unittest.TestCase):
    # 11:00-11:15 AEST: outside 17:00-24:00.
    MIDDAY = _grid(datetime(2026, 10, 6, 1, 0, tzinfo=UTC), 3)
    # 17:00-17:15 AEST.
    EVENING = _grid(datetime(2026, 10, 6, 7, 0, tzinfo=UTC), 3)

    def test_a_block_outside_17_to_24_now_gets_its_rate(self) -> None:
        rates = _rates(self.MIDDAY, [5.0, 5.0, 5.0])
        for r in rates:
            self.assertAlmostEqual(r, RATE, places=6)

    def test_outside_the_blocks_is_zero_even_in_the_evening(self) -> None:
        nan = float("nan")
        self.assertEqual(_rates(self.EVENING, [nan, nan, nan]), [0.0, 0.0, 0.0])

    def test_a_partial_window_is_gated_per_period(self) -> None:
        rates = _rates(self.MIDDAY, [float("nan"), 5.0, 0.0])
        self.assertEqual(rates[0], 0.0)
        self.assertAlmostEqual(rates[1], RATE, places=6)
        self.assertEqual(rates[2], 0.0)  # post-midnight self-consume pin

    def test_without_blocks_the_reference_window_is_unchanged(self) -> None:
        self.assertEqual(_rates(self.MIDDAY, None), [0.0, 0.0, 0.0])
        for r in _rates(self.EVENING, None):
            self.assertAlmostEqual(r, RATE, places=6)

    def test_a_window_of_the_wrong_length_is_ignored_not_misaligned(self) -> None:
        self.assertEqual(_rates(self.MIDDAY, [5.0]), [0.0, 0.0, 0.0])


def _blocks(*blocks) -> dict:
    cfg: dict = {}
    for n, (rate, start, end) in enumerate(blocks, 1):
        cfg[f"solver_p2p_block_{n}_rate_kw"] = rate
        cfg[f"solver_p2p_block_{n}_start_hour"] = start
        cfg[f"solver_p2p_block_{n}_end_hour"] = end
    return cfg


class BlocksDailyEnergy(unittest.TestCase):
    def test_reference_household(self) -> None:
        self.assertAlmostEqual(
            solver_shared.p2p_blocks_daily_energy_kwh(_blocks((11.5, 17, 24))), 80.5
        )

    def test_three_blocks_sum(self) -> None:
        # 14 x 3 + 4 x 1 + 17 x 3, the tester's figure on #1537.
        cfg = _blocks((14.0, 14, 17), (4.0, 17, 18), (17.0, 18, 21))
        self.assertAlmostEqual(solver_shared.p2p_blocks_daily_energy_kwh(cfg), 97.0)

    def test_unconfigured_and_invalid_blocks_count_nothing(self) -> None:
        self.assertEqual(solver_shared.p2p_blocks_daily_energy_kwh({}), 0.0)
        cfg = _blocks((0.0, 17, 24), (5.0, 20, 18))
        self.assertEqual(solver_shared.p2p_blocks_daily_energy_kwh(cfg), 0.0)

    def test_lead_time_does_not_change_the_committed_energy(self) -> None:
        cfg = {**_blocks((11.5, 17, 24)), "solver_p2p_block_lead_time_minutes": 1}
        self.assertAlmostEqual(solver_shared.p2p_blocks_daily_energy_kwh(cfg), 80.5)


GRID = _grid(datetime(2026, 10, 6, 7, 0, tzinfo=UTC), 3)
N = len(GRID)


def _generic(cfg):
    with (
        patch.object(
            solver_writer,
            "resample_generic_price_forecast_with_coverage",
            return_value=([0.1] * N, [True] * N),
        ),
    ):
        return prices.build_price_arrays(
            {
                "solver_import_price_sensor": "sensor.i",
                "solver_export_price_sensor": "sensor.e",
                **cfg,
            },
            GRID,
            N,
            GRID[0],
            False,
            None,
        )


class GenericBranchVolume(unittest.TestCase):
    def test_zero_volume_with_blocks_becomes_the_blocks_energy(self) -> None:
        out = _generic(_blocks((11.5, 17, 24)))
        self.assertAlmostEqual(out.p2p_recent_volume_kwh, 80.5)

    def test_a_configured_volume_is_honoured(self) -> None:
        out = _generic({**_blocks((11.5, 17, 24)), "solver_p2p_bonus_volume_kwh": 40})
        self.assertEqual(out.p2p_recent_volume_kwh, 40.0)

    def test_no_blocks_no_volume_stays_zero(self) -> None:
        self.assertEqual(_generic({}).p2p_recent_volume_kwh, 0.0)


def _primary(cfg, recent_volume=60.0):
    calls: dict = {}

    def fake_rate(grid, sensor_id=None, window=None):
        calls["window"] = window
        return [0.5] * len(grid)

    with patch.multiple(
        solver_writer,
        ha_get=lambda _e: {"attributes": {"forecast": []}},
        fetch_aemo_forecast=lambda _e: None,
        fetch_price_history=lambda *_a, **_k: [],
        compute_5min_offset=lambda *_a, **_k: {},
        compute_price_percentile_band=lambda *_a, **_k: {},
        resample_price_with_extrapolation=lambda *_a, **_k: ([0.1] * N, [True] * N),
        resample_real_p2p_rate=fake_rate,
        check_aemo_p5min_disagreement=lambda *_a, **_k: None,
        p2p_match_fraction=lambda **_k: 0.65,
        p2p_recent_avg_volume_kwh=lambda **_k: recent_volume,
    ):
        out = prices.build_price_arrays(
            {
                "solver_import_price_sensor": "sensor.i",
                "solver_export_price_sensor": "sensor.e",
                **cfg,
            },
            GRID,
            N,
            GRID[0],
            True,
            "sensor.array",
        )
    return out, calls


class PriceArrayBranch(unittest.TestCase):
    def test_the_blocks_reach_the_rate_as_its_window(self) -> None:
        _out, calls = _primary(_blocks((11.5, 17, 24)))
        self.assertEqual(calls["window"], [11.5, 11.5, 11.5])

    def test_no_blocks_passes_no_window(self) -> None:
        _out, calls = _primary({})
        self.assertIsNone(calls["window"])

    def test_no_settlement_sensor_uses_the_blocks_energy(self) -> None:
        out, _ = _primary(_blocks((11.5, 17, 24)))
        self.assertAlmostEqual(out.p2p_recent_volume_kwh, 80.5)

    def test_a_settlement_sensor_still_decides_the_volume(self) -> None:
        cfg = {
            **_blocks((11.5, 17, 24)),
            "solver_p2p_settlement_history_sensor": "sensor.hist",
        }
        out, _ = _primary(cfg, recent_volume=47.7)
        self.assertEqual(out.p2p_recent_volume_kwh, 47.7)

    def test_no_blocks_and_no_sensor_keeps_the_old_fallback(self) -> None:
        out, _ = _primary({}, recent_volume=60.0)
        self.assertEqual(out.p2p_recent_volume_kwh, 60.0)

    def test_bonus_is_the_premium_over_spot_only_inside_the_window(self) -> None:
        out, _ = _primary(_blocks((11.5, 17, 24)))
        for b in out.export_bonus_price:
            self.assertTrue(math.isclose(b, 0.4))


if __name__ == "__main__":
    unittest.main()
