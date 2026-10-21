"""Layer 1 of the rebuilt suite (#1837): the export bonus's latest-preferred
tie-break on the secondary channel (nimbus #1964, Mark Purcell's decision:
"a secondary-objective (lexicographic) tie-break, matching #696's own
precedent").

With a flat bonus price, the capped volume may be claimed in any periods;
the tie-break prefers the latest (household, 20 Aug 2026: "our window closes
0.00"). As a 1e-7-per-rank primary nudge, it fell inside HiGHS's tolerance
whenever the cap left a partial period, and the bonus scattered ON/OFF/ON
(`[0, 0, 0, 2.5, 0, 5]`, found by #1962's property on CI). On the secondary
channel, which production solves use (`CalibratedOptions`), it orders the
claim and never meets a real price. A plain solve, with no secondary
channel, keeps the primary nudge.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
from hypothesis import given
from hypothesis import strategies as st
from solver.elements import (
    BatteryConfig,
    GridConfig,
    LoadConfig,
    PeriodGrid,
    SolarConfig,
)
from solver.lp import CalibratedOptions, LexOptions
from solver.network import build_plan
from test_plan_invariants import SETTINGS, _num


@SETTINGS
@given(
    st.integers(3, 8),
    _num(5.0, 13.0),
    _num(0.1, 0.9),
    _num(0.05, 0.5),
    st.sampled_from(["calibrated", "lex"]),
)
def test_a_flat_export_bonus_is_claimed_in_one_block_at_the_end(
    n, rate, share, premium, mode
):
    cap = round(rate * n * share, 4)
    plan = build_plan(
        periods=PeriodGrid(
            hours=np.ones(n), start=datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
        ),
        grid=GridConfig(
            import_price=np.full(n, 0.15),
            export_price=np.full(n, 0.09),
            import_limit_kw=20.0,
            export_limit_kw=20.0,
            export_bonus_price=np.full(n, premium),
            export_bonus_volume_kwh=cap,
        ),
        batteries=[
            BatteryConfig(
                name="home",
                capacity_kwh=200.0,
                initial_soc_kwh=200.0,
                min_soc_kwh=10.0,
                max_soc_kwh=200.0,
                max_charge_kw=rate,
                max_discharge_kw=rate,
                charge_efficiency=0.95,
                discharge_efficiency=0.95,
                charge_cost=0.01,
                discharge_cost=0.01,
                salvage_value=0.02,
            )
        ],
        solar=SolarConfig(forecast_kw=np.zeros(n)),
        loads=[LoadConfig(name="house", forecast_kw=np.zeros(n))],
        solve_options=CalibratedOptions() if mode == "calibrated" else LexOptions(),
    )
    assert plan.status == "optimal"
    np.testing.assert_allclose(plan.grid_export_kw, rate, atol=1e-4)
    bonus = plan.export_bonus_kw
    # The lexicographic phases hold the claim to their own tolerance (a few
    # watts); the calibrated solve production runs is exact.
    tol = 1e-4 if mode == "calibrated" else 2e-2
    assert abs(float(np.sum(bonus)) - cap) < 1e-4
    on = bonus > tol
    first = int(np.argmax(on))
    assert on[first:].all(), bonus  # one block, running to the end
    np.testing.assert_allclose(bonus[first + 1 :], rate, atol=tol)
