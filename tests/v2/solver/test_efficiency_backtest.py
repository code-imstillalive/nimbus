"""The round-trip efficiency backtest (#1232) (#1837).

Each candidate efficiency plans a real day, and every plan is scored under
the battery as configured: assuming more efficiency than the battery has
then costs real money, so the best candidate is the battery's own, not
simply the largest.
"""

from __future__ import annotations

import numpy as np
from solver.backtest import (
    run_efficiency_sensitivity_sweep,
    score_candidate_day,
)
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)

N = 24


def _battery(**overrides):
    return BatteryConfig(
        **{
            "name": "battery",
            "capacity_kwh": 30.0,
            "initial_soc_kwh": 25.0,
            "min_soc_kwh": 2.0,
            "max_soc_kwh": 30.0,
            "max_charge_kw": 15.0,
            "max_discharge_kw": 15.0,
            "charge_efficiency": 0.95,
            "discharge_efficiency": 0.95,
            "charge_cost": 0.01,
            "discharge_cost": 0.01,
            "salvage_value": 0.10,
            **overrides,
        }
    )


def _day(import_limit=20.0):
    """Cheap overnight, a dear evening, midday sun: a day on which
    efficiency changes what the battery should do."""
    hour = np.arange(N)
    return {
        "periods": PeriodGrid(np.ones(N)),
        "grid": GridConfig(
            import_price=np.where((hour >= 17) & (hour < 21), 0.45, 0.20),
            export_price=np.full(N, 0.05),
            import_limit_kw=import_limit,
            export_limit_kw=import_limit,
        ),
        "solar": SolarConfig(forecast_kw=np.where((hour >= 9) & (hour < 15), 6.0, 0.0)),
        "load": LoadConfig(name="load", forecast_kw=np.ones(N)),
    }


def _sweep(battery=None, **kw):
    return run_efficiency_sensitivity_sweep(
        base_battery=battery or _battery(), **_day(), **kw
    )


def test_judged_by_its_own_assumption_a_lossier_battery_never_scores_better():
    perfect = score_candidate_day(
        **_day(), battery=_battery(charge_efficiency=0.99, discharge_efficiency=0.99)
    )
    lossy = score_candidate_day(
        **_day(), battery=_battery(charge_efficiency=0.70, discharge_efficiency=0.70)
    )
    assert np.isfinite(perfect) and perfect <= lossy + 1e-6
