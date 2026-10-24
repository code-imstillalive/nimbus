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

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.p2p_export import grid_export_bounds
from solver.quality_report import (
    _MIN_POSITIVE_SOC_KWH,
    _soc_envelope_containing_achieved,
    _widen_export_pin_to_achieved,
    compute_quality_report,
)
from solver.regret import evaluate_realized_cost_multi

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
