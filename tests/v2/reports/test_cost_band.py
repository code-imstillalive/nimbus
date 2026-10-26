"""The committed plan re-costed against the load forecast's lower and upper
bounds, the battery schedule held (#147) (#1837).
"""

from __future__ import annotations

import unittest

import numpy as np
import solver_publish


def _band(load_lower, load_upper, **overrides):
    n = len(load_lower)
    defaults = {
        "period_hours": np.full(n, 1.0),
        "load_lower_kw": np.array(load_lower),
        "load_upper_kw": np.array(load_upper),
        "solar_kw": np.zeros(n),
        "import_price": np.full(n, 0.20),
        "export_price": np.full(n, 0.05),
        "charge_committed_kw": np.zeros(n),
        "discharge_committed_kw": np.zeros(n),
        "charge_cost": 0.01,
        "discharge_cost_arr": np.full(n, 0.01),
        "final_soc_kwh": 10.0,
        "salvage_value": 0.0,
        "import_limit_kw": 20.0,
        "export_limit_kw": 20.0,
    }
    defaults.update(overrides)
    return solver_publish.compute_cost_band(**defaults)


class TestPeriodsWithinHours(unittest.TestCase):
    """nimbus issue #630: periods_within_hours() slices
    compute_cost_band()'s own inputs down to "the next 24 real hours"
    for a household-scaled band, distinct from the full 96h one."""

    def test_returns_none_on_internal_failure_not_a_crash(self):
        # Mismatched array lengths -- a real internal failure mode this
        # function must degrade gracefully from, since it's a read-only
        # diagnostic riding alongside a real solve that must not break.
        result = solver_publish.compute_cost_band(
            period_hours=np.full(2, 1.0),
            load_lower_kw=np.array([1.0, 1.0, 1.0]),
            load_upper_kw=np.array([2.0, 2.0]),
            solar_kw=np.zeros(2),
            import_price=np.full(2, 0.20),
            export_price=np.full(2, 0.05),
            charge_committed_kw=np.zeros(2),
            discharge_committed_kw=np.zeros(2),
            charge_cost=0.01,
            discharge_cost_arr=np.full(2, 0.01),
            final_soc_kwh=10.0,
            salvage_value=0.0,
            import_limit_kw=20.0,
            export_limit_kw=20.0,
        )
        self.assertIsNone(result)


class TestTheBandIsOnThePlanCostsBasis(unittest.TestCase):
    """nimbus #1704: the band left out the plan's own P2P bonus, so on a P2P
    plan it sat above the plan cost and the chart drew the plan line below
    its own best case."""

    def _p2p(self, bonus_credit):
        # 2 h exporting 10 kW at 5c plus a 50c P2P bonus on that export.
        n = 2
        common = {
            "solar_kw": np.zeros(n),
            "discharge_committed_kw": np.full(n, 12.0),
            "final_soc_kwh": 10.0,
        }
        point = _band([2.0, 2.0], [2.0, 2.0], **common)
        band = _band([1.0, 1.0], [3.0, 3.0], bonus_credit=bonus_credit, **common)
        return point, band
