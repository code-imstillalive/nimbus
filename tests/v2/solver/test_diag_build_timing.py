"""TEMPORARY diag: profile a production-sized joint stochastic solve. Not for merge."""

import cProfile
import io
import pstats
import time
from datetime import UTC, datetime

import numpy as np
from solver import stochastic_dispatch as sd
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)

N = 200
START = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)


def _inputs():
    t = np.arange(N)
    sun = np.clip(5 * np.sin((t % 48 - 12) / 24 * np.pi), 0, None)
    imp = 0.25 + 0.2 * ((t % 48) > 34)
    return {
        "periods": PeriodGrid(np.full(N, 0.5), start=START),
        "grid": GridConfig(
            import_price=imp,
            export_price=imp - 0.15,
            import_limit_kw=20.0,
            export_limit_kw=10.0,
        ),
        "batteries": [
            BatteryConfig(
                name="home",
                capacity_kwh=10.0,
                initial_soc_kwh=4.0,
                min_soc_kwh=1.0,
                max_soc_kwh=9.5,
                max_charge_kw=5.0,
                max_discharge_kw=5.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.006,
                discharge_cost=0.006,
                salvage_value=0.2,
            )
        ],
        "solar": SolarConfig(forecast_kw=sun, lower_kw=sun * 0.3, upper_kw=sun * 1.5),
        "loads": [LoadConfig(name="house", forecast_kw=np.full(N, 1.5))],
    }


def test_diag_profile_joint():
    pr = cProfile.Profile()
    t0 = time.monotonic()
    pr.enable()
    result = sd.build_stochastic_plan(_inputs())
    pr.disable()
    total = time.monotonic() - t0
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(35)
    raise AssertionError(
        f"total {total:.2f}s status {getattr(result, 'status', None)} "
        f"calls {getattr(result, 'joint_calls', None)}\n{s.getvalue()}"
    )
