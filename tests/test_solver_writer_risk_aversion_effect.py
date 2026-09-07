"""Real regression test for the 2026-09-07 household finding: "moved
[risk aversion] slider, nothing happened" -- with no way to tell whether
the slider/write path was broken or the mechanism was a correct no-op
(zero-width forecast band at that moment), the household had no way to
confirm the three risk-aversion sliders (Load/Solar, Import Price,
Export Price -- number.py's own _DESCRIPTIONS) were actually reaching
the LP at all.

_risk_aversion_effect_now() answers this directly: raw forecast/price
this solve was fed vs. plan.effective_solar_kw/effective_import_price/
effective_export_price (network.py's own _risk_adjusted()/
_risk_adjusted_one_sided() output, now exposed on Plan specifically for
this). A nonzero gap is direct proof the slider had an effect this
cycle; a zero gap at a nonzero slider setting is equally real proof of
a zero-width band, not a broken control -- the two must stay
distinguishable, which is why every field is None (not 0.0) when there's
no optimal plan to compare against.
"""

import unittest
from types import SimpleNamespace

import _solver_path  # noqa: F401
import numpy as np
import solver_writer


def _fake_plan(
    is_optimal=True,
    effective_solar_kw=(6.5,),
    effective_import_price=(0.02,),
    effective_export_price=(0.10,),
):
    return SimpleNamespace(
        is_optimal=is_optimal,
        effective_solar_kw=np.array(effective_solar_kw),
        effective_import_price=np.array(effective_import_price),
        effective_export_price=np.array(effective_export_price),
    )


class TestRiskAversionEffectNow(unittest.TestCase):
    def test_nonzero_gap_proves_the_slider_had_a_real_effect(self):
        plan = _fake_plan(
            effective_solar_kw=(6.5,),
            effective_import_price=(0.025,),
            effective_export_price=(0.095,),
        )
        result = solver_writer._risk_aversion_effect_now(
            plan, solar_kw=[8.1], import_price=[0.02], export_price=[0.10]
        )
        self.assertAlmostEqual(result["solar_risk_effect_now_kw"], 1.6)
        self.assertAlmostEqual(result["import_price_risk_effect_now"], 0.005)
        self.assertAlmostEqual(result["export_price_risk_effect_now"], 0.005)

    def test_zero_gap_at_a_real_slider_value_is_a_genuine_zero_not_none(self):
        """A zero-width forecast band means risk_aversion has nothing to
        hedge against -- the correct, honest result is 0.0 (proof the
        mechanism ran and found no band to act on), never None (which
        would wrongly suggest no data was available to compare at all).
        """
        plan = _fake_plan(
            effective_solar_kw=(8.1,),
            effective_import_price=(0.02,),
            effective_export_price=(0.10,),
        )
        result = solver_writer._risk_aversion_effect_now(
            plan, solar_kw=[8.1], import_price=[0.02], export_price=[0.10]
        )
        self.assertEqual(result["solar_risk_effect_now_kw"], 0.0)
        self.assertEqual(result["import_price_risk_effect_now"], 0.0)
        self.assertEqual(result["export_price_risk_effect_now"], 0.0)

    def test_non_optimal_plan_reports_none_not_zero(self):
        plan = _fake_plan(is_optimal=False)
        result = solver_writer._risk_aversion_effect_now(
            plan, solar_kw=[8.1], import_price=[0.02], export_price=[0.10]
        )
        self.assertIsNone(result["solar_risk_effect_now_kw"])
        self.assertIsNone(result["import_price_risk_effect_now"])
        self.assertIsNone(result["export_price_risk_effect_now"])

    def test_bare_plan_with_empty_effective_arrays_reports_none(self):
        """Every existing Plan built before this field existed (or any
        test constructing Plan directly) defaults effective_solar_kw to
        an empty array -- must degrade to None, not raise an
        IndexError."""
        plan = _fake_plan(effective_solar_kw=())
        result = solver_writer._risk_aversion_effect_now(
            plan, solar_kw=[8.1], import_price=[0.02], export_price=[0.10]
        )
        self.assertIsNone(result["solar_risk_effect_now_kw"])


if __name__ == "__main__":
    unittest.main()
