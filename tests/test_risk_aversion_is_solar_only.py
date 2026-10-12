"""The "Solar forecast risk" slider (`risk_aversion`) leans solar toward its
lower band and leaves house load on its point forecast (household, 12 Oct
2026). It used to lean load toward its upper band too: a house-load band of
1.1 -> 10.3 kW at 0.25 had the reference household's plan budget ~2 kW of
house load all evening that never came (battery planned to 7% at midnight
against a measured 15-21% on every night since 5 Oct), and push midday
charging past the 42 kW import limit, where the LP's grid_import_excess
penalty ($135 that morning) priced the breach. Real HiGHS solves.
"""

from __future__ import annotations

from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.network import build_plan

N = 8
HOURS = np.full(N, 0.5)


def _plan(risk: float, *, solar_kw=0.0, solar_lower=None):
    load = np.full(N, 1.2)
    return build_plan(
        periods=PeriodGrid(hours=HOURS, start=datetime(2026, 10, 12, 7, 0, tzinfo=UTC)),
        grid=GridConfig(
            import_price=np.full(N, 0.30),
            export_price=np.full(N, 0.20),
            import_limit_kw=42.0,
            export_limit_kw=40.0,
            fixed_export_kw=np.full(N, 11.5),
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=120.0,
                initial_soc_kwh=110.0,
                min_soc_kwh=2.4,
                max_soc_kwh=120.0,
                max_charge_kw=40.0,
                max_discharge_kw=40.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.01,
                salvage_value=0.1,
            )
        ],
        solar=SolarConfig(
            forecast_kw=np.full(N, solar_kw),
            lower_kw=None if solar_lower is None else np.full(N, solar_lower),
            upper_kw=None if solar_lower is None else np.full(N, solar_kw + 1.0),
        ),
        loads=[
            LoadConfig(
                name="house",
                forecast_kw=load,
                lower_kw=np.full(N, 0.0),
                upper_kw=np.full(N, 10.3),  # the 12 Oct evening band
            )
        ],
        risk_aversion=risk,
    )


def _drawn(plan):
    return plan.battery_discharge_kw - plan.battery_charge_kw


def test_a_wide_house_load_band_does_not_inflate_the_plan():
    hedged, plain = _plan(0.25), _plan(0.0)
    assert np.allclose(_drawn(hedged), _drawn(plain), atol=1e-6)
    assert np.allclose(hedged.battery_soc_kwh, plain.battery_soc_kwh, atol=1e-6)


def test_the_battery_supplies_the_block_and_the_house_only():
    plan = _plan(0.25)
    # discharge at the AC side = 11.5 kW export + 1.2 kW house, no phantom draw
    assert np.allclose(_drawn(plan), 11.5 + 1.2, atol=1e-6)
    assert float(np.sum(plan.grid_import_excess_kw)) == 0.0


def test_solar_still_leans_toward_its_lower_band():
    """Daytime: with solar forecast 6 kW and a 2 kW lower band, 0.25 plans on
    5 kW of solar, so the battery covers 1 kW more than at 0."""
    hedged = _plan(0.25, solar_kw=6.0, solar_lower=2.0)
    plain = _plan(0.0, solar_kw=6.0, solar_lower=2.0)
    assert np.allclose(_drawn(hedged) - _drawn(plain), 1.0, atol=1e-6)
