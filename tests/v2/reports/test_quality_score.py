"""The daily quality score: what the plan achieved against perfect
foresight (#1837).

`compute_quality_report()` prices three trajectories on the same day:
doing nothing (j_ref), what the battery actually did (j_ach) and the
perfect-foresight oracle (j_star). EPR and regret compare them. The oracle
is widened to whatever the battery demonstrably did (#956, #1001), so
achieved can never beat perfect foresight. None of the three is credited
with energy left at the end of an already-elapsed day. Synthetic data
only.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import solver_writer as sw
from solver import meter_reconciliation as mr
from solver.elements import (
    AdequacyLoadConfig,
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SheddableLoadConfig,
    SolarConfig,
)
from solver.epr import compute_epr
from solver.quality_report import (
    OracleControllableLoads,
    _hourly_means_by_key,
    _soc_envelope_containing_achieved,
    _soc_pct_at_hour_boundaries,
    _widen_export_pin_to_achieved,
    compute_quality_report,
)
from solver.regret import evaluate_realized_cost_multi
from solver_inputs import battery_participants as participants

qr = sys.modules[compute_quality_report.__module__]
N = 24
HOURS = np.ones(N)
CAP = 122.2
MIN_SOC = CAP * 0.02
START = datetime(2026, 9, 12, 0, 0, tzinfo=UTC)


def _battery(initial=60.0, **kw) -> BatteryConfig:
    args = {
        "name": "home",
        "capacity_kwh": CAP,
        "initial_soc_kwh": initial,
        "min_soc_kwh": MIN_SOC,
        "max_soc_kwh": CAP,
        "max_charge_kw": 40.0,
        "max_discharge_kw": 40.0,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "charge_cost": 0.01,
        "discharge_cost": 0.01,
        "salvage_value": 0.0,
        **kw,
    }
    return BatteryConfig(**args)


def _pin(value, hours):
    pin = np.full(N, np.nan)
    pin[list(hours)] = value
    return pin


def _grid(imp=0.20, exp=0.10, pin=None, band=None, export_limit=42.0) -> GridConfig:
    return GridConfig(
        import_price=np.broadcast_to(np.asarray(imp, dtype=float), (N,)).copy(),
        export_price=np.broadcast_to(np.asarray(exp, dtype=float), (N,)).copy(),
        import_limit_kw=42.0,
        export_limit_kw=export_limit,
        fixed_export_kw=pin,
        fixed_export_max_kw=band,
    )


def _report(
    battery, grid, *, discharge=None, charge=None, load=1.0, solar=0.0, p2p=0.0, **kw
):
    zero = np.zeros(N)
    discharge = zero if discharge is None else discharge
    charge = zero if charge is None else charge
    final = (
        battery.initial_soc_kwh
        + float(np.sum(charge)) * battery.charge_efficiency
        - float(np.sum(discharge)) / battery.discharge_efficiency
    )
    return compute_quality_report(
        periods=PeriodGrid(hours=HOURS, start=START),
        grid_residual=grid,
        grid_oracle=grid,
        batteries=[battery],
        solar=SolarConfig(forecast_kw=np.full(N, float(solar))),
        load=LoadConfig(name="house", forecast_kw=np.full(N, float(load))),
        timestamps=[START + timedelta(hours=i) for i in range(N)],
        real_p2p_dollars_earned=p2p,
        commanded_charge_kw=[zero],
        commanded_discharge_kw=[zero],
        actual_charge_kw=[charge],
        actual_discharge_kw=[discharge],
        final_soc_kwh_actual=[final],
        **kw,
    )


# --- the oracle's SoC envelope holds what the battery did (#956) ------------------------------


def _envelope(battery, charge=0.0, discharge=0.0):
    c = charge if isinstance(charge, np.ndarray) else np.full(N, charge)
    d = discharge if isinstance(discharge, np.ndarray) else np.full(N, discharge)
    return _soc_envelope_containing_achieved(battery, c, d, HOURS)


# --- the oracle's P2P pin holds what was delivered (#956, #1001) ------------------------------


def _widen(grid, export_kw):
    return _widen_export_pin_to_achieved(
        grid,
        hours=HOURS,
        load_kw=np.zeros(N),
        solar_kw=np.full(N, float(export_kw)),
        actual_charge_kw=[np.zeros(N)],
        actual_discharge_kw=[np.zeros(N)],
    )


def test_without_a_commitment_the_grid_is_returned_untouched():
    # test_without_a_commitment_the_grid_is_returned_untouched
    # test_without_a_commitment_the_grid_is_returned_untouched
    for grid in (_grid(), _grid(pin=np.full(N, np.nan))):
        assert _widen(grid, 5.0) is grid

    # test_an_hours_sample_is_the_soc_as_it_begins: Hour 0 is the initial SoC; later hours the end of the period before
    quarter = _bounds(_quarters(3), np.arange(12, dtype=float), -1.0)
    assert [quarter[_key(h)] for h in range(3)] == [-1.0, 3.0, 7.0]
    hourly = _bounds(np.full(3, 1.0), np.array([10.0, 20.0, 30.0]), 5.0)
    assert [hourly[_key(h)] for h in range(3)] == [5.0, 10.0, 20.0]
    assert _bounds(np.array([]), np.array([]), 1.0) == {}

    # test_a_sensor_tracking_the_ramp_exactly_agrees_only_when_compared_like_for_like
    rows, bounds, real = _ramp()
    fixed = sw._soc_discrepancy_stats([(real, 100.0)], rows, ach_soc_pct_at_hour=bounds)
    assert fixed["soc_discrepancy_basis"] == "hour_boundary"
    assert float(fixed["soc_discrepancy_max_pct"]) < 0.5
    assert fixed["soc_discrepancy_reliable"]
    before = sw._soc_discrepancy_stats([(real, 100.0)], rows, ach_soc_pct_at_hour=None)
    assert before["soc_discrepancy_basis"] == "hourly_mean"
    assert float(before["soc_discrepancy_max_pct"]) > 5.0
    partial = dict(list(bounds.items())[:2])
    mixed = sw._soc_discrepancy_stats(
        [(real, 100.0)], rows, ach_soc_pct_at_hour=partial
    )
    assert mixed["soc_discrepancy_basis"] == "mixed"

    # test_a_reconstruction_double_counting_the_meter_disagrees_either_sign: 5 Oct: reconstructed 71.6 / 88.6 kWh against a meter's 32.7 / 69.5.
    r = _reconcile(_metered(32.740, 69.530), 71.579, 88.642)
    assert r["status"] == mr.DISAGREES and "kWh" in r["reason"]
    assert (r["metered_import_kwh"], r["metered_export_kwh"]) == (
        pytest.approx(32.740),
        pytest.approx(69.530),
    )
    assert r["import_residual_kwh"] == pytest.approx(38.839, abs=1e-3)
    assert r["tolerance_kwh"] == pytest.approx(10.227, abs=1e-3)
    flipped = _reconcile(_metered(69.530, 32.740), 71.579, 88.642)
    assert flipped["status"] == mr.DISAGREES
    assert (
        max(abs(flipped["import_residual_kwh"]), abs(flipped["export_residual_kwh"]))
        > 19.0
    )
    assert _reconcile(_metered(30.0, 70.0), 45.0, 70.0)["status"] == mr.DISAGREES

    # test_no_meter_or_too_little_of_its_history_is_not_evidence
    for stale in [10.5, 50.0, None]:
        assert _reconcile(_metered(32.7, 69.5), 71.6, 88.6, stale=stale)["status"] == (
            mr.INSUFFICIENT_COVERAGE
        )
        none = _reconcile(None, 71.579, 88.642, stale=None, sensor=None)
        assert none["status"] == mr.NOT_CHECKED
        assert none["reconstructed_import_kwh"] == pytest.approx(71.579)
        assert (
            mr.TOLERANCE_FRACTION,
            mr.TOLERANCE_FLOOR_KWH,
            mr.MAX_STALE_PERIODS_PCT,
        ) == (
            0.10,
            2.0,
            10.0,
        )

    # test_each_batterys_leftover_is_credited_and_mismatched_lists_are_refused
    zero = np.zeros(1)
    a, b = _small("a", salvage_value=0.10), _small("b", salvage_value=0.20)
    cost = _fleet_cost([a, b], [zero, zero], [zero, zero], [10.0, 20.0], n=1, load=0.0)
    assert cost.total_cost == pytest.approx(-5.0, abs=1e-6)
    with pytest.raises(ValueError):
        _fleet_cost([_small()], [np.zeros(2), np.zeros(2)], [np.zeros(2)], [10.0])

    # test_curtailment_on_a_negative_price_hour_is_explained_too
    imp, exp = np.full(N, 0.30), np.full(N, 0.05)
    imp[12], exp[12] = -0.157, -0.160
    solar = np.zeros(N)
    solar[9:16] = 30.0
    r = _explained(_jstar_report(grid=_grid(imp=imp, exp=exp), solar=solar))
    assert abs(r.j_star_path_delta_unexplained) < 0.01

    # test_a_flat_day_is_never_both_perfect_and_flagged
    for delta in [0.0, 1e-12, 1e-10, 5e-10, 1e-9, 1e-8, 1e-6, 1.4411]:
        for sign in [1.0, -1.0]:
            r = compute_epr(j_ref=3.0, j_ach=2.0, j_star=3.0 + sign * delta)
            degenerate = r.epr == 1.0 and abs(r.theoretical_maximum_yield) < 1e-9
            assert not (degenerate and r.denominator_reason is not None)

    # test_each_unreliable_condition_warns_from_its_own_set
    # test_each_unreliable_condition_warns_from_its_own_set
    sets = {
        id(sw._QUALITY_REPORT_UNRELIABLE_WARNED),
        id(sw._QUALITY_REPORT_NEGATIVE_REGRET_WARNED),
        id(sw._QUALITY_REPORT_EPR_DENOMINATOR_WARNED),
    }
    assert len(sets) == 3

    # test_the_cap_widens_to_the_groups_peak_simultaneous_draw_only
    for members, expected in [
        (
            [
                ("a", "g", 25.0, [5, 5, 5, 0, 0, 0]),
                ("b", "g", 25.0, [5, 5, 0, 0, 0, 0]),
            ],
            [25.0, 25.0],
        ),
        (
            [
                ("a", "g", 25.0, [12.5, 0, 0, 0, 0, 0]),
                ("b", "g", 25.0, [12.5, 0, 0, 0, 0, 0]),
            ],
            [25.0, 25.0],
        ),
        (
            [
                ("a", "g", 25.0, [15, 15, 0, 0, 0, 0]),
                ("b", "g", 25.0, [14, 12, 0, 0, 0, 0]),
            ],
            [29.0, 29.0],
        ),
        (
            [
                ("a", "g", 5.0, [10, 10, 10, 10, 0, 0]),
                ("b", "g", 5.0, [10, 10, 10, 10, 0, 0]),
            ],
            [20.0, 20.0],
        ),
        (
            [("a", "g", 10.0, [15, 0, 0, 0, 0, 0])],
            [10.0],
        ),  # a group of one is a per-battery limit
        ([("home", None, None, [99] * 6)], [None]),
        ([("a", "g", None, [9] * 6), ("b", "g", None, [9] * 6)], [None, None]),
        (
            [
                ("a", "g", 10.0, [12] * 6),
                ("b", "g", 10.0, [1] * 6),
                ("c", "s", 10.0, [30] * 6),
                ("d", "s", 10.0, [1] * 6),
            ],
            [13.0, 13.0, 31.0, 31.0],
        ),
        ([], []),
    ]:
        assert _caps(members) == expected

    # test_no_controllable_loads_scores_exactly_as_before
    omitted = _cl_report()
    for other in (_cl_report(None), _cl_report(OracleControllableLoads())):
        assert [getattr(other, k) for k in SCALARS] == [
            getattr(omitted, k) for k in SCALARS
        ]
    assert (
        omitted.oracle_controllable_loads_scored,
        omitted.oracle_controllable_loads_skipped,
    ) == ((), ())
    assert omitted.oracle_controllable_loads_unsubtracted_kwh == 0.0

    # test_the_bundle_cannot_be_built_inconsistently
    with pytest.raises(ValueError, match="double-count"):
        OracleControllableLoads(adequacy=(_dishwasher(4.0),))
    with pytest.raises(ValueError, match="never give it back"):
        OracleControllableLoads(delivered_kwh_by_period=_ran((18, 19), 2.0))
    ok = _bundle(skipped=(("tank", "kind=thermal"),))
    assert len(ok.adequacy) == 1 and ok.skipped == (("tank", "kind=thermal"),)

    # test_a_sheddable_load_is_accepted_too
    profile = np.zeros(N)
    profile[12:16] = 1.5
    pool = SheddableLoadConfig(name="pool", forecast_kw=profile, shed_cost=0.45)
    r = _cl_report(
        OracleControllableLoads(
            sheddable=(pool,), delivered_kwh_by_period=profile * HOURS
        )
    )
    assert r.oracle_controllable_loads_scored == ("pool",)

    # test_energy_the_house_never_drew_is_published_not_absorbed: 12 kWh claimed across two 2 kW hours: only 4 can be removed.
    r = _cl_report(_bundle(kwh=6.0))
    assert r.oracle_controllable_loads_unsubtracted_kwh == pytest.approx(8.0, abs=1e-4)

    # test_each_hours_values_are_published_for_an_outside_check
    (row,) = _stats(55.0, 50.0)["soc_discrepancy_hourly"]
    expected = {
        "hour": H0.isoformat(),
        "real_pct": 55.0,
        "ach_pct": 50.0,
        "gap_pct": 5.0,
        "out_of_range": False,
        "boundary_exempted": False,
    }
    assert {k: row[k] for k in expected} == expected
    over = _stats(60.0, 327.67)["soc_discrepancy_hourly"][0]
    assert over["out_of_range"] and over["gap_pct"] == 40.0
    edge = _stats(0.0, -9.0)["soc_discrepancy_hourly"][0]
    assert edge["out_of_range"] and edge["boundary_exempted"]
    h1 = H0 + timedelta(hours=1)
    two = sw._soc_discrepancy_stats(
        [([(H0, 55.0), (h1, 40.0)], 100.0)],
        {H0.isoformat(): {"soc_pct": 50.0}, h1.isoformat(): {"soc_pct": 20.0}},
    )["soc_discrepancy_hourly"]
    assert {r["hour"]: r["gap_pct"] for r in two} == {
        H0.isoformat(): 5.0,
        h1.isoformat(): 20.0,
    }
    assert sw._soc_discrepancy_stats([], {})["soc_discrepancy_hourly"] is None
    assert _stats(55.0, None)["soc_discrepancy_hourly"] is None

    # test_beating_the_oracle_below_its_own_floor_is_named_and_measured_in_kwh
    s = _feasibility(DRIFTED, -0.73)
    assert (s["regret_reliable"], s["achieved_within_lp_soc_bounds"]) == (False, False)
    assert s["epr_reason"] == "oracle_beaten_achieved_outside_lp_soc_bounds"
    assert s["achieved_soc_min_pct"] == pytest.approx(0.5)
    assert s["achieved_below_floor_kwh"] == pytest.approx(1.5)
    assert s["achieved_above_ceiling_kwh"] == 0.0
    # A 0-100 % check would have passed the same day: the floor is the LP's.
    assert _feasibility(DRIFTED, -0.73, min_pct=0.0)["achieved_within_lp_soc_bounds"]

    # test_without_a_trajectory_the_regret_verdict_still_stands
    s = sw._achieved_feasibility_stats(
        {}, -0.5, min_pct=2.0, max_pct=100.0, capacity_kwh=100.0
    )
    assert (s["regret_reliable"], s["epr_reason"]) == (False, "oracle_beaten")
    assert (
        s["achieved_within_lp_soc_bounds"] is None
        and s["achieved_below_floor_kwh"] is None
    )
    assert (
        _feasibility([10.0, 20.0], -0.5, capacity=0.0)["achieved_soc_min_pct"] is None
    )
    rows = {"a": {"soc_pct": 10.0}, "b": {"battery_kw": 1.0}}
    assert (
        sw._achieved_feasibility_stats(
            rows, 1.0, min_pct=2.0, max_pct=100.0, capacity_kwh=100.0
        )["achieved_soc_min_pct"]
        == 10.0
    )


# --- end to end: achieved never beats the oracle ---------------------------------------------


def _sub_floor():
    exp = np.full(N, 0.05)
    exp[20] = 0.45
    initial = MIN_SOC + 3.0
    d = np.zeros(N)
    d[20] = (initial - 0.544) * 0.95  # down to 0.445 % against a 2 % floor
    return _battery(initial=initial, max_charge_kw=0.0), d, _grid(imp=0.30, exp=exp)


def _over_delivery():
    exp = np.full(N, 0.05)
    exp[17:24] = 0.45
    d = np.zeros(N)
    d[17:24] = 13.77  # 12.77 kW exported plus the 1 kW house, against a 12 kW pin
    initial = MIN_SOC + float(np.sum(d)) / 0.95 + 5.0
    return (
        _battery(initial=initial, max_charge_kw=0.0),
        d,
        _grid(imp=0.30, exp=exp, pin=_pin(12.0, range(17, 24))),
    )


def _soc_path(battery, d):
    return battery.initial_soc_kwh + np.cumsum(-d / battery.discharge_efficiency)


def _under_delivery():
    imp = np.full(N, 0.13)
    imp[17:24] = 0.37
    exp = np.full(N, 0.02)
    exp[17:24] = 0.075
    d = np.zeros(N)
    d[17:24] = 1.0  # the commitment ignored
    return _battery(initial=CAP * 0.30), d, imp, exp


# --- the SoC discrepancy compares like with like (#1228) --------------------------------------


BRIS = ZoneInfo("Australia/Brisbane")
SEP24 = datetime(2026, 9, 24, 0, 0, tzinfo=BRIS)


def _quarters(n_hours):
    return np.full(n_hours * 4, 0.25)


def _bounds(hours, soc, initial, day=SEP24):
    return _soc_pct_at_hour_boundaries(
        hours=hours, soc_pct=soc, initial_soc_pct=initial, day_start=day
    )


def _means(hours, soc, day=SEP24):
    return _hourly_means_by_key(hours=hours, per_period={"soc_pct": soc}, day_start=day)


def _key(h, day=SEP24):
    return (day + timedelta(hours=h)).isoformat()


def _ramp(rate=12.0, n_hours=6):
    hours = _quarters(n_hours)
    soc = 10.0 + rate * (np.arange(len(hours)) + 1) * 0.25
    real = [(SEP24, 10.0)] + [
        (SEP24 + timedelta(minutes=15 * (i + 1)), float(v)) for i, v in enumerate(soc)
    ]
    return _means(hours, soc), _bounds(hours, soc, 10.0), real


# --- the reconstruction is checked against the grid meter (#1465) -----------------------------


def _metered(imp, exp):
    return [imp / 12.0] * 12 + [-exp / 12.0] * 12


def _reconcile(metered, imp, exp, stale=0.0, sensor="sensor.grid"):
    return mr.reconcile(
        meter_sensor=sensor,
        metered_kw=metered,
        period_hours=[1.0] * 24 if metered is not None else None,
        stale_periods_pct=stale,
        reconstructed_import_kwh=imp,
        reconstructed_export_kwh=exp,
    )


# --- achieved cost for a fleet on one grid connection (#768) ----------------------------------


def _small(name="battery", **kw):
    return _battery(
        name=name,
        capacity_kwh=30.0,
        initial=15.0,
        min_soc_kwh=2.0,
        max_soc_kwh=30.0,
        max_charge_kw=15.0,
        max_discharge_kw=15.0,
        **kw,
    )


def _fleet_cost(batteries, charge, discharge, final, n=2, load=1.0, exp=0.05):
    return evaluate_realized_cost_multi(
        hours=np.full(n, 1.0),
        load_real_kw=np.full(n, load),
        solar_real_kw=np.zeros(n),
        import_price_real=np.full(n, 0.30),
        export_price_real=np.full(n, exp),
        batteries=batteries,
        charge_committed_kw=charge,
        discharge_committed_kw=discharge,
        final_soc_kwh=final,
    )


# --- j_star priced two ways, and the difference explained (#1081, #1480) -----------------------


SEP15 = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)


def _jstar_battery():
    return _battery(
        capacity_kwh=100.0,
        initial=50.0,
        min_soc_kwh=2.0,
        max_soc_kwh=100.0,
        max_charge_kw=20.0,
        max_discharge_kw=20.0,
        discharge_cost=np.full(N, 0.01),
    )


def _jstar_report(grid=None, solar=None, oracle=None, real_p2p=0.0):
    grid = grid or _grid(imp=0.30, exp=0.05)
    if solar is None:
        solar = np.zeros(N)
        solar[8:15] = 8.0
    zero, battery = np.zeros(N), _jstar_battery()
    return compute_quality_report(
        periods=PeriodGrid(hours=HOURS, start=SEP15),
        grid_residual=grid,
        grid_oracle=oracle or grid,
        batteries=[battery],
        solar=SolarConfig(forecast_kw=solar),
        load=LoadConfig(name="house", forecast_kw=np.full(N, 1.0)),
        timestamps=[SEP15 + timedelta(hours=i) for i in range(N)],
        real_p2p_dollars_earned=real_p2p,
        commanded_charge_kw=[zero],
        commanded_discharge_kw=[zero],
        actual_charge_kw=[zero],
        actual_discharge_kw=[zero],
        final_soc_kwh_actual=[battery.initial_soc_kwh],
    )


def _explained(r):
    assert r.j_star_path_delta == pytest.approx(
        round(r.j_star - r.j_star_evaluator, 4), abs=1e-4
    )
    assert (
        r.j_star_path_delta_explained + r.j_star_path_delta_unexplained
        == pytest.approx(r.j_star_path_delta, abs=1e-3)
    )
    return r


def _plan(gi, ge, curt):
    return SimpleNamespace(
        grid_import_kw=np.array(gi, float),
        grid_export_kw=np.array(ge, float),
        solar_curtailed_kw=np.array(curt, float),
    )


# --- EPR's denominator is checked (#1089, #1104) ----------------------------------------------


J_REF, J_ACH, J_STAR, J_STAR_EVAL = 5.4897, 8.9872, 6.9308, 4.4419  # 17 Sep


# --- a shared charger's cap reaches the oracle, as wide as the day and no wider (#1109) --------


Z6 = np.zeros(6)


def _ev(name, group, cap):
    return BatteryConfig(
        name=name,
        capacity_kwh=60.0,
        initial_soc_kwh=30.0,
        min_soc_kwh=0.0,
        max_soc_kwh=60.0,
        max_charge_kw=15.0,
        max_discharge_kw=15.0,
        charge_efficiency=0.97,
        discharge_efficiency=0.97,
        charge_cost=0.01,
        discharge_cost=0.01,
        salvage_value=0.0,
        shared_charger_group=group,
        shared_charger_max_kw=cap,
    )


def _caps(members):
    rows = [
        (_ev(n, g, cap), np.asarray(c, float), Z6, 0.0, []) for n, g, cap, c in members
    ]
    return [
        r[0].shared_charger_max_kw
        for r in participants._widen_shared_charger_cap_to_achieved(rows)
    ]


# --- the oracle can re-time the day's controllable loads, and only the oracle (#1357) ----------


def _cl_report(controllable=..., house=2.0):
    imp = np.full(N, 0.30)
    imp[1:5] = 0.10
    grid = _grid(imp=imp, exp=0.05)
    solar = np.zeros(N)
    solar[9:15] = 6.0
    battery = _jstar_battery()
    zero = np.zeros(N)
    kw = {}
    if controllable is not ...:
        kw["controllable_loads"] = controllable
    return compute_quality_report(
        periods=PeriodGrid(hours=HOURS, start=SEP15),
        grid_residual=grid,
        grid_oracle=grid,
        batteries=[battery],
        solar=SolarConfig(forecast_kw=solar),
        load=LoadConfig(name="house", forecast_kw=np.full(N, house)),
        timestamps=[SEP15 + timedelta(hours=i) for i in range(N)],
        real_p2p_dollars_earned=0.0,
        commanded_charge_kw=[zero],
        commanded_discharge_kw=[zero],
        actual_charge_kw=[zero],
        actual_discharge_kw=[zero],
        final_soc_kwh_actual=[battery.initial_soc_kwh],
        **kw,
    )


def _ran(periods, kwh):
    a = np.zeros(N)
    a[list(periods)] = kwh
    return a


def _dishwasher(target):
    return AdequacyLoadConfig(
        name="dishwasher",
        max_power_kw=2.0,
        target_kwh=target,
        earliest_period=0,
        deadline_period=N - 1,
        shortfall_price=4.0,
    )


def _bundle(periods=(18, 19), kwh=2.0, **kw):
    delivered = _ran(periods, kwh)
    return OracleControllableLoads(
        adequacy=(_dishwasher(float(delivered.sum())),),
        delivered_kwh_by_period=delivered,
        **kw,
    )


SCALARS = (
    "j_ref",
    "j_ach",
    "j_star",
    "tracking_cost",
    "p2p_commitment_shortfall_kwh",
    "j_star_evaluator",
    "j_star_path_delta",
)


# --- the SoC discrepancy statistic itself (#427, #445, #571, #681, #949) ------------------------


H0 = datetime(2026, 8, 24, 0, 0, tzinfo=sw.LOCAL_TZ)


def _stats(real, ach, capacity=100.0):
    rows = {H0.isoformat(): {} if ach is None else {"soc_pct": ach}}
    return sw._soc_discrepancy_stats([([(H0, real)], capacity)], rows)


# --- a negative regret is a broken comparison, not a score (#956) -----------------------------


def _feasibility(soc_pcts, regret, min_pct=2.0, max_pct=100.0, capacity=100.0):
    hourly = {
        f"2026-09-15T{h:02d}:00:00+10:00": {"soc_pct": v}
        for h, v in enumerate(soc_pcts)
    }
    return sw._achieved_feasibility_stats(
        hourly, regret, min_pct=min_pct, max_pct=max_pct, capacity_kwh=capacity
    )


DRIFTED = [16.0, 12.0, 5.0, 4.0, 10.0, 40.0, 80.0, 60.0, 30.0, 0.5]  # ends below 2 %
