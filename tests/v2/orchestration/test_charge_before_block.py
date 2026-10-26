"""Each plan publishes the charge it intends before the next P2P block, so a
plan that keeps deferring shows it (#1837).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
import solver_shared

KEYS = {"charge_before_block_kwh", "charge_before_block_pct", "next_block_start"}
START = datetime(2026, 10, 14, 0, 0, tzinfo=UTC)  # 10:00 AEST


def _times(n):
    return [(START + timedelta(hours=i)).isoformat() for i in range(n)]


def _fixed(n, block):
    fx = np.full(n, np.nan)
    for i in block:
        fx[i] = 11.5
    return fx


def test_a_block_already_running_points_at_the_next_one():
    # block now (0-1), gap (2-4) with charging, next block (5-6)
    charge = np.array([0, 0, 10, 10, 10, 0, 0], dtype=float)
    out = solver_shared.charge_before_block_summary(
        _fixed(7, [0, 1, 5, 6]), charge, [0.5] * 7, _times(7), 100.0
    )
    assert out["charge_before_block_kwh"] == 15.0
    assert out["next_block_start"].startswith("2026-10-14T15:00:00")


def test_period_hours_weight_the_energy():
    charge = np.array([40.0, 40.0, 0.0])
    out = solver_shared.charge_before_block_summary(
        _fixed(3, [2]), charge, [1 / 60, 5 / 60, 0.5], _times(3), 100.0
    )
    assert out["charge_before_block_kwh"] == pytest.approx(4.0, abs=0.01)


def test_zero_capacity_leaves_the_percentage_out():
    out = solver_shared.charge_before_block_summary(
        _fixed(3, [2]), np.array([5.0, 5.0, 0.0]), [1.0] * 3, _times(3), 0.0
    )
    assert out["charge_before_block_kwh"] == 10.0
    assert out["charge_before_block_pct"] is None
