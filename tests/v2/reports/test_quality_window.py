"""Scoring one window from recorder history (#1837).

`_compute_report_for_window()` reads the day's solar, load, battery, SoC
and prices from history, scores it with `compute_quality_report()`, and
reports what the score can be trusted for: achieved energy per battery,
whether the reconstructed SoC agrees with the real sensor, the P2P
settlement for a whole calendar day. Synthetic histories only.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import datetime, timedelta

import numpy as np
import pytest
import solver_shared as shared
import solver_writer as sw
from solver import elements
from solver_inputs import battery_participants as participants

TZ = sw.LOCAL_TZ
DAY = datetime(2026, 8, 24, 0, 0, tzinfo=TZ)
END = DAY + timedelta(days=1)


def flat(value, step=15):
    def rows(start, end):
        out, t = [], start
        while t < end:
            out.append((t, value(t) if callable(value) else value))
            t += timedelta(minutes=step)
        return out

    return rows


def prices(cheap=0.05, expensive=0.35, hour=17):
    return flat(lambda t: expensive if t.hour >= hour else cheap)


def cfg(**kw):
    return {
        "solver_solar_power_sensor": "sensor.real_solar",
        "solver_battery_power_sensor": "sensor.real_battery",
        "solver_whole_house_cross_check_sensor": "sensor.real_load",
        "solver_import_price_sensor": "sensor.import_price",
        "solver_export_price_sensor": "sensor.export_price",
        "solver_battery_soc_sensor": "sensor.combined_soc",
        "solver_battery_capacity_kwh": 50.0,
        "solver_battery_min_soc_percent": 5.0,
        "solver_battery_max_soc_percent": 100.0,
        "solver_max_charge_kw": 10.0,
        "solver_max_discharge_kw": 10.0,
        "solver_efficiency_percent": 95.0,
        "solver_charge_cost": 0.01,
        "solver_discharge_cost": 0.01,
        "solver_salvage_value": 0.1,
        "solver_grid_max_import_kw": 20.0,
        "solver_grid_max_export_kw": 20.0,
        **kw,
    }


@pytest.fixture
def history(monkeypatch):
    """Solar 0, load 2 kW, the battery idle, SoC 50 %, cheap overnight and
    dear from 17:00: real arbitrage for the oracle, none for an idle day."""
    series = {
        "sensor.real_solar": flat(0.0),
        "sensor.real_load": flat(2.0),
        "sensor.real_battery": flat(0.0),
        "sensor.combined_soc": flat(50.0),
        "sensor.import_price": prices(),
        "sensor.export_price": prices(0.02, 0.10),
    }
    calls = []

    def fetch(eid, start, end):
        calls.append(eid)
        rows = series.get(eid)
        return rows(start, end) if rows else []

    monkeypatch.setattr(sw, "fetch_entity_history_range", fetch)

    def offline(*_a, **_k):
        raise urllib.error.URLError("no Home Assistant in this test")

    # Every other read is one Home Assistant does not answer: refused at
    # once, not after a connect timeout per call.
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    series["_calls"] = calls
    return series


def window(c=None, start=DAY, end=END, partial=True):
    return sw._compute_report_for_window(c or cfg(), start, end, allow_partial=partial)


# --- the window's shape -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"), [(DAY, DAY), (END, DAY)], ids=["empty", "reversed"]
)
def test_a_window_with_no_length_is_not_scored(history, start, end):
    assert window(start=start, end=end) is None


def test_without_a_solar_sensor_nothing_is_read_or_scored(history):
    assert window(cfg(solver_solar_power_sensor=None)) is None
    assert history["_calls"] == []


def test_a_short_window_is_scored_only_when_partial_is_allowed(history):
    six = DAY + timedelta(hours=6)
    assert window(end=six, partial=False) is None
    scored = window(end=six, partial=True)
    assert {"epr", "j_ref", "j_ach", "j_star"} <= set(scored)


@pytest.mark.parametrize(
    ("length", "periods", "hours"),
    [
        (timedelta(minutes=30), 6, 5 / 60),  # inside the 5-minute tier
        (timedelta(hours=6), 12, 0.5),
        (timedelta(days=1), 48, 0.5),
        (timedelta(days=2), 96, 0.5),
    ],
    ids=["30min", "6h", "24h", "48h"],
)
def test_the_window_is_scored_at_the_dispatch_grids_own_resolution(
    history, monkeypatch, length, periods, hours
):
    grids = []
    real = elements.PeriodGrid

    def spy(*a, **kw):
        grids.append(kw["hours"])
        return real(*a, **kw)

    monkeypatch.setattr(sw.elements, "PeriodGrid", spy)
    assert window(end=DAY + length) is not None
    (used,) = grids
    assert (len(used), float(used[0])) == (periods, pytest.approx(hours))


# --- P2P settlement is read only for one whole calendar day ------------------------------------


SETTLEMENT = {
    "state": "ok",
    "attributes": {
        "history": {
            DAY.date().isoformat(): {"export_cost": 4.20, "export_volume": 10.0}
        }
    },
}


def test_a_calendar_day_reads_its_own_p2p_settlement(history, monkeypatch):
    monkeypatch.setattr(sw, "ha_get", lambda _e: SETTLEMENT)
    scored = window(
        cfg(solver_p2p_settlement_history_sensor="sensor.p2p"), partial=False
    )
    assert (scored["real_p2p_dollars"], scored["real_p2p_volume_kwh"]) == (4.20, 10.0)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (DAY + timedelta(hours=12), DAY + timedelta(hours=36)),  # crosses midnight
        (DAY, DAY + timedelta(hours=6)),  # part of a day
    ],
    ids=["cross_midnight", "partial_day"],
)
def test_any_other_window_never_looks_the_settlement_up(
    history, monkeypatch, start, end
):
    seen = []
    monkeypatch.setattr(shared, "ha_get", lambda e: seen.append(e) or SETTLEMENT)
    scored = window(
        cfg(solver_p2p_settlement_history_sensor="sensor.p2p"), start=start, end=end
    )
    assert (scored["real_p2p_dollars"], scored["real_p2p_volume_kwh"]) == (0.0, 0.0)
    assert "sensor.p2p" not in seen


def test_yesterdays_report_is_the_same_window_scored_whole(history):
    wrapper = sw.compute_daily_quality_report(
        cfg(), datetime(2026, 8, 25, 10, 0, tzinfo=TZ)
    )
    direct = window(partial=False)
    keys = [
        "epr",
        "theoretical_maximum_yield",
        "value_captured",
        "uplift_available",
        "j_ref",
        "j_ach",
        "j_star",
        "regret_dollars",
        "tracking_fidelity",
        "tracking_cost",
        "real_p2p_dollars",
        "real_p2p_volume_kwh",
        "j_ref_hourly",
        "j_ach_hourly",
        "j_star_hourly",
        "hourly_regret",
    ]
    assert {k: wrapper[k] for k in keys} == {k: direct[k] for k in keys}


# --- achieved energy, home battery and fleet (#532, #858) ---------------------------------------


@pytest.mark.parametrize(
    ("kw", "into", "out_of"),
    [(0.0, 0.0, 0.0), (20.0, 0.0, 480.0), (-20.0, 480.0, 0.0)],
    ids=["idle", "discharge", "charge"],
)
def test_achieved_energy_is_integrated_from_the_battery_power_sensor(
    history, kw, into, out_of
):
    history["sensor.real_battery"] = flat(kw)  # discharge positive
    scored = window()
    assert scored["achieved_energy_in_kwh"] == pytest.approx(into, abs=0.1)
    assert scored["achieved_energy_out_kwh"] == pytest.approx(out_of, abs=0.1)


def _participant(name, charge_kw, discharge_kw, n):
    battery = elements.BatteryConfig(
        name=name,
        capacity_kwh=60.0,
        initial_soc_kwh=30.0,
        min_soc_kwh=3.0,
        max_soc_kwh=60.0,
        max_charge_kw=11.0,
        max_discharge_kw=11.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.1,
    )
    return (
        battery,
        np.full(n, float(charge_kw)),
        np.full(n, float(discharge_kw)),
        30.0,
        [],
    )


@pytest.fixture
def fleet(history, monkeypatch):
    evs = []
    monkeypatch.setattr(
        participants,
        "_resolve_battery_participant_history",
        lambda *, n_periods, **_kw: [
            _participant(n, c, d, n_periods) for n, c, d in evs
        ],
    )
    history["sensor.real_battery"] = flat(20.0)
    return evs


def test_without_participants_the_fleet_is_the_home_battery(fleet):
    scored = window()
    assert scored["fleet_achieved_energy_out_kwh"] == pytest.approx(
        scored["achieved_energy_out_kwh"]
    )
    assert scored["fleet_achieved_energy_in_kwh"] == pytest.approx(
        scored["achieved_energy_in_kwh"]
    )
    (only,) = scored["achieved_energy_by_battery"].values()
    assert only["out_kwh"] == pytest.approx(scored["achieved_energy_out_kwh"])


def test_participants_count_in_the_fleet_and_by_battery_never_in_the_home_pair(fleet):
    home_only = window()
    fleet += [("ev_a", 5.0, 0.0), ("ev_b", 2.0, 0.0), ("ev_bad", 1500.0, 0.0)]
    scored = window()
    assert scored["fleet_achieved_energy_out_kwh"] == pytest.approx(480.0, abs=0.1)
    by = scored["achieved_energy_by_battery"]
    assert len(by) == 4
    assert (by["ev_a"]["in_kwh"], by["ev_b"]["in_kwh"]) == (
        pytest.approx(120.0, abs=0.1),
        pytest.approx(48.0, abs=0.1),
    )
    assert (
        by["ev_bad"]["in_kwh"] > 30000.0
    )  # a corrupt participant is named, not hidden
    assert scored["fleet_achieved_energy_in_kwh"] > 30000.0
    assert (
        scored["achieved_energy_in_kwh"] == home_only["achieved_energy_in_kwh"] == 0.0
    )
    assert scored["achieved_energy_out_kwh"] == pytest.approx(
        home_only["achieved_energy_out_kwh"]
    )


# --- whether the reconstructed SoC agrees with the real sensor (#532, #533, #538) -----------------


def _burst(history):
    """3 kW for 3 h then idle: the reconstruction settles ~18 points from a
    flat 50 % sensor without ever leaving [0, 100]."""
    history["sensor.real_battery"] = flat(
        lambda t: 3.0 if t < DAY + timedelta(hours=3) else 0.0
    )


@pytest.mark.parametrize(
    ("shape", "reliable", "reason"),
    [
        ("idle", True, None),
        ("out_of_range", False, "out_of_range"),  # 20 kW all day against 50 kWh
        ("disagreement", False, "disagreement"),
    ],
    ids=["idle", "out_of_range", "disagreement"],
)
def test_the_score_says_whether_its_soc_can_be_trusted_and_why_not(
    history, shape, reliable, reason
):
    if shape == "out_of_range":
        history["sensor.real_battery"] = flat(20.0)
    elif shape == "disagreement":
        _burst(history)
    scored = window()
    assert scored["soc_discrepancy_reliable"] is reliable
    assert scored["epr_reliable"] is reliable
    assert scored["soc_discrepancy_reason"] == reason
    if shape == "disagreement":
        assert (
            scored["soc_discrepancy_max_pct"] > 15.0
            and scored["soc_discrepancy_mean_pct"] > 8.0
        )


def test_a_household_can_accept_a_noisier_soc_sensor(history):
    _burst(history)
    scored = window(
        cfg(
            solver_soc_discrepancy_max_threshold_pct=50.0,
            solver_soc_discrepancy_mean_threshold_pct=50.0,
        )
    )
    assert scored["soc_discrepancy_reliable"] and scored["epr_reliable"]
    assert scored["soc_discrepancy_reason"] is None
