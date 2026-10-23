"""solver_writer's time-of-day rules, load sums and day-scoring helpers (#1837).

Cost and salvage schedules (blocks that wrap midnight), the summed circuit
load forecast and its refusal of a mostly-zero total, the per-battery energy
balance verdicts, whether a provisional score stands, and the load-nowcast
skill's every outcome. Covers what lived in #919, #933, #1067, #1200 and the
schedule and energy-balance files.
"""

from __future__ import annotations

import logging
import urllib.request
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
import solver_writer as sw
from golden.fake_ha import BASE, FakeHA

T0 = datetime(2026, 9, 20, 6, 0, tzinfo=UTC)


@pytest.fixture
def ha(monkeypatch):
    fake = FakeHA(states={})
    monkeypatch.setattr(sw, "HA_BASE", BASE)
    monkeypatch.setattr(sw, "_load_token", lambda: "test")
    monkeypatch.setattr(sw.NATIVE, "hass", None)
    monkeypatch.setattr(urllib.request, "urlopen", fake.urlopen)
    return fake


def _grid(n, minutes=30):
    return [T0 + timedelta(minutes=minutes * i) for i in range(n)]


# --- schedules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "start", "end", "inside"),
    [
        (5, 5, 5, False),
        (5, 5, 9, True),
        (9, 5, 9, False),
        (4, 5, 9, False),
        (17, 17, 7, True),
        (23, 17, 7, True),
        (3, 17, 7, True),
        (7, 17, 7, False),
        (12, 17, 7, False),
    ],
)
def test_a_schedule_block_is_start_inclusive_end_exclusive_and_may_wrap(
    hour, start, end, inside
):
    assert sw._hour_in_schedule_block(hour, start, end) is inside


def test_the_discharge_cost_follows_its_first_matching_active_block():
    cfg = {
        "solver_discharge_cost": 0.02,
        "solver_discharge_cost_schedule_block_1_rate": 0.10,  # 17-07 by default
        "solver_discharge_cost_schedule_block_2_rate": 0.30,
        "solver_discharge_cost_schedule_block_2_start_hour": 6,
        "solver_discharge_cost_schedule_block_2_end_hour": 9,
        "solver_discharge_cost_schedule_block_3_rate": 0.0,  # off
        "solver_discharge_cost_schedule_block_3_start_hour": 0,
        "solver_discharge_cost_schedule_block_3_end_hour": 24,
    }
    assert sw.scheduled_discharge_cost_rate(cfg, 20) == 0.10
    assert sw.scheduled_discharge_cost_rate(cfg, 6) == 0.10  # block 1 wins the overlap
    assert sw.scheduled_discharge_cost_rate(cfg, 8) == 0.30
    assert sw.scheduled_discharge_cost_rate(cfg, 12) == 0.02
    cfg["solver_discharge_cost_schedule_default_rate"] = 0.05
    assert sw.scheduled_discharge_cost_rate(cfg, 12) == 0.05
    assert sw.scheduled_discharge_cost_rate({}, 12) == 0.01


def test_the_salvage_value_follows_its_blocks_too():
    cfg = {
        "solver_salvage_value": 0.2,
        "solver_salvage_value_schedule_block_1_rate": 0.4,
    }
    assert sw.scheduled_salvage_value_rate(cfg, 18) == 0.4  # 17-24 by default
    assert sw.scheduled_salvage_value_rate(cfg, 10) == 0.2
    assert sw.scheduled_salvage_value_rate({}, 10) == 0.15


# --- summed circuit loads ------------------------------------------------------------


def _load(values, *, unit="kW", lower=None, upper=None):
    fc = []
    for i, v in enumerate(values):
        p = {"time": (T0 + timedelta(minutes=30 * i)).isoformat(), "value": v}
        if lower is not None:
            p["lower"], p["upper"] = lower[i], upper[i]
        fc.append(p)
    return {
        "state": str(values[0]),
        "attributes": {"unit_of_measurement": unit, "forecast": fc},
    }


def test_circuits_sum_with_bands_self_consumption_and_the_shortest_coverage(ha, caplog):
    ha.states["sensor.a"] = _load(
        [1.0, 2.0, 3.0], lower=[0.5, 1.5, 2.5], upper=[1.5, 2.5, 3.5]
    )
    ha.states["sensor.b"] = _load([500, -200], unit="W")  # negative clipped to 0
    ha.states["sensor.c"] = {"state": "1", "attributes": {"forecast": []}}
    with caplog.at_level(logging.WARNING, logger=sw._LOGGER.name):
        total, lower, upper, failed, warnings, coverage = sw.sum_load_forecasts(
            ["sensor.a", "sensor.b", "sensor.c", "sensor.gone"], _grid(3), 0.1
        )
    assert total == pytest.approx([1.6, 2.1, 3.1])
    # b has no bands: its lower and upper read as 0, so the sum's band is
    # clamped to include the central value.
    assert lower == pytest.approx([0.6, 1.6, 2.6])
    assert upper == pytest.approx([1.6, 2.6, 3.6])
    assert failed == ["sensor.c", "sensor.gone"]
    assert set(warnings) == {"sensor.c", "sensor.gone"}
    assert coverage == pytest.approx(0.5)  # b's last point is 30 minutes out
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_with_no_self_consumption_given_the_sum_is_the_circuits_alone(ha):
    ha.states["sensor.a"] = _load([1.0, 2.0])
    total, *_ = sw.sum_load_forecasts(["sensor.a"], _grid(2))
    assert total == [1.0, 2.0]


def test_no_circuits_is_just_self_consumption():
    total, lower, upper, failed, warnings, coverage = sw.sum_load_forecasts(
        [], _grid(2), 0.2
    )
    assert total == lower == upper == [0.2, 0.2]
    assert (failed, warnings, coverage) == ([], {}, None)


@pytest.mark.parametrize(
    ("total", "failed", "refused"),
    [
        ([0.0] * 10, [], False),  # nothing failed: a quiet house is believed
        ([0.0] * 10, ["sensor.x"], True),
        ([0.5] + [0.0] * 9, ["sensor.x"], False),  # 10% non-trivial
        ([0.2] * 10, ["sensor.x"], True),  # only self-consumption
        ([], ["sensor.x"], False),
    ],
)
def test_a_mostly_zero_sum_with_a_failed_circuit_is_refused(total, failed, refused):
    error = sw.near_zero_summed_load_error(
        total, failed, 0.2 if total == [0.2] * 10 else 0.0
    )
    assert (error is not None) is refused
    if refused:
        assert "#933" in error and "sensor.x" in error


# --- per-battery energy balance --------------------------------------------------------


def _balance(**kw):
    base = {
        "name": "home",
        "in_kwh": 10.0,
        "out_kwh": 0.0,
        "initial_soc_kwh": 5.0,
        "final_soc_kwh": 14.0,
        "capacity_kwh": 20.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
    }
    return sw.battery_energy_balance(**{**base, **kw})


def test_a_charge_only_window_gives_the_implied_charge_efficiency():
    out = _balance()
    assert out["implied_charge_efficiency"] == 0.9
    assert out["implied_efficiency_reason"] is None
    assert out["modelled_soc_delta_kwh"] == 9.5 and out["residual_kwh"] == 0.5
    assert out["residual_pct_of_capacity"] == 2.5
    assert out["implied_discharge_efficiency"] is None


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"away_period_count": 1, "n_periods": 48}, "participant_away_during_window"),
        ({"in_kwh": 0.0, "final_soc_kwh": 6.0}, "soc_moved_without_throughput"),
        ({"in_kwh": 0.0, "final_soc_kwh": 5.2}, "no_charge_throughput"),
        (
            {"in_kwh": 5.0, "out_kwh": 6.0, "final_soc_kwh": 5.1},
            "energy_out_exceeds_in_on_closed_loop",
        ),
        (
            {"in_kwh": 10.0, "out_kwh": 2.0, "final_soc_kwh": 12.0},
            "mixed_window_not_decisive",
        ),
        (
            {"in_kwh": 10.0, "out_kwh": 2.0, "final_soc_kwh": 14.0},
            "mixed_window_not_decisive_implied_above_unity",
        ),
        ({"final_soc_kwh": 16.0}, "implied_efficiency_above_unity"),
    ],
)
def test_each_window_that_cannot_measure_efficiency_says_why(kw, reason):
    assert _balance(**kw)["implied_efficiency_reason"] == reason


@pytest.mark.parametrize(("in_kwh", "out_kwh"), [(1e-6, 0.0), (0.0, 1e-6)])
def test_throughput_at_the_noise_floor_counts_as_none(in_kwh, out_kwh):
    out = _balance(in_kwh=in_kwh, out_kwh=out_kwh, final_soc_kwh=6.0)
    assert out["implied_efficiency_reason"] == "soc_moved_without_throughput"


def test_the_balance_is_reported_to_the_watt_hour():
    out = _balance(in_kwh=1.23456, final_soc_kwh=6.12345)
    assert out["modelled_soc_delta_kwh"] == 1.173
    assert out["measured_soc_delta_kwh"] == 1.123
    assert out["residual_kwh"] == 0.049
    assert out["implied_charge_efficiency"] == round(1.12345 / 1.23456, 4)


def test_unobserved_power_and_a_zero_capacity_are_reported_not_hidden():
    out = _balance(
        stale_period_count=12,
        n_periods=48,
        capacity_kwh=0.0,
        out_kwh=1.0,
        final_soc_kwh=12.0,
    )
    assert out["unobserved_power_fraction"] == 0.25
    assert out["residual_pct_of_capacity"] is None
    assert out["implied_discharge_efficiency"] == round(1.0 / (9.5 - 7.0), 4)


# --- whether a provisional score stands -------------------------------------------------


@pytest.mark.parametrize(
    ("attrs", "age_minutes", "keep"),
    [
        ({"real_p2p_settlement_status": "settled"}, 600, True),
        (
            {"real_p2p_settlement_status": "no_settlement_entry_for_this_date"},
            None,
            True,
        ),
        (
            {
                "real_p2p_settlement_status": "settlement_sensor_unreadable",
                "generated_at": "x",
            },
            None,
            True,
        ),
        ({"real_p2p_settlement_status": "no_settlement_entry_for_this_date"}, 59, True),
        (
            {"real_p2p_settlement_status": "no_settlement_entry_for_this_date"},
            60,
            False,
        ),
    ],
)
def test_a_provisional_score_is_redone_hourly_until_its_settlement_lands(
    attrs, age_minutes, keep
):
    attrs = dict(attrs)
    if age_minutes is not None:
        attrs["generated_at"] = (T0 - timedelta(minutes=age_minutes)).isoformat()
    assert sw._keep_published_quality_score(attrs, T0) is keep


# --- the load-nowcast skill ---------------------------------------------------------------


def _nowcast(monkeypatch, *, trail, prev, result, n=4, solar=None):
    reads = []

    def history(entity_id, start, end):
        reads.append((entity_id, start, end))
        return trail if entity_id == sw._LOAD_NOWCAST_TRAIL_ENTITY_ID else prev

    monkeypatch.setattr(sw, "fetch_entity_history_range", history)
    got = {}

    def compute(**kw):
        got.update(kw)
        return result

    monkeypatch.setattr(sw.nowcast_skill, "compute_load_nowcast_skill", compute)
    grid = _grid(n)
    out = sw._load_nowcast_skill_attributes(
        load_sensor="sensor.house",
        load_scale=0.001,
        grid_times=grid,
        period_hours=0.5,
        periods=None,
        grid=None,
        battery=None,
        solar_real_kw=np.zeros(n) if solar is None else solar,
        load_real_kw=np.ones(n),
        day_start=grid[0],
        day_end=grid[-1] + timedelta(minutes=30),
    )
    return out, reads, got


def test_without_a_recorded_trail_or_baseline_there_is_no_skill(monkeypatch):
    out, reads, _ = _nowcast(monkeypatch, trail=[], prev=[], result=None)
    assert out["load_nowcast_skill_reason"] == "no_forecast_trail"
    assert all(
        out[k] is None
        for k in sw._NOWCAST_SKILL_KEYS
        if k != "load_nowcast_skill_reason"
    )
    trail = [(t, 1.0) for t in _grid(4)]
    out, reads, _ = _nowcast(monkeypatch, trail=trail, prev=[], result=None)
    assert out["load_nowcast_skill_reason"] == "no_persistence_baseline"
    # The baseline is the same window a day earlier.
    assert reads[1] == (
        "sensor.house",
        T0 - timedelta(hours=24),
        T0 + timedelta(hours=2) - timedelta(hours=24),
    )


def test_the_baseline_is_yesterdays_load_shifted_and_scaled(monkeypatch):
    trail = [(t, 1.0) for t in _grid(4)]
    prev = [(t - timedelta(hours=24), 2000.0) for t in _grid(4)]
    result = SimpleNamespace(
        j_star=1.23456,
        j_forecast=2.0,
        j_persistence=3.0,
        value_add_dollars=1.0,
        coverage=1.0,
        n_periods_measured=4,
    )
    out, _, got = _nowcast(monkeypatch, trail=trail, prev=prev, result=result)
    np.testing.assert_allclose(got["load_persistence_kw"], [2.0] * 4)
    np.testing.assert_allclose(got["load_nowcast_kw"], [1.0] * 4)
    assert got["n_periods_measured"] == 4
    assert out == {
        "load_nowcast_skill_j_star": 1.2346,
        "load_nowcast_skill_j_forecast": 2.0,
        "load_nowcast_skill_j_persistence": 3.0,
        "load_nowcast_skill_value_add_dollars": 1.0,
        "load_nowcast_skill_coverage": 1.0,
        "load_nowcast_skill_periods_measured": 4,
        "load_nowcast_skill_reason": None,
    }


@pytest.mark.parametrize(
    ("trail_points", "solar", "reason"),
    [
        (4, np.zeros(3), "input_length_mismatch"),
        (1, None, "coverage_below_threshold"),
        (4, None, "scenario_solve_failed"),
    ],
)
def test_a_skill_that_cannot_be_computed_says_which_input_stopped_it(
    monkeypatch, trail_points, solar, reason
):
    trail = [(t, 1.0) for t in _grid(4)[:trail_points]]
    prev = [(t - timedelta(hours=24), 1.0) for t in _grid(4)]
    out, _, _ = _nowcast(monkeypatch, trail=trail, prev=prev, result=None, solar=solar)
    assert out["load_nowcast_skill_reason"] == reason
    assert out["load_nowcast_skill_periods_measured"] == trail_points
    assert out["load_nowcast_skill_coverage"] == round(trail_points / 4, 3)


# --- flex availability, the P2P step lookup, consolidation's spike rule ----------


def test_flex_availability_is_period_zeros_headroom_within_the_grid_limits():
    sig = SimpleNamespace(
        available_up_kw=np.array([4.0, 9.0]), available_down_kw=np.array([3.0, 9.0])
    )
    plan = SimpleNamespace(
        battery_signals=[sig, sig],
        grid_import_kw=np.array([2.0, 0.0]),
        grid_export_kw=np.array([0.0, 5.0]),
    )
    assert (
        sw._flex_available_physical(plan, 7.0, 10.0)
        == {
            "flex_available_up_kw": 5.0,  # 8 kW of batteries, 5 kW left under the import limit
            "flex_available_down_kw": 6.0,  # 6 kW of batteries, within 12 kW of export room
        }
    )
    none = SimpleNamespace(
        battery_signals=None, grid_import_kw=np.zeros(1), grid_export_kw=np.zeros(1)
    )
    assert sw._flex_available_physical(none, 7.0, 10.0) == {
        "flex_available_up_kw": 0.0,
        "flex_available_down_kw": 0.0,
    }


def test_the_raw_p2p_rate_counts_only_inside_its_window():
    grid = _grid(4)
    pts = [(grid[0], 0.2), (grid[2], 0.3)]
    window = [1.0, 0.0, 2.0, 2.0]
    # Outside the window (0 kW) no rate; inside, the latest real rate, then
    # past the last real point the median of the real positive rates.
    assert sw._resample_p2p_points(pts, grid, window) == [0.2, 0.0, 0.3, 0.25]
    assert sw._resample_p2p_points([], grid, window) == [0.0] * 4


def test_consolidation_keeps_spikes_unless_a_threshold_is_set(monkeypatch):
    seen = []

    def side(tracker, series, now, grid, hours, n_settled, settled, spike):
        seen.append(spike)
        return [], {}, False

    monkeypatch.setattr(sw.price_consolidation, "consolidate_side", side)
    monkeypatch.setattr(
        sw, "_load_price_trackers", lambda: {"import": {}, "export": {}}
    )
    monkeypatch.setattr(sw, "_consolidation_sources", lambda *a: {})
    monkeypatch.setattr(sw, "_record_price_errors", lambda *a: None)
    monkeypatch.setattr(sw, "_loss_factor", lambda prices: {})
    args = (
        T0,
        _grid(2),
        [0.5, 0.5],
        1,
        None,
        [0.1],
        [0.1],
        None,
        None,
        [0.1],
        [0.1],
        [],
        [],
    )
    assert sw.apply_price_consolidation({}, *args)["mode"] == "shadow"
    sw.apply_price_consolidation({"solver_price_spike_threshold": 3.0}, *args)
    assert seen == [0.0, 0.0, 3.0, 3.0]
