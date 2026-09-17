"""IV&V finding (since-#1058 pass, 2026-09-17): #937's level/shape split
(v0.94.367, `compute_forecast_regret()`) guards against a forecast that
sums to EXACTLY zero, but not one that is merely close to zero relative
to a real day's total -- which is the realistic shape of the failure it
means to guard against.

`forecast_regret.py`'s own comment says the intent plainly: "A forecast
summing to ~0 has no meaningful scale factor... report absence, not a
fabricated 0.0." The guard that implements this is:

    if abs(forecast_load_kwh) > 1e-6 and abs(real_load_kwh) > 1e-6:
        scale = real_load_kwh / forecast_load_kwh
        ...

1e-6 kWh is a reasonable floor for "did this literally read as all
zeros", but it is roughly seven orders of magnitude smaller than a real
household's daily load total (tens of kWh). A forecast that is
near-all-zero but not EXACTLY zero -- e.g. one non-zero period out of
96 surviving a partial/glitchy read, the same general shape this
project's own #370/#374 startup-race history documents for the load
forecaster -- clears 1e-6 easily and produces a `scale` factor in the
millions. The resulting `load_level_error_dollars` is not a fabricated
0.0 (the guard's stated concern) but it IS a number the guard's own
comment says should not exist: an "undefined" split masquerading as a
computed one, capable of coming out with an incorrect sign (the LP
absorbs the resulting physically-impossible spike via the existing
grid_import_excess slack from #390 rather than going infeasible, but
the dollar attribution itself is meaningless).

This test constructs a forecast summing to a few millionths of a kWh
against the fixture's real ~40 kWh/day load, and asserts the split
degrades to None (the documented "no meaningful scale factor" case)
rather than returning a number. It fails against current code because
the absolute epsilon lets a "near enough to zero to be meaningless"
forecast through as if it were a genuine, if small, real one.

Fix shape suggested (not prescribed): make the guard relative to a
plausible daily total instead of an absolute kWh floor -- e.g. require
`forecast_load_kwh` to be at least some small fraction (a percent or
so) of `real_load_kwh` -- or bound the accepted `scale` factor itself
(e.g. treat anything outside something like [0.1, 10] as undefined
rather than a real correction).
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig, PeriodGrid
from solver.forecast_regret import compute_forecast_regret

N = 24
START = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
HOUR = np.arange(N)

SOLAR_REAL = np.clip(8.0 * np.sin((HOUR - 6.0) / 12.0 * np.pi), 0.0, None)
LOAD_REAL = (
    1.5 + 1.0 * np.sin(HOUR / 24.0 * 2 * np.pi) + (2.0 * ((HOUR >= 17) & (HOUR <= 21)))
)
IMPORT_PRICE = np.where((HOUR >= 17) & (HOUR <= 21), 0.90, 0.20)
EXPORT_PRICE = np.where((HOUR >= 17) & (HOUR <= 21), 0.40, 0.05)


def _grid():
    return GridConfig(
        import_price=IMPORT_PRICE,
        export_price=EXPORT_PRICE,
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )


def _battery():
    return BatteryConfig(
        name="battery",
        capacity_kwh=20.0,
        initial_soc_kwh=10.0,
        min_soc_kwh=1.0,
        max_soc_kwh=20.0,
        max_charge_kw=10.0,
        max_discharge_kw=10.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        charge_cost=0.005,
        discharge_cost=0.01,
        salvage_value=0.15,
    )


def _periods():
    return PeriodGrid(hours=np.array([1.0] * N), start=START)


class TestNearZeroButNotExactlyZeroForecastDegradesToNone(unittest.TestCase):
    def test_a_near_all_zero_forecast_reports_absence_not_a_wild_number(self):
        # A realistic near-all-zero forecast: one lone period carries a
        # tiny non-zero reading (a glitchy/partial read), the other 23
        # are exactly 0.0 -- the same general shape this repo's own
        # #370/#374 startup-race history documents, not a hand-picked
        # edge case. Sums to ~2 microwatt-hours against a real ~40 kWh
        # day.
        near_zero_forecast = np.zeros(N)
        near_zero_forecast[3] = 2e-6

        r = compute_forecast_regret(
            periods=_periods(),
            grid=_grid(),
            battery=_battery(),
            solar_real_kw=SOLAR_REAL,
            load_real_kw=LOAD_REAL,
            solar_forecast_kw=SOLAR_REAL.copy(),
            load_forecast_kw=near_zero_forecast,
            solar_persistence_kw=np.roll(SOLAR_REAL, 3) * 0.85,
            load_persistence_kw=np.roll(LOAD_REAL, 2) * 1.1,
        )

        self.assertIsNone(
            r.load_level_error_dollars,
            "a forecast summing to a few millionths of a kWh against a "
            "real ~40 kWh day is functionally 'no meaningful scale "
            "factor', exactly what the guard's own comment says should "
            "degrade to None -- instead it computed a real (and, as "
            "reproduced during this IV&V pass, sometimes NEGATIVE) "
            f"number: {r.load_level_error_dollars!r}.",
        )


if __name__ == "__main__":
    unittest.main()
