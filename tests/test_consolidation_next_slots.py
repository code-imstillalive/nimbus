"""Household with Mark Purcell, 19 Oct 2026 ("show the differences between
sources"): consolidate_side publishes each source's own price and the blend
for the next periods after the settled block, for the Solver tab's Price
sources card."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import _solver_path  # noqa: F401
from solver_inputs import price_consolidation as pc

T0 = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)


def _grid(n):
    return [T0 + timedelta(minutes=5 * i) for i in range(n)]


def test_now_with_nothing_settled_or_no_periods():
    grid = _grid(2)
    out = pc.now_slot({"primary": []}, [], grid, None)
    assert out["settled"] is None
    assert out["sources"] == {"primary": None}
    assert out["consolidated"] is None
    assert pc.now_slot({"primary": [0.2]}, [0.2], [], 0.2) == {}
