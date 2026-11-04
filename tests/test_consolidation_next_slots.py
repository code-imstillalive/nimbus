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

    # test_a_source_with_no_price_in_the_slots_says_when_its_prices_start
    # (household, 2 Nov 2026: a day-2-to-7 source's row was all dashes).
    grid40 = _grid(40)
    late = [None] * 35 + [0.2] * 5
    out = pc.next_slots(
        {"primary": [0.1] * 40, "secondary_3": late}, [0.1] * 40, grid40, 1
    )
    assert out["starts"] == {"secondary_3": grid40[35].isoformat()}
    out = pc.next_slots({"secondary_3": [None] * 40}, [None] * 40, grid40, 1)
    assert out["starts"] == {}

    # test_the_rrp_forecast_is_aemos_own_where_it_reaches_then_the_regional
    # (#2190, Mark): AEMO NEM Data up to the end of its last 30-min row,
    # PD7DAY from there; no AEMO forecast leaves the regional one alone.
    aemo = [(T0, 0.07), (T0 + timedelta(minutes=30), 0.08)]
    pd7 = [(T0 + timedelta(minutes=30 * i), 0.05) for i in range(4)]
    joined, until = pc.join_rrp_forecasts(aemo, pd7)
    assert joined == aemo + pd7[2:]
    assert until == (T0 + timedelta(hours=1)).isoformat()
    assert pc.join_rrp_forecasts([], pd7) == (pd7, None)
