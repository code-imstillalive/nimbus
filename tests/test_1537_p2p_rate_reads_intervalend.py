"""nimbus #1537: the P2P matched-rate field reads LocalVolts v2's rows.

purcell-lab/localvolts_v2 >= 2.7.0 publishes Current Sell Rate forecast rows
carrying `matchedCost`, `volume` and `proportionP2P`, with the interval end
keyed as `intervalEnd`. The reference household's own sensor
(`sensor.localvolts_p2p_forecast`) carries the same fields with the interval
end renamed to `time`. `resample_real_p2p_rate` must give the same rate for
both, and must not change for a `time` row.

The real evening row below is from the reference household's production
sensor, 17:00-17:05 AEST 5 Oct 2026: 0.268861 / (0.9368 x 0.569915) =
$0.5036/kWh.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import _solver_path  # noqa: F401
import solver_writer

REAL = {"proportionP2P": 0.569915, "matchedCost": 0.268861, "volume": 0.9368}
END = "2026-10-05T07:05:00Z"  # interval 17:00-17:05 AEST
GRID = [
    datetime(2026, 10, 5, 7, 0, tzinfo=UTC) + timedelta(minutes=5 * i) for i in range(3)
]


def _rates(rows):
    with patch.object(
        solver_writer, "ha_get", return_value={"attributes": {"forecast": rows}}
    ):
        return solver_writer.resample_real_p2p_rate(GRID, "sensor.p2p")


class P2PRateReadsIntervalEnd(unittest.TestCase):
    def test_time_rows_unchanged(self) -> None:
        rates = _rates([{"time": END, **REAL}])
        self.assertAlmostEqual(rates[0], 0.268861 / (0.9368 * 0.569915), places=6)
        self.assertAlmostEqual(rates[0], 0.5036, places=4)

    def test_intervalend_rows_give_the_same_rate(self) -> None:
        self.assertEqual(
            _rates([{"intervalEnd": END, **REAL}]), _rates([{"time": END, **REAL}])
        )

    def test_time_wins_when_both_present(self) -> None:
        other = "2026-10-05T08:05:00Z"
        self.assertEqual(
            _rates([{"time": END, "intervalEnd": other, **REAL}]),
            _rates([{"time": END, **REAL}]),
        )

    def test_row_with_neither_key_is_skipped_not_fatal(self) -> None:
        self.assertEqual(_rates([{**REAL}]), [0.0, 0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
