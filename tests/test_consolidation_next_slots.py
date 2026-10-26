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


def test_the_number_of_slots_is_capped():
    n = pc.NEXT_SLOTS + 10
    grid = _grid(n)
    series = {"primary": [0.2] * n}
    out = pc.next_slots(series, [0.2] * n, grid, 2)
    assert len(out["times"]) == pc.NEXT_SLOTS
    assert all(len(v) == pc.NEXT_SLOTS for v in out["sources"].values())
    assert len(out["consolidated"]) == pc.NEXT_SLOTS


def test_a_short_series_pads_with_none():
    grid = _grid(4)
    out = pc.next_slots({"primary": [0.2, 0.3]}, [0.2], grid, 0)
    assert out["sources"]["primary"] == [0.2, 0.3, None, None]
    assert out["consolidated"] == [0.2, None, None, None]


def test_now_with_nothing_settled_or_no_periods():
    grid = _grid(2)
    out = pc.now_slot({"primary": []}, [], grid, None)
    assert out["settled"] is None
    assert out["sources"] == {"primary": None}
    assert out["consolidated"] is None
    assert pc.now_slot({"primary": [0.2]}, [0.2], [], 0.2) == {}


def test_the_providers_now_is_the_settled_price_when_its_series_is_empty():
    """Live, 21 Oct 2026: the provider's series is masked to the periods its
    forecast covers, so the running interval's slot was None and the Now
    column showed a dash for the provider and no Gap."""
    grid = _grid(3)
    series = {"primary": [None, 0.3, 0.4], "retail_from_rrp": [0.1412, 0.3, 0.4]}
    out = pc.now_slot(series, [0.1537, 0.3, 0.4], grid, 0.1472)
    assert out["sources"] == {"primary": 0.1472, "retail_from_rrp": 0.1412}
    # A real provider price for now is kept as it is.
    series["primary"][0] = 0.15
    assert pc.now_slot(series, [0.1537], grid, 0.1472)["sources"]["primary"] == 0.15
