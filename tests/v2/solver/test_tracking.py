"""Tracking fidelity: how faithfully a commanded kW was delivered (#283).

Until now `solver/tracking.py` was reached only through the daily reports,
and its empty-window answer by nothing (#1837's coverage-by-risk map).
These pin the formula in its own module docstring:

    TF = 1 - sum|commanded - actual| dt / sum|commanded| dt
"""

from __future__ import annotations

import numpy as np
import pytest
from solver.tracking import compute_tracking_fidelity, tracking_error_cost


def _tf(hours, commanded, actual):
    return compute_tracking_fidelity(
        hours=np.asarray(hours, dtype=float),
        commanded_kw=np.asarray(commanded, dtype=float),
        actual_kw=np.asarray(actual, dtype=float),
    )


def test_an_empty_window_is_perfect_and_points_nowhere():
    r = _tf([], [], [])
    assert r.tracking_fidelity == 1.0
    assert r.n_samples == 0 and r.worst_gap_index == -1
    assert r.mean_absolute_error_kw == r.energy_shortfall_kwh == r.worst_gap_kw == 0.0


def test_nothing_commanded_is_nothing_to_fail_to_track():
    assert _tf([0.5, 0.5], [0.0, 0.0], [0.0, 0.3]).tracking_fidelity == 1.0


def test_the_inverter_handoff_dip():
    """2026-08-17: a steady 10 kW discharge whose measured power drops to
    near zero for one of four half-hours."""
    r = _tf([0.5] * 4, [10.0] * 4, [10.0, 0.4, 10.0, 10.0])
    assert r.tracking_fidelity == pytest.approx(1 - 9.6 * 0.5 / (10.0 * 2.0))
    assert r.mean_absolute_error_kw == pytest.approx(9.6 / 4)
    assert r.energy_shortfall_kwh == pytest.approx(4.8)
    assert (r.worst_gap_kw, r.worst_gap_index) == (pytest.approx(9.6), 1)
    assert r.n_samples == 4


def test_over_delivery_lowers_fidelity_but_is_no_shortfall():
    r = _tf([1.0, 1.0], [5.0, 5.0], [5.0, 7.0])
    assert r.tracking_fidelity == pytest.approx(1 - 2.0 / 10.0)
    assert r.energy_shortfall_kwh == 0.0


def test_a_controller_worse_than_doing_nothing_goes_negative():
    assert _tf([1.0], [5.0], [-6.0]).tracking_fidelity == pytest.approx(1 - 11 / 5)


def test_the_cost_prices_only_the_shortfall_at_each_periods_rate():
    cost = tracking_error_cost(
        hours=np.array([0.5, 0.5, 0.5]),
        commanded_kw=np.array([10.0, 10.0, 10.0]),
        actual_kw=np.array([0.4, 12.0, 9.0]),
        export_price=np.array([0.50, 0.50, 0.0]),
    )
    assert cost == pytest.approx(9.6 * 0.50 * 0.5)
