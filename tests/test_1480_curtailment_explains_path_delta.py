"""#1480 follow-up: solar CURTAILMENT is a known LP-vs-evaluator difference,
and `j_star_path_delta_explained` must count it.

The LP may curtail solar; the evaluator re-derives grid flow from the real
balance and never curtails. Measured on the reference household, 30 Sep 2026:
one negative-price half-hour (import -15.7 c, export -16.0 c) where the oracle
imported at its limit AND curtailed 4.83 kW, left -0.3804 "unexplained" -- the
curtailed 2.417 kWh priced at -15.7 c. 28 and 29 Sep, with no curtailment,
explained to -0.0004 / -0.0003.

Pinned on a synthetic day built to force the same shape: a midday hour with
strongly negative prices and more solar than the house and battery can absorb.
"""

import sys
import unittest
from pathlib import Path

import _solver_path  # noqa: F401
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import test_jstar_path_delta as jt
from solver.elements import (
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.quality_report import (
    _curtailment_repricing,
    compute_quality_report,
)

N = jt.N


def _negative_price_report():
    imp = np.full(N, 0.30)
    exp_ = np.full(N, 0.05)
    imp[12], exp_[12] = -0.157, -0.160
    grid = GridConfig(
        import_price=imp, export_price=exp_, import_limit_kw=42.0, export_limit_kw=42.0
    )
    solar = np.zeros(N)
    solar[9:16] = 30.0
    battery = jt._battery()
    zero = np.zeros(N)
    return compute_quality_report(
        periods=PeriodGrid(hours=jt.HOURS, start=jt.START),
        grid_residual=grid,
        grid_oracle=grid,
        batteries=[battery],
        solar=SolarConfig(forecast_kw=solar),
        load=LoadConfig(name="house", forecast_kw=np.full(N, 1.0)),
        timestamps=jt.STARTS,
        real_p2p_dollars_earned=0.0,
        commanded_charge_kw=[zero],
        commanded_discharge_kw=[zero],
        actual_charge_kw=[zero],
        actual_discharge_kw=[zero],
        final_soc_kwh_actual=[battery.initial_soc_kwh],
    )


class TestCurtailmentIsExplained(unittest.TestCase):
    def setUp(self):
        self.report = _negative_price_report()

    def test_the_fixture_really_curtails(self):
        """Non-vacuity: the repricing term must be material on this day."""
        captured = {}
        qr = sys.modules[compute_quality_report.__module__]
        real = qr.build_plan

        def bp(*a, **k):
            captured["plan"] = real(*a, **k)
            captured["grid"] = k["grid"]
            return captured["plan"]

        qr.build_plan = bp
        try:
            _negative_price_report()
        finally:
            qr.build_plan = real
        term = _curtailment_repricing(
            captured["plan"],
            hours=jt.HOURS,
            import_price=captured["grid"].import_price,
            export_price=captured["grid"].export_price,
        )
        self.assertGreater(float(np.sum(captured["plan"].solar_curtailed_kw)), 1.0)
        self.assertGreater(abs(term), 0.05, f"curtailment term only {term}")

    def test_the_unexplained_residual_is_near_zero(self):
        r = self.report
        self.assertAlmostEqual(
            r.j_star_path_delta_explained + r.j_star_path_delta_unexplained,
            r.j_star_path_delta,
            places=3,
        )
        self.assertLess(abs(r.j_star_path_delta_unexplained), 0.01)


class TestTheTermIsExact(unittest.TestCase):
    def _plan(self, gi, ge, curt):
        return type(
            "P",
            (),
            {
                "grid_import_kw": np.array(gi, float),
                "grid_export_kw": np.array(ge, float),
                "solar_curtailed_kw": np.array(curt, float),
            },
        )()

    def test_importing_period_is_priced_at_import(self):
        p = self._plan([42.0], [0.0], [4.83])
        term = _curtailment_repricing(
            p, hours=[0.5], import_price=[-0.1572], export_price=[-0.16]
        )
        self.assertAlmostEqual(term, 0.5 * 4.83 * -0.1572, places=6)

    def test_a_curtailment_that_crosses_from_import_to_export(self):
        """LP imports 1 kW while curtailing 3: the evaluator exports 2."""
        p = self._plan([1.0], [0.0], [3.0])
        term = _curtailment_repricing(
            p, hours=[1.0], import_price=[0.30], export_price=[0.05]
        )
        self.assertAlmostEqual(term, 0.30 * 1.0 - (-0.05 * 2.0), places=6)

    def test_no_curtailment_is_exactly_zero(self):
        p = self._plan([5.0, 0.0], [0.0, 3.0], [0.0, 0.0])
        self.assertEqual(
            _curtailment_repricing(
                p, hours=[1, 1], import_price=0.3, export_price=0.05
            ),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
