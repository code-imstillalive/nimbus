"""Tracking fidelity: how faithfully a commanded kW was delivered (#283).

Until now `solver/tracking.py` was reached only through the daily reports,
and its empty-window answer by nothing (#1837's coverage-by-risk map).
These pin the formula in its own module docstring:

    TF = 1 - sum|commanded - actual| dt / sum|commanded| dt
"""

from __future__ import annotations

import numpy as np
from solver.tracking import compute_tracking_fidelity


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
