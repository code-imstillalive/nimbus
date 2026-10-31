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

    # test_the_settled_now_shows_its_settled_price_with_no_correction (#2190):
    # the blend for the running interval carried each source's learned
    # offset (9.84 against a settled 9.04 live); now it is the settled price.
    now = pc.now_slot(
        {"primary": [0.0904], "retail_from_rrp": [0.0905]}, [0.0984], grid, 0.0904
    )
    assert now["consolidated"] == 0.0904 and now["correction"] == 0.0
    assert now["spread"] == 0.0001  # the running interval's own spread

    # test_the_correction_row_is_what_the_offsets_added_to_the_sources_blend
    out = pc.next_slots(
        {"primary": [0.10, 0.11, 0.12]},
        [0.10, 0.118, 0.13],
        grid + _grid(3)[2:],
        1,
        [0.0, 0.008, 0.01],
    )
    assert out["correction"] == [0.008, 0.01]
    assert pc.next_slots({"primary": [0.1, 0.1]}, [0.1, 0.1], grid, 1)[
        "correction"
    ] == [None]
