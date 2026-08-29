"""Real tests for forecast_regret.py's three-scenario decomposition
(nimbus issue #273, Mark Purcell's EPR four-way decomposition -- see
that module's own docstring for the full method).

These tests prove the actual claims the module makes, not just "doesn't
crash":
  1. J_star is always <= both J_forecast and J_persistence -- the oracle
     structurally cannot be beaten (same reason EPR/regret elsewhere in
     this package must satisfy regret >= 0 -- see regret.py's own
     module docstring).
  2. A forecast that matches reality exactly reduces forecast_regret to
     (near) zero -- proves the comparison is actually sensitive to
     forecast quality, not a constant/degenerate result regardless of
     input.
  3. A forecast that is WORSE than persistence (deliberately, sharply
     wrong) produces a negative nimbus_value_add_dollars -- proves the
     value-add sign genuinely reflects which forecast was better, not a
     hardcoded direction.
  4. A forecast that is BETTER than persistence produces a positive
     nimbus_value_add_dollars -- the mirror-image check to #3.
"""

import unittest
from datetime import UTC, datetime

import _solver_path  # noqa: F401
import numpy as np
from solver.elements import BatteryConfig, GridConfig
from solver.forecast_regret import compute_forecast_regret

N = 24  # one real day, hourly periods
START = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)


def _grid(import_price, export_price):
    return GridConfig(
        import_price=import_price,
        export_price=export_price,
        import_limit_kw=20.0,
        export_limit_kw=20.0,
    )


def _battery():
    return BatteryConfig(
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
    from solver.elements import PeriodGrid

    return PeriodGrid(hours=np.array([1.0] * N), start=START)


class TestComputeForecastRegret(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(42)
        # Real-shaped daily solar curve (zero overnight, a daytime hump)
        # and a load curve with real morning/evening peaks -- not flat
        # synthetic data, so a wrong forecast actually costs something.
        hour = np.arange(N)
        self.solar_real = np.clip(8.0 * np.sin((hour - 6.0) / 12.0 * np.pi), 0.0, None)
        self.load_real = (
            1.5
            + 1.0 * np.sin(hour / 24.0 * 2 * np.pi)
            + (2.0 * ((hour >= 17) & (hour <= 21)))
        )
        self.import_price = np.full(N, 0.30)
        self.export_price = np.full(N, 0.10)
        # A deliberately "yesterday, flat-shifted" persistence baseline --
        # same rough shape but genuinely different (a full-day lag), so
        # it's neither identical to nor wildly divergent from reality.
        self.solar_persistence = np.roll(self.solar_real, 3) * 0.85
        self.load_persistence = np.roll(self.load_real, 2) * 1.1
        self._noise = rng.normal(0, 0.05, N)

    def test_oracle_is_never_worse_than_forecast_or_persistence(self):
        result = compute_forecast_regret(
            periods=_periods(),
            grid=_grid(self.import_price, self.export_price),
            battery=_battery(),
            solar_real_kw=self.solar_real,
            load_real_kw=self.load_real,
            solar_forecast_kw=np.clip(self.solar_real + self._noise, 0.0, None),
            load_forecast_kw=np.clip(self.load_real + self._noise, 0.0, None),
            solar_persistence_kw=self.solar_persistence,
            load_persistence_kw=self.load_persistence,
        )
        self.assertLessEqual(result.j_star, result.j_forecast + 1e-6)
        self.assertLessEqual(result.j_star, result.j_persistence + 1e-6)
        self.assertGreaterEqual(result.forecast_regret_dollars, -1e-6)
        self.assertGreaterEqual(result.persistence_regret_dollars, -1e-6)

    def test_perfect_forecast_has_near_zero_forecast_regret(self):
        result = compute_forecast_regret(
            periods=_periods(),
            grid=_grid(self.import_price, self.export_price),
            battery=_battery(),
            solar_real_kw=self.solar_real,
            load_real_kw=self.load_real,
            solar_forecast_kw=self.solar_real.copy(),
            load_forecast_kw=self.load_real.copy(),
            solar_persistence_kw=self.solar_persistence,
            load_persistence_kw=self.load_persistence,
        )
        self.assertAlmostEqual(result.forecast_regret_dollars, 0.0, places=4)

    def test_forecast_worse_than_persistence_gives_negative_value_add(self):
        # Persistence is a genuinely decent baseline here; the "forecast"
        # is deliberately badly wrong (inverted day/night solar, flat
        # load ignoring the real evening peak) -- Nimbus's own forecast
        # should NOT look better than doing nothing smarter than
        # persistence in this scenario.
        bad_solar_forecast = np.clip(8.0 - self.solar_real, 0.0, None)
        bad_load_forecast = np.full(N, float(np.mean(self.load_real)))
        result = compute_forecast_regret(
            periods=_periods(),
            grid=_grid(self.import_price, self.export_price),
            battery=_battery(),
            solar_real_kw=self.solar_real,
            load_real_kw=self.load_real,
            solar_forecast_kw=bad_solar_forecast,
            load_forecast_kw=bad_load_forecast,
            solar_persistence_kw=self.solar_persistence,
            load_persistence_kw=self.load_persistence,
        )
        self.assertLess(result.nimbus_value_add_dollars, 0.0)

    def test_forecast_better_than_persistence_gives_positive_value_add(self):
        # Nimbus's forecast here is close to real (small noise);
        # persistence is a full-day-old, scaled, badly-misaligned guess.
        good_solar_forecast = np.clip(self.solar_real + self._noise, 0.0, None)
        good_load_forecast = np.clip(self.load_real + self._noise, 0.0, None)
        bad_persistence_solar = np.roll(self.solar_real, 12) * 0.4
        bad_persistence_load = np.full(N, 0.1)
        result = compute_forecast_regret(
            periods=_periods(),
            grid=_grid(self.import_price, self.export_price),
            battery=_battery(),
            solar_real_kw=self.solar_real,
            load_real_kw=self.load_real,
            solar_forecast_kw=good_solar_forecast,
            load_forecast_kw=good_load_forecast,
            solar_persistence_kw=bad_persistence_solar,
            load_persistence_kw=bad_persistence_load,
        )
        self.assertGreater(result.nimbus_value_add_dollars, 0.0)


if __name__ == "__main__":
    unittest.main()
