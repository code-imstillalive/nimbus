"""nimbus issue #937 failure mode 2 (Mark Purcell asked for this
directly): separate the load forecast's LEVEL error from its SHAPE error.

#937 measured that naive persistence beat the ML load forecaster on 11 of
14 scored days (mean -$0.71/day) on the reference household's own data,
and its own "what would move this forward" list asked:

    "Is the forecast worse in *level* (biased) or in *shape* (right
     total, wrong timing)? Shape errors are what cost money here, since
     they move energy to the wrong price."

Mark's follow-up sharpened why this matters: the circuit-level
horizon-flip finding is a plausible *mechanism* for a SHAPE error (wrong
timing at the tail of a 48 h horizon validated at 4 h), but nothing had
actually separated bias from timing on the whole-house forecast the
dollar figure is scored against -- so nothing connected the two.

The split re-solves the same LP with one thing corrected at a time:

    J_forecast              load forecast as-is,  solar forecast
    J_load_level_corrected  load rescaled to the real daily total
                            (its own SHAPE, the right LEVEL)
    J_load_perfect          load = real (right level AND shape)
    J_star                  both perfect

which makes the three differences an EXACT additive breakdown of
forecast_regret_dollars. That exactness is the property most worth
pinning: a decomposition whose parts do not sum to the whole is not a
decomposition, and this is the kind of arithmetic that silently stops
holding when someone adds a fourth scenario later.

The decomposition is deliberately in the metric's own currency -- dollars
through the LP against realised prices -- not an MAE-style proxy,
because #937's whole point is that those are different questions: an
error at an expensive hour costs real money and the identical error at a
cheap hour costs nothing.
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

# A real-shaped day: overnight-zero solar with a daytime hump, and a load
# curve with a genuine evening peak sitting inside the expensive window.
# Flat data would make every scenario cost the same and the test vacuous.
SOLAR_REAL = np.clip(8.0 * np.sin((HOUR - 6.0) / 12.0 * np.pi), 0.0, None)
LOAD_REAL = (
    1.5 + 1.0 * np.sin(HOUR / 24.0 * 2 * np.pi) + (2.0 * ((HOUR >= 17) & (HOUR <= 21)))
)
# Prices that genuinely reward getting the TIMING right -- a flat price
# curve cannot distinguish a shape error from no error at all, which
# would make the whole split untestable.
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


def _run(load_forecast_kw, solar_forecast_kw=None):
    return compute_forecast_regret(
        periods=_periods(),
        grid=_grid(),
        battery=_battery(),
        solar_real_kw=SOLAR_REAL,
        load_real_kw=LOAD_REAL,
        solar_forecast_kw=SOLAR_REAL.copy()
        if solar_forecast_kw is None
        else solar_forecast_kw,
        load_forecast_kw=load_forecast_kw,
        # Persistence is irrelevant to this split; hold it at something
        # real so the scenario stays solvable.
        solar_persistence_kw=np.roll(SOLAR_REAL, 3) * 0.85,
        load_persistence_kw=np.roll(LOAD_REAL, 2) * 1.1,
    )


class TestTheSplitIsExactlyAdditive(unittest.TestCase):
    """The property that makes it a decomposition rather than three
    loosely-related numbers."""

    def test_the_three_parts_sum_to_forecast_regret(self):
        # A forecast wrong in BOTH ways at once -- 15% high on the total
        # AND shifted two hours late -- so every term is non-trivial and
        # the sum is a real test rather than 0 == 0.
        r = _run(np.roll(LOAD_REAL, 2) * 1.15)
        self.assertIsNotNone(r.load_level_error_dollars)
        total = (
            r.load_level_error_dollars
            + r.load_shape_error_dollars
            + r.solar_error_dollars
        )
        self.assertAlmostEqual(total, r.forecast_regret_dollars, places=6)

    def test_the_parts_are_not_all_zero_in_that_case(self):
        """Guards the test above from passing vacuously: 0+0+0 == 0
        would satisfy additivity while measuring nothing."""
        r = _run(np.roll(LOAD_REAL, 2) * 1.15)
        self.assertGreater(abs(r.load_level_error_dollars), 1e-6)
        self.assertGreater(abs(r.load_shape_error_dollars), 1e-6)


class TestItSeparatesTheTwoFailureModes(unittest.TestCase):
    """The actual question #937 asks. Each test constructs a forecast
    wrong in exactly ONE way and checks the split attributes it to the
    right term -- which is what makes the metric diagnostic rather than
    merely arithmetically consistent."""

    def test_a_purely_biased_forecast_is_charged_to_level_not_shape(self):
        """Right shape, 30% high on the total. A wider validation
        horizon cannot fix this -- a biased forecaster is biased at
        every horizon -- so it has to land in the LEVEL term."""
        r = _run(LOAD_REAL * 1.30)
        self.assertAlmostEqual(r.load_shape_error_dollars, 0.0, places=6)
        self.assertAlmostEqual(
            r.load_level_error_dollars, r.forecast_regret_dollars, places=6
        )

    def test_a_purely_mistimed_forecast_is_charged_to_shape_not_level(self):
        """Right daily total, wrong hours -- the shape the horizon-flip
        mechanism would produce. Rolling the curve preserves the sum
        exactly, so the level term must be ~0."""
        r = _run(np.roll(LOAD_REAL, 3))
        self.assertAlmostEqual(r.load_level_error_dollars, 0.0, places=6)
        self.assertAlmostEqual(
            r.load_shape_error_dollars, r.forecast_regret_dollars, places=6
        )

    def test_a_perfect_load_forecast_charges_nothing_to_either(self):
        r = _run(LOAD_REAL.copy())
        self.assertAlmostEqual(r.load_level_error_dollars, 0.0, places=6)
        self.assertAlmostEqual(r.load_shape_error_dollars, 0.0, places=6)


class TestSolarIsReportedSeparately(unittest.TestCase):
    """#937 assumes the load forecaster is the dominant term and nobody
    had checked. Publishing solar's own contribution is what makes that
    checkable from the data instead of arguable."""

    def test_solar_error_is_zero_when_solar_is_held_at_truth(self):
        """The shape nowcast_skill.py uses -- it passes the real solar
        as the solar 'forecast' in every scenario, so there is genuinely
        no solar error to attribute and the load split is the whole of
        forecast_regret_dollars."""
        r = _run(np.roll(LOAD_REAL, 2) * 1.15)
        self.assertAlmostEqual(r.solar_error_dollars, 0.0, places=6)

    def test_a_wrong_solar_forecast_lands_in_the_solar_term(self):
        """With the LOAD forecast perfect, everything left is solar's,
        and it must be strictly positive -- the oracle cannot be beaten.
        """
        r = _run(LOAD_REAL.copy(), solar_forecast_kw=SOLAR_REAL * 0.55)
        self.assertGreater(r.solar_error_dollars, 1e-6)
        self.assertAlmostEqual(r.load_level_error_dollars, 0.0, places=6)
        self.assertAlmostEqual(r.load_shape_error_dollars, 0.0, places=6)


class TestUndefinedRatherThanFabricated(unittest.TestCase):
    def test_a_zero_forecast_reports_absence_not_a_confident_zero(self):
        """A forecast summing to ~0 has no meaningful scale factor. The
        split is undefined, and None says so -- a 0.0 would read as
        'measured, and there is no level error', which is a different
        and false claim. Same convention as soc_discrepancy's own.
        """
        r = _run(np.zeros(N))
        self.assertIsNone(r.load_level_error_dollars)
        self.assertIsNone(r.load_shape_error_dollars)
        self.assertIsNone(r.solar_error_dollars)

    def test_the_headline_figures_still_compute_when_the_split_cannot(self):
        """The split is a secondary breakdown. It must never take down
        the number #937 is actually about -- the same 'degrade, never
        wedge' discipline as #366/#373.
        """
        r = _run(np.zeros(N))
        self.assertIsInstance(r.nimbus_value_add_dollars, float)
        self.assertGreaterEqual(r.forecast_regret_dollars, -1e-6)


if __name__ == "__main__":
    unittest.main()
