"""Household, 14 Oct 2026: "the plan in the morning was showing charging and
yes it kept waiting". The Solver tab's plan line records only period 0, so a
plan that held its charge for later drew 0 all morning. Each plan now
publishes the charge it intends before the next P2P block; its history shows
a plan that keeps deferring at a glance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
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


def test_charge_before_the_next_block():
    # 10:00-20:00 AEST hourly; block 17:00-20:00 (indices 7-9)
    charge = np.array([0, 0, 0, 40, 40, 20, 0, 0, 0, 0], dtype=float)
    out = solver_shared.charge_before_block_summary(
        _fixed(10, range(7, 10)), charge, [1.0] * 10, _times(10), 120.0
    )
    assert out["charge_before_block_kwh"] == 100.0
    assert out["charge_before_block_pct"] == pytest.approx(83.3)
    assert out["next_block_start"].startswith("2026-10-14T17:00:00")


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


@pytest.mark.parametrize(
    "fixed",
    [None, np.full(5, np.nan), np.zeros(5), np.full(5, 11.5)],
    ids=["no_pins", "all_nan", "all_zero", "block_to_the_end"],
)
def test_no_next_block_means_all_none(fixed):
    out = solver_shared.charge_before_block_summary(
        fixed, np.zeros(5), [1.0] * 5, _times(5), 100.0
    )
    assert set(out) == KEYS
    assert all(v is None for v in out.values())


def test_zero_capacity_leaves_the_percentage_out():
    out = solver_shared.charge_before_block_summary(
        _fixed(3, [2]), np.array([5.0, 5.0, 0.0]), [1.0] * 3, _times(3), 0.0
    )
    assert out["charge_before_block_kwh"] == 10.0
    assert out["charge_before_block_pct"] is None
