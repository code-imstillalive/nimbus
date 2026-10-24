"""The calibration weight used is carried from the LP result onto the plan
(#1291) (#1837).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _ha_stubs import install_ha_stubs

install_ha_stubs()

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from custom_components.nimbus_load.solver import network as network_mod


class TestPlanCarriesTheField(unittest.TestCase):
    def test_plan_dataclass_has_the_field(self):
        self.assertIn(
            "calibration_weight_used",
            network_mod.Plan.__dataclass_fields__,
            "Plan must carry calibration_weight_used, the same as its sibling "
            "calibration_fallback_reason, so a failed solve's blend weight "
            "actually reaches something a household or log line can read",
        )


if __name__ == "__main__":
    unittest.main()


def _plan_with_result(**overrides):
    import dataclasses
    from datetime import UTC, datetime

    import numpy as np

    from custom_components.nimbus_load.solver import elements as el

    def hook(p, solve_kwargs, **_kw):
        return dataclasses.replace(p.solve(**solve_kwargs), **overrides)

    n = 4
    return network_mod.build_plan(
        periods=el.PeriodGrid(
            hours=np.ones(n), start=datetime(2026, 9, 26, tzinfo=UTC)
        ),
        grid=el.GridConfig(
            import_price=np.full(n, 0.30),
            export_price=np.full(n, 0.05),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        ),
        batteries=[
            el.BatteryConfig(
                name="battery",
                capacity_kwh=10.0,
                initial_soc_kwh=5.0,
                min_soc_kwh=1.0,
                max_soc_kwh=10.0,
                max_charge_kw=5.0,
                max_discharge_kw=5.0,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.005,
                discharge_cost=0.005,
                salvage_value=0.1,
            )
        ],
        solar=el.SolarConfig(forecast_kw=np.zeros(n)),
        loads=[el.LoadConfig(name="house", forecast_kw=np.full(n, 1.0))],
        lp_solve_hook=hook,
    )


def test_every_plan_carries_the_weight_beside_the_fallback_reason():
    """#1291, #1179: an optimal plan and a failed one both carry the blend
    weight the solve used, whether it fell back, and why: the base rate among
    healthy cycles is what makes the failures' rate mean anything. Only a
    failed plan carries HiGHS's own numbers and the dumped model."""
    fallback = {
        "calibration_min_weight_fallback": True,
        "calibration_fallback_reason": "probe_not_optimal",
        "calibration_weight_used": 0.123,
        "highs_info": {"simplex_iteration_count": 7},
        "failing_model_path": "/tmp/model.mps",
    }
    for status in ("optimal", "error"):
        plan = _plan_with_result(
            **fallback, **({} if status == "optimal" else {"status": "error"})
        )
        assert plan.status == status
        assert (
            plan.calibration_min_weight_fallback,
            plan.calibration_fallback_reason,
            plan.calibration_weight_used,
        ) == (True, "probe_not_optimal", 0.123)
        if status == "optimal":
            assert (plan.highs_info, plan.failing_model_path) == ({}, None)
        else:
            assert plan.highs_info == {"simplex_iteration_count": 7}
            assert plan.failing_model_path == "/tmp/model.mps"
