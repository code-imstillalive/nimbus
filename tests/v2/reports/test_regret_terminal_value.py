"""Scoring a realised day: the end-of-day battery credit on the concave curve, and the hourly
breakdown's guard (#2393: closing solver/regret.py's coverage gap, one behaviour per test).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from solver.regret import (
    _terminal_value_credit,
    evaluate_realized_cost,
    hourly_regret_breakdown,
)

CURVE = [
    (5.0, 0.2),
    (5.0, 0.1),
]  # first 5 kWh above the floor at 0.2 $/kWh, the next 5 at 0.1


@pytest.mark.parametrize(
    ("final_soc", "credit"),
    [
        (0.5, 0.0),  # below the floor: nothing
        (1.0, 0.0),  # at the floor
        (4.0, 0.6),  # inside the first segment
        (8.0, 1.2),  # into the second
        (20.0, 1.5),  # past the curve: capped at its full width
    ],
)
def test_the_end_of_day_credit_fills_the_curve_from_its_highest_rate(final_soc, credit):
    # test_the_end_of_day_credit_fills_the_curve_from_its_highest_rate
    assert _terminal_value_credit(final_soc, 1.0, CURVE) == pytest.approx(credit)


def _one_hour(**kw):
    one = np.ones(1)
    return evaluate_realized_cost(
        hours=one,
        load_real_kw=one,
        solar_real_kw=np.zeros(1),
        import_price_real=one * 0.3,
        export_price_real=one * 0.1,
        charge_committed_kw=np.zeros(1),
        discharge_committed_kw=np.zeros(1),
        charge_cost=0.0,
        discharge_cost=0.0,
        final_soc_kwh=8.0,
        salvage_value=0.5,
        grid_import_limit_kw=100.0,
        grid_export_limit_kw=100.0,
        **kw,
    )


def test_a_realised_day_is_credited_on_the_curve_not_the_flat_salvage_value():
    # test_a_realised_day_is_credited_on_the_curve_not_the_flat_salvage_value
    # 1 kWh imported at 0.3; 8 kWh left: 1.2 on the curve (floor 1 kWh) against 4.0 flat
    assert _one_hour().total_cost == pytest.approx(0.3 - 4.0)
    assert _one_hour(
        terminal_value_breakpoints=CURVE, battery_min_soc_kwh=1.0
    ).total_cost == pytest.approx(0.3 - 1.2)
    with pytest.raises(ValueError, match="battery_min_soc_kwh"):
        _one_hour(terminal_value_breakpoints=CURVE)  # the curve needs the real floor


def test_the_hourly_breakdown_refuses_costs_that_do_not_line_up():
    # test_the_hourly_breakdown_refuses_costs_that_do_not_line_up
    t0 = datetime(2026, 11, 8, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="same length"):
        hourly_regret_breakdown(
            timestamps=[t0, t0 + timedelta(hours=1)],
            actual_cost_per_period=np.zeros(2),
            oracle_cost_per_period=np.zeros(3),
        )
