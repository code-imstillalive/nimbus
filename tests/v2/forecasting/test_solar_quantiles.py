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
from solver import elements
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


def test_the_band_always_brackets_p50_and_never_goes_negative():
    solar = [0.7, 0.0, 0.03, 0.123456789, 2.0, 7.777777, 12.0, 0.04]
    p10, p90, method = _bands(solar, table=_table(0.4, 1.6))
    assert method == sq.METHOD_EMPIRICAL
    assert all(
        0.0 <= lo <= mid <= hi for lo, mid, hi in zip(p10, solar, p90, strict=True)
    )


def test_no_spread_at_night_or_in_the_live_first_period():
    p10, p90, _ = _bands([3.0, 0.0, 0.049, 5.0], table=_table(0.4, 1.6))
    assert list(zip(p10, p90, strict=True))[:3] == [
        (3.0, 3.0),
        (0.0, 0.0),
        (0.049, 0.049),
    ]
    assert (p10[3], p90[3]) == pytest.approx((2.0, 8.0))


def test_each_period_reads_its_own_lead_bucket_and_size_class():
    table = _table(1.0, 1.0)
    table[("1-4 h", sq.SIZE_HIGH)] = (0.5, 2.0)
    table[("1-4 h", sq.SIZE_LOW)] = (0.1, 3.0)
    solar = [1.0] + [4.0] * 23
    solar[3] = 1.0  # 1.5 h ahead with a small p50: the low-size cell
    p10, p90, _ = _bands(solar, table=table)
    assert (p10[1], p90[1]) == (4.0, 4.0)  # under an hour ahead
    assert (p10[2], p90[2]) == (2.0, 8.0)
    assert (p10[3], p90[3]) == (0.1, 3.0)
    assert (p10[8], p90[8]) == (4.0, 4.0)  # 4 h ahead: the next bucket


def test_while_the_history_is_short_solcasts_band_is_widened_to_hold_the_blend():
    solar = [1.0, 5.0, 5.0, 0.0]
    band = ([1.0, 6.0, 4.0, 0.0], [1.0, 5.5, 3.0, 0.0], [1.0, 8.0, 4.5, 0.0])
    p10, p90, method = _bands(solar, solcast=band)
    assert method == sq.METHOD_SOLCAST
    assert [(p10[i], p90[i]) for i in (1, 2, 3)] == [(5.0, 8.0), (3.0, 5.0), (0.0, 0.0)]
    # The measured history, once there is one, wins.
    p10, p90, method = _bands(
        [1.0, 2.0], table=_table(0.5, 1.5), solcast=([1.0, 2.0], [1.0, 0.1], [1.0, 9.0])
    )
    assert method == sq.METHOD_EMPIRICAL and (p10[1], p90[1]) == (1.0, 3.0)


@pytest.mark.parametrize(
    "solcast",
    [None, ([1.0, 2.0], [1.0, 2.0], [1.0, 2.0])],
    ids=["nothing", "no_spread"],
)
def test_with_nothing_measured_and_no_usable_band_there_is_no_band(solcast):
    assert _bands([1.0, 2.0], solcast=solcast) == (None, None, None)


# --- the quantile table and its calibration ----------------------------------------------------


def test_a_cell_reads_its_own_samples_then_its_size_class_then_everything():
    assert (
        _history(_days(2, 40, 0.5, 1.5), "1-4 h").quantile_table() is None
    )  # two days
    assert sq.SolarErrorHistory().quantile_table() is None
    h = _history(_days(5, 20, 0.2, 1.8), "1-4 h")
    h.samples[sq.cell_key("24-48 h", sq.SIZE_HIGH)] = [["2026-10-01", 1.0]] * 10
    table = h.quantile_table()
    own = np.linspace(0.2, 1.8, 20)
    assert table[("1-4 h", sq.SIZE_HIGH)] == pytest.approx(
        (float(np.quantile(own, 0.1)), float(np.quantile(own, 0.9)))
    )
    high = sq._quantiles(
        [s[1] for k, v in h.samples.items() if sq.SIZE_HIGH in k for s in v]
    )
    assert table[("24-48 h", sq.SIZE_HIGH)] == high  # too few of its own
    assert table[("0-1 h", sq.SIZE_LOW)] == sq._quantiles([s[1] for s in h._all()])
    assert (
        sq._quantiles([1.5, 2.0, 3.0])[0] == 1.0
        and sq._quantiles([0.2, 0.3, 0.5])[1] == 1.0
    )


def test_coverage_is_graded_leaving_each_day_out():
    calibrated = [[f"d{d}", r] for d in range(4) for r in np.linspace(0.55, 1.45, 10)]
    assert sq.loo_coverage(calibrated) == (10.0, 10.0)
    narrow = [[f"d{d}", r] for d in range(3) for r in np.linspace(0.9, 1.1, 10)] + [
        ["d3", 0.2]
    ] * 10
    below, _above = sq.loo_coverage(narrow)
    assert below >= 25.0  # a band too narrow for one bad day shows it
    assert sq.loo_coverage([["d0", 1.0]] * 60) == (None, None)


def test_the_published_calibration_has_every_cell():
    cal = _history(_days(4, 15, 0.5, 1.5), "4-12 h").calibration()
    assert set(cal) == {sq.cell_key(b, z) for b, z in sq.CELLS}
    assert cal[sq.cell_key("0-1 h", sq.SIZE_LOW)] == {"samples": 0, "days": 0}
    entry = cal[sq.cell_key("4-12 h", sq.SIZE_HIGH)]
    assert (entry["samples"], entry["days"]) == (60, 4) and "below_p10_pct" in entry
    assert entry["q10_ratio"] <= 1.0 <= entry["q90_ratio"]


# --- the history -------------------------------------------------------------------------------


def test_an_interval_mean_is_duration_weighted_and_needs_its_whole_interval():
    times = [NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=6)]
    hours = [1 / 60, 5 / 60, 24 / 60]
    assert sq.interval_means(times, hours, [100.0, 3.0, 6.0]) == {}  # without index 0
    means = sq.interval_means(times, hours, [100.0, 3.0, 6.0], first_index=0)
    k = int(NOW.timestamp() // sq.INTERVAL_SECONDS)
    assert means[k] == pytest.approx((100 * 1 + 3 * 5 + 6 * 24) / 30)


def test_one_forecast_is_kept_per_interval_and_bucket_the_closest_to_its_lead():
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


def test_a_due_interval_is_graded_into_its_cell_and_a_missing_actual_is_dropped():
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


def test_old_samples_are_pruned_and_the_history_round_trips():
    h = sq.SolarErrorHistory()
    h.samples["0-1 h"] = [["2026-09-01", 1.0], ["2026-10-15", 0.9]]
    h.pending = {5: {"0-1 h": [0.5, 2.0]}}
    h.prune(NOW)
    assert h.samples["0-1 h"] == [["2026-10-15", 0.9]]
    back = sq.SolarErrorHistory.from_dict(json.loads(json.dumps(h.to_dict())))
    assert (back.samples, back.pending) == (h.samples, h.pending)
    assert sq.SolarErrorHistory.from_dict({"samples": 3}).samples == {}


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


def test_without_a_pv_sensor_or_solcast_there_is_no_band(tmp_path):
    times, hours = _grid()
    out = sq.update_and_build(
        _writer(tmp_path / "q.json", 1.0), {}, NOW, times, hours, [2.0] * 24, None
    )
    assert (
        out["p10"] is None and out["calibration"]["reason"] == "no_solar_power_sensor"
    )


def test_four_days_of_history_replace_solcasts_band_with_the_measured_one(tmp_path):
    cfg = {"solver_solar_power_sensor": "sensor.pv"}
    path = tmp_path / "q.json"
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


def test_a_broken_history_never_breaks_the_solve():
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


def test_the_solve_takes_the_pair_only_whole_ordered_and_beside_the_envelope():
    f = np.array([1.0, 2.0])
    elements.SolarConfig(
        forecast_kw=f, p10_kw=np.array([0.5, 1.0]), p90_kw=np.array([1.5, 3.0])
    )
    with pytest.raises(ValueError):
        elements.SolarConfig(forecast_kw=f, p10_kw=np.array([0.5, 1.0]))
    with pytest.raises(ValueError):
        elements.SolarConfig(
            forecast_kw=f, p10_kw=np.array([1.5, 1.0]), p90_kw=np.array([1.5, 3.0])
        )
    cfg = elements.SolarConfig(
        forecast_kw=np.array([1.0]), lower_kw=np.array([0.0]), upper_kw=np.array([9.0])
    )
    assert cfg.p10_kw is None and cfg.p90_kw is None and float(cfg.lower_kw[0]) == 0.0
