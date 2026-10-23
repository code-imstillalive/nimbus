"""nimbus issue #1807: calibrated solar p10/p90 beside the envelope.

`SolarConfig.lower_kw`/`upper_kw` are an envelope (min/max across sources plus
half their spread). #1805's stochastic scenarios need a p10/p90 instead. This
pins `solver_inputs/solar_quantiles.py`:

- the bands are monotone (p10 <= p50 <= p90) and carry no spread at night or
  in period 0 (the live anchor);
- the empirical path applies per-lead ratio quantiles to the p50;
- the Solcast path is used while the history is too short;
- with neither, both fields are None;
- the history records one forecast per interval and lead bucket, grades it
  against the PV sensor's time-weighted mean, and prunes;
- the calibration figures (leave-one-day-out coverage) are computed right;
- SolarConfig validates the new pair, and main() wiring publishes it.

Synthetic data only.
"""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta

import _solver_path  # noqa: F401
import numpy as np
import solver_writer
from solver import elements
from solver_inputs import solar as solar_inputs
from solver_inputs import solar_quantiles as sq

TZ = solver_writer.LOCAL_TZ
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=TZ)


def _grid(n: int = 24, hours: float = 0.5) -> tuple[list[datetime], list[float]]:
    times = [NOW + timedelta(hours=hours * i) for i in range(n)]
    return times, [hours] * n


def _history_with(
    ratios_by_day: dict[str, list[float]], bucket: str, size: str = sq.SIZE_HIGH
) -> sq.SolarErrorHistory:
    h = sq.SolarErrorHistory()
    h.samples[sq.cell_key(bucket, size)] = [
        [d, r] for d, rs in ratios_by_day.items() for r in rs
    ]
    return h


def _table(q10: float, q90: float) -> dict:
    return dict.fromkeys(sq.CELLS, (q10, q90))


def _spread_days(
    n_days: int, per_day: int, lo: float, hi: float
) -> dict[str, list[float]]:
    rs = list(np.linspace(lo, hi, per_day))
    return {f"2026-10-{d + 1:02d}": rs for d in range(n_days)}


class BandShape(unittest.TestCase):
    def _bands(self, solar, table=None, solcast=None):
        times, _ = _grid(len(solar))
        return sq.build_bands(
            now=NOW, grid_times=times, solar_kw=solar, table=table, solcast_band=solcast
        )

    def test_monotone_quantiles_everywhere(self):
        table = _table(0.4, 1.6)
        solar = [0.7, 0.0, 0.03, 0.123456789, 2.0, 7.777777, 12.0, 0.04]
        p10, p90, method = self._bands(solar, table=table)
        self.assertEqual(method, sq.METHOD_EMPIRICAL)
        for lo, mid, hi in zip(p10, solar, p90, strict=True):
            self.assertLessEqual(lo, mid)
            self.assertLessEqual(mid, hi)
            self.assertGreaterEqual(lo, 0.0)

    def test_night_and_period0_carry_no_spread(self):
        table = _table(0.4, 1.6)
        solar = [3.0, 0.0, 0.049, 5.0]
        p10, p90, _ = self._bands(solar, table=table)
        self.assertEqual((p10[0], p90[0]), (3.0, 3.0))  # live anchor
        self.assertEqual((p10[1], p90[1]), (0.0, 0.0))
        self.assertEqual((p10[2], p90[2]), (0.049, 0.049))
        self.assertAlmostEqual(p10[3], 2.0)
        self.assertAlmostEqual(p90[3], 8.0)

    def test_empirical_uses_the_lead_bucket_and_size(self):
        table = _table(1.0, 1.0)
        table[("1-4 h", sq.SIZE_HIGH)] = (0.5, 2.0)
        table[("1-4 h", sq.SIZE_LOW)] = (0.1, 3.0)
        solar = [1.0] + [4.0] * 23  # periods every 30 min from NOW
        solar[3] = 1.0  # 1.5 h ahead, small p50 -> the low-size cell
        p10, p90, _ = self._bands(solar, table=table)
        # index 2 starts 1 h ahead -> "1-4 h"; index 1 (30 min) -> "0-1 h".
        self.assertEqual((p10[1], p90[1]), (4.0, 4.0))
        self.assertEqual((p10[2], p90[2]), (2.0, 8.0))
        self.assertEqual((p10[3], p90[3]), (0.1, 3.0))
        self.assertEqual((p10[8], p90[8]), (4.0, 4.0))  # 4 h ahead -> "4-12 h"

    def test_solcast_path_widened_to_hold_the_blend(self):
        solar = [1.0, 5.0, 5.0, 0.0]
        value = [1.0, 6.0, 4.0, 0.0]
        lower = [1.0, 5.5, 3.0, 0.0]  # above the blend at index 1
        upper = [1.0, 8.0, 4.5, 0.0]  # below the blend at index 2
        p10, p90, method = self._bands(solar, solcast=(value, lower, upper))
        self.assertEqual(method, sq.METHOD_SOLCAST)
        self.assertEqual((p10[1], p90[1]), (5.0, 8.0))
        self.assertEqual((p10[2], p90[2]), (3.0, 5.0))
        self.assertEqual((p10[3], p90[3]), (0.0, 0.0))

    def test_empirical_wins_over_solcast(self):
        table = _table(0.5, 1.5)
        band = ([1.0, 2.0], [1.0, 0.1], [1.0, 9.0])
        p10, p90, method = self._bands([1.0, 2.0], table=table, solcast=band)
        self.assertEqual(method, sq.METHOD_EMPIRICAL)
        self.assertEqual((p10[1], p90[1]), (1.0, 3.0))

    def test_no_data_falls_back_to_none(self):
        self.assertEqual(self._bands([1.0, 2.0]), (None, None, None))
        # A Solcast band with no spread (p10 missing -> zero-width) is no band.
        flat = ([1.0, 2.0], [1.0, 2.0], [1.0, 2.0])
        self.assertEqual(self._bands([1.0, 2.0], solcast=flat), (None, None, None))


class QuantileTable(unittest.TestCase):
    def test_too_short_history_is_none(self):
        h = _history_with(_spread_days(2, 40, 0.5, 1.5), "1-4 h")  # 2 days only
        self.assertIsNone(h.quantile_table())
        self.assertIsNone(sq.SolarErrorHistory().quantile_table())

    def test_own_cell_then_size_then_pooled(self):
        h = _history_with(_spread_days(5, 20, 0.2, 1.8), "1-4 h")  # 100 samples
        h.samples[sq.cell_key("24-48 h", sq.SIZE_HIGH)] = [["2026-10-01", 1.0]] * 10
        table = h.quantile_table()
        assert table is not None
        q10, q90 = table[("1-4 h", sq.SIZE_HIGH)]
        self.assertAlmostEqual(
            q10, float(np.quantile(np.linspace(0.2, 1.8, 20), 0.1)), 6
        )
        self.assertAlmostEqual(
            q90, float(np.quantile(np.linspace(0.2, 1.8, 20), 0.9)), 6
        )
        # Too few of its own: the high-size class over all leads stands in.
        high = sq._quantiles(
            [s[1] for k, v in h.samples.items() if sq.SIZE_HIGH in k for s in v]
        )
        self.assertEqual(table[("24-48 h", sq.SIZE_HIGH)], high)
        # The low-size class has nothing: everything pooled stands in.
        pooled = sq._quantiles([s[1] for s in h._all()])
        self.assertEqual(table[("0-1 h", sq.SIZE_LOW)], pooled)

    def test_quantiles_bracket_one(self):
        # A forecast that always under-predicts must still give q10 <= 1.
        self.assertEqual(sq._quantiles([1.5, 2.0, 3.0])[0], 1.0)
        self.assertEqual(sq._quantiles([0.2, 0.3, 0.5])[1], 1.0)


class Calibration(unittest.TestCase):
    def test_leave_one_day_out_coverage(self):
        # Identical days of ten values 0.55..1.45: fitted on the other days,
        # q10 = 0.64 and q90 = 1.36, so 1 of 10 falls below and 1 above.
        samples = [[f"d{d}", r] for d in range(4) for r in np.linspace(0.55, 1.45, 10)]
        self.assertEqual(sq.loo_coverage(samples), (10.0, 10.0))

    def test_coverage_detects_a_too_narrow_band(self):
        # Day 3 is far worse than the others: graded on the others' band, all
        # of it falls below p10.
        samples = [[f"d{d}", r] for d in range(3) for r in np.linspace(0.9, 1.1, 10)]
        samples += [["d3", 0.2]] * 10
        below, above = sq.loo_coverage(samples)
        assert below is not None and above is not None
        self.assertGreaterEqual(below, 25.0)

    def test_one_day_is_no_coverage(self):
        self.assertEqual(sq.loo_coverage([["d0", 1.0]] * 60), (None, None))

    def test_calibration_attribute_shape(self):
        h = _history_with(_spread_days(4, 15, 0.5, 1.5), "4-12 h")
        cal = h.calibration()
        self.assertEqual(set(cal), {sq.cell_key(b, z) for b, z in sq.CELLS})
        self.assertEqual(
            cal[sq.cell_key("0-1 h", sq.SIZE_LOW)], {"samples": 0, "days": 0}
        )
        entry = cal[sq.cell_key("4-12 h", sq.SIZE_HIGH)]
        self.assertEqual(entry["samples"], 60)
        self.assertEqual(entry["days"], 4)
        self.assertIn("below_p10_pct", entry)
        self.assertLessEqual(entry["q10_ratio"], 1.0)
        self.assertGreaterEqual(entry["q90_ratio"], 1.0)


class Recording(unittest.TestCase):
    def test_interval_means_are_duration_weighted(self):
        times = [NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=6)]
        hours = [1 / 60, 5 / 60, 24 / 60]
        # index 0 is skipped; 01-06 at 3 kW and 06-30 at 6 kW cover 29 of 30 min
        means = sq.interval_means(times, hours, [100.0, 3.0, 6.0])
        self.assertEqual(means, {})  # 06:00-06:30 not fully covered without index 0
        means = sq.interval_means(times, hours, [100.0, 3.0, 6.0], first_index=0)
        k = int(NOW.timestamp() // sq.INTERVAL_SECONDS)
        self.assertAlmostEqual(means[k], (100 * 1 + 3 * 5 + 6 * 24) / 30)

    def test_keeps_the_forecast_closest_to_each_representative_lead(self):
        h = sq.SolarErrorHistory()
        times, hours = _grid(12)
        h.record(NOW, times, hours, [5.0] * 12)
        target = int((NOW + timedelta(hours=2.5)).timestamp() // sq.INTERVAL_SECONDS)
        self.assertEqual(h.pending[target]["1-4 h"], [2.5, 5.0])
        # A later solve, 30 minutes on, forecasting 7 kW: its lead to the same
        # interval is 2.0 h, further from 2.5 than the kept one, so it loses.
        later = NOW + timedelta(minutes=30)
        t2 = [later + timedelta(hours=0.5 * i) for i in range(12)]
        h.record(later, t2, hours, [7.0] * 12)
        self.assertEqual(h.pending[target]["1-4 h"], [2.5, 5.0])
        # Night intervals are not recorded.
        h2 = sq.SolarErrorHistory()
        h2.record(NOW, times, hours, [0.0] * 12)
        self.assertEqual(h2.pending, {})

    def test_settle_grades_and_drops(self):
        h = sq.SolarErrorHistory()
        k = int(NOW.timestamp() // sq.INTERVAL_SECONDS)
        h.pending = {
            k: {"0-1 h": [0.5, 4.0], "1-4 h": [2.5, 2.0]},
            k + 1: {"0-1 h": [0.5, 3.0]},
        }
        due = h.due(NOW + timedelta(minutes=30))
        self.assertEqual(due, [k])
        added = h.settle({k: 3.0}, TZ)
        self.assertEqual(added, 2)
        # Filed by lead bucket and the size of the p50 it was issued with.
        self.assertEqual(
            h.samples[sq.cell_key("0-1 h", sq.SIZE_HIGH)], [["2026-10-16", 0.75]]
        )
        self.assertEqual(
            h.samples[sq.cell_key("1-4 h", sq.SIZE_HIGH)], [["2026-10-16", 1.5]]
        )
        self.assertNotIn(k, h.pending)
        self.assertIn(k + 1, h.pending)
        # An interval with no actual is dropped ungraded.
        self.assertEqual(h.settle({k + 1: None}, TZ), 0)
        self.assertEqual(h.pending, {})

    def test_prune_and_round_trip(self):
        h = sq.SolarErrorHistory()
        h.samples["0-1 h"] = [["2026-09-01", 1.0], ["2026-10-15", 0.9]]
        h.pending = {5: {"0-1 h": [0.5, 2.0]}}
        h.prune(NOW)
        self.assertEqual(h.samples["0-1 h"], [["2026-10-15", 0.9]])
        back = sq.SolarErrorHistory.from_dict(json.loads(json.dumps(h.to_dict())))
        self.assertEqual(back.samples, h.samples)
        self.assertEqual(back.pending, h.pending)
        self.assertEqual(sq.SolarErrorHistory.from_dict({"samples": 3}).samples, {})


class EndToEnd(unittest.TestCase):
    """update_and_build() against a stand-in for the solver_writer module:
    real persistence to a temp file, only the recorder read faked."""

    def _sw(self, path, pv_kw):
        def fetch(_sensor, start, end):
            return [(start, pv_kw)]

        def mean(_hist, starts, _hours):
            return [pv_kw for _ in starts]

        return types.SimpleNamespace(
            SOLAR_QUANTILES_PATH=path,
            _SOLAR_QUANTILES_CACHE=None,
            _SOLAR_QUANTILES_SAVED=False,
            fetch_entity_history_range=fetch,
            resample_history_mean=mean,
            _kw_scale_factor=lambda _s: 1.0,
        )

    def test_no_sensor_no_solcast_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            sw = self._sw(os.path.join(d, "q.json"), 1.0)
            times, hours = _grid()
            out = sq.update_and_build(sw, {}, NOW, times, hours, [2.0] * 24, None)
        self.assertIsNone(out["p10"])
        self.assertEqual(out["calibration"]["reason"], "no_solar_power_sensor")

    def test_history_builds_up_then_empirical(self):
        cfg = {"solver_solar_power_sensor": "sensor.pv"}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "q.json")
            sw = self._sw(path, 3.0)  # actual is always 3 kW vs a 4 kW forecast
            day = NOW
            for _ in range(4 * 24 * 2):  # four days of half-hourly solves
                times = [day + timedelta(hours=0.5 * i) for i in range(24)]
                out = sq.update_and_build(
                    sw, cfg, day, times, [0.5] * 24, [4.0] * 24, None
                )
                day += timedelta(minutes=30)
            self.assertEqual(out["calibration"]["method"], sq.METHOD_EMPIRICAL)
            self.assertAlmostEqual(out["p10"][3], 3.0)  # 0.75 x 4, below 1.0
            self.assertAlmostEqual(out["p90"][3], 4.0)  # q90 clamped to 1.0
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                saved = sq.SolarErrorHistory.from_dict(json.load(f))
            self.assertTrue(saved.samples)

    def test_solcast_used_while_history_is_short(self):
        cfg = {"solver_solar_power_sensor": "sensor.pv"}
        with tempfile.TemporaryDirectory() as d:
            sw = self._sw(os.path.join(d, "q.json"), 1.0)
            times, hours = _grid(3)
            band = ([1.0, 4.0, 4.0], [1.0, 2.0, 2.5], [1.0, 6.0, 5.0])
            out = sq.update_and_build(sw, cfg, NOW, times, hours, [1.0, 4.0, 4.0], band)
        self.assertEqual(out["calibration"]["method"], sq.METHOD_SOLCAST)
        self.assertEqual(out["p10"], [1.0, 2.0, 2.5])
        self.assertEqual(out["p90"], [1.0, 6.0, 5.0])

    def test_never_raises(self):
        sw = types.SimpleNamespace(_SOLAR_QUANTILES_CACHE="not a history")
        out = sq.update_and_build(
            sw, {"solver_solar_power_sensor": "x"}, NOW, [], [], [], None
        )
        self.assertIsNone(out["p10"])
        self.assertTrue(out["calibration"]["reason"].startswith("error"))


class SolarConfigPair(unittest.TestCase):
    def test_pair_validated(self):
        f = np.array([1.0, 2.0])
        elements.SolarConfig(
            forecast_kw=f, p10_kw=np.array([0.5, 1.0]), p90_kw=np.array([1.5, 3.0])
        )
        with self.assertRaises(ValueError):
            elements.SolarConfig(forecast_kw=f, p10_kw=np.array([0.5, 1.0]))
        with self.assertRaises(ValueError):
            elements.SolarConfig(
                forecast_kw=f, p10_kw=np.array([1.5, 1.0]), p90_kw=np.array([1.5, 3.0])
            )

    def test_default_none_and_envelope_untouched(self):
        cfg = elements.SolarConfig(
            forecast_kw=np.array([1.0]),
            lower_kw=np.array([0.0]),
            upper_kw=np.array([9.0]),
        )
        self.assertIsNone(cfg.p10_kw)
        self.assertIsNone(cfg.p90_kw)
        self.assertEqual(float(cfg.lower_kw[0]), 0.0)


class SolcastBandOut(unittest.TestCase):
    def test_build_solar_arrays_hands_back_the_solcast_member(self):
        from unittest.mock import patch

        times, _ = _grid(3)
        band = ([1.0, 2.0, 3.0], [0.5, 1.0, 1.5], [1.5, 3.0, 4.5])
        out: list = []
        with (
            patch.object(
                solar_inputs, "_fetch_open_meteo_solar_raw", return_value=None
            ),
            patch.object(solar_inputs, "_fetch_solcast_solar_raw", return_value=band),
            patch.object(solar_inputs, "_apply_running_low_gate", return_value=None),
        ):
            solar, lower, _upper = solar_inputs.build_solar_arrays(
                {"solver_auto_include_known_solar": True},
                times,
                3,
                NOW,
                solcast_band_out=out,
            )
        self.assertEqual(out, [band])
        self.assertEqual(solar, [1.0, 2.0, 3.0])
        self.assertEqual(lower, [0.5, 1.0, 1.5])


if __name__ == "__main__":
    unittest.main()
