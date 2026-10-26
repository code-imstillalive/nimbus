"""Calibrated solar p10 / p90 for the two-stage plan (#1807) (#1837).

Empirical quantiles of actual / p50 by lead bucket and p50 size, from a
graded history of the forecasts the plan used; Solcast's own band, widened
to hold the blend, while that history is short; with neither, no band at
all. Always p10 <= p50 <= p90, and no spread at night or in the live first
period. Synthetic data only.
"""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta

import numpy as np
import pytest
import solver_writer as sw
from solver_inputs import solar_quantiles as sq

TZ = sw.LOCAL_TZ
NOW = datetime(2026, 10, 16, 6, 0, tzinfo=TZ)


def _grid(n=24, hours=0.5):
    return [NOW + timedelta(hours=hours * i) for i in range(n)], [hours] * n


def _table(q10, q90):
    return dict.fromkeys(sq.CELLS, (q10, q90))


def _bands(solar, table=None, solcast=None):
    times, _ = _grid(len(solar))
    return sq.build_bands(
        now=NOW, grid_times=times, solar_kw=solar, table=table, solcast_band=solcast
    )


def _history(ratios_by_day, bucket, size=sq.SIZE_HIGH):
    h = sq.SolarErrorHistory()
    h.samples[sq.cell_key(bucket, size)] = [
        [d, r] for d, rs in ratios_by_day.items() for r in rs
    ]
    return h


def _days(n, per_day, lo, hi):
    return {
        f"2026-10-{d + 1:02d}": list(np.linspace(lo, hi, per_day)) for d in range(n)
    }


# --- the band ----------------------------------------------------------------------------------


def test_with_nothing_measured_and_no_usable_band_there_is_no_band(tmp_path):
    # test_with_nothing_measured_and_no_usable_band_there_is_no_band
    # test_with_nothing_measured_and_no_usable_band_there_is_no_band
    for solcast in [None, ([1.0, 2.0], [1.0, 2.0], [1.0, 2.0])]:
        assert _bands([1.0, 2.0], solcast=solcast) == (None, None, None)

    # test_coverage_is_graded_leaving_each_day_out
    calibrated = [[f"d{d}", r] for d in range(4) for r in np.linspace(0.55, 1.45, 10)]
    assert sq.loo_coverage(calibrated) == (10.0, 10.0)
    narrow = [[f"d{d}", r] for d in range(3) for r in np.linspace(0.9, 1.1, 10)] + [
        ["d3", 0.2]
    ] * 10
    below, _above = sq.loo_coverage(narrow)
    assert below >= 25.0  # a band too narrow for one bad day shows it
    assert sq.loo_coverage([["d0", 1.0]] * 60) == (None, None)

    # test_one_forecast_is_kept_per_interval_and_bucket_the_closest_to_its_lead
    h = sq.SolarErrorHistory()
    times, hours = _grid(12)
    h.record(NOW, times, hours, [5.0] * 12)
    target = int((NOW + timedelta(hours=2.5)).timestamp() // sq.INTERVAL_SECONDS)
    assert h.pending[target]["1-4 h"] == [2.5, 5.0]
    later = NOW + timedelta(minutes=30)
    h.record(
        later, [later + timedelta(hours=0.5 * i) for i in range(12)], hours, [7.0] * 12
    )
    assert h.pending[target]["1-4 h"] == [2.5, 5.0]  # 2 h ahead is farther from 2.5
    night = sq.SolarErrorHistory()
    night.record(NOW, times, hours, [0.0] * 12)
    assert night.pending == {}

    # test_a_due_interval_is_graded_into_its_cell_and_a_missing_actual_is_dropped
    h = sq.SolarErrorHistory()
    k = int(NOW.timestamp() // sq.INTERVAL_SECONDS)
    h.pending = {
        k: {"0-1 h": [0.5, 4.0], "1-4 h": [2.5, 2.0]},
        k + 1: {"0-1 h": [0.5, 3.0]},
    }
    assert h.due(NOW + timedelta(minutes=30)) == [k]
    assert h.settle({k: 3.0}, TZ) == 2
    assert h.samples[sq.cell_key("0-1 h", sq.SIZE_HIGH)] == [["2026-10-16", 0.75]]
    assert h.samples[sq.cell_key("1-4 h", sq.SIZE_HIGH)] == [["2026-10-16", 1.5]]
    assert k not in h.pending and k + 1 in h.pending
    assert h.settle({k + 1: None}, TZ) == 0 and h.pending == {}

    # test_old_samples_are_pruned_and_the_history_round_trips
    h = sq.SolarErrorHistory()
    h.samples["0-1 h"] = [["2026-09-01", 1.0], ["2026-10-15", 0.9]]
    h.pending = {5: {"0-1 h": [0.5, 2.0]}}
    h.prune(NOW)
    assert h.samples["0-1 h"] == [["2026-10-15", 0.9]]
    back = sq.SolarErrorHistory.from_dict(json.loads(json.dumps(h.to_dict())))
    assert (back.samples, back.pending) == (h.samples, h.pending)
    assert sq.SolarErrorHistory.from_dict({"samples": 3}).samples == {}

    # test_four_days_of_history_replace_solcasts_band_with_the_measured_one
    tmp_1 = (
        tmp_path
        / "test_four_days_of_history_replace_solcasts_band_with_the_measured_one"
    )
    tmp_1.mkdir()
    cfg = {"solver_solar_power_sensor": "sensor.pv"}
    path = tmp_1 / "q.json"
    writer = _writer(path, 3.0)  # always 3 kW against a 4 kW forecast
    times, hours = _grid(3)
    band = ([1.0, 4.0, 4.0], [1.0, 2.0, 2.5], [1.0, 6.0, 5.0])
    early = sq.update_and_build(writer, cfg, NOW, times, hours, [1.0, 4.0, 4.0], band)
    assert early["calibration"]["method"] == sq.METHOD_SOLCAST
    assert (early["p10"], early["p90"]) == ([1.0, 2.0, 2.5], [1.0, 6.0, 5.0])
    day = NOW
    for _ in range(4 * 24 * 2):  # four days of half-hourly solves
        times = [day + timedelta(hours=0.5 * i) for i in range(24)]
        out = sq.update_and_build(writer, cfg, day, times, [0.5] * 24, [4.0] * 24, None)
        day += timedelta(minutes=30)
    assert out["calibration"]["method"] == sq.METHOD_EMPIRICAL
    assert (out["p10"][3], out["p90"][3]) == pytest.approx(
        (3.0, 4.0)
    )  # q90 held at 1.0
    saved = sq.SolarErrorHistory.from_dict(json.loads(path.read_text(encoding="utf-8")))
    assert saved.samples

    # test_a_broken_history_never_breaks_the_solve
    out = sq.update_and_build(
        types.SimpleNamespace(_SOLAR_QUANTILES_CACHE="not a history"),
        {"solver_solar_power_sensor": "x"},
        NOW,
        [],
        [],
        [],
        None,
    )
    assert out["p10"] is None and out["calibration"]["reason"].startswith("error")


# --- the quantile table and its calibration ----------------------------------------------------


# --- the history -------------------------------------------------------------------------------


# --- end to end, with the file it keeps --------------------------------------------------------


def _writer(path, pv_kw):
    return types.SimpleNamespace(
        SOLAR_QUANTILES_PATH=str(path),
        _SOLAR_QUANTILES_CACHE=None,
        _SOLAR_QUANTILES_SAVED=False,
        fetch_entity_history_range=lambda _s, start, end: [(start, pv_kw)],
        resample_history_mean=lambda _h, starts, _hours: [pv_kw for _ in starts],
        _kw_scale_factor=lambda _s: 1.0,
    )
