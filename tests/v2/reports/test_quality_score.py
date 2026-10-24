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
from solver import epr as epr_mod
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
from solver.epr import compute_epr, denominator_reason
from solver.p2p_export import grid_export_bounds
from solver.quality_report import (
    _MIN_POSITIVE_SOC_KWH,
    OracleControllableLoads,
    _curtailment_repricing,
    _hourly_means_by_key,
    _soc_envelope_containing_achieved,
    _soc_pct_at_hour_boundaries,
    _widen_export_pin_to_achieved,
    compute_quality_report,
)
from solver.regret import evaluate_realized_cost, evaluate_realized_cost_multi
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


def test_a_day_inside_the_envelope_leaves_it_exactly_as_configured():
    assert _envelope(_battery(), discharge=1.0) == (MIN_SOC, CAP)


def test_the_envelope_moves_only_on_the_side_the_battery_crossed():
    low = _battery(initial=MIN_SOC + 1.9)
    d = np.zeros(N)
    d[:2] = 1.0
    floor, ceiling = _envelope(low, discharge=d)
    assert floor == pytest.approx(low.initial_soc_kwh - 2.0 / 0.95, abs=1e-9)
    assert ceiling == CAP
    high = _battery(initial=CAP - 6.0, max_soc_kwh=CAP - 5.0)
    c = np.zeros(N)
    c[0] = 2.0
    floor, ceiling = _envelope(high, charge=c)
    assert ceiling == pytest.approx(CAP - 6.0 + 2.0 * 0.95, abs=1e-9)
    assert floor == MIN_SOC


@pytest.mark.parametrize(
    ("initial", "charge", "discharge", "expected"),
    [
        (
            1.0,
            0.0,
            np.r_[0.95, np.zeros(N - 1)],
            (_MIN_POSITIVE_SOC_KWH, None),
        ),  # emptied exactly
        (1.0, 0.0, 5.0, (_MIN_POSITIVE_SOC_KWH, None)),  # sensor nonsense below zero
        (CAP - 1.0, 40.0, 0.0, (None, CAP)),  # above capacity
    ],
    ids=["reaches_zero", "below_zero", "above_capacity"],
)
def test_physical_bounds_still_win_over_the_reconstruction(
    initial, charge, discharge, expected
):
    floor, ceiling = _envelope(_battery(initial=initial), charge, discharge)
    want_floor, want_ceiling = expected
    if want_floor is not None:
        assert floor == want_floor
    if want_ceiling is not None:
        assert ceiling == want_ceiling


@pytest.mark.parametrize("initial", [0.5, MIN_SOC, 60.0, CAP - 0.5])
@pytest.mark.parametrize(("c", "d"), [(0.0, 0.0), (40.0, 0.0), (0.0, 40.0), (1.0, 1.0)])
def test_the_widened_envelope_always_builds_a_battery(initial, c, d):
    battery = _battery(initial=max(MIN_SOC, initial))
    floor, ceiling = _envelope(battery, c, d)
    assert 0.0 < floor <= ceiling <= CAP
    _battery(
        initial=min(max(battery.initial_soc_kwh, floor), ceiling),
        min_soc_kwh=floor,
        max_soc_kwh=ceiling,
    )


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
    for grid in (_grid(), _grid(pin=np.full(N, np.nan))):
        assert _widen(grid, 5.0) is grid


@pytest.mark.parametrize(
    ("delivered", "lo", "hi"),
    [(12.0, 12.0, 12.0), (12.77, 12.0, 12.77), (9.0, 9.0, 12.0)],
    ids=["exact", "over", "under"],
)
def test_the_band_stretches_to_what_was_delivered(delivered, lo, hi):
    widened = _widen(_grid(pin=_pin(12.0, range(17, 24))), delivered)
    assert widened.fixed_export_kw[17:24] == pytest.approx([lo] * 7)
    assert widened.fixed_export_max_kw[17:24] == pytest.approx([hi] * 7)
    assert (
        np.isnan(widened.fixed_export_kw[:17]).all()
        and np.isnan(widened.fixed_export_max_kw[:17]).all()
    )


@pytest.mark.parametrize("delivered", [0.0, 5.0, 12.0, 12.4, 12.5, 99.0, 400.0])
def test_the_band_stays_valid_never_negative_never_inverted_never_over_the_limit(
    delivered,
):
    grid = _grid(pin=_pin(12.0, range(17, 24)), export_limit=12.5)
    widened = _widen(grid, delivered)
    lo, hi = widened.fixed_export_kw[17:24], widened.fixed_export_max_kw[17:24]
    assert (lo >= 0.0).all() and (lo <= hi).all() and (hi <= 12.5 + 1e-9).all()
    _grid(
        pin=widened.fixed_export_kw, band=widened.fixed_export_max_kw, export_limit=12.5
    )


def test_the_export_bounds_read_the_band_only_where_pinned():
    pinned = _grid(pin=_pin(12.0, range(17, 24)))
    assert grid_export_bounds(20, pinned, 42.0) == (12.0, 12.0)
    banded = _grid(pin=_pin(12.0, range(17, 24)), band=_pin(12.77, range(17, 24)))
    assert grid_export_bounds(20, banded, 42.0) == (12.0, 12.77)
    assert grid_export_bounds(3, banded, 42.0) == (0.0, 42.0)
    always = _grid(pin=_pin(12.0, range(24)), band=_pin(12.77, range(24)))
    assert grid_export_bounds(0, always, 42.0, override_p2p=True) == (
        12.0,
        42.0,
    )  # #694


@pytest.mark.parametrize(
    "bad",
    [
        {"band": _pin(12.77, range(17, 24))},  # a band without a pin
        {"pin": _pin(12.0, range(17, 24)), "band": np.full(N + 1, np.nan)},
        {
            "pin": _pin(12.0, range(17, 24)),
            "band": _pin(11.0, range(17, 24)),
        },  # inverted
        {
            "pin": _pin(12.0, range(17, 24)),
            "band": _pin(20.0, range(17, 24)),
            "export_limit": 12.5,
        },
    ],
    ids=["no_pin", "wrong_length", "inverted", "over_limit"],
)
def test_an_invalid_band_is_refused(bad):
    with pytest.raises(ValueError):
        _grid(**bad)
    _grid(pin=_pin(12.0, range(17, 24)), band=np.full(N, np.nan))  # all-nan is fine


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


def test_the_fixtures_cross_exactly_the_bound_each_is_about():
    b, d, _ = _sub_floor()
    assert _soc_path(b, d).min() < MIN_SOC
    b, d, _ = _over_delivery()
    assert _soc_path(b, d).min() > MIN_SOC


@pytest.mark.parametrize(
    "case", [_sub_floor, _over_delivery], ids=["sub_floor", "p2p_over_delivery"]
)
def test_achieved_never_beats_perfect_foresight_and_j_ach_is_what_the_battery_did(case):
    battery, d, grid = case()
    report = _report(battery, grid, discharge=d)
    assert report.epr.uplift_available >= 0.0 and report.epr.epr <= 1.0 + 1e-9
    direct = evaluate_realized_cost_multi(
        hours=HOURS,
        load_real_kw=np.full(N, 1.0),
        solar_real_kw=np.zeros(N),
        import_price_real=grid.import_price,
        export_price_real=grid.export_price,
        batteries=[battery],
        charge_committed_kw=[np.zeros(N)],
        discharge_committed_kw=[d],
        final_soc_kwh=[battery.initial_soc_kwh - float(np.sum(d)) / 0.95],
    )
    assert report.j_ach == pytest.approx(
        direct.total_cost, abs=1e-9
    )  # option B leaves j_ach alone


def _under_delivery():
    imp = np.full(N, 0.13)
    imp[17:24] = 0.37
    exp = np.full(N, 0.02)
    exp[17:24] = 0.075
    d = np.zeros(N)
    d[17:24] = 1.0  # the commitment ignored
    return _battery(initial=CAP * 0.30), d, imp, exp


def test_a_loss_making_commitment_skipped_neither_beats_the_oracle_nor_hides():
    """#1001: committed export at 7.5c against 37c import is a loss the
    oracle could not decline; skipping it must not beat perfect foresight,
    and the shortfall is reported instead."""
    battery, d, imp, exp = _under_delivery()
    report = _report(
        battery, _grid(imp=imp, exp=exp, pin=_pin(12.0, range(17, 24))), discharge=d
    )
    assert report.epr.uplift_available >= 0.0
    assert report.j_star <= report.j_ref
    assert report.p2p_commitment_shortfall_kwh > 80.0  # ~12 kW x 7 h owed
    assert (
        _report(
            battery, _grid(imp=imp, exp=exp), discharge=d
        ).p2p_commitment_shortfall_kwh
        == 0.0
    )


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


def test_an_hours_sample_is_the_soc_as_it_begins():
    """Hour 0 is the initial SoC; later hours the end of the period before
    (the per-period array is end-of-period)."""
    quarter = _bounds(_quarters(3), np.arange(12, dtype=float), -1.0)
    assert [quarter[_key(h)] for h in range(3)] == [-1.0, 3.0, 7.0]
    hourly = _bounds(np.full(3, 1.0), np.array([10.0, 20.0, 30.0]), 5.0)
    assert [hourly[_key(h)] for h in range(3)] == [5.0, 10.0, 20.0]
    assert _bounds(np.array([]), np.array([]), 1.0) == {}


@pytest.mark.parametrize(
    ("n_hours", "day"),
    [(24, SEP24), (30, SEP24), (4, datetime(2026, 9, 24, tzinfo=UTC))],
    ids=["day", "longer_window", "utc"],
)
def test_the_samples_and_the_hourly_rows_share_their_keys(n_hours, day):
    hours = _quarters(n_hours)
    soc = np.linspace(0.0, 100.0, len(hours))
    rows, bounds = _means(hours, soc, day), _bounds(hours, soc, 0.0, day)
    assert list(rows) == list(bounds) and len(rows) == n_hours


def _ramp(rate=12.0, n_hours=6):
    hours = _quarters(n_hours)
    soc = 10.0 + rate * (np.arange(len(hours)) + 1) * 0.25
    real = [(SEP24, 10.0)] + [
        (SEP24 + timedelta(minutes=15 * (i + 1)), float(v)) for i, v in enumerate(soc)
    ]
    return _means(hours, soc), _bounds(hours, soc, 10.0), real


def test_a_mean_sits_most_of_a_ramp_above_the_hours_start():
    rows, bounds, _ = _ramp()
    offsets = {k: rows[k]["soc_pct"] - bounds[k] for k in rows}
    assert list(offsets.values()) == pytest.approx([12.0 * 0.625] * 6)


def test_a_sensor_tracking_the_ramp_exactly_agrees_only_when_compared_like_for_like():
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


def test_a_real_disagreement_is_still_caught_and_absence_names_no_basis():
    rows, bounds, real = _ramp(n_hours=4)
    shifted = [(t, v + 20.0) for t, v in real]
    out = sw._soc_discrepancy_stats(
        [(shifted, 100.0)], rows, ach_soc_pct_at_hour=bounds
    )
    assert float(out["soc_discrepancy_max_pct"]) > 15.0
    assert (out["soc_discrepancy_reliable"], out["soc_discrepancy_reason"]) == (
        False,
        "disagreement",
    )
    none = sw._soc_discrepancy_stats([], {}, ach_soc_pct_at_hour={})
    assert none["soc_discrepancy_max_pct"] is None
    assert "soc_discrepancy_basis" in none and none["soc_discrepancy_basis"] is None


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


def test_a_reconstruction_double_counting_the_meter_disagrees_either_sign():
    """5 Oct: reconstructed 71.6 / 88.6 kWh against a meter's 32.7 / 69.5."""
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


@pytest.mark.parametrize(
    ("metered", "imp", "exp", "sign"),
    [
        (_metered(30.0, 70.0), 31.5, 66.5, "positive_is_import"),  # 5 % apart
        (_metered(40.0, 20.0), 20.0, 40.0, "positive_is_export"),
        (_metered(1.0, 2.0), 2.5, 0.5, None),  # the floor, on a quiet day
    ],
    ids=["within_tolerance", "export_positive_meter", "quiet_day"],
)
def test_agreement_within_the_declared_tolerance(metered, imp, exp, sign):
    r = _reconcile(metered, imp, exp)
    assert r["status"] == mr.AGREES and "reason" not in r
    if sign:
        assert r["meter_sign"] == sign
    else:
        assert r["tolerance_kwh"] == mr.TOLERANCE_FLOOR_KWH


@pytest.mark.parametrize("stale", [10.5, 50.0, None])
def test_no_meter_or_too_little_of_its_history_is_not_evidence(stale):
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


@pytest.mark.parametrize(
    "status", [mr.AGREES, mr.NOT_CHECKED, mr.INSUFFICIENT_COVERAGE, None]
)
def test_only_a_disagreeing_meter_changes_the_epr_verdict(status):
    assert sw._epr_reliability(True, True, None, mr.DISAGREES) is False
    assert sw._epr_reliability(None, True, None, mr.DISAGREES) is False
    assert sw._epr_reliability(True, True, None, status) is True
    assert sw._epr_reliability(None, True, None, status) is None
    assert sw._epr_reliability(False, True, None, status) is False


def test_the_history_row_names_the_meter_unless_the_soc_failed_too():
    day = {
        "epr_reliable": False,
        "regret_reliable": True,
        "epr_denominator_reason": None,
        "soc_discrepancy_reliable": True,
        "epr_reason": "grid_meter_disagrees",
        "meter_reconciliation": {"status": mr.DISAGREES},
    }
    assert sw._epr_reliability_code(day) == sw._RELIABILITY_METER == "m"
    day.update(
        soc_discrepancy_reliable=False,
        epr_reason="achieved_soc_unreliable:disagreement",
    )
    assert sw._epr_reliability_code(day) == sw._RELIABILITY_SOC


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


def test_one_battery_costs_exactly_what_the_single_battery_evaluator_says():
    n = 6
    battery = _small(salvage_value=0.05)
    charge = np.array([0.0, 0.0, 5.0, 5.0, 0.0, 0.0])
    discharge = np.array([2.0, 2.0, 0.0, 0.0, 3.0, 3.0])
    single = evaluate_realized_cost(
        hours=np.full(n, 1.0),
        load_real_kw=np.full(n, 3.0),
        solar_real_kw=np.zeros(n),
        import_price_real=np.full(n, 0.30),
        export_price_real=np.full(n, 0.08),
        charge_committed_kw=charge,
        discharge_committed_kw=discharge,
        charge_cost=battery.charge_cost,
        discharge_cost=battery.discharge_cost,
        final_soc_kwh=18.0,
        salvage_value=battery.salvage_value,
        grid_import_limit_kw=30.0,
        grid_export_limit_kw=30.0,
    )
    multi = _fleet_cost(
        [battery], [charge], [discharge], [18.0], n=n, load=3.0, exp=0.08
    )
    assert multi.total_cost == pytest.approx(single.total_cost, abs=1e-9)
    for field in ("grid_import_kw", "grid_export_kw", "cost_per_period"):
        np.testing.assert_allclose(getattr(single, field), getattr(multi, field))


def test_a_fleet_nets_on_one_grid_and_prices_each_battery_at_its_own_cost():
    cheap, pricey = (
        _small("cheap", discharge_cost=0.01),
        _small("pricey", discharge_cost=0.20),
    )
    zero, four = np.zeros(2), np.full(2, 4.0)
    a = _fleet_cost([cheap], [zero], [four], [10.0]).total_cost
    b = _fleet_cost([pricey], [zero], [four], [10.0]).total_cost
    assert b - a == pytest.approx(1.52, abs=1e-6)
    fleet = _fleet_cost([cheap, pricey], [zero, zero], [four, four], [10.0, 10.0])
    np.testing.assert_allclose(fleet.grid_export_kw, 7.0)
    np.testing.assert_allclose(fleet.grid_import_kw, 0.0)
    assert fleet.total_cost == pytest.approx(
        -0.05 * 7.0 * 2 + (0.01 + 0.20) * 4.0 * 2, abs=1e-6
    )


def test_each_batterys_leftover_is_credited_and_mismatched_lists_are_refused():
    zero = np.zeros(1)
    a, b = _small("a", salvage_value=0.10), _small("b", salvage_value=0.20)
    cost = _fleet_cost([a, b], [zero, zero], [zero, zero], [10.0, 20.0], n=1, load=0.0)
    assert cost.total_cost == pytest.approx(-5.0, abs=1e-6)
    with pytest.raises(ValueError):
        _fleet_cost([_small()], [np.zeros(2), np.zeros(2)], [np.zeros(2)], [10.0])


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


def test_the_lp_and_the_evaluator_differ_only_by_the_lps_own_terms():
    """No bonus: a real gap (the soft-SoC penalty and the #731 / #692
    tie-breaks the evaluator never pays), all of it explained."""
    r = _explained(_jstar_report())
    assert isinstance(r.j_star_evaluator, float) and r.j_star_evaluator != 0.0
    assert abs(r.j_star_path_delta) > 0.1
    assert abs(r.j_star_path_delta_unexplained) < 0.01


def test_a_bonus_priced_oracle_still_reprices_to_a_finite_delta():
    bonus = np.zeros(N)
    bonus[17:24] = 0.20
    oracle = GridConfig(
        import_price=np.full(N, 0.30),
        export_price=np.full(N, 0.05),
        import_limit_kw=42.0,
        export_limit_kw=42.0,
        export_bonus_price=bonus,
        export_bonus_volume_kwh=20.0,
    )
    r = _explained(_jstar_report(oracle=oracle, real_p2p=3.0))
    assert np.isfinite(r.j_star_path_delta)


def test_curtailment_on_a_negative_price_hour_is_explained_too():
    imp, exp = np.full(N, 0.30), np.full(N, 0.05)
    imp[12], exp[12] = -0.157, -0.160
    solar = np.zeros(N)
    solar[9:16] = 30.0
    r = _explained(_jstar_report(grid=_grid(imp=imp, exp=exp), solar=solar))
    assert abs(r.j_star_path_delta_unexplained) < 0.01


def test_the_negative_price_day_really_curtails(monkeypatch):
    seen = {}
    real = qr.build_plan

    def spy(*a, **kw):
        seen["plan"], seen["grid"] = real(*a, **kw), kw["grid"]
        return seen["plan"]

    monkeypatch.setattr(qr, "build_plan", spy)
    test_curtailment_on_a_negative_price_hour_is_explained_too()
    term = _curtailment_repricing(
        seen["plan"],
        hours=HOURS,
        import_price=seen["grid"].import_price,
        export_price=seen["grid"].export_price,
    )
    assert float(np.sum(seen["plan"].solar_curtailed_kw)) > 1.0 and abs(term) > 0.05


def _plan(gi, ge, curt):
    return SimpleNamespace(
        grid_import_kw=np.array(gi, float),
        grid_export_kw=np.array(ge, float),
        solar_curtailed_kw=np.array(curt, float),
    )


@pytest.mark.parametrize(
    ("plan", "hours", "imp", "exp", "expected"),
    [
        (_plan([42.0], [0.0], [4.83]), [0.5], [-0.1572], [-0.16], 0.5 * 4.83 * -0.1572),
        (
            _plan([1.0], [0.0], [3.0]),
            [1.0],
            [0.30],
            [0.05],
            0.30 * 1.0 + 0.05 * 2.0,
        ),  # crosses
        (_plan([5.0, 0.0], [0.0, 3.0], [0.0, 0.0]), [1, 1], 0.3, 0.05, 0.0),
    ],
    ids=["importing", "crosses_to_export", "none"],
)
def test_the_curtailment_term_is_exact(plan, hours, imp, exp, expected):
    term = _curtailment_repricing(plan, hours=hours, import_price=imp, export_price=exp)
    assert term == pytest.approx(expected, abs=1e-6)


# --- EPR's denominator is checked (#1089, #1104) ----------------------------------------------


J_REF, J_ACH, J_STAR, J_STAR_EVAL = 5.4897, 8.9872, 6.9308, 4.4419  # 17 Sep


@pytest.mark.parametrize(
    ("j_ref", "j_star", "flagged"),
    [
        (5.366, -22.0619, False),  # the oracle beats idle
        (J_REF, J_STAR, True),
        (0.0, 2.43, True),  # #1001's committed loss
        (3.0, 3.0, False),  # nothing to arbitrage
        (J_REF, J_STAR_EVAL, False),  # only the numerator negative
    ],
    ids=["normal", "17_sep", "committed_loss", "flat", "numerator_only"],
)
def test_an_oracle_no_better_than_idle_is_flagged_and_nothing_else(
    j_ref, j_star, flagged
):
    reason = denominator_reason(j_ref=j_ref, j_star=j_star)
    assert (reason == "oracle_not_better_than_idle") if flagged else reason is None
    if flagged:
        assert not any(
            w in reason
            for w in (
                "1081",
                "1001",
                "mismatch",
                "path",
                "commitment",
                "bug",
                "invalid",
            )
        )


def test_the_17_sep_day_published_its_242_percent_from_two_negatives():
    r = compute_epr(j_ref=J_REF, j_ach=J_ACH, j_star=J_STAR)
    assert r.denominator_reason == "oracle_not_better_than_idle"
    assert (r.value_captured, r.theoretical_maximum_yield, r.epr) == (
        pytest.approx(-3.4975, abs=1e-4),
        pytest.approx(-1.4411, abs=1e-4),
        pytest.approx(2.427, abs=1e-3),
    )
    honest = compute_epr(j_ref=J_REF, j_ach=J_ACH, j_star=J_STAR_EVAL)
    assert honest.denominator_reason is None
    assert (
        honest.theoretical_maximum_yield == pytest.approx(1.0478, abs=1e-4)
        and honest.epr < -3.0
    )
    clean = compute_epr(j_ref=5.366, j_ach=-20.8856, j_star=-22.0619)
    assert clean.denominator_reason is None and clean.epr == pytest.approx(
        0.9571, abs=1e-4
    )


@pytest.mark.parametrize("delta", [0.0, 1e-12, 1e-10, 5e-10, 1e-9, 1e-8, 1e-6, 1.4411])
@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_a_flat_day_is_never_both_perfect_and_flagged(delta, sign):
    r = compute_epr(j_ref=3.0, j_ach=2.0, j_star=3.0 + sign * delta)
    degenerate = r.epr == 1.0 and abs(r.theoretical_maximum_yield) < 1e-9
    assert not (degenerate and r.denominator_reason is not None)


def test_every_negative_denominator_divided_by_is_flagged():
    eps = epr_mod._DEGENERATE_YIELD_ABS
    for j_star in (eps, 2 * eps, 10 * eps, 1e-6, 1.4411):
        r = compute_epr(j_ref=0.0, j_ach=4.0, j_star=j_star)
        if abs(r.theoretical_maximum_yield) >= eps:
            assert r.denominator_reason is not None, j_star
    assert compute_epr(j_ref=3.0, j_ach=2.0, j_star=3.0).epr == 1.0
    assert eps < abs(J_REF - J_STAR) / 1e3


def test_each_unreliable_condition_warns_from_its_own_set():
    sets = {
        id(sw._QUALITY_REPORT_UNRELIABLE_WARNED),
        id(sw._QUALITY_REPORT_NEGATIVE_REGRET_WARNED),
        id(sw._QUALITY_REPORT_EPR_DENOMINATOR_WARNED),
    }
    assert len(sets) == 3


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


@pytest.mark.parametrize(
    ("members", "expected"),
    [
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
    ],
    ids=[
        "under",
        "at_the_cap",
        "over",
        "peak_not_total",
        "one_member",
        "no_group",
        "no_cap",
        "two_groups",
        "empty",
    ],
)
def test_the_cap_widens_to_the_groups_peak_simultaneous_draw_only(members, expected):
    assert _caps(members) == expected


def test_only_the_cap_moves():
    a, d = np.array([5.0, 5.0, 0, 0, 0, 0]), np.array([0, 0, 3.0, 0, 0, 0])
    ((_cfg, charge, discharge, final, _soc),) = (
        participants._widen_shared_charger_cap_to_achieved(
            [(_ev("ev1", "g", 25.0), a, d, 41.5, [])]
        )
    )
    np.testing.assert_array_equal(charge, a)
    np.testing.assert_array_equal(discharge, d)
    assert final == 41.5


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


def test_no_controllable_loads_scores_exactly_as_before():
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


def test_the_bundle_cannot_be_built_inconsistently():
    with pytest.raises(ValueError, match="double-count"):
        OracleControllableLoads(adequacy=(_dishwasher(4.0),))
    with pytest.raises(ValueError, match="never give it back"):
        OracleControllableLoads(delivered_kwh_by_period=_ran((18, 19), 2.0))
    ok = _bundle(skipped=(("tank", "kind=thermal"),))
    assert len(ok.adequacy) == 1 and ok.skipped == (("tank", "kind=thermal"),)


def test_only_the_oracle_sees_the_load_re_timed():
    plain, wired = (
        _cl_report(),
        _cl_report(_bundle(skipped=(("tank", "kind=thermal"),))),
    )
    for history in ("j_ach", "j_ref", "p2p_commitment_shortfall_kwh"):
        assert getattr(wired, history) == pytest.approx(
            getattr(plain, history), abs=1e-6
        )
    assert wired.oracle_controllable_loads_scored == ("dishwasher",)
    assert wired.oracle_controllable_loads_skipped == (("tank", "kind=thermal"),)


def test_freedom_given_to_the_oracle_never_makes_it_or_the_headline_worse():
    plain = _cl_report()
    for wired in (_cl_report(_bundle()), _cl_report(_bundle(periods=(19, 20)))):
        assert wired.j_star <= plain.j_star + 1e-6
        assert (
            wired.epr.theoretical_maximum_yield
            >= plain.epr.theoretical_maximum_yield - 1e-6
        )
        assert wired.oracle_controllable_loads_unsubtracted_kwh == 0.0


def test_a_sheddable_load_is_accepted_too():
    profile = np.zeros(N)
    profile[12:16] = 1.5
    pool = SheddableLoadConfig(name="pool", forecast_kw=profile, shed_cost=0.45)
    r = _cl_report(
        OracleControllableLoads(
            sheddable=(pool,), delivered_kwh_by_period=profile * HOURS
        )
    )
    assert r.oracle_controllable_loads_scored == ("pool",)


def test_energy_the_house_never_drew_is_published_not_absorbed():
    """12 kWh claimed across two 2 kW hours: only 4 can be removed."""
    r = _cl_report(_bundle(kwh=6.0))
    assert r.oracle_controllable_loads_unsubtracted_kwh == pytest.approx(8.0, abs=1e-4)


# --- the SoC discrepancy statistic itself (#427, #445, #571, #681, #949) ------------------------


H0 = datetime(2026, 8, 24, 0, 0, tzinfo=sw.LOCAL_TZ)


def _stats(real, ach, capacity=100.0):
    rows = {H0.isoformat(): {} if ach is None else {"soc_pct": ach}}
    return sw._soc_discrepancy_stats([([(H0, real)], capacity)], rows)


@pytest.mark.parametrize(
    ("real", "ach", "gap", "reliable", "reason"),
    [
        (55.0, 50.0, 5.0, True, None),
        (60.0, 327.67, 40.0, False, "out_of_range"),  # clamped, flagged
        (20.0, -85.0, 20.0, False, "out_of_range"),
        (0.0, -9.0, 0.0, True, None),  # integration loss at a real empty pack
        (100.0, 104.5, None, True, None),  # and at a full one
        (2.0, -9.0, None, False, "out_of_range"),  # just past the edge
        (-5.0, -9.0, None, False, "out_of_range"),  # the sensor itself out of range
    ],
    ids=[
        "in_range",
        "over_100",
        "below_0",
        "empty_edge",
        "full_edge",
        "past_edge",
        "sensor_out",
    ],
)
def test_the_gap_is_bounded_and_flagged_only_away_from_a_real_edge(
    real, ach, gap, reliable, reason
):
    out = _stats(real, ach)
    if gap is not None:
        assert out["soc_discrepancy_max_pct"] == gap
    assert out["soc_discrepancy_reliable"] is reliable
    assert out["soc_discrepancy_reason"] == reason


def test_both_sides_out_of_range_never_exceed_100():
    out = _stats(-40.0, 500.0)
    assert (
        out["soc_discrepancy_max_pct"] <= 100.0 and not out["soc_discrepancy_reliable"]
    )


def test_each_hours_values_are_published_for_an_outside_check():
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


@pytest.mark.parametrize(
    ("fleet", "ach", "gap", "reliable"),
    [
        ([(20.0, 100.0), (80.0, 50.0), (80.0, 50.0)], 50.0, 0.0, True),
        ([(20.0, 100.0)], 50.0, 30.0, False),  # the home sensor alone
        (
            [(60.0, 100.0), (0.0, 10.0)],
            60.0 / 110.0 * 100.0,
            0.0,
            True,
        ),  # weighted by capacity
        (
            [(50.0, 100.0), (None, 60.0)],
            50.0,
            0.0,
            True,
        ),  # a participant with no sensor is dropped
    ],
    ids=["fleet_agrees", "home_only", "capacity_weighted", "no_sensor"],
)
def test_the_real_side_is_the_capacity_weighted_fleet(fleet, ach, gap, reliable):
    socs = [([] if v is None else [(H0, v)], cap) for v, cap in fleet]
    out = sw._soc_discrepancy_stats(socs, {H0.isoformat(): {"soc_pct": ach}})
    assert out["soc_discrepancy_max_pct"] == pytest.approx(gap, abs=1e-6)
    assert out["soc_discrepancy_reliable"] is reliable
    if len(fleet) == 3:
        assert out["soc_discrepancy_mean_pct"] == 0.0


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


def test_beating_the_oracle_below_its_own_floor_is_named_and_measured_in_kwh():
    s = _feasibility(DRIFTED, -0.73)
    assert (s["regret_reliable"], s["achieved_within_lp_soc_bounds"]) == (False, False)
    assert s["epr_reason"] == "oracle_beaten_achieved_outside_lp_soc_bounds"
    assert s["achieved_soc_min_pct"] == pytest.approx(0.5)
    assert s["achieved_below_floor_kwh"] == pytest.approx(1.5)
    assert s["achieved_above_ceiling_kwh"] == 0.0
    # A 0-100 % check would have passed the same day: the floor is the LP's.
    assert _feasibility(DRIFTED, -0.73, min_pct=0.0)["achieved_within_lp_soc_bounds"]


@pytest.mark.parametrize(
    ("soc", "regret", "reliable", "inside", "reason"),
    [
        ([20.0, 50.0, 80.0, 30.0], 1.2345, True, True, None),
        ([20.0, 50.0], 0.0, True, True, None),  # matching the oracle is the best day
        ([0.5, 50.0], 0.5, True, False, None),  # outside alone is not this flag's call
        ([20.0, 50.0, 80.0], -0.25, False, True, "oracle_beaten"),
    ],
    ids=["healthy", "exactly_zero", "outside_but_positive", "beaten_inside"],
)
def test_only_a_negative_regret_is_unreliable_and_it_says_which_kind(
    soc, regret, reliable, inside, reason
):
    s = _feasibility(soc, regret)
    assert (
        s["regret_reliable"],
        s["achieved_within_lp_soc_bounds"],
        s["epr_reason"],
    ) == (
        reliable,
        inside,
        reason,
    )


def test_without_a_trajectory_the_regret_verdict_still_stands():
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
