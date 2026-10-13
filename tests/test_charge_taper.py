"""The plan's charge ceiling tapers near full (household, 12 Oct 2026: "it is
way nicer to plan for it ... not hope for 40kw which wont happen").

Reference household, measured 9-11 Oct 2026 on a 40 kW battery: ~24 kW at
92-96% SoC, ~14 at 97.5%, ~10 at 98.5%, ~8 at 99%, ~6 at 99.5%. The 12 Oct
plan charged at 40 kW right up to 98.7%. The default taper (from 90%, down to
10% of max at 100%) is a straight line through those points. Real HiGHS
solves for the LP half.
"""

from __future__ import annotations

from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
import pytest
import solver_plan
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

CAP = 122.2
MIN_SOC = CAP * 0.02
MAX_SOC = CAP


def _curve(cfg=None, max_soc=MAX_SOC):
    return solver_plan.charge_taper_curve(cfg or {}, CAP, MIN_SOC, max_soc, 40.0)


def _ceiling(curve, soc_kwh):
    return float(np.interp(soc_kwh, [s for s, _ in curve], [p for _, p in curve]))


def test_default_curve_is_flat_to_90_then_falls_to_4_kw():
    curve = _curve()
    assert curve == [
        (MIN_SOC, 40.0),
        (pytest.approx(CAP * 0.9), 40.0),
        (MAX_SOC, pytest.approx(4.0)),
    ]


@pytest.mark.parametrize(
    ("soc_pct", "measured_kw"),
    [(97.5, 14.0), (98.5, 10.0), (99.0, 8.0), (99.5, 6.0)],
)
def test_default_curve_tracks_the_measured_taper(soc_pct, measured_kw):
    assert _ceiling(_curve(), CAP * soc_pct / 100) == pytest.approx(
        measured_kw, abs=2.0
    )


def test_start_100_turns_it_off():
    assert _curve({"solver_charge_taper_start_percent": 100}) is None


def test_start_at_or_above_max_soc_is_off():
    assert _curve(max_soc=CAP * 0.9) is None


def test_lower_max_soc_cuts_the_line():
    curve = _curve(max_soc=CAP * 0.95)
    assert curve[-1] == (pytest.approx(CAP * 0.95), pytest.approx(22.0))


def _plan(curve):
    n = 12
    return build_plan(
        periods=PeriodGrid(
            hours=np.full(n, 1 / 12), start=datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.full(n, 0.05),
            export_price=np.full(n, 0.0),
            import_limit_kw=42.0,
            export_limit_kw=40.0,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=CAP,
                initial_soc_kwh=CAP * 0.88,
                min_soc_kwh=MIN_SOC,
                max_soc_kwh=MAX_SOC,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=0.97,
                discharge_efficiency=0.97,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.40,
                charge_power_curve=curve,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.full(n, 1.0))],
    )


def test_the_plan_charges_below_the_taper_near_full():
    curve = _curve()
    plan = _plan(curve)
    soc_before = np.concatenate([[CAP * 0.88], plan.battery_soc_kwh[:-1]])
    for c, soc in zip(plan.battery_charge_kw, soc_before, strict=True):
        assert c <= _ceiling(curve, soc) + 1e-6
    # it still charges hard below the taper, and slower than 40 kW near the top
    assert plan.battery_charge_kw[0] == pytest.approx(40.0, abs=1e-6)
    assert max(plan.battery_charge_kw[soc_before > CAP * 0.95]) < 25.0


def test_without_the_taper_it_plans_40_kw_to_the_top():
    plan = _plan(None)
    assert np.allclose(plan.battery_charge_kw[:4], 40.0, atol=1e-6)
    assert plan.battery_soc_kwh[3] > CAP * 0.98
